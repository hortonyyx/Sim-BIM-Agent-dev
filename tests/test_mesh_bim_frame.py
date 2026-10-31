"""Registration changes placement, not source partitions; projection uses real cameras."""
import copy
import json

import numpy as np
import pytest

from src.agent.geometry.mesh_bim_frame import (
    validate_mesh_frame, source_to_observation, observation_to_source, render_mesh_bim_overlay,
)
from src.agent.execution.source_proposal import export_source_proposal
from src.agent.geometry.proposal_edits import apply_proposal_edits
from src.agent.geometry.source_bim import build_source_bim
from src.agent.geometry.source_model import _digest
from test_source_bim import three_rooms
from test_source_proposal import _proposal


def _frame():
    return dict(mesh_sha256='a'*64, yaw_degrees=90, translation_m=[3, 4, 5],
                reason='synthetic reference', source_refs=['synthetic wall plane'])


def test_frame_transform_order_and_inverse_across_observation_yaws():
    # Raw (2,1,3) -> source R90*(2,1,3)+(3,4,5)=(2,6,8).
    frame = _frame()
    assert observation_to_source([[2,1,3]], frame, 0).tolist() == [[2,6,8]]
    np.testing.assert_allclose(source_to_observation([[2,6,8]], frame, 90), [[-1,2,3]])
    np.testing.assert_allclose(observation_to_source([[-1,2,3]], frame, 90), [[2,6,8]])
    with pytest.raises(ValueError, match='finite'):
        validate_mesh_frame({**frame, 'yaw_degrees': float('nan')})


def test_frame_survives_source_export_and_local_notes_without_changing_geometry(tmp_path):
    proposal = _proposal()
    before = copy.deepcopy(proposal)
    export_source_proposal(proposal, tmp_path/'original')
    proposal['mesh_frame'] = _frame()
    edited = apply_proposal_edits(proposal, [{'op':'set_notes', 'assumptions':['new placement'],
                                            'unresolved':['shape unverified']}])
    report = export_source_proposal(edited, tmp_path/'updated')
    assert report['source_geometry_ready'], report
    original = json.loads((tmp_path/'original/source_model.json').read_text(encoding="utf-8"))
    updated = json.loads((tmp_path/'updated/source_model.json').read_text(encoding="utf-8"))
    assert updated['mesh_frame'] == proposal['mesh_frame']
    assert updated['source_model_sha256'] != original['source_model_sha256']
    for key in ('spaces', 'boundaries', 'openings', 'connections'):
        assert original[key] == updated[key]
    assert proposal['geometry'] == before['geometry']


def test_overlay_uses_saved_camera_rotation_and_explicit_frame(tmp_path):
    from PIL import Image
    proposal = _proposal(); proposal['mesh_frame'] = _frame()
    export_source_proposal(proposal, tmp_path/'source')
    source = json.loads((tmp_path/'source/source_model.json').read_text(encoding="utf-8"))
    observation = {'mesh_sha256':'a'*64, 'resolution_px':{'width':200,'height':200},
        'source_coordinate_transform': {'yaw_degrees_counterclockwise_about_positive_z':0},
        'camera':{'target':[0,0,0], 'screen_right':[1,0,0], 'screen_up':[0,1,0]},
        'view_span_m':{'width':20,'height':20}}
    image = Image.new('RGB', (200,200), 'white')
    pixels = image.tobytes()
    _, record = render_mesh_bim_overlay(image, observation, source, floor_id='F1')
    first = record['objects'][0]
    boundary = next(b for b in source['boundaries'] if b['id'] == first['id'])
    # Independent inverse of a quarter-turn: raw=(source_y-4, 3-source_x, source_z-5).
    x,y,z = boundary['vertices'][0]
    np.testing.assert_allclose(first['observation_xyz'][0], [y-4,3-x,z-5], atol=1e-10)
    np.testing.assert_allclose(first['projected_pixels'][0], [(y-4)*10+99.5,99.5-(3-x)*10])
    assert record['opening_count'] == 1  # exterior window; internal door excluded
    assert record['fitted_to_mesh'] is False and image.tobytes() == pixels
    with pytest.raises(ValueError, match='different assets'):
        render_mesh_bim_overlay(image, {**observation,'mesh_sha256':'b'*64}, source)
    with pytest.raises(ValueError, match='unknown floor'):
        render_mesh_bim_overlay(image, observation, source, floor_id='absent')


def test_exterior_overlay_preserves_exposed_part_of_partially_contacted_wall():
    from PIL import Image

    geom = three_rooms()
    geom.floors[0].cells.pop()  # Hall's long east wall now contacts a room over half its length.
    source = build_source_bim(geom, capability_profile='orthogonal_polygon')
    source['mesh_frame'] = dict(mesh_sha256='a'*64, yaw_degrees=0,
                                translation_m=[0, 0, 0], reason='identity test frame',
                                source_refs=['synthetic partial contact'])
    source['source_model_sha256'] = _digest(
        {key: value for key, value in source.items() if key != 'source_model_sha256'})
    observation = {'mesh_sha256':'a'*64, 'resolution_px':{'width':200,'height':200},
        'source_coordinate_transform': {'yaw_degrees_counterclockwise_about_positive_z':0},
        'camera':{'target':[3,3,1.5], 'screen_right':[1,0,0], 'screen_up':[0,0,1]},
        'view_span_m':{'width':8,'height':4}}

    _, record = render_mesh_bim_overlay(
        Image.new('RGB', (200, 200), 'white'), observation, source,
        floor_id='F1', exterior_only=True)

    full_wall = next(
        boundary for boundary in source['boundaries']
        if boundary['space_id'] == 'hall' and boundary['geometry_type'] == 'wall'
        and boundary['adjacent_space_ids'] == ['a'])
    exposed = [row for row in record['objects'] if row['id'] == full_wall['id']]
    assert len(exposed) == 1
    points = np.asarray(exposed[0]['observation_xyz'])
    assert set(points[:, 0]) == {3.0}
    assert set(points[:, 1]) == {3.0, 6.0}
    assert set(points[:, 2]) == {0.0, 3.0}
