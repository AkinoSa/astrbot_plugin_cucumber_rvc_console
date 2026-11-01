# -*- coding: utf-8 -*-
"""CUSTOM-7：配置一致性静态校验（不 import astrbot，本地与 NAS 都能跑）。

校验三件事：
1. _conf_schema.json 是合法 JSON；
2. main.CONFIG_PATHS 的每个扁平键都指向 schema 里真实存在的嵌套路径；
3. schema 的每个叶子（除 model_genders 内部键）都有扁平键映射，
   避免「页面显示但代码永远读不到」的僵尸配置项（CUSTOM-6.10 的教训）。

    python tests/test_config_sync.py
"""

from __future__ import annotations

import ast
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN_DIR = os.path.dirname(HERE)

GENDER_STEMS = ("deng", "nuoyemu", "rym")


def load_config_paths() -> dict[str, str]:
    src = open(os.path.join(PLUGIN_DIR, "main.py"), encoding="utf-8").read()
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and getattr(node.targets[0], "id", "") == "CONFIG_PATHS":
            return {
                k.value: ".".join(e.value for e in v.elts)
                for k, v in zip(node.value.keys, node.value.values)
            }
    raise AssertionError("main.py 里没找到 CONFIG_PATHS")


def all_node_paths(schema: dict, prefix: str = "") -> set[str]:
    """所有节点路径（object 卡片本身也算），供 CONFIG_PATHS 目标校验用。"""
    out: set[str] = set()
    for key, meta in schema.items():
        if not isinstance(meta, dict):
            continue
        path = f"{prefix}{key}"
        out.add(path)
        if meta.get("type") == "object" and isinstance(meta.get("items"), dict):
            out |= all_node_paths(meta["items"], path + ".")
    return out


def leaf_paths(schema: dict, prefix: str = "") -> set[str]:
    out: set[str] = set()
    for key, meta in schema.items():
        if not isinstance(meta, dict):
            continue
        path = f"{prefix}{key}"
        if meta.get("type") == "object" and isinstance(meta.get("items"), dict):
            out |= leaf_paths(meta["items"], path + ".")
        else:
            out.add(path)
    return out


def main() -> int:
    schema = json.load(open(os.path.join(PLUGIN_DIR, "_conf_schema.json"), encoding="utf-8"))
    paths = load_config_paths()
    schema_keys = leaf_paths(schema)
    node_paths = all_node_paths(schema)

    bad_targets = [
        (flat, dotted) for flat, dotted in paths.items()
        if dotted not in node_paths
    ]
    assert not bad_targets, f"CONFIG_PATHS 指向了 schema 不存在的路径: {bad_targets}"

    flat_names = set(paths)
    for key in schema_keys:
        stem = key.split(".")[-1]
        if key.startswith("tuning.gender.model_genders.") and stem in GENDER_STEMS:
            continue
        if key == "tuning.gender.model_genders_extra" or key == "tuning.gender.model_gender_map":
            continue
        assert stem in flat_names, f"schema 叶子 {key} 没有对应的扁平键映射"

    # TTS 与歌曲翻唱的关键隔离键必须各有一份
    for key in ("rvc_default_model", "tts_rvc_model", "index_rate", "tts_index_rate",
                "reverb_enabled", "tts_reverb_enabled", "enable_rvc", "tts_enable"):
        assert key in paths, f"缺少关键配置映射: {key}"

    print(f"test_config_sync: all passed（{len(paths)} 个扁平键与 schema 完全对应）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
