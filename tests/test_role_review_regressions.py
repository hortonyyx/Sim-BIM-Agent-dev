"""Regression probes from the independent D1b A/C1-C3 review."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
from pathlib import Path

import pytest
from PIL import Image

from src.agent.runtime_roles.guidance import ELEVATION_EXAMPLE
from src.agent.runtime_roles.plan_review import opening_hosts, topology_issues, validate_topology
from src.agent.runtime_roles.readers import ReaderTools
from src.agent.runtime_roles.session import RoleSession
from src.agent.runtime_roles.submission import ReaderSubmission
from src.agent.runtime_roles.trial import PlanTrial
from src.agent_runtime.loop import RunLimits
from src.agent_runtime.store import EventStore
from test_agent_runtime import versions
from tests.test_role_readers import Frozen as ReaderFrozen
from tests.test_role_readers import plan as _plan
from tests.test_role_session import Frozen as SessionFrozen
from tests.test_role_session import ROUTES, delivered_adapter, dispatch
from tests.test_role_submission import PassedTrial, arguments, operation
from tests.test_role_trial import Tools


ROOT = Path(__file__).resolve().parents[1]


def plan():
    value = _plan()
    value["wall_metadata"] = [
        {"target": target, "thickness_m": thickness, "reference_side": "centerline",
         "evidence_status": "inferred", "basis": "Synthetic fixture", "source_refs": ["plan.png:fixture"]}
        for target, thickness in (("footprint:*", .2), ("partition:*", .1))]
    return value


def workspace(path):
    (path / "images").mkdir(parents=True, exist_ok=True)
    image = path / "images/plan.png"
    Image.new("RGB", (120, 120), "white").save(image)
    (path / "inputs.json").write_text(json.dumps({"images": {"plan.png": {
        "size": [120, 120], "sha256": hashlib.sha256(image.read_bytes()).hexdigest()}}}), encoding="utf-8")
    return path


class ScopeTrial:
    def __init__(self):
        self.calls = []

    async def call(self, value, **review):
        self.calls.append((value, review))
        raise ValueError("invalid plan schema: floor_id and pixel geometry are required")

    def durable_snapshot(self):
        return {"snapshot_sha256": "0" * 64}

    def artifacts(self):
        return []

    def image_origins(self, result):
        return {}


def _warning():
    return topology_issues([{
        "plan_sha256": "a" * 64,
        "reading_geometry_ready": True,
        "topology_dividers": {"P1": [[60, 10], [60, 110]]},
        "drawing_differences": {"items": [{
            "type": "unsupported_open_separator",
            "divider": "P1",
            "opening": "D1",
            "x_px": 60,
            "y_px": [10, 110],
            "look_box": [55, 8, 65, 112],
            "check": "one continuous space rather than a wall with an opening",
        }]},
    }])[0]


def _decision(warning, choice):
    return {
        "issue_id": warning["issue_id"],
        "decision": choice,
        "basis": "local drawing check",
        "bbox": warning["look_box"],
    }


def test_failed_compiled_malformed_plan_can_repair_missing_id(tmp_path):
    async def scenario():
        tools = Tools()
        trial = PlanTrial(tools, image_name="plan.png", workspace=workspace(tmp_path), receipt_directory=tmp_path / "receipts")
        malformed = plan()
        del malformed["openings"][0]["id"]
        first = await trial.run(malformed)
        assert first["status"] == "failed" and first["compiled_numeric_plan_sha256"] is None
        assert first["format_errors"] and tools.calls == []
        repaired = await trial.run(plan())
        assert repaired["status"] == "passed"
        assert repaired["changes"] == [] and repaired["base_plan_sha256"] is None

    asyncio.run(scenario())


def test_rework_change_bbox_must_be_inside_original_image(tmp_path):
    async def scenario():
        (tmp_path / "images").mkdir()
        image = tmp_path / "images" / "plan.png"
        # Shared plan() uses original-image coordinates through pixel 110.
        Image.new("RGB", (120, 120), "white").save(image)
        (tmp_path / "inputs.json").write_text(json.dumps({
            "images": {"plan.png": {
                "size": [120, 120],
                "sha256": hashlib.sha256(image.read_bytes()).hexdigest(),
            }},
        }), encoding="utf-8", newline="\n")
        tools = Tools(workspace=tmp_path)
        trial = PlanTrial(
            tools,
            image_name="plan.png",
            workspace=tmp_path,
            receipt_directory=tmp_path / "trial_receipts",
        )
        first = await trial.run(plan())
        changed = plan()
        changed["openings"][0]["p2"][1] += 1
        with pytest.raises(ValueError, match="bbox exceeds original image"):
            await trial.run(operations=[{**operation(p2=[10, 41]),
                "bbox": [999_999, 999_999, 1_000_000, 1_000_000]}])

    asyncio.run(scenario())


def test_retain_opening_cannot_move_flagged_object_to_another_wall():
    warning = _warning()
    value = plan()
    value["openings"].append({
        "id": "D1", "kind": "door", "p1": [10, 60], "p2": [10, 70],
        "z": [0, 2.1], "source_refs": ["moved to exterior wall"],
    })
    assert opening_hosts(value)[-1]["wall"] == "footprint:3"
    with pytest.raises(ValueError, match="flagged|gap|divider"):
        validate_topology([warning], [_decision(warning, "retain_opening")], value)


@pytest.mark.parametrize("renamed", [False, True])
def test_continuous_space_cannot_keep_flagged_divider_by_moving_it(renamed):
    warning = _warning()
    value = plan()
    value["partitions"][0]["points"] = [[61, 10], [61, 110]]
    if renamed:
        value["partitions"][0]["id"] = "P2"
    with pytest.raises(ValueError, match="spanning the flagged local gap|not moving its divider"):
        validate_topology([warning], [_decision(warning, "continuous_space")], value)

    value["partitions"] = []
    assert validate_topology(
        [warning], [_decision(warning, "continuous_space")], value,
    )[0]["decision"] == "continuous_space"


def test_opening_can_span_collinear_segments_of_one_declared_wall():
    value = plan()
    value["partitions"] = [{
        "id": "P1", "points": [[10, 50], [50, 50], [90, 50]],
        "source_refs": ["one straight physical wall"],
    }]
    value["openings"] = [{
        "id": "D1", "kind": "door", "p1": [40, 50], "p2": [60, 50],
        "z": [0, 2.1], "source_refs": ["door spans a collinear vertex"],
    }]
    assert opening_hosts(value)[0]["wall"] == "P1"


def test_unresolved_profile_remains_editable_without_becoming_accepted_geometry(tmp_path):
    async def scenario():
        trial = PlanTrial(Tools(), image_name="plan.png", workspace=workspace(tmp_path), receipt_directory=tmp_path / "receipts")
        unresolved = plan()
        unresolved["x_anchors"][0][0] = {"profile": "profile_999", "candidate": "C01"}
        first = await trial.run(unresolved)
        assert first["status"] == "failed" and first["compiled_numeric_plan_sha256"] is None

        repaired = await trial.run(plan())
        assert repaired["status"] == "passed"
        assert repaired["base_plan_sha256"] == first["plan_sha256"]
        assert trial.require_success(plan())["plan_sha256"] == repaired["plan_sha256"]

    asyncio.run(scenario())


def _east_reading(floor_id="F1"):
    value = copy.deepcopy(ELEVATION_EXAMPLE)
    value["orientation"] = "East"
    value["view_direction"] = "West"
    value["x_calibration"].update(
        world_axis="y", world_start_m=0, world_end_m=10,
    )
    value["openings"][0]["floor_id"] = floor_id
    value["counts"][0]["floor_id"] = floor_id
    return value


def test_submission_is_bound_to_facade_and_floor_target():
    wrong_facade = copy.deepcopy(ELEVATION_EXAMPLE)
    wrong_facade["orientation"] = "South"
    wrong_facade["view_direction"] = "North"
    wrong_facade["x_calibration"].update(world_start_m=0, world_end_m=10)
    submission = ReaderSubmission(
        role_id="elevation_reader", image_name="East_view.png", target="East/F1",
    )
    with pytest.raises(ValueError, match="target|East"):
        submission.submit(wrong_facade)
    with pytest.raises(ValueError, match="target|F1"):
        submission.submit(_east_reading("F2"))
    assert submission.submit(_east_reading())["status"] == "accepted"

    plan_submission = ReaderSubmission(
        role_id="plan_reader", image_name="plan.png", trial=PassedTrial(), target="F2",
    )
    with pytest.raises(ValueError, match="target|F2"):
        plan_submission.submit(arguments(plan_submission.trial))


def test_reader_tools_target_is_optional_but_session_passes_it(tmp_path, monkeypatch):
    # Direct construction remains compatible for local validators and old tests.
    ReaderTools(
        ReaderFrozen(), role_id="elevation_reader", image_name="North.png", target=None,
    )

    captured = []
    original = ReaderSubmission.__init__

    def capture(self, *args, **kwargs):
        captured.append(kwargs.get("target"))
        original(self, *args, **kwargs)

    monkeypatch.setattr(ReaderSubmission, "__init__", capture)
    monkeypatch.setattr("src.agent.runtime_roles.session.make_versions", lambda *args, **kwargs: versions())
    run = tmp_path / "bim"
    (run / "images").mkdir(parents=True)
    image = run / "images" / "north.png"
    Image.new("RGB", (100, 100), "white").save(image)
    (run / "inputs.json").write_text(json.dumps({
        "images": {"north.png": {
            "size": [100, 100], "sha256": hashlib.sha256(image.read_bytes()).hexdigest(),
        }},
    }), encoding="utf-8", newline="\n")
    limits = RunLimits(model_calls=2, tool_calls=10, seconds=120, tokens=200_000)
    with EventStore(
        tmp_path / "events", run_id="roles", task_id="root",
        budget_limit=limits.ledger_limit(),
    ) as store:
        session = RoleSession(
            store=store,
            frozen=SessionFrozen(run),
            routes=ROUTES,
            adapter_factory=lambda *args: delivered_adapter(),
            limits=limits,
            root=ROOT,
        )
        row = asyncio.run(session.delegate_many([dispatch(target="North/F1")]))["results"][0]
        assert row["status"] == "completed"
    assert captured and set(captured) == {"North/F1"}


def test_trial_bad_schema_and_nested_business_name_return_repairable_envelopes():
    async def scenario():
        trial = ScopeTrial()
        tools = ReaderTools(
            ReaderFrozen(), role_id="plan_reader", image_name="1f_view.png", trial=trial,
        )
        wrong_shape = await tools.call_tool("trial_plan_bim", {"plan": []})
        assert wrong_shape["isError"] is True
        assert wrong_shape["structuredContent"]["status"] == "rejected"
        assert trial.calls == []

        # This is the exact collision from the real D1b run: ``name`` is a
        # floor business field, not a request to read another image.
        wrong_plan = {
            "schema_version": "2",
            "floors": [{"name": "F1", "z_floor": 0, "cells": []}],
        }
        rejected = await tools.call_tool("trial_plan_bim", {"plan": wrong_plan})
        assert rejected["isError"] is True
        assert rejected["structuredContent"]["status"] == "rejected"
        assert "invalid plan schema" in rejected["structuredContent"]["reason"]
        assert trial.calls == [({**wrong_plan, "reading_type": "plan"}, {})]

    asyncio.run(scenario())


def test_trial_scope_still_rejects_real_other_image_and_unissued_profile_references():
    async def scenario():
        trial = ScopeTrial()
        tools = ReaderTools(
            ReaderFrozen(), role_id="plan_reader", image_name="1f_view.png", trial=trial,
        )
        other_image = await tools.call_tool("trial_plan_bim", {
            "plan": {"floor_id": "F1", "image": "other.png"},
        })
        assert other_image["isError"] is True
        assert "only image '1f_view.png'" in other_image["structuredContent"]["reason"]

        unknown_profile = await tools.call_tool("trial_plan_bim", {
            "plan": {"floor_id": "F1", "x_anchors": [[{
                "profile": "profile_999", "candidate": "C01",
            }, 0], [10, 1]]},
        })
        assert unknown_profile["isError"] is True
        assert "was not returned by this single-image task" in unknown_profile["structuredContent"]["reason"]
        assert trial.calls == []

        # Existing read-only tool scope remains strict as well.
        with pytest.raises(ValueError, match="only image"):
            await tools.call_tool("view_image", {"name": "other.png"})

    asyncio.run(scenario())
