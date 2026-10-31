"""Saved BIM JSON is UTF-8 even when Windows defaults to GBK."""
import json
from pathlib import Path

import pytest

from src.agent_runtime.continuation import ReadOnlyRun
from src.agent_runtime.loop import RunLimits
from src.agent_runtime.store import EventStore
from src.harness_contracts import RunLifecyclePayload
from scripts.tool_scripts.bim_agent_budget import saved_floor_status
from scripts.tool_scripts.run_bim_agent import Toolkit
from tests.test_bim_agent_plan_partition import example
from tests.test_bim_agent_tools import _run_with_one_image


STANDARD = "OpenStudio(R) standards — level 1 space types"


@pytest.fixture
def gbk_defaults(monkeypatch):
    read_text, write_text, path_open = Path.read_text, Path.write_text, Path.open

    def read(path, encoding=None, errors=None):
        return read_text(path, encoding=encoding or "gbk", errors=errors)

    def write(path, data, encoding=None, errors=None, newline=None):
        return write_text(path, data, encoding=encoding or "gbk", errors=errors, newline=newline)

    def open_file(path, mode="r", buffering=-1, encoding=None, errors=None, newline=None):
        return path_open(path, mode, buffering, encoding if "b" in mode else encoding or "gbk", errors, newline)

    monkeypatch.setattr(Path, "read_text", read)
    monkeypatch.setattr(Path, "write_text", write)
    monkeypatch.setattr(Path, "open", open_file)


def test_build_and_reread_source_with_nonascii_standard_under_gbk(tmp_path, gbk_defaults):
    run = _run_with_one_image(tmp_path)
    plan = example()
    plan["basis"] += "; 原图坐标 — observed calibration"
    toolkit = Toolkit(run)
    toolkit.log("unicode_observation", {"standard": STANDARD, "label": "源模型"})
    built = toolkit.build_plan("plan.png", json.dumps(plan, ensure_ascii=False))
    assert built["source_geometry_ready"], built
    raw = (run / built["candidate"] / "source_model.json").read_bytes()
    assert STANDARD in raw.decode("utf-8")
    with pytest.raises(UnicodeDecodeError):
        raw.decode("gbk")
    source = json.loads(raw)
    assert source["spaces"]
    assert built["height_coverage"]["schema_version"] == "opening_heights_v2"
    assert built["facade_counts"]["schema_version"] == "facade_count_report_v1"
    for key in ("height_coverage", "facade_counts"):
        assert built[key]["source_model_sha256"] == source["source_model_sha256"]
    image, view = toolkit.plan_view(built["candidate"], source["floors"][0]["id"])
    assert image.data and (run / view["plan_image"]).is_file()
    assert view["floor_id"] == source["floors"][0]["id"]
    status = saved_floor_status(toolkit, built["candidate"])
    assert status["candidate_source_geometry_ready"]
    assert status["unreadable_saved_candidates"] == []
    assert STANDARD in (run / "tools.jsonl").read_bytes().decode("utf-8")
    assert toolkit.input_view_status()["images"]


def test_native_receipt_roundtrip_preserves_utf8_bytes_under_gbk(tmp_path, gbk_defaults):
    path = tmp_path / "receipt.json"
    value = {"standard": STANDARD, "label": "源模型"}
    with EventStore(tmp_path, run_id="utf8", task_id="main",
                    budget_limit=RunLimits(model_calls=1, tool_calls=1, seconds=10, tokens=1000).ledger_limit()) as store:
        store.append(RunLifecyclePayload(action="start", reason="offline UTF-8 fixture"))
        store.write_json("receipt.json", value)
    before = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    assert json.loads(path.read_bytes().decode("utf-8")) == value
    source = ReadOnlyRun(tmp_path)
    assert source.read_json("receipt.json") == value
    assert source.verify_unchanged()
    assert {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()} == before
