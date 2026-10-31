"""Maintain immutable Sim-BIM release records in the development repository.

The installed runtime only reads these records. Registration and historical
metadata inspection stay here, outside the product payload.
"""
from __future__ import annotations

import argparse
from datetime import date, datetime
import json
import os
from pathlib import Path
import re
import subprocess
import sys

from src.agent_runtime.agent_registry import (
    AgentVersionMismatch, _registry_path, discover_files, load_agent_registry,
    release_records, source_commit,
)

_CATALOG_MODES = ("coordinator", "readonly", "coordinator_mesh", "readonly_mesh")

def _write_registry(path, registry):
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(registry, ensure_ascii=False, indent=2) + "\n",
                         encoding="utf-8", newline="\n")
    temporary.replace(path)


def _fingerprints(root, registry):
    command = [part.replace("{python}", sys.executable) for part in registry["fingerprint_provider"]]
    env = {**os.environ, "PYTHONPATH": str(root), "PYTHONDONTWRITEBYTECODE": "1",
           "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}
    output = subprocess.check_output(command, cwd=root, env=env, text=True,
                                     encoding="utf-8", timeout=180)
    result = json.loads(output)
    if set(result.get("tool_catalog_sha256", {})) != set(_CATALOG_MODES) or not result.get("mode_fingerprints"):
        raise ValueError("fingerprint provider did not return catalogs and mode fingerprints")
    return result


def register_versions(root, *, registry_path=None, registration_date=None, fingerprints=None):
    """Append changed runtime/domain snapshots without rewriting prior records."""
    root = Path(root).resolve()
    path = _registry_path(root, registry_path)
    loaded = load_agent_registry(root, path)
    if loaded.get("product_projection"):
        raise ValueError("Generated product versions are immutable; register and rebuild in the development repository")
    registry = loaded
    stamp = registration_date or date.today().strftime("%Y%m%d")
    if not re.fullmatch(r"\d{8}", stamp):
        raise ValueError("registration date must be YYYYMMDD")
    datetime.strptime(stamp, "%Y%m%d")
    snapshots = {line: discover_files(root, line, scope=registry["scope"], registry_path=path)
                 for line in ("runtime", "domain")}
    previous = registry["domain_versions"][registry["current_domain_version"]]
    domain_changed = snapshots["domain"] != previous["files"] or previous.get("coverage") != "automatic_directories"
    if fingerprints is None:
        fingerprints = _fingerprints(root, registry) if domain_changed else {
            "tool_catalog_sha256": previous["tool_catalog_sha256"], "mode_fingerprints": previous["mode_fingerprints"]}
    changed_modes = sorted(key for key in (previous.get("mode_fingerprints") or {}).keys() | fingerprints["mode_fingerprints"].keys()
                           if (previous.get("mode_fingerprints") or {}).get(key) != fingerprints["mode_fingerprints"].get(key))
    for line, before in snapshots.items():
        if before != discover_files(root, line, scope=registry["scope"], registry_path=path):
            raise ValueError("checkout changed during registration; retry after edits finish")
    changed = []
    for line in ("runtime", "domain"):
        records = registry[f"{line}_versions"]
        old = records.get(registry[f"current_{line}_version"], {})
        if (old.get("files") == snapshots[line] and old.get("coverage") == "automatic_directories"
                and (line == "runtime" or not changed_modes)):
            continue
        number = max((int(re.match(rf"{line}-v(\d+)-", key)[1]) for key in records), default=0) + 1
        version = f"{line}-v{number}-{stamp}"
        record = {"source_commit": source_commit(root), "coverage": "automatic_directories", "files": snapshots[line]}
        if line == "domain":
            record.update(fingerprints)
        records[version] = record
        registry[f"current_{line}_version"] = version
        changed.append(line)
    if changed:
        _write_registry(path, registry)
    return {"runtime_version": registry["current_runtime_version"],
            "domain_version": registry["current_domain_version"], "changed_lines": changed,
            "changed_modes": changed_modes, "source_commit": source_commit(root),
            "file_counts": {line: len(files) for line, files in snapshots.items()}}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    register = commands.add_parser("register", help="append changed runtime/domain snapshots")
    register.add_argument("--date", help="registration date YYYYMMDD (default: local today)")
    verify = commands.add_parser("verify", help="verify the current release or inspect recorded metadata")
    verify.add_argument("--version", help="recorded runtime/domain identity")
    verify.add_argument("--lookup", action="store_true", help="inspect metadata without asserting current bytes")
    for command in (register, verify):
        command.add_argument("--root", type=Path, default=Path.cwd())
        command.add_argument("--registry", type=Path)
    args = parser.parse_args(argv)
    if args.command == "register":
        result = register_versions(args.root, registry_path=args.registry, registration_date=args.date)
    elif args.lookup:
        registry = load_agent_registry(args.root, args.registry)
        selected = args.version or registry["current_domain_version"]
        record = registry["runtime_versions"].get(selected) or registry["domain_versions"].get(selected)
        if not isinstance(record, dict):
            raise AgentVersionMismatch(f"Version is not recorded: {selected}")
        result = {"version_id": selected, **record}
    else:
        records = release_records(args.root, registry_path=args.registry)
        if args.version and args.version not in {r["version_id"] for r in records.values()}:
            raise AgentVersionMismatch("Only the current release can be verified; use --lookup for recorded metadata")
        result = {line: {"version_id": record["version_id"], "source_commit": record["source_commit"]}
                  for line, record in records.items()}
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
