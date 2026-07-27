# Evidence and claim ledger

| Claim | Reproducible evidence | Allowed wording | Boundary |
|---|---|---|---|
| Hard-constraint planning | `benchmarks/results/phase-6-scale.json` | 120/120 frozen scenarios valid | Synthetic replay, not user trips |
| Utility versus greedy | Same result file | Mean utility +0.83% versus deterministic earliest-fit | Greedy was also 100% valid |
| Travel/budget mechanisms matter | Same result file | No-travel 0% valid; no-budget 30.83% valid under original constraints | Scenario-specific ablation |
| Dynamic recovery | Same result file | 120/120 closure recovery; 100% unaffected-item preservation | Frozen activity-closure events |
| Local/global trade-off | Same result file | 83.38% versus 17.28% preservation; 82.30% versus 91.66% utility retention | Neither policy dominates both objectives |
| Provider fault isolation | Same result file | 100/100 concurrent replay queries retained healthy partial results and classified timeout/failure | In-memory fault injection, not network QPS |
| Post-training data | `training/data/manifest.json` | 120 SFT traces and 222 DPO pairs, split by destination group | Deterministic synthetic labels |
| Policy reranker | `benchmarks/results/phase-7-post-training.json` | 95% held-out Top-1 versus 71.67% always-local | Synthetic weighted oracle, not human preference |
| LLM fine-tuning | `training/train_sft.py`, `training/train_dpo.py` | Current TRL/PEFT LoRA launch paths and data preflight implemented | No adapter quality gain claimed |
| Full-stack reliability | API tests, migrations, Compose, CI | Tenant isolation, idempotency, leases, recovery, Redis limiting, request metrics | Static token auth is a deployment reference, not an OIDC product |
| External suppliers | Provider contract tests and `docs/providers.md` | Amadeus/Booking/AMap adapters implemented and contract-tested | Production credentials and coverage not verified |

## Never claim

- “Prices are the cheapest across all travel apps.” TripChord can normalise the
  sources it is authorised to query; it cannot prove universal lowest price.
- “12306 real-time inventory” without a documented authorised API.
- “Fine-tuning improved planning quality” before a concrete base model,
  completed run, cost report, and unseen-city comparison.
- “Production QPS” from replay or injected in-memory providers.
