# astrbot_plugin_cucumber_rvc_console · RVC 控制台

<div align="center">

![RVC 控制台](logo.png)

**AstrBot AI 翻唱 + TTS 语音回复控制台**

翻唱《歌名》→ 自动取歌 → 副歌裁剪 → 3HP 人声分离 → RVC 变声 → 卷积混响 → 语音发回群聊
@Bot 我想听你说… → 人格反应 + 台词变声语音

![访客统计](https://count.getloli.com/get/@astrbot_plugin_cucumber_rvc_console?theme=rule34)

</div>

> [!NOTE]
> 本插件需要配合独立部署的 **[cucumber-rvc-gateway](https://github.com/<用户名>/cucumber-rvc-gateway)** 推理网关使用(见下方「后端准备」)。
> 当前项目由 [sdfsfsk/matsuko_cover](https://github.com/sdfsfsk/matsuko_cover) 派生而来,功能差异见 [docs/UPSTREAM_COMPARISON.md](docs/UPSTREAM_COMPARISON.md)。许可证 AGPL-3.0。

## 这是什么

一个 [AstrBot](https://github.com/AstrBotDevs/AstrBot) 插件,把「点歌变声」做成完整的产品化体验:

- **对用户**:群里发一句「翻唱《晴天》」或「@Bot 我想听你说XX」,机器人自动完成取歌/变声/语音发送,支持排队、取消、状态查询;
- **对管理员**:WebUI 控制台全套管理——任务监控(RVC/TTS 分栏)、RVC 控制台(手动转换/分离/切段)、TTS 试音台、模型管理(别名/性别/双启用)、卷积混响调音(8 参数)、诊断;
- **架构上**:插件只负责聊天交互与任务调度;重活(取音源、人声分离、RVC 推理)全部在独立的 Docker 网关中全局串行执行。

**功能边界**:插件不做推理;网关不做聊天;TTS 原始语音来自 AstrBot 已配置的 TTS 服务商(本插件不内置 TTS API)。

## 功能总览(以当前源码为准)

| 功能 | 说明 |
|---|---|
| 歌曲搜索与选择 | 多音源路由(网易云→酷狗→酷我→QQ,顺序可配)、启发式自动选歌(评分制)、伴奏/试听淘汰 |
| 音源获取 | meting 主路 + NodeJS 备路 + cookie 支持 + cookie 失效告警 |
| 片段裁剪 | 严格时间校验(颠倒/越界/超 30s 拒绝)+ 副歌启发式挑选(能量包络+停顿吸附)+ 变长输出 |
| 3HP 人声分离 | PyMSS 3_HP-Vocal-UVR,静音检测、人声偏轻自动换段重试 |
| RVC 音色转换 | 子进程推理 + 常驻 worker(省 ~40s 模型加载);参数实时生效 |
| 音色模型动态发现 | 网关扫描 weights 目录;index 自动匹配;无 index 自动无索引推理 |
| RVC 推理参数 | f0/index_rate/protect/rms_mix_rate/resample_sr,实时生效 |
| 卷积混响 | 合成 IR + FFT 卷积,wet/dry/room/decay/damping/pre-delay/width 全真实生效,峰值保护;翻唱与 TTS 独立配置 |
| TTS 语音回复 | AstrBot TTS 服务商 → RVC 变声;二段式(人格反应 + 台词朗读);独立模型/参数/混响 |
| 触发词意图分离 | 点歌 > 翻唱 > TTS 三级判定;点歌不被翻唱抢、翻唱不被 TTS 抢 |
| 任务队列 | 翻唱/TTS 独立队列 + 全局等待上限 + 网关全局串行(忙时等待≤180s) |
| 任务注册表 | task_id 级状态机、防重复提交、按用户查询与取消 |
| `/cover_status` | 查询自己的任务状态与排队位置(隐私隔离) |
| 多会话隔离 | 任务/结果/取消严格绑定原始会话,群聊私聊互不干扰 |
| 翻唱/TTS 历史 | 300 条,含触发用户/群号/台词,WebUI 可回放 |
| WebUI 控制台 | 概览/任务/RVC 控制台/TTS 试音台/模型管理/音频效果/设置/诊断 |
| 失败降级 | TTS 失败降级纯文字;Edge TTS 子进程兜底;网关 worker 失败回退子进程推理 |
| 配置实时生效 | 触发/冷却/推理参数/混响/性别映射等 60+ 项改完即用;需重载项明确标注 |

## 相对于原版的区别

当前项目由 [matsuko_cover](https://github.com/sdfsfsk/matsuko_cover)(AI 翻唱插件,RVC/SVC-Fusion/SoulX-SVCVC 三引擎)派生。完整对照表见 [docs/UPSTREAM_COMPARISON.md](docs/UPSTREAM_COMPARISON.md),演变史见 [docs/PROJECT_HISTORY.md](docs/PROJECT_HISTORY.md)。要点:

**继承自原版**:多音源点歌、LLM 工具调用框架、本地音频翻唱、自动升降调(性别识别)、选歌菜单与 QQ 按钮、偏好学习、消息分段。

**重构/增强**:音源路由化与自动选歌评分制;RVC 推理从「外部 Gradio 后端」改为「自研网关(分离+推理+缓存)」;推理参数真实生效;任务队列与全局串行调度;配置两层分组 + 实时生效;消息发送双兜底。

**新增**:TTS 语音回复全链路(TTS→RVC 变声,独立模型/参数/混响);卷积混响引擎;统一任务注册表(`task_id` 级状态机);`/cover_status`;WebUI 控制台 8 标签;常驻推理 worker;旧版配置/数据自动迁移。

**移除**:SVC-Fusion 与 SoulX-SVCVC 引擎及相关命令(实际无人使用,无回归计划);QQ 点歌命令(由点歌插件承担)。

**为什么需要独立 Gateway**:RVC/3HP 推理是重 CPU 任务(i5-6500 实测 30s 片段峰值 ~4GB 内存),必须全局串行且与聊天进程隔离;网关以 Docker 容器常驻,负责资源管理、队列、子进程生命周期与缓存,插件保持轻量。

## 安装

```bash
cd AstrBot/data/plugins
git clone https://github.com/<用户名>/astrbot_plugin_cucumber_rvc_console.git
```

或下载 Release 包解压到插件目录,然后在 AstrBot WebUI → 插件管理 → 启用并加载(依赖 `gradio_client`、`aiohttp` 自动安装)。

**升级**:覆盖插件目录后重载;配置与历史自动保留(新版首次加载自动从旧插件名 astrbot_plugin_chorus_cover 迁移)。

## 后端准备(cucumber-rvc-gateway)

1. 准备 RVC 整合包的 assets:`weights`(音色 .pth)、`indices`、`pymss_weights`(3HP 分离模型)、`hubert_base`、`rmvpe`;
2. 获取网关:[仓库地址](https://github.com/<用户名>/cucumber-rvc-gateway)(独立项目、独立部署、独立版本);
3. `cp .env.example .env`,按注释填写模型目录、工作目录、AstrBot plugin_data 路径与端口;
4. `docker compose up -d --build`,启动自检会逐项报告模型/目录状态;
5. 测试:`curl http://127.0.0.1:3333/`。

部署拓扑支持:同机 Docker、跨容器、跨主机——只要 AstrBot 能访问到 `rvc_base_url`。完整说明见 **[Gateway README](https://github.com/<用户名>/cucumber-rvc-gateway)**。

## 配置要点

全部配置在 WebUI「插件设置」,中文说明齐全,按生效方式分三类(每项都有标注):**实时生效** / **保存后自动同步** / **需重载**(45 键标注)。完整审计见 `docs/config-audit.json`。

| 配置 | 说明 |
|---|---|
| `rvc_base_url` | 网关地址。同机填 `http://127.0.0.1:3333/`;**跨容器时 127.0.0.1 指容器自身**,需用宿主机 IP 或容器网络地址 |
| `gateway_work_dir` | 网关工作目录的宿主机路径(与网关 `.env` 的 `RVC_WORK_DIR` 相同)——补发翻唱、TTS 落盘、历史回放依赖它 |
| 音色模型 / 默认音色 | 来自网关动态扫描;TTS 音色与翻唱音色相互独立,模型管理页可视化切换 |
| TTS 触发与冷却 | 触发词正则、@Bot 要求、用户/群冷却、复读间隔、队列上限 |
| 音源 | 音源顺序、meting/NodeJS 地址、cookie、失效告警 |
| 混响 | 歌曲/TTS 各自独立的 8 参数卷积混响 |

## 使用

| 命令/说法 | 效果 |
|---|---|
| `翻唱《晴天》` / `翻唱周杰伦的晴天` | 自动取歌→变声→语音 |
| `/rvc 晴天 3` | 指定歌名与升降调 |
| `@Bot 我想听你说今天也要加油鸭` / `@Bot 说一下XX` | TTS 语音回复(人格反应+台词变声) |
| `再说一遍` / `接着说` | 复读/接续(结合上下文) |
| `/cover_status` | 查询自己的任务与排队位置 |
| `取消翻唱任务` | 取消自己的全部进行中任务 |
| `/本地翻唱` | 翻唱刚发的音频文件 |
| `/刷新rvc模型` | 重新扫描音色模型 |
| `/查看翻唱缓存` / `/清理翻唱缓存` | 缓存管理 |

自然语言点歌与翻唱依赖 AstrBot 的 LLM 工具调用:请使用**支持 Function Calling 的模型提供商**(见下方排查)。

## 模型提供商(Function Calling)说明

「翻唱《XX》」这类自然语言翻唱由 LLM 调用插件的 `smart_cover` 等工具完成:

1. **LLM 不调工具**(只口头答应):确认模型提供商支持 Function Calling;插件已在请求阶段注入调用提示,但最终取决于模型能力;
2. **插件问题**:看 AstrBot 日志中 `[RVC控制台]` / `[TTS]` 前缀;
3. **网关问题**:WebUI 概览 Gateway 状态 / 诊断页;`curl http://<网关地址>/` 验证。

确定性替代:`/rvc 歌名` 命令不依赖 LLM。

## 路径说明

| 路径 | 位置 | 说明 |
|---|---|---|
| 插件数据目录 | `<AstrBot data>/plugin_data/astrbot_plugin_cucumber_rvc_console/` | 历史/偏好/临时音频(自动管理) |
| 插件配置 | `<AstrBot data>/config/astrbot_plugin_cucumber_rvc_console_config.json` | WebUI 与原生设置共用 |
| 网关工作目录 | 宿主机任意路径(网关 `.env` 的 `RVC_WORK_DIR`) | out/logs/tts/cache;**插件设置的 `gateway_work_dir` 必须填同一路径** |
| 容器内路径 | `/app/assets/...`、`/work/...` | 固定标准值,经 Docker 挂载映射 |

> [!WARNING]
> **容器内路径 ≠ 宿主机路径**。网关返回的成品路径必须能被 AstrBot 进程读取——这就是 `gateway_work_dir` 需要与网关 `.env` 保持一致的原因。

## 测试环境与性能

实测环境(所有性能数据来自此环境真实日志,未标注「未测量」的均为实测):

- 飞牛 fnOS + Docker;Intel i5-6500(4C4T);16GB RAM;**CPU 推理,无 GPU**;
- AstrBot 4.29.0(Python 3.12);Gradio 6.31.0;torch 2.4.1+cpu;
- 模型:rym/deng/nuoyemu.pth;分离:3_HP-Vocal-UVR(PyMSS)。

| 阶段 | 实测耗时(30s 歌曲 → 25s 成品) |
|---|---|
| 音源获取 | 2~20s(取决于音源) |
| 裁剪 | ~0.5s |
| 3HP 分离 | ~70s |
| RVC 推理(常驻 worker) | ~54s(首次含模型加载 ~110s) |
| 混响 | ~0.2s |
| **翻唱总耗时** | **~2~3 分钟** |
| TTS 生成+变声(短句) | 反应文字即时,语音 ~10-15s |

> 性能受 CPU、内存、模型、音频长度与排队影响;高峰期任务在队列中等待,等待耗时记录于任务日志 `queue_wait`。

## 模型常驻与资源说明

- **网关进程**常驻(Docker);**音色模型**在常驻 worker 中保持加载(首次任务加载 ~40s,之后免加载),**空闲 10 分钟自动卸载**;
- 3HP 分离每次任务以子进程运行,任务结束释放;
- TTS 原始语音由 TTS 服务商生成,不占推理资源;RVC 变声走同一常驻 worker;
- 任务失败自动回退子进程路径并释放槽位;全局串行保证同时只有一个推理任务;
- 实测内存:网关容器常驻 ~1.2GiB,worker 热态 +1.1GiB,推理峰值 ~4GiB(上限 12g)。

## 常见问题与故障排查

| 现象 | 排查 |
|---|---|
| 翻唱/TTS 一直没反应 | WebUI 概览看 Gateway 状态 → 诊断页逐项检查;`curl http://<网关地址>/` |
| Gateway 无法读取 | `rvc_base_url` 是否可达?跨容器时 127.0.0.1 指容器自身 |
| 模型列表为空 | 网关 `.env` 的 weights 目录挂载是否正确 → `/刷新rvc模型` |
| 某模型「无 index」 | 正常降级为无索引推理,音色贴合度略差;放入同名 index 后刷新 |
| 3HP 分离失败 | 看网关日志;确认 `RVC_PYMSS_WEIGHTS_DIR` 挂载与 `RVC_SEP_MODEL` 文件名 |
| RVC 推理失败/OOM | 缩短片段;检查容器内存上限;看网关日志退出码(-9 = OOM) |
| LLM 只说话不翻唱 | 确认模型支持 Function Calling;插件已注入调用提示但最终取决于模型;用 `/rvc` 命令替代 |
| TTS 只有文字没语音 | 看日志 `[TTS]` 前缀;TTS 服务商是否可用;`gateway_work_dir` 是否配置 |
| 「请填写 gateway_work_dir」 | 插件设置 → 高级 → 网关工作目录,填网关 `.env` 的 `RVC_WORK_DIR` 同值 |
| 任务取消没反应 | 排队任务立即取消;运行中任务软取消(转换完不发送) |
| 输出文件无法访问 | `gateway_work_dir` 与网关 `RVC_WORK_DIR` 是否一致 |

## 已知行为

- 推理顺序可能与提交顺序交换(取歌耗时不一致;全局串行保证不并发);
- 模型性别下拉为静态三槽位,新模型性别经 `model_genders_extra` 自动维护;
- 插件重启后内存中的排队任务丢失(不会误标成功);网关重启自动清扫任务残骸;
- 群内 TTS/翻唱的触发与冷却详见设置页「TTS 语音回复 → 触发与冷却」。

## 致谢与许可证

- 上游项目:[sdfsfsk/matsuko_cover](https://github.com/sdfsfsk/matsuko_cover)(AGPL-3.0)——本项目的直接前身,功能对照见 [docs/UPSTREAM_COMPARISON.md](docs/UPSTREAM_COMPARISON.md);
- 上游前身:[CCYellowStar2/astrbot_plugin_rvc_svc](https://github.com/CCYellowStar2/astrbot_plugin_rvc_svc);
- 框架:[AstrBot](https://github.com/AstrBotDevs/AstrBot);
- 推理:[Retrieval-based-Voice-Conversion-WebUI](https://github.com/RVC-Project/Retrieval-based-Voice-Conversion-WebUI)(MIT);
- 音源参考:[astrbot_plugin_music](https://github.com/Zhalslar/astrbot_plugin_music);
- 分离:[PyMSS](https://github.com/SUC-DriverOld/MSST-WebUI) 系 3HP 模型。

本项目基于 AGPL-3.0 发布,继承上游全部许可义务;详见 [LICENSE](LICENSE) 与 [NOTICE.md](NOTICE.md)。

![Moe Counter](https://count.getloli.com/get/@astrbot_plugin_cucumber_rvc_console?theme=rule34)
