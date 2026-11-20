"""Read-only Flash chat protocol probe with the installed DSH and a real BIM tool.

Local fixtures can exercise multiple input images and same-session resumption.
A single PNG and an explicit --go-credentials path select OpenCode Go subscription.
This development probe does not implement the product's building handoff.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
from collections import Counter
import copy
import hashlib
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
from pathlib import Path
import re
import secrets
import shutil
import sys
import threading
import time
from types import SimpleNamespace
from unittest.mock import patch
import uuid

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from scripts.dev.smoke_dsh_delivery import block_external_network
from src.agent.runtime_entry import prepare_inputs
from src.agent_runtime import opencode_go
from src.agent_runtime.connections import resolve_connection
from src.agent_runtime.providers import provider_parameters
from src.dsh_runtime import launcher
from src.dsh_runtime.profile import make_patch

MODEL = "deepseek-v4.1-flash"
TOOL = "mcp__sim_bim__view_image"
MAX_SECONDS = 300
MAX_CALLS = 3
OUTPUT_LIMIT = 4096


def flash_profile(base_url, model, *args, **kwargs):
    # DSH's native pi-ai chat adapter owns conversion to/from its transcript.
    profile = make_patch(base_url, model, *args, **kwargs)
    chat = """api: openai-completions
        compat:
          maxTokensField: max_tokens
          supportsStore: false
          supportsDeveloperRole: false
          supportsReasoningEffort: true
          supportsUsageInStreaming: true
          requiresReasoningContentOnAssistantMessages: true
          thinkingFormat: deepseek"""
    if "api: openai-responses" not in profile or "maxTokens: 128000" not in profile:
        raise ValueError("current DSH profile shape differs from the tested probe")
    return (profile.replace("api: openai-responses", chat)
                   .replace("maxTokens: 128000", f"maxTokens: {OUTPUT_LIMIT}")
                   .replace("GPT Sol subscription relay", "Flash observation probe"))


def sha(data):
    return hashlib.sha256(data).hexdigest()


def chat_sse(reply):
    choice, = reply["choices"]
    message = choice["message"]
    delta = {k: message[k] for k in ("role", "content", "reasoning_content") if k in message}
    if message.get("tool_calls"):
        delta["tool_calls"] = [dict(call, index=i) for i, call in enumerate(message["tool_calls"])]
    base = {"id": reply["id"], "model": reply["model"], "created": reply["created"],
            "object": "chat.completion.chunk"}
    parts = [
        {**base, "choices": [{"index": 0, "delta": delta, "finish_reason": None}]},
        {**base, "choices": [{"index": 0, "delta": {}, "finish_reason": choice["finish_reason"]}]},
    ]
    if "usage" in reply:
        parts.append({**base, "choices": [], "usage": reply["usage"]})
    return b"".join(b"data: " + json.dumps(p).encode() + b"\n\n" for p in parts) + b"data: [DONE]\n\n"


def identity(call):
    return (call["id"], call["type"], call["function"]["name"], json.loads(call["function"]["arguments"]))


def confirmed_results(messages, assistant, views):
    # pi-ai emits tool image bytes in a separate user message after the tool receipts.
    index = max(i for i, message in enumerate(messages) if message["role"] == "assistant")
    prior = messages[index]
    if ([identity(c) for c in prior.get("tool_calls", [])] != [identity(c) for c in assistant["tool_calls"]]
            or prior.get("reasoning_content", "") != (assistant.get("reasoning_content") or "")):
        raise ValueError("host did not replay the accepted Flash tool call and reasoning")
    *receipts, attachment = messages[index + 1:]
    expected = {cid: views[args["name"]]["returned_png_sha256"]
                for cid, _, _, args in map(identity, assistant["tool_calls"])}
    if (len(receipts) != len(expected) or any(m.get("role") != "tool" for m in receipts)
            or {m.get("tool_call_id") for m in receipts} != set(expected)):
        raise ValueError("host did not return all requested tool calls")
    for receipt in receipts:
        digest, = re.findall(r"Image sha256:([a-f0-9]{64});", receipt["content"])
        if digest != expected[receipt["tool_call_id"]]:
            raise ValueError("tool image receipt differs from original returned bytes")
    if attachment.get("role") != "user" or not isinstance(attachment.get("content"), list):
        raise ValueError("tool image attachment missing")
    label, *images = attachment["content"]
    if label != {"type": "text", "text": "Attached image(s) from tool result:"}:
        raise ValueError("unexpected host image attachment")
    actual = []
    for image in images:
        url = image.get("image_url", {}).get("url", "")
        if image.get("type") != "image_url" or not url.startswith("data:image/png;base64,"):
            raise ValueError("PNG evidence required")
        actual.append(sha(base64.b64decode(url.split(",", 1)[1], validate=True)))
    if Counter(actual) != Counter(expected.values()):
        raise ValueError("provider image differs from the view returned by the tool")
    return receipts, attachment, expected


def run_probe(args):
    if args.go_credentials is None:
        block_external_network()
    args.output.mkdir(parents=True, exist_ok=False)
    images = args.output / "input"
    images.mkdir()
    originals = {}
    for source in args.source:
        if source.suffix.lower() != ".png" or source.name in originals:
            raise ValueError("each source must be a uniquely named PNG")
        frozen = images / source.name
        shutil.copyfile(source, frozen)
        originals[source.name] = sha(frozen.read_bytes())
    started = time.monotonic()
    epoch = time.time()
    run, _, _ = prepare_inputs(args.output / "private/dsh/work", images=images, mesh=None,
        building_input=None, scope="Observe one original floor plan.", image_kind="drawings",
        max_candidates=1, started_epoch=epoch, seconds=MAX_SECONDS, orchestrator=True)
    session = args.output / "private/dsh/session"
    scope = (f"Read each of these plans: {', '.join(originals)}. Call {TOOL} once for each filename "
             "with coordinate_grid false to inspect the complete image. Then report "
             "two directly visible physical boundaries, their positions relative to the open space, "
             "and one uncertainty in at most 120 words. Use only the supplied plan.")
    (args.output / "shared").mkdir()
    (args.output / "shared/task.txt").write_text(scope, encoding="utf8")
    (args.output / "private/start.json").write_text(json.dumps({"epoch": epoch}), encoding="utf8")
    route = "opencode-go" if args.go_credentials else "local-fixture"
    report = {"route": route, "model": MODEL, "original_sha256": originals, "turns": [],
              "request_limit": MAX_CALLS, "wall_limit": MAX_SECONDS, "remote_requests": 0}
    saved = args.output / "observation.json"
    connection = (resolve_connection("opencode-go", args.go_credentials, model=MODEL)
                  if args.go_credentials else None)
    if connection:
        report["connection"] = connection.descriptor.model_dump(mode="json")
    if connection and connection.descriptor.billing_mode != "subscription":
        raise ValueError("subscription required")
    params = provider_parameters("opencode-go", model=MODEL, output_tokens=OUTPUT_LIMIT,
                                 reasoning_effort="high", thinking=True)
    names = {"name": {"type": "string", "enum": list(originals)},
             "coordinate_grid": {"type": "boolean", "enum": [False]}}
    tool = {"type": "function", "function": {"name": TOOL, "description": "View the full original PNG.",
            "parameters": {"type": "object", "properties": names,
                           "required": list(names), "additionalProperties": False}}}
    history = [{"role": "user", "content": scope}]
    session_key = str(uuid.uuid4())
    local_nonce = secrets.token_urlsafe(32)

    async def submit(body):
        left = MAX_SECONDS - (time.monotonic() - started)
        if left <= 0:
            raise TimeoutError("whole-probe deadline exceeded")
        adapter = connection.create_adapter(session_id=session_key)
        try:
            wire = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode()
            async with asyncio.timeout(left):
                return await adapter.send(SimpleNamespace(wire_bytes=wire), timeout=left)
        finally:
            await adapter.close()

    def reply(body):
        if connection:
            report["remote_requests"] += 1
            return asyncio.run(submit(body))
        first = len(history) == 1
        assistant = {"role": "assistant", "content": "Offline observation fixture."}
        if first:
            assistant = {"role": "assistant", "content": None, "reasoning_content": "fixture",
                         "tool_calls": [{"id": f"call_fixture_{i}", "type": "function",
                             "function": {"name": TOOL, "arguments": json.dumps(
                                 {"name": name, "coordinate_grid": False})}}
                             for i, name in enumerate(originals)]}
        return {"id": "chatcmpl-probe", "created": int(time.time()), "model": MODEL,
                "choices": [{"index": 0, "message": assistant,
                             "finish_reason": "tool_calls" if first else "stop"}]}

    class Observer(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            if self.path != "/v1/chat/completions" or not secrets.compare_digest(self.headers.get("Authorization", ""), "Bearer " + local_nonce):
                self.send_error(404)
                return
            if len(report["turns"]) >= MAX_CALLS or report.get("stopped") or report.get("error"):
                self.send_error(409)
                return
            try:
                raw = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                if (raw.get("model") != MODEL or not raw.get("stream")
                        or any(raw.get(key) != value for key, value in params.items())):
                    raise ValueError("host request changed model or mode")
                step = {"turn": len(report["turns"]) + 1}
                report["turns"].append(step)
                if args.interrupt and step["turn"] == 2:
                    self.server.pending = raw
                    step["injected_status"] = 503
                    self.send_error(503, "offline provider interruption")
                    return
                if args.interrupt and step["turn"] == 3:
                    marker = raw["messages"][-1]
                    prior = {**raw, "messages": raw["messages"][:-1]}
                    if prior != self.server.pending or marker != {"role": "user", "content": launcher.relay.RESUME_MARKER}:
                        raise ValueError("native session changed the interrupted submission")
                    raw = prior
                    report["resumed_exact_native_request"] = True
                if step["turn"] == 1:
                    if TOOL not in [t["function"]["name"] for t in raw["tools"]]:
                        raise ValueError("native view tool is absent")
                    body = {"model": MODEL, "messages": history, "tools": [tool],
                            "tool_choice": "auto", "stream": False, **params}
                else:
                    records = list((run / "image_views").glob("view_*.json"))
                    views = [json.loads(p.read_bytes()) for p in records]
                    by_name = {view["name"]: view for view in views}
                    if (len(by_name) != len(views)
                            or {name: view["image_sha256"] for name, view in by_name.items()} != originals):
                        raise ValueError("tool read an unexpected source image")
                    receipts, attachment, digests = confirmed_results(raw["messages"], history[-1], by_name)
                    history.extend([*receipts, attachment])
                    step["provider_image_sha256_by_call"] = digests
                    body = {"model": MODEL, "messages": history, "stream": False, **params}
                result = reply(body)
                step["provider_model"] = result.get("model")
                step["usage"] = result.get("usage")
                item, = result["choices"]
                message = item["message"]
                if item["finish_reason"] == "tool_calls" and step["turn"] == 1:
                    calls = list(map(identity, message["tool_calls"]))
                    if (len(calls) != len(originals) or len({c[0] for c in calls}) != len(calls)
                            or {c[3].get("name") for c in calls} != set(originals)
                            or any(not cid or kind != "function" or name != TOOL
                                   or args != {"name": args.get("name"), "coordinate_grid": False}
                                   for cid, kind, name, args in calls)):
                        raise ValueError("requested action is outside this read-only probe")
                    step["call_ids"] = [c[0] for c in calls]
                    history.append(copy.deepcopy(message))
                elif item["finish_reason"] == "stop" and not message.get("tool_calls"):
                    report["observation"] = message.get("content")
                    report["stopped"] = True
                else:
                    raise ValueError("incomplete or unsupported observation response")
                data = chat_sse(result)
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            except Exception as exc:
                # Neither credentials nor raw provider diagnostics enter the receipt.
                report["error"] = type(exc).__name__
                if type(exc) is ValueError and str(exc) in {
                        "requested action is outside this read-only probe",
                        "host did not replay the accepted Flash tool call and reasoning",
                        "provider image differs from the view returned by the tool"}:
                    report["error_reason"] = str(exc)
                details = getattr(exc, "details", {})
                for key in ("category", "http_status", "service_error_type", "service_error_code", "retry_hint", "usage_received"):
                    if isinstance(details.get(key), (str, int, bool)):
                        report["error_" + key] = details[key]
                self.send_error(400, "read-only probe failed")
            finally:
                saved.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf8")

    host_environment = launcher.host_environment

    def readonly(*values):
        env, cwd = host_environment(*values)
        env["SIM_BIM_WRITER"] = "0"
        env["SIM_BIM_LOCAL_ENDPOINT"] = values[-1] + "/chat/completions"
        return env, cwd

    with HTTPServer(("127.0.0.1", 0), Observer) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with patch.object(launcher, "make_patch", flash_profile), patch.object(launcher, "host_environment", readonly):
                config = {"model": MODEL, "limits": {"wall_seconds": MAX_SECONDS},
                          "dsh_executable": str(args.executable),
                          "context_window": opencode_go.model_contract(MODEL)[1]}
                url = f"http://127.0.0.1:{server.server_port}/v1"
                launcher.execute_host(args.output, session, config, url, local_nonce, 1, False)
                if args.interrupt and not report.get("error") and getattr(server, "pending", None):
                    launcher.execute_host(args.output, session, config, url, local_nonce, 2, True)
        finally:
            server.shutdown()
            thread.join()
    report["elapsed_seconds"] = round(time.monotonic() - started, 2)
    report["tool_executions"] = len(list((run / "image_views").glob("view_*.json")))
    report["confirmed"] = all((
        report["tool_executions"] == len(originals),
        report.get("stopped"),
        report.get("observation"),
        any(t.get("provider_image_sha256_by_call") for t in report["turns"]),
        not args.interrupt or report.get("resumed_exact_native_request"),
        not report.get("error"),
    ))
    saved.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf8")
    print(json.dumps({k: report[k] for k in ("route", "remote_requests", "confirmed", "elapsed_seconds")}))
    return 0 if report["confirmed"] else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", action="append", required=True, type=Path)
    parser.add_argument("--interrupt", action="store_true", help="Inject a 503 and resume the local synthetic session.")
    parser.add_argument("--executable", required=True, type=Path)
    parser.add_argument("--go-credentials", type=Path, help="Explicitly use this OpenCode Go subscription.")
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    if args.go_credentials and (len(args.source) != 1 or args.interrupt):
        parser.error("the live subscription probe accepts one PNG and does not retry")
    raise SystemExit(run_probe(args))
