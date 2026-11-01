# CHANGELOG_CUSTOM — 个性化修改记录

> 隐私说明:本文件面向公开仓库,测试用群号已脱敏为「测试群」;
> 完整未脱敏原件由开发者自行保留,不在本仓库中。
> 备份路径等开发者环境信息已在下方历史条目中如实保留(仅含路径,无群号/用户数据)。

格式：`[修订号] 日期 — 变更点（文件 · 原因 · 行为变化 · 验证 · 回滚）`
所有备份都保留在插件目录下的 `.bak-<时间戳>` 目录/文件中，未删除过任何备份。

---

## [CUSTOM-9] 2026-11-02 — 更名通用化（1.0.0）与全面回归

1. **更名**：插件 → `astrbot_plugin_cucumber_rvc_console`「RVC 控制台」(1.0.0)；网关 → `cucumber-rvc-gateway`。旧名零残留（迁移常量除外）。
2. **配置/数据迁移**：旧名配置值覆盖式迁移 + plugin_data 文件迁移，幂等；旧配置文件改名 `.migrated-` 标记保证只跑一次。修复迁移三连 bug（补缺语义失效/重复覆盖/顶层键错位）。
3. **路径通用化**：网关 compose 全参数化 + `.env.example` + 启动自检；插件 `gateway_work_dir` 配置化；page_api 路径动态推导；写死路径/模型名/个人环境 0 残留（审计）。
4. **设置保存统一实时同步** `apply_live_config`：默认音色/推理参数/队列上限/模型性别等 15 键保存即生效；「需重载」标注扩充至 45 键；配置审计 114 叶 0 未消费。
5. **联调修复 10 项**（详见交付报告 docs/delivery-report-rename.md 第 3 节）：网关缺失常量、page_api 缺失函数、音色列表编辑器读写错位、同会话单任务限制三层移除、排队列表残留、LLM 翻唱工具调用注入、矛盾复述抑制等。
6. **功能增强**：`/cover_status`；模型管理页 翻唱/TTS 独立启用开关；RVC/TTS 当前任务显示触发用户与四步进度；历史详情加 升降调/群号群名；排队列表含「准备中」阶段任务；默认音色/TTS 模型动态下拉（网关实时列表）。
7. **真实群测试**（测试群）：翻唱全链路/伴奏拒绝/取消/同会话排队 ✅；TTS 群内端到端与 `/cover_status` 群内展示待最终确认（容器级已验证）。
8. 回滚：见交付报告第 9 节（备份 pre-rename-20261101-182441）。
9. **追加联调（同日）**：模型管理页交互重做——每行模型两个启用单选（翻唱/TTS 各自互斥）+ 顶部「保存设置」统一提交（别名/性别/双默认）；每行显示 index 匹配状态；移除每行保存按钮与全局开关；「排队位置通知」移入通知播报分组；品牌 logo/艺术字更新；静态资源加版本参数根治浏览器缓存。修复 6 项实测问题（保存绑定丢失、性别实时同步、关键词后缀归一、队列轮询等），详见交付报告第 11 节。

---

## [CUSTOM-8] 2026-11-01 — 多会话并发任务调度与消息投递

1. **统一任务注册表**（新增 `task_registry.py`）：task_id 级状态机
   （received→queued→running→completed/failed/timeout/cancelled，非法流转拒绝、
   终态不可变、received 直接收尾按失败/取消兜底不卡名额）；每条记录含
   会话/群/用户/请求文本/网关任务号/输出路径，翻唱与 TTS 共用。
2. **接入点**：`_register_active_task` 同步登记+防重复（同用户+同会话+同请求，
   窗口 120s，拒绝原因区分「会话占用/重复提交/队列满」）；`_predict_cover` 运行态
   +排队位置通知（`queue_position_notice`，入队只发一次）；`_finish_active_task`
   带错误/超时终结；TTS 钩子全链登记+排队取消/运行软取消+重复拦截。
3. **全局等待上限** `task_queue_limit`（默认 10，跨翻唱/TTS 原子检查）。
4. **新命令 `/cover_status`**：查询自己的任务（类型/状态/排队位/错误摘要）+
   全局运行/排队数；隐私隔离（只见自己的）。
5. **取消扩展**：`cancel_cover_task` 覆盖该用户所有进行中任务（含排队 TTS）；
   排队任务立即取消、运行中翻唱走既有 Gradio cancel→子进程终止、运行中 TTS 软取消。
6. **网关**：任务日志新增 `queue_wait`（排队等待耗时）。
7. **验证**：注册表语义单测 8 组全绿（FIFO/上限/去重/流转/取消/隐私/收尾/规范化）；
   网关真实并发集成测试（双 TTS 同时提交，串行执行 + 双成品 + FIFO 确认）；
   配置 111 键双向一致。真实 QQ 多群并发未实测（方案禁止），投递绑定靠事件对象
   +单测，属已知风险（见交付报告 docs/delivery-report-scheduler.md 第 13 节）。
8. 单 Gateway 实例假设：threading 信号量不跨进程，**严禁启动第二个网关实例**。

---

## [CUSTOM-7.3] 2026-11-01 — TTS 群聊触发联调修复（真实群聊验证通过）

1. **开关语义澄清**：「群聊触发需要 @Bot」改为「群聊允许 TTS 语音回复」
   （关闭=群聊彻底不触发，此前文案有歧义导致联调误判）；新增独立开关
   **「群聊触发必须 @机器人」**（`tts_group_require_at`，默认开，可关成"不用 @ 也能触发"）。
2. **触发配置实时生效**：总开关/群聊/私聊/必须@/冷却时长/音色模型/音调/格式/服务商
   全部改为运行时读配置（此前启动时缓存，WebUI 改完不重载不生效且无提示）。
3. **语音发送修复**：`set_result` 整链替换时 Record 段在 Respond 阶段偶发丢失
   （NapCat 日志实证）；改为文字、语音均由钩子按「先文字后语音」主动 `event.send`
   （与翻唱成品同一发送路径），发完 `stop_event()` 阻止默认回复重复发送。
4. **全链路诊断日志**：钩子入口/每层判定/发送结果均有 `[TTS]` 前缀日志。
5. **真实群聊验证**：@Bot 说「我想听你说…」→ fishaudio 生成 → rym 变声(热路径 ~15s)
   → 文字+语音先后到达 ✅。

---

## [CUSTOM-7.2] 2026-11-01 — 网关常驻 RVC worker（推理提速 ~4 倍）+ 内存上限 12g

1. **常驻 worker**（新增 `rvc_worker.py`，gateway.py 增加 `RvcWorker` 管理器 + `run_rvc_stage` 统一入口）：
   - 原路径每次推理新起 `infer/cli.py` 子进程，模型/HuBERT/RMVPE 每次重载（CPU 固定 ~40s）；
   - worker 常驻、复用 cli.py 的 `resolve_model/load_model_metadata/select_speaker/resolve_index/write_audio`
     与 `infer.vc.modules.VC`，同模型连续请求零加载成本；模型切换自动重载（~40s，仅一次）；
   - 协议：JSON Lines over stdio（协议走复制的 stdout，防杂散 print 破坏流）；
   - **任何失败（崩溃/超时/OOM）自动回退子进程路径**（`GW_WORKER=0` 可整体停用）；
   - 空闲 `GW_WORKER_IDLE_SECONDS`（默认 600s）自动退出还内存，下次请求自动拉起；
   - `/convert` 与 `/tts_convert` 共用；单槽模型缓存，交替使用不同音色时各有一次重载。
2. **实测**：TTS RVC 阶段 40.8s → 9.5~9.7s（~4×）；歌曲 RVC 阶段 ~130s → 54.1s；
   模型切换（rym→nuoyemu）自动重载无失败；4 次推理全走 worker 零回退。
3. **内存**：网关常驻 1.227GiB → 热态 2.303GiB（**+1.08GiB** = worker 含模型），
   空闲后回落；容器上限 8g→**12g**（compose 显式写回，含 swap 16g）。
4. 回滚：`.bak-custom72-<时间戳>-gateway.py` 恢复 + `docker compose up -d --build`
   （rvc_worker.py 留着不影响，worker 代码只在 gateway.py 引用）。

---

## [CUSTOM-7] 2026-10-30 — TTS 音色回复 + 真 Reverb 卷积混响 + WebUI 重构（0.13.0）

> 升级方案三件套：TTS 音色转换（模型与歌曲翻唱完全独立）、aecho → 真卷积混响、
> WebUI 新增 TTS 控制台与音频效果页。开发在独立副本（chorus_cover_dev + gateway_dev）完成，
> 未触碰生产插件目录、生产 gateway.py、容器与模型。

### 1. TTS 语音回复（新模块 `tts.py` + `main.py` 钩子）

- 新模块 `tts.py`：`TTSManager`（AstrBot TTS Provider → `gateway_work/tts/<task_id>/` →
  网关 `/tts_convert` → ffprobe 校验成品）+ `CooldownGate`（用户/群双层冷却）。
  **适配 AstrBot 4.29 实测接口**：`get_provider_by_id` 是 async（参考插件 astrbot_plugin_rvc
  按同步调用，在 4.29 上拿到的是 coroutine，不能照搬）。
- `main.py` 新增 `@filter.on_llm_response()` 钩子：**只 set_result 替换消息链，绝不
  stop_event()**——实测 4.29 调度器里 stop_event 会连 Respond 阶段一起拦掉，导致消息丢失。
  触发顺序 点歌拒绝 > 歌曲翻唱 > TTS；群聊必须 @Bot（At 组件核验）；翻唱进行中让路；
  同一条事件只处理一次；失败降级纯文字（钩子层吞异常，实测 call_event_hook 吞 BaseException）。
- `intent_gate.py` 新增 `TICKET_PATTERNS_TTS`（整句式组合，单独「说/听/声音」不触发）、
  `is_tts_request` / `is_cover_request` / `has_tts_ticket`。
- 配置：新 `tts` 卡片（enable/provider/model/inference/trigger/reverb 子卡），
  `CONFIG_PATHS` 同步扩展（104 键与 schema 双向校验一致）。默认关闭。

### 2. Reverb 升级（网关 gateway.py）

- 内核换成**合成 IR + FFT 卷积**（`build_reverb_ir` + `apply_reverb`，scipy.fftconvolve，
  gwvenv 自带 numpy/scipy，无新依赖）：wet/dry/room_size/decay/damping/pre_delay/width
  7+1 参数**全部真实生效**——decay=尾音 -60dB 时长、room=早反射跨度（首个 tap 位置可精确预测）、
  damping=尾音单极点低通截止、pre_delay=IR 前置静音、width=尾音左右去相关（0=真单声道）。
  IR 固定种子（确定性，利于复现），单位能量归一（wet=湿声能量比），峰值保护（>0.95 压回），
  输出保留自然尾音（≤4s）。只作用于 RVC 输出人声（网关本来就不混伴奏）。
- 旧 aecho 保留为 `echo_enabled/echo_wet/echo_decay` legacy 组（默认关，与 reverb 互斥，
  reverb 优先）；`build_reverb_filter/apply_reverb` 旧函数更名为 `build_echo_filter/apply_echo`。
- `/convert` 与新 `/tts_convert` 共用 `apply_reverb`；缓存 key 加入全部混响/echo 字段
  （旧缓存自然失效，不会错误命中）。
- 插件侧 `_get_reverb_kwargs()` 按名字探测新参数（老网关缺哪个就不传哪个）；
  默认值改为方案推荐：wet 0.18 / dry 1.0 / room 0.65 / decay 1.4s / damping 0.45 /
  pre-delay 18ms / width 0.80，**默认仍关闭**。

### 3. 网关 `/tts_convert`（新端点）

- 输入白名单：只接受 `/work/tts` 与 `/work/uploads` 下的文件（路径校验，拒绝任意路径）；
  输入统一转 44.1k mono wav（TTS 产物可能是 mp3/ogg）；时长上限 `GW_TTS_MAX_SECONDS`(60s)、
  大小上限 30MB；与 `/convert` 共用 `_TASK_SLOT` 全局串行 + `_rvc_stage`/`run_stage`
  （取消/超时/子进程登记全套复用）；输出 `out/<task_id>_tts.<fmt>` + host_path JSON + 任务日志；
  `tts/` 任务目录 TTL 清理（`GW_TTS_TTL`，默认 1h，启动即扫）。无缓存。
- 其他：`/model_info` 增加 `index_matches`（每模型 index 匹配情况）；
  `HOST_PLUGIN_DIR` 默认值从旧 matsuko 目录修正为 chorus_cover。

### 4. WebUI（pages/dashboard + page_api.py）

- 新页签「TTS 控制台」：服务商下拉（读 AstrBot 实配）/音色/音调/格式/Reverb 单次覆盖、
  文本框、原始 TTS 与 RVC 转换后双波形预览（复用 mountPlayer + `/rvc/result`）。
  新路由 `/tts/providers`、`/tts/generate`（并发 1、文本限长、任务记录进 webui_jobs.json）。
- 新页签「音频效果」：歌曲 Reverb 8 参卡片 + TTS Reverb 8 参卡片（互相独立）+
  legacy echo 折叠卡（高级）。配置保存复用 `/config/save`（白名单+范围校验自动覆盖新键）。
- 插件设置：schema 卡片保持键名不变（旧配置零迁移），TTS 卡片自动出现在分组页签。
- 插件设置页签现为 9 组（原 8 组 + TTS）：按升级方案「尽量控制 8 类」的精神，
  未合并既有卡片——移动既有键的嵌套路径会破坏 `_migrate_config_layout` 的兼容性，
  权衡后保留 9 组并在交付说明中注明。

### 5. 验证与回滚

- 本地测试：`tests/test_tts_intent.py`（意图判定 40+ 用例，含方案 8.2 全部混淆表）、
  `tests/test_reverb_kernel.py`（IR 形状/确定性/width/damping/pre_delay/room 语义、
  峰值保护）、`tests/test_config_sync.py`（104 键 schema 双向一致）——本地全绿；
  `py_compile` 全部通过、app.js node --check 通过。原有 tests/test_custom_autopick.py
  需在 NAS 上跑（路径常量指向生产目录），阶段 D 执行。
- 回滚：插件副本整目录替换回 0.12.2；网关用 `.bak-<时间戳>` 恢复 gateway.py；
  配置无需回滚（新增键独立，旧键未动）。
- 部署前置：网关容器重启加载新 gateway.py + 插件重载（生产操作，另行批准）。

---

## [CUSTOM-6.10] 2026-10-30 — 修复 WebUI 设置与原生配置不同步 + 混响接入配置 + 设置页签布局

1. **不同步的根因（实测实锤）**：页面 API 的配置读写用的是「点号键」（`basic.chorus_seconds`），
   而 `_NestedConfig` 适配层只认扁平键名（`CONFIG_PATHS` 的键）——点号键落在 map 之外，
   **读取**回退成 schema 默认值（磁盘 25，页面显示 20），**写入**变成顶层带点的垃圾键。
   修复：适配层 `_resolve`/`__setitem__` 对含 `.` 的键直接按路径读写。
   验证：读=磁盘真实值 25、写入落到 `basic.chorus_seconds`、无垃圾键、
   「原生页改盘→插件热重载→页面读到新值」与「我们的页面写入→原生页可见」双向通过。
2. **人声混响接入配置**（上一轮只在网关，插件未暴露）：
   - Schema `separation` 卡片新增 `reverb_enabled`(false)/`reverb_wet`(0–0.5, 0.15)/
     `reverb_dry`(0.5–1.0, 1.0)/`reverb_decay`(0.05–0.9, 0.4)，配置 JSON 同步默认值；
   - `main.py` 新增 `_get_reverb_kwargs()`：开关打开且网关 `/convert` 支持 `reverb_*` 参数时
     按名字提交（读取即时生效，无需重载插件），不支持时警告一次并跳过；
   - 缓存键天然包含（网关把混响参数计入缓存键）。
3. **设置页签布局**：分组页签独占一行（`#cfgGroupSeg { flex: 1 1 100% }`），
   「保存设置/重新读取」回到下一行左侧，不再跑到页签右边。
4. **验证**：适配层点号键读写闭环、双向同步、`_get_reverb_kwargs` 开/关与取值、
   全量测试 65/65、`py_compile` 通过。
5. 备注：混响参数在「音频分离与输出 → 人声混响」分组，改完点保存即可（无需重载插件）。

## [CUSTOM-6.9 附] 2026-10-30 — Gateway 内存上限 4g → 8g（并解释为什么之前不生效）

- 现象：用户昨晚执行过
  `docker update --memory 8g --memory-swap 12g rvc-gateway`，
  但 2026-10-30 检查时仍是 4 GiB。
- 原因：`docker update` 只改**容器运行时 HostConfig**，不会写回 `compose.yaml`；
  而本项目用 `docker compose up -d --build rvc-gateway` 部署代码（混响功能期间执行过多次），
  容器按 compose 重新创建，资源限制被打回文件里的 `mem_limit: 4g`。
- 处理：把 8g / 12g **写回 compose**，保证重启与后续重建都持久：
  - `mem_limit: 8g`（RAM 上限）
  - `memswap_limit: 12g`（内存+Swap 合计，即 8G 内存 + 4G swap，与 docker update 的参数一致）
  - `cpus: 3` 未动
- 依据：宿主机实测 `total 15Gi / available 12Gi`（早期「8G 主机」注释已过时，同步更正）。
  20~30s 片段 RVC 推理峰值约 4GiB，旧上限会走 swap 变慢但不会 OOM（`OOMKilled=false`）。
- 验证（重建后）：`HostConfig.Memory=8589934592`、`MemorySwap=12884901888`、
  `NanoCpus=3000000000`、`docker stats` 限额显示 8GiB、端口仍 3333→7865、
  `/model_info` 真实返回 3 个模型、`rvc-cpu-test` 仍 Exited、无新增容器/端口。
- 备份：`/vol1/1000/docker/RVC-Project/gateway/.bak-compose-mem8g-20261030-021650`
- 回滚：把备份的 compose.yaml 拷回并 `docker compose up -d rvc-gateway`。

## [CUSTOM-6.9] 2026-10-30 — Gateway 人声混响（默认关闭）

- 只改 Gateway 项目（`gateway/gateway.py`、`gateway/README.md`），
  未动 RVC 容器（rvc-cpu-test 仍停止）、未改 `rvc_base_url`、未改 AstrBot 插件。
- 新增 `/convert` 参数 `reverb_enabled`(默认 false)、`reverb_wet`(0–0.5, 0.15)、
  `reverb_dry`(0.5–1.0, 1.0)、`reverb_decay`(0.05–0.9, 0.4)，均为真实 `aecho` 参数。
- 后处理独立函数 `apply_reverb()` / `build_reverb_filter()` / `_as_bool()`，
  输出独立文件 `<task_id>_reverb.<fmt>`，失败时保留 RVC 原始输出并记录错误。
- 验证：混响单测（0.17s、25.14s、44.1kHz/2ch、电平不升不削波、5 类非法参数全拒、失败不留文件）；
  端到端关闭 207.9s / 开启 212.9s 各跑一次完整转换，记录 `reverb.applied` 与实际滤镜参数。
- 回滚：`/vol1/1000/docker/RVC-Project/gateway/.bak-reverb-20261030-015106/`。

## [CUSTOM-6.8] 2026-10-29 — 人格播报 / 真实步骤 / 切段 / 转换参数修复 / 交互细节

1. **成功播报恢复人格**：`_notify_llm_with_context` 增加了
   - `_find_persona()`（按 `name`/`persona_id`/`id` 三种键找人格）
   - `_is_generic_persona()`（识别系统默认「通用助手」人格，命中则改用第一个自定义人格，
     即聊天里真正在用的那个）
   - 新配置 `tuning.notify_persona_id`（留空=自动；可手工填「若叶睦」锁定）
   - 每次播报都打日志：`成功播报使用人格: xxx（提示词 N 字，来源=…）`
   - 提示词收紧：禁「好消息/耶/啦/咯/快去听听」等客服话术，要求 ≤20 字、保持省略号与低情绪。
2. **当前任务显示真实步骤**：`page_api._infer_gateway_stage()` 按 Gateway `tasks/<id>/`
   下的中间文件推断阶段：`clip.wav`→准备音源，`sep/`→人声分离，`sep/*_Vocals.wav`→变声推理；
   页面步骤按 1 取歌 / 2 人声分离 / 3 变声推理 / 4 发送 着色（完成绿勾、进行中橙、未到浅色），
   已运行时间改为本地秒级平滑计时（不再 3 秒跳一次）。
3. **RVC 控制台新增「切段」子标签**：网关新增 `/clip`（只挑段并导出，不做分离/变声，
   复用与翻唱相同的窗口挑选逻辑），页面 `/rvc/clip` 提交任务、结果可直接播放/下载；
   转换面板去掉「时长模式」，只保留「输出时长（秒）」，转换固定走 chorus 挑选。
4. **修复「开始转换报 Internal server error」**：Gateway 的 `index_rate` 是 `Textbox(str)`，
   之前按 number 传导致服务端报错；同时页面表单空值（NaN）会让插件接口直接抛异常返回 500。
   现在数值统一走 `page_api._num()`（空/非法/越界都回退并夹取），`index_rate` 按字符串传。
5. **交互细节**：任务详情弹窗关闭时自动停止播放；插件设置按大分组做成页签切换；
   内容区整体放大（正文字号 15、宽度上限 1520、间距加大）。
6. **验证**：路由 20 条；`_num`、`_find_persona`、`_is_generic_persona`、
   `_infer_gateway_stage`（三种目录形态）单测通过；全量测试 65/65；`py_compile` 通过。

## [CUSTOM-6.7] 2026-10-29 — 视图切换失效修复 + 当前任务卡片重做

1. **修复「切到历史任务，当前任务的卡片还在；当前任务的工具栏跑到历史视图」**
   根因：`#paneCurrent/#paneHistory/#taskHistoryBar/#taskPagerBar` 都设了 `hidden`，
   但它们的 CSS `display:flex` 覆盖了 UA 的 `[hidden]{display:none}`。
   修复：全局加 `[hidden] { display: none !important; }`。
2. **当前任务卡片重做**：
   - 左右布局：正在处理约 70%，排队中固定 30%；
   - 正在处理显示歌名/歌手（`_acquire_cover_slot` 支持 meta，三个翻唱入口在提交前写入
     `_pending_cover_meta`，排队者与运行者都带歌名歌手）；
   - 内嵌步骤列表：取歌与音源准备（绿勾）→ 人声分离（橙色进行中）→ 变声推理 → 发送音频（浅色未到）；
     Gateway 内部无分步回调，步骤以其实际阶段为准（分离+变声合并期间显示为分离进行中）。
3. **验证**：`py_compile`、JS `node --check`、CSS `[hidden]` 规则、`active.song` 渲染。

## [CUSTOM-6.6] 2026-10-29 — 翻唱队列（默认 3 人）+ 当前任务视图 + 界面修正

1. **真实排队**：`_predict_cover` 改为排队包装（`_predict_cover_impl`）：
   同一时间只允许一个推理，`advanced.cover_queue_limit`（默认 3）控制最多等待者，
   超出直接报「当前排队已满（上限 3 人），请稍后再点」；0 = 不排队（空闲可直接跑，占用中直接拒绝）。
   状态保存在内存（`_cover_active` / `_cover_queue`），队列快照 `_queue_snapshot()`。
2. **当前任务视图**：页面 API 新增 `GET /active_tasks`；翻唱任务页新增「当前任务 / 历史任务」切换，
   当前任务显示：运行中的歌（文件名）、阶段、已运行秒数（不确定进度条）与排队列表（位置/已等待），
   3 秒轮询（仅停留该视图时）。
3. **界面修正**：
   - 任务列表改为「上歌名歌手 / 下文件」两列并存（`歌曲 / 歌手` 新增，原 `文件 / 输入` 保留）；
   - 分页控件移到搜索栏同一行最右侧；滚动条移到面板内部（`.panel-body`），
     搜索栏/保存按钮不再被滚动条穿过，也不随内容滚走；
   - 分段控件改为实底白底 + 内嵌蓝色边框选中态，修复选中边框与外框重叠、底色与背景同色的问题；
   - 侧栏配色恢复 CC Switch 风格（浅灰底 + 浅灰选中项），仅保留上一轮的尺寸/字号调整。
4. **验证**：队列语义单测 9/9（占用/排队/满员拒绝/位置/FIFO/limit=0）；路由 19 条；
   全量测试 65/65；JS `node --check`、HTML 配平。

## [CUSTOM-6.5] 2026-10-29 — 侧栏配色/尺寸、任务显示歌名歌手、顶部状态仅概览

- **侧栏**：底色改为 `#eaeef4`（暗色 `#12161a`）；宽度 236→244，导航项 `min-height:40px`、
  字号 14.5px、内边距 0 12px，选中态改为蓝底白字 + 轻阴影（对齐 Self Learning 的观感）。
- **任务列表显示歌名/歌手**：
  - `main.py` 新增 `_record_cover_history()`：成品发送时把 `{song, artist, model}` 写入
    `plugin_data/astrbot_plugin_chorus_cover/cover_history.json`（上限 300 条、原子写）；
  - `_send_cover_result()` 增加 `artist/model/record` 参数，三条成功路径分别传入歌曲信息；
  - `page_api._read_task_records()` 合并该历史，任务列表「歌曲 / 输入」列改为**上歌名下歌手**；
    概览最近任务同样显示歌手；
  - 旧任务用日志保守回填 14 条（发送时间紧邻 `stage=backend_done`、且 flow 未被占用才写入），
    对不上的 2 条保持未知，不做猜测。
- **顶部状态**：「插件 / Gateway / 刷新数据」只在「概览」显示，其他标签隐藏。
- **验证**：历史合并 10/10 条有歌名、14 条历史、`py_compile` 通过、JS `node --check` 通过、
  15 项断言全部通过。

## [CUSTOM-6.4] 2026-10-29 — 吸顶工具栏 / 侧栏加宽 / 波形播放器

- **布局**：`.main` 不再整体滚动，改为「`.topbar` 固定 + `.content` 独立纵向滚动」；
  各页 `.toolbar`（搜索框、状态下拉、保存设置等）与 RVC 的转换/分离切换 `.seg-lg` 设为 `position: sticky`，
  长页面滚动时标题与操作栏保持可见，侧栏「诊断」也不会再被顶出屏幕。
- **侧栏**：宽度 208→236，导航字号 13→14、图标 16→18、条目内边距加大。
- **音频播放器**：新增波形播放器（Canvas 绘制 + 点击/拖动波形跳转 + 播放/暂停 + 时间 + 下载）。
  - RVC 控制台的转换/分离结果改用它；
  - 翻唱任务详情新增播放器：新接口 `GET /task_audio?id=<task_id>` 从网关 `logs/<id>.json`
    读取成品（仅允许 `gateway_work/out` 内文件，>24MB 拒绝），页面用 base64 播放；
  - 浏览器无法解码时自动回退原生 `<audio controls>`。
- **验证**：路由 18 条（含 `/task_audio`）；真实任务 `73595bd1c6be` 返回 4.2MB / 25.0s 音频；
  前端结构配平、JS `node --check`、13 项断言全部通过。

## [CUSTOM-6.3] 2026-10-29 — 界面放大/改名/标签页标题/内滚动/任务分页

- **改名**：`metadata.yaml` 的 `display_name` 改为 `RVC翻唱`；页面 i18n `pages.dashboard`
  中英分别改为 `RVC翻唱` / `RVC Cover`；页面 `<title>` 与左侧导航品牌同步改名。
- **尺寸与居中**：内容区新增 `.main-inner`（`max-width: 1360px; margin: 0 auto`），
  页面整体字号 14→14.5、标题 20→22、KPI 19→21、卡片内边距 14/16→16/18，侧栏 196→208。
- **标签页标题**：右上标题不再固定「合唱翻唱」，按标签显示「概览 / 翻唱任务 / RVC 控制台 /
  模型管理 / 插件设置 / 诊断」；副标题改为该页一句说明，移除了 `Gateway 地址` 副标题。
- **内滚动**：`.shell` 固定 100% 高、`.main` 独立纵向滚动、`.side` 使用 `position: sticky` +
  `height: 100vh`，长页面下侧栏「诊断」不再被顶出屏幕；窄屏（≤760px）恢复整页滚动。
- **任务分页**：每页 30/50/100/200 可选，带上一页/下一页、页码与「第 x–y 条，共 n 条」；
  切换筛选或搜索时自动回到第 1 页。
- **验证**：HTML 结构配平、关键元素齐备、CSS/JS 断言 16/16、JS `node --check`、
  真实 `PluginPageService` 页面发现仍为 `dashboard`。

## [CUSTOM-6.2] 2026-10-29 — 修复“翻唱任务被静默取消、用户无输出”

- **现象**：15:58 点歌《绿色》，14:58:45 开始、14:58:47 识别出模型性别后完全停住，
  15:01:06 只留下两行日志：`翻唱任务结束 -> 已完成` + `后台任务已取消: matsuko_cover:_smart_cover_async`，
  用户没有收到任何消息或音频。
- **根因**：性别识别用 `asyncio.wait_for(coro, 15s)`，Provider 请求超时/中断时抛出的是
  **`CancelledError`（BaseException 子类）**，而外层只写了 `except Exception` → 未被捕获，
  异常一路冒泡到 `_smart_cover_async`，把整个后台翻唱任务标记为 cancelled；
  `finally` 里的 `_finish_active_task(event)` 又用默认文案 `已完成`，日志看起来像成功，实际什么都没做。
  同期 AstrBot 日志有 `[OpenAI] Request failed ... Request timed out`，与该时间点吻合。
- **修复**：`_with_timeout` 增加 `except asyncio.CancelledError`，用 `current_task().cancelling()`
  区分「内部取消（Provider 中断）」与「外部取消（插件重载/取消命令）」：
  内部取消 → 记 WARNING、按未知处理并继续翻唱；外部取消 → 照常向上传播。
  `_smart_cover_async` 增加 `except asyncio.CancelledError`，如实把会话状态记为「已取消」。
- **验证**：单测 3/3（内层取消继续、正常识别、外部取消仍传播）；`py_compile` 通过。
- **建议**：给三个音色在 WebUI「模型性别」填好，模型侧就不再调用 LLM，彻底避开此类等待。

## [CUSTOM-6.1] 2026-10-29 — 页面桥接修复 / 参考风格改版 / 人声伴奏分离

1. **修复「bridge 不可用 → 页面无数据」**
   - 根因：`index.html` 未引用 bridge SDK，AstrBot 把 bridge 注入在 `</body>` 之后，
     而 `app.js` 在解析时就缓存了 `window.AstrBotPluginView`（当时为 undefined），之后永远拿不到。
   - 修复：页面显式引入 `/api/plugin/page/bridge-sdk.js`（AstrBot 会替换成带 token 的地址）；
     `app.js` 改为**懒获取 + 轮询等待（最多 10s）**，并区分「桥接缺失」与「Gateway 异常」两种提示。
2. **参考截图风格改版**
   - 左侧图标导航（196px，选中浅灰底 + 蓝色图标）替代顶部标签，右上角橙色主按钮；
   - 分段药丸控件（任务状态筛选、RVC「转换 / 人声伴奏分离」切换）；
   - 模型列表改为带边框的行式列表，默认音色行使用浅蓝底 + 蓝边（同截图选中态）；
   - 卡片改为细边框白底，减少阴影；窄屏折叠为顶部图标条。
3. **RVC 控制台新增「人声 / 伴奏分离」**
   - 网关新增 `/separate`（真实分离，不跑 RVC）：输入音频 + 分离模型 + 处理方式（两者/仅人声/仅伴奏）
     + 导出格式（wav/flac/mp3/m4a），输出写入 `gateway_work/out/`；
   - 页面 API 新增 `GET /rvc/sep_models`、`POST /rvc/separate`，任务结果支持**多个输出**
     （`outputs[]`，前端为每个输出提供播放器与下载）；
   - 实测：33.9s 音频分离耗时 95.7s，人声/伴奏各 11.9MB、33.91s，md5 不同（确认不是同一文件）。
4. **验证**：网关 /separate 真实调用通过；分离 API 7/7；17 条路由注册；JS `node --check` 通过。
   浏览器内渲染仍需人工确认（未验证）。

## [CUSTOM-6] 2026-10-29 — AstrBot 原生插件页面（合唱翻唱管理界面）

- **机制（实测确认，非编造）**：AstrBot 4.29 支持插件页面：
  插件根目录 `pages/<页面名>/index.html` 会被 `PluginPageService` 自动发现（目录名即页面名，
  标题取 i18n `pages.<name>`）；页面通过 bridge SDK 调
  `/api/plug/<插件名>/<路径>`，后端用 `context.register_web_api(route, handler, methods, desc)` 注册；
  处理器里 `from quart import request` 读参数/JSON，返回 `{"status":"ok","data":...}`。
- **新增文件**：
  - `page_api.py`（15 条路由：overview/health/tasks/models/config/rvc/diagnostics）
  - `pages/dashboard/index.html`、`assets/app.css`、`assets/app.js`
  - `.astrbot-plugin/i18n/zh-CN.json`、`en-US.json`（菜单标题「合唱翻唱」）
- **后端能力**：真实调用 Gateway `/model_info`（3 个模型、目录）、读 Gateway 任务记录
  （`gateway_work/logs/*.json`）、读写插件配置（白名单+范围校验+敏感项排除）、
  刷新模型（复用 `_update_models_from_api`）、设置默认音色/别名/性别、
  RVC 转换（上传 → /convert → 轮询 → base64 返回音频）、诊断（连通性/配置检查/日志错误截取）。
- **前端**：6 个标签页（概览 / 翻唱任务 / RVC 控制台 / 模型管理 / 插件设置 / 诊断）；
  普通页面用圆角卡片式后台风格，仅 RVC 控制台使用传统 RVC 密集表单风格；
  灰度层级优先，颜色只用于状态与主操作；窄屏自动单列。
- **安全**：不支持任意路径读写；上传只落 `gateway_work/uploads`（扩展名/体积校验），
  结果只允许 `gateway_work/out` 内文件（base64 返回，不暴露路径）；不新增端口/容器。
- **验证**：py_compile、Schema/i18n JSON、真实 `PluginPageService` 发现页面、
  15 条路由注册、真实 Gateway 读接口 15/15、JS 语法 `node --check`、CSS 括号配对。
  浏览器内实际渲染与点击仍需在 AstrBot 面板人工确认（未验证）。
- **回滚**：删除 `page_api.py`、`pages/`、`.astrbot-plugin/`，并用
  `plugin_backups/astrbot_plugin_chorus_cover.webui-<时间戳>/main.py` 覆盖回去后重载插件。

## [CUSTOM-5.1] 2026-10-29 — 模板文案与 LLM 播报解耦 + 通知人格化

- **反馈**：CUSTOM-5 的“LLM 说了就不发模板”耦合逻辑不合理；且 LLM 那句“耶～你的《龙卷风》翻唱做好啦！”
  不像人格（人格平时是“……邓紫棋的版本。\n我去准备一下。”这种简短风格）。
- **改动**：
  1. 两个开关完全独立：「成功时发送模板提示（含本次配置）」控制固定文案（含“⚡命中缓存”）；
     「成功后让 LLM 用人设播报」控制 LLM 那句。删掉 `llm_said` 耦合逻辑。
  2. `_notify_llm_with_context` 现在会取**当前会话人格**（`persona_manager.resolve_selected_persona`）
     并带上最近 6 条上下文传给 `provider.text_chat(system_prompt=..., contexts=...)`；
     提示词改为“一到两句、不超过 20 字、保持一贯语气、别堆感叹号和卖萌语气词”。
  3. 失败/命中缓存也走同一套人格化通知。
- **验证**：新增 6 项测试（人格 system_prompt 透传、上下文带入、提示词约束、回复发送、
  模板与 LLM 解耦的静态断言），测试集 65/65。
- **回滚**：`plugin_backups/main.py.bak-20261029-custom51`。

## [CUSTOM-5] 2026-10-29 — LLM 接管时去掉重复罐头文案 + 首尾淡入淡出

1. **重复文案（BOT 感）**：`enable_llm_success_notify` 本身生效——那两句“好消息～”确实是 LLM 生成的；
   问题在于代码同时发了硬编码卡片（`已成功使用 RVC 音色…本次配置：模型=…`），一次翻唱出现两段文案。
   - 改动：成功时**先**让 LLM 用人设说话；LLM 说了（开关开 + 有 provider + 有返回）就不再发罐头文案，
     也不发“⚡ 命中缓存”提示；LLM 关闭/失败时才用固定文案兜底。
   - 覆盖三处：smart 流程、本地音频流程、缓存命中提示。
2. **首尾淡入淡出 0.5 秒**：
   - 网关 `convert` 新增 `fade_seconds` 参数（0~5），在最终精挑后对成品做 `afade` 淡入/淡出，并计入缓存键；
   - 插件新增配置「输出首尾淡入淡出 0.5 秒」（分离与输出 → 输出），默认开；网关不支持时自动跳过并记一次日志。
   - 验证：容器内单测头 100ms RMS 0.0013 / 中段 0.1865 / 尾 100ms 0.0045；全链路 E2E 见记录。
3. 回滚：`gateway/.bak-20261029-custom5-gateway.py`、`plugin_backups/main.py.bak-20261029-custom5`、
   `_conf_schema.json.bak-20261029-custom5`。

## [CUSTOM-4.6] 2026-10-29 — 修复“永远 30 秒”（too_short 短路）

- **根因**：候选片段长度（目标+缓冲）≈ 30s，而最终精挑调用传入的 target 也 ≈ 30s，
  `pick_chorus_window` 在开头有 `if total <= max(MIN, length + 0.5): return 整段("too_short")`，
  于是**永远直接返回整段**，根本没进入 CUSTOM-4.2 的多长度评分 —— 这就是两次都是 29.98s 的原因。
- **修复**：变长模式（给了 min_target）下，只有 total ≤ min_target+0.5 才短路；否则照常评分。
- **验证**：同一文件上 25/26/27/28/29/29.98 六档评分，25s 分最高（0.06476 vs 0.06034）；
  修复后 `pick_chorus_window(25~30)` 返回 start=2.70 len=25.00；偏好 0.03/0.08/0.15 均为 25.00。
- **回滚**：`gateway/.bak-20261029-custom46-gateway.py`。

## [CUSTOM-4.5] 2026-10-29 — 人声偏轻时多段评估取最响

- **复测发现**：只换一段还不够——《泡沫》(314.8s) 第一次 242~272s 为 -90.6 dBFS，
  换到 273.9~303.9s 仍 -97.8 dBFS，再换 174.2~204.2s 人声 -45.1 dBFS（能出但偏轻，成品 -38.4dB）。
- **改动**（网关）：
  1. 区分两档阈值：`GW_VOICE_SILENT_DB`(-50，低于则直接报错) 与 `GW_QUIET_VOICE_DB`(-35，低于则继续换段)；
  2. 重试次数默认 2；每次尝试记录人声电平，**最后取人声最响的一段**（重试更差不会反而变差）；
  3. 重试时排除已试区间并限制在整首前 85%（避开尾奏）；
  4. 仍全部低于 -50 才报错；报错文案说明“可能正好是间奏/尾奏，换个版本或指定时间段”。
- **回滚**：`gateway/.bak-20261029-custom45-gateway.py`。

## [CUSTOM-4.3] 2026-10-29 — 静音段保护 + 最终长度偏好

- **问题**：12:54 的《泡沫》(task 2bcc99092af7) 成品 mean -79.5dB（实质静音）：
  候选窗口选在 242~272s（314.8s 歌曲的间奏/尾奏），分离出来几乎没人声；
  另两次输出都是 29.98s，看起来像固定长度。
- **改动**（网关 `gateway.py`）：
  1. `audio_rms_db()`：算分离后 vocals 的平均电平；
  2. 分离后电平 < `GW_VOICE_SILENT_DB`(-50) → 自动避开该区间换一段重试（`GW_SEP_SILENT_RETRY=1`，独立 sep 子目录），
     仍静音则 `GatewayError`（插件会把失败原因回给用户），**不再发送静音成品**；
  3. `pick_chorus_window(..., avoid_range=)`：支持避开已判定静音的区间；
  4. 最终长度加 `GW_LENGTH_PREFERENCE=0.03` 轻微偏好：只要不明显更好就不拉长，25~30 会按内容浮动；
  5. 显式指定 start/end 的请求不重试（尊重用户指定区间）。
- **验证**：`audio_rms_db` 对静音成品 -79.8dB、正常成品 -13.8dB 判定正确；
  用同一首《泡沫》复测：首次仍选到 242~272s → 检测静音 → 换段 → 输出正常（见 E2E 记录）。
- **回滚**：`gateway/.bak-20261029-custom43-gateway.py` + `docker compose up -d --build rvc-gateway`。

## [CUSTOM-4.2] 2026-10-29 — 最终片段长度可变 + 默认音色命令

1. **最终输出长度不再固定**（网关 `pick_chorus_window`）
   - 需求：配 25 秒 + 5 秒缓冲，应输出「25~30 秒之间最优的一段」，而不是固定 25 秒最优。
   - 实现：`pick_chorus_window(..., min_target=...)` 在 [min_target, target] 之间按 0.5 秒步进
     一起评分，取得分最高者；固定长度调用方式与候选切片逻辑不变。
   - 验证：容器单测 4/4（25~30 取到 25.0、20~25 取到 21.5、固定 20、min>target 退化固定）；
     真实 25 秒请求输出见 E2E 记录。
2. **新增 `/设置默认音色 <模型名>`（管理员）**：直接写 `rvc.models.rvc_default_model` 并保存，
   WebUI 下拉刷新后被清空时可用此命令一键恢复；不带参数显示当前值与可用模型。
3. 说明：`/补发翻唱`（CUSTOM-4.1）仍用于重载中断后的成品补发。

## [CUSTOM-4.1] 2026-10-29 — 翻唱成品补发（重载中断不再白跑）

- **背景**：12:16 的《唯一》任务在 12:20:33 因插件重载被取消，而网关 12:20:39 才出成品，
  导致成品留在 `gateway_work/out/` 但没发出去（日志无 backend_done/发送记录）。
- **改动**（`main.py`）：
  - 新增 `GATEWAY_OUT_DIR`、`resolve_resend_file()`、`list_recent_outputs()`；
  - 新增管理员命令 `/补发翻唱`（不带参数=列出最近成品；带 task_id/文件名/目录内绝对路径=直接补发）；
  - `terminate()` 在有进行中任务时打 WARNING，提示成品位置与补发命令。
- **验证**：9 项新测试通过（task_id/完整名/前缀/目录内绝对路径/目录外拒绝/不存在/倒序/空参数/真实文件可解析），
  测试集 52/52。
- **回滚**：`/vol1/@appdata/astrbot/data/plugin_backups/main.py.bak-20261029-custom41`。
- **限制**：补发只发已存在的成品，不补齐失败任务；成品目录路径为常量，网关工作目录变更时需同步修改。

## [CUSTOM-4] 2026-10-29 — 翻唱时长缓冲 / 下拉保值 / 性别合并 / 嵌套配置卡片

1. **候选切片缓冲 15s → 5s，并新增分离后精挑**（网关 `gateway.py`）
   - 查明：旧代码/文档里的「+15 秒」实际只存在于 Schema 说明文字，网关照的是
     `target = min(requested, MAX_CLIP_SECONDS=30)`，且分离推理后没有精挑步骤。
   - 行为变化：chorus 模式候选切片 = 目标 + `GW_SEP_BUFFER_SECONDS(5)`；
     分离推理后再用同一套挑段逻辑在候选内精挑「目标时长」输出；custom 不加缓冲；
     full 仍明确拒绝。`GW_MAX_CLIP_SECONDS` 30 → 200（支持 180 目标），>60 秒打资源警告。
   - 验证：容器内 `compute_clip_target` 6 例全过（45→50、20→25、180→185、边界 30/12、0→5）；
     真实 20s 请求日志：候选 25.0s → 分离 78.8s → 推理 125.6s → 精挑输出 20.00s（ffprobe 20.000000）。
   - 回滚：`gateway/.bak-20261029-custom4-gateway.py`、`.bak-20261029-custom4-compose.yaml`，
     然后 `docker compose up -d --build rvc-gateway`。
2. **配置改为两层 object 卡片**（`_conf_schema.json` + `main.py`）
   - 8 张外层卡片，其中 selection/rvc/tuning/separation 各含内层子卡片；
     本地音频 7 项集中在一张卡片；不生效参数（inst_volume_db/shift_accompaniment）如实标注。
   - 新增适配层 `_NestedConfig` + 路径表 `CONFIG_PATHS` + 幂等迁移 `_migrate_config_layout`；
     命令写入自动落到嵌套路径。
   - 配置 JSON 已迁移（64 项，嵌套为准）；用真实 `AstrBotConfig` 在副本上验证 23 项抽查全保值，
     旧扁平键被 AstrBot 正常清理，顶层只剩 8 张卡片。
   - 回滚：`.bak-20261029-custom4/_conf_schema.json`、`main.py`、配置 `*.bak-20261029-custom4`。
3. **模型性别合并为唯一来源**（`main.py`）
   - `model_genders`（每音色下拉：未知/男声/女声）成为唯一权威；旧 `model_gender_map`
     首次启动并入后清空；非 Schema 模型写 `model_genders_extra`（type dict，不会被清理）。
   - WebUI 与 `/设置模型性别` 读写同一份数据；unknown=不参与自动升降调；
     已配置模型不再调用 LLM；手动升降调优先级不变。
   - 验证：迁移/写入/未知清除 8 项测试通过。
4. **刷新保值加固**（`main.py` `_update_models_from_api`）
   - 空列表/异常 → 返回失败、绝不写空；成功刷新保留别名、默认音色、性别映射；
     默认音色失效时保留原值并提示。5 组测试覆盖。
5. **清理**：默认音色归一为不带 `.pth`（与下拉选项一致）；Schema 默认值与已存值对齐。
7. **备份目录移出插件扫描区**（数据迁移，无代码改动）
   - 现象：WebUI 显示「加载失败插件（3）」，报 `No module named 'data.plugins.astrbot_plugin_chorus_cover.bak-...'`。
   - 根因：`cp -a` 备份建成了 `plugins/astrbot_plugin_chorus_cover.bak-*`，AstrBot 把含 main.py 的
     目录都当插件扫描，而带点的名字不是合法模块名。
   - 处理：三个备份整体移动到 `/vol1/@appdata/astrbot/data/plugin_backups/`（4.7MB，未删除任何文件）；
     主插件 `astrbot_plugin_chorus_cover` 12:05 重载日志确认加载成功，CUSTOM-4 代码正常生效。
   - 后续约定：新增备份一律放 `data/plugin_backups/`；插件内部 `.bak-*` 不受影响。
6. **配置去重与标签统一**：删除配置文件里与嵌套重复的 64 个扁平键；模型性别值统一为
   下拉选项的中文标签（女声/男声/未知），避免「存 female、选项是中文」导致 WebUI 显示空；
   旧 `model_gender_map` 迁移后清空。迁移前配置仍在 `*.bak-20261029-custom4`。

## [CUSTOM-3] 2026-10-29 — 选歌统一 / NameError / 配置 UI / 性别交互 / 刷新保护

1. **修复 `_claimed_original_name` NameError**（`main.py`）
   - 原因：CUSTOM-2 补丁把 helper 定义为类静态方法，调用点却用了裸函数名；
     方法内裸名只查模块全局 → 真实 QQ 报 `name '_claimed_original_name' is not defined`。
     此前的离线测试用 exec 提取到同一命名空间，掩盖了该缺陷（教训：测试必须 import 真实模块）。
   - 行为变化：`/rvc` 不再崩溃；调用改为 `self._claimed_original_name(...)`。
   - 验证：`tests/test_custom_autopick.py`「NameError 回归（真实作用域）」+ 真实日志无新异常。
   - 回滚：`.bak-20261029-022435/main.py`。
2. **统一自动选歌入口**（`main.py`）
   - 新增公共函数 `_auto_pick_from_sources()`；`/rvc`（`_handle_cover`）、LLM 工具与
     自然语言（`rvc_cover` → `_do_cover`，`smart_cover` → `_smart_cover_async` → `_do_cover`）、
     批量翻唱全部经由它；带歌手提示时把歌手拼进搜索词（搜「周杰伦 晴天」可命中真原唱）。
   - 行为变化：自然语言翻唱与命令翻唱同一套评分（原唱优先/冒名降权/别名/逐源/指定版本尊重）。
   - 验证：静态检查两处调用点 + 公共函数 mock 路由测试 + 真实探测（晴天→菜单；周杰伦晴天→Jay 原唱 112 分）。
   - 回滚：同上备份。
3. **尊重用户指定的翻唱版本**（`main.py`）
   - 查询里明确点名版本（女声版/DJ版/钢琴版…）时，命中同标记的候选不再 −25 降权，改 +10。
   - 验证：测试「用户指定翻唱版本被尊重」（女声版 59.9 分胜出）。
4. **配置页重排 + 如实标注**（`_conf_schema.json`，64 键全保留）
   - 按 基础/选歌与交互/RVC 音色与推理/自动升降调/音频分离与输出/本地音频/LLM 交互/网易云/缓存诊断
     排序；`inst_volume_db`、`shift_accompaniment` 标注「当前 Gateway 不使用」。
   - 新键：`auto_pick_fallback_menu`（选歌失败是否回退菜单）、`model_genders`（object 下拉）。
   - `rvc_default_model.options` 修正为网关真实模型 `["deng","nuoyemu","rym"]`（上游原为 8 个示例）；
     `chorus_seconds.default` 45→20、`inference_timeout.default` 300→600（防 WebUI 回写）。
   - 回滚：`.bak-20261029-022435/_conf_schema.json`。
5. **性别配置交互化**（`main.py` + Schema + 命令）
   - 新增 `model_genders` object（每个模型一个下拉：unknown/male/female），WebUI 直接选；
     新命令 `/设置模型性别 <名> <男声|女声|未知>`、`/设置歌手性别 <歌手> <男声|女声|未知>`（管理员）。
   - 旧 `model_gender_map` 字符串列表兼容保留，同键时下拉优先；加载时对象从未设置(unknown)的键
     会被旧列表值播种（界面与生效值一致）。
   - 不硬编码 deng/nuoyemu/rym 性别——当前值为用户自配（均 female）。
   - 验证：merge/norm 单测 + 配置解析。
6. **刷新保留已选值**（`main.py` `_update_models_from_api` / `refresh_rvc_models`）
   - 刷新只更新 `rvc_models_keywords`，绝不动 `rvc_default_model`/性别映射；
     默认音色不在新列表时回复明确提示（配置值原样保留，不清空）。
   - 验证：`_default_model_note` 单测（在列表/不在/未配置/.pth 差异）。
7. **配置治理**：`rvc_default_model` 归一为 `nuoyemu`（与下拉一致，运行时两种写法都认）。

## [CUSTOM-2] 2026-10-29 — 自动选歌第一轮

1. `_auto_pick_song()` 启发式评分：标题 difflib×50、标题命中+25、歌手命中+30、
   非原唱标记−25、原唱标注+20、来源顺序+8递减；分数<45 或分差<8 → 回菜单不硬猜
   （`main.py`；配置 `auto_pick_song/min_score/min_margin`）。
2. `_handle_cover` 接入自动选歌与默认音色快捷通道（`auto_pick_model` + `_configured_rvc_model_index`）。
3. 备份：`.bak-20261029-plugin-010433`。回滚即恢复该目录。

## [CUSTOM-1] 2026-10-29 — 网关契约与基础参数

1. 分离模型常量 `MSST_DEFAULT_MODEL`：`bs_roformer_ep_317_sdr_12.9755` → `3_HP-Vocal-UVR.pth`
   （修复 QQ「把伴奏和人声分开的那一步缺了个东西」；根因=网关分离目录无 bs_roformer）。
2. 网关侧（`/vol1/1000/docker/RVC-Project/gateway/gateway.py`）：`/convert` 对不可用 msst_model
   回退到网关默认并打 WARNING（保留请求值/实际值，绝不静默）。备份 `.bak-20261029-deploy-gateway.py`。
3. 20 秒默认片段（`chorus_seconds=20`）、推理超时 600s；`inference_timeout` 300→600。
4. 网关模型目录确认：`/vol1/1000/ai/rvc/weights`（deng/nuoyemu/rym）即用户原有模型；
   `rvc_default_model=nuoyemu`。
5. 证据：回退 WARNING 日志、15s 片段 132.5s 完整推理、缓存命中 1.0s/0.3s。

## 历史信息待核实

- 上游 0.12.2 之前是否有本机改动：**待核实**（插件目录 10-28 21:24 的文件时间戳是安装/同步时间，
  无法确认更早的个性化；如有请补充）。
- `selection_ui` / `outbound` / `intent_gate` 等子模块：当前未改动（与上游 0.12.2 一致性**待核实**）。
