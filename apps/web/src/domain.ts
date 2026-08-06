export type PriceState =
  | "estimated"
  | "live_search"
  | "revalidated"
  | "booked"
  | "user_snapshot";

export function priceLabel(state: PriceState): string {
  const labels: Record<PriceState, string> = {
    estimated: "估算价",
    live_search: "实时搜索价",
    revalidated: "已复价",
    booked: "已确认",
    user_snapshot: "用户快照",
  };
  return labels[state];
}

export function needsPriceWarning(state: PriceState): boolean {
  return !["revalidated", "booked"].includes(state);
}

export type ComponentCoverageSource =
  | "exact_quote"
  | "comparison_price_only"
  | "bounded_no_exact_quote"
  | "failure_terminal";

export type ComponentCoverageExplanation = {
  component_id: string;
  provider: string;
  vertical: string;
  coverage_source: ComponentCoverageSource;
  captured_at: string | null;
  expires_at: string | null;
  comparable_conditions: string[];
  failure_terminal_states: string[];
  hops: number | null;
};

export function componentCoverageExplanations(
  run: {
    coverage: Array<{
      provider: string;
      terminal_outcome_source_ids: string[];
      usable_quote_source_ids: string[];
      failed_source_ids: string[];
      failure_reasons: string[];
      flight_outcome_state: string | null;
    }>;
    flight_search_outcomes: Array<{
      source_task_id: string;
      provider: string;
      state: string;
      reason: string;
    }>;
    source_task_ids: string[];
  },
): ComponentCoverageExplanation[] {
  const flightByTask = new Map(
    run.flight_search_outcomes.map((outcome) => [outcome.source_task_id, outcome]),
  );
  const usable = new Set(run.coverage.flatMap((item) => item.usable_quote_source_ids));
  const terminal = new Set(
    run.coverage.flatMap((item) => item.terminal_outcome_source_ids),
  );
  const failedSources = new Set(run.coverage.flatMap((item) => item.failed_source_ids));
  const failedReasonsBySource = new Map<string, string[]>();
  for (const item of run.coverage) {
    for (const sourceId of item.failed_source_ids) {
      failedReasonsBySource.set(sourceId, item.failure_reasons);
    }
  }

  return run.source_task_ids.map((taskId) => {
    const provider = taskProviderOf(taskId);
    const vertical = taskVerticalOf(taskId);
    const flightOutcome = flightByTask.get(taskId);
    const failureReasons = failedReasonsBySource.get(taskId) ?? [];
    let coverageSource: ComponentCoverageSource = "bounded_no_exact_quote";
    let failureTerminalStates: string[] = [];
    if (usable.has(taskId)) {
      coverageSource = "exact_quote";
    } else if (flightOutcome && flightOutcome.state === "comparison_price_only") {
      coverageSource = "comparison_price_only";
    } else if (failedSources.has(taskId)) {
      // A failed terminal source (login / captcha / dom drift / timeout / ...)
      // is disclosed as a typed failure, never upgraded to a bounded miss.
      coverageSource = "failure_terminal";
      failureTerminalStates = failureReasons;
    } else if (terminal.has(taskId)) {
      coverageSource = "bounded_no_exact_quote";
    } else {
      coverageSource = "failure_terminal";
      failureTerminalStates = failureReasons;
    }
    return {
      component_id: taskId,
      provider,
      vertical,
      coverage_source: coverageSource,
      captured_at: null,
      expires_at: null,
      comparable_conditions: [],
      failure_terminal_states: failureTerminalStates,
      hops: null,
    };
  });
}

export function taskProviderOf(taskId: string): string {
  const match = /^source-([a-z0-9-]+?)-/.exec(taskId) ?? /^publication-([a-z0-9-]+?)-/.exec(taskId);
  return match ? match[1] : taskId;
}

export function taskVerticalOf(taskId: string): string {
  if (taskId.includes("-flight")) return "flight";
  if (taskId.includes("-lodging")) return "lodging";
  if (taskId.includes("-icom-") || taskId.includes("public-transfer")) return "transfer";
  return "unknown";
}

