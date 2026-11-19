"""A DSH client uses the same evidence-bound observers for drawings and mixed input."""

import asyncio
import json
from pathlib import Path
import sys
import time

import pytest

from src.agent import observer_mcp, runtime_entry
from src.agent.runtime_mesh_workers import observer_tool_specs
from src.agent_runtime.mcp_tools import McpToolClient
from src.agent_runtime.store import EventStore
from tests.test_mesh_input_admission import _case
from tests.test_mesh_worker_entry import _response


@pytest.mark.parametrize("with_mesh", [False, True])
def test_external_observer_evidence_accounting_and_restart(tmp_path, with_mesh):
    case = _case(tmp_path)
    inputs = Path(case["input_root"])
    run, _, _ = runtime_entry.prepare_inputs(tmp_path / "parent", images=inputs / "sketches",
        mesh=inputs / "building.glb" if with_mesh else None, building_input=inputs / "declaration.json",
        image_kind="drawings", scope="Read 1f.png as a schematic drawing.", max_candidates=2,
        started_epoch=time.time(), seconds=300, orchestrator=True)
    answer = {"directly_seen": [{"observation_id": "region", "statement": "This region appears blank.",
        "evidence_kind": "image", "evidence_id": "1f.png", "box_pixels": [1, 1, 6, 6]}],
        "drawing_readings": [{"image": "1f.png", "based_on_observation_ids": ["region"],
            "coordinate_frame": "original image pixels", "data": {"labels": []}}]}
    fixture = tmp_path / "observers.json"
    fixture.write_text(json.dumps({"observer": [_response("view", "view_image", {"name": "1f.png"}),
                                               _response("answer", answer=json.dumps(answer))]}))
    store = tmp_path / "pool"
    argv = ["--run", str(run), "--store", str(store), "--provider", "scripted", "--model", "scripted-model",
            "--script", str(fixture), "--max-concurrent", "2", "--model-calls", "4", "--tool-calls", "3"]
    args = observer_mcp.parser().parse_args(argv)
    task = {"tasks": [{"task_id": "observer", "question": "Check the marked region.",
                       "budget": {"model_calls": 3, "tool_calls": 1}}]}

    async def query():
        async with McpToolClient(command=sys.executable, args=["-m", "src.agent.observer_mcp", *argv],
                                 cwd=runtime_entry.ROOT, run_directory=run) as client:
            assert await client.list_tools() == observer_tool_specs(dsh=True)
            result = await client.call_tool("delegate_workers", task)
            assert not result["isError"], result
            worker = result["structuredContent"]["results"][0]
            assert worker["status"] == "completed", worker
            assert worker["result"]["drawing_readings"] == answer["drawing_readings"]
            assert worker["result_reference"]["read"]["tool"] == "read_worker"
            view = await client.call_tool("read_worker", {"task_id": "observer", "view_ids": ["1f.png"]})
            assert any(block["type"] == "image" for block in view["content"])

    asyncio.run(query())
    before = (store / "events.jsonl").read_bytes()
    asyncio.run(query())
    assert (store / "events.jsonl").read_bytes() == before
    events = EventStore.read_events(store / "events.jsonl")
    assert sum(event.payload.event_type == "adapter_request" for event in events) == 2
    packet_path, = store.glob("tasks/*/bim/mesh_worker_packet.json")
    packet = json.loads(packet_path.read_bytes())
    assert packet["building_scope"] == "Read 1f.png as a schematic drawing."
    assert packet["user_declaration"]["use"] == "user suspects hotel"
    assert bool(packet["mesh_sha256"]) == with_mesh
    args.model = "changed-scripted-alias"
    with pytest.raises(ValueError, match="changed on resume"), observer_mcp.open_observers(args):
        pass


def test_native_coordinator_carries_observer_route_and_rejects_changed_resumption(tmp_path):
    inputs = Path(_case(tmp_path)["input_root"])
    parent_script, worker_script = tmp_path / "parent.json", tmp_path / "child.json"
    parent_script.write_text(json.dumps([
        _response("ask", "delegate_workers", {"tasks": [{"task_id": "inspect", "question": "What is unknown?",
                                                        "budget": {"model_calls": 1, "tool_calls": 0}}]}),
        _response("stop", answer="The source is incomplete.")]))
    worker_script.write_text(json.dumps({"inspect": [_response("reply", answer=json.dumps(
        {"uncertain": [{"uncertainty_id": "scale", "statement": "The scale is unresolved."}]}))]}))
    out = tmp_path / "native"
    args = runtime_entry.parser().parse_args(["--out", str(out), "--run-root", str(tmp_path),
        "--images", str(inputs / "sketches"), "--provider", "scripted", "--script", str(parent_script),
        "--orchestrator", "--worker-concurrency", "2", "--worker-model", "observer-fixture",
        "--worker-output-tokens", "32768", "--mesh-worker-script", str(worker_script),
        "--model-calls", "6", "--tool-calls", "4", "--seconds", "180"])
    result = asyncio.run(runtime_entry.execute(args))
    assert result["workers"]["model"] == "observer-fixture"
    assert result["workers"]["tasks"][0]["status"] == "completed"
    events = EventStore.read_events(out / "events.jsonl")
    request, = [e.payload for e in events if e.task_id == "inspect" and e.payload.event_type == "adapter_request"]
    assert request.versions.remote_model.remote_alias == "observer-fixture"
    assert result["workers"]["token_budget"]["output_token_limit"] == 32768
    assert result["whole_run_counts"]["by_task"]["inspect"]["model_requests"] == 1
    assert result["whole_run_counts"]["model_requests"] == sum(
        event.payload.event_type == "adapter_request" for event in events)
    before = (out / "events.jsonl").read_bytes()
    args.resume = True
    args.worker_model = "different-worker"
    with pytest.raises(ValueError, match="configuration changed"):
        asyncio.run(runtime_entry.execute(args))
    assert (out / "events.jsonl").read_bytes() == before
