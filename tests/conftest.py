"""Isolation the whole suite depends on.

These tests exercise a tool whose entire job is to touch a real account on a
real host: a browser, a secret store, a runtime directory, the network. Every
one of those is reachable by accident from a test, and when it happens the
damage lands on whoever is running the suite rather than on a fixture — a
browser tab opening mid-run, a real credential answering a lookup and making a
test pass or fail for reasons that have nothing to do with the change.

So the escapes are closed here, once, for every test, rather than in each test
that happens to remember.
"""

from __future__ import annotations

import subprocess
import webbrowser

import pytest


@pytest.fixture(autouse=True)
def _no_browser(monkeypatch):
    """Opening a browser from a test is always a bug.

    A test that reaches this has failed to inject `browser_open`, and without
    the guard it announces itself only as a tab appearing on someone's screen.
    """

    def _refuse(url, *args, **kwargs):
        raise AssertionError(
            f"a test opened a real browser at {url!r}; pass browser_open= instead"
        )

    monkeypatch.setattr(webbrowser, "open", _refuse)
    monkeypatch.setattr(webbrowser, "open_new", _refuse, raising=False)
    monkeypatch.setattr(webbrowser, "open_new_tab", _refuse, raising=False)


@pytest.fixture(autouse=True)
def _no_host_state(monkeypatch, tmp_path):
    """Point every host path at a temporary directory.

    Without this the suite reads the caller's real configuration, secret store,
    plans, and lock files. A stored credential then answers a lookup the test
    meant to be empty, so the suite passes or fails according to whether the
    person running it happens to be logged in.
    """
    for variable in ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_RUNTIME_DIR", "XDG_CACHE_HOME"):
        directory = tmp_path / variable.lower()
        directory.mkdir(parents=True, exist_ok=True)
        monkeypatch.setenv(variable, str(directory))
    monkeypatch.delenv("NCL_CONFIG", raising=False)


@pytest.fixture(autouse=True)
def secret_store(monkeypatch):
    """An in-memory stand-in for `pass` and `secret-tool`.

    Never the real binaries: a test that reached one would read, write, or
    delete entries in the caller's own store, and a credential sitting there
    would answer a lookup the test meant to find empty. Emulating the two
    backends' *result contracts* also keeps the absent-versus-broken
    distinction under test, which is the part that matters.

    Yielded so a test can seed or inspect it.
    """
    from ncl import secrets

    store: dict[str, str] = {}

    def _fake_run(command, *, input_text=None):
        name = command[0]
        if name == "pass":
            action = command[1]
            if action == "show":
                key = command[2]
                if key not in store:
                    return subprocess.CompletedProcess(
                        command, 1, "", f"Error: {key} is not in the password store.\n"
                    )
                return subprocess.CompletedProcess(command, 0, store[key] + "\n", "")
            if action == "insert":
                store[command[-1]] = (input_text or "").rstrip("\n")
                return subprocess.CompletedProcess(command, 0, "", "")
            if action == "rm":
                store.pop(command[-1], None)
                return subprocess.CompletedProcess(command, 0, "", "")
        if name == "secret-tool":
            action = command[1]
            key = "/".join(command[2:])
            if action == "lookup":
                return subprocess.CompletedProcess(command, 0, store.get(key, ""), "")
            if action == "store":
                store[key] = (input_text or "").rstrip("\n")
                return subprocess.CompletedProcess(command, 0, "", "")
            if action == "clear":
                store.pop(key, None)
                return subprocess.CompletedProcess(command, 0, "", "")
        raise AssertionError(f"unexpected secret backend call: {command!r}")

    monkeypatch.setattr(secrets, "_run", _fake_run)
    monkeypatch.setattr(secrets.shutil, "which", lambda name: f"/usr/bin/{name}")
    return store


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    """Fail loudly instead of reaching a real host.

    Every protocol layer takes an injected transport; a test that bypasses one
    would otherwise depend on whatever the network does today.
    """
    from ncl import session

    def _refuse(*args, **kwargs):
        raise AssertionError("a test attempted a real HTTP request; inject a transport")

    # Only the transport is closed off. Subprocesses stay available: the lock
    # tests spawn real contenders, which is the only way to prove mutual
    # exclusion actually holds across processes.
    monkeypatch.setattr(session.UrllibTransport, "request", _refuse)
