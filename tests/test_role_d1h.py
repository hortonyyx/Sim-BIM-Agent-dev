"""One-step assembly behavior, using local deterministic geometry only."""

import asyncio
import copy
import hashlib
import json
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts.tool_scripts.run_bim_agent import Toolkit
from src.agent.runtime_roles.assembly import select_deliveries
from src.agent.runtime_roles.config import load_roles
from src.agent.runtime_roles.elevation import validate_elevation_artifact
from src.agent.runtime_roles.elevation_regularization import regularize_elevation_artifact
from src.agent.runtime_roles.levels import resolve_levels
from src.agent.runtime_roles.session import EXTRA_TOOLS, RoleSession, envelope, role_parameters
from src.agent_runtime.loop import RunLimits
from src.agent_runtime.providers import provider_parameters
from src.agent_runtime.store import EventStore, json_bytes
from tests.test_bim_claims import setup_run
from tests.test_role_configuration import role, roles
from tests.test_role_d1g import height_session


def level_registry():
    elevations = {}
    for side, kind, top in (("North", "roof", 6), ("South", "eave", 6.01), ("East", "other", 6)):
        elevations[side] = {"image": "plan.png", "elevations": [
            {"id": "base", "kind": "ground", "value_m": 0},
            {"id": "f2", "kind": "floor", "floor_id": "F2", "value_m": 3},
            {"id": "top", "kind": kind, "value_m": top}]}
    registry = SimpleNamespace(records={k: {"artifact": {"sha256": k}} for k in elevations},
                               read=lambda key, **kw: elevations[key])
    return registry, elevations


def test_levels_mix_reader_labels_keep_citations_and_require_explicit_conflict_resolution():
    registry, elevations = level_registry()
    elevations["North"]["elevations"][0].update(kind="floor", floor_id="F1")
    plans = {f: {"floor_id": f, "z_floor": {"value": 0 if f == "F1" else 3000, "unit": "mm"},
                 "ceiling_height": {"value": 2800, "unit": "mm"}} for f in ("F1", "F2")}
    resolved, issues = resolve_levels(registry, plans, elevations)
    assert not issues
    assert resolved["F1"]["ceiling_height"]["value_m"] == 3
    assert resolved["F2"]["z_floor"]["value_m"] == 3
    assert resolved["F2"]["ceiling_height"]["value_m"] == 3
    assert {r["task_id"] for r in resolved["F2"]["ceiling_height"]["references"]} == set(elevations)
    elevations["South"]["elevations"][-1]["value_m"] = 7
    conflicted, issues = resolve_levels(registry, plans, elevations)
    assert conflicted["F2"]["ceiling_height"]["value_m"] == 2.8
    assert [(r["floor_id"], r["field"]) for r in issues] == [("F2", "ceiling_height")]
    assert resolve_levels(registry, plans, elevations)[1] == issues
    fixed, pending = resolve_levels(registry, plans, elevations, [{"floor_id": "F2", "ceiling_height": 3,
        "ceiling_height_evidence": {"task_id": "North", "elevation_id": "top"}}])
    assert fixed["F2"]["ceiling_height"]["status"] == "override" and not pending
    elevations["South"]["elevations"].pop()
    missing, pending = resolve_levels(registry, plans, elevations)
    assert missing["F2"]["ceiling_height"]["status"] == "plan_assumption"
    assert pending[0]["missing_task_ids"] == ["South"]
    registry, elevations = level_registry()
    for artifact in elevations.values():
        artifact["elevations"][0].update(kind="floor", floor_id="F1")
    elevations["South"]["elevations"][1]["value_m"] = -3
    conflicted, pending = resolve_levels(registry, plans, elevations)
    assert list(conflicted) == ["F1", "F2"]
    assert conflicted["F1"]["ceiling_height"]["value_m"] == 2.8
    assert conflicted["F2"]["z_floor"]["value_m"] == 3
    assert {r["kind"] for r in pending} == {"level_unresolved"}


def test_role_knobs_preserve_old_glm_bytes_and_do_not_leak_between_roles():
    old = roles()
    parsed = {k: r.model_dump(mode="json") for k, r in load_roles(old).items()}
    assert json_bytes(parsed) == json_bytes(old)
    for cfg in parsed.values():
        assert json_bytes(role_parameters(cfg)) == json_bytes(provider_parameters(
            cfg["provider"], output_tokens=cfg["output_tokens"], reasoning_effort=cfg["reasoning_effort"]))
    qwen = {"provider": "paratera", "model": "Qwen3.8-27B", "output_tokens": 16384,
            "temperature": .7, "enable_thinking": True}
    parsed = load_roles(roles(plan_reader=qwen, elevation_reader={**qwen, "enable_thinking": False}))
    assert role_parameters(parsed["plan_reader"].model_dump())["enable_thinking"] is True
    assert role_parameters(parsed["elevation_reader"].model_dump())["enable_thinking"] is False
    assert role_parameters(parsed["plan_reader"].model_dump())["temperature"] == .7
    assert "temperature" not in role_parameters(parsed["coordinator"].model_dump())
    with pytest.raises(ValueError):
        load_roles(roles(plan_reader={**qwen, "reasoning_effort": "high"}))
    names = {t["name"] for t in EXTRA_TOOLS}
    assert "assemble_from_readers" in names
    assert not names & {"build_from_artifact", "match_elevation", "apply_elevation_heights"}


class LocalTools:
    def __init__(self, run):
        self.run_directory, self.toolkit, self.calls = run, Toolkit(run), []

    async def call_tool(self, name, args):
        self.calls.append((name, copy.deepcopy(args)))
        if name == "build_plan_bim":
            return envelope(self.toolkit.build_plan(args["image"], args["plan_json"]))
        if name == "assemble_plan_bim":
            return envelope(self.toolkit.assemble_plans(args["floors_json"]))
        if name == "revise_bim":
            return envelope(self.toolkit.revise(args["candidate"], args["operations_json"]))
        raise AssertionError(name)


def deliver_plan(session, task_id, floor, *, previous=None):
    task = {"task_id": task_id, "role_id": "plan_reader", "image": "plan.png", "target": floor,
            "instructions": "observed floor"}
    if previous:
        task.update(previous_task_id=previous, issues=["space name corrected"], rework_targets=["plan.space_seeds:room"])
    task = session._task(task)
    plan = {"reading_type": "plan", "floor_id": floor, "z_floor": {"value": 0 if floor == "F1" else 3000, "unit": "mm"},
        "ceiling_height": {"value": 3000, "unit": "mm"},
        "x_anchors": [[1, 0], [11, 10]], "y_anchors": [[1, 6], [7, 0]],
        "basis": "plan height assumption", "footprint_pixels": [[1, 1], [11, 1], [11, 7], [1, 7]],
        "partitions": [], "openings": [], "space_seeds": [{"id": "room", "point": [5, 4]}],
        "assumptions": [], "unresolved": []}
    if previous:
        plan["space_seeds"][0]["role"] = "office"
    trial = session.registry.child(task_id).task_directory / "bim/trial_workspace"
    shutil.copytree(session.run_directory / "images", trial / "images")
    shutil.copy2(session.run_directory / "inputs.json", trial / "inputs.json")
    from tests.test_role_trial import example, Tools
    from src.agent.runtime_roles.trial import PlanTrial
    plan["wall_metadata"] = example()["wall_metadata"]
    reading = PlanTrial(Tools(), image_name="plan.png", workspace=trial,
                        receipt_directory=trial / "trial_receipts")
    result = asyncio.run(reading.run(plan))
    assert result["reading_geometry_ready"], result
    session.registry.save(task, status="completed", artifact={"plan": reading.numeric_plan(result), "unresolved": []},
                          validation={**result, "validation_passed": True})


def test_repeated_assembly_and_one_floor_rework_reuse_unaffected_build(tmp_path):
    run, _ = setup_run(tmp_path)
    limits = RunLimits(model_calls=1, tool_calls=40, tokens=None, seconds=600)
    with EventStore(tmp_path / "journal", run_id="d1h", task_id="coordinator",
                    budget_limit=limits.ledger_limit()) as store, asyncio.Runner() as runner:
        frozen = LocalTools(run)
        session = RoleSession(store=store, frozen=frozen, routes={}, limits=limits,
            adapter_factory=lambda *args: pytest.fail("no model service allowed"), root=Path(__file__).resolve().parents[1])
        deliver_plan(session, "f1", "F1")
        deliver_plan(session, "f2", "F2")
        result = runner.run(session.call_tool("assemble_from_readers", {}))
        assert not result["isError"], result
        assert result["structuredContent"]["candidate"] == "candidate_03"
        assert runner.run(session.call_tool("assemble_from_readers", {}))["structuredContent"] == result["structuredContent"]
        assert len(frozen.calls) == 3
        deliver_plan(session, "f2_rework", "F2", previous="f2")
        selected, _, _ = select_deliveries(session)
        assert selected["F2"][0] == "f2_rework"
        with pytest.raises(ValueError):
            select_deliveries(session, ["f1", "f2"])
        changed = runner.run(session.call_tool("assemble_from_readers", {}))
        assert not changed["isError"], changed
        updated = session._source(changed["structuredContent"]["candidate"])
        room = next(row for row in updated["spaces"] if row["id"] == "F2:room")
        assert room["role"] == "office" and room["role_evidence"]["basis"] == "inferred"
        assert [n for n, _ in frozen.calls].count("build_plan_bim") == 3
        assert sum(json.loads(args["plan_json"])["floor_id"] == "F1" for name, args in frozen.calls
                   if name == "build_plan_bim") == 1
        assert runner.run(session.call_tool("assemble_from_readers", {}))["structuredContent"] == changed["structuredContent"]
        assert [n for n, _ in frozen.calls].count("revise_bim") == 1
        assert len(list(run.glob("candidate_*/report.json"))) == len(frozen.calls)


def test_vertical_conflict_is_excluded_before_safe_batch(tmp_path):
    with height_session(tmp_path) as session:
        artifact = session.registry.read("North")
        artifact.pop("artifact_sha256")
        artifact["openings"][0]["head_m"] = 8
        # This is a new synthetic reading, not a tampered replay of the prior
        # adopted values. Regenerate its audit before testing host height bounds.
        artifact, _ = regularize_elevation_artifact(artifact)
        artifact = validate_elevation_artifact(artifact)
        task = session._task({"task_id": "high_north", "role_id": "elevation_reader", "image": "plan.png",
                              "target": "North", "instructions": "high opening from elevation"})
        session.registry.save(task, status="completed", artifact=artifact, validation={"validation_passed": True})
        result = session.match("high_north", "candidate_01", height_bounds=True)["result"]
        assert not result["matches"] and not result["can_apply"]
        assert result["conflicts"][0]["type"] == "height_outside_host"
        assert session.match("South", "candidate_01", height_bounds=True)["result"]["can_apply"]
