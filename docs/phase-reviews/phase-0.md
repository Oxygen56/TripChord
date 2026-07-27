# Phase 0 review — baseline and clean-room foundation

Status: passed

## Planned

- Confirm an independent product identity and package namespace.
- Pin the tutorial reference without copying its code.
- Reproduce the upstream static/build baseline.
- Establish typed domain contracts, automated tests, and a frozen benchmark.

## Actual

- Product name fixed as TripChord（旅弦）after exact GitHub, npm, and PyPI
  package-name checks found no collision.
- The clean-room repository uses Python 3.12, FastAPI, Pydantic, React 19,
  TypeScript 7, and Vite 8 with locked Python and npm dependency graphs.
- The upstream reference was pinned at
  `6c616938c521c89bc4b2bf001bf237d259f1726b`.
- Upstream Python sources passed `compileall`; its checked frontend build failed
  with nine TypeScript/module errors. Its installed dependency tree reported
  13 audit findings: 2 moderate, 10 high, and 1 critical.
- TripChord API tests: 7 passed.
- TripChord frozen verifier scenarios: 2/2 passed.
- Ruff: passed. mypy strict: passed.
- TripChord web TypeScript/Vite build: passed.
- TripChord web Vitest: 1 passed.
- TripChord full npm dependency audit: 0 findings.
- TripChord Python environment audit: no known third-party vulnerabilities;
  the local `tripchord` package is correctly skipped because it is not on PyPI.

## Deviations

- The initial idea of direct secondary development was replaced with a
  clean-room implementation because the upstream is CC BY-NC-SA 4.0 and the
  desired outcome is an independently packageable product.
- The first Python test run exposed an incorrect benchmark import path and the
  first Ruff run exposed six style/typing-quality findings. Both were fixed and
  the complete checks were rerun successfully.

## Decision

Pass. Continue to phase 1 with the current modular-monolith architecture and
truth-labelled source contracts. The upstream is retained only as a benchmark;
no upstream dependency or source code enters TripChord.
