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


@pytest.mark.parametrize("reference_lines", [False, True])
def test_unified_draft_reuses_frozen_mesh_and_explicit_opening_heights(tmp_path, monkeypatch, reference_lines):
    from scripts.tool_scripts.bim_agent_source_precision import REFERENCE_LINES_POLICY
    _, run, _ = _prepare(tmp_path, monkeypatch)
    manifest = json.loads((run / "inputs.json").read_bytes())
    manifest["orchestrator"] = True
    if reference_lines:
        manifest["source_precision_policy"] = REFERENCE_LINES_POLICY
    (run / "inputs.json").write_text(json.dumps(manifest), encoding="utf-8")
    toolkit = Toolkit(run)
    value = plan()
    value["mesh_frame"] = dict(yaw_degrees=17, translation_m=[1, 2, 3],
        reason="Explicit test frame", source_refs=["original mesh measurement"])
    value["connections"] = [dict(id="east", kind="door", space_id="F1:office", other_space_id=None,
        p1=[6, .2], p2=[6, 1.2], z_min=0, z_max=2.1, source_refs=["synthetic elevation reading"])]
    raw = json.dumps(value)
    result = build_parametric(toolkit, raw)
    assert result["source_geometry_ready"], result
    folder = run / result["candidate"]
    assert (run / result["draft"]).read_text(encoding="utf-8") == raw
    assert json.loads((folder / "parametric_plan.json").read_bytes()) == value
    assert inspect_parametric(toolkit, candidate=result["candidate"])["exact_plan"]
    source = json.loads((folder / "source_model.json").read_bytes())
    assert source["mesh_frame"] == {**value["mesh_frame"], "mesh_sha256": manifest["mesh_input"]["sha256"]}
    assert {p[2] for o in source["openings"] if o["id"] == "east" for p in o["vertices"]} == {0.0, 2.1}
    if reference_lines:
        effective_path = run / result["architectural_regularization"]["effective"]["file"]
        effective_path.write_text("{}", encoding="utf-8")
        damaged = inspect_parametric(toolkit, candidate=result["candidate"])
        assert not damaged["exact_plan"] and not damaged["patch_allowed"]
        effective_path.unlink()
        missing = inspect_parametric(toolkit, candidate=result["candidate"])
        assert not missing["exact_plan"] and not missing["patch_allowed"]


def test_unified_shorthands_do_not_infer_heights_or_replace_a_foreign_mesh():
    from scripts.tool_scripts.bim_agent_architectural import effective_parametric_plan
    manifest = {"orchestrator": True, "mesh_input": {"sha256": "a" * 64}}
    for connection in ({"z_min": 1}, {"z_min": 1, "z_max": 3, "z": [0, 2]}, {}):
        with pytest.raises(ValueError, match=r"/connections/0"):
            effective_parametric_plan({"connections": [connection]}, manifest)
    with pytest.raises(ValueError, match="frozen original GLB"):
        effective_parametric_plan({"mesh_frame": {"mesh_sha256": "b" * 64}}, manifest)
    legacy = {"connections": [{"z_min": 1, "z_max": 3}], "mesh_frame": {}}
    assert effective_parametric_plan(legacy, {"mesh_input": manifest["mesh_input"]}) is legacy
