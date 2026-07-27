# Phase 1 review — travel data and offer truth

Status: conditional pass

## Planned

- Define provider-neutral travel and price contracts.
- Implement flight, lodging, POI, route, weather, replay, and user-snapshot paths.
- Make freshness, price state, source mode, comparison eligibility, and repricing explicit.
- Isolate provider failure instead of failing the entire search.

## Actual

- Added typed `SourceRecord`, `TravelOffer`, `Place`, `RouteLeg`, and
  `WeatherWindow` contracts.
- Added concurrent provider registry, structured failures, replay fixtures,
  user-confirmed quote import, and exact-context comparison grouping.
- Implemented Amadeus OAuth, flight search, and price confirmation adapters.
- Implemented Booking Demand v3.2 accommodation search and availability
  confirmation adapters.
- Implemented AMap geocoding, POI search, walking/driving/transit route parsing,
  and forecast parsing.
- Added API endpoints for offer search, repricing, quote import, places, routes,
  and weather.
- Phase checks after implementation: 15 Python tests passed; Ruff passed; mypy
  strict passed; web build and web test passed.

## Truth and access gate

- Official request/response contracts are tested with deterministic mock
  transports.
- No Amadeus production key, Booking partner credentials, or AMap Web Service
  key is currently configured. Production coverage and live-price claims remain
  unverified.
- Replay and sandbox values remain visibly non-live by domain validation.
- Provider production mode must be explicitly selected; it is never inferred
  merely from the presence of credentials.

## Deviations

- A generic `OfferSource` proved too price-specific once POI and weather were
  added. It was promoted to a shared `SourceRecord` while retaining a temporary
  alias for compatibility.
- The original plan implied railway API aggregation. This was rejected because
  no authorised public ticketing contract is available; user-confirmed rail
  quotes and official purchase handoff remain the supported route.

## Decision

Conditional pass. The code and replay gates are complete, so constraint and
planning work may continue. The production-provider gate stays open and must be
closed before any resume or README claim of live multi-source pricing.

