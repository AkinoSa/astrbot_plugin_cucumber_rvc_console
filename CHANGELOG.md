# 更新日志

本文件记录 **astrbot_plugin_cucumber_rvc_console**（副歌翻唱 · 自维护分支）的变更。
上游 `sdfsfsk/matsuko_cover` 的历史日志请见上游仓库，此处只记本分支的差异。

0.9.0 之前的条目保留发布当时的原文；其中提到的引擎、命令与配置已在 0.9.0 移除，
现在只剩 RVC 一条链路，以本文件最上方与 `README.md` 为准。

## v0.12.2（2026-10-28）

**列表不再伪装成「聊天记录」。** 之前 `/刷新rvc模型` 和选歌列表在 aiocqhttp 上会塞进一个
合并转发节点，节点里的发送者被写死成 `uin=1109587454, name="松子"`——那是上游作者留下的，
结果用户看到的是「聊天记录」，点开才是一串模型名，发送者还不是机器人自己。

### 变更

- `selection.py` 的 `send_list()`（模型列表）与选歌列表：去掉 `Node(...)` 合并转发，
  改成一条普通文字消息（`use_markdown(False)`）。QQ 官方机器人仍然走卡片/按钮那条路。
- 删掉随之无用的 `Node, Plain` 导入；全仓库已无 `Node(` 调用。
- 顺手清掉另一处上游残留文案：命中缓存时原本说「这首歌松子之前已经唱过啦」，
  改成「这首之前唱过」，不再冒充别的角色。
- 版本号 0.12.1 → 0.12.2。

## v0.12.1（2026-10-28）

**按插件市场审核意见修的两处合规问题。** 审核结论是「未发现恶意代码，但需修改后再上架」。

### 1. 日志：必须且只能 `from astrbot.api import logger`

- `outbound.py`：删掉 `import logging` 与 `logging.getLogger(__name__)`，改成
  `from astrbot.api import logger`（这个模块以前是唯一一处无条件自建 logger 的）。
- `netease_fetch.py`、`source_router.py`：删掉 `try/except` 里「脱离 AstrBot 就退回标准
  logging」的回退分支，统一 `from astrbot.api import logger`；`netease_fetch.py` 里
  随之无用的 `import logging` 也删了。
- 现在全仓库只剩 `from astrbot.api import logger` 这一种写法（`main.py` / `outbound.py` /
  `netease_fetch.py` / `source_router.py` / `selection.py` / `qq_callbacks.py`）。

### 2. 数据持久化：统一放到 `data/plugin_data/<插件名>/`

- 用户偏好：默认从 `data/user_preferences.json` 改为
  `data/plugin_data/astrbot_plugin_cucumber_rvc_console/user_preferences.json`。
  老配置里还是旧默认值的会被自动改写；如果旧文件存在而新位置没有，启动时**复制一份**过去
  （`shutil.copy2`），不删原文件，日志会记一行迁移记录。
- 临时音频：5 处 `os.path.join(os.path.dirname(__file__), "temp_audio")` 全部改成
  新的 `_temp_audio_dir()`，即 `data/plugin_data/astrbot_plugin_cucumber_rvc_console/temp_audio`；
  24 小时清理逻辑与 `_is_temp_audio()` 判断跟着一起走这个新目录。
- `_conf_schema.json` 的 `preference_storage_path` 默认值改为空字符串（空 = 用插件数据目录），
  hint 说明新默认路径和自动迁移。
- 新增模块级 `PLUGIN_NAME` 与 `_plugin_data_dir()`，用 AstrBot 的
  `get_astrbot_plugin_data_path()` 定位，脱离 AstrBot 跑单测时回退到 `./data/plugin_data`。
- 全仓库已无向插件代码目录写文件的代码（`os.path.dirname(__file__)` 的写操作 0 处）。

### 版本

- 0.12.0 → 0.12.1（`main.py` / `metadata.yaml` 同步）。

## v0.12.0（2026-10-28）

**模型目录从「读说明」变成「一条命令打开」。** 之前 `rvc_models_dir` 的提示写了四段、
又要用户自己去翻环境变量，实际没人看；而且漏了索引目录。

### 新增

- 管理员命令 **`/rvc模型文件夹`**：回显两个真实目录（音色 `.pth` 和索引 `.index`），
  并在跑机器人的这台机器上**直接打开模型文件夹**（Windows `os.startfile`），
  丢完文件再发 `/刷新rvc模型` 就能用。目录来自中间层 `/model_info`，不是插件猜的。
- `/刷新rvc模型` 和 `get_available_models` 的目录回显里补上 **索引目录**
  （`index_dirs`），不再只报音色目录。

### 变更

- `rvc_models_dir` 的提示从四段压成三句：留空用中间层目录 / 想看路径就发 `/rvc模型文件夹` /
  要换目录才去改 `CHORUS_WEIGHTS_DIR`。
- 抽出 `_fetch_models_info()`，`_update_models_from_api` 与打开文件夹命令共用同一套
  「`/model_info` 优先、老中间层退回 `/show_model`」逻辑。
- README「音色模型放哪」重写：一张表列出 `.pth` 和 `.index` 两个目录 + 一键打开命令。

### 吐槽点澄清

`rvc_models_dir` 本身**不是**让 RVC 换目录的开关——RVC 的 `get_vc()` 拼的是
`weight_root`，只有中间层的 `CHORUS_WEIGHTS_DIR` 能改。所以这个设置项降级成「核对用」，
真正好用的是新命令。

## v0.11.4（2026-10-28）

**补齐插件市场自动解析要读的字段。** AstrBot Cloud 现在会自动解析仓库里的
`metadata.yaml`，我们之前只写了六个基础键，于是发布预览页上「AstrBot 版本要求 / 支持平台 /
标签」三行全是横杠。

### 变更（只动 metadata.yaml）

- 新增 `astrbot_version: ">=4.16,<5"`（本机实测 AstrBot 4.29.3）。
- 新增 `tags`：音乐、RVC、翻唱、语音合成。
- 新增 `support_platforms`：`aiocqhttp`（QQ 个人号，走 OneBot v11）与 `qq_official`
  （插件里有 `qq_callbacks.py` 与 `qq_official_cards/buttons` 配置，官方机器人也走得通）。
- 版本号 0.11.3 → 0.11.4（`main.py` / `metadata.yaml` 同步）。

## v0.11.3（2026-10-28）

**上架插件市场前的准备。** 插件本身功能不变，只改了对外的描述和版本号。

- `metadata.yaml` 的 `desc` 重写：第一句说清「说一句翻唱就能出成品」，末尾明确写出
  **需要自备 RVC 整合包与 chorus-mw 中间层**——市场页面只显示这一行，不写清楚会有人装了
  发现连不上 3333。
- 版本号 0.11.2 → 0.11.3（`main.py` / `metadata.yaml` 同步）。
- 提交到 AstrBot Cloud 的字段：名称 `astrbot_plugin_cucumber_rvc_console`、显示名「RVC 副歌翻唱」、
  作者 `void-HaoNan`、仓库不带 `.git` 后缀。

## v0.11.2（2026-10-28）

**换图标。** 之前的 `logo.png` 是上游 `matsuko_cover` 的图，和本分支持久在做的事没关系。

### 变更

- `logo.png` 换成用户提供的新图（原图 AVIF 672×378，居中裁成 672→378 正方形后
  lanczos 缩放到 512×512，PNG RGB）。
- 版本号 → `0.11.2`（`metadata.yaml` / `main.py` / NOTICE 同步）。

### 说明

- AstrBot 认插件目录下的 `logo.png`（`star_manager.logo_fname`），文件名不变，不需要改任何配置。
- 图标按「居中正方形裁切」处理；原图是 16:9 横图，裁掉的左右两侧只是背景和手臂边缘。

## v0.11.1（2026-10-28）

**把配套中间层的仓库地址写进文档。** 之前 README 里 `chorus-mw` 被提到 7 次，却一次都没给
链接，读者只看到「配套中间层」四个字，不知道去哪拿——把它当成插件的一部分是必然的误解。

### 变更

- README 头部新增一条：**配套中间层（必需）** → <https://github.com/void-HaoNan/chorus-mw>，
  并写明「没有它插件连不上 3333，翻唱会直接失败」、插件自身不含任何分离 / 变声 / 混音代码。
- 「后端准备」一节开头点明：后端就是姊妹仓库 `chorus-mw`，不是随便一个 Gradio 服务；
  「本仓库配套的中间层」那一段的 `chorus-mw` 也加上了链接。
- `_conf_schema.json` 的 `rvc_base_url` 提示补全：写清它是中间层而不是 RVC 自带的 7897 WebUI、
  给出仓库地址，并解释为什么必须多这一层（插件 sse / RVC ws 协议不兼容，直连报
  `Unknown protocol: ws`）。
- NOTICE.md 增加「配套中间层（另一仓库）」一行与架构说明，版本号同步到 v0.11.1，
  并补记 0.11.0 / 0.11.1 的修改声明。

### 说明

- 中间层始终是**独立仓库、独立进程**（端口 3333），从未并入本插件；插件目录里没有
  `pipeline.py` / `chorus.py` / `rvc.py` 这类文件。
- 本次只改文档与配置提示，无行为变更。

## v0.11.0（2026-10-28）

**补上「音色模型文件夹」入口**。之前设置页有模型别名、有默认音色下拉，却没有一处说明
`.pth` 到底该放哪，新用户只能猜。

### 新增

- 配置项 `rvc_models_dir`：音色模型文件夹。留空 = 自动用中间层实际目录（默认
  `<RVC 整合包>\assets\weights`）；填了会作为 `models_dir` 传给中间层 `/model_info` 核对。
- `/刷新rvc模型` 的结果末尾新增 `📁 模型文件夹：<绝对路径>（N 个 .pth，来源：…）`；
  目录不存在、里面没有 `.pth`、或和中间层正在用的目录不一致时，追加 `⚠️` 说明。
- `get_available_models`（LLM 工具）同样带上这一行，模型列表和目录一起交给模型。
- `_update_models_from_api` 优先调中间层 `/model_info`（0.9.0 新增），拿不到就退回
  `/show_model`，老版本中间层不受影响。

### 说明

- 只改插件里的 `rvc_models_dir` **不会**让 RVC 换目录：RVC 只认自己进程里的
  `weight_root`。正确做法是给中间层设 `CHORUS_WEIGHTS_DIR` 并重启，中间层会把它传给
  RVC；插件这边填同一个路径用于核对。README 新增「音色模型放哪」一节写了完整三步。
- 索引文件仍从 RVC 的 `assets\indices` 找，不随这个设置变。

### 验证

- 中间层 0.9.0 `/model_info`：返回 `dir` / `backend_dir` / `count` / `models` /
  `source` / `matches_backend` / `rvc_port` / `rvc_running`，本机实测 8 个 `.pth`；
  传 `assets\indices` 时返回 mismatch 告警。
- `_conf_schema.json` 57 → 58 项，`json.loads` 通过；`main.py` `py_compile` 通过。

## v0.10.1（2026-10-28）

**清理设置页：只留真正生效的项**。按「schema 的 74 项逐项在 `main.py` 找读取点 →
再对照中间层 `app.py` 的 `/convert` 形参与 `pipeline.py` 实际消费」审计，
删掉 17 项历史遗留 / 重复 / 空转配置，剩 57 项。

### 移除（17 项）

- **重复**：`default_model_index`（与 `rvc_default_model` 下拉抢默认模型，一个序号一个名字）。
  代码固定回落到第 1 个模型；用户偏好里的同名字段改名 `model_index`。
- **空转**：`filter_radius`（RVC WebUI 本版 `/infer_convert` 没有这个入参，中间层只记日志）、
  `reverb_intensity`、`delay_intensity`、`vocal_postprocess`
  （`/convert` 收到后从不传给 `pipeline.run`，混响/EQ/压缩不会发生）。
- **空转**：`uvr5_agg`、`uvr5_tta`、`uvr5_postprocess`、`uvr5_window_size`、
  `uvr5_high_end_process`（分离走固定模型，`pipeline` 只认 `config.SEP_MODEL`，不用 UVR5 参数）。
- **空转**：`msst_default_model`、`msst_batch_size`、`msst_num_overlap`、`msst_normalize`、
  `msst_use_tta`（`/select_msst_model` 只改 `app._STATE`，`pipeline` 始终用后端
  `CHORUS_SEP_MODEL`；其余几个连 `convert()` 函数体都不读）。
  `msst_default_model` 改为代码常量，仅保留给 `/切换msst模型` 的运行时状态。
- **重复且绕过统一出口**：`enable_progress_bar`、`progress_update_interval`。
  这套分支在 `_async_predict` 里直接 `event.send` 固定 emoji 进度，和 `notify_progress`
  两套并存，也违反「回复由模型生成」。删除后进度只走 `outbound.py`（默认静音），
  超时仍由原有 deadline 逻辑兜住。

### 变更

- `main.py`：删掉上述配置读取与提交参数（不再往 `/convert` 送空转形参）；
  删除直发进度分支与 `_get_stage_emoji()`；`default_model_index` 相关引用统一改为
  `FALLBACK_MODEL_INDEX = 1`；偏好字段 `default_model_index` → `model_index`。
- `/切换msst模型` 的返回文案写清「chorus-mw 0.8.0 分离模型由后端固定，此选择不改变实际分离模型」，
  不再声称「之后的新翻唱会使用该模型」；也不再往已删除的配置键写值。
- 版本号 → `0.10.1`，README 配置表同步删项，新增本段说明。

### 验证

- `_conf_schema.json` 74 → 57 项，`json.loads` 通过；`main.py` 用 AstrBot 运行时
  Python 3.12 `py_compile` 通过。
- 假 context / 假 event 桩实例化成功；`star_handlers_registry` 中本插件
  handler 29 个（命令 13 + LLM 工具 14 + 平台/音频事件 2），与改动前一致。
- 代码里已无被删键的读取（grep 只剩注释与本 CHANGELOG 的历史说明）。

## v0.10.0（2026-10-27）

**影响翻唱效果的参数补齐并真的传下去**。此前设置页只有 `index_rate` 等少数几项，
而且下游中间层把值写死在 `config.py`，改了没效果。

### 变更

- 新增 4 个配置项，全部中文说明 + 默认值 + 范围：
  - `rms_mix_rate`（float，0~1，默认 0.35）：人声音量起伏贴合原唱的程度。
  - `protect`（float，0~0.5，默认 0.33）：保护清音/气声，越大越不容易撕裂。
  - `inst_volume_db`（float，-20~0，默认 -6）：伴奏音量（dB）。
  - `vocal_volume_db`（float，-12~+12，默认 0）：人声音量（dB）。
- `main.py` 启动时读取这 4 项（并夹到范围内，非法值回落默认并记 warning），
  在 `_predict_cover` 里通过 `_get_mix_kwargs()` **按名字**传给 `/convert`：
  `rms_mix_rate` / `protect` / `inst_volume_db` / `vocal_volume_db`。
  和 `render_mode` 一样先探测后端 `/info` 的参数表，老后端没有这些参数就不传，
  不会因为多传参数整次失败；日志里会提示「升级 chorus-mw 到 0.8.0」。
- `index_rate` 改走统一的 `_bounded_float()`，与其它浮点项一样做范围兜底。
- 设置页说明改写成人话：写清「调大/调小会怎样」和推荐区间；`filter_radius`
  注明「chorus-mw 0.8.0 只记日志、暂不生效」，不假装能用。
- 版本号 → `0.10.0`，README 配置表补 4 项并新增「音色与音量怎么调」一节。

### 验证

- 配合 `chorus-mw` 0.8.0 真实跑：`index_rate=0.3` vs `0.9` 成品不同
  （波形相关 0.9879、200–8000 Hz 频谱平均差 3.26 dB；同参数重跑逐样本一致，
  证明差异来自参数而非随机性）；`inst_volume_db=-6` vs `-12` 低频能量占比
  27.92% → 18.03%；`f0_method=rmvpe` / `pm` 均跑通，日志分别打了 `f0=rmvpe` / `f0=pm`。
- `_conf_schema.json` 通过 `json.loads`；`main.py` 通过 `py_compile`。

## v0.9.0（2026-10-27）

**收窄为纯 RVC 插件**：移除 SVC 与 SoulX 参考音色两套引擎的命令与配置，以及已下线的 QQ 音源兼容项；
设置页只保留生效项，命令表 / 工具表 / 配置表按删完之后的真实情况重写。

### 移除

- **命令 8 个**：两套引擎各自的翻唱入口、模型/音色刷新、后端地址设置、分离模型列出与切换。
- **工具 2 个**：SVC 翻唱工具、SoulX 参考音色翻唱工具。
- **配置 26 项**：两套引擎的地址、开关、模型别名与运行参数，加上已下线的 QQ 音乐开关、
  第三方 Key、Node.js 音源地址、默认音源选择、默认翻唱类型，以及从未生效的工具超时项。
- **代码**：旧音源接口文件 `api.py`（QQ / 网易云直连实现）、三引擎抽象层（取模型、取地址、
  取启用状态、引擎名、音域范围、有效音调）、SoulX 专属的分离参数与音色列表方法、
  旧的音源选择函数、QQ mid 识别、`import secrets`。

### 变更

- 引擎参数、分支、日志全部按 RVC 写死，不再有按引擎开关分叉的判断；
  `_predict_cover` / `_do_cover` / `_execute_local_cover` / `smart_cover` / `batch_cover`
  等入口去掉引擎类型与音乐源参数。
- `search_music` 改走 `source_router.search_candidates`，与取歌共用同一套音源顺序。
- 缓存命令只认 RVC：`/查看翻唱缓存`、`/清理翻唱缓存` 不再接受选择后端的参数。
- 补上代码一直在读、schema 里缺的 `local_audio_cache_ttl`（默认 300 秒）。
- 保留并继续生效：RVC 模型列表/刷新、`rvc_default_model` 下拉、本地音频翻唱、批量翻唱、
  任务查看/取消、统计、缓存命令、`render_mode`（chorus/full/custom）与 `chorus_seconds`、
  多音源路由与 80 秒闸门、统一出口 `outbound`（进度/重试默认静音、失败只说一句）。

### 验证

- 全部 `.py` 通过 `py_compile`；`_conf_schema.json` 通过 `json.loads`（70 项）。
- AST 列出命令 13 个、工具 14 个，均无相关残留；`self.*` 调用全部能对上方法定义。
- 代码 / 配置 / schema 全目录 grep，无已删除的命令、配置、地址字符串。
- 用假 event 的最小桩验证模块可 import、`MusicPlugin` 可实例化（不连网）。

## v0.8.0（2026-10-27）

选歌判定按用户定的规则重做：**整串当歌名、歌手只在明确指定时生效、别名按拼接匹配、
伴奏排除**；旧的「标题互相包含 + 长度打分」整套模糊规则删除，只留一套确定规则。
起因是「雨爱 司凤版」被模型拆成 `song_name='雨爱'` + `artist_name='司凤'`（把版本名当歌手），
结果选中了素人翻唱《雨爱·司凤》（180 秒），正确目标是
id `3445953575`《雨爱》/ Settle一虾子 / 别名「司凤版」/ 138 秒。

### 变更

- **`source_router.pick_best_song` 重写**：
  - 候选先排除伴奏类（伴奏/纯享/纯音乐/消音/无人声/off vocal/instrumental/inst/karaoke/dj版）；
  - 匹配文本为「标题 + 别名(alias/alia/tns)」，只有两档：完全相等（`标题 == 歌名` 或
    `标题+别名 == 歌名`）、包含（扣掉歌名后的剩余字符必须全部来自别名，解释不了就不匹配）；
  - 歌手只在明确给出时过滤；没有任何候选的歌手包含它时，日志写一行 `artist_filter_ignored`，
    改把它当版本别名（只留别名里含这个词的候选），不因歌手条件判失败；
  - 同档取舍只保留「剔除时长不到同档最长 90% 的片段/剪辑，再按搜索顺序」这一条，不参与相似度判定；
  - 删除标题长度×2、标题完全相等 +100、歌手命中 +100、标题+别名覆盖 +30 的打分，以及
    「标题互为子串」的模糊匹配。
- **搜索词**：`_search_keywords` 只返回 `song_name` 原文整串，不再补「歌名 + 第一位歌手」，
  不拆版本名；`artist` 只当过滤条件。
- **入口拆参**：`_do_cover`、`_smart_cover_async` 增加 `artist_name` 参数；
  `smart_cover` / `rvc_cover` / 另外两个引擎的翻唱工具不再把歌手拼进 `search_query`，
  歌名和歌手分开交给 `find_song`。
- **工具 docstring 改写**：明确「唱雨爱司凤版 → song_name='雨爱司凤版'，artist 不填」
  「唱周杰伦的晴天 → song_name='晴天'，artist='周杰伦'」「版本名/别名属于歌名的一部分，
  不要塞进 artist_name」「用户没明确说歌手时禁止凭印象填 artist_name」。

### 验证

用 AstrBot 自带 venv（`C:\Users\21903\AppData\Roaming\uv\tools\astrbot\Scripts\python.exe`）
跑真实网络 + 真实下载（不经过 AstrBot，不重启机器人）：

- 「雨爱司凤版」（song_name 整串、无 artist）→ **id 3445953575**《雨爱》- Settle一虾子
  （别名 司凤版），ffprobe **138.4 秒**，不是素人翻唱、不是伴奏。
- 「雨爱」+ artist「司凤」→ 日志出现 `artist_filter_ignored | song=雨爱 | artist=司凤`，
  仍然命中 **id 3445953575**（138.4 秒）。
- 「失眠飞行」→ 仍命中 **id 1365898499**《失眠飞行》- 接个吻，开一枪、沈以诚、薛明媛，**207.2 秒**。
- 「晴天」+ artist「周杰伦」→ 网易云 10 条候选里没有能确认的版本（同名翻唱/原唱标注全部不匹配），
  自动换源，酷狗命中周杰伦但直链要验证，酷我取到 **id 228908**《晴天》- 周杰伦，**269.7 秒**；
  没有乱选任何翻唱。
- 离线单测：完全相等 > 包含、包含的剩余字符必须来自别名、伴奏先淘汰、歌手命中时按歌手过滤，
  全部通过。
- `py_compile` 通过：`main.py` / `source_router.py` / `netease_fetch.py` / `api.py` /
  `selection.py` / `outbound.py` / `intent_gate.py` / `qq_callbacks.py`。
- 未重启 AstrBot（由用户重启）。

### 文件

- 修改：`source_router.py`、`main.py`、`netease_fetch.py`（注释）、`README.md`、
  `CHANGELOG.md`、`metadata.yaml`

## v0.7.0（2026-10-27）

多音源自动切换 + 别名匹配 + 伴奏排除。起因是一个真实案例：用户说「雨爱司凤版」，
网易云那首标题只有「雨爱」、歌手 Settle一虾子、「司凤版」在别名字段 `alias` 里；
旧逻辑只拿标题匹配，选中了「雨爱司凤版伴奏纯享」（纯伴奏），翻唱出来是静音。

### 新增

- **`source_router.py`**：按 `source_order` 依次尝试各平台，每个平台走同一套
  「搜歌（带别名）→ 严格匹配 → 排除伴奏 → 取播放链接 → 下载 → 80 秒闸门」；
  前一个平台任何一步没成就换下一个，日志里每个平台一行结果。
- **别名匹配**：匹配文本从「标题 + 歌手」扩成「标题 + 别名 + 歌手」。网易云搜索结果自带
  `alias`，没有时再查 `/api/song/detail` 补 `alia` / `tns`；`api.py` 的搜索也带上了别名。
- **伴奏排除**：候选标题或别名出现 伴奏、纯享、纯音乐、消音、无人声、off vocal、
  instrumental、inst、karaoke、dj版 的直接淘汰。所有候选都只是伴奏时对外返回「只有伴奏版」。
- **同分取舍**：同分候选先剔除时长不到最长候选 90% 的（视为片段/剪辑），再按搜索排序取第一条；
  「失眠飞行」这种几毫秒时长差的同一录音仍按搜索相关性选原唱。
- **新配置 `source_order`**：默认 `["netease", "kugou", "kuwo", "qqmusic"]`（网易云 → 酷狗 →
  酷我 → QQ 音乐）。列表同时决定顺序与启停：删掉某项即停用，清空退回默认；中文/别名也认。

### 变更

- `_prepare_song_audio` 改走路由器；原来单网易云的取歌逻辑原样保留成路由器里的第一个源
  （`netease_fetch.NeteaseFetcher`）。80 秒闸门对所有源生效。
- 命令选歌、LLM 工具搜歌（`_do_cover`）也改走路由器的搜歌 + 匹配，卡片菜单会显示实际来源和别名。
- 酷狗：点歌插件的 `gateway.kugou.com/v5/url` 现在常回「本次请求需要验证」，路由器在它失败后
  改用移动端接口 `m.kugou.com/app/i/getSongInfo.php?cmd=playInfo&hash=…` 取直链再下载。
- 酷狗/酷我/QQ 音乐的平台类按文件路径动态 import 点歌插件 `astrbot_plugin_music`
  （合成包名保证它内部的相对 import 正常），cookie/uin 从
  `data/config/astrbot_plugin_music_config.json` 读；QQ 音乐没配 `uin` 时跳过并记一行日志，
  绝不抛异常中断整条链路。
- 旧的 QQ 音源开关（禁用网易云 / 只用网易云 / 启用 QQ 音乐）保留但不再生效（改由 `source_order` 控制），
  schema 里的说明同步改掉。

### 验证

用 AstrBot 自带 venv（`C:\Users\21903\AppData\Roaming\uv\tools\astrbot\Scripts\python.exe`）
独立跑真实网络 + 真实下载（不经过 AstrBot，不重启机器人）：

- 「雨爱司凤版」默认顺序 → 网易云命中 **id 3445953575**《雨爱》- Settle一虾子
  （别名 司凤版），ffprobe **138.45 秒**，5.5 MB；未选中 138.48 秒的「伴奏纯享」。
- 网易云故意取不到（把 meting 指向死端口 `127.0.0.1:9`）→ 自动切到 **酷狗**，
  用移动端接口兜底下载成功（《雨爱 (司凤版)》- 音乐山，ffprobe 238.52 秒）。
- 「失眠飞行」→ 仍选中 **id 1365898499**《失眠飞行》- 接个吻，开一枪、沈以诚、薛明媛，
  ffprobe 207.16 秒（没回归）。
- 单元：3 条全带伴奏标记的候选 → `status=only_accompaniment`，原因 `只有伴奏版`。
- `py_compile` 通过：`main.py` / `source_router.py` / `netease_fetch.py` / `api.py` /
  `selection.py` / `outbound.py` / `intent_gate.py`；`_conf_schema.json` JSON 校验通过。
- 未重启 AstrBot（由用户重启）。

### 文件

- 新增：`source_router.py`
- 修改：`main.py`、`netease_fetch.py`、`api.py`、`_conf_schema.json`、`README.md`、
  `CHANGELOG.md`、`metadata.yaml`

## v0.6.0（2026-10-27）

新增「唱多长」配置：副歌片段 / 整首翻唱 / 自定义时长。后端 `chorus-mw` 同步升到 0.7.0，
默认链路从「整首分离再切」改成「先切 60 秒再分离」，同一首 4:39 的歌实测 72.2 秒 → 32.6 秒。

### 新增

- **`render_mode`**：`chorus`（默认，只翻副歌片段，45 秒）/ `full`（整首翻唱）/
  `custom`（自定义时长，用 `chorus_seconds`）。插件按名字探测后端 `/convert` 是否支持
  `render_mode`，老后端没有这个参数就整组不传、仍按副歌模式处理，不会报 invalid keyword。
- **`chorus_seconds`**：20~180 秒，默认 45；`custom` 模式按它定长，`chorus` 模式也用它当片段长度。
- **完整翻唱更慢**：`full` 要分离整首、变声整首，耗时可能是副歌模式的 5~10 倍，显存占用更高。
  4:39 的歌整首跑通常要几分钟以上，默认 `inference_timeout=300` 很可能不够；
  要整首翻唱请把 `inference_timeout` 和 `task_timeout_seconds` 一起调大。

### 说明

- 时长参数只对 RVC 后端（`chorus-mw` 0.7.0+）生效；SVC / SoulX 后端没有这组参数，
  插件自动跳过，行为不变。
- 中间层侧改动见 `chorus-mw` 的 CHANGELOG 0.7.0：先切后分离、常驻 worker、去混响与伴奏处理并行。

### 验证

- `py_compile` 通过：`main.py`。
- 实测同一首 4:39 的 mp3：`chorus` 端到端 34.1 秒（成品 45.0 秒 wav），
  `custom=30` 端到端 27.9 秒（成品 30.8 秒 wav），`full` 用 100 秒素材 52.7 秒跑通。
- 未重启 AstrBot（由用户重启）。

## v0.5.0（2026-10-27）

取歌/下载实现整套换成点歌插件 `astrbot_plugin_music` 那套，删掉自己写的那套和几条说明性日志横幅。

### 变更

- **新增 `netease_fetch.py`**：照搬点歌插件的取歌链路——搜歌 `POST http://music.163.com/api/search/get/web`
  （`s` / `limit` / `type=1` / `offset=0`，带 `appver=2.0.2`）、主路直链
  `GET https://api.qijieya.cn/meting/?type=song&id=<id>`、备用直链 NodeJS API
  （先 `/song/url/v1?level=…` 再 `/song/url`，可带 `MUSIC_U`）。
  落盘同样照搬：`temp_audio/` + `uuid4().hex` 命名、按 URL 后缀白名单、1024 字节分片、
  HTTP 非 200 / 返回错误页 / 超 60MB / 内容不全一律删半截文件。
  点歌插件里发卡片、发语音/文件、歌词与评论渲染、封面卡片等逻辑没有搬。
- **删除 `netease.py`**：原先自己写的「官方接口判定 + 外链兜底」实现（含 `download_by_id`
  / `download_by_api_service`）整体移除，改由 `netease_fetch.NeteaseFetcher` 承担。
- `_fetch_netease_audio` 改为直接 `await` 取歌器（不再 `asyncio.to_thread`），并保留
  cookie 失效连续计数与管理员提醒。
- `_search_netease_songs` 先用点歌插件那套搜索，失败/无结果再退回插件原来的搜索器。
- `_prepare_song_audio` 对外契约不变：`(本地文件路径 | None, 失败原因, 是否试听)`；
  **80 秒 `ffprobe` 闸门保留**，量不到时长照样判失败。
- 删除说明性日志横幅：`QQ 音源已下线：搜歌与取歌只走网易云官方接口`、
  `网易云取歌首选本地服务: …`、`没配置 netease_api_base，网易云取歌只走官方接口 + 外链方式`、
  `音乐源 xxx 已下线（QQ 音源下线），按网易云处理/搜索`；旧的「默认音源」配置那条警告改成只说事实。
  正常 `[取歌] …` 日志与错误日志全部保留。

### 配置

- 新增 `netease_meting_base`（默认 `https://api.qijieya.cn/meting/`）：取直链主路，不需要密钥。
- 新增 `netease_api_token`：备用 NodeJS 服务需要鉴权时填写（`Authorization: Bearer …`）。
- `netease_api_base` 默认值改为点歌插件用的 `https://zm.wwoyun.cn`（线上仍是本机 `3003`，不受影响）。
- `netease_cookie`、`netease_quality` 等原有配置项名称与作用保持不变；`netease_cookie` 的说明
  改成"备用路用"，因为主路 meting 不吃 cookie。

### 验证

- `py_compile` 通过：`main.py`、`netease_fetch.py`。
- 真实下载「失眠飞行」（网易云 id `1365898499`）：走主路 meting，文件 8,289,219 字节，
  `ffprobe` 时长 **207.157 秒**；按歌名搜索也能命中同一 id 并下到同一首歌。
- 不存在的 id（`999999999`）两条路都返回"没拿到链接"，不落垃圾文件。
- 未重启 AstrBot（由用户重启）。

## v0.4.3（2026-10-27）

新增「点歌 / 翻唱」需求判定闸门，翻唱插件不再接点歌的活。

### 新增

- **`intent_gate.py`**：判定逻辑和词表集中在这里（`TICKET_WORDS_MUSIC` / `TICKET_WORDS_COVER`）。
  消息里出现点歌类词（点歌、放歌、听歌、来一首、放一首、播一首，以及「放首歌」这类省略写法），
  且**不包含**翻唱类词（唱、翻唱、翻唱成、变声、cover、用…唱）→ 判定为点歌需求。
- 闸门接在每个翻唱入口：`smart_cover`、`rvc_cover`、另外两个引擎的翻唱工具、`batch_cover`、
  `cover_absolute_path_audio`、`cover_local_audio`，以及本地音频自动触发 `on_message_with_audio`。
  命中后只把「这是点歌需求」这句事实作为工具结果回给模型（并建议用「点歌 歌名」或点歌插件的
  `play_song_by_name`），不注册任务、不执行翻唱。

### 变更

- `local_audio_trigger_keywords` 默认值收窄为 `["翻唱", "变声"]`，去掉「转换」「处理」「修音」这类泛词。
- `local_audio_auto_trigger` 的 schema 默认值改为 `false`（代码里本来就是 `False`，之前 schema 写的是
  `true`，新装默认就会主动抢带音频的消息）。线上配置同步改成关闭 + 两个触发词。

### 修复

- 「嗯。点歌。失眠飞行。」这类不按命令格式发的点歌，不再被模型顺手调用 `smart_cover` 变成翻唱。
- 「点歌 晴天」即使命令没命中、落到模型手里，翻唱工具也会拒绝执行。

### 其它

- 同步线上已有的 `_pick_best_song`（搜歌时只接受歌名/歌手能对上的结果，宁可不选也不选错）：
  此前只改了线上文件，这次一并纳入仓库，保证两边 SHA256 一致。

### 验证

- `py_compile` 通过：`main.py`、`intent_gate.py`、点歌插件 `main.py`（只编译到 `_scratch`，没往点歌插件目录写文件）。
- 假 event 判定 7 条全过：点歌 晴天→拒绝、嗯。点歌。失眠飞行。→拒绝、唱晴天→允许、
  翻唱一下晴天→允许、用三月七唱晴天→允许、给我放首歌→拒绝、变声→允许。
- 线上与仓库文件 SHA256 对齐；未重启 AstrBot（由用户重启）。

## v0.4.2（2026-10-27）

### 修复
- smart_cover / rvc_cover 的工具描述里明确写清使用边界：只在用户要「唱 / 翻唱 / 变声」时调用，
  用户说「点歌 / 放首歌 / 听」是放原曲，不要用翻唱工具；拿不准时先反问。
  起因：命令插件热重载的空窗期，一条「点歌 晴天」落到模型手里，被误判成翻唱。

## v0.4.1（2026-10-27）

### 修复
- 本地网易云取歌服务没启动时，失败原因现在带上服务地址，日志里直接给出可执行提示（跑 netease-api\start.ps1 或用一键启动脚本），不用再翻半天日志才知道是服务没起。
- 配套：一键启动脚本 D:\QQBOT\启动\start.ps1 把该服务加进自启（端口 3003），-Status/-Stop 也一起纳管。

## v0.4.0（2026-10-27）

新增"本地 NeteaseCloudMusicApi 服务"取歌路径：能下会员歌，官方接口取不到时不再
只剩失败一条路。

### 背景

官方 `api/song/enhance/player/url` 能返回链接，但直连 CDN 一律 403；`outer/url`
对 `fee=1` 的会员歌只返回 HTML。实测两首失败样本：`657666`（残酷な天使のテーゼ）、
`536622304`（Lemon）；成功样本 `2652820720`（晴天）。

### 新增

- **`netease.py`**：新增 `download_by_api_service(song_id, cookie, base_url, quality_br,
  dest_dir, timeout)` —— 调本地服务的 `/song/url/v1` 换带时间签名的 CDN 直链再下载，
  判定沿用同一套整首闸门（`code=200`、`url` 非空、`freeTrialInfo` 为 null、
  `time >= 80000`），下载完再用 `ffprobe` 量真实时长，不足 80 秒删文件判失败。
- 配套小函数：`api_service_level()`（320000→`exhigh`、192000→`higher`、128000→`standard`）、
  `normalize_api_base()`（补 `http://`、去尾部斜杠）。
- 新增失败原因：`REASON_API_BASE`（没配置地址）、`REASON_API_UNREACHABLE`（服务连不上）。
- **`main.py`**：读取 `netease_api_base`；`_fetch_netease_audio()` 改成两条路——先本地服务，
  失败再回落原来的官方接口 + 外链方式；两条都失败才记一次 cookie 相关失败。
- **`_conf_schema.json`**：新增 `netease_api_base`（`string`，默认 `http://127.0.0.1:3000`，中文说明）。

### 实测（本地服务 `127.0.0.1:3003`）

| 歌曲 id | 接口 | 状态 | 时长 |
| --- | --- | --- | --- |
| 657666 残酷な天使のテーゼ | `/song/url/v1?level=exhigh` | 200 / 320k / 9.38MB | 245.9 秒 |
| 536622304 Lemon | 同上 | 200 / 320k / 9.77MB | 256.0 秒 |
| 2652820720 晴天 | 同上 | 200 / 320k / 10.65MB | 279.0 秒 |

### 部署提醒

- 端口 `3000`、`3001` 已被本机 SnowLuma 占用，实际服务起在 `3003`；线上插件配置里
  `netease_api_base` 填的是 `http://127.0.0.1:3003`，schema 默认值仍是 3000。
- 服务用 `D:\PYMSS-RVC\netease-api\start.ps1` 后台常驻，日志在 `netease-api\logs\server.log`。

### 版本

- `metadata.yaml`、`main.py` 的 `@register` 版本号升到 `0.4.0`，README 配置表与更新日志同步。

## v0.3.1（2026-10-27）

新增 RVC 默认音色下拉配置 `rvc_default_model`，不用再靠序号记音色。

### 新增

- **`_conf_schema.json`**：新增 `rvc_default_model`（`string`，下拉），`options` 固定为 `Yuuka.pth`、`ainuo.pth`、`guanguanV1.pth`、`keruanV1.pth`、`kikiV1.pth`、`youzhanv2-xi.pth`、`三月七.pth`、`穹.pth` 共 8 个文件名；`default` 为空串，表示不指定、沿用序号逻辑。
- **`main.py`**：读取 `self.rvc_default_model`；新增 `_configured_rvc_model_index()`，按文件名 / 去扩展名 / 别名做精确匹配，把下拉值换成当前模型列表里的序号。
- 生效位置：`smart_cover`、`batch_cover`、`cover_local_audio` / `cover_absolute_path_audio` 以及本地音频执行入口，只在用户没有显式给出模型名或序号时套用。

### 优先级

- 用户显式指定的模型名/序号 > `rvc_default_model` > 用户偏好 / `default_model_index`。
- 仅对 RVC 生效；下拉选的模型不在当前列表里（没 `/刷新rvc模型`、模型被删）时回落到原来的序号逻辑，并记一条 `logger.warning`。
- 命令与 LLM 工具里已经显式指定模型的分支未改动。

### 版本

- `metadata.yaml` 与 `main.py` 的 `@register` 版本号升到 `0.3.1`，README 配置表与更新日志同步。

### 验证

- `py_compile main.py` 通过；`_conf_schema.json` 可被 `json.loads` 解析，新项存在；临时脚本确认 `options` 为 8 个、顺序与要求一致、`default` 为空串，跑完已删除。
- 已同步改动的 `.py` / `.json` / `.md` / `.yaml` 到线上插件副本，逐文件 SHA256 对齐；未重启 AstrBot。

## v0.3.0（2026-10-27）

彻底移除洛雪（LX-Source），音源只剩网易云官方接口 + cookie；QQ 音源下线。

### 移除

- **`netease.py`**：删除 `download_via_lx()`、`build_lx_url()`、`probe_duration_ms()` 以及全部 `REASON_LX_*` 常量与 lx 相关辅助函数；文件只剩官方接口判定 + 外链下载这一条路，`download_by_id()` 核心逻辑未改。
- **`main.py`**：删除 `_fetch_lx_audio()`、`_fetch_qq_audio()`、`_handle_qq_cover()` 与四个 QQ 入口命令（`/qq点歌` 及三个 QQ 翻唱入口）；`_fetch_netease_audio()` 去掉全部洛雪兜底，取不到整首直接失败。
- **`_conf_schema.json`**：删除洛雪地址、洛雪认证头、洛雪兜底开关三项配置；`api.py` 里已废弃的第三方音源短路注释一并清掉。

### 变更为只用网易云

- 「只用网易云」开关默认改为 `true` 并在代码里固定为 `True`（描述写明“已锁死只用网易云”）；`_resolve_music_source()` 无论调用方或配置怎么写，最终一律返回网易云。
- `_prepare_song_audio()`：纯数字 → 网易云 id；旧的 QQ mid → 不再碰 QQ，直接按歌名去网易云搜，搜不到就失败；其它字符串 → 按歌名搜网易云；全部只过官方接口。
- `_do_cover()` 与 `search_music()` 删掉 QQ 搜索分支与风控重试，网易云搜索失败直接给失败原因。
- 旧的「默认音源」配置填 QQ 或未知值一律回落网易云官方接口并记 warning；「禁用网易云」开关保留读取但按网易云继续（只记 warning）。

### 版本

- 版本号 0.3.0；`metadata.yaml`、`README.md`、`NOTICE.md` 同步更新；`requirements.txt` 去掉已不再使用的 `qqmusic-api-python`。

### 验证

- 六个 Python 文件 `py_compile` 全过；`_conf_schema.json` JSON 解析通过；全目录 grep 确认无洛雪配置键、旧端口、失效域名残留。

## v0.2.2（2026-10-27）

修网易云整首下载 403：官方接口只用来判定，下载改走外链跳转入口。

### 修复

- **`download_by_id` 不再用接口返回的 `data[0].url` 下载**（该 CDN 直链实测 403 Forbidden），判定通过后固定改走外链跳转入口 `https://music.163.com/song/media/outer/url?id=<id>.mp3`，请求头同样带 `Cookie: MUSIC_U` / `Referer` / `User-Agent`，`urllib` 默认跟随 302。接口判定逻辑（HTTP 200、`code == 200`、`url` 非空、`freeTrialInfo is null`、`time >= 80000`）原样保留；判定失败（如只给试听）仍直接返回，不再退回外链碰运气。
- **下载后再用 `ffprobe` 量一次真实时长**：不足 80000ms 就删文件并返回失败原因“下载到的不是整首（时长 xx 秒）”，量不到时长按“下载完量不到时长，无法确认是整首”失败（宁可不出歌，也不把片段交出去）。
- **下载失败原因带 HTTP 状态**：`REASON_HTTP_DOWNLOAD`（“下载地址返回 HTTP xxx”）在直链返回 403/404 等时原样透出，方便定位。

### 变化

- 下载请求头抽成 `MEDIA_USER_AGENT` / `MEDIA_REFERER` + `_media_headers(cookie)`，与接口那套 `DEFAULT_USER_AGENT` / `OFFICIAL_REFERER` / `_api_headers(cookie)` 分开维护，改一边不影响另一边；`_download_file` 改为接收 `headers` 字典。

### 验证

- `netease.py`、`main.py` 的 `py_compile` 通过（Python 3.12）。
- 用线上配置里的 `netease_cookie` 真实跑了一次 `download_by_id('2652820720', cookie)`：落盘 4,464,684 字节（约 4.4MB），`ffprobe` 时长 278,962ms（约 279 秒），`reason` 为空、`is_trial=False`；跳转落在 `m701.music.126.net`，`Content-Type: audio/mpeg`，是整首 128k。
- 已同步 `netease.py` / `main.py` / `metadata.yaml` / `README.md` / `CHANGELOG.md` 到线上插件副本，逐文件 SHA256 对齐；未重启服务。

## v0.2.1（2026-10-27）

把"取歌"这条链收成一个入口：卡片选歌、命令点歌、LLM 工具、批量翻唱都只走 `_prepare_song_audio`，id / QQ mid 不再有任何一条路能原样递给中间层。

### 新增

- **统一取歌入口 `_prepare_song_audio(event, song) -> (本地路径|None, 失败原因, 是否试听)`**，判定顺序：
  1. 已经是存在的本地文件 → 直接返回；
  2. 纯数字 → 网易云 id（官方接口优先，失败再洛雪 `/link/wy/<id>/<音质>`，都过 80 秒闸门）；
  3. `^[0-9A-Za-z]{11,14}$` 且含字母 → QQ mid：洛雪 `/link/tx/<mid>/128k`（下完 `ffprobe` 量时长，< 80000ms 判试听失败）→ QQ 官方库 → 按歌名回网易云搜索，再回到第 2 条；
     （规格里写的是 11~12 位，但实测线上报错的两个 mid `0039MnYb0qxYhV`、`003YYGKC1DTKmZ` 都是 14 位，所以按 11~14 位认，否则这两个 id 会漏进"按歌名搜索"。）
  4. 其它 → 当歌名：网易云搜索第一条的 id 回到第 2 条，搜不到再洛雪 `/link/wy/<歌名>` 试一次。
  每一步失败的原因按顺序串成一句中文，技术细节只写 logger。
- 配置项「只用网易云」开关（bool，默认 `false`）：开启后搜歌阶段只走网易云，不再拿回 QQ mid；旧的 QQ 入口会提示改用网易云命令。两个搜索源同时禁用时按网易云处理并记 warning。
- `netease.py` 新增公开的 `probe_duration_ms(path)`，供插件给 QQ 直链 / 本地文件做 80 秒闸门。

### 变化

- **批量翻唱不再把 id 交给中间层**：`_do_cover`（LLM 工具 / 批量走这条）和 `_send_song`（卡片 / 命令选歌走这条）都改成调统一入口，先拿到本地音频文件再提交；`song_input_for_api = str(selected_song["id"])` 这类写法已全部删除（grep 确认无残留）。
- **修掉"网易云搜不到这首歌：<QQ mid>"的根因**：旧「默认音源」配置为 QQ 时 `self.api` 是 QQ 搜索器，而网易云分支直接复用它，拿到的 id 其实是 mid。现在网易云这条路固定用"确定是网易云"的搜索器，`self.api` 是 QQ 时临时新建网易云实现。
- **洛雪兜底支持多源**：洛雪下载函数不再强制纯数字 id（`tx` 传 mid、`wy` 可传歌名），落盘文件名做了安全化处理；按歌名兜底时 URL 路径段做百分号编码（中文直接拼进 URL 会让 `urllib` 抛 `UnicodeEncodeError`）。
- **批量报告只说人话**：每条失败改成"这首拿不到音源 / 生成超时了 / 这个引擎被禁用了"这类短句，中间层原始报错、重试次数等技术细节只进日志；批量整体异常的报告也收敛成一句。
- `_fetch_netease_audio` 返回值补上"是否试听"，供统一入口串失败原因。

### 验证

- 六个 Python 文件 `py_compile` 全通过。
- 临时自检脚本（假对象 / monkeypatch，不真下歌）覆盖：本地文件直通、纯数字走网易云、QQ mid 走洛雪失败后回落到按名搜索、歌名搜索不到、「只用网易云」开关为 true 时搜歌不调 QQ，全部通过；脚本跑完已删除。
- 已同步到线上插件目录，逐文件 SHA256 对齐；未重启服务。

## v0.2.0（2026-10-27）

网易云点歌改走**官方取直链接口 + MUSIC_U cookie**，洛雪源只当兜底；两条路都按"整首"标准过滤试听片段，cookie 连续失效会提醒管理员。

### 新增

- 新增 `netease.py`（纯标准库，不新增第三方依赖）：
  - `download_by_id(song_id, cookie, quality_br=320000, dest_dir, timeout=120) -> (路径|None, 失败原因, 是否试听)`：调官方接口 `GET https://music.163.com/api/song/enhance/player/url?ids=[<id>]&br=<码率>`，带 `Cookie: MUSIC_U=...`、`Referer: https://music.163.com`、`User-Agent: Mozilla/5.0`；成功就流式下载到本地，单文件上限 60MB。
  - 洛雪源兜底函数：向本机洛雪服务要直链，带认证头，下完再量时长。
  - `judge_result(...)` / `is_cookie_failure(...)`：把"整首"判定和"这次算不算 cookie 问题"抽成纯函数，方便单测。
- 配置项 7 个：`netease_cookie`、`netease_quality`、洛雪地址、洛雪认证头、洛雪兜底开关、`alert_admin_on_cookie_expired`、`cookie_alert_interval_hours`。
- cookie 失效提醒：连续 3 次取歌都因为 cookie 相关问题失败（没配置 / HTTP 401 / `code != 200` / 只给试听）时，私聊 AstrBot 全局配置里的 `admins_id`；读不到管理员就发回触发这次点歌的会话；同一失效状态按 `cookie_alert_interval_hours` 去重，取歌成功后清空状态。
- `outbound.py` 新增 `alert` 出口（管理员提醒），与 `start/progress/retry/failed/done` 共用同一个"台词交给模型现编"的出口；`say()` 现在返回是否真的发出去（`bool`），方便管理员会话发不出去时降级重发。

### 变化

- **网易云不再把歌曲 id 直接交给中间层**（中间层不认 id）：`_do_cover`（LLM 工具 / 批量翻唱走这条）和 `_send_song`（卡片选歌后走这条）两处都改成"先下载成文件，再把本地路径交给中间层"。
- **试听判定**：成功必须同时满足 HTTP 200、`code == 200`、`data[0].url` 非空、`data[0].freeTrialInfo is None`、`data[0].time >= 80000`（毫秒）。任一条不满足都算失败，失败原因（没拿到链接 / 只给了试听 / 接口 code=... / 接口返回 HTTP ... / 下载中断）只进日志和上层统一失败出口。
- **洛雪只作兜底**：官方接口失败后才用同一个数字 id 去 `/link/wy/<id>/320k`。洛雪返回体只有 `data`（链接）和 `msg`，没有时长字段，所以兜底路径下完文件后用 `ffprobe` 量真实时长，同样按 `>= 80000ms` 卡一遍，60 秒试听片段因此过不去；量不到时长按失败处理（宁可不出歌，也不把片段交给中间层）。
- 网易云这条路的下载文件与 QQ 音乐一致，落在 `temp_audio/`，任务结束由 `finally` 清理。

### 验证

- `netease.py` / `main.py` / `outbound.py` 三个文件 `py_compile` 通过。
- 判定逻辑离线自检 23 项全过：官方成功、试听、`code=404`、时长不足、没链接、HTTP 401、没 code、没时长、洛雪 `data` 直链形状、洛雪带 `freeTrialInfo`、非 cookie 原因不误报、空 cookie、非法 id、洛雪没配地址、duration 秒/毫秒兜底。
- `outbound.say` 行为自检 8 项全过：`alert` 只发一条且走模型文案、`progress` 静音返回 `False`、未知类型静音、发送异常返回 `False`、目标对象返回 `False` 时调用方可降级、拿不到 provider 时 `alert` 退回一句兜底中文。
- `ffprobe` 时长闸门实测：90 秒文件量到 90000ms（判为整首），30 秒文件量到 30000ms（判为试听）。
- **未覆盖**：真实 cookie 下的官方接口下载、真实洛雪兜底（本轮不跑长测试；实测洛雪 `/link/wy/.../320k` 当前返回 503，服务在但源没给链接），以及中间层真正吃到本地文件的端到端翻唱。

## v0.1.0（2026-10-27）

首个自维护版本，基线为上游 **sdfsfsk/matsuko_cover v2.13.0**，许可证 **AGPL-3.0**。

### 新增

- 新增 `outbound.py`（统一对话出口 `Outbound`）：把原先硬编码直接发给用户的三类文本——**进度提示**、**风控重试提示**、**报错详情**——收敛到一个出口，台词交给 AstrBot 当前使用的人格模型现编，插件只提供事实（歌名、模型、来源、失败原因）。
- 新增配置项 `notify_progress`、`notify_retry`（均为 `bool`，缺省 `false`）。

### 变化

- **进度提示 / 重试提示默认静音**：两个开关缺省关闭，关掉时一条都不发；打开后也只由模型说一句人话，不再刷"⏳ 遇到风控，正在自动重试…"这类模板句。
- **失败只发一句话**：失败时不再把整段 `result` 字符串（重试次数、错误码、可能原因清单）甩给用户，技术细节只写 `logger`；用户看到的是一句"这首没唱成，换个歌名我再试一次"级别的人话。
- **工具返回值去装饰**：`rvc_cover` / 另外两个引擎的翻唱工具 / `smart_cover` 不再返回"🎵 正在从【X】用【Y】翻唱《Z》…请稍等"，改为一句中性短句交给 LLM 组织措辞（避免"工具结果 + LLM 回复"两次说话）。
- **失败判断去关键词**：`_smart_cover_async`（以及批量翻唱）只认 `result.startswith("❌")` 或 `result.startswith("FAIL:")`，删掉"失败/错误/超时/未找到/无法获取/触发风控/当前没有/已禁用"的关键词匹配——正常文案里带"失败"两个字会被误判成任务失败。`_do_cover` 的返回值协议不变，保留 `❌` 前缀兼容现有返回点。
- 出口层自身异常（拿不到 provider、模型调用失败、发送失败）全部吞掉并退回极短兜底文本，只记 `warning`，不影响翻唱主流程。

### 修复

- **QQ 音乐 mid 解析**：新版 `qqmusic-api-python` 返回结构里 `mid` 可能已经是字符串，直接读 `obj.mid` 会抛 `'str' object has no attribute 'mid'`（日志里表现为"官方API获取失败"）。新增 `_extract_mid()`：有 `mid` 属性就取 `mid`，本身是字符串就用它，其余视为拿不到；拿不到时记日志并降级，不把异常抛给用户。
- **移除失效的备选音源接口**：该域名已无法解析（2026-10-27 实测 `getaddrinfo failed`），分支删除并短路返回，不再让每次重试白等一次 15 秒网络超时。

### 验证

- 五个 Python 文件 `py_compile` 全通过。
- 全目录检索确认已无 `遇到风控，正在自动重试`、`正在从【` 这类硬编码文案，统一出口接线 17 处。
- `outbound.py` 在 AstrBot 运行环境（uv 工具虚拟环境 python 3.12）跑行为测试 9 项全过：进度/重试默认静音、失败走模型、模型不可用退回兜底、`event.send` 抛异常被吞、开关打开后进度可发、未知类型静音。
- 线上加载验证：AstrBot 重启后日志显示 `Plugin astrbot_plugin_cucumber_rvc_console (v0.1.0) by void-HaoNan`、`Added llm tool: smart_cover`、平台钩子正常，无异常堆栈；配置从上游插件迁入后 `rvc_base_url`、8 个模型、两个新开关均正确。
- 未覆盖：`main.py` 17 处接线的运行时行为（需要真实点一首歌才能验证）。

### 说明

- 本分支**不再跟随上游更新**，需要上游新功能请直接使用上游仓库版本。
- `LICENSE` 保持上游原样（AGPL-3.0），修改声明见 `NOTICE.md`。
