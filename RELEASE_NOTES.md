# Release Notes — astrbot_plugin_cucumber_rvc_console v1.0.0

首个公开发布版本。由 [sdfsfsk/matsuko_cover](https://github.com/sdfsfsk/matsuko_cover) v2.13.0 派生并深度重构,详见 [docs/UPSTREAM_COMPARISON.md](docs/UPSTREAM_COMPARISON.md)。

## 功能亮点

- **AI 翻唱**:多音源自动取歌(网易云/酷狗/酷我/QQ)→ 副歌裁剪 → 3HP 分离 → RVC 变声 → 卷积混响 → 语音发回群聊
- **TTS 语音回复**:@Bot 说话 → 人格反应 + 台词变声语音(独立音色/参数/混响)
- **WebUI 控制台**:任务监控 / RVC 控制台 / TTS 试音台 / 模型管理 / 混响调音 / 诊断
- **任务调度**:FIFO 队列、防重复、按用户取消、`/cover_status`、多会话隔离
- **常驻 worker**:省 ~40s/次模型加载,失败自动回退

## 依赖

- [cucumber-rvc-gateway](https://github.com/AkinoSa/cucumber-rvc-gateway) v1.0.0+(独立部署,Docker)
- AstrBot >= 4.16(实测 4.29),支持 Function Calling 的 LLM 提供商

## 已知限制

- 仅验证 CPU 推理(x86_64);GPU/ARM 未验证
- 推理顺序可能与提交顺序交换(全局串行保证不并发)
- 模型性别下拉为静态三槽位,新模型经 extra 自动维护

## 兼容性

| 本插件 | 网关 | 说明 |
|---|---|---|
| 1.0.0 | 1.0.0+ | 完整功能(TTS 接口/混响扩展参数/index_matches) |
| 1.0.0 | 旧私有版(CUSTOM-7.2/8) | 可用,无 index_matches/queue_wait 展示 |
| <1.0.0 | — | 不支持(配置结构不同;可从旧 chorus_cover 自动迁移) |
