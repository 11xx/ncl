from __future__ import annotations

import pytest

from ncl import config, exits

VALID = """
default_profile = "home"

[profiles.home]
origin = "https://cloud.example.invalid"
secret_backend = "pass"
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
    assert loaded.profiles["home"].secret_backend == "pass"
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


def test_secret_backend_defaults_to_pass(tmp_path):
    content = VALID.replace('secret_backend = "pass"\n', "")

    loaded = config.load(write_config(tmp_path, content))

    assert loaded.profiles["home"].secret_backend == "pass"


@pytest.mark.parametrize("entry", ["/", "/.", "https://cloud.example.invalid/"])
def test_root_allowlist_entries_are_rejected(tmp_path, entry):
    content = VALID.replace(
        'calendars = ["/remote.php/dav/calendars/alice/"]',
        f'calendars = ["{entry}"]',
    )

    with pytest.raises(config.ConfigError, match="entire account"):
        config.load(write_config(tmp_path, content))
