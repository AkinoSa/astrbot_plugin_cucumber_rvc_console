#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""CUSTOM-4 测试集（含 CUSTOM-3 回归）。import 真实模块，不用文本提取。

运行：cd <AstrBot 数据目录>/astrbot/data/plugins && \
      /vol1/@apphome/astrbot/penv/bin/python3 astrbot_plugin_cucumber_rvc_console/tests/test_custom_autopick.py
"""
import asyncio
import os
import inspect
import json
import sys
import types
from types import SimpleNamespace

# 环境自适应:NAS 实机 → 开发镜像 → 本地仓库;均不可用时跳过
_CANDIDATE_PLUGINS = [
    "<AstrBot 数据目录>/astrbot/data/plugins",
    "/vol1/@appdata/astrbot/data/plugins",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
]
PLUGINS = next((p for p in _CANDIDATE_PLUGINS
                if os.path.isdir(os.path.join(p, "astrbot_plugin_cucumber_rvc_console"))),
               None)
if PLUGINS is None:
    print("test_custom_autopick: SKIP(未找到 AstrBot 插件目录,需 NAS 实机环境)")
    raise SystemExit(0)
sys.path.insert(0, PLUGINS)

import importlib
_main = importlib.import_module("astrbot_plugin_cucumber_rvc_console.main")
MusicPlugin = _main.MusicPlugin
_NestedConfig = _main._NestedConfig
_migrate = _main._migrate_config_layout
CONFIG_PATHS = _main.CONFIG_PATHS

results = []

def check(name, cond, detail=""):
    results.append(bool(cond))
    print("[%s] %s%s" % ("PASS" if cond else "FAIL", name, f"  {detail}" if detail else ""))

# ============ 通用桩 ============
class FakeRaw(dict):
    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.save_count = 0

    def save_config(self):
        self.save_count += 1

def make_self(extra=None):
    s = SimpleNamespace()
    s._NON_ORIGINAL_MARKERS = MusicPlugin._NON_ORIGINAL_MARKERS
    s._ORIGINAL_HINT_MARKERS = MusicPlugin._ORIGINAL_HINT_MARKERS
    s._ARTIST_ALIASES = MusicPlugin._ARTIST_ALIASES
    s._claimed_original_name = MusicPlugin._claimed_original_name
    s.auto_pick_min_score = 45.0
    s.auto_pick_min_margin = 8.0
    for name, fn in (extra or {}).items():
        setattr(s, name, types.MethodType(fn, s))
    return s

def S(name, artists, alias=""):
    return {"name": name, "artists": artists, "alias": alias, "source": "netease"}

# ============ 一、CUSTOM-3 回归：选歌评分 ============
self_ = make_self()

try:
    idx, score, reason = MusicPlugin._auto_pick_song(
        self_, "晴天", [S("晴天", "周杰伦"), S("晴天 (原唱 周杰伦)", "RyaVocal")])
    check("C3 回归 NameError（真实作用域）", idx == 0, f"pick={idx} score={score:.1f}")
except NameError as e:
    check("C3 回归 NameError（真实作用域）", False, str(e))

def pick(query, songs, hint=""):
    return MusicPlugin._auto_pick_song(self_, query, songs, artist_hint=hint)

idx, sc, rs = pick("晴天", [S("晴天", "Jay"), S("晴天", "清柚")], hint="周杰伦")
check("C3 歌手中文名 -> Jay 别名命中", idx == 0, f"{sc:.1f}")
idx, sc, rs = pick("晴天 女声版", [S("晴天", "周杰伦"), S("晴天 (女声版)", "小美")])
check("C3 用户指定版本被尊重", idx == 1, f"{sc:.1f}")
idx, sc, rs = pick("晴天", [S("晴天", "周杰伦"), S("晴天", "李四")])
check("C3 同名不硬猜", idx is None, f"{sc:.1f}")
check("C3 claimed 全角/半角",
      MusicPlugin._claimed_original_name("晴天 (原唱 周杰伦)") == "周杰伦"
      and MusicPlugin._claimed_original_name("晴天（原唱：周杰伦）") == "周杰伦")

# ============ 二、配置适配层 ============
raw = FakeRaw({
    "basic": {"enable_rvc": True, "chorus_seconds": 20},
    "rvc": {"models": {"rvc_default_model": "nuoyemu"}, "inference": {"protect": 0.4}},
    "legacy_only_key": "flat-value",
})
cfg = _NestedConfig(raw)
check("adapter 读嵌套", cfg.get("chorus_seconds") == 20 and cfg.get("protect") == 0.4)
check("adapter 回退扁平键", cfg.get("legacy_only_key") == "flat-value")
check("adapter 缺失返回默认", cfg.get("not_exist", "dft") == "dft")
cfg["rvc_default_model"] = "rym"
check("adapter 写回嵌套", raw["rvc"]["models"]["rvc_default_model"] == "rym")
cfg["something_new"] = 1
check("adapter 未知键写顶层", raw["something_new"] == 1)
check("adapter save_config 代理", (cfg.save_config(), raw.save_count)[1] == 1)
check("adapter contains", "chorus_seconds" in cfg and "not_exist" not in cfg)

# ============ 三、扁平 -> 嵌套迁移 ============
flat = FakeRaw({
    "chorus_seconds": 20, "rvc_default_model": "nuoyemu", "protect": 0.4,
    "model_genders": {"deng": "女声"}, "netease_quality": 320,
})
changed1 = _migrate(flat)
changed2 = _migrate(flat)
check("迁移写入嵌套", flat["basic"]["chorus_seconds"] == 20
      and flat["rvc"]["models"]["rvc_default_model"] == "nuoyemu"
      and flat["rvc"]["inference"]["protect"] == 0.4
      and flat["sources"]["netease_quality"] == 320)
check("迁移幂等（第二次不再改动）", changed1 is True and changed2 is False)
flat["basic"]["chorus_seconds"] = 45
_migrate(flat)
check("迁移不覆盖嵌套已有值", flat["basic"]["chorus_seconds"] == 45)

# ============ 四、性别配置单一来源 ============
def mk_gender_self(raw_cfg):
    s = SimpleNamespace()
    s.config = _NestedConfig(raw_cfg)
    s._parse_gender_map = types.MethodType(MusicPlugin._parse_gender_map, s)
    s._norm_gender = MusicPlugin._norm_gender
    s._load_model_genders = types.MethodType(MusicPlugin._load_model_genders, s)
    s._write_model_gender = types.MethodType(MusicPlugin._write_model_gender, s)
    s.model_gender_map = {}
    s.model_genders = {}
    s.model_genders_extra = {}
    return s

graw = FakeRaw({
    "tuning": {"gender": {
        "model_genders": {"deng": "未知", "nuoyemu": "未知", "rym": "未知"},
        "model_gender_map": ["deng:female", "nuoyemu.pth:male", "custom_voice:female"],
    }},
})
gs = mk_gender_self(graw)
merged = gs._load_model_genders(gs.config)
check("性别迁移：旧列表 -> 统一对象", merged == {"deng": "female", "nuoyemu": "male", "custom_voice": "female"})
check("性别迁移：schema 模型写进下拉对象", graw["tuning"]["gender"]["model_genders"]["deng"] == "女声"
      and graw["tuning"]["gender"]["model_genders"]["nuoyemu"] == "男声")
check("性别迁移：非 schema 模型写进 extra", graw["tuning"]["gender"]["model_genders_extra"]["custom_voice"] == "女声")
check("性别迁移：旧列表被清空", graw["tuning"]["gender"]["model_gender_map"] == [])
check("性别迁移：unknown 不算已设置",
      gs._load_model_genders(_NestedConfig(FakeRaw({"tuning": {"gender": {"model_genders": {"rym": "未知"}}}}))) == {})

gs._write_model_gender("rym", "female")
check("命令写性别 -> 下拉对象（WebUI 同源）", graw["tuning"]["gender"]["model_genders"]["rym"] == "女声"
      and gs.model_gender_map.get("rym") == "female")
gs._write_model_gender("newm", "male")
check("命令写新模型 -> extra", graw["tuning"]["gender"]["model_genders_extra"]["newm"] == "男声")
gs._write_model_gender("newm", "unknown")
check("命令设未知 -> 移除映射", "newm" not in gs.model_gender_map)

# ============ 五、刷新保值 ============
def mk_refresh_self(models, raise_exc=False):
    raw_cfg = FakeRaw({
        "rvc": {"models": {
            "rvc_default_model": "nuoyemu",
            "rvc_models_keywords": ["deng.pth|||逗哥", "nuoyemu.pth|||", "rym.pth|||"],
        }},
        "tuning": {"gender": {"model_genders": {"deng": "女声", "nuoyemu": "女声", "rym": "女声"}}},
    })
    s = SimpleNamespace()
    s.config = _NestedConfig(raw_cfg)
    s.rvc_models_keywords = list(raw_cfg["rvc"]["models"]["rvc_models_keywords"])
    s._default_model_note = MusicPlugin._default_model_note
    s._set_engine_models = types.MethodType(MusicPlugin._set_engine_models, s)
    s._update_models_from_api = types.MethodType(MusicPlugin._update_models_from_api, s)
    async def fake_fetch():
        if raise_exc:
            raise RuntimeError("gateway down")
        return {}, models
    s._fetch_models_info = fake_fetch
    return s, raw_cfg

# 5.1 正常刷新：别名/默认音色/性别全保留
s, raw_cfg = mk_refresh_self(["deng.pth", "nuoyemu.pth", "rym.pth", "newvoice.pth"])
res = asyncio.run(s._update_models_from_api())
km = raw_cfg["rvc"]["models"]["rvc_models_keywords"]
check("刷新成功 ok=True", res["ok"] is True)
check("刷新保留别名", "deng.pth|||逗哥" in km and "nuoyemu.pth|||" in km)
check("刷新增量模型", any(x.startswith("newvoice.pth") for x in km))
check("刷新不动默认音色", raw_cfg["rvc"]["models"]["rvc_default_model"] == "nuoyemu")
check("刷新不动性别映射", raw_cfg["tuning"]["gender"]["model_genders"]["deng"] == "女声")

# 5.2 空列表：不清空
s, raw_cfg = mk_refresh_self([])
res = asyncio.run(s._update_models_from_api())
check("空列表 -> 失败且保留原配置", res["ok"] is False
      and raw_cfg["rvc"]["models"]["rvc_models_keywords"] == ["deng.pth|||逗哥", "nuoyemu.pth|||", "rym.pth|||"]
      and raw_cfg["rvc"]["models"]["rvc_default_model"] == "nuoyemu")

# 5.3 接口异常：不清空
s, raw_cfg = mk_refresh_self([], raise_exc=True)
res = asyncio.run(s._update_models_from_api())
check("接口异常 -> 失败且保留原配置", res["ok"] is False
      and len(raw_cfg["rvc"]["models"]["rvc_models_keywords"]) == 3)

# 5.4 原值失效：保留并提示
s, raw_cfg = mk_refresh_self(["deng.pth", "rym.pth"])
res = asyncio.run(s._update_models_from_api())
check("默认音色失效 -> 保留原值并返回提示", res["ok"] is True
      and res["missing_default"] == "nuoyemu"
      and raw_cfg["rvc"]["models"]["rvc_default_model"] == "nuoyemu")

# 5.5 .pth 差异归一
check("带/不带 .pth 匹配", MusicPlugin._default_model_note("nuoyemu.pth", ["deng|||", "nuoyemu|||"]) == ""
      and MusicPlugin._default_model_note("nuoyemu", ["deng.pth|||", "nuoyemu.pth|||"]) == "")

# ============ 六、Schema 结构与兼容性 ============
SCHEMA = PLUGINS + "/astrbot_plugin_cucumber_rvc_console/_conf_schema.json"
sch = json.load(open(SCHEMA, encoding="utf-8-sig"))
ok_types = {"string", "text", "int", "float", "bool", "list", "file", "object", "template_list", "dict"}
def walk(node, path=""):
    bad = []
    for k, v in node.items():
        if not isinstance(v, dict):
            bad.append(f"{path}{k}: 非 dict")
            continue
        t = v.get("type")
        if t not in ok_types:
            bad.append(f"{path}{k}: 非法 type={t}")
        if t == "object" and not isinstance(v.get("items"), dict):
            bad.append(f"{path}{k}: object 缺 items")
        if t == "object":
            bad += walk(v["items"], f"{path}{k}.")
    return bad

errs = walk(sch)
check("Schema 字段类型全部受支持", not errs, "; ".join(errs[:3]))
check("Schema 8 张外层卡片", list(sch.keys()) == ["basic", "selection", "rvc", "tuning",
                                                 "separation", "local_audio", "sources", "advanced"])
sub_objects = [f"{g}.{s}" for g, v in sch.items() for s, sv in v.get("items", {}).items()
               if sv.get("type") == "object"]
check("两层嵌套卡片存在", {"selection.smart", "rvc.models", "rvc.inference",
                           "tuning.shift", "tuning.gender", "separation.output"} <= set(sub_objects),
      str(sub_objects))
check("模型性别下拉为中文选项",
      sch["tuning"]["items"]["gender"]["items"]["model_genders"]["items"]["deng"]["options"]
      == ["未知", "男声", "女声"])
check("旧性别列表已隐藏（仅迁移用）",
      sch["tuning"]["items"]["gender"]["items"]["model_gender_map"].get("invisible") is True)
check("默认音色选项=网关真实模型",
      sch["rvc"]["items"]["models"]["items"]["rvc_default_model"]["options"] == ["deng", "nuoyemu", "rym"])
check("翻唱时长 hint 说明 +5 缓冲与 full 不支持",
      "+ 5 秒" in sch["basic"]["items"]["chorus_seconds"]["hint"]
      and "full" in sch["basic"]["items"]["chorus_seconds"]["hint"])
check("不生效参数已标注",
      "不使用" in sch["separation"]["items"]["inst_volume_db"]["description"]
      or "不生效" in sch["separation"]["items"]["inst_volume_db"]["hint"])

# 适配层覆盖：main.py 里所有 config.get 键都要有映射或明确白名单
import re as _re
src = open(PLUGINS + "/astrbot_plugin_cucumber_rvc_console/main.py", encoding="utf-8").read()
used_keys = set(_re.findall(r'config\.get\("([a-z_0-9]+)"', src))
unmapped = sorted(k for k in used_keys if k not in CONFIG_PATHS)
check("config 键全部映射或白名单", set(unmapped) <= {"admins_id"}, str(unmapped))

# ============ 七、两入口共用公共选歌（C3 延续） ============
check("_handle_cover 用公共选歌", "_auto_pick_from_sources" in inspect.getsource(MusicPlugin._handle_cover))
check("_do_cover 用公共选歌", "_auto_pick_from_sources" in inspect.getsource(MusicPlugin._do_cover))


# ============ 八、补发成品（插件重载中断后的补救） ============
import tempfile, os as _os, time as _time
from pathlib import Path as _Path

_resolve = _main.resolve_resend_file
_list = _main.list_recent_outputs

with tempfile.TemporaryDirectory() as td:
    base = _Path(td)
    (base / "abc12345.wav").write_bytes(b"x" * 100)
    (base / "abc12345.picked.wav").write_bytes(b"y" * 200)
    (base / "zzz999.wav").write_bytes(b"z" * 50)
    _os.utime(base / "zzz999.wav", (_time.time() + 10, _time.time() + 10))

    p, r = _resolve("abc12345", td)
    check("补发：按 task_id 命中", p is not None and p.name == "abc12345.wav", str(p))
    p, r = _resolve("abc12345.picked.wav", td)
    check("补发：按完整文件名命中", p is not None and p.name == "abc12345.picked.wav")
    p, r = _resolve("zzz999", td)
    check("补发：只给前缀也能命中", p is not None and p.name == "zzz999.wav")
    p, r = _resolve(str(base / "zzz999.wav"), td)
    check("补发：目录内绝对路径可用", p is not None and p.name == "zzz999.wav")
    p, r = _resolve("/etc/passwd", td)
    check("补发：目录外路径被拒绝", p is None and "只允许" in r, r)
    p, r = _resolve("不存在xyz", td)
    check("补发：找不到给出原因", p is None and "没找到" in r, r)
    recent = _list(td, limit=2)
    check("补发：最近成品按时间倒序", [x.name for x in recent] == ["zzz999.wav", "abc12345.picked.wav"],
          str([x.name for x in recent]))
    p, r = _resolve("", td)
    check("补发：空参数给出提示", p is None and "没有给" in r)

# 真实成品目录可解析（只读校验，不发送）
_real, _reason = _resolve("bc241ced8e31")
check("补发：真实成品 bc241ced8e31 可解析", _real is not None and _real.is_file(), _reason or str(_real))


# ============ 九、CUSTOM-5：淡入淡出开关 ============
check("淡入淡出：路径表映射", CONFIG_PATHS.get("enable_fade") == ("separation", "output", "enable_fade"))
_raw5 = FakeRaw({"separation": {"output": {"enable_fade": False}}})
_cfg5 = _NestedConfig(_raw5)
check("淡入淡出：读嵌套值", _cfg5.get("enable_fade") is False)
_cfg5["enable_fade"] = True
check("淡入淡出：写回嵌套", _raw5["separation"]["output"]["enable_fade"] is True)
check("淡入淡出：schema 有开关且默认开",
      sch["separation"]["items"]["output"]["items"]["enable_fade"]["default"] is True
      and sch["separation"]["items"]["output"]["items"]["enable_fade"]["type"] == "bool")
check("淡入淡出：网关探测方法存在",
      callable(getattr(MusicPlugin, "_get_fade_kwargs", None)))


# ============ 十、CUSTOM-5.1：人格化通知 + 文案解耦 ============
src_main = open(PLUGINS + "/astrbot_plugin_cucumber_rvc_console/main.py", encoding="utf-8").read()
check("通知带人格解析", "persona_manager.resolve_selected_persona" in src_main)
check("通知传 system_prompt", "system_prompt=system_prompt or None" in src_main)
check("模板文案不再由 LLM 结果决定", "llm_said" not in src_main)
check("模板开关独立控制", "if not is_failed and self.enable_config_report:" in src_main)

class _Provider:
    def __init__(self):
        self.kw = None
    async def text_chat(self, **kw):
        self.kw = kw
        return SimpleNamespace(completion_text="……唱好了。")

class _ConvMgr:
    async def get_curr_conversation_id(self, umo):
        return "c1"
    async def get_conversation(self, umo, cid):
        return SimpleNamespace(persona_id="p1",
                               message=[{"role": "user", "content": "翻唱龙卷风"}])

class _PersonaMgr:
    async def resolve_selected_persona(self, **kw):
        return ("p1", {"system_prompt": "你是睦，说话简短克制，常用省略号。"}, None, False)

async def _test_notify():
    prov = _Provider()
    sent = []
    async def _send(msg):
        sent.append(str(msg))
    ev = SimpleNamespace(unified_msg_origin="q:1", get_platform_name=lambda: "qq", send=_send, plain_result=lambda t: t)
    async def _inject(event, text, role="assistant"):
        return None

    s = SimpleNamespace(
        enable_llm_success_notify=True,
        context=SimpleNamespace(
            get_using_provider=lambda umo: prov,
            conversation_manager=_ConvMgr(),
            persona_manager=_PersonaMgr(),
        ),
    )
    s._inject_message_to_context = _inject
    s._find_persona = types.MethodType(MusicPlugin._find_persona, s)
    s._is_generic_persona = types.MethodType(MusicPlugin._is_generic_persona, s)
    s._notify_llm_with_context = types.MethodType(MusicPlugin._notify_llm_with_context, s)
    reply = await s._notify_llm_with_context(ev, "龙卷风", "success")
    check("通知：把人格 system_prompt 传给 LLM",
          prov.kw and "睦" in (prov.kw.get("system_prompt") or ""), str(prov.kw and prov.kw.get("system_prompt"))[:40])
    check("通知：带入最近上下文", isinstance(prov.kw.get("contexts"), list) and len(prov.kw["contexts"]) == 1)
    check("通知：提示词要求简短且不堆语气词",
          "不超过 20 字" in prov.kw.get("prompt", "") and "耶/啦/咯" in prov.kw.get("prompt", ""))
    check("通知：回复已发送", reply == "……唱好了。" and len(sent) >= 1)

asyncio.run(_test_notify())

print("=" * 44)
print("TOTAL %d/%d PASS" % (sum(results), len(results)))
sys.exit(0 if all(results) else 1)
