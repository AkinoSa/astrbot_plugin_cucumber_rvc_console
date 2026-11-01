# UPSTREAM_COMPARISON · 与原版 matsuko_cover 的功能对照

> 上游:[sdfsfsk/matsuko_cover](https://github.com/sdfsfsk/matsuko_cover) v2.13.0(AGPL-3.0,基于 CCYellowStar2/astrbot_plugin_rvc_svc 扩展)。
> 当前:astrbot_plugin_cucumber_rvc_console 1.0.0(源码级对照,上游 commit 克隆于 research/matsuko_cover)。
> 变化类型:【继承】【改造】【增强】【新增】【移除】【未验证】。
> 判定依据:当前源码 + PROJECT_HISTORY.md + 交付报告;原版功能不直接计为当前功能。

## 总体结构对照

| 维度 | 原版 v2.13.0 | 当前 1.0.0 | 变化类型 |
|---|---|---|---|
| 代码规模 | ~6300 行(单 main.py 4995) | ~9000 行(main 6300+page_api 1500+tts/task_registry 等) | 【改造】 |
| 转换引擎 | RVC + SVC-Fusion + SoulX-SVCVC 三引擎 | 仅 RVC | 【移除】SVC/SVCVC(实际无人使用,维护成本高) |
| 推理后端 | 外部 Gradio 后端(需用户自备) | 自研 **cucumber-rvc-gateway** Docker 项目(分离+推理+TTS 接口+缓存) | 【新增】 |
| 配置结构 | 80+ 扁平键 | 两层嵌套卡片 112 键 + 适配层(兼容旧扁平键) | 【改造】 |
| WebUI | 无 | 原生插件页面 8 标签(概览/任务/RVC 控制台/TTS 试音台/模型管理/音频效果/设置/诊断) | 【新增】 |
| TTS 语音回复 | 无 | 完整链路(AstrBot TTS → RVC 变声 → 发送;二段式人格反应+台词) | 【新增】 |
| 许可证 | AGPL-3.0 | AGPL-3.0(继承,未改动) | 【继承】 |

## 功能逐项对照

| 功能 | 原版实现 | 当前实现 | 变化类型 | 验证依据 |
|---|---|---|---|---|
| 音源:网易云 | nodejs API 单源 | meting 主路+nodejs 备路+cookie 支持+cookie 失效告警 | 【增强】 | source_router.py/netease_fetch.py;真实群实测取歌 |
| 音源:QQ 音乐 | 直连+风控重试+第三方 VIP API | 经点歌插件 astrbot_plugin_music 桥接(酷狗/酷我/QQ) | 【改造】(方案:音源路由 source_order) | source_router.py;实测 |
| 音源:酷狗/酷我 | 无 | 点歌插件桥接 | 【新增】 | source_router.py `_attempt_reference` |
| 音源:伴奏/试听过滤 | 有 | 启发式评分含伴奏淘汰+版本指定加分 | 【增强】 | source_router.py `_auto_pick_song` |
| 自动选歌 | 无(手选菜单) | 评分制自动选歌(阈值/分差可配)+统一入口 | 【新增】 | main.py `_auto_pick_from_sources`;单测 65 项 |
| 片段裁剪 | start/end/duration 严格校验 | 保留严格校验 + 副歌启发式挑选(能量包络+停顿吸附)+变长输出 | 【增强】 | gateway.py `pick_chorus_window`;实测 |
| 人声分离 | 依赖后端 UVR5/MSST 参数 | 3HP(PyMSS)固定模型+静音检测+换段重试 | 【新增】 | gateway.py `_sep_stage` |
| RVC 推理 | Gradio /convert 调后端 | 子进程 infer/cli.py+常驻 worker 双模式(失败回退) | 【改造+增强】 | gateway.py `run_rvc_stage` |
| 混响 | reverb_intensity 传参(后端不消费) | 合成 IR 卷积混响 8 参数真实生效(wet/dry/room/decay/damping/pre-delay/width)+峰值保护 | 【新增】(原参数为无效传参) | gateway.py `apply_reverb`;kernel 单测 |
| 模型发现 | keywords 手工配置 | 网关动态扫描+index 自动匹配+无索引降级+index 状态展示 | 【增强】 | gateway.py `list_rvc_models` |
| 推理参数 | f0/index_rate/protect/filter_radius 等 | f0/index_rate/protect/rms_mix_rate/resample_sr 实时生效;无效参数明确记录 | 【改造】 | gateway.py `/convert` 参数表 |
| 任务队列 | 无(单任务+会话锁) | TTS/翻唱独立队列+全局等待上限+网关全局串行(忙时等待) | 【新增】 | task_registry.py+gateway.py `_TASK_SLOT` |
| 任务注册表 | 无 | task_id 级状态机(9 状态合法流转)+防重复+按用户查询/取消 | 【新增】 | task_registry.py;单测 8 组 |
| `/cover_status` | 无 | 用户任务状态查询(隐私隔离) | 【新增】 | main.py `cover_status_cmd` |
| 取消任务 | 取消当前会话任务 | 按用户取消全部(翻唱+TTS;排队即取消/运行软取消;Gradio job+子进程终止) | 【增强】 | main.py `cancel_cover_task` |
| 翻唱结果发送 | 语音/文件 | 语音+文件+LLBot 群文件核验兜底 | 【增强】 | main.py `_send_cover_result` |
| LLM 工具 | 16 个(含 svc/svcvc) | 14 个(移除 svc/svcvc;TTS 独立配置入口) | 【改造】 | main.py `@filter.llm_tool` |
| 翻唱工具调用可靠性 | 无保障 | 请求阶段注入「必须调用工具」提示 | 【新增】 | main.py `on_llm_request_tts` |
| 本地音频翻唱 | 有 | 保留+模型/升降调解析 | 【继承】 | main.py `_execute_local_cover` |
| 自动升降调(性别识别) | 有(关键词+LLM) | 保留+模型性别编辑器(中文值) | 【继承】 | main.py `_infer_key_shift_from_filename` |
| 选歌菜单/按钮 | SessionWaiter+QQ 按钮 | 保留+翻唱/TTS 语义分离 | 【继承】 | selection.py/qq_callbacks.py |
| 消息分段 | 无 | 保留 | 【继承】 | — |
| 翻唱缓存 | Gradio /cache_info /clear_cache | 保留 | 【继承】 | gateway.py |
| 偏好学习 | 有 | 保留 | 【继承】 | main.py preference |
| 翻唱与 TTS 双启用 | —(无 TTS) | 模型行双启用单选(各自互斥) | 【新增】 | app.js `modelEditor` |
| `/cover_status` | 无 | 有(隐私隔离) | 【新增】 | main.py |
| WebUI 任务页 | 无 | RVC/TTS 分栏,当前+历史,回放,用户/群号/升降调展示 | 【新增】 | page_api.py/app.js |
| TTS 语音回复 | 无 | AstrBot TTS→RVC 变声→发送;冷却/重复检测/队列;失败降级 | 【新增】 | tts.py |
| 常驻推理 worker | 无 | 省模型加载 ~40s/次;失败回退子进程;空闲自退 | 【新增】 | gateway.py/rvc_worker.py |
| 配置迁移 | 无 | 旧插件名配置+数据自动迁移(幂等+一次性) | 【新增】 | main.py `_migrate_legacy_name_data` |
| 移除:svc/svcvc 全部 | 三引擎 | 仅 RVC | 【移除】 | 实际无人使用 |
| 移除:qq 点歌/qqrvc 等命令 | 有 | 由点歌插件承担点歌 | 【移除】 | 职责分离 |
| 移除:进度条/风控重试参数 | 有 | 网关统一出口静音策略 | 【移除】 | outbound.py |

## 未实现 / 未经验证(如实标注)

| 项 | 状态 |
|---|---|
| GPU/ARM 推理 | 未验证(仅 CPU x86_64 实测) |
| GHCR 预构建镜像 | 未发布(README 预留说明) |
| 严格 FIFO(提交序=推理序) | 已知行为:可能交换,注册表有记录 |
| SVC-Fusion / SoulX-SVCVC | 已移除,无回归计划 |
| 群内 TTS 端到端最后一轮 + `/cover_status` 展示 | 容器级已验证,群内最终确认待用户执行 |

## 许可证结论

- 原版 LICENSE = **GNU AGPL-3.0**(全文保留于上游仓库);原版基于 CCYellowStar2/astrbot_plugin_rvc_svc 扩展,上游链为 AGPL-3.0。
- 当前项目属**派生项目**:必须(且已经)保留 AGPL-3.0 许可证文本、NOTICE.md 原版声明与上游链接;未改动许可协议;代码文件无原始版权头需逐文件保留(上游亦无文件级版权头,以 NOTICE.md + 本对照文件声明来源)。
- 以 AGPL-3.0 发布当前项目与上游许可兼容 ✅。
