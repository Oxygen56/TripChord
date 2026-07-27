# Data source and price truth policy

## Allowed source modes

- `production`: authorised provider response from a production endpoint.
- `sandbox`: provider test environment; never presented as live.
- `replay`: immutable recorded fixture used for deterministic tests.
- `user_snapshot`: quote supplied by the user and explicitly confirmed.

## Price states

- `live_search`: fresh search result that can still change.
- `revalidated`: selected offer confirmed by the provider's price-check step.
- `booked`: transaction confirmation supplied by an authorised channel or user.
- `estimated`: calculated or historical value, never presented as bookable.

`sandbox`, `replay`, and `user_snapshot` are source modes, not guarantees of
bookability. A user snapshot can only become `booked` after user confirmation.

## Prohibited behaviour

- Do not automate logins, CAPTCHAs, payments, or purchases.
- Do not depend on undocumented private endpoints or bypass access controls.
- Do not claim all-platform or lowest-price coverage without measured evidence.
- Do not compare rates with different taxes, occupancy, room, baggage, meal,
  cancellation, membership, locale, or currency context as if equivalent.

