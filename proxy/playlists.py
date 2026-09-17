"""洛雪（第三方平台）歌单/榜单数据源.

为飞牛音乐侧边栏提供「洛雪歌单」：从各平台公开接口拉取排行榜歌单，
转换成代理可识别的虚拟歌单（guid 前缀 ``online:playlist:chart:``）。

设计要点：
- 歌单是**虚拟**的：只在内存/磁盘缓存，不写入飞牛数据库。
- 榜单曲目只带平台 id，真实音质/直链仍由 lxmusic-service 在播放时解析。
- 拉取失败时保留上次可用缓存；单个榜单失败不影响其它榜单。

支持的来源（platform 对应 lxmusic-service 的子源命名）：
- ``wy`` 网易云：榜单列表 + 歌单详情
- ``kg`` 酷狗：排行榜列表 + 榜单歌曲
- ``kw`` 酷我：排行榜列表 + 榜单歌曲
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from typing import Any

import httpx

logger = logging.getLogger("fnmusic_proxy.playlists")

CHART_GUID_PREFIX = "online:playlist:chart:"

# 榜单缓存有效期（秒）；榜单变动不频繁
CHART_TTL_S = float(os.environ.get("FNMUSIC_CHART_TTL", "1800"))
# 单个榜单最多取多少首（侧边栏歌单不宜过大）
CHART_TRACK_LIMIT = int(os.environ.get("FNMUSIC_CHART_TRACK_LIMIT", "100"))
# 拉取超时
CHART_TIMEOUT_S = float(os.environ.get("FNMUSIC_CHART_TIMEOUT", "12"))
# 封面图请求尺寸（酷狗 URL 用 {size} 占位符；越大越清晰也越慢）
COVER_IMAGE_SIZE = os.environ.get("FNMUSIC_CHART_COVER_SIZE", "480")

_UA_PC = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
_UA_MOBILE = "Mozilla/5.0 (Linux; Android 12; Pixel 6) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Mobile Safari/537.36"

# 需要展示的榜单白名单（避免侧边栏被几十个榜单淹没）。
# 值为「平台 -> 允许的榜单标识集合」；空集合表示该平台全部展示。
# 网易云只保留官方榜中较通用的几个。
CHART_WHITELIST: dict[str, set[str]] = {
    "wy": {"3778678", "3779629", "19723756", "2884035"},  # 热歌/新歌/飙升/原创
    "kg": {"8888", "6666", "52144", "24971", "51341"},     # TOP500/飙升/短视频/民谣
    "kw": {"16", "93", "17", "26"},                        # 热歌/飙升/新歌/华语
}

# 拉取重试设置：平台接口偶发 TLS/超时抖动，重试可显著降低虚假失败
_FETCH_RETRIES = int(os.environ.get("FNMUSIC_CHART_RETRIES", "3"))
_FETCH_BACKOFF_S = 0.6

# 平台展示名
PLATFORM_LABELS = {"wy": "网易云", "kg": "酷狗", "kw": "酷我", "mg": "咪咕"}

# 榜单曲目默认按无损展示：有损字段才回退 mp3。
# 说明：显示格式只影响界面与文件名；真正播放的音质仍由 lxmusic-service
# 在 track/url 时按 lossless→high→standard 顺序解析。
DEFAULT_LOSSLESS_EXT = os.environ.get("FNMUSIC_CHART_DEFAULT_EXT", "flac") or "flac"

# 酷我 formats 里表示「存在无损/高解析」的标记
_KW_LOSSLESS_MARKERS = (
    "ALFLAC", "FLAC", "DTSX", "DTS", "ZPGA714", "ZPGA501",
    "DDJOC768", "DDJOC640", "DDJOC448", "DTSX",
)

# 进程内缓存：key -> {"ts": float, "value": Any}
_CACHE: dict[str, dict] = {}


def _wy_lossless(item: dict) -> bool:
    """网易云：sq（无损）/ hr（Hi-Res）字段存在即视为有无损音源。"""
    for key in ("sq", "hr"):
        v = item.get(key)
        if isinstance(v, dict) and (v.get("br") or v.get("size")):
            return True
    return bool(item.get("sq") or item.get("hr"))


def _kg_lossless(item: dict) -> bool:
    """酷狗：sqhash 存在且非空即表示有无损源；320hash 只能算有损。"""
    return bool(str(item.get("sqhash") or "").strip())


def _kw_lossless(item: dict) -> bool:
    """酷我：formats 中出现 ALFLAC/DTSX 等无损标记。"""
    formats = str(item.get("formats") or "").upper()
    if not formats:
        # 无 formats 信息时不妄称无损，交给默认值
        return True
    return any(marker in formats for marker in _KW_LOSSLESS_MARKERS)


def _cache_get(key: str, ttl: float) -> Any:
    entry = _CACHE.get(key)
    if not entry:
        return None
    if time.time() - entry["ts"] > ttl:
        return None
    return entry["value"]


def _cache_put(key: str, value: Any) -> None:
    _CACHE[key] = {"ts": time.time(), "value": value}
    # 简单上限，防止无界增长
    if len(_CACHE) > 256:
        for k in sorted(_CACHE, key=lambda k: _CACHE[k]["ts"])[:128]:
            _CACHE.pop(k, None)


def _normalize_cover(url: Any) -> str:
    """规范化封面 URL。

    酷狗返回的封面形如 ``http://imge.kugou.com/mcommon/{size}/...``，
    其中 ``{size}`` 是必须替换的尺寸占位符，否则 URL 不可访问。
    """
    raw = str(url or "").strip()
    if not raw:
        return ""
    if "{size}" in raw:
        raw = raw.replace("{size}", COVER_IMAGE_SIZE)
    if not raw.startswith(("http://", "https://")):
        return ""
    return raw


def chart_guid(platform: str, chart_id: str) -> str:
    """构造虚拟歌单 GUID。"""
    return f"{CHART_GUID_PREFIX}{platform}:{chart_id}"


def chart_name(platform: str, chart_id: str) -> "str | None":
    """从已缓存的榜单列表中查榜单名（无缓存时返回 None）。"""
    for key, entry in _CACHE.items():
        if not key.startswith("charts:"):
            continue
        for ch in entry.get("value") or []:
            if ch.get("platform") == platform and str(ch.get("id")) == str(chart_id):
                return str(ch.get("name") or "") or None
    return None


def remember_chart_count(platform: str, chart_id: str, count: int) -> None:
    """回填榜单曲目数。

    酷狗/酷我的「榜单列表」接口不提供歌曲数，只有真正拉过曲目才知道。
    这里把已知数量写回榜单缓存，使侧边栏下次加载能显示真实数量，
    避免长期显示 0 造成误解。
    """
    try:
        count = int(count)
    except (TypeError, ValueError):
        return
    if count <= 0:
        return
    for key, entry in _CACHE.items():
        if not key.startswith("charts:"):
            continue
        for ch in entry.get("value") or []:
            if ch.get("platform") == platform and str(ch.get("id")) == str(chart_id):
                if int(ch.get("trackCount") or 0) != count:
                    ch["trackCount"] = count


def is_chart_guid(guid: str | None) -> bool:
    return str(guid or "").startswith(CHART_GUID_PREFIX)


def parse_chart_guid(guid: str) -> "tuple[str, str] | tuple[None, None]":
    """解析虚拟歌单 GUID -> (platform, chart_id)。"""
    raw = str(guid or "")
    if not raw.startswith(CHART_GUID_PREFIX):
        return None, None
    rest = raw[len(CHART_GUID_PREFIX):]
    # 平台是已知的短码，chart_id 可能含冒号（极少见）
    if ":" not in rest:
        return None, None
    platform, _, chart_id = rest.partition(":")
    platform = platform.strip().lower()
    chart_id = chart_id.strip()
    if platform not in PLATFORM_LABELS or not chart_id:
        return None, None
    return platform, chart_id


def _chart_enabled(platform: str, chart_id: str) -> bool:
    allow = CHART_WHITELIST.get(platform)
    if not allow:
        return True
    return chart_id in allow


async def _get_with_retry(client: httpx.AsyncClient, url: str, **kwargs: Any) -> httpx.Response:
    """带退避重试的 GET。平台接口偶发 TLS/连接抖动时避免整榜失败。"""
    last: Exception | None = None
    for attempt in range(max(1, _FETCH_RETRIES)):
        try:
            r = await client.get(url, **kwargs)
            r.raise_for_status()
            return r
        except Exception as e:  # noqa: BLE001
            last = e
            if attempt + 1 < _FETCH_RETRIES:
                await asyncio.sleep(_FETCH_BACKOFF_S * (attempt + 1))
    assert last is not None
    raise last


# ------------------------------------------------------------------ 网易云 wy

async def _wy_charts(client: httpx.AsyncClient) -> list[dict]:
    """网易云排行榜列表。用 /api/toplist 获取官方榜。"""
    r = await _get_with_retry(
        client,
        "https://music.163.com/api/toplist",
        headers={"User-Agent": _UA_PC, "Referer": "https://music.163.com/"},
    )
    data = r.json() or {}
    charts = []
    for item in (data.get("list") or []):
        if not isinstance(item, dict):
            continue
        cid = str(item.get("id") or "")
        name = str(item.get("name") or "")
        if not cid or not name:
            continue
        try:
            count = int(item.get("trackCount") or 0)
        except (TypeError, ValueError):
            count = 0
        charts.append({
            "platform": "wy",
            "id": cid,
            "name": name,
            "cover": str(item.get("coverImgUrl") or ""),
            "description": str(item.get("updateFrequency") or ""),
            "trackCount": count,
        })
    return charts


async def _wy_chart_tracks(client: httpx.AsyncClient, chart_id: str) -> list[dict]:
    """网易云歌单曲目。playlist/detail 只回 10 首，需用 song/detail 补全。"""
    r = await _get_with_retry(
        client,
        "https://music.163.com/api/v6/playlist/detail",
        params={"id": chart_id, "n": CHART_TRACK_LIMIT, "s": 8},
        headers={"User-Agent": _UA_PC, "Referer": "https://music.163.com/"},
    )
    data = r.json() or {}
    playlist = data.get("playlist") or {}
    track_ids = [str(t.get("id")) for t in (playlist.get("trackIds") or []) if t.get("id")]
    inline = playlist.get("tracks") or []

    tracks: list[dict] = []
    if inline and len(inline) >= len(track_ids[:CHART_TRACK_LIMIT]):
        tracks = [_wy_track(t) for t in inline]
    elif track_ids:
        # 分批调用 song/detail 补全元数据（每批 <= 100 个 id）
        wanted = track_ids[:CHART_TRACK_LIMIT]
        for i in range(0, len(wanted), 100):
            batch = wanted[i:i + 100]
            payload = json.dumps([{"id": tid} for tid in batch], separators=(",", ":"))
            try:
                rr = await client.get(
                    "https://music.163.com/api/v3/song/detail",
                    params={"c": payload},
                    headers={"User-Agent": _UA_PC, "Referer": "https://music.163.com/"},
                )
                rr.raise_for_status()
                for song in ((rr.json() or {}).get("songs") or []):
                    t = _wy_track(song)
                    if t:
                        tracks.append(t)
            except Exception as e:  # noqa: BLE001
                logger.warning("wy song detail batch failed: %s", e)
        if not tracks:
            tracks = [_wy_track(t) for t in inline]
    return [t for t in tracks if t]


def _wy_track(item: dict) -> "dict | None":
    if not isinstance(item, dict):
        return None
    sid = item.get("id")
    title = str(item.get("name") or "").strip()
    if not sid or not title:
        return None
    artists = item.get("ar") or item.get("artists") or []
    artist = "、".join(str(a.get("name") or "") for a in artists if isinstance(a, dict))
    album = item.get("al") or item.get("album") or {}
    duration_ms = item.get("dt") or item.get("duration") or 0
    try:
        duration_s = float(duration_ms) / 1000.0 if duration_ms else 0.0
    except (TypeError, ValueError):
        duration_s = 0.0
    return {
        "id": f"lx:wy:{sid}",
        "lx_source": "wy",
        "title": title,
        "artist": artist,
        "album": str((album or {}).get("name") or ""),
        "cover_url": str((album or {}).get("picUrl") or ""),
        "duration_s": duration_s,
        "ext": DEFAULT_LOSSLESS_EXT if _wy_lossless(item) else "mp3",
        "fee": int(item.get("fee") or 0),
    }


# -------------------------------------------------------------------- 酷狗 kg

async def _kg_charts(client: httpx.AsyncClient) -> list[dict]:
    r = await _get_with_retry(
        client,
        "http://mobilecdn.kugou.com/api/v3/rank/list",
        params={"version": "9108", "plat": "0", "showtype": "2", "parentid": "0", "apiver": "3"},
        headers={"User-Agent": _UA_MOBILE},
    )
    data = r.json() or {}
    charts = []
    for item in (((data.get("data") or {}).get("info")) or []):
        if not isinstance(item, dict):
            continue
        rid = str(item.get("rankid") or "")
        name = str(item.get("rankname") or "")
        if not rid or not name:
            continue
        charts.append({
            "platform": "kg",
            "id": rid,
            "name": name,
            # 酷狗封面 URL 带 {size} 占位符，需替换为具体尺寸才能访问
            "cover": _normalize_cover(item.get("bannerurl") or item.get("imgurl") or ""),
            "description": "",
            "trackCount": 0,
        })
    return charts


async def _kg_chart_tracks(client: httpx.AsyncClient, chart_id: str) -> list[dict]:
    tracks: list[dict] = []
    page = 1
    per = 30
    while len(tracks) < CHART_TRACK_LIMIT and page <= 10:
        r = await _get_with_retry(
            client,
            "http://mobilecdn.kugou.com/api/v3/rank/song",
            params={"rankid": chart_id, "page": page, "pagesize": per, "version": "9108"},
            headers={"User-Agent": _UA_MOBILE},
        )
        data = r.json() or {}
        info = ((data.get("data") or {}).get("info")) or []
        if not info:
            break
        for item in info:
            t = _kg_track(item)
            if t:
                tracks.append(t)
        if len(info) < per:
            break
        page += 1
    return tracks[:CHART_TRACK_LIMIT]


def _kg_track(item: dict) -> "dict | None":
    if not isinstance(item, dict):
        return None
    fhash = str(item.get("hash") or "").strip()
    title = str(item.get("songname") or "").strip()
    if not fhash or not title:
        return None
    # 酷狗榜单里 singername 常为 null，歌手藏在 filename("歌手 - 歌名")
    artist = str(item.get("singername") or "").strip()
    if not artist:
        filename = str(item.get("filename") or "")
        if " - " in filename:
            maybe = filename.split(" - ", 1)[0].strip()
            if maybe and maybe != title:
                artist = maybe
    duration = item.get("duration") or item.get("timelen") or 0
    try:
        # 榜单接口 duration 单位是秒
        duration_s = float(duration)
        if duration_s > 100000:  # 毫秒
            duration_s /= 1000.0
    except (TypeError, ValueError):
        duration_s = 0.0
    return {
        "id": f"lx:kg:{fhash}",
        "lx_source": "kg",
        "title": title,
        "artist": artist,
        "album": str(item.get("album_name") or ""),
        "cover_url": "",
        "duration_s": duration_s,
        "ext": DEFAULT_LOSSLESS_EXT if _kg_lossless(item) else "mp3",
        "pay_type": int(item.get("feetype") or 0),
        "fail_process": int(item.get("fail_process") or 0),
    }


# -------------------------------------------------------------------- 酷我 kw

async def _kw_chart_meta(client: httpx.AsyncClient, chart_id: str) -> "dict | None":
    """拉取单个酷我榜单的元信息（名称/数量/封面）。榜单详情接口会返回这些字段。"""
    try:
        r = await _get_with_retry(
            client,
            "http://kbangserver.kuwo.cn/ksong.s",
            params={"from": "pc", "fmt": "json", "pn": "0", "rn": "1", "type": "bang",
                    "data": "content", "id": chart_id,
                    "show_copyright_off": "0", "pcmp4": "1", "isbang": "1"},
            headers={"User-Agent": _UA_PC},
        )
        data = r.json() or {}
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(data, dict):
        return None
    name = str(data.get("name") or "").strip()
    # v9_pic2 是酷我较新且稳定的封面字段；pic 在部分榜单是残缺路径
    cover = str(data.get("v9_pic2") or "").strip()
    if not cover.startswith(("http://", "https://")):
        pic = str(data.get("pic") or "").strip()
        cover = pic if pic.startswith(("http://", "https://")) and pic.endswith((".jpg", ".png")) else ""
    try:
        count = int(data.get("num") or 0)
    except (TypeError, ValueError):
        count = 0
    return {"name": name, "cover": cover, "trackCount": count}


async def _kw_charts(client: httpx.AsyncClient) -> list[dict]:
    """酷我排行榜列表。

    酷我没有「榜单列表」接口，因此使用内置榜单 id 集合；
    但每个榜单的详情接口会返回真实名称/数量/封面，故并发补齐。
    """
    known = [
        ("16", "酷我热歌榜"),
        ("93", "酷我飙升榜"),
        ("17", "酷我新歌榜"),
        ("26", "酷我经典榜"),
    ]
    wanted = [(cid, name) for cid, name in known if _chart_enabled("kw", cid)]

    metas = await asyncio.gather(*(_kw_chart_meta(client, cid) for cid, _ in wanted))

    charts: list[dict] = []
    for (cid, fallback_name), meta in zip(wanted, metas):
        meta = meta or {}
        charts.append({
            "platform": "kw",
            "id": cid,
            "name": meta.get("name") or fallback_name,
            "cover": meta.get("cover") or "",
            "description": "",
            "trackCount": int(meta.get("trackCount") or 0),
        })
    return charts


async def _kw_chart_tracks(client: httpx.AsyncClient, chart_id: str) -> list[dict]:
    r = await _get_with_retry(
        client,
        "http://kbangserver.kuwo.cn/ksong.s",
        params={"from": "pc", "fmt": "json", "pn": "0", "rn": str(CHART_TRACK_LIMIT),
                "type": "bang", "data": "content", "id": chart_id,
                "show_copyright_off": "0", "pcmp4": "1", "isbang": "1"},
        headers={"User-Agent": _UA_PC},
    )
    data = r.json() or {}
    tracks = []
    for item in (data.get("musiclist") or []):
        t = _kw_track(item)
        if t:
            tracks.append(t)
    return tracks


def _kw_track(item: dict) -> "dict | None":
    if not isinstance(item, dict):
        return None
    rid = str(item.get("id") or item.get("rid") or "").strip()
    title = str(item.get("name") or "").strip()
    if not rid or not title:
        return None
    duration = item.get("duration") or 0
    try:
        duration_s = float(duration)
    except (TypeError, ValueError):
        duration_s = 0.0
    return {
        "id": f"lx:kw:{rid}",
        "lx_source": "kw",
        "title": title,
        "artist": str(item.get("artist") or "").strip(),
        "album": str(item.get("album") or ""),
        "cover_url": "",
        "duration_s": duration_s,
        "ext": DEFAULT_LOSSLESS_EXT if _kw_lossless(item) else "mp3",
    }


# ------------------------------------------------------------------ 聚合入口

_FETCHERS = {
    "wy": (_wy_charts, _wy_chart_tracks),
    "kg": (_kg_charts, _kg_chart_tracks),
    "kw": (_kw_charts, _kw_chart_tracks),
}


def enabled_platforms() -> list[str]:
    raw = (os.environ.get("FNMUSIC_CHART_PLATFORMS") or "wy,kg,kw").strip()
    wanted = [p.strip().lower() for p in raw.split(",") if p.strip()]
    return [p for p in wanted if p in _FETCHERS]


async def list_charts(client: httpx.AsyncClient, platforms: list[str] | None = None) -> list[dict]:
    """并发拉取各平台榜单列表；单个平台失败不影响其它。"""
    platforms = platforms or enabled_platforms()
    cached = _cache_get("charts:" + ",".join(sorted(platforms)), CHART_TTL_S)
    if cached is not None:
        return cached

    async def one(platform: str) -> list[dict]:
        fetcher = _FETCHERS.get(platform)
        if not fetcher:
            return []
        try:
            charts = await asyncio.wait_for(fetcher[0](client), timeout=CHART_TIMEOUT_S)
        except Exception as e:  # noqa: BLE001
            logger.warning("chart list failed for %s: %s", platform, e)
            return []
        return [c for c in charts if _chart_enabled(platform, c["id"])]

    results = await asyncio.gather(*(one(p) for p in platforms))
    charts: list[dict] = []
    for group in results:
        charts.extend(group)

    if charts:
        _cache_put("charts:" + ",".join(sorted(platforms)), charts)
    return charts


async def chart_tracks(client: httpx.AsyncClient, platform: str, chart_id: str) -> list[dict]:
    """拉取指定榜单曲目。"""
    key = f"tracks:{platform}:{chart_id}"
    cached = _cache_get(key, CHART_TTL_S)
    if cached is not None:
        return cached

    fetcher = _FETCHERS.get(platform)
    if not fetcher:
        return []
    try:
        tracks = await asyncio.wait_for(fetcher[1](client, chart_id), timeout=CHART_TIMEOUT_S * 2)
    except Exception as e:  # noqa: BLE001
        logger.warning("chart tracks failed for %s/%s: %s", platform, chart_id, e)
        return []

    # 过滤明确不可播的条目（试听片段等）
    clean = [t for t in tracks if t.get("title") and t.get("id")]
    if clean:
        _cache_put(key, clean)
    return clean
