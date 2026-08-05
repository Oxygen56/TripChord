from __future__ import annotations

import hashlib
import json
from pathlib import Path

from tripchord.package_data import (
    read_replan_policy,
    read_replay_offers,
    read_replay_places,
)
from tripchord.planning.assembler import ReplayPlaceCatalog
from tripchord.planning.policy import ReplanPolicySelector
from tripchord.providers.replay import ReplayOfferProvider

ROOT = Path(__file__).resolve().parents[3]


def _digest(payload: str) -> str:
    canonical = json.dumps(
        json.loads(payload),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def test_packaged_runtime_data_matches_repository_sources() -> None:
    assert _digest(read_replay_offers()) == _digest(
        (ROOT / "data/replay/offers.json").read_text(encoding="utf-8")
    )
    assert _digest(read_replay_places()) == _digest(
        (ROOT / "data/replay/places.json").read_text(encoding="utf-8")
    )
    assert _digest(read_replan_policy()) == _digest(
        (ROOT / "training/artifacts/replan-policy.json").read_text(encoding="utf-8")
    )


def test_runtime_components_load_without_repository_paths() -> None:
    assert ReplayOfferProvider().name == "replay"
    assert ReplayPlaceCatalog().search("北京", ("历史",))
    decision = ReplanPolicySelector.from_package_data()
    assert decision is not None
