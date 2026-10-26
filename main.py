"""Yuki 发照片 —— NovelAI 中转站生图，由 LLM 用自然语言触发。

设计要点
--------
1. 提示词由 bot 的 LLM 给，它手里有完整会话上下文，能写出贴合剧情的场景。
2. 角色固定特征（外形）和画师串、负面词写在插件配置里，LLM 只负责场景部分，
   这样长相才稳定。
3. 出图后走 tool_direct_result 直发路径：`event.set_result(...)` + `return None`。
4. 多 key：中转站没有余额查询接口，所以用「失败即切换 + 冷却」实现等效效果。
5. 两种视角：solo（只有 Yuki）和 pov（第一人称，能看到对方的手/身体局部）。
   pov 必须配 1boy，所以单/双人的前缀是分开的。
6. 附带一个本地 tag 参考库（从社区法典提取），LLM 不确定服装/姿势词时可以查。
"""

import asyncio
import base64
import io
import json
import os
import re
import shutil
import time
import zipfile

import aiohttp
from quart import jsonify, request

from astrbot.api import logger
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star
from astrbot.core.message.message_event_result import MessageChain

PLUGIN_NAME = "astrbot_plugin_nai_draw"
LOG_TAG = "[nai_draw]"

# nsfw 开关关闭时追加到负面词的屏蔽列表
_SFW_NEGATIVE = (
    "nsfw, nude, naked, topless, bottomless, nipples, areola, pussy, penis, "
    "sex, cum, cumdrip, pussy juice, undressing, lingerie, underwear only, "
    "cleavage, breasts out"
)


DATA_DIR_CANDIDATES = (
    "/AstrBot/data/plugin_data/astrbot_plugin_nai_draw",
    "/AstrBot/data/plugin_data/astrbot_plugin_yuki_draw",
    "/AstrBot/data/yuki_draw",
)

# 老版本用过的数据目录，首次启动时把里面的东西搬过来
LEGACY_DATA_DIRS = (
    "/AstrBot/data/plugin_data/astrbot_plugin_yuki_draw",
    "/AstrBot/data/yuki_draw",
)


def _pick_data_dir() -> str:
    """选一个可写的数据目录：优先标准插件数据目录，其次退回旧位置。"""
    for cand in DATA_DIR_CANDIDATES:
        try:
            os.makedirs(cand, exist_ok=True)
            probe = os.path.join(cand, ".write_test")
            with open(probe, "w", encoding="utf-8") as f:
                f.write("1")
            os.remove(probe)
            return cand
        except Exception:  # noqa: BLE001
            continue
    return "/AstrBot/data/yuki_draw"


DATA_DIR = _pick_data_dir()
BUNDLED_DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")

STATE_FILE = os.path.join(DATA_DIR, "key_state.json")
INDEX_FILE = os.path.join(DATA_DIR, "tag_index.json")
LIB_FILE = os.path.join(DATA_DIR, "tag_library.json")
RECIPE_MISS_FILE = os.path.join(DATA_DIR, "recipe_miss.log")

# 精准参考（Vibe Transfer）用
REF_DIR = os.path.join(DATA_DIR, "references")
PRESET_FILE = os.path.join(DATA_DIR, "reference_presets.json")
DEFAULT_SITE_BASE = "https://dangao.iisbo.com"

# 上游硬要求：精确参考的参考图必须正好是 1024x1536（实测其他尺寸一律「上游异常」）
REF_CANVAS_W, REF_CANVAS_H = 1024, 1536


def _seed_data_files() -> None:
    """把包内 data/ 的初始文件复制到 DATA_DIR（目标已存在则跳过）。"""
    for name in ("tag_library.json", "tag_index.json"):
        src = os.path.join(BUNDLED_DATA_DIR, name)
        dst = os.path.join(DATA_DIR, name)
        if not os.path.exists(src) or os.path.exists(dst):
            continue
        try:
            shutil.copy2(src, dst)
            logger.info(f"{LOG_TAG} 已释放初始数据: {name}")
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"{LOG_TAG} 释放 {name} 失败: {exc}")


# 兼容旧位置：老版本把数据放在别的目录，首次启动时迁过去
for _LEGACY_DIR in LEGACY_DATA_DIRS:
    if DATA_DIR == _LEGACY_DIR or not os.path.isdir(_LEGACY_DIR):
        continue
    for _old, _new in (
        ("tag_library.json", LIB_FILE),
        ("tag_index.json", INDEX_FILE),
    ):
        _src = os.path.join(_LEGACY_DIR, _old)
        if os.path.exists(_src) and not os.path.exists(_new):
            try:
                os.makedirs(DATA_DIR, exist_ok=True)
                shutil.copy2(_src, _new)
                logger.info(f"{LOG_TAG} 已从旧目录迁移: {_old}")
            except Exception:  # noqa: BLE001
                pass

    # 人设表与参考图目录也搬
    _old_presets = os.path.join(_LEGACY_DIR, "reference_presets.json")
    if os.path.isfile(_old_presets) and not os.path.exists(PRESET_FILE):
        try:
            os.makedirs(DATA_DIR, exist_ok=True)
            shutil.copy2(_old_presets, PRESET_FILE)
            logger.info(f"{LOG_TAG} 已从旧目录迁移: reference_presets.json")
        except Exception:  # noqa: BLE001
            pass
    _old_refs = os.path.join(_LEGACY_DIR, "references")
    if os.path.isdir(_old_refs) and not os.path.isdir(REF_DIR):
        try:
            shutil.copytree(_old_refs, REF_DIR)
            logger.info(f"{LOG_TAG} 已从旧目录迁移: references/")
        except Exception:  # noqa: BLE001
            pass

_seed_data_files()

# LLM 在 prompt 里追加的配方标记：[[配方:名字]]
# 只认这一种写法，避免误伤正常文字；中文冒号也兼容。
_RECIPE_RE = re.compile(r"\[\[\s*配方\s*[:：]?\s*([^\[\]]{1,40}?)\s*\]\]")


def _parse_size(text: str, fallback: tuple[int, int]) -> tuple[int, int]:
    try:
        w, h = str(text).lower().replace("×", "x").split("x")
        return int(w.strip()), int(h.strip())
    except Exception:
        return fallback


class NaiDraw(Star):
    def __init__(self, context: Context, config):
        super().__init__(context)
        cfg = dict(config or {})
        self.api_url = str(cfg.get("api_url") or "").strip()
        raw_keys = str(cfg.get("api_keys") or cfg.get("api_key") or "")
        self.api_keys = [k.strip() for k in raw_keys.replace(",", "\n").splitlines() if k.strip()]
        self.key_cooldown = int(cfg.get("key_cooldown_seconds") or 600)
        self.model = str(cfg.get("model") or "nai-diffusion-4-5-full").strip()
        self.artist = str(cfg.get("artist_string") or "").strip()
        self.extra_fixed = str(cfg.get("extra_tags") or "").strip()
        self.nsfw = bool(cfg.get("nsfw", True))
        self.quality = str(cfg.get("quality_tags") or "").strip()
        self._strip_nsfw_from_quality()
        self.negative = str(cfg.get("negative_prompt") or "").strip()
        self.steps = int(cfg.get("steps") or 28)
        self.scale = float(cfg.get("scale") or 6)
        self.sampler = str(cfg.get("sampler") or "k_dpmpp_2m_sde").strip()
        self.portrait = _parse_size(cfg.get("portrait_size"), (832, 1216))
        self.landscape = _parse_size(cfg.get("landscape_size"), (1216, 832))
        self.cooldown = int(cfg.get("cooldown_seconds") or 0)
        self.wait_message = str(cfg.get("wait_message") or "").strip()
        self.save_dir = DATA_DIR

        # ── 精准参考（站内 Vibe Transfer）──
        self.site_base = str(cfg.get("site_base_url") or DEFAULT_SITE_BASE).strip().rstrip("/")
        self.site_email = str(cfg.get("site_email") or "").strip()
        self.site_password = str(cfg.get("site_password") or "")
        self.precise_enabled = bool(cfg.get("precise_enabled", False))
        self.precise_strength = float(cfg.get("precise_strength") or 0.6)
        self.precise_mode = str(cfg.get("precise_mode") or "auto").strip().lower()
        self.precise_retries = int(cfg.get("precise_retries") or 3)
        self.active_preset = str(cfg.get("active_preset") or "").strip()
        self._site_token = ""
        self._site_token_exp = 0.0
        os.makedirs(REF_DIR, exist_ok=True)
        self._presets: dict[str, dict] = self._load_presets()

        # 自动补全表：LLM 写的词 -> 补上的 tag。每行 "原文 => tag1, tag2"
        self.auto_map: list[tuple[str, str]] = []
        for line in str(cfg.get("auto_tag_map") or "").splitlines():
            if "=>" not in line:
                continue
            k, v = line.split("=>", 1)
            k, v = k.strip().lower(), v.strip()
            if k and v:
                self.auto_map.append((k, v))

        self._native_config = config if hasattr(config, "save_config") else None
        self._last_sent: dict[str, float] = {}
        self._key_state: dict[str, dict] = {}
        self._index: dict | None = None
        self._recipe_map: tuple[dict, dict] | None = None
        self._load_key_state()

        logger.info(
            f"[nai_draw] 已配置 {len(self.api_keys)} 个 key，失败冷却 {self.key_cooldown}s，"
            f"自动补全词条 {len(self.auto_map)} 条"
        )
        self._load_recipes()  # 预热配方表，启动日志里就能看到条数
        self._register_web_api()

    def _register_web_api(self) -> None:
        """注册 WebUI 配置页用到的接口。"""
        base = f"/{PLUGIN_NAME}"
        for path, fn, methods, desc in (
            ("config", self.web_get_config, ["GET"], "读取生图配置"),
            ("config", self.web_save_config, ["POST"], "保存生图配置"),
            ("test", self.web_test, ["POST"], "测试中转站连通性"),
            ("stats", self.web_stats, ["GET"], "读取用量统计"),
            ("presets", self.web_get_presets, ["GET"], "列出精准参考人设"),
            ("preset/save", self.web_save_preset, ["POST"], "新增或修改人设"),
            ("preset/delete", self.web_delete_preset, ["POST"], "删除人设"),
            ("preset/activate", self.web_activate_preset, ["POST"], "设当前人设"),
            ("preset/upload", self.web_upload_ref, ["POST"], "上传参考图"),
            ("preset/remove", self.web_remove_ref, ["POST"], "删除参考图"),
            ("ref-image", self.web_ref_image, ["GET"], "读取参考图"),
            ("site-login", self.web_site_login, ["POST"], "测试站内登录"),
            ("precise-test", self.web_precise_test, ["POST"], "精准参考试跑"),
        ):
            try:
                self.context.register_web_api(f"{base}/{path}", fn, methods, desc)
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[nai_draw] 注册 Web API {path} 失败: {exc}")

    async def terminate(self):
        self._save_key_state()

    # ---------------- key 状态 ----------------
    def _load_key_state(self) -> None:
        try:
            with open(STATE_FILE, encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                self._key_state = data
        except FileNotFoundError:
            pass
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"{LOG_TAG} 读取 key 状态失败: {exc}")

    def _save_key_state(self) -> None:
        try:
            os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
            with open(STATE_FILE, "w", encoding="utf-8") as f:
                json.dump(self._key_state, f, ensure_ascii=False)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"{LOG_TAG} 保存 key 状态失败: {exc}")

    def _state_of(self, key: str) -> dict:
        st = self._key_state.get(key)
        if not isinstance(st, dict):
            st = {}
        st.setdefault("count", 0)
        st.setdefault("cool_until", 0.0)
        st.setdefault("last_err", "")
        self._key_state[key] = st
        return st

    def _candidates(self) -> list[str]:
        now = time.time()
        usable = []
        for key in self.api_keys:
            st = self._state_of(key)
            if st["cool_until"] > now:
                continue
            usable.append((st["count"], key))
        usable.sort(key=lambda x: x[0])
        return [k for _, k in usable]

    @staticmethod
    def _mask(key: str) -> str:
        return key[:8] + "…" + key[-4:] if len(key) > 14 else key[:6] + "…"

    def _cooldown_report(self) -> str:
        now = time.time()
        parts = []
        for i, key in enumerate(self.api_keys, 1):
            st = self._state_of(key)
            left = int(st["cool_until"] - now)
            state = f"冷却中({left}s)" if left > 0 else "可用"
            parts.append(f"#{i} {state} 累计{st['count']}张")
        return " / ".join(parts)

    # ---------------- tag 参考库 ----------------
    def _load_index(self) -> dict:
        if self._index is not None:
            return self._index
        try:
            with open(INDEX_FILE, encoding="utf-8") as f:
                self._index = json.load(f)
            logger.info(
                f"{LOG_TAG} tag 索引已加载："
                f"R18 {len(self._index.get('r18', []))} 条 / "
                f"构图 {len(self._index.get('comp', []))} 条"
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"{LOG_TAG} tag 索引加载失败，查询功能不可用: {exc}")
            self._index = {}
        return self._index

    def _search_index(self, query: str, k: int, solo_only: bool = True) -> list[dict]:
        idx = self._load_index()
        terms: list[tuple[str, int]] = []   # (词, 权重)
        for tok in re.split(r"[\s,，、/]+", query.strip().lower()):
            if not tok:
                continue
            terms.append((tok, 5))
            # 中文整句当 token 匹配不上时，拆成 2-gram 兜底。
            # 例：「蕾丝睡裙」拆成 蕾丝 / 丝睡 / 睡裙，就能命中「蕾丝饰边连衣裙」
            if re.search(r"[\u4e00-\u9fff]", tok) and len(tok) > 2:
                for i in range(len(tok) - 1):
                    terms.append((tok[i:i + 2], 2))
        if not terms:
            return []

        scored = []
        for bucket in ("r18", "comp"):
            for e in idx.get(bucket, []):
                if bucket == "r18" and solo_only and "1boy" in e["g"].lower():
                    continue
                title = e["t"].lower()
                path = e["p"].lower()
                tags = e["g"].lower()
                score = 0
                for term, weight in terms:
                    if term in title:
                        score += weight * 2
                    elif term in path:
                        score += weight
                    elif term in tags:
                        score += weight
                if score:
                    scored.append((score, bucket, e))

        scored.sort(key=lambda x: -x[0])
        out = []
        seen = set()
        for _, bucket, e in scored:
            key = e["g"][:60]
            if key in seen:
                continue
            seen.add(key)
            out.append({"src": bucket, "title": e["t"], "path": e["p"], "tags": e["g"]})
            if len(out) >= k:
                break
        return out

    # ---------------- 提示词拼装 ----------------
    def _auto_extra(self, prompt: str) -> list[str]:
        """LLM 写了口语描述时，按 auto_tag_map 补上对应的标准 tag。"""
        low = prompt.lower()
        seen: list[str] = []
        for phrase, tags in self.auto_map:
            if phrase not in low:
                continue
            for t in tags.split(","):
                t = t.strip()
                key = t.lower()
                if not t:
                    continue
                if key not in {s.lower() for s in seen}:
                    seen.append(t)
        return seen

    def _strip_nsfw_from_quality(self) -> None:
        """quality_tags 里的 nsfw 由开关统一管理，避免重复注入。"""
        toks = [t.strip() for t in self.quality.split(",") if t.strip()]
        kept = [t for t in toks if t.lower() != "nsfw"]
        if len(kept) != len(toks):
            self.quality = ", ".join(kept)

    def _effective_negative(self) -> str:
        """实际使用的负面词：nsfw 关闭时叠加 SFW 屏蔽列表。"""
        if self.nsfw:
            return self.negative
        parts = [self.negative, _SFW_NEGATIVE]
        return ", ".join(p.strip().strip(",") for p in parts if p and p.strip())

    def _build_input(self, prompt: str, extra_tags: list[str] | None = None) -> str:
        """拼装最终提示词：画师串 + 固定 tag + 场景 + 补全 + 画质。

        不注入任何构图前缀 —— 画面怎么构成完全由 prompt 决定。
        """
        written = {self._norm_name(seg) for seg in prompt.split(",")}
        written.discard("")
        extra: list[str] = []
        for t in list(extra_tags or []) + self._auto_extra(prompt):
            t = str(t).strip()
            key = self._norm_name(t)
            if not t or not key or key in written:
                continue
            if key in {self._norm_name(x) for x in extra}:
                continue
            extra.append(t)
        parts = [self.artist, self.extra_fixed, prompt, ", ".join(extra),
                 self.quality, "nsfw" if self.nsfw else ""]
        return ", ".join(p.strip().strip(",") for p in parts if p and p.strip())

    # ---------------- 配方库（性爱配方 / 配方别名） ----------------
    @staticmethod
    def _norm_name(text: str) -> str:
        """名字归一：剥掉权重外壳、括号注释、空格、间隔号，统一小写。"""
        s = re.sub(r"[+-]?[\d.]+\s*::", "", str(text or ""))
        s = s.replace("::", "")
        s = s.strip("{}[]() ")
        s = re.sub(r"[（(].*?[)）]", "", s)
        s = re.sub(r"[\s·・_\-]+", "", s)
        return s.lower()

    def _load_recipes(self) -> tuple[dict, dict]:
        """加载 tag_library.json 的配方与别名表，只读一次。

        返回 (recipes, alias)：
          recipes[归一名] = (原名, tag 串)
          alias[归一口语] = 目标（配方原名或直接是 tag 串）
        """
        if self._recipe_map is not None:
            return self._recipe_map
        recipes: dict[str, tuple[str, str]] = {}
        alias: dict[str, str] = {}
        try:
            with open(LIB_FILE, encoding="utf-8") as f:
                data = json.load(f)
            for cat, bucket in (data.get("cats") or {}).items():
                if not isinstance(bucket, dict):
                    continue
                for name, tags in bucket.items():
                    if not isinstance(tags, str) or not tags.strip():
                        continue
                    key = self._norm_name(name)
                    if not key:
                        continue
                    if cat == "配方别名":
                        alias[key] = tags.strip()
                    elif cat.startswith("配方") or cat.startswith("性爱配方"):
                        recipes[key] = (name, tags.strip())
            logger.info(f"{LOG_TAG} 配方库已加载：{len(recipes)} 条配方 / {len(alias)} 条别名")
        except FileNotFoundError:
            logger.info(f"{LOG_TAG} 未找到 tag_library.json，配方点名功能跳过")
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"{LOG_TAG} 配方库加载失败: {exc}")
        self._recipe_map = (recipes, alias)
        return self._recipe_map

    def _resolve_recipe(self, token: str):
        """名字 -> (原名, tags)。找不到返回 None。

        顺序：精确 -> 别名 -> 唯一子串。命中多条视为歧义，不猜。
        """
        recipes, alias = self._load_recipes()
        key = self._norm_name(token)
        if not key or not recipes:
            return None
        if key in recipes:
            return recipes[key]
        target = alias.get(key)
        if target:
            tk = self._norm_name(target)
            if tk in recipes:
                return recipes[tk]
            # 别名值本身就是一串 tag
            return (token.strip(), target)
        hits = [v for k, v in recipes.items() if key in k or k in key]
        if len(hits) == 1:
            return hits[0]
        return None

    @staticmethod
    def _log_recipe_miss(name: str) -> None:
        """没点中的配方名落盘，方便回头补名字或别名。"""
        try:
            os.makedirs(os.path.dirname(RECIPE_MISS_FILE), exist_ok=True)
            with open(RECIPE_MISS_FILE, "a", encoding="utf-8") as f:
                f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')}\t{name}\n")
        except Exception:  # noqa: BLE001
            pass
        logger.info(f"{LOG_TAG} 配方未命中：{name}")

    def _expand_recipes(self, prompt: str) -> tuple[str, list[str]]:
        """把 prompt 里的 [[配方:名字]] 换成对应 tag 串。

        返回 (清理后的 prompt, 追加 tag 列表)。
        """
        recipes, alias = self._load_recipes()
        if not recipes and not alias:
            return prompt, []

        extra: list[str] = []

        def _add(tags: str) -> None:
            for t in tags.split(","):
                t = t.strip()
                key = self._norm_name(t)
                if not key or key in written:
                    continue
                if key not in {self._norm_name(x) for x in extra}:
                    extra.append(t)

        # 第一遍：把标记摘出来并清掉，prompt 变干净后再统计「已经写过的 tag」
        hits: list[tuple[str, str]] = []

        def _sub(m):
            name = m.group(1).strip()
            hit = self._resolve_recipe(name)
            if hit is None:
                self._log_recipe_miss(name)
            else:
                hits.append(hit)
                logger.info(f"{LOG_TAG} 配方命中：{hit[0]}（{len(hit[1].split(','))} 个 tag）")
            return ""

        prompt = _RECIPE_RE.sub(_sub, prompt).strip(" ,，")

        written = {self._norm_name(seg) for seg in prompt.split(",")}
        written.discard("")

        for _, tags in hits:
            _add(tags)

        # 兜底：LLM 没用标记，但 prompt 里出现了别名口语
        flat = self._norm_name(prompt)
        for akey, target in alias.items():
            if akey and akey in flat:
                hit = self._resolve_recipe(akey)
                if hit:
                    _add(hit[1])

        return prompt, extra

    async def _generate_with_key(
        self,
        key: str,
        prompt: str,
        orientation: str,
        extra_tags: list[str] | None = None,
    ) -> str:
        w, h = self.landscape if orientation.lower().startswith("land") else self.portrait
        full_input = self._build_input(prompt, extra_tags)
        # 打日志：出问题时能直接看到实际发了什么，不用猜
        logger.info(f"{LOG_TAG} 发送 prompt ({len(full_input)} 字符): {full_input}")
        payload = {
            "input": full_input,
            "model": self.model,
            "action": "generate",
            "parameters": {
                "width": w,
                "height": h,
                "scale": self.scale,
                "steps": self.steps,
                "sampler": self.sampler,
                "negative_prompt": self._effective_negative(),
            },
        }
        headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
        timeout = aiohttp.ClientTimeout(total=180)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(self.api_url, json=payload, headers=headers) as resp:
                body = await resp.read()
                if resp.status != 200:
                    text = body[:300].decode("utf-8", "replace")
                    raise RuntimeError(f"HTTP {resp.status}: {text}")

        if body[:2] == b"PK":
            with zipfile.ZipFile(io.BytesIO(body)) as zf:
                names = [n for n in zf.namelist() if n.lower().endswith(".png")]
                if not names:
                    raise RuntimeError("ZIP 里没有 PNG")
                data = zf.read(names[0])
        elif body[:8] == b"\x89PNG\r\n\x1a\n" or body[:2] == b"\xff\xd8":
            data = body
        else:
            raise RuntimeError(f"无法识别的返回（前 8 字节 {body[:8].hex()}）")

        os.makedirs(self.save_dir, exist_ok=True)
        path = os.path.join(self.save_dir, f"{int(time.time() * 1000)}.png")
        await asyncio.to_thread(lambda: open(path, "wb").write(data))
        return path

    def _cleanup(self) -> None:
        try:
            files = sorted(
                (os.path.join(self.save_dir, f) for f in os.listdir(self.save_dir)
                 if f.lower().endswith(".png")),
                key=os.path.getmtime,
                reverse=True,
            )
            for old in files[40:]:
                os.remove(old)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"{LOG_TAG} 清理旧图失败: {exc}")

    # ---------------- 工具 1：查 tag ----------------
    @filter.llm_tool(name="lookup_nai_tags")
    async def lookup_nai_tags(self, event: AstrMessageEvent, query: str):
        """查本地 NovelAI tag 参考库（11,716 条人物/场景 + 4,291 条构图）。

        写 prompt 时不确定某个词该用什么英文 tag，先用这个词查一下。

        query 用英文命中率最高（库里的 tag 是英文）：
          "lingerie" / "negligee" / "kneeling" / "spread legs" / "shibari" / "cowboy shot"

        中文也能查，但只在库里有对应中文名时才准：
          "蕾丝睡裙" / "自慰" / "情趣内衣" / "骑乘"

        查不到就换个词，或者直接照你自己的描述写 —— 不要因为查不到就不出图。

        返回最多 5 条，每条给中文名 + 标签路径 + 可直接复制的英文 tag 串。
        """
        try:
            hits = self._search_index(query, 5, False)
        except Exception as exc:  # noqa: BLE001
            logger.error(f"{LOG_TAG} tag 查询失败: {exc}")
            return f"查询失败：{exc}"

        if not hits:
            return f"没查到「{query}」。换个说法，或者直接用英文 tag 试（例如 negligee / kneeling / spread legs）。"

        lines = []
        for i, h in enumerate(hits, 1):
            lines.append(f"{i}. {h['title']}〔{h['path']}〕")
            lines.append(f"   {h['tags'][:220]}")
        return "\n".join(lines)

    # ---------------- 工具 2：出图 ----------------
    @filter.llm_tool(name="generate_image")
    async def generate_image(
        self,
        event: AstrMessageEvent,
        prompt: str,
        orientation: str = "portrait",
    ):
        """生成一张图片并直接发送到当前会话。

        prompt 用英文逗号分隔的 danbooru 风格 tag 写。

        **写什么**：
          · 主体 —— 几个人、什么特征（1girl / 2girls / 1boy, 1girl …）
          · 场景 —— 在哪（bedroom / classroom / park / beach …）
          · 状态 —— 穿着什么，或赤裸到哪一步
          · 姿势 —— 身体怎么摆、手在哪、视线往哪看
          · 表情 —— 情绪和神态
          · 光线 —— 光源位置和时间感

        示例：
          "1girl, solo, long black hair, purple eyes, oversized grey hoodie,
           sitting on bed, hand on own cheek, looking at viewer, soft smile,
           bedroom, warm afternoon sunlight"

        **构图完全由 prompt 决定**，插件不追加任何前缀：
          · 只要一个人 → 写 `solo`
          · 要两个人 → 写 `1boy, 1girl`
          · 第一人称视角 → 写 `pov`
          · 没有人 → 写 `no humans, scenery`

        **具体 tag 比模糊描述有效**：
          · 服装用标准 tag：negligee / hoodie / thighhighs / lace-trimmed_dress
          · 裸露用明确词：nude / topless / bottomless（不要写"衣服拉上去"）
          · 手部姿势从简：hand on own cheek / fingers spread / hand on own lap
            避免交叉手指、握持小物件、手朝镜头伸过来
          · 不确定的词用 lookup_nai_tags 查一下

        **配方点名**：tag_library.json 里有配方时，可以在 prompt 末尾加
        `[[配方:名字]]`，插件会把整串调好的 tag 拼进去（并自动去重）。
        配方库为空时这条被忽略。

        orientation: "portrait" 竖构图 / "landscape" 横构图

        出图约 10~15 秒。同一会话两次之间至少间隔冷却时间。
        """
        if not self._precise_ready():
            if not self.api_keys:
                return "出图失败：未配置 API Key（在插件配置页填写中转站地址和 key）"
            if not self.api_url:
                return "出图失败：未配置中转站地址（在插件配置页填写 api_url）"

        umo = str(event.unified_msg_origin or "")
        now = time.time()
        last = self._last_sent.get(umo, 0.0)
        if self.cooldown and now - last < self.cooldown:
            remain = int(self.cooldown - (now - last)) + 1
            return f"现在不能出图，{remain} 秒后再试"

        # 配方点名：把 [[配方:名字]] 换成库里那串 tag
        prompt, recipe_tags = self._expand_recipes(prompt)
        if not prompt:
            return "出图失败：prompt 为空（配方标记可能没解析出来）"

        self._last_sent[umo] = now

        if self.wait_message:
            try:
                await event.send(MessageChain().message(self.wait_message))
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"{LOG_TAG} 发送等待提示失败: {exc}")

        # 精准参考优先：开了开关、有账号、有人设图，就走站内 Vibe Transfer
        if self._precise_ready():
            try:
                path = await self._precise_generate(prompt, orientation, recipe_tags)
            except Exception as exc:  # noqa: BLE001
                logger.error(f"{LOG_TAG} 精准参考出图失败: {exc}")
                return f"出图失败（精准参考）：{str(exc)[:200]}"
            self._cleanup()
            event.set_result(event.image_result(path))
            return None

        candidates = self._candidates()
        if not candidates:
            logger.warning(f"{LOG_TAG} 所有 key 都在冷却：{self._cooldown_report()}")
            return "出图失败：所有 key 暂时不可用，稍后再试"

        errors = []
        for key in candidates:
            idx = self.api_keys.index(key) + 1
            try:
                path = await self._generate_with_key(key, prompt, orientation, recipe_tags)
            except Exception as exc:  # noqa: BLE001
                st = self._state_of(key)
                st["cool_until"] = time.time() + self.key_cooldown
                st["last_err"] = str(exc)[:160]
                self._save_key_state()
                errors.append(f"#{idx} {exc}")
                logger.warning(
                    f"{LOG_TAG} key #{idx} ({self._mask(key)}) 失败，"
                    f"冷却 {self.key_cooldown}s：{exc}"
                )
                continue

            st = self._state_of(key)
            st["count"] += 1
            st["last_err"] = ""
            self._save_key_state()
            logger.info(
                f"{LOG_TAG} key #{idx} ({self._mask(key)}) 出图完成，"
                f"该 key 累计 {st['count']} 张 ｜ {self._cooldown_report()}"
            )
            self._cleanup()
            event.set_result(event.image_result(path))
            return None

        logger.error(f"{LOG_TAG} 全部 key 失败：{errors}")
        return "出图失败：" + "；".join(errors[:2])

    # ---------------- 精准参考（Vibe Transfer）----------------
    def _load_presets(self) -> dict:
        try:
            with open(PRESET_FILE, encoding="utf-8") as f:
                d = json.load(f)
            if isinstance(d, dict):
                return {k: v for k, v in d.items() if isinstance(v, dict)}
        except FileNotFoundError:
            pass
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"{LOG_TAG} 人设表读取失败: {exc}")
        return {}

    def _save_presets(self) -> None:
        try:
            os.makedirs(os.path.dirname(PRESET_FILE), exist_ok=True)
            tmp = PRESET_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self._presets, f, ensure_ascii=False, indent=2)
            os.replace(tmp, PRESET_FILE)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"{LOG_TAG} 人设表保存失败: {exc}")

    def _preset_dir(self, name: str) -> str:
        safe = re.sub(r"[^\w\u4e00-\u9fa5.-]+", "_", str(name)).strip("._")[:40] or "_preset"
        d = os.path.join(REF_DIR, safe)
        os.makedirs(d, exist_ok=True)
        return d

    def _preset_image_paths(self, name: str) -> list[str]:
        p = self._presets.get(name) or {}
        out = []
        for fn in p.get("images") or []:
            fp = os.path.join(self._preset_dir(name), os.path.basename(str(fn)))
            if os.path.isfile(fp):
                out.append(fp)
        return out

    def _ref_image_b64(self, path: str) -> str:
        # 把参考图归一化到 1024x1536 再 base64。上游只认这个尺寸。
        raw = b""
        try:
            from PIL import Image

            with Image.open(path) as im:
                if im.size == (REF_CANVAS_W, REF_CANVAS_H):
                    with open(path, "rb") as f:
                        raw = f.read()
                else:
                    src = im.convert("RGBA")
                    src.thumbnail((REF_CANVAS_W, REF_CANVAS_H), Image.LANCZOS)
                    canvas = Image.new("RGB", (REF_CANVAS_W, REF_CANVAS_H), (0, 0, 0))
                    x = (REF_CANVAS_W - src.width) // 2
                    y = (REF_CANVAS_H - src.height) // 2
                    canvas.paste(src, (x, y), src)
                    buf = io.BytesIO()
                    canvas.save(buf, format="PNG")
                    raw = buf.getvalue()
                    try:
                        with open(path, "wb") as f:
                            f.write(raw)
                    except Exception:  # noqa: BLE001
                        pass
                    logger.info(
                        f"{LOG_TAG} 参考图已归一化 1024x1536: {os.path.basename(path)}"
                    )
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"{LOG_TAG} 参考图归一化失败，按原图发送: {exc}")
            with open(path, "rb") as f:
                raw = f.read()
        return base64.b64encode(raw).decode()

    def _active_preset_name(self) -> str:
        if self.active_preset and self._preset_image_paths(self.active_preset):
            return self.active_preset
        for n in self._presets:
            if self._preset_image_paths(n):
                return n
        return ""

    def _precise_ready(self) -> bool:
        if not self.precise_enabled:
            return False
        if not (self.site_email and self.site_password):
            return False
        return bool(self._active_preset_name())

    async def _site_login(self, force: bool = False) -> str:
        # 站内登录取令牌，缓存 6 小时
        if not (self.site_email and self.site_password):
            raise RuntimeError("未配置站内账号（site_email / site_password）")
        if not force and self._site_token and time.time() < self._site_token_exp:
            return self._site_token
        payload = {"email": self.site_email, "password": self.site_password, "remember_me": 30}
        timeout = aiohttp.ClientTimeout(total=40)
        async with aiohttp.ClientSession(timeout=timeout) as sess:
            async with sess.post(f"{self.site_base}/api/login", json=payload) as r:
                raw = await r.read()
                if r.status != 200:
                    raise RuntimeError(
                        f"站内登录失败 HTTP {r.status}: {raw[:200].decode('utf-8', 'replace')}"
                    )
                d = json.loads(raw)
        tok = str(d.get("token") or "").strip()
        if not tok:
            raise RuntimeError("站内登录未返回 token")
        self._site_token = tok
        self._site_token_exp = time.time() + 6 * 3600
        user = d.get("user") or {}
        logger.info(f"{LOG_TAG} 站内登录成功: {user.get('username') or user.get('email')}")
        return tok

    async def _precise_generate(self, prompt: str, orientation: str,
                                extra_tags: list[str] | None = None) -> str:
        # 走站内 Vibe Transfer：参考图提取特征，构图由 prompt 决定
        name = self._active_preset_name()
        if not name:
            raise RuntimeError("没有可用的人设参考图")
        paths = self._preset_image_paths(name)
        refs = [self._ref_image_b64(fp) for fp in paths]
        if not refs:
            raise RuntimeError(f"人设「{name}」的参考图都读不到")

        preset = self._presets.get(name) or {}
        strength = float(preset.get("strength") or self.precise_strength)
        w, h = self.landscape if str(orientation).lower().startswith("land") else self.portrait
        full_input = self._build_input(prompt, extra_tags)
        neg = self._effective_negative()

        mode = str(self.precise_mode or "auto").lower()
        tries = max(1, int(self.precise_retries or 1))
        logger.info(
            f"{LOG_TAG} 精准参考出图 人设={name} 参考图={len(refs)} 强度={strength} "
            f"尺寸={w}x{h} 模式={mode} 重试={tries} prompt={len(full_input)}字符"
        )

        base = {
            "width": w, "height": h,
            "scale": self.scale, "steps": self.steps, "sampler": self.sampler,
            "negative_prompt": neg,
            "v4_prompt": {
                "caption": {"base_caption": full_input, "char_captions": []},
                "use_coords": False, "use_order": True,
            },
            "v4_negative_prompt": {
                "caption": {"base_caption": neg, "char_captions": []},
                "legacy_uc": False,
            },
            "legacy_uc": False,
            "prefer_brownian": True,
            "deliberate_euler_ancestral_bug": False,
        }

        def vibe_params() -> dict:
            p = dict(base)
            p.update({
                "reference_image_multiple": refs,
                "reference_information_extracted_multiple": [1.0] * len(refs),
                "reference_strength_multiple": [strength] * len(refs),
                "normalize_reference_strength_multiple": True,
            })
            return p

        def director_params() -> dict:
            # 精确参考：上游成功率不稳，靠重试补
            p = dict(base)
            p.update({
                "director_reference_images": refs,
                "director_reference_strength_values": [strength] * len(refs),
                "director_reference_secondary_strength_values": [0.3] * len(refs),
                "director_reference_information_extracted": [1] * len(refs),
                "director_reference_descriptions": [
                    {"caption": {"base_caption": "character&style", "char_captions": []},
                     "legacy_uc": False}
                    for _ in refs
                ],
            })
            return p

        order: list[tuple[str, dict]] = []
        if mode in ("director", "auto"):
            order.append(("精确参考", director_params()))
        if mode in ("vibe", "auto") or not order:
            order.append(("氛围参考", vibe_params()))

        errors: list[str] = []
        for mi, (label, params) in enumerate(order):
            for i in range(1, tries + 1):
                try:
                    path = await self._submit_and_wait(full_input, params, label, i, tries)
                    if mi > 0:
                        logger.warning(f"{LOG_TAG} 前面的「{order[0][0]}」全部失败，已回退到「{label}」")
                    return path
                except Exception as exc:  # noqa: BLE001
                    errors.append(f"{label}#{i}: {str(exc)[:110]}")
                    logger.warning(f"{LOG_TAG} {label} 第 {i}/{tries} 次失败: {str(exc)[:150]}")
                    if i < tries:
                        await asyncio.sleep(min(8, 2 + i * 2))
        raise RuntimeError("；".join(errors[-3:]) or "精准参考出图失败")

    async def _submit_and_wait(self, full_input: str, params: dict,
                               label: str, attempt: int, tries: int) -> str:
        body = {"model": self.model, "input": full_input,
                "action": "generate", "parameters": params}
        tok = await self._site_login()
        hdr = {"Authorization": f"Bearer {tok}", "Content-Type": "application/json"}
        timeout = aiohttp.ClientTimeout(total=120)
        async with aiohttp.ClientSession(timeout=timeout) as sess:
            async with sess.post(f"{self.site_base}/api/novelai/generate",
                                 json=body, headers=hdr) as r:
                raw = await r.read()
                if r.status != 200:
                    raise RuntimeError(
                        f"提交失败 HTTP {r.status}: {raw[:160].decode('utf-8', 'replace')}"
                    )
                q = json.loads(raw)
        tid = str(q.get("task_id") or "").strip()
        if not tid:
            raise RuntimeError(f"未拿到 task_id: {raw[:160].decode('utf-8', 'replace')}")
        logger.info(
            f"{LOG_TAG} [{label} {attempt}/{tries}] 已排队 {tid} "
            f"eta={q.get('queue', {}).get('eta_seconds')}s"
        )

        img_url = ""
        deadline = time.time() + 240
        while time.time() < deadline:
            await asyncio.sleep(3)
            try:
                async with aiohttp.ClientSession(
                    timeout=aiohttp.ClientTimeout(total=60)
                ) as sess:
                    async with sess.get(f"{self.site_base}/api/novelai/task/{tid}",
                                        headers={"Authorization": f"Bearer {tok}"}) as r:
                        d = json.loads(await r.read())
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"{LOG_TAG} 轮询异常（继续等）: {exc}")
                continue
            st = str(d.get("status") or "")
            if st == "failed":
                raise RuntimeError(str(d.get("error") or "生成失败")[:200])
            res = d.get("result") or {}
            if res.get("images"):
                img_url = str(res.get("final_image") or res["images"][0].get("url") or "")
                break
        if not img_url:
            raise RuntimeError("等待生成超时（240 秒）")

        timeout = aiohttp.ClientTimeout(total=120)
        async with aiohttp.ClientSession(timeout=timeout) as sess:
            async with sess.get(f"{self.site_base}{img_url}",
                                headers={"Authorization": f"Bearer {tok}"}) as r:
                data = await r.read()
                if r.status != 200:
                    raise RuntimeError(f"下载图片失败 HTTP {r.status}")

        path = os.path.join(self.save_dir, f"{int(time.time() * 1000)}.png")
        with open(path, "wb") as f:
            f.write(data)
        logger.info(
            f"{LOG_TAG} 精准参考出图完成 [{label}] {len(data)} 字节 -> {os.path.basename(path)}"
        )
        return path

    # ---------------- 精准参考 WebUI 接口 ----------------
    def _preset_public(self) -> list[dict]:
        active = self._active_preset_name()
        out = []
        for name, p in self._presets.items():
            imgs = self._preset_image_paths(name)
            out.append({
                "name": name,
                "strength": float(p.get("strength") or self.precise_strength),
                "count": len(imgs),
                "images": [os.path.basename(x) for x in imgs],
                "active": name == active,
            })
        out.sort(key=lambda x: (not x["active"], x["name"]))
        return out

    async def web_get_presets(self):
        return jsonify({
            "success": True,
            "presets": self._preset_public(),
            "enabled": self.precise_enabled,
            "active": self._active_preset_name(),
            "has_account": bool(self.site_email and self.site_password),
            "default_strength": self.precise_strength,
            "mode": self.precise_mode,
            "retries": self.precise_retries,
        })

    async def web_save_preset(self):
        try:
            d = await request.get_json() or {}
            name = str(d.get("name") or "").strip()[:40]
            if not name:
                return jsonify({"success": False, "message": "人设名不能为空"}), 400
            old = str(d.get("rename_from") or "").strip()
            if old and old != name:
                if name in self._presets:
                    return jsonify({"success": False, "message": f"人设「{name}」已存在"}), 400
                p = self._presets.pop(old, None)
                if p is None:
                    return jsonify({"success": False, "message": "要改名的人设不存在"}), 400
                src, dst = self._preset_dir(old), self._preset_dir(name)
                for fn in p.get("images") or []:
                    a, b = os.path.join(src, fn), os.path.join(dst, fn)
                    if os.path.isfile(a) and not os.path.exists(b):
                        try:
                            shutil.move(a, b)
                        except Exception as exc:  # noqa: BLE001
                            logger.warning(f"{LOG_TAG} 迁移参考图失败 {fn}: {exc}")
                self._presets[name] = p
            p = self._presets.setdefault(name, {"images": []})
            p.setdefault("images", [])
            if "strength" in d:
                try:
                    p["strength"] = max(0.0, min(1.0, float(d["strength"])))
                except Exception:  # noqa: BLE001
                    pass
            self._save_presets()
            return jsonify({"success": True, "message": "已保存"})
        except Exception as exc:  # noqa: BLE001
            logger.error(f"{LOG_TAG} 保存人设失败: {exc}")
            return jsonify({"success": False, "message": f"保存失败：{exc}"}), 500

    async def web_delete_preset(self):
        try:
            d = await request.get_json() or {}
            name = str(d.get("name") or "").strip()
            if name not in self._presets:
                return jsonify({"success": False, "message": "人设不存在"}), 400
            self._presets.pop(name, None)
            self._save_presets()
            try:
                shutil.rmtree(self._preset_dir(name), ignore_errors=True)
            except Exception:  # noqa: BLE001
                pass
            return jsonify({"success": True, "message": "已删除"})
        except Exception as exc:  # noqa: BLE001
            return jsonify({"success": False, "message": f"删除失败：{exc}"}), 500

    async def web_activate_preset(self):
        try:
            d = await request.get_json() or {}
            name = str(d.get("name") or "").strip()
            if name and name not in self._presets:
                return jsonify({"success": False, "message": "人设不存在"}), 400
            self.active_preset = name
            cfg = getattr(self, "_native_config", None)
            if cfg is not None:
                try:
                    cfg["active_preset"] = name
                    cfg.save_config()
                except Exception as exc:  # noqa: BLE001
                    logger.warning(f"{LOG_TAG} 当前人设未落盘: {exc}")
            return jsonify({"success": True, "message": f"当前人设：{name or '（无）'}"})
        except Exception as exc:  # noqa: BLE001
            return jsonify({"success": False, "message": f"切换失败：{exc}"}), 500

    async def web_upload_ref(self):
        try:
            d = await request.get_json() or {}
            name = str(d.get("preset") or "").strip()
            if name not in self._presets:
                return jsonify({"success": False, "message": "先选一个人设"}), 400
            raw = str(d.get("data_b64") or "")
            if "," in raw and raw.lstrip().startswith("data:"):
                raw = raw.split(",", 1)[1]
            if not raw:
                return jsonify({"success": False, "message": "没有图片数据"}), 400
            blob = base64.b64decode(raw)
            if not blob:
                return jsonify({"success": False, "message": "图片为空"}), 400
            if len(blob) > 12 * 1024 * 1024:
                return jsonify({"success": False, "message": "图片超过 12MB"}), 400
            fn = str(d.get("filename") or "ref.png").strip()
            ext = os.path.splitext(fn)[1].lower()
            if ext not in (".png", ".jpg", ".jpeg", ".webp"):
                ext = ".png"
            if not (blob[:8] == b"\x89PNG\r\n\x1a\n" or blob[:2] == b"\xff\xd8"
                    or blob[:4] == b"RIFF"):
                return jsonify({"success": False, "message": "只收 PNG / JPEG / WebP"}), 400
            out = f"{int(time.time() * 1000)}{ext}"
            fp_new = os.path.join(self._preset_dir(name), out)
            with open(fp_new, "wb") as f:
                f.write(blob)
            # 立刻归一化成 1024x1536，省得每次出图都算
            self._ref_image_b64(fp_new)
            p = self._presets[name]
            p.setdefault("images", []).append(out)
            self._save_presets()
            logger.info(f"{LOG_TAG} 人设「{name}」新增参考图 {out} ({len(blob)} 字节)")
            return jsonify({"success": True, "message": f"已添加（{len(p['images'])} 张）"})
        except Exception as exc:  # noqa: BLE001
            logger.error(f"{LOG_TAG} 上传参考图失败: {exc}")
            return jsonify({"success": False, "message": f"上传失败：{exc}"}), 500

    async def web_remove_ref(self):
        try:
            d = await request.get_json() or {}
            name = str(d.get("preset") or "").strip()
            fn = os.path.basename(str(d.get("filename") or ""))
            p = self._presets.get(name)
            if not p or fn not in (p.get("images") or []):
                return jsonify({"success": False, "message": "找不到这张图"}), 400
            p["images"].remove(fn)
            self._save_presets()
            try:
                os.remove(os.path.join(self._preset_dir(name), fn))
            except Exception:  # noqa: BLE001
                pass
            return jsonify({"success": True, "message": "已删除"})
        except Exception as exc:  # noqa: BLE001
            return jsonify({"success": False, "message": f"删除失败：{exc}"}), 500

    async def web_ref_image(self):
        from quart import Response

        name = str(request.args.get("preset") or "")
        fn = os.path.basename(str(request.args.get("file") or ""))
        fp = os.path.join(self._preset_dir(name), fn)
        if not (name and fn and os.path.isfile(fp)):
            return Response(b"", status=404)
        ext = os.path.splitext(fn)[1].lower()
        mime = {".png": "image/png", ".jpg": "image/jpeg",
                ".jpeg": "image/jpeg", ".webp": "image/webp"}.get(ext, "application/octet-stream")
        with open(fp, "rb") as f:
            return Response(f.read(), mimetype=mime)

    async def web_site_login(self):
        try:
            tok = await self._site_login(force=True)
            return jsonify({"success": True,
                            "message": f"站内登录成功，令牌 {len(tok)} 字符"})
        except Exception as exc:  # noqa: BLE001
            return jsonify({"success": False, "message": str(exc)[:300]}), 500

    async def web_precise_test(self):
        # 用当前人设跑一次最小出图，验证整条链路
        try:
            d = await request.get_json(silent=True) or {}
            prompt = str(d.get("prompt") or "").strip()
            if not prompt:
                prompt = ("1girl, solo, long wavy hair, maid outfit, white apron, "
                          "sitting at an outdoor cafe table, holding a teacup, "
                          "looking at viewer, gentle smile, warm afternoon sunlight, best quality")
            if not self._precise_ready():
                return jsonify({
                    "success": False,
                    "message": "还没就绪：需要开启精准参考、填站内账号、并有人设参考图",
                }), 400
            path = await self._precise_generate(prompt, "portrait", None)
            with open(path, "rb") as f:
                b64 = base64.b64encode(f.read()).decode()
            name = self._active_preset_name()
            return jsonify({"success": True,
                            "message": f"出图成功（人设：{name}，{len(b64) * 3 // 4} 字节）",
                            "image_b64": b64})
        except Exception as exc:  # noqa: BLE001
            logger.error(f"{LOG_TAG} 精准参考试跑失败: {exc}")
            return jsonify({"success": False, "message": str(exc)[:300]}), 500

    # ---------------- WebUI 接口 ----------------
    def _config_for_page(self) -> dict:
        """给配置页的当前值（密钥掩码）。"""
        masked = []
        for k in self.api_keys:
            masked.append(self._mask(k))
        return {
            "api_url": self.api_url,
            "api_keys": "\n".join(masked),
            "key_count": len(self.api_keys),
            "key_cooldown_seconds": self.key_cooldown,
            "model": self.model,
            "steps": self.steps,
            "scale": self.scale,
            "sampler": self.sampler,
            "portrait_size": "%dx%d" % self.portrait,
            "landscape_size": "%dx%d" % self.landscape,
            "artist_string": self.artist,
            "extra_tags": self.extra_fixed,
            "nsfw": self.nsfw,
            "quality_tags": self.quality,
            "negative_prompt": self.negative,
            "cooldown_seconds": self.cooldown,
            "wait_message": self.wait_message,
            "auto_tag_map": "\n".join(f"{k} => {v}" for k, v in self.auto_map),
        }

    def _precise_config_for_page(self) -> dict:
        return {
            "site_base_url": self.site_base,
            "site_email": self.site_email,
            "site_password": "***" if self.site_password else "",
            "precise_enabled": self.precise_enabled,
            "precise_strength": self.precise_strength,
            "precise_mode": self.precise_mode,
            "precise_retries": self.precise_retries,
            "active_preset": self.active_preset,
        }

    async def web_get_config(self):
        cfg = self._config_for_page()
        cfg.update(self._precise_config_for_page())
        return jsonify({"success": True, "config": cfg})

    async def web_save_config(self):
        try:
            data = await request.get_json()
            if not isinstance(data, dict):
                return jsonify({"success": False, "message": "请求体必须是 JSON 对象"}), 400
            cfg = getattr(self, "_native_config", None)
            if cfg is None:
                return jsonify({"success": False, "message": "插件配置对象不可写，请到 AstrBot 插件配置页修改"}), 400

            url = str(data.get("api_url") or "").strip()
            if url:
                cfg["api_url"] = url

            keys_raw = str(data.get("api_keys") or "").strip()
            if keys_raw:
                # 掩码行（含 …）不覆盖真实 key
                lines = [x.strip() for x in keys_raw.splitlines() if x.strip()]
                real = [x for x in lines if "…" not in x]
                if real:
                    cfg["api_keys"] = "\n".join(real)

            if "key_cooldown_seconds" in data:
                cfg["key_cooldown_seconds"] = int(data["key_cooldown_seconds"] or 600)
            if "nsfw" in data:
                cfg["nsfw"] = bool(data["nsfw"])
            if "precise_enabled" in data:
                cfg["precise_enabled"] = bool(data["precise_enabled"])
            if "precise_strength" in data:
                cfg["precise_strength"] = float(data["precise_strength"] or 0.6)
            if "precise_mode" in data and isinstance(data["precise_mode"], str):
                cfg["precise_mode"] = data["precise_mode"]
            if "precise_retries" in data:
                cfg["precise_retries"] = max(1, min(10, int(data["precise_retries"] or 3)))
            if str(data.get("site_password") or "") not in ("", "***"):
                cfg["site_password"] = str(data["site_password"])
            for f in ("site_base_url", "site_email"):
                if f in data and isinstance(data[f], str):
                    cfg[f] = data[f]
            for f in ("model", "sampler", "artist_string", "extra_tags",
                      "quality_tags", "negative_prompt", "wait_message"):
                if f in data and isinstance(data[f], str):
                    cfg[f] = data[f]
            for f in ("steps", "cooldown_seconds"):
                if f in data:
                    cfg[f] = int(data[f] or 0)
            if "scale" in data:
                cfg["scale"] = float(data["scale"] or 6)
            if "auto_tag_map" in data and isinstance(data["auto_tag_map"], str):
                cfg["auto_tag_map"] = data["auto_tag_map"]

            cfg.save_config()
            self._reload_from(cfg)
            logger.info(f"{LOG_TAG} WebUI 已保存配置并热重载")
            return jsonify({"success": True, "message": "已保存并生效（无需重启）"})
        except Exception as exc:  # noqa: BLE001
            logger.error(f"{LOG_TAG} 保存配置失败: {exc}")
            return jsonify({"success": False, "message": f"保存失败：{exc}"}), 500

    async def web_test(self):
        """用第一个可用 key 打一次最小请求，验证地址与 key。"""
        try:
            data = await request.get_json(silent=True) or {}
            url = str(data.get("api_url") or self.api_url).strip()
            keys = [x.strip() for x in str(data.get("api_keys") or "").splitlines() if x.strip()]
            keys = [k for k in keys if "…" not in k] or self.api_keys
            if not url or not keys:
                return jsonify({"success": False, "message": "地址和 key 都要填"}), 400

            payload = {
                "input": "1girl, solo, simple background",
                "model": self.model,
                "action": "generate",
                "parameters": {
                    "width": 512, "height": 512,
                    "scale": 5, "steps": 10,
                    "sampler": self.sampler,
                    "negative_prompt": "lowres, bad anatomy",
                },
            }
            t0 = time.time()
            timeout = aiohttp.ClientTimeout(total=120)
            async with aiohttp.ClientSession(timeout=timeout) as s:
                async with s.post(url, json=payload,
                                  headers={"Authorization": f"Bearer {keys[0]}",
                                           "Content-Type": "application/json"}) as r:
                    body = await r.read()
                    ms = int((time.time() - t0) * 1000)
                    if r.status != 200:
                        txt = body[:300].decode("utf-8", "replace")
                        return jsonify({"success": False,
                                        "message": f"HTTP {r.status}（{ms}ms）：{txt}"})
                    if body[:2] == b"PK" or body[:8] == b"\x89PNG\r\n\x1a\n" or body[:2] == b"\xff\xd8":
                        return jsonify({"success": True,
                                        "message": f"连通正常（{ms}ms，返回 {len(body):,} 字节）"})
                    return jsonify({"success": False,
                                    "message": f"能连通但返回格式不认识（{ms}ms，前 8 字节 {body[:8].hex()}）"})
        except Exception as exc:  # noqa: BLE001
            return jsonify({"success": False, "message": f"连接失败：{exc}"}), 500

    async def web_stats(self):
        pairs = []
        now = time.time()
        for i, key in enumerate(self.api_keys, 1):
            st = self._state_of(key)
            left = int(st["cool_until"] - now)
            pairs.append({
                "index": i,
                "mask": self._mask(key),
                "count": st["count"],
                "cooldown": left if left > 0 else 0,
                "last_err": st.get("last_err", ""),
            })
        return jsonify({"success": True, "keys": pairs})

    def _reload_from(self, cfg) -> None:
        """保存后热重载内存里的配置。"""
        self.api_url = str(cfg.get("api_url") or "").strip()
        raw = str(cfg.get("api_keys") or "")
        self.api_keys = [k.strip() for k in raw.replace(",", "\n").splitlines() if k.strip()]
        self.key_cooldown = int(cfg.get("key_cooldown_seconds") or 600)
        self.model = str(cfg.get("model") or self.model).strip()
        self.site_base = str(cfg.get("site_base_url") or DEFAULT_SITE_BASE).strip().rstrip("/")
        self.site_email = str(cfg.get("site_email") or "").strip()
        if str(cfg.get("site_password") or "") not in ("", "***"):
            self.site_password = str(cfg.get("site_password"))
        self.precise_enabled = bool(cfg.get("precise_enabled", False))
        self.precise_strength = float(cfg.get("precise_strength") or 0.6)
        self.precise_mode = str(cfg.get("precise_mode") or "auto").strip().lower()
        self.precise_retries = int(cfg.get("precise_retries") or 3)
        self.active_preset = str(cfg.get("active_preset") or "").strip()
        self._site_token = ""
        self._site_token_exp = 0.0
        self.artist = str(cfg.get("artist_string") or "").strip()
        self.extra_fixed = str(cfg.get("extra_tags") or "").strip()
        self.nsfw = bool(cfg.get("nsfw", True))
        self.quality = str(cfg.get("quality_tags") or "").strip()
        self._strip_nsfw_from_quality()
        self.negative = str(cfg.get("negative_prompt") or "").strip()
        self.steps = int(cfg.get("steps") or 28)
        self.scale = float(cfg.get("scale") or 6)
        self.sampler = str(cfg.get("sampler") or self.sampler).strip()
        self.portrait = _parse_size(cfg.get("portrait_size"), (832, 1216))
        self.landscape = _parse_size(cfg.get("landscape_size"), (1216, 832))
        self.cooldown = int(cfg.get("cooldown_seconds") or 0)
        self.wait_message = str(cfg.get("wait_message") or "").strip()
        self.auto_map = []
        for line in str(cfg.get("auto_tag_map") or "").splitlines():
            if "=>" not in line:
                continue
            k, v = line.split("=>", 1)
            k, v = k.strip().lower(), v.strip()
            if k and v:
                self.auto_map.append((k, v))
