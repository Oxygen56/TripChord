from __future__ import annotations

from importlib.resources import files


def read_package_text(*parts: str) -> str:
    """Read immutable data shipped inside the ``tripchord`` wheel.

    ``importlib.resources`` keeps runtime data lookup independent of both the
    current working directory and the source-repository layout.
    """

    resource = files("tripchord")
    for part in parts:
        resource = resource.joinpath(part)
    return resource.read_text(encoding="utf-8")


def read_replay_offers() -> str:
    return read_package_text("data", "replay", "offers.json")


def read_replay_places() -> str:
    return read_package_text("data", "replay", "places.json")


def read_replan_policy() -> str:
    return read_package_text("data", "artifacts", "replan-policy.json")
