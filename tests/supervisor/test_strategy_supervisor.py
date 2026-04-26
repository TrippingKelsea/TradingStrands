"""Tests for the StrategySupervisor DDB-Streams handler.

The supervisor reacts to strategy row changes and reconciles ECS
service state. It's the only component allowed to change per-bot
Fargate services — keeping that concentrated means we have one place
to reason about races and one IAM role with ECS admin powers.
"""

from __future__ import annotations

from typing import Any

from trading_strands.supervisor.strategy_supervisor import (
    Action,
    Decision,
    classify_record,
    reconcile,
    service_name_for,
)

# ── service naming ───────────────────────────────────────────────────


def test_service_name_is_deterministic() -> None:
    assert service_name_for("abc123") == "ts-strategy-abc123"


# ── classify_record ──────────────────────────────────────────────────


def _mk_record(
    event_name: str, new_image: dict[str, Any] | None = None,
    old_image: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a DDB Stream record in the shape AWS actually sends."""

    dyn: dict[str, Any] = {}
    if new_image is not None:
        dyn["NewImage"] = _ddb_encode(new_image)
    if old_image is not None:
        dyn["OldImage"] = _ddb_encode(old_image)
    return {"eventName": event_name, "dynamodb": dyn}


def _ddb_encode(item: dict[str, Any]) -> dict[str, Any]:
    """Minimal DDB attribute-value encoder for test fixtures."""

    out: dict[str, Any] = {}
    for k, v in item.items():
        if isinstance(v, str):
            out[k] = {"S": v}
        elif isinstance(v, bool):
            out[k] = {"BOOL": v}
        elif isinstance(v, int):
            out[k] = {"N": str(v)}
        elif isinstance(v, list):
            out[k] = {"L": [{"S": str(x)} for x in v]}
        else:
            out[k] = {"S": str(v)}
    return out


def test_classify_ignores_non_strategy_rows() -> None:
    """The table holds ORG#, USER#, LEDGER#, etc. Only STRATEGY# rows
    drive Fargate. Everything else is NOOP."""

    rec = _mk_record("INSERT", new_image={"pk": "ORG#foo", "name": "x"})
    assert classify_record(rec).action is Action.NOOP


def test_classify_insert_active_starts_service() -> None:
    rec = _mk_record("INSERT", new_image={
        "pk": "STRATEGY#abc", "strategy_id": "abc",
        "org_id": "o1", "status": "active",
    })
    d = classify_record(rec)
    assert d.action is Action.ENSURE_RUNNING
    assert d.strategy_id == "abc"
    assert d.org_id == "o1"


def test_classify_insert_paused_does_nothing() -> None:
    """A strategy born paused doesn't need a running service."""

    rec = _mk_record("INSERT", new_image={
        "pk": "STRATEGY#abc", "strategy_id": "abc",
        "org_id": "o1", "status": "paused",
    })
    assert classify_record(rec).action is Action.NOOP


def test_classify_modify_active_to_paused_pauses() -> None:
    rec = _mk_record(
        "MODIFY",
        new_image={
            "pk": "STRATEGY#abc", "strategy_id": "abc",
            "org_id": "o1", "status": "paused",
        },
        old_image={
            "pk": "STRATEGY#abc", "strategy_id": "abc",
            "org_id": "o1", "status": "active",
        },
    )
    d = classify_record(rec)
    assert d.action is Action.PAUSE


def test_classify_modify_active_to_stopped_stops() -> None:
    """Stop = tear down the service. The strategy is finished."""

    rec = _mk_record(
        "MODIFY",
        new_image={
            "pk": "STRATEGY#abc", "strategy_id": "abc",
            "org_id": "o1", "status": "stopped",
        },
        old_image={
            "pk": "STRATEGY#abc", "strategy_id": "abc",
            "org_id": "o1", "status": "active",
        },
    )
    d = classify_record(rec)
    assert d.action is Action.STOP


def test_classify_modify_paused_to_active_resumes() -> None:
    rec = _mk_record(
        "MODIFY",
        new_image={
            "pk": "STRATEGY#abc", "strategy_id": "abc",
            "org_id": "o1", "status": "active",
        },
        old_image={
            "pk": "STRATEGY#abc", "strategy_id": "abc",
            "org_id": "o1", "status": "paused",
        },
    )
    assert classify_record(rec).action is Action.ENSURE_RUNNING


def test_classify_modify_unchanged_status_is_noop() -> None:
    """Markdown edit, renamed, capital change — none change service state."""

    rec = _mk_record(
        "MODIFY",
        new_image={
            "pk": "STRATEGY#abc", "strategy_id": "abc",
            "org_id": "o1", "status": "active", "name": "v2",
        },
        old_image={
            "pk": "STRATEGY#abc", "strategy_id": "abc",
            "org_id": "o1", "status": "active", "name": "v1",
        },
    )
    assert classify_record(rec).action is Action.NOOP


def test_classify_remove_tears_down() -> None:
    rec = _mk_record("REMOVE", old_image={
        "pk": "STRATEGY#abc", "strategy_id": "abc",
        "org_id": "o1", "status": "active",
    })
    d = classify_record(rec)
    assert d.action is Action.STOP
    assert d.strategy_id == "abc"


# ── reconcile ────────────────────────────────────────────────────────


class FakeECS:
    """Minimal ECS client stand-in. Tracks services + registered task
    definition revisions by family.

    Real ECS flow: register_task_definition(family, containerDefinitions)
    returns a revision ARN; create_service(taskDefinition=<arn>) attaches
    it. To pass per-strategy STRATEGY_ID/ORG_ID env, we register a new
    revision per strategy keyed on the base family.
    """

    def __init__(self) -> None:
        self.services: dict[str, dict[str, Any]] = {}
        self.task_defs: list[dict[str, Any]] = []
        self.calls: list[str] = []

    def register_task_definition(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(f"register_td:{kwargs.get('family', '?')}")
        revision = len(self.task_defs) + 1
        arn = f"arn:aws:ecs:test:0:task-definition/{kwargs['family']}:{revision}"
        td = {"taskDefinitionArn": arn, **kwargs}
        self.task_defs.append(td)
        return {"taskDefinition": td}

    def describe_services(
        self, cluster: str, services: list[str],
    ) -> dict[str, Any]:
        self.calls.append(f"describe:{services[0]}")
        name = services[0]
        s = self.services.get(name)
        if s is None:
            return {"services": [{"status": "MISSING"}]}
        return {"services": [s]}

    def create_service(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(f"create:{kwargs['serviceName']}")
        name = kwargs["serviceName"]
        self.services[name] = {
            "serviceName": name, "status": "ACTIVE",
            "desiredCount": kwargs.get("desiredCount", 1),
            "taskDefinition": kwargs.get("taskDefinition", ""),
        }
        return {"service": self.services[name]}

    def update_service(
        self, cluster: str, service: str, desiredCount: int,
    ) -> dict[str, Any]:
        self.calls.append(f"update:{service}:{desiredCount}")
        if service in self.services:
            self.services[service]["desiredCount"] = desiredCount
        return {"service": self.services.get(service, {})}

    def delete_service(
        self, cluster: str, service: str, force: bool = False,
    ) -> dict[str, Any]:
        self.calls.append(f"delete:{service}")
        self.services.pop(service, None)
        return {}


def _params() -> dict[str, Any]:
    return {
        "cluster": "ts-cluster",
        "base_task_definition_family": "ts-bot",
        "container_image": "0.dkr.ecr.test.amazonaws.com/ts:latest",
        "task_role_arn": "arn:aws:iam::0:role/ts-task",
        "execution_role_arn": "arn:aws:iam::0:role/ts-exec",
        "subnet_ids": ["subnet-a"],
        "security_group_ids": ["sg-1"],
        "log_group_name": "/ts/bots",
        "region": "us-west-2",
        "extra_env": {"DYNAMODB_TABLE": "ts-state"},
    }


def test_reconcile_ensure_running_creates_when_missing() -> None:
    ecs = FakeECS()
    reconcile(
        ecs_client=ecs,
        decision=Decision(
            action=Action.ENSURE_RUNNING, strategy_id="abc", org_id="o1",
        ),
        **_params(),
    )
    assert "create:ts-strategy-abc" in ecs.calls
    svc = ecs.services["ts-strategy-abc"]
    assert svc["desiredCount"] == 1


def test_reconcile_ensure_running_scales_up_when_existing_at_zero() -> None:
    """A resumed strategy: service exists at desiredCount=0, scale back up."""

    ecs = FakeECS()
    ecs.services["ts-strategy-abc"] = {
        "serviceName": "ts-strategy-abc", "status": "ACTIVE",
        "desiredCount": 0,
    }
    reconcile(
        ecs_client=ecs,
        decision=Decision(
            action=Action.ENSURE_RUNNING, strategy_id="abc", org_id="o1",
        ),
        **_params(),
    )
    assert "update:ts-strategy-abc:1" in ecs.calls
    # Don't re-create when one already exists.
    assert not any(c.startswith("create:") for c in ecs.calls)


def test_reconcile_ensure_running_noop_when_already_running() -> None:
    ecs = FakeECS()
    ecs.services["ts-strategy-abc"] = {
        "serviceName": "ts-strategy-abc", "status": "ACTIVE",
        "desiredCount": 1,
    }
    reconcile(
        ecs_client=ecs,
        decision=Decision(
            action=Action.ENSURE_RUNNING, strategy_id="abc", org_id="o1",
        ),
        **_params(),
    )
    # Only described; no state-change call.
    assert [c for c in ecs.calls if not c.startswith("describe:")] == []


def test_reconcile_pause_scales_to_zero() -> None:
    ecs = FakeECS()
    ecs.services["ts-strategy-abc"] = {
        "serviceName": "ts-strategy-abc", "status": "ACTIVE",
        "desiredCount": 1,
    }
    reconcile(
        ecs_client=ecs,
        decision=Decision(action=Action.PAUSE, strategy_id="abc", org_id="o1"),
        **_params(),
    )
    assert "update:ts-strategy-abc:0" in ecs.calls
    assert "delete:ts-strategy-abc" not in ecs.calls


def test_reconcile_pause_noop_when_service_missing() -> None:
    """Pausing a strategy whose service was never created is a no-op —
    nothing to do. Don't try to update a missing service."""

    ecs = FakeECS()
    reconcile(
        ecs_client=ecs,
        decision=Decision(action=Action.PAUSE, strategy_id="abc", org_id="o1"),
        **_params(),
    )
    assert not any(c.startswith("update:") for c in ecs.calls)


def test_reconcile_stop_deletes_service() -> None:
    ecs = FakeECS()
    ecs.services["ts-strategy-abc"] = {
        "serviceName": "ts-strategy-abc", "status": "ACTIVE",
        "desiredCount": 1,
    }
    reconcile(
        ecs_client=ecs,
        decision=Decision(action=Action.STOP, strategy_id="abc", org_id="o1"),
        **_params(),
    )
    # Must scale to 0 before delete (ECS refuses to delete a service
    # with running tasks unless force=True; we do both belt + braces).
    assert "update:ts-strategy-abc:0" in ecs.calls
    assert "delete:ts-strategy-abc" in ecs.calls
    update_idx = ecs.calls.index("update:ts-strategy-abc:0")
    delete_idx = ecs.calls.index("delete:ts-strategy-abc")
    assert update_idx < delete_idx


def test_reconcile_stop_idempotent_when_missing() -> None:
    """REMOVE event replayed after service already deleted — no error."""

    ecs = FakeECS()
    reconcile(
        ecs_client=ecs,
        decision=Decision(action=Action.STOP, strategy_id="abc", org_id="o1"),
        **_params(),
    )
    assert not any(c.startswith("delete:") for c in ecs.calls)


def test_reconcile_noop_decision_makes_no_calls() -> None:
    ecs = FakeECS()
    reconcile(
        ecs_client=ecs,
        decision=Decision(action=Action.NOOP, strategy_id=None, org_id=None),
        **_params(),
    )
    assert ecs.calls == []


# ── create passes the right env ──────────────────────────────────────


def test_reconcile_create_sets_strategy_and_org_env() -> None:
    """The task boots into single-bot mode via STRATEGY_ID + ORG_ID.
    Without these envs the process would raise on startup — so the
    supervisor must always set them on the registered task def."""

    ecs = FakeECS()
    reconcile(
        ecs_client=ecs,
        decision=Decision(
            action=Action.ENSURE_RUNNING, strategy_id="abc", org_id="o1",
        ),
        **_params(),
    )
    # A per-strategy task def was registered first, then the service
    # was created pointing at it.
    assert ecs.task_defs, "expected a task def registration"
    td = ecs.task_defs[0]
    container = td["containerDefinitions"][0]
    env = {e["name"]: e["value"] for e in container["environment"]}
    assert env["STRATEGY_ID"] == "abc"
    assert env["ORG_ID"] == "o1"
    # Extra env from the supervisor config (e.g. DYNAMODB_TABLE) flows
    # through too so the bot can find its table.
    assert env["DYNAMODB_TABLE"] == "ts-state"


# ── handler glue ─────────────────────────────────────────────────────


def test_handler_processes_multiple_records() -> None:
    """One stream batch can contain many records. Process all of them,
    don't stop on the first noop."""

    from trading_strands.supervisor import strategy_supervisor

    ecs = FakeECS()

    event = {
        "Records": [
            _mk_record("INSERT", new_image={
                "pk": "USER#x", "user_id": "x",  # non-strategy, noop
            }),
            _mk_record("INSERT", new_image={
                "pk": "STRATEGY#abc", "strategy_id": "abc",
                "org_id": "o1", "status": "active",
            }),
            _mk_record("INSERT", new_image={
                "pk": "STRATEGY#def", "strategy_id": "def",
                "org_id": "o2", "status": "active",
            }),
        ],
    }
    result = strategy_supervisor._run(
        ecs_client=ecs, event=event, **_params(),
    )
    assert result["processed"] == 3
    assert result["actions"]["ENSURE_RUNNING"] == 2
    assert result["actions"]["NOOP"] == 1
    assert "ts-strategy-abc" in ecs.services
    assert "ts-strategy-def" in ecs.services


def test_handler_continues_on_single_record_failure() -> None:
    """A per-record ECS error must not poison the whole batch — DDB
    Streams will retry the batch, and we'd rather process the other
    records than loop forever on one bad one."""

    from trading_strands.supervisor import strategy_supervisor

    class FlakyECS(FakeECS):
        def create_service(self, **kwargs: Any) -> dict[str, Any]:
            if kwargs["serviceName"] == "ts-strategy-bad":
                raise RuntimeError("capacity error")
            return super().create_service(**kwargs)

    ecs = FlakyECS()
    event = {
        "Records": [
            _mk_record("INSERT", new_image={
                "pk": "STRATEGY#bad", "strategy_id": "bad",
                "org_id": "o1", "status": "active",
            }),
            _mk_record("INSERT", new_image={
                "pk": "STRATEGY#good", "strategy_id": "good",
                "org_id": "o1", "status": "active",
            }),
        ],
    }
    result = strategy_supervisor._run(
        ecs_client=ecs, event=event, **_params(),
    )
    assert result["processed"] == 2
    assert result["errors"] == 1
    assert "ts-strategy-good" in ecs.services
    assert "ts-strategy-bad" not in ecs.services


def test_modify_without_old_image_is_noop() -> None:
    """Defensive: a MODIFY without OldImage (shouldn't happen given
    NEW_AND_OLD_IMAGES stream view, but we don't want to crash) is
    treated as a no-op rather than a spurious action."""

    rec = {
        "eventName": "MODIFY",
        "dynamodb": {
            "NewImage": _ddb_encode({
                "pk": "STRATEGY#abc", "strategy_id": "abc",
                "org_id": "o1", "status": "paused",
            }),
        },
    }
    assert classify_record(rec).action is Action.NOOP
