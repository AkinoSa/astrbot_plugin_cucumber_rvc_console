# PROJECT_HISTORY · 项目演变史

> 本文件按时间顺序整理 RVC 控制台(原 astrbot_plugin_chorus_cover)从上游继承到当前 1.0.0 的全部演变。
> 原则:历史记录用于解释「为什么」;当前源码决定「是否真实存在」;测试报告决定「是否已验证」。
> 原始记录:`CHANGELOG_CUSTOM.md`(CUSTOM-1~9 全文)、网关目录 `.bak-*` 快照、各阶段交付报告。
> 隐私说明:公开版已将测试群号/用户标识脱敏为「测试群/测试用户」。

## 阶段总表

| 阶段/日期 | 修改内容 | 涉及文件/模块 | 修改原因 | 测试结果 |
|---|---|---|---|---|
| 上游继承 | 基于 [sdfsfsk/matsuko_cover](https://github.com/sdfsfsk/matsuko_cover) v2.13.0(AGPL-3.0,继承自 CCYellowStar2/astrbot_plugin_rvc_svc):三引擎(RVC/SVC-Fusion/SoulX-SVCVC)、网易云+QQ 音源、LLM 工具调用、自动升降调 | main.py/api.py/selection.py/qq_callbacks.py | 项目起点 | 上游自带 |
| CUSTOM-1 (2026-10-29) | 收窄为纯 RVC:移除 SVC/SVCVC 引擎与相关命令;默认分离模型改 3_HP-Vocal-UVR;网关对不可用分离模型回退默认 | main.py/_conf_schema.json | 实际只用 RVC;bs_roformer 模型缺失导致分离失败 | py_compile + 实测 |
| CUSTOM-2 | 自动选歌第一版:启发式评分(阈值 45/分差 8),低于阈值回菜单不硬猜 | main.py `_auto_pick_song` | 手动选歌流程太长 | 单测+实测 |
| CUSTOM-3 | 统一自动选歌入口(命令与自然语言同评分);修复 `_claimed_original_name` NameError(教训:测试必须 import 真实模块);模型性别合并唯一来源(model_genders);配置页重排 | main.py/_conf_schema.json | 两套选歌逻辑不一致;性别配置散落 | 全量单测 65 项( tests/test_custom_autopick.py 前身) |
| CUSTOM-4 | 配置改两层嵌套卡片 + `_NestedConfig` 适配层(CONFIG_PATHS 60+ 键);候选切片缓冲 15s→5s+分离后精挑;「/设置默认音色」「/补发翻唱」;备份目录移出 plugins 扫描区 | main.py/page_api.py/_conf_schema.json | 配置键散乱;切片边界脏;中断后成品丢失 | 单测+迁移幂等测试 |
| CUSTOM-5/5.1 | 成功播报与模板提示解耦为两个开关;播报取会话人格+6 条上下文;首尾淡入淡出 0.5s | main.py `_notify_llm_with_context`/`_get_fade_kwargs` | 播报文案机械;剪辑点突兀 | 实测 |
| CUSTOM-6 | AstrBot 原生插件页面(WebUI):新增 page_api.py(路由)+pages/dashboard(6 标签);RVC 控制台(转换/分离/切段) | page_api.py/pages/ | 群里操作效率低 | 实测 |
| CUSTOM-6.1~6.5 | bridge SDK 修复;网关 /separate 仅分离;任务阶段推断;波形播放器;翻唱历史记录(300 条) | page_api.py/app.js | 页面体验 | 实测 |
| CUSTOM-6.6 | 翻唱队列:单并发+排队上限(cover_queue_limit) | main.py `_predict_cover` | 并发任务互相挤占 | 实测 |
| CUSTOM-6.8~6.10 | TTS 控制台前身(切段);index_rate 需字符串传参;任务名实时化;WebUI 设置与原生配置同步修复(点号键);网关混响接入配置 | page_api.py/main.py/gateway.py | 设置不同步 | 实测 |
| 2026-10-29~30 | **Gateway 独立化**:从「chorus-mw 概念中间层」落地为自研网关容器(rvc-gateway):取音源→裁剪→PyMSS 分离→RVC 推理(infer/cli.py 子进程)→返回宿主机路径;`/separate`/`/clip` 等扩展端点;内存 4g→8g(实测 30s 片段 4 线程 OOM,线程压 2) | gateway 项目(gateway.py/Dockerfile/compose) | 插件不能直接驱动 RVC 推理;CPU 资源管理 | 实测(OOM 修复+多轮真实翻唱) |
| CUSTOM-7 (2026-10-30~31) | **三大升级**:①真卷积混响(合成 IR+FFT,8 参数);②TTS 音色转换全链路(网关 /tts_convert+插件 tts.py,模型与翻唱完全隔离);③WebUI 重构(任务/RVC 控制台/TTS 试音台/模型管理/音频效果/设置/诊断)。修复 10 项实测问题(含 4.29 `get_provider_by_id` 异步、`on_llm_response` 不能 stop_event、任务目录未创建等) | 全部模块+gateway.py | 方案:升级方案一~五 | 单测 60+ 用例+网关集成 8 项+真实群实测 |
| CUSTOM-7.2 | 常驻 RVC worker(省每次 ~40s 模型加载):JSON Lines stdio 协议,空闲 10 分钟自退,失败回退子进程;网关内存 8g→12g | gateway.py/rvc_worker.py | TTS 短音频加载成本占比过高 | TTS 40.8s→9.9s;歌曲 RVC ~130s→54s |
| CUSTOM-8 | **多会话并发调度**:统一任务注册表(task_registry.py,task_id 级状态机);全局等待上限;防重复提交;`/cover_status`;按用户取消(翻唱+TTS);网关忙时等待而非拒绝+queue_wait 记录 | task_registry.py/main.py/gateway.py | 同群两用户共享任务记录,无法按用户管理 | 注册表 8 组单测;网关双任务并发实测(串行+FIFO) |
| CUSTOM-9 (2026-11-01~02) | **更名通用化**:插件→cucumber_rvc_console「RVC 控制台」1.0.0;网关→cucumber-rvc-gateway;配置/数据自动迁移;路径全部配置化(.env/动态推导);`apply_live_config` 实时生效机制;配置审计 114 键 0 未消费;**联调修复 16 项实测问题**(详见交付报告 §3/§11);模型管理页交互重做(每行翻唱/TTS 双启用+统一保存+index 标注);logo/艺术字 | 全部模块 | 开源发布准备 | 静态 12 项+单测 60+用例+网关集成 8 项+真实群多轮实测 |

## 关键设计决策溯源

| 设计 | 来源/原因 | 依据位置 |
|---|---|---|
| 网关独立容器 + 全局串行 | CPU 推理资源有限;i5-6500 实测 30s 片段 4 线程 OOM → 线程压 2、内存 8g→12g | CUSTOM-7.2/网关 README |
| RVC 走子进程 + 常驻 worker 双模式 | 子进程隔离保证崩溃不影响网关;worker 省模型加载;worker 失败自动回退子进程 | gateway.py `run_rvc_stage` |
| TTS 二段式(人格反应+台词变声) | 用户要求反应与朗读分离;台词经二次 LLM 提取,支持复读/接着说 | main.py `_extract_speak_text` |
| TTS 模型与翻唱模型完全隔离 | 方案 6.1;配置层/网关层双重隔离 | `_conf_schema.json` tts 卡片 |
| 混响只作用于人声 | 网关不混伴奏;合成 IR 卷积 8 参数全真实生效 | gateway.py `apply_reverb` |
| 状态机唯一入口 | 防止散落回调乱改状态;非法流转拒绝 | task_registry.py `set_status` |
| `on_llm_response` 只 set_result 不 stop_event | 4.29 调度器实测:stop_event 会拦截 Respond 阶段导致消息丢失;TTS 改为主动 send | CUSTOM-7.3 |
| Edge TTS 子进程兜底 | Edge 接口对 AstrBot 进程内连接风控(同机 CLI 稳定);子进程完全复刻成功路径 | tts.py `_edge_subprocess_fallback` |
| FIFO 已知行为 | 取歌耗时不一致可能导致推理顺序交换;严格 FIFO 需取歌占槽,不牺牲取歌效率 | 交付报告 §11.1 |

## 测试资产

| 资产 | 覆盖 | 位置 |
|---|---|---|
| test_task_registry.py | 注册表 8 组语义(FIFO/上限/去重/流转/取消/隐私/收尾/规范化) | tests/ |
| test_tts_intent.py | TTS/翻唱/点歌意图判定 40+ 对照用例 | tests/ |
| test_config_sync.py | 112 配置键 ↔ schema 双向一致 | tests/ |
| test_reverb_kernel.py | 混响 IR 形状/确定性/参数语义/峰值保护 | tests/ |
| test_custom_autopick.py | 自动选歌评分 65 项(CUSTOM-3 期) | tests/ |
| 网关并发集成 | 双 TTS 并发提交→串行+FIFO+queue_wait | 网关部署环境 |
| 真实群测试 | 翻唱/伴奏拒绝/取消/同会话排队/TTS | 测试群(已脱敏) |
