"""Freeze a draft and launch the same DSH product case used by the CLI."""
from __future__ import annotations

from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time
import traceback
import uuid

import portalocker
from src.agent_runtime.output_limits import default_output_tokens
from .storage import (DEFAULT_BRIEF, ROOT, InputError, inside, locked, read_json, read_project,
                      revision_check, save_json)

RUN_ID = re.compile(r"run-\d{8}-\d{6}-[a-f0-9]{8}\Z")
OUTPUT_FILE = re.compile(r"(?:delivery\.(?:html|json)|candidate_\d+/(?:viewer\.html|(?:source_model|report|proposal)\.json|[^/\\:]+\.(?:glb|png)))\Z")


def workspace_tag(root):
    return hashlib.sha256(os.fsencode(str(root))).hexdigest()[:16]


def run_dir(root, identity):
    if not isinstance(identity, str) or not RUN_ID.fullmatch(identity):
        raise InputError("未找到运行。", "Run not found.", 404)
    return inside(root, "runs/" + identity)


def freeze(root, target, draft):
    materials, blocks = {}, []
    if not draft["coordinator"]:
        raise InputError("请选择主控模型。", "Select a coordinator model.")
    files = [a for b in draft["blocks"] for a in b["assets"]]
    if not any(a["kind"] in ("image", "mesh") for a in files):
        raise InputError("请至少添加一张 PNG 或一个 GLB。", "Add at least one PNG or GLB.")
    if sum(a["kind"] == "mesh" for a in files) > 1:
        raise InputError("一次建模支持一个 GLB，请选定本次使用的网格。", "Choose one GLB for this run.")
    counts = Counter(a["name"].casefold() for a in files)
    used = set(counts)
    declarations = []
    for b in draft["blocks"]:
        assets = []
        for a in b["assets"]:
            original = a["name"]
            alias = original
            if counts[original.casefold()] > 1:
                alias = a["id"] + "-" + original
                while alias.casefold() in used:
                    alias = "_" + alias
                used.add(alias.casefold())
            group = {"image": "images", "mesh": "mesh", "declaration": "declarations"}[a["kind"]]
            rel = Path("inputs") / group / alias
            dest = inside(target, str(rel))
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(inside(root, a["path"]), dest)
            with dest.open("rb") as stream:
                digest = hashlib.file_digest(stream, "sha256").hexdigest()
            assets.append({"id": a["id"], "name": original, "kind": a["kind"],
                           "file": alias, "path": rel.as_posix(), "sha256": digest})
            if a["kind"] == "image":
                materials["images"] = "images"
            elif a["kind"] == "mesh":
                materials["mesh"] = (Path(group) / alias).as_posix()
            else:
                declarations.append((b["id"], alias, dest))
        blocks.append({"id": b["id"], "caption": b["caption"], "assets": assets})
    if len(declarations) == 1:
        materials["building_input"] = "declarations/" + declarations[0][1]
    elif declarations:
        save_json(target / "inputs/building.json", {"declarations": [
            {"block": bid, "file": name, "content": read_json(path)} for bid, name, path in declarations]})
        materials["building_input"] = "building.json"
    save_json(target / "input.json", {"schema": "sim-bim-run.v1", "revision": draft["revision"],
              "locale": draft["locale"], "blocks": blocks, "taskBrief": draft["taskBrief"],
              "coordinator": draft["coordinator"], "workers": draft["workers"]})
    return materials, blocks


def configuration(directory, draft, materials, blocks):
    route = draft["coordinator"]
    case = {"case_id": directory.name, "mode": "orchestrator", "runtime": "dsh", "output": "production",
            "materials": materials, "blocks": blocks, "locale": draft["locale"],
            "scope": draft["taskBrief"].strip() or DEFAULT_BRIEF[draft["locale"]],
            **route, "workers": draft["workers"], "reasoning_effort": "high",
            "output_tokens": default_output_tokens(route["model"]), "image_history_batches": 3,
            "max_candidates": 256,
            "limits": {"model_calls": 100, "tool_calls": 500, "seconds": 5400, "tokens": None, "time_budget_mode": "wall"}}
    if draft["workers"]:
        case["worker"] = {"output_tokens": max(default_output_tokens(w["model"]) for w in draft["workers"])}
    return {"schema_version": 1, "approval_required_before_launch": True, "maximum_seconds_per_case": 5400,
            "batch_id": directory.name, "input_root": str(directory / "inputs"),
            "run_root": str(directory), "cases": [case]}


def begin(root, revision, catalog):
    with locked(root):
        draft = read_project(root)
        revision_check(draft, revision)
        allowed = {(c["provider"], m["id"]) for c in catalog if c["connected"] for m in c["models"]}
        for route in [draft["coordinator"], *draft["workers"]]:
            if route and (route["provider"], route["model"]) not in allowed:
                raise InputError("该模型当前不可用，请刷新连接。", "This model is unavailable. Refresh the connection.")
        previous = latest(root)
        if previous and previous["status"] == "running" and previous.get("revision") == revision:
            return previous
        identity = time.strftime("run-%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:8]
        directory = run_dir(root, identity)
        directory.parent.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=directory.parent, prefix=".preparing-") as staging:
            prepared = Path(staging) / "run"
            prepared.mkdir()
            materials, blocks = freeze(root, prepared, draft)
            save_json(prepared / "case.json", configuration(directory, draft, materials, blocks))
            started = {"id": identity, "status": "running", "revision": revision, "message": ""}
            save_json(prepared / "status.json", started)
            prepared.rename(directory)
        try:
            with (directory / "run.log").open("ab") as log:
                subprocess.Popen([sys.executable, "-B", "-m", "src.workbench.runs", str(directory)],
                    cwd=ROOT, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                    env={**os.environ, "PYTHONUTF8": "1", "PYTHONPATH": str(ROOT)},
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except OSError:
            status = {"id": identity, "status": "failed", "message": "启动失败。" if draft["locale"] == "zh-CN" else "Could not start the run."}
            save_json(directory / "status.json", status)
            return status
        return started


def status_at(directory):
    data = read_json(directory / "status.json")
    if data["status"] == "running" and time.time() - (directory / "status.json").stat().st_mtime > 30:
        # The supervisor owns this lock throughout the run, including while the UI is closed.
        try:
            with portalocker.Lock(directory / ".running", timeout=0):
                data = read_json(directory / "status.json")
                if data["status"] != "running":
                    return data
                data = {"id": data["id"], "status": "failed",
                        "message": "运行已中断。" if read_json(directory / "input.json")["locale"] == "zh-CN" else "The run was interrupted."}
                save_json(directory / "status.json", data)
        except portalocker.exceptions.LockException:
            pass
    return data


def latest(root):
    runs = inside(root, "runs")
    if not runs.is_dir():
        return None
    paths = [d for d in runs.iterdir() if RUN_ID.fullmatch(d.name) and (d / "status.json").is_file()]
    return status_at(max(paths, key=lambda d: (d / "input.json").stat().st_mtime_ns)) if paths else None


def execute(directory):
    from src.agent.runtime_configuration import load_configuration, selected_case
    from src.dsh_runtime.product import launch
    identity = directory.name
    locale = read_json(directory / "input.json")["locale"]
    with portalocker.Lock(directory / ".running", timeout=0):
        try:
            case = selected_case(load_configuration(directory / "case.json"), identity)
            if launch(case) != 0:
                raise ValueError("The model run stopped before delivery.")
            bim = inside(directory, "production/private/dsh/work/bim")
            delivery = read_json(bim / "delivery.json")
            viewer = delivery["viewer"]
            if not OUTPUT_FILE.fullmatch(viewer) or not inside(bim, viewer).is_file() or not (bim / "delivery.html").is_file():
                raise ValueError("No delivered viewer found")
            base = f"/results/{workspace_tag(directory.parent.parent)}/{identity}/"
            result = {"id": identity, "status": "completed", "message": "", "report": base + "delivery.html", "viewer": base + viewer}
        except Exception as exc:
            traceback.print_exc()
            message = "运行未完成，请查看工作区本次运行的 run.log。" if locale == "zh-CN" else "Run did not complete. See run.log in the run folder."
            if locale == "en" and isinstance(exc, ValueError):
                message = f"{exc} See run.log for details."
            result = {"id": identity, "status": "failed", "message": message}
        save_json(directory / "status.json", result)
    return int(result["status"] != "completed")


if __name__ == "__main__":
    raise SystemExit(execute(Path(sys.argv[1]).resolve()))
