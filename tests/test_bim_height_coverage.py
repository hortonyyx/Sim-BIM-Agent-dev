"""Opening height coverage stays narrower than facade/opening review coverage."""
import json

from src.agent.execution.bim_claim_state import project
from src.agent.execution.bim_height_coverage import height_coverage
from src.agent.geometry.source_model import _digest
from tests.test_bim_claims import adopt, claim, setup_run


def _opening(report, identity):
    return next(row for row in report["openings"] if row["opening_id"] == identity)


def _window_claim(toolkit, *, basis="annotation_and_pixels", values=None):
    return adopt(toolkit, claim(
        objects=[{"kind": "window", "id": "window"}],
        basis=basis,
        sources=[] if basis in {"inference", "declared"} else [{
            "image": "plan.png", "box": [0, 0, 12, 8],
        }],
        values=values or {"height": {"type": "literal", "value": [1, 2], "unit": "m"}},
    ))


def _confirm_window(toolkit, claim_id, changes):
    operation = {"op": "update_window", "id": "window", "changes": changes,
                 "reason": "confirm current opening parameter"}
    toolkit.confirm_claims("seed", json.dumps([operation]))


def test_actual_absolute_heights_start_unchecked_and_empty_facades_do_not_claim_review(tmp_path):
    _, toolkit = setup_run(tmp_path)
    state = project(toolkit.claims(), "seed")
    report = height_coverage(toolkit.claims(), "seed", current_state=state)

    assert report["delivery_blocked"] is False
    assert report["summary"] == {"total_count": 2, "image_linked_count": 0,
                                  "non_image_linked_count": 0,
                                  "unchecked_height_opening_ids": ["door", "window"]}
    assert _opening(report, "window")["absolute_z_m"] == [1.0, 2.0]
    assert _opening(report, "door")["absolute_z_m"] == [0.0, 2.1]
    assert {row["coverage_state"] for row in report["openings"]} == {"unchecked"}
    floor = report["floors"][0]
    west = next(row for row in floor["facades"] if row["facade"] == "West")
    assert west["actual_opening_ids"] == ["window"]
    assert west["unchecked_height_opening_ids"] == ["window"]
    empty = next(row for row in floor["facades"] if not row["actual_opening_ids"])
    assert empty["height_coverage"] == "no_built_openings"
    assert empty["drawing_fidelity"] == "not_evaluated"
    assert floor["non_facade"]["actual_opening_ids"] == ["door"]

    assert "retained_image_evidence" in _opening(report, "window")


def test_only_retained_z_is_linked_and_adopted_image_basis_checks_height(tmp_path):
    _, toolkit = setup_run(tmp_path)
    row = _window_claim(toolkit, values={
        "height": {"type": "literal", "value": [1, 2], "unit": "m"},
        "width": {"type": "literal", "value": [1, 2], "unit": "m"},
    })
    _confirm_window(toolkit, row["id"], {
        "z": {"claim": row["id"], "value": "height"},
        "span": {"claim": row["id"], "value": "width"},
    })

    report = height_coverage(toolkit.claims(), "seed")
    window = _opening(report, "window")
    assert window["coverage_state"] == "image_evidence_linked"
    assert window["image_height_evidence_linked"] is True
    assert len(window["retained_image_evidence"]) == 1
    assert window["retained_image_evidence"][0]["parameter"] == "z"
    assert window["retained_image_evidence"][0]["source_images"] == ["plan.png"]

    toolkit.decide_claim(row["id"], "retracted", "height interpretation contradicted")
    after = _opening(height_coverage(toolkit.claims(), "seed"), "window")
    assert after["coverage_state"] == "unchecked"
    assert after["retained_image_evidence"] == []

    span = _window_claim(toolkit, values={
        "width": {"type": "literal", "value": [1, 2], "unit": "m"},
    })
    _confirm_window(toolkit, span["id"], {
        "span": {"claim": span["id"], "value": "width"},
    })
    span_only = _opening(height_coverage(toolkit.claims(), "seed"), "window")
    assert span_only["coverage_state"] == "unchecked"
    assert span_only["retained_image_evidence"] == []

    toolkit.record_claim(json.dumps(claim(
        objects=[{"kind": "window", "id": "window"}],
        values={"height": {"type": "literal", "value": [1, 2], "unit": "m"}},
    )))
    pending = _opening(height_coverage(toolkit.claims(), "seed"), "window")
    assert pending["coverage_state"] == "unchecked"


def test_inference_binding_is_visible_but_remains_in_unchecked_drawing_scope(tmp_path):
    _, toolkit = setup_run(tmp_path)
    row = _window_claim(toolkit, basis="inference")
    _confirm_window(toolkit, row["id"], {
        "z": {"claim": row["id"], "value": "height"},
    })

    report = height_coverage(toolkit.claims(), "seed")
    window = _opening(report, "window")
    assert window["coverage_state"] == "non_image_evidence_only"
    assert window["image_height_evidence_linked"] is False
    assert window["retained_image_evidence"] == []
    assert window["retained_non_image_evidence"][0]["basis"] == "inference"
    west = next(row for row in report["floors"][0]["facades"] if row["facade"] == "West")
    assert west["non_image_evidence_only_opening_ids"] == ["window"]
    assert west["unchecked_height_opening_ids"] == ["window"]


def test_changed_height_invalidates_check_and_reports_new_source_vertices(tmp_path):
    _, toolkit = setup_run(tmp_path)
    row = _window_claim(toolkit)
    _confirm_window(toolkit, row["id"], {
        "z": {"claim": row["id"], "value": "height"},
    })
    revised = toolkit.revise("seed", json.dumps([{
        "op": "update_window", "id": "window", "changes": {"z": [1, 2.2]},
        "reason": "later source interpretation", "source_refs": ["fixture"],
    }]))["candidate"]

    window = _opening(height_coverage(toolkit.claims(), revised), "window")
    assert window["absolute_z_m"] == [1.0, 2.2]
    assert window["coverage_state"] == "unchecked"
    assert window["retained_image_evidence"] == []


def test_cross_floor_opening_is_unchecked_in_each_host_floor_scope(tmp_path):
    run, toolkit = setup_run(tmp_path)
    source_path = run / "seed/source_model.json"
    source = json.loads(source_path.read_text(encoding="utf-8"))
    source["floors"].append({**source["floors"][0], "id": "ANNEX"})
    next(row for row in source["spaces"] if row["id"] == "room")["floor_id"] = "ANNEX"
    source["source_model_sha256"] = _digest({
        key: value for key, value in source.items() if key != "source_model_sha256"
    })
    source_path.write_text(json.dumps(source))

    report = height_coverage(toolkit.claims(), "seed")
    door = _opening(report, "door")
    assert door["floor_ids"] == ["ANNEX", "F1"]
    for floor in report["floors"]:
        assert "door" in floor["all_openings"]["actual_opening_ids"]
        assert "door" in floor["all_openings"]["unchecked_height_opening_ids"]
