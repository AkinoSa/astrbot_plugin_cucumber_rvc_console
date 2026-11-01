# CUSTOM-9 交付报告:更名、通用化与全面回归

> astrbot_plugin_cucumber_rvc_console 1.0.0 + cucumber-rvc-gateway · 2026-11-02
> 本阶段目标:更名、路径/环境通用化、全面回归。不含 GitHub 上传/Release/图标/第三阶段。
> 测试分级:【实测】【单测】【静态检查】【检查】【待验证】——严格区分。

---

## 1. 命名与项目结构

| 项 | 旧 | 新 |
|---|---|---|
| 插件中文名 | RVC翻唱 | **RVC 控制台** |
| 插件目录/注册名 | astrbot_plugin_chorus_cover | **astrbot_plugin_cucumber_rvc_console** |
| 插件版本 | 0.13.0(CUSTOM-8) | **1.0.0**(应用户要求) |
| 网关项目 | (无名,gateway.py) | **cucumber-rvc-gateway · RVC 控制台网关** |
| 网关镜像/容器 | rvc-gateway:latest / rvc-gateway | cucumber-rvc-gateway:latest / **cucumber-rvc-gateway**(compose 服务名仍为 rvc-gateway) |

旧名残留扫描:插件侧仅剩迁移函数 `LEGACY_PLUGIN_NAME` 常量与迁移说明(必要);`matsuko` 0 残留;网关侧 0 残留("chorus" 仅作为功能名出现在副歌挑选注释中)。

## 2. 通用化结果

### 网关(全部【实测】:改名后容器重建+健康检查+接口冒烟通过)
- 新增 **`.env.example`**(通用占位,无任何真实路径)+ `.env`(本机实际值,gitignore)+ `.gitignore`;
- `compose.yaml` 全参数化:`${RVC_*:?}` 必填校验(compose 层直接报"请在 .env 设置…")、端口/CPU/内存可配、新增 **healthcheck**;
- `gateway.py` 启动自检 `_validate_startup_paths()`:模型/index/分离模型/系统 Python 缺失时明确报错退出,不静默回退;
- **NAS 路径 0 残留**(grep 验证);容器内部路径保持标准(/app/assets/…,符合方案 4.1 的两类划分);
- 模型动态发现保留:3 个实测模型 deng/rym/nuoyemu(含 nuoyemu 无 index 路径)、3HP 在 `legacy_vr/` 子目录递归扫描可发现(与已知信息的路径差异已记录)。

### 插件(全部【静态检查】+迁移【实测】)
- 新增配置 `gateway_work_dir`(网关工作目录宿主机路径):补发翻唱、TTS 历史回放、TTS 落盘依赖;从旧版迁移时自动写入原路径;未配置时对应功能停用并明确提示(不回退到开发者路径);
- `page_api.py` 所有宿主机路径改为动态:AstrBot 插件数据路径自动推导 + `CHORUS_PLUGIN_DATA/CHORUS_GATEWAY_WORK/CHORUS_PLUGIN_LOG` 环境变量覆盖;
- 写死内容清除:默认音色 hint/options、TTS 模型 options、README 全部重写为通用部署文档(含「已验证/未验证」声明:仅 CPU x86_64 实测,GPU/ARM 未验证)。

### 迁移(【实测】+过程中修复 2 个 bug)
- 旧插件名 → 新名:**配置值覆盖式迁移 + plugin_data 文件迁移**,幂等;
- 迁移完成后旧配置文件改名 `.migrated-` 标记(**只执行一次**);
- 已知修复:①初版"只补缺"语义对 schema 默认值失效(tts_enable=False 被当成已填写)→ 改覆盖式;②覆盖式每次重载重跑,用旧配置反复覆盖新设置(默认音色 deng 被改回 nuoyemu)→ 加一次性标记;③`gateway_work_dir` 误写顶层 → 挪回 advanced 并清理。

## 3. 本轮联调修复的问题清单(全部【实测】复现→修复→复测)

| # | 问题 | 根因 | 修复 |
|---|---|---|---|
| 1 | WebUI「Gateway 无法读取」 | page_api 缺失 `_task_log_dir/_out_dir/_upload_dir` 函数定义(NameError) | 补定义,AST 验证 |
| 2 | TTS 全挂:`CONTAINER_PLUGIN_DIR is not defined` | 网关通用化时误删该常量,to_container_path 崩 | 补回,真实 TTS 转换复测 ✅ |
| 3 | 模型性别保存失败/显示丢失 | 编辑器用英文值,schema 校验只认中文;读取走叶子键而编辑器读父键 | 编辑器统一中文值+叶子键读写 |
| 4 | 音色别名/性别保存"没有变化" | 编辑器写父对象键,保存接口只认叶子 | 后端接受 object/dict 节点键,前端全量提交 |
| 5 | 同会话第二首翻唱被拒 | 旧「同会话单任务」限制三层(工具前置×2+注册拒绝) | 全部移除,同会话任务进统一队列 |
| 6 | 排队列表残留已完成任务、计时不动 | `_predict_cover` 用旧键查注册表,begin 落空,任务永远停在 queued | 改用事件上的 registry_id |
| 7 | LLM 只口头答应不调翻唱工具 | 工具调用随机失手 | `on_llm_request` 注入「必须立即调用 smart_cover」(【实测】生效) |
| 8 | 失败通知后跟着自相矛盾的"去准备了" | LLM 复述异步受理确认 | 受理返回附「不要向用户复述」指令 |
| 9 | WebUI 改默认音色/推理参数不生效 | 启动时缓存属性 | `_configured_rvc_model_index`/`_predict_cover_impl` 改实时读取 + `apply_live_config` 统一同步机制 |
| 10 | 排队列表看不到「准备中」阶段任务 | 注册表任务未纳入 WebUI | active_tasks 返回 registry_waiting,两面板并入显示 |

## 4. CUSTOM-8 并发架构保护(方案第七节)

全部保留【检查+单测+实测】:TaskRegistry/状态机/全局上限/防重复/`/cover_status`/取消/会话隔离/网关串行/queue_wait。本轮**增强**:同会话并发排队(原单任务限制移除)、运行中任务句柄入注册表、取消命令统一按用户取消(翻唱+TTS)。回归:注册表 8 组单测全绿;真实群验证同会话第二首正确入队(LLM 调用 smart_cover → 注册成功)。

## 5. 配置项全面审计(方案要求"每个配置项测一遍")

【静态检查+检查】审计脚本逐键核对 schema ↔ 代码消费:**114 个配置叶,0 个未消费**;分级:
- **实时生效**(WebUI 改完即用,无需重载):TTS 全部(开关/触发/冷却/参数/混响)、翻唱推理参数(f0/index_rate/protect/rms/音量)、混响 8+3 参、排队位置通知、gateway_work_dir、升降调默认值等——**共 60+ 键**;
- **保存后运行时同步**(apply_live_config):enable_rvc、llm_force_mode、rvc_default_model、inference_timeout、tts_timeout、各队列上限、task_queue_limit、duplicate_window、max_batch_size、chorus_seconds、render_mode、model_genders(实时更新性别映射)——**15 键**;
- **启动时生效**(保存后标注「需重载插件生效」,RELOAD_REQUIRED 已扩充至 45 键):音源配置、自动选歌、本地音频、通知人格、QQ 官方卡片等——**其余键**;
- 隐藏键 2 个(model_genders_extra/model_gender_map)按设计自动维护。
- 审计中发现并修复:模型性别改动运行时不同步 → 已加实时同步。

审计明细:`docs/config-audit.json`(键/可见性/读取时机/消费状态)。

## 6. 测试结果汇总(2026-11-02 02:14~02:17 真实群轮次补充)

真实群【实测】补充(2026-11-02 02:14~02:17 轮次):
8. ✅ **同会话排队实测**:雨物語(02:14:46)与我不难过(02:15:11)同会话先后提交,两单均注册(唯一键 `base:registry_id`),双双完成——CUSTOM-8.1 修复生效;
9. ✅ 完成后注册表状态正确流转,排队列表无残留(此前"完成后仍挂在排队中"确认已修);
10. ⚠️ **实测发现:推理顺序可能与提交顺序交换**(取歌耗时不一致时,先完成取歌的任务先推理)。提交顺序在注册表有记录,推理由网关全局串行保证不并发;严格按提交顺序的 FIFO 需要把取歌也纳入槽位(会占用推理槽等待网络),本轮不实现,记为已知行为;
11. ✅ 队列面板轮询改为 1 秒(排队已等/运行计时逐秒刷新)。

## 6.0 测试结果汇总(原始)

| 类别 | 数量 | 通过 | 失败 | 待验证 |
|---|---|---|---|---|
| 静态检查(编译/JSON/残留扫描/敏感信息) | 12 项 | 12 | 0 | 0 |
| 单元测试(注册表/意图/混响内核/配置同步) | 4 套 60+ 用例 | 全过 | 0 | 0 |
| 网关集成(启动/模型/分离列表/缓存/TTS 转换/宿主机路径可读) | 8 项 | 8 | 0 | 0 |
| 真实群测试(群 测试群,含 02:14 同会话排队轮次) | 9 项 | 8 | 1(已修) | 1(TTS 群内端到端最后一轮 + /cover_status 展示) |
| 配置审计 | 114 键 | 114 | 0 | 0 |

真实群【实测】明细(2026-11-02 00:32~01:37):
1. ✅ 翻唱《晴天》全链路(工具调用→取歌→变声→语音回群,deng 生效验证);
2. ✅ 翻唱《龙卷风》伴奏拒绝:只出一句失败说明(矛盾复述已修);
3. ✅ 翻唱《雨物語》:完成后历史含歌名/用户;
4. ✅ 取消:雨物語 重发后取消,注册表状态「已取消」(日志);
5. ✅ 同会话第二首排队:修复前被拒(已修),修复后 smart_cover 正确注册;
6. ❌→✅ TTS 语音:网关 NameError 致失败(已修),容器内直测转换+宿主机可读✅,**群内端到端重测待做**;
7. ⏳ `/cover_status` 群内展示待验证。

## 7. 性能与资源

无新增常驻组件;网关容器 cucumber-rvc-gateway 12g 上限、healthcheck 通过;推理串行模型未变(峰值与 CUSTOM-7.2 交付一致);配置审计/注册表内存占用 <1MB。

## 8. 风险与遗留问题

1. **模型性别静态槽位**(deng/nuoyemu/rym):其他用户的新模型性别走 extra 自动维护可用,但原生设置页无下拉槽(AstrBot schema 静态所致)——后续可做 schema 动态生成;
2. **TTS 群内端到端、`/cover_status` 群内展示**:容器级已验证,群内最终确认待做(见第 6 节);
3. 工具调用随机失手已用注入提示压低,但 LLM 固有不确定性无法 100% 消除;确定性路径:`/rvc` 命令;
4. 队列「已等」计时随 2~3 秒轮询刷新(非逐秒);
5. 网关 `.env` 含本机路径(不提交仓库,已 gitignore);`.env.example` 为通用占位;
6. 迁移仅覆盖 chorus_cover → cucumber_rvc_console 单跳;旧配置文件改名 `.migrated-` 保留在 config 目录。

## 9. 回滚方案(不删任何数据)

```bash
# 插件回滚到 CUSTOM-8(0.13.0)
mv <AstrBot 数据目录>/astrbot/data/plugins/astrbot_plugin_cucumber_rvc_console \
   <AstrBot 数据目录>/astrbot/data/plugin_backups/disabled-cucumber-$(date +%s)
cp -a <AstrBot 数据目录>/astrbot/data/plugin_backups/pre-rename-20261101-182441/plugin-dir \
   <AstrBot 数据目录>/astrbot/data/plugins/astrbot_plugin_chorus_cover
mv <AstrBot 数据目录>/astrbot/data/config/astrbot_plugin_chorus_cover_config.json.migrated-to-cucumber-rvc-console \
   <AstrBot 数据目录>/astrbot/data/config/astrbot_plugin_chorus_cover_config.json
# → AstrBot 重载插件(配置/历史原样恢复)

# 网关回滚
cp <AstrBot 数据目录>/astrbot/data/plugin_backups/pre-rename-20261101-182441/gateway.py \
   <RVC 项目目录>/RVC-Project/gateway/gateway.py
cp <AstrBot 数据目录>/astrbot/data/plugin_backups/pre-rename-20261101-182441/compose.yaml \
   <RVC 项目目录>/RVC-Project/gateway/compose.yaml
cd <RVC 项目目录>/RVC-Project/gateway && docker compose up -d --build
# (compose 回滚后容器名回到 rvc-gateway;gateway_work 数据在宿主机,不受影响)
```

## 10. 本阶段遵守情况

未上传 GitHub、未拉取到本地新建 clone、未动图标、未换模型、未动 RVC WebUI 容器、未改无关插件、未压测;真实群测试仅 测试群 少量消息。**报告即暂停点,等待批准后才会进行任何后续阶段。**

---

## 11. 追加联调(报告初稿后的模型管理交互重做与保存链路修复)

用户实测继续暴露问题,追加修复(全部【实测】复现→修复):

| # | 问题 | 根因 | 修复 |
|---|---|---|---|
| 1 | 模型管理「保存设置」点击无任何反应 | 多次脚本编辑互相覆盖:保存按钮绑定丢失 + 两个指向已删元素的全局开关绑定在启动时抛异常,中断后续所有绑定 | 死绑定删除、绑定补回、AST+绑定交叉核对 |
| 2 | 保存后模型管理页性别显示不更新(设置页已更新) | 统一保存提交完整路径键 `tuning.gender.model_genders`,运行时同步按键名精确匹配没命中 | 同步逻辑改按路径匹配,性别映射实时刷新 |
| 3 | 模型列表每行重复显示两次 | 统一保存把音色关键词写成无 `.pth` 后缀的模型名,与网关文件名列表匹配失败,配置条目被当成"网关未发现"重复追加 | 写入/读取双向补全 `.pth` 后缀 |
| 4 | 浏览器缓存导致"改了前端等于没改"反复出现 | 静态资源无版本参数 | index.html 资源全部加 `?v=` 版本参数,前端版本号可悬停查看(1.0.0-r10),每次改动 bump |
| 5 | 「刷新模型」点击后文字变回「刷新模型列表」 | 收尾代码硬编码旧文案 | 修正为「刷新模型」 |
| 6 | 队列「已等」计时非逐秒 | 3 秒轮询 | 面板轮询改 1 秒 |

### 交互重做(按用户要求)

- **模型管理页重做**:每行模型 = 模型名(含 index 状态标注:✓/无 index 自动降级)/ 别名 / 性别(中文值,与设置页一致)/ **翻唱启用** / **TTS 启用** 两组单选(各自互斥,点第二行的 TTS 第一行 TTS 自动熄灭);移除每行独立保存按钮与全局启用开关;顶部「保存设置」一次提交所有行改动(实时生效)。
- 「保存设置」在「刷新模型」之前;「刷新模型列表」更名「刷新模型」。
- **index 匹配状态展示**:每行标注 index ✓ / 无 index(自动无索引推理),数据来自网关 `/model_info` 的 `index_matches`。
- 品牌区:logo 换为用户提供的图(512×512,Pillow 处理),「RVC 控制台」红色渐变艺术字(亮/暗主题各一套)。
- 「排队位置通知」设置移入「通知与播报 → 排队通知」。

## 11.1 rvc_base_url 可配置性专项验证(2026-11-02)

`rvc_base_url` 已改为**实时读取**(main/tts/page_api 全部入口;此前为启动时缓存,改地址需重载)。四种地址状态用插件同一调用方式(gradio_client)实测:

| 场景 | 结果 | 耗时 |
|---|---|---|
| A 正常地址(127.0.0.1:3333) | ✅ 连接成功,10 个端点(含 /tts_convert 完整参数契约) | 6.2s |
| B 错误主机(192.168.50.99) | ✅ 明确失败 ConnectTimeout,不挂起 | 5.0s |
| C 端口不可访问(127.0.0.1:3444) | ✅ 明确失败 Connection refused | 0.1s |
| D 网关容器停止 | ✅ 明确失败 Connection refused;重启后 8s 恢复 200 | 6.1s |

结论:地址完全可配置(同机/跨容器/跨主机/任意端口),所有失败模式快速、明确、不影响聊天主流程(翻唱/TTS 各自有降级路径)。另审计发现并修复:`rvc_base_url` 此前也是启动缓存属性,已改实时读取。

**FIFO 已知行为(按方案要求确认)**:提交顺序在注册表完整记录,推理由网关全局串行保证不并发;但取歌/下载阶段不占推理槽,取歌耗时不一致可能导致推理顺序与提交顺序交换。**不为实现严格 FIFO 牺牲取歌效率**(严格化需在取歌阶段占推理槽,会让槽位空等网络数秒到数十秒)。已记录为已知行为。

## 11.2 重载完整性最终核查(2026-11-02)

多轮重载(0.14.0↔1.0.0↔r9/r10)后实测核查:配置文件完整(rvc_base_url/默认音色 deng/tts_enable=True/gateway_work_dir 全部保留)、TTS 历史 5 条 + 翻唱历史 29 条可读、注册表单测回归通过、无错误投递记录。任务注册表为内存态,重启清空为设计行为(见 §9 持久化结论)。

## 12. 最终状态

- 全部 12 项静态检查、4 套单测(60+ 用例)、8 项网关集成、114 键配置审计通过;
- 真实群(测试群)实测:翻唱全链路 ✅ / 伴奏拒绝 ✅ / 取消 ✅ / 同会话排队 ✅(02:14 轮次,两单注册+串行完成)/ TTS 容器级 ✅;
- **待用户最终确认**:群内 TTS 语音端到端(最后一次)、`/cover_status` 群内展示——容器级与代码路径均已验证;
- 已知行为:推理顺序可能与提交顺序交换(取歌耗时差,网关串行保证不并发);模型性别静态槽位仅三个,新模型走 extra 自动维护。
