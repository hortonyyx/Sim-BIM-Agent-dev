"""A referenced multi-storey core appears on each storey, with only its local doors."""
from copy import deepcopy

from PIL import Image

from src.agent.geometry.source_image_overlay import render_source_overlay
from src.agent.geometry.source_model import _digest
from src.agent.geometry.source_plan_view import render_source_plan
from src.agent.geometry.source_space_relations import review_space_relations


def _complete_source(source):
    """Give the synthetic spatial fixture explicit footprints and opening hosts."""
    from shapely.geometry import LineString

    source["boundaries"] = []
    for floor in source["floors"]:
        spaces = [row for row in source["spaces"] if row["floor_id"] == floor["id"]]
        points = [point for space in spaces for point in space["polygon"]]
        xs, ys = [p[0] for p in points], [p[1] for p in points]
        floor["footprint"] = [[min(xs), min(ys)], [max(xs), min(ys)],
                              [max(xs), max(ys)], [min(xs), max(ys)]]
    for space in source["spaces"]:
        ring = space["polygon"]
        lo, hi = space["z_floor"], space["z_floor"] + space["height"]
        for i, (a, b) in enumerate(zip(ring, ring[1:] + ring[:1])):
            source["boundaries"].append({"id": f"{space['id']}/wall/{i}",
                "space_id": space["id"], "geometry_type": "wall", "kind": "physical",
                "vertices": [[*a, lo], [*b, lo], [*b, hi], [*a, hi]]})
    source["opening_hosts"] = {
        opening["id"]: [row["id"] for row in source["boundaries"]
            if row["space_id"] in opening["space_ids"]
            and LineString([v[:2] for v in row["vertices"][:2]]).covers(
                LineString([v[:2] for v in opening["vertices"][:2]]))]
        for opening in source["openings"]}
    return source


def _source(*, f2_references_core=True):
    floors = [
        {"id": "F1", "z_floor": 0, "height": 3, "spanning_space_ids": ["core"]},
        {"id": "F2", "z_floor": 3, "height": 3,
         **({"spanning_space_ids": ["core"]} if f2_references_core else {})},
        {"id": "CORE", "z_floor": 0, "height": 6},
    ]
    spaces = [
        {"id": "f1_room", "floor_id": "F1", "z_floor": 0, "height": 3,
         "polygon": [[2, 0], [10, 0], [10, 10], [2, 10]]},
        {"id": "f2_room", "floor_id": "F2", "z_floor": 3, "height": 3,
         "polygon": [[2, 0], [10, 0], [10, 10], [2, 10]]},
        {"id": "core", "floor_id": "CORE", "z_floor": 0, "height": 6,
         "polygon": [[0, 0], [2, 0], [2, 10], [0, 10]]},
    ]
    openings = [
        {"id": "f1_core_door", "kind": "door", "space_ids": ["f1_room", "core"],
         "vertices": [[2, 4, 0.5], [2, 6, 0.5], [2, 6, 2.5], [2, 4, 2.5]]},
        {"id": "f2_core_door", "kind": "door", "space_ids": ["f2_room", "core"],
         "vertices": [[2, 4, 3.5], [2, 6, 3.5], [2, 6, 5.5], [2, 4, 5.5]]},
        {"id": "f1_core_window", "kind": "window", "space_ids": ["core"],
         "vertices": [[0, 1, 1], [0, 3, 1], [0, 3, 2], [0, 1, 2]]},
        {"id": "f2_core_window", "kind": "window", "space_ids": ["core"],
         "vertices": [[0, 1, 4], [0, 3, 4], [0, 3, 5], [0, 1, 5]]},
    ]
    source = {"floors": floors, "spaces": spaces, "openings": openings,
              "connections": [
                  {"opening_id": row["id"], "kind": "door", "space_ids": row["space_ids"]}
                  for row in openings if row["kind"] == "door"]}
    _complete_source(source)
    source["source_model_sha256"] = _digest(source)
    return source


def _review(source, floor_id, points, expected):
    return review_space_relations(source, floor_id=floor_id, image_size=[121, 121],
        x_anchors=[[0, 0], [100, 10]], y_anchors=[[100, 0], [0, 10]],
        observations=[{"id": "pair", "points": points, "expected": expected,
                       "evidence": "Observed source-floor relationship for a synthetic plan."}])["observations"][0]


def _overlay(source, floor_id):
    _, metadata = render_source_overlay(source, Image.new("RGB", (121, 121), "white"),
        floor_id=floor_id, x_anchors=[[0, 0], [100, 10]], y_anchors=[[100, 0], [0, 10]],
        basis="synthetic common floor frame")
    return metadata


def test_each_storey_sees_same_core_and_only_its_height_local_openings():
    source = _source()
    before = deepcopy(source)
    for floor_id, room_id, door_id, window_id in [
        ("F1", "f1_room", "f1_core_door", "f1_core_window"),
        ("F2", "f2_room", "f2_core_door", "f2_core_window"),
    ]:
        same = _review(source, floor_id, [[10, 20], [10, 80]], "same_space")
        assert same["actual_relation"] == "same_space"
        assert [row["space_id"] for row in same["points"]] == ["core", "core"]
        across = _review(source, floor_id, [[10, 50], [70, 50]], "separate_spaces")
        assert [row["space_id"] for row in across["points"]] == ["core", room_id]
        assert [row["opening_id"] for row in across["direct_connections"]] == [door_id]
        _, plan = render_source_plan(source, floor_id)
        overlay = _overlay(source, floor_id)
        assert set(plan["space_ids"]) == {room_id, "core"}
        assert set(plan["opening_ids"]) == {door_id, window_id}
        assert {row["id"] for row in overlay["projected_spaces"]} == {room_id, "core"}
        assert {row["id"] for row in overlay["projected_openings"]} == {door_id, window_id}
    assert source == before


def test_unreferenced_core_is_not_silently_added_to_a_storey():
    source = _source(f2_references_core=False)
    core_point = _review(source, "F2", [[10, 50], [70, 50]], "separate_spaces")
    assert core_point["actual_relation"] == "indeterminate"
    assert core_point["points"][0]["status"] == "outside_modelled_spaces"
    assert core_point["points"][1]["space_id"] == "f2_room"
    _, plan = render_source_plan(source, "F2")
    overlay = _overlay(source, "F2")
    assert plan["space_ids"] == ["f2_room"]
    # The room's physical door still belongs on its plan, even when the core
    # is absent from that storey's declared space inventory.
    assert plan["opening_ids"] == ["f2_core_door"]
    assert [row["id"] for row in overlay["projected_spaces"]] == ["f2_room"]
    assert [row["id"] for row in overlay["projected_openings"]] == ["f2_core_door"]


def test_same_level_annex_connection_remains_visible_on_both_floor_group_plans():
    source = _source(f2_references_core=False)
    source["floors"] = [
        {"id": "MAIN", "z_floor": 0, "height": 3},
        {"id": "ANNEX", "z_floor": 0, "height": 3},
    ]
    source["spaces"] = [
        {"id": "main", "floor_id": "MAIN", "z_floor": 0, "height": 3,
         "polygon": [[0, 0], [2, 0], [2, 10], [0, 10]]},
        {"id": "annex", "floor_id": "ANNEX", "z_floor": 0, "height": 3,
         "polygon": [[2, 0], [10, 0], [10, 10], [2, 10]]},
    ]
    source["openings"] = [
        {"id": "annex_door", "kind": "door", "space_ids": ["main", "annex"],
         "vertices": [[2, 4, 0.5], [2, 6, 0.5], [2, 6, 2.5], [2, 4, 2.5]]},
    ]
    source["connections"] = [
        {"opening_id": "annex_door", "kind": "door", "space_ids": ["main", "annex"]},
    ]
    _complete_source(source)
    source["source_model_sha256"] = _digest({key: value for key, value in source.items()
                                               if key != "source_model_sha256"})
    for floor_id, space_id in [("MAIN", "main"), ("ANNEX", "annex")]:
        _, plan = render_source_plan(source, floor_id)
        overlay = _overlay(source, floor_id)
        assert plan["space_ids"] == [space_id]
        assert plan["opening_ids"] == ["annex_door"]
        assert [row["id"] for row in overlay["projected_spaces"]] == [space_id]
        assert [row["id"] for row in overlay["projected_openings"]] == ["annex_door"]


def test_relation_connections_use_the_requested_storeys_opening_height():
    source = _source()
    source["floors"][0]["spanning_space_ids"].append("other_core")
    source["floors"][1]["spanning_space_ids"].append("other_core")
    source["floors"].append({"id": "OTHER_CORE", "z_floor": 0, "height": 6})
    source["spaces"].append({"id": "other_core", "floor_id": "OTHER_CORE",
        "z_floor": 0, "height": 6, "polygon": [[2, 0], [10, 0], [10, 10], [2, 10]]})
    # Remove the ordinary rooms so both sides of these doors span both storeys.
    source["spaces"] = [row for row in source["spaces"] if row["id"] not in {"f1_room", "f2_room"}]
    for floor in source["floors"][:2]:
        floor["spanning_space_ids"] = ["core", "other_core"]
    source["openings"] = [
        {"id": f"level_{level}_door", "kind": "door", "space_ids": ["core", "other_core"],
         "vertices": [[2, 4, base + 0.5], [2, 6, base + 0.5],
                      [2, 6, base + 2.5], [2, 4, base + 2.5]]}
        for level, base in [(1, 0), (2, 3)]
    ]
    source["connections"] = [
        {"opening_id": row["id"], "kind": "door", "space_ids": row["space_ids"]}
        for row in source["openings"]]
    source["source_model_sha256"] = _digest({key: value for key, value in source.items()
                                             if key != "source_model_sha256"})
    for floor_id, expected_door in [("F1", "level_1_door"), ("F2", "level_2_door")]:
        relation = _review(source, floor_id, [[10, 50], [70, 50]], "separate_spaces")
        assert [row["opening_id"] for row in relation["direct_connections"]] == [expected_door]
