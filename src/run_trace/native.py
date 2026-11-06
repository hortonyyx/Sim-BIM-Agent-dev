"""Project the native append-only journal, retaining the IDs linking each lifetime."""
import hashlib
import json
from pathlib import Path
import re

from src.agent_runtime.json_tree import read_json_tree
from src.harness_contracts import HashedBlobRef

from .event_data import event, json_lines, timestamp, tool_feedback, tool_summary, visible
from .privacy import public_value


class Attachments:
    def __init__(self, root):
        self.root = root

    def read(self, ref):
        digest = ref.sha256
        if not re.fullmatch(r"[0-9a-f]{64}", digest) or ref.uri != f"blobs/{digest}":
            raise ValueError("Invalid evidence attachment reference")
        path = (self.root / ref.uri).resolve()
        if not path.is_relative_to(self.root):
            raise ValueError("Attachment lies outside the run")
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != digest:
            raise ValueError("Attachment hash mismatch")
        return raw

    def capture(self, data):
        if not isinstance(data, dict):
            return {"unavailable": "No capture recorded"}
        if data.get("kind") == "inline":
            return visible(data.get("value"))
        try:
            ref = HashedBlobRef.model_validate(data["blob"])
            if data.get("kind") in {"json_references", "image_references"}:
                # Image capture trees have null image slots, with exact PNGs
                # recorded separately; do not expand them to repeated base64.
                return visible(read_json_tree(ref, self.read))
            return visible(json.loads(self.read(ref)))
        except (ValueError, OSError, KeyError, TypeError):
            return {"unavailable": "Capture missing, invalid or unavailable", "reference": data.get("blob")}

    def source(self, row, name):
        for ref in row.get("source_refs", []):
            if ref.get("source_id") == name and ref.get("blob"):
                return self.capture({"kind": "blob", "blob": ref["blob"]})
        return {}


def _tool_outcome(payload, result, feedback):
    if payload.get("outcome") != "succeeded":
        return {"failed": "failed"}.get(payload.get("outcome"), "unknown")
    if isinstance(result, dict):
        if result.get("isError") or feedback.get("error") or feedback.get("status") in {"error", "rejected"}:
            return "failed"
    return "completed"


def read_native(directory: Path):
    rows, requests, calls, by_invocation, by_execution = [], {}, {}, {}, {}
    latest_turn, input_seen = {}, set()
    blobs = Attachments(directory)

    def tool(task, cid, name, args, turn):
        key = (task, turn, cid)
        if key not in calls:
            obj = event(f"tool-{len(calls) + 1}", "tool", str(name), turn=turn,
                        status="unconfirmed", summary="模型提出调用，执行回执待核对",
                        call_id=cid, tool_name=name, arguments=args, task_id=task)
            calls[key] = obj
            rows.append(obj)
        return calls[key]

    for row in json_lines(directory / "events.jsonl"):
        if "_incomplete_line" in row:
            rows.append(event("incomplete-tail", "status", "末行尚未写完整", status="incomplete"))
            continue
        eid, task = row["event_id"], row.get("task_id")
        p, when = row["payload"], timestamp(row.get("occurred_at"))
        kind, turn = p["event_type"], latest_turn.get(task)
        if kind == "adapter_request":
            turn = len(requests) + 1
            latest_turn[task] = turn
            for injection in p.get("injected_content", []):
                source = injection.get("source", {})
                sid = source.get("source_id", "")
                if not sid.startswith("initial-message-") or (task, sid) in input_seen:
                    continue
                input_seen.add((task, sid))
                capture = injection.get("content") or {}
                rows.append(event(f"input-{len(input_seen)}", "input", "初始消息", turn=turn,
                                  start=when, end=when, timing_basis="first_request_including_message",
                                  summary="随本次模型请求发送", input=blobs.capture(capture),
                                  recorded_images=capture.get("images", []), task_id=task))
            req = event(f"model-{turn}", "model", f"模型请求 {turn}", start=when, turn=turn,
                        status="incomplete", summary="已记录请求，响应待核对", request_event_id=eid,
                        task_id=task, adapter=p.get("adapter"), parameters=p.get("parameters"),
                        purpose=p.get("logical_purpose"), body_sha256=p.get("wire_sha256"),
                        timing_basis="journal_request_to_response_or_failure",
                        model=(p.get("versions") or {}).get("remote_model", {}).get("model"),
                        images=p.get("images", []))
            capture = p.get("final_request_body") or {}
            req["detail"]["final_request_body"] = public_value(blobs.capture(capture))
            if capture.get("images"):
                req["detail"]["request_image_references"] = capture["images"]
            requests[eid] = req
            rows.append(req)
        elif kind == "model_response":
            req = requests.get(p.get("request_event_id"))
            requested = [{"call_id": x["call_id"], "tool_name": x["tool_name"],
                          "arguments": x.get("full_arguments")} for x in p.get("tool_calls", [])]
            if req is not None:
                turn = req["turn"]
                req.update(status="completed", end=when, summary=f"返回 {len(requested)} 个工具调用"
                           if requested else "收到模型响应")
                duration = blobs.source(row, "request-duration")
                req["detail"].update(response_event_id=eid, visible_text=p.get("visible_text", []),
                                     usage=p.get("usage"), tool_calls=requested, timing=duration)
            for x in requested:
                tool(task, x["call_id"], x["tool_name"], x["arguments"], turn)
        elif kind in {"tool_invocation", "tool_execution"}:
            cid = p["call_id"]
            obj = by_invocation.get(p.get("invocation_event_id")) or tool(
                task, cid, p.get("tool_name"), p.get("full_arguments"), turn)
            if kind == "tool_invocation":
                by_invocation[eid] = obj
                obj.update(start=when, status="incomplete", summary="已调度，执行结果待核对")
                obj["detail"]["invocation_event_id"] = eid
            else:
                by_execution[eid] = obj
                capture = p.get("raw_result") or {}
                result = blobs.capture(capture)
                feedback = tool_feedback(result)
                outcome = _tool_outcome(p, result, feedback)
                timing = blobs.source(row, "tool-duration")
                start = timestamp(timing.get("started_epoch"))
                if start is not None and type(timing.get("elapsed_seconds")) in (int, float):
                    obj.update(start=start, end=start + timing["elapsed_seconds"])
                else:
                    obj["end"] = when
                obj.update(status=outcome, summary=tool_summary(feedback, outcome))
                obj["detail"].update(execution_event_id=eid, result=result, images=capture.get("images", []),
                                     execution_outcome=p.get("outcome"), domain_feedback=feedback,
                                     failure=p.get("failure"), timing=timing)
        elif kind == "tool_presentation":
            obj = by_execution.get(p.get("tool_execution_event_id"))
            if obj:
                obj["detail"].setdefault("presentations", []).append({
                    "event_id": eid, "request_event_id": p.get("request_event_id"), "time": when})
        elif kind == "response_truncation":
            req = requests.get(p.get("request_event_id"))
            if req is not None:
                req.update(status="truncated", summary="服务报告达到单次输出上限")
                req["detail"]["truncation"] = {k: v for k, v in p.items() if k != "event_type"}
                for call in req["detail"].get("tool_calls", []):
                    obj = calls.get((task, req["turn"], call["call_id"]))
                    if obj and obj["status"] == "unconfirmed":
                        obj.update(status="not_executed", summary="输出被截断，运行端丢弃了这次调用")
        elif kind in {"run_lifecycle", "context", "control"}:
            failure = p.get("model_failure") or {}
            req = requests.get(failure.get("request_event_id"))
            if req is not None:
                req.update(status="failed", end=when, summary=str(failure.get("category") or "模型请求失败"))
                req["detail"]["failure"] = {k: v for k, v in failure.items() if k != "body_excerpt"}
            action, reason = p.get("action"), p.get("reason")
            status = "recorded"
            if action in {"failure", "timeout", "cancel"} or (action == "stop" and reason != "completed"):
                status = "failed"
            elif action == "stop":
                status = "completed"
            rows.append(event(f"log-{eid}", "status", f"{kind} / {action}", start=when, end=when,
                              turn=turn, status=status, summary=str(reason or ""), source_event_id=eid,
                              task_id=task, action=action, reason=reason))
    return rows
