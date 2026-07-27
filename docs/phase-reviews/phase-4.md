# Phase 4 review — event injection and local replanning

Status: passed

## Planned

- Model price, availability, weather, closure, transport-delay, and user-change
  events.
- Resolve direct targets from plan item, offer, or source references.
- Traverse only the downstream dependency subgraph.
- Preserve every unaffected item exactly and block unsafe changes to locked
  items.
- Reverify and repair the affected scope, with auditable preservation metrics.

## Actual

- Added temporal, travel, and booking dependency contracts plus a deterministic
  graph builder and transitive impact analyser.
- Added event-scoped replanning for price changes, delays, closures, weather,
  sold-out inventory, and changed requirements.
- Unaffected items are temporarily locked during verification and repair, then
  compared against the original plan before a result can become ready.
- Transport and lodging disruptions require a sourced replacement; the engine
  does not silently remove a trip anchor.
- Locked direct targets block automation. Unmatched and already-applied events
  return an auditable no-effect result.
- Applied event IDs travel with the plan lineage, preventing duplicate delay or
  price events from being processed twice.
- Added `/api/v1/plans/replan`, versioned plan diffs, overall preservation, and
  unaffected-item preservation metrics.

## Verification

- Python tests: 39 passed.
- Frozen event scenarios: 5/5 passed.
- Frozen event scenarios preserved 100% of unaffected items.
- Ruff: passed.
- mypy strict: passed.
- API tests cover both repair and event-replan entry points.

## Deviations and findings

- Treating an empty dependency tuple as “build a default graph” made it
  impossible to express an intentionally independent plan. `None` now means
  infer dependencies; an empty tuple explicitly means no edges.
- A first event model was replayable but not idempotent. Plan versions now carry
  applied event IDs so repeated provider notifications are no-ops.
- Removing a sold-out transport or hotel could make a superficially valid but
  unusable itinerary. Those item kinds now require a replacement with enough
  provenance to pass the existing verifier.

## Decision

Pass. Event recovery is a scoped planning capability rather than a full-plan
regeneration feature. The next phase will turn these APIs into a persistent,
end-to-end user workflow with version comparison and progress streaming.
