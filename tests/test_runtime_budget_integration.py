"""Narrow integration checks for RuntimeBudget wiring in the durable loop."""

from __future__ import annotations

import asyncio
from decimal import Decimal
import json

from src.agent_runtime.adapter import prepare_request
from src.agent_runtime.loop import RunLimits
from src.harness_contracts import (
    BudgetAmounts,
    BudgetEventPayload,
    BudgetReservation,
    UsageReported,
)

from test_agent_runtime import MESSAGES, response, runtime, record_reserved_request


def test_runtime_settlement_preserves_seconds_overrun_and_stops(tmp_path):
    engine = runtime(tmp_path, [])
    reservation = BudgetReservation(
        reservation_id="request-1",
        purpose="primary_task",
        task_id=engine.store.task_id,
        amounts=BudgetAmounts(
            tokens=100, seconds=Decimal("1"), calls=1
        ),
    )
    with engine.store:
        engine.store.append(
            BudgetEventPayload(action="reserve", reservation=reservation)
        )
        record_reserved_request(engine, reservation)
        engine._load_budget()
        reason = engine._settle(
            reservation.reservation_id,
            UsageReported(raw_usage={"total_tokens": 30}),
            seconds=2.5,
        )
        assert reason == "time_budget_exhausted"
        assert engine.budget.ledger.settlements == ()
        violation = next(
            event
            for event in engine.store.events
            if event.payload.event_type == "run_lifecycle"
            and event.payload.failure_stage == "budget_settlement"
        )
        evidence = next(
            source for source in violation.source_refs
            if source.source_id == "budget-violation"
        )
        observed = json.loads(engine.store.get_bytes(evidence.blob))
        assert observed["decision"]["reason"] == "actual_usage_exceeds_reservation"
        assert observed["observed"]["seconds"] == "2.5"
        assert observed["decision"]["exceeded_dimensions"] == ["seconds"]


def test_missing_usage_response_still_stops_after_crash_recovery(tmp_path):
    class Crash(BaseException):
        pass

    engine = runtime(
        tmp_path,
        [response(("write", "save", {"value": 7}), usage=False)],
    )

    def interrupt_after_durable_response(name, _engine):
        if name == "after_response":
            raise Crash()

    engine.fault_hook = interrupt_after_durable_response
    with engine.store:
        try:
            asyncio.run(engine.run(MESSAGES))
        except Crash:
            pass

    resumed = runtime(tmp_path, [], tools=engine.tools)
    with resumed.store:
        receipt = asyncio.run(resumed.run(MESSAGES, resume=True))
        assert receipt["status"] == "token_usage_unavailable"
        assert receipt["usage_complete"] is False
        assert engine.tools.calls == []
        assert not (tmp_path / "tools/saved.json").exists()


def test_retry_and_primary_requests_charge_the_same_root_ledger(tmp_path):
    limits = RunLimits(
        model_calls=3,
        tool_calls=0,
        seconds=30.0,
        tokens=100_000,
        max_model_retries=1,
    )
    engine = runtime(
        tmp_path,
        [ConnectionError("offline scripted failure"), response(text="Recovered")],
        limits=limits,
    )
    with engine.store:
        receipt = asyncio.run(engine.run(MESSAGES))
        assert receipt["status"] == "completed"
        assert receipt["model_calls"] == 2
        assert receipt["retries"] == 1
        ledger = engine.budget.ledger
        assert [item.purpose for item in ledger.reservations] == [
            "primary_task",
            "retry",
        ]
        assert len(ledger.settlements) == 2
        assert ledger.settlements[0].usage.kind == "missing"
        assert ledger.settlements[0].actual.tokens is None
        assert ledger.charged.tokens == (
            ledger.reservations[0].amounts.tokens + 30
        )


def test_output_reduction_changes_wire_request_and_records_decision(tmp_path):
    probe = runtime(tmp_path / "probe", [])
    with probe.store:
        listed = asyncio.run(probe.tools.list_tools())
        specs = [
            {
                "type": "function",
                "function": {
                    "name": item["name"],
                    "description": item.get("description", ""),
                    "parameters": item["inputSchema"],
                },
            }
            for item in listed
        ]
        sources = [
            probe.store.source(f"probe-message-{index}", message)
            for index, message in enumerate(MESSAGES)
        ]
        prepared = prepare_request(
            store=probe.store,
            model="test",
            messages=MESSAGES,
            message_sources=sources,
            tools=specs,
            tool_source=probe.store.source("probe-tools", specs),
            parameters={"max_tokens": 256, "temperature": 0.0},
            versions=probe.versions,
        )
        token_limit = prepared.input_token_upper_bound + 40

    limits = RunLimits(
        model_calls=1,
        tool_calls=0,
        seconds=30.0,
        tokens=token_limit,
        near_limit="reduce_output",
        min_output_tokens=10,
    )
    engine = runtime(tmp_path / "actual", [response(text="Done")], limits=limits)
    with engine.store:
        receipt = asyncio.run(engine.run(MESSAGES))
        assert receipt["status"] == "completed"
        request = next(
            event.payload
            for event in engine.store.events
            if event.payload.event_type == "adapter_request"
        )
        body = engine.store.resolve(request.final_request_body)
        assert body["max_tokens"] == 40
        reserve = next(
            event
            for event in engine.store.events
            if event.payload.event_type == "budget"
            and event.payload.action == "reserve"
        )
        evidence = next(
            source for source in reserve.source_refs
            if source.source_id == "request-budget-decision"
        )
        decision = json.loads(engine.store.get_bytes(evidence.blob))
        assert decision["near_limit_action"] == "reduce_output"
        assert decision["original_output_limit"] == 256
        assert decision["actual_output_limit"] == 40
        assert decision["degradation"]["action"] == "reduce_output"
        assert decision["degradation"]["output_token_limit"] == 40
        assert decision["token_estimate"]["output_token_limit"] == 40
        assert decision["token_estimate"]["input_tokens_upper_bound"] == decision[
            "effective_estimate"]["input_token_upper_bound"]
        assert decision["context_limits"] == {
            "model_profile": None,
            "configured": None,
            "effective": None,
        }
        assert decision["effective_estimate"]["estimate_source"] == decision[
            "token_estimate"]["source"]


def test_settlement_rejects_missing_request_without_fabricating_image_accounting(tmp_path):
    import pytest
    engine = runtime(tmp_path, [])
    reservation = BudgetReservation(reservation_id="missing-request", purpose="primary_task",
        task_id=engine.store.task_id, amounts=BudgetAmounts(tokens=100, calls=1, seconds=Decimal("1")))
    with engine.store:
        engine.store.append(BudgetEventPayload(action="reserve", reservation=reservation))
        engine._load_budget()
        with pytest.raises(ValueError, match="requires its recorded adapter request"):
            engine._settle(reservation.reservation_id, UsageReported(raw_usage={"total_tokens": 30}), seconds=.5)
        assert not engine.budget.ledger.settlements
