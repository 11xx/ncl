from __future__ import annotations

import pytest

from ncl import config, exits

VALID = """
default_profile = "home"

[profiles.home]
origin = "https://cloud.example.invalid"
auth = "oauth"
secret_backend = "pass"
callback_port = 41417
client_id = "client-home"
calendars = ["/remote.php/dav/calendars/alice/"]
files_roots = ["/remote.php/dav/files/alice/work/"]
"""


def write_config(tmp_path, content: str = VALID):
    path = tmp_path / "config.toml"
    path.write_text(content)
    return path


def test_valid_config_loads(tmp_path):
    loaded = config.load(write_config(tmp_path))

    assert loaded.default_profile == "home"
    assert loaded.profiles["home"].client_id == "client-home"
    assert loaded.profiles["home"].calendars == ("/remote.php/dav/calendars/alice/",)


def test_unknown_key_is_rejected(tmp_path):
    path = write_config(tmp_path, VALID + "unexpected = true\n")

    with pytest.raises(config.ConfigError) as error:
        config.load(path)

    assert error.value.code == exits.PRECONDITION_FAILED
    assert "unknown key" in str(error.value)


def test_bad_origin_is_rejected_with_reason(tmp_path):
    path = write_config(tmp_path, VALID.replace("https://cloud.example.invalid", "http://cloud.example.invalid"))

    with pytest.raises(config.ConfigError, match="http:// is allowed only"):
        config.load(path)


def test_oauth_requires_client_id(tmp_path):
    path = write_config(tmp_path, VALID.replace('client_id = "client-home"\n', ""))

    with pytest.raises(config.ConfigError, match="client_id"):
        config.load(path)


def test_callback_port_must_be_in_range(tmp_path):
    path = write_config(tmp_path, VALID.replace("callback_port = 41417", "callback_port = 1023"))

    with pytest.raises(config.ConfigError, match="between 1024 and 65535"):
        config.load(path)
