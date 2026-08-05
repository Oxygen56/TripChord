from __future__ import annotations

import hashlib
import json
import math
import random
from dataclasses import asdict, dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "benchmarks" / "scenarios" / "date-search-full-universe-v1.jsonl"
CALIBRATION_OUTPUT = (
    ROOT / "benchmarks" / "scenarios" / "date-search-calibration-v1.jsonl"
)
GENERATOR_VERSION = "date-search-full-universe-v1"


@dataclass(frozen=True)
class Condition:
    id: str
    coarse_noise_ratio: float
    platform_missing_probability: float
    exact_failure_probability: float


CONDITIONS: tuple[Condition, ...] = (
    Condition("low-noise-full-prior", 0.04, 0.00, 0.04),
    Condition("medium-noise-partial-prior", 0.12, 0.35, 0.08),
    Condition("high-noise-sparse-prior", 0.25, 0.65, 0.12),
    Condition("medium-noise-partial-prior-high-exact-failure", 0.12, 0.45, 0.45),
)
CALIBRATION_SEEDS: tuple[int, ...] = (2026080401, 2026080402, 2026080403, 2026080404)
TEST_SEEDS: tuple[int, ...] = (
    2026080411,
    2026080412,
    2026080413,
    2026080414,
    2026080415,
    2026080416,
    2026080417,
    2026080418,
)


def _stable_seed(seed: int, condition_id: str) -> int:
    digest = hashlib.sha256(f"{seed}:{condition_id}".encode()).digest()
    return int.from_bytes(digest[:8], "big")


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _make_scenario(
    *,
    split: str,
    seed: int,
    condition: Condition,
    night_counts: tuple[int, ...] = (5, 6, 7, 8),
    source_request_day_range: tuple[int, int] | None = None,
) -> dict[str, Any]:
    if len(night_counts) != 4 or tuple(sorted(night_counts)) != night_counts:
        raise ValueError("date-search universe requires four ordered night counts")
    rng = random.Random(_stable_seed(seed, condition.id))
    start = date(2026, 8, 1)
    destination_effect = rng.randint(-18_000, 18_000)
    price_wells = tuple(
        (rng.randrange(31), rng.randrange(4), rng.randint(35_000, 95_000))
        for _ in range(3)
    )
    records: list[dict[str, Any]] = []
    for departure_offset in range(31):
        departure = start + timedelta(days=departure_offset)
        for night_offset, night_count in enumerate(night_counts):
            pair_id = f"2026-08-{departure.day:02d}:{night_count}n"
            weekend = 22_000 if departure.weekday() in {4, 5, 6} else 0
            seasonal = int(42_000 * math.sin((departure_offset + 2) / 31 * math.tau))
            duration = night_offset * 31_000
            local_shape = int(9_000 * math.cos((departure_offset + night_count) / 7))
            well_discount = sum(
                max(
                    0,
                    depth
                    - 18_000
                    * (abs(departure_offset - well_day) + abs(night_offset - well_night)),
                )
                for well_day, well_night, depth in price_wells
            )
            exact_noise = rng.randint(-15_000, 15_000)
            exact_total = max(
                250_000,
                720_000
                + destination_effect
                + weekend
                + seasonal
                + duration
                + local_shape
                - well_discount
                + exact_noise,
            )
            exact_available = rng.random() >= condition.exact_failure_probability

            platform_prices: list[int] = []
            for _platform_index in range(3):
                if rng.random() < condition.platform_missing_probability:
                    continue
                platform_noise = rng.gauss(0, exact_total * condition.coarse_noise_ratio)
                platform_bias = rng.randint(-12_000, 12_000)
                platform_prices.append(max(0, round(exact_total + platform_noise + platform_bias)))
            platform_prices.sort()
            coarse_total = (
                platform_prices[len(platform_prices) // 2] if platform_prices else None
            )
            records.append(
                {
                    "id": pair_id,
                    "departure_date": departure.isoformat(),
                    "return_date": (departure + timedelta(days=night_count)).isoformat(),
                    "night_count": night_count,
                    "coarse_total_cents": coarse_total,
                    "platform_coverage_count": len(platform_prices),
                    "exact_total_cents": exact_total if exact_available else None,
                }
            )

    universe_contract: dict[str, Any] = {
        "departure_days": 31,
        "night_counts": list(night_counts),
        "pair_count": 124,
    }
    if source_request_day_range is not None:
        universe_contract.update(
            {
                "source_request_day_range": list(source_request_day_range),
                "day_to_night_rule": "n calendar travel days map to n-1 lodging nights",
            }
        )
    payload: dict[str, Any] = {
        "generator_version": GENERATOR_VERSION,
        "id": f"{split}:{condition.id}:{seed}",
        "split": split,
        "seed": seed,
        "condition": asdict(condition),
        "universe_contract": universe_contract,
        "records": records,
    }
    payload["scenario_sha256"] = hashlib.sha256(_canonical_json(payload).encode()).hexdigest()
    return payload


def generate_scenarios() -> list[dict[str, Any]]:
    scenarios: list[dict[str, Any]] = []
    for split, seeds in (("calibration", CALIBRATION_SEEDS), ("test", TEST_SEEDS)):
        for condition in CONDITIONS:
            for seed in seeds:
                scenarios.append(_make_scenario(split=split, seed=seed, condition=condition))
    return scenarios


def generate_calibration_scenarios() -> list[dict[str, Any]]:
    return [item for item in generate_scenarios() if item["split"] == "calibration"]


def write_scenarios(path: Path = OUTPUT) -> str:
    lines = tuple(_canonical_json(item) for item in generate_scenarios())
    content = "\n".join(lines) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    return hashlib.sha256(content.encode()).hexdigest()


def write_calibration_scenarios(path: Path = CALIBRATION_OUTPUT) -> str:
    lines = tuple(_canonical_json(item) for item in generate_calibration_scenarios())
    content = "\n".join(lines) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    return hashlib.sha256(content.encode()).hexdigest()


if __name__ == "__main__":
    print(f"full={write_scenarios()}")
    print(f"calibration={write_calibration_scenarios()}")
