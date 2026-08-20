# Legacy v3 migration fixtures

These two JSON files are static captures of the pre-P0 registry serializer at
Git commit `8cba6bc`. They were produced by actually running that producer's
`LivePlanningJobRegistry` with `defer_start=False` and a stubborn operation,
then capturing the durable record while it was in the old cancellation or
deadline cleanup branch. The captures retain the complete serializer field
sets, including `legacy_isolated: false`, and the producer's branch-specific
revision/error values.

Only nondeterministic identity fields were normalized for a stable repository
fixture: the generated job ID, created/updated/deadline timestamps, and the
corresponding tenant/idempotency identifiers. The request digest and all
semantic producer fields are fixed synthetic values; no user, provider, or
secret data is included.

Tests copy these files into an owner-only temporary state path. They do not
load the historical Git object at runtime, so a clean clone has the same
legacy migration coverage.
