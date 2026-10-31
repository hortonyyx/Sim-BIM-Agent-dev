"""Real frozen-MCP D1 role pipeline and process-death recovery checks."""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections import Counter

import jsonschema
import pytest
from PIL import Image

from src.agent.geometry.plan_drawing_differences import drawing_differences
from src.agent.runtime_roles.entry import execute
from src.agent.runtime_roles.plan_review import opening_hosts, topology_issues
from src.agent.runtime_roles.submission import ELEVATION_SCHEMA, PLAN_SCHEMA
from src.agent.runtime_roles.trial import canonical_plan_sha256

from role_d1_fixtures import (
    CASES,
    assembly_review,
    elevation_submission,
    event_counts,
    make_fixture,
    plan_submission,
    reader_record,
)


class SimulatedProcessDeath(BaseException):
    pass


class CrashOnce:
    def __init__(self, boundary, predicate):
        self.boundary = boundary
        self.predicate = predicate
        self.triggered = False

    def __call__(self, boundary, engine):
        if not self.triggered and boundary == self.boundary and self.predicate(engine):
            self.triggered = True
            raise SimulatedProcessDeath(boundary)


def _events(output):
    rows = []
    for path in output.rglob("events.jsonl"):
        for line in path.read_text(encoding="utf-8").splitlines():
            rows.append((path.relative_to(output).as_posix(), json.loads(line)["payload"]))
    return rows


def _reader_record_hashes(fixture):
    result = {}
    for path in (fixture.output / "tasks").glob("*/reader_record.json"):
        row = json.loads(path.read_bytes())
        result[row["task_id"]] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def _reader_events(fixture, task_id):
    # Child EventStores share the root append-only journal; task_id is carried
    # by the event envelope rather than a per-task events file.
    return [
        row["payload"]
        for line in (fixture.output / "events.jsonl").read_text(encoding="utf-8").splitlines()
        if (row := json.loads(line))["task_id"] == task_id
    ]


def _reader_request_count(fixture):
    return sum(
        row.get("event_type") == "adapter_request"
        for task in fixture.tasks
        for row in _reader_events(fixture, task["task_id"])
    )


def _tool_count(output, name):
    return sum(
        row.get("event_type") == "tool_invocation" and row.get("tool_name") == name
        for _, row in _events(output)
    )


def _resume(fixture, *, fault_hook=None, reader_fault_hook=None):
    fixture.args.resume = True
    return asyncio.run(
        execute(
            fixture.args,
            adapter_factory=fixture.adapter_factory,
            fault_hook=fault_hook,
            reader_fault_hook=reader_fault_hook,
        )
    )


def _records(fixture):
    return [reader_record(fixture.output, task["task_id"]) for task in fixture.tasks]


def _assert_complete_pipeline(fixture, result, *, two_floors):
    assert result["status"] == "completed"
    assert result["finalization"]["status"] == "delivered"
    assert (fixture.output / "bim/delivery.json").is_file()
    assert (fixture.output / "bim/delivery.html").is_file()
    assert len(list((fixture.output / "bim").glob("candidate_*"))) <= 24
    selection = json.loads(
        (fixture.output / "bim/delivery_selection.json").read_bytes()
    )
    source = json.loads(
        (
            fixture.output
            / "bim"
            / selection["candidate"]
            / "source_model.json"
        ).read_bytes()
    )
    assert source["source_model_sha256"] == selection["source_model_sha256"]
    assert len({space["floor_id"] for space in source["spaces"]}) == (2 if two_floors else 1)

    records = _records(fixture)
    assert all(record["status"] == "completed" for record in records)
    plan_records = [record for record in records if record["role_id"] == "plan_reader"]
    assert all(record["validation"]["validation_passed"] for record in plan_records)
    assert all(record["artifact"]["sha256"] for record in records)

    counts = event_counts(fixture.output)
    tools = Counter(counts.tools)
    assert tools["inputs"] == 1
    assert tools["delegate_readers"] == 1
    assert tools["trial_plan_bim"] == len(plan_records)
    assert tools["submit_plan_reading"] == len(plan_records)
    assert tools["submit_elevation_reading"] == 4
    # Deterministic alignment needs no review. sm24's two current >30 cm
    # differences each need paired evidence, an explicit decision and one
    # reassembly; the two conflict-free fixtures still assemble only once.
    has_position_conflicts = fixture.case_name == "sm24"
    assert tools["assemble_from_readers"] == (2 if has_position_conflicts else 1)
    assert tools["review_role_assembly"] == 0
    assert tools["role_state"] == (2 if has_position_conflicts else 0)
    assert tools["edit_bim"] == (1 if has_position_conflicts else 0)
    assert not any(tools[name] for name in (
        "read_role_artifact", "build_from_artifact", "match_elevation",
        "apply_elevation_heights", "assemble_plan_bim", "inspect_plan_draft"))
    assert tools["check_openings"] == 1
    assert tools["inspect_candidate"] == 1
    assert tools["finish_bim"] == 1
    receipt = next((fixture.output / "role_assemblies").glob("*.json"))
    assembled = json.loads(receipt.read_bytes())["response"]
    assert assembled["candidate"] == selection["candidate"]
    assert assembled["height_write"]["status"] == "completed"
    source = json.loads((fixture.output / "bim" / assembled["candidate"] / "source_model.json").read_bytes())
    opening_z = {row["id"]: sorted({p[2] for p in row["vertices"]}) for row in source["openings"]}

    latest_matches = {}
    for path in sorted((fixture.output / "role_matches").glob("*.json"), key=lambda p: p.stat().st_mtime_ns):
        value = json.loads(path.read_bytes())
        latest_matches[value["task_id"]] = value
    matches = list(latest_matches.values())
    assert len(matches) == 4
    assert all(row["result"]["matches"] for row in matches)
    # Independent position differences remain visible and are never converted
    # into an automatic/default delivery resolution.
    all_positions = json.loads(
        (fixture.output / "role_position_review.json").read_bytes()
    )["items"]
    positions = {
        decision_id: row
        for decision_id, row in all_positions.items()
        if row["bucket"] == "gt_30cm"
    }
    assert all(row["status"] != "pending" for row in all_positions.values())
    assert len(positions) == (2 if has_position_conflicts else 0)
    assert all(row["status"] == "decided" for row in positions.values())
    assert all(row["decision"]["choice"] == "keep_plan"
               and not row["decision"].get("automatic")
               and len(row["decision"]["views"]) == 2
               for row in positions.values())
    saved_views = {
        path.stem for path in (fixture.output / "bim/image_views").glob("view_*.json")
    }
    assert len(saved_views) == (4 if has_position_conflicts else 0)
    assert all({view["view_id"] for view in row["decision"]["views"]} <= saved_views
               for row in positions.values())
    for match in matches:
        for conflict in match["result"]["conflicts"]:
            assert conflict["type"] == "position_or_width_conflict"
            assert any(row["source_opening_id"] == conflict["source_opening_id"]
                       for row in positions.values())
    assert all(not row["result"]["source_only"] for row in matches)
    assert all(not row["result"]["elevation_only"] for row in matches)
    for match in matches:
        for row in match["result"]["matches"]:
            assert opening_z[row["source_opening_id"]] == [row["sill_m"], row["head_m"]]
    delivery = json.loads((fixture.output / "bim/delivery.json").read_bytes())
    delivered_positions = delivery["position_review"]["items"]
    assert delivered_positions == all_positions
    assert all("delivery_resolution" not in row for row in delivered_positions.values())
    accounting = result["role_accounting"]
    assert accounting["requests"] > 0
    assert accounting["by_role"]["coordinator"]["requests"] > 0
    assert accounting["by_role"]["plan_reader"]["requests"] == len(plan_records) * 2
    assert accounting["by_role"]["elevation_reader"]["requests"] == 8
    progress = accounting["by_role"]["plan_reader"]["before_first_trial"]
    assert {row["task_id"] for row in progress} == {row["task_id"] for row in plan_records}
    assert all(row["observation_calls_before_first_trial"] == 0
               and row["first_trial_elapsed_seconds"] >= 0 for row in progress)
    review = assembly_review(fixture.output)
    assert review and review["status"] in {"unchanged", "reviewed"}
    if review["status"] == "reviewed":
        assert {row["change_id"] for row in review["changes"]} == {
            row["change_id"] for row in review["decisions"]
        }


@pytest.mark.parametrize(("case_name", "two_floors"), [("sm21", True), ("sm24", False)])
def test_scripted_role_pipeline_runs_real_frozen_mcp(tmp_path, case_name, two_floors):
    fixture = make_fixture(case_name, tmp_path)
    result = asyncio.run(
        execute(fixture.args, adapter_factory=fixture.adapter_factory)
    )
    _assert_complete_pipeline(fixture, result, two_floors=two_floors)


def test_cross_storey_conflict_stops_without_claiming_a_complete_building(tmp_path):
    fixture = make_fixture("sm25", tmp_path)
    # Twelve reader requests plus inputs/delegate/assemble and one explicit
    # unresolved answer: stop deterministically after the rejection is observed.
    fixture.args.model_calls = 16
    result = asyncio.run(execute(fixture.args, adapter_factory=fixture.adapter_factory))
    assert result["status"] == "model_budget_exhausted"
    assert result.get("completion") is None
    assert not (fixture.output / "bim/delivery_selection.json").exists()
    report = json.loads(next((fixture.output / "role_assemblies").glob("*.json")).read_bytes())["response"]
    assert report["status"] == "build_failed" and report["candidate"] is None
    detail = next(row["detail"] for row in report["decisions"] if row["kind"] == "assembly_failed")
    rejections = detail["regularization"]["rejections"]
    assert {row["type"] for row in rejections} == {"storey_wall_offset", "thin_horizontal_contact"}
    assert next(row for row in rejections if row["type"] == "storey_wall_offset")["deviation_m"] == pytest.approx(.1)
    assert next(row for row in rejections if row["type"] == "thin_horizontal_contact")["width_m"] == pytest.approx(.1)
    assert all(row["tolerance_m"] == .3 for row in rejections)
    assert not detail["source_geometry_ready"] and detail["saved_candidate"] is None
    assert _tool_count(fixture.output, "finish_bim") == 0
    assert not (fixture.output / "bim/delivery.json").exists()
    assert result["finalization"]["delivery"] is None
    assert result["finalization"]["status"] == "assembly_review_required"


@pytest.mark.parametrize("case_name", ["sm21", "sm24", "sm25"])
def test_fixture_submissions_match_explicit_tool_contracts(tmp_path, case_name):
    fixture = make_fixture(case_name, tmp_path)
    assert fixture.args.max_candidates == 24
    for artifact in fixture.plan_artifacts.values():
        arguments = plan_submission(artifact)
        jsonschema.validate(arguments, PLAN_SCHEMA)
        assert len(opening_hosts(artifact["plan"])) == len(artifact["plan"].get("openings", []))
        image_name = "1f_view.png" if artifact["plan"]["floor_id"] == "F1" else "2f_view.png"
        with Image.open(CASES[case_name] / "images" / image_name) as image:
            differences = drawing_differences(image, artifact["plan"])
        assert topology_issues([{
            "plan_sha256": canonical_plan_sha256(artifact["plan"]),
            "drawing_differences": differences,
        }]) == []
        assert arguments["topology_decisions"] == []
    for artifact in fixture.elevation_artifacts.values():
        arguments = elevation_submission(artifact)
        jsonschema.validate(arguments, ELEVATION_SCHEMA)
        assert set(arguments) == set(ELEVATION_SCHEMA["required"])


def test_resume_reader_after_trial_checkpoint_does_not_repeat_trial(tmp_path):
    fixture = make_fixture("sm24", tmp_path)
    crash = CrashOnce(
        "after_checkpoint",
        lambda engine: engine.role.role_id == "plan_reader"
        and engine.counts["tool_calls"] == 1,
    )
    with pytest.raises(SimulatedProcessDeath):
        asyncio.run(
            execute(
                fixture.args,
                adapter_factory=fixture.adapter_factory,
                reader_fault_hook=crash,
            )
        )
    assert crash.triggered
    assert _tool_count(fixture.output, "trial_plan_bim") == 1

    result = _resume(fixture)
    _assert_complete_pipeline(fixture, result, two_floors=False)
    assert _tool_count(fixture.output, "trial_plan_bim") == 1
    plan_task = next(task for task in fixture.tasks if task["role_id"] == "plan_reader")
    plan_events = [
        row
        for row in _reader_events(fixture, plan_task["task_id"])
        if row.get("event_type") == "adapter_request"
    ]
    assert len(plan_events) == 2


def test_resume_after_all_readers_preserves_reader_records(tmp_path):
    fixture = make_fixture("sm24", tmp_path)

    def all_readers_before_build(engine):
        return (
            len(_reader_record_hashes(fixture)) == len(fixture.tasks)
            and _tool_count(fixture.output, "assemble_from_readers") == 0
        )

    crash = CrashOnce("after_checkpoint", all_readers_before_build)
    with pytest.raises(SimulatedProcessDeath):
        asyncio.run(
            execute(
                fixture.args,
                adapter_factory=fixture.adapter_factory,
                fault_hook=crash,
            )
        )
    hashes_at_crash = _reader_record_hashes(fixture)
    reader_requests_at_crash = _reader_request_count(fixture)
    assert reader_requests_at_crash > 0

    result = _resume(fixture)
    _assert_complete_pipeline(fixture, result, two_floors=False)
    assert _reader_record_hashes(fixture) == hashes_at_crash
    assert _reader_request_count(fixture) == reader_requests_at_crash


@pytest.mark.parametrize("boundary_tool", ["build_plan_bim", "claim_transaction"])
def test_resume_after_inner_receipt_does_not_repeat_write(tmp_path, monkeypatch, boundary_tool):
    from src.agent.runtime_roles.session import RoleSession

    fixture = make_fixture("sm24", tmp_path)
    original = RoleSession._once
    crashed = False

    async def stop_after_receipt(session, operation_id, name, arguments, *, reference):
        nonlocal crashed
        result = await original(session, operation_id, name, arguments, reference=reference)
        if name == boundary_tool and not crashed:
            crashed = True
            raise SimulatedProcessDeath("inner build receipt saved")
        return result

    monkeypatch.setattr(RoleSession, "_once", stop_after_receipt)
    with pytest.raises(SimulatedProcessDeath):
        asyncio.run(execute(fixture.args, adapter_factory=fixture.adapter_factory))
    assert crashed
    count_at_crash = len(list((fixture.output / "bim").glob("candidate_*")))
    assert count_at_crash == 1
    result = _resume(fixture)
    _assert_complete_pipeline(fixture, result, two_floors=False)
    operations = [json.loads(p.read_bytes()) for p in (fixture.output / "role_operations").glob("*.json")]
    assert sum(row["tool"] == "build_plan_bim" for row in operations) == 1
    # Current recovery reuses both durable operation receipts; neither the
    # original build nor the claim batch is issued again.
    assert sum(row["tool"] == "claim_transaction" for row in operations) == 1
    assert len(list((fixture.output / "bim").glob("candidate_*"))) == count_at_crash
    assert len(list((fixture.output / "bim/plan_drafts").glob("draft_*"))) == 1
