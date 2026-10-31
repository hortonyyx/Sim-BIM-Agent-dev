"""Reader targets have one spelling, and a plan trial always carries the task floor."""

from __future__ import annotations

import asyncio

import pytest

from src.agent.runtime_roles.readers import READER_TOOL_NAMES, ReaderTools
from src.agent.runtime_roles.submission import parse_target


@pytest.mark.parametrize(("role", "written", "parsed"), [
    ("plan_reader", "plan/F1", ("F1", set())),
    ("plan_reader", "plan F1", ("F1", set())),  # 10-07 sm24 run4 deadlock
    ("plan_reader", "PLAN / f1", ("F1", set())),
    ("plan_reader", " Floor: F2 ", ("F2", set())),
    ("elevation_reader", "elevation/North", ("North", set())),
    ("elevation_reader", "facade south / F1, F2", ("South", {"F1", "F2"})),
    ("elevation_reader", "ELEVATION/sOuTh/f1,f2", ("South", {"F1", "F2"})),
])
def test_role_prefixed_and_spaced_targets_are_normalized(role, written, parsed):
    assert parse_target(role, written) == parsed


@pytest.mark.parametrize("written", ["first floor", "F1/F2"])
def test_plan_target_must_be_one_floor_id(written):
    with pytest.raises(ValueError):
        parse_target("plan_reader", written)


class _Frozen:
    async def list_tools(self):
        names = [*READER_TOOL_NAMES["plan_reader"], "view_pixel_profile"]
        return [{"name": name, "description": "", "inputSchema": {"type": "object", "properties": {}}}
                for name in names]


class _Trial:
    def __init__(self):
        self.plans = []

    async def call(self, plan=None, *, operations=None):
        self.plans.append(plan)
        return {"content": [], "isError": False, "structuredContent": {"status": "passed"}}


def test_plan_trial_uses_the_task_floor_id():
    trial = _Trial()
    tools = ReaderTools(_Frozen(), role_id="plan_reader", image_name="1f_view.png", trial=trial, target="plan F1")
    asyncio.run(tools.call_tool("trial_plan_bim", {"plan": {"floor_id": "1F", "partitions": []}}))
    assert trial.plans == [{"floor_id": "F1", "partitions": [], "reading_type": "plan"}]
