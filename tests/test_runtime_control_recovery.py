"""Operator steering and explicit read recovery through the real journal loop."""
from __future__ import annotations

import asyncio
import json

import pytest

from src.agent_runtime.control import submit_command, control_status
from src.agent_runtime.context import ContextPolicy
from src.agent_runtime.loop import RunLimits
from src.agent_runtime.store import EventStore
from src.harness_contracts import RunLifecyclePayload, TaskControlPayload

from test_agent_runtime import MESSAGES, response, runtime
from test_runtime_child_tasks import _child
from test_runtime_recovery_edges import InjectedCrash


async def until(predicate):
    async def wait():
        while not predicate():
            await asyncio.sleep(0.005)
    await asyncio.wait_for(wait(), 3)


def test_control_target_and_duplicate_rejections_leave_journal_unchanged(tmp_path):
    engine = runtime(tmp_path, [])
    with engine.store as store:
        command = store.put_json({"source": "offline"})
        store.append(TaskControlPayload(command_id="once", action="pause",
            target_task_id=store.task_id, command=command))
        before = store.path.read_bytes()
        for payload in (
            TaskControlPayload(command_id="once", action="resume",
                target_task_id=store.task_id, command=command),
            TaskControlPayload(command_id="wrong-target", action="pause",
                target_task_id="another-task", command=command),
        ):
            with pytest.raises(ValueError):
                store.append(payload)
            assert store.path.read_bytes() == before
        store.validate()


def test_inflight_correction_waits_for_tool_batch_and_pause_preserves_request_count(tmp_path):
    engine = runtime(tmp_path, [response(("read", "view", {})), response(text="corrected")])

    async def exercise():
        sent, release = asyncio.Event(), asyncio.Event()
        original = engine.adapter.send

        async def blocked(request, *, timeout):
            if not engine.adapter.requests:
                sent.set()
                await release.wait()
            return await original(request, timeout=timeout)

        engine.adapter.send = blocked
        task = asyncio.create_task(engine.run(MESSAGES))
        await sent.wait()
        submit_command(engine.store.directory, target_task_id="task", action="pause", command_id="pause")
        submit_command(engine.store.directory, target_task_id="task", action="message", text="Use value 8", command_id="edit")
        release.set()
        await until(lambda: engine._control_paused)
        assert len(engine.adapter.requests) == 1
        assert len(engine.tools.calls) == 1
        assert all(c["status"] == "applied" for c in control_status(engine.store.directory)["commands"])
        submit_command(engine.store.directory, target_task_id="task", action="resume", command_id="resume")
        result = await task
        assert result["status"] == "completed"
        body = json.loads(engine.adapter.requests[-1])
        assert body["messages"][-1] == {"role": "user", "content": "Use value 8"}
        assert sum(m.get("content") == "Use value 8" for m in body["messages"]) == 1
        assert len(engine.adapter.requests) == 2
        assert result["runtime_processing"]["process_phase_seconds"]["request_prepare"] > 0
        durations = [s for e in engine.store.events for s in e.source_refs if s.source_id == "request-duration"]
        assert all("response_parse_capture_seconds" in json.loads(engine.store.get_bytes(s.blob))["phases"] for s in durations)

    with engine.store:
        asyncio.run(exercise())


@pytest.mark.parametrize("context", [False, True])
def test_crash_after_control_ack_replays_message_once(tmp_path, context):
    first = runtime(tmp_path, [])
    if context:
        first.context_policy = ContextPolicy()
    submit_command(first.store.directory, target_task_id="task", action="message", text="correct once", command_id="edit")

    def crash(name, _):
        if name == "after_control_event":
            raise InjectedCrash(name)

    first.fault_hook = crash
    with first.store:
        with pytest.raises(InjectedCrash):
            asyncio.run(first.run(MESSAGES))
    resumed = runtime(tmp_path, [response(text="done")], tools=first.tools)
    if context:
        resumed.context_policy = ContextPolicy()
    with resumed.store:
        assert asyncio.run(resumed.run(MESSAGES, resume=True))["status"] == "completed"
        body = json.loads(resumed.adapter.requests[0])
        assert sum(m.get("content") == "correct once" for m in body["messages"]) == 1
        assert sum(e.payload.event_type == "task_control" for e in resumed.store.events) == 1
        resumed.store.validate()


def test_pause_survives_checkpoint_and_restart_without_new_budget(tmp_path):
    first = runtime(tmp_path, [])
    submit_command(first.store.directory, target_task_id="task", action="pause", command_id="pause")

    def crash(name, engine):
        if name == "after_checkpoint" and engine._control_paused:
            raise InjectedCrash(name)

    first.fault_hook = crash
    with first.store:
        with pytest.raises(InjectedCrash):
            asyncio.run(first.run(MESSAGES))
        original_start = first.started_epoch
    resumed = runtime(tmp_path, [response(text="done")], tools=first.tools)
    submit_command(resumed.store.directory, target_task_id="task", action="message", text="continue with correction", command_id="edit")
    submit_command(resumed.store.directory, target_task_id="task", action="resume", command_id="resume")
    with resumed.store:
        result = asyncio.run(resumed.run(MESSAGES, resume=True))
        assert result["status"] == "completed"
        assert result["started_epoch"] == original_start
        assert result["model_calls"] == 1


def test_command_ids_are_idempotent_and_targets_checked(tmp_path):
    engine = runtime(tmp_path, [])
    with engine.store:
        first = submit_command(engine.store.directory, target_task_id="task", action="pause", command_id="x")
        assert submit_command(engine.store.directory, target_task_id="task", action="pause", command_id="x") == first
        with pytest.raises(ValueError, match="different content"):
            submit_command(engine.store.directory, target_task_id="task", action="resume", command_id="x")
        with pytest.raises(ValueError, match="not started"):
            submit_command(engine.store.directory, target_task_id="absent", action="pause")
        with pytest.raises(ValueError):
            submit_command(engine.store.directory, target_task_id="task", action="pause", command_id="../escape")


@pytest.mark.parametrize("boundary", ["after_tool", "cancel"])
def test_explicit_repeatable_read_recovers_but_preserves_unknown_evidence(tmp_path, boundary):
    first = runtime(tmp_path, [response(("read", "view", {}))])
    first.tools.recovery_policy = lambda name: "retry_read" if name == "view" else "manual"
    original = first.tools.call_tool
    if boundary == "after_tool":
        def crash(name, _):
            if name == "after_tool":
                raise InjectedCrash(name)
        first.fault_hook = crash
        with first.store:
            with pytest.raises(InjectedCrash):
                asyncio.run(first.run(MESSAGES))
    else:
        async def cancel():
            entered = asyncio.Event()
            async def wait_forever(name, arguments):
                entered.set()
                await asyncio.Future()
            first.tools.call_tool = wait_forever
            task = asyncio.create_task(first.run(MESSAGES))
            await entered.wait()
            task.cancel()
            assert (await task)["status"] == "cancelled"
        with first.store:
            asyncio.run(cancel())
    first.tools.call_tool = original
    resumed = runtime(tmp_path, [response(text="done")], tools=first.tools)
    with resumed.store:
        result = asyncio.run(resumed.run(MESSAGES, resume=True))
        assert result["status"] == "completed"
        executions = [e for e in resumed.store.events if e.payload.event_type == "tool_execution"]
        assert [e.payload.outcome for e in executions] == ["unknown", "succeeded"]
        assert executions[-1].payload.retry_event_id is not None
        assert result["tool_calls"] == 2
        assert result["model_calls"] == 2
        resumed.store.validate()


def test_read_permission_alone_does_not_authorize_unknown_tool_replay(tmp_path):
    first = runtime(tmp_path, [response(("read", "view", {}))])
    def crash(name, _):
        if name == "after_tool":
            raise InjectedCrash(name)
    first.fault_hook = crash
    with first.store:
        with pytest.raises(InjectedCrash):
            asyncio.run(first.run(MESSAGES))
    resumed = runtime(tmp_path, [], tools=first.tools)
    with resumed.store:
        assert asyncio.run(resumed.run(MESSAGES, resume=True))["status"] == "resume_pending_operation"
        assert len(first.tools.calls) == 1


def test_six_tasks_share_budget_while_only_target_receives_control(tmp_path):
    limits = RunLimits(model_calls=12, tool_calls=12, seconds=120, tokens=1_000_000)
    with EventStore(tmp_path/"run", run_id="six", task_id="root", budget_limit=limits.ledger_limit()) as root:
        root.append(RunLifecyclePayload(action="start", reason="six task recovery test"))
        engines = [_child(root, tmp_path, str(i), [response(text="done")]) for i in range(6)]
        for engine in engines:
            engine.request_timeout_seconds = 5

        def crash(name, _):
            if name == "before_request":
                raise InjectedCrash(name)
        engines[0].fault_hook = crash
        with pytest.raises(InjectedCrash):
            asyncio.run(engines[0].run(MESSAGES))
        engines[0] = _child(root, tmp_path, "0", [response(text="recovered")], tools=engines[0].tools)
        engines[0].request_timeout_seconds = 5
        submit_command(root.directory, target_task_id="0", action="pause", command_id="p")
        submit_command(root.directory, target_task_id="0", action="message", text="Only child zero", command_id="m")

        async def exercise():
            tasks = [asyncio.create_task(engine.run(MESSAGES, resume=i==0)) for i,engine in enumerate(engines)]
            await until(lambda: getattr(engines[0], "_control_paused", False))
            await until(lambda: all(task.done() for task in tasks[1:]))
            assert engines[0].adapter.requests == []
            submit_command(root.directory, target_task_id="0", action="resume", command_id="r")
            return await asyncio.gather(*tasks)

        results = asyncio.run(exercise())
        assert all(r["status"] == "completed" for r in results)
        assert len([e for e in root.all_events if e.payload.event_type == "adapter_request"]) == 6
        for i,engine in enumerate(engines):
            text = engine.adapter.requests[0].decode()
            assert ("Only child zero" in text) == (i == 0)
        root.validate()


def test_pause_does_not_extend_original_wall_deadline(tmp_path):
    limits = RunLimits(model_calls=1, tool_calls=0, seconds=0.3, tokens=100_000)
    engine = runtime(tmp_path, [], limits=limits)
    submit_command(engine.store.directory, target_task_id="task", action="pause")
    with engine.store:
        result = asyncio.run(engine.run(MESSAGES))
        assert result["status"] == "time_budget_exhausted"
        assert engine.adapter.requests == []
        assert result["deadline_epoch"] == result["started_epoch"] + 0.3


def test_context_estimate_uses_same_reasoning_sources_as_wire(tmp_path, monkeypatch):
    import src.agent_runtime.loop as loop_module
    estimates = []
    original = loop_module.estimate_chat_request

    def capture(body, **kwargs):
        estimates.append(body)
        return original(body, **kwargs)

    monkeypatch.setattr(loop_module, "estimate_chat_request", capture)
    monkeypatch.setattr("src.agent_runtime.adapter.estimate_chat_request", capture)
    engine = runtime(tmp_path, [response(("read", "view", {}), reasoning=True), response(text="done")])
    engine.reasoning_history = "current_tool_chain"
    engine.context_policy = ContextPolicy()
    with engine.store:
        assert asyncio.run(engine.run(MESSAGES))["status"] == "completed"
        projected = [body for body in estimates if any(m.get("tool_calls") for m in body["messages"])]
        assert projected
        for body in projected:
            assert body["messages"][-1]["role"] == "user"  # generated state
            assert any(m.get("reasoning_content") for m in body["messages"])
        wire = json.loads(engine.adapter.requests[-1])
        assert [m.get("reasoning_content") for m in projected[-1]["messages"]] == [
            m.get("reasoning_content") for m in wire["messages"]]
