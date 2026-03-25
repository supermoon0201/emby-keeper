import asyncio

import embykeeper.emby.api as api_module
import pytest

from embykeeper.emby.api import Emby, EmbyConnectError, EmbyPlayError
from embykeeper.schema import EmbyAccount


class FakeResponse:
    def __init__(self, payload=None):
        self._payload = payload or {}

    def json(self):
        return self._payload


class DummyStreamTask:
    def __init__(self, coro):
        coro.close()

    def cancel(self):
        pass

    def __await__(self):
        async def _wait():
            raise asyncio.CancelledError

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

    with pytest.raises(EmbyPlayError, match="播放状态设定错误次数过多"):
        asyncio.run(emby.play({"Id": "item-id", "Name": "Demo"}, time=270))

    assert get_progress_calls() == 13
