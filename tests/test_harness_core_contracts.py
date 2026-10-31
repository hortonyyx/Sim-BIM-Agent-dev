from __future__ import annotations

import re
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError

from src.harness_contracts import (
    AdapterRequestPayload,
    BudgetAmounts,
    BudgetEventPayload,
    BudgetLedger,
    BudgetReservation,
    BudgetSettlement,
    ContextEventPayload,
    EstimatedCostUpperBound,
    EventEnvelope,
    EventLog,
    ContextImportPayload,
    HashedBlobRef,
    ImageTransmission,
    InjectedContent,
    InlineCapture,
    InputMaterialRequirement,
    MissingCapture,
    ModelBinding,
    ModelResponsePayload,
    ModelRouteRef,
    ParameterAudit,
    ParametersNotReported,
    ParametersReported,
    ParametersUnverified,
    PublicThinking,
    RemoteModelIdentity,
    ReportedCost,
    ResponseToolCall,
    ReturnRequirement,
    RoleDefinition,
    RootTask,
    RunLifecyclePayload,
    SourceRef,
    StateInspectionPayload,
    ThinkingSignature,
    ThinkingSummary,
    ThinkingTokenCount,
    ThinkingUnavailable,
    ToolExecutionPayload,
    ToolGrant,
    UsageMissing,
    UsageReported,
    ValidatedScope,
    VersionManifest,
    VersionStamp,
    authorize_tool_call,
    initial_model_for,
)


def _blob(name: str, digit: str = "a") -> HashedBlobRef:
    return HashedBlobRef(
        uri=f"artifact://{name}", media_type="application/json", sha256=digit * 64
    )


def _source(name: str, digit: str = "a") -> SourceRef:
    return SourceRef(
        source_id=name,
        source_kind="runtime",
        locator=f"records/{name}.json",
        blob=_blob(name, digit),
    )


def _stamp(name: str) -> VersionStamp:
    return VersionStamp(identifier=f"{name}-v1", evidence=_source(name))


def make_valid_request_payload() -> AdapterRequestPayload:
    return AdapterRequestPayload(
        adapter="openai-compatible-chat",
        final_request_body=InlineCapture(
            value={
                "model": "remote-alias",
                "messages": [
                    {"role": "system", "content": "runtime policy"},
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "image_url",
                                "image_url": "data:image/png;base64,AA==",
                            }
                        ],
                    },
                ],
                "temperature": 0.2,
            }
        ),
        injected_content=(
            InjectedContent(
                request_location="/messages/0/content",
                content=InlineCapture(value="runtime policy"),
                source=_source("runtime-policy"),
            ),
        ),
        images=(
            ImageTransmission(
                original=_blob("original-image", "b"),
                sent=_blob("sent-image", "c"),
                request_reference="/messages/1/content/0/image_url",
            ),
        ),
        parameters=ParameterAudit(
            requested={"temperature": 0.2, "reasoning_effort": "high"},
            provider_report=ParametersReported(values={"temperature": 0.2}),
            effect=ParametersUnverified(
                reason="service did not attest that every requested parameter took effect"
            ),
        ),
        versions=VersionManifest(
            code_commit=_stamp("code"),
            dependency_lock=_stamp("lock"),
            prompt=_stamp("prompt"),
            tool_definitions=_stamp("tools"),
            inference_parameters=_stamp("parameters"),
            model_route=_stamp("route"),
            remote_model=RemoteModelIdentity(
                route_id="route-main",
                remote_alias="remote-alias",
                alias_status="unverified",
            ),
        ),
    )


def _event(sequence: int, payload, *, event_id: str | None = None) -> EventEnvelope:
    return EventEnvelope(
        event_id=event_id or f"event-{sequence}",
        run_id="run-1",
        task_id="task-main",
        parent_task=RootTask(),
        sequence=sequence,
        occurred_at={"kind": "known", "value": datetime(2026, 10, 2, tzinfo=UTC)},
        payload=payload,
    )


def make_valid_role() -> RoleDefinition:
    return RoleDefinition(
        role_id="local_observer",
        responsibilities=("inspect supplied material", "return located observations"),
        tool_whitelist=(ToolGrant(tool_name="inspect_region", access="read"),),
        input_materials=(
            InputMaterialRequirement(name="evidence", media_type="image/png"),
        ),
        return_requirements=(
            ReturnRequirement(name="observations", schema_ref="schema://observation-v1"),
        ),
        budget=BudgetAmounts(tokens=4_000, calls=6),
        read_only=True,
    )


def make_valid_ledger() -> BudgetLedger:
    reservations = (
        BudgetReservation(
            reservation_id="child",
            purpose="child_task",
            amounts=BudgetAmounts(tokens=1_000, money_usd=Decimal("2.00"), calls=1),
            task_id="child-task",
        ),
        BudgetReservation(
            reservation_id="summary",
            purpose="context_summary",
            amounts=BudgetAmounts(tokens=500, money_usd=Decimal("1.00"), calls=1),
            task_id="task-main",
        ),
        BudgetReservation(
            reservation_id="retry",
            purpose="retry",
            amounts=BudgetAmounts(tokens=800, money_usd=Decimal("1.50"), calls=1),
            task_id="task-main",
        ),
    )
    settlements = (
        BudgetSettlement(
            reservation_id="child",
            actual=BudgetAmounts(
                tokens=720, money_usd=Decimal("1.25"), calls=1
            ),
            usage=UsageReported(raw_usage={"input_tokens": 600, "output_tokens": 120}),
            cost=ReportedCost(usd=Decimal("1.25")),
        ),
        BudgetSettlement(
            reservation_id="retry",
            actual=BudgetAmounts(calls=1),
            usage=UsageMissing(reason="provider omitted usage"),
            cost=EstimatedCostUpperBound(
                usd=Decimal("1.50"), reason="reservation retained as conservative cap"
            ),
        ),
    )
    return BudgetLedger(
        total_limit=BudgetAmounts(
            tokens=4_000, money_usd=Decimal("8.00"), calls=8
        ),
        reservations=reservations,
        settlements=settlements,
    )


def make_valid_event_log() -> EventLog:
    request = _event(0, make_valid_request_payload(), event_id="request")
    response = _event(
        1,
        ModelResponsePayload(
            request_event_id="request",
            visible_text=("I will inspect the supplied material.",),
            tool_calls=(
                ResponseToolCall(
                    call_id="call-read", tool_name="inspect_region", full_arguments={"x": 2}
                ),
            ),
            thinking=(
                PublicThinking(content="public reasoning"),
                ThinkingSummary(summary="brief reasoning summary"),
                ThinkingTokenCount(tokens=23),
                ThinkingSignature(signature="opaque-signature-not-content"),
            ),
            usage=UsageReported(raw_usage={"input_tokens": 50, "output_tokens": 20}),
            raw_response=InlineCapture(value={"id": "response-1"}),
        ),
        event_id="response",
    )
    unknown_write = _event(
        2,
        ToolExecutionPayload(
            call_id="write-1",
            tool_name="persist_value",
            full_arguments={"value": 7},
            raw_result=MissingCapture(reason="connection ended before result"),
            shown_result=MissingCapture(reason="no result was shown"),
            repeatability="non_idempotent_write",
            outcome="unknown",
            operation_key="set:value:7",
        ),
        event_id="unknown-write",
    )
    inspection = _event(
        3,
        StateInspectionPayload(
            purpose="unknown_write_recovery",
            target_event_id="unknown-write",
            persisted_state=_blob("state-before-retry", "d"),
            conclusion="not_applied",
        ),
        event_id="write-inspection",
    )
    retry = _event(
        4,
        RunLifecyclePayload(
            action="retry",
            reason="persisted state proves the write did not apply",
            retry_of_event_id="unknown-write",
            attempt=2,
            state_inspection_event_id="write-inspection",
        ),
        event_id="retry",
    )
    applied = _event(
        5,
        ToolExecutionPayload(
            call_id="write-2",
            tool_name="persist_value",
            full_arguments={"value": 7},
            raw_result=InlineCapture(value={"status": "applied"}),
            shown_result=InlineCapture(value="applied"),
            repeatability="non_idempotent_write",
            outcome="succeeded",
            operation_key="set:value:7",
            applied_write_id="mutation-7",
            retry_event_id="retry",
        ),
        event_id="applied-write",
    )
    resume_inspection = _event(
        6,
        StateInspectionPayload(
            purpose="resume",
            target_event_id="applied-write",
            persisted_state=_blob("resume-checkpoint", "e"),
            conclusion="safe_to_resume",
        ),
        event_id="resume-inspection",
    )
    resume = _event(
        7,
        RunLifecyclePayload(
            action="resume",
            reason="checkpoint matches persisted state",
            state_inspection_event_id="resume-inspection",
            checkpoint=_blob("resume-checkpoint", "e"),
            partial_artifacts=(_blob("partial-output", "f"),),
        ),
        event_id="resume",
    )
    return EventLog(
        mode="complete",
        events=(
            request,
            response,
            unknown_write,
            inspection,
            retry,
            applied,
            resume_inspection,
            resume,
        ),
    )


def test_valid_factories_cover_request_response_recovery_role_and_budget() -> None:
    log = make_valid_event_log()
    role = make_valid_role()
    ledger = make_valid_ledger()

    assert log.events[0].payload.versions.remote_model.alias_status == "unverified"
    assert log.events[0].payload.images[0].original.sha256 == "b" * 64
    assert authorize_tool_call(role, "inspect_region", "read").access == "read"
    assert {item.purpose for item in ledger.reservations} == {
        "child_task",
        "context_summary",
        "retry",
    }


def test_request_rejects_injected_content_or_image_pointer_not_in_final_body() -> None:
    valid = make_valid_request_payload().model_dump(mode="python")
    valid["injected_content"][0]["content"]["value"] = "different"
    with pytest.raises(ValidationError, match="differs from final request"):
        AdapterRequestPayload.model_validate(valid)

    valid = make_valid_request_payload().model_dump(mode="python")
    valid["images"][0]["request_reference"] = "/messages/9/image"
    with pytest.raises(ValidationError, match="request reference does not exist"):
        AdapterRequestPayload.model_validate(valid)


def test_thinking_states_are_distinct_and_usage_missing_is_not_zero() -> None:
    unavailable = ModelResponsePayload(
        request_event_id="historical-request",
        thinking=(ThinkingUnavailable(reason="provider exposed no thinking fields"),),
        usage=UsageMissing(reason="provider omitted raw usage"),
        raw_response=InlineCapture(value={"text": "done"}),
        visible_text=("done",),
    )
    assert unavailable.usage.kind == "missing"
    assert not hasattr(unavailable.usage, "input_tokens")

    with pytest.raises(ValidationError, match="cannot be combined"):
        ModelResponsePayload(
            request_event_id="request",
            thinking=(
                ThinkingUnavailable(reason="missing"),
                ThinkingSummary(summary="invented summary"),
            ),
            usage=UsageMissing(reason="missing"),
            raw_response=InlineCapture(value={}),
        )


def test_incomplete_legacy_records_are_rejected_by_current_contracts() -> None:
    with pytest.raises(ValidationError):
        SourceRef(source_id="old", source_kind="history", locator="old.json", blob={
            "kind": "historical_hash_missing", "uri": "old.json", "media_type": "application/json", "reason": "missing"})
    event = _event(0, RunLifecyclePayload(action="start", reason="test"))
    for field in ("occurred_at", "parent_task"):
        row = event.model_dump(mode="python")
        row[field] = {"kind": "historical_missing", "reason": "missing"}
        with pytest.raises(ValidationError):
            EventEnvelope.model_validate(row)
    with pytest.raises(ValidationError):
        EventLog(mode="excerpt", events=(event,))


def test_complete_log_requires_final_request_and_prior_request_event() -> None:
    payload = make_valid_request_payload().model_dump(mode="python")
    payload["final_request_body"] = MissingCapture(reason="not captured")
    payload["injected_content"] = ()
    payload["images"] = ()
    with pytest.raises(ValidationError, match="exact final adapter request"):
        EventLog(events=(_event(0, AdapterRequestPayload.model_validate(payload)),))
    response = _event(0, ModelResponsePayload(request_event_id="missing", visible_text=("text",),
        thinking=(ThinkingUnavailable(reason="not exposed"),), usage=UsageMissing(reason="not exposed"),
        raw_response=InlineCapture(value={"text": "text"})))
    with pytest.raises(ValidationError, match="model request event does not exist"):
        EventLog(events=(response,))


def test_role_whitelist_and_model_binding_do_not_turn_recommendations_into_fallbacks() -> None:
    role = make_valid_role()
    with pytest.raises(ValueError, match="no write grant"):
        authorize_tool_call(role, "inspect_region", "write")
    with pytest.raises(ValidationError, match="read-only role"):
        RoleDefinition.model_validate(
            {
                **role.model_dump(mode="python"),
                "tool_whitelist": ({"tool_name": "mutate", "access": "write"},),
            }
        )

    binding = ModelBinding(
        role_id=role.role_id,
        default_model=ModelRouteRef(route_id="local", model_alias="model-a"),
        recommended_models=(
            ModelRouteRef(route_id="hosted", model_alias="model-b"),
        ),
        validated_scopes=(
            ValidatedScope(
                model=ModelRouteRef(route_id="local", model_alias="model-a"),
                scope="single read-only inspection",
                evidence=(_source("validation"),),
            ),
        ),
    )
    assert initial_model_for(binding).model_alias == "model-a"
    with pytest.raises(ValidationError, match="stop_and_report"):
        ModelBinding.model_validate(
            {
                **binding.model_dump(mode="python"),
                "failure_policy": "auto_recommended",
            }
        )


def test_model_binding_allows_explicitly_unvalidated_models_without_using_recommendation() -> None:
    binding = ModelBinding(
        role_id="new-role",
        default_model=ModelRouteRef(route_id="local", model_alias="not-yet-tested"),
        recommended_models=(
            ModelRouteRef(route_id="hosted", model_alias="also-not-yet-tested"),
        ),
        validated_scopes=(),
    )
    assert binding.validated_scopes == ()
    assert initial_model_for(binding) == binding.default_model
    assert initial_model_for(binding) != binding.recommended_models[0]


def test_contracts_reject_non_json_nan_and_infinity() -> None:
    with pytest.raises(ValidationError, match="finite number"):
        InlineCapture(value=float("nan"))
    with pytest.raises(ValidationError, match="finite number"):
        InlineCapture(value=float("inf"))


def test_budget_rejects_empty_reservation_fabricated_zero_and_cost_mismatch() -> None:
    with pytest.raises(ValidationError, match="positive budget dimension"):
        BudgetReservation(
            reservation_id="empty",
            purpose="retry",
            amounts=BudgetAmounts(),
            task_id="task",
        )
    with pytest.raises(ValidationError, match="must remain None"):
        BudgetSettlement(
            reservation_id="retry",
            actual=BudgetAmounts(tokens=0, calls=1),
            usage=UsageMissing(reason="not reported"),
            cost=EstimatedCostUpperBound(usd=Decimal("1"), reason="cap"),
        )
    with pytest.raises(ValidationError, match="must equal"):
        BudgetSettlement(
            reservation_id="child",
            actual=BudgetAmounts(money_usd=Decimal("0.50"), calls=1),
            usage=UsageReported(raw_usage={"tokens": 1}),
            cost=ReportedCost(usd=Decimal("0.60")),
        )


def test_budget_events_require_reservation_before_matching_task_settlement() -> None:
    reservation = BudgetReservation(
        reservation_id="summary-budget",
        purpose="context_summary",
        amounts=BudgetAmounts(tokens=100, money_usd=Decimal("0.50"), calls=1),
        task_id="task-main",
    )
    settlement = BudgetSettlement(
        reservation_id="summary-budget",
        actual=BudgetAmounts(
            tokens=80, money_usd=Decimal("0.25"), calls=1
        ),
        usage=UsageReported(raw_usage={"total_tokens": 80}),
        cost=ReportedCost(usd=Decimal("0.25")),
    )
    reserve_event = _event(
        0,
        BudgetEventPayload(action="reserve", reservation=reservation),
        event_id="reserve",
    )
    settle_event = _event(
        1,
        BudgetEventPayload(action="settle", settlement=settlement),
        event_id="settle",
    )
    assert EventLog(
        mode="complete",
        events=(reserve_event, settle_event),
        budget_limit=BudgetAmounts(tokens=500, money_usd=Decimal("2"), calls=3),
    )

    with pytest.raises(ValidationError, match="reserved before"):
        EventLog(
            mode="complete",
            events=(
                settle_event.model_copy(update={"sequence": 0}),
                reserve_event.model_copy(update={"sequence": 1}),
            ),
            budget_limit=BudgetAmounts(tokens=500, money_usd=Decimal("2"), calls=3),
        )


def test_unknown_write_retry_requires_state_read_and_duplicate_application_is_rejected() -> None:
    valid = make_valid_event_log()
    events_without_inspection = tuple(
        item for item in valid.events if item.event_id != "write-inspection"
    )
    resequenced = tuple(
        item.model_copy(update={"sequence": index})
        for index, item in enumerate(events_without_inspection)
    )
    with pytest.raises(ValidationError, match="does not exist"):
        EventLog(mode="complete", events=resequenced)

    duplicate = _event(
        8,
        ToolExecutionPayload(
            call_id="write-3",
            tool_name="persist_value",
            full_arguments={"value": 7},
            raw_result=InlineCapture(value={"status": "applied again"}),
            shown_result=InlineCapture(value="applied again"),
            repeatability="non_idempotent_write",
            outcome="succeeded",
            operation_key="set:value:7",
            applied_write_id="mutation-8",
            retry_event_id="retry",
        ),
    )
    with pytest.raises(ValidationError, match="immediately previous attempt"):
        EventLog(mode="complete", events=(*valid.events, duplicate))


def test_write_cannot_bypass_recovery_by_omitting_retry_link() -> None:
    valid = make_valid_event_log()
    unknown = next(event for event in valid.events if event.event_id == "unknown-write")
    applied = next(event for event in valid.events if event.event_id == "applied-write")
    direct = applied.model_copy(
        update={
            "sequence": 1,
            "payload": applied.payload.model_copy(update={"retry_event_id": None}),
        }
    )
    with pytest.raises(ValidationError, match="repeated write operation needs"):
        EventLog(mode="complete", events=(unknown.model_copy(update={"sequence": 0}), direct))


@pytest.mark.parametrize(
    ("update", "message"),
    [
        ({"tool_name": "different_writer"}, "tool name"),
        ({"full_arguments": {"value": 8}}, "full arguments"),
    ],
)
def test_retried_write_preserves_tool_and_full_arguments(
    update: dict[str, object], message: str
) -> None:
    valid = make_valid_event_log()
    changed = list(valid.events)
    applied_index = next(
        index for index, event in enumerate(changed) if event.event_id == "applied-write"
    )
    applied = changed[applied_index]
    changed[applied_index] = applied.model_copy(
        update={"payload": applied.payload.model_copy(update=update)}
    )
    with pytest.raises(ValidationError, match=message):
        EventLog(mode="complete", events=tuple(changed))


def test_resume_must_use_exact_inspected_checkpoint() -> None:
    valid = make_valid_event_log()
    changed = list(valid.events)
    resume = changed[-1]
    changed[-1] = resume.model_copy(
        update={
            "payload": resume.payload.model_copy(
                update={"checkpoint": _blob("different-checkpoint", "9")}
            )
        }
    )
    with pytest.raises(ValidationError, match="checkpoint must match"):
        EventLog(mode="complete", events=tuple(changed))


@pytest.mark.parametrize("action", ["cancel", "timeout", "failure"])
def test_stops_keep_partial_artifacts_and_failure_stage(action: str) -> None:
    payload = RunLifecyclePayload(
        action=action,
        reason=f"recorded {action}",
        failure_stage="tool_dispatch" if action == "failure" else None,
        partial_artifacts=(_blob(f"partial-{action}", "8"),),
    )
    assert payload.partial_artifacts[0].sha256 == "8" * 64


def test_model_request_retry_is_recordable_but_successful_tool_retry_is_rejected() -> None:
    request = _event(0, make_valid_request_payload(), event_id="request")
    request_retry = _event(
        1,
        RunLifecyclePayload(
            action="retry",
            reason="transport timeout before response",
            retry_of_event_id="request",
            attempt=2,
        ),
        event_id="request-retry",
    )
    assert EventLog(mode="complete", events=(request, request_retry))

    succeeded = _event(
        2,
        ToolExecutionPayload(
            call_id="read-1",
            tool_name="inspect",
            full_arguments={},
            raw_result=InlineCapture(value={"ok": True}),
            shown_result=InlineCapture(value="ok"),
            repeatability="read_only",
            outcome="succeeded",
        ),
        event_id="successful-tool",
    )
    bad_retry = _event(
        3,
        RunLifecyclePayload(
            action="retry",
            reason="should not retry known success",
            retry_of_event_id="successful-tool",
            attempt=2,
        ),
    )
    with pytest.raises(ValidationError, match="successful tool execution"):
        EventLog(
            mode="complete",
            events=(request, request_retry, succeeded, bad_retry),
        )


def test_context_records_compaction_image_removal_and_reasoned_retrieval() -> None:
    request = _event(0, make_valid_request_payload(), event_id="request")
    compact = _event(
        1,
        ContextEventPayload(
            action="compact",
            reason="token budget threshold",
            summary=_blob("summary", "1"),
            replaced_event_ids=("request",),
        ),
        event_id="compact",
    )
    remove = _event(
        2,
        ContextEventPayload(
            action="remove_image",
            reason="image is outside the active context window",
            image=_blob("original-image", "b"),
        ),
        event_id="remove-image",
    )
    retrieve = _event(
        3,
        ContextEventPayload(
            action="retrieve_image",
            reason="current task needs the cited pixels",
            image=_blob("original-image", "b"),
            removal_event_id="remove-image",
        ),
        event_id="retrieve-image",
    )
    assert EventLog(mode="complete", events=(request, compact, remove, retrieve))

    bad_compact = compact.model_copy(
        update={
            "payload": compact.payload.model_copy(
                update={"replaced_event_ids": ("future-event",)}
            )
        }
    )
    with pytest.raises(ValidationError, match="does not exist"):
        EventLog(mode="complete", events=(request, bad_compact))


def test_context_import_records_source_and_result() -> None:
    payload = ContextImportPayload(
        request_content=InlineCapture(value={"source_events_sha256": "a" * 64}),
        result_content=InlineCapture(value={"status": "imported"}),
    )
    event = _event(0, payload)
    assert EventLog(events=(event,)).events[0].payload.event_type == "context_import"


def test_remote_alias_claim_and_parameter_report_cannot_be_overstated() -> None:
    with pytest.raises(ValidationError, match="needs a revision and evidence"):
        RemoteModelIdentity(
            route_id="route",
            remote_alias="latest",
            alias_status="verified_fixed",
        )
    with pytest.raises(ValidationError, match="cannot be marked verified"):
        ParameterAudit(
            requested={"temperature": 0},
            provider_report=ParametersNotReported(reason="not returned"),
            effect={"kind": "verified", "evidence": _source("unsupported")},
        )


def test_core_package_has_no_building_or_tool_script_dependency() -> None:
    from pathlib import Path

    package = Path(__file__).parents[1] / "src" / "harness_contracts"
    combined = "\n".join(path.read_text(encoding="utf-8") for path in package.glob("*.py"))
    assert "src.agent" not in combined
    assert "scripts.tool_scripts" not in combined
    for forbidden in ("space", "wall", "window", "door", "building"):
        assert re.search(rf"\b{forbidden}s?\b", combined.lower()) is None
