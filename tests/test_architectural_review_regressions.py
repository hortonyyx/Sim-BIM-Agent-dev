"""Independent review regressions for staged architectural diagnostics."""
from __future__ import annotations

import copy
import math

import pytest
from shapely.geometry import Polygon

import src.agent.geometry.architectural_regularization as regularization
from src.agent.geometry.architectural_regularization import (
    ArchitecturalRegularizationError,
    DEFAULT_ARCHITECTURAL_POLICY,
    regularize_architectural_plan,
)
from src.agent.geometry.ring_failure_diagnostics import ring_failure_diagnostic
from tests.test_architectural_regularization import single


def test_duplicate_consecutive_vertices_report_all_rings_before_any_adoption(monkeypatch):
    raw = {
        'schema_version': 'source_polygon_v1',
        'templates': {},
        'instances': [],
        'assumptions': ['Synthetic independent rings with repeated observations'],
        'unresolved': [],
    }
    expected_paths = set()
    for name, offset in [('first', 0), ('second', 10)]:
        ring = [[offset + .024, .024], [offset + 4.024, .024],
                [offset + 4.024, .024], [offset + 4.024, 3.024],
                [offset + .024, 3.024]]
        # GEOS accepts these polygons, so the supporting-edge collection must
        # diagnose the repeated points without aborting other independent rings.
        assert Polygon(ring).is_valid and Polygon(ring).area > 0
        raw['templates'][name] = {
            'footprint': copy.deepcopy(ring),
            'spaces': [{'id': 'room', 'role': 'office', 'polygon': ring,
                        'source_refs': ['synthetic repeated boundary point']}],
        }
        raw['instances'].append({'id': name.upper(), 'template': name, 'z': 0, 'height': 3})
        expected_paths.update({f'templates.{name}.footprint',
                               f'templates.{name}.spaces[room].polygon'})
    # A downstream host failure must not replace the complete input-ring report.
    raw['templates']['first']['doors'] = [{
        'id': 'later', 'space': 'room', 'other_space': None,
        'p1': [500, 500], 'p2': [500, 501], 'z': [0, 2],
        'source_refs': ['deliberately invalid later-stage host'],
    }]
    before = copy.deepcopy(raw)

    def downstream_must_not_run(*args, **kwargs):
        pytest.fail('Invalid input rings must block all geometry adoption and downstream checks.')

    monkeypatch.setattr(regularization, '_snap', downstream_must_not_run)
    monkeypatch.setattr(regularization, '_vertical', downstream_must_not_run)
    monkeypatch.setattr(regularization, '_protect_volume_contacts', downstream_must_not_run)

    with pytest.raises(ArchitecturalRegularizationError) as error:
        regularize_architectural_plan(raw, policy=DEFAULT_ARCHITECTURAL_POLICY)

    audit = error.value.audit
    assert audit['status'] == 'rejected'
    assert len(audit['rejections']) == len(expected_paths)
    assert {item['code'] for item in audit['rejections']} == {'degenerate_edge'}
    assert {item['path'] for item in audit['rejections']} == expected_paths
    blocked = {item['path'] for item in audit['not_assessed']
               if item.get('blocked_by') == 'input_rings'}
    assert {'ring_adoption', 'apertures', 'vertical', 'contacts'} <= blocked
    assert audit['changes'] == []
    assert raw == before


@pytest.mark.parametrize('yaw,origin', [(0, [0, 0]), (29, [120.3, -44.2]), (-47, [-8.1, 17.6])])
def test_input_self_intersection_reports_verified_edges_in_source_coordinates(yaw, origin):
    c, s = math.cos(math.radians(yaw)), math.sin(math.radians(yaw))
    def world(p):
        return [origin[0] + c*p[0] - s*p[1], origin[1] + s*p[0] + c*p[1]]
    raw = single(list(map(world, [[0, 0], [4, 4], [0, 4], [4, 0]])))
    before = copy.deepcopy(raw)
    with pytest.raises(ArchitecturalRegularizationError) as caught:
        regularize_architectural_plan(raw, policy=DEFAULT_ARCHITECTURAL_POLICY)
    findings = caught.value.audit['rejections']
    assert len(findings) == 2
    assert {f['code'] for f in findings} == {'invalid_ring'}
    for finding in findings:
        diagnostic = finding['geometry_diagnostic']
        assert diagnostic['basis'] == 'input_ring'
        assert not diagnostic['is_valid']
        assert diagnostic['location_status'] == 'located'
        assert diagnostic['validity_location_xy'] == pytest.approx(world([2, 2]))
        assert diagnostic['intersection_samples'][0]['edge_indices'] == [0, 2]
        assert diagnostic['intersection_samples'][0]['intersection']['coordinates'] == pytest.approx(world([2, 2]))
        assert not diagnostic['truncated']
    assert raw == before
    assert caught.value.audit['changes'] == []


def test_numerical_zero_width_connection_explains_failed_adoption_without_repair():
    # A bridge only 10 femtometres wide is not a robust physical connection.
    # Preserve its actual input rather than calling it a measured positive gap.
    ring = [[0, 0], [4, 0], [4, 4], [2, 4], [2, 1e-14],
            [1, 1e-14], [1, 4], [0, 4]]
    assert Polygon(ring).is_valid
    raw = single(ring)
    before = copy.deepcopy(raw)
    with pytest.raises(ArchitecturalRegularizationError) as caught:
        regularize_architectural_plan(raw, policy=DEFAULT_ARCHITECTURAL_POLICY)
    findings = caught.value.audit['rejections']
    assert {f['code'] for f in findings} == {'unsafe_polygon_change'}
    for finding in findings:
        diagnostic = finding['geometry_diagnostic']
        assert diagnostic['source']['is_valid']
        assert not diagnostic['is_valid']
        assert diagnostic['location_status'] == 'located'
        assert any(sample['source_segment_distance_m'] == pytest.approx(1e-14, abs=1e-16)
                   for sample in diagnostic['intersection_samples'])
        index = int(diagnostic['full_ring_audit_pointer'].rsplit('/', 1)[1])
        attempt = caught.value.audit['changes'][index]
        assert attempt['type'] == 'rejected_ring_adoption' and not attempt['applied']
        assert attempt['source_ring'] == ring
        assert not Polygon(attempt['attempted_ring']).is_valid
    assert raw == before


def test_localized_samples_are_bounded_and_explicitly_not_exhaustive():
    ring = [[0, 0], [2, 0], [0, 2]] * 12
    before = copy.deepcopy(ring)
    diagnostic = ring_failure_diagnostic(ring, epsilon=regularization.EPS)
    assert diagnostic['candidate_edges_truncated']
    assert diagnostic['truncated']
    assert diagnostic['examined_pair_count'] <= 120
    assert len(diagnostic['intersection_samples']) <= 4
    assert ring == before


def test_unlocatable_and_short_ring_diagnostics_do_not_invent_an_edge_pair():
    # Valid but below the existing area/edge budget: GEOS has no error point.
    tiny = [[0, 0], [1e-10, 0], [0, 1e-10]]
    diagnostic = ring_failure_diagnostic(tiny, epsilon=regularization.EPS)
    assert diagnostic['is_valid']
    assert diagnostic['location_status'] == 'unlocated'
    assert diagnostic['intersection_samples'] == []
    assert diagnostic['short_edge_count'] == 3
    incomplete = ring_failure_diagnostic([[0, 0], [1, 0]], epsilon=regularization.EPS)
    assert incomplete['location_status'] == 'unlocated'
    assert incomplete['unlocated_reason']


def test_normal_adoption_does_not_run_failure_diagnostics(monkeypatch):
    raw = single()
    before = copy.deepcopy(raw)
    expected, expected_audit = regularize_architectural_plan(raw, policy=DEFAULT_ARCHITECTURAL_POLICY)
    def unexpected(*args, **kwargs):
        pytest.fail('A successful ring must not run the failure-only diagnostic.')
    monkeypatch.setattr(regularization, 'ring_failure_diagnostic', unexpected)
    actual, actual_audit = regularize_architectural_plan(raw, policy=DEFAULT_ARCHITECTURAL_POLICY)
    assert actual == expected and actual_audit == expected_audit
    assert raw == before
