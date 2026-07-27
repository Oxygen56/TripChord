# Final-form implementation roadmap

| Phase | Outcome | Evidence gate |
|---|---|---|
| 0 | Clean-room repo, upstream baseline, frozen evaluation skeleton | Builds and tests reproduce from a pinned manifest |
| 1 | Travel data gateway and normalised offers | Provenance, freshness, and contract tests pass |
| 2 | Typed trip constraints and candidate generation | Parsing and candidate coverage benchmark |
| 3 | Planner plus deterministic optimiser | Temporal, spatial, and budget feasibility |
| 4 | Verifier–Repair loop | Hard-constraint lift without regression |
| 5 | Event injection and local replanning | Recovery and unaffected-plan preservation |
| 6 | Complete planning workspace | End-to-end product and recovery tests |
| 7 | Frozen benchmark, live canary, ablations | Reproducible metrics and failure taxonomy |
| 8 | SFT, preference optimisation, reranking | Held-out improvement with cost accounting |
| 9 | Reliability, deployment, docs, evidence-backed resume | CI, observability, demo, and claim ledger |

Implementation status: all local product phases are complete. External supplier
production verification, a completed LLM adapter experiment, remote CI results,
and a clean-network container build remain separate evidence gates rather than
unfinished hidden scope.

Each phase produces a run manifest, metrics, failure cases, and a phase review.
The only review outcomes are pass, conditional pass, rework, or plan change.
