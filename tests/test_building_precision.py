"""Precision diagnostics preserve geometry and distinguish actual near-line facts."""
import copy

import pytest

from src.agent.geometry_model.schema import GeometryInput
from src.agent.geometry.source_bim import build_source_bim
from src.agent.geometry.building_precision import precision_report


def source(offset=.06, gap=0):
    floors = []
    for index, x in enumerate((3., 3.+offset)):
        floors.append(dict(name=f'F{index+1}', z_floor=3*index+gap*index, ceiling_height=3, cells=[
            dict(id=f'L{index}', x=[0,x], y=[0,6]), dict(id=f'R{index}', x=[x,6], y=[0,6])]))
    geom = GeometryInput.model_validate(dict(schema_version='2', footprint_x=[0,6], footprint_y=[0,6],
        floors=floors, windows=[], openings=[]))
    return build_source_bim(geom, capability_profile='orthogonal_polygon')


def evidence(mpp=.02, thickness=.24):
    return [dict(floor_id=fid, metres_per_pixel=[mpp,mpp], wall_thickness_m=thickness,
                 basis='test plan scale and observed wall width') for fid in ('F1','F2')]


def test_offset_and_contact_are_reported_without_edits_or_average_target():
    model = source()
    before = copy.deepcopy(model)
    report = precision_report(model, floor_evidence=evidence())
    assert report['counts'] == {'storey_wall_offset':1, 'thin_horizontal_contact':1}
    wall = report['items'][0]
    assert wall['deviation_m'] == pytest.approx(.06)
    assert wall['overlap_m'] == 6 and wall['tolerance_m'] == .30
    assert {r['coordinate_m'] for r in wall['align_to_options']} == {3.,3.06}
    strip = report['items'][1]
    assert strip['width_m'] == pytest.approx(.06) and strip['length_m'] == 6
    assert strip['align_to_options']
    assert model == before


@pytest.mark.parametrize('offset', [0., .3, 1.])
def test_aligned_walls_and_real_large_setbacks_are_quiet(offset):
    assert precision_report(source(offset), floor_evidence=evidence())['items'] == []


def test_floor_slab_gap_does_not_disable_xy_alignment():
    report = precision_report(source(gap=.15), floor_evidence=evidence())
    assert report['counts'] == {'storey_wall_offset':1, 'vertical_gap_or_overlap':1}
    assert report['status'] == 'rejected'


def test_unknown_scale_and_wall_width_do_not_weaken_the_hard_threshold():
    report = precision_report(source())
    assert report['status'] == 'rejected'
    assert report['coverage']['floors_without_tolerance'] == []
    assert all(row['objects'] and row['fix'] for row in report['items'])
    report = precision_report(source(.1), floor_evidence=evidence(mpp=.1, thickness=.08))
    assert report['tolerances']['F1']['default_m'] == .30 and report['items']


def test_micrometre_strips_are_real_facts_but_numerical_roundoff_is_not():
    report = precision_report(source(.000009), floor_evidence=evidence())
    assert report['counts'] == {'storey_wall_offset':1, 'thin_horizontal_contact':1}
    assert precision_report(source(1e-8), floor_evidence=evidence())['items'] == []


def test_step_gap_and_thin_space_keep_objects_and_existing_lines():
    model = source(0)
    # Inspect read-only source fixtures, including gaps an upstream validator may reject.
    model['spaces'][0]['polygon'] = [[0,0],[2.94,0],[2.94,3],[3,3],[3,6],[0,6]]
    report = precision_report(model, floor_evidence=evidence())
    assert any(r['type']=='small_step' and r['width_m']==.06 for r in report['items'])
    assert any(r['type']=='thin_coverage_gap' for r in report['items'])
    model['spaces'][0]['polygon'] = [[2.94,0],[3,0],[3,6],[2.94,6]]
    report = precision_report(model, floor_evidence=evidence())
    assert any(r['type']=='thin_space' and r['space_id']=='L0' for r in report['items'])


def test_thin_shape_never_suggests_aligning_perpendicular_edges():
    model = source(0)
    model['spaces'][0]['polygon'] = [[2.94,0],[3,0],[3,6],[2.94,6]]
    # Only one long edge matches a source wall. The two short end walls cannot
    # become alternative alignment targets for that long edge.
    report = precision_report(model, floor_evidence=evidence())
    item = next(r for r in report['items'] if r['type']=='thin_space')
    assert {line['axis'] for line in item['align_to_options']} == {'x'}
    assert {line['coordinate_m'] for line in item['align_to_options']} == {3}
