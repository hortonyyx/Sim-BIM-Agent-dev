"""New mesh admission must actually compile architectural adoption and bind it."""
import copy
import json
import pytest

from scripts.tool_scripts.bim_agent_parametric import build_parametric, inspect_parametric
from scripts.tool_scripts.run_bim_agent import Toolkit
from src.agent.geometry.parametric_proposal import expand_parametric_proposal
from tests.test_bim_agent_mesh import _prepare
from tests.test_parametric_proposal import plan


def measured_plan():
    value = plan()
    template = value['templates']['typical']
    template['footprint'] = [[0,0],[6.04,0],[6.04,2],[3.04,2],[3.04,5],[0,5]]
    template['spaces'][0]['rect'][2] = 3.04
    template['spaces'][1]['rect'] = [3.04,0,6.04,2]
    for point in ('p1', 'p2'):
        template['doors'][0][point][0] = 3.04
    value['instances'] = []
    value['repeats'] = [{'id':'office', 'template':'typical', 'count':3,
                        'id_prefix':'F', 'start':1, 'z':0, 'height':3.02,
                        'source_refs':['synthetic repeated-floor hypothesis']}]
    return value


def test_new_mesh_adopts_main_dimensions_and_hosts_but_retains_observations(tmp_path, monkeypatch):
    _, run, _ = _prepare(tmp_path, monkeypatch)
    toolkit = Toolkit(run)
    value = measured_plan()
    before = copy.deepcopy(value)
    built = build_parametric(toolkit, json.dumps(value))
    assert built['source_geometry_ready'], built
    assert value == before
    reference = built['architectural_regularization']
    adopted = json.loads((run/reference['adopted']['file']).read_bytes())
    assert max(p[0] for p in adopted['templates']['typical']['spaces'][0]['polygon']) == 3
    assert adopted['repeats'][0]['height'] == 3
    source = json.loads((run/built['candidate']/'source_model.json').read_bytes())
    assert len(source['connections']) == 3
    assert source['validation']['status'] == 'pass'
    assert json.loads((run/built['candidate']/'parametric_plan.json').read_bytes()) == before
    assert len(built['source_plan_views']) == 1
    assert built['build_timings']['export_seconds'] > 0
    assert built['timings']['source_kernel_seconds'] > 0
    inspected = inspect_parametric(toolkit, candidate=built['candidate'])
    assert inspected['exact_plan'], inspected
    (run/reference['adopted']['file']).write_text('{}')
    stale = inspect_parametric(toolkit, candidate=built['candidate'])
    assert not stale['exact_plan'], stale


def test_direct_proposal_uses_same_policy_and_keeps_identity(tmp_path, monkeypatch):
    _, run, _ = _prepare(tmp_path, monkeypatch)
    raw = expand_parametric_proposal(measured_plan())
    result = Toolkit(run).build(raw)
    assert result['source_geometry_ready'], result
    adopted = json.loads((run/result['candidate']/'proposal.json').read_bytes())
    assert adopted['geometry']['floors'][0]['ceiling_height'] == 3
    assert [r['id'] for r in raw['geometry']['openings']] == [r['id'] for r in adopted['geometry']['openings']]
    assert raw['geometry']['floors'][0]['ceiling_height'] == 3.02


def test_historical_mesh_manifest_keeps_its_recorded_policy(tmp_path, monkeypatch):
    _, run, _ = _prepare(tmp_path, monkeypatch)
    manifest = json.loads((run/'inputs.json').read_bytes())
    manifest.pop('architectural_modelling_policy')
    (run/'inputs.json').write_text(json.dumps(manifest), encoding='utf-8')
    raw = expand_parametric_proposal(measured_plan())
    result = Toolkit(run).build(raw)
    assert result['source_geometry_ready'], result
    assert 'architectural_regularization' not in result
    assert json.loads((run/result['candidate']/'proposal.json').read_bytes()) == raw
