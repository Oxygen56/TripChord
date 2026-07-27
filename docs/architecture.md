# TripChord target architecture

## Decision boundary

TripChord is a planning and decision system, not an OTA and not a ticketing
agent. Booking remains with official or authorised supplier channels.

## Modules

1. **Trip intake** converts conversation and form edits into a typed `TripSpec`.
2. **Travel data gateway** normalises provider-specific POIs, routes, weather,
   transport, lodging, and user-imported quotes.
3. **Candidate builder** creates transport, lodging-zone, and activity options.
4. **Planner and optimiser** combine preference reasoning with deterministic
   temporal, spatial, and budget constraints.
5. **Verifier** checks hard constraints, data provenance, freshness, and soft
   preference coverage.
6. **Repair engine** applies targeted changes and emits a plan diff.
7. **Event engine** maps price, availability, weather, closure, delay, and user
   changes to affected plan nodes before local replanning.
8. **Evaluation lab** runs frozen replay scenarios, live canaries, ablations,
   post-training comparisons, latency, and cost measurements.

## State flow

```text
draft -> sourcing -> candidate_ready -> planning -> verifying
      -> repairing -> ready -> revalidating -> confirmed
      -> event_received -> impact_scoped -> replanning -> verifying
```

Every transition is persisted and idempotent. External calls have request IDs,
timeouts, bounded retries, circuit-breaker state, and replay fixtures.

## Why a modular monolith

The planning transaction shares rich state and is easier to test and replay in
one deployable unit. Boundaries are expressed as Python protocols and domain
events, allowing later extraction without premature distributed complexity.

