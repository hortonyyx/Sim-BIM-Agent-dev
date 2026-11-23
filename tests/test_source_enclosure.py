import copy

import pytest

from src.agent.geometry.source_bim import build_source_bim
from src.agent.geometry.source_enclosure import apply_source_enclosure, ReciprocalEnclosureError
from src.agent.geometry.source_model import _digest
from tests.test_source_bim import three_rooms


def _source():
    return build_source_bim(three_rooms(), capability_profile="orthogonal_polygon")


def _declaration(source, *, spaces=None, boundaries=None):
    return {
        "schema_version": "source_enclosure_input_v1",
        "base_source_model_sha256": source["source_model_sha256"],
        "spaces": spaces or [],
        "boundaries": boundaries or [],
    }


def _evidence(**extra):
    return {"source_refs": ["manual:sheet-A"], "assumptions": [],
            "evidence_kind": "manual_annotation", **extra}


def test_whole_open_exterior_is_virtual_and_keeps_logical_parent():
    source = _source()
    boundary = next(b for b in source["boundaries"]
                    if b["space_id"] == "a" and b["geometry_type"] == "wall"
                    and not b["adjacent_space_ids"])
    declaration = _declaration(
        source,
        spaces=[{"space_id": "a", "enclosure": "semi_open", **_evidence()}],
        boundaries=[{"boundary_id": boundary["id"], "condition": "open", "scope": "whole", **_evidence()}],
    )

    result = apply_source_enclosure(source, declaration)

    changed = next(b for b in result["boundaries"] if b["id"] == boundary["id"])
    assert source["schema_version"] == "source_bim_v2"
    assert changed["kind"] == "virtual"
    assert changed["enclosure"] == "open"
    assert changed["vertices"] == boundary["vertices"]
    assert changed["enclosure_regions"][0]["vertices"] == boundary["vertices"]
    by_space = {space["id"]: space for space in result["spaces"]}
    assert by_space["a"]["enclosure"] == "semi_open"
    assert "enclosure" not in by_space["b"]
    assert result["source_enclosure"]["base_source_model_sha256"] == source["source_model_sha256"]
    assert result["source_enclosure"]["declaration_sha256"]
    assert result["schema_version"] == "source_bim_v3"
    assert result["source_model_sha256"] != source["source_model_sha256"]
    assert result["connections"] == source["connections"]
    exterior_connections = result["source_enclosure"]["open_connections"]
    assert len(exterior_connections) == 1
    exterior = exterior_connections[0]
    assert {key: exterior[key] for key in ("boundary_id", "space_ids", "exterior", "condition", "holes")} == {
        "boundary_id": boundary["id"], "space_ids": ["a"], "exterior": True,
        "condition": "open", "holes": [],
    }
    assert {tuple(point) for point in exterior["vertices"]} == {tuple(point) for point in boundary["vertices"]}


def test_partial_open_keeps_physical_parent_and_uses_3d_region():
    source = _source()
    boundary = next(b for b in source["boundaries"]
                    if b["space_id"] == "a" and b["geometry_type"] == "wall"
                    and not b["adjacent_space_ids"])
    a, b, c, d = boundary["vertices"]
    region = [a, b, [b[0], b[1], (b[2] + c[2]) / 2], [a[0], a[1], (a[2] + d[2]) / 2]]
    result = apply_source_enclosure(source, _declaration(
        source, boundaries=[{"boundary_id": boundary["id"], "condition": "open", "scope": "partial",
                             "vertices": region, **_evidence(evidence_kind="example")}],
    ))

    changed = next(b for b in result["boundaries"] if b["id"] == boundary["id"])
    assert changed["kind"] == "physical"
    assert changed["enclosure"] == "mixed"
    assert changed["vertices"] == boundary["vertices"]
    assert changed["enclosure_regions"] == [{"condition": "open", "vertices": region,
                                               "source_refs": ["manual:sheet-A"], "assumptions": [],
                                               "evidence_kind": "example"}]


def test_shared_open_requires_matching_other_side_declaration():
    source = _source()
    relation = source["boundary_relations"][0]
    first, second = relation["boundary_ids"]
    declaration = _declaration(source, boundaries=[
        {"boundary_id": first, "condition": "open", "scope": "whole", **_evidence()},
    ])
    with pytest.raises(ValueError, match="matching open declaration"):
        apply_source_enclosure(source, declaration)

    # The full shared wall needs no room for a window/door in this fixture and
    # becomes virtual only when both source sides make the same assertion.
    declaration["boundaries"].append(
        {"boundary_id": second, "condition": "open", "scope": "whole", **_evidence()}
    )
    result = apply_source_enclosure(source, declaration)
    rows = {b["id"]: b for b in result["boundaries"]}
    assert rows[first]["kind"] == rows[second]["kind"] == "virtual"
    shared = result["source_enclosure"]["open_connections"]
    assert len(shared) == 2
    assert all(row["condition"] == "open" and not row["exterior"]
               and set(row["space_ids"]) == {"a", "b"} for row in shared)
    assert all(len(row["vertices"]) == 4 for row in shared)


def test_conflicting_partial_declarations_report_one_pair_with_both_edit_locations():
    source = _source()
    a, b = source['boundary_relations'][0]['boundary_ids']
    rows = [dict(boundary_id=bid, condition='open', scope='partial', **_evidence(),
                 vertices=[[3,3,z], [6,3,z], [6,3,z+1], [3,3,z+1]])
            for bid, z in ((a, 0), (b, 2))]
    with pytest.raises(ReciprocalEnclosureError) as caught:
        apply_source_enclosure(source, _declaration(source, boundaries=rows))
    assert caught.value.base_sha256 == source['source_model_sha256']
    assert caught.value.conflicts == [dict(boundary_id=a, declarations=['/enclosure_declaration/boundaries/0'],
        counterpart_id=b, counterpart_declarations=['/enclosure_declaration/boundaries/1'])]


def test_shared_partial_open_ring_preserves_its_physical_inner_hole():
    source = _source()
    first, second = source["boundary_relations"][0]["boundary_ids"]
    # These four non-overlapping regions share edges and form an open ring on
    # the a↔b wall.  The central 1 m² remains physical enclosure.
    ring = [
        [[3, 3, 0], [6, 3, 0], [6, 3, 1], [3, 3, 1]],
        [[3, 3, 1], [4, 3, 1], [4, 3, 2], [3, 3, 2]],
        [[5, 3, 1], [6, 3, 1], [6, 3, 2], [5, 3, 2]],
        [[3, 3, 2], [6, 3, 2], [6, 3, 3], [3, 3, 3]],
    ]
    rows = [
        {"boundary_id": boundary_id, "condition": "open", "scope": "partial",
         "vertices": vertices, **_evidence()}
        for boundary_id in (first, second) for vertices in ring
    ]

    result = apply_source_enclosure(source, _declaration(source, boundaries=rows))

    connections = result["source_enclosure"]["open_connections"]
    assert len(connections) == 2
    assert all(not row["exterior"] and set(row["space_ids"]) == {"a", "b"}
               for row in connections)
    assert all(len(row["holes"]) == 1 for row in connections)
    assert all({tuple(point) for point in row["holes"][0]} == {
        (4.0, 3.0, 1.0), (5.0, 3.0, 1.0), (5.0, 3.0, 2.0), (4.0, 3.0, 2.0),
    } for row in connections)


def test_unknown_is_visible_in_schema_and_preserves_base_severe_findings():
    source = _source()
    source["validation"]["findings"].append({"code": "source.preexisting", "severity": "severe"})
    source["validation"]["status"] = "severe"
    # This deliberately makes a well-formed frozen source so the test covers
    # preservation rather than integrity rejection.
    source["source_model_sha256"] = _digest({k: v for k, v in source.items() if k != "source_model_sha256"})
    boundary = next(b for b in source["boundaries"] if b["geometry_type"] == "wall")
    result = apply_source_enclosure(source, _declaration(
        source, boundaries=[{"boundary_id": boundary["id"], "condition": "unknown", "scope": "whole",
                             **_evidence(evidence_kind="observed")}],
    ))
    changed = next(b for b in result["boundaries"] if b["id"] == boundary["id"])
    assert changed["kind"] == "unknown"
    assert changed["enclosure"] == "unknown"
    assert result["validation"]["status"] == "severe"
    assert {row["code"] for row in result["validation"]["findings"]} >= {
        "source.preexisting", "source.enclosure_unknown"
    }


def test_rejects_stale_digest_and_opening_overlap():
    source = _source()
    stale = _declaration(source, spaces=[{"space_id": "a", "enclosure": "open", **_evidence()}])
    stale["base_source_model_sha256"] = "0" * 64
    with pytest.raises(ValueError, match="base_source_model_sha256"):
        apply_source_enclosure(source, stale)

    window_source = copy.deepcopy(source)
    exterior = next(b for b in window_source["boundaries"] if b["geometry_type"] == "wall" and not b["adjacent_space_ids"])
    window = {"id": "window-test", "kind": "window", "host_boundary_id": exterior["id"],
              "space_ids": [exterior["space_id"]], "exterior": True,
              "vertices": exterior["vertices"], "connectivity": "unknown", "source_refs": [], "assumptions": []}
    window_source["openings"].append(window)
    window_source["opening_hosts"]["window-test"] = [exterior["id"]]
    window_source["source_model_sha256"] = _digest(
        {k: v for k, v in window_source.items() if k != "source_model_sha256"}
    )
    with pytest.raises(ValueError, match="overlaps"):
        apply_source_enclosure(window_source, _declaration(
            window_source, boundaries=[{"boundary_id": exterior["id"], "condition": "open", "scope": "whole", **_evidence()}],
        ))

def test_rejects_non_coplanar_outside_and_overlapping_partial_regions():
    source = _source()
    boundary = next(b for b in source["boundaries"] if b["id"] == "space/a/wall/0")
    a, b, c, d = boundary["vertices"]

    non_coplanar = [a, [b[0], b[1] + 0.01, b[2]], c, d]
    with pytest.raises(ValueError, match="planar polygon on its source wall"):
        apply_source_enclosure(source, _declaration(
            source, boundaries=[{"boundary_id": boundary["id"], "condition": "open", "scope": "partial",
                                 "vertices": non_coplanar, **_evidence()}],
        ))

    outside = [[a[0] - 0.1, a[1], a[2]], [b[0], b[1], b[2]],
               [b[0], b[1], b[2] + 1], [a[0] - 0.1, a[1], a[2] + 1]]
    with pytest.raises(ValueError, match="outside"):
        apply_source_enclosure(source, _declaration(
            source, boundaries=[{"boundary_id": boundary["id"], "condition": "open", "scope": "partial",
                                 "vertices": outside, **_evidence()}],
        ))

    left = [[a[0], a[1], a[2]], [a[0] + 2, a[1], a[2]],
            [a[0] + 2, a[1], a[2] + 1], [a[0], a[1], a[2] + 1]]
    right = [[a[0] + 1, a[1], a[2]], [b[0], b[1], b[2]],
             [b[0], b[1], b[2] + 1], [a[0] + 1, a[1], a[2] + 1]]
    with pytest.raises(ValueError, match="regions overlap"):
        apply_source_enclosure(source, _declaration(
            source, boundaries=[
                {"boundary_id": boundary["id"], "condition": "open", "scope": "partial",
                 "vertices": left, **_evidence()},
                {"boundary_id": boundary["id"], "condition": "unknown", "scope": "partial",
                 "vertices": right, **_evidence()},
            ],
        ))
