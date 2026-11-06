"""Attach sanitized provider-side wire recordings to a native or third-party trace."""
from __future__ import annotations

from collections import defaultdict
import gzip
import json
from pathlib import Path
import re

from .event_data import event as trace_event
from .native import Attachments
from .privacy import public_value

_FILE = re.compile(r"^(\d+)-(out\.json\.gz|reply\.json|assembled-reply\.json|events\.jsonl\.gz)$")
_SECRET_KEY = re.compile(r"(?i)^(?:authorization|proxy-authorization|cookie|set-cookie|x-api-key|api[_-]?key|(?:access|refresh|auth)[_-]?token|password|client[_-]?secret)$")
_DATA = re.compile(r"data:([^,\s]{0,160});base64,([a-zA-Z0-9+/=_-]+)", re.I)
_LONG_ENCODED = re.compile(r"(?<![A-Za-z0-9+/=_-])[A-Za-z0-9+/=_-]{256,}(?![A-Za-z0-9+/=_-])")
_THOUGHT_TAG = re.compile(r"<(think|thinking|reasoning)(?:\s[^>]*)?>.*?(?:</\1\s*>|$)", re.I | re.S)
_PRIVATE = {"reasoning_content", "reasoning_details", "thinking_content", "encrypted_content",
            "redacted_thinking", "signature", "analysis", "thought", "thoughts", "reasoning_text", "thinking_text",
            "thinkingSignature", "reasoningSignature", "reasoning_signature", "thought_signature"}
_BLOCKS = {"thinking", "reasoning", "reasoning_text", "redacted_thinking", "analysis"}


def _string(text: str) -> str:
    text = _THOUGHT_TAG.sub("[private reasoning omitted]", text)
    text = _DATA.sub(lambda m: f"[{m[1]} attachment, {len(m[2])} encoded characters omitted]", text)
    text = _LONG_ENCODED.sub("[encoded payload omitted]", text)
    return public_value(text)


def _json_string(value: str) -> str:
    # MCP tool messages and function arguments often contain another JSON envelope.
    if value.lstrip().startswith(("{", "[")):
        try:
            embedded, pos = json.JSONDecoder().raw_decode(value.lstrip())
        except (ValueError, RecursionError):
            pass
        else:
            cleaned = _safe(embedded)
            if cleaned != embedded:
                tail = value.lstrip()[pos:]
                return json.dumps(cleaned, ensure_ascii=False) + _string(tail)
    return _string(value)


def _safe(value):
    if isinstance(value, str):
        return _json_string(value)
    if isinstance(value, list):
        return [_safe(item) for item in value]
    if not isinstance(value, dict):
        return value
    if isinstance(value.get("type"), str) and value["type"] in _BLOCKS:
        return {"type": "private_reasoning_omitted"}
    out = {}
    for key, val in value.items():
        if _SECRET_KEY.fullmatch(key):
            out[key] = "[redacted]"
        elif key in _PRIVATE or key == "thinking" or key == "reasoning":
            if isinstance(val, str):
                out[f"{key}_chars"] = len(val)
            elif val is not None:
                out[f"{key}_omitted"] = True
        elif key in {"data", "b64_json", "base64", "inline_data", "file_data"} and isinstance(val, str):
            out[key] = f"[{len(val)} characters omitted]"
        else:
            out[key] = _safe(val)
    return out


def _read(path: Path) -> dict:
    if path.suffix == ".gz":
        with gzip.open(path, "rt", encoding="utf-8-sig") as fh:
            result = json.load(fh)
    else:
        with path.open(encoding="utf-8-sig") as fh:
            result = json.load(fh)
    if not isinstance(result, dict):
        raise ValueError("expected a JSON object")
    return result


def _rows(path):
    if not path.is_file():
        return
    with path.open("rb") as stream:
        for line in stream:
            try:
                row = json.loads(line)
            except (UnicodeError, ValueError):
                continue
            if isinstance(row, dict):
                yield row


def _relay_records(path, arm):
    indexed = defaultdict(dict)
    for row in _rows(path):
        if row.get("arm") != arm:
            continue
        turn = row.get("turn")
        if type(turn) is int:
            indexed[turn][row.get("event")] = row
    return indexed


def _host_evidence(folder: Path, arm: str, journal: Path | None = None):
    """Read only adjacent experiment records. Match native calls by provider response ID."""
    root = folder.parent.parent.parent / arm
    received = set()
    dsh_usage = {}
    timeout_step = None
    timeout_at = None
    if folder.parent.name != "wire":
        return received, dsh_usage, timeout_step, timeout_at
    if arm == "native":
        work = root / "work"
        attachments = Attachments(work.resolve())
        for row in _rows(work / "events.jsonl"):
            payload = row.get("payload", {})
            if payload.get("event_type") == "model_response":
                response = attachments.capture(payload.get("raw_response"))
                if isinstance(response, dict) and isinstance(response.get("id"), str):
                    received.add(response["id"])
    elif arm == "dsh":
        session = journal or root / "session"
        step = None
        index = 0
        for log in (session / "events.jsonl", *sorted(session.glob("attempt-*/events.jsonl"))):
            for row in _rows(log):
                if row.get("type") != "status":
                    continue
                if row.get("phase") == "step_start":
                    index += 1
                    step = index
                elif row.get("phase") == "step_end" and isinstance(row.get("usage"), dict):
                    dsh_usage[step] = row["usage"]
                elif row.get("phase") == "turn_end":
                    reason = row.get("reason", {})
                    if (reason.get("error") or {}).get("code") == "TIMEOUT":
                        timeout_step = step
                        try:
                            timeout_at = _read(log.parent / "execution.json").get("ended")
                        except (OSError, ValueError):
                            pass
    return received, dsh_usage, timeout_step, timeout_at


def _host_status(evidence, turn, response, end):
    received, dsh_usage, timeout_step, timeout_at = evidence
    if response.get("id") in received:
        return {"status": "received", "evidence": "宿主保存的模型回执与供应商响应 ID 一致。"}
    usage = response.get("usage") or {}
    host_usage = dsh_usage.get(turn, {})
    if (isinstance(usage.get("total_tokens"), int) and usage["total_tokens"] > 0
            and host_usage.get("totalTokens") == usage["total_tokens"]
            and host_usage.get("outputTokens") == usage.get("completion_tokens", usage.get("output_tokens"))):
        return {"status": "received", "evidence": "同一 headless 步骤记录了与供应商响应一致的总 token 和输出 token 数。"}
    if (turn == timeout_step and isinstance(timeout_at, (int,float)) and isinstance(end, (int,float))
            and timeout_at < end):
        return {"status": "not_received", "evidence": "headless 步骤因等待响应超时而结束。宿主退出时间早于转发器记录上游响应的时间。",
                "host_end": timeout_at}
    return {"status": "unverified", "evidence": "当前证据只能确认转发器一侧的记录。"}


def _request(body, filename):
    fields = {k:v for k,v in body.items() if k not in {"messages","input","tools","thinking","reasoning"}}
    parameters = _safe(fields)
    for name in ("thinking","reasoning"):
        config = body.get(name)
        if isinstance(config, dict):
            parameters[name] = {k:_safe(v) for k,v in config.items() if k in {"type","effort","budget_tokens","enabled"}}
    messages_key = "input" if "input" in body else "messages"
    return {"source":"provider_request_body","file":filename,"parameters":parameters,
            "protocol": "Responses" if messages_key == "input" else "Chat Completions",
            messages_key:_safe(body.get(messages_key, [])),"tools":_safe(body.get("tools", []))}


def _response(body, filename):
    if body.get("error") is not None:
        return {"source":"provider_response_body","file":filename,"error":_safe(body["error"])}
    if "output" in body:
        return {"source":"provider_response_body","file":filename,"id":_safe(body.get("id")),
                "model":_safe(body.get("model")),"status":body.get("status"),
                "output":_safe(body["output"]),"usage":_safe(body.get("usage",{}))}
    choices = []
    for choice in body.get("choices") or []:
        choices.append({"index":choice.get("index"),"finish_reason":choice.get("finish_reason"),
                        "message":_safe(choice.get("message", {}))})
    return {"source":"provider_response_body","file":filename,"id":_safe(body.get("id")),
            "model":_safe(body.get("model")),"choices":choices,"usage":_safe(body.get("usage",{}))}


def _response_summary(wire):
    response = wire.get("response", {})
    if "output" in response:
        names = [item.get("name", "") for item in response["output"] if item.get("type") == "function_call"]
        if names:
            return f"提议 {len(names)} 次工具调用 · " + "、".join(dict.fromkeys(names))
        return " ".join(part.get("text", "") for item in response["output"] if item.get("type") == "message"
                        for part in item.get("content", []) if part.get("type") == "output_text")[:140]
    choices = wire.get("response", {}).get("choices") or []
    if not choices:
        return ""
    choice = choices[0]
    message = choice.get("message") or {}
    names = [call.get("function", {}).get("name") for call in message.get("tool_calls") or []
             if isinstance(call, dict)]
    names = [name for name in names if isinstance(name, str)]
    reason = choice.get("finish_reason") or ""
    if names:
        return f"提议 {len(names)} 次工具调用 · " + "、".join(dict.fromkeys(names))
    content = message.get("content")
    snippet = " ".join(content.split())[:140] if isinstance(content, str) else ""
    if reason == "length":
        return "达到输出长度上限" + (f" · {snippet}" if snippet else "")
    return f"{reason} · {snippet}" if reason and snippet else snippet or reason


def _set_summary(event, wire):
    host = wire["host_delivery"]["status"]
    summary = _response_summary(wire)
    if host == "not_received":
        event["status"] = "timeout"
        event["summary"] = "宿主超时，未收到本轮响应"
        if summary:
            event["summary"] += f"。转发器随后收到：{summary}"
        return
    if summary and (host == "received" or str(event.get("id", "")).startswith("wire-")):
        if event.get("status") not in {"failed", "timeout", "rejected"}:
            event["summary"] = summary


def _partial_stream(path):
    items = {}
    last = None
    damaged = False
    try:
        with gzip.open(path, "rt", encoding="utf-8") as stream:
            for line in stream:
                record = json.loads(line)
                last = record.get("time", last)
                data = record.get("event", {})
                if data.get("type") == "response.output_item.done":
                    index = data.get("output_index")
                    if type(index) is int and isinstance(data.get("item"), dict):
                        items[index] = _safe(data["item"])
    except (OSError, ValueError, EOFError):
        damaged = True
    return {"file":path.name, "output_items_fully_streamed":[items[k] for k in sorted(items)],
            "last_event_time":last, "damaged_tail":damaged, "terminal_response_saved":False, "usage":None}


def enrich_wire(events: list[dict], directory: Path, *, relay: Path | None = None,
                arm: str | None = None, journal: Path | None = None) -> list[dict]:
    """Merge per-turn upstream request/reply files. Missing evidence stays explicit."""
    folder = Path(directory)
    if arm and (folder / arm).is_dir():
        folder = folder / arm
    arm = arm or folder.name
    if not folder.is_dir():
        return list(events)
    files = defaultdict(dict)
    for path in folder.iterdir():
        m = _FILE.fullmatch(path.name)
        if m and path.is_file():
            files[int(m[1])][m[2]] = path
    if not files:
        return list(events)
    relay_path = Path(relay) if relay is not None else folder.parent.parent / "relay.jsonl"
    records = _relay_records(relay_path, arm)
    evidence = _host_evidence(folder, arm, journal)
    result = list(events)
    indices = defaultdict(list)
    for idx, event in enumerate(events):
        if event.get("kind") == "model" and type(event.get("turn")) is int and event.get("arm", arm) == arm:
            indices[event["turn"]].append(idx)

    for turn, pair in sorted(files.items()):
        entries = records[turn]
        entered = entries.get("request", {}).get("time")
        response_log = entries.get("response", entries.get("provider_error", entries.get("transport_stream_interrupted", {})))
        ended = response_log.get("time")
        wire = {"turn":turn,"arm":arm,"timing":{"start":entered,"end":ended},"host_delivery":{"status":"unverified"}}
        if entries:
            wire["relay"] = {key: {name: _safe(item[name]) for name in
                                  ("time", "seconds", "status", "body_sha256", "bytes", "upstream_model", "finish_reason", "usage")
                                  if name in item} for key, item in entries.items() if key in {"request", "response", "provider_error"}}
        body = {}
        for name, field in (("out.json.gz","request"),("reply.json","response")):
            path = pair.get(name)
            if path is None:
                continue
            try:
                parsed = _read(path)
                if field == "request":
                    wire[field] = _request(parsed, path.name)
                else:
                    body = parsed
                    wire[field] = _response(body,path.name)
            except (OSError, ValueError, EOFError, RecursionError):
                wire[field] = {"file":path.name,"error":"Unreadable wire recording"}
        assembled = pair.get("assembled-reply.json")
        if assembled and body.get("id"):
            try:
                streamed = _read(assembled)
                if streamed.get("id") == body["id"]:
                    wire["response"]["output"] = _safe(streamed.get("output", []))
                    wire["response"]["assembled_file"] = assembled.name
            except (OSError, ValueError):
                wire["response"]["assembly_error"] = "Unreadable assembled response"
        if not body and pair.get("events.jsonl.gz"):
            wire["partial_stream"] = _partial_stream(pair["events.jsonl.gz"])
        wire["host_delivery"] = _host_status(evidence,turn,body,ended)
        if len(indices[turn]) == 1:
            idx = indices[turn][0]
            event = dict(result[idx])
            event["detail"] = {**(event.get("detail") or {}), "wire":wire}
            if event.get("start") is None:
                event["start"] = entered
            if event.get("end") is None:
                event["end"] = wire["host_delivery"].get("host_end", ended)
            _set_summary(event, wire)
            result[idx] = event
            continue
        status = ("failed" if body.get("error") is not None or body.get("status") == "failed" else
                  "completed" if "choices" in body or body.get("status") == "completed" else "incomplete")
        added = trace_event(f"wire-{arm}-{turn}", "model", f"供应商模型请求 {turn}",
                            turn=turn, start=entered, end=ended, status=status,
                            summary="来自供应商转发记录", wire=wire)
        _set_summary(added, wire)
        result.append(added)
    finish_file = relay_path.parent / "dsh-relay-finish.json"
    if finish_file.is_file():
        for index, row in enumerate(result):
            detail = row.get("detail", {})
            if (row.get("kind") == "model" and "wire" not in detail
                    and (detail.get("usage") or {}).get("totalTokens") == 0
                    and detail.get("visible_text") == ["Verified whole-building handoff saved."]):
                result[index] = {**row, "kind":"status", "title":"本地交付确认", "turn":None,
                                 "summary":"运行端已核验交付回执", "detail":{**detail, "local_completion":True}}
    return result
