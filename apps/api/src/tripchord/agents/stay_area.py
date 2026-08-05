from __future__ import annotations

import unicodedata
from enum import StrEnum

from pydantic import Field

from tripchord.domain.common import DomainModel


class StayAreaSearchProfileSource(StrEnum):
    SYSTEM_DERIVED_GOLDEN = "system_derived_golden"


class StayAreaSearchProfile(DomainModel):
    gateway_destination: str = Field(min_length=1)
    destination_island_lodging_search_term: str = Field(min_length=1)
    airport_island_lodging_search_term: str = Field(min_length=1)
    source: StayAreaSearchProfileSource
    assumption_zh: str = Field(min_length=1)


def system_stay_area_search_profile(
    destination: str,
) -> StayAreaSearchProfile | None:
    """Return the explicit, editable Golden profile for the Malé gateway."""

    normalized = "".join(
        character
        for character in unicodedata.normalize("NFKD", destination)
        if not unicodedata.combining(character)
    )
    normalized = "".join(character for character in normalized.casefold() if character.isalnum())
    if normalized not in {"马累", "male", "mle"}:
        return None
    return StayAreaSearchProfile(
        gateway_destination=destination,
        destination_island_lodging_search_term="Maafushi",
        airport_island_lodging_search_term="Hulhumalé",
        source=StayAreaSearchProfileSource.SYSTEM_DERIVED_GOLDEN,
        assumption_zh=(
            "系统生成的可比较自由行场景，不是用户原话，可改：马累/MLE 作为航班"
            "门户，整段及中段住宿搜索 Maafushi，首晚及末晚住宿搜索 Hulhumalé。"
        ),
    )
