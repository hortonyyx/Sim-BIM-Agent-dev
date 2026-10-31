"""Failed declarations remain evidence and repair bases, never accepted geometry."""

import asyncio
import copy
import hashlib
import json
from contextlib import asynccontextmanager

import pytest
from PIL import Image

from src.agent.runtime_roles.readers import ReaderTools
from src.agent.runtime_roles.session import envelope
from src.agent.runtime_roles.trial import PlanTrial
from src.harness_contracts import HashedBlobRef, ToolExecutionPayload
from tests.test_role_readers import Frozen
from tests.test_role_session import dispatch, environment  # noqa: F401
from tests.test_role_trial import Tools, example


def workspace(path, image_name="plan.png"):
    (path / "images").mkdir(parents=True)
    Image.new("RGB", (100, 100), "white").save(path / "images" / image_name)
    digest = hashlib.sha256((path / "images" / image_name).read_bytes()).hexdigest()
    (path / "inputs.json").write_text(json.dumps({"images": {
        image_name: {"size": [100, 100], "sha256": digest},
    }}), encoding="utf-8")
    return digest


def op(**value):
    return {"reason": "located original evidence", "source_refs": ["plan.png: original"],
            "bbox": [0, 0, 20, 20], **value}


def unhosted_plan():
    value = example()
    value["openings"][0].update(p1=[4, 3], p2=[4, 4])
    return value


def test_precompiler_geometry_failure_is_durable_readable_and_locally_repairable(tmp_path):
    async def scenario():
        image_sha = workspace(tmp_path)
        tools = Tools(workspace=tmp_path)
        trial = PlanTrial(tools, image_name="plan.png", workspace=tmp_path,
                          receipt_directory=tmp_path / "trial_receipts")
        first = await trial.run(unhosted_plan())
        assert first["status"] == "failed" and first["compiled_numeric_plan_sha256"] is None
        assert first["unhosted_openings"][0]["opening"] == "D1" and tools.calls == []
        assert first["editable_draft"]["editable"] is True
        partial = await trial.run(operations=[op(op="update", collection="openings", id="D1",
                                                 changes={"z": [0, 2.2]})])
        assert partial["status"] == "failed" and tools.calls == []
        resumed = PlanTrial(tools, image_name="plan.png", workspace=tmp_path,
                            receipt_directory=tmp_path / "trial_receipts")
        assert resumed.baseline()[1] == partial["plan_sha256"]
        assert resumed.read_draft(field="openings", object_id="D1")["value"]["z"] == [0, 2.2]
        assert resumed.read_draft(trial_id="trial_001", field="plan")["plan"] == unhosted_plan()
        selected, _ = resumed.verified_failed_draft(image_sha, receipt_file=partial["receipt_file"],
                                                   plan_sha256=partial["plan_sha256"])
        assert selected["openings"][0]["z"] == [0, 2.2]
        with pytest.raises(ValueError, match="successful isolated trial"):
            resumed.verified_plan(partial["plan_sha256"])
        original = example()["openings"][0]
        passed = await resumed.run(operations=[op(op="update", collection="openings", id="D1",
                                                  changes={key: original[key] for key in ("p1", "p2")})])
        assert passed["status"] == "passed" and tools.calls == []
        assert resumed.verified_plan(passed["plan_sha256"])[0]["openings"][0]["z"] == [0, 2.2]
    asyncio.run(scenario())


def test_format_failures_remain_explicit_and_cannot_bypass_complete_validation(tmp_path):
    async def scenario():
        workspace(tmp_path)
        trial = PlanTrial(Tools(workspace=tmp_path), image_name="plan.png", workspace=tmp_path,
                          receipt_directory=tmp_path / "trial_receipts")
        incomplete = example()
        incomplete.pop("x_anchors")
        first = await trial.run(incomplete)
        assert first["error_type"] == "plan_format" and first["editable_draft"]["editable"]
        still_bad = await trial.run(operations=[op(op="set", field="basis", value="updated observation")])
        assert still_bad["status"] == "failed" and trial.tools.calls == []
        with pytest.raises(ValueError, match="successful isolated trial"):
            trial.verified_plan(still_bad["plan_sha256"])
        repaired = await trial.run(operations=[op(op="set", field="x_anchors", value=example()["x_anchors"])])
        assert repaired["status"] == "passed"
        assert trial.read_draft(trial_id="trial_001", field="plan")["plan"] == incomplete
        duplicate = example()
        duplicate["openings"].append(copy.deepcopy(duplicate["openings"][0]))
        invalid = await trial.run(duplicate)
        assert invalid["editable_draft"]["editable"] is False
        assert "duplicate IDs" in invalid["editable_draft"]["reason"]
        assert trial.read_draft(field="plan")["plan"] == duplicate
        assert trial.baseline()[1] == repaired["plan_sha256"]
        resumed = PlanTrial(Tools(), image_name="plan.png", workspace=tmp_path,
                            receipt_directory=tmp_path / "trial_receipts")
        assert resumed.read_draft()["editable"] is False
        assert resumed.baseline()[1] == repaired["plan_sha256"]
    asyncio.run(scenario())


def test_failed_object_deletion_and_invalid_operations_keep_last_passed_plan(tmp_path, monkeypatch):
    async def scenario():
        workspace(tmp_path)
        trial = PlanTrial(Tools(), image_name="plan.png", workspace=tmp_path,
                          receipt_directory=tmp_path / "trial_receipts")
        passed = await trial.run(example())
        assert passed["status"] == "passed"
        def failed_compiler(*args, **kwargs):
            raise ValueError("synthetic local compiler failure after the declared deletion")
        with monkeypatch.context() as patch:
            patch.setattr("src.agent.runtime_roles.plan_reading.build_reading", failed_compiler)
            deleted = await trial.run(operations=[op(op="remove", collection="openings", id="D1")])
        assert deleted["status"] == "failed"
        assert trial.baseline()[1] == deleted["plan_sha256"]
        with pytest.raises(ValueError, match="does not exist"):
            await trial.run(operations=[op(op="remove", collection="openings", id="MISSING")])
        assert trial.baseline()[1] == deleted["plan_sha256"]
        assert trial.verified_plan(passed["plan_sha256"])[0] == example()
        assert trial.read_draft()["last_passed_plan_sha256"] == passed["plan_sha256"]
    asyncio.run(scenario())


def test_profile_evidence_survives_failure_before_numeric_product(tmp_path):
    async def scenario():
        prior_path = tmp_path / "prior"
        image_sha = workspace(prior_path)
        profiles = prior_path / "profiles"
        profiles.mkdir()
        profile = {"name": "plan.png", "image_sha256": image_sha, "axis": "x",
                   "candidates": [{"id": "C01", "pixels": [1, 1], "peak": 1}]}
        (profiles / "profile_001.json").write_text(json.dumps(profile), encoding="utf-8")
        value = unhosted_plan()
        value["x_anchors"][0][0] = {"profile": "profile_001", "candidate": "C01"}
        prior = PlanTrial(Tools(), image_name="plan.png", workspace=prior_path,
                          receipt_directory=prior_path / "trial_receipts", profile_directory=profiles)
        failed = await prior.run(value)
        assert failed["status"] == "failed" and failed["compiled_numeric_plan_sha256"] is None
        assert failed["measurement_profiles"][0]["profile_id"] == "profile_001"
        current_path = tmp_path / "current"
        assert workspace(current_path) == image_sha
        current = PlanTrial(Tools(), image_name="plan.png", workspace=current_path,
                            profile_directory=current_path / "profiles")
        current.inherit_failed_reference(prior, image_sha, receipt_file=failed["receipt_file"],
                                         plan_sha256=failed["plan_sha256"])
        assert current.inherited_reference_ids() == {"profile_001"}
        saved = prior_path / failed["measurement_profiles"][0]["file"]
        saved.write_text("{}", encoding="utf-8")
        with pytest.raises(ValueError, match="profile changed"):
            current.inherit_failed_reference(prior, image_sha, receipt_file=failed["receipt_file"],
                                             plan_sha256=failed["plan_sha256"])
    asyncio.run(scenario())


def test_reader_has_read_only_draft_inspection_without_compilation(tmp_path):
    async def scenario():
        workspace(tmp_path)
        trial = PlanTrial(Tools(), image_name="plan.png", workspace=tmp_path,
                          receipt_directory=tmp_path / "trial_receipts")
        value = unhosted_plan()
        assert (await trial.run(value))["status"] == "failed"
        reader = ReaderTools(Frozen(), role_id="plan_reader", image_name="plan.png", trial=trial)
        result = await reader.call_tool("read_plan_draft", {"field": "openings", "object_id": "D1"})
        assert result["structuredContent"]["value"] == value["openings"][0]
        assert trial.tools.calls == [] and len(trial.receipts) == 1
        assert reader.repeatability("read_plan_draft") == "read_only"
        assert reader.recovery_policy("read_plan_draft") == "retry_read"
        elevation = ReaderTools(Frozen(), role_id="elevation_reader", image_name="plan.png")
        with pytest.raises(ValueError, match="outside elevation_reader"):
            await elevation.call_tool("read_plan_draft", {})
    asyncio.run(scenario())


@pytest.mark.parametrize("tamper", ["disk", "memory"])
def test_inherited_failed_report_is_bounded_stable_and_tamper_checked(tmp_path, tamper):
    async def scenario():
        image_sha = workspace(tmp_path)
        prior = PlanTrial(Tools(), image_name="plan.png", workspace=tmp_path,
                          receipt_directory=tmp_path / "trial_receipts")
        failed = await prior.run(unhosted_plan())
        failed["building_precision"] = {"status": "fail", "items": [
            {"id": f"issue-{i}", "details": "located geometry evidence " * 500} for i in range(24)]}
        receipt_path = tmp_path / failed["receipt_file"]
        receipt_path.write_text(json.dumps(failed), encoding="utf-8")
        original = receipt_path.read_bytes()
        current = PlanTrial(Tools(), image_name="plan.png")
        current.inherit_failed_reference(prior, image_sha, receipt_file=failed["receipt_file"],
                                         plan_sha256=failed["plan_sha256"])
        first = current.read_draft(field="report:/building_precision/items")
        assert first["inherited"] and first["trial_status"] == "failed"
        assert first["next"]["trial_id"] == "reference"
        assert len(json.dumps(first).encode()) < 16_000

        # A new local trial uses the same trial_001 label; inherited references
        # must keep pointing to the admitted original report and declaration.
        changed = unhosted_plan()
        changed["basis"] = "new observation in this reader"
        await current.run(changed)
        page = current.read_draft(**first["next"])
        assert page["entries"][0]["key"] == 8
        assert page["reference"]["read"]["arguments"]["trial_id"] == "reference"
        assert current.read_draft(trial_id="reference", field="plan")["plan"] == unhosted_plan()
        assert current.read_draft(field="basis")["value"] == changed["basis"]
        assert receipt_path.read_bytes() == original
        if tamper == "disk":
            saved = json.loads(original)
            saved["building_precision"]["status"] = "pass"
            receipt_path.write_text(json.dumps(saved), encoding="utf-8")
        else:
            prior.receipts[0]["building_precision"]["status"] = "pass"
        with pytest.raises(ValueError, match="changed since"):
            current.read_draft(trial_id="reference", field="report:/building_precision")
    asyncio.run(scenario())


def test_previous_task_inherits_precompiler_draft_and_enforces_scope(environment, monkeypatch):
    store, make = environment
    session = make()
    task = session._task({**dispatch("plan-failed"), "role_id": "plan_reader", "target": "F1"})
    child = session.registry.admit(task)
    path = child.task_directory / "bim/trial_workspace"
    workspace(path, "north.png")
    trial = PlanTrial(Tools(), image_name="north.png", workspace=path, receipt_directory=path / "trial_receipts")
    receipt = asyncio.run(trial.run(unhosted_plan()))
    captured = child.capture(envelope(receipt, error=True))
    child.append(ToolExecutionPayload(call_id="failed-trial", tool_name="trial_plan_bim", full_arguments={"plan": {}},
        raw_result=captured, shown_result=captured, repeatability="non_idempotent_write",
        operation_key="failed-trial", outcome="failed"))
    # A known prewrite operation rejection must not hide the earlier editable
    # declaration. Unknown outcomes remain covered by the existing recovery test.
    reader = ReaderTools(Frozen(), role_id="plan_reader", image_name="north.png", trial=trial)
    rejected = asyncio.run(reader.call_tool("trial_plan_bim", {"operations": []}))
    assert rejected["structuredContent"]["draft_unchanged"] is True
    captured_rejection = child.capture(rejected)
    child.append(ToolExecutionPayload(call_id="invalid-ops", tool_name="trial_plan_bim", full_arguments={"operations": []},
        raw_result=captured_rejection, shown_result=captured_rejection,
        repeatability="non_idempotent_write", operation_key="invalid-ops", outcome="failed"))
    session.registry.save(task, status="failed", reason="answer_validation_failed")
    handoff = session._failure_handoff("plan-failed")
    assert handoff["editable_draft"]["compiled_numeric_plan_sha256"] is None
    current = {}

    @asynccontextmanager
    async def isolated_trial(directory, image_name, *, root):
        current["trial"] = PlanTrial(Tools(), image_name=image_name)
        yield current["trial"]

    monkeypatch.setattr("src.agent.runtime_roles.trial.PlanTrialSession", isolated_trial)

    def stop_at_adapter_boundary(*args):
        inherited = current["trial"]
        assert inherited.baseline() == (unhosted_plan(), receipt["plan_sha256"])
        assert inherited.reference_validation_passed is False
        assert inherited.allowed_rework_targets == ["plan.openings:D1"]
        raise ValueError("verified inherited failed draft without model request")

    session.adapter_factory = stop_at_adapter_boundary
    result = asyncio.run(session.delegate_many([{
        **dispatch("plan-rework"), "role_id": "plan_reader", "target": "F1", "previous_task_id": "plan-failed",
        "issues": ["correct D1 host"], "rework_targets": ["plan.openings:D1"],
    }]))["results"][0]
    assert "verified inherited" in result["reason"]
    assert not any(event.payload.event_type == "adapter_request" for event in store.all_events)
    saved = path / receipt["input_plan_file"]
    saved.write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="changed"):
        session._failure_handoff("plan-failed")


def test_role_state_keeps_full_audit_off_model_context(environment, monkeypatch):
    store, make = environment
    session = make()
    complete = {"requests": 1000, "provider_reported_tokens": None, "usage_complete": False,
                "ledger": {"reservations": [{"id": str(i), "details": "large-audit" * 100} for i in range(1000)]},
                "by_role": {"plan_reader": {"failures": ["detailed-reason"]}}}
    monkeypatch.setattr("src.agent.runtime_roles.session.role_accounting", lambda *_: complete)
    visible = asyncio.run(session.call_tool("role_state", {}))["structuredContent"]
    assert visible["usage"] == {key: complete[key] for key in ("requests", "provider_reported_tokens", "usage_complete")}
    assert len(json.dumps(visible)) < 3000
    assert "ledger" not in visible["usage"] and "by_role" not in visible["usage"]
    assert json.loads((store.directory / "role_accounting.json").read_bytes()) == complete
    ref = HashedBlobRef.model_validate(visible["audit"]["blob"])
    assert json.loads(store.get_bytes(ref)) == complete
    assert visible["budget"]["available"] and session.state()["usage"] == complete
