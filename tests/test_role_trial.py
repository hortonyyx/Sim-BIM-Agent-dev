import asyncio
import base64
import hashlib
import json
from pathlib import Path

import pytest
from PIL import Image, ImageDraw

from src.agent.runtime_roles.trial import PlanTrial, PlanTrialSession, canonical_plan_sha256
from tests.test_bim_agent_plan_partition import example as _geometry_example
from tests.test_bim_agent_tools import _json_result, _run_with_one_image, _server_session


ROOT = Path(__file__).resolve().parents[1]


def example():
    value = _geometry_example()
    value["reading_type"] = "plan"
    value["wall_metadata"] = [
        {"target": target, "thickness_m": thickness, "reference_side": "centerline",
         "evidence_status": "inferred", "basis": "Synthetic reader test assumption",
         "source_refs": ["plan.png:fixture"]}
        for target, thickness in (("footprint:*", .2), ("partition:*", .1))
    ]
    return value


def prepare_workspace(path, *, image_bytes=None):
    path = Path(path)
    (path / "images").mkdir(parents=True, exist_ok=True)
    image = path / "images/plan.png"
    if image_bytes is None:
        Image.new("RGB", (12, 8), "white").save(image, "PNG")
    else:
        image.write_bytes(image_bytes)
    raw = image.read_bytes()
    with Image.open(image) as decoded:
        size = list(decoded.size)
    (path / "inputs.json").write_text(json.dumps({
        "images": {"plan.png": {"size": size, "sha256": hashlib.sha256(raw).hexdigest()}},
    }), encoding="utf-8", newline="\n")
    return path


class Tools:
    """A model-tool sentinel; planar trials must use their own local compiler."""
    def __init__(self, workspace=None):
        self.workspace = workspace
        self.calls = []

    async def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        raise AssertionError("plan trials must not call a model or a 3D build tool")

    def image_origins(self, raw):
        return {}


def test_trial_hashes_exact_plan_preserves_checks_and_requires_same_success(tmp_path):
    async def scenario():
        value = example()
        workspace = prepare_workspace(tmp_path / "trial")
        trial = PlanTrial(Tools(), image_name="plan.png", receipt_directory=workspace / "receipts", workspace=workspace)
        envelope = await trial.call(value)
        visible = envelope["structuredContent"]
        receipt = trial.require_success(value)
        assert receipt["status"] == "passed"
        assert visible["status"] == "passed"
        assert visible["plan_sha256"] == receipt["plan_sha256"]
        assert receipt["plan_sha256"] == canonical_plan_sha256(value)
        assert receipt["original_plan_sha256"] == receipt["plan_sha256"]
        assert receipt["compiled_numeric_plan_sha256"] == receipt["compiled_plan_sha256"]
        assert receipt["reading_geometry_ready"] is True
        assert receipt["source_geometry_ready"] is False
        assert receipt["drawing_differences"]["status"] == "reported"
        product = json.loads((workspace / receipt["reading_product_file"]).read_bytes())
        assert product["validation"]["topology"] == "pass"
        assert "corridor" in receipt["corridor_review"]["hint"].lower()
        assert trial.require_success(value) == receipt
        assert trial.delivery_receipt(value)["validation_passed"] is True
        with pytest.raises(ValueError, match="has not been run"):
            trial.require_success({**value, "floor_id": "F2"})
        assert (workspace / "receipts/trial_001.json").is_file()
    asyncio.run(scenario())


def test_failed_trial_is_retained_but_not_accepted(tmp_path):
    async def scenario():
        value = example()
        value["openings"][0]["p2"][0] += 1
        workspace = prepare_workspace(tmp_path / "trial")
        trial = PlanTrial(Tools(), image_name="plan.png", workspace=workspace,
                          receipt_directory=workspace / "receipts")
        receipt = await trial.run(value)
        assert receipt["status"] == "failed"
        assert "opening" in json.dumps(receipt["repair_hint"])
        with pytest.raises(ValueError, match="no successful isolated trial"):
            trial.require_success(value)
        delivery = trial.delivery_receipt(value)
        assert delivery["validation_passed"] is False
        assert delivery["reason"] == receipt["reason"]
    asyncio.run(scenario())


def test_new_reader_locally_repairs_hash_verified_failed_draft_with_retained_profile(tmp_path, monkeypatch):
    async def scenario():
        prior_workspace = prepare_workspace(tmp_path / "prior")
        raw_image = (prior_workspace / "images/plan.png").read_bytes()
        image_sha = hashlib.sha256(raw_image).hexdigest()

        def matching_workspace(name):
            return prepare_workspace(tmp_path / name, image_bytes=raw_image)

        profiles = tmp_path / "prior_profiles"
        profiles.mkdir()
        profile = {
            "name": "plan.png", "image_sha256": image_sha, "axis": "x",
            "candidates": [
                {"id": "C01", "pixels": [5, 5], "peak": 5},
                {"id": "C02", "pixels": [7, 7], "peak": 7},
            ],
        }
        (profiles / "profile_001.json").write_text(
            json.dumps(profile), encoding="utf-8", newline="\n")
        plan = example()
        midpoint = {"midpoint": [
            {"profile": "profile_001", "candidate": "C01"},
            {"profile": "profile_001", "candidate": "C02"},
        ]}
        for point in plan["partitions"][0]["points"]:
            point[0] = midpoint
        for field in ("p1", "p2"):
            plan["openings"][0][field][0] = midpoint
        for row in [*plan["partitions"], *plan["openings"]]:
            row["source_refs"] = ["view_old: located on the admitted plan"]

        prior = PlanTrial(
            Tools(workspace=prior_workspace), image_name="plan.png",
            receipt_directory=prior_workspace / "trial_receipts", workspace=prior_workspace,
            profile_directory=profiles,
        )
        from src.agent.runtime_roles import plan_reading
        build = plan_reading.build_reading

        def mismatched_product(*args, **kwargs):
            result = build(*args, **kwargs)
            result["structuredContent"]["reading_product_sha256"] = "0" * 64
            return result

        with monkeypatch.context() as patch:
            patch.setattr(plan_reading, "build_reading", mismatched_product)
            failed = await prior.run(plan)
        assert failed["status"] == "failed" and failed["compiled_numeric_plan_sha256"]
        with pytest.raises(ValueError, match="successful isolated trial"):
            prior.verified_plan(failed["plan_sha256"])

        current_workspace = matching_workspace("current")
        current_tools = Tools(workspace=current_workspace)
        current = PlanTrial(
            current_tools, image_name="plan.png",
            receipt_directory=current_workspace / "trial_receipts", workspace=current_workspace,
            profile_directory=tmp_path / "current_profiles",
        )
        inherited = current.inherit_failed_reference(
            prior, image_sha, receipt_file=failed["receipt_file"],
            plan_sha256=failed["plan_sha256"],
            compiled_numeric_plan_sha256=failed["compiled_numeric_plan_sha256"],
        )
        assert inherited["validation_passed"] is False and inherited["status"] == "failed"
        assert current.inherited_reference_ids() == {"profile_001"}
        assert (tmp_path / "current_profiles/profile_001.json").is_file()
        repaired = await current.run(operations=[{
            "op": "update", "collection": "openings", "id": "D1", "changes": {"z": [0, 2.2]},
            "reason": "repair the named height only",
            "source_refs": ["view_current: opening height mark"],
            "bbox": [4, 2, 8, 6],
        }])
        assert repaired["status"] == "passed"
        assert repaired["base_plan_sha256"] == failed["plan_sha256"]
        declaration = current.load_plan(repaired)
        assert declaration["openings"][0]["z"] == [0, 2.2]
        assert declaration["openings"][1] == plan["openings"][1]
        assert declaration["partitions"] == plan["partitions"]
        compiled_call = current.numeric_plan(repaired)
        assert current_tools.calls == []
        assert compiled_call["partitions"][0]["source_refs"] == ["view_old: located on the admitted plan"]
        assert repaired["measurement_profiles"][0]["profile_id"] == "profile_001"
        copied = current_workspace / repaired["measurement_profiles"][0]["file"]
        assert copied.is_file()

        wrong_image_reader = PlanTrial(
            Tools(), image_name="plan.png", receipt_directory=tmp_path / "wrong/receipts",
            workspace=matching_workspace("wrong"),
        )
        with pytest.raises(ValueError, match="does not match this reader task"):
            wrong_image_reader.inherit_failed_reference(
                prior, "0" * 64, receipt_file=failed["receipt_file"],
                plan_sha256=failed["plan_sha256"],
                compiled_numeric_plan_sha256=failed["compiled_numeric_plan_sha256"],
            )

    asyncio.run(scenario())


def test_resolution_error_is_a_repairable_failed_receipt(tmp_path):
    async def scenario():
        (tmp_path / "images").mkdir()
        (tmp_path / "images" / "plan.png").write_bytes(b"original")
        tools = Tools()
        plan = example()
        plan["x_anchors"] = [[{"profile": "profile_001", "candidate": "C01"}, 0], [10, 1]]
        trial = PlanTrial(
            tools,
            image_name="plan.png",
            receipt_directory=tmp_path / "trial_receipts",
            workspace=tmp_path,
            profile_directory=tmp_path / "pixel_profiles",
        )
        envelope = await trial.call(plan)
        visible = envelope["structuredContent"]
        receipt = trial.receipts[-1]
        assert visible["status"] == "failed"
        assert envelope["isError"] is True
        assert "trial_plan_bim failed" in envelope["content"][-1]["text"]
        assert receipt["status"] == "failed"
        assert receipt["original_plan_sha256"] == canonical_plan_sha256(plan)
        assert receipt["compiled_numeric_plan_file"] is None
        assert "existing profile_id" in receipt["reason"]
        assert "profile_001" in receipt["reason"]
        assert "example" not in receipt["repair_hint"]
        assert tools.calls == []
        assert trial.delivery_receipt(plan)["validation_passed"] is False
        resumed = PlanTrial(
            Tools(),
            image_name="plan.png",
            receipt_directory=tmp_path / "trial_receipts",
            workspace=tmp_path,
            profile_directory=tmp_path / "pixel_profiles",
        )
        assert resumed.delivery_receipt(plan)["reason"] == receipt["reason"]

    asyncio.run(scenario())


def test_contradictory_alias_failure_receipt_can_resume(tmp_path):
    async def scenario():
        bad = {"partitions": [{
            "id": "P1", "points": [[1, 2], [3, 4]], "pixels": [[1, 2], [3, 4]],
        }]}
        receipts = tmp_path / "trial_receipts"
        trial = PlanTrial(Tools(), image_name="plan.png", receipt_directory=receipts, workspace=tmp_path)
        result = await trial.call(bad)
        assert result["isError"] and "both pixels and points" in str(result["structuredContent"]["format_errors"])
        resumed = PlanTrial(Tools(), image_name="plan.png", receipt_directory=receipts, workspace=tmp_path)
        assert resumed.receipts[0]["reason"] == result["structuredContent"]["reason"]

    asyncio.run(scenario())


def test_trial_persists_images_and_resume_reuses_verified_receipt(tmp_path):
    async def scenario():
        workspace = tmp_path / "trial_workspace"
        prepare_workspace(workspace)
        original_bytes = (workspace / "images/plan.png").read_bytes()
        receipts = workspace / "trial_receipts"
        value = example()
        tools = Tools(workspace=workspace)
        first = PlanTrial(tools, image_name="plan.png", receipt_directory=receipts, workspace=workspace)
        envelope = await first.call(value)
        assert envelope["content"][0]["type"] == "image"
        assert envelope["structuredContent"]["reading_geometry_ready"] is True
        assert set(envelope["structuredContent"]["returned_images"][0]) == {"file", "sha256"}
        full_receipt = first.require_success(value)
        assert full_receipt["returned_images"][0]["origin"]["origin_status"] == "verified trial input lineage"
        image_sha = full_receipt["returned_images"][0]["sha256"]
        origins = first.image_origins({"structuredContent": full_receipt})
        assert origins[image_sha]["source_image_name"] == "plan.png"
        assert origins[image_sha]["original_sha256"] == hashlib.sha256(original_bytes).hexdigest()
        assert first.durable_snapshot()["snapshot_sha256"]
        numeric = workspace / full_receipt["compiled_numeric_plan_file"]
        assert hashlib.sha256(numeric.read_bytes()).hexdigest() == full_receipt["compiled_numeric_plan_sha256"]
        assert any(path.name == "reading.json" for path in first.artifacts())
        resumed_tools = Tools(workspace=workspace)
        resumed = PlanTrial(resumed_tools, image_name="plan.png", receipt_directory=receipts, workspace=workspace)
        reused = await resumed.call(value)
        assert reused["structuredContent"]["plan_sha256"] == full_receipt["plan_sha256"]
        assert reused["structuredContent"]["status"] == "passed"
        assert resumed_tools.calls == []
        assert resumed._image_content(resumed.require_success(value))[0]["data"] == envelope["content"][0]["data"]
        assert resumed.delivery_receipt(value)["validation_passed"] is True
    asyncio.run(scenario())


def test_alias_normalization_has_one_trial_identity(tmp_path):
    canonical = example()
    alias = example()
    alias["partitions"][0]["pixels"] = alias["partitions"][0].pop("points")
    assert canonical_plan_sha256(alias) == canonical_plan_sha256(canonical)

    async def scenario():
        workspace = prepare_workspace(tmp_path / "trial")
        trial = PlanTrial(Tools(), image_name="plan.png", workspace=workspace,
                          receipt_directory=workspace / "receipts")
        receipt = await trial.run(alias)
        assert receipt["field_aliases"] == [
            "plan.partitions[0].pixels accepted as plan.partitions[0].points; coordinates unchanged."
        ]
        assert trial.require_success(canonical) == receipt

    asyncio.run(scenario())


def test_trial_session_prepares_exactly_one_image_workspace(tmp_path):
    reader = tmp_path / "reader"
    (reader / "images").mkdir(parents=True)
    raw = b"one-original"
    (reader / "images" / "plan.png").write_bytes(raw)
    (reader / "inputs.json").write_text(json.dumps({
        "images": {"plan.png": {"size": [12, 8], "sha256": hashlib.sha256(raw).hexdigest()}},
        "image_kind": "drawings", "started_epoch": 1, "deadline_epoch": 10,
    }), encoding="utf-8", newline="\n")
    session = PlanTrialSession(reader, "plan.png", root=tmp_path)
    session._prepare()
    manifest = json.loads((reader / "trial_workspace" / "inputs.json").read_text())
    assert list(manifest["images"]) == ["plan.png"]
    assert manifest["scope"] == "one plan-reader image; no parent building draft"
    assert (reader / "trial_workspace" / "images" / "plan.png").read_bytes() == raw


def test_real_profile_is_resolved_to_recoverable_independent_numeric_plan(tmp_path):
    async def scenario():
        reader = _run_with_one_image(tmp_path)
        image = Image.new("RGB", (12, 8), "black")
        draw = ImageDraw.Draw(image)
        for x in (5, 7):
            draw.line((x, 0, x, 7), fill=(128, 128, 128))
        image.save(reader / "images" / "plan.png")
        manifest = json.loads((reader / "inputs.json").read_text())
        manifest["images"]["plan.png"]["sha256"] = hashlib.sha256(
            (reader / "images" / "plan.png").read_bytes()
        ).hexdigest()
        (reader / "inputs.json").write_text(json.dumps(manifest), encoding="utf-8", newline="\n")

        async with _server_session(reader, readonly=True) as service:
            response = await service.call_tool("view_pixel_profile", {
                "name": "plan.png", "box": [0, 0, 12, 8], "axis": "x",
                "rgb": [128, 128, 128], "tolerance": 0, "min_fraction": 0.5,
                "include_image": False,
            })
            assert not response.isError
            profile = json.loads(response.content[0].text)
        assert [row["peak"] for row in profile["candidates"]] == [5, 7]

        plan = example()
        midpoint = {"midpoint": [
            {"profile": profile["profile_id"], "candidate": "C01"},
            {"profile": profile["profile_id"], "candidate": "C02"},
        ]}
        for point in plan["partitions"][0]["points"]:
            point[0] = midpoint
        for key in ("p1", "p2"):
            plan["openings"][0][key][0] = midpoint

        async with PlanTrialSession(reader, "plan.png", root=ROOT) as trial:
            receipt = await trial.run(plan)
            assert receipt["status"] == "passed", receipt
            assert len(receipt["measurement_bindings"]) == 4
            assert receipt["measurement_profiles"] == [{
                "profile_id": profile["profile_id"],
                "file": f"trial_receipts/trial_001_profiles/{profile['profile_id']}.json",
                "sha256": hashlib.sha256(
                    (reader / "pixel_profiles" / f"{profile['profile_id']}.json").read_bytes()
                ).hexdigest(),
            }]
            numeric_path = reader / "trial_workspace" / receipt["compiled_numeric_plan_file"]
            numeric_bytes = numeric_path.read_bytes()
            assert hashlib.sha256(numeric_bytes).hexdigest() == receipt["compiled_numeric_plan_sha256"]
            numeric = json.loads(numeric_bytes)
            assert numeric["partitions"][0]["points"] == [[6, 1], [6, 7]]
            assert receipt["compiled_plan_sha256"] == receipt["compiled_numeric_plan_sha256"]

        # The saved numeric plan compiles from a clean run containing no profiles.
        independent = tmp_path / "independent"
        (independent / "images").mkdir(parents=True)
        (independent / "images" / "plan.png").write_bytes(
            (reader / "images" / "plan.png").read_bytes()
        )
        (independent / "inputs.json").write_bytes((reader / "inputs.json").read_bytes())
        from src.agent.runtime_roles.plan_reading import build_reading
        built = build_reading(json.loads(numeric_bytes), workspace=independent,
                              image_name="plan.png", number=1)["structuredContent"]
        assert built["reading_geometry_ready"] and not (independent / "pixel_profiles").exists()

        # A new task edits the verified prior declaration without retyping its
        # profile-based coordinates. Failed edits remain the repair baseline;
        # the last passed geometry remains separately verifiable.
        next_reader = tmp_path / "next_reader"
        (next_reader / "images").mkdir(parents=True)
        (next_reader / "images/plan.png").write_bytes((reader / "images/plan.png").read_bytes())
        (next_reader / "inputs.json").write_bytes((reader / "inputs.json").read_bytes())
        prior = PlanTrial(None, image_name="plan.png", workspace=reader / "trial_workspace",
                          receipt_directory=reader / "trial_workspace/trial_receipts")
        op = {"op": "update", "collection": "openings", "id": "D1", "changes": {"z": [0, 2.2]},
              "reason": "revised declared height", "source_refs": ["plan.png: explicit assumption"], "bbox": [5, 2, 8, 6]}
        async with PlanTrialSession(next_reader, "plan.png", root=ROOT) as rework:
            rework.inherit_reference(prior, receipt["plan_sha256"], ["plan.openings:D1"])
            edited = await rework.run(operations=[op])
            assert edited["reading_geometry_ready"], edited
            assert rework.load_plan(edited)["partitions"] == plan["partitions"]
            assert rework.load_plan(edited)["openings"][1] == plan["openings"][1]
            failed = await rework.run(operations=[{**op, "changes": {"p2": [7, 4.5]}}])
            assert failed["status"] == "failed"
        async with PlanTrialSession(next_reader, "plan.png", root=ROOT) as rework:
            rework.inherit_reference(prior, receipt["plan_sha256"], ["plan.openings:D1"])
            draft, draft_sha256 = rework.baseline()
            assert draft_sha256 == failed["plan_sha256"]
            assert draft["openings"][0]["p2"] == [7, 4.5]
            assert draft["openings"][0]["z"] == [0, 2.2]
            assert rework.read_draft()["last_passed_plan_sha256"] == edited["plan_sha256"]
            accepted, validation = rework.verified_plan(edited["plan_sha256"])
            assert validation["validation_passed"] is True
            assert accepted["openings"][0]["p2"] == plan["openings"][0]["p2"]
            with pytest.raises(ValueError, match="successful isolated trial"):
                rework.verified_plan(failed["plan_sha256"])
            repaired = await rework.run(operations=[{**op, "changes": {
                "p2": plan["openings"][0]["p2"], "z": [0, 2.3],
            }}])
            assert repaired["reading_geometry_ready"]
            assert repaired["base_plan_sha256"] == failed["plan_sha256"]
            assert repaired["changes"][0]["before"]["p2"] == [7, 4.5]
            assert rework.verified_plan(repaired["plan_sha256"])[0]["openings"][0]["p2"] == plan["openings"][0]["p2"]

        # Resume reuses the verified receipt. Both derived products are tamper-evident.
        async with PlanTrialSession(reader, "plan.png", root=ROOT) as resumed:
            assert resumed.require_success(plan)["compiled_numeric_plan_sha256"] == receipt["compiled_numeric_plan_sha256"]
        numeric_path.write_bytes(numeric_bytes + b" ")
        with pytest.raises(ValueError, match="numeric plan changed"):
            async with PlanTrialSession(reader, "plan.png", root=ROOT):
                pass
        numeric_path.write_bytes(numeric_bytes)
        copied_profile = reader / "trial_workspace" / receipt["measurement_profiles"][0]["file"]
        copied_profile.write_bytes(copied_profile.read_bytes() + b" ")
        with pytest.raises(ValueError, match="measurement profile changed"):
            async with PlanTrialSession(reader, "plan.png", root=ROOT):
                pass

    asyncio.run(scenario())
