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
uv run python benchmarks/evaluate_planning.py
uv run python benchmarks/evaluate_repair.py
uv run python benchmarks/evaluate_events.py
uv run python -m benchmarks.evaluate_scale
uv run python -m benchmarks.evaluate_replanning_scale
uv run python -m benchmarks.evaluate_faults
```

`planning-scale-v1.jsonl` is generated once from seed `20260727` and checked in.
Regenerate it only when creating a new benchmark version:

```bash
uv run python benchmarks/generate_scale_scenarios.py
```

Scale metrics are synthetic replay measurements. They establish deterministic
constraint and recovery behaviour, not production traffic, preference quality,
or supplier-network latency.
