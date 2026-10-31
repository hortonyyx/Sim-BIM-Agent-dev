import asyncio
import json
from pathlib import Path

import pytest

from src.agent.runtime_entry import execute, parser
from src.agent.runtime_configuration import argv_for, load_configuration
from src.agent_runtime.estimation import get_model_profile
from src.agent_runtime.output_limits import default_output_tokens, validate_output_limit
from src.agent_runtime.loop import RunLimits

from test_agent_runtime import MESSAGES, response, runtime


ROOT = Path(__file__).resolve().parents[1]


def test_reasoning_model_defaults_have_observed_basis():
    for name, minimum in (("glm-5.3-flash", 32000), ("Qwen3.8-27B", 16384), ("Qwen3.8-Flash", 16384)):
        profile = get_model_profile(name)
        assert default_output_tokens(name) == minimum
        assert "observed" in profile.output_limit_source.lower()
        assert "tokens" in profile.output_limit_source
        validate_output_limit(name, minimum)
        with pytest.raises(ValueError, match="below the recommended minimum"):
            validate_output_limit(name, minimum - 1)
    assert default_output_tokens("scripted-model") == 2048


@pytest.mark.parametrize("reason", ["", " ", True, 1])
def test_override_requires_a_written_reason(reason):
    with pytest.raises(ValueError, match="non-empty reason"):
        validate_output_limit("GLM-5.3-Flash", 8, reason=reason)


def test_cli_rejects_low_cap_before_creating_output_or_reading_credentials(tmp_path):
    out = tmp_path / "rejected-start"
    args = parser().parse_args(["--out", str(out), "--run-root", str(tmp_path), "--provider", "paratera",
        "--model", "GLM-5.3-Flash", "--output-tokens", "8", "--credentials-file", "/does/not/exist"])
    with pytest.raises(ValueError, match="recommended minimum 32000"):
        asyncio.run(execute(args))
    assert not out.exists()


def test_runtime_override_is_local_audited_and_not_sent_to_provider(tmp_path):
    engine = runtime(tmp_path, [response(text="OK")])
    engine.model = "GLM-5.3-Flash"
    engine.parameters["max_tokens"] = 8
    with engine.store:
        with pytest.raises(ValueError, match="recommended minimum 32000"):
            asyncio.run(engine.run(MESSAGES))
        assert engine.adapter.requests == [] and engine.store.events == []
        engine.low_output_limit_reason = "Deliberately small cap for a text-only protocol probe."
        result = asyncio.run(engine.run(MESSAGES))
        assert result["status"] == "completed"
        assert result["output_limit_policy"]["override_reason"] == engine.low_output_limit_reason
        assert "low_output_limit_reason" not in json.loads(engine.adapter.requests[0])
        config = engine._config()
        assert config["low_output_limit_reason"] == engine.low_output_limit_reason


def test_budget_output_reduction_does_not_silently_drop_below_recommendation(tmp_path):
    limits = RunLimits(model_calls=2, tool_calls=0, seconds=30, tokens=10_000, near_limit="reduce_output")
    engine = runtime(tmp_path, [response(text="forbidden")], limits=limits)
    engine.model = "GLM-5.3-Flash"
    engine.parameters["max_tokens"] = 32000
    with engine.store:
        result = asyncio.run(engine.run(MESSAGES))
        assert result["status"] == "token_budget_exhausted"
        assert result["model_calls"] == 0




def test_explicit_output_override_reaches_command_without_mutating_configuration(tmp_path):
    from tests.current_runtime_fixtures import configuration
    value = configuration(provider="paratera", model="GLM-5.3-Flash")
    value["cases"][0]["output_tokens"] = 16384
    path = tmp_path / "configuration.json"
    path.write_text(json.dumps(value), encoding="utf-8")
    original = path.read_bytes()
    with pytest.raises(ValueError, match="recommended minimum 32000"):
        load_configuration(path)
    reason = "Text-only protocol test with a deliberately small allowance."
    loaded = load_configuration(path, low_output_limit_reason=reason)
    argv = argv_for(loaded["cases"][0])
    assert argv[argv.index("--low-output-limit-reason") + 1] == reason
    assert argv[argv.index("--output-tokens") + 1] == "16384"
    assert path.read_bytes() == original
