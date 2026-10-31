import json

import pytest

from scripts.dev.bim_regression_report import report


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))


def run_fixture(root, name, *, positions=29, image="same", recovery=False, completed=True):
    run = root / name
    save(run / "summary.json", {"agent_response_completed": completed,
         "delivery": {"candidate": "candidate_01"}, "subscription_invocations": 1})
    save(run / "inputs.json", {"plan_regularization_rule": "plan_regularization_v1", "max_candidates": 24, "images": {"plan": {"sha256": image}}, "scope": "same",
         "provider": "test", "implementation_sha256": {"runner": "same"},
         "input_contents": {"saved_generated_proposal": {"included": recovery}}})
    save(run / "agent_request.json", {"effort": "medium", "system_prompt": "same"})
    save(run / "agent_receipt.json", {"actual_model": "test"})
    save(run / "postrun_audit.json", {"candidate": "candidate_01", "source_model_sha256": "bound",
         "strict_partition_status": "severe", "profile": {"successful_replies": 0}})
    save(run / "candidate_01/source_model.json", {"source_model_sha256": "bound",
         "floors": [{}], "spaces": [{}], "openings": [{}], "connections": []})
    save(run / "evaluation/original_openings.json", {"candidate": "candidate_01",
         "source_model_sha256": "bound", "reference_sha256": "same-ref", "reference_count": 29,
         "matched": 29, "positions": positions, "hosts": 29, "door_connections": 14})
    return {"case": "case", "condition": "one", "run": name}


def test_preserves_all_observations_and_worst_result(tmp_path):
    entries = [run_fixture(tmp_path, "a"), run_fixture(tmp_path, "b", positions=4)]
    data = report(tmp_path, {"runs": entries})
    assert len(data["runs"]) == 2
    assert data["groups"][0]["position_count_range"] == [4, 29]
    assert data["groups"][0]["stability"] == "not_established"
    assert data["runs"][0]["quality"]["strict_partition_status"] == "severe"
    assert data["runs"][0]["quality"]["space_identity_findings"] is None


@pytest.mark.parametrize("difference", [{"image": "different"}, {"recovery": True}])
def test_different_input_or_recovery_is_not_pooled(tmp_path, difference):
    entries = [run_fixture(tmp_path, "a"), run_fixture(tmp_path, "b", **difference)]
    group = report(tmp_path, {"runs": entries})["groups"][0]
    assert not group["comparable_saved_conditions"]
    assert group["position_count_range"] is None


def test_unfinished_and_interrupted_runs_are_not_dropped(tmp_path):
    entries = [run_fixture(tmp_path, "a", completed=False),
               {"case": "case", "condition": "one", "run": "missing"}]
    data = report(tmp_path, {"runs": entries})
    assert [r["status"] for r in data["runs"]] == ["interrupted", "unfinished_or_missing_summary"]
    assert data["runs"][1]["quality"] is None
    assert data["groups"][0]["sample_count"] == 2
    assert data["groups"][0]["position_count_range"] is None


def test_tool_completed_task_is_not_mislabeled_as_interrupted(tmp_path):
    entry = run_fixture(tmp_path, "tool-finished", completed=False)
    save(tmp_path / entry["run"] / "summary.json", {
        "task_completed": True, "agent_response_completed": False,
        "completion_origin": "domain_tool", "runtime_status": "completed"})
    assert report(tmp_path, {"runs": [entry]})["runs"][0]["status"] == "completed"


def test_unexercised_feature_is_distinct_from_output_quality(tmp_path):
    entry = run_fixture(tmp_path, "a")
    entry["feature_count"] = {"name": "new reply", "file": "postrun_audit.json",
                              "path": ["profile", "successful_replies"]}
    row = report(tmp_path, {"runs": [entry]})["runs"][0]
    assert row["feature_use"]["status"] == "unexercised"
    assert row["quality"]["openings"]["positions"] == 29


@pytest.mark.parametrize("filename,value", [
    ("postrun_audit.json", {"candidate": "candidate_02"}),
    ("postrun_audit.json", {"candidate": "candidate_01", "source_model_sha256": "stale"}),
    ("evaluation/original_openings.json", {"candidate": "candidate_02", "reference_count": 29}),
])
def test_stale_candidate_or_source_evidence_cannot_score(tmp_path, filename, value):
    entry = run_fixture(tmp_path, "a")
    save(tmp_path / "a" / filename, value)
    row = report(tmp_path, {"runs": [entry]})["runs"][0]
    assert row["evidence_issues"]
    assert row["quality"] is None or row["quality"]["openings"] is None


def test_missing_opening_evaluation_does_not_become_zero_or_pass(tmp_path):
    entry = run_fixture(tmp_path, "a")
    (tmp_path / "a/evaluation/original_openings.json").unlink()
    row = report(tmp_path, {"runs": [entry]})["runs"][0]
    assert row["quality"]["openings"] is None
    assert row["evidence_issues"]


def test_duplicate_run_is_not_independent_evidence(tmp_path):
    entry = run_fixture(tmp_path, "a")
    with pytest.raises(ValueError, match="same run"):
        report(tmp_path, {"runs": [entry, {**entry, "run": "./a"}]})


def test_experiment_override_is_part_of_comparability(tmp_path):
    entries = [run_fixture(tmp_path, "a"), run_fixture(tmp_path, "b")]
    save(tmp_path / "b/experiment_condition.json", {"variant": "different"})
    assert not report(tmp_path, {"runs": entries})["groups"][0]["comparable_saved_conditions"]


def test_empty_reference_is_unknown_instead_of_zero_over_zero(tmp_path):
    entry = run_fixture(tmp_path, "a")
    save(tmp_path / "a/evaluation/original_openings.json", {
        "reference_count": 0, "matched": 0, "positions": 0, "hosts": 0, "door_connections": 0})
    row = report(tmp_path, {"runs": [entry]})["runs"][0]
    assert row["quality"]["openings"] is None
    assert row["evidence_issues"]


def test_changed_evaluation_tolerance_is_not_pooled(tmp_path):
    entries = [run_fixture(tmp_path, "a"), run_fixture(tmp_path, "b")]
    path = tmp_path / "b/evaluation/original_openings.json"
    value = json.loads(path.read_text())
    save(path, {**value, "tolerance": {"along_m": 0.5}})
    assert not report(tmp_path, {"runs": entries})["groups"][0]["comparable_saved_conditions"]


def test_run_destination_and_record_commit_do_not_change_experiment_conditions(tmp_path):
    entries = [run_fixture(tmp_path, "a"), run_fixture(tmp_path, "b")]
    for name in ("a", "b"):
        save(tmp_path / name / "experiment_condition.json", {
            "variant": "same", "run": name, "producer_commit": name})
    assert report(tmp_path, {"runs": entries})["groups"][0]["comparable_saved_conditions"]
