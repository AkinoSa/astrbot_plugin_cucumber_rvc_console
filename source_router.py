"""多音源取歌路由（0.8.0：按用户定的确定规则选歌）。

按配置顺序逐个平台执行同一套流程：

    搜歌（song_name 原文整串）→ 确定规则匹配（标题 / 标题+别名）→ 排除伴奏类
    → 取播放链接 → 下载 → 80 秒闸门

前一个平台任何一步没成就自动换下一个，每个平台在日志里留一行结果。

选歌规则（0.8.0，不再打分、不再猜版本）
======================================

1. **整串当歌名**：``song_name`` 原样去搜、原样比，不做任何拆分或改写。
2. **歌手只在明确给出时生效**：``artist`` 非空才按歌手过滤。若没有任何候选的歌手包含它，
   写一行 ``artist_filter_ignored``，改把该词当版本别名用（只留别名里含这个词的候选）；
   一个都没有就返回「没有能确认的版本」，绝不拿同名翻唱顶替。
3. **别名只在明确出现时参与**：用户说的版本词（司凤版等）会体现在 ``song_name`` 整串里，
   因此「标题 + 别名(alias/alia/tns)」拼起来正好等于歌名的那一版才算命中。
4. **匹配只有两档**：
   - 完全相等：``标题 == song_name``，或 ``标题+别名 == song_name``；
   - 包含：``标题+别名`` 包含 ``song_name``，且扣掉歌名后的剩余字符必须全部来自别名
     （任何解释不了的剩余字符都当不匹配，例如「晴天 (原唱 周杰伦)」「【ROZA】失眠飞行」）。
5. **同档取舍**：先剔除时长不到同档最长候选 90% 的（视为片段/剪辑），再按搜索顺序取第一条。
   这是唯一的同档取舍，不参与匹配判定。

音源实现
========

- ``netease``：本插件自己的 :class:`netease_fetch.NeteaseFetcher`（0.5.0 起照搬点歌插件
  那套：搜歌 + meting 主路 + NodeJS 备用路 + 80 秒闸门）。原来的单网易云逻辑就是
  路由器里的第一个源。
- ``kugou`` / ``kuwo`` / ``qqmusic``：按文件路径动态 import 点歌插件
  ``astrbot_plugin_music`` 的现成类（``core/platform/*.py``），不复制它那几百行代码。
  这些平台自己的 cookie / uin 从点歌插件的配置文件
  ``data/config/astrbot_plugin_music_config.json`` 里读；读不到、或者平台必须要的配置
  为空（例如 QQ 音乐的 uin）就跳过该平台并记一行日志，绝不抛异常中断整条链路。
- 酷狗有一个兜底：点歌插件的 ``gateway.kugou.com/v5/url`` 现在经常回
  「本次请求需要验证」，这时改用酷狗移动端老接口
  ``m.kugou.com/app/i/getSongInfo.php?cmd=playInfo&hash=…`` 取直链（同样是先搜后取）。

别名匹配
========

候选的匹配文本是「标题 + 别名」：

- 网易云搜索结果自带 ``alias``；没有时再用 ``/api/song/detail`` 的 ``alia`` / ``tns`` 补；
- 酷狗 / 酷我 / QQ 音乐的搜索结果没有别名字段，别名部分为空（靠标题匹配）。

别名**不单独当候选**：0.8.0 起只有「标题+别名整段等于歌名」或「剩余字符全部来自别名」
才算命中；用户没说版本，就不会因为别名里有版本词而乱挑版本。

伴奏排除
========

候选的标题或别名里出现 伴奏 / 纯享 / 纯音乐 / 消音 / 无人声 / off vocal /
instrumental / inst / karaoke / dj版 的，直接淘汰——这些版本没有人声，翻唱只会出静音。
所有音源的候选都被淘汰时对外返回「只有伴奏版」。
"""

from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import json
import re
import sys
import types
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from astrbot.api import logger

from . import netease_fetch


# ---------------------------------------------------------------------------
# 平台与原因常量
# ---------------------------------------------------------------------------
#: 默认顺序：网易云 → 酷狗 → 酷我 → QQ 音乐
DEFAULT_SOURCE_ORDER = ("netease", "kugou", "kuwo", "qqmusic")

SOURCE_DISPLAY = {
    "netease": "网易云音乐",
    "kugou": "酷狗音乐",
    "kuwo": "酷我音乐",
    "qqmusic": "QQ音乐",
}

#: 平台标识的常见写法（配置里写中文或别名都能认）
_SOURCE_ALIASES = {
    "netease": "netease",
    "网易云": "netease",
    "网易云音乐": "netease",
    "163": "netease",
    "kugou": "kugou",
    "酷狗": "kugou",
    "酷狗音乐": "kugou",
    "kuwo": "kuwo",
    "酷我": "kuwo",
    "酷我音乐": "kuwo",
    "qqmusic": "qqmusic",
    "qq": "qqmusic",
    "qq音乐": "qqmusic",
}

#: 点歌插件里的类名
_REFERENCE_CLASS_NAMES = {
    "kugou": "KuGouMusic",
    "kuwo": "KuwoMusic",
    "qqmusic": "QQMusic",
}

REASON_ONLY_ACCOMPANIMENT = "只有伴奏版"
REASON_NO_RESULT = "搜不到"
REASON_NO_MATCH = "没有能确认的版本"
REASON_NO_URL = "没拿到播放链接"
REASON_DOWNLOAD = "下载失败"

#: 时长闸门沿用 netease_fetch 里的 80 秒
MIN_FULL_LENGTH_MS = netease_fetch.MIN_FULL_LENGTH_MS


# ---------------------------------------------------------------------------
# 文本归一化 / 伴奏判定
# ---------------------------------------------------------------------------
_NON_WORD_RE = re.compile(r"[\W_]+", re.UNICODE)
_COMPACT_ACCOMPANIMENT_WORDS = (
    "伴奏",
    "纯享",
    "纯音乐",
    "消音",
    "无人声",
    "offvocal",
    "instrumental",
    "karaoke",
    "dj版",
)


def normalize_text(value: Any) -> str:
    """去掉标点与空白、转小写，得到用于比较的紧凑文本。"""
    return _NON_WORD_RE.sub("", str(value or "").casefold())


def is_accompaniment(*texts: Any) -> bool:
    """标题/别名里是否带「伴奏类」标记。

    ``off vocal`` 这种带空格的写法先去空格再比；``inst`` 只按完整单词认，
    避免把 "instrumental" 以外的普通单词误伤。
    """
    joined = "".join(str(text or "") for text in texts)
    compact = re.sub(r"\s+", "", joined.casefold())
    if any(word in compact for word in _COMPACT_ACCOMPANIMENT_WORDS):
        return True
    return bool(re.search(r"(?<![a-z])inst(?![a-z])", joined.casefold()))


def _alias_text(value: Any) -> str:
    """把别名（字符串 / 列表）拼成一段可比较文本。"""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (list, tuple)):
        return " / ".join(str(item).strip() for item in value if str(item).strip())
    return ""


# ---------------------------------------------------------------------------
# 候选与匹配
# ---------------------------------------------------------------------------
@dataclass
class SongCandidate:
    """一个平台给出的一首候选（已把别名和歌手摊平）。"""

    id: str
    name: str
    artists: str = ""
    duration_ms: int = 0
    alias: str = ""
    source: str = ""
    page_url: str = ""
    raw: Any = None

    def as_dict(self) -> dict:
        """转成插件内部通用的歌曲 dict（带来源，供取歌时直接命中同一个源）。"""
        return {
            "id": self.id,
            "name": self.name,
            "artists": self.artists,
            "duration": self.duration_ms,
            "alias": self.alias,
            "source": self.source,
            "page_url": self.page_url,
        }


@dataclass
class MatchStats:
    total: int = 0
    excluded: int = 0
    matched: int = 0


def candidate_from_dict(song: dict, source: str = "") -> Optional[SongCandidate]:
    """把 ``{id,name,artists,duration,alias}`` 形状的 dict 变成候选。"""
    if not isinstance(song, dict):
        return None
    song_id = str(song.get("id") or song.get("songmid") or "").strip()
    name = str(song.get("name") or song.get("title") or "").strip()
    if not song_id or not name:
        return None
    alias = _alias_text(
        song.get("alias") or song.get("alia") or song.get("tns") or ""
    )
    try:
        duration = int(
            song.get("duration") or song.get("duration_ms") or 0
        )
    except (TypeError, ValueError):
        duration = 0
    return SongCandidate(
        id=song_id,
        name=name,
        artists=str(song.get("artists") or song.get("artist") or "").strip(),
        duration_ms=duration,
        alias=alias,
        source=str(song.get("source") or source or "").strip(),
        page_url=str(song.get("page_url") or "").strip(),
        raw=song,
    )


def _candidate_from_song(song: Any, source: str) -> Optional[SongCandidate]:
    """点歌插件 ``Song`` 对象 → 候选。"""
    song_id = str(getattr(song, "id", "") or "").strip()
    name = str(getattr(song, "name", "") or "").strip()
    if not song_id or not name:
        return None
    try:
        duration = int(getattr(song, "duration", 0) or 0)
    except (TypeError, ValueError):
        duration = 0
    return SongCandidate(
        id=song_id,
        name=name,
        artists=str(getattr(song, "artists", "") or "").strip(),
        duration_ms=duration,
        alias="",
        source=source,
        page_url=str(getattr(song, "page_url", "") or "").strip(),
        raw=song,
    )


def _match_tier(candidate: SongCandidate, normalized_query: str) -> int:
    """候选与歌名的匹配档位：2=完全相等，1=可解释的包含，0=不匹配。

    「完全相等」指归一化后 ``标题 == 歌名``，或 ``标题+别名 == 歌名``；
    「包含」只接受 ``标题+别名`` 里的歌名以外的剩余字符全部来自别名，
    任何解释不了的剩余字符都算不匹配（例如标题里的 (Live)、【ROZA】、原唱 xxx）。
    """
    title = normalize_text(candidate.name)
    alias = normalize_text(candidate.alias)
    combined = title + alias
    if title == normalized_query or combined == normalized_query:
        return 2
    if normalized_query and normalized_query in combined:
        leftover = combined.replace(normalized_query, "", 1)
        if not leftover or leftover in alias:
            return 1
    return 0


def pick_best_song(
    candidates: list[SongCandidate], query: str, artist: str = ""
) -> tuple[Optional[SongCandidate], str, MatchStats]:
    """按用户定的确定规则挑歌；一个都不像就返回空。

    规则（宁可不选，也不选错）：

    1. 标题或别名里带伴奏类标记的，直接淘汰（翻唱会出静音）；
    2. 给了 ``artist`` 才按歌手过滤；没有任何候选的歌手包含它时，记一行
       ``artist_filter_ignored``，改把它当版本别名（只留别名里含这个词的候选），
       一个都没有就判「没有能确认的版本」；
    3. 匹配只分两档（见 :func:`_match_tier`）：完全相等优先，其次是剩余字符全部来自别名的包含；
    4. 同档取舍：先剔除时长不到同档最长候选 90% 的（片段/剪辑），再按搜索顺序取第一条。

    不再有标题长度打分、歌手加分、标题互相包含之类的模糊规则。

    Returns:
        ``(候选 或 None, 状态, 统计)``。状态取 ``ok`` / ``empty`` / ``no_match`` /
        ``only_accompaniment``；统计里记总候选数、被伴奏规则淘汰数、正选数。
    """
    stats = MatchStats(total=len(candidates))
    usable = [c for c in candidates if isinstance(c, SongCandidate)]
    if not usable:
        return None, "empty", stats

    normalized_query = normalize_text(query)
    normalized_artist = normalize_text(artist)
    if not normalized_query:
        first = usable[0]
        stats.matched = 1
        return first, "ok", stats

    # 1) 先排除伴奏类：这些版本没有人声，选中也只会出静音
    playable: list[SongCandidate] = []
    for candidate in usable:
        if is_accompaniment(candidate.name, candidate.alias):
            stats.excluded += 1
            continue
        playable.append(candidate)
    if not playable:
        return None, "only_accompaniment", stats

    # 2) 歌手只在明确给了的情况下过滤；没人命中就改当版本别名用，不因它判失败
    if normalized_artist:
        by_singer = [
            candidate
            for candidate in playable
            if normalized_artist in normalize_text(candidate.artists)
        ]
        if by_singer:
            playable = by_singer
        else:
            logger.info(
                f"artist_filter_ignored | song={query} | artist={artist} | "
                "没有任何候选的歌手包含它，改按别名里的版本词匹配"
            )
            by_alias = [
                candidate
                for candidate in playable
                if normalized_artist in normalize_text(candidate.alias)
            ]
            if not by_alias:
                stats.matched = 0
                return None, "no_match", stats
            playable = by_alias

    # 3) 分档：完全相等 > 可解释的包含
    matches: list[tuple[int, int, SongCandidate]] = []
    for rank, candidate in enumerate(playable):
        tier = _match_tier(candidate, normalized_query)
        if tier:
            matches.append((tier, rank, candidate))
    if not matches:
        status = "only_accompaniment" if stats.excluded else "no_match"
        return None, status, stats

    best_tier = max(item[0] for item in matches)
    top = [item for item in matches if item[0] == best_tier]

    # 4) 同档取舍：剔除明显是片段的（不足同档最长 90%），再按搜索顺序
    longest = max(item[2].duration_ms for item in top)
    if longest > 0:
        kept = [
            item
            for item in top
            if item[2].duration_ms <= 0 or item[2].duration_ms >= longest * 0.9
        ]
        if kept:
            top = kept
    top.sort(key=lambda item: item[1])
    stats.matched = 1
    return top[0][2], "ok", stats


def pick_best_song_dict(
    songs: list, query: str, artist: str = ""
) -> dict:
    """``songs`` 是 dict 列表时直接返回选中的 dict（没选中返回 ``{}``）。"""
    candidates = [
        candidate
        for candidate in (
            candidate_from_dict(song) for song in (songs or []) if isinstance(song, dict)
        )
        if candidate is not None
    ]
    picked, _status, _stats = pick_best_song(candidates, query, artist)
    return dict(picked.raw) if picked and isinstance(picked.raw, dict) else {}


# ---------------------------------------------------------------------------
# 配置顺序
# ---------------------------------------------------------------------------
def normalize_source_order(value: Any) -> tuple[str, ...]:
    """把配置里的顺序收拾成去重后的平台标识元组。

    支持 ``["netease", "kugou"]`` 或 ``"netease,kugou"``；中文/别名会映射成标准名；
    不认识的项忽略并记日志；空值退回默认顺序。
    """
    raw_items: list[Any] = []
    if isinstance(value, str):
        raw_items = re.split(r"[,，\s]+", value)
    elif isinstance(value, (list, tuple)):
        for item in value:
            if isinstance(item, str):
                raw_items.extend(re.split(r"[,，\s]+", item))
            else:
                raw_items.append(item)

    order: list[str] = []
    for item in raw_items:
        key = str(item or "").strip().casefold()
        if not key:
            continue
        source = _SOURCE_ALIASES.get(key)
        if not source:
            logger.warning(f"[音源路由] 不认识的音源 {item!r}，已忽略")
            continue
        if source not in order:
            order.append(source)
    return tuple(order) or DEFAULT_SOURCE_ORDER


def describe_order(order: tuple[str, ...]) -> str:
    """给日志/报错用的一行顺序说明。"""
    return " → ".join(SOURCE_DISPLAY.get(name, name) for name in order)


# ---------------------------------------------------------------------------
# 点歌插件平台类的动态加载
# ---------------------------------------------------------------------------
_CLASS_CACHE: dict[tuple[str, str], type] = {}


def _load_module(name: str, file: Path) -> types.ModuleType:
    existing = sys.modules.get(name)
    if existing is not None and getattr(existing, "__file__", None) == str(file):
        return existing
    spec = importlib.util.spec_from_file_location(name, file)
    if spec is None or spec.loader is None:
        raise ImportError(f"无法加载模块: {file}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(name, None)
        raise
    return module


def load_reference_class(source: str, plugin_dir: str | Path) -> type:
    """按文件路径加载点歌插件里的平台类（用合成包名让相对 import 正常工作）。"""
    class_name = _REFERENCE_CLASS_NAMES.get(source)
    if not class_name:
        raise ImportError(f"没有对应的点歌插件平台类: {source}")
    root = Path(plugin_dir).expanduser().resolve()
    cache_key = (str(root), source)
    if cache_key in _CLASS_CACHE:
        return _CLASS_CACHE[cache_key]

    platform_dir = root / "core" / "platform"
    if not platform_dir.is_dir():
        raise ImportError(f"点歌插件目录不存在: {root}")
    package = f"_chorus_cover_music_{hashlib.md5(str(root).encode()).hexdigest()[:8]}"
    packages = (
        (package, root),
        (f"{package}.core", root / "core"),
        (f"{package}.core.platform", platform_dir),
    )
    for name, path in packages:
        if name in sys.modules:
            continue
        module = types.ModuleType(name)
        module.__path__ = [str(path)]
        module.__package__ = name
        sys.modules[name] = module

    modules = (
        (f"{package}.core.model", root / "core" / "model.py"),
        (f"{package}.core.config", root / "core" / "config.py"),
        (f"{package}.core.platform.base", platform_dir / "base.py"),
        (f"{package}.core.platform.{source}", platform_dir / f"{source}.py"),
    )
    for name, path in modules:
        if name not in sys.modules:
            _load_module(name, path)

    module = sys.modules.get(f"{package}.core.platform.{source}")
    player_class = getattr(module, class_name, None) if module else None
    if not isinstance(player_class, type):
        raise ImportError(f"点歌插件 {source}.py 里没有类 {class_name}")
    _CLASS_CACHE[cache_key] = player_class
    return player_class


class _PlatformSettings:
    """点歌插件一个平台的配置（只取取歌需要的字段）。"""

    def __init__(self, data: Optional[dict] = None, default_quality: str = "standard"):
        data = data or {}
        self.cookies = str(data.get("cookies") or "")
        self.proxy = str(data.get("proxy") or "")
        self.uin = str(data.get("uin") or "")
        self.base_url = str(data.get("base_url") or "")
        self.api_token = str(data.get("api_token") or "")
        self.quality = str(data.get("quality") or default_quality)
        self.enable = bool(data.get("enable", True))


class _PlayersConfig:
    def __init__(self, settings: dict[str, _PlatformSettings]):
        for name, item in settings.items():
            setattr(self, name, item)


class _ShimConfig:
    """点歌插件平台类需要的 ``cfg`` 形状（temp_dir + players.<name>）。"""

    def __init__(self, temp_dir: str | Path, settings: dict[str, _PlatformSettings]):
        self.temp_dir = Path(temp_dir)
        self.players = _PlayersConfig(settings)


def load_music_plugin_players(path: str | Path) -> dict[str, dict]:
    """读点歌插件配置文件里的 ``players_template``（读不到返回空 dict）。"""
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except Exception as exc:
        logger.info(f"[音源路由] 读不到点歌插件配置（{path}）：{exc}；备用音源按无配置处理")
        return {}
    items = raw.get("players_template") if isinstance(raw, dict) else None
    players: dict[str, dict] = {}
    for item in items or []:
        if not isinstance(item, dict):
            continue
        key = str(item.get("__template_key") or "").strip()
        if key:
            players[key] = item
    return players


# ---------------------------------------------------------------------------
# 路由结果
# ---------------------------------------------------------------------------
@dataclass
class SourceAttempt:
    source: str
    display: str
    ok: bool = False
    status: str = ""
    reason: str = ""
    is_trial: bool = False
    had_candidate: bool = False
    candidate: Optional[SongCandidate] = None
    path: str = ""


@dataclass
class RouterResult:
    path: Optional[str] = None
    source: str = ""
    candidate: Optional[SongCandidate] = None
    reason: str = ""
    attempts: list[SourceAttempt] = field(default_factory=list)
    is_trial: bool = False


class _SkipSource(Exception):
    """这个平台没有可用配置，直接跳过（只记一行日志，不当失败堆栈）。"""


# ---------------------------------------------------------------------------
# 路由器
# ---------------------------------------------------------------------------
class SourceRouter:
    """按顺序尝试多个音源，把一首歌落到本地。

    Args:
        source_order: 平台顺序（``source_order`` 配置）；空则用默认顺序。
        netease_fetcher: 已经构造好的 :class:`netease_fetch.NeteaseFetcher`。
        dest_dir: 下载目录（插件自己的 ``temp_audio/``）。
        music_plugin_dir: 点歌插件目录（默认取本插件的同级 ``astrbot_plugin_music``）。
        music_config_path: 点歌插件配置文件（默认取 ``data/config/astrbot_plugin_music_config.json``）。
        timeout: 单次搜歌 / 取链接的超时（秒）。
        download_timeout: 单次下载的超时（秒）。
        limit: 每个平台搜多少条候选。
    """

    def __init__(
        self,
        *,
        source_order: Any = None,
        netease_fetcher: netease_fetch.NeteaseFetcher,
        dest_dir: str | Path,
        music_plugin_dir: str | Path | None = None,
        music_config_path: str | Path | None = None,
        timeout: float = 20.0,
        download_timeout: float = 120.0,
        limit: int = 10,
        quality_br: int = 320000,
    ):
        self.order = normalize_source_order(source_order)
        self.netease_fetcher = netease_fetcher
        self.dest_dir = Path(dest_dir)
        self.music_plugin_dir = Path(
            music_plugin_dir
            or Path(__file__).resolve().parent.parent / "astrbot_plugin_music"
        )
        if music_config_path is None:
            music_config_path = (
                Path(__file__).resolve().parent.parent.parent
                / "config"
                / "astrbot_plugin_music_config.json"
            )
        self.music_config_path = Path(music_config_path)
        try:
            self.timeout = max(5.0, float(timeout))
        except (TypeError, ValueError):
            self.timeout = 20.0
        try:
            self.download_timeout = max(30.0, float(download_timeout))
        except (TypeError, ValueError):
            self.download_timeout = 120.0
        try:
            self.limit = max(1, int(limit))
        except (TypeError, ValueError):
            self.limit = 10
        try:
            self.quality_br = max(64000, int(quality_br or 320000))
        except (TypeError, ValueError):
            self.quality_br = 320000

        self._settings: dict[str, _PlatformSettings] = {}
        self._platforms: dict[str, Any] = {}
        self._platform_errors: dict[str, str] = {}
        self._loaded_settings = load_music_plugin_players(self.music_config_path)
        logger.info(
            f"[音源路由] 就绪：{describe_order(self.order)}"
            f"（点歌插件目录：{self.music_plugin_dir}）"
        )

    # ---------------- 对外入口 ----------------

    async def close(self) -> None:
        """关掉备用音源平台自己开的 HTTP session。"""
        for platform in self._platforms.values():
            try:
                await platform.close()
            except Exception as exc:
                logger.debug(f"[音源路由] 关平台 session 失败: {exc}")
        self._platforms.clear()

    async def search_candidates(
        self, keyword: str, artist: str = "", limit: Optional[int] = None
    ) -> tuple[list[SongCandidate], str]:
        """按配置顺序搜歌，返回**第一个有结果**的平台给出的候选。

        菜单展示用；真正的选歌仍然走 :func:`pick_best_song`。
        """
        keyword = str(keyword or "").strip()
        if not keyword:
            return [], "没有歌名"
        size = max(1, int(limit or self.limit))
        reasons: list[str] = []
        for source in self.order:
            try:
                candidates = await self._search_source(source, keyword, size)
            except _SkipSource as exc:
                logger.info(f"[音源路由] {SOURCE_DISPLAY[source]}：跳过（{exc}）")
                reasons.append(f"{SOURCE_DISPLAY[source]}：{exc}")
                continue
            except Exception as exc:
                logger.info(
                    f"[音源路由] {SOURCE_DISPLAY[source]}：搜歌失败（{type(exc).__name__}: {exc}）"
                )
                reasons.append(f"{SOURCE_DISPLAY[source]}：搜歌失败")
                continue
            if candidates:
                logger.info(
                    f"[音源路由] {SOURCE_DISPLAY[source]}：搜到 {len(candidates)} 条候选"
                )
                return candidates, ""
            logger.info(f"[音源路由] {SOURCE_DISPLAY[source]}：搜不到")
            reasons.append(f"{SOURCE_DISPLAY[source]}：搜不到")
        return [], "；".join(reasons) or "所有音源都没搜到"

    async def search_candidates_by_source(
        self, keyword: str, limit: Optional[int] = None
    ) -> list[tuple[str, list["SongCandidate"]]]:
        """按顺序搜每个平台，返回 [(平台, 候选列表), ...]，只含有结果的平台。

        供「自动选歌逐源尝试」用：第一个源挑不出原唱时，还能去下一个源找。
        """
        keyword = str(keyword or "").strip()
        if not keyword:
            return []
        size = max(1, int(limit or self.limit))
        results: list[tuple[str, list[SongCandidate]]] = []
        for source in self.order:
            try:
                candidates = await self._search_source(source, keyword, size)
            except _SkipSource as exc:
                logger.info(f"[音源路由] {SOURCE_DISPLAY[source]}：跳过（{exc}）")
                continue
            except Exception as exc:
                logger.info(
                    f"[音源路由] {SOURCE_DISPLAY[source]}：搜歌失败（{type(exc).__name__}: {exc}）"
                )
                continue
            if candidates:
                logger.info(
                    f"[音源路由] {SOURCE_DISPLAY[source]}：搜到 {len(candidates)} 条候选"
                )
                results.append((source, candidates))
            else:
                logger.info(f"[音源路由] {SOURCE_DISPLAY[source]}：搜不到")
        return results

    async def find_song(
        self, keyword: str, artist: str = "", limit: Optional[int] = None
    ) -> tuple[dict, str, list[SourceAttempt]]:
        """按顺序搜歌 + 严格匹配，返回第一个能确认的歌曲 dict。

        Returns:
            ``(歌曲 dict 或 {}, 失败原因, 每个平台的尝试记录)``。
        """
        keyword = str(keyword or "").strip()
        if not keyword:
            return {}, "没有歌名", []
        size = max(1, int(limit or self.limit))
        artist_hint = self._artist_hint(artist)
        attempts: list[SourceAttempt] = []
        for source in self.order:
            display = SOURCE_DISPLAY[source]
            attempt = SourceAttempt(source=source, display=display)
            try:
                candidates = await self._search_source(source, keyword, size, artist_hint)
                if not candidates:
                    attempt.status = "no_result"
                    attempt.reason = "搜不到"
                    logger.info(f"[音源路由] {display}：搜不到")
                    attempts.append(attempt)
                    continue
                picked, status, stats = pick_best_song(candidates, keyword, artist_hint)
                if not picked and source == "netease":
                    enriched = await self._enrich_netease_aliases(candidates)
                    if enriched:
                        picked, status, stats = pick_best_song(
                            candidates, keyword, artist_hint
                        )
                if not picked:
                    attempt.status = status
                    attempt.reason = (
                        REASON_ONLY_ACCOMPANIMENT
                        if status == "only_accompaniment"
                        else REASON_NO_MATCH
                    )
                    logger.info(
                        f"[音源路由] {display}：{attempt.reason}"
                        f"（候选 {stats.total} 条，伴奏淘汰 {stats.excluded} 条）"
                    )
                    attempts.append(attempt)
                    continue
                attempt.ok = True
                attempt.candidate = picked
                attempt.had_candidate = True
                logger.info(
                    f"[音源路由] {display}：命中 id={picked.id}"
                    f"《{picked.name}》- {picked.artists or '未知歌手'}"
                )
                attempts.append(attempt)
                return picked.as_dict(), "", attempts
            except _SkipSource as exc:
                attempt.status = "skipped"
                attempt.reason = str(exc)
                logger.info(f"[音源路由] {display}：跳过（{exc}）")
                attempts.append(attempt)
            except Exception as exc:
                attempt.status = "error"
                attempt.reason = f"{type(exc).__name__}: {exc}"
                logger.info(f"[音源路由] {display}：搜索出错（{attempt.reason}）")
                attempts.append(attempt)
        return {}, self._compose_reason(attempts), attempts

    async def fetch_audio(
        self, song: Any, dest_dir: Optional[str | Path] = None
    ) -> RouterResult:
        """把一首歌落到本地：按顺序试每个平台，第一个成功就返回。

        Args:
            song: 歌曲 dict（``id`` / ``name`` / ``artists`` / ``source``）或纯歌名字符串。
            dest_dir: 下载目录；默认用构造时给的 ``dest_dir``。

        Returns:
            :class:`RouterResult`。``path`` 为空时 ``reason`` 里是每个平台的失败原因。
        """
        request = self._normalize_request(song)
        destination = Path(dest_dir or self.dest_dir)
        try:
            destination.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            logger.error(f"[音源路由] 建下载目录失败: {destination}（{exc}）")
            return RouterResult(reason=REASON_DOWNLOAD)

        attempts: list[SourceAttempt] = []
        order = list(self.order)
        preferred = request["source"]
        if preferred in order:
            order = [preferred] + [name for name in order if name != preferred]
        total = len(order)
        for index, source in enumerate(order, 1):
            attempt = await self._attempt(source, request, str(destination))
            attempts.append(attempt)
            prefix = f"[音源路由] {index}/{total} {attempt.display}："
            if attempt.ok and attempt.candidate is not None:
                candidate = attempt.candidate
                logger.info(
                    prefix
                    + f"成功 id={candidate.id}《{candidate.name}》- "
                    f"{candidate.artists or '未知歌手'}"
                    f"（{candidate.duration_ms / 1000:.1f} 秒）"
                )
                return RouterResult(
                    path=attempt.path,
                    source=source,
                    candidate=candidate,
                    attempts=attempts,
                    is_trial=attempt.is_trial,
                )
            logger.info(prefix + f"失败（{attempt.reason or attempt.status}）")
        return RouterResult(
            reason=self._compose_reason(attempts),
            attempts=attempts,
            is_trial=any(attempt.is_trial for attempt in attempts),
        )

    # ---------------- 单平台尝试 ----------------

    async def _attempt(
        self, source: str, request: dict, destination: str
    ) -> SourceAttempt:
        display = SOURCE_DISPLAY.get(source, source)
        try:
            if source == "netease":
                return await self._attempt_netease(request, destination)
            return await self._attempt_reference(source, request, destination)
        except _SkipSource as exc:
            return SourceAttempt(
                source=source, display=display, status="skipped", reason=str(exc)
            )
        except Exception as exc:
            logger.warning(
                f"[音源路由] {display} 尝试时出错: {type(exc).__name__}: {exc}"
            )
            return SourceAttempt(
                source=source,
                display=display,
                status="error",
                reason=f"出错（{type(exc).__name__}）",
            )

    async def _attempt_netease(
        self, request: dict, destination: str
    ) -> SourceAttempt:
        display = SOURCE_DISPLAY["netease"]
        attempt = SourceAttempt(source="netease", display=display)
        raw_id = request["id"]
        artists = request["artists"]

        if raw_id.isdigit() and request["source"] in ("", "netease"):
            path, reason, is_trial = await self._netease_fetch(raw_id, destination)
            if path:
                return self._succeed(
                    attempt, SongCandidate(
                        id=raw_id,
                        name=request["name"] or raw_id,
                        artists=artists,
                        source="netease",
                    ), path,
                )
            attempt.is_trial = is_trial
            attempt.reason = reason or REASON_NO_URL
            return attempt

        artist_hint = self._artist_hint(artists)
        last_status = "no_result"
        last_reason = REASON_NO_RESULT
        last_stats = MatchStats()
        for keyword in self._search_keywords(request):
            candidates = await self._search_netease(keyword)
            if not candidates:
                continue
            picked, status, stats = pick_best_song(candidates, keyword, artist_hint)
            if not picked:
                enriched = await self._enrich_netease_aliases(candidates)
                if enriched:
                    picked, status, stats = pick_best_song(
                        candidates, keyword, artist_hint
                    )
            last_status, last_stats = status, stats
            last_reason = (
                REASON_ONLY_ACCOMPANIMENT
                if status == "only_accompaniment"
                else REASON_NO_MATCH
            )
            if not picked:
                continue
            path, reason, is_trial = await self._netease_fetch(picked.id, destination)
            if path:
                return self._succeed(attempt, picked, path)
            attempt.is_trial = is_trial
            attempt.had_candidate = True
            attempt.reason = reason or REASON_NO_URL
            logger.info(
                f"[音源路由] {display}：命中 id={picked.id}"
                f"《{picked.name}》- {picked.artists or '未知歌手'}，"
                f"但取歌失败（{attempt.reason}）"
            )
            return attempt

        attempt.status = last_status
        attempt.reason = last_reason
        if last_stats.excluded:
            logger.info(
                f"[音源路由] {display}：候选 {last_stats.total} 条，"
                f"伴奏淘汰 {last_stats.excluded} 条"
            )
        return attempt

    async def _attempt_reference(
        self, source: str, request: dict, destination: str
    ) -> SourceAttempt:
        display = SOURCE_DISPLAY[source]
        attempt = SourceAttempt(source=source, display=display)
        platform = self._get_platform(source)
        artist_hint = self._artist_hint(request["artists"])

        picked: Optional[SongCandidate] = None
        last_status = "no_result"
        last_reason = REASON_NO_RESULT
        last_stats = MatchStats()
        for keyword in self._search_keywords(request):
            songs = await asyncio.wait_for(
                platform.fetch_songs(keyword, self.limit), timeout=self.timeout
            )
            candidates = [
                candidate
                for candidate in (
                    _candidate_from_song(song, source) for song in (songs or [])
                )
                if candidate is not None
            ]
            if not candidates:
                continue
            picked, last_status, last_stats = pick_best_song(
                candidates, keyword, artist_hint
            )
            last_reason = (
                REASON_ONLY_ACCOMPANIMENT
                if last_status == "only_accompaniment"
                else REASON_NO_MATCH
            )
            if picked:
                break

        if picked is None:
            attempt.status = last_status
            attempt.reason = last_reason
            if last_stats.excluded:
                logger.info(
                    f"[音源路由] {display}：候选 {last_stats.total} 条，"
                    f"伴奏淘汰 {last_stats.excluded} 条"
                )
            return attempt

        # 已经能确认是这首了：后面取链接/下载失败只算这个平台没成，不算「只有伴奏」
        attempt.had_candidate = True

        # 1) 用点歌插件那套取直链 + 下载
        audio_url = ""
        song_obj = picked.raw
        try:
            await asyncio.wait_for(
                platform.fetch_extra(song_obj), timeout=self.timeout
            )
            audio_url = str(getattr(song_obj, "audio_url", "") or "").strip()
        except Exception as exc:
            logger.info(
                f"[音源路由] {display}：取链接出错（{type(exc).__name__}: {exc}）"
            )
        if audio_url:
            platform.cfg.temp_dir = Path(destination)
            try:
                path = await asyncio.wait_for(
                    platform.download_audio_for_file(song_obj),
                    timeout=self.download_timeout,
                )
            except Exception as exc:
                logger.info(
                    f"[音源路由] {display}：下载出错（{type(exc).__name__}: {exc}）"
                )
                path = None
            if path:
                return self._succeed(attempt, picked, str(path))

        # 2) 酷狗兜底：v5/url 经常要验证，改用移动端老接口拿直链
        if source == "kugou":
            legacy_url = await self._kugou_legacy_url(picked.id)
            if legacy_url:
                legacy_path, legacy_reason = await self._download_direct(
                    legacy_url, destination
                )
                if legacy_path:
                    logger.info(
                        f"[音源路由] {display}：v5/url 没给直链，"
                        "已用移动端接口兜底"
                    )
                    return self._succeed(attempt, picked, legacy_path)
                logger.info(
                    f"[音源路由] {display}：移动端接口兜底下载失败（{legacy_reason}）"
                )

        attempt.status = "no_url" if not audio_url else "download_failed"
        attempt.reason = REASON_NO_URL if not audio_url else REASON_DOWNLOAD
        return attempt

    # ---------------- 各平台搜歌 ----------------

    async def _search_source(
        self,
        source: str,
        keyword: str,
        limit: int,
        artist_hint: str = "",
    ) -> list[SongCandidate]:
        if source == "netease":
            return await self._search_netease(keyword, limit)
        platform = self._get_platform(source)
        songs = await asyncio.wait_for(
            platform.fetch_songs(keyword, limit), timeout=self.timeout
        )
        return [
            candidate
            for candidate in (
                _candidate_from_song(song, source) for song in (songs or [])
            )
            if candidate is not None
        ]

    async def _search_netease(
        self, keyword: str, limit: Optional[int] = None
    ) -> list[SongCandidate]:
        songs = await asyncio.wait_for(
            self.netease_fetcher.search_songs(keyword, limit or self.limit),
            timeout=self.timeout,
        )
        candidates: list[SongCandidate] = []
        for song in songs or []:
            candidate = candidate_from_dict(song, "netease")
            if candidate is not None:
                candidates.append(candidate)
        return candidates

    async def _enrich_netease_aliases(
        self, candidates: list[SongCandidate]
    ) -> bool:
        """搜索没带 alias 时，用歌曲详情（alia / tns）补一把；补到返回 True。"""
        fetch_alias = getattr(self.netease_fetcher, "fetch_song_alias", None)
        missing = [candidate for candidate in candidates if not candidate.alias]
        if not callable(fetch_alias) or not missing:
            return False
        missing = missing[: self.limit]
        logger.info(f"[音源路由] 网易云：{len(missing)} 条候选没有别名，查详情补别名")
        results = await asyncio.gather(
            *(fetch_alias(candidate.id) for candidate in missing),
            return_exceptions=True,
        )
        enriched = False
        for candidate, result in zip(missing, results):
            if isinstance(result, Exception) or not result:
                continue
            candidate.alias = str(result)
            enriched = True
        return enriched

    # ---------------- 平台实例 ----------------

    def _get_platform(self, source: str) -> Any:
        if source in self._platforms:
            return self._platforms[source]
        if source in self._platform_errors:
            raise _SkipSource(self._platform_errors[source])
        try:
            player_class = load_reference_class(source, self.music_plugin_dir)
        except Exception as exc:
            reason = f"点歌插件里没加载到 {source}（{type(exc).__name__}），跳过"
            self._platform_errors[source] = reason
            logger.info(f"[音源路由] {reason}")
            raise _SkipSource(reason) from exc

        settings = {
            name: _PlatformSettings(self._loaded_settings.get(name))
            for name in ("netease", "kugou", "kuwo", "qqmusic")
        }
        if source == "qqmusic" and not settings["qqmusic"].uin.strip():
            reason = "点歌插件没配置 QQ 音乐 uin，跳过"
            self._platform_errors[source] = reason
            logger.info(f"[音源路由] {reason}")
            raise _SkipSource(reason)

        try:
            platform = player_class(_ShimConfig(self.dest_dir, settings))
        except Exception as exc:
            reason = f"初始化失败（{type(exc).__name__}），跳过"
            self._platform_errors[source] = reason
            logger.info(f"[音源路由] {source} {reason}")
            raise _SkipSource(reason) from exc
        self._platforms[source] = platform
        return platform

    # ---------------- 小工具 ----------------

    async def _netease_fetch(
        self, song_id: str, destination: str
    ) -> tuple[Optional[str], str, bool]:
        try:
            path, reason, is_trial = await asyncio.wait_for(
                self.netease_fetcher.fetch_song(
                    song_id, destination, self.quality_br
                ),
                timeout=self.download_timeout,
            )
        except Exception as exc:
            logger.warning(
                f"[音源路由] 网易云取歌出错: {type(exc).__name__}: {exc}"
            )
            return None, f"取歌出错（{type(exc).__name__}）", False
        return path, reason, is_trial

    async def _download_direct(
        self, url: str, destination: str
    ) -> tuple[Optional[str], str]:
        try:
            return await asyncio.wait_for(
                self.netease_fetcher.download(url, destination),
                timeout=self.download_timeout,
            )
        except Exception as exc:
            return None, f"下载出错（{type(exc).__name__}）"

    async def _kugou_legacy_url(self, song_hash: str) -> str:
        """酷狗移动端老接口取直链（点歌插件的 v5/url 要验证时的兜底）。"""
        hash_text = str(song_hash or "").strip()
        if not hash_text:
            return ""
        url = (
            "http://m.kugou.com/app/i/getSongInfo.php?cmd=playInfo&hash="
            + urllib.parse.quote(hash_text)
        )

        def _request() -> Any:
            request = urllib.request.Request(
                url,
                headers={
                    "User-Agent": (
                        "Mozilla/5.0 (iPhone; CPU iPhone OS 13_2_3 like Mac OS X) "
                        "AppleWebKit/605.1.15 (KHTML, like Gecko) "
                        "Version/13.0.3 Mobile/15E148 Safari/604.1"
                    )
                },
            )
            with urllib.request.urlopen(request, timeout=15) as response:
                return json.loads(response.read().decode("utf-8", "ignore"))

        try:
            data = await asyncio.to_thread(_request)
        except Exception as exc:
            logger.debug(f"[音源路由] 酷狗移动端接口失败: {type(exc).__name__}: {exc}")
            return ""
        link = data.get("url") if isinstance(data, dict) else ""
        return str(link or "").strip()

    def _succeed(
        self, attempt: SourceAttempt, candidate: SongCandidate, path: str
    ) -> SourceAttempt:
        """量一次真实时长，过 80 秒闸门后把成功写回 attempt。"""
        length = netease_fetch.probe_duration_ms(path)
        if length is None:
            netease_fetch._remove_quietly(path)
            attempt.status = "no_duration"
            attempt.reason = "下载完量不到时长"
            return attempt
        if length < MIN_FULL_LENGTH_MS:
            netease_fetch._remove_quietly(path)
            attempt.status = "too_short"
            attempt.reason = f"只有 {int(round(length / 1000))} 秒，不像整首"
            return attempt
        candidate.duration_ms = length
        attempt.ok = True
        attempt.candidate = candidate
        attempt.path = path
        attempt.had_candidate = True
        return attempt

    @staticmethod
    def _artist_hint(artists: Any) -> str:
        """只有一个歌手时拿来做严格过滤；一串歌手（A、B、C）就不当硬条件。"""
        text = str(artists or "").strip()
        if not text or re.search(r"[、/,，&]", text):
            return ""
        return text

    @staticmethod
    def _normalize_request(song: Any) -> dict:
        if isinstance(song, str):
            text = song.strip()
            return {"id": text, "name": text, "artists": "", "source": ""}
        if not isinstance(song, dict):
            text = str(song or "").strip()
            return {"id": text, "name": text, "artists": "", "source": ""}
        return {
            "id": str(song.get("songmid") or song.get("id") or "").strip(),
            "name": str(song.get("name") or song.get("title") or "").strip(),
            "artists": str(song.get("artists") or song.get("artist") or "").strip(),
            "source": str(song.get("source") or "").strip().casefold(),
        }

    def _search_keywords(self, request: dict) -> list[str]:
        """搜歌关键词：只用 song_name 原文整串，不拼歌手、不拆版本。

        0.8.0 起不再补「歌名 + 歌手」这种关键词：用户怎么说的就怎么搜，
        歌手只当过滤条件，避免把「司凤」这类版本词拼进搜索词后带偏结果。
        """
        name = str(request.get("name") or "").strip()
        return [name] if name else []

    @staticmethod
    def _compose_reason(attempts: list[SourceAttempt]) -> str:
        if not attempts:
            return "没有可用的音源"
        if (
            any(attempt.status == "only_accompaniment" for attempt in attempts)
            and not any(attempt.had_candidate for attempt in attempts)
        ):
            return REASON_ONLY_ACCOMPANIMENT
        parts = [
            f"{attempt.display}：{attempt.reason or attempt.status or '没成'}"
            for attempt in attempts
        ]
        return "；".join(parts) or "所有音源都没取到"
