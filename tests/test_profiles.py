from __future__ import annotations

import pytest

from ncl import config, exits
from ncl.profiles import in_scope, resolve


def test_resolve_uses_default_or_explicit_profile(tmp_path):
    loaded = config.validate(
        {
            "default_profile": "home",
            "profiles": {
                "home": {
                    "origin": "https://cloud.example.invalid",
                    "secret_backend": "pass",
                    "calendars": ["/calendars/alice"],
                    "files_roots": ["/files/alice"],
                },
                "other": {
                    "origin": "https://cloud.example.invalid",
                    "secret_backend": "pass",
                    "calendars": ["/calendars/alice"],
                    "files_roots": ["/files/alice"],
                },
            },
        },
        tmp_path / "config.toml",
    )

    assert resolve(loaded=loaded).name == "home"
    assert resolve("other", loaded=loaded).name == "other"
    with pytest.raises(config.ConfigError) as error:
        resolve("missing", loaded=loaded)
    assert error.value.code == exits.NOT_CONFIGURED


def test_scope_decodes_segments_without_using_raw_prefixes():
    assert in_scope("/files/alice/%77ork/report.txt", ["/files/alice/work"])
    assert not in_scope("/files/alice/work-old/report.txt", ["/files/alice/work"])


def test_scope_respects_segment_boundaries():
    assert in_scope("/files/user/work/report.txt", ["/files/user/work"])
    assert not in_scope("/files/user/work-old", ["/files/user/work"])


def test_scope_rejects_dot_dot_before_or_after_decoding():
    assert not in_scope("/files/user/../secret", ["/files/user"])
    assert not in_scope("/files/user/%2e%2e/secret", ["/files/user"])
    assert not in_scope("/files/user/%2E%2E/secret", ["/files/user"])


def test_scope_keeps_encoded_slash_inside_one_segment():
    assert in_scope("/files/user/a%2Fb", ["/files/user"])
    assert not in_scope("/files/user/a%2Fb", ["/files/user/a/b"])


def test_scope_ignores_trailing_slash_difference():
    assert in_scope("/files/user/work/", ["/files/user/work"])
    assert in_scope("/files/user/work", ["/files/user/work/"])


def test_scope_normalizes_default_ports_without_matching_other_ports():
    assert in_scope(
        "https://cloud.example/files/home/report.txt",
        ["https://cloud.example:443/files/home"],
    )
    assert not in_scope(
        "https://cloud.example:444/files/home/report.txt",
        ["https://cloud.example:443/files/home"],
    )


def test_scope_normalizes_unicode_to_nfc():
    assert in_scope("/files/alice/cafe\u0301", ["/files/alice/caf\u00e9"])


def test_empty_allowlist_admits_nothing():
    assert not in_scope("/files/alice/work", [])


def test_root_allowlist_entry_admits_nothing():
    assert not in_scope("/anything/at/all", ["/"])
