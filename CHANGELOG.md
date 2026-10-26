# 更新日志

版本格式 `主版本.次版本.修订号`。每次改动记在这里，最新的在最上面。

---

## 1.1.0 — 2026-10-26

### 新增：精准参考

**给一套参考图，让模型认出这个角色，姿势和场景仍由提示词决定。** 这是 img2img 做不到的（img2img 会连构图一起抄）。

- **两种机制**
  - `精确参考`（Director Reference）：显式角色参考，`base_caption` 可选 character / style / character&style
  - `氛围参考`（Vibe Transfer）：把参考图编码成特征向量混合
- **`auto` 模式**：先试精确参考，重试 N 次都失败自动回退氛围参考，不会卡住
- **站内账号**：自动登录 `POST /api/login` 取令牌，缓存 6 小时
- **异步任务**：`POST /api/novelai/generate` 拿 `task_id`，轮询 `/api/novelai/task/{id}` 取图

### 新增：多套人设

- 设置页新增「精准参考（人设）」区块
- 人设可新建 / 切换 / 重命名 / 删除，每套单独设参考强度
- 每套可上传多张参考图（上限 100），全部一起作为参考
- 缩略图预览，单张可删
- 存储：`plugin_data/astrbot_plugin_nai_draw/references/<人设名>/`

### 新增：9 个配置项

| 配置 | 默认 | 说明 |
|---|---|---|
| `site_base_url` | `https://dangao.iisbo.com` | 站内根地址 |
| `site_email` | 空 | 只支持 QQ 邮箱 |
| `site_password` | 空 | 用于自动登录 |
| `precise_enabled` | `false` | 精准参考总开关 |
| `precise_strength` | `0.6` | 默认参考强度 |
| `precise_mode` | `auto` | auto / director / vibe |
| `precise_retries` | `3` | 精确参考重试次数 |
| `active_preset` | 空 | 当前人设（页面维护） |
| （`nsfw` 之外的字段见 `_conf_schema.json`） | | |

### 新增：9 个 Web 接口

```
GET  presets              列出人设 + 缩略图 + 当前项
POST preset/save          新建 / 改强度 / 改名
POST preset/delete        删人设及参考图
POST preset/activate      切当前人设
POST preset/upload        base64 上传参考图
POST preset/remove        删单张参考图
GET  ref-image            读参考图（走 dashboard cookie 鉴权）
POST site-login           测试站内登录
POST precise-test         试跑一次精准参考（调试用）
```

### 修复

- **配方别名被当成配方读**
  1.0.0 的 `_load_recipes` 用 `cat.startswith("配方")`，把「配方别名」分类也吃进配方表，
  导致 `154 条配方 / 0 条别名`。改为先判 `cat == "配方别名"`，现在是正确的 `110 / 45`。
  症状：配方点名经常命中不到，或命中到错的串。

- **`_strip_nsfw_from_quality` 调用顺序错误**
  插在 `self.quality` 赋值之前，插件启动直接抛
  `AttributeError: 'NaiDraw' object has no attribute 'quality'`，加载失败。
  已挪到赋值之后。

### 改进

- **数据目录改名**：`plugin_data/astrbot_plugin_yuki_draw` → `plugin_data/astrbot_plugin_nai_draw`。
  首次启动自动迁移 `tag_library.json` / `tag_index.json` / `reference_presets.json` / `references/`，
  原目录保留不删。

- **`_build_input` 精简**：只拼 `画师串 + 固定 tag + prompt + 补全 + 画质词`，
  不再注入任何构图前缀。

- **`lookup_nai_tags` 去掉 solo_only 过滤**，默认全库检索。

### 已知问题

- **精确参考对客户端 IP 敏感。** 同账号同参数：家用宽带 → 成功（`points_cost 6`）；
  云服务器 → 一直「上游生成服务暂时异常」。推测中转站有多条上游线路，部分不支持精确参考。
  **云服务器上建议直接把 `precise_mode` 设成 `vibe`，或留 `auto` 自动回退。**

- **精确参考上游本身不稳。** 即使线路可用也会间歇失败，靠 `precise_retries` 重试命中。
  失败不扣费，只有成功那一步按 `points_cost` 结算。

- **`information_extracted` 只接受整数 1**，传 0.5 会被参数校验拒绝（400）。

- **Director Reference 的参数形状**（供后续排查参考）：

```json
{
  "params_version": 3,
  "v4_prompt": {"caption": {"base_caption": "<正向>", "char_captions": []},
                "use_coords": false, "use_order": true},
  "v4_negative_prompt": {"caption": {"base_caption": "<负面>", "char_captions": []},
                         "legacy_uc": false},
  "legacy_uc": false,
  "director_reference_images": ["<裸 base64，不带 data: 前缀>"],
  "director_reference_strength_values": [0.75],
  "director_reference_secondary_strength_values": [0.0],
  "director_reference_information_extracted": [1],
  "director_reference_descriptions": [
    {"caption": {"base_caption": "character", "char_captions": []}, "legacy_uc": false}
  ]
}
```

  注意：`char_captions` 是**复数数组**不是 `char_caption: ""`；
  参考图是**裸 base64**，带 `data:image/png;base64,` 前缀会 503。
  这套形状是从中转站前端 JS 逐字扒出来的，缺 `params_version: 3` 会 400。

---

## 1.0.0 — 2026-10-24

### 首次发布

从角色专用插件（Yuki 自拍）改造成通用生图工具。

### 基础功能

- **自然语言触发**：LLM 自己判断何时出图，无需命令和关键词
- **构图自由**：不注入 `solo` 等任何前缀，构图完全由 prompt 决定
- **多 key 轮换**：某个 key 报错自动冷却并切换，按累计用量升序挑选
- **配方库**：110 条场景配方 + 45 条别名，LLM 可写 `[[配方:名字]]` 点名
- **本地 tag 查询**：16,007 条社区法典索引
- **自带设置页**：WebUI 里填地址和 key，保存即时生效

### 配置项（16 个）

```
api_url / api_keys / key_cooldown_seconds
model / steps / scale / sampler / portrait_size / landscape_size
artist_string / extra_tags / quality_tags / negative_prompt / auto_tag_map
cooldown_seconds / wait_message
```

### 出厂值

**不带任何角色**：`extra_tags` 空、`artist_string` 空、负面词只留通用解剖与画质问题。

### 两个工具

- `generate_image(prompt, orientation)` 出图并直发
- `lookup_nai_tags(query)` 查本地 tag 库

### 数据文件

- `data/tag_library.json`（60 KB）11 分类 915 条
- `data/tag_index.json`（5.47 MB）16,007 条
- 首次启动自动释放到 `plugin_data/`，已存在则跳过，升级不覆盖
