import copy

import pytest

from src.agent.geometry_model.schema import GeometryInput, FootprintRing
from src.agent.geometry.opening_review import facade_inventory, opening_inventory, review_openings
from src.agent.geometry.source_bim import build_source_bim
from src.agent.geometry.source_model import _digest


def _source():
    geom = GeometryInput.model_validate({
        "footprint_x": [0, 4], "footprint_y": [0, 2],
        "floors": [{"name": "F1", "z_floor": 0, "ceiling_height": 3, "cells": [
            {"id": "A", "x": [0, 2], "y": [0, 2]},
            {"id": "B", "x": [2, 4], "y": [0, 2]},
        ]}],
        "windows": [{"id": "W1", "floor": "F1", "facade": "South", "span": [0.4, 1.2], "z": [1, 2], "room": "A"}],
        "openings": [
            {"id": "D1", "kind": "door", "space_id": "A", "other_space_id": "B", "p1": [2, 0.4], "p2": [2, 1.2], "z": [0, 2], "source_refs": ["test:door"]},
            {"id": "DOUT", "kind": "door", "space_id": "A", "other_space_id": None, "p1": [0, 0.4], "p2": [0, 1.2], "z": [0, 2], "source_refs": ["test:outside"]},
        ],
    })
    return build_source_bim(geom)


def _partly_shared_wall_source():
    """A's north wall contacts B on x=0..2 and is exposed on x=2..6."""
    geom = GeometryInput.model_validate({
        "schema_version": "2", "footprint_x": [0, 6], "footprint_y": [0, 4],
        "floors": [{"name": "F1", "z_floor": 0, "ceiling_height": 3, "cells": [
            {"id": "A", "x": [0, 6], "y": [0, 2]},
            {"id": "B", "x": [0, 2], "y": [2, 4]},
        ]}],
        "windows": [{"id": "NW", "floor": "F1", "facade": "North",
                     "span": [3, 5], "z": [1, 2], "room": "A"}],
        "openings": [{"id": "D", "kind": "door", "space_id": "A",
                      "other_space_id": "B", "p1": [0.5, 2], "p2": [1.5, 2],
                      "z": [0, 2], "source_refs": ["test:shared-door"]}],
    })
    geom.floors[0].footprint = FootprintRing.model_validate({
        "vertices": [[0, 0], [6, 0], [6, 2], [2, 2], [2, 4], [0, 4]],
    })
    return build_source_bim(geom, capability_profile="orthogonal_polygon")


def _partly_shared_height_source():
    """The full wall length contacts B below z=1.5, then faces outdoors."""
    geom = GeometryInput.model_validate({
        "schema_version": "2", "footprint_x": [0, 4], "footprint_y": [0, 4],
        "floors": [
            {"name": "TALL", "z_floor": 0, "ceiling_height": 3,
             "cells": [{"id": "A", "x": [0, 4], "y": [0, 2]}]},
            {"name": "SHORT", "z_floor": 0, "ceiling_height": 1.5,
             "cells": [{"id": "B", "x": [0, 4], "y": [2, 4]}]},
        ],
        "windows": [{"id": "HIGH", "floor": "TALL", "facade": "North",
                     "span": [1, 3], "z": [2, 2.8], "room": "A"}],
    })
    geom.floors[0].footprint = FootprintRing.model_validate({
        "vertices": [[0, 0], [4, 0], [4, 2], [0, 2]],
    })
    geom.floors[1].footprint = FootprintRing.model_validate({
        "vertices": [[0, 2], [4, 2], [4, 4], [0, 4]],
    })
    return build_source_bim(geom, capability_profile="orthogonal_polygon")


def _images():
    return {"plan.png": {"size": [100, 80], "sha256": "a" * 64}}


def _review(*, kind="door", coverage="complete", marks=None):
    return {"floor_id": "F1", "kind": kind, "image": "plan.png", "coverage": coverage,
            "marks": marks if marks is not None else [{
                "mark_id": "m1", "box": [1, 2, 10, 12], "opening_ids": ["D1"],
                "space_ids": ["A", "B"], "basis": "visible", "note": "internal door",
            }, {"mark_id": "m2", "box": [20, 2, 30, 12], "opening_ids": ["DOUT"],
                "space_ids": ["A"], "basis": "visible", "note": "outside door"}]}


def _facade_review(facade, *, kind="window", marks=None):
    return {"floor_id": "F1", "kind": kind, "facade": facade, "image": "plan.png",
            "coverage": "complete", "marks": marks if marks is not None else []}


def test_inventory_exposes_actual_openings_by_floor_and_space_without_mutating_source():
    source = _source()
    before = copy.deepcopy(source)
    inventory = opening_inventory(source)
    floor = inventory["floors"][0]
    assert floor["opening_ids"] == ["D1", "DOUT", "W1"]
    assert floor["counts"] == {"door": 2, "passage": 0, "window": 1}
    assert next(row for row in inventory["spaces"] if row["space_id"] == "A")["opening_ids"] == ["D1", "DOUT", "W1"]
    assert "openings" not in next(row for row in inventory["spaces"] if row["space_id"] == "A")
    assert next(row for row in floor["openings"] if row["id"] == "DOUT")["space_ids"] == ["A"]
    assert source == before


def test_complete_review_flags_extra_model_door_and_reports_per_room_missing_ids():
    report = review_openings(_source(), _review(marks=[_review()["marks"][0]]), _images())
    assert {row["code"] for row in report["findings"]} == {"unaccounted_model_opening"}
    assert report["room_coverage"] == [
        {"space_id": "A", "actual_opening_ids": ["D1", "DOUT"], "actual_opening_count": 2,
         "matched_opening_ids": ["D1"], "matched_opening_count": 1, "missing_opening_ids": ["DOUT"]},
        {"space_id": "B", "actual_opening_ids": ["D1"], "actual_opening_count": 1,
         "matched_opening_ids": ["D1"], "matched_opening_count": 1, "missing_opening_ids": []},
    ]
    assert report["drawing_fidelity"] == "not_evaluated"


def test_unmodeled_observation_and_multiple_or_reused_ids_are_findings():
    marks = [
    {"mark_id": "seen", "box": [1, 2, 10, 12], "opening_ids": [], "space_ids": ["A", "B"], "basis": "visible", "note": "door arc"},
        {"mark_id": "many", "box": [20, 2, 30, 12], "opening_ids": ["D1", "DOUT"], "space_ids": ["A", "B"], "basis": "visible", "note": "bad grouping"},
        {"mark_id": "again", "box": [40, 2, 50, 12], "opening_ids": ["D1"], "space_ids": ["A", "B"], "basis": "visible", "note": "duplicate"},
    ]
    codes = {row["code"] for row in review_openings(_source(), _review(marks=marks), _images())["findings"]}
    assert {"unmodeled_observed_mark", "mark_multiple_opening_ids", "opening_id_reused", "unaccounted_model_opening"} <= codes


def test_unmodeled_uncertain_mark_is_still_pending_and_inventory_names_unbuilt_scope():
    source = _source()
    source["unbuilt_openings"] = [{"id": "not-built"}]
    source["unsupported"] = [{"id": "ambiguous"}]
    source["source_model_sha256"] = _digest({key: value for key, value in source.items() if key != "source_model_sha256"})
    report = review_openings(source, _review(marks=[{
        "mark_id": "seen", "box": [1, 2, 10, 12], "opening_ids": [], "space_ids": ["A"],
        "basis": "uncertain", "note": "possible opening",
    }]), _images())
    assert {"unmodeled_observed_mark", "observation_pending", "unaccounted_model_opening"} <= {row["code"] for row in report["findings"]}
    inventory = opening_inventory(source)
    assert inventory["scope"]["unbuilt_opening_ids"] == ["not-built"]
    assert inventory["scope"]["unsupported_observation_count"] == 1


def test_wrong_connection_and_pending_basis_are_explicit_not_a_fidelity_pass():
    review = _review(marks=[{**_review()["marks"][0], "space_ids": ["A"], "basis": "inferred"}])
    report = review_openings(_source(), review, _images())
    assert {row["code"] for row in report["findings"]} >= {"mark_space_ids_mismatch", "observation_pending", "unaccounted_model_opening"}
    assert report["conclusion"] == "observations_require_follow_up"


def test_partial_review_never_claims_complete_coverage_or_consistency():
    review = _review(coverage="partial", marks=[_review()["marks"][0]])
    report = review_openings(_source(), review, _images())
    assert [row["code"] for row in report["findings"]] == ["partial_review_not_complete"]
    assert report["conclusion"] != "consistent_with_supplied_observations"


def test_facade_scope_uses_actual_exterior_host_direction_not_image_name():
    south = _facade_review("South", marks=[{
        "mark_id": "south-window", "box": [1, 2, 10, 12], "opening_ids": ["W1"],
        "space_ids": ["A"], "basis": "visible", "note": "south elevation window",
    }])
    report = review_openings(_source(), south, _images())

    assert report["model_opening_ids"] == ["W1"]
    assert report["matched_opening_ids"] == ["W1"]
    assert report["findings"] == []
    assert report["review_scope"]["facade"] == "South"
    assert report["facade_scope"]["exterior_boundary_ids"] == ["space/A/wall/0", "space/B/wall/0"]


def test_facade_scope_keeps_interior_and_unknown_host_openings_explicit():
    west = _facade_review("West", kind="door", marks=[{
        "mark_id": "west-door", "box": [1, 2, 10, 12], "opening_ids": ["DOUT"],
        "space_ids": ["A"], "basis": "visible", "note": "exterior door",
    }])
    report = review_openings(_source(), west, _images())
    assert report["model_opening_ids"] == ["DOUT"]
    assert report["facade_scope"]["excluded_openings"] == [{
        "opening_id": "D1", "kind": "door", "floor_id": "F1", "facade": None, "reason": "not_exterior",
    }]

    source = _source()
    next(row for row in source["openings"] if row["id"] == "DOUT")["host_boundary_id"] = "missing-wall"
    source["source_model_sha256"] = _digest({key: value for key, value in source.items() if key != "source_model_sha256"})
    unknown = review_openings(source, _facade_review("West", kind="door"), _images())
    assert unknown["model_opening_ids"] == []
    assert {row["reason"] for row in unknown["facade_scope"]["excluded_openings"]} == {
        "not_exterior", "unknown_host_boundary"}


def test_facade_mark_cannot_claim_an_interior_door():
    report = review_openings(_source(), _facade_review("West", kind="door", marks=[{
        "mark_id": "wrong-door", "box": [1, 2, 10, 12], "opening_ids": ["D1"],
        "space_ids": ["A", "B"], "basis": "visible", "note": "not on west facade",
    }]), _images())
    assert {row["code"] for row in report["findings"]} >= {
        "mark_facade_mismatch", "unaccounted_model_opening"}


def test_partial_contact_wall_keeps_exposed_window_and_height_facade_scope(tmp_path):
    from src.agent.execution.bim_height_coverage import height_coverage

    source = _partly_shared_wall_source()
    assert source["validation"]["status"] == "pass"
    before = copy.deepcopy(source)
    inventory = facade_inventory(source)
    assert inventory["opening_classifications"]["NW"]["facade"] == "North"
    assert inventory["opening_classifications"]["D"]["reason"] == "not_exterior"
    north = next(row for row in inventory["floors"][0]["facades"] if row["facade"] == "North")
    assert north["opening_ids"] == ["NW"]
    assert "space/A/wall/2" in north["exterior_boundary_ids"]
    report = review_openings(source, _facade_review("North", marks=[{
        "mark_id": "north-recess", "box": [1, 2, 10, 12], "opening_ids": ["NW"],
        "space_ids": ["A"], "basis": "visible", "note": "exposed part of wall",
    }]), _images())
    assert report["model_opening_ids"] == ["NW"]
    assert report["findings"] == []

    class EmptyClaimStore:
        run = tmp_path / "bim"

        def candidate(self, identity):
            return {"geometry": {"windows": [{"id": "NW"}], "openings": []}}, source

        def status(self):
            return {"claims": []}

    coverage = height_coverage(EmptyClaimStore(), "candidate",
                               current_state={"candidate": "candidate", "claims": []})
    north_coverage = next(row for row in coverage["floors"][0]["facades"] if row["facade"] == "North")
    assert north_coverage["actual_opening_ids"] == ["NW"]
    assert north_coverage["unchecked_height_opening_ids"] == ["NW"]
    assert source == before


def test_vertical_partial_contact_classifies_only_the_exposed_height():
    source = _partly_shared_height_source()
    assert source["validation"]["status"] == "pass"
    assert facade_inventory(source)["opening_classifications"]["HIGH"]["facade"] == "North"
    # A synthetic corrupted source may claim an aperture below the short
    # neighbour's roof is outdoors; the actual contact region must win.
    low = copy.deepcopy(next(row for row in source["openings"] if row["id"] == "HIGH"))
    low["id"] = "FALSE_LOW"
    for vertex in low["vertices"]:
        vertex[2] -= 2.0
    source["openings"].append(low)
    source["source_model_sha256"] = _digest({k: v for k, v in source.items()
                                              if k != "source_model_sha256"})
    classified = facade_inventory(source)["opening_classifications"]
    assert classified["HIGH"]["facade"] == "North"
    assert classified["FALSE_LOW"]["facade"] is None
    assert classified["FALSE_LOW"]["reason"] == "host_not_exterior"


def test_fully_shared_interior_wall_rejects_false_exterior_claim():
    source = _source()
    door = next(row for row in source["openings"] if row["id"] == "D1")
    door["exterior"] = True
    door["space_ids"] = ["A"]
    source["source_model_sha256"] = _digest({k: v for k, v in source.items()
                                              if k != "source_model_sha256"})
    classified = facade_inventory(source)["opening_classifications"]["D1"]
    assert classified["facade"] is None
    assert classified["reason"] == "host_not_exterior"


def test_missing_contact_regions_fail_closed_in_facade_inventory():
    source = _partly_shared_wall_source()
    host_id = next(row["host_boundary_id"] for row in source["openings"] if row["id"] == "NW")
    relation = next(row for row in source["boundary_relations"] if host_id in row["boundary_ids"])
    for incomplete in ("regions_missing", "relation_missing"):
        damaged = copy.deepcopy(source)
        if incomplete == "regions_missing":
            next(row for row in damaged["boundary_relations"]
                 if host_id in row["boundary_ids"])["regions"] = []
        else:
            damaged["boundary_relations"] = [row for row in damaged["boundary_relations"]
                                             if row["boundary_ids"] != relation["boundary_ids"]]
        damaged["source_model_sha256"] = _digest({k: v for k, v in damaged.items()
                                                   if k != "source_model_sha256"})
        inventory = facade_inventory(damaged)
        assert inventory["opening_classifications"]["NW"]["facade"] is None
        assert inventory["opening_classifications"]["NW"]["reason"] == "contact_geometry_unavailable"
        assert {row["boundary_id"] for row in inventory["floors"][0]["unsupported_exterior_boundaries"]} >= {host_id}


@pytest.mark.parametrize("mutate", [
    lambda review: review.update(image="missing.png"),
    lambda review: review["marks"][0].update(box=[1, 2, float("nan"), 12]),
    lambda review: review["marks"][1].update(mark_id="m1"),
    lambda review: review.update(extra=True),
])
def test_invalid_pixels_or_schema_are_rejected_and_source_is_unchanged(mutate):
    source = _source()
    before = copy.deepcopy(source)
    review = _review()
    mutate(review)
    with pytest.raises(ValueError, match="invalid opening review"):
        review_openings(source, review, _images())
    assert source == before


@pytest.mark.parametrize("pending", [False, True])
def test_optional_note_preserves_observations_and_all_review_findings(pending):
    source = _source()
    review = _review()
    if pending:
        review["marks"][0].update(basis="uncertain", space_ids=["A"])
    expected = review_openings(source, review, _images())
    for mark in review["marks"]:
        mark.pop("note")
    before_source, before_review = copy.deepcopy(source), copy.deepcopy(review)
    assert review_openings(source, review, _images()) == expected
    assert source == before_source and review == before_review
    with_empty_notes = copy.deepcopy(review)
    for mark in with_empty_notes["marks"]:
        mark["note"] = ""
    assert review_openings(source, with_empty_notes, _images()) == expected
    if pending:
        assert {row["code"] for row in expected["findings"]} >= {
            "mark_space_ids_mismatch", "observation_pending", "unaccounted_model_opening"}


@pytest.mark.parametrize("note", [None, 7, []])
def test_explicit_non_string_note_is_rejected_without_changing_observations(note):
    review = _review()
    review["marks"][1]["note"] = note
    before = copy.deepcopy(review)
    with pytest.raises(ValueError, match=r"marks\[1\].*mark_id='m2'.*note must be a string"):
        review_openings(_source(), review, _images())
    assert review == before


@pytest.mark.parametrize("missing,extra", [(["basis"], []), ([], ["evidence"]),
                                           (["mark_id", "space_ids"], ["notes"])])
def test_mark_field_error_locates_exact_missing_and_extra_keys(missing, extra):
    from scripts.tool_scripts.bim_agent_feedback import repair_hint

    review = _review()
    mark = review["marks"][1]
    for key in missing:
        mark.pop(key)
    for key in extra:
        mark[key] = "unsupported"
    with pytest.raises(ValueError) as rejected:
        review_openings(_source(), review, _images())
    error = str(rejected.value)
    assert f"marks[1] (mark_id={mark.get('mark_id')!r})" in error
    assert f"missing fields {missing}" in error
    assert f"unsupported fields {extra}" in error
    hint = repair_hint(None, "check_openings", {}, error)
    assert 'topic="opening_review"' in hint and "plan_partition" not in hint


def test_duplicate_box_and_unknown_floor_are_findings_not_rejections():
    duplicate = _review()
    duplicate["marks"][1]["box"] = [1, 2, 10, 12]
    assert "duplicate_mark_box" in {row["code"] for row in review_openings(_source(), duplicate, _images())["findings"]}
    unknown = _review()
    unknown["floor_id"] = "F404"
    assert "unknown_floor" in {row["code"] for row in review_openings(_source(), unknown, _images())["findings"]}


def test_same_level_wing_connection_is_listed_on_both_sides_and_counted_once():
    from src.agent.geometry.opening_review import facade_inventory
    from src.agent.geometry.bim_delivery import summarize_delivery
    source=_source()
    # Keep the exact built doorway and room geometry, but give the adjoining
    # building wing its own floor/volume identity at the same elevation.
    floor=copy.deepcopy(source['floors'][0]);floor['id']='ANNEX'
    source['floors'].append(floor)
    next(s for s in source['spaces'] if s['id']=='B')['floor_id']='ANNEX'
    source['source_model_sha256']=_digest({k:v for k,v in source.items() if k!='source_model_sha256'})
    before=copy.deepcopy(source)
    inventory=opening_inventory(source)
    assert inventory['opening_count']==3
    assert inventory['counts']['door']==2
    floors={f['floor_id']:f for f in inventory['floors']}
    assert floors['ANNEX']['opening_ids']==['D1']
    assert floors['F1']['opening_ids']==['D1','DOUT','W1']
    assert floors['ANNEX']['openings'][0]['floor_ids']==['ANNEX','F1']
    assert next(f for f in facade_inventory(source)['floors'] if f['floor_id']=='ANNEX')['non_facade_openings'][0]['opening_id']=='D1'
    review=_review(marks=_review()['marks'][:1]);review['floor_id']='ANNEX'
    result=review_openings(source,review,images=_images())
    assert result['conclusion']=='consistent_with_supplied_observations'
    delivery=summarize_delivery(source,[])
    assert delivery['counts']['openings']==3
    assert source==before


def test_continuous_core_connection_is_scoped_without_accepting_wrong_height():
    source=_source()
    floor=copy.deepcopy(source['floors'][0]);floor.update(id='CORE',z_floor=-3,ceiling_height=9)
    source['floors'].append(floor)
    core=next(s for s in source['spaces'] if s['id']=='B')
    core.update(floor_id='CORE',z_floor=-3,height=9)
    def rehash():source['source_model_sha256']=_digest({k:v for k,v in source.items() if k!='source_model_sha256'})
    rehash()
    assert opening_inventory(source)['opening_count']==3
    core['z_floor']=3;rehash()
    with pytest.raises(ValueError,match='outside a connected space'):
        opening_inventory(source)
