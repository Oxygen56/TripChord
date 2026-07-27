# Phase 7 review — post-training data and policy reranking

Status: conditional pass

## Planned

- Convert reproducible planning, ablation, and recovery traces into SFT and DPO
  datasets without allowing related cities to leak across splits.
- Provide current, executable LoRA SFT and DPO launch paths.
- Train at least one lightweight learned component end to end and compare it
  against a simple held-out baseline.
- Keep all learned policies behind deterministic hard-constraint verification.

## Actual

- Generated 120 strict-JSON conversational SFT traces: 80 train, 20 validation,
  and 20 test.
- Generated 222 preference pairs: 146 train, 36 validation, and 40 test. Every
  rejected result carries either a hard-constraint failure from travel/budget
  ablation or a measured lower-utility reason.
- Split by destination group rather than individual row: groups 0–7 train,
  8–9 validation, and 10–11 test. No group crosses a split.
- Added TRL/PEFT LoRA SFT and DPO entrypoints, explicit model selection, dataset
  preflight validation, locked optional dependencies, and an 8192-token limit.
- Installed the locked training environment locally and instantiated
  `SFTConfig`, `DPOConfig`, and `LoraConfig`; both trainer signatures accepted
  `peft_config` under TRL 1.9.1 and PEFT 0.19.1.
- Trained a deterministic pairwise logistic policy model over 360 matched
  replan decisions. It chooses local repair or global regeneration from declared
  stability/quality weights and measured candidate properties.

## Held-out result

- Train: 96.25% Top-1 versus 68.75% for always-local.
- Validation: 96.67% versus 70.00%.
- Unseen-city test: 95.00% versus 71.67%.
- Test mean oracle regret: 0.0 under the synthetic weighted policy objective.
- Model SHA-256:
  `3fbc95f69529164865522d6adf62c95c5ea805d54b856f81d608849ddd69f272`.

## Deviations and findings

- The SFT/DPO infrastructure is real and structurally validated, but no base
  language model was selected or trained. Model license, Chinese capability,
  compute, and inference cost remain explicit experiment choices. Therefore no
  LLM quality lift is claimed.
- The reranker result is not evidence of human preference learning. Its labels
  come from a documented synthetic weighted objective. Its value is proving the
  training/evaluation/deployment seam and exposing the stability-quality tradeoff.
- Directly generating final itineraries with an LLM would weaken the safety
  contract. Learned components may interpret or rank, while the deterministic
  optimiser and Verifier retain authority over hard constraints.

## Decision

Conditional pass. The data, split, CPU-trained reranker, held-out comparison,
and current trainer interfaces are reproducible. The learned policy must now be
wired into the product behind the Verifier. LLM adapter gains remain an honest
open experiment rather than a resume claim.
