"""Generate a CycloneDX SBOM and build-provenance manifest (v0.9).

Produces ``benchmarks/results/sbom.cyclonedx.json`` (CycloneDX 1.5) and
``benchmarks/results/build-provenance.json`` from the two frozen lockfiles:

* ``uv.lock`` (Python, parsed with the stdlib ``tomllib``)
* ``package-lock.json`` (npm, parsed with the stdlib ``json``)

No third-party tool is required, so the generator runs in CI with a plain
``uv run``.  Local editable packages (workspace members without a registry
source) and git dependencies are excluded from the component inventory; the
inventory is exactly the frozen registry packages the lockfiles resolve to.

Subcommands:

* ``generate`` — write both artifacts (wall-clock timestamp, like real SBOM
  tools).  A committed copy documents the dependency state at the time it was
  last refreshed; CI regenerates a build-time copy (with the then-current
  commit SHA) into a scratch directory for provenance.
* ``check`` — regenerate the inventory and the lockfile digests in memory and
  compare them against the committed artifacts; exit 0 when the component
  inventory, the metadata component and the ``source_digests`` are all
  current.  ``commit_sha`` in a committed artifact can never equal the commit
  it lives in (a file cannot reference its own commit), so it is informational
  only; drift detection is bound to the lockfile content hashes and the
  component inventory instead.  Wall-clock fields (``metadata.timestamp``,
  ``serialNumber``, ``generated_at``, ``sbom_sha256``) are also intentionally
  not part of the drift check.

``check`` is wired into CI so a dependency change without an SBOM refresh
fails the build instead of silently drifting.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

try:
    import tomllib
except ImportError:  # pragma: no cover - Python < 3.11
    tomllib = None  # type: ignore[assignment]

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT_DIR = ROOT / "benchmarks" / "results"
SBOM_FILENAME = "sbom.cyclonedx.json"
PROVENANCE_FILENAME = "build-provenance.json"
CYCLONEDX_SPEC_VERSION = "1.5"
PROVENANCE_SCHEMA = "tripchord-build-provenance-v1"
APPLICATION_NAME = "tripchord"


def _application_version() -> str:
    if tomllib is None:  # pragma: no cover
        raise SbomError("tomllib is required (Python >= 3.11)")
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    version = project.get("project", {}).get("version")
    if not isinstance(version, str) or not version:
        raise SbomError("pyproject.toml does not contain a valid project version")
    return version


class SbomError(RuntimeError):
    pass


def _git_head() -> tuple[str, str]:
    """Return (commit sha, committed_at iso) of the working tree HEAD."""
    try:
        sha = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        committed_at = subprocess.run(
            ["git", "show", "-s", "--format=%cI", "HEAD"],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError) as exc:
        raise SbomError(f"cannot resolve git HEAD for provenance: {exc}") from exc
    if not sha or not committed_at:
        raise SbomError("git HEAD resolved to an empty commit")
    return sha, committed_at


def _load_uv_packages() -> list[dict[str, Any]]:
    if tomllib is None:  # pragma: no cover
        raise SbomError("tomllib is required (Python >= 3.11)")
    lock_path = ROOT / "uv.lock"
    if not lock_path.exists():
        raise SbomError(f"missing {lock_path.relative_to(ROOT)}")
    with lock_path.open("rb") as handle:
        data = tomllib.load(handle)
    components: list[dict[str, Any]] = []
    for package in data.get("package", []):
        source = package.get("source", {})
        if "registry" not in source:
            # Local editable / workspace package: it is the application itself
            # or a path dependency, not a third-party component.
            continue
        name = package["name"]
        version = package.get("version")
        if not version:
            continue
        digest = ""
        sdist = package.get("sdist", {}) or {}
        wheels = package.get("wheels") or []
        if sdist.get("hash"):
            digest = sdist["hash"]
        else:
            for wheel in wheels:
                if wheel.get("hash"):
                    digest = wheel["hash"]
                    break
        components.append(
            {
                "name": name,
                "version": version,
                "digest": digest,
                "ecosystem": "pypi",
            }
        )
    return components


def _load_npm_packages() -> list[dict[str, Any]]:
    lock_path = ROOT / "package-lock.json"
    if not lock_path.exists():
        raise SbomError(f"missing {lock_path.relative_to(ROOT)}")
    with lock_path.open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    components: list[dict[str, Any]] = []
    for key, entry in data.get("packages", {}).items():
        version = entry.get("version")
        if not version or key in {"", "apps/web"}:
            # Root workspace and workspace-members are the application, not
            # third-party components.
            continue
        marker = "node_modules/"
        if marker not in key:
            continue
        name = key[key.index(marker) + len(marker) :]
        integrity = entry.get("integrity", "")
        components.append(
            {
                "name": name,
                "version": version,
                "digest": integrity,
                "ecosystem": "npm",
            }
        )
    return components


def _purl(ecosystem: str, name: str, version: str) -> str:
    if ecosystem == "pypi":
        return f"pkg:pypi/{name.lower().replace('_', '-')}@{version}"
    # npm scoped names keep the slash but percent-encode the leading @.
    encoded = name
    if name.startswith("@"):
        encoded = "%40" + name[1:]
    return f"pkg:npm/{encoded}@{version}"


def _hash_entry(ecosystem: str, digest: str) -> dict[str, str] | None:
    """Normalise a lockfile digest into a CycloneDX ``hashes`` entry."""
    if not digest:
        return None
    if ecosystem == "pypi" and digest.startswith("sha256:"):
        return {"alg": "SHA-256", "content": digest[len("sha256:") :]}
    if ecosystem == "npm" and digest.startswith("sha512-"):
        return {"alg": "SHA-512", "content": digest[len("sha512-") :]}
    return None


def _build_inventory() -> list[dict[str, Any]]:
    components: list[dict[str, Any]] = []
    for raw in [*_load_uv_packages(), *_load_npm_packages()]:
        name = raw["name"]
        version = raw["version"]
        ecosystem = raw["ecosystem"]
        bom_ref = f"{ecosystem}:{name}@{version}"
        component: dict[str, Any] = {
            "type": "library",
            "bom-ref": bom_ref,
            "name": name,
            "version": version,
            "purl": _purl(ecosystem, name, version),
        }
        hashes = _hash_entry(ecosystem, raw["digest"])
        if hashes is not None:
            component["hashes"] = [hashes]
        components.append(component)
    components.sort(key=lambda item: item["purl"])
    return components


def _generate_sbom(
    inventory: list[dict[str, Any]],
    *,
    commit_sha: str,
    committed_at: str,
    generated_at: str,
    serial: str,
) -> dict[str, Any]:
    return {
        "bomFormat": "CycloneDX",
        "specVersion": CYCLONEDX_SPEC_VERSION,
        "serialNumber": serial,
        "version": 1,
        "metadata": {
            "timestamp": generated_at,
            "component": {
                "type": "application",
                "bom-ref": APPLICATION_NAME,
                "name": APPLICATION_NAME,
                "version": _application_version(),
            },
            "properties": [
                {"name": "tripchord:commit_sha", "value": commit_sha},
                {"name": "tripchord:committed_at", "value": committed_at},
            ],
        },
        "components": inventory,
    }


def _source_digests() -> dict[str, str]:
    digests: dict[str, str] = {}
    for filename in ("uv.lock", "package-lock.json"):
        path = ROOT / filename
        if not path.exists():
            raise SbomError(f"missing {filename}")
        digests[filename] = hashlib.sha256(path.read_bytes()).hexdigest()
    return digests


def _generate_provenance(
    *,
    commit_sha: str,
    committed_at: str,
    generated_at: str,
    sbom_text: str,
    python_count: int,
    npm_count: int,
) -> dict[str, Any]:
    return {
        "schema": PROVENANCE_SCHEMA,
        "commit_sha": commit_sha,
        "committed_at": committed_at,
        "generated_at": generated_at,
        "sbom_file": SBOM_FILENAME,
        "sbom_sha256": hashlib.sha256(sbom_text.encode("utf-8")).hexdigest(),
        "component_counts": {
            "pypi": python_count,
            "npm": npm_count,
            "total": python_count + npm_count,
        },
        "source_digests": _source_digests(),
        "sources": ["uv.lock", "package-lock.json"],
    }


def _inventory_counts(inventory: list[dict[str, Any]]) -> tuple[int, int]:
    python_count = sum(1 for item in inventory if item["purl"].startswith("pkg:pypi/"))
    npm_count = len(inventory) - python_count
    return python_count, npm_count


def _render(value: dict[str, Any]) -> str:
    return json.dumps(value, indent=2, ensure_ascii=False, sort_keys=True) + "\n"


def _display_path(path: Path) -> str:
    """Render repository paths compactly without rejecting external outputs."""
    resolved = path.resolve()
    try:
        return str(resolved.relative_to(ROOT))
    except ValueError:
        # CI intentionally writes build-time artifacts to /tmp.  Keep the
        # absolute path in that case so the message remains unambiguous.
        return str(resolved)


def generate(output_dir: Path) -> None:
    commit_sha, committed_at = _git_head()
    generated_at = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    serial = f"urn:uuid:{uuid.uuid4()}"
    inventory = _build_inventory()
    python_count, npm_count = _inventory_counts(inventory)
    sbom_text = _render(
        _generate_sbom(
            inventory,
            commit_sha=commit_sha,
            committed_at=committed_at,
            generated_at=generated_at,
            serial=serial,
        )
    )
    provenance_text = _render(
        _generate_provenance(
            commit_sha=commit_sha,
            committed_at=committed_at,
            generated_at=generated_at,
            sbom_text=sbom_text,
            python_count=python_count,
            npm_count=npm_count,
        )
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    sbom_path = output_dir / SBOM_FILENAME
    provenance_path = output_dir / PROVENANCE_FILENAME
    sbom_path.write_text(sbom_text, encoding="utf-8")
    provenance_path.write_text(provenance_text, encoding="utf-8")
    print(
        f"wrote {_display_path(sbom_path)} "
        f"({python_count} pypi + {npm_count} npm components) and "
        f"{_display_path(provenance_path)}"
    )


def _component_inventory(value: dict[str, Any]) -> list[dict[str, Any]]:
    return value.get("components", [])


def _metadata_component(value: dict[str, Any]) -> dict[str, Any] | None:
    return (value.get("metadata") or {}).get("component")


def check(output_dir: Path) -> int:
    inventory = _build_inventory()
    python_count, npm_count = _inventory_counts(inventory)
    source_digests = _source_digests()

    sbom_path = output_dir / SBOM_FILENAME
    provenance_path = output_dir / PROVENANCE_FILENAME
    if not sbom_path.exists() or not provenance_path.exists():
        print(
            "error: missing committed artifacts; run `uv run python "
            "scripts/generate_sbom.py generate` first",
            file=sys.stderr,
        )
        return 1

    committed_sbom = json.loads(sbom_path.read_text(encoding="utf-8"))
    committed_provenance = json.loads(provenance_path.read_text(encoding="utf-8"))

    errors: list[str] = []
    if _component_inventory(committed_sbom) != inventory:
        errors.append("component inventory differs from the frozen lockfiles")
    if _metadata_component(committed_sbom) != {
        "type": "application",
        "bom-ref": APPLICATION_NAME,
        "name": APPLICATION_NAME,
        "version": _application_version(),
    }:
        errors.append("metadata component differs from the application identity")
    if committed_provenance.get("source_digests") != source_digests:
        errors.append("provenance source_digests differ from the frozen lockfiles")
    if committed_provenance.get("component_counts") != {
        "pypi": python_count,
        "npm": npm_count,
        "total": python_count + npm_count,
    }:
        errors.append("provenance component_counts differ from the frozen lockfiles")

    if errors:
        print("sbom/provenance drift detected:", file=sys.stderr)
        for message in errors:
            print(f"  - {message}", file=sys.stderr)
        print(
            "fix: `uv run python scripts/generate_sbom.py generate` and commit "
            "the regenerated artifacts",
            file=sys.stderr,
        )
        return 1
    print("sbom/provenance are current")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command",
        choices=("generate", "check"),
        help="generate the SBOM/provenance artifacts, or check them for drift",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help=f"directory for the artifacts (default: {DEFAULT_OUTPUT_DIR.relative_to(ROOT)})",
    )
    args = parser.parse_args()
    try:
        if args.command == "generate":
            generate(args.output_dir)
            return 0
        return check(args.output_dir)
    except SbomError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
