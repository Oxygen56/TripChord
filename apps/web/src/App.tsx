import { FormEvent, useEffect, useMemo, useState } from "react";

import {
  advanceLiveRunAfterEvent,
  type AgenticRunSummary,
  type AgentRun,
  cancelLiveFlexiblePlanningJob,
  checkLiveMonitorNow,
  checkLiveBridgeHealth,
  comparePlans,
  confirmAgentPreferenceMemory,
  consumeHandoff,
  fetchProviderCapabilities,
  repriceComponent,
  type RepriceComponentResponse,
  getAgenticMetricsPresentation,
  getBreakfastPreferenceApplication,
  getLiveMonitor,
  getLivePackage,
  type Job,
  type JsonValue,
  type LiveBridgeHealth,
  type LiveEventReplanRun,
  type LiveFlexibleFromTextResponse,
  type LiveMonitorStatus,
  type LivePackageAgentRun,
  type LivePlanningJobSnapshot,
  type LiveProvider,
  type ProviderCapabilitiesResponse,
  loadWorkspace,
  type Offer,
  type PlanDiff,
  type PlanItem,
  type PreferenceMode,
  type ReplanResult,
  normalizeBreakfastWeight,
  requireLiveBridgeAvailability,
  replanLivePackage,
  replanWorkspace,
  revokeAgentPreferenceMemory,
  resolveFlexibleOption,
  runAgentPlanning,
  setApiCredential,
  setProviderSelection,
  startLiveFlexiblePlanningFromTextJob,
  startLiveMonitor,
  stopLiveMonitor,
  summarizeLiveProviderCoverage,
  searchOffers,
  startPlanning,
  subscribeToJob,
  subscribeToLiveFlexiblePlanningJob,
  type TripSpec,
  type Workspace,
} from "./api";
import {
  componentCoverageExplanations,
  priceLabel,
  taskProviderOf,
  taskVerticalOf,
  type PriceState,
} from "./domain";

const stageLabels: Record<string, string> = {
  queued: "任务已入队",
  optimizing: "约束求解中",
  verifying: "逐项校验中",
  complete: "规划完成",
  failed: "规划失败",
};

const liveJobStageLabels: Record<string, string> = {
  queued: "等待实时查询槽位",
  interpreting_requirement: "解析并锁定需求事实",
  searching_live_sources: "多平台并发核价与 Agent 规划",
  blocked_before_live_search: "需求事实不完整，未启动平台查询",
  caching_pair_runs: "保存可重规划方案",
  assembling_result: "汇总预算、证据与边界",
  cancelling: "正在取消并回收浏览器任务",
  cancelled: "已取消",
  complete: "实时规划完成",
  failed: "实时规划失败",
};

const decisionLabels = {
  accept: "接受",
  reject_and_replan: "拒绝并重新规划",
  human_block: "阻塞并转人工",
} as const;

const eventDispositionLabels = {
  no_change: "无需改变",
  refresh: "刷新证据",
  local_repair: "局部修复",
  global_replan: "全局重规划",
  human_block: "阻塞并转人工",
} as const;

const agentRoleLabels: Record<string, string> = {
  query_strategist: "查询策略 Agent",
  candidate_curator: "候选策展 Agent",
  risk_critic: "风险批判 Agent",
  recritic: "修复后风险复审 Agent",
  repair_strategist: "修复策略 Agent",
  event_diagnoser: "事件诊断 Agent",
  orchestrator: "主控 Agent",
  explanation: "解释 Agent",
  memory_curator: "记忆策展 Agent",
};

const preferenceModeLabels: Record<PreferenceMode, string> = {
  required: "必须",
  weighted: "加权",
  forbidden: "禁止",
  indifferent: "无要求",
};

const preferenceApplicationLabels = {
  ranked: "已进入排序",
  not_ranked: "未进入排序",
  hard_constraint: "已进入硬约束",
  not_requested: "无要求，不参与排序",
  unconfirmed: "后端未确认",
} as const;

const providerLabels: Record<LiveProvider, string> = {
  ctrip: "携程",
  qunar: "去哪儿",
  tongcheng: "同程",
  "icom-public-transfer": "iCom 官方快艇",
};

function isLiveProvider(value: string): value is LiveProvider {
  return value in providerLabels;
}

function providerLabel(value: string): string {
  return isLiveProvider(value) ? providerLabels[value] : value;
}

const verticalLabels = {
  flight: "机票",
  lodging: "酒店",
} as const;

const defaultLiveRequirement = `出发地：杭州
目的地：马累
去程：2026-8月
返程：玩5-8天
人数：2名成人
酒店：1间房
偏好：提供几个方案对比一下预算、早餐无要求、星级无要求、无行李、接受中转`;

const factLabels: Record<string, string> = {
  origin: "出发地",
  destination: "目的地",
  earliest_departure: "最早出发",
  latest_departure: "最晚出发",
  min_nights: "最少住宿",
  max_nights: "最多住宿",
  adults: "成人",
  rooms: "房间",
  currency: "比较币种",
  budget_cents: "预算",
  require_checked_baggage: "托运行李",
  require_breakfast: "早餐",
};

function splitTerms(value: string): string[] {
  return value
    .split(/[，,、]/)
    .map((term) => term.trim())
    .filter(Boolean);
}

function formatTime(value: string): string {
  return new Intl.DateTimeFormat("zh-CN", {
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  }).format(new Date(value));
}

function formatMoney(amount: string, currency = "CNY"): string {
  return new Intl.NumberFormat("zh-CN", {
    style: "currency",
    currency,
    maximumFractionDigits: 0,
  }).format(Number(amount));
}

function formatCents(amount: number, currency = "CNY"): string {
  return formatMoney(String(amount / 100), currency);
}

function formatDateTime(value: string): string {
  const parsed = new Date(value);
  if (Number.isNaN(parsed.valueOf())) return value;
  return new Intl.DateTimeFormat("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  }).format(parsed);
}

function asObject(value: JsonValue | undefined): Record<string, JsonValue> | null {
  return value !== null && typeof value === "object" && !Array.isArray(value)
    ? value
    : null;
}

function taskIdentity(taskId: string): { provider: string; vertical: string } {
  if (taskId.includes("public-transfer-icom")) {
    return { provider: "icom-public-transfer", vertical: "transfer" };
  }
  const provider =
    (["ctrip", "qunar", "tongcheng"] as const).find((item) => taskId.includes(item)) ??
    "unknown";
  const vertical = taskId.includes("flight")
    ? "flight"
    : taskId.includes("lodging")
      ? "lodging"
      : "unknown";
  return { provider, vertical };
}

function taskScopeLabel(taskId: string): string {
  if (taskId.includes("public-transfer-icom")) return "官方接驳";
  if (taskId.includes("flight")) return "机票";
  if (taskId.includes("lodging-full")) return "酒店·整段";
  if (taskId.includes("lodging-first")) return "酒店·首晚";
  if (taskId.includes("lodging-middle")) return "酒店·中段";
  if (taskId.includes("lodging-last")) return "酒店·末晚";
  return taskId.includes("lodging") ? "酒店" : "未知源";
}

function taskSnapshotState(
  output: Record<string, JsonValue> | undefined,
  success: boolean | undefined,
): string {
  const snapshot = asObject(output?.snapshot);
  if (typeof snapshot?.state === "string") return snapshot.state;
  if (output?.isolated_failure) return "failed";
  return success ? "succeeded" : "failed";
}

function quoteFreshness(expiresAt: string): { label: string; className: string } {
  const remaining = new Date(expiresAt).valueOf() - Date.now();
  if (!Number.isFinite(remaining)) return { label: "有效期未知", className: "unknown" };
  if (remaining <= 0) return { label: "已过期", className: "expired" };
  const minutes = Math.max(1, Math.ceil(remaining / 60_000));
  return { label: `${minutes} 分钟内有效`, className: "fresh" };
}

function AgenticRuntimePanel({
  summary,
  title = "模型 Agent 运行回执",
}: {
  summary: AgenticRunSummary | null | undefined;
  title?: string;
}) {
  if (!summary) {
    return (
      <div className="agent-runtime-panel unavailable">
        <strong>{title}</strong>
        <p>后端未返回 Agent 运行契约，页面不会推断模型、请求或降级状态。</p>
      </div>
    );
  }
  const fallbackCount = summary.stages.filter((stage) => stage.fallback_used).length;
  const failedCount = summary.stages.filter((stage) => stage.failure).length;
  const metrics = getAgenticMetricsPresentation(summary);
  return (
    <div className={`agent-runtime-panel ${summary.enabled ? "enabled" : "disabled"}`}>
      <div className="agent-runtime-head">
        <div>
          <span>AGENT RUNTIME RECEIPT</span>
          <strong>{title}</strong>
        </div>
        <em>
          {summary.enabled
            ? summary.required
              ? "模型必需模式"
              : "模型增强模式"
            : summary.required
              ? "必需模型未运行"
              : "确定性路径"}
        </em>
      </div>
      <div className="agent-runtime-stats">
        <span>{summary.stage_count} 个 Agent 阶段</span>
        <span>{summary.model_stage_count} 个模型阶段</span>
        <span>{summary.logical_request_count} 轮逻辑请求</span>
        <span>primary {summary.primary_http_attempt_count} 次</span>
        <span>fallback {summary.fallback_http_attempt_count} 次</span>
        <span>{metrics.attempt_evidence_label}</span>
        <span>{metrics.latency_label}</span>
        <span>{metrics.estimated_cost_label}</span>
        <span>{summary.total_token_usage} tokens</span>
        <span>{fallbackCount} 个降级阶段</span>
        <span>{failedCount} 个失败阶段</span>
      </div>
      <p>
        {summary.models.length > 0
          ? `${summary.providers.join("、") || "供应方未回执"} · ${summary.models.join("、")}`
          : "没有模型名称回执；不能声称本轮使用了某个具体模型。"}
      </p>
      {summary.stages.length > 0 && (
        <details className="agent-stage-ledger">
          <summary>查看 {summary.stage_count} 个 Agent 阶段的请求、模型、上下文与工具回执</summary>
          <div>
            {summary.stages.map((stage, index) => {
              const scripted = stage.provider?.toLowerCase() === "scripted";
              return (
                <article
                  className={`${stage.fallback_used ? "fallback" : ""} ${stage.failure ? "failed" : ""}`}
                  key={`${stage.task_id}-${index}`}
                >
                  <header>
                    <strong>{agentRoleLabels[stage.role] ?? stage.role}</strong>
                    <span>
                      {stage.model_called
                        ? `${stage.provider ?? "未知供应方"} / ${stage.model ?? "未知模型"}`
                        : "未调用模型"}
                    </span>
                  </header>
                  <small>{stage.task_id}</small>
                  <div>
                    <span>逻辑请求 {stage.logical_request_count} 轮</span>
                    <span>primary {stage.primary_http_attempt_count}</span>
                    <span>fallback {stage.fallback_http_attempt_count}</span>
                    <span>
                      {scripted ? "client attempts" : "HTTP attempts"} {stage.http_attempt_count}
                      {scripted ? "（非网络）" : ""}
                    </span>
                    <span>延迟 {stage.total_latency_seconds.toFixed(3)} 秒</span>
                    <span>usage 成本 US${stage.estimated_cost_usd.toFixed(6)}</span>
                    <span>输出 {stage.token_usage} tokens</span>
                    <span>
                      上下文 {stage.context_used_tokens}/{stage.context_token_budget}
                    </span>
                    <span>工具观察 {stage.tool_observation_tokens} tokens</span>
                    <span>截断 {stage.truncated_tool_observations} 条</span>
                  </div>
                  <p>
                    工具：{stage.tool_names.join("、") || "无"}
                    {stage.fallback_used ? " · 已使用确定性降级" : ""}
                    {stage.failure ? ` · 失败：${stage.failure}` : ""}
                  </p>
                </article>
              );
            })}
          </div>
        </details>
      )}
      <small className="agent-metrics-boundary">{summary.metrics_boundary}</small>
      <small className="agent-safety-boundary">{summary.safety_boundary}</small>
    </div>
  );
}

function LiveSetupPanel({
  health,
  checking,
  onRefresh,
}: {
  health: LiveBridgeHealth | null;
  checking: boolean;
  onRefresh: () => void;
}) {
  const available = health?.available === true;
  return (
    <div className="live-setup">
      <div className="live-setup-head">
        <div>
          <p className="eyebrow">REAL MULTI-PLATFORM ENTRY</p>
          <h2>{checking ? "正在检查实时入口" : available ? "只读浏览器桥已就绪" : "实时入口尚未接通"}</h2>
        </div>
        <button type="button" onClick={onRefresh} disabled={checking}>
          {checking ? "检查中…" : "重新检查"}
        </button>
      </div>
      <p className={`bridge-message ${available ? "ok" : "unavailable"}`}>
        {health?.message ??
          "选择实时模式后，TripChord 会先检查本地只读浏览器桥，不会自动使用回放报价代替。"}
      </p>
      {!available && (
        <ol className="setup-steps">
          <li><strong>启动实时 API 与本地浏览器桥</strong><span>后端需暴露自然语言灵活规划入口与 /browser-bridge/v1/companions/status。</span></li>
          <li><strong>配对 Chrome Companion</strong><span>仅授予携程、去哪儿、同程当前能力矩阵需要的查询域名。</span></li>
          <li><strong>确认三平台登录状态</strong><span>验证码、登录失效或页面结构变化会被记录为失败，不会伪造成功报价。</span></li>
          <li><strong>回到此页重新检查</strong><span>桥接成功后才允许发起实时规划；下单、支付、优惠券与账号修改始终禁止。</span></li>
        </ol>
      )}
      {available && (
        <div className="setup-ready-note">
          桥服务可达，但尚未代表全部已选平台核价成功。提交后必须以能力矩阵实际生成的源任务、终态覆盖率与报价证据为准。
        </div>
      )}
    </div>
  );
}

function ProviderMatrix() {
  const [capabilities, setCapabilities] = useState<ProviderCapabilitiesResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  async function refresh() {
    setLoading(true);
    setError(null);
    try {
      const next = await fetchProviderCapabilities();
      setCapabilities(next);
    } catch (err) {
      setError(err instanceof Error ? err.message : "读取平台能力矩阵失败");
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    void refresh();
  }, []);

  async function toggleScope(scope: string, enabled: boolean) {
    setError(null);
    try {
      const result = await setProviderSelection(scope, enabled);
      setCapabilities((previous) =>
        previous ? { ...previous, scopes: result.updated } : previous,
      );
    } catch (err) {
      setError(err instanceof Error ? err.message : "更新平台选择失败");
    }
  }

  if (loading) {
    return (
      <div className="provider-matrix">
        <h3>平台 × 垂类能力矩阵</h3>
        <p className="bridge-message">正在读取能力矩阵…</p>
      </div>
    );
  }

  if (!capabilities) {
    return (
      <div className="provider-matrix">
        <h3>平台 × 垂类能力矩阵</h3>
        <p className="bridge-message unavailable">{error ?? "无法读取能力矩阵"}</p>
        <button type="button" onClick={() => void refresh()}>重新读取</button>
      </div>
    );
  }

  const flight = capabilities.scopes.filter((scope) => scope.vertical === "flight");
  const lodging = capabilities.scopes.filter((scope) => scope.vertical === "lodging");

  return (
    <div className="provider-matrix">
      <h3>平台 × 垂类能力矩阵</h3>
      <p className="bridge-message">
        profile {capabilities.profile_version} · 默认勾选全部当前合格项，可逐项关闭；关闭项不会产生浏览器任务、模型工具调用或网络访问。
      </p>
      {error && <p className="bridge-message unavailable">{error}</p>}
      {capabilities.missing_verticals.length > 0 && (
        <p className="bridge-message unavailable">
          下列垂类无合格来源，无法启动搜索：{capabilities.missing_verticals.join("、")}
        </p>
      )}
      <table className="capability-table">
        <thead>
          <tr>
            <th>平台</th>
            <th>垂类</th>
            <th>认证阶段</th>
            <th>适配器版本</th>
            <th>选择</th>
          </tr>
        </thead>
        <tbody>
          {[...flight, ...lodging].map((scope) => (
            <tr key={scope.key}>
              <td>{scope.display_name || scope.provider}</td>
              <td>{verticalLabels[scope.vertical as keyof typeof verticalLabels] ?? scope.vertical}</td>
              <td>
                <span className={`stage-pill ${scope.certification_stage}`}>
                  {scope.certification_stage}
                </span>
              </td>
              <td><code>{scope.adapter_version}</code></td>
              <td>
                <label>
                  <input
                    type="checkbox"
                    checked={scope.user_enabled}
                    disabled={scope.certification_stage !== "certified_active"}
                    onChange={(event) =>
                      void toggleScope(scope.key, event.target.checked)
                    }
                  />
                  {scope.eligible ? "已选" : "未选"}
                </label>
                {scope.exclusion_reason && (
                  <small className="exclusion-reason">{scope.exclusion_reason}</small>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function BreakfastPreferenceEditor({
  mode,
  weight,
  onModeChange,
  onWeightChange,
}: {
  mode: PreferenceMode;
  weight: number;
  onModeChange: (mode: PreferenceMode) => void;
  onWeightChange: (weight: number) => void;
}) {
  const submittedWeight = normalizeBreakfastWeight(mode, weight);
  const weightNote =
    mode === "weighted"
      ? "0 表示几乎不影响取舍，1 表示最高软偏好；是否进入排序以后端回执为准。"
      : mode === "indifferent"
        ? "无要求固定提交权重 0，不参与排序。"
        : "硬约束固定提交规范权重 1，不把它伪装成软排序分。";
  return (
    <div className="preference-editor" aria-label="早餐偏好结构化设置">
      <label>
        早餐偏好四态
        <select
          value={mode}
          onChange={(event) => onModeChange(event.target.value as PreferenceMode)}
        >
          <option value="required">必须</option>
          <option value="weighted">加权</option>
          <option value="forbidden">禁止</option>
          <option value="indifferent">无要求</option>
        </select>
      </label>
      <label>
        <span>
          0–1 权重 <output>{submittedWeight.toFixed(2)}</output>
        </span>
        <input
          type="range"
          min="0"
          max="1"
          step="0.05"
          value={submittedWeight}
          aria-label="早餐偏好权重"
          aria-valuetext={submittedWeight.toFixed(2)}
          onChange={(event) => onWeightChange(Number(event.target.value))}
          disabled={mode !== "weighted"}
        />
        <small className="preference-weight-note">{weightNote}</small>
      </label>
    </div>
  );
}

function factValueLabel(field: string, value: JsonValue): string {
  if (field === "min_nights" || field === "max_nights") return `${String(value)} 晚`;
  if (field === "adults") return `${String(value)} 人`;
  if (field === "rooms") return `${String(value)} 间`;
  if (field === "budget_cents" && typeof value === "number") return formatCents(value);
  if (typeof value === "boolean") return value ? "需要" : "不需要";
  if (value === null) return "无硬性要求";
  if (typeof value === "object") return JSON.stringify(value);
  return String(value);
}

function FlexiblePlanningSummary({
  response,
  selectedDatePairId,
  onSelect,
}: {
  response: LiveFlexibleFromTextResponse;
  selectedDatePairId: string;
  onSelect: (datePairId: string) => void;
}) {
  const interpretation = response.interpretation;
  const window = interpretation.window;
  const run = response.run;
  const recommendableCount =
    run?.ranked_options.filter((option) => option.recommendable).length ?? 0;
  const breakfastApplication = getBreakfastPreferenceApplication(response);
  const pairById = new Map(
    run?.pair_runs.map((pair) => [pair.date_pair.id, pair]) ?? [],
  );

  return (
    <div className="flexible-summary">
      <section
        className={`interpretation-card state-${interpretation.state}`}
        aria-label="自然语言需求解析"
      >
        <div className="interpretation-head">
          <div>
            <p className="eyebrow">DETERMINISTIC-FIRST CONTEXT ENGINE</p>
            <h2>
              {interpretation.state === "ready"
                ? "需求已转成可执行约束"
                : "需要补充关键信息"}
            </h2>
          </div>
          <strong>
            {response.model_enhancement_enabled ? "模型增强已启用" : "模型增强未启用"}
          </strong>
        </div>
        {window && (
          <div className="window-summary">
            <span>{window.origin} → {window.destination}</span>
            <strong>{window.min_nights}–{window.max_nights} 晚</strong>
            <span>{window.adults} 位成人 · {window.rooms} 间房</span>
            <small>
              可选去程 {window.earliest_departure} 至 {window.latest_departure}
            </small>
          </div>
        )}
        {run?.stay_area_search_profile && (
          <div className="stay-area-profile" aria-label="系统生成的住宿搜索区域">
            <div>
              <span>航班门户</span>
              <strong>{run.stay_area_search_profile.gateway_destination}</strong>
            </div>
            <div>
              <span>整段 / 中段住宿</span>
              <strong>
                {run.stay_area_search_profile.destination_island_lodging_search_term}
              </strong>
            </div>
            <div>
              <span>首晚 / 末晚住宿</span>
              <strong>
                {run.stay_area_search_profile.airport_island_lodging_search_term}
              </strong>
            </div>
            <p>{run.stay_area_search_profile.assumption_zh}</p>
          </div>
        )}
        <div className="fact-grid">
          {interpretation.facts.map((fact) => (
            <article key={fact.field}>
              <span>{factLabels[fact.field] ?? fact.field}</span>
              <strong>{factValueLabel(fact.field, fact.value)}</strong>
              <small>{fact.explicit ? "用户明确提供" : "确定性推导/系统默认"}</small>
            </article>
          ))}
        </div>
        <div
          className={`preference-application state-${breakfastApplication.state}`}
          aria-label="早餐偏好后端应用状态"
        >
          <div>
            <span>后端解释</span>
            <strong>
              {breakfastApplication.mode
                ? preferenceModeLabels[breakfastApplication.mode]
                : "未确认模式"}
            </strong>
            <small>
              {breakfastApplication.weight === null
                ? "权重未确认"
                : `权重 ${breakfastApplication.weight.toFixed(2)}`}
            </small>
          </div>
          <div>
            <span>实际应用</span>
            <strong>
              {preferenceApplicationLabels[breakfastApplication.state]}
            </strong>
            <small>以本次后端响应为准</small>
          </div>
          <p>{breakfastApplication.reason}</p>
        </div>
        {(interpretation.unresolved.length > 0 || interpretation.conflicts.length > 0) && (
          <div className="requirement-issues">
            {interpretation.unresolved.map((item, index) => (
              <p
                className={item.critical ? "critical" : ""}
                key={`${item.field}-${index}`}
              >
                <strong>{factLabels[item.field] ?? item.field}</strong>
                <span>{item.reason}</span>
              </p>
            ))}
            {interpretation.conflicts.map((item, index) => (
              <p className="critical" key={`conflict-${item.field}-${index}`}>
                <strong>{factLabels[item.field] ?? item.field}</strong>
                <span>{item.reason}</span>
              </p>
            ))}
          </div>
        )}
        <p className="claim-boundary">{interpretation.claim_boundary}</p>
        <p className="execution-boundary">{response.execution_boundary}</p>
      </section>

      {run && (
        <section className="query-observability" aria-label="查询策略 Agent 与自适应核价轨迹">
          <div className="stage-title">
            <div><span>Q</span><h3>Query Strategist → 自适应精确核价</h3></div>
            <strong>
              {run.query_plan.selected_pair_ids.length}/{run.exploration.universe_size} 个日期对进入精确查询
            </strong>
          </div>
          <div className="query-strategy-grid">
            <article>
              <span>确定性候选宇宙</span>
              <strong>{run.exploration.universe_size} 个日期对</strong>
              <small>{run.exploration.search_metrics.evaluation_note}</small>
            </article>
            <article>
              <span>硬查询预算</span>
              <strong>{run.query_plan.selected_pair_ids.length} 个日期对</strong>
              <small>{run.query_plan.total_task_count} 个平台源任务</small>
            </article>
            <article>
              <span>先验覆盖</span>
              <strong>
                {(Number(run.exploration.search_metrics.prior_coverage) * 100).toFixed(1)}%
              </strong>
              <small>{run.exploration.search_metrics.metric_status}</small>
            </article>
          </div>
          {run.query_strategy ? (
            <div className="query-strategy-proposal">
              <div>
                <span>QUERY STRATEGIST 提案</span>
                <strong>{run.query_strategy.summary}</strong>
              </div>
              <p>
                建议日期对：{run.query_strategy.selected_pair_ids.join("、")} · 模型建议预算 {run.query_strategy.query_budget_pairs} 组
              </p>
              {run.query_strategy.selection_reasons.length > 0 && (
                <ul>
                  {run.query_strategy.selection_reasons.map((reason, index) => (
                    <li key={`${reason}-${index}`}>{reason}</li>
                  ))}
                </ul>
              )}
              <small>停止条件：{run.query_strategy.stop_condition}</small>
              {run.query_strategy.uncertainty_flags.length > 0 && (
                <small>不确定性：{run.query_strategy.uncertainty_flags.join("；")}</small>
              )}
              <em>Agent 只提出候选顺序；白名单、ID 与硬预算仍由控制器校验。</em>
            </div>
          ) : (
            <div className="query-strategy-proposal deterministic">
              <strong>本轮没有 Query Strategist 模型提案</strong>
              <p>控制器使用确定性候选顺序；页面不会把它包装成模型自主选择。</p>
            </div>
          )}
          <AgenticRuntimePanel
            summary={run.query_agentic}
            title="Query Strategist 模型与降级状态"
          />
          <div className="refinement-trace">
            <strong>逐轮核价 / 提前停止轨迹</strong>
            {run.refinement_trace.length > 0 ? (
              <ol>
                {run.refinement_trace.map((step) => {
                  const selectedPair = step.selected_pair_id
                    ? pairById.get(step.selected_pair_id)?.date_pair
                    : null;
                  return (
                    <li className={step.stopped_early ? "stopped" : ""} key={step.round}>
                      <span>第 {step.round} 轮</span>
                      <div>
                        <strong>
                          {step.stopped_early
                            ? "满足停止条件"
                            : selectedPair
                              ? `${selectedPair.departure_date} → ${selectedPair.return_date}`
                              : step.selected_pair_id ?? "未选择新日期对"}
                        </strong>
                        <small>{step.reason}</small>
                      </div>
                      <em>
                        {step.incumbent_total_cents === null
                          ? "暂无可推荐基准价"
                          : `当前最优 ${formatCents(step.incumbent_total_cents)}`}
                        {` · 剩余预算 ${step.remaining_budget_pairs}`}
                      </em>
                    </li>
                  );
                })}
              </ol>
            ) : (
              <p>后端未返回逐轮 refinement trace，不能声称发生过自适应调整。</p>
            )}
          </div>
          {(run.exploration.warnings.length > 0 || run.query_plan.warnings.length > 0) && (
            <p className="sampling-boundary">
              {[...run.exploration.warnings, ...run.query_plan.warnings].join("；")}
            </p>
          )}
        </section>
      )}

      {run && (
        <section className="option-comparison" aria-label="灵活日期预算方案对比">
          <div className="stage-title">
            <div><span>00</span><h3>已核价日期方案与预算对比</h3></div>
            <strong>
              {recommendableCount >= 2
                ? `${recommendableCount} 个可推荐方案`
                : `${recommendableCount} 个可推荐方案 · 证据不足时不凑数`}
            </strong>
          </div>
          <div className="option-grid">
            {run.ranked_options.slice(0, 3).map((option) => {
              const pair = pairById.get(option.date_pair_id);
              const selected = selectedDatePairId === option.date_pair_id;
              return (
                <button
                  type="button"
                  className={`${selected ? "selected" : ""} ${option.recommendable ? "recommendable" : "blocked"}`}
                  onClick={() => onSelect(option.date_pair_id)}
                  key={option.date_pair_id}
                >
                  <span>本轮核价第 {option.rank} 名</span>
                  <strong>
                    {option.total_budget_cents === null
                      ? "未形成安全预算"
                      : formatCents(option.total_budget_cents)}
                  </strong>
                  <p>{option.departure_date} → {option.return_date}</p>
                  <small>
                    {pair?.date_pair.night_count ?? "?"} 晚 ·{" "}
                    {option.all_platforms_complete ? "全部已选平台完整" : "平台覆盖不完整"} ·
                    证据 {(Number(option.evidence_completeness) * 100).toFixed(0)}%
                  </small>
                  <em>{option.recommendable ? "可推荐，查看完整闭环" : "主控未接受"}</em>
                </button>
              );
            })}
          </div>
          <p className="sampling-boundary">
            {run.sampled_not_exhaustive
              ? `这里只比较本轮 ${run.ranked_options.length} 个精确日期对，是抽样结果，不是全月或全网最低价。`
              : "排序只在有完整日历支持并执行精确查询的候选日期对内有效。"}
          </p>
          <p className="claim-boundary">{run.claim_boundary}</p>
        </section>
      )}
    </div>
  );
}

function HandoffActionBar({
  runId,
  componentId,
}: {
  runId: string;
  componentId: string;
}) {
  const [state, setState] = useState<"idle" | "repricing" | "ready" | "error">("idle");
  const [result, setResult] = useState<RepriceComponentResponse | null>(null);
  const [error, setError] = useState("");
  const [consumed, setConsumed] = useState(false);

  async function startReprice() {
    setState("repricing");
    setError("");
    try {
      const response = await repriceComponent(runId, componentId);
      setResult(response);
      setState("ready");
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "重核价失败");
      setState("error");
    }
  }

  async function goToOfficial(handoffId: string, url: string) {
    // Two-step flow: the user explicitly clicks "去官方页面". Opening the
    // official page never creates a booked state and never focuses the tab
    // automatically beyond this one user action.
    const consume = await consumeHandoff(runId, componentId, handoffId).catch(
      () => null,
    );
    if (consume) setConsumed(consume.consumed);
    window.open(url, "_blank", "noopener,noreferrer");
  }

  const checklist = result?.checklist ?? null;
  const handoff = checklist?.official_handoff ?? null;
  const receipt = result?.revalidation_receipt ?? null;
  const canGo = checklist?.suggested_next_step === "go_to_official" && handoff && !consumed;

  return (
    <div className="handoff-actions">
      <button
        type="button"
        className="reprice-btn"
        disabled={state === "repricing"}
        onClick={() => void startReprice()}
      >
        {state === "repricing" ? "重核价中…" : "重核价并查看差异"}
      </button>
      {canGo && (
        <button
          type="button"
          className="official-btn"
          onClick={() => void goToOfficial(handoff.handoff_id, handoff.url)}
        >
          去官方页面
        </button>
      )}
      {result && (
        <small className={`reprice-outcome ${result.outcome}`}>
          {result.outcome === "unchanged" && receipt
            ? `价格未变 · ${formatCents(receipt.total_for_party_cents ?? 0, "CNY")}`
            : result.outcome === "changed"
              ? "价格有变化，旧 handoff 已失效"
              : result.outcome === "not_found"
                ? "该组件暂无法重核价"
                : result.outcome === "live_unavailable"
                  ? "需要授权 Companion 会话才能实时重核价"
                  : result.blocked_reason ?? "无法重核价"}
        </small>
      )}
      {handoff && !canGo && result?.outcome === "unchanged" && (
        <small className="reprice-outcome unchanged">handoff 已使用（单次有效）</small>
      )}
      {error && <small className="reprice-error">{error}</small>}
    </div>
  );
}

function LivePackageConsole({
  run,
  runId,
  expiresAt,
  bridgeHealth,
}: {
  run: LivePackageAgentRun;
  runId: string;
  expiresAt: string;
  bridgeHealth: LiveBridgeHealth | null;
}) {
  const [confirmingMemoryKey, setConfirmingMemoryKey] = useState("");
  const [revokingMemoryKey, setRevokingMemoryKey] = useState("");
  const [confirmedMemoryRecords, setConfirmedMemoryRecords] = useState<Record<string, string>>({});
  const [memoryConfirmationError, setMemoryConfirmationError] = useState("");
  const coverageCount = run.coverage.filter((item) => item.complete).length;
  const resultsByTask = new Map(
    run.scheduler.results.map((result) => [result.task_id, result]),
  );
  const normalizationIssues = run.normalization_results.flatMap((result) =>
    result.issues.map((issue) => ({
      ...issue,
      provider: result.provider,
      kind: result.kind,
    })),
  );
  const packageResult = run.package;
  const initialCandidate = packageResult?.initial_candidate;
  const finalCandidate = packageResult?.final_candidate;
  const evidenceRefs = packageResult?.evidence_refs ?? run.decision.evidence_refs;
  const finalQuotes = finalCandidate
    ? [finalCandidate.flight, ...finalCandidate.lodgings, ...finalCandidate.transfers]
    : [];
  const longTermMemoryCandidates =
    run.memory_candidates?.candidates.filter(
      (candidate) => candidate.scope === "user" && candidate.requires_user_confirmation,
    ) ?? [];

  useEffect(() => {
    setConfirmingMemoryKey("");
    setRevokingMemoryKey("");
    setConfirmedMemoryRecords({});
    setMemoryConfirmationError("");
  }, [runId]);

  async function confirmMemoryCandidate(candidate: (typeof longTermMemoryCandidates)[number]) {
    setConfirmingMemoryKey(candidate.key);
    setMemoryConfirmationError("");
    try {
      const response = await confirmAgentPreferenceMemory({
        key: candidate.key,
        value: candidate.value,
        source_evidence_refs: candidate.source_evidence_refs,
      });
      setConfirmedMemoryRecords((current) => ({
        ...current,
        [candidate.key]: response.record.id,
      }));
    } catch (caught) {
      setMemoryConfirmationError(
        caught instanceof Error ? caught.message : "长期偏好确认失败",
      );
    } finally {
      setConfirmingMemoryKey("");
    }
  }

  async function revokeMemoryCandidate(key: string, recordId: string) {
    setRevokingMemoryKey(key);
    setMemoryConfirmationError("");
    try {
      const response = await revokeAgentPreferenceMemory(recordId);
      if (!response.revoked) throw new Error("后端没有确认撤销长期偏好");
      setConfirmedMemoryRecords((current) => {
        const next = { ...current };
        delete next[key];
        return next;
      });
    } catch (caught) {
      setMemoryConfirmationError(
        caught instanceof Error ? caught.message : "长期偏好撤销失败",
      );
    } finally {
      setRevokingMemoryKey("");
    }
  }

  return (
    <div className="live-console">
      <section className={`live-hero decision-${run.decision.state}`}>
        <div className="live-hero-head">
          <div>
            <p className="eyebrow">REAL MULTI-AGENT RUN · 非回放</p>
            <h2>{decisionLabels[run.decision.state]}</h2>
            <small className="workspace-id">{runId}</small>
          </div>
          <strong className="decision-badge">{run.mode === "strict" ? "严格覆盖" : "降级覆盖"}</strong>
        </div>
        <p>{run.decision.summary}</p>
        <div className="live-summary-grid">
          <article>
            <span>浏览器桥</span>
            <strong className={bridgeHealth?.available ? "good" : "warn"}>
              {bridgeHealth?.available ? "只读桥可达" : "运行后状态未知"}
            </strong>
          </article>
          <article>
            <span>平台完整覆盖</span>
            <strong className={run.all_platforms_complete ? "good" : "warn"}>
              {coverageCount}/3
            </strong>
          </article>
          <article>
            <span>Agent 调度并发上限</span>
            <strong>{run.scheduler.max_parallel_tasks} 路</strong>
          </article>
          <article>
            <span>Agent 墙钟耗时</span>
            <strong>{run.scheduler.wall_time_seconds.toFixed(2)} 秒</strong>
          </article>
        </div>
        <p className="claim-boundary">{run.claim_boundary}</p>
      </section>

      <section className="live-stage">
        <div className="stage-title">
          <div><span>01</span><h3>多平台搜索 Agent 并发核价</h3></div>
          <strong>
            {run.source_task_ids.length + run.public_transfer_task_ids.length} 个源任务
          </strong>
        </div>
        <div className="coverage-grid">
          {run.coverage.map((item) => {
            const presentation = summarizeLiveProviderCoverage(run, item);
            return (
              <article className={item.complete ? "complete" : "incomplete"} key={item.provider}>
                <div>
                  <strong>{providerLabel(item.provider)}</strong>
                  <span>{presentation.capability_label}</span>
                </div>
                <em>
                  {presentation.completed_source_count}/{presentation.expected_source_count}
                </em>
                <small>
                  终态搜索 {presentation.completed_source_count} · 可用精确报价 {presentation.usable_quote_count}
                  {item.flight_outcome_state ? ` · 航班状态 ${item.flight_outcome_state}` : ""}
                </small>
                {item.failure_reasons.length > 0 && (
                  <ul>{item.failure_reasons.map((reason) => <li key={reason}>{reason}</li>)}</ul>
                )}
              </article>
            );
          })}
          {run.public_transfer_coverage && (
            <article
              className={run.public_transfer_coverage.complete ? "complete" : "incomplete"}
            >
              <div>
                <strong>iCom 官方快艇</strong>
                <span>
                  {run.public_transfer_coverage.complete
                    ? "直住与分段方案所需四个日期方向已覆盖"
                    : "官方接驳覆盖不完整"}
                </span>
              </div>
              <em>
                {run.public_transfer_coverage.successful_source_ids.length}/
                {run.public_transfer_coverage.expected_source_ids.length || 4}
              </em>
              <small>{run.public_transfer_coverage.price_boundary}</small>
              {run.public_transfer_coverage.failure_reasons.length > 0 && (
                <ul>
                  {run.public_transfer_coverage.failure_reasons.map((reason) => (
                    <li key={reason}>{reason}</li>
                  ))}
                </ul>
              )}
            </article>
          )}
        </div>
        <div className="task-grid">
          {[...run.source_task_ids, ...run.public_transfer_task_ids].map((taskId) => {
            const result = resultsByTask.get(taskId);
            const identity = taskIdentity(taskId);
            const state = taskSnapshotState(result?.output, result?.success);
            return (
              <article className={`task-card state-${state}`} key={taskId}>
                <div>
                  <strong>
                    {providerLabel(identity.provider)} · {taskScopeLabel(taskId)}
                  </strong>
                  <span>{state}</span>
                </div>
                <small>{result?.summary ?? "未返回任务结果"}</small>
              </article>
            );
          })}
        </div>
      </section>

      <section className="live-stage">
        <div className="stage-title">
          <div><span>02</span><h3>报价归一化与问题隔离</h3></div>
          <strong>
            {run.normalization_results.filter((item) => item.status === "usable").length} 可用 /{" "}
            {run.normalization_results.length} 结果
          </strong>
        </div>
        <div className="normalization-list">
          {run.normalization_results.map((result, index) => (
            <article className={`normalization-row ${result.status}`} key={`${result.provider}-${result.kind}-${index}`}>
              <div>
                <strong>
                  {providerLabel(result.provider)} ·{" "}
                  {verticalLabels[result.kind]}
                </strong>
                <span>{result.status === "usable" ? "口径可比" : result.status === "rejected" ? "已拒绝" : "不可用"}</span>
              </div>
              <small>
                {result.quote
                  ? `${formatCents(result.quote.total_for_party_cents, result.quote.currency)} · 税费${result.quote.taxes_and_fees_included ? "已确认包含" : "未确认"}`
                  : result.issues[0]?.message ?? "未形成可用标准报价"}
              </small>
            </article>
          ))}
        </div>
        {normalizationIssues.length > 0 && (
          <div className="issue-box">
            <strong>归一化问题</strong>
            <ul>
              {normalizationIssues.map((issue, index) => (
                <li key={`${issue.provider}-${issue.kind}-${issue.code}-${index}`}>
                  <b>{issue.code}</b> · {issue.message}
                </li>
              ))}
            </ul>
          </div>
        )}
      </section>

      <section className="live-stage">
        <div className="stage-title">
          <div><span>03</span><h3>Planner → Verifier → Repair → 主控裁决</h3></div>
          <strong>{run.scheduler.graph.tasks.length} 个总任务</strong>
        </div>
        <div className="pipeline-grid">
          <article>
            <span className="pipeline-kicker">PLANNER · 初始候选</span>
            {initialCandidate ? (
              <>
                <strong>{formatCents(initialCandidate.declared_total_cents, initialCandidate.currency)}</strong>
                <p>{initialCandidate.id}</p>
                <small>
                  {initialCandidate.flight.provider} 航班 · {initialCandidate.lodgings.length} 段住宿 ·{" "}
                  {initialCandidate.transfers.length} 段接驳
                </small>
              </>
            ) : (
              <p>没有形成可验证候选，主控只能阻塞。</p>
            )}
          </article>
          <article className={packageResult?.initial_violations.length ? "rejected" : "passed"}>
            <span className="pipeline-kicker">VERIFIER · 硬约束裁决</span>
            <strong>
              {packageResult
                ? packageResult.initial_violations.length > 0
                  ? `拒绝 ${packageResult.initial_violations.length} 项`
                  : "初始候选通过"
                : "未进入候选校验"}
            </strong>
            {packageResult?.initial_violations.length ? (
              <ul>
                {packageResult.initial_violations.map((violation, index) => (
                  <li key={`${violation.code}-${index}`}>
                    <b>{violation.code}</b> · {violation.message}
                  </li>
                ))}
              </ul>
            ) : (
              <small>没有发现日期、税费、衔接、预算或证据硬约束违规。</small>
            )}
          </article>
          <article>
            <span className="pipeline-kicker">REPAIR · 最小改动重规划</span>
            {packageResult?.diff ? (
              <>
                <strong>保留率 {(Number(packageResult.diff.preservation_ratio) * 100).toFixed(0)}%</strong>
                <p>
                  移除 {packageResult.diff.removed_component_ids.length} · 新增{" "}
                  {packageResult.diff.added_component_ids.length} · 保留{" "}
                  {packageResult.diff.preserved_component_ids.length}
                </p>
                <small>{packageResult.diff.before_candidate_id} → {packageResult.diff.after_candidate_id}</small>
              </>
            ) : (
              <p>{packageResult ? "Verifier 未触发组件替换，Repair 无差异。" : "没有可修复候选。"}</p>
            )}
          </article>
          <article className={`orchestrator decision-${run.decision.state}`}>
            <span className="pipeline-kicker">主控 AGENT · 最终裁决</span>
            <strong>{decisionLabels[run.decision.state]}</strong>
            <p>{run.decision.summary}</p>
            {run.decision.violation_codes.length > 0 && (
              <small>{run.decision.violation_codes.join(" · ")}</small>
            )}
          </article>
        </div>
        {packageResult && (
          <div className="decision-sequence">
            <span>裁决链</span>
            {packageResult.decisions.map((decision, index) => (
              <strong className={`decision-${decision.state}`} key={`${decision.state}-${index}`}>
                {index + 1}. {decisionLabels[decision.state]}
              </strong>
            ))}
          </div>
        )}
        <AgenticRuntimePanel summary={run.agentic} title="整包多 Agent 模型与降级状态" />
        {run.explanation && (
          <div className="agent-runtime-proof" aria-label="解释 Agent 输出">
            <div>
              <span>解释 Agent</span>
              <strong>{run.explanation.summary}</strong>
              <small>{run.explanation.uncertainties.join("；") || "未声明额外不确定性"}</small>
              {run.explanation.tradeoffs.length > 0 && (
                <div className="tradeoff-list">
                  <em>取舍说明（仅展示有证据支持的比较）</em>
                  <ul>
                    {run.explanation.tradeoffs.map((tradeoff) => (
                      <li key={tradeoff}>{tradeoff}</li>
                    ))}
                  </ul>
                </div>
              )}
            </div>
          </div>
        )}
        {longTermMemoryCandidates.length > 0 && (
          <div className="memory-confirmation-panel">
            <strong>记忆 Agent 提出的长期偏好（必须由你确认）</strong>
            {longTermMemoryCandidates.map((candidate) => {
              const confirmedRecordId = confirmedMemoryRecords[candidate.key];
              const confirmed = Boolean(confirmedRecordId);
              return (
                <article key={candidate.key}>
                  <div>
                    <span>{candidate.key}</span>
                    <small>{JSON.stringify(candidate.value)}</small>
                  </div>
                  <button
                    type="button"
                    className={confirmed ? "revoke" : ""}
                    disabled={
                      confirmingMemoryKey === candidate.key || revokingMemoryKey === candidate.key
                    }
                    onClick={() =>
                      confirmedRecordId
                        ? void revokeMemoryCandidate(candidate.key, confirmedRecordId)
                        : void confirmMemoryCandidate(candidate)
                    }
                  >
                    {revokingMemoryKey === candidate.key
                      ? "撤销中…"
                      : confirmed
                        ? "撤销长期偏好"
                      : confirmingMemoryKey === candidate.key
                        ? "确认中…"
                        : "确认作为长期偏好"}
                  </button>
                </article>
              );
            })}
            <p className="memory-boundary">
              只有点击确认才会写入当前认证用户的长期偏好；写入后可在这里调用后端 DELETE 接口撤销。不要保存账号凭据、支付信息等敏感内容。
            </p>
            {memoryConfirmationError && <p className="error-banner">{memoryConfirmationError}</p>}
          </div>
        )}
      </section>

      <section className="live-stage">
        <div className="stage-title">
          <div><span>04</span><h3>预算、报价证据与新鲜度</h3></div>
          <strong>结果缓存至 {formatDateTime(expiresAt)}</strong>
        </div>
        {packageResult ? (
          <>
            <div className="budget-output">
              <div>
                <span>
                  {packageResult.budget.adults} 人
                  {packageResult.budget.is_all_in_total
                    ? "整包总预算"
                    : "已确认小计（非全包总价）"}
                </span>
                <strong>
                  {formatCents(
                    packageResult.budget.confirmed_subtotal_cents,
                    packageResult.budget.currency,
                  )}
                </strong>
              </div>
              <p>{packageResult.budget.formula}</p>
              <div className="budget-parts">
                <span>机票 {formatCents(packageResult.budget.flight_cents, packageResult.budget.currency)}</span>
                <span>住宿 {formatCents(packageResult.budget.lodging_cents, packageResult.budget.currency)}</span>
                <span>接驳 {formatCents(packageResult.budget.transfer_cents, packageResult.budget.currency)}</span>
              </div>
              {packageResult.budget.supplemental_published_base_fares.length > 0 && (
                <div className="budget-parts">
                  {packageResult.budget.supplemental_published_base_fares.map((fare) => (
                    <span key={`${fare.currency}-${fare.price_contract_ids.join("-")}`}>
                      另付公开基础价{" "}
                      {formatCents(fare.total_for_party_cents, fare.currency)}
                      {" "}· 税费未知
                    </span>
                  ))}
                  <span>预算合规尚未完全验证</span>
                </div>
              )}
            </div>
            <div className="freshness-grid">
              {finalQuotes.map((quote) => {
                const freshness = quoteFreshness(quote.expires_at);
                return (
                  <article key={quote.id}>
                    <div>
                      <strong>{providerLabel(quote.provider)}</strong>
                      <em className={freshness.className}>{freshness.label}</em>
                    </div>
                    <p>{formatCents(quote.total_for_party_cents, quote.currency)} · {quote.id}</p>
                    <small>采集 {formatDateTime(quote.captured_at)} · 到期 {formatDateTime(quote.expires_at)}</small>
                    <HandoffActionBar runId={runId} componentId={quote.id} />
                  </article>
                );
              })}
            </div>
            <div className="coverage-explanation">
              <div className="stage-title">
                <div><span>04b</span><h3>覆盖来源与逐组件解释</h3></div>
                <strong>Source 终态 · 精确报价 · 可比组件分开统计</strong>
              </div>
              <div className="coverage-stats">
                <span>
                  来源执行终态{" "}
                  <strong>
                    {run.source_execution_completeness.terminal_source_ids.length}/
                    {run.source_execution_completeness.expected_source_ids.length}
                  </strong>
                </span>
                <span>
                  精确报价分段{" "}
                  <strong>
                    {run.exact_quote_comparison_coverage
                      ? `${run.exact_quote_comparison_coverage.segments.filter(
                          (segment) => segment.complete,
                        ).length}/${run.exact_quote_comparison_coverage.segments.length}`
                      : "0/0"}
                  </strong>
                </span>
                <span>
                  跨平台可比组件{" "}
                  <strong>{finalCandidate ? finalQuotes.length : 0}</strong>
                </span>
              </div>
              {run.exact_quote_comparison_coverage && (
                <details className="evidence-ledger">
                  <summary>
                    查看 {run.exact_quote_comparison_coverage.segments.length} 个住宿分段的精确报价比价
                  </summary>
                  <div className="segment-coverage-list">
                    {run.exact_quote_comparison_coverage.segments.map((segment) => (
                      <article key={segment.segment_id}>
                        <div>
                          <strong>{segment.segment_id}</strong>
                          <span
                            className={segment.complete ? "coverage-ok" : "coverage-warn"}
                          >
                            {segment.distinct_exact_quote_provider_count}/
                            {segment.required_distinct_provider_count} 家精确报价
                          </span>
                        </div>
                        <small>
                          {segment.provider_evidence
                            .map(
                              (evidence) =>
                                `${evidence.provider} ${
                                  evidence.inventory_state ?? "无精确报价"
                                }`,
                            )
                            .join(" · ")}
                        </small>
                      </article>
                    ))}
                  </div>
                  <p className="memory-boundary">{run.exact_quote_comparison_coverage.evidence_boundary}</p>
                </details>
              )}
              <div className="component-coverage-grid">
                {componentCoverageExplanations(run).map((explanation) => {
                  const sourceLabel = {
                    exact_quote: "精确报价",
                    comparison_price_only: "仅比较价",
                    bounded_no_exact_quote: "有界未命中",
                    failure_terminal: "失败终态",
                  }[explanation.coverage_source];
                  return (
                    <article key={explanation.component_id}>
                      <div>
                        <strong>{taskProviderOf(explanation.component_id)}</strong>
                        <span>{taskVerticalOf(explanation.component_id)}</span>
                      </div>
                      <em>{sourceLabel}</em>
                      <small>{explanation.component_id}</small>
                      {explanation.failure_terminal_states.length > 0 && (
                        <ul>
                          {explanation.failure_terminal_states.map((reason) => (
                            <li key={reason}>{reason}</li>
                          ))}
                        </ul>
                      )}
                    </article>
                  );
                })}
              </div>
            </div>
            <details className="evidence-ledger">
              <summary>查看 {evidenceRefs.length} 条证据引用</summary>
              <ul>{evidenceRefs.map((ref) => <li key={ref}>{ref}</li>)}</ul>
            </details>
          </>
        ) : (
          <div className="issue-box">
            <strong>没有可输出的整包预算</strong>
            <p>主控已拒绝在覆盖、归一化或候选校验不充分时拼接一个看似完整的数字。</p>
          </div>
        )}
      </section>
    </div>
  );
}

function LiveEventLab({
  run,
  runId,
  onRunAdvanced,
}: {
  run: LivePackageAgentRun;
  runId: string;
  onRunAdvanced: (run: LivePackageAgentRun) => void;
}) {
  const candidate = run.package?.final_candidate;
  const components = candidate
    ? [candidate.flight, ...candidate.lodgings, ...candidate.transfers]
    : [];
  const [targetId, setTargetId] = useState(components[0]?.id ?? "");
  const [eventKind, setEventKind] = useState<"price_changed" | "sold_out">("price_changed");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState("");
  const [eventRun, setEventRun] = useState<LiveEventReplanRun | null>(null);
  const target = components.find((item) => item.id === targetId);

  useEffect(() => {
    if (!components.some((component) => component.id === targetId)) {
      setTargetId(components[0]?.id ?? "");
    }
  }, [components.map((component) => component.id).join("|"), targetId]);

  async function replan() {
    if (!target || !isLiveProvider(target.provider)) {
      setError("该组件没有可审计的专用实时重查适配器");
      return;
    }
    setSubmitting(true);
    setError("");
    setEventRun(null);
    try {
      const response = await replanLivePackage(runId, {
        event: {
          id: `ui-live-event-${crypto.randomUUID()}`,
          kind: eventKind,
          target_component_id: target.id,
          affected_provider: target.provider,
        },
        timeout_seconds: 120,
      });
      setEventRun(response.run);
      onRunAdvanced(advanceLiveRunAfterEvent(run, response.run));
    } catch (caught) {
      setError(
        caught instanceof Error
          ? caught.message
          : "实时事件重规划入口未接通，且不会回退为演示结果。",
      );
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <section className="live-stage live-event-lab">
      <div className="stage-title">
        <div><span>05</span><h3>事件注入与受影响平台精确重查</h3></div>
        <strong>Diagnoser 建议 · 主控决定局部或全局动作</strong>
      </div>
      {components.length === 0 ? (
        <div className="issue-box">
          <strong>当前没有可重规划组件</strong>
          <p>必须先形成并接受一个可验证整包候选，才能注入售罄或价格变化事件。</p>
        </div>
      ) : (
        <>
          <div className="live-event-controls">
            <label>
              受影响组件
              <select value={targetId} onChange={(event) => setTargetId(event.target.value)}>
                {components.map((item) => (
                  <option value={item.id} key={item.id}>
                    {providerLabel(item.provider)} · {item.id}
                  </option>
                ))}
              </select>
            </label>
            <label>
              实时事件
              <select value={eventKind} onChange={(event) => setEventKind(event.target.value as typeof eventKind)}>
                <option value="price_changed">报价变化，重新核价</option>
                <option value="sold_out">库存售罄，寻找替代</option>
              </select>
            </label>
            <button type="button" onClick={() => void replan()} disabled={submitting}>
              {submitting ? "受影响平台重查中…" : "注入事件并动态重规划"}
            </button>
          </div>
          <p className="live-event-boundary">
            将只重新查询 {target ? providerLabel(target.provider) : "受影响平台"} 的对应日期与垂类；不会把旧报价改个数字当成新证据。
          </p>
        </>
      )}
      {error && (
        <div className="error-banner">
          {error}。请确认后端已启用 `/api/v1/agents/live-plans/{runId}/events/replan`。
        </div>
      )}
      {eventRun && (
        <div className={`event-run-result decision-${eventRun.decision.state}`}>
          <div>
            <p className="eyebrow">LIVE EVENT REPLAN · 非回放</p>
            <h4>{decisionLabels[eventRun.decision.state]}</h4>
            <span>{eventRun.source_task_ids.length} 个受影响源 · {eventRun.requeried_providers.map(providerLabel).join("、")}</span>
          </div>
          <p>{eventRun.decision.summary}</p>
          <div className="event-disposition-grid" aria-label="事件诊断建议与实际裁决">
            <article>
              <span>事件诊断 Agent 建议</span>
              <strong>
                {eventRun.event_diagnosis
                  ? eventDispositionLabels[eventRun.event_diagnosis.recommended_disposition]
                  : "未返回模型建议"}
              </strong>
              <small>
                {eventRun.event_diagnosis
                  ? `${eventRun.event_diagnosis.summary} · 置信度 ${(eventRun.event_diagnosis.confidence * 100).toFixed(0)}%`
                  : "主控不会补写不存在的诊断结果"}
              </small>
            </article>
            <article>
              <span>确定性事件解析</span>
              <strong>
                {eventRun.event_resolution
                  ? eventDispositionLabels[eventRun.event_resolution.disposition]
                  : "无解析回执"}
              </strong>
              <small>{eventRun.event_resolution?.reason ?? "没有可展示的事件语义证据"}</small>
            </article>
            <article className="applied">
              <span>主控实际执行</span>
              <strong>
                {eventRun.applied_disposition
                  ? eventDispositionLabels[eventRun.applied_disposition]
                  : "未执行"}
              </strong>
              <small>
                {eventRun.global_run
                  ? "已生成完整的新一代全局运行，后续事件从该版本继续。"
                  : "后续事件从本次局部更新后的候选继续，不再使用原始方案。"}
              </small>
            </article>
          </div>
          {eventRun.event_diagnosis &&
            (eventRun.event_diagnosis.dependencies_to_refresh.length > 0 ||
              eventRun.event_diagnosis.evidence_gaps.length > 0) && (
              <div className="event-diagnosis-details">
                <span>
                  需刷新：{eventRun.event_diagnosis.dependencies_to_refresh.join("、") || "无"}
                </span>
                <span>
                  证据缺口：{eventRun.event_diagnosis.evidence_gaps.join("；") || "无"}
                </span>
              </div>
            )}
          <AgenticRuntimePanel summary={eventRun.agentic} title="Event Diagnoser 模型与降级状态" />
          {eventRun.global_run && (
            <div className="global-run-receipt">
              <div>
                <span>GLOBAL RUN</span>
                <strong>全局重规划已实际执行</strong>
              </div>
              <p>{eventRun.global_run.decision.summary}</p>
              <small>
                新运行包含 {eventRun.global_run.source_task_ids.length + eventRun.global_run.public_transfer_task_ids.length} 个源任务 · {eventRun.global_run.coverage.filter((item) => item.complete).length}/3 平台完成
              </small>
              <AgenticRuntimePanel
                summary={eventRun.global_run.agentic}
                title="全局重规划多 Agent 模型与降级状态"
              />
            </div>
          )}
          {eventRun.package ? (
            <div className="event-result-facts">
              <strong>
                {formatCents(
                  eventRun.package.budget.confirmed_subtotal_cents,
                  eventRun.package.budget.currency,
                )}
                {eventRun.package.budget.is_all_in_total ? "" : " 已确认小计"}
              </strong>
              <span>
                {eventRun.package.diff
                  ? `移除 ${eventRun.package.diff.removed_component_ids.length} / 新增 ${eventRun.package.diff.added_component_ids.length} / 保留率 ${(Number(eventRun.package.diff.preservation_ratio) * 100).toFixed(0)}%`
                  : "复查后无需替换组件"}
              </span>
              <small>{eventRun.package.budget.formula}</small>
            </div>
          ) : (
            <div className="issue-box">
              <strong>事件后没有安全替代方案</strong>
              <p>主控保持阻塞，不输出无证据的新预算。</p>
            </div>
          )}
          <small className="event-claim">{eventRun.claim_boundary}</small>
        </div>
      )}
    </section>
  );
}

function LiveMonitorPanel({
  run,
  runId,
  onRunAdvanced,
}: {
  run: LivePackageAgentRun;
  runId: string;
  onRunAdvanced: (run: LivePackageAgentRun) => void;
}) {
  const [monitor, setMonitor] = useState<LiveMonitorStatus | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const canStart = Boolean(run.package && run.decision.state === "accept");

  async function refreshCurrentRun() {
    const current = await getLivePackage(runId);
    onRunAdvanced(current.run);
  }

  useEffect(() => {
    if (!monitor || monitor.state !== "active") return;
    let cancelled = false;
    const timer = window.setInterval(() => {
      void getLiveMonitor(monitor.id)
        .then(async ({ monitor: next }) => {
          if (cancelled) return;
          const changed = next.check_count > monitor.check_count;
          setMonitor(next);
          if (changed) await refreshCurrentRun();
        })
        .catch((caught) => {
          if (!cancelled) {
            setError(caught instanceof Error ? caught.message : "读取后台核价状态失败");
          }
        });
    }, 5_000);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, [monitor?.id, monitor?.state, monitor?.check_count, runId]);

  async function start() {
    setBusy(true);
    setError("");
    try {
      const response = await startLiveMonitor(runId, {
        interval_seconds: 60,
        max_checks: 120,
        timeout_seconds: 120,
      });
      setMonitor(response.monitor);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "无法开启周期核价");
    } finally {
      setBusy(false);
    }
  }

  async function checkNow() {
    if (!monitor) return;
    setBusy(true);
    setError("");
    try {
      const response = await checkLiveMonitorNow(monitor.id);
      setMonitor(response.monitor);
      await refreshCurrentRun();
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "立即核价失败");
    } finally {
      setBusy(false);
    }
  }

  async function stop() {
    if (!monitor) return;
    setBusy(true);
    setError("");
    try {
      const response = await stopLiveMonitor(monitor.id);
      setMonitor(response.monitor);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "停止周期核价失败");
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="live-stage live-monitor-panel">
      <div className="stage-title">
        <div><span>06</span><h3>后台周期核价与自动重规划</h3></div>
        <strong>显式开启 · 每 1 分钟 · 最多 120 轮</strong>
      </div>
      <p className="live-event-boundary">
        每轮只读重查当前整包的一个组件；发现变化后进入 Event Diagnoser、Repair、ReVerifier 与主控裁决。
        这不是供应商推送、锁价、常驻云任务或自动下单，API 进程重启后需重新开启。
      </p>
      {!monitor ? (
        <button type="button" onClick={() => void start()} disabled={busy || !canStart}>
          {busy ? "正在开启…" : "开启后台周期核价"}
        </button>
      ) : (
        <div className="live-event-controls">
          <div>
            <strong>{monitor.state === "active" ? "监控中" : monitor.state}</strong>
            <small>{monitor.check_count}/{monitor.max_checks} 轮</small>
          </div>
          <button type="button" onClick={() => void checkNow()} disabled={busy || monitor.state !== "active"}>
            {busy ? "核价中…" : "立即重核一次"}
          </button>
          <button type="button" onClick={() => void stop()} disabled={busy || monitor.state !== "active"}>
            停止
          </button>
        </div>
      )}
      {!canStart && !monitor && (
        <small>只有已通过硬验证并被主控接受的整包，才能开启周期核价。</small>
      )}
      {monitor?.last_check && (
        <div className="event-result-facts">
          <strong>第 {monitor.last_check.sequence} 轮 · {monitor.last_check.decision_state}</strong>
          <span>{monitor.last_check.summary}</span>
          <small>{new Date(monitor.last_check.checked_at).toLocaleString()}</small>
        </div>
      )}
      {monitor?.last_error && <div className="error-banner">{monitor.last_error}</div>}
      {error && <div className="error-banner">{error}</div>}
    </section>
  );
}

function App() {
  const [origin, setOrigin] = useState("杭州");
  const [destination, setDestination] = useState("马累");
  const [startDate, setStartDate] = useState("2026-08-12");
  const [endDate, setEndDate] = useState("2026-08-18");
  const [budget, setBudget] = useState("30000");
  const [interests, setInterests] = useState("海岛，浮潜，日落");
  const [mustVisit, setMustVisit] = useState("");
  const [maxActivities, setMaxActivities] = useState(2);
  const [apiKey, setApiKey] = useState(
    () => (typeof window === "undefined" ? "" : sessionStorage.getItem("tripchord-api-key") ?? ""),
  );
  const [planningMode, setPlanningMode] = useState<"replay" | "live">("replay");
  const [liveRequirementText, setLiveRequirementText] = useState(defaultLiveRequirement);
  const [liveMaxPairs, setLiveMaxPairs] = useState(3);
  const [bridgeToken, setBridgeToken] = useState("");
  const [liveBridgeHealth, setLiveBridgeHealth] = useState<LiveBridgeHealth | null>(null);
  const [liveHealthChecking, setLiveHealthChecking] = useState(false);
  const [liveFlexibleResponse, setLiveFlexibleResponse] =
    useState<LiveFlexibleFromTextResponse | null>(null);
  const [livePlanningJob, setLivePlanningJob] =
    useState<LivePlanningJobSnapshot | null>(null);
  const [selectedLiveDatePairId, setSelectedLiveDatePairId] = useState("");
  const [liveRunOverrides, setLiveRunOverrides] =
    useState<Record<string, LivePackageAgentRun>>({});
  const [workspace, setWorkspace] = useState<Workspace | null>(null);
  const [job, setJob] = useState<Job | null>(null);
  const [offers, setOffers] = useState<Offer[]>([]);
  const [selectedVersion, setSelectedVersion] = useState(1);
  const [diff, setDiff] = useState<PlanDiff | null>(null);
  const [error, setError] = useState("");
  const [replanResult, setReplanResult] = useState<ReplanResult | null>(null);
  const [eventKind, setEventKind] = useState("place_closed");
  const [eventTarget, setEventTarget] = useState("");
  const [replanPreference, setReplanPreference] = useState<
    "minimum_change" | "balanced" | "quality_first"
  >("minimum_change");
  const [submitting, setSubmitting] = useState(false);
  const [agentRun, setAgentRun] = useState<AgentRun | null>(null);
  const [agentBoundary, setAgentBoundary] = useState("");
  const [breakfastMode, setBreakfastMode] = useState<PreferenceMode>("indifferent");
  const [breakfastWeight, setBreakfastWeight] = useState(0.7);
  const selectedFlexibleOption = useMemo(
    () =>
      liveFlexibleResponse
        ? resolveFlexibleOption(
            liveFlexibleResponse,
            selectedLiveDatePairId || undefined,
          )
        : null,
    [liveFlexibleResponse, selectedLiveDatePairId],
  );
  const liveRunId = selectedFlexibleOption?.handle?.run_id ?? "";
  const baseLiveRun = selectedFlexibleOption?.pair.run ?? null;
  const liveRun = liveRunId ? liveRunOverrides[liveRunId] ?? baseLiveRun : baseLiveRun;
  const liveRunExpiresAt = selectedFlexibleOption?.handle?.expires_at ?? "";

  const plan = useMemo(
    () => workspace?.plans.find((candidate) => candidate.version === selectedVersion) ?? null,
    [workspace, selectedVersion],
  );
  const groupedItems = useMemo(() => {
    const groups = new Map<string, PlanItem[]>();
    for (const item of plan?.items ?? []) {
      const date = item.starts_at.slice(0, 10);
      groups.set(date, [...(groups.get(date) ?? []), item]);
    }
    return [...groups.entries()];
  }, [plan]);
  const knownCost = useMemo(
    () =>
      (plan?.items ?? []).reduce(
        (sum, item) => sum + (item.cost?.currency === "CNY" ? Number(item.cost.amount) : 0),
        0,
      ),
    [plan],
  );

  useEffect(() => {
    if (!workspace || !job || job.status === "succeeded" || job.status === "failed") return;
    return subscribeToJob(
      workspace.id,
      job.id,
      async (nextJob) => {
        setJob(nextJob);
        if (nextJob.status === "succeeded") {
          const loaded = await loadWorkspace(workspace.id);
          setWorkspace(loaded);
          setSelectedVersion(loaded.plans.at(-1)?.version ?? 1);
          setEventTarget(loaded.plans.at(-1)?.items[0]?.id ?? "");
        }
        if (nextJob.status === "failed") setError(nextJob.error ?? "规划任务失败");
      },
      setError,
    );
  }, [job?.id, workspace?.id]);

  useEffect(() => {
    if (
      !livePlanningJob ||
      ["succeeded", "failed", "cancelled"].includes(livePlanningJob.state)
    ) {
      return;
    }
    return subscribeToLiveFlexiblePlanningJob(
      livePlanningJob.id,
      (nextJob) => {
        setLivePlanningJob(nextJob);
        if (nextJob.state === "succeeded") {
          if (!nextJob.result) {
            setError("实时任务已结束，但没有返回可验证的规划结果。");
            return;
          }
          setLiveFlexibleResponse(nextJob.result);
          setSelectedLiveDatePairId(
            nextJob.result.run?.recommended_option_ids[0] ??
              nextJob.result.run?.ranked_options[0]?.date_pair_id ??
              "",
          );
        } else if (nextJob.state === "failed") {
          setError(nextJob.error ?? "实时多平台规划失败");
        } else if (nextJob.state === "cancelled") {
          setError("实时多平台规划已取消，未生成或发布部分方案。");
        }
      },
      setError,
    );
  }, [livePlanningJob?.id, livePlanningJob?.state]);

  useEffect(() => {
    if (!workspace || selectedVersion <= 1) {
      setDiff(null);
      return;
    }
    let active = true;
    void comparePlans(workspace.id, selectedVersion - 1, selectedVersion)
      .then((comparison) => {
        if (active) setDiff(comparison);
      })
      .catch(() => {
        if (active) setDiff(null);
      });
    return () => {
      active = false;
    };
  }, [selectedVersion, workspace?.id]);

  async function refreshLiveHealth() {
    setLiveHealthChecking(true);
    const health = await checkLiveBridgeHealth(bridgeToken);
    setLiveBridgeHealth(health);
    setLiveHealthChecking(false);
    return health;
  }

  async function handlePlan(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setSubmitting(true);
    setError("");
    setWorkspace(null);
    setSelectedVersion(1);
    setDiff(null);
    setReplanResult(null);
    setAgentRun(null);
    setAgentBoundary("");
    setOffers([]);
    setLiveFlexibleResponse(null);
    setLivePlanningJob(null);
    setSelectedLiveDatePairId("");
    setLiveRunOverrides({});
    setApiCredential(apiKey);
    const spec: TripSpec = {
      origin,
      destinations: [destination],
      start_date: startDate,
      end_date: endDate,
      budget: { amount: budget, currency: "CNY" },
      max_main_activities_per_day: maxActivities,
      interests: splitTerms(interests),
      must_visit: splitTerms(mustVisit),
    };
    try {
      if (planningMode === "live") {
        const health = await refreshLiveHealth();
        requireLiveBridgeAvailability(health);
        const started = await startLiveFlexiblePlanningFromTextJob(
          {
            requirement: {
              text: liveRequirementText,
              trip_id: `ui-live-flexible-${crypto.randomUUID()}`,
              breakfast_mode: breakfastMode,
              breakfast_weight: normalizeBreakfastWeight(breakfastMode, breakfastWeight),
            },
            coverage_mode: "strict",
            timeout_seconds: 120,
            total_timeout_seconds: 900,
            max_pairs: liveMaxPairs,
          },
          `ui-live-job-${crypto.randomUUID()}`,
        );
        setLivePlanningJob(started.job);
        return;
      }
      const [started, foundOffers, agentResponse] = await Promise.all([
        startPlanning(spec),
        searchOffers(spec).catch(() => []),
        runAgentPlanning(spec, {
          mode: breakfastMode,
          weight: normalizeBreakfastWeight(breakfastMode, breakfastWeight),
        }).catch(() => null),
      ]);
      setWorkspace(started.workspace);
      setJob(started.job);
      setOffers(foundOffers);
      setAgentRun(agentResponse?.run ?? null);
      setAgentBoundary(agentResponse?.claim_boundary ?? "");
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "无法启动规划");
    } finally {
      setSubmitting(false);
    }
  }

  async function cancelLivePlanning() {
    if (!livePlanningJob) return;
    setError("");
    try {
      setLivePlanningJob(await cancelLiveFlexiblePlanningJob(livePlanningJob.id));
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "无法取消实时规划任务");
    }
  }

  async function injectEvent() {
    if (!workspace || !eventTarget) return;
    setError("");
    let payload: Record<string, string | number> | undefined;
    if (eventKind === "transport_delayed") payload = { delay_minutes: 60 };
    if (eventKind === "price_changed") payload = { new_amount: "300", currency: "CNY" };
    try {
      const response = await replanWorkspace(workspace.id, {
        targetId: eventTarget,
        kind: eventKind,
        preference: replanPreference,
        payload,
      });
      setWorkspace(response.workspace);
      setReplanResult(response.result);
      setSelectedVersion(response.workspace.plans.at(-1)?.version ?? selectedVersion);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "事件重规划失败");
    }
  }

  return (
    <div className="app-shell">
      <header className="topbar">
        <a className="brand" href="#top" aria-label="TripChord 首页">
          <span className="brand-mark">弦</span>
          <span><strong>TripChord</strong><small>旅弦 · 可验证的自由行规划</small></span>
        </a>
        <div className={`status-pill ${planningMode} ${planningMode === "live" && liveBridgeHealth?.available ? "connected" : ""}`}>
          <span />{" "}
          {planningMode === "replay"
            ? "回放演示 · 证据可追溯"
            : liveBridgeHealth?.available
              ? "本地只读桥已联通"
              : "实时入口未接通"}
        </div>
      </header>

      <main id="top" className="workspace">
        <section className="intro-panel">
          <p className="eyebrow">CONSTRAINT-AWARE TRAVEL PLANNING</p>
          <h1>不是生成旅行文案，<br />而是求解一趟可执行的旅行。</h1>
          <p className="lede">交通、住宿、景点、路线、预算和变化事件进入同一条计划链。每次修改都有来源、校验结果和版本差异。</p>
          <form className="trip-form" onSubmit={handlePlan}>
            <div className="mode-switch" role="tablist" aria-label="数据模式">
              <button
                type="button"
                role="tab"
                aria-selected={planningMode === "replay"}
                className={planningMode === "replay" ? "active" : ""}
                onClick={() => {
                  setPlanningMode("replay");
                  setError("");
                }}
              >
                <strong>回放演示</strong>
                <span>离线数据，可重复评测</span>
              </button>
              <button
                type="button"
                role="tab"
                aria-selected={planningMode === "live"}
                className={planningMode === "live" ? "active live" : ""}
                onClick={() => {
                  setPlanningMode("live");
                  setError("");
                }}
              >
                <strong>真实多平台</strong>
                <span>Chrome 只读查询，不下单</span>
              </button>
            </div>
            {planningMode === "live" ? (
              <>
                <label className="live-requirement-input">
                  用自然语言描述整趟自由行
                  <textarea
                    rows={10}
                    value={liveRequirementText}
                    onChange={(event) => setLiveRequirementText(event.target.value)}
                  />
                  <small>
                    解析器会把“玩 5–8 天”换算为 4–7 晚；缺少目的地、日期、人数或房间时只返回待补充项，不启动平台搜索。
                  </small>
                </label>
                <BreakfastPreferenceEditor
                  mode={breakfastMode}
                  weight={breakfastWeight}
                  onModeChange={setBreakfastMode}
                  onWeightChange={setBreakfastWeight}
                />
                <div className="form-row compact-row">
                  <label>
                    本轮精确核价日期对
                    <select
                      value={liveMaxPairs}
                      onChange={(event) => setLiveMaxPairs(Number(event.target.value))}
                    >
                      <option value={2}>2 组，用于预算对比</option>
                      <option value={3}>3 组，推荐</option>
                      <option value={5}>最多 5 组，自适应提前停止</option>
                      <option value={8}>最多 8 组，深度核价</option>
                    </select>
                  </label>
                  <div className="live-policy-card">
                    <span>覆盖策略</span>
                    <strong>严格覆盖全部已选平台 · 来源数按能力矩阵生成</strong>
                    <small>同程当前只查国际机票；单个平台的必需来源未终态时主控不会伪造完整结果。</small>
                  </div>
                </div>
                <label>
                  本地浏览器桥配对令牌
                  <input
                    type="password"
                    autoComplete="off"
                    value={bridgeToken}
                    onChange={(event) => {
                      setBridgeToken(event.target.value);
                      setLiveBridgeHealth(null);
                    }}
                    placeholder="至少 32 位，仅在当前页面内存中使用"
                  />
                </label>
                <small className="field-note">
                  默认使用确定性优先解析，不调用付费模型。桥健康只代表本地服务可达，不代表全部已选平台核价成功。
                </small>
              </>
            ) : (
              <>
                <div className="form-row">
                  <label>从哪里出发<input value={origin} onChange={(e) => setOrigin(e.target.value)} /></label>
                  <label>去哪里<input value={destination} onChange={(e) => setDestination(e.target.value)} /></label>
                </div>
                <div className="form-row">
                  <label>出发日期<input type="date" value={startDate} onChange={(e) => setStartDate(e.target.value)} /></label>
                  <label>返程日期<input type="date" value={endDate} onChange={(e) => setEndDate(e.target.value)} /></label>
                </div>
                <div className="form-row compact-row">
                  <label>总预算<input value={budget} inputMode="numeric" onChange={(e) => setBudget(e.target.value)} /></label>
                  <label>每天主要景点<input type="number" min="1" max="8" value={maxActivities} onChange={(e) => setMaxActivities(Number(e.target.value))} /></label>
                </div>
                <label>兴趣偏好<input value={interests} onChange={(e) => setInterests(e.target.value)} /></label>
                <label>必须安排<input value={mustVisit} onChange={(e) => setMustVisit(e.target.value)} /></label>
                <BreakfastPreferenceEditor
                  mode={breakfastMode}
                  weight={breakfastWeight}
                  onModeChange={setBreakfastMode}
                  onWeightChange={setBreakfastWeight}
                />
              </>
            )}
            <label>部署 API Key（本地演示可留空）<input type="password" autoComplete="off" value={apiKey} onChange={(e) => setApiKey(e.target.value)} placeholder="仅保存在当前浏览器会话" /></label>
            <button
              type="submit"
              disabled={
                submitting ||
                (livePlanningJob !== null &&
                  !["succeeded", "failed", "cancelled"].includes(livePlanningJob.state))
              }
            >
              {submitting
                ? planningMode === "live"
                  ? "并发核价与规划中…"
                  : "正在创建工作区…"
                : planningMode === "live"
                  ? "启动真实多平台闭环 →"
                  : "开始回放规划 →"}
            </button>
            {planningMode === "live" && livePlanningJob && (
              <div className="live-job-progress" aria-live="polite">
                <div>
                  <strong>
                    {liveJobStageLabels[livePlanningJob.stage] ?? livePlanningJob.stage}
                  </strong>
                  <span>{livePlanningJob.progress}%</span>
                </div>
                <progress max="100" value={livePlanningJob.progress} />
                <small>{livePlanningJob.boundary}</small>
                {!["succeeded", "failed", "cancelled"].includes(
                  livePlanningJob.state,
                ) && (
                  <button type="button" onClick={() => void cancelLivePlanning()}>
                    取消并回收后台查询
                  </button>
                )}
              </div>
            )}
          </form>
          {planningMode === "replay" ? (
            <div className="truth-banner replay"><strong>回放演示数据边界</strong><p>景点、路线与 Agent 轨迹使用明确标注的离线回放场景。这里出现的报价不是可预订实时价，也不会被包装成真实多平台核价。</p></div>
          ) : (
            <div className={`truth-banner live ${liveBridgeHealth?.available ? "connected" : ""}`}><strong>真实模式授权边界</strong><p>只读搜索、筛选并打开报价详情；禁止下单、支付、使用优惠券、修改账号或导出登录凭据。桥可达不等于全部已选平台任务已成功。</p></div>
          )}
        </section>

        <section className="plan-panel" aria-label="规划工作区">
          {error && <div className="error-banner">{error}</div>}
          {planningMode === "live" ? (
            liveFlexibleResponse ? (
              <>
                <FlexiblePlanningSummary
                  response={liveFlexibleResponse}
                  selectedDatePairId={selectedLiveDatePairId}
                  onSelect={setSelectedLiveDatePairId}
                />
                {liveRun ? (
                  <>
                    <LivePackageConsole
                      run={liveRun}
                      runId={liveRunId || "未缓存的日期方案"}
                      expiresAt={liveRunExpiresAt || new Date().toISOString()}
                      bridgeHealth={liveBridgeHealth}
                    />
                    {liveRunId ? (
                      <>
                        <LiveEventLab
                          key={`event-${selectedLiveDatePairId}-${liveRunId}`}
                          run={liveRun}
                          runId={liveRunId}
                          onRunAdvanced={(advancedRun) =>
                            setLiveRunOverrides((current) => ({
                              ...current,
                              [liveRunId]: advancedRun,
                            }))
                          }
                        />
                        <LiveMonitorPanel
                          key={`monitor-${selectedLiveDatePairId}-${liveRunId}`}
                          run={liveRun}
                          runId={liveRunId}
                          onRunAdvanced={(advancedRun) =>
                            setLiveRunOverrides((current) => ({
                              ...current,
                              [liveRunId]: advancedRun,
                            }))
                          }
                        />
                      </>
                    ) : (
                      <div className="issue-box">
                        <strong>该方案没有事件重规划句柄</strong>
                        <p>为避免跨租户或过期状态被复用，本页不会伪造缓存 ID。</p>
                      </div>
                    )}
                  </>
                ) : liveFlexibleResponse.run ? (
                  <div className="issue-box">
                    <strong>所选日期方案未形成可执行运行</strong>
                    <p>查看该方案的失败类型，或选择另一组已经完成的精确日期对。</p>
                  </div>
                ) : null}
              </>
            ) : (
              <>
                <LiveSetupPanel
                  health={liveBridgeHealth}
                  checking={liveHealthChecking}
                  onRefresh={() => void refreshLiveHealth()}
                />
                <ProviderMatrix />
              </>
            )
          ) : !workspace ? (
            <div className="empty-state"><span>⌁</span><h2>等待一组旅行约束</h2><p>提交后可以看到求解进度、报价真实性、逐日安排、版本差异和事件恢复。</p></div>
          ) : (
            <>
              <div className="plan-header">
                <div><p className="eyebrow">PERSISTED WORKSPACE</p><h2>{workspace.title}</h2><small className="workspace-id">{workspace.id}</small></div>
                <div className={`job-badge ${job?.status ?? "queued"}`}><strong>{job?.progress ?? 0}%</strong><span>{stageLabels[job?.stage ?? "queued"] ?? job?.stage}</span></div>
              </div>
              {job && job.status !== "succeeded" && job.status !== "failed" && <div className="progress-track"><span style={{ width: `${job.progress}%` }} /></div>}

              {agentRun && <section className={`agent-console ${agentRun.decision.state}`}><div className="agent-console-head"><div><p className="eyebrow">REPLAY MULTI-AGENT TRACE · 非实时</p><h3>{agentRun.decision.state === "accept" ? "直接接受" : agentRun.decision.state === "accept_with_exception" ? "确认例外后接受" : "重新规划或暂停"}</h3></div><strong>回放 · {agentRun.scheduler.max_parallel_tasks} 路并发</strong></div><p>{agentRun.decision.summary}</p>{agentRun.decision.verifier_violations.length > 0 && <div className="violation-list">{agentRun.decision.verifier_violations.map((item) => <span key={item}>{item}</span>)}</div>}<div className="agent-facts"><span>{agentRun.scheduler.graph.tasks.length} 个动态任务</span><span>{agentRun.evidence.length} 条证据</span><span>{Math.round(agentRun.scheduler.wall_time_seconds * 1000)} ms 回放耗时</span></div><details><summary>查看回放 Agent 轨迹</summary><ol>{agentRun.scheduler.trace.filter((item) => item.kind === "task_finished" || item.kind === "task_spawned").map((item) => <li key={item.sequence}><b>{item.kind === "task_spawned" ? "动态创建" : "完成"}</b> {item.task_id} · {item.agent_role}</li>)}</ol></details><small>{agentBoundary}</small></section>}

              {plan && (
                <>
                  <div className="version-bar"><div><strong>计划版本</strong><span>{workspace.plans.length} 个可追溯版本</span></div><select value={selectedVersion} onChange={(e) => setSelectedVersion(Number(e.target.value))}>{workspace.plans.map((version) => <option key={version.id} value={version.version}>v{version.version} · {version.status}</option>)}</select></div>
                  <div className="facts"><article><span>预算上限</span><strong>{formatMoney(budget)}</strong></article><article><span>已知活动成本</span><strong>{formatMoney(String(knownCost))}</strong></article><article><span>已应用事件</span><strong className="good">{plan.applied_event_ids.length}</strong></article></div>

                  {offers.length > 0 && <div className="offer-grid">{offers.map((offer) => <article className="price-card" key={offer.id}><div><span className="source-logo">{offer.kind === "flight" ? "航" : "住"}</span><p><strong>{offer.title}</strong><small>{offer.source.provider} · {offer.source.mode}</small></p></div><div className="price"><strong>{formatMoney(offer.price.total.amount, offer.price.total.currency)}</strong><span>{priceLabel(offer.price_state as PriceState)}</span></div></article>)}</div>}
                  <p className="truth-note">回放/沙箱报价仅用于可重复评测；确认前必须调用供应商复价或由用户回原平台核验。</p>

                  {diff && <div className="diff-strip"><strong>v{selectedVersion - 1} → v{selectedVersion}</strong><span>新增 {diff.added_item_ids.length}</span><span>移除 {diff.removed_item_ids.length}</span><span>修改 {diff.changed_items.length}</span></div>}

                  {groupedItems.map(([date, items], index) => <div className="day-block" key={date}><div className="day-heading"><span>DAY {index + 1}</span><strong>{date}</strong></div><div className="timeline">{items.map((item) => <article className="timeline-item" key={item.id}><time>{formatTime(item.starts_at)}–{formatTime(item.ends_at)}</time><span className={`timeline-dot ${item.kind}`} /><div><strong>{item.title}</strong><p>{item.location_name ?? "位置待导航确认"} · {item.source_refs[0] ?? "用户偏好项"}</p></div></article>)}</div></div>)}

                  <div className="event-lab"><div><p className="eyebrow">EVENT INJECTION LAB</p><h3>模拟异常，在合格候选中选择恢复策略</h3></div><select value={eventTarget} onChange={(e) => setEventTarget(e.target.value)}>{plan.items.map((item) => <option key={item.id} value={item.id}>{item.title}</option>)}</select><select value={eventKind} onChange={(e) => setEventKind(e.target.value)}><option value="place_closed">临时闭园</option><option value="weather_alert">天气预警</option><option value="transport_delayed">延误 60 分钟</option><option value="price_changed">价格变为 ¥300</option></select><select value={replanPreference} onChange={(e) => setReplanPreference(e.target.value as typeof replanPreference)}><option value="minimum_change">最少改动</option><option value="balanced">平衡策略</option><option value="quality_first">质量优先</option></select><button type="button" onClick={injectEvent}>注入并重规划</button></div>
                  {replanResult && <div className={`replan-result ${replanResult.status}`}><strong>{replanResult.status === "ready" ? `${replanResult.selected_mode === "local" ? "局部修复" : "全局重优化"}完成` : "自动恢复已阻塞"}</strong><p>{replanResult.message}</p><span>未受影响项保留率 {(replanResult.unaffected_preservation_ratio * 100).toFixed(0)}% · {replanResult.candidates.length} 个候选通过策略比较</span></div>}

                  <div className="verification-bar"><div><span>✓</span><p><strong>确定性 Verifier 已检查</strong><small>日期、时间窗、移动间隔、预算、必去项与来源</small></p></div><em>{plan.status}</em></div>
                </>
              )}
            </>
          )}
        </section>
      </main>
    </div>
  );
}

export default App;
