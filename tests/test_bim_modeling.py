"""Construction properties: preserve intent and reject accidental degradation."""
import copy
import math

import pytest
from shapely.affinity import rotate, translate
from shapely.geometry import LineString, Polygon, box

from src.agent.modeling import clearance_report, region, shared_edges, shared_opening


def test_region_combines_explicit_parts_and_keeps_real_holes_without_mutation():
    original = {"polygon": [[0, 0], [4, 0], [4, 4], [0, 4]], "holes": []}
    before = copy.deepcopy(original)
    result = region(original, box(4, 0, 6, 2), subtract=[box(1, 1, 2, 2)])
    polygon = Polygon(result["polygon"], result["holes"])
    assert polygon.area == 19
    assert len(polygon.interiors) == 1
    assert original == before


def test_region_does_not_discard_components_fill_gaps_or_repair_bowties():
    with pytest.raises(ValueError, match="no components are discarded"):
        region(box(0, 0, 1, 1), box(1.000001, 0, 2, 1))
    with pytest.raises(ValueError, match="no components are discarded"):
        region(box(0, 0, 3, 1), subtract=[box(1, -1, 2, 2)])
    with pytest.raises(ValueError, match="no repair"):
        region(Polygon([(0, 0), (2, 2), (0, 2), (2, 0)]))
    with pytest.raises(ValueError, match="no components"):
        region(box(0, 0, 1, 1), subtract=[box(-1, -1, 2, 2)])


def test_region_rejects_touching_hole_that_source_format_cannot_accept():
    touching = Polygon([(0, 0), (4, 0), (4, 4), (0, 4)],
                       [[(0, 2), (1, 1), (1, 3)]])
    assert touching.is_valid
    with pytest.raises(ValueError, match="strictly inside"):
        region(touching)


def test_shared_edges_join_collinear_fragments_and_ignore_vertex_order():
    left = Polygon([(0, 0), (2, 0), (2, 1), (2, 3), (2, 4), (0, 4)])
    right = Polygon([(2, 0), (4, 0), (4, 4), (2, 4), (2, 2)])
    expected = [{"p1": [2, 0], "p2": [2, 4], "length_m": 4}]
    assert shared_edges(left, right) == expected
    assert shared_edges(Polygon(list(left.exterior.coords)[::-1]),
                        Polygon(list(right.exterior.coords)[::-1])) == expected
    assert shared_edges(right, left) == expected


@pytest.mark.parametrize("angle,origin", [(0, (0, 0)), (29, (100, -20)), (-47, (-5, 8)), (90, (0, 0))])
def test_rotated_shared_edge_and_requested_width_are_preserved(angle, origin):
    def transformed(p):
        return translate(rotate(p, angle, origin=(0, 0)), *origin)
    left, right = transformed(box(0, 0, 2, 4)), transformed(box(2, 0, 4, 4))
    before = left.wkb, right.wkb
    edges = shared_edges(left, right)
    assert len(edges) == 1
    assert edges[0]["length_m"] == pytest.approx(4)
    aperture = shared_opening(left, right, edge=0, offset=.4, width=1.1)
    assert math.dist(aperture["p1"], aperture["p2"]) == pytest.approx(1.1)
    aperture_line = LineString([aperture["p1"], aperture["p2"]])
    # Tolerance only checks the outcome here; the helper never buffers inputs.
    assert left.boundary.buffer(1e-10).covers(aperture_line)
    assert right.boundary.buffer(1e-10).covers(aperture_line)
    assert (left.wkb, right.wkb) == before
    assert shared_edges(Polygon(list(left.exterior.coords)[::-1]), right) == edges


def test_shared_edges_keep_turns_and_disconnected_contacts_separate():
    room = box(1, 1, 3, 3)
    hall = box(0, 0, 4, 4).difference(room)
    edges = shared_edges(room, hall)
    assert len(edges) == 4
    assert all(x["length_m"] == 2 for x in edges)
    assert shared_edges(room, hall) == shared_edges(hall, room)
    for i in range(4):
        assert math.dist(*shared_opening(room, hall, edge=i, offset=.25, width=1).values()) == pytest.approx(1)


def test_shared_edges_do_not_weld_nearby_regions_or_select_overlapping_interiors():
    assert shared_edges(box(0, 0, 2, 2), box(2.000000001, 0, 4, 2)) == []
    assert shared_edges(box(0, 0, 2, 2), box(2, 2, 4, 4)) == []
    with pytest.raises(ValueError, match="interiors overlap"):
        shared_edges(box(0, 0, 2, 2), box(1.999, 0, 4, 2))


@pytest.mark.parametrize("angle", [17, 29, -47])
def test_independently_rotated_fragments_never_gain_an_unshared_straight_edge(angle):
    left = rotate(Polygon([(0, 0), (2, 0), (2, 1), (2, 3), (2, 4), (0, 4)]), angle, origin=(0, 0))
    right = rotate(Polygon([(2, 0), (4, 0), (4, 4), (2, 4), (2, 2)]), angle, origin=(0, 0))
    # Independent subdivision/rotation can create floating-point overlap. The
    # supported outcomes are explicit rejection or actual shared segments, never
    # silently replacing the shapes with an intended-but-different full wall.
    before = left.wkb, right.wkb
    try:
        edges = shared_edges(left, right)
    except ValueError as error:
        assert "overlap" in str(error)
        assert left.intersection(right).area > 0
    else:
        for edge in edges:
            line = LineString([edge["p1"], edge["p2"]])
            assert left.boundary.covers(line)
            assert right.boundary.covers(line)
    assert (left.wkb, right.wkb) == before


def test_opening_never_shrinks_requested_width_to_fit_short_shared_edge():
    room, hall = box(0, 0, 2, 2), box(2, 1.73, 4, 3)
    assert shared_edges(room, hall)[0]["length_m"] == pytest.approx(.27)
    with pytest.raises(ValueError, match="available_length_m=.*requested_width_m=1.0; no shrinking"):
        shared_opening(room, hall, edge=0, offset=.05, width=1.0)
    # An intentionally small service opening is permitted when explicitly sized.
    opening = shared_opening(room, hall, edge=0, offset=.05, width=.17)
    assert math.dist(opening["p1"], opening["p2"]) == pytest.approx(.17)


@pytest.mark.parametrize("edge", [-1, 1, True, .5])
def test_opening_requires_an_actual_selected_edge(edge):
    with pytest.raises(ValueError, match="explicit shared edge"):
        shared_opening(box(0, 0, 2, 2), box(2, 0, 4, 2), edge=edge, offset=.1, width=1)


@pytest.mark.parametrize("width", [0, -1, True, float("nan"), float("inf")])
def test_clearance_requires_explicit_finite_positive_width(width):
    with pytest.raises(ValueError, match="explicit finite positive"):
        clearance_report(box(0, 0, 4, 4), width=width)


@pytest.mark.parametrize("angle", [0, 29, -47])
def test_clearance_exposes_thin_connection_without_claiming_door_reachability(angle):
    hall = box(0, 0, 3, 3).union(box(3, 1.4, 5, 1.6)).union(box(5, 0, 8, 3))
    hall = rotate(hall, angle, origin=(0, 0))
    before = hall.wkb
    narrow = clearance_report(hall, width=.8, include_geometry=True)
    assert narrow["component_count"] == 2
    assert len(narrow["components"]) == 2
    assert all(Polygon(c["geometry"]["polygon"], c["geometry"]["holes"]).area > 0 for c in narrow["components"])
    assert clearance_report(hall, width=.1)["component_count"] == 1
    assert narrow["width_m"] == .8
    assert narrow["max_arc_chord_deviation_m"] > 0
    assert narrow["diagnostic_only"]
    assert "pass" not in narrow and "status" not in narrow
    assert "No door/entry/destination reachability" in narrow["limitations"][0]
    assert hall.wkb == before


def test_clearance_reports_room_too_small_without_inventing_space():
    report = clearance_report(box(0, 0, .3, .3), width=.8)
    assert report["component_count"] == 0
    assert report["components"] == []
    assert report["centre_area_m2"] == 0
