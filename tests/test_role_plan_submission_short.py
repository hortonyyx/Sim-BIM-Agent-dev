"""Reference-only plan submission and full/operation rework share the same safeguards."""

from reader_delivery_fixtures import REVIEW, compact_elevation, accept_elevation

import asyncio
import copy
import hashlib
import json

import pytest
from PIL import Image

from src.agent.runtime_roles.readers import _evidence_targets
from src.agent.runtime_roles.submission import ReaderSubmission, _normalize_note_items
from src.agent.runtime_roles.trial import PlanTrial, canonical_plan_sha256
from tests.test_role_readers import plan as plan_declaration
from tests.test_role_submission import PassedTrial, operation
from tests.test_role_trial import Tools, example, prepare_workspace


def plan():
    value = plan_declaration()
    value["wall_metadata"] = copy.deepcopy(example()["wall_metadata"])
    return value


def test_automatic_boxes_use_resolved_pixels_keep_inferences_and_allow_wall_override():
    trial = PassedTrial()
    numeric = copy.deepcopy(trial.plan)
    trial.plan["openings"][0]["p1"][0] = {"profile": "profile_001", "candidate": "C01"}
    trial.receipt["plan_sha256"] = canonical_plan_sha256(trial.plan)
    trial.numeric_plan = lambda receipt: copy.deepcopy(numeric)
    submission = ReaderSubmission(role_id="plan_reader", image_name="plan.png", trial=trial)
    result = submission.submit({"local_review": REVIEW, "trial_id": "latest", "notes": [
        {"item": "left", "kind": "inferred", "basis": "use from furniture"},
        {"item": "openings.W1", "kind": "unresolved", "basis": "verify height"}],
        "wall_reference": {"partitions": {"convention": "inner_face", "basis": "dimension converted to inner face"}}})
    assert result["status"] == "accepted"
    saved = submission.read()
    evidence = {row["item"]: row for row in saved["artifact"]["evidence"]}
    assert set(evidence) == _evidence_targets(trial.plan)
    assert evidence["plan.openings:W1"]["bbox"] == [8, 28, 12, 42]
    assert {row["source"] for row in evidence.values()} == {"plan.png"}
    assert "[inferred] use from furniture" in evidence["plan.space_seeds:left"]["basis"]
    assert "plan.openings:W1: verify height" in saved["artifact"]["unresolved"]
    assert saved["artifact"]["plan"] == numeric
    refs = saved["validation"]["wall_reference"]
    assert refs["perimeter"]["convention"] == "outer_face"
    assert refs["partitions"]["dimension_basis"] == "inner_face"
    assert saved["validation"]["notes"][0]["kind"] == "inferred"
    assert [row["item"] for row in saved["validation"]["notes"]] == [
        "plan.space_seeds:left", "plan.openings:W1"
    ]


def test_note_object_aliases_normalize_only_when_unambiguous():
    notes = [{"item": "W1", "kind": "unresolved", "basis": "verify height"}]
    assert _normalize_note_items(notes, {"plan.openings:W1", "plan.partitions:P1"})[0]["item"] == (
        "plan.openings:W1"
    )
    with pytest.raises(ValueError, match="ambiguous"):
        _normalize_note_items(notes, {"plan.openings:W1", "plan.partitions:W1"})
    with pytest.raises(ValueError, match="not a declared plan object"):
        _normalize_note_items(
            [{"item": "W9", "kind": "unresolved", "basis": "unknown mark"}],
            {"plan.openings:W1"},
        )
    field_note = [{"item": "openings", "kind": "assumption", "basis": "heights await elevations"}]
    with pytest.raises(ValueError, match="ambiguous"):
        _normalize_note_items(field_note, {"plan.openings", "plan.partitions:openings"})


def test_field_and_collection_notes_preserve_trial_and_do_not_invent_pixel_evidence():
    trial = PassedTrial()
    original = copy.deepcopy(trial.plan)
    submission = ReaderSubmission(role_id="plan_reader", image_name="plan.png", trial=trial)
    result = submission.submit({"local_review": REVIEW, "trial_id": "latest", "notes": [
        {"item": "ceiling_height", "kind": "assumption", "basis": "No section; assumed height."},
        {"item": "plan.openings", "kind": "assumption", "basis": "Heights await elevations."},
        {"item": "z_floor", "kind": "unresolved", "basis": "Confirm shared vertical datum."},
    ]})
    assert result["status"] == "accepted"
    saved = submission.read()
    assert saved["artifact"]["plan"] == original
    assert {row["item"] for row in saved["artifact"]["evidence"]} == _evidence_targets(original)
    assert [row["item"] for row in saved["validation"]["notes"]] == [
        "plan.ceiling_height", "plan.openings", "plan.z_floor"]
    assert "plan.z_floor: Confirm shared vertical datum." in saved["artifact"]["unresolved"]


def test_current_trial_fields_accept_notes_without_product_or_evidence_changes(tmp_path):
    from tests.test_plan_reading_delivery import setup

    original, trial = setup(tmp_path)
    receipt = asyncio.run(trial.run(original))
    assert receipt["status"] == "passed"
    baseline = ReaderSubmission(role_id="plan_reader", image_name="plan.png", trial=trial)
    baseline.submit({"trial_id": "latest", "local_review": REVIEW})
    before = {path: path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}
    receipt_before = copy.deepcopy(receipt)
    params = {"trial_id": "latest", "local_review": REVIEW, "notes": [
        {"item": "axis_orientation", "kind": "assumption", "basis": "Absolute north needs confirmation."},
        {"item": "trial.wall_metadata_summary", "kind": "inferred", "basis": "Coverage is local, not completeness."},
        {"item": "drawing_differences", "kind": "inferred", "basis": "Only the reported ink scope was checked."},
    ]}
    params_before = copy.deepcopy(params)
    submission = ReaderSubmission(role_id="plan_reader", image_name="plan.png", directory=tmp_path, trial=trial)
    assert submission.submit(params)["status"] == "accepted"
    saved = submission.read()
    assert [note["item"] for note in saved["validation"]["notes"]] == [
        "trial.axis_orientation", "trial.wall_metadata_summary", "trial.drawing_differences"]
    assert saved["artifact"] == baseline.read()["artifact"]
    assert saved["artifact_sha256"] == baseline.read()["artifact_sha256"]
    assert trial.receipts == [receipt_before] and params == params_before
    assert all(path.read_bytes() == raw for path, raw in before.items())


@pytest.mark.parametrize("target", ["axis_orientation", "trial.axis_orientation", "trial.missing_field",
                                    "trial.ceiling_height", "trial.local_review"])
def test_notes_cannot_create_missing_trial_fields_or_borrow_them_from_failed_trials(target):
    trial = PassedTrial()
    trial.receipts.append({"status": "failed", "axis_orientation": {}, "missing_field": {}})
    before = copy.deepcopy(trial.receipts)
    submission = ReaderSubmission(role_id="plan_reader", image_name="plan.png", trial=trial)
    with pytest.raises(ValueError, match="not a declared plan object or verified trial field"):
        submission.submit({"trial_id": "latest", "local_review": REVIEW, "notes": [
            {"item": target, "kind": "assumption", "basis": "A note is not a reading."}]})
    assert submission.read() is None and trial.receipts == before


def test_trial_short_names_conflict_with_plan_fields_and_object_aliases():
    trial = PassedTrial()
    trial.receipt.update(basis="Report basis", P1={"diagnostic": "reported"})
    for short in ["basis", "P1"]:
        submission = ReaderSubmission(role_id="plan_reader", image_name="plan.png", trial=trial)
        with pytest.raises(ValueError, match="ambiguous"):
            submission.submit({"trial_id": "latest", "local_review": REVIEW, "notes": [
                {"item": short, "kind": "assumption", "basis": "Specify the target."}]})
        assert submission.read() is None
    submission = ReaderSubmission(role_id="plan_reader", image_name="plan.png", trial=trial)
    targets = ["plan.basis", "trial.basis", "plan.partitions:P1", "trial.P1"]
    assert submission.submit({"trial_id": "latest", "local_review": REVIEW, "notes": [
        {"item": target, "kind": "assumption", "basis": "Explicit target."} for target in targets]})["status"] == "accepted"
    assert [note["item"] for note in submission.read()["validation"]["notes"]] == targets
    # Explicit namespaces remain canonical even if an object ID has that text.
    note = {"item": "trial.basis", "kind": "assumption", "basis": "Report only."}
    assert _normalize_note_items([note], {"trial.basis", "plan.partitions:trial.basis"}) == [note]


def test_orientation_note_does_not_replace_required_north_arrow():
    trial = PassedTrial()
    trial.plan["x_anchors"] = [[10, 10], [110, 0]]
    trial.receipt["plan_sha256"] = canonical_plan_sha256(trial.plan)
    trial.receipt["axis_orientation"] = {"world_east_toward": "image_left", "world_north_toward": "image_top"}
    submission = ReaderSubmission(role_id="plan_reader", image_name="plan.png", trial=trial)
    with pytest.raises(ValueError, match="north_arrow"):
        submission.submit({"trial_id": "latest", "local_review": REVIEW, "notes": [
            {"item": "axis_orientation", "kind": "assumption", "basis": "A note cannot prove orientation."}]})
    assert submission.read() is None


def test_missing_optional_chain_note_does_not_create_dimension_evidence():
    trial = PassedTrial()
    original = copy.deepcopy(trial.plan)
    assert original["reading_type"] == "plan" and "dimension_chains" not in original
    submission = ReaderSubmission(role_id="plan_reader", image_name="plan.png", trial=trial)
    assert submission.submit({"local_review": REVIEW, "trial_id": "latest", "notes": [
        {"item": "dimension_chains", "kind": "unresolved", "basis": "No chain supplied."}]})["status"] == "accepted"
    saved = submission.read()
    assert saved["artifact"]["plan"] == original
    assert saved["validation"]["notes"][0]["item"] == "plan.dimension_chains"
    assert not any(row["item"] == "plan.dimension_chains" for row in saved["artifact"]["evidence"])


def test_latest_reference_survives_resume_and_rejects_stale_or_tampered_plan(tmp_path):
    workspace = tmp_path / "trial"
    (workspace / "images").mkdir(parents=True)
    Image.new("RGB", (120, 120)).save(workspace / "images/plan.png")
    digest = hashlib.sha256((workspace / "images/plan.png").read_bytes()).hexdigest()
    (workspace / "inputs.json").write_text(json.dumps({"images": {"plan.png": {
        "sha256": digest, "size": [120, 120]}}}), encoding="utf-8", newline="\n")

    def make_trial():
        return PlanTrial(Tools(workspace=workspace), image_name="plan.png", workspace=workspace,
                         receipt_directory=workspace / "trial_receipts")

    async def prepare():
        trial = make_trial()
        first = await trial.run(plan())
        second = await trial.run(operations=[operation(p2=[10, 41])])
        return first, second

    first, second = asyncio.run(prepare())
    resumed = make_trial()
    submission = ReaderSubmission(role_id="plan_reader", image_name="plan.png", trial=resumed,
                                  directory=tmp_path / "submission", target="F1")
    for params in ({"trial_id": first["trial_id"]}, {"plan_sha256": first["plan_sha256"]},
                   {"local_review": REVIEW, "trial_id": "latest", "plan_sha256": "0" * 64}):
        with pytest.raises(ValueError):
            submission.submit(params)
    assert submission.submit({"trial_id": second["trial_id"], "local_review": REVIEW})["status"] == "accepted"
    restored = ReaderSubmission(role_id="plan_reader", image_name="plan.png", trial=make_trial(),
                                 directory=tmp_path / "submission", target="F1")
    assert restored.read() == submission.read()
    assert restored.read()["validation"]["wall_reference"]["partitions"]["convention"] == "centerline"
    (workspace / second["compiled_numeric_plan_file"]).write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError):
        restored.read()


def test_notes_can_reference_dimension_evidence_consumed_by_numeric_resolution():
    trial = PassedTrial()
    numeric = copy.deepcopy(trial.plan)
    trial.plan["dimension_chains"] = [{"id": "observed", "axis": "x",
        "segments_mm": [5000, 5000], "tick_pixels": [10, 60, 110], "source_refs": ["plan.png"]}]
    trial.receipt["plan_sha256"] = canonical_plan_sha256(trial.plan)
    trial.numeric_plan = lambda receipt: copy.deepcopy(numeric)
    submission = ReaderSubmission(role_id="plan_reader", image_name="plan.png", trial=trial)
    result = submission.submit({"trial_id": "latest", "local_review": REVIEW, "notes": [
        {"item": "dimension_chains", "kind": "assumption", "basis": "Original dimensions retained in trial audit."}]})
    assert result["status"] == "accepted"
    saved = submission.read()
    assert saved["validation"]["notes"][0]["item"] == "plan.dimension_chains"
    assert saved["artifact"]["plan"] == numeric
    assert not any(row["item"] == "plan.dimension_chains" for row in saved["artifact"]["evidence"])


def test_full_and_operation_rework_allow_notes_but_preserve_unpointed_geometry(tmp_path):
    async def scenario():
        image = tmp_path / "plan.png"
        Image.new("RGB", (120, 120), "white").save(image)
        prior_workspace = prepare_workspace(tmp_path / "prior", image_bytes=image.read_bytes())
        current_workspace = prepare_workspace(tmp_path / "current", image_bytes=image.read_bytes())
        prior = PlanTrial(Tools(prior_workspace), image_name="plan.png", workspace=prior_workspace,
                          receipt_directory=prior_workspace / "trial_receipts")
        first = await prior.run(plan())
        assert first["status"] == "passed", first
        current = PlanTrial(Tools(current_workspace), image_name="plan.png", workspace=current_workspace,
                            receipt_directory=current_workspace / "trial_receipts")
        current.inherit_reference(prior, first["plan_sha256"], ["plan.openings:W1"])
        changed = plan()
        changed["openings"][0]["p2"] = [10, 41]
        changed["unresolved"] = ["recheck after correction"]
        changed["partitions"][0]["source_refs"] += ["plan.png: unchanged wall reviewed"]
        full = await current.run(changed)
        assert [row["item"] for row in full["changes"]] == ["plan.openings:W1"]
        assert {row["item"] for row in full["plan_revision"]["annotation_changes"]} == {
            "plan.unresolved", "plan.partitions:P1"}
        revised = await current.run(operations=[operation(p2=[10, 42]), {
            "op": "set", "field": "unresolved", "value": [], "reason": "resolved", "bbox": [0, 0, 110, 110],
            "source_refs": ["plan.png: rechecked"]}])
        assert revised["base_plan_sha256"] == full["plan_sha256"]
        assert current.load_plan(revised)["unresolved"] == []
        invalid = current.load_plan(revised)
        invalid["partitions"][0]["points"] = [[61, 10], [61, 110]]
        with pytest.raises(ValueError):
            await current.run(invalid)
        with pytest.raises(ValueError):
            await current.run(operations=[{**operation(), "collection": "space_seeds", "id": "left",
                                          "changes": {"role": "corridor"}}])
        assert current.baseline()[1] == revised["plan_sha256"]

    asyncio.run(scenario())
