from __future__ import annotations

import re
from datetime import date
from decimal import Decimal

from pydantic import Field

from tripchord.domain.common import DomainModel, Money
from tripchord.domain.trip import Pace, TravelParty, TripSpec


class RequirementEvidence(DomainModel):
    field: str
    value: str
    matched_text: str
    start: int = Field(ge=0)
    end: int = Field(gt=0)


class TripSpecDraft(DomainModel):
    origin: str | None = None
    destination: str | None = None
    start_date: date | None = None
    end_date: date | None = None
    budget_cny: Decimal | None = Field(default=None, ge=0)
    adults: int = Field(default=1, ge=1, le=20)
    pace: Pace = Pace.BALANCED
    max_main_activities_per_day: int = Field(default=3, ge=1, le=8)
    interests: tuple[str, ...] = ()
    must_visit: tuple[str, ...] = ()
    avoid: tuple[str, ...] = ()


class RequirementParseResult(DomainModel):
    text: str
    draft: TripSpecDraft
    evidence: tuple[RequirementEvidence, ...]
    missing_fields: tuple[str, ...]
    clarifying_questions: tuple[str, ...]

    @property
    def complete(self) -> bool:
        return not self.missing_fields

    def to_spec(self) -> TripSpec:
        if not self.complete:
            missing = ", ".join(self.missing_fields)
            raise ValueError(f"cannot create TripSpec; missing fields: {missing}")
        assert self.draft.origin is not None
        assert self.draft.destination is not None
        assert self.draft.start_date is not None
        assert self.draft.end_date is not None
        return TripSpec(
            origin=self.draft.origin,
            destinations=(self.draft.destination,),
            start_date=self.draft.start_date,
            end_date=self.draft.end_date,
            party=TravelParty(adults=self.draft.adults),
            budget=(
                Money(amount=self.draft.budget_cny, currency="CNY")
                if self.draft.budget_cny is not None
                else None
            ),
            pace=self.draft.pace,
            max_main_activities_per_day=self.draft.max_main_activities_per_day,
            interests=self.draft.interests,
            must_visit=self.draft.must_visit,
            avoid=self.draft.avoid,
            notes=self.text,
        )


class ChineseRequirementParser:
    """Evidence-producing fallback parser for common Chinese trip requests.

    It intentionally asks for missing hard fields. An LLM extractor may enrich
    soft preferences later, but it cannot silently invent required values.
    """

    _interest_terms = (
        "历史",
        "博物馆",
        "建筑",
        "艺术",
        "自然",
        "徒步",
        "摄影",
        "亲子",
        "美食",
        "本地菜",
        "夜景",
        "购物",
    )

    def parse(self, text: str, *, default_year: int) -> RequirementParseResult:
        evidence: list[RequirementEvidence] = []
        values: dict[str, object] = {}

        route_match = re.search(
            r"从(?P<origin>[\u4e00-\u9fff]{2,8}?)(?:出发)?(?:去|到)"
            r"(?P<destination>[\u4e00-\u9fff]{2,8}?)(?=[，,。\s]|玩|旅|$)",
            text,
        )
        if route_match:
            values["origin"] = route_match.group("origin")
            values["destination"] = route_match.group("destination")
            evidence.extend(
                self._match_evidence(route_match, "origin", route_match.group("origin"), text),
            )
            evidence.extend(
                self._match_evidence(
                    route_match,
                    "destination",
                    route_match.group("destination"),
                    text,
                ),
            )

        date_match = re.search(
            r"(?:(?P<year>20\d{2})年)?(?P<start_month>\d{1,2})月(?P<start_day>\d{1,2})日?"
            r"(?:到|至|[-—~～])"
            r"(?:(?P<end_year>20\d{2})年)?(?P<end_month>\d{1,2})月(?P<end_day>\d{1,2})日?",
            text,
        )
        if date_match:
            start_year = int(date_match.group("year") or default_year)
            end_year = int(date_match.group("end_year") or start_year)
            start_date = date(
                start_year,
                int(date_match.group("start_month")),
                int(date_match.group("start_day")),
            )
            end_date = date(
                end_year,
                int(date_match.group("end_month")),
                int(date_match.group("end_day")),
            )
            values["start_date"] = start_date
            values["end_date"] = end_date
            evidence.append(self._evidence("start_date", str(start_date), date_match, text))
            evidence.append(self._evidence("end_date", str(end_date), date_match, text))

        budget_match = re.search(
            r"预算(?:大约|约|不超过|控制在)?\s*(?P<budget>\d+(?:\.\d+)?)\s*元",
            text,
        )
        if budget_match:
            values["budget_cny"] = Decimal(budget_match.group("budget"))
            evidence.append(
                self._evidence("budget_cny", budget_match.group("budget"), budget_match, text)
            )

        adults_match = re.search(r"(?P<adults>\d+)\s*(?:个)?(?:成人|人)(?!民)", text)
        if adults_match:
            values["adults"] = int(adults_match.group("adults"))
            evidence.append(
                self._evidence("adults", adults_match.group("adults"), adults_match, text)
            )

        max_match = re.search(r"每天(?:最多|不超过)\s*(?P<count>\d+)\s*个?(?:主要)?景点", text)
        if max_match:
            values["max_main_activities_per_day"] = int(max_match.group("count"))
            evidence.append(
                self._evidence(
                    "max_main_activities_per_day",
                    max_match.group("count"),
                    max_match,
                    text,
                )
            )

        if any(token in text for token in ("轻松", "悠闲", "不要太累", "慢节奏")):
            values["pace"] = Pace.RELAXED
        elif any(token in text for token in ("特种兵", "紧凑", "尽可能多", "高强度")):
            values["pace"] = Pace.INTENSIVE

        interests = tuple(term for term in self._interest_terms if term in text)
        if interests:
            values["interests"] = interests
        must_visit = self._named_list(text, ("必去", "一定要去"))
        avoid = self._named_list(text, ("不去", "避开"))
        if must_visit:
            values["must_visit"] = must_visit
        if avoid:
            values["avoid"] = avoid

        draft = TripSpecDraft.model_validate(values)
        required = {
            "origin": draft.origin,
            "destination": draft.destination,
            "start_date": draft.start_date,
            "end_date": draft.end_date,
        }
        missing = tuple(field for field, value in required.items() if value is None)
        questions = tuple(self._question(field) for field in missing)
        return RequirementParseResult(
            text=text,
            draft=draft,
            evidence=tuple(evidence),
            missing_fields=missing,
            clarifying_questions=questions,
        )

    def _named_list(self, text: str, prefixes: tuple[str, ...]) -> tuple[str, ...]:
        prefix = "|".join(map(re.escape, prefixes))
        matches = re.findall(
            rf"(?:{prefix})(?P<name>[\u4e00-\u9fffA-Za-z0-9·]+?)(?=[，,。；;\s]|$)",
            text,
        )
        return tuple(dict.fromkeys(item for item in matches if len(item) >= 2))

    def _match_evidence(
        self,
        match: re.Match[str],
        field: str,
        value: str,
        text: str,
    ) -> list[RequirementEvidence]:
        start, end = match.span(field)
        return [
            RequirementEvidence(
                field=field,
                value=value,
                matched_text=text[start:end],
                start=start,
                end=end,
            )
        ]

    def _evidence(
        self,
        field: str,
        value: str,
        match: re.Match[str],
        text: str,
    ) -> RequirementEvidence:
        start, end = match.span()
        return RequirementEvidence(
            field=field,
            value=value,
            matched_text=text[start:end],
            start=start,
            end=end,
        )

    def _question(self, field: str) -> str:
        return {
            "origin": "你从哪里出发？",
            "destination": "这次旅行的目的地是哪里？",
            "start_date": "计划哪天出发？",
            "end_date": "计划哪天结束？",
        }[field]
