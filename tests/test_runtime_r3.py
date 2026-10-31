"""R3 runs the real runtime and T1 tools against local scripted responses."""

import asyncio
from contextlib import asynccontextmanager
import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace
from time import monotonic
from unittest.mock import patch

import pytest
from mcp.server.fastmcp import FastMCP
from mcp.types import CallToolResult
from PIL import Image

from scripts.tool_scripts import run_bim_agent as runner
from scripts.tool_scripts import bim_agent_budget
from src.agent import runtime_entry
from src.agent.runtime_configuration import argv_for, load_configuration
from src.agent_runtime import loop
from src.agent_runtime.agent_registry import load_agent_registry
from src.agent.runtime_tools import FrozenBimTools, local_observer_role
from src.agent_runtime.adapter import ScriptedAdapter
from src.agent_runtime.loop import RunLimits
from src.agent_runtime.store import EventStore


ROOT = Path(__file__).resolve().parents[1]
PLAN = dict(floor_id="local", z_floor=0, ceiling_height=3,
    x_anchors=[[0, 0], [11, 11]], y_anchors=[[0, 7], [7, 0]], basis="synthetic frame",
    footprint_pixels=[[1, 1], [10, 1], [10, 6], [1, 6]],
    partitions=[], openings=[], assumptions=[], unresolved=[])


def _evidence(name, value):
    directory = os.environ.get("R3_EVIDENCE_OUT")
    if directory:
        target = Path(directory)
        target.mkdir(parents=True, exist_ok=True)
        (target / (name + ".json")).write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


class LocalClient:
    """Only the transport is replaced; FastMCP/T1 execute unmodified tools."""

    def __init__(self, run, clock, *, readonly=False):
        servers = []
        with patch.object(FastMCP, "run", lambda server: servers.append(server)):
            runner.serve(run, readonly=readonly)
        self.server, self.clock = servers[0], clock
        self.before_tool_time = None

    async def list_tools(self):
        return [t.model_dump(mode="json", exclude_none=True) for t in await self.server.list_tools()]

    async def call_tool(self, name, arguments):
        if self.before_tool_time is not None:
            self.clock[0] = self.before_tool_time
            self.before_tool_time = None
        value = await self.server.call_tool(name, arguments)
        if isinstance(value, CallToolResult):
            return value.model_dump(mode="json", exclude_none=True)
        content, structured = value if isinstance(value, tuple) else (value, None)
        return {"content": [b.model_dump(mode="json", exclude_none=True) for b in content],
            "isError": False, **({"structuredContent": structured} if structured is not None else {})}


async def _scenario(tmp_path, monkeypatch, actions, *, model_calls=20, preparation_seconds=0,
                    seconds=6000, image_history_batches=None, resume_completed=False, role="coordinator"):
    images = tmp_path / "images"
    images.mkdir()
    for name in ("plan.png", "upstairs.png"):
        Image.new("RGB", (12, 8), "white").save(images / name)
    clock, clients, requests = [1000.0], [], []
    fake_time = SimpleNamespace(perf_counter=monotonic, time=lambda: clock[0], monotonic=lambda: clock[0] + monotonic() * 1e-6)
    for module in (loop, runtime_entry, runner, bim_agent_budget):
        monkeypatch.setattr(module, "time", fake_time)

    @asynccontextmanager
    async def client(run_directory, readonly=False, repository_root=None):
        clock[0] += preparation_seconds
        value = LocalClient(run_directory, clock, readonly=readonly)
        clients.append(value)
        yield value

    out = tmp_path / "output"

    class ScenarioAdapter:
        def __init__(self, responses):
            self.responses = iter(responses)

        async def send(self, prepared, *, timeout):
            requests.append(prepared.body)
            row = next(self.responses)
            clock[0] = row.get("now", clock[0])
            clients[0].before_tool_time = row.get("at_tool")
            commands = row.get("tools", [row] if row.get("tool") else [])
            calls = []
            for index, command in enumerate(commands):
                arguments = dict(command.get("arguments", {}))
                if command["tool"] == "assemble_plan_bim":
                    toolkit = runner.Toolkit(out / "bim")
                    arguments["floors_json"] = json.dumps([
                        dict(draft_id=f"draft_{i:03d}",
                            expected_plan_sha256=toolkit.inspect_plan(f"draft_{i:03d}")["plan_sha256"],
                            floor_id=f"F{i}", z_floor=3*(i-1), evidence="synthetic floor scope")
                        for i in (1, 2)])
                calls.append({"id": f"call_{len(requests)}_{index}", "type": "function", "function": {
                    "name": command["tool"], "arguments": json.dumps(arguments)}})
            if calls:
                message = {"role": "assistant", "content": None, "tool_calls": calls}
            else:
                message = {"role": "assistant", "content": row.get("text", "Saved results; no further action.")}
            return {"id": f"offline_{len(requests)}", "model": "scripted-model",
                "choices": [{"message": message, "finish_reason": row.get("finish_reason", "tool_calls" if calls else "stop")}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20}}

    monkeypatch.setattr(runtime_entry, "frozen_bim_client", client)
    monkeypatch.setattr(runtime_entry, "ScriptedAdapter", ScenarioAdapter)
    script = tmp_path / "actions.json"
    script.write_text(json.dumps(actions))
    args = runtime_entry.parser().parse_args(["--out", str(out), "--run-root", str(tmp_path), "--images", str(images),
        "--provider", "scripted", "--script", str(script),
        "--role", role,
        *(["--no-time-limit"] if seconds is None else ["--seconds", str(seconds)]),
        "--model-calls", str(model_calls), "--tool-calls", "20", "--tokens", "20000000",
        "--max-candidates", "8", "--floor-plan-image", "plan.png",
        "--floor-plan-image", "upstairs.png",
        *(["--no-context"] if image_history_batches is None else
          ["--image-history-batches", str(image_history_batches)])])
    receipt = await runtime_entry.execute(args)
    if resume_completed:
        assert receipt["status"] == "completed"
        completion = receipt["completion"]
        args.resume = True
        receipt = await runtime_entry.execute(args)
        assert receipt["completion"] == completion
        for name, sha in completion["artifact_sha256"].items():
            assert hashlib.sha256((out / "bim" / name).read_bytes()).hexdigest() == sha
    events = [json.loads(line) for line in (out / "events.jsonl").read_text(encoding="utf-8").splitlines()]
    manifest = json.loads((out / "bim/inputs.json").read_bytes())
    assert manifest["started_epoch"] == 1000
    assert manifest["deadline_epoch"] == (1000 + seconds if seconds is not None else None)
    assert manifest["time_budget_seconds"] == seconds
    assert receipt["started_epoch"] == manifest["started_epoch"]
    assert receipt["deadline_epoch"] == manifest["deadline_epoch"]
    assert receipt["agent_version"] == load_agent_registry(ROOT)["current_domain_version"]
    return out, receipt, requests, events


def _build(image="plan.png"):
    return {"tool": "build_plan_bim", "arguments": {"image": image, "plan_json": json.dumps(PLAN)}}


def test_half_near_deadline_and_late_tool_refusal_through_runtime(tmp_path, monkeypatch):
    actions = [{"tool": "inputs"}, {"tool": "inputs", "now": 4000},
        {"tool": "inputs", "now": 6101},
        {**_build(), "now": 6999, "at_tool": 7000}]
    out, receipt, requests, events = asyncio.run(_scenario(tmp_path, monkeypatch, actions))
    shown = [m["content"] for request in requests for m in request["messages"] if m["role"] == "tool"]
    text = "\n".join(shown)
    assert "保守剩余（本运行）：时间 100.0 分钟" in text
    assert "最紧额度已过半，尚无草稿的楼层图：plan.png, upstairs.png" in text
    assert "剩余不足15%" in text and "停止新范围探索" in text
    assert "有界复核已列严重问题" in text and "交付并列未决" in text
    assert receipt["status"] == "time_budget_exhausted"
    assert not list((out / "bim").glob("candidate_*"))
    # The final refusal is recorded even though the expired runtime cannot
    # send a further model request to display it.
    saved = "\n".join(p.read_bytes().decode("utf-8", errors="replace") for p in (out / "blobs").rglob("*") if p.is_file())
    assert "Run deadline reached; no further tool actions" in saved
    assert "时间上限已到" in saved
    _evidence("time_boundaries", {"model_requests": 0, "scripted_responses": len(requests),
        "half_reminder": True, "near_reminder": True, "late_action_refused": True,
        "candidate_count": 0, "status": receipt["status"], "clock": [1000, 7000]})


def test_tool_preparation_uses_the_same_start_and_does_not_restart_clock(tmp_path, monkeypatch):
    _, receipt, _, _ = asyncio.run(_scenario(tmp_path, monkeypatch, [{}], preparation_seconds=600, model_calls=1))
    assert 600 <= receipt["elapsed_seconds"] < 601
    assert receipt["started_epoch"] == 1000 and receipt["deadline_epoch"] == 7000




@pytest.mark.parametrize("ending", ["deadline", "normal", "model_limit"])
def test_stops_deliver_complete_before_newer_partial(tmp_path, monkeypatch, ending):
    actions = [_build(), _build("upstairs.png"), {"tool": "assemble_plan_bim"}, _build()]
    if ending == "deadline":
        actions += [{"tool": "finish_bim", "arguments": {"candidate": "candidate_04"}},
            {"tool": "inputs", "now": 6999, "at_tool": 7000}]
    elif ending == "normal":
        actions += [{}, {}, {}]
    out, receipt, requests, _ = asyncio.run(_scenario(tmp_path, monkeypatch, actions,
        model_calls=4 if ending == "model_limit" else 20))
    delivery = json.loads((out / "bim/delivery.json").read_bytes())
    assert receipt["status"] == {"deadline": "time_budget_exhausted", "normal": "domain_completion_unmet",
                                 "model_limit": "model_budget_exhausted"}[ending]
    assert delivery["candidate"] == "candidate_03"
    assert delivery["selection_origin"] == "latest_complete_fallback_not_agent_selected"
    assert delivery["floor_completeness"]["complete_building"] is True
    assert delivery["floor_completeness"]["latest_saved_candidate"] == "candidate_04"
    assert str(out / "bim/delivery.json") in receipt["artifact_paths"]
    assert receipt["finalization"]["delivery"]["candidate"] == "candidate_03"
    assert (out / "bim/delivery.html").is_file()
    _evidence("full_then_partial_" + ending, {"model_requests": 0, "status": receipt["status"],
        "delivery": receipt["finalization"], "floor_completeness": delivery["floor_completeness"]})


def test_only_partial_saved_is_delivered_as_incomplete(tmp_path, monkeypatch):
    actions = [_build(), {"tool": "inputs", "now": 6999, "at_tool": 7000}]
    out, receipt, _, _ = asyncio.run(_scenario(tmp_path, monkeypatch, actions))
    delivery = json.loads((out / "bim/delivery.json").read_bytes())
    assert delivery["candidate"] == "candidate_01"
    assert delivery["selection_origin"] == "latest_saved_fallback_not_agent_selected"
    assert delivery["floor_completeness"]["complete_building"] is False
    assert delivery["floor_completeness"]["missing_candidate_images"] == ["upstairs.png"]
    assert delivery["generation_status"]["timed_out"] is True
    _evidence("partial_only", {"model_requests": 0, "status": receipt["status"],
        "delivery": receipt["finalization"], "floor_completeness": delivery["floor_completeness"]})


def test_non_timeout_preserves_model_selection(tmp_path, monkeypatch):
    actions = [_build(), _build("upstairs.png"), {"tool": "assemble_plan_bim"},
        {"tool": "finish_bim", "arguments": {"candidate": "candidate_01"}}, {}, {}, {}]
    out, receipt, _, _ = asyncio.run(_scenario(tmp_path, monkeypatch, actions))
    delivery = json.loads((out / "bim/delivery.json").read_bytes())
    assert delivery["candidate"] == "candidate_01" and delivery["selection_origin"] == "agent_selected"
    assert delivery["floor_completeness"]["complete_building"] is False
    assert receipt["status"] == "domain_completion_unmet" and "completion" not in receipt
    assert json.loads((out / "bim/summary.json").read_bytes())["task_completed"] is False
    _evidence("explicit_selection", receipt["finalization"])


@pytest.mark.parametrize("initial", ["text", "truncated"])
def test_incomplete_response_continues_to_actual_two_floor_handoff(tmp_path, monkeypatch, initial):
    first = {"text": "The building is not modeled yet; I need to continue."}
    if initial == "truncated":
        first["finish_reason"] = "length"
    actions = [first, _build(), _build("upstairs.png"), {"tool": "assemble_plan_bim"},
        {"tool": "finish_bim", "arguments": {"candidate": "candidate_03"}}]
    out, receipt, requests, events = asyncio.run(_scenario(tmp_path, monkeypatch, actions, seconds=None))
    assert receipt["status"] == "completed" and receipt["answer"] is None
    assert receipt["model_calls"] == len(requests) == 5
    assert receipt["tool_calls"] == 4 and receipt["truncations"] == int(initial == "truncated")
    assert len(receipt["task_budget"]["settlements"]) == 5
    assert receipt["reported_tokens"] == 100
    assert len([e for e in events if e["payload"]["event_type"] == "tool_execution"]) == 4
    assert receipt["completion"]["candidate"] == "candidate_03"
    delivery = json.loads((out / "bim/delivery.json").read_bytes())
    assert delivery["floor_completeness"]["complete_building"] is True
    assert delivery["selection_origin"] == "agent_selected"
    summary = json.loads((out / "bim/summary.json").read_bytes())
    assert summary["task_completed"] is True and summary["completion_origin"] == "domain_tool"
    _evidence("continue_to_handoff_" + initial, {"live_model_requests": 0,
        "scripted_responses": len(requests), "status": receipt["status"],
        "tool_calls": receipt["tool_calls"], "completion": receipt["completion"],
        "summary": summary, "drawing_fidelity": "not_evaluated"})


@pytest.mark.parametrize("model_calls,expected", [(1, "model_budget_exhausted"), (20, "domain_completion_unmet")])
def test_text_without_candidate_never_reports_completed_building(tmp_path, monkeypatch, model_calls, expected):
    out, receipt, requests, _ = asyncio.run(_scenario(tmp_path, monkeypatch, [{}, {}, {}], model_calls=model_calls))
    assert receipt["status"] == expected and receipt["finalization"]["delivery"] is None
    assert receipt["model_calls"] == len(requests) == min(model_calls, 3)
    summary = json.loads((out / "bim/summary.json").read_bytes())
    assert summary["task_completed"] is False and summary["agent_response_completed"] is False
    assert summary["completion_origin"] is None and summary["delivery"] is None
    assert not list((out / "bim").glob("candidate_*"))


def test_readonly_entry_text_still_completes_observation_without_building(tmp_path, monkeypatch):
    out, receipt, requests, _ = asyncio.run(_scenario(tmp_path, monkeypatch,
        [{"text": "Read-only inspection is complete."}], role="local_observer"))
    assert receipt["status"] == "completed" and receipt["answer"] == "Read-only inspection is complete."
    assert len(requests) == 1 and receipt["finalization"]["delivery"] is None
    summary = json.loads((out / "bim/summary.json").read_bytes())
    assert summary["agent_response_completed"] is True and summary["task_completed"] is False
    assert summary["completion_origin"] is None


@pytest.mark.parametrize("project_reply", [False, True])
def test_finish_complete_stops_without_extra_request_or_rewriting_handoff(tmp_path, monkeypatch, project_reply):
    from src.agent.runtime_delivery import completed_building_handoff
    from scripts.tool_scripts import bim_agent_replies
    delivered, original = [], runner.Toolkit.delivery

    def delivery(toolkit, *args, **kwargs):
        result = original(toolkit, *args, **kwargs)
        delivered.append(result)
        return result

    monkeypatch.setattr(runner.Toolkit, "delivery", delivery)
    if project_reply:
        original_reply = bim_agent_replies.compact_reply

        def projected_reply(run, tool, result, **kwargs):
            value = original_reply(run, tool, result, **kwargs)
            if tool == "finish_bim":
                # Exercise the same immutable reference used when a full reply
                # does not fit; completion cannot depend on visible JSON size.
                return {k: value[k] for k in ("details_file", "details_sha256", "details_read")}
            return value

        monkeypatch.setattr(bim_agent_replies, "compact_reply", projected_reply)
    actions = [_build(), _build("upstairs.png"), {"tool": "assemble_plan_bim"},
               {"tool": "finish_bim", "arguments": {"candidate": "candidate_03"}}]
    out, receipt, requests, _ = asyncio.run(_scenario(tmp_path, monkeypatch, actions))
    assert receipt["status"] == "completed" and receipt["answer"] is None
    assert len(requests) == receipt["model_calls"] == 4
    assert receipt["completion"]["tool_name"] == "finish_bim"
    assert len(delivered) == 1 and receipt["finalization"]["reused_handoff"] is True
    assert receipt["finalization"]["delivery"]["selection_origin"] == "agent_selected"
    summary = json.loads((out / "bim/summary.json").read_bytes())
    assert summary["task_completed"] is True and summary["agent_response_completed"] is False
    assert summary["completion_origin"] == "domain_tool"
    handoff = json.loads((out / "bim/delivery.json").read_bytes())
    assert handoff["generation_status"]["agent_response_completed"] is False
    assert "已选定并保存 BIM，未完成事项见下方。" in (out / "bim/delivery.html").read_text(encoding="utf-8")
    assert "模型调用尚未结束。" not in (out / "bim/delivery.html").read_text(encoding="utf-8")
    assert "Single-model runtime completion" in (out / "guide.txt").read_text(encoding="utf-8")
    # A former successful handoff cannot attest changed files, even if the
    # candidate name and self-declared source digest have not changed.
    with EventStore(out, run_id=out.name, task_id="coordinator",
                    budget_limit=RunLimits.model_validate(receipt["limits"]).ledger_limit()) as store:
        engine = SimpleNamespace(store=store, role=SimpleNamespace(read_only=False),
                                 tools=SimpleNamespace(run_directory=out / "bim"))
        assert completed_building_handoff(engine) == receipt["completion"]
        for name, sha in receipt["completion"]["artifact_sha256"].items():
            path = out / "bim" / name
            before = path.read_bytes()
            assert hashlib.sha256(before).hexdigest() == sha
            path.write_bytes(before + b" ")
            assert completed_building_handoff(engine) is None
            path.write_bytes(before)
        assert completed_building_handoff(engine) == receipt["completion"]


def test_successful_finish_at_deadline_needs_no_next_request(tmp_path, monkeypatch):
    original = LocalClient.call_tool

    async def after_deadline(client, name, arguments):
        result = await original(client, name, arguments)
        if name == "finish_bim":
            client.clock[0] = 7001.0
        return result

    monkeypatch.setattr(LocalClient, "call_tool", after_deadline)
    actions = [_build(), _build("upstairs.png"), {"tool": "assemble_plan_bim"},
               {"tool": "finish_bim", "arguments": {"candidate": "candidate_03"}, "now": 6999}]
    _, receipt, requests, _ = asyncio.run(_scenario(tmp_path, monkeypatch, actions))
    assert receipt["status"] == "completed" and len(requests) == 4
    assert receipt["elapsed_seconds"] > 6000  # Completion is not a latency pass.
    assert receipt["finalization"]["delivery"]["selection_origin"] == "agent_selected"


def test_no_time_limit_finish_bim_after_ten_minutes_is_agent_selected_completion(tmp_path, monkeypatch):
    actions = [_build(), {**_build("upstairs.png"), "now": 1701}, {"tool": "assemble_plan_bim"},
               {"tool": "finish_bim", "arguments": {"candidate": "candidate_03"}, "now": 1901}]
    out, receipt, requests, _ = asyncio.run(_scenario(tmp_path, monkeypatch, actions, seconds=None))
    assert receipt["status"] == "completed" and len(requests) == receipt["model_calls"] == 4
    assert receipt["elapsed_seconds"] >= 901 and receipt["deadline_epoch"] is None
    assert receipt["completion"]["tool_name"] == "finish_bim" and receipt["answer"] is None
    assert receipt["finalization"]["reused_handoff"] is True
    assert receipt["finalization"]["delivery"]["selection_origin"] == "agent_selected"
    shown = "\n".join(m["content"] for request in requests for m in request["messages"] if m["role"] == "tool")
    assert "整案不设时间上限" in shown and "时间上限已到" not in shown
    summary = json.loads((out / "bim/summary.json").read_bytes())
    assert summary["task_completed"] is True and summary["agent_response_completed"] is False
    _evidence("no_time_limit_finish", {"model_requests": 0, "scripted_responses": len(requests),
        "status": receipt["status"], "elapsed_seconds": receipt["elapsed_seconds"],
        "deadline_epoch": None, "selection_origin": "agent_selected"})


def test_image_history_entry_tool_versions_and_completed_resume(tmp_path, monkeypatch):
    from src.agent_runtime.store import json_bytes

    actions = [_build(), _build("upstairs.png"), {"tool": "assemble_plan_bim"},
               {"tool": "finish_bim", "arguments": {"candidate": "candidate_03"}}]
    out, receipt, requests, _ = asyncio.run(_scenario(tmp_path, monkeypatch, actions,
        seconds=None, image_history_batches=1, resume_completed=True))
    assert receipt["status"] == "completed" and receipt["model_calls"] == len(requests) == 4
    assert receipt["finalization"]["delivery"]["selection_origin"] == "agent_selected"
    versions = json.loads((out / "versions.json").read_bytes())
    for request in requests:
        assert sum(t["function"]["name"] == "read_context_image" for t in request["tools"]) == 1
        assert hashlib.sha256(json_bytes(request["tools"])).hexdigest() == versions["tool_definitions"]["identifier"]
    _evidence("image_history_entry", {"model_requests": 0, "scripted_responses": len(requests),
        "status": receipt["status"], "image_history_batches": 1,
        "completed_resume_artifacts_unchanged": True})


def test_handoff_changed_after_attestation_fails_without_fallback(tmp_path, monkeypatch):
    original = runtime_entry.finalize_runtime_building

    def mutate_before_finalization(engine, reason):
        assert engine.completion and reason == "completed"
        path = engine.tools.run_directory / "delivery.html"
        path.write_bytes(b"changed after attestation")
        return original(engine, reason)

    monkeypatch.setattr(runtime_entry, "finalize_runtime_building", mutate_before_finalization)
    actions = [_build(), _build("upstairs.png"), {"tool": "assemble_plan_bim"},
               {"tool": "finish_bim", "arguments": {"candidate": "candidate_03"}}]
    out, receipt, requests, _ = asyncio.run(_scenario(tmp_path, monkeypatch, actions))
    assert receipt["status"] == "completion_state_changed" and len(requests) == 4
    assert receipt["finalization"]["completion_valid"] is False
    assert receipt["finalization"]["delivery"] is None
    assert (out / "bim/delivery.html").read_bytes() == b"changed after attestation"
    assert json.loads((out / "bim/delivery.json").read_bytes())["selection_origin"] == "agent_selected"
    summary = json.loads((out / "bim/summary.json").read_bytes())
    assert summary["task_completed"] is False and summary["completion_origin"] is None


def test_read_timeout_after_finish_preserves_selection_without_task_completion(tmp_path, monkeypatch):
    original = LocalClient.call_tool

    async def timeout_after_finish(client, name, arguments):
        if name == "inputs":
            client.clock[0] = 7001.0
            raise TimeoutError("synthetic read timeout after finish")
        return await original(client, name, arguments)

    monkeypatch.setattr(LocalClient, "call_tool", timeout_after_finish)
    actions = [_build(), _build("upstairs.png"), {"tool": "assemble_plan_bim"},
               {"tools": [{"tool": "finish_bim", "arguments": {"candidate": "candidate_03"}},
                          {"tool": "inputs"}]}]
    out, receipt, requests, _ = asyncio.run(_scenario(tmp_path, monkeypatch, actions))
    assert receipt["status"] == "time_budget_exhausted" and len(requests) == 4
    assert "completion" not in receipt and receipt["answer"] is None
    assert receipt["finalization"]["reused_handoff"] is True
    assert receipt["finalization"]["delivery"]["selection_origin"] == "agent_selected"
    summary = json.loads((out / "bim/summary.json").read_bytes())
    assert summary["task_completed"] is False and summary["agent_response_completed"] is False
    assert summary["completion_origin"] is None
    saved = json.loads(next((out / "bim/tool_reports").glob("finish_bim_*.json")).read_bytes())
    assert all(hashlib.sha256((out / "bim" / name).read_bytes()).hexdigest() == sha
               for name, sha in saved["delivery_artifact_sha256"].items())




def test_floor_scope_configuration_and_input_rejection(tmp_path):
    from tests.current_runtime_fixtures import configuration
    value = configuration()
    value["cases"][0]["floor_plan_images"] = ["1f_view.png"]
    config = tmp_path / "configuration.json"
    config.write_text(json.dumps(value))
    case = load_configuration(config)["cases"][0]
    argv = argv_for(case)
    assert argv[argv.index("--floor-plan-image") + 1] == "1f_view.png"
    value["cases"][0]["floor_plan_images"] = ["../unknown.png"]
    config.write_text(json.dumps(value))
    with pytest.raises(ValueError, match="admitted input filenames"):
        load_configuration(config)
    images = tmp_path / "images"
    images.mkdir()
    Image.new("RGB", (12, 8)).save(images / "1f_view.png")
    kwargs = dict(images=images, mesh=None, building_input=None, scope="fixture", image_kind="drawings", max_candidates=1)
    with pytest.raises(ValueError, match="admitted input filenames"):
        runtime_entry.prepare_inputs(tmp_path / "bad", **kwargs, floor_plan_images=["unknown.png"])
    run, _, _ = runtime_entry.prepare_inputs(tmp_path / "hint", **kwargs)
    hint = json.loads((run / "inputs.json").read_bytes())
    assert hint["floor_plan_images"] == ["1f_view.png"]
    assert hint["floor_scope_source"] == "input_filename_hint"
