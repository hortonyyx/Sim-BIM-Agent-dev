"""Role context remains lossless across capacity policies and restoration."""

import base64
import io
import json

from PIL import Image
import pytest

from src.agent.runtime_roles.config import RoleContextOverrides
from src.agent.runtime_roles.context_policy import (context_policy_from_effective,
    effective_role_context, role_context_policy)
from src.agent_runtime.anthropic import convert_messages
from src.agent_runtime.context import ContextManager, ContextPolicy, StateEntry
from src.agent_runtime.store import json_bytes
from test_runtime_context import _event_source, _store


def _image_message():
    buffer = io.BytesIO()
    Image.new("RGB", (4, 4), "blue").save(buffer, format="PNG")
    raw = buffer.getvalue()
    return raw, {"role": "user", "content": [{"type": "image_url", "image_url": {
        "url": "data:image/png;base64," + base64.b64encode(raw).decode()}}]}


def _interaction(manager, source, index, picture):
    manager.append({"role": "assistant", "content": "inspect", "tool_calls": [{
        "id": f"call-{index}", "type": "function", "function": {"name": "view", "arguments": "{}"}}]}, source)
    manager.append({"role": "tool", "tool_call_id": f"call-{index}",
                    "content": f"Observation {index}: " + "wall " * 90}, source)
    if picture is not None:
        manager.append(picture, source)


def _wire(projection):
    system, _, messages, _ = convert_messages(projection.messages, projection.sources)
    return json_bytes({"system": system, "messages": messages})


def test_explicit_compaction_preserves_exact_history_and_restores_projection(tmp_path):
    with _store(tmp_path) as store:
        manager = ContextManager(store, policy=ContextPolicy(compact_at_tokens=550))
        source = _event_source(store, "original input")
        manager.append({"role": "system", "content": "guide"}, source)
        manager.append({"role": "user", "content": "task"}, source)
        full_record = [{"task_id": "reader", "runtime": {"result": "full original record"}}]
        manager.set_state(StateEntry(key="reader-artifacts", category="artifact_version",
            value=full_record, epistemic_status="computed", source_refs=(source,)))
        picture = _image_message()[1]
        for index in range(9):
            _interaction(manager, source, index, picture if index % 3 == 0 else None)
            manager.project()
        assert manager._archived_history_ids
        assert len(manager.history) == 23
        projected = manager.project()
        assert projected.messages[:2] == [
            {"role": "system", "content": "guide"}, {"role": "user", "content": "task"}]
        restored = ContextManager.load(store, manager.dump())
        assert restored.full_history() == manager.full_history()
        assert restored.state[0].value == full_record
        assert _wire(restored.project()) == _wire(projected)
        store.validate()


@pytest.mark.parametrize("role", ["coordinator", "plan_reader", "elevation_reader"])
def test_role_compaction_preserves_full_records_references_images_and_checkpoint(tmp_path, role):
    with _store(tmp_path) as store:
        source = _event_source(store, "artifact evidence")
        artifact = store.put_json({"plan": {"openings": ["door-1"]}, "evidence": "original pixels"})
        full = [{"task_id": "plan_F1", "role_id": "plan_reader", "target": "F1", "status": "completed",
                 "artifact": artifact.model_dump(mode="json"), "runtime": {"history": "large ledger " * 1000},
                 "validation": {"unresolved": ["height uncertain"]}},
                {"task_id": "elev_N", "status": "failed", "reason": "image missing"}]
        manager = ContextManager(store, policy=role_context_policy(role, compact_at_tokens=1500,
            state_item_fields={"reader-artifacts": ("task_id", "role_id", "target", "status", "artifact", "reason")}))
        manager.append({"role": "system", "content": "guide"}, source)
        manager.append({"role": "user", "content": "Read artifacts using read_role_artifact; complete status via role_state."}, source)
        manager.set_state(StateEntry(key="reader-artifacts", category="artifact_version", value=full,
            epistemic_status="computed", source_refs=(source,)))
        raw, picture = _image_message()
        for i in range(14):
            _interaction(manager, source, i, picture if i == 0 else None)
            manager.project()
        assert manager._archived_history_ids
        projected = manager.project()
        assert "large ledger" not in json.dumps(projected.messages)
        assert "image missing" in json.dumps(projected.messages)
        assert artifact.sha256 in json.dumps(projected.messages)
        assert "read_role_artifact" in json.dumps(projected.messages)
        assert manager.state[0].value == full
        assert json.loads(store.get_bytes(artifact))["plan"]["openings"] == ["door-1"]
        saved = manager.dump()
        restored = ContextManager.load(store, saved)
        assert restored.full_history() == manager.full_history()
        assert restored.project().messages == projected.messages
        assert restored.state == manager.state
        removed = next(image for image in restored.images if not image.active)
        assert restored.retrieve_image(removed.view_id, removed.image.sha256) == raw
        retrieved = restored.project()
        assert picture["content"][0] in retrieved.messages[-2]["content"]
        assert restored.full_history()[0][:2] == manager.full_history()[0][:2]
        store.validate()


def test_reader_policies_default_to_automatic_capacity_and_allow_explicit_overrides():
    for role in ("plan_reader", "elevation_reader"):
        assert role_context_policy(role) == ContextPolicy(compact_at_tokens=None, compact_to_ratio=0.3)
    policy = role_context_policy("coordinator", compact_at_tokens=75_000, max_images=3)
    assert policy.compact_at_tokens == 75_000 and policy.max_images == 3
    assert policy.compact_to_ratio == 0.3


def test_effective_context_uses_role_defaults_then_global_and_role_overrides():
    assert effective_role_context("coordinator").compact_at_tokens is None
    assert effective_role_context("plan_reader").compact_at_tokens is None
    value = effective_role_context("plan_reader", global_defaults={
        "context_tokens": 220_000, "compact_at_tokens": 120_000,
        "max_images": 5, "max_image_bytes": 9_000_000,
    }, role_overrides=RoleContextOverrides(
        context_tokens=180_000, compact_at_tokens=90_000, max_images=2))
    assert value.model_dump(mode="json") == {
        "context_tokens": 180_000,
        "compact_at_tokens": 90_000, "compact_to_ratio": 0.3,
        "max_images": 2, "max_image_bytes": 9_000_000,
    }
    policy = context_policy_from_effective("plan_reader", value)
    assert policy.compact_at_tokens == 90_000
    assert policy.max_images == 2 and policy.max_image_bytes == 9_000_000


def test_explicit_role_null_restores_auto_but_omission_inherits_global():
    global_defaults = {"context_tokens": 220_000, "compact_at_tokens": 120_000}
    automatic = effective_role_context("plan_reader", global_defaults=global_defaults,
        role_overrides=RoleContextOverrides(context_tokens=None, compact_at_tokens=None))
    assert automatic.context_tokens is None and automatic.compact_at_tokens is None
    inherited = effective_role_context("plan_reader", global_defaults=global_defaults,
        role_overrides=RoleContextOverrides(max_images=2))
    assert inherited.context_tokens == 220_000 and inherited.compact_at_tokens == 120_000
