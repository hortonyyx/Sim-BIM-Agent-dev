"""Run offline checks for the current product and its development interfaces."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import subprocess
import sys
import uuid

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tests.check_selection import files_for, inventory, unclassified

def select(scope, paths, changed):
    if paths:
        selected = []
        for value in paths:
            path = Path(value.split("::", 1)[0])
            absolute = path if path.is_absolute() else ROOT / path
            relative = absolute.resolve().relative_to(ROOT).as_posix()
            if not absolute.is_file() or not relative.startswith("tests/test_"):
                raise ValueError(f"select a test module explicitly: {value}")
            if relative not in inventory():
                raise ValueError(f"classify the test module first: {relative}")
            selected.append(relative + ("::" + value.split("::", 1)[1] if "::" in value else ""))
        return sorted(set(selected))
    missing = unclassified()
    if missing:
        raise ValueError("unclassified tests: " + ", ".join(missing))
    if changed:
        from scripts.dev.affected_tests import affected_tests
        result = affected_tests(changed)
        if result.scope == "SUBSET":
            selected = [p for p in result.tests if p in inventory()]
            if selected:
                return selected
        print("Affected mapping is incomplete; selecting node checks: " + "; ".join(result.reasons),
              file=sys.stderr)
        return files_for("node")
    if scope == "targeted":
        raise ValueError("targeted checks require explicit test files or --changed paths")
    return files_for(scope)

def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("scope", nargs="?", default="fast", choices=("fast", "targeted", "node"))
    parser.add_argument("paths", nargs="*")
    parser.add_argument("--changed", nargs="+")
    parser.add_argument("--cache-root", type=Path, default=Path("D:/Sim-BIM-Agent/cache/pytest"))
    parser.add_argument("--workers", type=int, choices=(0, 1, 2), default=0)
    parser.add_argument("--list", action="store_true", help="show the selection without running it")
    args = parser.parse_args(argv)
    try:
        if args.changed and args.paths:
            raise ValueError("choose explicit files or --changed, not both")
        if not args.cache_root.is_absolute():
            raise ValueError("--cache-root must be an absolute development cache directory")
        selected = select(args.scope, args.paths, args.changed)
    except (ValueError, KeyError) as exc:
        parser.error(str(exc))
    print(f"{args.scope}: {len(selected)} modules; workers={args.workers}; offline network guard active",
          flush=True)
    if args.list:
        print("\n".join(selected))
        return 0
    cache = args.cache_root / uuid.uuid4().hex[:12]
    cache.mkdir(parents=True)
    env = {**os.environ, "SIM_BIM_TEST_SCOPE": args.scope, "PYTHONPATH": str(ROOT),
           "PYTHONUTF8": "1", "PYTHONDONTWRITEBYTECODE": "1", "TEMP": str(cache), "TMP": str(cache)}
    command = [sys.executable, "-X", "utf8", "-B", "-m", "pytest", "-n", str(args.workers),
               "-p", "no_billed_gate", "-p", "no:cacheprovider", "--basetemp", str(cache / "temp"),
               "--junitxml", str(cache / "results.xml"), "-q", *selected]
    print("Evidence cache: " + str(cache), flush=True)
    return subprocess.run(command, cwd=ROOT, env=env).returncode

if __name__ == "__main__":
    raise SystemExit(main())
