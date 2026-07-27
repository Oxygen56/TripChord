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

