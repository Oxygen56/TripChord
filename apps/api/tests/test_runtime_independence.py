from __future__ import annotations

import ast
import json
import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
FORBIDDEN_RUNTIME_PACKAGES = {"anthropic", "chatgpt", "codex", "openai"}


def _normalized(name: str) -> str:
    return name.casefold().replace("_", "-")


def _node_package_name(package_path: str) -> str | None:
    marker = "node_modules/"
    if marker not in package_path:
        return None
    tail = package_path.rsplit(marker, 1)[1]
    parts = tail.split("/")
    if tail.startswith("@") and len(parts) >= 2:
        return f"{parts[0]}/{parts[1]}"
    return parts[0] if parts else None


def test_python_and_node_dependency_graph_has_no_codex_chatgpt_or_provider_sdk() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    direct = {
        _normalized(re.match(r"[A-Za-z0-9_.-]+", item).group())
        for item in project["project"]["dependencies"]
    }
    lock = tomllib.loads((ROOT / "uv.lock").read_text(encoding="utf-8"))
    locked = {_normalized(item["name"]) for item in lock["package"]}
    node_lock = json.loads((ROOT / "package-lock.json").read_text(encoding="utf-8"))
    node = {
        _normalized(name)
        for package_path in node_lock.get("packages", {})
        if (name := _node_package_name(package_path)) is not None
    }

    assert direct.isdisjoint(FORBIDDEN_RUNTIME_PACKAGES)
    assert locked.isdisjoint(FORBIDDEN_RUNTIME_PACKAGES)
    assert node.isdisjoint(FORBIDDEN_RUNTIME_PACKAGES)


def test_runtime_source_has_no_provider_sdk_or_codex_chatgpt_import() -> None:
    imported_roots: set[str] = set()
    for path in (ROOT / "apps/api/src/tripchord").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported_roots.update(alias.name.split(".", 1)[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported_roots.add(node.module.split(".", 1)[0])
            elif (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "import_module"
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)
            ):
                imported_roots.add(node.args[0].value.split(".", 1)[0])

    assert {_normalized(item) for item in imported_roots}.isdisjoint(
        FORBIDDEN_RUNTIME_PACKAGES
    )
