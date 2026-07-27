# TripChord evaluation lab

Frozen replay scenarios are immutable once a phase result has been reported.
New failures are added as new scenarios; old expected outputs are changed only
with a documented benchmark correction.

Live canaries are stored separately because supplier inventory, weather, and
prices are volatile. Live results must never be compared with replay results as
if they came from the same distribution.

Run the current deterministic verifier baseline:

```bash
uv run python benchmarks/evaluate.py
```

