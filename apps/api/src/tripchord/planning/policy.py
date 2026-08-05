from __future__ import annotations

import json
from enum import StrEnum
from pathlib import Path

from pydantic import Field

from tripchord.domain.common import DomainModel
from tripchord.package_data import read_replan_policy


class ReplanPreference(StrEnum):
    MINIMUM_CHANGE = "minimum_change"
    BALANCED = "balanced"
    QUALITY_FIRST = "quality_first"


class ReplanMode(StrEnum):
    LOCAL = "local"
    GLOBAL = "global"


class ReplanCandidateMetrics(DomainModel):
    mode: ReplanMode
    hard_valid: bool
    preservation_ratio: float = Field(ge=0, le=1)
    utility_retention: float = Field(ge=0)


class ReplanPolicyDecision(DomainModel):
    preference: ReplanPreference
    selected_mode: ReplanMode
    local_score: float
    global_score: float | None = None
    model_sha256: str
    fallback_reason: str | None = None


class ReplanPolicySelector:
    def __init__(
        self,
        *,
        feature_order: tuple[str, ...],
        weights: tuple[float, ...],
        profiles: dict[str, tuple[float, float]],
        model_sha256: str,
    ) -> None:
        if len(feature_order) != len(weights):
            raise ValueError("policy feature and weight counts differ")
        self._feature_order = feature_order
        self._weights = weights
        self._profiles = profiles
        self._model_sha256 = model_sha256

    @classmethod
    def from_path(cls, path: Path) -> ReplanPolicySelector:
        payload = path.read_text(encoding="utf-8")
        return cls.from_json(payload)

    @classmethod
    def from_package_data(cls) -> ReplanPolicySelector:
        return cls.from_json(read_replan_policy())

    @classmethod
    def from_json(cls, payload: str) -> ReplanPolicySelector:
        import hashlib

        model = json.loads(payload)
        return cls(
            feature_order=tuple(model["feature_order"]),
            weights=tuple(float(value) for value in model["weights"]),
            profiles={
                name: (float(values[0]), float(values[1]))
                for name, values in model["profiles"].items()
            },
            model_sha256=hashlib.sha256(payload.encode()).hexdigest(),
        )

    def select(
        self,
        preference: ReplanPreference,
        local: ReplanCandidateMetrics,
        global_candidate: ReplanCandidateMetrics | None,
    ) -> ReplanPolicyDecision:
        local_score = self._score(preference, local)
        if global_candidate is None or not global_candidate.hard_valid:
            return ReplanPolicyDecision(
                preference=preference,
                selected_mode=ReplanMode.LOCAL,
                local_score=local_score,
                model_sha256=self._model_sha256,
                fallback_reason="no verifier-approved global candidate",
            )
        if not local.hard_valid:
            return ReplanPolicyDecision(
                preference=preference,
                selected_mode=ReplanMode.GLOBAL,
                local_score=local_score,
                global_score=self._score(preference, global_candidate),
                model_sha256=self._model_sha256,
                fallback_reason="local candidate failed deterministic verification",
            )
        global_score = self._score(preference, global_candidate)
        selected = ReplanMode.LOCAL if local_score >= global_score else ReplanMode.GLOBAL
        return ReplanPolicyDecision(
            preference=preference,
            selected_mode=selected,
            local_score=local_score,
            global_score=global_score,
            model_sha256=self._model_sha256,
        )

    def _score(
        self,
        preference: ReplanPreference,
        candidate: ReplanCandidateMetrics,
    ) -> float:
        stability_weight, quality_weight = self._profiles[preference.value]
        is_local = float(candidate.mode == ReplanMode.LOCAL)
        values = {
            "weighted_preservation": stability_weight * candidate.preservation_ratio,
            "weighted_utility": quality_weight * candidate.utility_retention,
            "preservation": candidate.preservation_ratio,
            "utility": candidate.utility_retention,
            "is_local": is_local,
            "stability_weight_x_is_local": stability_weight * is_local,
            "bias": 1.0,
        }
        features = tuple(values[name] for name in self._feature_order)
        return sum(
            weight * feature for weight, feature in zip(self._weights, features, strict=True)
        )
