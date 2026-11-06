"""Architectural adoption keeps identities, host links and real volume gaps."""
import copy
import math

import pytest
from shapely.geometry import Polygon

from src.agent.geometry.architectural_regularization import (
    ArchitecturalRegularizationError, DEFAULT_ARCHITECTURAL_POLICY,
    regularize_architectural_plan, regularize_architectural_proposal,
)
from src.agent.geometry.parametric_proposal import expand_parametric_instances, expand_parametric_proposal
from src.agent.execution.source_proposal import export_source_proposal


POLICY = DEFAULT_ARCHITECTURAL_POLICY


def rectangle(x, y, w, h):
    return [[x,y],[x+w,y],[x+w,y+h],[x,y+h]]


def single(points=None, *, z=0, height=3):
    points = points or rectangle(.024,.024,6,4)
    return dict(schema_version='source_polygon_v1', templates={'main': {
        'footprint': copy.deepcopy(points), 'spaces': [dict(id='room', role='office',
            polygon=copy.deepcopy(points), source_refs=['synthetic measured shell'])]}},
        instances=[dict(id='F1', template='main', z=z, height=height)],
        assumptions=['Interior is a hypothesis'], unresolved=[])


def shared_plan():
    plan = single(z=.024)
    template = plan['templates']['main']
    template['spaces'] = [dict(id='hall', role='corridor', rect=[.024,.024,3.024,4.024], source_refs=['test']),
                          dict(id='room', role='office', rect=[3.024,.024,6.024,4.024], source_refs=['test'])]
    template['doors'] = [dict(id='door', space='hall', other_space='room', p1=[3.024,1.024],
                              p2=[3.024,2.024], z=[0,2.1], source_refs=['test'])]
    template['window_rows'] = [dict(id='west', facade='West', plane=.024,
                                    spans=[[1.024,2.024]], z=[.8,2.2], source_refs=['test'])]
    return plan


def test_shared_wall_aperture_and_evidence_survive_source_save(tmp_path):
    raw = shared_plan()
    before = copy.deepcopy(raw)
    adopted, audit = regularize_architectural_plan(raw, policy=POLICY)
    assert raw == before
    assert audit['status'] == 'adopted'
    assert audit['contact_check']['status'] == 'passed'
    template = adopted['templates']['main']
    assert template['doors'][0]['p1'] == [3,1]
    assert template['doors'][0]['p2'] == [3,2]
    assert template['window_rows'][0]['plane'] == 0
    assert template['window_rows'][0]['spans'] == [[1,2]]
    proposal = expand_parametric_proposal(adopted)
    assert proposal['geometry']['openings'][0]['z'] == [0,2.1]
    assert proposal['geometry']['openings'][0]['source_refs'] == ['test']
    report = export_source_proposal(proposal, tmp_path/'adopted')
    assert report['source_geometry_ready'], report
    assert report['counts']['spaces'] == 2


def test_rotated_local_frame_regularizes_dimensions_not_world_vertices():
    origin, yaw = [100.023,-40.017], 31
    c,s = math.cos(math.radians(yaw)), math.sin(math.radians(yaw))
    local = rectangle(.024,.024,4.014,2.014)
    points = [[origin[0]+c*x-s*y,origin[1]+s*x+c*y] for x,y in local]
    plan = single(points)
    frame = {'id':'wing', 'origin':origin, 'yaw_degrees':yaw, 'templates':['main']}
    plan['modelling'] = {'frames':[frame]}
    plan['mesh_frame'] = {'unchanged_transform_sentinel':[-46.5,0,0]}
    adopted,audit = regularize_architectural_plan(plan, policy=POLICY)
    ring = adopted['templates']['main']['spaces'][0]['polygon']
    assert math.dist(ring[0],ring[1]) == pytest.approx(4)
    assert math.dist(ring[1],ring[2]) == pytest.approx(2)
    assert ring[0] == pytest.approx(origin)
    assert any(abs(v*10-round(v*10))>.01 for p in ring for v in p)
    assert adopted['mesh_frame'] == plan['mesh_frame']
    assert 'modelling' not in adopted
    assert audit['frames']['main']['origin'] == origin


def test_oblique_polygon_retains_directions_and_real_host(tmp_path):
    points = [[.023,.027],[6.041,.027],[8.041,4.047],[2.023,4.047]]
    plan = single(points)
    a,b = points[1:3]
    plan['templates']['main']['opening_rows'] = [dict(id='oblique', space='room', kind='window',
        p1=a,p2=b,spans=[[.5,1.5]],z=[.8,2.2],source_refs=['oblique observation'])]
    adopted,audit = regularize_architectural_plan(plan, policy=POLICY)
    after = adopted['templates']['main']['spaces'][0]['polygon']
    for a,b,aa,bb in zip(points,points[1:]+points[:1],after,after[1:]+after[:1]):
        assert (b[0]-a[0])*(bb[1]-aa[1])-(b[1]-a[1])*(bb[0]-aa[0]) == pytest.approx(0, abs=1e-9)
    assert any(abs(v*10-round(v*10))>1e-4 for p in after for v in p)
    report = export_source_proposal(expand_parametric_proposal(adopted), tmp_path/'oblique')
    assert report['source_geometry_ready'], report
    assert audit['not_assessed'] == []


def test_nearly_same_lines_use_fixed_twenty_micrometre_anchor_without_chaining():
    plan = single()
    plan['templates'],plan['instances'] = {},[]
    for i,x in enumerate([3.04999,3.050005,3.05002]):
        template = single(rectangle(x,i*5,2,3))['templates']['main']
        plan['templates'][str(i)] = template
        plan['instances'].append(dict(id=f'F{i}', template=str(i),z=0,height=3))
    adopted,_ = regularize_architectural_plan(plan, policy=POLICY)
    xvalues = [min(p[0] for p in adopted['templates'][str(i)]['spaces'][0]['polygon']) for i in range(3)]
    assert xvalues == [3.0,3.0,3.1]


def test_fifteen_micrometre_overlap_is_the_same_shared_wall_seam():
    raw = shared_plan()
    raw['templates']['main']['spaces'][1]['rect'][0] -= .000015
    adopted,audit = regularize_architectural_plan(raw, policy=POLICY)
    cells = adopted['templates']['main']['spaces']
    assert Polygon(cells[0]['polygon']).intersection(Polygon(cells[1]['polygon'])).area == 0
    assert adopted['templates']['main']['doors'][0]['p1'] == [3,1]
    assert audit['contact_check']['status'] == 'passed'


def test_acute_intersection_cannot_amplify_grid_adoption_into_a_metre_shift():
    raw = single([[.024,.024],[20.024,.024],[.024,1.073]])
    before = copy.deepcopy(raw)
    with pytest.raises(ArchitecturalRegularizationError) as error:
        regularize_architectural_plan(raw, policy=POLICY)
    rejection = error.value.audit['rejections'][0]
    assert rejection['code'] == 'outline_displacement_exceeded'
    assert rejection['max_vertex_displacement_m'] > .97
    assert rejection['limit_m'] == .1
    assert raw == before


@pytest.mark.parametrize('origin,yaw', [([0, 0], 0), ([103.024, -28.013], 31)])
def test_grid_dimensioned_chamfer_keeps_derived_support_and_complete_aperture(origin, yaw, tmp_path):
    local = [[0, 0], [8.1, 0], [8.1, 5.1], [7.5, 6.5], [0, 6.5]]
    c, s = math.cos(math.radians(yaw)), math.sin(math.radians(yaw))
    points = [[origin[0]+c*x-s*y, origin[1]+s*x+c*y] for x, y in local]
    plan = single(points)
    plan['modelling'] = {'frames': [dict(id='wing', origin=origin, yaw_degrees=yaw, templates=['main'])]}
    # The whole chamfer is the declared host. Independently rounding its
    # irrational normal or tangent offset moves already dimensioned endpoints.
    plan['templates']['main']['doors'] = [dict(id='edge', space='room', other_space=None,
        p1=points[2], p2=points[3], z=[0, 2.1], source_refs=['grid-dimensioned chamfer'])]
    before = copy.deepcopy(plan)
    adopted, audit = regularize_architectural_plan(plan, policy=POLICY)
    assert plan == before
    for old, new in zip(points, adopted['templates']['main']['footprint']):
        assert new == pytest.approx(old, abs=1e-10)
    door = adopted['templates']['main']['doors'][0]
    assert door['p1'] == pytest.approx(points[2], abs=1e-10)
    assert door['p2'] == pytest.approx(points[3], abs=1e-10)
    assert audit['observed_max_vertex_displacement_m'] < 1e-10
    support, = [row for row in audit['diagnostics'] if row['type'] == 'grid_derived_support']
    for actual, expected in zip(sorted(support['source_edge']), sorted(local[2:4])):
        assert actual == pytest.approx(expected, abs=1e-10)
    assert abs(support['adopted_offset_m']*10-round(support['adopted_offset_m']*10)) > .01
    repeated, _ = regularize_architectural_plan(adopted, policy={**POLICY, 'frames': plan['modelling']['frames']})
    for old, new in zip(adopted['templates']['main']['footprint'], repeated['templates']['main']['footprint']):
        assert new == pytest.approx(old, abs=1e-10)
    for key in ('p1', 'p2'):
        assert repeated['templates']['main']['doors'][0][key] == pytest.approx(door[key], abs=1e-10)
    report = export_source_proposal(expand_parametric_proposal(adopted), tmp_path/'chamfer')
    assert report['source_geometry_ready'], report


def test_grid_derived_shared_wall_includes_partial_edges_and_preserves_incidence(tmp_path):
    a, b = [.1, .1], [6.1, 4.1]
    split = [a[i]+.37*(b[i]-a[i]) for i in (0, 1)]
    plan = single([a, [6.1, .1], b, [.1, 4.1]])
    template = plan['templates']['main']
    template['spaces'] = [dict(id='upper', role='office', polygon=[a, split, b, [.1, 4.1]],
                              source_refs=['synthetic shared partition']),
                          dict(id='lower', role='office', polygon=[a, [6.1, .1], b],
                              source_refs=['synthetic shared partition'])]
    endpoints = [[a[i]+t*(b[i]-a[i]) for i in (0, 1)] for t in (.45, .7)]
    template['doors'] = [dict(id='shared', space='upper', other_space='lower',
        p1=endpoints[0], p2=endpoints[1], z=[0, 2.1], source_refs=['shared sloping wall'])]
    adopted, audit = regularize_architectural_plan(plan, policy=POLICY)
    for old, new in zip(template['spaces'], adopted['templates']['main']['spaces']):
        assert Polygon(old['polygon']).hausdorff_distance(Polygon(new['polygon'])) < 1e-10
    door = adopted['templates']['main']['doors'][0]
    assert door['p1'] == pytest.approx(endpoints[0], abs=1e-10)
    assert door['p2'] == pytest.approx(endpoints[1], abs=1e-10)
    assert audit['observed_max_vertex_displacement_m'] < 1e-10
    template['spaces'].reverse()
    template['footprint'].reverse()
    for space in template['spaces']:
        space['polygon'].reverse()
    reversed_plan, _ = regularize_architectural_plan(plan, policy=POLICY)
    shapes = {s['id']: Polygon(s['polygon']) for s in adopted['templates']['main']['spaces']}
    for space in reversed_plan['templates']['main']['spaces']:
        assert shapes[space['id']].hausdorff_distance(Polygon(space['polygon'])) < 1e-10
    report = export_source_proposal(expand_parametric_proposal(adopted), tmp_path/'shared')
    assert report['source_geometry_ready'], report


def test_curve_declarations_and_derived_vertices_are_untouched():
    plan = single([[0,0],[4.023,0],[2.85,2.85],[0,4.023]])
    path = [{'curve_id':'curve'}]
    plan['templates']['main']['footprint'] = {'vertices':plan['templates']['main']['footprint'], 'boundary_path':path}
    plan['templates']['main']['spaces'][0]['boundary_path'] = path
    plan['boundary_curves'] = {'curve':{'sentinel':'preserve curve parameters'}}
    before = copy.deepcopy(plan)
    adopted,audit = regularize_architectural_plan(plan, policy=POLICY)
    assert adopted == before
    assert audit['not_assessed'][0]['scope'] == 'curve XY dimensions and narrow-space width'
    assert audit['contact_check']['straight_prism_count'] == 0


def test_real_roof_blade_rejected_once_but_legal_shaft_width_is_not_expanded():
    blade = [[-1.7,-7.35],[-1.355,-7.476],[-.27,-2.593],[-.674,-2.484]]
    plan = single(blade, z=75.523, height=5.26)
    space = plan['templates']['main']['spaces'][0]
    space['role'] = 'electrical/mechanical'
    space['assumptions'] = ['Geometry-only narrow blade/parapet abstraction, not occupied room']
    with pytest.raises(ArchitecturalRegularizationError) as error:
        regularize_architectural_plan(plan, policy=POLICY)
    findings = error.value.audit['rejections']
    assert len(findings) == 1 and findings[0]['code'] == 'narrow_component_as_space'
    assert findings[0]['width_m'] == pytest.approx(.4180245852)
    assert 'main' in findings[0]['path'] and 'room' in findings[0]['path']
    shaft = single(rectangle(0,0,.4,2))
    shaft['templates']['main']['spaces'][0]['role'] = 'elevator'
    adopted,audit = regularize_architectural_plan(shaft, policy=POLICY)
    assert adopted['templates']['main']['spaces'][0]['polygon'] == shaft['templates']['main']['spaces'][0]['polygon']
    assert audit['diagnostics'][0]['type'] == 'narrow_space'


def test_small_roof_top_differences_align_without_merging_spaces():
    plan = single(rectangle(0,0,3,3), z=75.523, height=5.26)
    plan['templates']['second'] = single(rectangle(5,0,3,3))['templates']['main']
    plan['instances'].append(dict(id='F2', template='second', z=75.523,height=5.233))
    adopted,audit = regularize_architectural_plan(plan, policy=POLICY)
    assert [round(i['z']+i['height'],9) for i in adopted['instances']] == [80.8,80.8]
    assert [i['id'] for i in adopted['instances']] == ['F1','F2']
    assert any(d['type']=='rounded_height_coincidence' for d in audit['diagnostics'])


@pytest.mark.parametrize('axis',['xy','z'])
def test_rounding_cannot_close_a_real_gap(axis):
    plan = single(rectangle(0,0,3.02,3), height=3.02)
    points = rectangle(3.04,0,3,3) if axis=='xy' else rectangle(0,0,3.02,3)
    plan['templates']['second'] = single(points)['templates']['main']
    plan['instances'].append(dict(id='F2',template='second',z=3.04 if axis=='z' else 0,height=3))
    with pytest.raises(ArchitecturalRegularizationError) as error:
        regularize_architectural_plan(plan, policy=POLICY)
    assert error.value.audit['rejections'][0]['code'] == 'volume_contact_changed'


def test_repeat_has_uniform_height_and_shared_top_anchor():
    plan = single(rectangle(0,0,6,4))
    plan['instances'] = [dict(id='roof',template='main',z=9.084,height=3)]
    plan['repeats'] = [dict(id='tower',template='main',id_prefix='F',start=1,count=3,z=.024,
                            height=3.02,source_refs=['measured repeated facade'])]
    adopted,_ = regularize_architectural_plan(plan, policy=POLICY)
    expanded = sorted(expand_parametric_instances(adopted),key=lambda i:i['z'])
    assert [i['z'] for i in expanded] == [0,3,6,9]
    assert [i['height'] for i in expanded[:3]] == [3,3,3]
    assert adopted['repeats'][0]['source_refs'] == plan['repeats'][0]['source_refs']


def test_repeat_does_not_accumulate_a_main_height_error():
    plan = single()
    plan['instances'] = []
    plan['repeats'] = [dict(id='tower',template='main',id_prefix='F',start=1,count=20,z=0,
                            height=3.149,source_refs=['test'])]
    with pytest.raises(ArchitecturalRegularizationError) as error:
        regularize_architectural_plan(plan, policy=POLICY)
    assert error.value.audit['rejections'][0]['code'] == 'repeat_endpoint_conflict'


def test_generic_ids_metadata_and_absolute_opening_heights_survive(tmp_path):
    raw = expand_parametric_proposal(shared_plan())
    raw['mesh_frame'] = {'schema_version':'mesh_bim_frame_v1','units':'metres',
        'mesh_to_bim':{'yaw_degrees':0,'translation_m':[0,0,0]},'basis':'test','source_refs':['test']}
    # Frame shape is intentionally irrelevant to this pure transform; kernel validation is separate.
    raw.pop('mesh_frame')
    raw['component_attributes'] = []
    before = copy.deepcopy(raw)
    adopted,audit = regularize_architectural_proposal(raw, policy=POLICY)
    assert raw == before
    assert adopted['geometry']['openings'][0]['z'] == [0,2.1]
    assert adopted['geometry']['windows'][0]['z'] == [.8,2.2]
    assert adopted['geometry']['windows'][0]['span'] == [1,2]
    assert adopted['component_attributes'] == []
    for old,new in zip(raw['geometry']['openings'],adopted['geometry']['openings']):
        assert {k:v for k,v in old.items() if k not in ('p1','p2','z')} == {k:v for k,v in new.items() if k not in ('p1','p2','z')}
    assert [c['id'] for c in adopted['geometry']['floors'][0]['cells']] == ['F1:hall','F1:room']
    assert audit['adapter'] == 'generic_floor_cells'
    report = export_source_proposal(adopted,tmp_path/'generic')
    assert report['source_geometry_ready'], report


def test_generic_optional_role_and_unique_implicit_window_host_preserve_input_fields():
    raw = expand_parametric_proposal(shared_plan())
    del raw['geometry']['floors'][0]['cells'][0]['role']
    del raw['geometry']['windows'][0]['room']
    adopted,_ = regularize_architectural_proposal(raw, policy=POLICY)
    assert 'role' not in adopted['geometry']['floors'][0]['cells'][0]
    assert 'room' not in adopted['geometry']['windows'][0]
    assert adopted['geometry']['windows'][0]['span'] == [1,2]


@pytest.mark.parametrize('offset', [0, .024])
def test_generic_shared_footprint_is_declared_and_can_be_regularized(tmp_path, offset):
    from tests.test_source_proposal import _proposal

    raw = _proposal()
    geometry = raw['geometry']
    for key in ('footprint_x', 'footprint_y'):
        geometry[key] = [v+offset for v in geometry[key]]
    for floor in geometry['floors']:
        floor['z_floor'] += offset
        for cell in floor['cells']:
            for key in ('x', 'y'):
                cell[key] = [v+offset for v in cell[key]]
    for opening in geometry['openings']:
        for key in ('p1', 'p2', 'z'):
            opening[key] = [v+offset for v in opening[key]]
    for window in geometry['windows']:
        for key in ('span', 'z'):
            window[key] = [v+offset for v in window[key]]
    before = copy.deepcopy(raw)
    adopted,audit = regularize_architectural_proposal(raw, policy=POLICY)
    assert raw == before and 'footprint' not in raw['geometry']['floors'][0]
    floor = adopted['geometry']['floors'][0]
    assert floor['footprint']['vertices'] == [[0,0],[6,0],[6,6],[0,6]]
    assert [c['id'] for c in floor['cells']] == ['hall','room']
    assert adopted['geometry']['openings'][0]['p1'] == [3,1]
    assert adopted['geometry']['openings'][0]['z'] == [0,2.1]
    basis = next(row for row in audit['diagnostics'] if row.get('basis')=='shared_footprint')
    assert basis['footprint_x'] == before['geometry']['footprint_x']
    report = export_source_proposal(adopted, tmp_path/'legacy')
    assert report['source_geometry_ready'], report
    assert report['counts']['spaces'] == 2 and report['counts']['openings'] == 2


def test_existing_component_bindings_are_not_silently_rewritten():
    raw = expand_parametric_proposal(single())
    raw['component_attributes'] = [{'host_identity':'sentinel exact binding', 'thickness_m':.123}]
    before = copy.deepcopy(raw)
    with pytest.raises(ArchitecturalRegularizationError) as error:
        regularize_architectural_proposal(raw, policy=POLICY)
    assert error.value.audit['rejections'][0]['code'] == 'component_rebinding_required'
    assert raw == before


def test_disabled_policy_is_exact_identity_and_cannot_be_weakened_by_embedded_policy():
    raw = single()
    adopted,audit = regularize_architectural_plan(raw)
    assert adopted == raw and adopted is not raw
    assert audit['status'] == 'not_requested'
    raw['modelling'] = {'grid_m':.001}
    with pytest.raises(ArchitecturalRegularizationError) as error:
        regularize_architectural_plan(raw, policy=POLICY)
    assert error.value.audit['rejections'][0]['code'] == 'conflicting_policy'
