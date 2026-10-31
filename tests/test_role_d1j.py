"""Behavior at D1j's role-only boundaries; local geometry, no model services."""

from reader_delivery_fixtures import REVIEW

import asyncio
import copy
import hashlib
import json

import pytest
from PIL import Image, ImageDraw

from src.agent.runtime_roles.elevation import ElevationReaderTools, match_elevation, validate_elevation_artifact
from src.agent.runtime_roles.guidance import COMPACT_ELEVATION_EXAMPLE
from src.agent.runtime_roles.lineage import opening_plan
from src.agent.runtime_roles.session import envelope
from tests.test_role_d1g import LocalTools, height_session
from tests.test_role_elevation import _opening
from tests.test_role_readers import Frozen


@pytest.mark.parametrize("facade,view,axis,world", [
    ("North", "South", "x", [9.46, 4.66]), ("South", "North", "x", [.54, 5.34]),
    ("East", "West", "y", [.54, 5.34]), ("West", "East", "y", [9.46, 4.66]),
])
def test_reader_task_supplies_direction_and_partial_chain_origin(tmp_path, facade, view, axis, world):
    async def scenario():
        image_dir = tmp_path / "images"
        image_dir.mkdir()
        image_path = image_dir / "North.png"
        image = Image.new("L", (900, 800), 255)
        # Synthetic aperture ink supports this coordinate/direction fixture.
        ImageDraw.Draw(image).rectangle((200, 380, 300, 567), outline=0, width=2)
        image.save(image_path)
        digest = hashlib.sha256(image_path.read_bytes()).hexdigest()
        (tmp_path / "inputs.json").write_text(
            json.dumps({"images": {"North.png": {"sha256": digest}}}),
            encoding="utf-8",
        )
        tools = ElevationReaderTools(Frozen(tmp_path), role_id="elevation_reader", image_name="North.png", target=facade + "/F1")
        args = copy.deepcopy(COMPACT_ELEVATION_EXAMPLE)
        args["x_calibration"].update(distance_start_m=.54, distance_end_m=5.34, facade_length_m=10)
        args["counts"][0]["window_count"] = 2
        failed = await tools.call_tool("trial_elevation_reading", args)
        assert failed["isError"] and tools.submission.read() is None
        args["counts"][0]["window_count"] = 1
        prepared = await tools.call_tool("trial_elevation_reading", args)
        assert not prepared["isError"], prepared
        assert tools.submission.read() is None
        review_args = {"trial_id": prepared["structuredContent"]["trial_id"], "local_review": REVIEW}
        accepted = await tools.call_tool("submit_elevation_reading", review_args)
        assert not accepted["isError"], accepted
        saved = tools.submission.read()["artifact"]
        assert (saved["orientation"], saved["view_direction"]) == (facade, view)
        c = saved["x_calibration"]
        assert c["world_axis"] == axis
        assert [c["world_start_m"], c["world_end_m"]] == pytest.approx(world)
        assert validate_elevation_artifact(saved) == saved
        assert (await tools.call_tool("submit_elevation_reading", review_args)) == accepted
        args["x_calibration"]["facade_length_m"] = 4
        assert (await tools.call_tool("submit_elevation_reading", args))["isError"]
        assert tools.submission.read()["artifact"] == saved
    asyncio.run(scenario())


def drift_group(*, factor=1.04, offset=0, bend=0, count=3, source_count=3, bad_width=False):
    centers = [10, 15, 19]
    source = {"source_model_sha256": "a" * 64, "spaces": [{"id": "room", "floor_id": "F1"}],
        "boundaries": [{"id": "north", "vertices": [[30, 10, 0], [0, 10, 0], [0, 10, 4], [30, 10, 4]]}],
        "openings": [_opening(str(i), "window", (c - .5, 10), (c + .5, 10), (1, 2), "north")
                     for i, c in enumerate(centers[:source_count])]}
    artifact = copy.deepcopy(COMPACT_ELEVATION_EXAMPLE)
    artifact.update(image="North.png", orientation="North", view_direction="South",
        x_calibration={"pixel_start": 100, "pixel_end": 3100, "world_start_m": 30, "world_end_m": 0})
    artifact["openings"] = []
    for i in reversed(range(count)):
        coordinate = centers[i] * factor + offset + (bend if i == 1 else 0)
        px = 100 + (30 - coordinate) * 100
        artifact["openings"].append({"id": "read-" + str(i), "floor_id": "F1", "kind": "window",
            "x_px": [px - 50, px + 50], "width_m": 2 if bad_width and i == 1 else 1,
            "sill_m": 1, "head_m": 2.4, "evidence_type": "pixels", "bbox": [px - 51, 200, px + 51, 400]})
    artifact["counts"][0]["window_count"] = count
    return source, artifact


def test_complete_ordered_fit_reports_transform_and_never_moves_source():
    source, artifact = drift_group()
    before = copy.deepcopy(source)
    result = match_elevation(source, artifact)
    assert len(result["matches"]) == 3 and not result["conflicts"]
    assert result["horizontal_fits"][0]["scale"] == pytest.approx(1 / 1.04)
    assert result["horizontal_fits"][0]["max_residual_m"] < 1e-9
    assert all(p["artifact_opening_id"] == "read-" + p["source_opening_id"] for p in result["matches"])
    assert all(abs(p["raw_position_difference_m"]) > .35 for p in result["matches"])
    assert source == before


@pytest.mark.parametrize("parameters", [{"count": 2, "source_count": 2}, {"count": 2},
    {"factor": 1.2}, {"offset": 1}, {"bend": .5}, {"bad_width": True}])
def test_incomplete_or_unjustified_alignment_stays_unresolved(parameters):
    source, artifact = drift_group(**parameters)
    result = match_elevation(source, artifact)
    assert not result["horizontal_fits"]
    assert len(result["matches"]) < min(len(source["openings"]), len(artifact["openings"]))


class EditTools(LocalTools):
    async def call_tool(self, name, arguments):
        if name == "revise_bim":
            self.calls.append((name, copy.deepcopy(arguments)))
            return envelope(self.toolkit.revise(arguments["candidate"], arguments["operations_json"]))
        return await super().call_tool(name, arguments)


def test_local_use_note_position_and_height_edits_preserve_other_content_and_replay(tmp_path):
    async def scenario(session):
        session.frozen = EditTools(session.run_directory)
        before = session._source("candidate_01")
        old_notes = json.loads((session.run_directory / "candidate_01/proposal.json").read_bytes())["unresolved"]
        args = {"candidate": "candidate_01", "edits": [
            {"action": "use", "id": "room", "role": "office", "image": "plan.png", "reason": "desk layout"},
            {"action": "note", "text": "ceiling mark remains uncertain", "reason": "drawing limitation"},
            {"action": "position", "id": "North", "along_start_m": 1.1, "along_end_m": 2.2,
             "image": "plan.png", "reason": "observed jambs"}]}
        result = await session.call_tool("edit_bim", args)
        assert not result["isError"], result
        candidate = result["structuredContent"]["candidate"]
        after = session._source(candidate)
        room = next(r for r in after["spaces"] if r["id"] == "room")
        assert room["role"] == "office" and room["role_evidence"]["basis"] == "inferred"
        assert room["role_evidence"]["assumptions"] == ["desk layout"]
        assert json.loads((session.run_directory / candidate / "proposal.json").read_bytes())["unresolved"] == old_notes + ["ceiling mark remains uncertain"]
        for original, updated in zip(before["openings"], after["openings"], strict=True):
            if original["id"] != "North":
                assert original == updated
        calls = len(session.frozen.calls)
        guard = session.assembly.guard
        def pending_review():
            raise ValueError("a positional correction now awaits assembly review")
        session.assembly.guard = pending_review
        assert (await session.call_tool("edit_bim", args))["structuredContent"] == result["structuredContent"]
        assert len(session.frozen.calls) == calls
        fresh = {"candidate": candidate, "edits": [{"action": "note", "text": "new note", "reason": "new edit"}]}
        assert (await session.call_tool("edit_bim", fresh))["isError"]
        assert len(session.frozen.calls) == calls
        session.assembly.guard = guard
        heights = {"candidate": candidate, "edits": [{"action": "height", "id": "North", "sill_m": .9,
            "head_m": 2.3, "image": "plan.png", "bbox": [0, 0, 2, 2], "reason": "observed sill and head"}]}
        result = await session.call_tool("edit_bim", heights)
        assert not result["isError"], result
        saved = session._source(result["structuredContent"]["candidate"])
        assert opening_plan(after) == opening_plan(saved)
        assert sorted({p[2] for r in saved["openings"] if r["id"] == "North" for p in r["vertices"]}) == [.9, 2.3]
    with height_session(tmp_path) as session:
        asyncio.run(scenario(session))


def test_bad_edit_batch_has_no_partial_write_and_unknown_outcome_never_repeats(tmp_path):
    async def scenario(session):
        session.frozen = EditTools(session.run_directory)
        note = {"action": "note", "text": "keep this", "reason": "valid note"}
        bad = {"action": "use", "id": "missing", "role": "office", "image": "plan.png", "reason": "bad target"}
        assert (await session.call_tool("edit_bim", {"candidate": "candidate_01", "edits": [note, bad]}))["isError"]
        assert not session.frozen.calls
        ordinary = session.frozen.call_tool
        async def killed(name, args):
            await ordinary(name, args)
            raise RuntimeError("simulated process death before durable receipt")
        session.frozen.call_tool = killed
        args = {"candidate": "candidate_01", "edits": [note]}
        with pytest.raises(RuntimeError):
            await session.call_tool("edit_bim", args)
        count = len(session.frozen.calls)
        session.frozen.call_tool = ordinary
        assert (await session.call_tool("edit_bim", args))["isError"]
        assert len(session.frozen.calls) == count
    with height_session(tmp_path) as session:
        asyncio.run(scenario(session))
