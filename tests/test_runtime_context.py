from __future__ import annotations

import base64
import asyncio
import json
from pathlib import Path
from decimal import Decimal

import pytest

from src.agent_runtime.context import (
    ContextManager,
    ContextPolicy,
    StateEntry,
    SummaryCandidate,
    SummaryStatement,
)
from src.agent_runtime.store import EventStore
from src.harness_contracts import BudgetAmounts, RunLifecyclePayload, SourceRef


def _store(tmp_path: Path) -> EventStore:
    return EventStore(
        tmp_path / "run",
        run_id="run-context",
        task_id="task-context",
        budget_limit=BudgetAmounts(tokens=100_000, calls=100, seconds=Decimal("1000")),
    )


def _event_source(store: EventStore, name: str) -> SourceRef:
    event = store.append(RunLifecyclePayload(action="start", reason=name))
    return SourceRef(
        source_id=name,
        source_kind="runtime",
        locator=f"events.jsonl:{event.sequence + 1}",
        event_id=event.event_id,
    )


def _image_message(data: bytes) -> dict:
    encoded = base64.b64encode(data).decode("ascii")
    return {
        "role": "user",
        "content": [
            {"type": "text", "text": "inspect"},
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{encoded}"}},
        ],
    }


def _append_image_batch(manager, store, index, images):
    source = _event_source(store, f"tool-batch-{index}")
    call_id = f"call-{index}"
    manager.append({"role": "assistant", "content": None, "tool_calls": [{
        "id": call_id, "type": "function", "function": {"name": "view", "arguments": "{}"}}]}, source)
    manager.append({"role": "tool", "tool_call_id": call_id, "content": f"unchanged result {index}"}, source)
    keys, blocks = [], []
    for view, data in images:
        keys.append(manager.register_image(view, store.put_bytes(data, "image/png"), source=source))
        blocks.extend(_image_message(data)["content"])
    manager.append({"role": "user", "content": blocks}, SourceRef(source_id="tool-images", source_kind="tool",
        locator=source.event_id, event_id=source.event_id,
        blob=store.put_json({"origin_events": [source.event_id]})), image_keys=tuple(keys))
    return source.event_id


def _projected_images(projection):
    return [base64.b64decode(block["image_url"]["url"].split(",", 1)[1])
            for message in projection.messages if isinstance(message.get("content"), list)
            for block in message["content"] if block.get("type") == "image_url"]


def test_image_history_projects_occurrences_without_losing_text_originals_or_audit(tmp_path):
    assert "image_history_batches" not in ContextPolicy().model_dump(mode="json")
    with _store(tmp_path) as store:
        manager = ContextManager(store, policy=ContextPolicy(image_history_batches=1))
        source = _event_source(store, "initial")
        initial = [f"original-{i}".encode() for i in range(6)]
        manager.append({"role": "system", "content": "guidance"}, source)
        manager.append({"role": "user", "content": [block for raw in initial
            for block in _image_message(raw)["content"]]}, source)
        for index in range(8):
            # Repeated original bytes in old tool positions must not stay merely
            # because the initial input's hash is still resident.
            _append_image_batch(manager, store, index, [("original-tool", initial[0]),
                (f"diagnostic-{index}", f"diagnostic-{index}".encode())])
            before = manager.full_history()
            projection = manager.project(token_estimator=lambda _: 1, consume_retrievals=False)
            assert _projected_images(projection) == [*initial, initial[0], f"diagnostic-{index}".encode()]
            assert manager.full_history() == before and not projection.omitted_history_ids
            assert sum(message.get("role") == "tool" for message in projection.messages) == index + 1
        texts = [block["text"] for message in projection.messages if isinstance(message.get("content"), list)
                 for block in message["content"] if block.get("type") == "text" and "archived_image" in block["text"]]
        assert len(texts) == 14
        archived = json.loads(texts[0])["archived_image"]
        reference = archived["read"]
        history = next(row for row in manager.history if row.history_id == archived["history_id"])
        registered = next(row for row in manager.images if row.key == manager.image_key(
            archived["view_id"], archived["sha256"]))
        assert archived["history_source_event_id"] == history.source.event_id
        assert archived["registered_source_event_id"] == registered.source.event_id
        assert "source_event_id" not in archived
        assert reference["tool"] == "read_context_image"
        assert manager.retrieve_image(**reference["arguments"]) == initial[0]
        restored = ContextManager.load(store, manager.dump())
        assert restored.full_history() == manager.full_history()
        assert restored.full_history()[0][:-1] == before[0]
        assert _projected_images(restored.project(token_estimator=lambda _: 1))[-1] == initial[0]
        store.validate()


def test_image_history_retains_unpresented_batch_and_never_clips_new_images(tmp_path):
    with _store(tmp_path) as store:
        manager = ContextManager(store, policy=ContextPolicy(image_history_batches=1,
            max_images=1, max_image_bytes=1))
        source = _event_source(store, "initial")
        for message in [{"role": "system", "content": "guide"}, {"role": "user", "content": "task"}]:
            manager.append(message, source)
        pending = _append_image_batch(manager, store, 1, [("pending", b"undelivered")])
        _append_image_batch(manager, store, 2, [(f"fresh-{i}", bytes([i]) * 20) for i in range(8)])
        projection = manager.project(token_estimator=lambda _: 100, target_tokens=1,
            force_compact=True, undelivered_image_event_ids=(pending,))
        assert _projected_images(projection) == [b"undelivered", *[bytes([i]) * 20 for i in range(8)]]
        assert not projection.omitted_history_ids  # Protected indivisible batches exceed the soft target.
        store.validate()


def test_image_history_exact_old_versions_are_checkpointed_and_cross_task_lookup_fails(tmp_path):
    with _store(tmp_path) as store:
        manager = ContextManager(store, policy=ContextPolicy(image_history_batches=1))
        source = _event_source(store, "initial")
        for message in [{"role": "system", "content": "guide"}, {"role": "user", "content": "task"}]:
            manager.append(message, source)
        for index in range(3):
            _append_image_batch(manager, store, index, [("same-view", f"version-{index}".encode())])
        manager.project(token_estimator=lambda _: 1)
        versions = sorted(manager.images, key=lambda image: image.registration_index)
        for image in versions[:2]:
            assert manager.retrieve_image(image.view_id, image.image.sha256) == store.get_bytes(image.image)
        restored = ContextManager.load(store, manager.dump())
        for _ in range(2):
            assert _projected_images(restored.project(token_estimator=lambda _: 1, consume_retrievals=False)) == [
                b"version-2", b"version-0", b"version-1"]
        assert len(restored.acknowledge_projection()) == 2
        # A subsequent image batch retires the previous batch, including the
        # explicit reads whose one-response pins have now been consumed.
        _append_image_batch(restored, store, 3, [("same-view", b"version-2")])
        assert _projected_images(restored.project(token_estimator=lambda _: 1)) == [b"version-2"]
        with pytest.raises(KeyError):
            restored.retrieve_image("same-view", "0" * 64)
        foreign = ContextManager(store.for_task("other", "task-context"), policy=manager.policy)
        with pytest.raises(KeyError):
            foreign.retrieve_image(versions[0].view_id, versions[0].image.sha256)
        # Integrity is checked by EventStore on every exact retrieval (covered
        # independently by the store corruption tests); no arbitrary URI is accepted.
        with pytest.raises(KeyError):
            restored.retrieve_image("../../other-task/private.png", versions[0].image.sha256)
        store.validate()


def _state(source: SourceRef, **updates) -> StateEntry:
    fields = {
        "key": "requirement.keep_rooms",
        "category": "user_requirement",
        "value": "keep every physical room",
        "epistemic_status": "user_stated",
        "source_refs": (source,),
    }
    fields.update(updates)
    return StateEntry(**fields)


def test_lossless_history_is_separate_from_deterministic_projection_and_checkpoint(tmp_path):
    with _store(tmp_path) as store:
        manager = ContextManager(
            store,
            policy=ContextPolicy(
                preserve_initial_messages=1, compact_at_tokens=30,
                ),
        )
        original = []
        for index in range(6):
            message = {
                "role": "system" if index == 0 else "user",
                "content": "same large result" * 10 if index in {2, 3} else f"message {index}",
            }
            original.append(message)
            manager.append(
                message,
                _event_source(store, f"message-{index}"),
                kind="tool_result" if index in {2, 3} else "message",
            )
        manager.set_state(_state(store.source("user-rule", {"rule": "keep rooms"}, kind="user")))

        projection = manager.project()
        assert manager.full_history()[0] == original
        assert projection.included_history_ids == ("history-000000", "history-000004", "history-000005")
        assert set(projection.omitted_history_ids) == {
            "history-000001",
            "history-000002",
            "history-000003",
        }
        assert any("Deterministic context compaction" in str(message) for message in projection.messages)
        assert projection.checklist.entries("user_requirement")[0].key == "requirement.keep_rooms"
        assert any(event.payload.event_type == "context" and event.payload.action == "compact"
                   for event in store.events)

        checkpoint = manager.dump()
        assert "same large result" not in json.dumps(checkpoint)
        restored = ContextManager.load(store, checkpoint)
        assert restored.full_history() == manager.full_history()
        assert restored.checklist() == manager.checklist()
        store.validate()


def test_old_summary_versions_and_duplicate_large_results_are_removed_deterministically(tmp_path):
    with _store(tmp_path) as store:
        manager = ContextManager(
            store,
            policy=ContextPolicy(preserve_initial_messages=1, compact_at_tokens=30,
                                 ),
        )
        first = manager.append(
            {"role": "system", "content": "system"}, _event_source(store, "system")
        )
        old = manager.append(
            {"role": "system", "content": "old summary"},
            _event_source(store, "old-summary"), kind="summary", summary_slot="rolling",
        )
        duplicate = {"role": "user", "content": "large-result" * 10}
        duplicate_old = manager.append(
            duplicate, _event_source(store, "duplicate-old"), kind="tool_result"
        )
        duplicate_new = manager.append(
            duplicate, _event_source(store, "duplicate-new"), kind="tool_result"
        )
        latest = manager.append(
            {"role": "system", "content": "latest summary"},
            _event_source(store, "latest-summary"), kind="summary", summary_slot="rolling",
        )

        projection = manager.project()
        assert first in projection.included_history_ids
        assert latest in projection.included_history_ids
        assert old in projection.omitted_history_ids
        assert duplicate_old in projection.omitted_history_ids
        # Token pressure archives both old results; the durable history keeps their exact bytes.
        assert duplicate_new in projection.omitted_history_ids
        assert len(manager.history) == 5


def test_multi_tool_batch_is_atomic_under_token_pressure_and_keeps_exact_envelopes(tmp_path):
    with _store(tmp_path) as store:
        manager = ContextManager(
            store,
            policy=ContextPolicy(preserve_initial_messages=1, compact_at_tokens=20,
                                 ),
        )
        manager.append({"role": "system", "content": "system"}, _event_source(store, "system"))
        assistant = {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {"id": "call-a", "type": "function",
                 "function": {"name": "view", "arguments": "{}"}},
                {"id": "call-b", "type": "function",
                 "function": {"name": "view", "arguments": "{}"}},
            ],
        }
        manager.append(assistant, _event_source(store, "assistant-batch"))
        repeated = "the same complete large result" * 4
        source_a = _event_source(store, "tool-a")
        source_b = _event_source(store, "tool-b")
        manager.append(
            {"role": "tool", "tool_call_id": "call-a", "content": repeated}, source_a
        )
        manager.append(
            {"role": "tool", "tool_call_id": "call-b", "content": repeated}, source_b
        )
        image_bytes = b"tool-batch-image"
        image_ref = store.put_bytes(image_bytes, "image/png")
        image_source = _event_source(store, "tool-images")
        image_key = manager.register_image("view-batch", image_ref, source=image_source)
        manager.append(_image_message(image_bytes), image_source, image_keys=(image_key,))

        projection = manager.project()
        assert [message["role"] for message in projection.messages] == [
            "system", "assistant", "tool", "tool", "user"
        ]
        tool_messages = [message for message in projection.messages if message["role"] == "tool"]
        assert {message["tool_call_id"] for message in tool_messages} == {"call-a", "call-b"}
        assert all(message["content"] == repeated for message in tool_messages)
        assert projection.sources[2].event_id == source_a.event_id
        assert projection.sources[3].event_id == source_b.event_id
        assert not projection.omitted_history_ids  # latest whole batch cannot be split to meet target


def test_incomplete_tool_batch_is_rejected_instead_of_sending_invalid_history(tmp_path):
    with _store(tmp_path) as store:
        manager = ContextManager(
            store, policy=ContextPolicy(preserve_initial_messages=0)
        )
        manager.append(
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {"id": "call-a", "type": "function",
                     "function": {"name": "view", "arguments": "{}"}},
                    {"id": "call-b", "type": "function",
                     "function": {"name": "view", "arguments": "{}"}},
                ],
            },
            _event_source(store, "assistant-incomplete"),
        )
        manager.append(
            {"role": "tool", "tool_call_id": "call-a", "content": "only one result"},
            _event_source(store, "only-tool"),
        )
        with pytest.raises(ValueError, match="ended before all tool replies"):
            manager.project()


def test_image_policy_records_only_transitions_and_retrieves_exact_identity(tmp_path):
    with _store(tmp_path) as store:
        manager = ContextManager(
            store,
            policy=ContextPolicy(preserve_initial_messages=0, compact_at_tokens=1,
                                 pinned_tags=("current_floor",), max_images=2),
        )
        old_bytes = b"old-image-bytes"
        current_bytes = b"current-image-bytes"
        old_ref = store.put_bytes(old_bytes, "image/png")
        current_ref = store.put_bytes(current_bytes, "image/png")
        old_source = _event_source(store, "old-view")
        current_source = _event_source(store, "current-view")
        old_key = manager.register_image("view-old", old_ref, source=old_source, tags=("old_floor",))
        current_key = manager.register_image(
            "view-current", current_ref, source=current_source, tags=("current_floor",)
        )
        manager.append(_image_message(old_bytes), old_source, image_keys=(old_key,))
        manager.append(_image_message(current_bytes), current_source, image_keys=(current_key,))

        projection = manager.project(required_tags=("current_floor",))
        decisions = [
            event for event in store.events
            if event.payload.event_type == "context" and event.payload.action in {"retain_image", "remove_image"}
        ]
        assert [(event.payload.action, event.payload.view_id) for event in decisions] == [("remove_image", "view-old")]
        assert next(image for image in manager.images if image.key == current_key).active
        assert all(event.payload.before and event.payload.after and event.payload.details
                   for event in decisions[-2:])
        assert len(projection.decision_event_ids) >= 2
        count = len(store.events)
        assert manager.project(required_tags=("current_floor",)).messages == projection.messages
        assert len(store.events) == count
        assert base64.b64encode(old_bytes).decode("ascii") not in json.dumps(projection.messages)
        checkpoint = manager.dump()
        assert base64.b64encode(old_bytes).decode("ascii") not in json.dumps(checkpoint)
        restored = ContextManager.load(store, checkpoint)
        assert restored.full_history() == manager.full_history()

        # A later request that explicitly needs the old view records retrieval
        # before reconstructing the request; it is not mislabeled as retention.
        assert manager.retrieve_image("view-old", old_ref.sha256) == old_bytes
        second = manager.project(required_view_ids=("view-old",))
        second_events = [store.events_by_id[event_id] if hasattr(store, "events_by_id") else
                         next(event for event in store.events if event.event_id == event_id)
                         for event_id in second.decision_event_ids]
        assert any(event.payload.action == "retrieve_image" and event.payload.view_id == "view-old"
                   for event in store.events)

        assert manager.retrieve_image("view-old", old_ref.sha256) == old_bytes
        retrieve = [event for event in store.events
                    if event.payload.event_type == "context"
                    and event.payload.action == "retrieve_image"][-1]
        assert retrieve.payload.removal_event_id in {event.event_id for event in decisions}
        with pytest.raises(KeyError, match="unknown image identity"):
            manager.retrieve_image("view-old", "0" * 64)
        store.validate()


def test_image_retention_under_token_pressure_keeps_latest_original(tmp_path):
    with _store(tmp_path) as store:
        manager = ContextManager(
            store,
            policy=ContextPolicy(preserve_initial_messages=0, compact_at_tokens=1,
                                 max_images=1, max_image_bytes=100),
        )
        data = b"same-image"
        ref = store.put_bytes(data, "image/png")
        source_a = _event_source(store, "image-a")
        source_b = _event_source(store, "image-b")
        key_a = manager.register_image("view-a", ref, source=source_a)
        key_b = manager.register_image("view-b", ref, source=source_b)
        manager.append(_image_message(data), source_a, image_keys=(key_a,))
        manager.append(_image_message(data), source_b, image_keys=(key_b,))

        manager.project()
        records = {image.key: image for image in manager.images}
        assert not records[key_a].active
        assert records[key_b].active
        projected = manager.project()
        image_blocks = [
            block
            for message in projected.messages
            for block in (message.get("content") if isinstance(message.get("content"), list) else [])
            if block.get("type") == "image_url"
        ]
        assert len(image_blocks) == 1
        detail_payloads = [
            json.loads(store.get_bytes(event.payload.details))
            for event in store.events
            if event.payload.event_type == "context" and event.payload.details
        ]
        assert any("token-threshold compaction" in item.get("reason", "")
                   for item in detail_payloads)


def test_summary_validation_cannot_promote_inference_or_become_only_geometry_source(tmp_path):
    with _store(tmp_path) as store:
        manager = ContextManager(store)
        history_source = _event_source(store, "observation")
        history_id = manager.append(
            {"role": "user", "content": "window seems 1.2 m wide"}, history_source
        )
        inferred = StateEntry(
            key="dimension.window-W1.width",
            category="dimension",
            value=1.2,
            epistemic_status="inferred",
            source_refs=(history_source,),
        )
        manager.set_state(inferred)
        bad_statement = SummaryStatement(
            state_key=inferred.key,
            value=1.2,
            epistemic_status="observed",
            source_refs=inferred.source_refs,
        )
        bad = SummaryCandidate(
            covered_history_ids=(history_id,),
            statements=(bad_statement,),
            text=ContextManager.render_summary((bad_statement,)),
        )
        with pytest.raises(ValueError, match="cannot change"):
            manager.validate_summary(bad)

        statement = SummaryStatement(
            state_key=inferred.key,
            value=inferred.value,
            epistemic_status=inferred.epistemic_status,
            source_refs=inferred.source_refs,
        )
        good = SummaryCandidate(
            covered_history_ids=(history_id,),
            statements=(statement,),
            text=ContextManager.render_summary((statement,)),
        )
        assert manager.validate_summary(good) == good
        summary_history = manager.compact_with_summary(good)
        assert summary_history.startswith("history-")

        summary_event = next(
            event for event in reversed(store.events)
            if event.payload.event_type == "context"
            and event.payload.action == "compact"
            and event.payload.details
            and json.loads(store.get_bytes(event.payload.details)).get("kind")
            == "validated_model_summary"
        )
        disguised_blob_source = SourceRef(
            source_id="ordinary-runtime-capture",
            source_kind="runtime",
            locator=summary_event.payload.summary.uri,
            blob=summary_event.payload.summary,
        )
        disguised_event_source = SourceRef(
            source_id="ordinary-runtime-event",
            source_kind="runtime",
            locator=f"events.jsonl:{summary_event.sequence + 1}",
            event_id=summary_event.event_id,
        )
        for index, disguised in enumerate((disguised_blob_source, disguised_event_source)):
            with pytest.raises(ValueError, match="cannot exist only in a summary"):
                manager.set_state(StateEntry(
                    key=f"geometry.disguised-{index}", category="geometry",
                    value={"x": 1}, epistemic_status="inferred",
                    source_refs=(disguised,),
                ))

        generated = store.source("context-summary-only", {"claim": "made up"}, kind="generated")
        with pytest.raises(ValueError, match="cannot exist only in a summary"):
            manager.set_state(inferred.model_copy(update={
                "key": "geometry.space-S1", "category": "geometry",
                "source_refs": (generated,),
            }))


def test_state_update_invalidates_old_model_summary_without_losing_history(tmp_path):
    with _store(tmp_path) as store:
        manager = ContextManager(
            store, policy=ContextPolicy(preserve_initial_messages=0)
        )
        source = _event_source(store, "inference-v1")
        history_id = manager.append({"role": "user", "content": "estimate"}, source)
        first = StateEntry(
            key="dimension.window-W1.width", category="dimension", value=1.2,
            epistemic_status="inferred", source_refs=(source,),
        )
        manager.set_state(first)
        statement = SummaryStatement(
            state_key=first.key, value=first.value,
            epistemic_status=first.epistemic_status, source_refs=first.source_refs,
        )
        candidate = SummaryCandidate(
            covered_history_ids=(history_id,), statements=(statement,),
            text=ContextManager.render_summary((statement,)),
        )
        summary_id = manager.compact_with_summary(candidate)
        update_source = _event_source(store, "measurement-v2")
        manager.set_state(first.model_copy(update={
            "value": 1.3, "epistemic_status": "observed",
            "source_refs": (update_source,), "revision": 2,
        }))

        projection = manager.project()
        assert summary_id in projection.omitted_history_ids
        assert candidate.text not in json.dumps(projection.messages)
        assert manager.history[-1].history_id == summary_id
        assert manager.checklist().entries("dimension")[0].value == 1.3


def test_image_registration_is_idempotent_and_preserves_first_provenance(tmp_path):
    with _store(tmp_path) as store:
        manager = ContextManager(store)
        data = b"same registered image"
        ref = store.put_bytes(data, "image/png")
        original = _event_source(store, "original-image-source")
        replay = _event_source(store, "replayed-image-source")
        key = manager.register_image("view-one", ref, source=original, tags=("current_floor",))
        assert manager.register_image("view-one", ref, source=replay, tags=("other",)) == key
        saved = manager.images[0]
        assert saved.source == original
        assert saved.tags == ("current_floor",)
        assert saved.registration_index == 0


def test_exact_old_image_retrieval_is_persisted_until_one_projection(tmp_path):
    with _store(tmp_path) as store:
        manager = ContextManager(
            store, policy=ContextPolicy(preserve_initial_messages=0, compact_at_tokens=1)
        )
        old_bytes, new_bytes = b"old-view-version", b"new-view-version"
        old_ref = store.put_bytes(old_bytes, "image/png")
        new_ref = store.put_bytes(new_bytes, "image/png")
        old_source = _event_source(store, "view-old-version")
        new_source = _event_source(store, "view-new-version")
        old_key = manager.register_image("view-shared", old_ref, source=old_source)
        new_key = manager.register_image("view-shared", new_ref, source=new_source)
        manager.append(_image_message(old_bytes), old_source, image_keys=(old_key,))
        manager.append(_image_message(new_bytes), new_source, image_keys=(new_key,))
        manager.append({"role": "user", "content": "current work"}, _event_source(store, "current"))
        manager.project()
        assert not next(image for image in manager.images if image.key == old_key).active

        assert manager.retrieve_image("view-shared", old_ref.sha256) == old_bytes
        restored = ContextManager.load(store, manager.dump())
        projection = restored.project(consume_retrievals=False)
        wire = json.dumps(projection.messages)
        assert base64.b64encode(old_bytes).decode("ascii") in wire
        assert base64.b64encode(new_bytes).decode("ascii") not in wire
        # Summary/preflight projections and checkpoints do not consume the pin.
        second_preflight = restored.project(consume_retrievals=False)
        assert base64.b64encode(old_bytes).decode("ascii") in json.dumps(second_preflight.messages)
        restored = ContextManager.load(store, restored.dump())
        third_preflight = restored.project(consume_retrievals=False)
        assert base64.b64encode(old_bytes).decode("ascii") in json.dumps(third_preflight.messages)
        assert restored.acknowledge_projection() == (old_key,)
        restored.append({"role": "user", "content": "next interaction"}, _event_source(store, "next-work"))
        second = restored.project(consume_retrievals=False)
        assert base64.b64encode(old_bytes).decode("ascii") not in json.dumps(second.messages)


def test_pinned_images_survive_retention_target_and_remain_for_runtime_budget_check(tmp_path):
    with _store(tmp_path) as store:
        manager = ContextManager(
            store,
            policy=ContextPolicy(preserve_initial_messages=0, compact_at_tokens=1,
                                 pinned_tags=("unresolved",), max_images=1),
        )
        for index in range(2):
            data = f"pinned-{index}".encode()
            ref = store.put_bytes(data, "image/png")
            source = _event_source(store, f"pinned-{index}")
            key = manager.register_image(
                f"view-{index}", ref, source=source, tags=("unresolved",)
            )
            manager.append(_image_message(data), source, image_keys=(key,))
        projection = manager.project()
        assert len(_projected_images(projection)) == 2
        assert not projection.omitted_history_ids
        assert all(image.active for image in manager.images)


def test_checkpoint_suffix_context_events_can_be_replayed(tmp_path):
    with _store(tmp_path) as store:
        manager = ContextManager(
            store, policy=ContextPolicy(preserve_initial_messages=0, compact_at_tokens=1)
        )
        source = _event_source(store, "history")
        history_id = manager.append({"role": "user", "content": "task"}, source)
        state = _state(store.source("requirement", {"keep": True}, kind="user"))
        manager.set_state(state)
        image_bytes = b"suffix-image"
        image_ref = store.put_bytes(image_bytes, "image/png")
        image_source = _event_source(store, "suffix-image-source")
        image_key = manager.register_image("view-suffix", image_ref, source=image_source)
        manager.append(_image_message(image_bytes), image_source, image_keys=(image_key,))
        manager.append({"role": "user", "content": "new active work"}, _event_source(store, "new-work"))
        snapshot = manager.dump()
        checkpoint_sequence = store.events[-1].sequence

        manager.project()
        statement = SummaryStatement(
            state_key=state.key, value=state.value,
            epistemic_status=state.epistemic_status, source_refs=state.source_refs,
        )
        candidate = SummaryCandidate(
            covered_history_ids=(history_id,), statements=(statement,),
            text=ContextManager.render_summary((statement,)),
        )
        response_source = _event_source(store, "summary-model-response")
        manager.compact_with_summary(candidate, source=response_source)
        summary_event = next(
            event for event in reversed(store.events)
            if event.payload.event_type == "context"
            and event.payload.action == "compact"
            and event.payload.details
            and json.loads(store.get_bytes(event.payload.details)).get("kind")
            == "validated_model_summary"
        )
        assert response_source in summary_event.source_refs
        assert json.loads(store.get_bytes(summary_event.payload.details))[
            "response_event_id"
        ] == response_source.event_id
        suffix = [event for event in store.events if event.sequence > checkpoint_sequence]

        restored = ContextManager.load(store, snapshot)
        count = restored.replay_events(suffix)
        assert count >= 2
        restored_image = next(image for image in restored.images if image.key == image_key)
        assert not restored_image.active
        assert any(record.kind == "summary" for record in restored.history)
        # Replaying the same suffix is idempotent for the reconstructed summary.
        restored.replay_events(suffix)
        assert sum(record.kind == "summary" for record in restored.history) == 1


def test_summary_callback_is_injected_and_candidate_is_validated(tmp_path):
    with _store(tmp_path) as store:
        source = _event_source(store, "summary-source")
        entry = _state(store.source("requirement", {"value": "keep rooms"}, kind="user"))
        callback_requests = []

        async def callback(request):
            callback_requests.append(request)
            statement = SummaryStatement(
                state_key=entry.key,
                value=entry.value,
                epistemic_status=entry.epistemic_status,
                source_refs=entry.source_refs,
            )
            return SummaryCandidate(
                covered_history_ids=(request.history[0].history_id,),
                statements=(statement,),
                text=ContextManager.render_summary((statement,)),
            )

        manager = ContextManager(store, summary_callback=callback)
        history_id = manager.append({"role": "user", "content": "task"}, source)
        manager.set_state(entry)
        result = asyncio.run(manager.summarize((history_id,)))
        assert result.covered_history_ids == (history_id,)
        assert len(callback_requests) == 1


def test_context_core_has_no_building_imports():
    source = Path("src/agent_runtime/context.py").read_text(encoding="utf-8")
    assert "src.agent." not in source
    assert "scripts.tool_scripts" not in source
