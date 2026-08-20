from __future__ import annotations

import json
import stat
from types import SimpleNamespace

import pytest

from ncl import cli, exits, plans

PROFILE = SimpleNamespace(name="home")


def step(action: str, number: int, *, payload: bytes = b"body") -> plans.Step:
    return plans.freeze_step(
        action=action,
        href=f"https://cloud.example.invalid/resource/{number}",
        etag="",
        summary=f"step {number}",
        payload=payload,
        content_type="application/octet-stream",
        details={"number": number},
    )


def bundle(*steps: plans.Step, now: float | None = None, ttl: float = 900) -> plans.Plan:
    return plans.write_bundle(
        profile=PROFILE.name,
        summary="bundle",
        steps=steps,
        now=now,
        ttl=ttl,
    )


def dispatcher(
    allowed: set[str],
    calls: list[str],
    *,
    execute_error: dict[str, int] | None = None,
    reconciliation: dict[str, str] | None = None,
) -> plans.Dispatcher:
    def validate(item: plans.Step) -> None:
        if item.action not in allowed:
            raise plans.PlanError(f"unknown fake action {item.action!r}", exits.USAGE)

    def execute(profile, *, session, step):
        calls.append(f"execute:{step.action}")
        if execute_error and step.action in execute_error:
            raise plans.PlanError("server result is not persisted", execute_error[step.action])
        return {"action": step.action, "href": step.href}

    def reconcile(profile, *, session, step):
        calls.append(f"reconcile:{step.action}")
        return {"state": (reconciliation or {}).get(step.action, "uncertain")}

    return plans.Dispatcher(validate, execute, reconcile)


def apply_bundle(plan: plans.Plan, dispatch: plans.Dispatcher):
    with plans.claim(plan.plan_id):
        return plans.apply(
            PROFILE,
            session=object(),
            plan=plan,
            dispatchers={"fake.": dispatch},
        )


def reconcile_bundle(plan: plans.Plan, dispatch: plans.Dispatcher):
    with plans.claim(plan.plan_id):
        return plans.reconcile(
            PROFILE,
            session=object(),
            plan=plan,
            dispatchers={"fake.": dispatch},
        )


def test_bundle_writer_requires_ordered_nonempty_steps_and_redacts_payload():
    with pytest.raises(plans.PlanError) as error:
        plans.write_bundle(profile="home", summary="empty", steps=[])
    assert error.value.code == exits.USAGE

    plan = bundle(step("fake.first", 1, payload=b"secret"), step("fake.second", 2))

    assert not hasattr(plan, "action")
    assert [item.action for item in plan.steps] == ["fake.first", "fake.second"]
    view = plan.as_dict()
    assert [item["action"] for item in view["steps"]] == ["fake.first", "fake.second"]
    assert all("payload" not in item for item in view["steps"])
    assert view["steps"][0]["payload_bytes"] == len(b"secret")
    assert [item["state"] for item in view["progress"]] == ["pending", "pending"]


def test_two_and_three_step_success_are_dispatched_in_order_and_consumed():
    calls: list[str] = []
    dispatch = dispatcher({"fake.one", "fake.two", "fake.three"}, calls)
    plan = bundle(step("fake.one", 1), step("fake.two", 2), step("fake.three", 3))

    result = apply_bundle(plan, dispatch)

    assert calls == ["execute:fake.one", "execute:fake.two", "execute:fake.three"]
    assert [item["action"] for item in result["steps"]] == [
        "fake.one",
        "fake.two",
        "fake.three",
    ]
    with pytest.raises(plans.PlanError) as error:
        plans.read(plan.plan_id)
    assert error.value.code == exits.TARGET_NOT_FOUND


def test_unknown_later_action_is_rejected_before_any_request_or_progress():
    calls: list[str] = []
    dispatch = dispatcher({"fake.valid"}, calls)
    plan = bundle(step("fake.valid", 1), step("fake.unknown", 2))

    with pytest.raises(plans.PlanError) as error:
        apply_bundle(plan, dispatch)

    assert error.value.code == exits.USAGE
    assert calls == []
    stored = plans.read(plan.plan_id)
    assert [item.state for item in stored.progress] == ["pending", "pending"]


def test_ordinary_second_step_failure_records_first_verified_and_resume_skips_it():
    calls: list[str] = []
    plan = bundle(step("fake.one", 1), step("fake.two", 2))
    failing = dispatcher(
        {"fake.one", "fake.two"}, calls, execute_error={"fake.two": exits.CONFLICT}
    )

    with pytest.raises(plans.PlanError) as error:
        apply_bundle(plan, failing)
    assert error.value.code == exits.CONFLICT
    stored = plans.read(plan.plan_id)
    assert [item.state for item in stored.progress] == ["verified", "pending"]
    assert stored.progress[1].exit_code == exits.CONFLICT
    assert stored.expires_at is None

    resumed_calls: list[str] = []
    resumed = dispatcher({"fake.one", "fake.two"}, resumed_calls)
    result = apply_bundle(stored, resumed)
    assert resumed_calls == ["execute:fake.two"]
    assert result["complete"] is True


def test_uncertain_step_is_durable_blocks_apply_and_prevents_later_steps():
    calls: list[str] = []
    plan = bundle(step("fake.one", 1), step("fake.two", 2))
    uncertain = dispatcher(
        {"fake.one", "fake.two"},
        calls,
        execute_error={"fake.one": exits.OUTCOME_UNCERTAIN},
    )

    with pytest.raises(plans.PlanError) as error:
        apply_bundle(plan, uncertain)
    assert error.value.code == exits.OUTCOME_UNCERTAIN
    stored = plans.read(plan.plan_id)
    assert [item.state for item in stored.progress] == ["uncertain", "pending"]
    assert stored.progress[0].exit_code == exits.OUTCOME_UNCERTAIN

    blocked_calls: list[str] = []
    with pytest.raises(plans.PlanError) as blocked:
        apply_bundle(stored, dispatcher({"fake.one", "fake.two"}, blocked_calls))
    assert blocked.value.code == exits.OUTCOME_UNCERTAIN
    assert blocked_calls == []


def test_reconcile_verified_first_uncertain_step_then_resume_advances():
    calls: list[str] = []
    plan = bundle(step("fake.one", 1), step("fake.two", 2))
    with pytest.raises(plans.PlanError) as error:
        apply_bundle(
            plan,
            dispatcher(
                {"fake.one", "fake.two"},
                calls,
                execute_error={"fake.one": exits.OUTCOME_UNCERTAIN},
            ),
        )
    assert error.value.code == exits.OUTCOME_UNCERTAIN

    reconcile_calls: list[str] = []
    result = reconcile_bundle(
        plans.read(plan.plan_id),
        dispatcher(
            {"fake.one", "fake.two"},
            reconcile_calls,
            reconciliation={"fake.one": "verified"},
        ),
    )
    assert result["state"] == "verified"
    assert reconcile_calls == ["reconcile:fake.one"]
    stored = plans.read(plan.plan_id)
    assert [item.state for item in stored.progress] == ["verified", "pending"]

    resume_calls: list[str] = []
    apply_bundle(stored, dispatcher({"fake.one", "fake.two"}, resume_calls))
    assert resume_calls == ["execute:fake.two"]


def test_reconcile_pending_allows_retry_and_conflict_remains_blocked():
    calls: list[str] = []
    plan = bundle(step("fake.one", 1))
    with pytest.raises(plans.PlanError):
        apply_bundle(
            plan,
            dispatcher(
                {"fake.one"}, calls, execute_error={"fake.one": exits.OUTCOME_UNCERTAIN}
            ),
        )

    pending = plans.read(plan.plan_id)
    assert reconcile_bundle(
        pending,
        dispatcher({"fake.one"}, calls, reconciliation={"fake.one": "pending"}),
    )["state"] == "pending"
    retry_calls: list[str] = []
    apply_bundle(plans.read(plan.plan_id), dispatcher({"fake.one"}, retry_calls))
    assert retry_calls == ["execute:fake.one"]

    plan = bundle(step("fake.one", 1))
    with pytest.raises(plans.PlanError):
        apply_bundle(
            plan,
            dispatcher(
                {"fake.one"}, calls, execute_error={"fake.one": exits.OUTCOME_UNCERTAIN}
            ),
        )
    with pytest.raises(plans.PlanError) as conflict:
        reconcile_bundle(
            plans.read(plan.plan_id),
            dispatcher({"fake.one"}, calls, reconciliation={"fake.one": "uncertain"}),
        )
    assert conflict.value.code == exits.OUTCOME_UNCERTAIN
    assert plans.read(plan.plan_id).progress[0].state == "uncertain"


def test_progress_is_atomic_0600_and_stores_no_failure_prose(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    plan = bundle(step("fake.one", 1))
    with plans.claim(plan.plan_id):
        plans.update_progress(
            plan,
            0,
            state="uncertain",
            exit_code=exits.OUTCOME_UNCERTAIN,
            timestamp=2000,
        )
    path = plans._path(plan.plan_id)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    raw = path.read_text()
    assert "server result is not persisted" not in raw
    assert set(json.loads(raw)["progress"][0]) == {"state", "timestamp", "exit_code"}


def test_partial_progress_ignores_ttl_but_untouched_plan_is_stale():
    untouched = bundle(step("fake.one", 1), now=1000, ttl=1)
    with pytest.raises(plans.PlanError) as stale:
        plans.check_fresh(untouched, now=2000)
    assert stale.value.code == exits.PLAN_STALE

    partial = bundle(step("fake.one", 1), now=1000, ttl=1)
    with plans.claim(partial.plan_id):
        plans.update_progress(
            partial,
            0,
            state="verified",
            exit_code=exits.OK,
            timestamp=2000,
        )
    stored = plans.read(partial.plan_id)
    assert stored.expires_at is None
    plans.check_fresh(stored, now=100000)


@pytest.mark.parametrize(
    "raw",
    [
        {
            "plan_id": "old",
            "profile": "home",
            "action": "cal.create",
            "href": "https://cloud.example.invalid/old",
            "etag": "",
            "summary": "old",
            "payload": "",
            "content_type": "",
            "details": {},
            "created_at": 1,
            "expires_at": 2,
        },
        {
            "plan_id": "new",
            "profile": "home",
            "summary": "bad progress",
            "steps": [
                {
                    "action": "fake.one",
                    "href": "https://cloud.example.invalid/one",
                    "etag": "",
                    "summary": "one",
                    "payload": "Yg==",
                    "content_type": "application/octet-stream",
                    "details": {},
                }
            ],
            "created_at": 1,
            "expires_at": 2,
            "progress": [{"state": "blocked", "timestamp": 1, "exit_code": None}],
        },
    ],
)
def test_malformed_old_alpha_plan_and_progress_are_plan_stale(tmp_path, monkeypatch, raw):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    path = plans._path(raw["plan_id"])
    path.write_text(json.dumps(raw))

    with pytest.raises(plans.PlanError) as error:
        plans.read(raw["plan_id"])
    assert error.value.code == exits.PLAN_STALE


def test_plan_list_and_show_redact_payloads_and_display_order(capsys):
    plan = bundle(step("fake.first", 1, payload=b"PRIVATE"), step("fake.second", 2))

    assert cli.main(["plan", "list", "--json"]) == exits.OK
    listing = json.loads(capsys.readouterr().out)
    assert [item["action"] for item in listing["plans"][0]["steps"]] == [
        "fake.first",
        "fake.second",
    ]
    assert "PRIVATE" not in json.dumps(listing)

    assert cli.main(["plan", "show", plan.plan_id]) == exits.OK
    shown = capsys.readouterr().out
    assert "step 1: pending fake.first" in shown
    assert "step 2: pending fake.second" in shown
    assert "PRIVATE" not in shown


def test_reconcile_requires_an_uncertain_step():
    plan = bundle(step("fake.one", 1))
    with pytest.raises(plans.PlanError) as error:
        reconcile_bundle(
            plan,
            dispatcher({"fake.one"}, [], reconciliation={"fake.one": "verified"}),
        )
    assert error.value.code == exits.USAGE
