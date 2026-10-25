"""Construction properties independent of any historical building coordinates."""
import copy
import itertools
import math

import pytest
from shapely.geometry import Polygon
from shapely.ops import unary_union

from src.agent.geometry.architectural_regularization import (
    ArchitecturalRegularizationError, DEFAULT_ARCHITECTURAL_POLICY as POLICY,
    regularize_architectural_plan,
)
from src.agent.geometry.parametric_proposal import expand_parametric_proposal
from src.agent.execution.source_proposal import export_source_proposal
from tests.test_architectural_junctions import junction_plan
from tests.test_architectural_regularization import single


def assert_partition(template):
    fp = template['footprint']
    outline = Polygon(fp if isinstance(fp, list) else fp['vertices'],
                      [] if isinstance(fp, list) else fp.get('holes', []))
    spaces = [Polygon(s['polygon'], s.get('holes', [])) for s in template['spaces']]
    assert outline.symmetric_difference(unary_union(spaces)).area < 1e-9
    assert sum(s.area for s in spaces) - unary_union(spaces).area < 1e-9


def assert_parallel(before, after):
    for a, b, aa, bb in zip(before, before[1:] + before[:1], after, after[1:] + after[:1]):
        assert (b[0]-a[0])*(bb[1]-aa[1]) - (b[1]-a[1])*(bb[0]-aa[0]) == pytest.approx(0, abs=1e-8)


@pytest.mark.parametrize('origin,yaw', [([0, 0], 0), ([120.25, -70.4], 31), ([-16.2, 27.8], -47)])
def test_incidence_is_covariant_with_explicit_frame_and_preserves_directions(origin, yaw, tmp_path):
    plan = junction_plan()
    c, s = math.cos(math.radians(yaw)), math.sin(math.radians(yaw))
    def world(p):
        x, y = p
        return [origin[0] + c*x - s*y, origin[1] + s*x + c*y]
    t = plan['templates']['main']
    t['footprint'] = list(map(world, t['footprint']))
    for space in t['spaces']:
        space['polygon'] = list(map(world, space['polygon']))
    plan['modelling'] = {'frames': [dict(id='frame', origin=origin, yaw_degrees=yaw, templates=['main'])]}
    before = copy.deepcopy(plan)
    adopted, audit = regularize_architectural_plan(plan, policy=POLICY)
    assert plan == before
    target = adopted['templates']['main']
    assert_partition(target)
    assert_parallel(t['footprint'], target['footprint'])
    for old, new in zip(t['spaces'], target['spaces']):
        assert_parallel(old['polygon'], new['polygon'])
        assert new['polygon'][0] == pytest.approx(world([.5, .3]), abs=1e-9)
    assert audit['observed_max_vertex_displacement_m'] <= .1
    assert export_source_proposal(expand_parametric_proposal(adopted), tmp_path/'source')['source_geometry_ready']


def test_nearly_collinear_corner_uses_its_declared_third_support(tmp_path):
    a,b,c,d = [.024,.024], [8.024,.024], [8.024,6.024], [.024,6.024]
    low, joint, top, right = [3.024,.024], [3.02404,3.024], [3.024,6.024], [8.024,3.024]
    rings = [[a,low,joint,top,d], [low,b,right,joint], [joint,right,c,top]]
    plan = junction_plan()
    plan['templates']['main'] = dict(footprint=[a,b,c,d], spaces=[
        dict(id=str(i),role='office',polygon=ring,source_refs=['synthetic exact common junction'])
        for i,ring in enumerate(rings)])
    before = copy.deepcopy(plan)
    adopted, audit = regularize_architectural_plan(plan, policy=POLICY)
    assert plan == before
    target = adopted['templates']['main']
    assert_partition(target)
    points = [target['spaces'][0]['polygon'][2], target['spaces'][1]['polygon'][3],
              target['spaces'][2]['polygon'][0]]
    assert max(math.dist(a,b) for a in points for b in points) <= 1e-8
    assert audit['observed_max_vertex_displacement_m'] < .1
    for old, new in zip(rings, target['spaces']):
        assert_parallel(old,new['polygon'])
    assert export_source_proposal(expand_parametric_proposal(adopted), tmp_path/'source')['source_geometry_ready']


def test_incidence_does_not_depend_on_space_order_or_ring_winding():
    raw = junction_plan()
    expected, _ = regularize_architectural_plan(raw, policy=POLICY)
    raw['templates']['main']['footprint'].reverse()
    raw['templates']['main']['spaces'].reverse()
    for space in raw['templates']['main']['spaces']:
        space['polygon'].reverse()
    actual, _ = regularize_architectural_plan(raw, policy=POLICY)
    originals = {s['id']: Polygon(s['polygon']) for s in expected['templates']['main']['spaces']}
    for space in actual['templates']['main']['spaces']:
        assert originals[space['id']].hausdorff_distance(Polygon(space['polygon'])) < 1e-9


def test_unrelated_rounded_aperture_is_rejected_but_an_explicit_edge_span_is_valid():
    raw = single([[0,0],[3,4],[1,5],[-2,1]])
    door = dict(id='entry', space='room', p1=[.600048,.799964], p2=[1.200048,1.599964],
                z=[0,2], source_refs=['declared independently rounded door'])
    raw['templates']['main']['doors'] = [door]
    with pytest.raises(ArchitecturalRegularizationError) as caught:
        regularize_architectural_plan(raw, policy=POLICY)
    assert caught.value.audit['rejections'][0]['code'] == 'ambiguous_aperture_host'
    # Construct from the declared edge and distances instead of guessing a
    # nearest edge for the rejected coordinates. Width and evidence survive.
    a,b = raw['templates']['main']['spaces'][0]['polygon'][:2]
    length = math.dist(a,b)
    door['p1'],door['p2'] = [[x+(y-x)*distance/length for x,y in zip(a,b)] for distance in (1.,2.)]
    adopted,_ = regularize_architectural_plan(raw, policy=POLICY)
    moved = adopted['templates']['main']['doors'][0]
    assert math.dist(moved['p1'],moved['p2']) == pytest.approx(1.)
    assert moved['source_refs'] == door['source_refs']


@pytest.mark.parametrize('delta', [0., 1e-9, 3e-9, 7.5e-9, 1.1e-8])
def test_numerical_incidence_preserves_source_residual_and_is_order_stable(delta, tmp_path):
    raw = junction_plan()
    for i, space in enumerate(raw['templates']['main']['spaces']):
        space['polygon'][0] = [.524+i*delta, .34]
    assert export_source_proposal(expand_parametric_proposal(raw), tmp_path/'original')['source_geometry_ready']
    expected = None
    for order in itertools.permutations(range(3)):
        plan = copy.deepcopy(raw)
        plan['templates']['main']['spaces'] = [plan['templates']['main']['spaces'][i] for i in order]
        before = copy.deepcopy(plan)
        adopted, audit = regularize_architectural_plan(plan, policy=POLICY)
        assert plan == before
        shapes = {space['id']: space['polygon'] for space in adopted['templates']['main']['spaces']}
        assert audit['observed_max_vertex_displacement_m'] <= .1
        for old in raw['templates']['main']['spaces']:
            assert_parallel(old['polygon'], shapes[old['id']])
        if 0 < delta < 1e-8:
            # Near incidence is not exact incidence: the nanometre residuals
            # remain, instead of forcing distinct directions to collapse.
            for i in (1, 2):
                assert shapes[str(i)][0][0]-shapes['0'][0][0] == pytest.approx(i*delta, abs=1e-12)
        if expected is None:
            expected = shapes
            assert export_source_proposal(expand_parametric_proposal(adopted), tmp_path/'adopted')['source_geometry_ready']
        else:
            for identity, ring in shapes.items():
                assert Polygon(expected[identity]).hausdorff_distance(Polygon(ring)) < 1e-9
        for change in audit['changes']:
            if change['type'] == 'shared_incidence_anchors':
                assert all(node['max_source_spread_m'] <= 1e-8 for node in change['nodes'])
