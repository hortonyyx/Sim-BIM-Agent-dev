"""C3-T real saves and failed/partial application; strictly offline."""
import asyncio
import copy
import json
from types import SimpleNamespace

import pytest

from scripts.tool_scripts.bim_agent_saved_result import read_saved_result, result_metadata
from src.agent.runtime_context import update_building_context
from src.agent_runtime.adapter import ScriptedAdapter
from src.agent_runtime.loop import Runtime, RunLimits
from src.agent.runtime_tools import coordinator_role
from tests.test_agent_runtime import response, versions
from src.agent.runtime_behaviour import summarise
from src.agent_runtime.context import ContextManager, ContextPolicy
from src.agent_runtime.store import EventStore
from src.harness_contracts import BudgetAmounts
from tests.test_bim_absorb_a5t import server
from tests.test_bim_claims import setup_run, claim, edit
from tests.test_source_proposal import _proposal


class LocalTools:
    def __init__(self, run):
        self.run_directory = run
        self.api = server(run)
        self.calls = []

    async def list_tools(self):
        return [t.model_dump(mode="json") for t in await self.api.list_tools()]

    def repeatability(self, name):
        return "non_idempotent_write"

    def snapshot_state(self):
        return {"candidates": sorted(p.name for p in self.run_directory.glob("candidate_*"))}

    async def call_tool(self, name, arguments):
        self.calls.append(name)
        value = await self.api.call_tool(name, arguments)
        if hasattr(value, "model_dump"):
            return value.model_dump(mode="json")
        if isinstance(value, tuple):
            return dict(content=[v.model_dump(mode="json") for v in value[0]], structuredContent=value[1])
        return dict(content=[v.model_dump(mode="json") for v in value])

    def artifacts(self):
        return sorted(self.run_directory.glob("candidate_*/source_model.json"))

    def image_origins(self, raw):
        return {}


@pytest.mark.parametrize("outcome", ["completed", "failed", "partial"])
def test_transaction_current_source_effects_and_recovery(tmp_path, outcome):
    run, toolkit = setup_run(tmp_path)
    first = toolkit.build(_proposal())["saved_candidate"]
    valid = dict(claim=claim(candidate=first), action="apply", operations=[edit("$claim")])
    invalid = dict(claim={}, action="apply")
    entries = {"completed": [valid], "failed": [invalid], "partial": [valid, invalid]}[outcome]
    tools = LocalTools(run)
    limits = RunLimits(model_calls=2, tool_calls=1, seconds=60, tokens=None)
    with EventStore(tmp_path / "audit", run_id="c3t", task_id="root",
                    budget_limit=limits.ledger_limit()) as store:
        initial = toolkit.build(_proposal())
        old = initial["saved_candidate"]
        def update(engine, event, raw):
            if not any(s.key == "current-source-bim" for s in engine.context.state):
                before = SimpleNamespace(payload=SimpleNamespace(tool_name="build_bim"), event_id=None)
                setup_engine = SimpleNamespace(context=engine.context, store=store, tools=tools,
                    _event_source=lambda _: store.source("fixture-initial-build", initial))
                update_building_context(setup_engine, before, {"structuredContent": initial})
            update_building_context(engine, event, raw)
        engine = Runtime(store=store, tools=tools, adapter=ScriptedAdapter([
            response(("transaction", "claim_transaction", dict(candidate=first, entries_json=json.dumps(entries)))),
            response(text="The transaction is saved.")]),
            role=coordinator_role(limits.ledger_limit()), model="test", parameters={"max_tokens": 256},
            versions=versions(), limits=limits, context_update=update,
            context_policy=ContextPolicy(compact_at_tokens=90000))
        receipt = asyncio.run(engine.run([{"role": "user", "content": "Apply the transaction once."}]))
        assert receipt["status"] == "completed"
        context = engine.context
        event = next(e for e in reversed(store.events) if e.payload.event_type == "tool_execution")
        raw = store.resolve(event.payload.raw_result)
        data = result_metadata(raw)
        assert not raw.get("isError") and data["status"] == outcome
        applied = outcome != "failed"
        final = data["saved_candidate"] if applied else old
        assert data["save_effects"]["geometry_applied"] is applied
        assert data["save_effects"]["audit_written"]
        assert bool(data["save_effects"]["created_candidates"]) is applied
        event = next(e for e in reversed(store.events) if e.payload.event_type == "tool_execution")
        assert event.payload.applied_write_id  # The transaction audit was durably written.
        update_building_context(engine, event, raw)
        assert next(s.value["candidate"] for s in context.state if s.key == "current-source-bim") == final
        if applied:
            assert data["claim_application"]["status"] == "applied"
            assert data["source_plan_views"]  # Same feedback as an ordinary revision.
            assert any(b["type"] == "image" for b in raw["content"])
            door = json.loads((run/final/"proposal.json").read_text(encoding="utf-8"))["geometry"]["openings"][0]
            assert door["z"] == [.3, 2.1]
        context.policy = ContextPolicy(compact_at_tokens=400)
        # Force a genuine context compaction, then load its durable checkpoint.
        for i in range(5):
            context.append({"role": "user", "content": "old observation " * 100},
                store.source(f"history-{i}", {}).model_copy(update={"event_id": event.event_id}))
            context.project()
        assert any(e.payload.event_type == "context" and e.payload.action == "compact" for e in store.events)
        restored = ContextManager.load(store, context.dump())
        engine.context = restored
        update_building_context(engine, event, raw)
        assert next(s.value["candidate"] for s in restored.state if s.key == "current-source-bim") == final
        assert tools.calls == ["claim_transaction"]  # Recovery never repeats committed entries.
        record = dict(run="fixture", _source_root=str(run), source_format="test", gaps=[], invocations=[dict(
            steps=[dict(index=1, tool="claim_transaction", arguments={}, result_data=data, is_error=False, t_call=1)])])
        summary = summarise(record)
        assert (summary["call_errors"], summary["domain_failures"], summary["usable_source_drafts"]) == (
            0, int(outcome != "completed"), int(applied))


def test_noop_revision_and_confirm_are_not_geometry_application(tmp_path):
    run, toolkit = setup_run(tmp_path)
    result = toolkit.revise("seed", json.dumps([dict(op="set_notes", assumptions=["inferred"], unresolved=[])]))
    assert result["saved_candidate"] == result["candidate"]
    assert not result["save_effects"]["geometry_applied"]
    assert result["save_effects"]["created_candidates"] == [result["candidate"]]
    assert read_saved_result({"candidate": result["candidate"]})["saved_candidate"] is None


def test_physical_signature_tracks_connectivity_and_excludes_room_use():
    from scripts.tool_scripts.bim_agent_saved_result import _geometry
    from tests.test_building_precision import source
    model = source()
    changed = copy.deepcopy(model)
    changed['spaces'][0]['role'] = 'corridor'
    changed['spaces'][0]['assumptions'] = ['inferred from context']
    assert _geometry(changed) == _geometry(model)
    changed['boundaries'][0]['kind'] = 'abstract'
    assert _geometry(changed) != _geometry(model)
    changed = copy.deepcopy(model)
    changed['opening_hosts'] = {'door': ['wall']}
    assert _geometry(changed) != _geometry(model)
    model['coordinate_system']['north_axis'] = dict(value_deg=20, provenance='assumed', source_ids=[])
    changed = copy.deepcopy(model)
    changed['coordinate_system']['north_axis'].update(provenance='observed', source_ids=['plan.png'])
    assert _geometry(changed) == _geometry(model)
    changed['coordinate_system']['north_axis']['value_deg'] = 30
    assert _geometry(changed) != _geometry(model)


def test_unattested_candidate_and_old_transaction_cannot_declare_save(tmp_path):
    run, toolkit = setup_run(tmp_path)
    current = toolkit.revise("seed", json.dumps([dict(op="set_notes", assumptions=["inferred"], unresolved=[])]))
    assert current["saved_candidate"]
    old = dict(candidate="seed", entries=[dict(status="applied", result_candidate=current["candidate"])],
               audit_file="claims/transaction_0001.json")
    for result in (old, {"candidate": current["candidate"], "source_geometry_ready": True}):
        saved = read_saved_result(result)
        assert saved["saved_candidate"] is None
        assert not saved["save_effects"]["geometry_applied"]
        assert saved["save_effects"]["created_candidates"] == []


def test_precision_changes_compare_real_reports_and_preserve_resolved_findings(tmp_path):
    from scripts.tool_scripts.bim_agent_precision import building_precision
    from scripts.tool_scripts.bim_agent_replies import precision_summary
    from tests.test_building_precision import source
    run, toolkit = setup_run(tmp_path)
    calibrations = [(str(i), dict(floor_id=f'F{i}', calibration_id=str(i), image='plan.png',
        image_sha256=toolkit.manifest['images']['plan.png']['sha256'],
        x_anchors=[[0, 0], [120, 6]], y_anchors=[[0, 0], [120, 6]])) for i in (1, 2)]
    toolkit.registered_calibrations = lambda: calibrations
    reports = []
    for index, offset in enumerate((.06, .06, 0)):
        candidate = f'candidate_{index + 1:02}'
        folder = run / candidate
        folder.mkdir()
        (folder / 'proposal.json').write_text('{}')
        model = source(offset)
        if index:
            model['generation'] = {'provenance': {'parent_candidate': f'candidate_{index:02}'}}
        raw = json.dumps(model)
        (folder / 'source_model.json').write_text(raw)
        reports.append(building_precision(toolkit, candidate))
        assert (folder / 'source_model.json').read_text(encoding="utf-8") == raw
        assert json.loads((folder / 'precision_report.json').read_text(encoding="utf-8")) == reports[-1]
    assert reports[0]['total'] == 2
    assert reports[1]['changes']['unchanged'] == [0, 1]
    assert not reports[1]['changes']['new']
    assert reports[2]['total'] == 0
    assert reports[2]['changes']['resolved'] == reports[0]['items']
    assert all(g['change'] == 'resolved' for g in precision_summary(reports[2])['groups'])


def test_schema_removes_annotations_but_preserves_title_parameter(tmp_path):
    run, _ = setup_run(tmp_path)
    api = server(run)
    @api.tool()
    def custom_title(title: str):
        return title
    schemas = {t.name: t.inputSchema for t in asyncio.run(api.list_tools())}
    assert schemas['custom_title']['properties']['title'] == {'type': 'string'}
    assert schemas['custom_title']['required'] == ['title']
    assert schemas['view_pixel_profile']['properties']['axis']['enum'] == ['x', 'y']
    assert 'title' not in schemas['view_pixel_profile']['properties']['name']


def test_profile_full_readback_keeps_every_interval_and_unchanged_picture(tmp_path):
    from scripts.tool_scripts.bim_agent_replies import compact_reply, read_report
    run, toolkit = setup_run(tmp_path)
    _, raw = toolkit.view_profile('plan.png', [0, 0, 12, 8], 'x', [0, 0, 0], 70, .1)
    original = json.loads(raw)
    result = compact_reply(run, 'view_pixel_profile', original)
    assert result['candidates'] == original['candidates']
    assert not {'panel_note', 'display_note', 'evidence_note'} & result.keys()
    assert result['threshold_excluded_support']['matching_pixels'] == original['threshold_excluded_support']['matching_pixels']
    assert result['positive_support_summary']['run_count'] == len(original['positive_support_runs'])
    pieces, offset = [], 0
    while True:
        page = read_report(run, result['details_file'], offset, 123)
        pieces.append(page['text'])
        if page['next_offset'] is None:
            break
        offset = page['next_offset']
    assert json.loads(''.join(pieces)) == original


def test_precision_keeps_late_severe_items_and_groups_without_repeated_lines():
    from scripts.tool_scripts.bim_agent_replies import precision_summary
    lines = [dict(floor_id=f'F{i}', boundary_ids=[f'wall-{i}'], axis='x', coordinate_m=float(i)) for i in (1, 2)]
    items = [dict(type='storey_wall_offset', lines=lines, align_to_options=copy.deepcopy(lines),
        deviation_m=.1 + i, tolerance_m=.02, severity='severe') for i in range(12)]
    full = dict(status='reported', total=len(items), items=items, coverage={'floors':['F1','F2']},
        tolerances={'F1':{'default_m':.02, 'basis':{'image':'plan.png'}}},
        not_checked=['drawing fidelity'], wall_placement={'total':6, 'items':items[:6]},
        changes={'status':'compared', 'previous_candidate':'candidate_01', 'new':list(range(10)),
                 'unchanged':[10,11], 'resolved':[dict(type='thin_space', space_id='old', width_m=.01)]})
    before = copy.deepcopy(full)
    result = precision_summary(full)
    assert full == before
    assert len(result['wall_lines']['rows']) == 2
    assert result['tolerances'] == full['tolerances'] and result['not_checked'] == full['not_checked']
    assert result['wall_placement']['items'] == items[:6]
    assert sum(len(g['rows']) for g in result['groups'] if g['change'] != 'resolved') == 12
    assert sum(len(g['rows']) for g in result['groups'] if g['change'] == 'resolved') == 1
    for group in result['groups']:
        if group['type'] == 'storey_wall_offset':
            for values in group['rows']:
                row = {**group['common'], **dict(zip(group['columns'], values))}
                assert row['lines'] == row['align_to_options'] == ['L1','L2']
                assert row['severity'] == 'severe'


def test_height_summary_omits_only_unbound_rows_without_distinct_problems():
    from scripts.tool_scripts.bim_agent_replies import height_summary
    rows = [dict(opening_id='missing', status='missing', evidence=[], issues=['no_current_height_binding']),
        dict(opening_id='wide', status='missing', evidence=[], issues=['no_current_height_binding', 'distinct_width_on_facade']),
        dict(opening_id='bound', status='located_applied', evidence=[{'claim_id':'claim_0001'}], issues=[])]
    full = dict(openings=rows, summary={'exterior_count':3}, calibration_problems=['keep'])
    result = height_summary(full)
    assert result['openings'] == rows[1:]
    assert result['unbound_without_other_issues'] == 1
    assert result['calibration_problems'] == full['calibration_problems']
    assert full['openings'] == rows


@pytest.mark.parametrize('field', ['z', 'p1', 'p2'])
def test_geometry_feedback_names_object_and_missing_field(tmp_path, field):
    from tests.test_bim_agent_plan_partition import example
    run, toolkit = setup_run(tmp_path)
    plan = example()
    identity = plan['openings'][0]['id']
    del plan['openings'][0][field]
    result = toolkit.build_plan('plan.png', json.dumps(plan))
    feedback = result['plan_input']['geometry_feedback']
    assert feedback['status'] == 'unavailable'
    assert feedback['object_id'] == identity and feedback['field'] == field
    assert identity in feedback['reason'] and field in feedback['reason']
    assert feedback['repair_hint']['example']['id'] == identity
    assert {'id','kind','p1','p2','z','source_refs'} <= feedback['repair_hint']['example'].keys()
