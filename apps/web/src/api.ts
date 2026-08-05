export type Money = { amount: string; currency: string };

export type PlanItem = {
  id: string;
  kind: "transport" | "lodging" | "activity" | "meal" | "buffer";
  title: string;
  starts_at: string;
  ends_at: string;
  location_name: string | null;
  cost: Money | null;
  source_refs: string[];
  locked: boolean;
};

export type PlanVersion = {
  id: string;
  trip_id: string;
  version: number;
  status: string;
  items: PlanItem[];
  parent_version_id: string | null;
  applied_event_ids: string[];
};

export type TripSpec = {
  origin: string;
  destinations: string[];
  start_date: string;
  end_date: string;
  budget?: Money;
  max_main_activities_per_day: number;
  interests: string[];
  must_visit: string[];
};

export type Workspace = {
  id: string;
  title: string;
  spec: TripSpec;
  plans: PlanVersion[];
  events: Array<{ event: { id: string; kind: string }; result: unknown }>;
};

export type Job = {
  id: string;
  workspace_id: string;
  status: "queued" | "running" | "succeeded" | "failed";
  stage: string;
  progress: number;
  error: string | null;
};

export type Offer = {
  id: string;
  kind: "flight" | "rail" | "lodging" | "activity";
  title: string;
  price_state: string;
  price: { total: Money };
  source: { provider: string; mode: string; captured_at: string };
  terms: { refundable: boolean | null; cancellation_summary: string | null };
};

export type PlanDiff = {
  added_item_ids: string[];
  removed_item_ids: string[];
  changed_items: Array<{ item_id: string; changed_fields: string[] }>;
};

export type ReplanResult = {
  status: "ready" | "blocked" | "no_effect";
  message: string;
  overall_preservation_ratio: number;
  unaffected_preservation_ratio: number;
  diff: PlanDiff;
  preference: "minimum_change" | "balanced" | "quality_first";
  selected_mode: "local" | "global";
  candidates: Array<{
    mode: "local" | "global";
    hard_valid: boolean;
    preservation_ratio: number;
    utility_retention: number;
  }>;
};

export type PreferenceMode = "required" | "weighted" | "forbidden" | "indifferent";

export function normalizeBreakfastWeight(mode: PreferenceMode, weight: number): number {
  if (mode === "indifferent") return 0;
  if (mode === "required" || mode === "forbidden") return 1;
  return Math.min(1, Math.max(0, weight));
}

export type AgentRun = {
  decision: {
    state: "accept" | "accept_with_exception" | "replan_or_block";
    summary: string;
    verifier_violations: string[];
    evidence_refs: string[];
    requires_user_confirmation: boolean;
  };
  selected_candidate_id: string | null;
  scheduler: {
    wall_time_seconds: number;
    max_parallel_tasks: number;
    succeeded: boolean;
    graph: { tasks: Array<{ id: string; role: string; dependencies: string[] }> };
    trace: Array<{
      sequence: number;
      kind: string;
      task_id: string | null;
      agent_role: string | null;
    }>;
  };
  evidence: Array<{ id: string; topic: string; source: string; confidence: number }>;
};

export type LiveCoverageMode = "strict" | "degraded";
export type LiveRunPurpose = "exploration_selection" | "final_publication";
export type LiveFinalizationState = "exploration_sealed" | "final_published";
export type LiveDecisionState = "accept" | "reject_and_replan" | "human_block";
export type LiveBrowserProvider = "ctrip" | "qunar" | "tongcheng";
export const LIVE_BROWSER_PROVIDERS: readonly LiveBrowserProvider[] = [
  "ctrip",
  "qunar",
  "tongcheng",
];
export type LiveProvider = LiveBrowserProvider | "icom-public-transfer";
export type LiveVertical = "flight" | "lodging";
export type JsonValue =
  | null
  | boolean
  | number
  | string
  | JsonValue[]
  | { [key: string]: JsonValue };

export type LivePackageIntent = {
  trip_id: string;
  origin: string;
  destination: string;
  start_date: string;
  end_date: string;
  adults: number;
  rooms: number;
  currency: string;
  budget_cents: number | null;
  require_checked_baggage: boolean | null;
  require_breakfast: boolean | null;
  breakfast_preference_mode?: PreferenceMode | null;
  breakfast_preference_weight?: number | null;
  minimum_arrival_to_boat_minutes?: number;
  minimum_airport_buffer_minutes?: number;
};

export type LiveBrowserSearchQuery = {
  origin: string;
  destination: string;
  start_date: string;
  end_date: string;
  adults: number;
  rooms: number;
  currency: string;
  origin_code?: string | null;
  destination_code?: string | null;
  search_url?: string | null;
  options: Record<string, JsonValue>;
};

export type LiveQuote = {
  id: string;
  provider: string;
  currency: string;
  total_for_party_cents: number;
  taxes_and_fees_included: boolean | null;
  captured_at: string;
  expires_at: string;
  availability: "available" | "sold_out";
  evidence_refs: string[];
  [key: string]: JsonValue;
};

export type LivePackageCandidate = {
  id: string;
  trip_id: string;
  version: number;
  parent_candidate_id: string | null;
  kind: "continuous_island" | "split_airport_island";
  flight: LiveQuote;
  lodgings: LiveQuote[];
  transfers: LiveQuote[];
  declared_total_cents: number;
  currency: string;
  applied_event_ids: string[];
};

export type LiveViolation = {
  code: string;
  severity: "error" | "warning";
  message: string;
  component_ids: string[];
  details: Record<string, string | number | boolean>;
};

export type LiveDecision = {
  state: LiveDecisionState;
  summary: string;
  violation_codes: string[];
  evidence_refs: string[];
};

export type LivePackageResult = {
  initial_candidate: LivePackageCandidate;
  final_candidate: LivePackageCandidate;
  decisions: LiveDecision[];
  final_decision: LiveDecision;
  initial_violations: LiveViolation[];
  final_violations: LiveViolation[];
  diff: {
    before_candidate_id: string;
    after_candidate_id: string;
    removed_component_ids: string[];
    added_component_ids: string[];
    preserved_component_ids: string[];
    preservation_ratio: number | string;
  } | null;
  preservation_ratio: number | string;
  budget: {
    currency: string;
    adults: number;
    flight_cents: number;
    lodging_cents: number;
    transfer_cents: number;
    total_cents: number;
    confirmed_subtotal_cents: number;
    supplemental_published_base_fares: Array<{
      currency: string;
      adults: number;
      total_for_party_cents: number;
      price_contract_ids: string[];
      transfer_ids: string[];
    }>;
    budget_compliance_fully_verified: boolean;
    is_all_in_total: boolean;
    formula: string;
  };
  evidence_refs: string[];
  preference_applications: Array<{
    key: string;
    mode: PreferenceMode | null;
    weight: number | null;
    state: "applied" | "not_applied" | "hard_constraint" | "not_requested";
    reason: string;
    comparable_candidate_count: number;
    selected_candidate_id: string;
    selected_breakfast_coverage: number | string | null;
    selected_breakfast_evidence_complete: boolean | null;
  }>;
};

export type AgenticStageTrace = {
  task_id: string;
  role: string;
  model_called: boolean;
  provider: string | null;
  model: string | null;
  token_usage: number;
  logical_request_count: number;
  primary_http_attempt_count: number;
  fallback_http_attempt_count: number;
  http_attempt_count: number;
  total_latency_seconds: number;
  estimated_cost_usd: number;
  context_token_budget: number;
  context_used_tokens: number;
  tool_observation_tokens: number;
  truncated_tool_observations: number;
  tool_names: string[];
  fallback_used: boolean;
  failure: string | null;
};

export type AgenticRunSummary = {
  enabled: boolean;
  required: boolean;
  stage_count: number;
  model_stage_count: number;
  logical_request_count: number;
  primary_http_attempt_count: number;
  fallback_http_attempt_count: number;
  http_attempt_count: number;
  total_latency_seconds: number;
  total_estimated_cost_usd: number;
  /** Deprecated backend compatibility alias for logical_request_count. */
  model_call_count: number;
  total_token_usage: number;
  providers: string[];
  models: string[];
  stages: AgenticStageTrace[];
  safety_boundary: string;
  metrics_boundary: string;
};

export type LivePlatformSearchCoverage = {
  provider: LiveBrowserProvider;
  selected_stay_plan_id: string | null;
  completed_search_verticals: LiveVertical[];
  successful_verticals: LiveVertical[];
  failed_verticals: LiveVertical[];
  successful_source_ids: string[];
  terminal_outcome_source_ids: string[];
  usable_quote_source_ids: string[];
  failed_source_ids: string[];
  failure_reasons: string[];
  flight_outcome_state: string | null;
  complete: boolean;
};

export type LivePackageAgentRun = {
  run_purpose: LiveRunPurpose;
  finalization_state: LiveFinalizationState;
  deferred_stage_ids: string[];
  exploration_seal_passed: boolean;
  mode: LiveCoverageMode;
  intent: LivePackageIntent;
  search_query: LiveBrowserSearchQuery;
  decision: LiveDecision;
  claim_boundary: string;
  all_platforms_complete: boolean;
  coverage: LivePlatformSearchCoverage[];
  public_transfer_coverage: {
    provider: "icom-public-transfer";
    requested: boolean;
    enabled: boolean;
    expected_source_ids: string[];
    successful_source_ids: string[];
    failed_source_ids: string[];
    usable_option_count: number;
    failure_reasons: string[];
    complete: boolean;
    price_boundary: string;
  } | null;
  inventory: {
    flights: LiveQuote[];
    lodgings: LiveQuote[];
    transfers: LiveQuote[];
  };
  normalization_results: Array<{
    provider: string;
    kind: LiveVertical;
    status: "usable" | "unavailable" | "rejected";
    quote: LiveQuote | null;
    transfers: LiveQuote[];
    issues: Array<{
      code: string;
      message: string;
      field: string | null;
      scope: string;
    }>;
  }>;
  package: LivePackageResult | null;
  scheduler: {
    graph: {
      tasks: Array<{
        id: string;
        role: string;
        goal: string;
        dependencies: string[];
      }>;
    };
    results: Array<{
      task_id: string;
      agent_role: string;
      success: boolean;
      summary: string;
      output: Record<string, JsonValue>;
      failure_class: string | null;
    }>;
    trace: Array<{
      sequence: number;
      kind: string;
      task_id: string | null;
      agent_role: string | null;
      occurred_at: string;
      details: Record<string, JsonValue>;
    }>;
    wall_time_seconds: number;
    max_parallel_tasks: number;
    succeeded: boolean;
  };
  source_task_ids: string[];
  public_transfer_task_ids: string[];
  agentic: AgenticRunSummary;
  orchestrator_proposal_block_reason?: string | null;
  explanation_grounding_block_reason?: string | null;
  explanation?: {
    summary: string;
    why_selected: string[];
    tradeoffs: string[];
    uncertainties: string[];
    next_user_actions: string[];
  } | null;
  memory_candidates?: {
    summary: string;
    candidates: Array<{
      key: string;
      value: JsonValue;
      scope: "trip" | "user";
      confidence: number | string;
      source_evidence_refs: string[];
      requires_user_confirmation: boolean;
    }>;
  } | null;
};

export type LivePlanResponse = {
  run_id: string;
  expires_at: string;
  run: LivePackageAgentRun;
};

export type LiveMonitorState = "active" | "stopped" | "completed" | "failed";

export type LiveMonitorCheck = {
  sequence: number;
  checked_at: string;
  target_component_id: string;
  event_id: string;
  applied_disposition: string | null;
  decision_state: LiveDecisionState;
  package_changed: boolean;
  summary: string;
};

export type LiveMonitorStatus = {
  id: string;
  run_id: string;
  state: LiveMonitorState;
  interval_seconds: number;
  max_checks: number;
  timeout_seconds: number;
  check_count: number;
  next_check_at: string | null;
  created_at: string;
  updated_at: string;
  last_check: LiveMonitorCheck | null;
  last_error: string | null;
  boundary: string;
};

export type LiveMonitorResponse = {
  monitor: LiveMonitorStatus;
};

export type PackageRequestState = "ready" | "human_block";

export type FlexibleTravelWindow = {
  origin: string;
  destination: string;
  earliest_departure: string;
  latest_departure: string;
  min_nights: number;
  max_nights: number;
  max_pairs: number;
  adults: number;
  rooms: number;
  currency: string;
};

export type PackageRequirementInterpretation = {
  state: PackageRequestState;
  window: FlexibleTravelWindow | null;
  intent_template: {
    trip_id: string;
    origin: string;
    destination: string;
    adults: number;
    rooms: number;
    currency: string;
    budget_cents: number | null;
    require_checked_baggage: boolean | null;
    require_breakfast: boolean | null;
    breakfast_preference_mode: PreferenceMode | null;
    breakfast_preference_weight: number | null;
    minimum_arrival_to_boat_minutes: number;
    minimum_airport_buffer_minutes: number;
  } | null;
  preferences: {
    rules: Array<{
      key: string;
      mode: PreferenceMode;
      weight: number | string;
      expected: JsonValue;
      source: string;
      reason: string;
    }>;
  };
  facts: Array<{
    field: string;
    value: JsonValue;
    source: string;
    evidence_text: string;
    explicit: boolean;
  }>;
  unresolved: Array<{
    field: string;
    reason: string;
    critical: boolean;
    model_proposal: JsonValue;
  }>;
  conflicts: Array<{
    field: string;
    deterministic_value: JsonValue;
    model_value: JsonValue;
    reason: string;
  }>;
  claim_boundary: string;
};

export type FlexibleDatePair = {
  id: string;
  rank: number;
  departure_date: string;
  return_date: string;
  night_count: number;
  source: "fused_fare_hint" | "stratified_sample";
  audit_reason: string;
};

export type FlexibleDateExploration = {
  mode:
    | "full_calendar_top_k"
    | "full_universe_no_complete_prior"
    | "sampled_not_exhaustive";
  sampled_not_exhaustive: boolean;
  universe_size: number;
  candidates: FlexibleDatePair[];
  missing_platforms: LiveBrowserProvider[];
  stale_platforms: LiveBrowserProvider[];
  ignored_hint_count: number;
  search_metrics: {
    universe_size: number;
    coarse_window_pair_count: number;
    prior_observed_pair_count: number;
    prior_coverage: number | string;
    shortlist_pair_count: number;
    shortlist_coverage: number | string;
    exact_search_budget_pairs: number;
    exact_search_coverage: number | string;
    recall_at_k: number | string | null;
    price_regret_cents: number | null;
    metric_status: "full_window_evaluable" | "partial_prior_only";
    evaluation_note: string;
  };
  warnings: string[];
};

export type FlexibleQueryPlan = {
  selected_pair_ids: string[];
  omitted_pair_ids: string[];
  total_task_count: number;
  task_count_by_platform: Record<string, number>;
  sampled_not_exhaustive: boolean;
  query_hash: string;
  warnings: string[];
};

export type QueryStrategyProposal = {
  summary: string;
  selected_pair_ids: string[];
  selection_reasons: string[];
  stop_condition: string;
  query_budget_pairs: number;
  uncertainty_flags: string[];
};

export type AdaptiveRefinementDecision = {
  round: number;
  selected_pair_id: string | null;
  remaining_budget_pairs: number;
  incumbent_total_cents: number | null;
  priority_score: number | string | null;
  stopped_early: boolean;
  reason: string;
};

export type FlexiblePairExecution = {
  date_pair: FlexibleDatePair;
  state: "completed" | "failed";
  run: LivePackageAgentRun | null;
  failure_class: string | null;
  failure_message: string | null;
};

export type FlexibleRankedOption = {
  rank: number;
  date_pair_id: string;
  departure_date: string;
  return_date: string;
  decision_state: LiveDecisionState;
  recommendable: boolean;
  total_budget_cents: number | null;
  evidence_completeness: number | string;
  all_platforms_complete: boolean;
  final_candidate_id: string | null;
};

export type StayAreaSearchProfile = {
  gateway_destination: string;
  destination_island_lodging_search_term: string;
  airport_island_lodging_search_term: string;
  source: "system_derived_golden";
  assumption_zh: string;
};

export type FlexibleLiveAgentRun = {
  requested_window: FlexibleTravelWindow;
  effective_window: FlexibleTravelWindow;
  exploration: FlexibleDateExploration;
  query_plan: FlexibleQueryPlan;
  pair_runs: FlexiblePairExecution[];
  ranked_options: FlexibleRankedOption[];
  refinement_trace: AdaptiveRefinementDecision[];
  recommended_option_ids: string[];
  final_decision: LiveDecision;
  query_strategy: QueryStrategyProposal | null;
  query_agentic: AgenticRunSummary;
  stay_area_search_profile?: StayAreaSearchProfile | null;
  sampled_not_exhaustive: boolean;
  claim_boundary: string;
};

export type LiveFlexiblePairRunHandle = {
  date_pair_id: string;
  run_id: string;
  expires_at: string;
};

export type LiveFlexibleFromTextResponse = {
  interpretation: PackageRequirementInterpretation;
  run: FlexibleLiveAgentRun | null;
  cached_pair_runs: LiveFlexiblePairRunHandle[];
  model_enhancement_enabled: boolean;
  execution_boundary: string;
};

export type LiveFlexibleFromTextInput = {
  requirement: {
    text: string;
    trip_id?: string;
    breakfast_mode?: PreferenceMode | null;
    breakfast_weight?: number | null;
  };
  coverage_mode: LiveCoverageMode;
  timeout_seconds: number;
  total_timeout_seconds: number;
  max_pairs: number;
};

export type LivePlanningJobState =
  | "queued"
  | "running"
  | "succeeded"
  | "failed"
  | "cancelled";

export type LivePlanningJobSnapshot = {
  id: string;
  state: LivePlanningJobState;
  stage: string;
  progress: number;
  cancellation_requested: boolean;
  revision: number;
  result: LiveFlexibleFromTextResponse | null;
  error: string | null;
  created_at: string;
  updated_at: string;
  expires_at: string | null;
  boundary: string;
};

export type StartLivePlanningJobResponse = {
  job: LivePlanningJobSnapshot;
  status_url: string;
  events_url: string;
  replayed: boolean;
};

export type BreakfastPreferenceApplication = {
  state:
    | "ranked"
    | "not_ranked"
    | "hard_constraint"
    | "not_requested"
    | "unconfirmed";
  mode: PreferenceMode | null;
  weight: number | null;
  reason: string;
};

export function getBreakfastPreferenceApplication(
  response: LiveFlexibleFromTextResponse,
): BreakfastPreferenceApplication {
  const rule = response.interpretation.preferences.rules.find(
    (candidate) => candidate.key === "hotel_breakfast",
  );
  const unresolved = response.interpretation.unresolved.find(
    (item) => item.field === "preference_application:hotel_breakfast",
  );
  const parsedWeight = rule ? Number(rule.weight) : Number.NaN;
  const weight =
    Number.isFinite(parsedWeight) && parsedWeight >= 0 && parsedWeight <= 1
      ? parsedWeight
      : null;

  if (unresolved) {
    return {
      state: "not_ranked",
      mode: rule?.mode ?? null,
      weight,
      reason: unresolved.reason,
    };
  }
  if (!rule || weight === null) {
    return {
      state: "unconfirmed",
      mode: rule?.mode ?? null,
      weight,
      reason: "后端响应没有给出可核对的 hotel_breakfast 模式与 0–1 权重。",
    };
  }
  if (rule.mode === "required" || rule.mode === "forbidden") {
    const expected = rule.mode === "required";
    if (response.interpretation.intent_template?.require_breakfast !== expected) {
      return {
        state: "unconfirmed",
        mode: rule.mode,
        weight,
        reason: "后端返回了早餐规则，但可执行意图中的早餐硬约束没有与之对应。",
      };
    }
    return {
      state: "hard_constraint",
      mode: rule.mode,
      weight,
      reason: "后端已将该模式解释为硬约束；规范权重不是软排序得分。",
    };
  }
  if (rule.mode === "indifferent") {
    return {
      state: "not_requested",
      mode: rule.mode,
      weight,
      reason: "后端解释为无要求，权重为 0，不参与方案排序。",
    };
  }
  const runtimeApplications =
    response.run?.pair_runs.flatMap(
      (pair) =>
        pair.run?.package?.preference_applications.filter(
          (application) => application.key === "hotel_breakfast",
        ) ?? [],
    ) ?? [];
  const applied = runtimeApplications.find(
    (application) => application.state === "applied",
  );
  if (applied) {
    return {
      state: "ranked",
      mode: rule.mode,
      weight,
      reason: applied.reason,
    };
  }
  const notApplied = runtimeApplications.find(
    (application) => application.state === "not_applied",
  );
  if (notApplied) {
    return {
      state: "not_ranked",
      mode: rule.mode,
      weight,
      reason: notApplied.reason,
    };
  }
  return {
    state: "not_ranked",
    mode: rule.mode,
    weight,
    reason: "后端尚未返回实时整包的早餐偏好应用证据，不能声称权重已进入排序。",
  };
}

export type ResolvedFlexibleOption = {
  option: FlexibleRankedOption;
  pair: FlexiblePairExecution;
  handle: LiveFlexiblePairRunHandle | null;
};

export type LivePackageEvent = {
  id: string;
  kind: "price_changed" | "sold_out";
  target_component_id: string;
  affected_provider: LiveProvider;
};

export type EventDisposition =
  | "no_change"
  | "refresh"
  | "local_repair"
  | "global_replan"
  | "human_block";

export type EventDiagnosisProposal = {
  summary: string;
  recommended_disposition: EventDisposition;
  affected_component_ids: string[];
  dependencies_to_refresh: string[];
  evidence_gaps: string[];
  confidence: number;
};

export type OfferEventResolution = {
  disposition: EventDisposition;
  verified_change: boolean;
  reason: string;
  replacement_component_id: string | null;
  cascade_component_ids: string[];
  candidate_pool_expansion_required: boolean;
};

export type LiveEventReplanRun = {
  event: LivePackageEvent;
  event_resolution: OfferEventResolution | null;
  event_diagnosis: EventDiagnosisProposal | null;
  applied_disposition: EventDisposition | null;
  agentic: AgenticRunSummary;
  decision: LiveDecision;
  claim_boundary: string;
  inventory: LivePackageAgentRun["inventory"];
  normalization_results: LivePackageAgentRun["normalization_results"];
  package: LivePackageResult | null;
  global_run: LivePackageAgentRun | null;
  scheduler: LivePackageAgentRun["scheduler"];
  requeried_providers: LiveProvider[];
  source_task_ids: string[];
};

export type LiveEventReplanResponse = {
  run_id: string;
  expires_at: string;
  run: LiveEventReplanRun;
};

export type LiveProviderCoveragePresentation = {
  capability_label: string;
  completed_source_count: number;
  expected_source_count: number;
  usable_quote_count: number;
};

export type AgenticMetricsPresentation = {
  attempt_evidence_label: string;
  latency_label: string;
  estimated_cost_label: string;
  scripted_only: boolean;
  includes_scripted: boolean;
};

function uniqueStrings(values: string[]): string[] {
  return [...new Set(values)];
}

export function getAgenticMetricsPresentation(
  summary: AgenticRunSummary,
): AgenticMetricsPresentation {
  const providers = uniqueStrings([
    ...summary.providers,
    ...summary.stages.flatMap((stage) => (stage.provider ? [stage.provider] : [])),
  ]).map((provider) => provider.toLowerCase());
  const includesScripted = providers.includes("scripted");
  const scriptedOnly = providers.length > 0 && providers.every((provider) => provider === "scripted");
  const attemptEvidenceLabel = scriptedOnly
    ? `${summary.http_attempt_count} 次 client attempt（Scripted 离线回放，不是 HTTP 请求）`
    : includesScripted
      ? `${summary.http_attempt_count} 次 HTTP/client attempt（含 Scripted 离线回放）`
      : `${summary.http_attempt_count} 次 HTTP attempt`;
  return {
    attempt_evidence_label: attemptEvidenceLabel,
    latency_label: `${summary.total_latency_seconds.toFixed(3)} 秒累计请求墙钟时间`,
    estimated_cost_label: `已知 usage 估算成本 US$${summary.total_estimated_cost_usd.toFixed(6)}`,
    scripted_only: scriptedOnly,
    includes_scripted: includesScripted,
  };
}

function combineAgenticRunSummaries(
  previous: AgenticRunSummary,
  next: AgenticRunSummary,
): AgenticRunSummary {
  const stages = [...previous.stages, ...next.stages];
  const logicalRequestCount = stages.reduce(
    (total, stage) => total + stage.logical_request_count,
    0,
  );
  return {
    enabled: previous.enabled || next.enabled,
    required: previous.required || next.required,
    stage_count: stages.length,
    model_stage_count: stages.filter((stage) => stage.logical_request_count > 0).length,
    logical_request_count: logicalRequestCount,
    primary_http_attempt_count: stages.reduce(
      (total, stage) => total + stage.primary_http_attempt_count,
      0,
    ),
    fallback_http_attempt_count: stages.reduce(
      (total, stage) => total + stage.fallback_http_attempt_count,
      0,
    ),
    http_attempt_count: stages.reduce(
      (total, stage) => total + stage.http_attempt_count,
      0,
    ),
    total_latency_seconds: stages.reduce(
      (total, stage) => total + stage.total_latency_seconds,
      0,
    ),
    total_estimated_cost_usd: stages.reduce(
      (total, stage) => total + stage.estimated_cost_usd,
      0,
    ),
    model_call_count: logicalRequestCount,
    total_token_usage: stages.reduce((total, stage) => total + stage.token_usage, 0),
    providers: uniqueStrings([...previous.providers, ...next.providers]).sort(),
    models: uniqueStrings([...previous.models, ...next.models]).sort(),
    stages,
    safety_boundary: next.safety_boundary || previous.safety_boundary,
    metrics_boundary: next.metrics_boundary || previous.metrics_boundary,
  };
}

export function summarizeLiveProviderCoverage(
  run: LivePackageAgentRun,
  coverage: LivePlatformSearchCoverage,
): LiveProviderCoveragePresentation {
  const scheduledSourceIds = uniqueStrings(
    run.source_task_ids.filter((taskId) =>
      taskId.startsWith(`source-${coverage.provider}-`),
    ),
  );
  const expectedSourceIds =
    scheduledSourceIds.length > 0
      ? scheduledSourceIds
      : uniqueStrings([
          ...coverage.terminal_outcome_source_ids,
          ...coverage.failed_source_ids,
        ]);
  const lodgingSegmentCount = expectedSourceIds.filter((taskId) =>
    taskId.includes("-lodging-"),
  ).length;
  return {
    capability_label:
      lodgingSegmentCount > 0
        ? `机票 + ${lodgingSegmentCount} 个住宿分段`
        : "仅机票（当前能力矩阵）",
    completed_source_count: uniqueStrings(coverage.terminal_outcome_source_ids).length,
    expected_source_count: expectedSourceIds.length,
    usable_quote_count: uniqueStrings(coverage.usable_quote_source_ids).length,
  };
}

export function advanceLiveRunAfterEvent(
  previous: LivePackageAgentRun,
  replanned: LiveEventReplanRun,
): LivePackageAgentRun {
  if (replanned.global_run) return replanned.global_run;

  const affectedPublicTransfer =
    replanned.event.affected_provider === "icom-public-transfer";
  const sourceTaskIds = affectedPublicTransfer
    ? previous.source_task_ids
    : [...previous.source_task_ids, ...replanned.source_task_ids].slice(-128);
  const publicTransferTaskIds = affectedPublicTransfer
    ? [...previous.public_transfer_task_ids, ...replanned.source_task_ids].slice(-128)
    : previous.public_transfer_task_ids;

  return {
    ...previous,
    decision: replanned.decision,
    claim_boundary: replanned.claim_boundary,
    inventory: replanned.inventory,
    normalization_results: [
      ...previous.normalization_results,
      ...replanned.normalization_results,
    ].slice(-256),
    package: replanned.package ?? previous.package,
    source_task_ids: sourceTaskIds,
    public_transfer_task_ids: publicTransferTaskIds,
    agentic: combineAgenticRunSummaries(previous.agentic, replanned.agentic),
    explanation: null,
    memory_candidates: null,
  };
}

export type LiveBridgeHealth = {
  available: boolean;
  status: "ok" | "unavailable";
  scope: string | null;
  message: string;
  checked_at: string;
};

export function requireLiveBridgeAvailability(
  health: LiveBridgeHealth,
): asserts health is LiveBridgeHealth & { available: true; status: "ok" } {
  if (!health.available || health.status !== "ok") {
    throw new Error(health.message || "本地只读浏览器桥不可用");
  }
}

type StartPlanningResponse = {
  workspace: Workspace;
  job: Job;
  data_mode: string;
  candidate_count: number;
};

function browserSessionStorage(): Storage | null {
  return typeof window === "undefined" ? null : window.sessionStorage;
}

let apiCredential = browserSessionStorage()?.getItem("tripchord-api-key") ?? "";

export function setApiCredential(credential: string): void {
  apiCredential = credential.trim();
  const storage = browserSessionStorage();
  if (!storage) return;
  if (apiCredential) storage.setItem("tripchord-api-key", apiCredential);
  else storage.removeItem("tripchord-api-key");
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, {
    ...init,
    headers: {
      "Content-Type": "application/json",
      ...(apiCredential ? { Authorization: `Bearer ${apiCredential}` } : {}),
      ...init?.headers,
    },
  });
  if (!response.ok) {
    const body = (await response.json().catch(() => null)) as { detail?: string } | null;
    throw new Error(body?.detail ?? `请求失败 (${response.status})`);
  }
  return response.json() as Promise<T>;
}

export function startPlanning(spec: TripSpec): Promise<StartPlanningResponse> {
  return request("/api/v1/trips/plan", {
    method: "POST",
    body: JSON.stringify({ spec }),
  });
}

export function runAgentPlanning(
  spec: TripSpec,
  breakfast: { mode: PreferenceMode; weight: number },
): Promise<{ mode: string; claim_boundary: string; run: AgentRun }> {
  return request("/api/v1/agents/replay-plan", {
    method: "POST",
    body: JSON.stringify({
      spec,
      preferences: {
        rules:
          breakfast.mode === "indifferent"
            ? []
            : [
                {
                  key: "hotel_breakfast",
                  mode: breakfast.mode,
                  weight: breakfast.weight,
                  expected: true,
                  source: "explicit_current_trip",
                  reason: "用户在当前行程中显式设置",
                },
              ],
      },
    }),
  });
}

export async function checkLiveBridgeHealth(bridgeToken: string): Promise<LiveBridgeHealth> {
  const checkedAt = new Date().toISOString();
  if (bridgeToken.trim().length < 32) {
    return {
      available: false,
      status: "unavailable",
      scope: null,
      message: "尚未提供有效的本地配对令牌；不会在未授权状态下探测浏览器桥。",
      checked_at: checkedAt,
    };
  }
  try {
    const response = await request<{
      status?: string;
      companions?: Array<{
        providers?: string[];
        is_fresh?: boolean;
      }>;
    }>(
      "/browser-bridge/v1/companions/status",
      {
        headers: { "X-TripChord-Bridge-Token": bridgeToken.trim() },
        signal: AbortSignal.timeout(5_000),
      },
    );
    const readyCompanion = (response.companions ?? []).some(
      (companion) =>
        companion.is_fresh === true &&
        LIVE_BROWSER_PROVIDERS.every((provider) =>
          (companion.providers ?? []).includes(provider),
        ),
    );
    if (response.status !== "connected" || !readyCompanion) {
      return {
        available: false,
        status: "unavailable",
        scope: "local-read-only-browser",
        message: "本地桥可达，但没有 45 秒内宣告携程、去哪儿、同程的 Chrome Companion；实时查询已阻止。",
        checked_at: checkedAt,
      };
    }
    return {
      available: true,
      status: "ok",
      scope: "local-read-only-browser",
      message: "本地只读桥与三平台 Chrome Companion 心跳已就绪；这仍不代表本轮核价已成功。",
      checked_at: checkedAt,
    };
  } catch (caught) {
    return {
      available: false,
      status: "unavailable",
      scope: null,
      message:
        caught instanceof Error
          ? `实时入口未接通：${caught.message}`
          : "实时入口未接通：无法访问本地浏览器桥。",
      checked_at: checkedAt,
    };
  }
}

export function runLivePackagePlanning(input: {
  intent: LivePackageIntent;
  search_query: LiveBrowserSearchQuery;
  coverage_mode: LiveCoverageMode;
  timeout_seconds: number;
}): Promise<LivePlanResponse> {
  return request("/api/v1/agents/live-plan", {
    method: "POST",
    body: JSON.stringify(input),
  });
}

export function runLiveFlexiblePlanningFromText(
  input: LiveFlexibleFromTextInput,
): Promise<LiveFlexibleFromTextResponse> {
  return request("/api/v1/agents/live-flexible-plan-from-text", {
    method: "POST",
    body: JSON.stringify(input),
  });
}

export function startLiveFlexiblePlanningFromTextJob(
  input: LiveFlexibleFromTextInput,
  idempotencyKey?: string,
): Promise<StartLivePlanningJobResponse> {
  return request("/api/v1/agents/live-flexible-plan-from-text/jobs", {
    method: "POST",
    headers: idempotencyKey ? { "Idempotency-Key": idempotencyKey } : undefined,
    body: JSON.stringify(input),
  });
}

export function getLiveFlexiblePlanningJob(
  jobId: string,
): Promise<LivePlanningJobSnapshot> {
  return request(
    `/api/v1/agents/live-flexible-plan-from-text/jobs/${encodeURIComponent(jobId)}`,
  );
}

export function cancelLiveFlexiblePlanningJob(
  jobId: string,
): Promise<LivePlanningJobSnapshot> {
  return request(
    `/api/v1/agents/live-flexible-plan-from-text/jobs/${encodeURIComponent(jobId)}`,
    { method: "DELETE" },
  );
}

export function subscribeToLiveFlexiblePlanningJob(
  jobId: string,
  onJob: (job: LivePlanningJobSnapshot) => void,
  onError: (message: string) => void,
): () => void {
  let active = true;
  let timer: ReturnType<typeof setTimeout> | undefined;
  const terminalStates = new Set<LivePlanningJobState>([
    "succeeded",
    "failed",
    "cancelled",
  ]);
  const poll = async () => {
    try {
      const job = await getLiveFlexiblePlanningJob(jobId);
      if (!active) return;
      onJob(job);
      if (!terminalStates.has(job.state)) timer = setTimeout(poll, 500);
    } catch (caught) {
      if (!active) return;
      onError(
        caught instanceof Error
          ? caught.message
          : "实时规划进度查询中断，请稍后重试",
      );
    }
  };
  void poll();
  return () => {
    active = false;
    if (timer) clearTimeout(timer);
  };
}

export function resolveFlexibleOption(
  response: LiveFlexibleFromTextResponse,
  requestedDatePairId?: string,
): ResolvedFlexibleOption | null {
  if (!response.run) return null;
  const preferredId =
    requestedDatePairId ??
    response.run.recommended_option_ids[0] ??
    response.run.ranked_options[0]?.date_pair_id;
  const option = response.run.ranked_options.find(
    (candidate) => candidate.date_pair_id === preferredId,
  );
  const pair = response.run.pair_runs.find(
    (candidate) => candidate.date_pair.id === preferredId,
  );
  if (!option || !pair) return null;
  return {
    option,
    pair,
    handle:
      response.cached_pair_runs.find(
        (candidate) => candidate.date_pair_id === preferredId,
      ) ?? null,
  };
}

export function replanLivePackage(
  runId: string,
  input: {
    event: LivePackageEvent;
    timeout_seconds: number;
  },
): Promise<LiveEventReplanResponse> {
  return request(`/api/v1/agents/live-plans/${encodeURIComponent(runId)}/events/replan`, {
    method: "POST",
    body: JSON.stringify(input),
  });
}

export function getLivePackage(runId: string): Promise<LivePlanResponse> {
  return request(`/api/v1/agents/live-plans/${encodeURIComponent(runId)}`);
}

export function startLiveMonitor(
  runId: string,
  input: {
    interval_seconds: number;
    max_checks: number;
    timeout_seconds: number;
  },
): Promise<LiveMonitorResponse> {
  return request(`/api/v1/agents/live-plans/${encodeURIComponent(runId)}/monitor`, {
    method: "POST",
    body: JSON.stringify(input),
  });
}

export function getLiveMonitor(monitorId: string): Promise<LiveMonitorResponse> {
  return request(`/api/v1/agents/live-monitors/${encodeURIComponent(monitorId)}`);
}

export function checkLiveMonitorNow(monitorId: string): Promise<LiveMonitorResponse> {
  return request(
    `/api/v1/agents/live-monitors/${encodeURIComponent(monitorId)}/check-now`,
    { method: "POST" },
  );
}

export function stopLiveMonitor(monitorId: string): Promise<LiveMonitorResponse> {
  return request(`/api/v1/agents/live-monitors/${encodeURIComponent(monitorId)}`, {
    method: "DELETE",
  });
}

export function confirmAgentPreferenceMemory(input: {
  key: string;
  value: JsonValue;
  source_evidence_refs: string[];
}): Promise<{ record: { id: string; version: number }; boundary: string }> {
  return request("/api/v1/agents/memory/preferences/confirm", {
    method: "POST",
    body: JSON.stringify(input),
  });
}

export function revokeAgentPreferenceMemory(
  recordId: string,
): Promise<{ record_id: string; revoked: boolean; boundary: string }> {
  return request(`/api/v1/agents/memory/${encodeURIComponent(recordId)}`, {
    method: "DELETE",
  });
}

export function loadWorkspace(workspaceId: string): Promise<Workspace> {
  return request(`/api/v1/workspaces/${workspaceId}`);
}

export function comparePlans(
  workspaceId: string,
  fromVersion: number,
  toVersion: number,
): Promise<PlanDiff> {
  return request(
    `/api/v1/workspaces/${workspaceId}/plans/${fromVersion}/diff/${toVersion}`,
  );
}

export async function searchOffers(spec: TripSpec): Promise<Offer[]> {
  const common = {
    origin: spec.origin,
    destination: spec.destinations[0],
    start_date: spec.start_date,
    end_date: spec.end_date,
  };
  const [flights, lodging] = await Promise.all([
    request<{ offers: Offer[] }>("/api/v1/offers/search", {
      method: "POST",
      body: JSON.stringify({ ...common, kind: "flight" }),
    }),
    request<{ offers: Offer[] }>("/api/v1/offers/search", {
      method: "POST",
      body: JSON.stringify({ ...common, kind: "lodging" }),
    }),
  ]);
  return [...flights.offers, ...lodging.offers];
}

export function replanWorkspace(
  workspaceId: string,
  input: {
    targetId: string;
    kind: string;
    preference: "minimum_change" | "balanced" | "quality_first";
    payload?: Record<string, string | number>;
  },
): Promise<{ result: ReplanResult; workspace: Workspace }> {
  return request(`/api/v1/workspaces/${workspaceId}/events/replan`, {
    method: "POST",
    body: JSON.stringify({
      preference: input.preference,
      event: {
        id: `ui-${input.kind}-${crypto.randomUUID()}`,
        trip_id: workspaceId,
        kind: input.kind,
        occurred_at: new Date().toISOString(),
        target_refs: [input.targetId],
        payload: input.payload ?? {},
      },
    }),
  });
}

export function subscribeToJob(
  workspaceId: string,
  jobId: string,
  onJob: (job: Job) => void,
  onError: (message: string) => void,
): () => void {
  let active = true;
  let timer: ReturnType<typeof setTimeout> | undefined;
  const poll = async () => {
    try {
      const job = await request<Job>(
        `/api/v1/workspaces/${workspaceId}/jobs/${jobId}`,
      );
      if (!active) return;
      onJob(job);
      if (job.status !== "succeeded" && job.status !== "failed") {
        timer = setTimeout(poll, 300);
      }
    } catch (caught) {
      if (!active) return;
      onError(caught instanceof Error ? caught.message : "进度查询中断，请稍后重试");
    }
  };
  void poll();
  return () => {
    active = false;
    if (timer) clearTimeout(timer);
  };
}
