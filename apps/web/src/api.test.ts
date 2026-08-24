import { afterEach, describe, expect, it, vi } from "vitest";

import {
  advanceLiveRunAfterEvent,
  cancelLiveFlexiblePlanningJob,
  checkLiveMonitorNow,
  checkLiveBridgeHealth,
  consumeHandoff,
  getLiveFlexiblePlanningJob,
  getAgenticMetricsPresentation,
  getBreakfastPreferenceApplication,
  getLiveMonitor,
  getLivePackage,
  modifyLivePackage,
  normalizeBreakfastWeight,
  requireLiveBridgeAvailability,
  replanLivePackage,
  repriceComponent,
  revokeAgentPreferenceMemory,
  resolveFlexibleOption,
  runLiveFlexiblePlanningFromText,
  runLivePackagePlanning,
  startLiveFlexiblePlanningFromTextJob,
  startLiveMonitor,
  stopLiveMonitor,
  subscribeToLiveFlexiblePlanningJob,
  summarizeLiveProviderCoverage,
  type AgenticRunSummary,
  type LiveFlexibleFromTextResponse,
  type LiveEventReplanRun,
  type LivePackageAgentRun,
  type LivePlatformSearchCoverage,
} from "./api";

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("live planning progress stream", () => {
  it("does not publish an old terminal refetch after unsubscribe", async () => {
    let finishRefetch: ((response: Response) => void) | undefined;
    const refetch = new Promise<Response>((resolve) => {
      finishRefetch = resolve;
    });
    const reader = {
      read: vi.fn().mockResolvedValueOnce({
        done: false,
        value: new TextEncoder().encode(
          'event: job\ndata: {"state":"failed"}\n\n',
        ),
      }),
    };
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce({
        ok: true,
        body: { getReader: () => reader },
      })
      .mockImplementationOnce(() => refetch);
    vi.stubGlobal("fetch", fetchMock);
    const onJob = vi.fn();

    const unsubscribe = subscribeToLiveFlexiblePlanningJob(
      "old-job",
      onJob,
      vi.fn(),
    );
    await vi.waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));
    unsubscribe();
    finishRefetch!(
      new Response(JSON.stringify({ state: "failed", result: null }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
    );
    await Promise.resolve();
    await Promise.resolve();

    expect(onJob).not.toHaveBeenCalled();
  });
});

function makeAgenticSummary({
  taskId,
  provider = "openai",
  model = "model-a",
  logicalRequests = 1,
  primaryAttempts = 1,
  fallbackAttempts = 0,
  latencySeconds = 0.1,
  estimatedCostUsd = 0.001,
  tokenUsage = 10,
}: {
  taskId: string;
  provider?: string;
  model?: string;
  logicalRequests?: number;
  primaryAttempts?: number;
  fallbackAttempts?: number;
  latencySeconds?: number;
  estimatedCostUsd?: number;
  tokenUsage?: number;
}): AgenticRunSummary {
  const attempts = primaryAttempts + fallbackAttempts;
  return {
    enabled: true,
    required: false,
    stage_count: 1,
    model_stage_count: logicalRequests > 0 ? 1 : 0,
    logical_request_count: logicalRequests,
    primary_http_attempt_count: primaryAttempts,
    fallback_http_attempt_count: fallbackAttempts,
    http_attempt_count: attempts,
    total_latency_seconds: latencySeconds,
    total_estimated_cost_usd: estimatedCostUsd,
    model_call_count: logicalRequests,
    total_token_usage: tokenUsage,
    providers: [provider],
    models: [model],
    stages: [
      {
        task_id: taskId,
        role: "query_strategist",
        model_called: logicalRequests > 0,
        provider,
        model,
        token_usage: tokenUsage,
        logical_request_count: logicalRequests,
        primary_http_attempt_count: primaryAttempts,
        fallback_http_attempt_count: fallbackAttempts,
        http_attempt_count: attempts,
        total_latency_seconds: latencySeconds,
        estimated_cost_usd: estimatedCostUsd,
        context_token_budget: 1_000,
        context_used_tokens: 500,
        tool_observation_tokens: 100,
        truncated_tool_observations: 0,
        tool_names: ["quote-search"],
        fallback_used: fallbackAttempts > 0,
        failure: null,
      },
    ],
    safety_boundary: "模型只能提案",
    metrics_boundary: "Scripted client attempts 不是网络请求证据",
  };
}

describe("real multi-platform API boundary", () => {
  it("normalizes all four breakfast modes to the backend canonical 0-1 weight", () => {
    expect(normalizeBreakfastWeight("required", 0.35)).toBe(1);
    expect(normalizeBreakfastWeight("forbidden", 0.35)).toBe(1);
    expect(normalizeBreakfastWeight("indifferent", 0.35)).toBe(0);
    expect(normalizeBreakfastWeight("weighted", 0.85)).toBe(0.85);
    expect(normalizeBreakfastWeight("weighted", 2)).toBe(1);
    expect(normalizeBreakfastWeight("weighted", -1)).toBe(0);
  });

  it("does not probe the protected bridge without a valid pairing token", async () => {
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);

    const health = await checkLiveBridgeHealth("too-short");

    expect(health.available).toBe(false);
    expect(health.message).toContain("不会在未授权状态下探测");
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("uses the pairing token for a read-only bridge health check", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(
        JSON.stringify({
          status: "connected",
          companions: [
            {
              providers: ["ctrip", "qunar", "tongcheng"],
              is_fresh: true,
            },
          ],
        }),
        { status: 200, headers: { "Content-Type": "application/json" } },
      ),
    );
    vi.stubGlobal("fetch", fetchMock);

    const token = "a".repeat(32);
    const health = await checkLiveBridgeHealth(token);
    const [path, init] = fetchMock.mock.calls[0] as [string, RequestInit];

    expect(health.available).toBe(true);
    expect(path).toBe("/browser-bridge/v1/companions/status");
    expect(init.headers).toMatchObject({
      "X-TripChord-Bridge-Token": token,
    });
    expect(init.signal).toBeInstanceOf(AbortSignal);
    expect(() => requireLiveBridgeAvailability(health)).not.toThrow();
  });

  it("fails closed when the bridge is reachable but the active companion is stale", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(
        JSON.stringify({
          status: "disconnected",
          companions: [
            {
              providers: ["ctrip", "qunar", "tongcheng"],
              is_fresh: false,
            },
          ],
        }),
        { status: 200, headers: { "Content-Type": "application/json" } },
      ),
    );
    vi.stubGlobal("fetch", fetchMock);

    const health = await checkLiveBridgeHealth("a".repeat(32));

    expect(health.available).toBe(false);
    expect(health.message).toContain("45 秒内");
    expect(() => requireLiveBridgeAvailability(health)).toThrow();
  });

  it("fails closed before live planning when the bridge preflight is unavailable", () => {
    expect(() =>
      requireLiveBridgeAvailability({
        available: false,
        status: "unavailable",
        scope: null,
        message: "当前没有已配对的 Chrome Companion",
        checked_at: "2026-08-03T00:00:00Z",
      }),
    ).toThrow("当前没有已配对的 Chrome Companion");
  });

  it("submits the live-plan contract without falling back to replay", async () => {
    const run = {
      mode: "strict",
      run_purpose: "final_publication",
      finalization_state: "final_published",
      deferred_stage_ids: [],
      exploration_seal_passed: false,
      source_task_ids: Array.from({ length: 15 }, (_, index) => `source-${index}`),
    } as unknown as LivePackageAgentRun;
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(
        JSON.stringify({
          run_id: "live-run-1",
          expires_at: "2026-08-01T00:10:00Z",
          run,
        }),
        { status: 200, headers: { "Content-Type": "application/json" } },
      ),
    );
    vi.stubGlobal("fetch", fetchMock);

    const request = {
      intent: {
        trip_id: "trip-1",
        origin: "杭州",
        destination: "马累",
        start_date: "2026-08-12",
        end_date: "2026-08-18",
        adults: 2,
        rooms: 1,
        currency: "CNY",
        budget_cents: 3_000_000,
        require_checked_baggage: false,
        require_breakfast: null,
      },
      search_query: {
        origin: "杭州",
        destination: "马累",
        start_date: "2026-08-12",
        end_date: "2026-08-18",
        adults: 2,
        rooms: 1,
        currency: "CNY",
        options: { allow_connections: true },
      },
      coverage_mode: "strict" as const,
      timeout_seconds: 120,
    };

    const response = await runLivePackagePlanning(request);
    const [path, init] = fetchMock.mock.calls[0] as [string, RequestInit];

    expect(response.run_id).toBe("live-run-1");
    expect(response.run).toMatchObject({
      run_purpose: "final_publication",
      finalization_state: "final_published",
      deferred_stage_ids: [],
      exploration_seal_passed: false,
    });
    expect(path).toBe("/api/v1/agents/live-plan");
    expect(init.method).toBe("POST");
    expect(JSON.parse(String(init.body))).toEqual(request);
  });

  it("submits natural language to the flexible live endpoint without replay fallback", async () => {
    const responseBody = {
      interpretation: {
        state: "human_block",
        window: null,
        intent_template: null,
        preferences: {
          rules: [
            {
              key: "hotel_breakfast",
              mode: "weighted",
              weight: 0.85,
              expected: true,
              source: "explicit_current_trip",
              reason: "用户通过结构化控件设置早餐模式或权重",
            },
          ],
        },
        facts: [],
        unresolved: [
          {
            field: "destination",
            reason: "用户未明确目的地",
            critical: true,
            model_proposal: null,
          },
          {
            field: "preference_application:hotel_breakfast",
            reason: "早餐权重尚未映射到实时 Planner 排序分",
            critical: false,
            model_proposal: null,
          },
        ],
        conflicts: [],
        claim_boundary: "只完成需求解析；没有启动报价搜索",
      },
      run: null,
      cached_pair_runs: [],
      model_enhancement_enabled: false,
      execution_boundary: "确定性优先；模型增强未启用",
    } satisfies LiveFlexibleFromTextResponse;
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify(responseBody), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
    );
    vi.stubGlobal("fetch", fetchMock);

    const request = {
      requirement: {
        text: "出发地：杭州，2026年8月出发，玩5-8天，2名成人，1间房",
        breakfast_mode: "weighted" as const,
        breakfast_weight: 0.85,
      },
      coverage_mode: "strict" as const,
      timeout_seconds: 120,
      total_timeout_seconds: 600,
      max_pairs: 3,
    };
    const response = await runLiveFlexiblePlanningFromText(request);
    const [path, init] = fetchMock.mock.calls[0] as [string, RequestInit];

    expect(path).toBe("/api/v1/agents/live-flexible-plan-from-text");
    expect(init.method).toBe("POST");
    expect(JSON.parse(String(init.body))).toEqual(request);
    expect(response.interpretation.state).toBe("human_block");
    expect(response.run).toBeNull();
    expect(getBreakfastPreferenceApplication(response)).toMatchObject({
      state: "not_ranked",
      mode: "weighted",
      weight: 0.85,
      reason: "早餐权重尚未映射到实时 Planner 排序分",
    });
  });

  it("uses the cancellable live job control plane for long multi-platform planning", async () => {
    const job = {
      id: "live-job/1",
      state: "queued" as const,
      stage: "queued",
      progress: 0,
      cancellation_requested: false,
      revision: 1,
      result: null,
      error: null,
      created_at: "2026-08-03T00:00:00Z",
      updated_at: "2026-08-03T00:00:00Z",
      expires_at: null,
      boundary: "process-local bounded job",
    };
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify({
            job,
            status_url: "/status",
            events_url: "/events",
            replayed: false,
          }),
          { status: 202, headers: { "Content-Type": "application/json" } },
        ),
      )
      .mockResolvedValueOnce(
        new Response(JSON.stringify({ ...job, state: "running", progress: 20 }), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        }),
      )
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify({
            ...job,
            state: "cancelled",
            stage: "cancelled",
            progress: 100,
            cancellation_requested: true,
          }),
          { status: 200, headers: { "Content-Type": "application/json" } },
        ),
      );
    vi.stubGlobal("fetch", fetchMock);

    const input = {
      requirement: { text: "杭州到马累，2026年8月，2成人" },
      coverage_mode: "strict" as const,
      timeout_seconds: 120,
      total_timeout_seconds: 600,
      max_pairs: 3,
    };
    const started = await startLiveFlexiblePlanningFromTextJob(input, "ui-retry-key-1");
    const running = await getLiveFlexiblePlanningJob(started.job.id);
    const cancelled = await cancelLiveFlexiblePlanningJob(started.job.id);

    expect(started.job.id).toBe("live-job/1");
    expect(running.state).toBe("running");
    expect(cancelled.state).toBe("cancelled");
    expect(fetchMock.mock.calls.map(([path]) => path)).toEqual([
      "/api/v1/agents/live-flexible-plan-from-text/jobs",
      "/api/v1/agents/live-flexible-plan-from-text/jobs/live-job%2F1",
      "/api/v1/agents/live-flexible-plan-from-text/jobs/live-job%2F1",
    ]);
    expect((fetchMock.mock.calls[2]?.[1] as RequestInit).method).toBe("DELETE");
    expect((fetchMock.mock.calls[0]?.[1] as RequestInit).headers).toMatchObject({
      "Idempotency-Key": "ui-retry-key-1",
    });
  });

  it("does not infer weighted ranking from the absence of a parser issue", () => {
    const response = {
      interpretation: {
        state: "ready",
        window: null,
        intent_template: null,
        preferences: {
          rules: [
            {
              key: "hotel_breakfast",
              mode: "weighted",
              weight: "0.9",
              expected: true,
              source: "explicit_current_trip",
              reason: "structured override",
            },
          ],
        },
        facts: [],
        unresolved: [],
        conflicts: [],
        claim_boundary: "test boundary",
      },
      run: null,
      cached_pair_runs: [],
      model_enhancement_enabled: false,
      execution_boundary: "test boundary",
    } satisfies LiveFlexibleFromTextResponse;

    expect(getBreakfastPreferenceApplication(response)).toMatchObject({
      state: "not_ranked",
      mode: "weighted",
      weight: 0.9,
      reason: "后端尚未返回实时整包的早餐偏好应用证据，不能声称权重已进入排序。",
    });
  });

  it("reports weighted ranking only from package application evidence", () => {
    const response = {
      interpretation: {
        state: "ready",
        preferences: {
          rules: [
            {
              key: "hotel_breakfast",
              mode: "weighted",
              weight: 0.9,
            },
          ],
        },
        unresolved: [],
      },
      run: {
        pair_runs: [
          {
            run: {
              package: {
                preference_applications: [
                  {
                    key: "hotel_breakfast",
                    mode: "weighted",
                    weight: 0.9,
                    state: "applied",
                    reason: "后端已在两个同证据层候选中应用权重",
                  },
                ],
              },
            },
          },
        ],
      },
    } as unknown as LiveFlexibleFromTextResponse;

    expect(getBreakfastPreferenceApplication(response)).toMatchObject({
      state: "ranked",
      mode: "weighted",
      weight: 0.9,
      reason: "后端已在两个同证据层候选中应用权重",
    });
  });

  it("maps a ranked date option to its completed run and tenant-safe cache handle", () => {
    const pairRun = {
      date_pair: {
        id: "pair-b",
        rank: 1,
        departure_date: "2026-08-20",
        return_date: "2026-08-26",
        night_count: 6,
        source: "stratified_sample",
        audit_reason: "stratified sample",
      },
      state: "completed",
      run: { source_task_ids: Array.from({ length: 15 }, (_, index) => `s-${index}`) },
      failure_class: null,
      failure_message: null,
    };
    const response = {
      interpretation: { state: "ready" },
      run: {
        recommended_option_ids: ["pair-b"],
        ranked_options: [
          {
            rank: 1,
            date_pair_id: "pair-b",
            departure_date: "2026-08-20",
            return_date: "2026-08-26",
            decision_state: "accept",
            recommendable: true,
            total_budget_cents: 14_477_00,
            evidence_completeness: "1",
            all_platforms_complete: true,
            final_candidate_id: "package-b",
          },
        ],
        pair_runs: [pairRun],
      },
      cached_pair_runs: [
        {
          date_pair_id: "pair-b",
          run_id: "live-run-b",
          expires_at: "2026-08-01T00:10:00Z",
        },
      ],
    } as unknown as LiveFlexibleFromTextResponse;

    const selected = resolveFlexibleOption(response);

    expect(selected?.option.date_pair_id).toBe("pair-b");
    expect(selected?.pair.run?.source_task_ids).toHaveLength(15);
    expect(selected?.handle?.run_id).toBe("live-run-b");
  });

  it("uses the API final_plan as the sole default selection", () => {
    const option = (id: string) => ({
      rank: id === "pair-final" ? 2 : 1,
      date_pair_id: id,
      departure_date: "2026-08-20",
      return_date: "2026-08-26",
      decision_state: "accept",
      recommendable: true,
      total_budget_cents: 1200000,
      evidence_completeness: "1",
      all_platforms_complete: true,
      final_candidate_id: id,
    });
    const response = {
      interpretation: { state: "ready" },
      final_plan: {
        option_id: "pair-final",
        date_pair_id: "pair-final",
        departure_date: "2026-08-20",
        return_date: "2026-08-26",
        total_budget_cents: 1200000,
        optimality_status: "best_verified",
        claim_boundary: "test",
        price_comparability: "complete_cny",
        party: { adults: 2, rooms: 1 },
      },
      run: {
        recommended_option_ids: ["pair-ranked"],
        ranked_options: [option("pair-ranked"), option("pair-final")],
        pair_runs: [
          { date_pair: { id: "pair-ranked" }, state: "completed", run: {} },
          { date_pair: { id: "pair-final" }, state: "completed", run: {} },
        ],
      },
      cached_pair_runs: [],
    } as unknown as LiveFlexibleFromTextResponse;

    expect(resolveFlexibleOption(response)?.option.date_pair_id).toBe("pair-final");
  });

  it("resolves a composite final option id through the explicit date-pair id", () => {
    const response = {
      interpretation: { state: "ready" },
      final_plan: {
        option_id: "pair-final:maafushi_icom",
        date_pair_id: "pair-final",
      },
      run: {
        recommended_option_ids: ["pair-final:maafushi_icom"],
        ranked_options: [
          {
            rank: 1,
            date_pair_id: "pair-final",
            departure_date: "2026-08-20",
            return_date: "2026-08-26",
            decision_state: "accept",
            recommendable: true,
            total_budget_cents: 1200000,
            evidence_completeness: "1",
            all_platforms_complete: true,
            final_candidate_id: "pair-final:maafushi_icom",
          },
        ],
        pair_runs: [
          { date_pair: { id: "pair-final" }, state: "completed", run: {} },
        ],
      },
      cached_pair_runs: [
        {
          date_pair_id: "pair-final",
          run_id: "live-run-final",
          expires_at: "2026-08-01T00:10:00Z",
        },
      ],
    } as unknown as LiveFlexibleFromTextResponse;

    const selected = resolveFlexibleOption(response);

    expect(selected?.option.date_pair_id).toBe("pair-final");
    expect(selected?.handle?.run_id).toBe("live-run-final");
  });

  it("sends a live event only to the live replan endpoint", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(
        JSON.stringify({
          run_id: "live-run-1",
          expires_at: "2026-08-01T00:10:00Z",
          run: {
            source_task_ids: ["event-ctrip-flight"],
            requeried_providers: ["ctrip"],
          },
        }),
        { status: 200, headers: { "Content-Type": "application/json" } },
      ),
    );
    vi.stubGlobal("fetch", fetchMock);

    await replanLivePackage("live-run-1", {
      event: {
        id: "event-1",
        kind: "sold_out",
        target_component_id: "flight-1",
        affected_provider: "ctrip",
      },
      timeout_seconds: 120,
    });
    const [path, init] = fetchMock.mock.calls[0] as [string, RequestInit];

    expect(path).toBe("/api/v1/agents/live-plans/live-run-1/events/replan");
    expect(init.method).toBe("POST");
    expect(JSON.parse(String(init.body))).toMatchObject({
      event: { kind: "sold_out", affected_provider: "ctrip" },
    });
  });

  it("sends a natural-language change only to the current live plan modify endpoint", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(
        JSON.stringify({
          run_id: "live-run/1",
          expires_at: "2026-08-22T08:00:00Z",
          modification: {
            status: "modified",
            intent: {
              instruction: "酒店换成海景房，航班和接驳保持不变",
              affected_scope: "lodging",
              preserve_scopes: ["flight", "transfer"],
              exclude_current_property: false,
              required_room_features: ["sea_view"],
              require_breakfast: null,
              require_non_basic_lodging: null,
              require_non_remote_lodging: null,
              date_patch: null,
              unresolved_reasons: [],
              parse_boundary: "只解析明确修改",
            },
            summary: "只替换住宿",
            before_candidate_id: "candidate-before",
            after_candidate_id: "candidate-after",
            changed_component_ids: ["lodging-before", "lodging-after"],
            preserved_component_ids: ["flight", "transfer-out", "transfer-in"],
            before_confirmed_cny_cents: 1068700,
            after_confirmed_cny_cents: 1080700,
            difference_cny_cents: 12000,
            source_task_ids: ["modification-source-ctrip-lodging-full"],
            source_outcomes: [],
            verifier_passed: true,
            reverifier_passed: true,
            boundary: "不是下单或库存锁定",
          },
          run: { source_task_ids: [] },
          final_plan: null,
        }),
        { status: 200, headers: { "Content-Type": "application/json" } },
      ),
    );
    vi.stubGlobal("fetch", fetchMock);

    const response = await modifyLivePackage("live-run/1", {
      instruction: "酒店换成海景房，航班和接驳保持不变",
      timeout_seconds: 120,
    });
    const [path, init] = fetchMock.mock.calls[0] as [string, RequestInit];

    expect(path).toBe("/api/v1/agents/live-plans/live-run%2F1/modify");
    expect(init.method).toBe("POST");
    expect(JSON.parse(String(init.body))).toEqual({
      instruction: "酒店换成海景房，航班和接驳保持不变",
      timeout_seconds: 120,
    });
    expect(response.modification.status).toBe("modified");
    expect(response.modification.difference_cny_cents).toBe(12000);
  });

  it("uses only the tenant-bound live monitor lifecycle endpoints", async () => {
    const monitor = {
      id: "monitor/1",
      run_id: "live-run/1",
      state: "active",
      interval_seconds: 300,
      max_checks: 24,
      timeout_seconds: 120,
      check_count: 0,
      next_check_at: "2026-08-03T12:05:00Z",
      created_at: "2026-08-03T12:00:00Z",
      updated_at: "2026-08-03T12:00:00Z",
      last_check: null,
      last_error: null,
      boundary: "opt-in periodic read-only revalidation",
    };
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(
        new Response(JSON.stringify({ monitor }), {
          status: 201,
          headers: { "Content-Type": "application/json" },
        }),
      )
      .mockResolvedValueOnce(
        new Response(JSON.stringify({ monitor }), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        }),
      )
      .mockResolvedValueOnce(
        new Response(JSON.stringify({ monitor }), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        }),
      )
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify({
            run_id: "live-run/1",
            expires_at: "2026-08-03T12:30:00Z",
            run: { source_task_ids: [] },
          }),
          { status: 200, headers: { "Content-Type": "application/json" } },
        ),
      )
      .mockResolvedValueOnce(
        new Response(JSON.stringify({ monitor: { ...monitor, state: "stopped" } }), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        }),
      );
    vi.stubGlobal("fetch", fetchMock);

    await startLiveMonitor("live-run/1", {
      interval_seconds: 300,
      max_checks: 24,
      timeout_seconds: 120,
    });
    await getLiveMonitor("monitor/1");
    await checkLiveMonitorNow("monitor/1");
    await getLivePackage("live-run/1");
    await stopLiveMonitor("monitor/1");

    expect(fetchMock.mock.calls.map(([path]) => path)).toEqual([
      "/api/v1/agents/live-plans/live-run%2F1/monitor",
      "/api/v1/agents/live-monitors/monitor%2F1",
      "/api/v1/agents/live-monitors/monitor%2F1/check-now",
      "/api/v1/agents/live-plans/live-run%2F1",
      "/api/v1/agents/live-monitors/monitor%2F1",
    ]);
    expect((fetchMock.mock.calls[0][1] as RequestInit).method).toBe("POST");
    expect((fetchMock.mock.calls[2][1] as RequestInit).method).toBe("POST");
    expect((fetchMock.mock.calls[4][1] as RequestInit).method).toBe("DELETE");
  });

  it("propagates cached final_plan replacement and invalidation for monitor refresh", async () => {
    const run = { decision: { state: "human_block" } };
    const finalPlan = { option_id: "new", date_pair_id: "dates" };
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(new Response(JSON.stringify({ run_id: "r", expires_at: "x", run, final_plan: null }), { status: 200 }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ run_id: "r", expires_at: "x", run, final_plan: finalPlan }), { status: 200 }));
    vi.stubGlobal("fetch", fetchMock);
    expect((await getLivePackage("r")).final_plan).toBeNull();
    expect((await getLivePackage("r")).final_plan).toEqual(finalPlan);
  });

  it("derives provider capability and denominator from actually scheduled sources", () => {
    const run = {
      source_task_ids: [
        "source-ctrip-flight",
        "source-ctrip-lodging-full",
        "source-ctrip-lodging-first",
        "source-tongcheng-flight",
      ],
    } as LivePackageAgentRun;
    const tongcheng = {
      provider: "tongcheng",
      selected_stay_plan_id: null,
      completed_search_verticals: ["flight"],
      successful_verticals: ["flight"],
      failed_verticals: [],
      successful_source_ids: ["source-tongcheng-flight"],
      terminal_outcome_source_ids: ["source-tongcheng-flight"],
      usable_quote_source_ids: ["source-tongcheng-flight"],
      failed_source_ids: [],
      failure_reasons: [],
      flight_outcome_state: "quote_found",
      complete: true,
    } satisfies LivePlatformSearchCoverage;

    expect(summarizeLiveProviderCoverage(run, tongcheng)).toEqual({
      capability_label: "仅机票（当前能力矩阵）",
      completed_source_count: 1,
      expected_source_count: 1,
      usable_quote_count: 1,
    });
  });

  it("advances consecutive event state to the repaired candidate without mutating the original", () => {
    const originalPackage = {
      final_candidate: {
        flight: { id: "flight-old" },
        lodgings: [],
        transfers: [],
      },
    };
    const repairedPackage = {
      final_candidate: {
        flight: { id: "flight-repaired" },
        lodgings: [],
        transfers: [],
      },
    };
    const previous = {
      run_purpose: "final_publication",
      finalization_state: "final_published",
      deferred_stage_ids: [],
      exploration_seal_passed: false,
      decision: { state: "accept", summary: "old", violation_codes: [], evidence_refs: [] },
      claim_boundary: "old boundary",
      inventory: { flights: [], lodgings: [], transfers: [] },
      normalization_results: [],
      package: originalPackage,
      source_task_ids: ["source-ctrip-flight"],
      public_transfer_task_ids: [],
      agentic: makeAgenticSummary({
        taskId: "query-old",
        latencySeconds: 0.2,
        estimatedCostUsd: 0.002,
        tokenUsage: 20,
      }),
      explanation: { summary: "stale" },
      memory_candidates: { summary: "stale", candidates: [] },
    } as unknown as LivePackageAgentRun;
    const eventRun = {
      event: {
        id: "event-1",
        kind: "sold_out",
        target_component_id: "flight-old",
        affected_provider: "ctrip",
      },
      global_run: null,
      decision: { state: "accept", summary: "repaired", violation_codes: [], evidence_refs: [] },
      claim_boundary: "new boundary",
      inventory: { flights: [], lodgings: [], transfers: [] },
      normalization_results: [],
      package: repairedPackage,
      source_task_ids: ["event-source-ctrip-flight"],
      agentic: makeAgenticSummary({
        taskId: "event-new",
        fallbackAttempts: 1,
        latencySeconds: 0.3,
        estimatedCostUsd: 0.003,
        tokenUsage: 10,
      }),
    } as unknown as LiveEventReplanRun;

    const advanced = advanceLiveRunAfterEvent(previous, eventRun);

    expect(advanced.package?.final_candidate.flight.id).toBe("flight-repaired");
    expect(previous.package?.final_candidate.flight.id).toBe("flight-old");
    expect(advanced.source_task_ids).toContain("event-source-ctrip-flight");
    expect(advanced.agentic.stage_count).toBe(2);
    expect(advanced.agentic.model_stage_count).toBe(2);
    expect(advanced.agentic.logical_request_count).toBe(2);
    expect(advanced.agentic.primary_http_attempt_count).toBe(2);
    expect(advanced.agentic.fallback_http_attempt_count).toBe(1);
    expect(advanced.agentic.http_attempt_count).toBe(3);
    expect(advanced.agentic.total_latency_seconds).toBeCloseTo(0.5);
    expect(advanced.agentic.total_estimated_cost_usd).toBeCloseTo(0.005);
    expect(advanced.agentic.model_call_count).toBe(2);
    expect(advanced.run_purpose).toBe("final_publication");
    expect(advanced.finalization_state).toBe("final_published");
    expect(advanced.deferred_stage_ids).toEqual([]);
    expect(advanced.exploration_seal_passed).toBe(false);
    expect(advanced.explanation).toBeNull();
    expect(advanced.memory_candidates).toBeNull();
  });

  it("replaces the whole current run when the event controller performs a global replan", () => {
    const previous = { source_task_ids: ["source-old"] } as LivePackageAgentRun;
    const globalRun = { source_task_ids: ["source-global"] } as LivePackageAgentRun;
    const eventRun = { global_run: globalRun } as LiveEventReplanRun;

    expect(advanceLiveRunAfterEvent(previous, eventRun)).toBe(globalRun);
  });

  it("labels Scripted attempts as offline client attempts rather than HTTP evidence", () => {
    const summary = makeAgenticSummary({
      taskId: "scripted-query",
      provider: "scripted",
      model: "fixture-model",
      primaryAttempts: 2,
      fallbackAttempts: 1,
      latencySeconds: 0.01234,
      estimatedCostUsd: 0.004,
    });

    expect(getAgenticMetricsPresentation(summary)).toEqual({
      attempt_evidence_label: "3 次 client attempt（Scripted 离线回放，不是 HTTP 请求）",
      latency_label: "0.012 秒累计请求墙钟时间",
      estimated_cost_label: "已知 usage 估算成本 US$0.004000",
      scripted_only: true,
      includes_scripted: true,
    });
  });

  it("calls the authenticated memory DELETE contract for an immediate undo", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(
        JSON.stringify({
          record_id: "memory/user/preference/hotel_breakfast",
          revoked: true,
          boundary: "只能撤销当前用户自己的记忆",
        }),
        { status: 200, headers: { "Content-Type": "application/json" } },
      ),
    );
    vi.stubGlobal("fetch", fetchMock);

    const response = await revokeAgentPreferenceMemory(
      "memory/user/preference/hotel_breakfast",
    );
    const [path, init] = fetchMock.mock.calls[0] as [string, RequestInit];

    expect(response.revoked).toBe(true);
    expect(path).toBe(
      "/api/v1/agents/memory/memory%2Fuser%2Fpreference%2Fhotel_breakfast",
    );
    expect(init.method).toBe("DELETE");
  });
});

describe("v0.5 official-handoff API", () => {
  it("repriceComponent posts to the per-component reprice endpoint", async () => {
    const fetchMock = vi.fn(async (_path: string, _init?: RequestInit) =>
      Promise.resolve({
        ok: true,
        json: async () => ({
          run_id: "run-1",
          component_id: "comp-1",
          plan_version: "run-1",
          scope_key: "ctrip:flight",
          outcome: "unchanged",
          live_mode: "fixture",
          revalidation_receipt: {
            receipt_id: "receipt-1",
            outcome: "unchanged",
            total_for_party_cents: 120000,
          },
          checklist: {
            component_id: "comp-1",
            suggested_next_step: "go_to_official",
            official_handoff: { handoff_id: "handoff-1", url: "https://flights.ctrip.com/x" },
          },
        }),
      }),
    );
    vi.stubGlobal("fetch", fetchMock);

    const response = await repriceComponent("run-1", "comp-1");
    const [path, init] = fetchMock.mock.calls[0] as [string, RequestInit];

    expect(response.outcome).toBe("unchanged");
    expect(response.checklist?.suggested_next_step).toBe("go_to_official");
    expect(path).toBe("/api/v1/agents/live-plans/run-1/components/comp-1/reprice");
    expect(init.method).toBe("POST");
  });

  it("consumeHandoff posts handoff id and never marks booked", async () => {
    const fetchMock = vi.fn(async (_path: string, _init?: RequestInit) =>
      Promise.resolve({
        ok: true,
        json: async () => ({
          handoff_id: "handoff-1",
          consumed: true,
          state: "used",
          booked: false,
        }),
      }),
    );
    vi.stubGlobal("fetch", fetchMock);

    const response = await consumeHandoff("run-1", "comp-1", "handoff-1");
    const [path, init] = fetchMock.mock.calls[0] as [string, RequestInit];

    expect(response.consumed).toBe(true);
    expect(response.booked).toBe(false);
    expect(path).toBe(
      "/api/v1/agents/live-plans/run-1/components/comp-1/handoff/consume",
    );
    expect(init.method).toBe("POST");
    expect(JSON.parse(String(init.body))).toEqual({ handoff_id: "handoff-1" });
  });
});
