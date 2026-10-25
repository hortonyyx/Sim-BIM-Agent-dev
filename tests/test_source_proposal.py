"""Direct geometry proposals stay inspectable without claiming a correction run."""
from __future__ import annotations

import copy
import hashlib
import json

import pytest

from src.agent.execution.source_proposal import export_source_proposal, compile_source_proposal


def _proposal():
    return {
        "geometry": {
            "schema_version": "2", "footprint_x": [0, 6], "footprint_y": [0, 6],
            "floors": [{"name": "F1", "z_floor": 0, "ceiling_height": 3, "cells": [
                {"id": "hall", "x": [0, 3], "y": [0, 6]},
                {"id": "room", "x": [3, 6], "y": [0, 6]},
            ]}],
            "windows": [{"id": "window", "floor": "F1", "facade": "West", "span": [1, 2],
                         "z": [1, 2], "room": "hall"}],
            "openings": [{"id": "door", "kind": "door", "space_id": "hall", "other_space_id": "room",
                          "p1": [3, 1], "p2": [3, 2], "z": [0, 2.1], "source_refs": ["proposal:door"]}],
        },
        "assumptions": ["<墙体位置由平面推断>"],
        "unresolved": ["<b>未确认门状态</b>"],
    }


@pytest.mark.parametrize("bad_host", [False, True])
def test_preview_and_export_use_identical_source_kernel_without_rendering(tmp_path, monkeypatch, bad_host):
    from src.agent.execution import source_proposal as module
    proposal = _proposal()
    if bad_host:
        proposal["geometry"]["openings"][0]["other_space_id"] = "missing"
    before = copy.deepcopy(proposal)
    exported = export_source_proposal(proposal, tmp_path / "saved")
    expected = json.loads((tmp_path / "saved/source_model.json").read_bytes())
    def forbidden(*args, **kwargs):
        pytest.fail("the construction check must not render a source")
    monkeypatch.setattr(module, "source_view_geometry", forbidden)
    monkeypatch.setattr(module, "build_source_viewer_html", forbidden)
    source, checked = compile_source_proposal(proposal)
    assert json.loads(json.dumps(source)) == expected
    assert checked["source_geometry_ready"] == exported["source_geometry_ready"] == (not bad_host)
    assert checked["source_validation"] == exported["source_validation"]
    assert proposal == before


def test_direct_proposal_exports_independent_source_bim_with_openings(tmp_path):
    proposal = _proposal()
    out = tmp_path / "candidate"
    report = export_source_proposal(proposal, out, provenance={"input": "agent"})

    assert report["mode"] == "agent_geometry_proposal"
    assert report["source_geometry_ready"]
    assert report["source_geometry_self_consistency"]["status"] == "pass"
    assert report["drawing_fidelity"] == "not_evaluated"
    assert report["human_confirmation"] == "not_evaluated"
    assert report["counts"]["openings"] == 2
    assert report["provenance"] == {"input": "agent"}
    assert report["proposal_sha256"] == hashlib.sha256((out / "proposal.json").read_bytes()).hexdigest()
    assert report["source_model_sha256"] == json.loads((out / "source_model.json").read_text())["source_model_sha256"]
    source = json.loads((out / "source_model.json").read_text())
    assert proposal["assumptions"][0] in source["assumptions"]
    assert source["generation"]["unresolved"] == proposal["unresolved"]
    assert json.loads((out / "proposal.json").read_text()) == proposal
    assert (out / "display_geometry.json").exists()
    viewer = (out / "viewer.html").read_text(encoding="utf-8")
    assert "&lt;墙体位置由平面推断&gt;" in viewer
    assert "&lt;b&gt;未确认门状态&lt;/b&gt;" in viewer
    assert "<b>未确认门状态</b>" not in viewer


def test_invalid_opening_still_exports_candidate_with_severe_finding(tmp_path):
    proposal = _proposal()
    proposal["geometry"]["openings"][0]["other_space_id"] = "missing"
    out = tmp_path / "severe"
    report = export_source_proposal(proposal, out)

    assert report["status"] == "severe"
    assert not report["source_geometry_ready"]
    assert report["counts"]["spaces"] == 2
    assert report["counts"]["unbuilt_openings"] == 1
    source = json.loads((out / "source_model.json").read_text())
    assert source["unbuilt_openings"][0]["id"] == "door"
    assert (out / "viewer.html").exists()


def test_room_observations_and_function_basis_survive_export_without_becoming_wall_evidence(tmp_path):
    proposal = _proposal()
    cell = proposal["geometry"]["floors"][0]["cells"][1]
    cell.update(role="office", source_refs=["plan: room boundary at [300,100]"],
                assumptions=["Geometry uses representative wall planes"], role_evidence={
                    "role": "office", "basis": "inferred", "source_refs": ["plan: desks at [400,200]"],
                    "assumptions": ["Furniture supports office use; <unconfirmed>"]})
    before = copy.deepcopy(proposal)
    target = tmp_path / "with_evidence"
    assert export_source_proposal(proposal, target)["source_geometry_ready"]
    source = json.loads((target / "source_model.json").read_text())
    room = next(s for s in source["spaces"] if s["id"] == "room")
    assert room["source_refs"] == ["correction:floor/F1/cell/room", *cell["source_refs"]]
    assert room["assumptions"] == cell["assumptions"]
    assert room["role_evidence"] == cell["role_evidence"]
    assert all("desks" not in str(b["source_refs"]) for b in source["boundaries"])
    display = json.loads((target / "display_geometry.json").read_text())
    assert display["source_model"]["spaces"] == source["spaces"]
    assert json.loads((target / "proposal.json").read_text()) == before == proposal
    # A retained evidence record cannot silently certify a different new role.
    cell["role"] = "storage"
    failed = export_source_proposal(proposal, tmp_path / "mismatch")
    assert failed["status"] == "error" and "role" in failed["error"]


def test_conflicting_window_kinds_are_reported_together_without_retyping(tmp_path):
    proposal = _proposal()
    for kind in ("door", "passage", None):
        item = copy.deepcopy(proposal["geometry"]["windows"][0])
        item.update(id=f"declared_{kind}", kind=kind, z=[0, 2.1])
        proposal["geometry"]["windows"].append(item)
    original = copy.deepcopy(proposal)
    out = tmp_path / "conflicting_types"
    report = export_source_proposal(proposal, out)

    assert report["status"] == "error" and not report["source_geometry_ready"]
    for kind in ("door", "passage", None):
        assert f"declared_{kind}" in report["error"]
    assert "geometry.openings" in report["error"]
    assert "other_space_id=null" in report["error"]
    assert proposal == original
    assert json.loads((out / "proposal.json").read_text()) == original
    assert not (out / "source_model.json").exists()
    assert not (out / "viewer.html").exists()


def test_explicit_window_kind_and_exterior_door_keep_their_declared_types(tmp_path):
    proposal = _proposal()
    proposal["geometry"]["windows"][0]["kind"] = "window"
    proposal["geometry"]["openings"].append({
        "id": "external_door", "kind": "door", "space_id": "hall", "other_space_id": None,
        "p1": [0, 3], "p2": [0, 4], "z": [0, 2.1], "source_refs": ["synthetic exterior door"],
    })
    out = tmp_path / "explicit_types"
    report = export_source_proposal(proposal, out)
    assert report["source_geometry_ready"]
    source = json.loads((out / "source_model.json").read_text())
    kinds = {opening["id"]: opening["kind"] for opening in source["openings"]}
    assert kinds == {"window": "window", "door": "door", "external_door": "door"}
    assert json.loads((out / "proposal.json").read_text()) == proposal


def test_proposal_output_never_overwrites_or_invents_a_run_record(tmp_path):
    out = tmp_path / "candidate"
    export_source_proposal(_proposal(), out)
    before = (out / "report.json").read_bytes()
    with pytest.raises(FileExistsError):
        export_source_proposal(_proposal(), out)
    assert (out / "report.json").read_bytes() == before
    assert not (out / "_run" / "geometry_approval.json").exists()
    assert "accepted" not in json.loads(before)

    failed = tmp_path / "failed"
    report = export_source_proposal({"geometry": {}, "assumptions": [], "unresolved": [], "windows": []}, failed)
    assert report["status"] == "error"
    assert "unknown windows" in report["error"]
    assert json.loads((failed / "report.json").read_text())["status"] == "error"


def test_export_retains_complete_local_edit_audit_and_rehashes_source(tmp_path):
    from src.agent.geometry.proposal_edits import apply_proposal_edits
    from src.agent.geometry.source_model import _digest

    parent = _proposal()
    original = copy.deepcopy(parent)
    revised = apply_proposal_edits(parent, [
        {"op": "update_opening", "id": "door", "changes": {"state": "closed", "assumptions": ["leaf checked"]},
         "reason": "new drawing evidence", "source_refs": ["detail:door"]},
        {"op": "remove_opening", "id": "door", "reason": "opening contradicted",
         "source_refs": ["detail:solid-wall"]},
    ])
    assert parent == original
    export_source_proposal(revised, tmp_path / "candidate")
    source = json.loads((tmp_path / "candidate" / "source_model.json").read_text())

    audit = revised["geometry"]["corrections"]
    assert source["generation"]["corrections"] == audit
    assert audit[-1]["before"]["id"] == "door" and audit[-1]["before"]["source_refs"] == ["detail:door"]
    assert source["source_model_sha256"] == _digest({
        key: value for key, value in source.items() if key != "source_model_sha256"
    })
