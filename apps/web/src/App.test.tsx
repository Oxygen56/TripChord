import { describe, expect, it } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";

import {
  UnifiedTripCard,
  formatTravelLocalDateTime,
  livePlanModificationHeading,
  modelParticipationLabel,
  selectPlanForCard,
} from "./App";
import type { LiveFlexibleFromTextResponse } from "./api";

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

describe("final result card selection", () => {
  it("uses the same card contract for a final plan and a best available plan", () => {
    const finalPlan = { departure_date: "2026-09-03" } as never;
    const bestPlan = { departure_date: "2026-09-04" } as never;
    const finalResult = selectPlanForCard({ final_plan: finalPlan, best_available_plan: bestPlan } as never);
    const bestResult = selectPlanForCard({ final_plan: null, best_available_plan: bestPlan } as never);

    expect(finalResult.plan).toBe(finalPlan);
    expect(finalResult.isBestAvailable).toBe(false);
    expect(bestResult.plan).toBe(bestPlan);
    expect(bestResult.isBestAvailable).toBe(true);
  });
});

describe("unified trip card", () => {
  it("shows route, party total, fixed activity, provider and source link", () => {
    const card = {
          status: "candidate",
          title: "多城市异地进出与固定活动方案",
          start_date: "2026-10-02",
          end_date: "2026-10-08",
          city_order: ["大阪", "京都", "东京"],
          traveler_count: 2,
          total_cny_cents: 852000,
          activity_price_included: false,
          unresolved_items: ["活动费用未提供，未计入总价。"],
          source_boundary: "仅对当前有界来源目录求得最优",
          query_captured_at: "2026-08-25T10:00:00Z",
          source_statuses: [
            {
              source_id: "frozen",
              provider: "frozen-fixture",
              state: "succeeded",
              detail: "来源目录已返回",
              query_task_ids: ["query:transport:杭州-大阪"],
              captured_at: "2026-08-25T10:00:00Z",
            },
          ],
          fixed_activities: [
            {
              kind: "anchor",
              offer_id: "concert",
              label: "已持有演唱会",
              provider: "user-provided",
              start: "2026-10-03T19:00:00",
              end: "2026-10-03T21:30:00",
              place_from: "大阪",
              price_contract_id: "user-provided-not-priced",
              detail_url: "",
              price_cny_cents: null,
              shared_price_contract: false,
              participant_ids: ["traveler:a"],
            },
          ],
          travelers: [
            {
              id: "traveler:a",
              name: "甲",
              origin: { id: "杭州", name: "杭州", city: "杭州" },
              available_window: {
                start: "2026-10-02T00:00:00",
                end: "2026-10-08T23:59:59",
              },
              age_category: "adult",
            },
            {
              id: "traveler:b",
              name: "乙",
              origin: { id: "北京", name: "北京", city: "北京" },
              available_window: {
                start: "2026-10-02T00:00:00",
                end: "2026-10-06T23:59:59",
              },
              age_category: "adult",
            },
          ],
          shared_components: [
            {
              kind: "stay",
              offer_id: "shared-stay",
              label: "共享住宿",
              provider: "frozen-fixture",
              start: "2026-10-02",
              end: "2026-10-06",
              place_from: "大阪",
              price_contract_id: "pc-shared-stay",
              detail_url: "https://example.test/shared-stay",
              price_cny_cents: 200000,
              shared_price_contract: false,
              participant_ids: ["traveler:a", "traveler:b"],
            },
          ],
          traveler_itineraries: [
            {
              traveler_id: "traveler:a",
              traveler_name: "甲",
              origin: { id: "杭州", name: "杭州", city: "杭州" },
              components: [],
            },
            {
              traveler_id: "traveler:b",
              traveler_name: "乙",
              origin: { id: "北京", name: "北京", city: "北京" },
              components: [],
            },
          ],
          traveler_costs: [
            {
              traveler_id: "traveler:a",
              traveler_name: "甲",
              direct_cny_cents: 426000,
              allocated_shared_cny_cents: 0,
              attributable_total_cny_cents: 426000,
            },
            {
              traveler_id: "traveler:b",
              traveler_name: "乙",
              direct_cny_cents: 426000,
              allocated_shared_cny_cents: 0,
              attributable_total_cny_cents: 426000,
            },
          ],
          shared_cost_cny_cents: 0,
          price_contracts: [],
          components: [
            {
              kind: "transport",
              offer_id: "hgh-osa",
              label: "杭州→大阪",
              provider: "frozen-fixture",
              start: "2026-10-02T09:00:00",
              end: "2026-10-02T13:00:00",
              place_from: "杭州",
              place_to: "大阪",
              price_contract_id: "pc-hgh-osa",
              detail_url: "https://example.test/hgh-osa",
              price_cny_cents: 148000,
              shared_price_contract: false,
              participant_ids: ["traveler:a"],
            },
          ],
    } satisfies NonNullable<LiveFlexibleFromTextResponse["trip_card"]>;
    const html = renderToStaticMarkup(<UnifiedTripCard card={card} />);
    const noSolutionHtml = renderToStaticMarkup(
      <UnifiedTripCard
        card={{ ...card, status: "no_solution", total_cny_cents: null, components: [] }}
      />,
    );

    expect(html).toContain("¥8,520.00");
    expect(html).toContain("大阪 → 京都 → 东京");
    expect(html).toContain("2 人");
    expect(html).toContain("已持有演唱会");
    expect(html).toContain("共同安排");
    expect(html).toContain("每人行程");
    expect(html).toContain("费用对账");
    expect(html).toContain("不代表实际结算方式");
    expect(html).toContain("frozen-fixture");
    expect(html).toContain("https://example.test/hgh-osa");
    expect(noSolutionHtml).toContain("暂无可行方案");
    expect(noSolutionHtml).toContain("已持有演唱会");
  });
});
