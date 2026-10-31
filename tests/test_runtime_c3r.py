"""C3-R boundary checks; offline model stubs only."""
import asyncio
import copy
import json
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from src.agent_runtime.run_paths import resolve_run_output
from src.agent_runtime.adapter import reasoning_history_messages
from src.harness_contracts import SourceRef
from test_agent_runtime import MESSAGES, response, runtime

ROOT = Path(__file__).resolve().parents[1]


def test_checkpoints_reuse_observed_state_but_each_tool_keeps_both_snapshots(tmp_path):
    engine = runtime(tmp_path, [response(("v", "view", {}), ("s", "save", {"v": 7})),
                                response(text="done")])
    with engine.store, patch.object(engine.tools, "snapshot_state", wraps=engine.tools.snapshot_state) as scan:
        receipt = asyncio.run(engine.run(MESSAGES))
        assert receipt["status"] == "completed"
        assert scan.call_count == 5  # initialization + before/after both tools
        events = engine.store.events
        saves = [e for e in events if e.payload.event_type == "tool_invocation"
                 and e.payload.tool_name == "save"]
        assert json.loads(engine.store.get_bytes(saves[0].payload.state_before)) == {}
        result = next(e for e in events if e.payload.event_type == "tool_execution"
                      and e.payload.tool_name == "save")
        after = json.loads(engine.store.get_bytes(next(s.blob for s in result.source_refs
                                                       if s.source_id == "tool-state-after")))
        assert set(after) == {"saved.json"}
        for event in events:
            if event.payload.event_type == "checkpoint" and event.sequence > result.sequence:
                assert engine.store.get_json_tree(event.payload.state)["tool_state"] == after


def test_explicit_storage_root_preserves_default_and_rejects_escape(tmp_path):
    assert resolve_run_output(Path("runs/a"), repository_root=ROOT) == ROOT / "runs/a"
    assert resolve_run_output(Path("a"), repository_root=ROOT, run_root=tmp_path) == tmp_path / "a"
    with pytest.raises(ValueError, match="escapes"):
        resolve_run_output(Path("../a"), repository_root=ROOT, run_root=tmp_path)
    with pytest.raises(ValueError, match="absolute"):
        resolve_run_output(Path("a"), repository_root=ROOT, run_root=Path("relative"))


def test_explicit_storage_root_cannot_authorize_another_checkout_or_symlink(tmp_path):
    foreign = tmp_path / "other"
    foreign.mkdir()
    (foreign / ".git").write_text("gitdir: other-worktree")
    for root in (tmp_path, foreign):
        with pytest.raises(ValueError, match="another worktree"):
            resolve_run_output(foreign / "run", repository_root=ROOT, run_root=root)
    alias = tmp_path / "alias"
    alias.symlink_to(foreign, target_is_directory=True)
    with pytest.raises(ValueError, match="another worktree"):
        resolve_run_output(alias / "run", repository_root=ROOT, run_root=tmp_path)
    with patch("src.agent_runtime.run_paths.subprocess.check_output", return_value=(
            f"worktree {ROOT}\n\nworktree {tmp_path / 'registered'}\n")):
        with pytest.raises(ValueError, match="another worktree"):
            resolve_run_output(tmp_path / "registered/run", repository_root=ROOT, run_root=tmp_path)


def test_default_cli_output_keeps_cwd_semantics_and_still_refuses_foreign_cwd(tmp_path, monkeypatch):
    repository = tmp_path / "repository"
    repository.mkdir()
    (repository / ".git").mkdir()
    monkeypatch.setattr("src.agent_runtime.run_paths.subprocess.check_output",
                        lambda *args, **kwargs: f"worktree {repository}\n\n")
    local = repository / "local"
    local.mkdir()
    monkeypatch.chdir(local)
    assert resolve_run_output(Path("relative"), repository_root=repository) == local / "relative"
    monkeypatch.chdir(tmp_path)
    with pytest.raises(ValueError, match="escapes"):
        resolve_run_output(Path("relative"), repository_root=repository)


def test_configuration_output_keeps_repository_semantics_from_foreign_cwd(tmp_path, monkeypatch):
    from src.agent.runtime_configuration import load_configuration, argv_for
    from tests.current_runtime_fixtures import configuration as fixture_configuration
    configuration = fixture_configuration(provider="paratera", model="Qwen3.8-27B")
    case = configuration["cases"][0]
    case.pop("run_root")
    case["output"] = "runs/relative-config-output"
    path = tmp_path / "config.json"
    path.write_text(json.dumps(configuration))
    monkeypatch.chdir(ROOT.parent)
    argv = argv_for(load_configuration(path)["cases"][0])
    assert argv[argv.index("--out") + 1] == str(ROOT / case["output"])


@pytest.mark.parametrize("policy,kept", [("all", [True, True]), ("current_tool_chain", [True, True])])
def test_reasoning_policy_changes_only_wire_history_and_keeps_current_parallel_batch(tmp_path, policy, kept):
    responses = [response(("old", "view", {}), reasoning=True),
                 response(("new1", "view", {}), ("new2", "error", {}), reasoning=True),
                 response(text="done", reasoning=True)]
    engine = runtime(tmp_path, responses)
    engine.reasoning_history = policy
    with engine.store:
        assert asyncio.run(engine.run(MESSAGES))["status"] == "completed"
        messages = json.loads(engine.adapter.requests[-1])["messages"]
        assistants = [m for m in messages if m["role"] == "assistant"]
        assert ["reasoning_content" in m for m in assistants] == kept
        assert [c["id"] for c in assistants[-1]["tool_calls"]] == ["new1", "new2"]
        assert [m["tool_call_id"] for m in messages if m["role"] == "tool"] == ["old", "new1", "new2"]
        assert all("reasoning_content" in m for m in engine.messages if m["role"] == "assistant")
        raw = [engine.store.resolve(e.payload.raw_response) for e in engine.store.events
               if e.payload.event_type == "model_response"]
        assert raw == responses
        engine.store.validate()


def test_new_user_turn_ends_reasoning_chain_but_tool_images_do_not():
    assistant = response(("a", "view", {}), reasoning=True)["choices"][0]["message"]
    messages = [assistant, {"role": "tool", "tool_call_id": "a", "content": "result"},
                {"role": "user", "content": "next"}]
    sources = [SourceRef(source_id="response", source_kind="history", event_id="event-response", locator="response"),
               SourceRef(source_id="result", source_kind="tool", event_id="event-result", locator="result"),
               SourceRef(source_id="user", source_kind="user", event_id="event-user", locator="user")]
    original = copy.deepcopy(messages)
    assert "reasoning_content" not in reasoning_history_messages(messages, "current_tool_chain", sources)[0]
    sources[-1] = sources[1]
    assert reasoning_history_messages(messages, "current_tool_chain", sources) == original
    assert messages == original
    with pytest.raises(ValueError, match="reasoning_history"):
        reasoning_history_messages(messages, "unknown")


def test_completed_older_chain_is_removed_but_all_current_batches_survive():
    older = response(("old", "view", {}), reasoning=True)["choices"][0]["message"]
    final = response(text="previous answer", reasoning=True)["choices"][0]["message"]
    current = response(("new", "view", {}), reasoning=True)["choices"][0]["message"]
    history = [older, {"role": "tool", "content": "old result"}, final,
               {"role": "user", "content": "new question"}, current,
               {"role": "tool", "content": "new result"}, copy.deepcopy(current)]
    selected = reasoning_history_messages(history, "current_tool_chain")
    assert ["reasoning_content" in m for m in selected if m["role"] == "assistant"] == [False, False, True, True]
    assert selected[2]["content"] == "previous answer"


def test_anthropic_wire_bytes_do_not_change_with_chat_reasoning_policy(tmp_path):
    from src.agent_runtime.loop import RunLimits
    from test_runtime_anthropic import native_engine, native_response
    wires = []
    for policy in ("all", "current_tool_chain"):
        directory = tmp_path / policy
        directory.mkdir()
        engine = native_engine(directory, [native_response(("a", "view", {})),
            native_response(("b", "error", {})), native_response(text="done")],
            limits=RunLimits(model_calls=3, tool_calls=3, seconds=60, tokens=500000))
        engine.reasoning_history = policy
        with engine.store:
            assert asyncio.run(engine.run(MESSAGES))["status"] == "completed"
            wires.append(engine.adapter.requests)
    assert wires[0] == wires[1]


@pytest.mark.parametrize("limiting", ["tokens", "money_cny", "money_usd", "calls", "tool_calls", "seconds"])
def test_tool_tail_uses_tightest_dimension_and_does_not_invent_disabled_caps(tmp_path, limiting):
    from scripts.tool_scripts.bim_agent_budget import time_status
    run = tmp_path / "bim"
    (run / ".harness_tmp").mkdir(parents=True)
    manifest = dict(started_epoch=1000, deadline_epoch=7000, floor_plan_images=["floor.png"])
    toolkit = SimpleNamespace(run=run, manifest=manifest, readonly=False)
    dimensions = {"seconds": {"remaining": "2466", "remaining_fraction": .411}}
    dimensions[limiting] = {"remaining": "10", "remaining_fraction": .1}
    (run / ".harness_tmp/budget_status.json").write_text(json.dumps(dict(
        schema_version=1, run_directory=str(run.resolve()), started_epoch=1000, dimensions=dimensions)))
    status = time_status(toolkit, now=4534)
    assert status["tightest_dimension"] == limiting
    assert "最紧额度已过半" in status["line"] and "floor.png" in status["line"]
    assert "剩余不足15%" in status["line"] and "有界复核已列严重问题" in status["line"]
    assert status["line"].count("。") == 1
    assert set(status["dimensions"]) == set(dimensions)
    toolkit.readonly = True
    readonly = time_status(toolkit, now=4534)["line"]
    assert "返回已有观察与未核项" in readonly and "草稿" not in readonly


def test_budget_bridge_reads_settled_usage_and_counts_pending_tool_once(tmp_path):
    from src.agent.runtime_entry import publish_tool_budget
    engine = runtime(tmp_path, [response(("s", "save", {"v": 1})), response(text="done")])
    engine.tools.run_directory = engine.tools.directory
    seen = []
    def publish(engine):
        publish_tool_budget(engine)
        seen.append(json.loads((engine.tools.directory / ".harness_tmp/budget_status.json").read_text()))
    engine.tool_budget_update = publish
    with engine.store:
        assert asyncio.run(engine.run(MESSAGES))["status"] == "completed"
        status = seen[0]
        assert status["scope"] == "runtime_managed_requests_only"
        assert "external_coordinator_usage" not in status
        assert status["dimensions"]["calls"]["remaining"] == "3"
        assert status["dimensions"]["tool_calls"]["remaining"] == "4"
        assert 0 < int(status["dimensions"]["tokens"]["remaining"]) < 100000
        assert "money_cny" not in status["dimensions"]


def test_explicit_registry_still_rejects_changed_source_and_relative_override(tmp_path, monkeypatch):
    from src.agent_runtime.agent_registry import agent_version_record, load_agent_registry, AgentVersionMismatch
    registry = load_agent_registry(ROOT)
    current = registry["domain_versions"][registry["current_domain_version"]]
    current["files"][next(iter(current["files"]))]["sha256"] = "0" * 64
    path = tmp_path / "registry.json"
    path.write_text(json.dumps(registry))
    monkeypatch.setenv("BIM_AGENT_REGISTRY_PATH", str(path))
    with pytest.raises(AgentVersionMismatch, match="mismatch"):
        agent_version_record(ROOT)
    monkeypatch.setenv("BIM_AGENT_REGISTRY_PATH", "relative.json")
    with pytest.raises(ValueError, match="absolute"):
        load_agent_registry(ROOT)
