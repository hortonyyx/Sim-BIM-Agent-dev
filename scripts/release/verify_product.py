"""Verify an exported product offline using its own Python and external evidence.

Usage: python scripts/release/verify_product.py --product C:/path/to/product
       --output D:/Sim-BIM-Agent/cache/product-smoke-001
The output must be new. This script neither installs dependencies nor signs in.
"""
from __future__ import annotations

import argparse
import asyncio
from contextlib import redirect_stdout
import hashlib
from html.parser import HTMLParser
import importlib.machinery
import io
import ipaddress
import json
import os
from pathlib import Path
import runpy
import shutil
import signal
import subprocess
import sys
import time
from urllib.parse import unquote, urlsplit


def _save(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _require(condition, message):
    if not condition:
        raise ValueError(message)


def _project_name(name):
    return name in {"src", "scripts"} or name.startswith(("src.", "scripts."))


def _spec_paths(spec):
    paths = list(spec.submodule_search_locations or [])
    if spec.origin and spec.origin not in {"built-in", "frozen"}:
        paths.append(spec.origin)
    return sorted({str(Path(path).resolve()) for path in paths})


def _loaded_origins(product):
    origins = {}
    for name, module in list(sys.modules.items()):
        if not _project_name(name) or module is None:
            continue
        spec = getattr(module, "__spec__", None)
        paths = _spec_paths(spec) if spec else []
        if getattr(module, "__file__", None):
            paths.append(str(Path(module.__file__).resolve()))
        _require(paths and all(Path(path).is_relative_to(product) for path in paths),
                 f"Project module loaded outside product: {name}: {paths}")
        origins[name] = sorted(set(paths))
    return origins


def _guard_imports(product, log):
    # Check startup .pth side effects too, before choosing the product source tree.
    _loaded_origins(product)
    sys.path.insert(0, str(product))  # Equivalent to launching product/sim_bim.py.
    finders = tuple(sys.meta_path)

    class ProductFinder:
        def find_spec(self, fullname, path=None, target=None):
            if not _project_name(fullname):
                return None
            for finder in finders:
                method = getattr(finder, "find_spec", None)
                spec = method(fullname, path, target) if method else None
                if spec is None:
                    continue
                paths = _spec_paths(spec)
                _require(paths and all(Path(p).is_relative_to(product) for p in paths),
                         f"Project import escapes product: {fullname}: {paths}")
                with log.open("a", encoding="utf-8") as stream:
                    stream.write(json.dumps({"module": fullname, "paths": paths}) + "\n")
                return spec
            raise ImportError(f"Project module unavailable in product: {fullname}")

    sys.meta_path.insert(0, ProductFinder())


def _guard_network(log):
    """Allow only local IPC sockets needed by asyncio; no DNS or remote sockets."""
    def local(host):
        if host == "localhost":
            return True
        try:
            return ipaddress.ip_address(host).is_loopback
        except ValueError:
            return False

    def audit(event, args):
        host = None
        if event == "socket.getaddrinfo":
            host = args[0]
        elif event in {"socket.connect", "socket.sendto"}:
            address = args[-1]
            if isinstance(address, tuple) and address:
                host = address[0]
        if host is not None and not local(host):
            with log.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps({"blocked_event": event, "host": str(host)}) + "\n")
            raise PermissionError("Product smoke forbids DNS and non-loopback network access")
    sys.addaudithook(audit)


def _environment(product, output):
    keep = {"SYSTEMROOT", "WINDIR", "COMSPEC", "PATH", "PATHEXT", "NUMBER_OF_PROCESSORS"}
    environment = {key: value for key, value in os.environ.items() if key.upper() in keep}
    for name in ("home", "temp", "home/local", "home/roaming"):
        (output / name).mkdir(parents=True, exist_ok=True)
    environment.update(PYTHONPATH="", PYTHONUTF8="1", PYTHONDONTWRITEBYTECODE="1",
        PYTHONNOUSERSITE="1", HOME=str(output / "home"), USERPROFILE=str(output / "home"),
        LOCALAPPDATA=str(output / "home/local"), APPDATA=str(output / "home/roaming"),
        TMP=str(output / "temp"), TEMP=str(output / "temp"), TMPDIR=str(output / "temp"),
        OPENBLAS_NUM_THREADS="1", OMP_NUM_THREADS="1", MPLBACKEND="Agg")
    return environment


def _check_environment_files(product):
    candidates = ([product / ".venv/Lib/site-packages"] if os.name == "nt" else
                  list((product / ".venv/lib").glob("python*/site-packages")))
    _require(len(candidates) == 1, "Cannot identify the product environment's site-packages")
    site = candidates[0]
    _require(site.is_dir(), f"Product site-packages missing: {site}")
    _require(not list(site.glob("*.egg-link")), "Product environment contains editable egg-link")
    checked = []
    for path in site.glob("*.pth"):
        text = path.read_text(encoding="utf-8-sig")
        _require("__editable__" not in text.casefold(), f"Editable import hook: {path.name}")
        for line in text.splitlines():
            line = line.strip()
            if line and not line.startswith(("#", "import ", "import\t")):
                _require((site / line).resolve().is_relative_to(product),
                         f"External package path in {path.name}: {line}")
        checked.append(path.name)
    for path in site.glob("*.dist-info/direct_url.json"):
        data = json.loads(path.read_bytes())
        _require(not data.get("dir_info", {}).get("editable"), f"Editable distribution: {path.parent.name}")
    return {"site_packages": str(site), "pth_files": sorted(checked), "editable_distributions": 0}


def _all_module_specs(product):
    manifest = json.loads((product / "release-manifest.json").read_bytes())
    specs, result, nonmodules = {}, {}, []

    def find(name):
        if name not in specs:
            parent = name.rpartition(".")[0]
            paths = find(parent).submodule_search_locations if parent else [str(product)]
            # Resolve one path component without importing its parent. A full
            # dotted namespace name assumes that parent is in sys.modules.
            spec = importlib.machinery.PathFinder.find_spec(name.rsplit(".", 1)[-1], paths)
            _require(spec is not None, f"Missing payload module: {name}")
            if spec.submodule_search_locations is not None:
                spec.submodule_search_locations = list(spec.submodule_search_locations)
            specs[name] = spec
        return specs[name]

    for relative in manifest["files"]:
        path = Path(relative)
        if path.parts[0] not in {"src", "scripts"} or path.suffix != ".py":
            continue
        parts = list(path.with_suffix("").parts)
        if parts[-1] == "__init__":
            parts.pop()
        if not all(part.isidentifier() for part in parts):
            nonmodules.append(relative)
            continue
        name = ".".join(parts)
        spec = find(name)
        _require(spec.origin and Path(spec.origin).resolve() == (product / relative).resolve(),
                 f"Payload module resolves to another installation: {name}: {spec.origin}")
        result[name] = _spec_paths(spec)
    return {"module_count": len(result), "modules": result, "nonimportable_script_paths": nonmodules}


def _cli(product, arguments):
    capture, previous = io.StringIO(), sys.argv
    try:
        sys.argv = [str(product / "sim_bim.py"), *arguments]
        with redirect_stdout(capture):
            try:
                runpy.run_path(str(product / "sim_bim.py"), run_name="__main__")
            except SystemExit as stopped:
                _require(stopped.code in (None, 0), f"Product CLI failed: {arguments[0]}")
    finally:
        sys.argv = previous
    return json.loads(capture.getvalue())


class _HtmlResources(HTMLParser):
    def __init__(self):
        super().__init__()
        self.urls = []

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        key = "src" if tag in {"script", "img", "source", "iframe"} else "href" if tag == "link" else None
        if key and values.get(key):
            self.urls.append(values[key])


def _check_demo(product, output, demo):
    _require(demo["status"] == "completed" and demo["model_requests"] == 0, "Demo did not complete offline")
    source, viewer = Path(demo["source_model"]).resolve(), Path(demo["viewer"]).resolve()
    _require(source.is_relative_to(output / "demo") and viewer.is_relative_to(output / "demo"),
             "Demo returned a path outside its output")
    data, html = json.loads(source.read_bytes()), viewer.read_text(encoding="utf-8")
    kinds = sorted(opening["kind"] for opening in data["openings"])
    _require(len(data["spaces"]) == demo["spaces"] == 2, "Demo must contain two source spaces")
    _require(kinds == ["door", "window"] and demo["openings"] == 2, "Demo must contain one door and one window")
    provenance = data["generation"]["provenance"]
    _require(provenance.get("mode") == "synthetic_offline_demo"
             and provenance.get("generator") == "sim-bim synthetic offline demo"
             and "subscription" not in json.dumps(provenance).lower(), "Demo provenance is not synthetic/offline")
    embedded = {}
    for name in ("three.min.js", "OrbitControls.js"):
        path = product / "scripts/tool_scripts/vendor" / name
        raw = path.read_bytes()
        _require(raw.decode("utf-8") in html, f"Viewer does not embed its bundled {name}")
        embedded[name] = hashlib.sha256(raw).hexdigest()
    resources = _HtmlResources()
    resources.feed(html)
    for url in resources.urls:
        parsed = urlsplit(url)
        if parsed.scheme == "data":
            continue
        _require(not parsed.scheme and not parsed.netloc, f"Viewer requires external resource: {url}")
        target = (viewer.parent / unquote(parsed.path)).resolve()
        _require(target.is_relative_to(output / "demo") and target.is_file(), f"Viewer resource is unavailable: {url}")
    return {**demo, "provenance": provenance, "opening_kinds": kinds,
            "embedded_vendor_sha256": embedded, "resource_urls": resources.urls,
            "browser_rendering": "not_exercised", "drawing_fidelity": "not_evaluated"}


def _response(tool=False):
    message = {"role": "assistant", "content": None if tool else "Offline MCP inputs received."}
    if tool:
        message["tool_calls"] = [{"id": "release-probe", "type": "function",
            "function": {"name": "inputs", "arguments": "{}"}}]
    return {"id": "offline-probe", "model": "scripted-model", "choices": [{"index": 0,
        "message": message, "finish_reason": "tool_calls" if tool else "stop"}],
        "usage": {"prompt_tokens": 20, "completion_tokens": 10, "total_tokens": 30}}


async def _input_routes_probe(product, output):
    """Exercise the shipped templates and native input tools without a model."""
    import numpy as np
    from PIL import Image
    import trimesh
    from src.agent import runtime_configuration, runtime_entry
    from src.agent.runtime_roles import entry as drawing_entry
    from src.agent.geometry.mesh_observation import MeshObservation
    from scripts.tool_scripts.run_bim_agent import build_tool_server

    root = output / "input-routes"
    inputs = root / "inputs"
    drawings = inputs / "my-building"
    auxiliary = drawings / "auxiliary"
    auxiliary.mkdir(parents=True)
    Image.new("RGB", (12, 8), "white").save(drawings / "plan.png")
    Image.new("RGB", (12, 8), "gray").save(auxiliary / "sketch.png")
    _save(drawings / "building.json", {"description": "Synthetic offline box; not a real building."})
    box = trimesh.creation.box(extents=[4, 3, 6])
    box.visual = trimesh.visual.TextureVisuals(
        uv=np.zeros((len(box.vertices), 2)), image=Image.new("RGB", (4, 4), "gray"))
    asset = drawings / "building.glb"
    asset.write_bytes(trimesh.Scene(box).export(file_type="glb"))

    results = {}
    for name, template, entry in (("drawings", "config.example.json", drawing_entry),
                                  ("mesh", "config.mesh.example.json", runtime_entry)):
        configuration = json.loads((product / template).read_bytes())
        configuration.update(input_root=str(inputs), run_root=str(root / "runs"))
        path = root / f"{name}.json"
        _save(path, configuration)
        case = runtime_configuration.load_configuration(path)["cases"][0]
        command = runtime_configuration.argv_for(case)
        _require(command[2] == entry.__name__, f"Wrong {name} runtime entrypoint")
        args = entry.parser().parse_args(command[3:])
        run, _, _ = entry.prepare_inputs(args.out, images=args.images, mesh=args.mesh,
            building_input=args.building_input, scope=args.scope, image_kind=args.image_kind,
            max_candidates=args.max_candidates, floor_plan_images=args.floor_plan_images)
        manifest = json.loads((run / "inputs.json").read_bytes())
        frozen = []
        for image_name, metadata in manifest["images"].items():
            frozen.append((args.images / image_name, run / "images" / image_name, metadata["sha256"]))
        if args.mesh is not None:
            frozen.append((args.mesh, run / "assets/input.glb", manifest["mesh_input"]["sha256"]))
        if args.building_input is not None:
            frozen.append((args.building_input, run / "building_input.json",
                           manifest["building_input"]["raw_sha256"]))
        for original, copied, digest in frozen:
            _require(original.read_bytes() == copied.read_bytes()
                     and hashlib.sha256(copied.read_bytes()).hexdigest() == digest,
                     f"{name} input was not frozen faithfully: {copied.name}")
        catalog = await build_tool_server(run).list_tools()
        available = {tool.name for tool in catalog}
        required = {"inputs", "view_image", "build_bim", "finish_bim"}
        if name == "mesh":
            required.update({"inspect_mesh", "view_mesh", "measure_mesh_pixels"})
        _require(required <= available, f"Missing {name} tools: {sorted(required - available)}")
        results[name] = {"passed": True, "template": template, "mode": case["mode"],
            "entrypoint": command[2], "source_input_mode": manifest["source_input_mode"],
            "frozen_files": len(frozen), "required_tools": sorted(required)}
        if name == "mesh":
            prefix = run / "mesh_observations" / "release_probe"
            prefix.parent.mkdir()
            picture, metadata = MeshObservation(run / "assets/input.glb").render(prefix,
                eye=[10, 10, 10], target=[0, 0, 0], width_m=10, height_m=10,
                width_px=64, height_px=64)
            with np.load(prefix.with_suffix(".npz")) as pixels:
                hits = len(pixels["pixel_indices"])
            _require(picture.size == (64, 64) and hits > 0, "Mesh renderer produced no surface pixels")
            _require(metadata["mesh_sha256"] == manifest["mesh_input"]["sha256"],
                     "Mesh observation refers to another input")
            results[name]["render"] = {"image": str(prefix.with_suffix(".png")),
                "image_sha256": metadata["image_sha256"], "surface_pixels": hits}
    return {"passed": True, "live_model_requests": 0, "routes": results,
            "scope": "shipped configuration, entrypoint parsing, frozen inputs, tool availability and mesh rendering",
            "not_exercised": ["live model calls", "reader delegation", "whole-building reconstruction",
                              "architectural quality", "browser rendering"]}


async def _runtime_probe(product, output):
    from src.agent.runtime_tools import FrozenBimTools, coordinator_role
    from src.agent_runtime.adapter import ScriptedAdapter
    from src.agent_runtime.loop import RunLimits, Runtime
    from src.agent_runtime.mcp_tools import McpToolClient
    from src.agent_runtime.store import EventStore
    from src.agent_runtime.versions import make_versions
    from src.harness_contracts import VersionManifest

    class Interrupted(BaseException):
        pass

    def fault(name, engine):
        if name == "after_checkpoint" and any(event.payload.event_type == "tool_execution"
                                               for event in engine.store.events):
            raise Interrupted()

    limits = RunLimits(model_calls=3, tool_calls=2, seconds=180, tokens=1000000,
                       max_model_retries=0, summary_every=0)
    role = coordinator_role(limits.ledger_limit())
    guide = "Offline release verification: call inputs once, then finish."
    messages = [{"role": "system", "content": guide}, {"role": "user", "content": "Read the synthetic demo inventory."}]
    parameters = {"max_tokens": 256, "temperature": 0.0}
    directory, phases = output / "runtime", []
    for phase in ("interrupt", "resume", "already_completed"):
        client = McpToolClient(command=sys.executable, args=["-I", "-B", str(Path(__file__).resolve()),
            "--mcp-server", "--product", str(product), "--output", str(output), "--phase", phase],
            cwd=output / "isolated-cwd", run_directory=output / "demo")
        async with client:
            tools = FrozenBimTools(client, role, run_directory=output / "demo")
            catalog = await tools.list_tools()
            specs = [{"type": "function", "function": {"name": tool["name"],
                "description": tool.get("description", ""), "parameters": tool["inputSchema"]}} for tool in catalog]
            adapter = ScriptedAdapter([_response(tool=True)] if phase == "interrupt" else
                                      [_response()] if phase == "resume" else [])
            with EventStore(directory, run_id="release-smoke", task_id="coordinator", budget_limit=limits.ledger_limit()) as store:
                if phase == "interrupt":
                    versions = make_versions(store, root=product, prompt=guide, tools=specs, parameters=parameters,
                        route={"route_id": "scripted", "model": "scripted-model"})
                    store.write_json("versions.json", versions.model_dump(mode="json"))
                else:
                    versions = VersionManifest.model_validate_json((directory / "versions.json").read_bytes())
                engine = Runtime(store=store, adapter=adapter, tools=tools, role=role, model="scripted-model",
                    parameters=parameters, versions=versions, limits=limits,
                    fault_hook=fault if phase == "interrupt" else None)
                before = store.path.read_bytes() if store.path.exists() else b""
                try:
                    receipt = await engine.run(messages, resume=phase != "interrupt")
                except Interrupted:
                    _require(phase == "interrupt", "Unexpected interruption during resume")
                    receipt = {"status": "interrupted_after_durable_tool_checkpoint"}
                else:
                    _require(phase != "interrupt", "Checkpoint interruption was not reached")
                    _require(receipt["status"] == "completed", f"Runtime probe did not complete: {receipt['status']}")
                events = store.validate().events
                executed = [event.payload for event in events if event.payload.event_type == "tool_execution"]
                _require(len(executed) == 1 and executed[0].tool_name == "inputs"
                         and executed[0].outcome == "succeeded", "MCP tool failed or was replayed on resume")
                if phase == "already_completed":
                    _require(receipt.get("resume_status") == "already_completed"
                             and store.path.read_bytes() == before and not adapter.requests,
                             "Completed resume changed the journal or sent a request")
                phases.append({"phase": phase, "status": receipt["status"],
                    "fixture_requests": len(adapter.requests), "durable_tool_executions": len(executed),
                    "event_count": len(events)})
                store.write_json(f"smoke-{phase}.json", receipt)
    _require(sum(phase["fixture_requests"] for phase in phases) == 2, "Unexpected fixture request count")
    return {"passed": True, "phases": phases, "live_model_requests": 0, "fixture_requests": 2,
            "real_mcp_tool_executions": 1, "journal": str(directory / "events.jsonl")}


def _worker(args):
    product, output = args.product.resolve(), args.output.resolve()
    if args.mcp_server:
        _guard_network(output / "blocked-network.jsonl")
        _guard_imports(product, output / f"mcp-{args.phase}-imports.jsonl")
        sys.argv = [str(product / "scripts/tool_scripts/run_bim_agent.py"), "serve", str(output / "demo")]
        runpy.run_path(sys.argv[0], run_name="__main__")
        return 0
    report = {"passed": False, "product": str(product), "python": sys.executable,
              "cwd": str(Path.cwd()), "pythonpath": os.environ.get("PYTHONPATH"), "live_model_requests": 0}
    try:
        _guard_network(output / "blocked-network.jsonl")
        _guard_imports(product, output / "worker-imports.jsonl")
        _require(not (product / ".git").exists(), "Product still contains Git metadata")
        _require(Path(sys.prefix).resolve() == product / ".venv", "Worker is not using product Python")
        _require(Path.cwd().resolve() == output / "isolated-cwd", "Worker cwd is not isolated")
        report["doctor"] = _cli(product, ["doctor"])
        _require(Path(report["doctor"]["directory"]).resolve() == product, "Doctor inspected another checkout")
        report["module_specs"] = _all_module_specs(product)
        report["demo"] = _check_demo(product, output, _cli(product, ["demo", "--output", str(output / "demo")]))
        report["input_routes"] = asyncio.run(_input_routes_probe(product, output))
        report["runtime"] = asyncio.run(_runtime_probe(product, output))
        report["loaded_project_modules"] = _loaded_origins(product)
        _require(not (output / "blocked-network.jsonl").exists(), "A network attempt was blocked; inspect evidence")
        report["passed"] = True
    except Exception as error:
        report["error"] = {"type": type(error).__name__, "message": str(error)}
    finally:
        _save(output / "worker-result.json", report)
    return 0 if report["passed"] else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--product", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--timeout-seconds", type=int, default=300)
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--mcp-server", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--phase", default="", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.worker or args.mcp_server:
        return _worker(args)
    product, output = args.product.expanduser().resolve(), args.output.expanduser().resolve()
    _require(not output.exists(), "Output must be a new directory; evidence is never overwritten")
    _require(not output.is_relative_to(product) and not output.is_relative_to(Path(__file__).resolve().parents[2]),
             "Verification output must be outside both product and development checkout")
    _require(args.timeout_seconds > 0, "timeout-seconds must be positive")
    output.mkdir(parents=True)
    (output / "isolated-cwd").mkdir()
    started = time.monotonic()
    report = {"schema_version": "sim-bim-product-verification.v1", "passed": False, "product": str(product),
              "output": str(output), "live_model_requests": 0, "paid_api_requests": 0,
              "stdout": str(output / "stdout.txt"), "stderr": str(output / "stderr.txt")}
    try:
        manifest_path = product / "release-manifest.json"
        report["release_manifest_sha256"] = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
        python = product / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        _require(python.is_file(), f"Product Python missing: {python}")
        _require(not (product / ".git").exists(), "Product contains Git metadata")
        report["environment"] = _check_environment_files(product)
        worker = output / "verify-worker.py"
        shutil.copyfile(Path(__file__).resolve(), worker)
        command = [str(python), "-I", "-B", str(worker), "--worker", "--product", str(product), "--output", str(output)]
        report["command"] = command
        with (output / "stdout.txt").open("w", encoding="utf-8") as stdout, (output / "stderr.txt").open("w", encoding="utf-8") as stderr:
            process = subprocess.Popen(command, cwd=output / "isolated-cwd", env=_environment(product, output),
                stdout=stdout, stderr=stderr, start_new_session=os.name != "nt")
            try:
                returncode = process.wait(timeout=args.timeout_seconds)
            except subprocess.TimeoutExpired:
                # Own process tree only: a killed worker must not orphan its MCP server.
                if os.name == "nt":
                    subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                                   stdout=stdout, stderr=stderr, check=False, timeout=20)
                else:
                    os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=20)
                raise
        report["returncode"] = returncode
        if (output / "worker-result.json").is_file():
            report["verification"] = json.loads((output / "worker-result.json").read_bytes())
        _require(hashlib.sha256(manifest_path.read_bytes()).hexdigest() == report["release_manifest_sha256"],
                 "Release manifest changed during verification")
        report["passed"] = returncode == 0 and report.get("verification", {}).get("passed") is True
    except Exception as error:
        report["error"] = {"type": type(error).__name__, "message": str(error)}
    report["elapsed_seconds"] = round(time.monotonic() - started, 3)
    _save(output / "report.json", report)
    print(json.dumps({"passed": report["passed"], "report": str(output / "report.json")}, ensure_ascii=False))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
