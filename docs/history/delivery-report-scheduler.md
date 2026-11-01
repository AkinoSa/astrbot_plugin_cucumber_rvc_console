# CUSTOM-8 交付报告:多会话并发任务调度与消息投递

> astrbot_plugin_chorus_cover(0.13.0 → CUSTOM-8)· 交付日期 2026-11-01
> 本报告按升级方案第十六节逐项交付。测试分级标注:【实测】=真实环境执行、【单测】=单元测试、【检查】=代码审查。

---

## 1. 原有并发架构与发现的问题

```
入口(5类)                      插件层                             网关层(rvc-gateway 容器)
/rvc 命令 ─────► _send_song ───► 翻唱队列(_cover_slot, ≤3) ──┐
对话LLM工具 ───► _do_cover 后台任务 ──► 同上                  ├──► 全局串行锁 _TASK_SLOT
/本地翻唱 ─────► _execute_local_cover ──► 同上               │    (等待≤180s,拒绝超时)
TTS 钩子 ──────► _maybe_tts_reply ──► TTS队列(≤3) ───────────┘    Gradio queue(1, max 4)
WebUI 控制台 ──► page_api jobs ──► 同一网关(同样被串行约束)
```

改造前确认的问题(全部为【检查】得出,方案第二节要求的 9 项检查在阶段 A 报告与本轮源码复查中完成):

| # | 问题 | 方案条款 |
|---|---|---|
| 1 | 翻唱任务按会话(umo)存储,同群两用户共享一条记录,无法按用户查询/取消 | 3.1 |
| 2 | 无 task_id 级统一状态机,状态散在队列变量里 | 五 |
| 3 | 无重复提交检测 | 4.5 |
| 4 | 无用户可用的状态查询命令 | 6.6 |
| 5 | TTS 排队任务不可取消 | 7.1 |
| 6 | 无全局等待上限(翻唱 3 + TTS 3 各自为政) | 4.4 |
| 7 | 网关任务日志无排队等待耗时 | 十三 |
| 8 | `threading.Semaphore` 不跨进程 → 仅单 Gateway 实例安全 | 3.2 |

## 2. 修改的文件清单

| 文件 | 变更 |
|---|---|
| `task_registry.py` | **新增**:统一任务注册表(TaskRecord/TaskRegistry,纯标准库) |
| `main.py` | 修改:注册表初始化;`_register_active_task` 加防重复+登记;`_finish_active_task(error/timeout)`;`_predict_cover` 运行态+排队通知;TTS 钩子全链登记+取消检查;`cancel_cover_task` 扩展;新增 `/cover_status` 命令 |
| `_conf_schema.json` | 修改:advanced 卡新增 `task_queue_limit`/`duplicate_window_seconds`/`queue_position_notice` |
| `tests/test_task_registry.py` | **新增**:注册表语义回归(8 组用例) |
| 网关 `gateway.py` | 修改:日志记录 `queue_wait`(排队等待耗时) |

## 3. 任务调度架构图(改造后)

```
                    ┌── 插件统一任务注册表 self.tasks(TaskRegistry)
                    │    task_id → TaskRecord{会话/用户/类型/请求/状态/结果}
入口(5类,不变) ──►  │    全局等待上限 task_queue_limit(默认10) + 防重复窗口(默认120s)
                    │
   翻唱 ──► 翻唱队列(≤3) ──┐
   TTS  ──► TTS 队列(≤3) ──┼──► 网关 _TASK_SLOT 串行(忙时等待≤180s,拒绝超时)
                           │    └─ worker 常驻推理(一次一个)
   WebUI ──────────────────┘    Gradio queue(FIFO, concurrency=1)兜住同时提交

状态流转(唯一入口 TaskRegistry.set_status,非法流转被拒):
received → queued → running → completed
                ↘ cancelled      ↘ failed / timeout
                   running → cancelling → cancelled
```

## 4. 队列容量、执行策略与超时配置

| 配置 | 默认 | 说明 |
|---|---|---|
| `task_queue_limit` | 10 | 全局等待上限(received+queued 跨翻唱/TTS 总数),原子检查,超限拒绝 |
| `cover_queue_limit` / `tts_queue_limit` | 3 / 3 | 各自模块队列(已有,保留) |
| `duplicate_window_seconds` | 120 | 防重复窗口(同用户+同会话+规范化同请求,且先前任务仍在进行中) |
| `GW_SLOT_WAIT` | 180s | 网关忙时最长等待,超时明确报错 |
| `inference_timeout` / `tts_timeout` / `task_timeout_seconds` | 600/300/9000 | 既有超时,未改动 |

顺序策略:**FIFO**(按提交时间);网关侧同时提交的请求由 Gradio queue FIFO 兜住。预留优先级扩展点(注册表按 created_at 排序,可插队),本轮未实现。

## 5. 插件与网关接口变化

- 插件新增命令 **`/cover_status`**:只返回查询者自己的任务(类型/状态/排队位/错误摘要)+ 全局运行/排队数;他人任务内容不可见(隐私要求 6.6)。
- `取消翻唱任务`(LLM 工具 `cancel_cover_task`)扩展:除原翻唱取消外,把该用户所有进行中任务(含排队的 TTS)逐一 `request_cancel`;运行中 TTS 的取消为**软取消**(转换完成后不再发送,成品留 out 可补发);排队 TTS 立即取消。
- 网关 `/convert`、`/tts_convert`:忙时**等待而非拒绝**(上一轮已做),本轮补记 `queue_wait` 耗时;参数校验、路径白名单不变;旧调用完全兼容。

## 6. 任务状态流转

见第 3 节图。要点:非法流转被拒绝(终态不可变、received 不能直跳 completed);**从未入队/运行的记录收尾时按 failed/cancelled 处理**,不会卡在活跃态占用名额(单测用例 3b 实测覆盖)。

## 7. 取消机制与子进程终止

- 排队取消:注册表立即置 `cancelled`,队列位置自动顺延【单测】;
- 运行中取消(翻唱):既有机制——Gradio `job.cancel()` → 网关 `run_stage` 的 yield 循环感知 → `_kill_procs(task_id)` 终止该任务登记的全部子进程(terminate→wait(5s)→kill),`finally` 释放信号量【检查+既往运行验证】;
- 运行中取消(TTS):软取消——钩子在转换完成后、发送前检查 `cancel_requested`,放弃发送;成品保留在网关 out 目录;
- 子进程隔离:按 task_id 登记,`_kill_procs` 只杀本任务登记的进程,不触碰 RVC WebUI(rvc-cpu-test,已退出)或其他容器【检查】。

## 8. 多会话结果隔离

投递正确性由**事件绑定**保证:翻唱/TTS 的发送都使用请求时的 event 对象(含原始 umo/群号/用户),不存在"发给最后说话的人"的全局变量【检查:所有发送点均经 event/registry 绑定】。场景 A~E 的队列与注册表行为以单测覆盖(见第 10 节);真实 QQ 端到端多群并发未实测(方案禁止真实群消息),列为风险。

## 9. 任务持久化方式

- **评估结论:不引入 SQLite**。理由:注册表是内存态生命周期管理(活跃任务最长几十分钟),重启后"未完成任务"本身就不可恢复(方案 11.6 明确不自动重跑);持久化需求(历史/回放)已由既有 json 文件承担(`cover_history.json` / `tts_history.json` / 网关 logs/)。
- 重启语义:插件重启 → 内存队列丢失(未完成任务自然消失,无错误状态残留);网关重启 → `sweep_stale_tasks(ttl=0)` 清扫任务残骸,日志里只有成功任务不会误标【检查】。此语义已在第 13 节明确标注。

## 10. 4+ 会话并发模拟结果

`tests/test_task_registry.py`【单测,本地+NAS 全绿】:
1. 4 任务 FIFO 顺序(跨会话混排仍按提交时间)✅
2. 全局上限拒绝 + 运行中不占等待名额 ✅
3. 防重复:同用户同会话命中/跨会话/跨用户/不同请求/结束后放开 ✅
4. 非法流转拒绝、终态不可变 ✅
5. 排队取消/运行取消(cancelling 流转)/越权拒绝/管理员放行 ✅
6. 隐私视图无路径无句柄 ✅
7. received 异常收尾不卡名额 ✅
8. 去重规范化(空白/标点/大小写)✅

## 11. 连续真实音频任务

【实测】网关并发集成测试(容器内两个线程同时提交 `/tts_convert`):
- A:wall 38.8s(queue_wait 0,B 在 Gradio 队列等待);B:wall 46.1s(RVC 阶段仅 8.09s,A 结束后才开始);
- 总耗时 47.1s ≈ A+B 串行,两成品各 2.98s 正常;全局 FIFO 得到真实验证。

## 12. 资源占用

无新增常驻组件(注册表为纯内存 dict,记录上限 300+200 条,单条 <1KB,内存影响可忽略)。推理串行策略未变,峰值资源与 CUSTOM-7.2 交付时一致(RVC 峰值 ~4GiB,网关上限 12g)。

## 13. 未解决问题与风险

1. **跨模块 FIFO 弱保证**:翻唱队列与 TTS 队列独立,两队列的头任务同时到网关时由 Gradio queue 定序(先到先得),严格跨模块 FIFO 未保证(方案 4.3 允许"预留扩展");
2. **运行中 TTS 取消是软取消**(转换不中断,发送被拦截);硬取消需要 worker 支持请求级中断,本轮未做;
3. **真实 QQ 多群并发未实测**(方案禁止);投递绑定逻辑经代码检查与单测,建议生产观察;
4. 单 Gateway 实例假设:`threading.Semaphore` 不跨进程,**严禁启动第二个网关容器/进程**(当前部署单实例,无风险);
5. 插件重启丢失内存队列(方案 11 允许,已如实说明);网关重启清扫残骸;
6. `cover_history` 既有记录无 user_name(老数据昵称行显示"-")。

## 14. 回滚方案

```bash
# 插件回滚:用 20261101 前的备份目录覆盖 + 重载插件
# task_registry.py 留在目录里无副作用(未被 import 即不生效)
cp <AstrBot 数据目录>/astrbot/data/plugin_backups/astrbot_plugin_chorus_cover.bak-20261030-233244/. \
   <AstrBot 数据目录>/astrbot/data/plugins/astrbot_plugin_chorus_cover/
# 网关回滚:CUSTOM-7.2 版(差异仅 queue_wait 记录与等待逻辑)
cp <RVC 项目目录>/RVC-Project/gateway/.bak-custom72-*-gateway.py \
   <RVC 项目目录>/RVC-Project/gateway/gateway.py
cd <RVC 项目目录>/RVC-Project/gateway && docker compose up -d --build
```

## 15. 配置迁移

新增 3 键(`task_queue_limit`/`duplicate_window_seconds`/`queue_position_notice`)由幂等迁移自动补缺,默认 10/120/true,旧配置零改动(单测确认 111 键双向一致)。
