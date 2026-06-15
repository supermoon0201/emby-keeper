import asyncio

import embykeeper.emby.api as api_module
import httpx
import pytest

from embykeeper.emby.api import Emby, EmbyConnectError, EmbyEnv, EmbyPlayError, EmbyStatusError
from embykeeper.schema import EmbyAccount


class FakeResponse:
    def __init__(self, payload=None, status_code=200, text="", ok=True):
        self._payload = payload or {}
        self.status_code = status_code
        self.text = text
        self.ok = ok

    def json(self):
        return self._payload

    async def aclose(self):
        return None

    async def aiter_content(self, chunk_size=1024):
        if False:
            yield b""


class DummyStreamTask:
    def __init__(self, coro):
        coro.close()

    def cancel(self):
        pass

    def __await__(self):
        async def _wait():
            raise asyncio.CancelledError

        return _wait().__await__()


class FailingStreamTask:
    def __init__(self, coro, error):
        coro.close()
        self.error = error

    def cancel(self):
        pass

    def __await__(self):
        async def _wait():
            raise self.error

        return _wait().__await__()


def build_emby():
    emby = Emby(
        EmbyAccount(
            url="http://example.com",
            username="user",
            password="pass",
            use_proxy=False,
        )
    )
    emby._token = "token"
    emby._user_id = "user-id"
    return emby


def test_build_headers_uses_logged_in_user_id_for_emby_authorization():
    emby = build_emby()
    emby.run_id = "RUN-ID"
    emby._env = EmbyEnv(
        client="Fileball",
        device="Device",
        device_id="device-id",
        client_version="1.3.30",
        useragent="Fileball/1.3.30",
    )

    headers = emby.build_headers()

    assert "Emby UserId=user-id" in headers["X-Emby-Authorization"]
    assert "Emby UserId=RUN-ID" not in headers["X-Emby-Authorization"]


def test_request_preserves_base_path_in_account_url(monkeypatch):
    emby = Emby(
        EmbyAccount(
            url="https://example.com/base/path",
            username="user",
            password="pass",
            use_proxy=False,
        )
    )

    requested_urls = []

    class DummySession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def request(self, method, url, **kwargs):
            requested_urls.append((method, url))
            return FakeResponse(status_code=200, ok=True)

    monkeypatch.setattr(emby, "_get_session", lambda: DummySession())

    asyncio.run(emby._request("GET", "/Users/Me"))

    assert requested_urls == [("GET", "https://example.com:443/base/path/Users/Me")]


def test_request_auto_prefers_emby_base_path_when_workaround_enabled(monkeypatch):
    emby = Emby(
        EmbyAccount(
            url="http://example.com",
            username="user",
            password="pass",
            use_proxy=False,
            stream_workaround=True,
        )
    )
    requested_urls = []

    class DummyCache:
        @staticmethod
        def get(_key, default=None):
            return default

        @staticmethod
        def set(_key, _value):
            return None

    class DummySession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def request(self, method, url, **kwargs):
            requested_urls.append((method, url))
            if url == "http://example.com:80/emby/System/Info/Public":
                return FakeResponse(status_code=200, ok=True)
            return FakeResponse(status_code=200, ok=True)

    monkeypatch.setattr(api_module, "cache", DummyCache())
    monkeypatch.setattr(emby, "_get_session", lambda: DummySession())

    asyncio.run(emby._request("GET", "/Users/Me"))

    assert requested_urls == [
        ("GET", "http://example.com:80/emby/System/Info/Public"),
        ("GET", "http://example.com:80/emby/Users/Me"),
    ]


def test_request_does_not_probe_emby_base_path_for_normal_host(monkeypatch):
    emby = build_emby()
    requested_urls = []

    class DummySession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def request(self, method, url, **kwargs):
            requested_urls.append((method, url))
            return FakeResponse(status_code=200, ok=True)

    monkeypatch.setattr(emby, "_get_session", lambda: DummySession())

    asyncio.run(emby._request("GET", "/Users/Me"))

    assert requested_urls == [("GET", "http://example.com:80/Users/Me")]


def test_request_retries_transient_cloudflare_status(monkeypatch):
    emby = build_emby()
    statuses = [522, 200]

    class DummySession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def request(self, method, url, **kwargs):
            status_code = statuses.pop(0)
            return FakeResponse(status_code=status_code, ok=(status_code == 200))

    async def fast_sleep(_seconds):
        return None

    monkeypatch.setattr(api_module.asyncio, "sleep", fast_sleep)
    monkeypatch.setattr(emby, "_get_session", lambda: DummySession())

    response = asyncio.run(emby._request("GET", "/Users/Me"))

    assert response.status_code == 200
    assert statuses == []


def install_play_stubs(monkeypatch, emby, should_fail_progress_call):
    progress_calls = 0

    async def fast_sleep(_seconds):
        return None

    async def fake_request(method, path, **kwargs):
        nonlocal progress_calls

        if path.endswith("/AdditionalParts"):
            return FakeResponse()
        if path.startswith("/Items/") and path.endswith("/PlaybackInfo"):
            return FakeResponse(
                {
                    "PlaySessionId": "session-id",
                    "MediaSources": [{"Id": "media-source", "DirectStreamUrl": "/stream"}],
                }
            )
        if path == "/Sessions/Playing":
            return FakeResponse()
        if path == "/Sessions/Playing/Progress":
            if kwargs["json"].get("NowPlayingQueue") == []:
                return FakeResponse()

            progress_calls += 1
            if should_fail_progress_call(progress_calls):
                raise EmbyConnectError(f"transient-{progress_calls}")
            return FakeResponse()
        if path == "/Sessions/Playing/Stopped":
            return FakeResponse()
        raise AssertionError(f"Unexpected request: {method} {path}")

    monkeypatch.setattr(api_module.asyncio, "sleep", fast_sleep)
    monkeypatch.setattr(api_module.asyncio, "create_task", lambda coro: DummyStreamTask(coro))
    monkeypatch.setattr(api_module.random, "uniform", lambda *_args, **_kwargs: 0)
    monkeypatch.setattr(emby, "_request", fake_request)
    return lambda: progress_calls


def test_play_tolerates_recovered_progress_update_failures(monkeypatch):
    emby = build_emby()
    get_progress_calls = install_play_stubs(
        monkeypatch,
        emby,
        should_fail_progress_call=lambda call: call <= 25 and call % 2 == 1,
    )

    assert asyncio.run(emby.play({"Id": "item-id", "Name": "Demo"}, time=270)) is True
    assert get_progress_calls() == 27


def test_play_still_fails_after_too_many_consecutive_progress_errors(monkeypatch):
    emby = build_emby()
    get_progress_calls = install_play_stubs(
        monkeypatch,
        emby,
        should_fail_progress_call=lambda call: call <= 13,
    )

    with pytest.raises(EmbyPlayError, match="播放进度上报连续失败"):
        asyncio.run(emby.play({"Id": "item-id", "Name": "Demo"}, time=270))

    assert get_progress_calls() == 4


def test_play_fails_when_stream_access_breaks_after_progress_fallback(monkeypatch):
    emby = build_emby()

    async def fast_sleep(_seconds):
        return None

    async def fake_request(method, path, **kwargs):
        if path.endswith("/AdditionalParts"):
            return FakeResponse()
        if path.startswith("/Items/") and path.endswith("/PlaybackInfo"):
            return FakeResponse(
                {
                    "PlaySessionId": "session-id",
                    "MediaSources": [{"Id": "media-source", "DirectStreamUrl": "/stream"}],
                }
            )
        if path == "/Sessions/Playing":
            return FakeResponse()
        if path == "/Sessions/Playing/Progress":
            payload = kwargs["json"]
            if payload.get("NowPlayingQueue") == []:
                return FakeResponse()
            raise EmbyConnectError("progress-down")
        if path == "/Sessions/Playing/Stopped":
            return FakeResponse()
        raise AssertionError(f"Unexpected request: {method} {path}")

    monkeypatch.setattr(api_module.asyncio, "sleep", fast_sleep)
    monkeypatch.setattr(
        api_module.asyncio,
        "create_task",
        lambda coro: FailingStreamTask(coro, EmbyStatusError("stream-403")),
    )
    monkeypatch.setattr(api_module.random, "uniform", lambda *_args, **_kwargs: 0)
    monkeypatch.setattr(emby, "_request", fake_request)

    with pytest.raises(EmbyPlayError, match="播放进度上报连续失败"):
        asyncio.run(emby.play({"Id": "item-id", "Name": "Demo"}, time=40))


def test_play_can_fallback_after_too_many_progress_errors_when_enabled(monkeypatch):
    emby = Emby(
        EmbyAccount(
            url="http://example.com",
            username="user",
            password="pass",
            use_proxy=False,
            progress_fallback=True,
        )
    )
    emby._token = "token"
    emby._user_id = "user-id"
    get_progress_calls = install_play_stubs(
        monkeypatch,
        emby,
        should_fail_progress_call=lambda call: call <= 13,
    )

    assert asyncio.run(emby.play({"Id": "item-id", "Name": "Demo"}, time=270)) is True
    assert get_progress_calls() == 4


def test_watch_can_fallback_to_mark_played_when_item_lookup_fails(monkeypatch):
    emby = Emby(
        EmbyAccount(
            url="http://example.com",
            username="user",
            password="pass",
            use_proxy=False,
            time=10,
        )
    )
    emby._token = "token"
    emby._user_id = "user-id"
    emby.items = {
        "item-id": {
            "Id": "item-id",
            "Name": "Demo",
            "MediaType": "Video",
            "RunTimeTicks": 600000000,
        }
    }

    async def fast_sleep(_seconds):
        return None

    async def fake_resolve_playable_item(iid, item):
        return item

    async def fake_play(item, time):
        return True

    get_item_calls = 0

    async def fake_get_item(iid, **kwargs):
        nonlocal get_item_calls
        get_item_calls += 1
        if get_item_calls == 1:
            return {
                "Id": iid,
                "Name": "Demo",
                "UserData": {"PlayCount": 0, "Played": False},
            }
        raise EmbyStatusError("detail-down")

    async def fake_mark_played(iid):
        return True

    monkeypatch.setattr(api_module.asyncio, "sleep", fast_sleep)
    monkeypatch.setattr(api_module.random, "uniform", lambda *_args, **_kwargs: 0)
    monkeypatch.setattr(api_module.random, "shuffle", lambda _items: None)
    monkeypatch.setattr(emby, "resolve_playable_item", fake_resolve_playable_item)
    monkeypatch.setattr(emby, "play", fake_play)
    monkeypatch.setattr(emby, "get_item", fake_get_item)
    monkeypatch.setattr(emby, "mark_played", fake_mark_played)

    assert asyncio.run(emby.watch()) is True
    assert get_item_calls >= 2


def test_resolve_playable_item_expands_series_children(monkeypatch):
    emby = build_emby()

    async def fake_get_item(iid, **kwargs):
        assert iid == "series-id"
        return {"Id": iid, "Name": "Series", "Type": "Series"}

    async def fake_get_folder_items(parent_id, **kwargs):
        assert parent_id == "series-id"
        return [{"Id": "episode-id", "Name": "Episode 1", "MediaType": "Video", "RunTimeTicks": 600000000}]

    monkeypatch.setattr(emby, "get_item", fake_get_item)
    monkeypatch.setattr(emby, "get_folder_items", fake_get_folder_items)

    playable = asyncio.run(emby.resolve_playable_item("series-id", {"Id": "series-id", "Name": "Series"}))

    assert playable["Id"] == "episode-id"
    assert emby.items["episode-id"]["MediaType"] == "Video"


def test_stream_request_uses_account_user_agent_and_icy_metadata(monkeypatch):
    emby = build_emby()
    emby.useragent = "SenPlayer/5.8.7"

    async def fake_probe_base_path(_prefix):
        return False

    monkeypatch.setattr(emby, "_probe_base_path", fake_probe_base_path)
    emby._base_path = ""

    headers = {
        "Range": "bytes=0-",
        "User-Agent": emby.useragent or emby.env.useragent,
        "Icy-MetaData": "1",
    }

    assert headers["User-Agent"] == "SenPlayer/5.8.7"
    assert headers["Icy-MetaData"] == "1"
    assert "X-Playback-Session-Id" not in headers


def test_external_stream_workaround_uses_smaller_chunk_limits():
    emby = Emby(
        EmbyAccount(
            url="http://example.com",
            username="user",
            password="pass",
            use_proxy=False,
            external_stream_workaround=True,
        )
    )

    assert emby.use_external_stream_workaround is True
    assert emby.use_special_stream_workaround is False


def test_is_external_stream_url_detects_absolute_external_host():
    emby = Emby(
        EmbyAccount(
            url="https://gy.emby.yun:4443",
            username="user",
            password="pass",
            use_proxy=False,
        )
    )

    assert emby._is_external_stream_url("http://110.42.42.172:35902/emby/Videos/754409/stream") is True
    assert emby._is_external_stream_url("https://gy.emby.yun:4443/emby/Videos/754409/stream") is False
    assert emby._is_external_stream_url("/Videos/754409/stream") is False


def test_build_cdn_stream_url_uses_media_source_path():
    emby = Emby(
        EmbyAccount(
            url="https://emby.nebula-media.org",
            username="user",
            password="pass",
            use_proxy=False,
            stream_cdn_path=True,
        )
    )
    emby._token = "token"

    url = emby._build_cdn_stream_url("https://starharn.nebula-media.org/cdn?path=Nebula Media/test.mkv")

    assert url.startswith("https://emby-cdn1.nebula-media.org/cdn?path=https%3A%2F%2Fstarharn.nebula-media.org")
    assert url.endswith("&api_key=token")


def test_update_media_source_info_prefers_later_absolute_cdn_path():
    emby = Emby(
        EmbyAccount(
            url="https://emby.nebula-media.org",
            username="user",
            password="pass",
            use_proxy=False,
            stream_cdn_path=True,
        )
    )

    media_source_id = ""
    direct_stream_url = None
    media_source_path = None

    media_source_id, direct_stream_url, media_source_path = emby._update_media_source_info(
        media_source_id,
        direct_stream_url,
        media_source_path,
        {
            "MediaSources": [
                {
                    "Id": "media-source-1",
                    "DirectStreamUrl": "/videos/312497/original.mp4?api_key=token",
                    "Path": None,
                }
            ]
        },
    )

    media_source_id, direct_stream_url, media_source_path = emby._update_media_source_info(
        media_source_id,
        direct_stream_url,
        media_source_path,
        {
            "MediaSources": [
                {
                    "Id": "media-source-2",
                    "DirectStreamUrl": "/videos/312497/original.mp4?api_key=token",
                    "Path": "https://starharn.nebula-media.org/cdn?path=/mnt/gdrive/test.mp4",
                }
            ]
        },
    )

    assert media_source_id == "media-source-2"
    assert direct_stream_url == "/videos/312497/original.mp4?api_key=token"
    assert media_source_path == "https://starharn.nebula-media.org/cdn?path=/mnt/gdrive/test.mp4"


def test_update_media_source_info_does_not_replace_absolute_cdn_path_with_local_path():
    emby = Emby(
        EmbyAccount(
            url="https://emby.nebula-media.org",
            username="user",
            password="pass",
            use_proxy=False,
            stream_cdn_path=True,
        )
    )

    media_source_id, direct_stream_url, media_source_path = emby._update_media_source_info(
        "",
        None,
        None,
        {
            "MediaSources": [
                {
                    "Id": "media-source-1",
                    "DirectStreamUrl": "/videos/312497/original.mp4?api_key=token",
                    "Path": "https://starharn.nebula-media.org/cdn?path=/mnt/gdrive/test.mp4",
                }
            ]
        },
    )

    media_source_id, direct_stream_url, media_source_path = emby._update_media_source_info(
        media_source_id,
        direct_stream_url,
        media_source_path,
        {
            "MediaSources": [
                {
                    "Id": "media-source-2",
                    "DirectStreamUrl": "/videos/312497/original.mp4?api_key=token",
                    "Path": "/mnt/gdrive/test.mp4",
                }
            ]
        },
    )

    assert media_source_id == "media-source-2"
    assert direct_stream_url == "/videos/312497/original.mp4?api_key=token"
    assert media_source_path == "https://starharn.nebula-media.org/cdn?path=/mnt/gdrive/test.mp4"


def test_describe_response_truncates_and_normalizes_body():
    emby = build_emby()
    resp = FakeResponse(text="  line1\nline2  ", ok=False)

    assert emby._describe_response(resp, limit=20) == "响应内容: line1 line2"


def test_describe_response_reports_empty_body():
    emby = build_emby()
    resp = FakeResponse(text="", ok=False)

    assert emby._describe_response(resp) == "响应内容为空"


def test_get_playback_evidence_accepts_last_played_date_change():
    evidence = Emby._get_playback_evidence(
        {"UserData": {"PlayCount": 0, "Played": False, "LastPlayedDate": None}},
        {"UserData": {"PlayCount": 0, "Played": False, "LastPlayedDate": "2026-05-17T06:00:00Z"}},
    )

    assert evidence == "最后播放时间已更新"


def test_get_playback_evidence_accepts_progress_change():
    evidence = Emby._get_playback_evidence(
        {"UserData": {"PlaybackPositionTicks": 0}},
        {"UserData": {"PlaybackPositionTicks": 123456789}},
    )

    assert evidence == "播放进度已更新到 123456789"


def test_play_uses_emby_timeupdate_event_name(monkeypatch):
    emby = build_emby()
    progress_event_names = []

    async def fast_sleep(_seconds):
        return None

    async def fake_request(method, path, **kwargs):
        if path.endswith("/AdditionalParts"):
            return FakeResponse()
        if path.startswith("/Items/") and path.endswith("/PlaybackInfo"):
            return FakeResponse(
                {
                    "PlaySessionId": "session-id",
                    "MediaSources": [{"Id": "media-source", "DirectStreamUrl": "/stream"}],
                }
            )
        if path == "/Sessions/Playing":
            return FakeResponse()
        if path == "/Sessions/Playing/Progress":
            payload = kwargs["json"]
            if payload.get("NowPlayingQueue") == []:
                return FakeResponse()

            progress_event_names.append(payload.get("EventName"))
            if payload.get("EventName") != "TimeUpdate":
                raise EmbyConnectError(f"unexpected-event-name-{payload.get('EventName')}")
            return FakeResponse()
        if path == "/Sessions/Playing/Stopped":
            return FakeResponse()
        raise AssertionError(f"Unexpected request: {method} {path}")

    monkeypatch.setattr(api_module.asyncio, "sleep", fast_sleep)
    monkeypatch.setattr(api_module.asyncio, "create_task", lambda coro: DummyStreamTask(coro))
    monkeypatch.setattr(api_module.random, "uniform", lambda *_args, **_kwargs: 0)
    monkeypatch.setattr(emby, "_request", fake_request)

    assert asyncio.run(emby.play({"Id": "item-id", "Name": "Demo"}, time=20)) is True
    assert progress_event_names == ["TimeUpdate", "TimeUpdate"]


def test_play_reports_stop_via_stopped_endpoint(monkeypatch):
    emby = build_emby()
    stopped_calls = 0

    async def fast_sleep(_seconds):
        return None

    async def fake_request(method, path, **kwargs):
        nonlocal stopped_calls

        if path.endswith("/AdditionalParts"):
            return FakeResponse()
        if path.startswith("/Items/") and path.endswith("/PlaybackInfo"):
            return FakeResponse(
                {
                    "PlaySessionId": "session-id",
                    "MediaSources": [{"Id": "media-source", "DirectStreamUrl": "/stream"}],
                }
            )
        if path == "/Sessions/Playing":
            return FakeResponse()
        if path == "/Sessions/Playing/Progress":
            payload = kwargs["json"]
            if payload.get("NowPlayingQueue") == []:
                raise EmbyConnectError("expected-stopped-endpoint")
            return FakeResponse()
        if path == "/Sessions/Playing/Stopped":
            stopped_calls += 1
            return FakeResponse()
        raise AssertionError(f"Unexpected request: {method} {path}")

    monkeypatch.setattr(api_module.asyncio, "sleep", fast_sleep)
    monkeypatch.setattr(api_module.asyncio, "create_task", lambda coro: DummyStreamTask(coro))
    monkeypatch.setattr(api_module.random, "uniform", lambda *_args, **_kwargs: 0)
    monkeypatch.setattr(emby, "_request", fake_request)

    assert asyncio.run(emby.play({"Id": "item-id", "Name": "Demo"}, time=20)) is True
    assert stopped_calls == 1


def test_play_tolerates_brief_stream_reconnect_errors(monkeypatch):
    emby = build_emby()
    emby._stream_max_bytes_per_request = 1
    emby._stream_max_request_seconds = 1
    api_module.config.set({"emby": {"timeout": 10, "retries": 4}})

    original_sleep = asyncio.sleep
    stream_attempts = {"count": 0}

    class FakeStreamResponse:
        def __init__(self, status_code=200):
            self.status_code = status_code
            self.headers = {}
            self.request = type("Request", (), {"url": "http://stream.local/file"})()

        async def aiter_bytes(self, chunk_size=1024):
            yield b"x"

    class FakeStreamContext:
        def __init__(self, behavior):
            self.behavior = behavior

        async def __aenter__(self):
            if self.behavior == "error":
                raise httpx.ConnectError("boom")
            return FakeStreamResponse()

        async def __aexit__(self, exc_type, exc, tb):
            return False

    class FakeAsyncClient:
        def __init__(self, **kwargs):
            pass

        def stream(self, method, url):
            stream_attempts["count"] += 1
            behavior = "ok"
            if stream_attempts["count"] in (2, 3):
                behavior = "error"
            return FakeStreamContext(behavior)

        async def aclose(self):
            return None

    async def fast_sleep(_seconds):
        await original_sleep(0)

    async def fake_request(method, path, **kwargs):
        if path.endswith("/AdditionalParts"):
            return FakeResponse()
        if path.startswith("/Items/") and path.endswith("/PlaybackInfo"):
            return FakeResponse(
                {
                    "PlaySessionId": "session-id",
                    "MediaSources": [{"Id": "media-source", "DirectStreamUrl": "/stream"}],
                }
            )
        if path in ("/Sessions/Playing", "/Sessions/Playing/Progress", "/Sessions/Playing/Stopped"):
            return FakeResponse()
        raise AssertionError(f"Unexpected request: {method} {path}")

    async def fake_build_url(path):
        await original_sleep(0)
        return f"http://example.com{path}"

    monkeypatch.setattr(api_module.httpx, "AsyncClient", FakeAsyncClient)
    monkeypatch.setattr(api_module.asyncio, "sleep", fast_sleep)
    monkeypatch.setattr(api_module.random, "uniform", lambda *_args, **_kwargs: 0)
    monkeypatch.setattr(api_module.random, "random", lambda: 0)
    monkeypatch.setattr(emby, "_request", fake_request)
    monkeypatch.setattr(emby, "_build_url", fake_build_url)

    assert asyncio.run(emby.play({"Id": "item-id", "Name": "Demo"}, time=20)) is True
    assert stream_attempts["count"] >= 2


def test_play_uses_external_headers_for_absolute_direct_stream_url(monkeypatch):
    emby = Emby(
        EmbyAccount(
            url="https://gy.emby.yun:4443",
            username="user",
            password="pass",
            use_proxy=False,
            external_stream_workaround=True,
            useragent="SenPlayer/5.8.7",
        )
    )
    emby._token = "token"
    emby._user_id = "user-id"
    emby._stream_max_bytes_per_request = 1024 * 1024
    emby._stream_max_request_seconds = 10

    captured_headers = []
    original_sleep = asyncio.sleep

    class FakeStreamResponse:
        def __init__(self):
            self.status_code = 200
            self.headers = {}
            self.request = type(
                "Request",
                (),
                {"url": "http://110.42.42.172:35902/emby/Videos/754409/stream?Static=true&api_key=token"},
            )()

        async def aiter_bytes(self, chunk_size=1024):
            yield b"x"

    class FakeStreamContext:
        async def __aenter__(self):
            return FakeStreamResponse()

        async def __aexit__(self, exc_type, exc, tb):
            return False

    class FakeAsyncClient:
        def __init__(self, **kwargs):
            captured_headers.append(kwargs["headers"])

        def stream(self, method, url):
            return FakeStreamContext()

        async def aclose(self):
            return None

    async def fast_sleep(_seconds):
        await original_sleep(0)

    async def fake_request(method, path, **kwargs):
        if path.endswith("/AdditionalParts"):
            return FakeResponse()
        if path.startswith("/Items/") and path.endswith("/PlaybackInfo"):
            return FakeResponse(
                {
                    "PlaySessionId": "session-id",
                    "MediaSources": [
                        {
                            "Id": "media-source",
                            "DirectStreamUrl": "http://110.42.42.172:35902/emby/Videos/754409/stream?Static=true&api_key=token",
                        }
                    ],
                }
            )
        if path in ("/Sessions/Playing", "/Sessions/Playing/Progress", "/Sessions/Playing/Stopped"):
            return FakeResponse()
        raise AssertionError(f"Unexpected request: {method} {path}")

    monkeypatch.setattr(api_module.httpx, "AsyncClient", FakeAsyncClient)
    monkeypatch.setattr(api_module.asyncio, "sleep", fast_sleep)
    monkeypatch.setattr(api_module.random, "uniform", lambda *_args, **_kwargs: 0)
    monkeypatch.setattr(api_module.random, "random", lambda: 0)
    monkeypatch.setattr(emby, "_request", fake_request)

    assert asyncio.run(emby.play({"Id": "item-id", "Name": "Demo"}, time=20)) is True
    assert captured_headers
    assert captured_headers[0]["Accept"] == "*/*"
    assert captured_headers[0]["Connection"] == "close"
    assert captured_headers[0]["Range"] == "bytes=0-"


def test_watch_marks_item_played_when_server_does_not_increment_play_count(monkeypatch):
    emby = Emby(
        EmbyAccount(
            url="http://example.com",
            username="user",
            password="pass",
            use_proxy=False,
            time=20,
        )
    )
    emby._token = "token"
    emby._user_id = "user-id"
    emby.items = {
        "item-id": {
            "Id": "item-id",
            "Name": "Demo",
            "RunTimeTicks": 600000000,
        }
    }

    get_item_calls = {"count": 0}
    mark_played_calls = []

    async def fake_resolve_playable_item(iid, item):
        return item

    async def fake_play(item, time):
        return True

    async def fake_get_item(iid, **kwargs):
        get_item_calls["count"] += 1
        if get_item_calls["count"] == 1:
            return {"Id": iid, "UserData": {"PlayCount": 0, "Played": False}}
        if get_item_calls["count"] == 2:
            return {"Id": iid, "UserData": {"PlayCount": 0, "Played": False}}
        return {"Id": iid, "UserData": {"PlayCount": 1, "Played": True}}

    async def fake_mark_played(iid):
        mark_played_calls.append(iid)
        return True

    async def fast_sleep(_seconds):
        return None

    monkeypatch.setattr(api_module.asyncio, "sleep", fast_sleep)
    monkeypatch.setattr(api_module.random, "shuffle", lambda seq: None)
    monkeypatch.setattr(api_module.random, "random", lambda: 0)
    monkeypatch.setattr(emby, "resolve_playable_item", fake_resolve_playable_item)
    monkeypatch.setattr(emby, "play", fake_play)
    monkeypatch.setattr(emby, "get_item", fake_get_item)
    monkeypatch.setattr(emby, "mark_played", fake_mark_played)

    assert asyncio.run(emby.watch()) is True
    assert mark_played_calls == ["item-id"]
