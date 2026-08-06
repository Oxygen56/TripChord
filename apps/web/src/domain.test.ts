import { describe, expect, it } from "vitest";

import {
  componentCoverageExplanations,
  needsPriceWarning,
  priceLabel,
  taskProviderOf,
  taskVerticalOf,
} from "./domain";

describe("price truth labels", () => {
  it("keeps snapshots visibly distinct from confirmed prices", () => {
    expect(priceLabel("user_snapshot")).toBe("用户快照");
    expect(needsPriceWarning("user_snapshot")).toBe(true);
    expect(needsPriceWarning("revalidated")).toBe(false);
  });
});

describe("component coverage explanations", () => {
  const run = {
    coverage: [
      {
        provider: "ctrip",
        terminal_outcome_source_ids: ["source-ctrip-flight"],
        usable_quote_source_ids: ["source-ctrip-flight"],
        failed_source_ids: [],
        failure_reasons: [],
        flight_outcome_state: "quote_found",
      },
      {
        provider: "qunar",
        terminal_outcome_source_ids: ["source-qunar-flight"],
        usable_quote_source_ids: [],
        failed_source_ids: ["source-qunar-flight"],
        failure_reasons: ["source-qunar-flight: login_required - 需要登录"],
        flight_outcome_state: null,
      },
    ],
    flight_search_outcomes: [
      {
        source_task_id: "source-ctrip-flight",
        provider: "ctrip",
        state: "quote_found",
        reason: "exact",
      },
      {
        source_task_id: "source-qunar-flight",
        provider: "qunar",
        state: "bounded_no_exact_quote",
        reason: "login",
      },
    ],
    source_task_ids: ["source-ctrip-flight", "source-qunar-flight"],
  };

  it("marks usable quote sources as exact_quote", () => {
    const explanations = componentCoverageExplanations(run);
    const ctrip = explanations.find((item) => item.component_id === "source-ctrip-flight");
    expect(ctrip?.coverage_source).toBe("exact_quote");
    expect(ctrip?.provider).toBe("ctrip");
    expect(ctrip?.vertical).toBe("flight");
  });

  it("marks failed terminal sources with their reasons", () => {
    const explanations = componentCoverageExplanations(run);
    const qunar = explanations.find((item) => item.component_id === "source-qunar-flight");
    expect(qunar?.coverage_source).toBe("failure_terminal");
    expect(qunar?.failure_terminal_states).toContain("source-qunar-flight: login_required - 需要登录");
  });

  it("parses provider and vertical from task ids", () => {
    expect(taskProviderOf("source-ctrip-flight")).toBe("ctrip");
    expect(taskVerticalOf("source-ctrip-flight")).toBe("flight");
    expect(taskVerticalOf("source-qunar-lodging-full")).toBe("lodging");
    expect(taskVerticalOf("public-transfer-icom-continuous-outbound")).toBe("transfer");
  });
});

