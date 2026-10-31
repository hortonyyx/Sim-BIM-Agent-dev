"""D1b assembly comparisons preserve accepted per-floor reader facts."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.agent.execution.source_proposal import export_source_proposal
from src.agent.runtime_roles.assembly_review import (
    AssemblyReview,
    _review_identity,
    accepted_regularization_changes,
    compare_floor,
    finalize_role_building,
)
from src.agent.runtime_roles.config import load_roles
from src.agent.runtime_roles.session import RoleSession, envelope
from src.agent_runtime.loop import RunLimits
from src.agent_runtime.store import EventStore
from src.harness_contracts import ToolInvocationPayload
from tests.test_bim_claims import setup_run
from tests.test_source_proposal import _proposal
from tests.reader_delivery_fixtures import comparison_reading_bytes


ROOT = Path(__file__).resolve().parents[1]


def _source(tmp_path, name="source", proposal=None):
    folder = tmp_path / name
    assert export_source_proposal(proposal or _proposal(), folder)["source_geometry_ready"]
    return json.loads((folder / "source_model.json").read_bytes())


def test_compare_floor_ignores_height_only_but_catches_plan_facts(tmp_path):
    expected = _source(tmp_path)
    height_only = copy.deepcopy(expected)
    for opening in height_only["openings"]:
        for vertex in opening["vertices"]:
            vertex[2] += 0.35
    assert compare_floor(expected, height_only, "F1") == []

    changed = copy.deepcopy(height_only)
    window = next(row for row in changed["openings"] if row["id"] == "window")
    for vertex in window["vertices"]:
        vertex[1] += 0.2
    changed["boundary_relations"] = []
    changes = compare_floor(expected, changed, "F1")
    assert {row["item"] for row in changes} == {"adjacency", "openings:window"}
    assert all(row["change_id"] for row in changes)
    assert all(
        all(len(point) == 2 for point in row[side]["xy"])
        for row in changes
        if row["item"] == "openings:window"
        for side in ("before", "after")
    )


def test_exact_audited_regularization_is_classified_but_real_geometry_still_requires_review(tmp_path):
    expected = _source(tmp_path)
    actual = copy.deepcopy(expected)
    window = next(row for row in actual["openings"] if row["id"] == "window")
    before = window["vertices"][0][0]
    for vertex in window["vertices"]:
        vertex[0] += 0.2
    changes = compare_floor(expected, actual, "F1")
    report = {
        "schema": "plan_stack_regularization_report_v1",
        "rule_version": "plan_regularization_v1",
        "status": "pass",
        "changes": [{
            "type": "move_wall_line",
            "floor_id": "F1",
            "axis": "x",
            "from_m": before,
            "to_m": before + 0.2,
            "movement_m": 0.2,
            "span_m": [min(vertex[1] for vertex in window["vertices"]),
                       max(vertex[1] for vertex in window["vertices"])],
            "opening_ids": ["window"],
            "blocked_collapses": [],
        }],
    }

    accepted, remaining = accepted_regularization_changes(changes, report, "F1")

    assert [row["item"] for row in accepted] == ["openings:window"]
    assert not remaining
    actual["boundary_relations"] = []
    accepted, remaining = accepted_regularization_changes(
        compare_floor(expected, actual, "F1"), report, "F1"
    )
    assert [row["item"] for row in accepted] == ["openings:window"]
    assert [row["item"] for row in remaining] == ["adjacency"]


def test_lite_grid_audit_accepts_joint_jamb_rounding_but_not_changed_connection():
    before = {"kind": "door", "space_ids": ["room", "hall"], "exterior": False,
              "xy": [(3.94, 2.0), (4.96, 2.0)]}
    after = {**before, "xy": [(3.9, 2.0), (5.0, 2.0)]}
    change = {"floor_id": "F1", "item": "openings:D1", "before": before, "after": after}
    relationships = {"rooms": ["room", "hall"], "door": ["room", "hall"]}
    rows = [{"type": "lite_grid_coordinate", "floor_id": "F1", "axis": "x",
             "from_m": start, "to_m": end, "movement_m": end - start,
             "grid_step_m": 0.1, "span_m": [0.0, 6.0], "opening_ids": ["D1"],
             "before_relationships": relationships, "after_relationships": relationships}
            for start, end in ((3.94, 3.9), (4.96, 5.0))]
    rows.append({"type": "lite_grid_height", "floor_id": "F1", "axis": "z",
                 "from_m": 2.13, "to_m": 2.1, "movement_m": -0.03, "grid_step_m": 0.1,
                 "before_relationships": relationships, "after_relationships": relationships})
    report = {"schema": "plan_regularization_report_v1", "rule_version": "plan_regularization_v1",
              "status": "pass", "changes": rows}
    accepted, remaining = accepted_regularization_changes([change], report, "F1")
    assert len(accepted) == 1 and not remaining
    changed_connection = copy.deepcopy(change)
    changed_connection["after"]["space_ids"] = ["room", "outside"]
    assert accepted_regularization_changes([changed_connection], report, "F1") == ([], [changed_connection])

    # A hash-bound receipt still has to describe an actual grid movement.
    bad_report = copy.deepcopy(report)
    bad_report["changes"][0].update(to_m=3.92, movement_m=-0.02)
    assert accepted_regularization_changes([change], bad_report, "F1") == ([], [change])
    bad_report = copy.deepcopy(report)
    bad_report["changes"][0]["after_relationships"] = {"rooms": ["room"]}
    assert accepted_regularization_changes([change], bad_report, "F1") == ([], [change])


def test_lite_grid_producer_report_replays_exported_room_and_opening_changes(tmp_path):
    from src.agent.geometry.lite_bim_regularization import regularize_lite_plan
    from src.agent.geometry.plan_partition import compile_plan_partition
    from src.agent.geometry.plan_regularization import regularize_plan
    from tests.test_lite_bim_regularization import SIZE, plan

    raw = plan()
    before, _ = compile_plan_partition(raw, image_size=SIZE, image_name="plan")
    adopted, _ = regularize_lite_plan(raw, image_size=SIZE, image_name="plan")
    adopted, report = regularize_plan(adopted, image_size=SIZE, image_name="plan")
    after, _ = compile_plan_partition(adopted, image_size=SIZE, image_name="plan")
    expected = _source(tmp_path, "before", before)
    actual = _source(tmp_path, "after", after)
    changes = compare_floor(expected, actual, "F1")
    assert changes and any(row["type"] == "lite_grid_height" for row in report["changes"])
    accepted, remaining = accepted_regularization_changes(changes, report, "F1")
    assert {row["change_id"] for row in accepted} == {row["change_id"] for row in changes}
    assert not remaining


def test_crossing_opening_requires_explicit_width_preserving_world_adjustment():
    change = {
        "floor_id": "F1", "item": "openings:window", "change_id": "crossing-window",
        "before": {"kind": "window", "space_ids": ["room"], "exterior": True,
                   "xy": [(1.1, 2.0), (2.1, 2.0)]},
        "after": {"kind": "window", "space_ids": ["room"], "exterior": True,
                  "xy": [(1.2, 2.0), (2.2, 2.0)]},
    }
    report = {
        "schema": "plan_stack_regularization_report_v1",
        "rule_version": "plan_regularization_v1",
        "status": "pass",
        "changes": [{
            "type": "move_wall_line", "floor_id": "F1", "axis": "x",
            "from_m": 1.0, "to_m": 1.2, "movement_m": 0.2, "span_m": [0.0, 4.0],
            "opening_ids": ["window"], "adjusted_crossing_opening_ids": ["window"],
            "opening_adjustments": [{
                "opening_id": "window", "mode": "translate_preserve_width",
                "before": {"p1": [11, 20], "p2": [21, 20],
                           "world_p1": [1.1, 2.0], "world_p2": [2.1, 2.0], "width_m": 1.0},
                "after": {"p1": [12, 20], "p2": [22, 20],
                          "world_p1": [1.2, 2.0], "world_p2": [2.2, 2.0], "width_m": 1.0},
                "jamb_deltas_m": {"p1": 0.1, "p2": 0.1}, "width_change_m": 0.0,
                "inference": {"basis": "entity_opening_width_preserved"},
                "boundary_ids": {"swept_boundary": "wall"},
            }],
            "blocked_collapses": [],
        }],
    }

    accepted, remaining = accepted_regularization_changes([change], report, "F1")
    assert [row["item"] for row in accepted] == ["openings:window"]
    assert not remaining

    del report["changes"][0]["opening_adjustments"]
    accepted, remaining = accepted_regularization_changes([change], report, "F1")
    assert not accepted and [row["item"] for row in remaining] == ["openings:window"]


def test_crossing_open_passage_requires_complete_structured_audit():
    change = {
        "floor_id": "F1", "item": "openings:passage", "change_id": "crossing-passage",
        "before": {"kind": "door", "space_ids": ["left", "right"], "exterior": False,
                   "xy": [(1.1, 2.0), (2.1, 2.0)]},
        "after": {"kind": "door", "space_ids": ["left", "right"], "exterior": False,
                  "xy": [(1.2, 2.0), (2.1, 2.0)]},
    }
    report = {
        "schema": "plan_stack_regularization_report_v1",
        "rule_version": "plan_regularization_v1", "status": "pass", "changes": [{
            "type": "move_wall_line", "floor_id": "F1", "axis": "x",
            "from_m": 1.0, "to_m": 1.2, "movement_m": 0.2, "span_m": [0.0, 4.0],
            "opening_ids": ["passage"], "adjusted_crossing_opening_ids": ["passage"],
            "opening_adjustments": [{
                "opening_id": "passage", "mode": "open_passage_boundary_follow",
                "before": {"p1": [11, 20], "p2": [21, 20],
                           "world_p1": [1.1, 2.0], "world_p2": [2.1, 2.0], "width_m": 1.0},
                "after": {"p1": [12, 20], "p2": [21, 20],
                          "world_p1": [1.2, 2.0], "world_p2": [2.1, 2.0], "width_m": 0.9},
                "jamb_deltas_m": {"p1": 0.1, "p2": 0.0}, "width_change_m": -0.1,
                "inference": {
                    "basis": "structured_complete_open_passage_compatibility",
                    "state": "open", "structured_gap_support": True,
                    "complete_between_terminal_boundaries": True,
                    "hard_width_reference": False,
                },
                "boundary_ids": {"p1": "moving", "p2": "terminal"},
                "boundary_offsets_before_m": {"p1": 0.1, "p2": -0.1},
                "boundary_offsets_after_m": {"p1": 0.1, "p2": -0.1},
            }],
            "blocked_collapses": [],
        }],
    }

    accepted, remaining = accepted_regularization_changes([change], report, "F1")
    assert [row["item"] for row in accepted] == ["openings:passage"] and not remaining

    report["changes"][0]["opening_adjustments"][0]["inference"]["structured_gap_support"] = False
    accepted, remaining = accepted_regularization_changes([change], report, "F1")
    assert not accepted and remaining == [change]


def test_audited_wall_move_treats_removed_collinear_ring_vertices_as_same_geometry():
    change = {
        "floor_id": "F1",
        "item": "rooms:room",
        "before": [(0, 0), (20, 0), (20, 1), (15, 1), (15, 0.9), (11, 0.9),
                   (11, 10), (0, 10)],
        "after": [(0, 0), (20, 0), (20, 1.000001), (15, 1), (11, 1),
                  (11, 10), (0, 10)],
        "change_id": "room-change",
    }
    report = {
        "schema": "plan_regularization_report_v1",
        "rule_version": "plan_regularization_v1",
        "status": "pass",
        "changes": [{
            "type": "move_wall_line", "floor_id": "F1", "axis": "y",
            "from_m": 0.9, "to_m": 1.0, "movement_m": 0.1,
            "span_m": [11, 15],
            "opening_ids": [], "blocked_collapses": [],
        }],
    }

    accepted, remaining = accepted_regularization_changes([change], report, "F1")

    assert [row["item"] for row in accepted] == ["rooms:room"]
    assert not remaining
    changed_shape = copy.deepcopy(change)
    changed_shape["after"] = [(0, 0), (20, 0), (20, 1.1), (11, 1),
                              (11, 10), (0, 10)]
    accepted, remaining = accepted_regularization_changes([changed_shape], report, "F1")
    assert not accepted and [row["item"] for row in remaining] == ["rooms:room"]


def test_audited_wall_move_is_limited_to_reported_span():
    before = [(0, 0), (20, 0), (20, 1), (15, 1), (15, 0.9), (11, 0.9),
              (11, 10), (0, 10), (0, 0.9), (8, 0.9), (8, 2), (0, 2)]
    after = [(0, 0), (20, 0), (20, 1), (11, 1), (11, 10), (0, 10),
             (0, 0.9), (8, 0.9), (8, 2), (0, 2)]
    change = {"floor_id": "F1", "item": "rooms:room", "before": before,
              "after": after, "change_id": "local-span-change"}
    report = {
        "schema": "plan_regularization_report_v1",
        "rule_version": "plan_regularization_v1",
        "status": "pass",
        "changes": [{
            "type": "move_wall_line", "floor_id": "F1", "axis": "y",
            "from_m": 0.9, "to_m": 1.0, "movement_m": 0.1,
            "span_m": [11, 15], "opening_ids": [], "blocked_collapses": [],
        }],
    }

    accepted, remaining = accepted_regularization_changes([change], report, "F1")
    assert [row["item"] for row in accepted] == ["rooms:room"] and not remaining

    over_moved = copy.deepcopy(change)
    over_moved["after"] = [
        (x, 1.0 if y == 0.9 else y) for x, y in after
    ]
    accepted, remaining = accepted_regularization_changes([over_moved], report, "F1")
    assert not accepted and [row["item"] for row in remaining] == ["rooms:room"]


def test_audited_strip_merge_removes_only_its_zero_width_backtrack():
    moved_room = {
        "floor_id": "F2", "item": "rooms:S-corr",
        "before": [(4.995637, 5.888768), (8.944154, 5.888768),
                   (8.944154, 13.980371), (4.995637, 13.980371)],
        "after": [(4.995637, 5.997819), (8.944154, 5.997819),
                  (8.944154, 13.980371), (4.995637, 13.980371)],
        "change_id": "moved-room",
    }
    collapsed_room = {
        "floor_id": "F2", "item": "rooms:S-e4",
        "before": [(11.125654, 5.888768), (25.0, 5.888768),
                   (25.0, 5.997819), (14.986911, 5.997819),
                   (14.986911, 7.82988), (11.125654, 7.82988)],
        "after": [(11.125654, 5.997819), (14.986911, 5.997819),
                  (14.986911, 7.82988), (11.125654, 7.82988)],
        "change_id": "collapsed-room",
    }
    door = {
        "floor_id": "F2", "item": "openings:D14",
        "before": {"kind": "door", "space_ids": ["S-corr", "S-corr-s"],
                   "exterior": False, "xy": [(9.009599, 5.888768), (10.972949, 5.888768)]},
        "after": {"kind": "door", "space_ids": ["S-corr", "S-corr-s"],
                  "exterior": False, "xy": [(9.009599, 5.997819), (10.972949, 5.997819)]},
        "change_id": "moved-door",
    }
    reassigned_window = {
        "floor_id": "F2", "item": "openings:WLONG",
        "before": {"kind": "window", "space_ids": ["S-e4"], "exterior": True,
                   "xy": [(15.292321, 5.997819), (23.298429, 5.997819)]},
        "after": {"kind": "window", "space_ids": ["S-corr-s"], "exterior": True,
                  "xy": [(15.292321, 5.997819), (23.298429, 5.997819)]},
        "change_id": "reassigned-window",
    }
    report = {
        "schema": "plan_regularization_report_v1", "rule_version": "plan_regularization_v1",
        "status": "pass", "changes": [
            {"type": "retain_openings_on_merged_wall", "floor_id": "F2", "axis": "y",
             "from_m": 5.997818975, "to_m": 5.997818975, "movement_m": 0.0,
             "span_m": [14.986910995, 25.0], "opening_ids": ["WLONG"]},
            {"type": "merge_duplicate_wall_into_fixed_footprint", "floor_id": "F2",
             "axis": "y", "from_m": 5.888767721, "to_m": 5.997818975,
             "movement_m": 0.109051254, "span_m": [14.986910995, 25.0],
             "opening_ids": ["WLONG"]},
            {"type": "move_wall_line", "floor_id": "F2", "axis": "y",
             "from_m": 5.888767721, "to_m": 5.997818975, "movement_m": 0.109051254,
             "span_m": [4.995636998, 14.986910995], "opening_ids": ["D14"],
             "blocked_collapses": []},
        ],
    }
    changes = [moved_room, collapsed_room, door, reassigned_window]

    accepted, remaining = accepted_regularization_changes(changes, report, "F2")
    assert [row["item"] for row in accepted] == ["rooms:S-corr", "rooms:S-e4", "openings:D14"]
    assert [row["item"] for row in remaining] == ["openings:WLONG"]

    after_stack_move = copy.deepcopy(collapsed_room)
    after_stack_move["after"] = [
        (x, 5.980498 if y == 5.997819 else y) for x, y in collapsed_room["after"]
    ]
    stacked = copy.deepcopy(report)
    stacked["schema"] = "plan_stack_regularization_report_v1"
    stacked["changes"].extend([
        {"type": "move_wall_line", "floor_id": "F2", "axis": "y",
         "from_m": 5.997818975, "to_m": 5.980498375, "movement_m": 0.0173206,
         "span_m": [4.995636998, 14.986910995], "opening_ids": [],
         "blocked_collapses": []},
        {"type": "move_footprint_edge", "floor_id": "F2", "axis": "y",
         "from_m": 5.997818975, "to_m": 5.980498375, "movement_m": 0.0173206,
         "span_m": [14.986910995, 25.0], "opening_ids": [],
         "blocked_collapses": []},
    ])
    accepted, remaining = accepted_regularization_changes([after_stack_move], stacked, "F2")
    assert [row["item"] for row in accepted] == ["rooms:S-e4"] and not remaining

    outside_span = copy.deepcopy(report)
    outside_span["changes"][1]["span_m"] = [15.1, 25.0]
    accepted, remaining = accepted_regularization_changes(changes, outside_span, "F2")
    assert "rooms:S-e4" in {row["item"] for row in remaining}

    moving_retained_opening = copy.deepcopy(report)
    moving_retained_opening["changes"][0].update(to_m=6.0, movement_m=0.002181025)
    accepted, remaining = accepted_regularization_changes(changes, moving_retained_opening, "F2")
    assert not accepted and remaining == changes


def test_changed_assembly_requires_one_nonempty_reason_per_change(tmp_path):
    limits = RunLimits(model_calls=1, tool_calls=1, seconds=10, tokens=1000)
    with EventStore(
        tmp_path / "journal",
        run_id="assembly",
        task_id="coordinator",
        budget_limit=limits.ledger_limit(),
    ) as store:
        review = AssemblyReview(SimpleNamespace(store=store))
        changes = [
            {"change_id": "change-a", "floor_id": "F1", "item": "room_count"},
            {"change_id": "change-b", "floor_id": "F1", "item": "openings:W1"},
        ]
        report = {
            "candidate": "candidate_01",
            "source_sha256": "a" * 64,
            "require_all": True,
            "bindings": {},
            "checked_floors": ["F1"],
            "changes": changes,
            "review_id": "review-current",
            "status": "needs_review",
        }
        store.write_json("role_assembly_reviews/review-current.json", report)
        store.write_json("role_assembly_current.json", {"review_id": "review-current"})
        review.check = lambda candidate, require_all=False: report

        with pytest.raises(ValueError, match="exactly one nonempty reason"):
            review.acknowledge(
                "review-current", [{"change_id": "change-a", "reason": "accepted"}]
            )
        with pytest.raises(ValueError, match="exactly one nonempty reason"):
            review.acknowledge(
                "review-current",
                [
                    {"change_id": "change-a", "reason": "accepted"},
                    {"change_id": "change-b", "reason": "  "},
                ],
            )
        accepted = review.acknowledge(
            "review-current",
            [
                {"change_id": "change-a", "reason": "Room merge is intended."},
                {"change_id": "change-b", "reason": "Opening move is intended."},
            ],
        )
        assert accepted["status"] == "reviewed"
        assert {row["change_id"] for row in accepted["decisions"]} == {
            "change-a",
            "change-b",
        }


def test_review_identity_ignores_diagnostics_but_not_safety_findings():
    facts = {
        "bindings": {"F1": {"task_id": "plan-f1"}, "F2": {"task_id": "plan-f2"}},
        "changes": [{"change_id": "relationship-change", "item": "openings:WLONG"}],
        "blockers": [],
        "candidate": "candidate_02", "source_sha256": "a" * 64,
        "require_all": False, "checked_floors": ["F2"],
        "regularization_changes": [{"item": "rooms:S-e4"}],
        "delivery_scope": [], "used_plan_tasks": [],
    }
    final = {**facts, "candidate": "candidate_03", "source_sha256": "b" * 64,
             "require_all": True, "checked_floors": ["F1", "F2"],
             "regularization_changes": [{"item": "rooms:S-e4"}, {"item": "openings:D14"}],
             "delivery_scope": [{"floor_id": "F1"}], "used_plan_tasks": ["plan-f1", "plan-f2"]}

    assert _review_identity(final) == _review_identity(facts)
    changed = copy.deepcopy(final)
    changed["changes"].append({"change_id": "new-change", "item": "rooms:other"})
    assert _review_identity(changed) != _review_identity(facts)


class _Registry:
    def __init__(self, task_directory, source_sha256):
        self.task_directory = task_directory
        self.records = {
            "plan-f1": {
                "artifact": {"sha256": "artifact-sha"},
                "validation": {
                    "reading_product_file": "reading_product.json",
                    "reading_product_sha256": hashlib.sha256((task_directory / "bim/trial_workspace/reading_product.json").read_bytes()).hexdigest(),
                },
            }
        }

    def read(self, task_id, *, sha256=None, role_id=None):
        assert task_id == "plan-f1" and role_id == "plan_reader"
        if sha256 is not None:
            assert sha256 == "artifact-sha"
        return {"plan": {"floor_id": "F1"}}

    def child(self, task_id):
        assert task_id == "plan-f1"
        return SimpleNamespace(task_directory=self.task_directory)


class _AssemblySession:
    def __init__(self, store, run_directory, registry):
        self.store = store
        self.run_directory = run_directory
        self.registry = registry

    def _source(self, candidate):
        return json.loads(
            (self.run_directory / candidate / "source_model.json").read_bytes()
        )


def _bound_review_environment(tmp_path):
    expected = _source(tmp_path, "expected")
    raw = json.dumps(expected, ensure_ascii=False, sort_keys=True).encode()
    task = tmp_path / "reader-task"
    accepted = task / "bim/trial_workspace/reading_product.json"
    accepted.parent.mkdir(parents=True)
    accepted.write_bytes(comparison_reading_bytes(json.loads(raw)))
    run = tmp_path / "run"
    candidate = run / "candidate_01/source_model.json"
    candidate.parent.mkdir(parents=True)
    candidate.write_bytes(raw)
    limits = RunLimits(model_calls=1, tool_calls=1, seconds=10, tokens=1000)
    return run, task, raw, limits


def test_review_survives_height_only_descendant_but_not_a_new_horizontal_edit(tmp_path):
    run, task, raw, limits = _bound_review_environment(tmp_path)
    with EventStore(
        tmp_path / "journal",
        run_id="assembly-hash",
        task_id="coordinator",
        budget_limit=limits.ledger_limit(),
    ) as store:
        registry = _Registry(task, hashlib.sha256(raw).hexdigest())
        review = AssemblyReview(_AssemblySession(store, run, registry))
        review.bind("plan-f1")
        changed = json.loads(raw)
        for vertex in changed["openings"][0]["vertices"]:
            vertex[1] += 0.2
        (run / "candidate_01/source_model.json").write_text(
            json.dumps(changed, ensure_ascii=False), encoding="utf-8", newline="\n"
        )
        first = review.check("candidate_01", require_all=True)
        assert first["status"] == "needs_review" and first["changes"]
        decisions = [{"change_id": row["change_id"], "reason": "bounded coordinator correction"}
                     for row in first["changes"]]
        assert review.acknowledge(first["review_id"], decisions)["status"] == "reviewed"

        for vertex in changed["openings"][0]["vertices"]:
            vertex[2] += 0.2
        changed["spaces"][0]["role"] = "meeting"
        changed["spaces"][0].setdefault("assumptions", []).append("Coordinator use note")
        changed["spaces"][0].setdefault("source_refs", []).append("delivery:use-note")
        (run / "candidate_01/source_model.json").write_text(
            json.dumps(changed, ensure_ascii=False), encoding="utf-8", newline="\n"
        )
        inherited = review.check("candidate_01", require_all=False)
        assert inherited["review_id"] == first["review_id"]
        assert inherited["status"] == "reviewed" and inherited["decisions"] == decisions
        finish_check = review.check("candidate_01", require_all=True)
        assert finish_check["review_id"] == first["review_id"]
        assert finish_check["status"] == "reviewed" and finish_check["decisions"] == decisions

        for vertex in changed["openings"][0]["vertices"]:
            vertex[1] += 0.1
        (run / "candidate_01/source_model.json").write_text(
            json.dumps(changed, ensure_ascii=False), encoding="utf-8", newline="\n"
        )
        revised = review.check("candidate_01", require_all=True)
        assert revised["review_id"] != first["review_id"]
        assert revised["status"] == "needs_review"


@pytest.mark.parametrize("receipt_kind", ["plan_assembly", "plan_input"])
def test_hashed_regularization_receipt_does_not_open_reader_review(tmp_path, receipt_kind):
    run, task, raw, limits = _bound_review_environment(tmp_path)
    actual = json.loads(raw)
    window = next(row for row in actual["openings"] if row["id"] == "window")
    before = window["vertices"][0][0]
    for vertex in window["vertices"]:
        vertex[0] += 0.2
    regularization = {
        "schema": ("plan_stack_regularization_report_v1" if receipt_kind == "plan_assembly"
                   else "plan_regularization_report_v1"),
        "rule_version": "plan_regularization_v1",
        "status": "pass",
        "changes": [{
            "type": "move_wall_line", "floor_id": "F1", "axis": "x",
            "from_m": before, "to_m": before + 0.2, "movement_m": 0.2,
            "span_m": [min(vertex[1] for vertex in window["vertices"]),
                       max(vertex[1] for vertex in window["vertices"])],
            "opening_ids": ["window"], "blocked_collapses": [],
        }],
    }
    receipt_value = {"regularization": regularization} if receipt_kind == "plan_assembly" else regularization
    receipt = json.dumps(receipt_value, sort_keys=True).encode()
    receipt_path = run / ("plan_assemblies/assembly.json" if receipt_kind == "plan_assembly"
                          else "plan_drafts/draft_001/regularization.json")
    receipt_path.parent.mkdir(parents=True)
    receipt_path.write_bytes(receipt)
    reference = {"file": receipt_path.relative_to(run).as_posix(),
                 "sha256": hashlib.sha256(receipt).hexdigest()}
    provenance = ({"plan_assembly": reference} if receipt_kind == "plan_assembly" else {
        "plan_input": {"regularization_report": reference}
    })
    actual.setdefault("generation", {})["provenance"] = provenance
    (run / "candidate_01/source_model.json").write_text(
        json.dumps(actual, ensure_ascii=False), encoding="utf-8", newline="\n"
    )

    with EventStore(
        tmp_path / "journal-regularization",
        run_id="assembly-regularization",
        task_id="coordinator",
        budget_limit=limits.ledger_limit(),
    ) as store:
        review = AssemblyReview(_AssemblySession(
            store, run, _Registry(task, hashlib.sha256(raw).hexdigest())
        ))
        review.bind("plan-f1")
        report = review.check("candidate_01")

    assert report["status"] == "unchanged" and not report["changes"]
    assert [row["item"] for row in report["regularization_changes"]] == ["openings:window"]


def test_stack_receipt_inherits_hash_bound_single_floor_regularization(tmp_path):
    run, task, raw, limits = _bound_review_environment(tmp_path)
    actual = json.loads(raw)
    window = next(row for row in actual["openings"] if row["id"] == "window")
    before = window["vertices"][0][0]
    for vertex in window["vertices"]:
        vertex[0] += 0.2
    single = {
        "schema": "plan_regularization_report_v1", "rule_version": "plan_regularization_v1",
        "status": "pass", "floor_id": "F1", "changes": [{
            "type": "move_wall_line", "floor_id": "F1", "axis": "x",
            "from_m": before, "to_m": before + 0.2, "movement_m": 0.2,
            "span_m": [min(vertex[1] for vertex in window["vertices"]),
                       max(vertex[1] for vertex in window["vertices"])],
            "opening_ids": ["window"], "blocked_collapses": [],
        }],
    }
    plan = json.dumps({"floor_id": "F1", "regularization": single}, sort_keys=True).encode()
    plan_path = run / "plan_drafts/draft_001/plan.json"
    plan_path.parent.mkdir(parents=True)
    plan_path.write_bytes(plan)
    empty_floor = {"schema": "plan_regularization_report_v1",
                   "rule_version": "plan_regularization_v1", "status": "pass", "changes": []}
    stack = {"schema": "plan_stack_regularization_report_v1",
             "rule_version": "plan_regularization_v1", "status": "pass",
             "floor_reports": {"F1": empty_floor}, "changes": []}
    receipt_value = {
        "floors": [{"draft_id": "draft_001", "floor_id": "F1",
                    "expected_plan_sha256": hashlib.sha256(plan).hexdigest()}],
        "regularization": stack,
    }
    receipt = json.dumps(receipt_value, sort_keys=True).encode()
    receipt_path = run / "plan_assemblies/assembly.json"
    receipt_path.parent.mkdir(parents=True)
    receipt_path.write_bytes(receipt)
    actual.setdefault("generation", {})["provenance"] = {"plan_assembly": {
        "file": receipt_path.relative_to(run).as_posix(),
        "sha256": hashlib.sha256(receipt).hexdigest(),
    }}
    (run / "candidate_01/source_model.json").write_text(
        json.dumps(actual, ensure_ascii=False), encoding="utf-8", newline="\n"
    )

    with EventStore(
        tmp_path / "journal-stack-regularization", run_id="assembly-stack-regularization",
        task_id="coordinator", budget_limit=limits.ledger_limit(),
    ) as store:
        review = AssemblyReview(_AssemblySession(
            store, run, _Registry(task, hashlib.sha256(raw).hexdigest())
        ))
        review.bind("plan-f1")
        report = review.check("candidate_01")
        assert report["status"] == "unchanged" and not report["changes"]
        assert [row["item"] for row in report["regularization_changes"]] == ["openings:window"]

        plan_path.write_bytes(plan + b" ")
        with pytest.raises(ValueError, match="floor plan changed"):
            review.check("candidate_01")


def test_require_all_reports_each_unbound_actual_floor(tmp_path):
    source = _source(tmp_path, "two-floor")
    second = copy.deepcopy(source["spaces"][0])
    second.update(id="F2:extra", floor_id="F2", polygon=[
        [0, 0, 3.0], [2, 0, 3.0], [2, 2, 3.0], [0, 2, 3.0]
    ])
    source["spaces"].append(second)
    run = tmp_path / "run"
    path = run / "candidate_01/source_model.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(source), encoding="utf-8", newline="\n")
    limits = RunLimits(model_calls=1, tool_calls=1, seconds=10, tokens=1000)
    with EventStore(
        tmp_path / "journal",
        run_id="assembly-unbound",
        task_id="coordinator",
        budget_limit=limits.ledger_limit(),
    ) as store:
        registry = SimpleNamespace(records={})
        review = AssemblyReview(_AssemblySession(store, run, registry))
        report = review.check("candidate_01", require_all=True)

    unexpected = [row for row in report["changes"] if row["item"] == "unexpected_floor"]
    assert {row["floor_id"] for row in unexpected} == {"F1", "F2"}
    assert all(row["change_id"] for row in unexpected)


class _CoverageRegistry:
    def __init__(self, task_directory, records, tasks, floors):
        self.task_directory = task_directory
        self.records = records
        self.tasks = tasks
        self.floors = floors

    def read(self, task_id, *, sha256=None, role_id=None):
        row = self.records[task_id]
        if sha256 is not None:
            assert sha256 == row["artifact"]["sha256"]
        if role_id is not None:
            assert role_id == row["role_id"]
        return {"plan": {"floor_id": self.floors[task_id]}}

    def child(self, task_id):
        return SimpleNamespace(task_directory=self.task_directory)

    def task(self, task_id):
        return self.tasks.get(task_id, {})


def _coverage_record(task_id, target, rank, *, status="completed", valid=True):
    artifact = {"sha256": task_id + "-sha"} if status == "completed" else None
    return {
        "task_id": task_id,
        "role_id": "plan_reader",
        "target": target,
        "status": status,
        "artifact": artifact,
        "validation": {"validation_passed": valid} if status == "completed" else None,
        "delivered_at_ns": rank,
    }


def _coverage_review(tmp_path, records, tasks, floors, used_task):
    source = {
        "spaces": [{
            "id": "F1:R1",
            "floor_id": "F1",
            "polygon": [[0, 0, 0], [2, 0, 0], [2, 2, 0], [0, 2, 0]],
        }],
        "openings": [],
        "boundary_relations": [],
        "boundaries": [],
    }
    raw = json.dumps(source, ensure_ascii=False, sort_keys=True).encode()
    records[used_task]["validation"].update({
        "reading_product_file": "reading_product.json",
        "reading_product_sha256": hashlib.sha256(comparison_reading_bytes(source)).hexdigest(),
    })
    run = tmp_path / "run"
    candidate = run / "candidate_01/source_model.json"
    candidate.parent.mkdir(parents=True)
    candidate.write_bytes(raw)
    task = tmp_path / "reader-task"
    accepted = task / "bim/trial_workspace/reading_product.json"
    accepted.parent.mkdir(parents=True)
    accepted.write_bytes(comparison_reading_bytes(json.loads(raw)))
    limits = RunLimits(model_calls=1, tool_calls=1, seconds=10, tokens=1000)
    store = EventStore(
        tmp_path / "journal",
        run_id="assembly-coverage",
        task_id="coordinator",
        budget_limit=limits.ledger_limit(),
    )
    registry = _CoverageRegistry(task, records, tasks, floors)
    review = AssemblyReview(_AssemblySession(store, run, registry))
    bound = records[used_task]
    store.write_json("role_floor_sources.json", {"F1": {
        "task_id": used_task,
        "artifact_sha256": bound["artifact"]["sha256"],
        "source_sha256": hashlib.sha256(comparison_reading_bytes(source)).hexdigest(),
    }})
    store.write_json("role_operations/build.json", {
        "tool": "build_plan_bim",
        "reference": {"task_id": used_task},
        "result": {"structuredContent": {"candidate": "candidate_01"}},
    })
    return store, review


def test_missing_latest_floor_requires_persisted_partial_delivery_reason(tmp_path):
    records = {
        "good-f1": _coverage_record("good-f1", "F1", 1),
        "good-f2": _coverage_record("good-f2", "F2", 2),
        "failed-f2-retry": _coverage_record(
            "failed-f2-retry", "F2", 3, status="failed"
        ),
    }
    tasks = {"failed-f2-retry": {"previous_task_id": "good-f2"}}
    floors = {"good-f1": "F1", "good-f2": "F2"}
    store, review = _coverage_review(
        tmp_path, records, tasks, floors, used_task="good-f1"
    )
    with store:
        assert review.check("candidate_01", require_all=False)["status"] == "unchanged"
        report = review.check("candidate_01", require_all=True)
        missing = [row for row in report["changes"] if row["item"] == "missing_required_floor"]
        assert [row["floor_id"] for row in missing] == ["F2"]
        assert {row["task_id"] for row in report["delivery_scope"]} == {
            "good-f1", "good-f2"
        }
        assert report["status"] == "needs_review"

        accepted = review.acknowledge(report["review_id"], [{
            "change_id": missing[0]["change_id"],
            "reason": "F2 is explicitly omitted from this partial delivery.",
        }])
        assert accepted["status"] == "reviewed"
        # An edit-time partial check may omit delivery coverage, but it must not
        # erase or silently waive the persisted complete-delivery decision.
        partial = review.check("candidate_01", require_all=False)
        assert partial["status"] == "unchanged"
        assert not any(row["item"] == "missing_required_floor" for row in partial["changes"])
        persisted = review.check("candidate_01", require_all=True)
        assert persisted["status"] == "reviewed"
        assert "explicitly omitted" in persisted["decisions"][0]["reason"]


@pytest.mark.parametrize("linked_rework", [False, True])
def test_old_plan_delivery_is_blocked_by_latest_same_target(tmp_path, linked_rework):
    records = {
        "old-f1": _coverage_record("old-f1", "F1", 1),
        "new-f1": _coverage_record("new-f1", "F1", 2),
    }
    tasks = {"new-f1": {"previous_task_id": "old-f1"}} if linked_rework else {}
    floors = {"old-f1": "F1", "new-f1": "F1"}
    store, review = _coverage_review(
        tmp_path, records, tasks, floors, used_task="old-f1"
    )
    with store:
        report = review.check("candidate_01", require_all=True)
        assert report["status"] == "blocked"
        assert {row["item"] for row in report["blockers"]} == {
            "stale_plan_delivery"
        }
        with pytest.raises(ValueError):
            review.acknowledge(report["review_id"], [])
        with pytest.raises(ValueError):
            review.guard()


def test_timeout_fallback_runs_the_same_delivery_guard(tmp_path, monkeypatch):
    class BlockingReview:
        def __init__(self):
            self.calls = []

        def check(self, candidate, *, require_all=False):
            self.calls.append((candidate, require_all))
            return {
                "candidate": candidate,
                "status": "blocked",
                "blockers": [{"item": "stale_plan_delivery"}],
            }

        def guard(self):
            self.calls.append("guard")
            raise ValueError("blocked")

    review = BlockingReview()
    engine = SimpleNamespace(
        tools=SimpleNamespace(run_directory=tmp_path, assembly=review)
    )
    monkeypatch.setattr(
        "scripts.tool_scripts.bim_agent_budget.fallback_selection",
        lambda toolkit: ("candidate_01", "fallback"),
    )
    monkeypatch.setattr(
        "scripts.tool_scripts.run_bim_agent.Toolkit", lambda run_directory: object()
    )

    result = finalize_role_building(engine, "time_budget_exhausted")

    assert review.calls == [("candidate_01", True), "guard"]
    assert result["status"] == "assembly_delivery_blocked"
    assert result["delivery"] is None


@pytest.mark.parametrize("with_openings", [False, True])
@pytest.mark.parametrize("receipt_state", ["completed", "intent_only", "missing", "changed_arguments", "changed_compiled"])
def test_recovered_build_receipt_still_binds_and_checks_reader_floor(tmp_path, with_openings, receipt_state):
    run, _ = setup_run(tmp_path)
    limits = RunLimits(model_calls=1, tool_calls=4, seconds=30, tokens=10_000)
    routes = load_roles(
        {
            role: {
                "provider": "scripted",
                "model": "scripted-model",
                "reasoning_effort": "medium",
                "output_tokens": 1000,
            }
            for role in ("coordinator", "plan_reader", "elevation_reader")
        },
        allow_scripted=True,
    )

    class Frozen:
        run_directory = run

        def __init__(self):
            self.calls = []

        async def call_tool(self, name, arguments):
            self.calls.append((name, arguments))
            raise AssertionError("receipt recovery must never execute a building write")

        def snapshot_state(self):
            return {}

    class AssemblyProbe:
        def __init__(self):
            self.binds = []
            self.checks = []

        def bind(self, task_id):
            self.binds.append(task_id)

        def check(self, candidate, *, require_all=False):
            self.checks.append((candidate, require_all))
            return {"candidate": candidate, "status": "unchanged", "changes": []}

        def current(self):
            return None

        def _load(self, name, default):
            return default

    with EventStore(
        tmp_path / "journal",
        run_id="assembly-recovery",
        task_id="coordinator",
        budget_limit=limits.ledger_limit(),
    ) as store:
        session = RoleSession(
            store=store,
            frozen=Frozen(),
            routes=routes,
            adapter_factory=lambda *args: None,
            limits=limits,
            root=ROOT,
        )
        from src.agent.runtime_roles.plan_materialization import materialize_plan_reading
        from tests.test_role_trial import example
        plan = example()
        if not with_openings:
            plan["openings"] = []
        workspace = tmp_path / "reader-task/bim/trial_workspace"
        workspace.mkdir(parents=True)
        raw_plan = json.dumps(plan).encode()
        (workspace / "compiled.json").write_bytes(raw_plan)
        # Materialization is an upstream concern here; preserve its actual request
        # shape in the durable operation fixture rather than bypassing recovery.
        from PIL import Image
        with Image.open(run / "images/plan.png") as image:
            image_size = image.size
        materialized, decisions = materialize_plan_reading(plan, elevations={}, image_size=image_size, image_name="plan.png")
        assert bool(decisions) is with_openings

        class RecoveryRegistry:
            records = {
                "plan-f1": {
                    "artifact": {"sha256": "artifact-sha"},
                    "image": "plan.png",
                    "validation": {"validation_passed": True, "compiled_numeric_plan_file": "compiled.json",
                                   "compiled_numeric_plan_sha256": hashlib.sha256(raw_plan).hexdigest()},
                }
            }

            def read(self, task_id, *, sha256=None, role_id=None):
                assert (task_id, sha256, role_id) == (
                    "plan-f1",
                    "artifact-sha",
                    "plan_reader",
                )
                return {"plan": plan}

            def child(self, task_id):
                return SimpleNamespace(task_directory=tmp_path / "reader-task")

            def state(self):
                return {"tasks": {}}

        session.registry = RecoveryRegistry()
        probe = AssemblyProbe()
        session.assembly = probe
        arguments = {"task_id": "plan-f1", "sha256": "artifact-sha"}
        identity = session._build_identity("plan-f1", {"materialization": decisions})
        operation_path = hashlib.sha256(identity.encode()).hexdigest()
        durable_result = envelope(
            {"candidate": "candidate_01", "source_geometry_ready": True}
        )
        saved_operation = {
            "operation_id": identity, "tool": "build_plan_bim",
            "arguments": {"image": "plan.png", "plan_json": json.dumps(materialized, ensure_ascii=False)},
            "reference": arguments, "result": durable_result,
        }
        if receipt_state == "intent_only":
            saved_operation.pop("result")
        elif receipt_state == "changed_arguments":
            saved_operation["arguments"]["plan_json"] = "{}"
        elif receipt_state == "changed_compiled":
            (workspace / "compiled.json").write_bytes(raw_plan + b" ")
        if receipt_state != "missing":
            store.write_json("role_operations/" + operation_path + ".json", saved_operation)
        operation_files_before = {p.name: p.read_bytes() for p in (store.directory / "role_operations").glob("*.json")}
        invocation = store.append(
            ToolInvocationPayload(
                call_id="recover-build",
                tool_name="build_from_artifact",
                full_arguments=arguments,
                repeatability="non_idempotent_write",
                operation_key="assembly-recovery:recover-build",
            )
        )

        if receipt_state in {"changed_arguments", "changed_compiled"}:
            with pytest.raises(ValueError, match="different arguments|compiled reader plan hash mismatch"):
                asyncio.run(session.recover_pending())
            recovered = []
        else:
            recovered = asyncio.run(session.recover_pending())
        assert session.frozen.calls == []
        assert {p.name: p.read_bytes() for p in (store.directory / "role_operations").glob("*.json")} == operation_files_before
        if receipt_state == "completed":
            assert recovered == [invocation.event_id]
            assert probe.binds == ["plan-f1"]
            assert probe.checks == [("candidate_01", False)]
            execution = next(event for event in store.events if event.payload.event_type == "tool_execution")
            assert execution.payload.invocation_event_id == invocation.event_id
            assert execution.payload.outcome == "succeeded"
            recovered_result = store.resolve(execution.payload.raw_result)
            assert recovered_result["structuredContent"].get("materialization", []) == decisions
            assert asyncio.run(session.recover_pending()) == []
            assert probe.binds == ["plan-f1"]
        else:
            assert recovered == [] and probe.binds == probe.checks == []
            assert not any(event.payload.event_type == "tool_execution" for event in store.events)
