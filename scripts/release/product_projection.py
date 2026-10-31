"""Deterministic current-BIM payload and dependency projection.

The profile names runtime entry points, dynamic resources and deliberate package
facades. Python imports supply the remaining closure; forbidden subsystems fail
the build instead of silently widening the distributed product.
"""
from __future__ import annotations

import ast
import hashlib
import json
from pathlib import PurePosixPath
import sys
import tomllib

PROFILE = "scripts/release/product_profile.json"
LOCK = "scripts/release/product-runtime.lock"
LOCK_METADATA = "scripts/release/product-runtime-lock.json"
REGISTRY = "src/agent_runtime/agent_versions.json"


def sha(data):
    return hashlib.sha256(data).hexdigest()


def json_bytes(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")


def module_name(path):
    return path[:-3].replace("/", ".").removesuffix(".__init__")


class RuntimeImports(ast.NodeVisitor):
    def __init__(self):
        self.imports = []
        self.dynamic = []
        self.unresolved_dynamic = []

    def visit_If(self, node):
        test = node.test
        checking = ((isinstance(test, ast.Name) and test.id == "TYPE_CHECKING")
                    or (isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING"))
        if checking:
            for child in node.orelse:
                self.visit(child)
        else:
            self.generic_visit(node)

    def visit_Import(self, node):
        self.imports.append(node)

    def visit_ImportFrom(self, node):
        self.imports.append(node)

    def visit_Call(self, node):
        name = getattr(node.func, "id", getattr(node.func, "attr", ""))
        if name in {"import_module", "__import__"} and node.args and isinstance(node.args[0], ast.Constant):
            if isinstance(node.args[0].value, str):
                self.dynamic.append(node.args[0].value)
            else:
                self.unresolved_dynamic.append(node.lineno)
        elif name in {"import_module", "__import__"}:
            self.unresolved_dynamic.append(node.lineno)
        self.generic_visit(node)


def project_sources(sources):
    profile = json.loads(sources[PROFILE])
    if profile.get("schema_version") != "sim-bim-product-profile.v1":
        raise ValueError("Unknown product profile")
    facades = profile["facades"]
    modules = {module_name(p): p for p in sources
               if p.endswith(".py") and p.startswith(("src/", "scripts/"))}
    dynamic = ast.parse(sources["src/agent/bim_inputs.py"].decode("utf-8-sig"))
    declarations = [n.value for n in dynamic.body if isinstance(n, ast.Assign)
                    and any(isinstance(t, ast.Name) and t.id == "IMPLEMENTATION_FILES" for t in n.targets)]
    if len(declarations) != 1:
        raise ValueError("Cannot identify the frozen-input implementation resource list")
    resources = set(profile["resources"]) | set(ast.literal_eval(declarations[0]))
    todo = set(profile["roots"]) | {module_name(p) for p in resources if p.endswith(".py")}
    reached, external = set(), set()

    def enqueue(name, *, required=False):
        if name in modules:
            todo.add(name)
        elif any(m.startswith(name + ".") for m in modules):
            pass  # A Python namespace package has no __init__.py payload.
        elif required and name.startswith(("src.", "scripts.")):
            raise ValueError(f"Unavailable internal product import: {name}")
        elif not name.startswith(("src.", "scripts.")):
            external.add(name.split(".")[0])

    while todo:
        name = todo.pop()
        if name in reached:
            continue
        if name not in modules:
            raise ValueError(f"Product entry module is unavailable: {name}")
        reached.add(name)
        path = modules[name]
        if path in profile["forbidden_files"] or path.startswith(tuple(profile["forbidden_prefixes"])):
            raise ValueError(f"Current BIM runtime imports an excluded subsystem: {path}")
        tree = ast.parse(sources[facades.get(path, path)].decode("utf-8-sig"))
        visitor = RuntimeImports()
        visitor.visit(tree)
        if visitor.unresolved_dynamic:
            raise ValueError(f"Unresolved dynamic product imports in {path}: {visitor.unresolved_dynamic}")
        for imported in visitor.imports:
            if isinstance(imported, ast.Import):
                for alias in imported.names:
                    enqueue(alias.name, required=True)
            else:
                package = name if path.endswith("/__init__.py") else name.rpartition(".")[0]
                parts = package.split(".")
                parent = ".".join(parts[:len(parts) - imported.level + 1]) if imported.level else ""
                base = ".".join(x for x in (parent, imported.module) if x)
                enqueue(base, required=True)
                for alias in imported.names:
                    candidate = base + "." + alias.name
                    if candidate in modules:
                        enqueue(candidate)
                    elif modules.get(base) in facades:
                        raise ValueError(f"Product uses an excluded aggregate API: {base}.{alias.name}")
        for imported in visitor.dynamic:
            enqueue(imported, required=True)
        for size in range(1, len(name.split("."))):
            parent = ".".join(name.split(".")[:size])
            if parent in modules:
                todo.add(parent)
    selected = resources | {modules[m] for m in reached} | set(profile["entry_files"])
    route_tree = ast.parse(sources["src/agent/runtime_configuration.py"].decode("utf-8-sig"))
    route_values = {target.id: ast.literal_eval(node.value) for node in route_tree.body
                    if isinstance(node, ast.Assign) for target in node.targets
                    if isinstance(target, ast.Name) and target.id in {"ALLOWED_ENTRYPOINTS", "PRODUCT_RUN_MODES"}}
    for mode in route_values["PRODUCT_RUN_MODES"]:
        module = route_values["ALLOWED_ENTRYPOINTS"][mode]
        if module.replace(".", "/") + ".py" not in selected:
            raise ValueError(f"Product mode {mode} points to an undistributed entrypoint: {module}")
    payload, origins = {}, {}
    for name in sorted(selected):
        original = facades.get(name, name)
        if original not in sources:
            raise ValueError(f"Required product resource is unavailable: {original}")
        payload[name] = sources[original]
        origins[name] = {"source": original, "source_sha256": sha(sources[original]),
                         "transform": "package_facade" if name in facades else "identity"}
    for name in profile["product_files"]:
        original = "product/" + name
        data = sources[original]
        windows_script = PurePosixPath(name).suffix.casefold() in {".cmd", ".bat", ".ps1"}
        payload[name] = data.replace(b"\r\n", b"\n").replace(b"\n", b"\r\n") if windows_script else data
        origins[name] = {"source": original, "source_sha256": sha(data),
                         "transform": "windows_crlf" if windows_script else "identity"}
    imports = external - set(sys.stdlib_module_names) - {"__future__"}
    unowned = imports - profile["import_distributions"].keys()
    if unowned:
        raise ValueError(f"Unmapped runtime dependencies: {sorted(unowned)}")
    return payload, origins, profile, imports


def dependency_project(sources, profile, imports):
    development = tomllib.loads(sources["pyproject.toml"].decode("utf-8"))["project"]
    lock = tomllib.loads(sources["uv.lock"].decode("utf-8"))
    versions = {}
    for package in lock["package"]:
        if "version" in package and "registry" in package.get("source", {}):
            versions.setdefault(package["name"], set()).add(package["version"])
    requirements = []
    for distribution in sorted({profile["import_distributions"][name] for name in imports}):
        name = distribution.split("[")[0]
        if len(versions.get(name, set())) != 1:
            raise ValueError(f"Product dependency needs one development-locked version: {name}")
        requirements.append(distribution + "==" + next(iter(versions[name])))
    lines = ["# Generated from the current BIM import closure and development uv.lock.",
             "[project]", 'name = "sim-bim-agent"', 'version = "0.1.0"',
             'description = "Sim-BIM Agent: drawings and textured mesh to structured BIM"',
             'readme = "README.md"', "requires-python = " + json.dumps(development["requires-python"]),
             "dependencies = [", *["    " + json.dumps(r) + "," for r in requirements], "]", "",
             "[project.scripts]", 'sim-bim-agent = "src.agent.product_cli:main"', "",
             "[tool.uv]", "package = false", ""]
    return "\n".join(lines).encode("utf-8"), requirements


def with_dependencies(sources, payload, origins, profile, imports):
    pyproject, requirements = dependency_project(sources, profile, imports)
    metadata = json.loads(sources[LOCK_METADATA])
    expected = {"profile_sha256": sha(sources[PROFILE]), "development_lock_sha256": sha(sources["uv.lock"]),
                "product_pyproject_sha256": sha(pyproject), "product_lock_sha256": sha(sources[LOCK])}
    if any(metadata.get(k) != v for k, v in expected.items()):
        raise ValueError("Product dependency projection changed; regenerate its reviewed lock")
    payload["pyproject.toml"] = pyproject
    payload["uv.lock"] = sources[LOCK]
    origins["pyproject.toml"] = {"source": "pyproject.toml", "source_sha256": sha(sources["pyproject.toml"]),
                                 "transform": "runtime_dependencies_from_development_lock", "dependencies": requirements}
    origins["uv.lock"] = {"source": LOCK, "source_sha256": sha(sources[LOCK]), "transform": "identity"}
    return requirements


def register_projection(destination, *, sources, commit, profile):
    """Create new product identities; never rewrite a source registry record."""
    from src.agent_runtime.agent_registry import discover_files, _fingerprints, product_version_id

    source_registry = json.loads(sources[REGISTRY])
    strategy_files = [PROFILE, "scripts/release/product_projection.py", "scripts/release/build_product.py",
                      "scripts/release/prepare_product_dependencies.py", LOCK_METADATA,
                      *sorted(set(profile["facades"].values()))]
    provenance = {"schema_version": "sim-bim-product-projection.v1", "source_commit": commit,
                  "source_runtime_version": source_registry["current_runtime_version"],
                  "source_domain_version": source_registry["current_domain_version"],
                  "source_registry_sha256": sha(sources[REGISTRY]),
                  "strategy_sources": {name: sha(sources[name]) for name in strategy_files}}
    # Broad code scopes deliberately detect injected parent packages and new
    # unregistered code too. Runtime ownership is subtracted from domain.
    scope = {"runtime": ["src/agent_runtime", "src/harness_contracts", "src/__init__.py",
                         "src/utils", "pyproject.toml", "uv.lock"],
             "domain": ["src", "scripts", "sim_bim.py", "Start.cmd", "setup.ps1", ".python-version",
                        *[p for p in profile["product_files"] if p.endswith(".json")]]}
    registry = {"schema_version": "agent-version-registry.v2", "scope": scope,
                "fingerprint_provider": ["{python}", "-m", "src.agent.version_fingerprints"],
                "runtime_versions": {}, "domain_versions": {}, "aliases": {}, "product_projection": provenance}
    fingerprints = _fingerprints(destination, registry)
    for line in ("runtime", "domain"):
        record = {"source_commit": commit, "coverage": "automatic_directories",
                  "files": discover_files(destination, line, scope=scope),
                  "product_projection": provenance}
        if line == "domain":
            record.update(fingerprints)
        identity = product_version_id(line, record, scope)
        registry[f"{line}_versions"][identity] = record
        registry[f"current_{line}_version"] = identity
    return registry, provenance
