import asyncio

import tomlkit

from embykeeper.api import emby as emby_accounts


def test_update_emby_account_keeps_settings_outside_form(tmp_path, monkeypatch):
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        tomlkit.dumps(
            {
                "emby": {
                    "account": [
                        {
                            "id": "account-one",
                            "url": "https://example.com",
                            "username": "user",
                            "password": "test-password",
                            "timeout": 75,
                            "device_id": "test-device",
                            "jellyfin": True,
                            "play_id": "old-item",
                        },
                        {
                            "id": "account-two",
                            "url": "https://example.org",
                            "username": "user",
                            "password": "other-password",
                            "timeout": 30,
                        },
                    ]
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(emby_accounts, "_config_path", lambda: config_path)
    monkeypatch.setattr(emby_accounts, "_serialize_account", lambda account: {"id": account.id})

    payload = emby_accounts.EmbyAccountPayload(
        url="https://example.com",
        username="user",
        enabled=False,
        play_id=None,
    )
    result = asyncio.run(emby_accounts.update_emby_account(key="account-one", payload=payload, _=True))
    changed, other = tomlkit.parse(config_path.read_text(encoding="utf-8"))["emby"]["account"]

    assert result == {"success": True, "account": {"id": "account-one"}}
    assert changed["timeout"] == 75
    assert changed["device_id"] == "test-device"
    assert changed["jellyfin"] is True
    assert changed["password"] == "test-password"
    assert changed["enabled"] is False
    assert "play_id" not in changed
    assert other["timeout"] == 30
