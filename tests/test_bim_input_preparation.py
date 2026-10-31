"""Native input admission freezes originals and explicit run policies."""
import json

from PIL import Image
import pytest

from src.agent import runtime_entry
from src.agent.bim_inputs import prepare_bim_inputs


@pytest.mark.parametrize('names', [('1f_view.png', '2f_view.png', 'South.png'),
                                  ('1f_view.png', 'East.png'), ('1f_view.png', '2f_view.png', 'West.png')])
def test_native_input_freezing_preserves_originals_and_excludes_neighbors(tmp_path, names):
    images = tmp_path / 'originals'
    images.mkdir()
    for name in names:
        Image.new('RGB', (12, 8)).save(images / name)
    originals = {name: (images / name).read_bytes() for name in names}
    (images / 'private.json').write_text('not admitted', encoding='utf-8')
    run, _, _ = runtime_entry.prepare_inputs(tmp_path / 'runtime', images=images, mesh=None,
        building_input=None, scope='same input scope', image_kind='drawings', max_candidates=4,
        started_epoch=1000.0, seconds=60)
    manifest = json.loads((run / 'inputs.json').read_bytes())
    for name in names:
        assert (run / 'images' / name).read_bytes() == (images / name).read_bytes() == originals[name]
    assert not (run / 'images/private.json').exists()
    assert manifest['input_contents']['ground_truth_or_evaluation'] == {'included': False}
    assert manifest['lite_bim_grid_step_m'] == .1
    assert manifest['deadline_epoch'] == 1060.
    assert 'src/agent/bim_inputs.py' in manifest['implementation_sha256']


def test_identical_shared_function_options_write_identical_manifest_bytes(tmp_path):
    images = tmp_path / 'images'
    images.mkdir()
    Image.new('RGB', (2, 2)).save(images/'1f.png')
    options = dict(images_path=images, mesh_path=None, building_input_path=None,
        scope='shared', image_kind='drawings', max_candidates=2, started_epoch=1000., seconds=30.)
    for name in ('a', 'b'):
        prepare_bim_inputs(tmp_path/name, **options)
    assert (tmp_path/'a/inputs.json').read_bytes() == (tmp_path/'b/inputs.json').read_bytes()


def test_unlimited_runtime_inputs_preserve_clock_without_a_deadline(tmp_path):
    images = tmp_path / 'images'
    images.mkdir()
    image = images / '1f.png'
    Image.new('RGB', (2, 2)).save(image)
    original = image.read_bytes()
    run, _, _ = runtime_entry.prepare_inputs(tmp_path / 'runtime', images=images, mesh=None,
        building_input=None, scope='unlimited offline input', image_kind='drawings', max_candidates=2,
        started_epoch=1000.0, seconds=None)
    manifest = json.loads((run / 'inputs.json').read_bytes())
    assert manifest['started_epoch'] == 1000.0
    assert manifest['time_budget_seconds'] is None and manifest['deadline_epoch'] is None
    assert (run / 'images/1f.png').read_bytes() == image.read_bytes() == original


@pytest.mark.parametrize('image_kind', ['drawings', 'photos', 'unknown'])
def test_only_new_drawing_manifests_select_lite_grid_independently_of_scope(tmp_path, image_kind):
    images = tmp_path / 'images'
    images.mkdir()
    Image.new('RGB', (2, 2)).save(images/'plan.png')
    manifest = prepare_bim_inputs(tmp_path/'run', images_path=images, mesh_path=None,
        building_input_path=None, scope='model text requests a different 0.2 m grid',
        image_kind=image_kind, max_candidates=2)
    assert manifest.get('lite_bim_grid_step_m') == (.1 if image_kind == 'drawings' else None)
