import { describe, expect, it } from "vitest";

import {
  formatTravelLocalDateTime,
  livePlanModificationHeading,
  modelParticipationLabel,
} from "./App";

describe("travel local date and time", () => {
  it("preserves the source airport local time instead of converting it to the browser zone", () => {
    expect(formatTravelLocalDateTime("2026-09-04T12:20:00+05:00")).toBe(
      "2026年9月4日 12:20（UTC+05:00）",
    );
    expect(formatTravelLocalDateTime("2026-09-09T00:40:00+08:00")).toBe(
      "2026年9月9日 00:40（UTC+08:00）",
    );
  });
});

describe("model participation label", () => {
  it("does not imply multi-agent execution when the real run made no model calls", () => {
    expect(
      modelParticipationLabel({
        model_enhancement_enabled: true,
        model_trace_count: 0,
      }),
    ).toBe("本次未调用模型");
  });
});

describe("live plan modification result heading", () => {
  it("states that a failed global date change kept the original plan", () => {
    expect(livePlanModificationHeading("blocked", "global")).toBe(
      "修改未完成，原方案保留",
    );
  });

  it("keeps successful global replans and local blocks distinct", () => {
    expect(livePlanModificationHeading("global_replan", "global")).toBe(
      "已按新日期完整规划",
    );
    expect(livePlanModificationHeading("blocked", "lodging")).toBe(
      "没有安全替代项",
    );
  });
});
