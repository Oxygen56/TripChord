import { describe, expect, it } from "vitest";

import { needsPriceWarning, priceLabel } from "./domain";

describe("price truth labels", () => {
  it("keeps snapshots visibly distinct from confirmed prices", () => {
    expect(priceLabel("user_snapshot")).toBe("用户快照");
    expect(needsPriceWarning("user_snapshot")).toBe(true);
    expect(needsPriceWarning("revalidated")).toBe(false);
  });
});

