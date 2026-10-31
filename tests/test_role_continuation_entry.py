"""Real-MCP terminal continuation: conserve allowance and reuse verified readers."""

import asyncio
import copy
import hashlib
import json
from dataclasses import replace

import pytest

from src.agent.runtime_roles.entry import execute
from src.agent_runtime.failures import ModelServiceError
from src.harness_contracts import UsageMissing
from role_d1_fixtures import CoordinatorAdapter, make_fixture


def _hashes(directory):
    return {p.relative_to(directory).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in directory.rglob("*") if p.is_file()}


def test_terminal_new_run_reuses_six_readers_and_delivers_real_viewer(tmp_path, monkeypatch):
    source = make_fixture("sm21", tmp_path)

    def terminal_failure():
        raise ModelServiceError({"category": "service_unavailable", "retryable": False,
            "service_error_code": "server_is_overloaded", "retry_hint": "NO_MORE_RETRY",
            "usage_received": False}, UsageMissing(reason="offline injected terminal failure"))

    def source_factory(task_id, config, store):
        if task_id == "coordinator":
            return CoordinatorAdapter(store=store,
                stages=source._coordinator_stages()[:2] + [terminal_failure])
        return source.adapter_factory(task_id, config, store)

    first = asyncio.run(execute(source.args, adapter_factory=source_factory))
    assert first["status"] == "service_unavailable"
    before = _hashes(source.output)
    args = copy.copy(source.args)
    args.out = tmp_path / "sm21-continuation"
    args.continue_from = source.output
    args.source_events_sha256 = before["events.jsonl"]
    target = replace(source, output=args.out, args=args, adapters=[])

    def target_factory(task_id, config, store):
        assert task_id == "coordinator", "Completed imported readers must not call a model again"
        stages = target._coordinator_stages()
        return CoordinatorAdapter(store=store, stages=stages[:1] + stages[2:])

    role_file = source.output / "role_configuration.json"
    original_roles = role_file.read_bytes()
    forged = json.loads(original_roles)
    forged["max_concurrent_readers"] = args.max_concurrent_readers + 1
    role_file.write_text(json.dumps(forged), encoding="utf-8")
    with pytest.raises(ValueError, match="durable request route"):
        asyncio.run(execute(args, adapter_factory=target_factory))
    assert not target.output.exists()
    role_file.write_bytes(original_roles)

    result = asyncio.run(execute(args, adapter_factory=target_factory))
    assert result["status"] == "completed"
    assert result["finalization"]["status"] == "delivered"
    assert (target.output / "bim/delivery.json").is_file()
    assert (target.output / "bim/delivery.html").stat().st_size > 1000
    imported = json.loads((target.output / "reader_continuation.json").read_bytes())
    assert len(imported["summary"]["completed_ids"]) == 6
    assert _hashes(source.output) == before
    allowance = result["continuation"]["allowance"]
    assert allowance["effective_limits"]["model_calls"] < source.args.model_calls
    assert allowance["effective_limits"]["tool_calls"] < source.args.tool_calls
    assert allowance["effective_limits"]["seconds"] < source.args.seconds
    events = [json.loads(line) for line in (target.output / "events.jsonl").read_text().splitlines()]
    assert all(row["task_id"] == "coordinator" for row in events)
    assert not any(row["payload"].get("tool_name") == "delegate_readers" for row in events)
    from src.agent_runtime.continuation import ReadOnlyRun
    continued = ReadOnlyRun(target.output)
    bound_versions = continued.verified_versions()
    bound_route = json.loads(continued.get_bytes(bound_versions["model_route"]["evidence"]["blob"]))
    assert bound_route["reader_continuation"]["manifest_sha256"] == hashlib.sha256(
        (target.output / "reader_continuation.json").read_bytes()).hexdigest()
    # Even with no checkpoint, an existing request anchors the imported evidence.
    manifest_file = target.output / "reader_continuation.json"
    original_manifest = manifest_file.read_bytes()
    manifest_file.write_bytes(original_manifest + b" ")
    args.resume = True
    with monkeypatch.context() as patch:
        patch.setattr("src.agent_runtime.store.EventStore.latest_checkpoint", lambda self: None)
        with pytest.raises(ValueError, match="continuation identity changed"):
            asyncio.run(execute(args, adapter_factory=lambda *a: pytest.fail("Must fail before adapter creation")))
    manifest_file.write_bytes(original_manifest)
    assert _hashes(source.output) == before
