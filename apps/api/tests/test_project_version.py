"""Keep the public package and service version declarations in sync."""

import json
import tomllib
from pathlib import Path

from tripchord import __version__

ROOT = Path(__file__).resolve().parents[3]


def test_public_versions_match_project_metadata() -> None:
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    workspace_package = json.loads((ROOT / "package.json").read_text(encoding="utf-8"))
    web_package = json.loads((ROOT / "apps/web/package.json").read_text(encoding="utf-8"))
    node_lock = json.loads((ROOT / "package-lock.json").read_text(encoding="utf-8"))

    expected = pyproject["project"]["version"]
    assert expected == "2.0.0"
    assert __version__ == expected
    assert workspace_package["version"] == expected
    assert web_package["version"] == expected
    assert node_lock["version"] == expected
    assert node_lock["packages"][""]["version"] == expected
    assert node_lock["packages"]["apps/web"]["version"] == expected
