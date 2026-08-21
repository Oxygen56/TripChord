import { describe, expect, it } from "vitest";

import { livePlanModificationHeading } from "./App";

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
