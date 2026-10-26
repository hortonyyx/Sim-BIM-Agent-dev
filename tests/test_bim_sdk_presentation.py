"""Printed receipts are smaller without changing programmatic or saved evidence."""
import asyncio
import base64
import copy
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from mcp.types import CallToolResult, ImageContent, TextContent

from src.agent.sdk import BimSession, ToolReply, main


def reply(data, *, tool="build_parametric_bim", error=False, texts=None):
    return ToolReply(data, texts or [], [], error, "D:/run/sdk/receipts/example.json", tool)


def test_regularization_log_folds_without_losing_any_rejection_or_warning():
    rejections = [{"code": "outline_displacement_exceeded", "path": f"room{i}",
                   "worst_vertex": {"before": [i, 0], "after": [i, .4],
                                    "supports": [{"normal": [1, 0], "offset": i}]},
                   "limit_m": .1} for i in range(60)]
    warnings = [{"code": f"uncertain_{i}", "message": "keep " * 1000} for i in range(8)]
    audit = {"schema_version": "architectural_regularization_v1", "status": "rejected",
             "changes": [{"type": "supporting_line_anchor", "observed_offsets_m": [i, i + .001]}
                         for i in range(100)], "attempted_changes": [{"path": "last"}],
             "rejections": rejections, "diagnostics": [{"code": "rounded_line_coincidence"}],
             "not_assessed": [{"path": "apertures", "blocked_by": "ring_adoption"}],
             "warnings": warnings}
    data = {"status": "error", "error": "ring_adoption", "parametric": {"regularization_failure": audit},
            "edit_route": {"next_action": "repair these objects"}}
    original = copy.deepcopy(data)
    result = reply(data, error=True)
    shown = result.presentation()["data"]
    projected = shown["parametric"]["regularization_failure"]
    assert projected["changes"]["count"] == 100
    assert projected["changes"]["full_receipt_pointer"].endswith("/parametric/regularization_failure/changes")
    for key in ("rejections", "diagnostics", "not_assessed", "warnings", "status"):
        assert projected[key] == audit[key]
    assert shown["edit_route"] == data["edit_route"]
    assert result.data == original
    with pytest.raises(RuntimeError, match="ring_adoption"):
        result.require_ok()


@pytest.mark.parametrize("status", ["severe", "blocked", "not_evaluated", "warning"])
def test_no_new_verdict_for_saved_or_uncertain_candidate(status):
    data = {"status": status, "candidate": "candidate_1", "saved_candidate": None,
            "source_geometry_ready": False, "source_model_sha256": "a" * 64,
            "source_validation": {"status": status, "findings": [{"id": i} for i in range(100)]},
            "closeout": {"machine_check": {"status": "blocked"}, "next_action": "repair"},
            "mesh_feedback": {"overlays": [{"overlay_id": "overlay_3", "observation": "mesh_2"}]}}
    result = reply(data, tool="patch_parametric_bim")
    assert json.loads(str(result))["data"] == data
    assert result.require_ok() is data  # tool success still does not mean acceptance
    assert result.is_error is False


def test_unknown_tool_and_unknown_audit_schema_are_not_cropped():
    data = {"schema_version": "future_audit_v2", "status": "pass",
            "changes": [{"value": "large" * 1000}] * 50,
            "inventory": {"future_format": [1, 2, 3]}}
    for tool in ("future_tool", "build_parametric_bim"):
        assert reply(data, tool=tool).presentation()["data"] == data
    assert reply({"images": ["future form"]}, tool="inputs").presentation()["data"]["images"] == ["future form"]


def test_successful_audit_keeps_current_scope_and_status_without_coordinate_history():
    audit = {"schema_version": "architectural_regularization_v1", "status": "adopted",
             "changes": [{"type": "supporting_line_anchor", "before": [0, .02], "after": [0, 0]}] * 40,
             "rejections": [], "diagnostics": [{"code": "nearby_distinct_support"}],
             "required_next_check": "common source kernel and topology save gate",
             "input_sha256": "i" * 64, "adopted_sha256": "a" * 64}
    data = {"regularization": audit, "source_geometry_ready": True}
    shown = reply(data).presentation()["data"]["regularization"]
    assert shown["changes"]["count"] == 40
    assert {k: v for k, v in shown.items() if k != "changes"} == {k: v for k, v in audit.items() if k != "changes"}


def test_input_paths_identity_scope_and_view_state_survive():
    data = {"scope": "Only declared floors; keep uncertainty.",
            "images": {"plan.png": {"size": [900, 800], "sha256": "p" * 64}},
            "implementation_sha256": {f"source/{i}.py": "h" * 64 for i in range(100)},
            "building_input": {"source_path": "D:/original/building.json", "frozen_path": "building_input.json",
                "raw_sha256": "b" * 64, "declaration": {"user_observation": "could be a hotel"},
                "field_semantics": {"other_fields": "not independently verified"}},
            "mesh_input": {"source_path": "D:/original/building.glb", "frozen_path": "assets/input.glb",
                           "sha256": "m" * 64, "geometry": {"coordinate_frame": "Z-up", "bounds": [[0,0,0],[1,1,1]]}},
            "input_view_status": {"no_direct_view_images": ["plan.png"], "delivery_blocked": False}}
    original = copy.deepcopy(data)
    shown = reply(data, tool="inputs").presentation()["data"]
    for key in ("scope", "input_view_status"):
        assert shown[key] == data[key]
    assert shown["images"]["plan.png"]["sha256"] == "p" * 64
    assert shown["images"]["plan.png"]["size"] == [900, 800]
    assert Path(shown["images"]["plan.png"]["path"]) == Path("D:/run/images/plan.png")
    assert Path(shown["building_input"]["read_path"]) == Path("D:/run/building_input.json")
    assert shown["building_input"]["raw_sha256"] == "b" * 64
    assert shown["mesh_input"]["geometry"] == data["mesh_input"]["geometry"]
    assert shown["building_input"]["field_semantics"] == data["building_input"]["field_semantics"]
    assert shown["implementation_sha256"]["file_count"] == 100
    assert data == original


def test_budget_label_and_duplicate_text_are_display_only():
    data = {"remaining_seconds": 1600, "error": "keep failure"}
    texts = [json.dumps(data), "保守剩余（本运行）：时间 26.7 分钟；停止新范围探索。", "distinct warning"]
    result = reply(data, texts=texts, error=True)
    shown = result.presentation()
    assert len(shown["texts"]) == 2
    assert "硬执行剩余额度" in shown["texts"][0]
    assert "任务目标可能更短" in shown["texts"][0]
    assert "停止新范围探索" in shown["texts"][0]
    assert shown["texts"][-1] == "distinct warning"
    assert result.texts == texts
    assert shown["data"] == data


def test_existing_domain_reference_keeps_hash_pointer_and_current_values():
    reference = {"report_file": "tool_reports/saved.json", "report_sha256": "r" * 64,
                 "json_pointer": "/building_precision", "sha256": "v" * 64,
                 "read": {"tool": "read_candidate_items", "arguments": {"candidate": "",
                    "collection": "report", "report_file": "tool_reports/saved.json", "json_pointer": "/building_precision"}}}
    data = {"details_file": reference["report_file"], "building_precision": {
            "reference": reference, "entries": [{"key": "status", "value": "partially_applicable"}],
            "next_offset": 8}}
    shown = reply(data).presentation()["data"]
    assert shown["building_precision"]["reference"] == {k: v for k, v in reference.items() if k != "read"}
    assert shown["building_precision"]["entries"] == data["building_precision"]["entries"]
    assert shown["building_precision"]["next_offset"] == 8
    reference["read"]["warning"] = "future reader constraint"
    assert reply(data).presentation()["data"]["building_precision"]["reference"] == reference


def test_cli_and_python_keep_full_receipt_and_original_image_bytes(tmp_path, monkeypatch, capsys):
    # Exercise the real SDK response boundary with a no-model tool result.
    pixels = b"lossless image payload, never transformed by the SDK"
    data = {"images": {}, "scope": "preserve scope", "implementation_sha256": {"file.py": "a" * 64}}
    calls = []

    def session_factory(run, **kwargs):
        session = object.__new__(BimSession)
        session.run = Path(run).resolve()
        session._runner, session._closed = asyncio.Runner(), False
        session._receipts, session._images = session.run / "sdk/receipts", session.run / "sdk/images"
        for folder in (session._receipts, session._images):
            folder.mkdir(parents=True, exist_ok=True)
        session._tools = {"inputs": {"inputSchema": {"properties": {}}}}

        async def call_tool(name, arguments):
            calls.append((name, arguments))
            return CallToolResult(structuredContent=copy.deepcopy(data), content=[
                TextContent(type="text", text=json.dumps(data)),
                TextContent(type="text", text="warning remains"),
                ImageContent(type="image", mimeType="image/png", data=base64.b64encode(pixels).decode())])
        session._server = SimpleNamespace(call_tool=call_tool)
        return session

    with session_factory(tmp_path) as session:
        result = session.call("inputs")
        raw = Path(result.receipt_file).read_bytes()
        assert result.data == data
        assert result.texts == ["warning remains"]
        assert result.images[0]["sha256"] == hashlib.sha256(pixels).hexdigest()
        assert Path(result.images[0]["path"]).read_bytes() == pixels
        shown = json.loads(str(result))
        assert shown["data"]["implementation_sha256"]["file_count"] == 1
        assert Path(result.receipt_file).read_bytes() == raw
        assert json.loads(raw)["result"]["structuredContent"] == data
        assert json.loads(raw)["result"]["content"][0]["text"] == json.dumps(data)
    monkeypatch.setattr("src.agent.sdk.BimSession", session_factory)
    assert main(["--run", str(tmp_path), "call", "inputs"]) == 0
    cli = json.loads(capsys.readouterr().out)
    assert cli["data"] == shown["data"]
    assert cli["texts"] == shown["texts"]
    assert cli["images"] == shown["images"]
    assert calls == [("inputs", {}), ("inputs", {})]


def saved_data():
    adoption = {"schema_version": 1, "kind": "parametric",
        "policy": {"grid_m": .1}, "modelling": {"frames": [{"id": "local", "origin": [0, 0]}]},
        "original_sha256": "a" * 64, "adopted_sha256": "b" * 64,
        "original": {"file": "draft.json"}, "audit": {"file": "audit.json"}}
    return {"candidate": "candidate_01", "mode": "agent_geometry_proposal",
        "status": "not_evaluated", "source_geometry_ready": True,
        "source_validation": {"status": "pass", "findings": [], "not_evaluated": ["drawing fidelity"]},
        "source_model_sha256": "s" * 64, "drawing_fidelity": "not_evaluated",
        "architectural_regularization": adoption,
        "provenance": {"generator": "normal tool", "architectural_regularization": copy.deepcopy(adoption)},
        "parametric": {"schema_version": 1, "status": "current", "draft": "draft.json",
                       "architectural_regularization": copy.deepcopy(adoption)},
        "edit_route": {"recommended_tool": "patch_parametric_bim",
            "patch_arguments": {"candidate": "candidate_01", "expected_plan_sha256": "h" * 64},
            "patch_contract": {"operations": ["test", "replace"], "allowed_roots": ["templates"]}},
        "source_plan_views": [{"floor_id": "level", "plan_image": "plan.png",
            "space_names": {f"s{i}": f"Room {i}" for i in range(200)},
            "space_ids": [f"s{i}" for i in range(200)], "opening_ids": [f"d{i}" for i in range(200)]}],
        "process_review": {"pending": [{"kind": "plan", "image": "plan.png", "floor_id": "level"}],
            "building_review": {"status": "missing"}},
        "save_feedback": {"door_geometry": {"measured_count": 2, "unmeasured_count": 1,
            "smallest_measured_doors": [{"id": "door-access", "width_m": .82}],
            "meaning": "Wall opening XY span; not minimum clear passage."}}}


def test_saved_candidate_normal_display_budget_keeps_facts_pending_and_patch_arguments():
    data = saved_data()
    original = copy.deepcopy(data)
    result = reply(data)
    shown = result.presentation()["data"]
    assert len(str(result)) < 6000  # ordinary receipt, not a cap on diagnostic output
    assert shown["status"] == "not_evaluated" and shown["source_geometry_ready"] is True
    for field in ("source_validation", "process_review", "save_feedback"):
        assert shown[field] == data[field]
    assert shown["edit_route"]["patch_arguments"] == data["edit_route"]["patch_arguments"]
    assert shown["source_plan_views"][0]["floor_id"] == "level"
    assert shown["source_plan_views"][0]["plan_image"] == "plan.png"
    assert "space_names" not in shown["source_plan_views"][0]
    assert result.require_ok() is data and data == original


def test_every_saved_issue_unknown_and_unassessed_scope_stays_visible_without_cap():
    data = saved_data()
    findings = [{"code": "review_needed", "id": f"room/{i}", "location": [i, 2],
                 "message": "independent evidence required " * 10} for i in range(100)]
    data["source_validation"]["findings"] = findings
    data["building_precision"] = {"schema": "building_precision_v2", "status": "pass",
        "warnings": findings, "axis_rule_applicability": {"status": "not_applicable",
            "not_applicable_space_ids": ["oblique-room"]},
        "wall_placement": {"status": "not_assessed", "reason": "no annotations"}}
    data["unresolved"] = [{"id": "unknown-use", "question": "original label unclear"}]
    data["provenance"]["architectural_regularization"]["policy"]["future_warning"] = findings[-1]
    data["future_diagnostic"] = {"new_schema": 2, "item": findings[-1]}
    shown = reply(data).presentation()["data"]
    for key in ("source_validation", "building_precision", "unresolved", "future_diagnostic"):
        assert shown[key] == data[key]
    assert shown["provenance"]["architectural_regularization"]["policy"] == data["provenance"]["architectural_regularization"]["policy"]
    assert len(json.dumps(shown)) > 6000


def test_overlay_folds_only_normal_coordinates_and_preserves_every_exception():
    normal = [{"id": f"space-{i}", "pixel_polygon": [[j, i] for j in range(80)],
               "out_of_image": False} for i in range(20)]
    outside = {"id": "outside", "pixel_polygon": [[-3, 2]], "out_of_image": True}
    unknown = {"id": "future", "pixel_polygon": [[1, 2]], "out_of_image": False,
               "future_visibility": "unknown"}
    data = {"mode": "source_image_overlay", "candidate": "candidate_01", "image": "plan.png",
        "floor_id": "floor", "calibration_unverified": True, "drawing_fidelity": "not_evaluated",
        "scale": {"warnings": [{"type": "disagreement", "relative_deviation": .004}]},
        "limits": ["caller supplied calibration"], "out_of_image": {"space_ids": ["outside"]},
        "projected_spaces": normal + [outside, unknown], "projected_openings": [],
        "future_field": {"status": "not_assessed"}}
    original = copy.deepcopy(data)
    shown = reply(data, tool="overlay_candidate").presentation()["data"]
    assert shown["projected_spaces"]["count"] == 22
    assert shown["projected_spaces"]["exceptions"] == [outside, unknown]
    assert {k: v for k, v in shown.items() if k != "projected_spaces"} == {k: v for k, v in data.items() if k != "projected_spaces"}
    assert len(json.dumps(shown)) < len(json.dumps(data)) / 4
    assert data == original


def test_unknown_overlay_mode_and_failed_overlay_are_not_shortened():
    data = {"mode": "future_overlay_v2", "projected_spaces": [{"id": "p", "out_of_image": False}]}
    assert reply(data, tool="overlay_candidate").presentation()["data"] == data
    data["mode"] = "source_image_overlay"
    data["error"] = "bad calibration"
    assert reply(data, tool="overlay_candidate", error=True).presentation()["data"] == data


def test_view_image_keeps_issued_identity_coordinates_and_actual_image_path():
    data = {"name": "section.png", "image_sha256": "i" * 64, "view_id": "view_7",
        "source_reference": {"view_id": "view_7"}, "original_size": [1400, 900],
        "box_original_pixels": [100, 200, 700, 600], "returned_size": [600, 400],
        "original_pixels_per_returned_pixel": [1, 1], "coordinate_note": "Original pixel coordinates."}
    images = [{"path": "D:/run/sdk/images/current.png", "sha256": "p" * 64, "bytes": 81}]
    result = ToolReply(data, [], images, False, "D:/run/sdk/receipts/view.json", "view_image")
    shown = result.presentation()
    assert shown["data"] == data and shown["images"] == images


def test_display_receipt_pointers_resolve_to_full_original_objects():
    data = saved_data()
    shown = reply(data).presentation()["data"]
    pointers = []
    def visit(value):
        if isinstance(value, dict):
            if "full_receipt_pointer" in value:
                pointers.append(value["full_receipt_pointer"])
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)
    visit(shown)
    assert pointers
    for pointer in pointers:
        current = {"result": {"structuredContent": data}}
        for token in pointer.removeprefix("/").split("/"):
            token = token.replace("~1", "/").replace("~0", "~")
            current = current[int(token)] if isinstance(current, list) else current[token]
        assert current is not None


def test_unsupported_location_factoring_is_lossless_not_sampling():
    data = saved_data()
    rows = [{"boundary_id": f"wall-{i}", "reason": "host_direction_unsupported"} for i in range(100)]
    rows[-1]["extra"] = "last diagnosis stays"
    data["facade_counts"] = {"schema_version": "facade_count_report_v1",
        "unsupported_exterior_boundaries": [{"floor_id": "L", "boundaries": rows}]}
    factored = reply(data).presentation()["data"]["facade_counts"]["unsupported_exterior_boundaries"][0]["boundaries"]
    assert [{**factored["common"], **row} for row in factored["rows"]] == rows


def test_changed_explanatory_context_and_unknown_status_are_not_rewritten():
    from src.agent.sdk_presentation import _CONTEXT
    data = saved_data()
    current, _ = _CONTEXT["input_view_status/note"]
    data["input_view_status"] = {"note": current + " New current warning: inspect west face."}
    data["parametric"]["patch"] = {"status": {"future": "unknown"}, "warning": "keep"}
    shown = reply(data).presentation()["data"]
    assert shown["input_view_status"] == data["input_view_status"]
    assert shown["parametric"]["patch"] == data["parametric"]["patch"]


def test_existing_details_and_future_nested_report_are_not_overwritten():
    data = saved_data()
    data["edit_route"]["details"] = {"warning": "caller must read this"}
    data["architectural_regularization"]["policy"] = {"schema_version": "future_policy_v2", "new_fact": [1, 2]}
    data["building_precision"] = {"schema": "future_precision_v3",
        "source_model_sha256": data["source_model_sha256"], "new_result": "unknown"}
    shown = reply(data).presentation()["data"]
    assert shown["edit_route"] == data["edit_route"]
    assert shown["architectural_regularization"]["policy"] == data["architectural_regularization"]["policy"]
    assert shown["building_precision"] == data["building_precision"]
