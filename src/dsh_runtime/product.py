"""Launch a unified product case with the installed DeepSeek Harness."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
import shutil
import sys

from src.agent.runtime_configuration import (
    ROOT, load_configuration, resolve_case_input, resolve_supplemental_inputs, selected_case,
)
from src.agent.bim_inputs import admitted_png_paths
from src.agent_runtime.connections import resolve_connection, validate_connection_model
from src.agent_runtime.estimation import get_model_profile
from src.agent_runtime.openai_subscription import SubscriptionCredentials
from src.agent_runtime.run_paths import resolve_run_output
from . import launcher


def installed_executable(configured: str | None) -> Path:
    explicit = configured or os.environ.get("SIM_BIM_DSH_EXECUTABLE")
    if explicit:
        executable = Path(explicit).expanduser().resolve()
    else:
        cli = shutil.which("dsh.cmd")
        if not cli:
            raise ValueError("set dsh_executable to the installed DeepSeek Harness.exe or add dsh.cmd to PATH")
        executable = Path(cli).resolve().parents[4] / "DeepSeek Harness.exe"
    if not executable.is_file() or not (executable.parent / "resources/app.asar").is_file():
        raise ValueError(f"DeepSeek Harness desktop installation missing at {executable}")
    return executable


def host_config(case, auth_dir):
    limits = case["limits"]
    coordinator_limits = {"wall_seconds": limits["seconds"], "model_calls": limits["model_calls"],
                          "tool_calls": limits["tool_calls"], "candidates": case["max_candidates"]}
    model = case["model"]
    config = {"runtime": "dsh", "model": model, "reasoning_effort": "high",
              "image_kind": case["image_kind"], "floor_images": case.get("floor_plan_images"),
              "chatgpt_auth_dir": str(auth_dir), "limits": coordinator_limits,
              "dsh_executable": str(installed_executable(case.get("dsh_executable"))),
              "context_window": case.get("context_tokens") or get_model_profile(model, strict=True).context_window_tokens,
              "image_history_batches": case.get("image_history_batches")}
    if case.get("worker") is not None:
        worker = case["worker"]
        pool = {"model": worker.get("model", model),
                "output_tokens": worker.get("output_tokens", case["output_tokens"]),
                "reasoning_effort": worker.get("reasoning_effort", "high"),
                "max_concurrent": worker.get("max_concurrent", 1)}
        if worker.get("background"):
            pool["background"] = True
        for key in ("model_calls", "tool_calls"):
            pool[key] = max(1, limits[key] // 4)
            coordinator_limits[key] -= pool[key]
        config["worker"] = pool
    return config


def verify_resume_inputs(manifest, material_paths):
    images = material_paths["images"]
    hashes = {p.name: launcher.digest(p) for p in admitted_png_paths(images)} if images else {}
    expected = {name: data["sha256"] for name, data in manifest["images"].items()}
    if hashes != expected:
        raise ValueError("configured images differ from the admitted DSH run")
    for field, saved, digest_key in (("mesh", "mesh_input", "sha256"),
                                     ("building_input", "building_input", "raw_sha256")):
        path = material_paths[field]
        actual = launcher.digest(path) if path else None
        if actual != (manifest.get(saved) or {}).get(digest_key):
            raise ValueError(f"configured {field} differs from the admitted DSH run")


def launch(case, *, resume=False):
    if case.get("mode") != "orchestrator" or case.get("runtime") != "dsh":
        raise ValueError("select an orchestrator case using the dsh runtime")
    run_root = case.get("run_root")
    output = resolve_run_output(Path(case["output"]) if run_root else ROOT / case["output"],
                                repository_root=ROOT, run_root=run_root)
    credentials = SubscriptionCredentials(Path(case["chatgpt_auth_dir"]) if case.get("chatgpt_auth_dir") else None)
    config = host_config(case, credentials.directory)
    primary = resolve_case_input(case)
    auxiliary, declaration = resolve_supplemental_inputs(case)
    mesh = primary if case["input_kind"] == "mesh" else None
    materials = {"images": auxiliary if mesh else primary, "mesh": mesh, "building_input": declaration}
    if resume:
        if launcher.read(output / "config.json") != config or (output / "shared/task.txt").read_text(encoding="utf-8") != case["scope"]:
            raise ValueError("requested DSH configuration or task differs from the frozen run")
        verify_resume_inputs(launcher.read(output / "private/dsh/work/bim/inputs.json"), materials)
    elif output.exists():
        raise ValueError(f"run directory already exists: {output}")
    connection = resolve_connection(case["provider"], None, chatgpt_auth_dir=credentials.directory)
    for model in dict.fromkeys([case["model"], config.get("worker", {}).get("model", case["model"])]):
        asyncio.run(validate_connection_model(connection, model))
    if not resume:
        output.mkdir(parents=True, exist_ok=False)
        (output / "shared").mkdir()
        (output / "shared/task.txt").write_text(case["scope"], encoding="utf-8")
        (output / "config.json").write_text(json.dumps(config, indent=2), encoding="utf-8")
        launcher.prepare(output, material_paths=materials)
    return launcher.run(output, resume, credential=credentials,
                        image_history_batches=case.get("image_history_batches"))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("configuration", type=Path)
    parser.add_argument("--case", required=True)
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args()
    case = selected_case(load_configuration(args.configuration), args.case)
    try:
        return launch(case, resume=args.resume)
    except (OSError, ValueError) as error:
        print(str(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
