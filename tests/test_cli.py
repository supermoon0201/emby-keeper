import os
import asyncio
import sys
import types
from pathlib import Path
from typer.testing import CliRunner

import pytest

import embykeeper
import embykeeper.cli as cli_module
from embykeeper.cli import app

runner = CliRunner()


@pytest.fixture()
def in_temp_dir(tmp_path: Path):
    current = os.getcwd()
    os.chdir(tmp_path)
    yield tmp_path
    os.chdir(current)


def test_get_proxy_str_encodes_userinfo():
    from embykeeper.schema import ProxyConfig
    from embykeeper.utils import get_proxy_str

    proxy = ProxyConfig(
        hostname="222.128.19.51",
        port=8327,
        scheme="socks5",
        username="yangle",
        password="R$^M6@LJ4TizNc",
    )

    assert get_proxy_str(proxy, curl=True) == "socks5h://yangle:R%24%5EM6%40LJ4TizNc@222.128.19.51:8327"
    assert get_proxy_str(proxy) == "socks5://yangle:R%24%5EM6%40LJ4TizNc@222.128.19.51:8327"


def test_version():
    result = runner.invoke(app, ["--version"])
    assert embykeeper.__version__ in result.stdout
    assert result.exit_code == 0


def test_create_config(in_temp_dir: Path):
    result = runner.invoke(app, ["--example-config"])
    assert "这是一个配置文件范例" in result.stdout
    assert "random_delay = true" in result.stdout
    assert "random_delay_range = [180, 360]" in result.stdout
    assert result.exit_code == 0


def test_emby_random_delay_config_defaults_to_enabled():
    from embykeeper.schema import EmbyConfig

    assert EmbyConfig().random_delay is True
    assert EmbyConfig(random_delay=False).random_delay is False
    assert EmbyConfig(random_delay_range=[10, 30]).random_delay_range == [10, 30]


@pytest.mark.parametrize("random_delay_range", ([30, 10], [-1, 30], [10]))
def test_emby_random_delay_range_requires_valid_range(random_delay_range):
    from pydantic import ValidationError
    from embykeeper.schema import EmbyConfig

    with pytest.raises(ValidationError):
        EmbyConfig(random_delay_range=random_delay_range)


class FakeTask:
    def __init__(self, coro, name):
        self._name = name
        self._task = asyncio.create_task(coro)

    def get_name(self):
        return self._name

    def __await__(self):
        return self._task.__await__()


class FakePool:
    def __init__(self):
        self._tasks = []

    def add(self, coro, name=None):
        self._tasks.append(FakeTask(coro, name))

    async def wait(self):
        if self._tasks:
            await asyncio.gather(*(task._task for task in self._tasks), return_exceptions=True)

    async def as_completed(self):
        for task in self._tasks:
            yield task


class FakeEmbyManager:
    async def run_all(self, instant=False):
        return None

    async def schedule_all(self):
        return None


def prepare_main_monkeypatch(monkeypatch, tmp_path: Path):
    fake_apprise_module = types.SimpleNamespace(
        Apprise=lambda: types.SimpleNamespace(add=lambda *_args, **_kwargs: None, notify=lambda **_kwargs: True),
        NotifyType=types.SimpleNamespace(INFO="info", WARNING="warning", FAILURE="failure", SUCCESS="success"),
    )
    monkeypatch.setitem(sys.modules, "apprise", fake_apprise_module)
    fake_cache_module = types.SimpleNamespace(
        cache=types.SimpleNamespace(
            set=lambda *_args, **_kwargs: None,
            get=lambda *_args, **_kwargs: "test",
            delete=lambda *_args, **_kwargs: None,
        )
    )
    monkeypatch.setitem(sys.modules, "embykeeper.cache", fake_cache_module)

    import embykeeper.notify as notify_module
    import embykeeper.emby.main as emby_main_module

    calls = {"start": 0, "stop": 0}
    fake_config = types.SimpleNamespace(
        mongodb=None,
        proxy=None,
        nofail=True,
        noexit=False,
        debug_cron=False,
        basedir=tmp_path,
        windows=False,
        public=False,
    )

    async def fake_reload_conf(_config_file):
        return True

    async def fake_stop_notifier():
        calls["stop"] += 1

    async def fake_start_notifier():
        calls["start"] += 1
        return [object()]

    fake_config.reload_conf = fake_reload_conf
    fake_config.on_change = lambda *_args, **_kwargs: None

    monkeypatch.setattr(cli_module, "AsyncTaskPool", FakePool)
    monkeypatch.setattr(cli_module, "config", fake_config)
    monkeypatch.setattr(emby_main_module, "EmbyManager", FakeEmbyManager)
    monkeypatch.setattr(notify_module, "start_notifier", fake_start_notifier)
    monkeypatch.setattr(notify_module, "_stop_notifier", fake_stop_notifier)

    return calls


def run_main(tmp_path: Path, **overrides):
    kwargs = dict(
        config_file=None,
        help=False,
        checkiner=False,
        emby=True,
        subsonic=False,
        monitor=False,
        messager=False,
        registrar=False,
        registrar_bot=None,
        version=False,
        example_config=False,
        instant=False,
        once=False,
        verbosity=0,
        debug_cron=False,
        debug_notify=False,
        simple_log=False,
        disable_color=False,
        follow=False,
        analyze=False,
        dump=[],
        top=False,
        play=None,
        save=False,
        telegram_test_server=False,
        public=False,
        windows=False,
        basedir=tmp_path,
        noexit=False,
        clean=False,
    )
    kwargs.update(overrides)
    return asyncio.run(cli_module.main(**kwargs))


def test_main_stops_notifier_on_shutdown(monkeypatch, tmp_path: Path):
    calls = prepare_main_monkeypatch(monkeypatch, tmp_path)

    run_main(tmp_path, once=False)

    assert calls == {"start": 1, "stop": 1}


def test_main_once_mode_exits_without_notifier(monkeypatch, tmp_path: Path):
    calls = prepare_main_monkeypatch(monkeypatch, tmp_path)

    run_main(tmp_path, once=True)

    assert calls == {"start": 0, "stop": 0}
