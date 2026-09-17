"""洛雪歌单（平台排行榜）测试。

覆盖：
- GUID 构造/解析与平台校验
- 榜单曲目 -> 飞牛 track 结构转换
- 榜单曲目元数据登记（避免 unknown.mp3 / 空标题）
- 代理各歌单端点对榜单 GUID 的处理
- 平台接口失败时的降级（不影响官方歌单与每日推荐）

所有测试均使用 MockTransport，不访问外网。
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import importlib.util

import httpx
import pytest
from fastapi.testclient import TestClient

_HERE = os.path.dirname(os.path.abspath(__file__))
_PROXY = os.path.dirname(_HERE)


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, os.path.join(_PROXY, f"{name}.py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def _isolate_storage(app_mod, tmp_path, monkeypatch):
    """把落盘路径重定向到临时目录。

    榜单曲目会写歌词(.lrc)与媒体路径记录(.ref)；若不隔离，
    测试会污染真实的仓库 cache/ 与用户曲库目录。
    """
    cache_dir = str(tmp_path / "cache")
    lib_dir = str(tmp_path / "library")
    os.makedirs(cache_dir, exist_ok=True)
    os.makedirs(lib_dir, exist_ok=True)
    monkeypatch.setitem(app_mod.CONF, "cache_dir", cache_dir)
    monkeypatch.setitem(app_mod.CONF, "library_dir", lib_dir)
    monkeypatch.setenv("FNMUSIC_CACHE_DIR", cache_dir)
    monkeypatch.setenv("FNMUSIC_LIBRARY_DIR", lib_dir)
    return cache_dir, lib_dir


charts = _load("playlists")


class TestChartGuid:
    def test_build_and_parse(self):
        g = charts.chart_guid("wy", "3778678")
        assert g == "online:playlist:chart:wy:3778678"
        assert charts.is_chart_guid(g)
        assert charts.parse_chart_guid(g) == ("wy", "3778678")

    def test_rejects_non_chart(self):
        assert not charts.is_chart_guid("online:playlist:daily:20260917")
        assert not charts.is_chart_guid("online:lx:wy:123")
        assert charts.parse_chart_guid("online:playlist:daily:20260917") == (None, None)

    def test_rejects_unknown_platform(self):
        assert charts.parse_chart_guid("online:playlist:chart:zz:1") == (None, None)

    def test_rejects_missing_id(self):
        assert charts.parse_chart_guid("online:playlist:chart:wy") == (None, None)
        assert charts.parse_chart_guid("online:playlist:chart:wy:") == (None, None)

    def test_all_known_platforms(self):
        for p in ("wy", "kg", "kw", "mg"):
            assert charts.parse_chart_guid(charts.chart_guid(p, "1")) == (p, "1")


class TestChartName:
    def test_lookup_from_cache(self):
        charts._CACHE.clear()
        charts._cache_put("charts:wy,kg,kw", [
            {"platform": "wy", "id": "3778678", "name": "热歌榜"},
            {"platform": "kg", "id": "8888", "name": "TOP500"},
        ])
        assert charts.chart_name("wy", "3778678") == "热歌榜"
        assert charts.chart_name("kg", "8888") == "TOP500"
        assert charts.chart_name("wy", "nope") is None

    def test_empty_cache_returns_none(self):
        charts._CACHE.clear()
        assert charts.chart_name("wy", "3778678") is None


class TestChartCountBackfill:
    """酷狗/酷我列表接口不给歌曲数：拉到曲目后应回填，避免侧边栏长期显示 0。"""

    def test_backfills_cached_count(self):
        charts._CACHE.clear()
        charts._cache_put("charts:kg", [
            {"platform": "kg", "id": "8888", "name": "TOP500", "trackCount": 0},
        ])
        charts.remember_chart_count("kg", "8888", 100)
        assert charts._CACHE["charts:kg"]["value"][0]["trackCount"] == 100

    def test_ignores_invalid_count(self):
        charts._CACHE.clear()
        charts._cache_put("charts:kg", [{"platform": "kg", "id": "8888", "trackCount": 0}])
        for bad in (0, -5, None, "abc"):
            charts.remember_chart_count("kg", "8888", bad)
        assert charts._CACHE["charts:kg"]["value"][0]["trackCount"] == 0

    def test_no_cache_is_noop(self):
        charts._CACHE.clear()
        charts.remember_chart_count("kg", "8888", 50)  # 不应抛异常


class TestWyTrackMapping:
    def test_maps_fields(self):
        t = charts._wy_track({
            "id": 123, "name": "歌名",
            "ar": [{"name": "歌手A"}, {"name": "歌手B"}],
            "al": {"name": "专辑", "picUrl": "http://c/x.jpg"},
            "dt": 210000, "fee": 8,
        })
        assert t["id"] == "lx:wy:123"
        assert t["title"] == "歌名"
        assert t["artist"] == "歌手A、歌手B"
        assert t["album"] == "专辑"
        assert t["duration_s"] == 210.0
        assert t["cover_url"] == "http://c/x.jpg"

    def test_skips_invalid(self):
        assert charts._wy_track({}) is None
        assert charts._wy_track({"id": 1}) is None
        assert charts._wy_track({"name": "x"}) is None
        assert charts._wy_track(None) is None

    def test_accepts_legacy_artists_key(self):
        t = charts._wy_track({"id": 9, "name": "n", "artists": [{"name": "A"}], "duration": 1000})
        assert t["artist"] == "A"
        assert t["duration_s"] == 1.0


class TestKgTrackMapping:
    def test_artist_from_filename_when_singername_null(self):
        t = charts._kg_track({
            "hash": "ABC", "songname": "歌名", "singername": None,
            "filename": "歌手 - 歌名", "duration": 210,
        })
        assert t["id"] == "lx:kg:ABC"
        assert t["artist"] == "歌手"
        assert t["duration_s"] == 210.0

    def test_prefers_singername(self):
        t = charts._kg_track({"hash": "H", "songname": "n", "singername": "S", "filename": "X - n"})
        assert t["artist"] == "S"

    def test_skips_invalid(self):
        assert charts._kg_track({}) is None
        assert charts._kg_track({"hash": "H"}) is None
        assert charts._kg_track({"songname": "n"}) is None

    def test_millisecond_duration_normalized(self):
        t = charts._kg_track({"hash": "H", "songname": "n", "duration": 210000})
        assert t["duration_s"] == 210.0


class TestKwTrackMapping:
    def test_maps_fields(self):
        t = charts._kw_track({"id": "624683929", "name": "歌", "artist": "人", "album": "专", "duration": 49})
        assert t["id"] == "lx:kw:624683929"
        assert t["artist"] == "人"
        assert t["duration_s"] == 49.0

    def test_skips_invalid(self):
        assert charts._kw_track({}) is None
        assert charts._kw_track({"id": "1"}) is None

    def test_rid_alias(self):
        t = charts._kw_track({"rid": "9", "name": "n"})
        assert t["id"] == "lx:kw:9"


class TestLosslessFormatDetection:
    """榜单曲目默认按无损展示（flac），仅确认无无损源时才回退 mp3。"""

    def test_wy_lossless_when_sq_present(self):
        assert charts._wy_lossless({"sq": {"br": 900000, "size": 30000000}})
        assert charts._wy_lossless({"hr": {"br": 1700000, "size": 60000000}})

    def test_wy_not_lossless_without_sq(self):
        assert not charts._wy_lossless({"h": {"br": 320000}, "l": {"br": 128000}})
        assert not charts._wy_lossless({})

    def test_wy_track_ext_defaults_flac(self):
        t = charts._wy_track({"id": 1, "name": "n", "sq": {"br": 900000}})
        assert t["ext"] == "flac"

    def test_wy_track_ext_falls_back_mp3(self):
        t = charts._wy_track({"id": 1, "name": "n", "h": {"br": 320000}})
        assert t["ext"] == "mp3"

    def test_kg_lossless_when_sqhash_present(self):
        assert charts._kg_lossless({"sqhash": "ABCDEF"})
        assert not charts._kg_lossless({"sqhash": ""})
        assert not charts._kg_lossless({"hash": "ABC", "320hash": "DEF"})

    def test_kg_track_ext(self):
        assert charts._kg_track({"hash": "H", "songname": "n", "sqhash": "SQ"})["ext"] == "flac"
        assert charts._kg_track({"hash": "H", "songname": "n"})["ext"] == "mp3"

    def test_kw_lossless_from_formats(self):
        assert charts._kw_lossless({"formats": "WMA96|MP3128|ALFLAC|AAC48"})
        assert charts._kw_lossless({"formats": "DTSX|ZPLY"})
        assert not charts._kw_lossless({"formats": "WMA96|MP3128|AAC48"})

    def test_kw_track_ext(self):
        assert charts._kw_track({"id": "1", "name": "n", "formats": "ALFLAC"})["ext"] == "flac"
        assert charts._kw_track({"id": "1", "name": "n", "formats": "MP3128"})["ext"] == "mp3"

    def test_default_ext_is_flac(self):
        assert charts.DEFAULT_LOSSLESS_EXT == "flac"


class TestWhitelist:
    def test_filters_unlisted_chart(self):
        assert charts._chart_enabled("wy", "3778678")
        assert not charts._chart_enabled("wy", "99999999")
        assert not charts._chart_enabled("kg", "999999")
        assert charts._chart_enabled("kg", "8888")

    def test_unknown_platform_allows_all(self):
        assert charts._chart_enabled("zz", "anything")


def _mock_charts():
    """构造榜单接口的 MockTransport。"""
    def handler(req: httpx.Request) -> httpx.Response:
        url = str(req.url)
        if "toplist" in url:
            return httpx.Response(200, json={"code": 200, "list": [
                {"id": 3778678, "name": "热歌榜", "coverImgUrl": "http://c/a.jpg", "updateFrequency": "每天"},
                {"id": 99999999, "name": "不在白名单"},
            ]})
        if "playlist/detail" in url:
            return httpx.Response(200, json={"playlist": {
                "trackCount": 2, "tracks": [],
                "trackIds": [{"id": 111}, {"id": 222}],
            }})
        if "song/detail" in url:
            return httpx.Response(200, json={"songs": [
                {"id": 111, "name": "第一首", "ar": [{"name": "歌手一"}],
                 "al": {"name": "专辑一", "picUrl": "http://c/1.jpg"}, "dt": 200000, "fee": 0,
                 "sq": {"br": 900000, "size": 23000000}},
                {"id": 222, "name": "第二首", "ar": [{"name": "歌手二"}],
                 "al": {"name": "专辑二", "picUrl": ""}, "dt": 180000, "fee": 0,
                 "h": {"br": 320000, "size": 8000000}},
            ]})
        if "rank/list" in url:
            return httpx.Response(200, json={"data": {"info": [
                {"rankid": 8888, "rankname": "TOP500",
                 "bannerurl": "http://imge.kugou.com/mcommonbanner/{size}/2018/a.jpg"},
                {"rankid": 999999, "rankname": "不在白名单"},
            ]}})
        if "rank/song" in url:
            return httpx.Response(200, json={"data": {"info": [
                {"hash": "H1", "songname": "酷狗歌", "singername": "酷狗人", "duration": 200},
            ]}})
        if "kbangserver" in url:
            # 单曲请求（rn=1）用于取榜单元信息，带封面与数量。
            # 注意用精确匹配，避免把 rn=100 也误判成 rn=1。
            if re.search(r"[?&]rn=1(&|$)", url):
                return httpx.Response(200, json={
                    "name": "酷我热歌榜", "num": "300",
                    "v9_pic2": "http://img4.kuwo.cn/star/albumcover/120/x.jpg",
                    "musiclist": [],
                })
            return httpx.Response(200, json={"musiclist": [
                {"id": "555", "name": "酷我歌", "artist": "酷我人", "duration": 100},
            ]})
        return httpx.Response(404)

    return httpx.MockTransport(handler)


@pytest.fixture(autouse=True)
def _clear_cache():
    charts._CACHE.clear()
    yield
    charts._CACHE.clear()


class TestListCharts:
    def test_lists_and_filters_whitelist(self):
        import asyncio

        async def run():
            async with httpx.AsyncClient(transport=_mock_charts()) as c:
                return await charts.list_charts(c, ["wy"])

        got = asyncio.run(run())
        ids = [ch["id"] for ch in got]
        assert "3778678" in ids
        assert "99999999" not in ids
        assert all(ch["platform"] == "wy" for ch in got)

    def test_kugou_charts(self):
        import asyncio

        async def run():
            async with httpx.AsyncClient(transport=_mock_charts()) as c:
                return await charts.list_charts(c, ["kg"])

        got = asyncio.run(run())
        assert [ch["id"] for ch in got] == ["8888"]

    def test_platform_failure_isolated(self):
        """一个平台失败不影响其它平台。"""
        import asyncio

        def handler(req: httpx.Request) -> httpx.Response:
            if "toplist" in str(req.url):
                raise httpx.ConnectError("boom")
            if "rank/list" in str(req.url):
                return httpx.Response(200, json={"data": {"info": [
                    {"rankid": 8888, "rankname": "TOP500"},
                ]}})
            return httpx.Response(404)

        async def run():
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
                return await charts.list_charts(c, ["wy", "kg"])

        got = asyncio.run(run())
        assert [ch["id"] for ch in got] == ["8888"]

    def test_all_network_platforms_failing_returns_only_static(self):
        """网络全挂时：wy/kg 返空，kw 用内置榜单列表（其接口本就不提供榜单列表）。"""
        import asyncio

        def handler(req: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("down")

        async def run():
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
                return await charts.list_charts(c, ["wy", "kg", "kw"])

        got = asyncio.run(run())
        assert {ch["platform"] for ch in got} == {"kw"}
        assert all(ch["id"] in charts.CHART_WHITELIST["kw"] for ch in got)


class TestChartTracks:
    def test_wy_tracks_filled_via_song_detail(self):
        import asyncio

        async def run():
            async with httpx.AsyncClient(transport=_mock_charts()) as c:
                return await charts.chart_tracks(c, "wy", "3778678")

        got = asyncio.run(run())
        assert [t["title"] for t in got] == ["第一首", "第二首"]
        assert got[0]["artist"] == "歌手一"
        assert got[0]["id"] == "lx:wy:111"

    def test_kw_tracks(self):
        import asyncio

        async def run():
            async with httpx.AsyncClient(transport=_mock_charts()) as c:
                return await charts.chart_tracks(c, "kw", "16")

        got = asyncio.run(run())
        assert got and got[0]["id"] == "lx:kw:555"

    def test_failure_returns_empty(self):
        import asyncio

        def handler(req: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("down")

        async def run():
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
                return await charts.chart_tracks(c, "wy", "3778678")

        assert asyncio.run(run()) == []

    def test_unknown_platform_returns_empty(self):
        import asyncio

        async def run():
            async with httpx.AsyncClient(transport=_mock_charts()) as c:
                return await charts.chart_tracks(c, "zz", "1")

        assert asyncio.run(run()) == []


class TestProxyEndpoints:
    """代理侧端点：榜单 GUID 应被接管，且上游不可达也能工作。"""

    @pytest.fixture
    def client(self, monkeypatch, tmp_path):
        monkeypatch.setenv("FNMUSIC_CHARTS_ENABLED", "true")
        monkeypatch.setenv("FNMUSIC_UPSTREAM_SOCK", "/tmp/does-not-exist.sock")
        # app.py 的兜底分支用 `import recommend`（uvicorn --app-dir proxy），
        # 测试时需要把 proxy 目录放进 sys.path，否则重新加载会 ImportError。
        if _PROXY not in sys.path:
            sys.path.insert(0, _PROXY)
        app_mod = _load("app")
        app_mod.CONF["charts_enabled"] = True
        # 隔离落盘路径，避免污染真实 cache/ 与曲库
        _isolate_storage(app_mod, tmp_path, monkeypatch)
        app_mod.app.state.chart_client = httpx.AsyncClient(transport=_mock_charts())
        with TestClient(app_mod.app) as c:
            yield c

    def test_track_list_returns_chart_tracks_with_metadata(self, client):
        r = client.get("/music/api/v1/track/playlist-detail/list",
                       params={"playlistGUID": "online:playlist:chart:wy:3778678"})
        assert r.status_code == 200
        data = r.json()["data"]
        assert data["total"] == 2
        first = data["list"][0]
        assert first["title"] == "第一首"
        assert first["guid"] == "online:lx:wy:111"
        # 播放所需字段（该曲有 sq 无损源 → flac）
        assert first["audioSpec"]["format"] == "flac"
        assert first["artists"][0]["name"] == "歌手一"

    def test_playlist_detail_uses_real_chart_name(self, client):
        r = client.get("/music/api/v1/playlist/detail",
                       params={"guid": "online:playlist:chart:wy:3778678"})
        assert r.status_code == 200
        d = r.json()["data"]
        assert d["name"] == "热歌榜"
        assert d["trackCount"] == 2
        assert d["isChart"] is True

    def test_batch_detail_handles_chart_guids(self, client):
        r = client.get("/music/api/v1/playlist/batch-detail",
                       params={"guids": "online:playlist:chart:wy:3778678"})
        assert r.status_code == 200
        lst = r.json()["data"]["list"]
        assert len(lst) == 1
        assert lst[0]["name"] == "热歌榜"

    def test_chart_track_metadata_is_registered(self, client):
        """榜单曲目必须注册元数据，否则播放会写出 unknown.mp3。"""
        client.get("/music/api/v1/track/playlist-detail/list",
                   params={"playlistGUID": "online:playlist:chart:wy:3778678"})
        r = client.get("/music/api/v1/track/metadata", params={"guid": "online:lx:wy:111"})
        assert r.status_code == 200
        d = r.json()["data"]
        assert d["title"] == "第一首"
        assert d["artists"][0]["name"] == "歌手一"

    def test_chart_track_keeps_lossless_format_in_listing(self, client):
        """列表里的 format 应为无损（而不是一律 mp3）；确无无损源才回退 mp3。"""
        r = client.get("/music/api/v1/track/playlist-detail/list",
                       params={"playlistGUID": "online:playlist:chart:wy:3778678"})
        lst = r.json()["data"]["list"]
        first, second = lst[0], lst[1]
        # 第一首有 sq（无损）
        assert first["format"] == "flac"
        assert first["audioSpec"]["format"] == "flac"
        assert first["ext"] == "flac"
        # 第二首只有 h（有损）→ 回退 mp3
        assert second["format"] == "mp3"
        assert second["ext"] == "mp3"

    def test_chart_meta_does_not_short_circuit_lyric(self, client, monkeypatch):
        """榜单曲目不得因为元数据短路而丢失歌词。

        历史 bug：_fetch_online_info 对榜单曲目直接返回 chart_meta（lyric=""），
        导致永远查不到歌词，表现为「有封面没歌词」。
        """
        app_mod = sys.modules["app"]

        async def _fake_lyric(request, guid):
            return "[00:01.00] 测试歌词"

        monkeypatch.setattr(app_mod, "_fetch_lx_lyric", _fake_lyric)
        # 先加载榜单，登记元数据
        client.get("/music/api/v1/track/playlist-detail/list",
                   params={"playlistGUID": "online:playlist:chart:wy:3778678"})
        # 榜单曲目必须仍能拿到歌词
        r = client.get("/music/api/v1/track/lyrics", params={"guid": "online:lx:wy:111"})
        assert r.status_code == 200
        assert "测试歌词" in json.dumps(r.json(), ensure_ascii=False)

    def test_chart_meta_carries_lyric_from_lx(self, client, monkeypatch):
        """_fetch_online_info 应把 lx 歌词并入榜单元数据。"""
        app_mod = sys.modules["app"]

        async def _fake_lyric(request, guid):
            return "[00:02.00] merged"

        monkeypatch.setattr(app_mod, "_fetch_lx_lyric", _fake_lyric)
        client.get("/music/api/v1/track/playlist-detail/list",
                   params={"playlistGUID": "online:playlist:chart:wy:3778678"})

        class _Req:
            def __init__(self):
                self.app = app_mod.app

        info = asyncio.run(app_mod._fetch_online_info(_Req(), "online:lx:wy:111"))
        assert info is not None
        assert info["title"] == "第一首"
        assert "merged" in (info.get("lyric") or "")

    def test_disabled_charts_does_not_inject(self, monkeypatch):
        monkeypatch.setenv("FNMUSIC_CHARTS_ENABLED", "false")
        app_mod = _load("app")
        assert app_mod.CONF["charts_enabled"] is False

    def test_non_chart_guid_still_forwarded(self, client):
        """普通 GUID 仍应透传上游（此处上游不可达 → 抛连接错误，证明走了透传）。"""
        with pytest.raises(Exception):
            client.get("/music/api/v1/playlist/detail", params={"guid": "some-local-guid"})


class TestChartCovers:
    """侧边栏歌单封面。

    飞牛用 coverId（=歌单 guid）请求 /static/cover；榜单封面不来自在线曲目，
    必须单独登记，否则侧边栏歌单没有封面。
    """

    @pytest.fixture
    def client(self, monkeypatch, tmp_path):
        monkeypatch.setenv("FNMUSIC_CHARTS_ENABLED", "true")
        monkeypatch.setenv("FNMUSIC_UPSTREAM_SOCK", "/tmp/does-not-exist.sock")
        if _PROXY not in sys.path:
            sys.path.insert(0, _PROXY)
        app_mod = _load("app")
        app_mod.CONF["charts_enabled"] = True
        _isolate_storage(app_mod, tmp_path, monkeypatch)
        app_mod._CHART_COVERS.clear()
        # /playlist/list 需要上游返回歌单信封 + 鉴权通过
        async def _envelope(request, client):
            return {"code": 0, "msg": "ok", "data": {"list": [], "total": 0}}

        async def _auth(request, client):
            return True, "u", None

        app_mod.fetch_upstream_envelope = _envelope
        app_mod._probe_upstream_auth = _auth
        app_mod.app.state.chart_client = httpx.AsyncClient(transport=_mock_charts())
        with TestClient(app_mod.app) as c:
            yield c

    def test_normalize_cover_replaces_size_placeholder(self):
        assert charts._normalize_cover("http://a/{size}/b.jpg") == f"http://a/{charts.COVER_IMAGE_SIZE}/b.jpg"

    def test_normalize_cover_rejects_invalid(self):
        assert charts._normalize_cover("") == ""
        assert charts._normalize_cover("ftp://a/b.jpg") == ""
        assert charts._normalize_cover(None) == ""

    def test_kugou_cover_placeholder_resolved(self):
        import asyncio

        async def run():
            async with httpx.AsyncClient(transport=_mock_charts()) as c:
                return await charts.list_charts(c, ["kg"])

        got = asyncio.run(run())
        assert got, "应有酷狗榜单"
        assert "{size}" not in got[0]["cover"], "占位符必须被替换"
        assert got[0]["cover"].startswith("http://")

    def test_kw_chart_meta_fills_name_count_cover(self):
        import asyncio

        async def run():
            async with httpx.AsyncClient(transport=_mock_charts()) as c:
                return await charts.list_charts(c, ["kw"])

        got = asyncio.run(run())
        assert got
        ch = got[0]
        assert ch["name"] == "酷我热歌榜"
        assert ch["trackCount"] == 300
        assert ch["cover"].startswith("http://")

    def test_list_injects_and_registers_covers(self, client):
        client.get("/music/api/v1/playlist/list")
        app_mod = sys.modules["app"]
        cover = app_mod.chart_cover_for("online:playlist:chart:wy:3778678")
        assert cover.startswith("http://")

    def test_static_cover_redirects_for_chart(self, client):
        client.get("/music/api/v1/playlist/list")
        r = client.get("/music/api/v1/static/cover",
                       params={"guid": "online:playlist:chart:wy:3778678"},
                       follow_redirects=False)
        assert r.status_code == 302
        assert r.headers["location"].startswith("http")

    def test_static_cover_works_when_cache_is_cold(self, client):
        """代理刚重启（无缓存）时直接请求封面也应成功。"""
        app_mod = sys.modules["app"]
        app_mod._CHART_COVERS.clear()
        app_mod._CHART_TRACK_CACHE.clear()
        r = client.get("/music/api/v1/static/cover",
                       params={"guid": "online:playlist:chart:kw:16"},
                       follow_redirects=False)
        assert r.status_code == 302

    def test_static_cover_path_form(self, client):
        client.get("/music/api/v1/playlist/list")
        r = client.get("/music/api/v1/static/cover/online:playlist:chart:kg:8888",
                       follow_redirects=False)
        assert r.status_code == 302

    def test_static_cover_head_method(self, client):
        client.get("/music/api/v1/playlist/list")
        r = client.head("/music/api/v1/static/cover",
                        params={"guid": "online:playlist:chart:wy:3778678"},
                        follow_redirects=False)
        assert r.status_code == 302

    def test_invalid_platform_chart_cover_404(self, client):
        r = client.get("/music/api/v1/static/cover",
                       params={"guid": "online:playlist:chart:zz:1"},
                       follow_redirects=False)
        assert r.status_code == 404
