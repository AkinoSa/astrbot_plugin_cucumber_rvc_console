# 个性化插件版本清单（简版）

- **当前修订**：CUSTOM-6.10（配置同步修复/混响接入配置/设置页签布局）（上游 0.12.2 + AstrBot 4.29.0）
- **详细记录**：`../../CHANGELOG_CUSTOM.md`（逐项变更+回滚）、`../../CUSTOMIZATION.md`（总入口）
- **测试**：`../../tests/test_custom_autopick.py`（52 项，import 真实模块；网关切片单测在容器内跑）
- **备份索引**（都在插件目录内，未删除）：
  - `.bak-20261029-plugin-010433/` — CUSTOM-2 前（自动选歌第一轮之前）
  - `<AstrBot 数据目录>/astrbot/data/plugin_backups/.bak-20261029-014911/` — CUSTOM-3 前（第二轮修复前）
  - `<AstrBot 数据目录>/astrbot/data/plugin_backups/.bak-20261029-022435/` — CUSTOM-3 开始前
  - `<AstrBot 数据目录>/astrbot/data/plugin_backups/.bak-20261029-custom4/` — CUSTOM-4 开始前（本轮还原点，含 main.py/Schema/配置）
  - 配置备份：`data/config/astrbot_plugin_cucumber_rvc_console_config.json.bak-*`
  - 网关：`<RVC 项目目录>/RVC-Project/gateway/.bak-20261029-custom4-gateway.py`、`.bak-20261029-custom4-compose.yaml`
- **网关侧**（不在本目录）：`<RVC 项目目录>/RVC-Project/gateway/.bak-20261029-deploy-gateway.py`
- **快速回滚到本轮之前**：
  ```bash
  cd <AstrBot 数据目录>/astrbot/data/plugins/astrbot_plugin_cucumber_rvc_console
  cp -a .bak-20261029-022435/main.py main.py
  cp -a .bak-20261029-022435/_conf_schema.json _conf_schema.json
  cp -a .bak-20261029-022435/source_router.py source_router.py 2>/dev/null || true
  ```
- **导出**：见 `../../CUSTOMIZATION.md` 的 tar 命令（自动排除 Cookie/缓存/模型/备份）。
- **下次改动**：先备份 → 改 → `py_compile` + 跑测试 → 在 CHANGELOG_CUSTOM.md 追加修订号。

## CUSTOM-4 结构变更提示

- 配置从扁平字段改为嵌套（`basic` / `selection` / `rvc` / `tuning` / `separation` /
  `local_audio` / `sources` / `advanced`），旧扁平值已迁移，迁移函数幂等。
- `/设置模型性别` 与 WebUI「模型性别」下拉写同一份数据（`tuning.gender.model_genders`）。
- 回滚到 CUSTOM-3：恢复 `.bak-20261029-custom4/` 三件套 + `*.bak-20261029-custom4` 配置，
  网关恢复 `.bak-20261029-custom4-*` 后重建 rvc-gateway。
