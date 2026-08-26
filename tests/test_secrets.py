"""The credential record: one item per profile, decoded strictly or not at all."""

from __future__ import annotations

import pytest

from ncl import exits, secrets
from ncl.config import Profile

PROFILE = Profile(
    "home",
    "https://cloud.example.invalid",
    "pass",
    ("/remote.php/dav/calendars/alice/work/",),
    ("/remote.php/dav/files/alice/",),
)


class _Store:
    """A backend that records every operation performed on it."""

    def __init__(self, initial: dict[str, str] | None = None) -> None:
        self.items = dict(initial or {})
        self.operations: list[tuple[str, str]] = []

    def install(self, monkeypatch, *, fail_on: str = "") -> None:
        def _get(profile, key):
            self.operations.append(("get", key))
            return self.items.get(key)

        def _set(profile, key, value):
            self.operations.append(("set", key))
            if fail_on == "set":
                raise secrets.SecretError("backend refused the write")
            self.items[key] = value

        def _delete(profile, key):
            self.operations.append(("delete", key))
            if fail_on == "delete":
                raise secrets.SecretError("backend refused the removal")
            self.items.pop(key, None)

        monkeypatch.setattr(secrets, "get", _get)
        monkeypatch.setattr(secrets, "set", _set)
        monkeypatch.setattr(secrets, "delete", _delete)


def test_a_profile_stores_its_whole_credential_as_one_item(monkeypatch):
    store = _Store()
    store.install(monkeypatch)

    secrets.store_credential(PROFILE, "alice", "fixture-secret")

    assert list(store.items) == [secrets.RECORD_KEY]
    assert [name for name, _ in store.operations if name == "set"] == ["set"]
    assert secrets.load_credential(PROFILE) == secrets.Credential("alice", "fixture-secret")


def test_replacement_interrupted_mid_write_leaves_the_previous_credential(monkeypatch):
    store = _Store()
    store.install(monkeypatch)
    secrets.store_credential(PROFILE, "alice", "first-secret")

    def _interrupt(profile, key, value):
        raise KeyboardInterrupt

    monkeypatch.setattr(secrets, "set", _interrupt)
    with pytest.raises(KeyboardInterrupt):
        secrets.store_credential(PROFILE, "bob", "second-secret")

    # There is no half credential to find: the record is written whole or not.
    assert secrets.load_credential(PROFILE) == secrets.Credential("alice", "first-secret")


def test_a_read_never_pairs_fields_from_two_generations(monkeypatch):
    store = _Store()
    store.install(monkeypatch)
    secrets.store_credential(PROFILE, "alice", "first-secret")
    observed = []

    def _racing_get(profile, key):
        # A concurrent login lands between the read and whatever follows it.
        value = store.items.get(key)
        store.items[secrets.RECORD_KEY] = secrets._encode(
            secrets.Credential("bob", "second-secret")
        )
        return value

    monkeypatch.setattr(secrets, "get", _racing_get)
    observed.append(secrets.load_credential(PROFILE))
    observed.append(secrets.load_credential(PROFILE))

    assert observed[0] == secrets.Credential("alice", "first-secret")
    assert observed[1] == secrets.Credential("bob", "second-secret")


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "not json at all",
        "null",
        "[]",
        '"a string"',
        "{}",
        '{"version": 1, "login_name": "alice"}',
        '{"version": 1, "app_password": "secret"}',
        '{"version": 2, "login_name": "alice", "app_password": "secret"}',
        '{"version": "1", "login_name": "alice", "app_password": "secret"}',
        '{"version": true, "login_name": "alice", "app_password": "secret"}',
        '{"version": 1, "login_name": "", "app_password": "secret"}',
        '{"version": 1, "login_name": "alice", "app_password": ""}',
        '{"version": 1, "login_name": 7, "app_password": "secret"}',
        '{"version": 1, "login_name": "alice", "app_password": "secret", "extra": 1}',
        '{"version": 1, "login_name": "alice", "login_name": "bob", "app_password": "s"}',
        '{"version": 1, "login_name": "alice", "app_password": "secret"} trailing',
    ],
)
def test_a_malformed_record_is_refused_with_the_same_remedy(monkeypatch, raw):
    store = _Store({secrets.RECORD_KEY: raw})
    store.install(monkeypatch)

    with pytest.raises(secrets.SecretError) as error:
        secrets.load_credential(PROFILE)

    assert "run `ncl login`" in str(error.value)
    assert error.value.code == exits.PRECONDITION_FAILED


def test_an_entry_from_the_replaced_layout_is_not_read_as_a_credential(monkeypatch):
    store = _Store({"app_password": "fixture-secret", "login_name": "alice"})
    store.install(monkeypatch)

    assert secrets.load_credential(PROFILE) is None
    assert secrets.has_credential(PROFILE) is False


def test_absence_is_distinguished_from_a_record_that_cannot_be_read(monkeypatch):
    empty = _Store()
    empty.install(monkeypatch)
    assert secrets.has_credential(PROFILE) is False

    broken = _Store({secrets.RECORD_KEY: "{}"})
    broken.install(monkeypatch)
    # Present but unreadable is a conflict, not absence: storing over it stays
    # a deliberate act rather than something a second login does silently.
    assert secrets.has_credential(PROFILE) is True
    with pytest.raises(secrets.SecretError):
        secrets.load_credential(PROFILE)


def test_clearing_removes_the_record_and_any_entry_the_old_layout_left(monkeypatch):
    store = _Store(
        {
            secrets.RECORD_KEY: secrets._encode(secrets.Credential("alice", "s")),
            "app_password": "stale-secret",
            "login_name": "alice",
        }
    )
    store.install(monkeypatch)

    secrets.clear_credential(PROFILE)

    assert store.items == {}


def test_clearing_reports_a_backend_failure_rather_than_claiming_success(monkeypatch):
    store = _Store({secrets.RECORD_KEY: secrets._encode(secrets.Credential("alice", "s"))})
    store.install(monkeypatch, fail_on="delete")

    with pytest.raises(secrets.SecretError):
        secrets.clear_credential(PROFILE)

    assert secrets.RECORD_KEY in store.items


def test_storing_an_empty_half_is_refused_before_the_backend_is_touched(monkeypatch):
    store = _Store()
    store.install(monkeypatch)

    for login_name, app_password in (("", "secret"), ("alice", "")):
        with pytest.raises(secrets.SecretError):
            secrets.store_credential(PROFILE, login_name, app_password)

    assert store.operations == []


def test_a_record_survives_values_json_would_have_to_escape(monkeypatch):
    store = _Store()
    store.install(monkeypatch)
    awkward = 'p"a\\s s\nword\té'

    secrets.store_credential(PROFILE, "alice", awkward)

    assert secrets.load_credential(PROFILE) == secrets.Credential("alice", awkward)


def test_an_unsupported_backend_key_is_refused(monkeypatch):
    with pytest.raises(secrets.SecretError):
        secrets._check_key("something-else")
