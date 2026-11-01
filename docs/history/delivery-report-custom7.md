# CUSTOM-7 交付报告

> astrbot_plugin_chorus_cover 升级:TTS 音色转换 / 真 Reverb 混响 / WebUI 重构 / 常驻 worker
> 交付日期:2026-11-01 · 版本:插件 0.13.0(上游 void-HaoNan 0.12.2 + CUSTOM-1~7.2)
> 本报告对应升级方案(阶段 A~E)的验收交付,依据实测数据,不含推断值。

---

## 1. 修改前后的文件清单

### 插件(`<AstrBot 数据目录>/astrbot/data/plugins/astrbot_plugin_chorus_cover/`,版本 0.12.2 → 0.13.0)

| 文件 | 变更 |
|---|---|
| `tts.py` | **新增**:TTS 管理器(Provider 解析→TTS 生成→网关转换→校验→降级)、冷却门、Edge 子进程兜底 |
| `main.py` | 修改:TTS 配置读取与钩子(`on_llm_response_tts`)、CONFIG_PATHS +28 键、`_get_reverb_kwargs` 支持卷积混响 8 参与 echo legacy |
| `intent_gate.py` | 修改:新增 `TICKET_PATTERNS_TTS`、`is_tts_request` / `is_cover_request` / `has_tts_ticket` |
| `_conf_schema.json` | 修改:新增 `tts` 卡片(enable/model/inference/trigger/reverb)、separation 增卷积混响 4 参 + echo 子卡;设置项描述全面改写 |
| `page_api.py` | 修改:新增 `/tts/providers`、`/tts/generate` 路由;`/rvc/convert` 支持单次混响试听参数 |
| `pages/dashboard/*` | 修改:新增 TTS 控制台、音频效果页签;设置分类 9→5 并加「通知与播报」;音色列表编辑器(别名+性别合并);1s 计时;保存不跳回 |
| `tests/test_tts_intent.py`、`tests/test_reverb_kernel.py`、`tests/test_config_sync.py` | **新增**:意图判定 / 混响内核 / 配置一致性三套回归 |
| `metadata.yaml`、`CHANGELOG_CUSTOM.md` | 版本 0.13.0;CUSTOM-7 / 7.1 / 7.2 条目 |

### 网关(`<RVC 项目目录>/RVC-Project/gateway/`)

| 文件 | 变更 |
|---|---|
| `gateway.py` | 修改:卷积混响内核(合成 IR + FFT)、`/tts_convert` 新端点、echo legacy、`run_rvc_stage` 统一推理入口 + `RvcWorker` 常驻 worker 管理器、缓存 key 扩展、`/model_info` 加 index_matches |
| `rvc_worker.py` | **新增**:常驻推理 worker(复用 infer/cli.py 函数,JSON Lines 协议,空闲自退) |
| `Dockerfile` / `compose.yaml` | 修改:COPY worker;内存 8g→12g(swap 16g) |

## 2. 备份位置(全部保留,未删除任何文件)

| 对象 | 备份 |
|---|---|
| 插件(升级前 0.12.2 完整目录) | `<AstrBot 数据目录>/astrbot/data/plugin_backups/astrbot_plugin_chorus_cover.bak-20261030-233244` |
| 网关 gateway.py(CUSTOM-6.10 版) | `<RVC 项目目录>/RVC-Project/gateway/.bak-custom7-20261030-233244-gateway.py` |
| 网关 gateway.py(worker 前 CUSTOM-7 版) | `.bak-custom72-20261101-*-gateway.py` |
| mihomo(目录里 10-07 的旧 compose) | 同目录 `docker-compose.yml.old-20261007`(与本插件无关,顺手保护) |

## 3. Gateway API 改动

| 端点 | 变化 |
|---|---|
| `/tts_convert` | **新增**:TTS 音频换声。输入白名单(`/work/tts`、`/work/uploads`)、时长 ≤60s(`GW_TTS_MAX_SECONDS`)、大小 ≤30MB、自动转 44.1k mono、无缓存、任务日志落 logs/、返回 JSON(host_path 等)。参数按函数签名命名:`tts_src/model_dropdown/key_shift/f0_method/index_rate/protect/rms_mix_rate/resample_sr/output_format/speaker_id + reverb_*(8)` |
| `/convert` | 新增可选参数:`reverb_room_size/damping/pre_delay/width`、`echo_enabled/wet/decay`;缓存 key 加入全部混响字段(旧缓存自然失效,不会错误命中) |
| `/model_info` | 新增 `index_matches`(每模型 index 匹配情况) |
| 推理执行 | `run_rvc_stage`:常驻 worker 优先 → 失败自动回退子进程(`GW_WORKER=0` 可停用) |

## 4. TTS Provider 兼容性(AstrBot 4.29.0 实测)

- `get_audio(text)->str`(音频路径)✓;`get_using_provider(TEXT_TO_SPEECH, umo)` 同步 ✓;
- **`get_provider_by_id` 是 async**,必须 await(参考插件 astrbot_plugin_rvc 此处为同步调用,在 4.29 上失效,未照搬);
- 已实测:Edge TTS(进程内偶发 403 风控 → 自动重试 → **独立子进程兜底**,复用其配置音色)、**fish.audio**(走 mihomo 代理,普通 HTTPS,稳定,推荐);
- 失败降级:连续失败自动降级纯文字,不影响聊天主流程(钩子层吞异常)。

## 5. 歌曲翻唱与 TTS 模型隔离验证

- 配置层:`tts.*` 卡片与 `rvc.*` 卡片键名/路径完全独立;`test_config_sync` 校验 104 键双向一致;
- 运行层:worker 单槽缓存,TTS 用 rym、翻唱用 deng/nuoyemu 时自动重载,互不覆盖对方配置;
- 实测:翻唱(nuoyemu)→ TTS(rym)交替,输出模型字段各自正确。

## 6. Reverb 实现与默认参数

- 实现:合成 IR(双声道指数衰减噪声 + 早反射) + `scipy.fftconvolve` 卷积;`wet` 即湿声能量比(IR 单位能量归一);峰值保护(>0.95 压回);输出保留自然尾音(≤4s);IR 固定种子(确定性);
- 默认(关闭,开启后):Wet 0.18 / Dry 1.0 / Room 0.65 / Decay 1.4s / Damping 0.45 / Pre-delay 18ms / Width 0.80;
- 8 参数全部真实生效(测试逐项验证:decay=尾音-60dB 时长、room=早反射起点精确可预测、width=0 真单声道等);
- 只作用于 RVC 输出人声(网关本来不混伴奏);旧 aecho 保留为 `echo_*` legacy(默认关,与 reverb 互斥);
- **试听文件**(同源同参数,仅混响不同):
  - 无混响:`<RVC 数据目录>/rvc/gateway_work/out/1ae90602941b.fade.wav`
  - 有混响:`<RVC 数据目录>/rvc/gateway_work/out/fd9cb4845244_reverb.wav`
- fish.audio 原声(未变声):`/vol1/1000/fish_test.wav`

## 7. 测试结果汇总

| 组 | 内容 | 结果 |
|---|---|---|
| 意图判定 | 40+ 触发/不触发对照(方案 8.2 混淆表全量) | ✅ 本地全绿 |
| 混响内核 | IR 形状/确定性/5 参数语义/峰值保护 | ✅ 本地全绿 |
| 配置一致性 | 104 键 schema↔CONFIG_PATHS 双向 | ✅ 本地+生产全绿 |
| 翻唱回归 | 取歌→裁剪→分离→RVC→淡入淡出(真实群友请求 + 测试样本) | ✅ |
| 混响 A/B | 同片段开关对比,engine=conv_ir 正确应用 | ✅ |
| TTS 全链路 | fish.audio→rym 变声 ✅;Edge 进程内 403→子进程兜底 ✅ | ✅ |
| worker | TTS 冷 41.5s→热 9.9s;歌曲 RVC ~130s→54.1s;模型切换自动重载;4/4 零回退 | ✅ |
| 兼容 | 旧插件(0.12.2)↔新网关:重建当晚真实群友翻唱直接成功 | ✅ |

## 8. 资源占用与耗时(i5-6500 CPU / 16GB / 无显卡)

| 指标 | 数值 |
|---|---|
| 网关常驻内存 | 1.227 GiB(基线)→ 2.303 GiB(worker 热态,**+1.08 GiB**),空闲 10min 回落 |
| 容器上限 | 12g(含 swap 16g) |
| TTS 转换(2s 音频) | 冷 41.5s → 热 **9.9s**;fish.audio 生成 ~3s |
| 歌曲翻唱(30s 样本,25s 目标) | 分离 ~70s + RVC 热 54s ≈ **125~180s** |
| 混响后处理(25s 音频) | ~0.2s |

## 9. 已知问题与限制

1. TTS 热路径 ~10s,未达当初估计的 3-5s(剩余为 torch 推理本体 + rmvpe + faiss,再压需改 RVC 库,不做);
2. worker 单槽模型缓存:翻唱(deng)与 TTS(rym)交替使用时各有一次 ~40s 重载(用户已选方案 A:保持现状);
3. Edge TTS 进程内连接会被风控 403(环境相关),已由重试+子进程兜底覆盖;日常建议用 fish.audio;
4. fish.audio 依赖 mihomo 容器(`127.0.0.1:7890`,订阅每日自动更新);mihomo 停止时 fish.audio 不可用并自动降级;
5. `api.fish.audio` 在当前网络被 DNS 污染,必须走代理(已解决);`api.fish-audio.cn` NXDOMAIN,AstrBot 默认值不可用;
6. NAS 根分区 10-30 曾被某临时占用写满(后自愈),部署脚本已用 `TMPDIR/BUILDX_CONFIG=/vol1/...` 规避复发影响;
7. 设置页「音色列表」里改别名/性别后,提示"需重载插件生效"(键在 RELOAD_REQUIRED,既有行为);
8. 原生 AstrBot 设置页仍显示 9 张卡片(键名不动),WebUI 内才是 5 大类 + 通知归并。

## 10. 配置迁移说明

- **零手工迁移**:新增键(tts.*、reverb_room_size/damping/pre_delay/width、echo.*、audio.*)由 `_migrate_config_layout` 幂等补缺,旧值一个没动;
- 默认值:reverb 关闭;TTS 关闭、模型 rym.pth;所有旧配置语义不变;
- 混响缓存 key 扩展导致旧缓存不命中(预期,首次重跑);其余缓存行为不变。

## 11. 回滚方法

```bash
# 插件回滚(回到 0.12.2)
cp -a <AstrBot 数据目录>/astrbot/data/plugin_backups/astrbot_plugin_chorus_cover.bak-20261030-233244/. \
      <AstrBot 数据目录>/astrbot/data/plugins/astrbot_plugin_chorus_cover/
# 然后在 AstrBot WebUI 重载插件(配置不需回滚,新增键会被忽略)

# 网关回滚(worker 前)
cp <RVC 项目目录>/RVC-Project/gateway/.bak-custom7-20261030-233244-gateway.py \
   <RVC 项目目录>/RVC-Project/gateway/gateway.py
cd <RVC 项目目录>/RVC-Project/gateway && docker compose up -d --build
# (如需同时回到 8g 上限,把 compose.yaml 里 mem_limit 改回 8g)

# mihomo(如需停用,不影响其他功能,fish.audio 自动降级)
cd /vol1/1000/docker/mihomo && docker compose down
```

## 12. 新增外部组件

- **mihomo 容器**(`/vol1/1000/docker/mihomo/`):为 fish.audio 提供出站代理;仅绑定宿主机回环 7890;配置内含订阅链接(敏感,勿外传);旧 compose 已改名留档。
