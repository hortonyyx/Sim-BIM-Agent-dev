"""Expanded reader batches survive crashes without spending sibling work twice."""
from __future__ import annotations

from reader_delivery_fixtures import elevation_responses

import asyncio
import hashlib
import json

import pytest
from PIL import Image

from src.agent_runtime.adapter import ScriptedAdapter
from src.agent_runtime.control import submit_command
from src.harness_contracts import MissingCapture, ToolExecutionPayload, ToolInvocationPayload

from test_agent_runtime import response
from test_role_session import dispatch, elevation, environment
from test_runtime_recovery_edges import InjectedCrash


def setup_batch(environment, *, recovery_retries=1):
    store, make = environment
    session = make(max_concurrent_readers=1)
    run = session.run_directory
    Image.new("RGB", (100, 100), "white").save(run / "images/south_view.png")
    manifest = json.loads((run / "inputs.json").read_bytes())
    manifest["image_kind"] = "drawings"
    manifest["images"]["south_view.png"] = {
        "sha256": hashlib.sha256((run / "images/south_view.png").read_bytes()).hexdigest()}
    (run / "inputs.json").write_text(json.dumps(manifest), encoding="utf-8")
    session.limits = session.limits.model_copy(update={"max_tool_recovery_retries": recovery_retries})
    sent = []

    def factory(task_id, config, child):
        reading = elevation()
        if task_id != "north":
            reading.update(orientation="South", view_direction="North")
            reading["x_calibration"].update(world_start_m=0, world_end_m=10)
        adapter = ScriptedAdapter(elevation_responses(reading, "prepare-" + task_id))
        original = adapter.send

        async def send(request, *, timeout):
            sent.append(task_id)
            return await original(request, timeout=timeout)

        adapter.send = send
        return adapter

    def crash_south(name, engine):
        if name == "before_request" and engine.store.task_id != "north":
            raise InjectedCrash("auto-added child before_request")

    def new(*, crash=False):
        value = make(factory=factory, max_concurrent_readers=1,
                     reader_fault_hook=crash_south if crash else None)
        value.limits = session.limits
        return value

    arguments = {"tasks": [dispatch(origin="One shared drawing origin")]}
    original = store.append(ToolInvocationPayload(call_id="delegate-1", tool_name="delegate_readers",
        full_arguments=arguments, repeatability="read_only", operation_key=None,
        state_before=store.put_json(session.snapshot_state())))
    return store, new, arguments, original, sent


@pytest.mark.parametrize("recovery_retries", [1, 2])
def test_expanded_batch_recovers_latest_attempt_after_second_crash(environment, recovery_retries):
    store, new, arguments, original, sent = setup_batch(environment, recovery_retries=recovery_retries)
    with pytest.raises(InjectedCrash):
        asyncio.run(new(crash=True).call_tool("delegate_readers", arguments))
    first = new()
    auto_id = next(key for key, row in first.registry.records.items() if row["image"] == "south_view.png")
    assert first.registry.records["north"]["status"] == "completed"
    assert first.registry.records[auto_id]["status"] == "running"
    assert sent == ["north", "north"]
    child_workspace = first.registry.child(auto_id).task_directory / "bim"
    reference_paths = [child_workspace / "reader_issued_references.json",
                       *sorted((child_workspace / "image_views").glob("*.json"))]
    original_references = {path: path.read_bytes() for path in reference_paths}
    store.append(ToolExecutionPayload(call_id="delegate-1", tool_name="delegate_readers",
        full_arguments=arguments, repeatability="read_only", operation_key=None,
        invocation_event_id=original.event_id, outcome="unknown",
        raw_result=MissingCapture(reason="parent process interrupted"),
        shown_result=MissingCapture(reason="not presented"), presentation_status="prepared"))
    with pytest.raises(InjectedCrash):
        asyncio.run(new(crash=True).recover_pending())
    assert {path: path.read_bytes() for path in reference_paths} == original_references
    assert set((child_workspace / "image_views").glob("*.json")) == set(reference_paths[1:])
    pending = [event for event in store.events if event.payload.event_type == "tool_invocation"]
    assert len(pending) == 2
    assert all(event.payload.full_arguments == arguments for event in pending)
    resumed = new()
    assert asyncio.run(resumed.recover_pending()) == [pending[-1].event_id]
    executions = [event for event in store.events if event.payload.event_type == "tool_execution"]
    assert [event.payload.outcome for event in executions] == ["unknown", "succeeded"]
    assert executions[-1].payload.retry_event_id is not None
    shown = store.resolve(executions[-1].payload.raw_result)["structuredContent"]
    assert {row["task_id"] for row in shown["results"]} == {"north", auto_id}
    assert shown["auto_added"][0]["task_id"] == auto_id
    assert all(row["status"] == "completed" for row in shown["results"]), json.dumps(shown)
    assert next(row for row in shown["results"] if row["task_id"] == "north")["reused_saved_result"]
    assert sent == ["north", "north", auto_id, auto_id]
    assert len([event for event in store.events if event.payload.event_type == "tool_invocation"]) == 2
    assert asyncio.run(new().recover_pending()) == []
    store.validate()


def test_auto_added_child_pause_is_applied_before_recovered_model_request(environment):
    store, new, arguments, original, sent = setup_batch(environment)
    with pytest.raises(InjectedCrash):
        asyncio.run(new(crash=True).call_tool("delegate_readers", arguments))
    resumed = new()
    auto_id = next(key for key, row in resumed.registry.records.items() if row["image"] == "south_view.png")
    submit_command(store.directory, target_task_id=auto_id, action="pause", command_id="pause-auto")

    async def exercise():
        recovery = asyncio.create_task(resumed.recover_pending())

        async def paused():
            while not any(event.payload.event_type == "task_control"
                          and event.payload.command_id == "pause-auto" for event in store.all_events):
                await asyncio.sleep(0.005)

        await asyncio.wait_for(paused(), 3)
        assert sent == ["north", "north"]
        assert not recovery.done()
        submit_command(store.directory, target_task_id=auto_id, action="resume", command_id="resume-auto")
        assert await asyncio.wait_for(recovery, 3) == [original.event_id]

    asyncio.run(exercise())
    assert sent == ["north", "north", auto_id, auto_id]
    store.validate()


def test_expanded_batch_rejects_metadata_tampering_before_any_child_request(environment):
    store, new, arguments, _, sent = setup_batch(environment)
    with pytest.raises(InjectedCrash):
        asyncio.run(new(crash=True).call_tool("delegate_readers", arguments))
    path, = (store.directory / "role_batches").glob("*.json")
    record = json.loads(path.read_bytes())
    record["tasks"] = record["tasks"][:1]
    path.write_text(json.dumps(record), encoding="utf-8")
    with pytest.raises(ValueError, match="batch metadata hash"):
        asyncio.run(new().delegate_many(arguments["tasks"], call_id="delegate-1"))
    assert sent == ["north", "north"]


def test_parent_call_cannot_replace_its_expanded_task_arguments(environment):
    _, new, arguments, _, sent = setup_batch(environment)
    with pytest.raises(InjectedCrash):
        asyncio.run(new(crash=True).call_tool("delegate_readers", arguments))
    replacement = [{**arguments["tasks"][0], "instructions": "Changed instructions"}]
    with pytest.raises(ValueError, match="call identity already belongs"):
        asyncio.run(new().delegate_many(replacement, call_id="delegate-1"))
    assert sent == ["north", "north"]
