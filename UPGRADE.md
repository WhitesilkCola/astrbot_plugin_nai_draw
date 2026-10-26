# 升级说明（1.0.0 → 1.1.0）

## 一句话

**只替换插件目录，配置和 plugin_data 不要动。数据会自动迁移。**

## 路径对照

```
/AstrBot/data/plugins/astrbot_plugin_nai_draw/              ← 替换这里
/AstrBot/data/config/astrbot_plugin_nai_draw_config.json    ← 别动
/AstrBot/data/plugin_data/...                               ← 别动
```

## 步骤

### ① 备份

```bash
cp -a /AstrBot/data/plugins/astrbot_plugin_nai_draw \
      /AstrBot/data/plugins/astrbot_plugin_nai_draw.bak-1.0.0
cp -a /AstrBot/data/config/astrbot_plugin_nai_draw_config.json /tmp/
```

Windows 直接把 `astrbot_plugin_nai_draw` 文件夹复制一份改名 `astrbot_plugin_nai_draw.bak-1.0.0` 即可。

### ② 替换

```bash
cd /AstrBot/data/plugins
rm -rf astrbot_plugin_nai_draw
unzip astrbot_plugin_nai_draw-1.1.0.zip -d /AstrBot/data/plugins/
```

Windows：删掉旧文件夹，把新的 `astrbot_plugin_nai_draw` 放进去。

### ③ 重启

重启 AstrBot，或在 WebUI 插件页点「重载」。

## 会看到什么

**日志里的数据迁移：**

```
[nai_draw] 已从旧目录迁移: tag_library.json
[nai_draw] 已从旧目录迁移: tag_index.json
[nai_draw] 已从旧目录迁移: reference_presets.json   ← 1.0.0 没有，跳过
[nai_draw] 已从旧目录迁移: references/               ← 同上
```

**原目录保留，不会删。**

**配置自动补全：**

1.0.0 有 16 个配置项，1.1.0 有 25 个。加载时：

```
原有 16 项   → 原样保留（接口地址 / API Key / 画师串 / 负面词 / 偏好…）
新增 9 项    → 自动填默认值
```

**所以不用重填任何东西。**

## 验证清单

- [ ] 插件页版本显示 `1.1.0`
- [ ] 设置页能翻到「精准参考（人设）」整块
- [ ] 原有配置还在（接口地址、API Key、画师串、负面词）
- [ ] 普通出图还能用
- [ ] 日志出现迁移行（如果数据在旧目录）

## 这次修了什么

**如果你 1.0.0 时觉得「配方点名老是不准」—— 那是 bug，已修。**
配方别名被当成配方读了（`154 条配方 / 0 条别名` → 现在是 `110 / 45`）。

详见 `CHANGELOG.md`。

## 出问题怎么回退

```bash
cd /AstrBot/data/plugins
rm -rf astrbot_plugin_nai_draw
mv astrbot_plugin_nai_draw.bak-1.0.0 astrbot_plugin_nai_draw
# 重启
```

**配置和 plugin_data 全程没动，回退就是回到原状。**

## 插件被禁用的情况

如果 1.0.0 加载失败过，AstrBot 会自动标记禁用。换完代码后日志若出现：

```
Plugin astrbot_plugin_nai_draw is disabled.
```

**在 WebUI 插件页点一下开关启用即可。**

## 新增功能怎么用

**精准参考：设置页 →「精准参考（人设）」**

```
1. 填站内地址 / 站内邮箱（只支持 @qq.com）/ 站内密码
2. 点「测试站内登录」
3. 新建人设，上传 1~3 张参考图（立绘或三视图最好）
4. 打开「启用」开关
```

**参考机制选 `auto`（默认）或 `vibe`。**

**云服务器上建议直接选 `vibe`** —— 精确参考档位对客户端 IP 敏感，
云服务器常常命中不了上游线路，`auto` 会一直重试再回退，白白多等几十秒。
