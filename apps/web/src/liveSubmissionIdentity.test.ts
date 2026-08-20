import { afterEach, describe, expect, it } from "vitest";

import {
  ApiError,
  canonicalLiveInput,
  clearLiveSubmissionIdentity,
  liveSubmissionIdentity,
  applyLivePlanningJob,
  shouldClearLiveSubmissionIdentity,
} from "./App";
import { retryDelayMs } from "./api";

function installStorage() {
  const values = new Map<string, string>();
  const storage = {
    getItem: (key: string) => values.get(key) ?? null,
    setItem: (key: string, value: string) => values.set(key, value),
    removeItem: (key: string) => values.delete(key),
    key: (index: number) => [...values.keys()][index] ?? null,
    get length() {
      return values.size;
    },
  };
  Object.assign(globalThis, { window: { localStorage: storage } });
  return storage;
}

const input = (text: string, breakfast = "indifferent") =>
  ({
    requirement: { text, trip_id: "pending", breakfast_mode: breakfast },
    coverage_mode: "strict",
    timeout_seconds: 120,
    total_timeout_seconds: 600,
    max_pairs: 66,
  }) as never;

describe("live submission identity", () => {
  afterEach(() => {
    delete (globalThis as { window?: unknown }).window;
  });

  it("canonicalizes nested fields and distinguishes text/preferences", async () => {
    installStorage();
    const first = canonicalLiveInput(input("海岛", "indifferent"));
    const second = canonicalLiveInput(input("城市", "required"));
    expect(first).not.toEqual(second);
    const identityA = await liveSubmissionIdentity(input("海岛"));
    const identityB = await liveSubmissionIdentity(input("城市"));
    expect(identityA.idempotencyKey).not.toBe(identityB.idempotencyKey);
  });

  it("reuses an active identity, then creates a new one after clear", async () => {
    const storage = installStorage();
    const first = await liveSubmissionIdentity(input("海岛"));
    const retry = await liveSubmissionIdentity(input("海岛"));
    expect(retry).toEqual(first);
    clearLiveSubmissionIdentity(first.storageKey);
    const next = await liveSubmissionIdentity(input("海岛"));
    expect(next.idempotencyKey).not.toBe(first.idempotencyKey);
    expect(storage.length).toBe(1);
  });

  it("keeps transient and authorization failures retryable", () => {
    expect(shouldClearLiveSubmissionIdentity(new ApiError("missing", 404))).toBe(true);
    expect(shouldClearLiveSubmissionIdentity(new ApiError("gone", 410))).toBe(true);
    for (const status of [401, 429, 500, 503]) {
      expect(shouldClearLiveSubmissionIdentity(new ApiError("retry", status))).toBe(false);
    }
    expect(shouldClearLiveSubmissionIdentity(new TypeError("network"))).toBe(false);
  });

  it("replaces malformed active storage without reusing it", async () => {
    const storage = installStorage();
    const key = `tripchord-live-submission:${"a".repeat(64)}`;
    storage.setItem(key, "{malformed");
    const identity = await liveSubmissionIdentity(input("海岛"));
    expect(identity.idempotencyKey).toBeTruthy();
    expect(storage.getItem(identity.storageKey)).toContain(identity.tripId);
  });

  it("uses bounded exponential polling backoff", () => {
    expect(retryDelayMs(1)).toBe(500);
    expect(retryDelayMs(2)).toBe(1000);
    expect(retryDelayMs(20)).toBe(8000);
  });

  it("applies every terminal and running job state consistently", () => {
    const base = { id: "job", stage: "x", progress: 1, cancellation_requested: false, revision: 1, created_at: "", updated_at: "", expires_at: null, boundary: "" };
    expect(applyLivePlanningJob({ ...base, state: "running", result: null, error: null }).clearIdentity).toBe(false);
    expect(applyLivePlanningJob({ ...base, state: "failed", result: null, error: "失败" }).error).toBe("失败");
    expect(applyLivePlanningJob({ ...base, state: "cancelled", result: null, error: null }).clearIdentity).toBe(true);
    expect(applyLivePlanningJob({ ...base, state: "succeeded", result: null, error: null }).error).toContain("没有返回");
  });
});
