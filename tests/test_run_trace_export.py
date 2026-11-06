import base64
import gzip
import hashlib
import io
import json

from PIL import Image
import pytest

from src.run_trace import export
from src.run_trace.media import TraceMedia
from src.run_trace.privacy import public_value


def test_dsh_runtime_case_discovery_and_responses_tool_images(tmp_path):
    session = tmp_path / "private/dsh/session"
    session.mkdir(parents=True)
    (session / "events.jsonl").write_text('{"type":"session","sessionId":"fixture"}\n')
    wire = tmp_path / "control/wire/dsh"
    wire.mkdir(parents=True)
    (tmp_path / "control/relay.jsonl").write_text('')
    source, = export.find_sources(tmp_path)
    assert source.directory == session and source.wire == wire
    data = io.BytesIO()
    Image.new("RGB", (2, 3)).save(data, "PNG")
    uri = "data:image/png;base64," + base64.b64encode(data.getvalue()).decode()
    request = {"input":[{"type":"function_call_output","call_id":"view", "output":[
        {"type":"input_image","image_url":uri}]}]}
    with gzip.open(wire / "01-out.json.gz", "wt", encoding="utf8") as stream:
        json.dump(request, stream)
    media = TraceMedia(tmp_path / "bundle")
    images = media.event_images({"kind":"model","turn":1}, session, wire)
    assert len(images) == 1
    assert (tmp_path / "bundle" / images[0]["src"]).read_bytes() == data.getvalue()


def test_offline_packet_is_lazy_and_preserves_existing_outputs(tmp_path, monkeypatch):
    run = tmp_path / "example"
    run.mkdir()
    (run / "events.jsonl").write_text('{"type":"session","sessionId":"example"}\n')
    sources = export.find_sources(run)
    assert sources[0].runtime == "deepseek"
    monkeypatch.setattr(export, "read_events", lambda *_: [{
        "id": "event-1", "kind": "model", "title": "模型",
        "detail": {"input": "one step", "reasoning_content": "private text",
                   "nested": '{"api_key":"sensitive-value"}', "usage": {"reasoning_tokens": 42}}}])
    out = tmp_path / "view"
    assert export.export_trace(sources, out, "Example") == out / "index.html"
    assert "one step" not in (out / "trace-data.js").read_text(encoding="utf8")
    detail = (out / "details/r1-e1.js").read_text(encoding="utf8")
    assert "one step" in detail and "reasoning_tokens" in detail
    assert "private text" not in detail and "sensitive-value" not in detail
    with pytest.raises(ValueError, match="already exists"):
        export.export_trace(sources, out, "Example")


def test_recorded_images_are_deduplicated_and_verified(tmp_path):
    data = io.BytesIO()
    Image.new("RGB", (2, 3)).save(data, "PNG")
    png = data.getvalue()
    digest = hashlib.sha256(png).hexdigest()
    wire = tmp_path / "wire"
    wire.mkdir()
    part = {"type": "image_url", "image_url": {"url": "data:image/png;base64," + base64.b64encode(png).decode()}}
    with gzip.open(wire / "01-out.json.gz", "wt", encoding="utf8") as f:
        json.dump({"messages": [{"role": "user", "content": [part, part]}]}, f)
    media = TraceMedia(tmp_path / "bundle")
    imgs = media.event_images({"kind": "model", "turn": 1}, tmp_path, wire)
    assert len(imgs) == 2 and imgs[0]["src"] == imgs[1]["src"] == f"media/{digest}.png"
    assert (tmp_path / "bundle" / imgs[0]["src"]).read_bytes() == png
    attachment = tmp_path / "dsh-home/attachments/v1/objects" / digest[:2] / digest
    attachment.parent.mkdir(parents=True)
    event = {"kind": "tool", "detail": {"images":[{"sha256": digest, "media_type": "image/png"}]}}
    attachment.write_bytes(b"unrelated")
    assert not media.event_images(event, tmp_path, None)
    attachment.write_bytes(png)
    assert media.event_images(event, tmp_path, None)[0]["src"] == imgs[0]["src"]
    blobs = tmp_path / "blobs"
    blobs.mkdir()
    original = io.BytesIO()
    Image.new("RGB", (4, 6)).save(original, "PNG")
    original_hash = hashlib.sha256(original.getvalue()).hexdigest()
    (blobs / original_hash).write_bytes(original.getvalue())
    (blobs / digest).write_bytes(png)
    native = {"kind": "model", "detail": {"request_event_id": "req-1", "images": [{
        "original": {"sha256": original_hash, "media_type": "image/png"},
        "sent": {"sha256": digest, "media_type": "image/png"},
        "request_reference": "/messages/1/content/0",
    }]}}
    assert media.event_images(native, tmp_path, None) == [
        {"src": imgs[0]["src"], "label": "请求用图 · /messages/1/content/0"}]


def test_harness_json_feedback_with_appended_footer_and_private_blocks():
    value = '{"accessToken":"test_credential","reasoning_content":"private_a"}\n剩余时间 60 秒'
    assert public_value(value).endswith("剩余时间 60 秒")
    exported = json.dumps(public_value({
        "toolResult": value,
        "text": "<think>private_b</think>公开回复",
        "provider_specific_fields": {"reasoning_details": [{"text": "private_c"}]},
    }), ensure_ascii=False)
    assert "test_credential" not in exported and "private_" not in exported and "公开回复" in exported
