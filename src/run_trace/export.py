"""Compose journal readers and optional provider evidence into a portable offline view."""

from dataclasses import dataclass
import json
from pathlib import Path
import shutil
import tempfile

from .media import TraceMedia
from .privacy import public_value
from .readers import read_events
from .wire import enrich_wire


@dataclass(frozen=True)
class TraceSource:
    directory: Path
    runtime: str
    wire: Path | None = None
    relay: Path | None = None
    arm: str | None = None


def find_sources(path: Path) -> list[TraceSource]:
    path = path.expanduser().resolve(strict=True)
    if path.is_file() and path.name == "events.jsonl":
        path = path.parent
    events = path / "events.jsonl"
    if events.is_file():
        with events.open(encoding="utf-8-sig") as stream:
            for line in stream:
                if line.strip():
                    first = json.loads(line)
                    break
            else:
                raise ValueError(f"Empty event log: {events}")
        if isinstance(first, dict):
            if "payload" in first and "task_id" in first:
                return [TraceSource(path, "native")]
            if first.get("type") in {"session", "session_start", "turn_start", "step_start"}:
                return [TraceSource(path, "deepseek")]
        raise ValueError(f"Unrecognized event format: {events}")

    # A paired controller owns the association between journals and wire captures.
    # A standalone session is never matched to a relay by timestamp or model name.
    private = path / "private"
    sources = []
    for control in (private / "control", path / "control"):
        if not control.is_dir():
            continue
        for arm, runtime, subdir in (("native", "native", "work"), ("dsh", "deepseek", "session")):
            directory = private / arm / subdir
            if (directory / "events.jsonl").is_file():
                wire = control / "wire" / arm
                relay = control / "relay.jsonl"
                sources.append(TraceSource(directory, runtime, wire if wire.is_dir() else None,
                                           relay if relay.is_file() else None, arm))
    if not sources:
        raise ValueError(f"No supported run at {path}. Select a directory containing events.jsonl or a paired case.")
    return sources


def _js(value):
    # A JSON string literal avoids interpreting keys like __proto__ as JS object syntax.
    data = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    return "JSON.parse(" + json.dumps(data, ensure_ascii=False).replace("\u2028", "\\u2028").replace("\u2029", "\\u2029") + ")"


def export_trace(sources: list[TraceSource], output: Path, title: str) -> Path:
    if not sources or len(set(s.directory.resolve() for s in sources)) != len(sources):
        raise ValueError("Select at least one run and select each run only once.")
    output = output.expanduser().resolve()
    if output.exists():
        raise ValueError(f"Output already exists. Choose a new directory: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    labels = {"native": "自研 runtime", "deepseek": "DeepSeek Harness"}
    with tempfile.TemporaryDirectory(dir=output.parent, prefix=".run-trace-") as temp:
        # Renaming the inner folder publishes the complete index and its dependencies together.
        bundle = Path(temp) / "trace"
        details = bundle / "details"
        details.mkdir(parents=True)
        media = TraceMedia(bundle)
        runs = []
        for number, source in enumerate(sources, start=1):
            events = read_events(source.directory, source.runtime)
            if source.wire:
                events = enrich_wire(events, source.wire, relay=source.relay, arm=source.arm,
                                     journal=source.directory)
            # Preserve journal positions for undated evidence. Put the remaining
            # evidence in recorded time order, including late user input.
            dated = iter(sorted((e for e in events if e.get("start") is not None),
                                key=lambda e: e["start"]))
            events = [next(dated) if e.get("start") is not None else e for e in events]
            label = labels[source.runtime]
            if sum(s.runtime == source.runtime for s in sources) > 1:
                label += f" · {source.directory.parent.name}/{source.directory.name} ({number})"
            run = {"id": f"run-{number}", "label": label, "events": []}
            for i, event in enumerate(events, start=1):
                detail = event.get("detail", {})
                images = media.event_images(event, source.directory, source.wire)
                safe = public_value(detail)
                if images:
                    safe["inputImages"] = images
                key = f"r{number}-e{i}"
                name = f"details/{key}.js"
                (bundle / name).write_text(
                    f'window.RUN_TRACE_DETAILS["{key}"]={_js(safe)};\n', encoding="utf-8")
                row = public_value({k: event.get(k) for k in (
                    "id", "kind", "title", "summary", "start", "end", "turn", "status")})
                row.update(detailKey=key, detailFile=name)
                run["events"].append(row)
            runs.append(run)
        (bundle / "trace-data.js").write_text("window.RUN_TRACE=" + _js(
            {"title": title, "runs": runs}) + ";\n", encoding="utf-8")
        shutil.copyfile(Path(__file__).with_name("viewer.html"), bundle / "index.html")
        bundle.rename(output)
    return output / "index.html"
