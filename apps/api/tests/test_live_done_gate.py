from __future__ import annotations

from types import SimpleNamespace

from tripchord.agents.live_done_gate import _round_trip_quote_errors
from tripchord.providers.browser_bridge import BrowserProvider


def test_tongcheng_round_trip_quote_uses_staged_confirmed_party_contract() -> None:
    quote = SimpleNamespace(
        provider=BrowserProvider.TONGCHENG,
        taxes_included=True,
        details={
            "workflow_kind": "staged_outbound_return",
            "combination_status": "round_trip_complete",
            "journey_price_scope": "round_trip",
            "price_finality": "final_for_combination",
            "party_availability_status": "confirmed_for_party",
            "combination_id": "tongcheng-visible-combination",
            "price_basis_evidence": "¥8058含税总价",
            "tax_evidence": "¥8058含税总价",
            "selection_evidence": "已选去程并查看返程完整含税总价",
            "outbound_departure_at": "2026-08-09T00:10:00+08:00",
            "outbound_arrival_at": "2026-08-09T15:00:00+05:00",
            "return_departure_at": "2026-08-13T11:15:00+05:00",
            "return_arrival_at": "2026-08-14T15:55:00+08:00",
        },
    )

    assert _round_trip_quote_errors(quote) == ()
