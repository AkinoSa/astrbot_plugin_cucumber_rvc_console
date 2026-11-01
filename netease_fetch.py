"""网易云取歌 + 下载：照搬点歌插件 ``astrbot_plugin_music`` 的实现。

来源（只读参考，未改动点歌插件）
============================

- ``core/platform/netease.py``     搜歌走 ``music.163.com/api/search/get/web``，
  直链走 ``https://api.qijieya.cn/meting/?type=song&id=<id>``；
- ``core/platform/netease_api.py`` 另一条路：NodeJS 版 API 的 ``/song/url``
  （本模块用带 level 的 ``/song/url/v1``，失败再退回 ``/song/url``）；
- ``core/platform/base.py``        ``_request`` / ``_parse_response`` 的请求与解析，
  ``download_audio`` 的流式落盘（uuid 命名、按 URL 后缀白名单、失败删半截文件）；
- ``main.py``                      按音源优先级串联：第一条路拿不到就换下一条。

保留
====

搜歌、取直链、下载、落盘命名与失败清理，以及"一条路失败换下一条"的回退顺序。

去掉
====

发送卡片 / 语音 / 文件、歌词与评论渲染、封面卡片、签名卡片等与"把歌落到本地"
无关的部分。

本插件自己加的一条保护
======================

**80 秒时长闸门**：下载完用 ``ffprobe`` 量真实时长，不足 80 秒的删文件判失败
（点歌插件没有这道闸门，它只把"试听片段"写在卡片备注里）。取不到时长也按失败
处理——宁可不出歌，也不把 60 秒片段交给中间层。
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import urllib.parse
import uuid
from typing import Any, Optional, Tuple

import aiohttp

from astrbot.api import logger


# ---------------------------------------------------------------------------
# 接口常量（与点歌插件保持一致）
# ---------------------------------------------------------------------------
#: 搜歌接口：POST，参数 s / limit / type=1 / offset=0，带 appver=2.0.2 这个 cookie
SEARCH_URL = "http://music.163.com/api/search/get/web"
SEARCH_COOKIES = {"appver": "2.0.2"}
#: 取直链的主路：点歌插件 netease 平台用的 meting 服务
DEFAULT_METING_BASE = "https://api.qijieya.cn/meting/"
#: 取直链的备用路：点歌插件 netease_api 平台的默认地址（可被配置覆盖）
DEFAULT_API_BASE = "https://zm.wwoyun.cn"
#: 点歌插件里那份 UA，原样带过来
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; WOW64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/55.0.2883.87 Safari/537.36"
    )
}
#: 码率 → NodeJS API 的 level 参数
API_LEVELS = ((320000, "exhigh"), (192000, "higher"), (128000, "standard"))
#: 单文件下载上限 60MB（整首 320k 约 10MB，留足余量）
MAX_DOWNLOAD_BYTES = 60 * 1024 * 1024
#: 整首歌最短时长：80 秒（本插件自己的保护）
MIN_FULL_LENGTH_MS = 80_000
#: 点歌插件下载时就是 1024 字节一片，这里照搬
_CHUNK_SIZE = 1024
_JSON_LIMIT = 4 * 1024 * 1024
#: 认得出的音频后缀，其它一律按 .mp3 存（同点歌插件）
_AUDIO_SUFFIXES = (
    ".mp3",
    ".flac",
    ".m4a",
    ".aac",
    ".ogg",
    ".opus",
    ".wav",
)

# ---------------------------------------------------------------------------
# 失败原因（一句短话，给用户/日志看）
# ---------------------------------------------------------------------------
REASON_BAD_ID = "歌曲id不合法"
REASON_NO_URL = "没拿到链接"
REASON_NO_COOKIE = "没配置cookie"
REASON_TRIAL = "只给了试听"
REASON_DOWNLOAD = "下载中断"
REASON_NOT_AUDIO = "下载地址返回的不是音频"
REASON_TOO_BIG = "文件超过60MB"
REASON_HTTP = "接口返回 HTTP {status}"
REASON_HTTP_DOWNLOAD = "下载地址返回 HTTP {status}"
REASON_DL_NO_TIME = "下载完量不到时长，无法确认是整首"
REASON_DL_TOO_SHORT = "下载到的不是整首（时长 {seconds} 秒）"
REASON_API_UNREACHABLE = "取歌服务连不上"


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------
def _short_error(exc: BaseException) -> str:
    """把网络异常压成一句话，别把整段堆栈塞进用户可见文案。"""
    text = str(exc or "").strip()
    if not text:
        return type(exc).__name__
    return text.splitlines()[0][:80]


def _safe_json(raw: str) -> Any:
    try:
        return json.loads(raw)
    except Exception:
        return None


def normalize_api_base(base_url: Any) -> str:
    """把服务地址收拾成 ``http(s)://host:port``（去空格、去尾部斜杠）。"""
    text = str(base_url or "").strip()
    if not text:
        return ""
    if not text.lower().startswith(("http://", "https://")):
        text = "http://" + text
    return text.rstrip("/")


def normalize_meting_base(base_url: Any) -> str:
    """meting 服务地址：没填就用默认，末尾补一个 ``/`` 好拼 ``?type=...``。"""
    text = str(base_url or "").strip() or DEFAULT_METING_BASE
    if not text.lower().startswith(("http://", "https://")):
        text = "https://" + text
    return text if text.endswith(("/", "?")) else text + "/"


def api_service_level(quality_br: int) -> str:
    """把码率配置（320000/192000/128000）换成服务端要的 ``level`` 参数。"""
    try:
        br = int(quality_br or 320000)
    except (TypeError, ValueError):
        br = 320000
    for bitrate, level in API_LEVELS:
        if br >= bitrate:
            return level
    return "standard"


def _suffix_from_url(url: str) -> str:
    """按 URL 路径取后缀；不是认识的音频后缀就按 .mp3 存（同点歌插件）。"""
    path = str(url or "").split("?", 1)[0]
    suffix = os.path.splitext(path)[1].lower()
    return suffix if suffix in _AUDIO_SUFFIXES else ".mp3"


def _remove_quietly(path: str) -> None:
    try:
        if path and os.path.isfile(path):
            os.remove(path)
    except OSError as exc:
        logger.debug(f"删除临时文件失败: {path} ({exc})")


def _as_dict(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def _first_item(data: Any) -> dict:
    """取返回体里描述这首歌的那一条（兼容 list / dict）。"""
    if isinstance(data, list):
        return _as_dict(data[0]) if data else {}
    return _as_dict(data)


def _alias_text(value: Any) -> str:
    """把别名（字符串 / 列表）拼成一段可比较文本；没有就返回空串。"""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (list, tuple)):
        return " / ".join(str(item).strip() for item in value if str(item).strip())
    return ""


def _pick_time_ms(payload: Any) -> Optional[int]:
    """读整首时长（毫秒）；读不到返回 None。"""
    meta: dict = {}
    if isinstance(payload, dict):
        meta.update({k: v for k, v in payload.items() if k != "data"})
        meta.update(_first_item(payload.get("data")))
    elif isinstance(payload, list):
        meta.update(_first_item(payload))
    value = meta.get("time")
    if value is None:
        value = meta.get("duration")
        if value is not None:
            try:
                seconds = float(value)
                value = seconds * 1000 if seconds < 1000 else seconds
            except (TypeError, ValueError):
                value = None
    if value is None:
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def judge_url_payload(payload: Any) -> Tuple[str, str, bool]:
    """从 NodeJS API 的 ``/song/url`` 返回体里取直链并判"是不是试听"。

    Returns:
        ``(直链, 失败原因, 是否试听)``；拿到链接时失败原因为空串。
        接口明确说只给试听片段时也返回链接（下载后还有 80 秒闸门兜着）。
    """
    if not isinstance(payload, (dict, list)):
        return "", REASON_NO_URL, False
    data = payload.get("data") if isinstance(payload, dict) else payload
    info = _first_item(data)
    link = str(info.get("url") or "").strip()
    if not link:
        return "", REASON_NO_URL, False
    free_trial = info.get("freeTrialInfo")
    is_trial = free_trial not in (None, "", "null")
    length = _pick_time_ms(payload)
    if length is not None and length < MIN_FULL_LENGTH_MS:
        is_trial = True
    return link, "", is_trial


def is_cookie_failure(reason: str, is_trial: bool = False) -> bool:
    """判断这次失败是不是"cookie 相关"（用于连续 3 次后提醒管理员）。

    认定范围沿用旧实现：只给试听、HTTP 401、``code != 200``、没配 cookie。
    改用点歌插件那套实现后 meting 这条主路不吃 cookie，所以这条判定只在
    备用服务/试听片段上生效，不会因为"没填 cookie"就误报。
    """
    if is_trial:
        return True
    text = str(reason or "").strip()
    if not text:
        return False
    if text == REASON_NO_COOKIE:
        return True
    if text.startswith("接口返回 HTTP 401"):
        return True
    if text.startswith("接口返回 code="):
        return True
    return False


def probe_duration_ms(path: str) -> Optional[int]:
    """用 ffprobe 量本地音频时长（毫秒）；没有 ffprobe 或读不出来返回 None。"""
    exe = shutil.which("ffprobe")
    if not exe:
        logger.debug("没找到 ffprobe，无法测量下载文件时长")
        return None
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        completed = subprocess.run(
            [
                exe,
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=nw=1:nk=1",
                path,
            ],
            capture_output=True,
            text=True,
            timeout=20,
            creationflags=creationflags,
        )
        text = (completed.stdout or "").strip()
        if not text:
            return None
        return int(float(text) * 1000)
    except Exception as exc:
        logger.debug(f"ffprobe 读取时长失败: {path} ({exc})")
        return None


# ---------------------------------------------------------------------------
# 取歌器
# ---------------------------------------------------------------------------
class NeteaseFetcher:
    """搜歌 + 取直链 + 下载，把点歌插件的网易云实现压缩成一个类。

    用法::

        fetcher = NeteaseFetcher(cookie=..., api_base=...)
        path, reason, is_trial = await fetcher.fetch_song("1365898499", temp_dir)
        await fetcher.close()

    所有方法都是异步的（点歌插件本身就是 aiohttp），不要再塞进 ``to_thread``。
    """

    def __init__(
        self,
        *,
        timeout: float = 20.0,
        cookie: str = "",
        meting_base: str = DEFAULT_METING_BASE,
        api_base: str = "",
        api_token: str = "",
        proxy: str = "",
    ):
        try:
            timeout_value = float(timeout or 20.0)
        except (TypeError, ValueError):
            timeout_value = 20.0
        self.timeout = max(5.0, timeout_value)
        self.cookie = str(cookie or "").strip()
        self.meting_base = normalize_meting_base(meting_base)
        self.api_base = normalize_api_base(api_base)
        self.api_token = str(api_token or "").strip()
        self.proxy = str(proxy or "").strip()
        self.session: Optional[aiohttp.ClientSession] = None

    # ---------- HTTP ----------

    def _get_session(self) -> aiohttp.ClientSession:
        """按需建 session（同点歌插件：懒加载、用完要 close）。"""
        if self.session is None or self.session.closed:
            self.session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=self.timeout)
            )
        return self.session

    async def close(self) -> None:
        """关掉自己建的 session。"""
        if self.session is not None and not self.session.closed:
            await self.session.close()
        self.session = None

    async def _request(
        self,
        url: str,
        *,
        method: str = "GET",
        data: Optional[dict] = None,
        headers: Optional[dict] = None,
        cookies: Optional[dict] = None,
        attempts: int = 2,
    ) -> Any:
        """发一次请求并解析 JSON（点歌插件 ``_request`` + ``_parse_response``）。

        网络类错误会重试一次（点歌插件没有重试，这是本模块加的；失败原因仍然
        只返回短句，技术细节进 logger）。
        """
        request_headers = dict(headers or HEADERS)
        proxy = self.proxy or None
        last_error = ""
        for attempt in range(1, max(1, attempts) + 1):
            try:
                session = self._get_session()
                if method.upper() == "POST":
                    context = session.post(
                        url,
                        data=data,
                        headers=request_headers,
                        cookies=cookies,
                        proxy=proxy,
                    )
                else:
                    context = session.get(
                        url,
                        headers=request_headers,
                        cookies=cookies,
                        proxy=proxy,
                    )
                async with context as resp:
                    text = await resp.text()
                    if resp.status != 200:
                        logger.warning(f"HTTP 请求返回 {resp.status}: {text[:200]}")
                        return None
                    if not text.strip():
                        logger.warning("HTTP 响应为空")
                        return None
                    payload = _safe_json(text)
                    return payload if payload is not None else text
            except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as exc:
                last_error = _short_error(exc)
                logger.warning(
                    f"请求 {url} 失败（第 {attempt}/{attempts} 次）: {last_error}"
                )
                if attempt >= attempts:
                    break
                await asyncio.sleep(0.5)
        return None

    # ---------- 搜歌 ----------

    async def search_songs(self, keyword: str, limit: int = 5) -> list[dict]:
        """按关键词搜网易云，返回 ``[{id, name, artists, duration, alias}]``。

        和点歌插件 ``NetEaseMusic.fetch_songs`` 完全同一个接口和参数；多带一个
        ``alias``（搜索结果里的别名字段，例如「雨爱」的「司凤版」），
        0.8.0 起选歌把「标题 + 别名」拼成一段和歌名整串比较。
        """
        keyword = str(keyword or "").strip()
        if not keyword:
            return []
        try:
            limit = max(1, int(limit or 5))
        except (TypeError, ValueError):
            limit = 5
        result = await self._request(
            SEARCH_URL,
            method="POST",
            data={"s": keyword, "limit": limit, "type": 1, "offset": 0},
            cookies=SEARCH_COOKIES,
        )
        if (
            not isinstance(result, dict)
            or not isinstance(result.get("result"), dict)
            or not isinstance(result["result"].get("songs"), list)
        ):
            logger.warning(f"搜歌返回意料之外的数据: {str(result)[:200]}")
            return []
        songs: list[dict] = []
        for item in result["result"]["songs"][:limit]:
            if not isinstance(item, dict) or item.get("id") is None:
                continue
            artists = item.get("artists") or item.get("ar") or []
            songs.append(
                {
                    "id": str(item.get("id")),
                    "name": str(item.get("name") or ""),
                    "artists": "、".join(
                        str(artist.get("name") or "")
                        for artist in artists
                        if isinstance(artist, dict) and artist.get("name")
                    ),
                    "duration": item.get("duration") or item.get("dt"),
                    "alias": _alias_text(
                        item.get("alias")
                        or item.get("alia")
                        or item.get("tns")
                        or ""
                    ),
                }
            )
        return songs

    async def fetch_song_alias(self, song_id: Any) -> str:
        """查歌曲详情补别名（搜索没带 alias 时用）。

        详情接口里的别名字段有三种写法：``alias`` / ``alia`` / ``tns``，
        这里都试一遍；拿不到就返回空串，不影响主流程。
        """
        song_id = str(song_id or "").strip()
        if not song_id.isdigit():
            return ""
        payload = await self._request(
            f"https://music.163.com/api/song/detail?ids=[{song_id}]"
        )
        if not isinstance(payload, dict):
            return ""
        songs = payload.get("songs")
        if not isinstance(songs, list) or not songs or not isinstance(songs[0], dict):
            return ""
        song = songs[0]
        parts: list[str] = []
        for key in ("alias", "alia", "tns"):
            text = _alias_text(song.get(key))
            if text and text not in parts:
                parts.append(text)
        return " / ".join(parts)

    # ---------- 取直链 ----------

    async def _direct_url_from_meting(
        self, song_id: str
    ) -> Tuple[str, str]:
        """主路：点歌插件 netease 平台的 meting 接口。"""
        url = f"{self.meting_base}?type=song&id={song_id}"
        payload = await self._request(url)
        if isinstance(payload, dict):
            payload = [payload]
        if not isinstance(payload, list) or not payload:
            return "", REASON_NO_URL
        item = _first_item(payload)
        link = str(item.get("url") or "").strip()
        if not link:
            return "", REASON_NO_URL
        return link, ""

    async def _direct_url_from_api(
        self, song_id: str, quality_br: int
    ) -> Tuple[str, str, bool]:
        """备用路：点歌插件 netease_api 平台的 NodeJS 接口。

        先按本插件旧实现用带 level 的 ``/song/url/v1``（能换到带签名的链接），
        不行再退回点歌插件原版的 ``/song/url``。
        """
        base = self.api_base or DEFAULT_API_BASE
        headers = dict(HEADERS)
        if self.api_token:
            headers["Authorization"] = f"Bearer {self.api_token}"
        level = api_service_level(quality_br)
        query = f"id={song_id}&level={level}"
        if self.cookie:
            query += "&cookie=" + urllib.parse.quote(f"MUSIC_U={self.cookie}", safe="")

        payload = await self._request(f"{base}/song/url/v1?{query}", headers=headers)
        link, reason, is_trial = judge_url_payload(payload)
        if link:
            return link, "", is_trial

        payload = await self._request(f"{base}/song/url?{query}", headers=headers)
        link, reason, is_trial = judge_url_payload(payload)
        if link:
            return link, "", is_trial
        return "", reason, is_trial

    async def resolve_direct_url(
        self, song_id: str, quality_br: int = 320000
    ) -> Tuple[str, str, bool, str]:
        """取播放直链：先 meting，拿不到再换 NodeJS 服务。

        Returns:
            ``(直链, 失败原因, 是否试听, 实际走的哪条路)``。
        """
        song_id = str(song_id or "").strip()
        if not song_id.isdigit():
            return "", REASON_BAD_ID, False, ""

        link, reason = await self._direct_url_from_meting(song_id)
        if link:
            logger.info(f"[取歌] meting 拿到直链: id={song_id}")
            return link, "", False, "meting"
        logger.warning(f"[取歌] meting 没拿到直链: id={song_id} reason={reason}")

        if self.api_base:
            link, reason, is_trial = await self._direct_url_from_api(song_id, quality_br)
            if link:
                logger.info(f"[取歌] 备用服务拿到直链: id={song_id} base={self.api_base}")
                return link, "", is_trial, "api"
            logger.warning(
                f"[取歌] 备用服务也没拿到直链: id={song_id} "
                f"base={self.api_base} reason={reason}"
            )
            return "", reason or REASON_NO_URL, is_trial, ""

        return "", reason or REASON_NO_URL, False, ""

    # ---------- 下载 ----------

    async def download(self, url: str, dest_dir: str) -> Tuple[str, str]:
        """把直链流式下到 ``dest_dir``，返回 ``(本地路径, 失败原因)``。

        命名 / 落盘 / 失败清理照搬点歌插件 ``BaseMusicPlayer.download_audio``：
        ``uuid4().hex`` 当文件名、按 URL 后缀白名单、写一半失败直接删掉。
        """
        if not url:
            return "", REASON_NO_URL
        if not dest_dir:
            return "", REASON_DOWNLOAD
        try:
            os.makedirs(dest_dir, exist_ok=True)
        except OSError as exc:
            logger.error(f"创建临时目录失败: {dest_dir} ({exc})")
            return "", REASON_DOWNLOAD

        file_path = os.path.join(dest_dir, f"{uuid.uuid4().hex}{_suffix_from_url(url)}")
        written = 0
        expected = 0
        try:
            async with self._get_session().get(
                url, headers=HEADERS, proxy=self.proxy or None
            ) as response:
                if response.status != 200:
                    logger.warning(f"歌曲下载失败，HTTP 状态码：{response.status}")
                    return "", REASON_HTTP_DOWNLOAD.format(status=response.status)
                content_type = str(response.headers.get("Content-Type") or "").lower()
                if (
                    content_type.startswith("text/")
                    or "json" in content_type
                    or "html" in content_type
                ):
                    # 直链失效时 CDN 常返回 200 + 错误页，别把它当 mp3 存下来
                    logger.warning(f"下载地址返回的不是音频: {content_type}")
                    return "", REASON_NOT_AUDIO
                try:
                    expected = int(response.headers.get("Content-Length") or 0)
                except (TypeError, ValueError):
                    expected = 0
                if expected > MAX_DOWNLOAD_BYTES:
                    return "", REASON_TOO_BIG
                with open(file_path, "wb") as handle:
                    async for chunk in response.content.iter_chunked(_CHUNK_SIZE):
                        written += len(chunk)
                        if written > MAX_DOWNLOAD_BYTES:
                            raise ValueError(REASON_TOO_BIG)
                        handle.write(chunk)
        except ValueError as exc:
            _remove_quietly(file_path)
            return "", str(exc) or REASON_TOO_BIG
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as exc:
            _remove_quietly(file_path)
            logger.error(f"歌曲下载失败，错误信息：{_short_error(exc)}")
            return "", REASON_DOWNLOAD

        if written <= 0:
            _remove_quietly(file_path)
            return "", REASON_DOWNLOAD
        if expected and written < expected:
            # 连接被掐断时文件已落盘但内容不全，宁可丢掉也不要半首歌
            logger.warning(
                f"下载不完整: {file_path}（期望 {expected} 字节，实际 {written} 字节）"
            )
            _remove_quietly(file_path)
            return "", REASON_DOWNLOAD
        logger.info(f"[取歌] 下载完成: {file_path}（{written} 字节）")
        return file_path, ""

    # ---------- 对外总入口 ----------

    async def fetch_song(
        self,
        song_id: Any,
        dest_dir: str,
        quality_br: int = 320000,
    ) -> Tuple[Optional[str], str, bool]:
        """按歌曲 id 取整首并落到本地：取直链 → 下载 → 过 80 秒闸门。

        Returns:
            ``(本地路径 或 None, 失败原因, 是否试听)``。
        """
        song_id = str(song_id if song_id is not None else "").strip()
        if not song_id.isdigit():
            return None, REASON_BAD_ID, False

        link, reason, is_trial, via = await self.resolve_direct_url(song_id, quality_br)
        if not link:
            return None, reason or REASON_NO_URL, is_trial

        path, download_reason = await self.download(link, dest_dir)
        if not path:
            return None, download_reason or REASON_DOWNLOAD, is_trial

        length = probe_duration_ms(path)
        if length is None:
            logger.warning(f"下载完量不到时长，按失败处理: id={song_id} file={path}")
            _remove_quietly(path)
            return None, REASON_DL_NO_TIME, is_trial
        if length < MIN_FULL_LENGTH_MS:
            seconds = int(round(length / 1000))
            logger.warning(
                f"[取歌] 下载到的不是整首: id={song_id} 时长={seconds} 秒 via={via}"
            )
            _remove_quietly(path)
            return None, REASON_DL_TOO_SHORT.format(seconds=seconds), True
        logger.info(
            f"[取歌] 整首取到: id={song_id} via={via} 时长={length / 1000:.1f} 秒"
        )
        return path, "", False
