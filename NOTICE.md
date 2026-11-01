# NOTICE

本项目 **astrbot_plugin_cucumber_rvc_console**（副歌翻唱 · 自维护分支）是第三方维护的分支，**不是上游官方版本**。

## 来源

| 项目 | 地址 / 版本 |
|---|---|
| 上游仓库 | <https://github.com/sdfsfsk/matsuko_cover> |
| 上游插件名 | `astrbot_plugin_matsuko_cover` |
| 上游基线版本 | **v2.13.0** |
| 上游再上游 | <https://github.com/CCYellowStar2/astrbot_plugin_rvc_svc>（上游 README 注明基于该项目扩展） |
| 本分支仓库 | <https://github.com/void-HaoNan/astrbot-plugin-chorus-cover> |
| 配套中间层（必需，另一仓库） | <https://github.com/void-HaoNan/chorus-mw> |
| 本分支维护者 | void-HaoNan |
| 本分支版本 | v0.11.2 |

## 许可证

- 本分支与上游一致，采用 **GNU Affero General Public License v3.0（AGPL-3.0）**。
- 仓库内的 `LICENSE` 文件**保持上游原样，未作任何修改**。
- 上游代码版权归原作者（Matsuko 及其上游作者）所有；本分支新增与修改的部分同样以 AGPL-3.0 发布。
- 以网络服务形式提供本插件时，按 AGPL-3.0 第 13 条应向使用者提供完整对应源码（即本仓库）。

## 修改声明（AGPL-3.0 第 5 条）

自 **2026-10-27** 起，在 v2.13.0 基础上做了以下修改，修改后的完整源码就是本仓库：

1. **新增 `outbound.py`**：统一对话出口 `Outbound`，把进度提示、风控重试提示、报错详情三类硬编码文本收敛到一个出口，交给人格模型（`provider.text_chat`）说人话；出口层异常全部吞掉并退回极短兜底文本。
2. **`main.py`**：
   - `rvc_cover` / `smart_cover` 的工具返回值去掉"🎵 正在从【…】…"装饰，改为一句中性短句交给 LLM；
   - 风控重试提示改走 `outbound.say(event, "retry", …)`（默认静音）；
   - `_send_progress_notice` 改走 `outbound.say(event, "progress", …)`（默认静音）；
   - 失败强制发送整段错误文本改为 `outbound.say(event, "failed", …)`，技术细节只写日志；
   - `_smart_cover_async` 与批量翻唱的失败判断只认 `❌` / `FAIL:` 明确标记，删掉关键词匹配；
   - 失败路径不再额外触发一次 LLM 说话，避免同一任务两条回复。
3. **`_conf_schema.json`**：新增 `notify_progress`、`notify_retry` 两个开关（缺省 `false`）。
4. **元数据与文档**：`metadata.yaml` 改为本分支的名字/版本/作者/仓库；新增 `CHANGELOG.md` 与 `NOTICE.md`；`README.md` 按本分支的真实功能另写。
5. **v0.3.0（2026-10-27）**：删除洛雪（LX-Source）兜底与 `download_via_lx` / `build_lx_url`，只保留网易云官方接口 + cookie；QQ 音源下线，搜歌与取歌都只走网易云。
6. **v0.7.0（2026-10-27）**：搜歌/取歌改为多音源路由（网易云 → 酷狗 → 酷我 → QQ 音乐，`source_order` 可配），复用点歌插件 `astrbot_plugin_music` 的平台实现与配置。
7. **v0.9.0（2026-10-27）**：收窄为纯 RVC 插件——删除另外两套推理引擎的命令、工具、配置与代码分支；删除本分支新增过的 `api.py`（旧 QQ / 网易云直连接口，改写后已无人调用）。
8. **v0.11.0（2026-10-28）**：新增 `rvc_models_dir` 设置项，`/刷新rvc模型` 与 `get_available_models` 回显真实音色模型目录（走中间层 `/model_info`）。
9. **v0.11.2（2026-10-28）**：文档补齐配套中间层 [`chorus-mw`](https://github.com/void-HaoNan/chorus-mw) 的仓库地址与职责说明——它一直是独立仓库、独立进程，**没有**并入本插件。

> 架构说明：本插件不包含任何分离 / 切副歌 / 变声 / 混音代码，这些都在 `chorus-mw` 里。插件与中间层靠 Gradio 4（sse）通信；RVC 整合包自带 WebUI 是 Gradio 3.14（ws），两者协议不兼容，所以中间层不可省略。


本分支**不再跟随上游更新**。上游假如更新了同名功能，本分支不会自动合并。
