from __future__ import annotations

from pathlib import Path

from scripts import generate_sbom


def test_display_path_keeps_repo_paths_relative() -> None:
    path = generate_sbom.ROOT / "benchmarks" / "results" / "sbom.cyclonedx.json"

    assert generate_sbom._display_path(path) == "benchmarks/results/sbom.cyclonedx.json"


def test_display_path_accepts_ci_scratch_paths(tmp_path: Path) -> None:
    path = tmp_path / "tripchord-sbom" / "sbom.cyclonedx.json"

    assert generate_sbom._display_path(path) == str(path.resolve())
