"""R1b token overruns settle actual usage without leaking child limits."""

from __future__ import annotations

import json
import time
from dataclasses import asdict
from decimal import Decimal
from pathlib import Path

import pytest

from src.agent_runtime.adapter import ScriptedAdapter, parse_response, prepare_request
from src.agent_runtime.budget import RequestEstimate, RuntimeBudget
from src.agent_runtime.loop import RunLimits, Runtime
from src.agent_runtime.store import EventStore
from src.harness_contracts import (
    BudgetAmounts,
    BudgetEventPayload,
    BudgetReservation,
    RunLifecyclePayload,
    UsageReported,
)

from test_agent_runtime import record_reserved_request, Tools, role, versions


ROOT = Path(__file__).resolve().parents[1]
GLM_RESPONSES = ROOT / "tests/fixtures/runtime_overrun.json"

def _glm_row() -> dict:
    evidence = json.loads(GLM_RESPONSES.read_text(encoding="utf-8"))
    row = evidence
    assert row["case"] == "text_control"
    assert row["request"]["model"] == "GLM-5.3-Flash"
    assert row["request"]["max_tokens"] == 8
    assert row["response"]["choices"][0]["finish_reason"] == "stop"
    usage = row["response"]["usage"]
    assert usage == {
        "completion_tokens": 24,
        "prompt_tokens": 31,
        "total_tokens": 55,
        "completion_tokens_details": {"reasoning_tokens": 22},
        "prompt_tokens_details": {"cached_tokens": 0},
    }
    return row


def _glm_usage() -> dict:
    return _glm_row()["response"]["usage"]


def _estimate(*, task_id="coordinator", tokens=54):
    return RequestEstimate.for_model_call(
        purpose="child_task" if task_id != "coordinator" else "primary_task",
        task_id=task_id,
        input_token_upper_bound=tokens - 8,
        output_token_limit=8,
        seconds=Decimal("10"),
        estimate_source="GLM calibration fixture: input upper bound plus max_tokens",
    )


def test_glm_one_token_overrun_settles_and_root_with_capacity_continues():
    budget = RuntimeBudget(BudgetAmounts(tokens=100, calls=2, seconds=Decimal("30")))
    reservation = budget.reserve("glm-1", _estimate()).reservation

    decision = budget.settle(
        reservation.reservation_id,
        actual=BudgetAmounts(tokens=55, calls=1, seconds=Decimal("1")),
        usage=UsageReported(raw_usage=_glm_usage()),
    )

    assert decision.action == "allow"
    assert decision.settlement.token_overrun == 1
    assert budget.ledger.charged.tokens == 55
    assert budget.available.tokens == 45
    assert budget.reserve("next", _estimate(tokens=20)).action == "allow"


def test_glm_one_token_overrun_past_root_limit_stops_but_reopens_with_full_charge():
    limit = BudgetAmounts(tokens=54, calls=2, seconds=Decimal("30"))
    budget = RuntimeBudget(limit)
    reservation = budget.reserve("glm-1", _estimate()).reservation

    decision = budget.settle(
        reservation.reservation_id,
        actual=BudgetAmounts(tokens=55, calls=1, seconds=Decimal("1")),
        usage=UsageReported(raw_usage=_glm_usage()),
    )

    assert decision.action == "stop"
    assert decision.reason == "token_budget_exhausted"
    assert decision.exceeded_dimensions == ("tokens",)
    assert decision.settlement.token_overrun == 1
    assert budget.ledger.charged.tokens == 55
    assert budget.available.tokens == 0

    recovered = RuntimeBudget.from_events(
        limit,
        (
            BudgetEventPayload(action="reserve", reservation=reservation),
            BudgetEventPayload(action="settle", settlement=decision.settlement),
        ),
    )
    assert recovered.ledger.charged.tokens == 55
    assert recovered.available.tokens == 0
    assert recovered.reserve("must-stop", _estimate(tokens=9)).action == "stop"


def test_child_overrun_stops_child_while_root_charges_actual_and_sibling_continues(tmp_path):
    root_limit = BudgetAmounts(tokens=200, calls=3, seconds=Decimal("120"))
    child_limits = RunLimits(
        model_calls=1, tool_calls=0, seconds=60.0, tokens=54
    )
    root = EventStore(
        tmp_path / "run",
        run_id="r1b-child-overrun",
        task_id="coordinator",
        budget_limit=root_limit,
    )
    root.append(RunLifecyclePayload(action="start", reason="test root started"))
    child = root.for_task("observer-a", parent_task_id="coordinator")
    reservation = BudgetReservation(
        reservation_id="observer-a:request-1",
        purpose="child_task",
        task_id="observer-a",
        amounts=BudgetAmounts(tokens=54, calls=1, seconds=Decimal("10")),
    )
    child.append(BudgetEventPayload(action="reserve", reservation=reservation))
    engine = Runtime(
        store=child,
        adapter=ScriptedAdapter([]),
        tools=Tools(tmp_path / "tools-a"),
        role=role(child_limits, readonly=True),
        model="GLM-5.3-Flash",
        parameters={"max_tokens": 8, "temperature": 0.0, "reasoning_effort": "medium"},
        versions=versions(),
        limits=child_limits,
    )
    record_reserved_request(engine, reservation)
    engine.started = time.monotonic()
    engine.elapsed_before = 0.0
    engine.answer = None
    engine._load_budget()
    engine._refresh_counts()

    with root:
        reason = engine._settle(
            reservation.reservation_id,
            UsageReported(raw_usage=_glm_usage()),
            seconds=1.0,
        )

        assert reason == "child_token_budget_exhausted"
        task_budget = RuntimeBudget.from_events(
            child_limits.ledger_limit(), child.events
        )
        root_budget = RuntimeBudget.from_events(root_limit, root.all_events)
        assert task_budget.ledger.charged.tokens == 55
        assert task_budget.available.tokens == 0
        assert root_budget.ledger.charged.tokens == 55
        assert root_budget.available.tokens == 145

        overrun = next(
            event.payload
            for event in child.events
            if event.payload.event_type == "budget_overrun"
        )
        assert overrun.model == "GLM-5.3-Flash"
        assert overrun.reservation_id == reservation.reservation_id
        assert overrun.reserved_tokens == 54
        assert overrun.actual_tokens == 55
        assert overrun.overrun_tokens == 1
        # The recorded request makes the actual completion/output-cap comparison
        # available even when this profile has no additional reasoning allowance.
        assert overrun.reasoning_token_allowance == 0
        assert overrun.completion_over_max_tokens == 16
        assert overrun.within_profile_allowance is False
        assert overrun.root_limit_exceeded is False
        assert overrun.task_limit_exceeded is True

        sibling_root_view = RuntimeBudget.from_events(root_limit, root.all_events)
        sibling = sibling_root_view.reserve(
            "observer-b:request-1", _estimate(task_id="observer-b", tokens=54)
        )
        assert sibling.action == "allow"
        assert sibling_root_view.available.tokens == 91


@pytest.mark.parametrize(
    ("token_limit", "expected_reason", "limit_exceeded"),
    [
        (100, None, False),
        (54, "token_budget_exhausted", True),
    ],
)
@pytest.mark.parametrize("recorded_allowance", [0, 32])
def test_glm_overrun_request_evidence_and_recovery_are_durable(
    tmp_path, token_limit, expected_reason, limit_exceeded, recorded_allowance
):
    """Replay the old 54-token reservation with the exact recorded GLM exchange."""

    row = _glm_row()
    limits = RunLimits(
        model_calls=2, tool_calls=0, seconds=60.0, tokens=token_limit
    )
    directory = tmp_path / f"root-{token_limit}-allowance-{recorded_allowance}"
    store = EventStore(
        directory,
        run_id=f"r1b-glm-request-{token_limit}-{recorded_allowance}",
        task_id="coordinator",
        budget_limit=limits.ledger_limit(),
    )
    store.append(RunLifecyclePayload(action="start", reason="test root started"))
    request = row["request"]
    messages = request["messages"]
    prepared = prepare_request(
        store=store,
        model=request["model"],
        messages=messages,
        message_sources=[
            store.source(f"calibration-message-{index}", message)
            for index, message in enumerate(messages)
        ],
        tools=[],
        tool_source=store.source("calibration-tools", []),
        parameters={
            key: value
            for key, value in request.items()
            if key not in {"model", "messages", "stream", "n"}
        },
        versions=versions(),
        strict_model_profile=True,
    )
    assert prepared.token_estimate.reasoning_token_allowance == 32

    # Offline fault injection: persist the original 54-token reservation from
    # before reasoning margins entered reservation sizing. The recorded margin
    # varies separately to cover both the old evidence (0) and new profile (32).
    reservation = BudgetReservation(
        reservation_id="coordinator:request-1",
        purpose="primary_task",
        task_id="coordinator",
        amounts=BudgetAmounts(tokens=54, calls=1, seconds=Decimal("10")),
    )
    store.append(
        BudgetEventPayload(action="reserve", reservation=reservation),
        source_refs=(
            store.source(
                "request-budget-decision",
                {
                    "token_estimate": {
                        **asdict(prepared.token_estimate),
                        "reasoning_token_allowance": recorded_allowance,
                    }
                },
            ),
        ),
    )
    request_event = store.append(
        prepared.event_payload.model_copy(
            update={
                "reservation_id": reservation.reservation_id,
                "logical_purpose": "primary_task",
            }
        )
    )
    parsed = parse_response(row["response"], request_event.event_id, store)
    store.append(parsed.event_payload)

    engine = Runtime(
        store=store,
        adapter=ScriptedAdapter([]),
        tools=Tools(tmp_path / f"tools-{token_limit}"),
        role=role(limits, readonly=True),
        model=request["model"],
        parameters={"max_tokens": 8, "temperature": 0.0, "reasoning_effort": "medium"},
        versions=versions(),
        limits=limits,
    )
    engine.started = time.monotonic()
    engine.elapsed_before = 0.0
    engine.answer = None
    engine._load_budget()
    engine._refresh_counts()
    reason = engine._settle(
        reservation.reservation_id,
        parsed.event_payload.usage,
        seconds=1.0,
    )
    assert reason == expected_reason

    overrun = next(
        event.payload
        for event in store.events
        if event.payload.event_type == "budget_overrun"
    )
    assert overrun.model == "GLM-5.3-Flash"
    assert overrun.reservation_id == reservation.reservation_id
    assert overrun.reserved_tokens == 54
    assert overrun.actual_tokens == 55
    assert overrun.overrun_tokens == 1
    assert overrun.reasoning_token_allowance == recorded_allowance
    assert overrun.completion_over_max_tokens == 16
    assert overrun.within_profile_allowance is (recorded_allowance == 32)
    assert overrun.root_limit_exceeded is limit_exceeded
    assert overrun.task_limit_exceeded is limit_exceeded
    assert engine.budget.ledger.charged.tokens == 55
    assert engine.budget.available.tokens == max(0, token_limit - 55)
    assert len(engine.budget.ledger.settlements) == 1
    store.close()

    # A resumed runtime reconstructs the full charge and recognizes both
    # durable records. Re-settlement neither loses nor duplicates either one.
    reopened = EventStore(
        directory,
        run_id=f"r1b-glm-request-{token_limit}-{recorded_allowance}",
        task_id="coordinator",
        budget_limit=limits.ledger_limit(),
    )
    resumed = Runtime(
        store=reopened,
        adapter=ScriptedAdapter([]),
        tools=Tools(tmp_path / f"resumed-tools-{token_limit}"),
        role=role(limits, readonly=True),
        model=request["model"],
        parameters={"max_tokens": 8, "temperature": 0.0, "reasoning_effort": "medium"},
        versions=versions(),
        limits=limits,
    )
    resumed._load_budget()
    assert resumed.budget.ledger.charged.tokens == 55
    assert resumed.budget.available.tokens == max(0, token_limit - 55)
    assert resumed._settle(
        reservation.reservation_id,
        parsed.event_payload.usage,
        seconds=1.0,
    ) == expected_reason
    recovered_overruns = [
        event.payload
        for event in reopened.events
        if event.payload.event_type == "budget_overrun"
    ]
    assert recovered_overruns == [overrun]
    assert sum(
        event.payload.event_type == "budget"
        and event.payload.action == "settle"
        for event in reopened.events
    ) == 1
    assert sum(
        event.payload.event_type == "adapter_request" for event in reopened.events
    ) == 1
    assert sum(
        event.payload.event_type == "model_response" for event in reopened.events
    ) == 1
    assert reopened.validate().events == tuple(reopened.events)
    reopened.close()


def test_token_and_time_overrun_preserves_full_tokens_and_time_violation_on_resume(
    tmp_path,
):
    limits = RunLimits(model_calls=2, tool_calls=0, seconds=60.0, tokens=100)
    directory = tmp_path / "token-and-time-overrun"
    store = EventStore(
        directory,
        run_id="r1b-token-and-time-overrun",
        task_id="coordinator",
        budget_limit=limits.ledger_limit(),
    )
    store.append(RunLifecyclePayload(action="start", reason="test root started"))
    reservation = BudgetReservation(
        reservation_id="coordinator:request-1",
        purpose="primary_task",
        task_id="coordinator",
        amounts=BudgetAmounts(tokens=54, calls=1, seconds=Decimal("10")),
    )
    store.append(BudgetEventPayload(action="reserve", reservation=reservation))
    engine = Runtime(
        store=store,
        adapter=ScriptedAdapter([]),
        tools=Tools(tmp_path / "time-tools"),
        role=role(limits, readonly=True),
        model="GLM-5.3-Flash",
        parameters={"max_tokens": 8, "temperature": 0.0, "reasoning_effort": "medium"},
        versions=versions(),
        limits=limits,
    )
    record_reserved_request(engine, reservation)
    engine.started = time.monotonic()
    engine.elapsed_before = 0.0
    engine.answer = None
    engine._load_budget()
    engine._refresh_counts()

    assert engine._settle(
        reservation.reservation_id,
        UsageReported(raw_usage=_glm_usage()),
        seconds=11.0,
    ) == "time_budget_exhausted"
    settlement = next(
        event.payload.settlement
        for event in store.events
        if event.payload.event_type == "budget" and event.payload.action == "settle"
    )
    assert settlement.actual.tokens == 55
    assert settlement.actual.seconds is None
    assert engine.budget.ledger.charged.tokens == 55
    assert engine.budget.ledger.committed.seconds == Decimal("10")
    assert sum(
        event.payload.event_type == "budget_overrun" for event in store.events
    ) == 1
    violation = next(
        event
        for event in store.events
        if event.payload.event_type == "run_lifecycle"
        and event.payload.action == "failure"
        and event.payload.failure_stage == "budget_settlement"
    )
    evidence_ref = next(
        ref.blob for ref in violation.source_refs if ref.source_id == "budget-violation"
    )
    evidence = json.loads(store.get_bytes(evidence_ref))
    assert Decimal(evidence["observed"]["seconds"]) == Decimal("11.0")
    store.close()

    reopened = EventStore(
        directory,
        run_id="r1b-token-and-time-overrun",
        task_id="coordinator",
        budget_limit=limits.ledger_limit(),
    )
    resumed = Runtime(
        store=reopened,
        adapter=ScriptedAdapter([]),
        tools=Tools(tmp_path / "resumed-time-tools"),
        role=role(limits, readonly=True),
        model="GLM-5.3-Flash",
        parameters={"max_tokens": 8, "temperature": 0.0, "reasoning_effort": "medium"},
        versions=versions(),
        limits=limits,
    )
    resumed._load_budget()
    assert resumed.budget.ledger.charged.tokens == 55
    assert resumed.budget.ledger.committed.seconds == Decimal("10")
    assert resumed._settle(
        reservation.reservation_id,
        UsageReported(raw_usage=_glm_usage()),
        seconds=11.0,
    ) == "time_budget_exhausted"
    assert sum(
        event.payload.event_type == "budget_overrun" for event in reopened.events
    ) == 1
    assert sum(
        event.payload.event_type == "budget"
        and event.payload.action == "settle"
        for event in reopened.events
    ) == 1
    assert reopened.validate().events == tuple(reopened.events)
    reopened.close()
