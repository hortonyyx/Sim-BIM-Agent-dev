"""Real offline common-tool calls through the development client."""
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts.tool_scripts.bim_agent_bridge import invoke
from scripts.tool_scripts import run_bim_agent as runner
from test_bim_agent_tools import _run_with_one_image


def test_bridge_preserves_images_records_calls_and_excludes_model_delegation(tmp_path):
    run = _run_with_one_image(tmp_path)
    catalog = asyncio.run(invoke(run))
    assert "review_detail" not in {row["name"] for row in catalog["tools"]}
    result = asyncio.run(invoke(run, [{"tool": "view_image", "arguments": {
        "name": "plan.png", "coordinate_grid": False}}]))
    assert result["completed"]
    images = [row for row in result["replies"][0]["content"] if row["type"] == "image"]
    from PIL import Image
    with Image.open(images[0]["path"]) as image:
        assert image.size == (12, 8)
    assert json.loads((Path(result['record']) / 'requests.json').read_text())[0]['tool'] == 'view_image'
    with pytest.raises(ValueError, match="admitted"):
        asyncio.run(invoke(run, [{"tool": "review_detail", "arguments": {}}]))
