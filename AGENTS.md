# TripChord contributor guide

## Product contract

TripChord is an independent, clean-room implementation of a grounded leisure
travel planning system. Do not copy source code from HelloAgents or other
travel planners. Upstreams may be used only as documented comparison baselines.

Never label a sandbox, replay, cached, estimated, or user-imported price as a
live bookable price. Every external fact must carry provider, capture time, and
freshness metadata.

## Engineering rules

- Keep hard-constraint checking deterministic and testable.
- Use the LLM for preference interpretation, candidate reasoning, and
  explanations; do not delegate arithmetic or source-of-truth validation to it.
- Every repair must emit a plan diff and preserve unaffected items by default.
- Add or update a frozen benchmark whenever a planning failure is fixed.
- Resume and README claims must link to reproducible evidence.
- Python code is formatted and linted with Ruff and type-checked with mypy.
- Web code must pass TypeScript build and Vitest.

## Commands

- `uv sync --all-groups`
- `uv run pytest`
- `uv run ruff check .`
- `uv run mypy apps/api/src`
- `npm install`
- `npm run build`
- `npm test`

