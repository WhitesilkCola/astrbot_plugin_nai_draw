# NovelAI 生图（AstrBot 插件）

让 bot 会出图。

LLM 在聊天里自己判断什么时候该画，写 prompt，插件调中转站生成并直接发出来。
不需要命令、不需要关键词、不需要手动传参。

---

## 特性

| | |
|---|---|
| **自然语言触发** | LLM 自己决定何时出图，你正常聊天就行 |
| **构图自由** | 插件**不追加任何前缀** —— 一个人、两个人、风景、第一人称视角，全由 prompt 决定 |
| **NSFW 开关** | 一个开关同时控制正向的 `nsfw` 和负面的一整套遮挡词 |
| **多 key 轮换** | 一个 key 报错自动冷却并切换，按累计用量均衡 |
| **配方库** | 附带 915 条精选 tag / 110 条场景配方，LLM 可点名整串 tag |
| **本地 tag 查询** | 1.6 万条社区法典索引，不确定的词可自查 |
| **自带设置页** | 装完在 AstrBot 插件页点进去填地址和 key，不用改配置 JSON |

---

## 环境要求

```
AstrBot >= 4.26，< 5
一个 NovelAI 格式的生图中转站
```

**接口约定**（插件按这个格式发请求，两种返回都支持）：

```http
POST {api_url}
Authorization: Bearer {api_key}
Content-Type: application/json

{
  "input": "完整的 prompt 字符串",
  "model": "nai-diffusion-4-5-full",
  "action": "generate",
  "parameters": {
    "width": 832, "height": 1216,
    "scale": 6, "steps": 32,
    "sampler": "k_dpmpp_2m_sde",
    "negative_prompt": "..."
  }
}
```

**返回**：ZIP 包（内含 PNG）或裸 PNG / JPEG。

---

## 安装

1. 把整个 `astrbot_plugin_nai_draw/` 目录放进：

   ```
   /AstrBot/data/plugins/astrbot_plugin_nai_draw/
   ```

2. 重启 AstrBot。

3. 打开 AstrBot WebUI → 插件 → **NovelAI 生图** → 点进设置页，
   填 **接口地址** 和 **API Key**，点「测试连接」确认通，再点「保存并生效」。

**数据文件不用手动放** —— 首次启动会把包内 `data/` 下的配方库和 tag 索引
自动释放到：

```
/AstrBot/data/plugin_data/astrbot_plugin_nai_draw/
```

已存在就不覆盖，升级不会丢你的改动。

---

## 设置页

插件自带一个页面（`pages/settings/`），分四块：

```
中转站      接口地址 / API Key / 失败冷却 / 测试连接 / Key 状态表
生成参数    模型 / 步数 / CFG / 采样器 / 竖横尺寸
画风与内容  画师串 / 固定正向 tag / 画质构图词 / 负面词
行为        口语翻译表 / 冷却 / 等待提示语
```

**保存后立即生效，不用重启。**

**Key 状态表**能直接看到每个 key 累计出了多少张、有没有在冷却、最后一次报什么错。

**API Key 回显是掩码的** —— 只想改别的配置时留空不动它，要换 key 就整行覆盖。

---

## 换角色

插件出厂**不带任何角色**：

```
extra_tags   空
artist_string 空
```

要固定一个角色，把外形写进 **`extra_tags`**：

```
1girl, solo focus, 20 years old, long black hair, purple eyes,
1.4::two horns::, 1.5::tail with heart at the tip::, slim body
```

**写法建议**（这些是踩出来的）：

- 出过错的特征用数值权重钉住：`1.4::two horns::`
- **同一个特征不要用两套描述** —— `curved horns` + `purple horns` 会画成四个角
- 长复合词拆开更准：`horns on forehead hairline` 比 `horns on head` 准
- **画面里要不要人，写在 prompt 里**，`extra_tags` 只放外形

---

## 构图

插件**不注入 `solo` 或任何前缀**。想画成什么样，在 prompt 里写：

```
一个人        solo
两个人        1boy, 1girl
多人          2girls, 1boy
第一人称      pov
风景          no humans, scenery
```

**唯一强制的是 `action: generate` 和尺寸** —— 其他全部由 prompt 决定。

---

## NSFW 开关

设置页「画风与内容」第一项就是一个开关，**同时管两头**：

| | 开启（默认） | 关闭 |
|---|---|---|
| 正向词末尾 | 追加 `nsfw` | 不追加 |
| 负面词 | 用你配的那份 | 叠加一整套遮挡词 |

**关闭时叠加的负面词：**

```
nsfw, nude, naked, topless, bottomless, nipples, areola, pussy, penis,
sex, cum, cumdrip, pussy juice, undressing, lingerie, underwear only,
cleavage, breasts out
```

**两点注意：**

```
① quality_tags 里不要再写 nsfw —— 由开关统一管理，
   插件启动时会把 quality_tags 里的 nsfw 自动剔掉，避免重复注入。

② 关掉之后配方库里那些露骨配方也会被压制 —— 配方本身带露骨 tag，
   和遮挡负面词会打架，结果是画面变糊。
   只想出普通图的话，把 tag_library.json 里的「性爱配方」两类删掉更干净。
```

---

## 配方库

`data/tag_library.json` 里 11 个分类、915 条：

```
服装 68 / 姿势-基础姿势 53 / 手臂姿势 77 / 腿部姿势与移动 97
手势 67 / 日常行为 116 / 物体互动 196 / 视角构图 86
性爱配方 77 / 性爱配方-pov 33 / 配方别名 45
```

**LLM 在 prompt 末尾写 `[[配方:名字]]`**，插件把整串调好的 tag 拼进去并去重。

**加新配方**：

```json
{
  "cats": {
    "性爱配方": { "我的新配方": "1girl, solo, 1.5::tag1::, {{tag2}}" },
    "配方别名": { "口语说法": "我的新配方" }
  }
}
```

**分类名以「配方」或「性爱配方」开头就当配方读，「配方别名」当别名表读。**

改完重启生效（配方表只在启动时读一次）。

**漏点追踪**：LLM 点了名但没匹配上，会记进 `recipe_miss.log`，方便补名字。
常点的那些值得加个别名。

**不想用配方库**：删掉 `tag_library.json` 即可，功能自动跳过。

---

## 两个工具

| 工具 | 作用 |
|---|---|
| `generate_image` | 出图并直发。参数 `prompt` / `orientation` |
| `lookup_nai_tags` | 查本地 tag 库。不确定服装/姿势词时用 |

---

## 排查

| 现象 | 处理 |
|---|---|
| 一直不出图 | 看日志有没有 `[nai_draw] 发送 prompt (...)`。没有说明 LLM 没调工具 |
| `出图失败：未配置…` | 设置页里地址或 key 没填 |
| `所有 key 暂时不可用` | 都进了冷却。设置页的 Key 状态表能看到各自最后一次错误 |
| 出图但和描述不符 | 看日志里完整的 prompt，对照 `口语翻译表` 补映射 |
| 某个部位画不出来 | 八成是**取景**问题不是 tag 问题。例：要露某个部位得让那个区域成为画面主体 |
| 画成多份 / 多个 | 同一特征写了多套描述，删到只剩一套 |
| 多余部件压不住 | 负面词加权：`{{{{{{pointed ears, elf ears}}}}}}` |
| 配方点名没生效 | 看 `recipe_miss.log`，名字对不上就加别名 |

**每次出图日志都会打完整 prompt**：

```
[nai_draw] 发送 prompt (972 字符): [[[artist:...]]], 1girl, solo, ...
```

**这是排查的唯一可靠依据** —— 先看实际发了什么，再改配置。

---

## 已知边界

- **手部出图率约 80~90%**。手势从简（五指张开、贴脸、放腿上），避免交叉手指、
  握持小物件、手朝镜头伸过来。
- **整套提示词约 800~1200 字符**，改动任何一块都会影响其他部分，一次只改一个变量。
- `tag_index.json` 有 5.5 MB，只给 `lookup_nai_tags` 用。删掉能省体积，功能会跳过。

---

## 文件结构

```
astrbot_plugin_nai_draw/
├── main.py                    插件主体
├── _conf_schema.json          配置项定义（AstrBot 通用配置页也读这份）
├── metadata.yaml              插件元信息
├── README.md                  本文件
├── pages/
│   └── settings/index.html    自带设置页
└── data/
    ├── tag_library.json       配方库 + 精选 tag（915 条）
    └── tag_index.json         社区法典索引（16,007 条）
```

---

## 声明

配方库与 tag 索引来自社区公开整理的 NovelAI 提示词库，仅供本地研究与个人使用。
使用前请自行确认所在地法律与目标服务的使用条款。
