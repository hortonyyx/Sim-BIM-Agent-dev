"""Cross-storey membership must not duplicate a shaft or add intermediate slabs."""
import json
import pytest
from src.agent.geometry_model.schema import GeometryInput, Floor, Cell, FootprintRing
from src.agent.execution.source_proposal import export_source_proposal
from src.agent.geometry.source_bim import build_source_bim


def geometry():
    floors = [Floor(name=f'F{i+1}', z_floor=3*i, ceiling_height=3,
        cells=[Cell(id=f'room{i+1}', x=[0,3], y=[0,4])],
        footprint=FootprintRing(vertices=[(0,0),(4,0),(4,4),(0,4)]),
        spanning_space_ids=['core']) for i in range(2)]
    floors.append(Floor(name='CORE', z_floor=0, ceiling_height=6,
        cells=[Cell(id='core', role='stairwell', x=[3,4], y=[0,4])],
        footprint=FootprintRing(vertices=[(3,0),(4,0),(4,4),(3,4)])))
    return GeometryInput(schema_version='2', footprint_x=[0,4], footprint_y=[0,4], floors=floors, windows=[])


def test_reference_covers_storeys_without_new_space_or_middle_slab(tmp_path):
    geom = geometry()
    source = build_source_bim(geom, capability_profile='orthogonal_polygon')
    assert source['validation']['status'] == 'pass'
    assert len(source['spaces']) == 3
    assert source['floors'][0]['spanning_space_ids'] == ['core']
    horizontal = [b for b in source['boundaries'] if b['space_id']=='core' and b['geometry_type']!='wall']
    assert len(horizontal) == 2
    assert sorted({v[2] for b in horizontal for v in b['vertices']}) == [0,6]
    proposal = {'geometry':geom.model_dump(mode='json'),'assumptions':[],'unresolved':[]}
    report = export_source_proposal(proposal, tmp_path/'roundtrip')
    assert report['source_geometry_ready']
    saved = json.loads((tmp_path/'roundtrip/source_model.json').read_text())
    assert saved['spaces'] == source['spaces']
    assert saved['boundaries'] == source['boundaries']


@pytest.mark.parametrize('change,match', [('unknown','unknown spanning'),('short','whole storey height'),('duplicate','duplicate spanning'),('local','already a local member')])
def test_invalid_reference_cannot_hide_missing_coverage(change, match):
    geom = geometry()
    if change=='unknown': geom.floors[0].spanning_space_ids=['missing']
    if change=='short': geom.floors[2].ceiling_height=5
    if change=='duplicate': geom.floors[0].spanning_space_ids=['core','core']
    if change=='local': geom.floors[0].spanning_space_ids=['room1']
    with pytest.raises(ValueError, match=match):
        build_source_bim(geom, capability_profile='orthogonal_polygon')


def test_undeclared_core_still_fails_coverage_and_empty_field_keeps_old_dump():
    geom=geometry()
    for floor in geom.floors: floor.spanning_space_ids=[]
    assert all('spanning_space_ids' not in f.model_dump(mode='json') for f in geom.floors)
    source=build_source_bim(geom, capability_profile='orthogonal_polygon')
    assert any(f['code']=='source.floor_coverage' for f in source['validation']['findings'])
    assert all('spanning_space_ids' not in f for f in source['floors'])
