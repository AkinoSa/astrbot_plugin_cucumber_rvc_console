# CUSTOMIZATION — 个性化分支维护说明

本文件是 `astrbot_plugin_cucumber_rvc_console` 个性化分支的总入口。上游原版插件经本机持续定制，
所有改动都在这里登记；升级上游版本前，先读完本文和 `CHANGELOG_CUSTOM.md`。

## 当前修订版本

**CUSTOM-4**（基于上游 0.12.2，AstrBot 4.29.0 实测环境）

## 个性化改动总览（按主题）

| 主题 | 涉及文件 | 详细记录 |
|---|---|---|
| RVC 网关适配与 API 契约 | 网关侧 `<RVC 项目目录>/RVC-Project/gateway`（不在本插件内） | CHANGELOG CUSTOM-1 |
| 自动选歌评分 + 原唱冒名降权 + 歌手中英别名 + 逐源选歌 + 指定版本尊重 | `main.py` `_auto_pick_song` / `_auto_pick_from_sources`、`source_router.py` | CHANGELOG CUSTOM-1~3 |
| 默认音色 / 模型列表修复 / 刷新保留选择 | `main.py` `_update_models_from_api`、`_conf_schema.json` | CHANGELOG CUSTOM-1~3 |
| 分离模型默认值与回退 | 网关 `gateway.py` + `main.py` `MSST_DEFAULT_MODEL` | CHANGELOG CUSTOM-1 |
| 模型性别映射（结构化下拉 + 命令） | `main.py` `_merge_model_genders` 等、`_conf_schema.json` `model_genders` | CHANGELOG CUSTOM-3 |
| 自动升降调与性别识别 | `main.py` `_detect_*` / `_calc_auto_key_shift` | CHANGELOG CUSTOM-1~3 |
| 20 秒默认片段 / 600 秒超时 | `_conf_schema.json` + 配置 | CHANGELOG CUSTOM-1 |
| 配置页重排（7 分组） | `_conf_schema.json` | CHANGELOG CUSTOM-3 |
| 嵌套配置卡片 + 配置适配层 + 旧配置迁移 | `_conf_schema.json`、`main.py` `_NestedConfig`/`_migrate_config_layout` | CHANGELOG CUSTOM-4 |
| 翻唱时长：候选 +5 秒缓冲 + 分离后精挑 | 网关 `gateway.py` `compute_clip_target` | CHANGELOG CUSTOM-4 |
| 模型性别合并为唯一来源 | `main.py` `_load_model_genders` / `_write_model_gender` | CHANGELOG CUSTOM-4 |
| 日志与错误处理 | `main.py` 全局 | CHANGELOG CUSTOM-3 |

## 分工边界（哪些不在本插件目录）

- **RVC Gateway**（`rvc-gateway` 容器，端口 3333）：
  `<RVC 项目目录>/RVC-Project/gateway/gateway.py`，有自己的备份
  `.bak-20261029-deploy-gateway.py`、`.bak-20261029-001512` 等。
  插件与网关的契约：`/convert`（含 `msst_model` 回退语义）、`/model_info`、
  `/cache_info`、`/clear_cache`、`/show_msst_models`、`/select_msst_model`。
- **音色/索引数据**（只读挂载）：`<RVC 数据目录>/rvc/weights`、`<RVC 数据目录>/rvc/indices`。
  当前模型：deng.pth、nuoyemu.pth、rym.pth；索引：deng、rym（nuoyemu 缺 .index → 无索引推理）。


## 配置结构（CUSTOM-4 起）

配置 Schema 改为**两层 object 卡片**（AstrBot 4.29 原生支持，参考已验证的旧插件写法）：

```
basic            基础设置
selection        选歌与交互
  └ smart        对话与通知
rvc              RVC 音色与推理
  ├ models       音色与模型列表
  └ inference    推理参数
tuning           自动升降调与调音
  ├ shift        移调策略
  └ gender       性别配置（唯一入口）
separation       音频分离与输出
  └ output       输出
local_audio      本地音频翻唱（全部集中在这张卡片）
sources          音乐源（网易云）
advanced         缓存、诊断与高级选项
```

**代码读取方式**：`main.py` 里的 `CONFIG_PATHS` 把扁平键名映射到嵌套路径，
`_NestedConfig` 适配层负责读写（嵌套优先、回退旧扁平键），所以几十处
`config.get("扁平键")` 无需改动；命令里的 `self.config["键"] = 值` 会自动写入嵌套位置。

**旧配置迁移规则**（`_migrate_config_layout`，幂等）：
- 只把「嵌套里没有的」键补进去，**绝不覆盖**已有嵌套值；
- 迁移后 AstrBot 会按新 Schema 清除旧扁平键（日志 `Config key removed: ...`），属正常；
- 上一轮的 `model_gender_map` 旧列表会在首次启动时并入 `tuning.gender.model_genders` 后清空。

**刷新与保值**：
- 配置页没有「刷新模型」按钮（AstrBot 配置表单不提供插件动态 options API）；
  真实刷新入口是命令 `/刷新rvc模型`；
- 刷新只更新 `rvc.models.rvc_models_keywords`，不动默认音色、别名、性别映射；
- 后端返回空列表或异常时**直接失败返回**，绝不写空覆盖原配置；
- 默认音色带不带 `.pth` 都能匹配；失效时保留原值并提示。

**超时与取消**：性别识别的 LLM 请求超时会被 `wait_for` 取消，
Provider 抛出的 `CancelledError` 现在会被识别为「内部取消」并按未知性别继续翻唱，
不会再把整个后台任务杀掉；只有插件重载/取消命令这类外部取消才会中断任务（并如实记录「已取消」）。

**静音段保护**：分离后若人声电平低于 -50dBFS（挑中了间奏/尾奏），网关会自动换一段重试一次，
低于 -35dBFS（偏轻）会换段再试，最多 2 次，最后取人声最响的一段；全部低于 -50dBFS 才报错，而不是发静音文件。最终输出长度在「目标 ~ 目标+5 秒」内按内容取最优，
且带轻微长度偏好（不会总是取最长）。相关环境变量：`GW_VOICE_SILENT_DB`、`GW_SEP_SILENT_RETRY`、
`GW_LENGTH_PREFERENCE`。

**输出淡入淡出**：配置「输出首尾淡入淡出 0.5 秒」（默认开）；
网关在成品首尾各做 0.5 秒 afade，避免剪辑点突兀。网关不支持时自动跳过并记日志。

**成功提示（两个独立开关）**：
- 「成功时发送模板提示（含本次配置）」→ 固定文案 + “⚡命中缓存”提示；
- 「成功后让 LLM 用人设播报」→ 取当前会话人格与最近上下文，让 LLM 自然说一句（≤20 字）。
两者互不影响；只想要人格话就关模板、开 LLM。

**补发机制**：插件重载/中断会让进行中的翻唱任务被取消，但网关成品仍保留在
`<RVC 数据目录>/rvc/gateway_work/out/`。重载后用 `/补发翻唱` 列出最近成品，
或 `/补发翻唱 <task_id>` 直接补发，无需重跑推理。

**已知限制**：
- `model_genders` 的下拉条目是静态 Schema（当前 deng/nuoyemu/rym）。网关以后新增模型，
  用 `/设置模型性别 <模型名> <男声|女声>` 写入隐藏的 `model_genders_extra`（`type: dict`，
  不会被完整性检查删除），但在 WebUI 里看不到下拉位，需要更新 Schema 才有下拉。
- `inst_volume_db`、`shift_accompaniment` 当前网关不生效，已在界面标注。
- full（整首）模式被网关明确拒绝（资源不允许）。


## 插件页面（WebUI）

AstrBot 4.29 原生插件页面机制：

- 入口：`pages/<页面名>/index.html`（本插件为 `pages/dashboard/`），加载时自动扫描；
- 菜单标题：`.astrbot-plugin/i18n/<locale>.json` 里的 `pages.dashboard`；
- 后端：`page_api.py` 用 `context.register_web_api("/<插件名>/page/<路径>", handler, ["GET"], "说明")` 注册；
- **bridge 必须显式引入**：`<script src="/api/plugin/page/bridge-sdk.js"></script>`（AstrBot 会替换为带 token 的 URL），否则脚本先于 bridge 执行会拿不到对象；
- 前端：`window.AstrBotPluginView.apiGet/apiPost("page/<路径>")`，返回 `{status, data}`，
  页面里统一 `unwrap()`；
- 约束：不新增端口/容器；上传只写 `gateway_work/uploads`，结果只读 `gateway_work/out`。

改页面时：改 `pages/dashboard/*` 后浏览器强刷即可；改 `page_api.py` 或 `main.py` 需要重载插件。

## 备份存放约定（重要，避免插件加载失败）

**备份目录绝不能放在 `data/plugins/` 下**：AstrBot 会把 `plugins/` 里每个含 `main.py`
的文件夹当插件加载，而 `.bak-时间戳` 这种带点的目录名不是合法模块名，会报
`No module named 'data.plugins.<插件名>.bak-...'` 并显示「加载失败插件」。

统一放在扫描范围之外：

```
<AstrBot 数据目录>/astrbot/data/plugin_backups/
├── astrbot_plugin_cucumber_rvc_console.bak-20261029-014911/
├── astrbot_plugin_cucumber_rvc_console.bak-20261029-022435/
└── astrbot_plugin_cucumber_rvc_console.bak-20261029-custom4/
```

插件内部的 `.bak-*`（如 `astrbot_plugin_cucumber_rvc_console/.bak-xxx/`）不会被扫描，是安全的。

## 测试

`tests/test_custom_autopick.py` — 用 AstrBot 同款解释器运行（import 真实模块，不是文本提取）：

```bash
cd <AstrBot 数据目录>/astrbot/data/plugins
/vol1/@apphome/astrbot/penv/bin/python3 astrbot_plugin_cucumber_rvc_console/tests/test_custom_autopick.py
```

覆盖（43 项）：CUSTOM-3 全部回归（NameError、原唱/冒名/Jay 别名/版本降权/同名/指定版本）、
配置适配层读写与回退、扁平→嵌套迁移幂等与不覆盖、性别单一来源迁移与命令同源、
刷新保值（正常/空列表/异常/默认音色失效/.pth 差异）、Schema 类型与嵌套结构校验、
两入口共用公共选歌。网关切片时长（+5 缓冲）在容器内用 `compute_clip_target` 单测验证。

## 升级上游版本的操作顺序

1. 备份整个插件目录（`cp -a` 带时间戳）。
2. 阅读上游 changelog，识别 `main.py` / `source_router.py` / `_conf_schema.json` 的冲突点。
3. 按 `CHANGELOG_CUSTOM.md` 逐项把个性化改动重新落到新代码上（不要直接覆盖整个文件）。
4. `py_compile` 全部文件 + 跑 `tests/test_custom_autopick.py`。
5. 在 `_conf_schema.json` 顶部核对 `rvc_default_model.options` 是否仍是网关真实模型。
6. WebUI 重载插件，按 CHANGELOG 的「验证方式」逐项回归。
7. 在 `CHANGELOG_CUSTOM.md` 追加修订记录。

## 导出到本地电脑

```bash
# 在 NAS 上打包（排除敏感信息/缓存/模型）
ts=$(date +%Y%m%d)
tar --exclude='*.log' --exclude='__pycache__' --exclude='.bak-*' \
    --exclude='tests/__pycache__' \
    -czf /tmp/chorus_cover_custom_${ts}.tar.gz \
    -C <AstrBot 数据目录>/astrbot/data/plugins astrbot_plugin_cucumber_rvc_console
# 然后用 scp / fnOS 文件管理 把 tar 拷回本地电脑
```

包含：全部插件代码、Schema、本文档、变更记录、测试脚本、`config_template.json`（见
`docs/customization/README.md` 的说明）。**绝不包含**：`netease_cookie`、API token、
个人偏好文件、temp_audio、模型 .pth/.index、任何备份目录。
