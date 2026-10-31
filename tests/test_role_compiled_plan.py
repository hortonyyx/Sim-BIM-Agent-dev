"""Role consumers use the immutable plan which the successful trial compiled."""

import asyncio
import copy
import hashlib
import json
from contextlib import asynccontextmanager

import pytest

from src.agent.runtime_roles.trial import PlanTrial, canonical_plan_sha256
from tests.test_role_trial import example
from tests.test_role_session import dispatch, environment  # noqa: F401


@pytest.fixture(autouse=True)
def effective_plan_metadata(monkeypatch):
    """Keep the real planar compiler; distinguish its verified output from input."""
    from src.agent.runtime_roles import plan_reading
    compile_reading = plan_reading.build_reading
    def compile_effective(plan, **kwargs):
        effective = copy.deepcopy(plan)
        effective["regularization"] = {"rule_version": "plan_regularization_v1",
            "changes": [{"rule": "fixture", "action": "metadata_only"}], "rejections": []}
        return compile_reading(effective, **kwargs)
    monkeypatch.setattr(plan_reading, "build_reading", compile_effective)


def _save_delivery(session, task_id, *, artifact_kind="compiled", validation_changes=None,
                   compiled_file_bytes=None):
    task = session._task({
        **dispatch(task_id), "role_id": "plan_reader", "target": "F1",
    })
    child = session.registry.admit(task)
    workspace = child.task_directory / "bim/trial_workspace"
    (workspace / "images").mkdir(parents=True)
    source_image = session.run_directory / "images/north.png"
    (workspace / "images/north.png").write_bytes(source_image.read_bytes())
    (workspace / "inputs.json").write_text(json.dumps({"images": {"north.png": {
        "sha256": hashlib.sha256(source_image.read_bytes()).hexdigest(),
        "size": [100, 100],
    }}}), encoding="utf-8", newline="\n")
    trial = PlanTrial(
        None, image_name="north.png", workspace=workspace,
        receipt_directory=workspace / "trial_receipts",
    )
    original = example()
    receipt = asyncio.run(trial.run(original))
    assert receipt["status"] == "passed", receipt
    compiled = trial.numeric_plan(receipt)
    assert compiled != original and compiled["regularization"]["rule_version"] == "plan_regularization_v1"
    artifact_plan = copy.deepcopy(compiled if artifact_kind == "compiled" else original)
    if artifact_kind == "forged":
        artifact_plan["basis"] = "forged after the successful trial"
    validation = {**receipt, "validation_passed": True}
    if compiled_file_bytes is not None:
        compiled_path = workspace / receipt["compiled_numeric_plan_file"]
        compiled_path.write_bytes(compiled_file_bytes)
        validation["compiled_numeric_plan_sha256"] = hashlib.sha256(compiled_file_bytes).hexdigest()
    validation.update(validation_changes or {})
    row = session.registry.save(
        task, status="completed",
        artifact={"plan": artifact_plan, "evidence": [], "unresolved": []},
        validation=validation,
    )
    return row, original, compiled, workspace


def test_build_uses_compiled_delivery_and_rejects_original_declaration(environment):
    _, make = environment
    session = make()
    for task_id, artifact_kind in (("plan-current", "compiled"), ("plan-original", "original")):
        row, _, compiled, _ = _save_delivery(
            session, task_id, artifact_kind=artifact_kind,
        )
        if artifact_kind == "original":
            with pytest.raises(ValueError, match="differs from.*compiled"):
                asyncio.run(session.build_from_artifact(task_id, row["artifact"]["sha256"]))
        else:
            result = asyncio.run(session.build_from_artifact(task_id, row["artifact"]["sha256"]))
            assert not result.get("isError")
            sent = json.loads(session.frozen.calls[-1][1]["plan_json"])
            assert sent["regularization"] == compiled["regularization"]
            assert sent["footprint_pixels"] == compiled["footprint_pixels"]


def test_build_rejects_forgery_escape_changed_bytes_and_nonobject_json(environment):
    _, make = environment
    session = make()

    # The forged plan is itself an immutable artifact; rejection therefore
    # comes from binding it to the trial rather than from registry file checks.
    forged, _, _, _ = _save_delivery(session, "plan-forged", artifact_kind="forged")
    calls_before = len(session.frozen.calls)
    with pytest.raises(ValueError):
        asyncio.run(session.build_from_artifact("plan-forged", forged["artifact"]["sha256"]))
    assert len(session.frozen.calls) == calls_before

    escaped, _, _, _ = _save_delivery(
        session, "plan-escape", validation_changes={"compiled_numeric_plan_file": "../escaped.json"},
    )
    with pytest.raises(ValueError):
        asyncio.run(session.build_from_artifact("plan-escape", escaped["artifact"]["sha256"]))
    assert len(session.frozen.calls) == calls_before

    changed, _, _, workspace = _save_delivery(session, "plan-changed-bytes")
    receipt = changed["validation"]
    compiled_path = workspace / receipt["compiled_numeric_plan_file"]
    compiled_path.write_bytes(compiled_path.read_bytes() + b" ")
    with pytest.raises(ValueError):
        asyncio.run(session.build_from_artifact(
            "plan-changed-bytes", changed["artifact"]["sha256"],
        ))
    assert len(session.frozen.calls) == calls_before

    nonobject, _, _, _ = _save_delivery(
        session, "plan-nonobject", artifact_kind="original", compiled_file_bytes=b"[]\n",
    )
    with pytest.raises(ValueError):
        asyncio.run(session.build_from_artifact(
            "plan-nonobject", nonobject["artifact"]["sha256"],
        ))
    assert len(session.frozen.calls) == calls_before


def test_rework_uses_compiled_delivery_and_rejects_original_declaration(
    environment, monkeypatch,
):
    store, make = environment
    session = make()
    expected = {}
    for suffix, artifact_kind in (("current", "compiled"), ("original", "original")):
        _, original, compiled, _ = _save_delivery(
            session, f"plan-{suffix}", artifact_kind=artifact_kind,
        )
        expected[f"next-{suffix}"] = compiled if artifact_kind == "compiled" else original

    captured = {}

    @asynccontextmanager
    async def isolated_trial(directory, image_name, *, root):
        trial = PlanTrial(None, image_name=image_name)
        captured["trial"] = trial
        yield trial

    monkeypatch.setattr("src.agent.runtime_roles.trial.PlanTrialSession", isolated_trial)

    def stop_at_adapter_boundary(task_id, *args):
        plan, digest = captured["trial"].baseline()
        assert plan == expected[task_id]
        assert digest == canonical_plan_sha256(expected[task_id])
        raise ValueError("compiled rework baseline checked")

    session.adapter_factory = stop_at_adapter_boundary
    for suffix in ("current", "original"):
        result = asyncio.run(session.delegate_many([{
            **dispatch(f"next-{suffix}"), "role_id": "plan_reader", "target": "F1",
            "previous_task_id": f"plan-{suffix}", "issues": ["correct the declared wall"],
            "rework_targets": ["plan.partitions:wall-A"],
        }]))["results"][0]
        assert result["status"] == "failed"
        assert ("compiled rework baseline checked" if suffix == "current" else "differs from its successful trial") in result["reason"]
    assert not any(event.payload.event_type == "adapter_request" for event in store.all_events)
