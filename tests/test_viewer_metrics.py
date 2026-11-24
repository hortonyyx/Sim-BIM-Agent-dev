from copy import deepcopy

from src.agent.geometry.source_bim import build_source_bim
from src.agent.geometry.viewer_metrics import viewer_metrics
from tests.test_source_spanning_spaces import geometry as spanning_geometry


def rect(left, bottom, right, top):
    return [[left, bottom], [right, bottom], [right, top], [left, top]]


def test_prisms_courtyard_and_disconnected_floor_components():
    floor = {"id": "L", "z_floor": -3, "height": 3}
    spaces = [{"id": "A", "floor_id": "L", "z_floor": -3, "height": 3,
               "polygon": rect(0, 0, 4, 3), "holes": [rect(1, 1, 3, 2)]},
              {"id": "B", "floor_id": "L", "z_floor": -3, "height": 3,
               "polygon": rect(6, 0, 8, 2)}]
    source = {"floors": [floor], "spaces": spaces, "openings": [
        {"id": "window", "space_ids": ["A"], "vertices": [[1, 0, -2], [2, 0, -2], [2, 0, -1], [1, 0, -1]]}]}
    original = deepcopy(source)
    assert viewer_metrics(source) == {
        "spaces": {"A": {"area_m2": 10, "volume_m3": 30}, "B": {"area_m2": 4, "volume_m3": 12}},
        "floors": {"L": {"space_count": 2, "area_m2": 14, "volume_m3": 42}}}
    assert source == original


def test_stepped_neighbors_share_projection_but_not_air_volume():
    # A expands above z=1 precisely where B narrows. Their full-height projections overlap.
    spaces = [
        {"id": "A", "floor_id": "L", "z_floor": 0, "height": 3, "polygon": rect(0, 0, 4, 2),
         "vertical_sections": [{"z": [0, 1], "polygon": rect(0, 0, 3, 2)},
                               {"z": [1, 3], "polygon": rect(0, 0, 4, 2)}]},
        {"id": "B", "floor_id": "L", "z_floor": 0, "height": 3, "polygon": rect(3, 0, 5, 2),
         "vertical_sections": [{"z": [0, 1], "polygon": rect(3, 0, 5, 2)},
                               {"z": [1, 3], "polygon": rect(4, 0, 5, 2)}]}]
    metrics = viewer_metrics({"floors": [{"id": "L", "z_floor": 0, "height": 3}], "spaces": spaces})
    assert metrics["spaces"] == {"A": {"area_m2": 8, "volume_m3": 22},
                                 "B": {"area_m2": 4, "volume_m3": 8}}
    assert metrics["floors"]["L"] == {"space_count": 2, "area_m2": 10, "volume_m3": 30}
    # Only the upper section of B belongs to this explicitly referenced level.
    upper = {"id": "upper", "z_floor": 1, "height": 2, "spanning_space_ids": ["B"]}
    assert viewer_metrics({"floors": [upper], "spaces": spaces})["floors"]["upper"] == {
        "space_count": 1, "area_m2": 2, "volume_m3": 4}


def test_spanning_space_once_per_storey_and_in_its_own_full_height_group():
    source = build_source_bim(spanning_geometry())
    metrics = viewer_metrics(source)
    assert metrics["spaces"]["core"] == {"area_m2": 4, "volume_m3": 24}
    assert metrics["floors"]["CORE"] == {"space_count": 1, "area_m2": 4, "volume_m3": 24}
    for floor in ("F1", "F2"):
        assert metrics["floors"][floor] == {"space_count": 2, "area_m2": 16, "volume_m3": 48}
    assert sum(m["volume_m3"] for m in metrics["spaces"].values()) == 96


def test_partial_geometry_has_only_supported_quantities():
    assert viewer_metrics(None) == {"floors": {}, "spaces": {}}
    data = {"floors": [{"id": "L", "z_floor": 0, "height": 3}], "spaces": [
        {"id": "A", "floor_id": "L", "polygon": rect(0, 0, 4, 3)}, {"id": "B", "floor_id": "L"}]}
    assert viewer_metrics(data) == {"spaces": {"A": {"area_m2": 12}, "B": {}},
                                    "floors": {"L": {"space_count": 2}}}
