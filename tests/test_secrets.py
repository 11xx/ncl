"""The credential record, and what a backend check does and does not touch."""

from __future__ import annotations

from types import SimpleNamespace

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


class _Commands:
    """A `_run` that records every backend command and answers by prefix."""

    def __init__(self, answers: dict[tuple[str, ...], tuple[int, str, str]]) -> None:
        self.answers = answers
        self.commands: list[list[str]] = []

    def __call__(self, command, *, input_text=None):
        self.commands.append(list(command))
        for prefix, (code, out, err) in self.answers.items():
            if tuple(command[: len(prefix)]) == prefix:
                return SimpleNamespace(returncode=code, stdout=out, stderr=err)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    @property
    def mutations(self) -> list[list[str]]:
        """Commands that change the store, which inspection must never issue."""
        changing = {"insert", "rm", "store", "clear"}
        return [command for command in self.commands if changing.intersection(command)]


@pytest.mark.parametrize(
    ("backend", "listing", "expected"),
    [
        (secrets.PassBackend(), ("pass", "ls"), secrets.READY),
        (secrets.LibsecretBackend(), ("secret-tool", "lookup"), secrets.READY),
    ],
)
def test_inspection_answers_without_changing_the_store(
    monkeypatch, backend, listing, expected
):
    runner = _Commands({listing: (0, "", "")})
    monkeypatch.setattr(secrets, "_run", runner)
    monkeypatch.setattr(secrets.shutil, "which", lambda name: f"/usr/bin/{name}")

    state = backend.inspect()

    assert state.state == expected
    assert state.round_tripped is False
    assert state.side_effects == ()
    assert runner.mutations == []


@pytest.mark.parametrize(
    "backend", [secrets.PassBackend(), secrets.LibsecretBackend()]
)
def test_a_missing_binary_is_unavailable_rather_than_a_failed_round_trip(
    monkeypatch, backend
):
    runner = _Commands({})
    monkeypatch.setattr(secrets, "_run", runner)
    monkeypatch.setattr(secrets.shutil, "which", lambda name: None)

    state = backend.inspect()

    assert state.state == secrets.UNAVAILABLE
    assert runner.commands == []
    # An absent binary is not a store that failed: nothing was asked of it.
    assert backend.round_trip().state == secrets.UNAVAILABLE


def test_an_uninitialised_password_store_is_misconfigured(monkeypatch):
    runner = _Commands({("pass", "ls"): (1, "", "Error: password store is empty.")})
    monkeypatch.setattr(secrets, "_run", runner)
    monkeypatch.setattr(secrets.shutil, "which", lambda name: "/usr/bin/pass")

    state = secrets.PassBackend().inspect()

    assert state.state == secrets.MISCONFIGURED
    assert "pass init" in state.detail
    assert runner.mutations == []


def test_a_locked_secret_service_is_locked_rather_than_misconfigured(monkeypatch):
    runner = _Commands(
        {("secret-tool", "lookup"): (1, "", "the collection is locked and could not be unlocked")}
    )
    monkeypatch.setattr(secrets, "_run", runner)
    monkeypatch.setattr(secrets.shutil, "which", lambda name: "/usr/bin/secret-tool")

    state = secrets.LibsecretBackend().inspect()

    assert state.state == secrets.LOCKED
    assert runner.mutations == []


def test_an_absent_credential_is_not_an_unusable_backend(monkeypatch):
    """An empty successful lookup means the service works and holds nothing."""
    runner = _Commands({("secret-tool", "lookup"): (1, "", "")})
    monkeypatch.setattr(secrets, "_run", runner)
    monkeypatch.setattr(secrets.shutil, "which", lambda name: "/usr/bin/secret-tool")

    assert secrets.LibsecretBackend().inspect().state == secrets.READY


@pytest.mark.parametrize(
    ("backend", "listing"),
    [
        (secrets.PassBackend(), ("pass", "ls")),
        (secrets.LibsecretBackend(), ("secret-tool", "lookup")),
    ],
)
def test_a_round_trip_states_the_effects_it_had(monkeypatch, backend, listing):
    runner = _Commands({listing: (0, "", "")})
    stored: dict[str, str] = {}

    def _run(command, *, input_text=None):
        runner(command, input_text=input_text)
        if "insert" in command or "store" in command:
            stored["value"] = (input_text or "").rstrip("\n")
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if "show" in command or command[1] == "lookup":
            return SimpleNamespace(returncode=0, stdout=stored.get("value", ""), stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(secrets, "_run", _run)
    monkeypatch.setattr(secrets.shutil, "which", lambda name: f"/usr/bin/{name}")

    state = backend.round_trip()

    assert state.usable
    assert state.round_tripped is True
    assert state.side_effects
    assert runner.mutations, "a round trip must actually write"


def test_a_failed_round_trip_removes_its_value_and_names_no_secret(monkeypatch):
    runner = _Commands({("pass", "ls"): (0, "", "")})
    written: list[str] = []

    def _run(command, *, input_text=None):
        runner(command, input_text=input_text)
        if "insert" in command:
            written.append(input_text or "")
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if "show" in command:
            # The store accepted the value and returned something else.
            return SimpleNamespace(returncode=0, stdout="not-what-was-stored\n", stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(secrets, "_run", _run)
    monkeypatch.setattr(secrets.shutil, "which", lambda name: "/usr/bin/pass")

    state = secrets.PassBackend().round_trip()

    assert state.state == secrets.MISCONFIGURED
    assert any("rm" in command for command in runner.commands), "residue must be removed"
    probe_value = written[0].strip()
    assert probe_value not in state.detail
    assert not any(probe_value in effect for effect in state.side_effects)


@pytest.mark.parametrize(
    ("backend", "expected"),
    [
        (secrets.PassBackend(), secrets.MISCONFIGURED),
        (secrets.LibsecretBackend(), secrets.LOCKED),
    ],
)
def test_a_backend_that_does_not_respond_is_reported_not_raised(
    monkeypatch, backend, expected
):
    """`doctor` must survive the backend it exists to report on."""

    def _timeout(command, *, input_text=None):
        raise secrets.SecretError("the secret backend did not complete the request")

    monkeypatch.setattr(secrets, "_run", _timeout)
    monkeypatch.setattr(secrets.shutil, "which", lambda name: f"/usr/bin/{name}")

    state = backend.inspect()

    assert state.state == expected
    assert state.usable is False
    # The round trip stops at the same reading rather than writing anyway.
    assert backend.round_trip().state == expected
