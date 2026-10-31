"""The current Sim-BIM MCP server keeps stdout exclusively for protocol frames."""
import subprocess
import sys

from tests.test_bim_agent_tools import SERVER, ROOT, _run_with_one_image


def test_mcp_stdio_startup_keeps_stdout_protocol_clean(tmp_path):
    run = _run_with_one_image(tmp_path)
    result = subprocess.run(
        [sys.executable, "-X", "utf8", "-B", str(SERVER), "serve", str(run)],
        cwd=ROOT, input="", text=True, capture_output=True, timeout=120, check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == ""
