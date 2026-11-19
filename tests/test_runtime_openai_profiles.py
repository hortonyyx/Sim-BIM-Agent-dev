from __future__ import annotations

import base64
import io
import math
from dataclasses import replace

import pytest
from PIL import Image

from src.agent_runtime.estimation import (
    estimate_chat_request,
    get_model_profile,
    openai_image_tokens,
)


def _image_url(width: int, height: int) -> str:
    stream = io.BytesIO()
    Image.new("RGB", (width, height), "white").save(stream, format="PNG")
    return "data:image/png;base64," + base64.b64encode(stream.getvalue()).decode("ascii")


@pytest.mark.parametrize("model", ["gpt-6-astra", "gpt-6-sol", "gpt-6-luna", "gpt-6.1-sol"])
def test_official_context_and_local_output_reservation_are_distinguished(model):
    profile = get_model_profile(model, strict=True)
    assert profile.canonical_name == model
    assert profile.context_window_tokens == 1_050_000
    assert profile.native_context_window_tokens == 1_050_000
    assert profile.endpoint_context_window_tokens is None
    assert profile.context_source == f"https://developers.openai.com/api/docs/models/{model}"
    assert "subscription endpoint" in profile.context_uncertainty
    assert profile.recommended_min_output_tokens == 32_000
    assert "128,000-token maximum output" in profile.output_limit_source
    assert "not a provider-enforced cap" in profile.output_limit_source
    assert "no enforced service output cap" in profile.reasoning_allowance_source


@pytest.mark.parametrize("model", ["gpt-6.2-sol", "gpt-6.1-terra"])
def test_new_subscription_profiles_use_the_account_default(model):
    profile = get_model_profile(model, strict=True)
    assert profile.context_window_tokens == profile.endpoint_context_window_tokens == 500_000
    assert profile.native_context_window_tokens == 1_050_000
    assert profile.context_source == f"https://developers.openai.com/api/docs/models/{model}"
    assert profile.recommended_min_output_tokens == 32_000


@pytest.mark.parametrize(
    ("width", "height", "expected"),
    [(1024, 1024, (1229, 1024, 1024)),
     (2048, 2048, (3000, 1600, 1600)),
     (4096, 512, (2458, 4096, 512))],
)
def test_astra_high_matches_official_images_vision_examples(width, height, expected):
    # https://developers.openai.com/api/docs/guides/images-vision
    assert openai_image_tokens(
        width, height, get_model_profile("gpt-6-astra", strict=True), detail="high"
    ) == expected


def test_detail_changes_sizing_and_auto_preserves_original_resolution():
    profile = get_model_profile("gpt-6-astra", strict=True)
    assert openai_image_tokens(2048, 2048, profile, detail="low") == (308, 512, 512)
    assert openai_image_tokens(2048, 2048, profile, detail="high") == (3000, 1600, 1600)
    assert openai_image_tokens(2048, 2048, profile, detail="original") == (4916, 2048, 2048)
    assert openai_image_tokens(2048, 2048, profile, detail="auto") == (4916, 2048, 2048)
    assert openai_image_tokens(32, 16, profile, detail="low") == (2, 32, 16)


@pytest.mark.parametrize("detail", ["original", "auto"])
def test_original_patch_limit_rejects_instead_of_silently_downsizing(detail):
    profile = get_model_profile("gpt-6-astra", strict=True)
    with pytest.raises(ValueError, match="30000 patch limit"):
        openai_image_tokens(6144, 6144, profile, detail=detail)
    assert openai_image_tokens(6144, 6144, profile, detail="high")[0] == 3000


@pytest.mark.parametrize("detail", ["high", "original", "auto"])
def test_maximum_dimension_and_narrow_images_keep_positive_dimensions(detail):
    profile = get_model_profile("gpt-6-astra", strict=True)
    tokens, width, height = openai_image_tokens(131070, 66, profile, detail=detail)
    assert 0 < width <= 65535
    assert 0 < height <= 65535
    assert tokens == math.ceil(math.ceil(width / 32) * math.ceil(height / 32) * 1.2)
    if detail == "high":
        assert tokens <= 3000
    else:
        assert (width, height) == (65535, 33)


@pytest.mark.parametrize("model,family", [("gpt-6-sol", "Sol"),
                                        ("gpt-6-luna", "Luna"),
                                        ("gpt-6.1-sol", "Sol")])
def test_sol_and_luna_image_formula_is_explicitly_an_uncalibrated_proxy(model, family):
    profile = get_model_profile(model, strict=True)
    assert openai_image_tokens(2048, 2048, profile, detail="high") == (6000, 1600, 1600)
    assert "unverified_image_proxy" in profile.profile_status
    assert "UNVERIFIED PROXY" in profile.image_uncertainty
    assert f"not a {family} formula" in profile.image_uncertainty
    assert "provider hard upper bound" in profile.image_uncertainty
    with pytest.raises(ValueError, match="no reviewed estimation profile"):
        get_model_profile("gpt-6.1-astra", strict=True)


def test_request_uses_explicit_image_detail_and_retains_official_auto_default():
    image_url = {"url": _image_url(2048, 2048), "detail": "high"}
    body = {
        "model": "gpt-6-astra",
        "messages": [{"role": "user", "content": [
            {"type": "image_url", "image_url": image_url},
        ]}],
        "max_tokens": 32000,
    }
    high = estimate_chat_request(body, strict=True)
    assert high.image_tokens == 3000
    assert high.images[0].request_reference == "/messages/0/content/0/image_url/url"
    assert high.images[0].resized_width == 1600
    del image_url["detail"]
    auto = estimate_chat_request(body, strict=True)
    assert auto.image_tokens == 4916
    assert auto.reservation_tokens - high.reservation_tokens == 1916


@pytest.mark.parametrize("model", ["gpt-6-sol", "gpt-6-luna", "gpt-6.1-sol"])
def test_two_plan_images_fit_local_context_and_smaller_local_limit_is_honored(model):
    profile = get_model_profile(model, strict=True)
    body = {
        "model": model,
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": "Inspect the two plans 图纸."},
            *[{"type": "image_url", "image_url": {
                "url": _image_url(width, height), "detail": "high",
            }} for width, height in [(1600, 1200), (3200, 2400)]],
        ]}],
        "max_tokens": 32000,
    }
    estimate = estimate_chat_request(body, profile=profile, strict=True)
    assert 0 < estimate.image_tokens <= 12000
    assert estimate.reservation_tokens < 50000
    assert estimate.fits_context is True
    assert "UNVERIFIED PROXY" in estimate.uncertainty
    assert estimate_chat_request(body, profile=replace(
        profile, context_window_tokens=estimate.reservation_tokens - 1
    )).fits_context is False


@pytest.mark.parametrize("model", ["gpt-6-sol", "gpt-6-luna", "gpt-6.2-sol", "gpt-6.1-terra"])
def test_new_model_responses_keep_identity_and_image_bytes_with_a_local_reservation(model):
    from src.agent_runtime.providers import CHATGPT_SUBSCRIPTION, provider_parameters
    from src.agent_runtime.responses import build_responses_body, estimate_responses_request

    original = _image_url(3200, 2400)
    parameters = provider_parameters(CHATGPT_SUBSCRIPTION, output_tokens=32_000,
                                     reasoning_effort="high")
    body = build_responses_body(model=model, messages=[{"role": "user", "content": [
        {"type": "image_url", "image_url": {"url": original}},
    ]}], tools=[], parameters=parameters)
    assert body["model"] == model
    assert body["reasoning"] == {"effort": "high"}
    assert body["input"][0]["content"][0] == {
        "type": "input_image", "image_url": original, "detail": "high"}
    assert not {"max_tokens", "max_completion_tokens", "max_output_tokens"} & body.keys()
    estimate = estimate_responses_request(body, output_token_limit=32_000, strict=True)
    assert estimate.model == model
    assert 0 < estimate.image_tokens <= 6000
    assert estimate.reservation_tokens == estimate.input_tokens_upper_bound + 32_000
    assert "UNVERIFIED PROXY" in estimate.uncertainty


@pytest.mark.parametrize("width,height,detail", [(0, 1, "high"), (1, -1, "high"),
                                                (True, 1, "high"), (1, 1, "invalid")])
def test_invalid_dimensions_or_detail_fail_before_a_false_estimate(width, height, detail):
    with pytest.raises(ValueError):
        openai_image_tokens(width, height, get_model_profile("gpt-6-astra"), detail=detail)
