from __future__ import annotations

from ncl import checks, config, exits, secrets


def test_one_registered_factory_feeds_every_backend_consumer(monkeypatch, tmp_path):
    """A factory-only registry is the one extension point for every consumer."""
    items: dict[tuple[str, str], str] = {}
    calls: list[str] = []
    instances = []

    class FakeBackend:
        def __init__(self) -> None:
            instances.append(self)

        @staticmethod
        def _address(profile, key: str) -> tuple[str, str]:
            name = profile.name if hasattr(profile, "name") else str(profile)
            return name, key

        def get(self, profile, key):
            calls.append("get")
            return items.get(self._address(profile, key))

        def set(self, profile, key, value):
            calls.append("set")
            items[self._address(profile, key)] = value

        def delete(self, profile, key):
            calls.append("delete")
            items.pop(self._address(profile, key), None)

        def inspect(self):
            calls.append("inspect")
            return secrets.BackendState(secrets.READY, "fake backend")

        def round_trip(self):
            calls.append("round_trip")
            profile = "fake-probe"
            self.set(profile, secrets.RECORD_KEY, "probe")
            returned = self.get(profile, secrets.RECORD_KEY)
            self.delete(profile, secrets.RECORD_KEY)
            if returned != "probe":
                return secrets.BackendState(
                    secrets.MISCONFIGURED, "fake backend did not round-trip"
                )
            return secrets.BackendState(
                secrets.READY,
                "fake backend round-tripped a value",
                round_tripped=True,
                side_effects=("fake effect",),
            )

    def factory():
        return FakeBackend()

    monkeypatch.setitem(secrets._BACKEND_FACTORIES, "fake", factory)
    assert secrets.backend_names() == ("pass", "libsecret", "fake")

    path = tmp_path / "config.toml"
    path.write_text(
        """default_profile = "home"

[profiles.home]
origin = "https://cloud.example.invalid"
secret_backend = "fake"
calendars = ["/remote.php/dav/calendars/alice/"]
files_roots = ["/remote.php/dav/files/alice/"]
"""
    )
    loaded = config.load(path)
    profile = loaded.profiles["home"]

    report = checks.run(path)
    assert report.exit_code == exits.OK
    assert any(
        check.name == "secret-backend:home:fake" and check.status == "pass"
        for check in report.checks
    )

    assert secrets.inspect_backend("fake").state == secrets.READY
    assert secrets.round_trip_backend("fake").round_tripped is True
    assert secrets.probe(profile) is True
    secrets.store_credential(profile, "alice", "fixture-secret")
    assert secrets.load_credential(profile) == secrets.Credential("alice", "fixture-secret")
    assert secrets.has_credential(profile) is True
    secrets.clear_credential(profile)
    assert secrets.load_credential(profile) is None

    assert {type(instance) for instance in instances} == {FakeBackend}
    assert len({id(instance) for instance in instances}) == len(instances)
    assert {"inspect", "round_trip", "set", "get", "delete"} <= set(calls)
