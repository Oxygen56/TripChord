# Upstream comparison baseline

## Reference

- Repository: `datawhalechina/hello-agents`
- Commit: `6c616938c521c89bc4b2bf001bf237d259f1726b`
- Subproject: `code/chapter13/helloagents-trip-planner`
- License: CC BY-NC-SA 4.0

TripChord is a clean-room implementation. No upstream source is copied into
this repository. The reference is used to measure how an educational agent
demo differs from a grounded, tested planning product.

## Pre-implementation observations

- Four sequential agents produce a trip plan through a tutorial workflow.
- POI, route, weather, and geocoding parsing contains unfinished paths.
- Fallback content includes placeholder locations and coordinates.
- The subproject contains no automated tests.
- The frontend did not pass its checked build in the prior audit.

Exact reproduction output belongs in `docs/phase-reviews/phase-0.md`; claims
must be updated from command evidence rather than this initial observation.

