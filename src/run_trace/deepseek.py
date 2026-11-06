"""Read the DeepSeek Harness headless feed and its optional timestamped session journal."""
import json
import os
from pathlib import Path
import shutil
import subprocess

from .event_data import arguments, event, json_lines, timestamp, tool_feedback, tool_summary, visible
from .privacy import public_value


# DSH writes one independent Zstandard frame per append; a one-shot Node
# decompression can return only the first frame. No DSH app or provider is run.
_NODE_DECODE = """
const fs = require('node:fs'), zlib = require('node:zlib');
const file = fs.readFileSync(process.argv[1]);
let offset = 0;
while (offset < file.length) {
  const frame = zlib.zstdDecompressSync(file.subarray(offset), { info: true });
  if (!frame.engine.bytesWritten) throw new Error('Invalid Zstandard frame');
  process.stdout.write(frame.buffer);
  offset += frame.engine.bytesWritten;
}
"""


def _journal(directory, session_id):
    names = ("session.v4.jsonl", "session.v4.jsonl.zstd")
    paths = [directory / n for n in names if (directory / n).is_file()]
    if not paths:
        root = directory / "dsh-home" / "sessions"
        paths = [p for n in names for p in root.glob(f"*/*/{n}")
                 if session_id and p.parent.name in {session_id, f"session-{session_id}"}]
    if len(paths) != 1:
        return [], "未找到与此会话唯一匹配的持久化日志"
    path = paths[0].resolve()
    if not path.is_relative_to(directory):
        return [], "会话日志引用超出输入目录"
    if path.suffix == ".jsonl":
        return list(json_lines(path)), None
    try:
        try:
            from compression import zstd  # Python 3.14+
        except ImportError:
            zstd = None
        if zstd:
            with zstd.open(path, "rb") as file:
                raw = file.read()
        else:
            binary = shutil.which("node")
            if not binary:
                return [], "缺少 Zstandard 解码器，使用 headless 记录"
            # Ignore inherited hooks so decoding untrusted input cannot load
            # extension code. All arguments (including the path) are data.
            env = {k: v for k, v in os.environ.items() if k.upper() not in {"NODE_OPTIONS", "NODE_PATH"}}
            raw = subprocess.run([binary, "-e", _NODE_DECODE, str(path)], env=env, check=True,
                                 stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=60).stdout
        records = [json.loads(line) for line in raw.splitlines() if line.strip()]
        if not all(isinstance(x, dict) for x in records):
            raise ValueError("Invalid session records")
        return records, None
    except (OSError, ValueError, EOFError, subprocess.SubprocessError):
        return [], "持久化日志无法完整解码，使用 headless 记录"


def _attachment_refs(content):
    refs = []
    for part in content if isinstance(content, list) else []:
        if not isinstance(part, dict) or part.get("type") != "image":
            continue
        attachment = part.get("attachment") or {}
        identity = attachment.get("attachmentId", "")
        if isinstance(identity, str) and identity.startswith("sha256:"):
            refs.append({"sha256": identity[7:], "media_type": attachment.get("mediaType")})
    return refs


def _index(records):
    steps, calls, results, inputs, ends = {}, {}, {}, [], {}
    settings = {}
    active = None
    for record in records:
        kind, data = record.get("type"), record.get("data") or {}
        millis = timestamp(record.get("time"))
        when = millis / 1000 if millis is not None else None
        pair = (data.get("turn"), data.get("step"))
        if kind in {"step/start", "assistant/message", "assistant/attempt", "step/end"}:
            step = steps.setdefault(pair, {})
            step[kind] = when
            if kind == "step/start":
                active = pair
                step.update(settings)
            if kind == "assistant/message":
                step["content"] = visible(data.get("message", {}).get("content", []))
                step["usage"] = data.get("usage")
        elif kind == "tool/call":
            calls[(*pair, data.get("callId"))] = (when, data)
        elif kind == "tool/result":
            msg = data.get("message") or {}
            results[(*pair, msg.get("toolCallId"))] = (when, msg)
        elif kind in {"system/message", "user/message"}:
            content = (data.get("message") or data).get("content")
            inputs.append((when, kind.split("/")[0], visible(content)))
        elif kind == "turn/end":
            ends[data.get("turn")] = when
        elif kind in {"request/context", "request/header"}:
            key = "host_request_context" if kind == "request/context" else "host_request_header"
            value = data if kind == "request/context" else data.get("header")
            settings[key] = public_value(visible(value))
            if active is not None:
                steps[active][key] = settings[key]
    return steps, calls, results, inputs, ends


def read_deepseek(directory: Path):
    logs = [directory / "events.jsonl", *sorted(directory.glob("attempt-*/events.jsonl"))]
    headless = [row for path in logs for row in json_lines(path)]
    session_id = next((r.get("sessionId") for r in headless if r.get("type") == "session"), None)
    timeline, note = _journal(directory, session_id)
    steps, durable_calls, durable_results, inputs, ends = _index(timeline)
    rows = [event(f"input-{idx}", "input", f"{role} 消息", start=when, end=when,
                  summary="运行端持久化消息", role=role, input=content, images=_attachment_refs(content))
            for idx, (when, role, content) in enumerate(inputs, 1)]
    if note:
        rows.append(event("headless-only", "status", "headless 日志", summary=note,
                          evidence="无逐步时间戳的记录保持空值"))
    models, calls = {}, {}
    current = None
    pair = (None, None)
    for number, record in enumerate(headless, 1):
        kind = record.get("type")
        phase = record.get("phase") if kind == "status" else kind
        when = timestamp(record.get("timestamp"))
        if "_incomplete_line" in record:
            rows.append(event("incomplete-tail", "status", "末行尚未写完整", status="incomplete"))
        elif phase == "step_start":
            pair = (record.get("turn"), record.get("step"))
            step = steps.get(pair, {})
            index = len(models) + 1
            current = event(f"model-{index}", "model", f"模型请求 {index}", turn=index,
                            start=step.get("step/start", when), status="incomplete",
                            summary="运行端模型步骤，响应待核对", host_turn=pair[0], host_step=pair[1],
                            timing_basis="host_step_start_to_assistant_message")
            current["detail"]["tool_calls"] = []
            current["detail"].update({k: step[k] for k in ("host_request_header", "host_request_context") if k in step})
            models[pair] = current
            rows.append(current)
        elif phase == "text" and current:
            current["detail"].setdefault("visible_text", []).append(record.get("text", ""))
        elif phase == "tool_call":
            cid, name = record.get("callId"), record.get("tool")
            start, stored = durable_calls.get((*pair, cid), (when, {}))
            args = arguments(stored.get("arguments", record.get("input")))
            index = current["turn"] if current else None
            obj = event(f"tool-{number}", "tool", str(name), turn=index, start=start,
                        status="incomplete", summary="已记录调用，执行结果待核对",
                        call_id=cid, tool_name=name, arguments=args)
            rows.append(obj)
            calls[(*pair, cid)] = obj
            if current:
                current["detail"]["tool_calls"].append({"call_id": cid, "tool_name": name, "arguments": args})
        elif phase == "tool_result":
            cid = record.get("callId")
            obj = calls.get((*pair, cid))
            if obj is None:
                obj = event(f"tool-{number}", "tool", "工具结果", call_id=cid, tool_name=None,
                            evidence="未记录调用，无法关联轮次")
                rows.append(obj)
            end, msg = durable_results.get((*pair, cid), (when, {}))
            result = visible(msg.get("content", record.get("result")))
            feedback = tool_feedback(result)
            ok = msg.get("isError") is False or record.get("status") == "completed"
            status = "completed" if ok else "unknown" if not record.get("status") and not msg else "failed"
            if msg.get("isError") or feedback.get("error") or feedback.get("status") in {"error", "rejected"}:
                status = "failed"
            obj.update(end=end, status=status, summary=tool_summary(feedback, status))
            obj["detail"].update(result=result, images=_attachment_refs(result), domain_feedback=feedback,
                                 truncated=not bool(msg) and bool(record.get("truncated")))
        elif phase == "step_end" and current:
            step = steps.get((record.get("turn"), record.get("step")), {})
            evidence = current["detail"]
            evidence["usage"] = step.get("usage") or record.get("usage")
            # DSH emits step_end even for first-byte silence / client timeout.
            reported_tokens = any(type(value) in (int, float) and value > 0
                                  for value in (evidence["usage"] or {}).values())
            received = ("assistant/message" in step or reported_tokens
                        or evidence["tool_calls"] or evidence.get("visible_text"))
            if received:
                current.update(end=step.get("assistant/message", when), status="completed",
                               summary=f"运行端收到响应，记录 {len(evidence['tool_calls'])} 个工具调用")
            if "content" in step:
                evidence["visible_text"] = [p["text"] for p in step["content"]
                                            if p.get("type") == "text" and isinstance(p.get("text"), str)]
                for part in step["content"]:
                    if part.get("type") != "tool-call" or (*pair, part.get("id")) in calls:
                        continue
                    cid, name, args = part.get("id"), part.get("name"), arguments(part.get("arguments"))
                    evidence["tool_calls"].append({"call_id": cid, "tool_name": name, "arguments": args})
                    obj = event(f"proposal-{number}-{len(evidence['tool_calls'])}", "tool", str(name),
                                turn=current["turn"], status="unconfirmed", summary="模型提出调用，缺少执行回执",
                                call_id=cid, tool_name=name, arguments=args)
                    calls[(*pair, cid)] = obj
                    rows.append(obj)
        elif phase == "turn_end":
            reason = record.get("reason") or {}
            failure = reason.get("kind") in {"error", "abort", "aborted", "cancelled", "timeout"} if isinstance(reason, dict) else False
            end = ends.get(record.get("turn"), when)
            if current and failure and current["status"] == "incomplete":
                detail = current["detail"]
                step = steps.get((detail["host_turn"], detail["host_step"]), {})
                current.update(status="failed", end=step.get("assistant/attempt", end), summary="运行端未取得正常响应")
                detail["failure"] = visible(reason)
            rows.append(event(f"end-{number}", "status", "会话轮次结束", start=end, end=end,
                              turn=current["turn"] if current else None,
                              status="failed" if failure else "completed", reason=visible(reason)))
        elif phase == "final" and record.get("text"):
            rows.append(event(f"final-{number}", "status", "最终公开输出", summary="运行端最后输出的文本",
                              turn=current["turn"] if current else None, visible_text=record["text"]))
    return rows
