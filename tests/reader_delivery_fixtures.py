"""Explicit offline reader observations; never used as real-model evidence."""

import copy
import json
import re

from test_agent_runtime import response


REVIEW = {"summary": "Offline fixture: inspected the returned result and overlay; this is not independent drawing quality evidence."}


def compact_elevation(reading):
    value = copy.deepcopy(reading)
    orientation = value.pop("orientation", "North")
    value.pop("view_direction", None)
    cal = value["x_calibration"]
    if "world_start_m" in cal:
        start, end = cal["world_start_m"], cal["world_end_m"]
        length = max(start, end)
        low, high = (length - start, length - end) if orientation in {"North", "West"} else (start, end)
        value["x_calibration"] = {"pixel_start": cal["pixel_start"], "pixel_end": cal["pixel_end"],
            "distance_start_m": low, "distance_end_m": high, "facade_length_m": length}
    value.setdefault("z_calibration", {"pixel_start": 0, "pixel_end": 100, "world_start_m": 10, "world_end_m": 0})
    return value


def accept_elevation(request):
    ids = re.findall(r"elevation_[a-f0-9]{16}", json.dumps(request["messages"]))
    assert ids, "No prepared trial reached the model"
    return response(("accept-elevation", "submit_elevation_reading", {"trial_id": ids[-1], "local_review": REVIEW}))


def elevation_responses(reading, call_id="prepare"):
    return [response((call_id, "trial_elevation_reading", compact_elevation(reading))), accept_elevation]


def comparison_reading_bytes(source, floor_id="F1"):
    """Synthetic XY facts for assembly consumers, not a compiler test or real reading."""
    product = {"mapping": {"floor_id": floor_id, "space_mapping": [
        {"space_id": row["id"], "world_polygon_m": [v[:2] for v in row["polygon"]]}
        for row in source["spaces"] if row["floor_id"] == floor_id], "opening_hosts": []}}
    for row in source["openings"]:
        points = list(dict.fromkeys(tuple(v[:2]) for v in row["vertices"]))
        product["mapping"]["opening_hosts"].append({
            "opening_id": row["id"], "kind": row["kind"], "space_ids": row["space_ids"],
            "p1_world_m": points[0], "p2_world_m": points[-1], "exterior": row["exterior"]})
    return json.dumps(product, ensure_ascii=False, sort_keys=True).encode("utf-8")
