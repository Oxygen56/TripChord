import { FormEvent, useEffect, useMemo, useState } from "react";

import {
  ApiError,
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
  getLiveFlexiblePlanningJob,
  type Job,
  type JsonValue,
  type LiveBridgeHealth,
  type FinalPlanProjection,
  type LiveFlexibleFromTextResponse,
  type LiveFlexibleFromTextInput,
  type LiveMonitorStatus,
  type LivePackageAgentRun,
  type LivePlanningJobSnapshot,
  type LivePlanModificationReceipt,
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
  modifyLivePackage,
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
export { ApiError } from "./api";
import {
  componentCoverageExplanations,
  priceLabel,
  taskProviderOf,
  taskVerticalOf,
  type PriceState,
} from "./domain";

export function livePlanModificationHeading(
  status: LivePlanModificationReceipt["status"],
  affectedScope: LivePlanModificationReceipt["intent"]["affected_scope"],
): string {
  if (status === "modified") return "修改完成";
  if (status === "global_replan") return "已按新日期完整规划";
  if (status === "blocked") {
    return affectedScope === "global"
      ? "修改未完成，原方案保留"
      : "没有安全替代项";
  }
  return "需要把指令说完整";
}

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

const workflowSteps = [
  { key: "requirement", label: "需求", detail: "模式与行程约束" },
  { key: "platform", label: "平台", detail: "能力矩阵与授权" },
  { key: "progress", label: "进度", detail: "搜索与 Agent 调度" },
  { key: "plan", label: "方案", detail: "候选、自然语言修改与预订" },
] as const;

function deriveWorkflowStep(
  planningMode: "replay" | "live",
  workspace: Workspace | null,
  job: Job | null,
  livePlanningJob: LivePlanningJobSnapshot | null,
  liveFlexibleResponse: LiveFlexibleFromTextResponse | null,
): number {
  if (planningMode === "live") {
    if (liveFlexibleResponse) return 3;
    if (
      livePlanningJob &&
      !["succeeded", "failed", "cancelled"].includes(livePlanningJob.state)
    ) {
      return 2;
    }
    return 1;
  }
  if (!workspace) return 0;
  if (job && job.status !== "succeeded" && job.status !== "failed") return 2;
  return 3;
}

function WorkflowSteps({ current }: { current: number }) {
  return (
    <nav className="workflow-steps" aria-label="自由行规划工作流步骤">
      {workflowSteps.map((step, index) => (
        <div
          key={step.key}
          className={`workflow-step ${index === current ? "active" : ""} ${
            index < current ? "done" : ""
          }`}
          aria-current={index === current ? "step" : undefined}
        >
          <span className="workflow-step-num">{index + 1}</span>
          <span className="workflow-step-body">
            <strong>{step.label}</strong>
            <small>{step.detail}</small>
          </span>
        </div>
      ))}
    </nav>
  );
}

const decisionLabels = {
  accept: "接受",
  reject_and_replan: "拒绝并重新规划",
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

const extendedProviderLabels: Record<string, string> = {
  ...providerLabels,
  kaani_official: "Kaani 酒店官网",
  arena_official: "Arena 酒店官网",
};

const airportLabels: Record<string, string> = {
  HGH: "杭州萧山国际机场",
  MLE: "维拉纳国际机场",
  PEK: "北京首都国际机场",
  PKX: "北京大兴国际机场",
  PVG: "上海浦东国际机场",
  SIN: "新加坡樟宜机场",
};

const placeLabels: Record<string, string> = {
  velana_airport: "维拉纳国际机场（MLE）",
  maafushi: "马富施岛",
  hulhumale: "胡鲁马累",
  airport: "机场",
  destination_island: "目的地岛",
  airport_island: "机场岛",
};

function isLiveProvider(value: string): value is LiveProvider {
  return value in providerLabels;
}

function providerLabel(value: string): string {
  return extendedProviderLabels[value] ?? (isLiveProvider(value) ? providerLabels[value] : value);
}

function airportLabel(code: string | null, fallback: string): string {
  if (!code) return fallback;
  return `${airportLabels[code] ?? fallback}（${code}）`;
}

function placeLabel(placeKey: string | null, area: string): string {
  return (placeKey && placeLabels[placeKey]) || placeLabels[area] || area;
}

function roomNameLabel(value: string | null): string {
  if (!value) return "房型待确认";
  if (value === "Deluxe Double Room Seaview + Balcony") {
    return "豪华海景阳台大床房（Deluxe Double Room Seaview + Balcony）";
  }
  return value;
}

function locationEvidenceLabel(value: string): string {
  return value
    .replace(
      "near Maafushi's main ferry jetty; official page states: steps from Maafushi's main ferry jetty",
      "靠近 Maafushi 主渡轮码头（官网描述为步行可达）",
    )
    .replace(/\bbeachfront\b/gi, "海滨位置");
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
    maximumFractionDigits: 2,
  }).format(Number(amount));
}

function formatCents(amount: number, currency = "CNY"): string {
  return formatMoney(String(amount / 100), currency);
}

function formatDateTime(value: string): string {
  const parsed = new Date(value);
  if (Number.isNaN(parsed.valueOf())) return value;
  return new Intl.DateTimeFormat("zh-CN", {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  }).format(parsed);
}

export function formatTravelLocalDateTime(value: string): string {
  const match = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})(?::\d{2}(?:\.\d+)?)?(Z|[+-]\d{2}:\d{2})$/.exec(
    value,
  );
  if (!match) return value;
  const [, year, month, day, hour, minute, offset] = match;
  const timezone = offset === "Z" ? "UTC" : `UTC${offset}`;
  return `${year}年${Number(month)}月${Number(day)}日 ${hour}:${minute}（${timezone}）`;
}

export function modelParticipationLabel(
  response: Pick<
    LiveFlexibleFromTextResponse,
    "model_enhancement_enabled" | "model_trace_count"
  >,
): string {
  if (response.model_trace_count === 0) return "本次未调用模型";
  if (typeof response.model_trace_count === "number") {
    return `本次调用模型 ${response.model_trace_count} 次`;
  }
  return response.model_enhancement_enabled ? "模型能力可用" : "确定性路径";
}

function formatTravelDate(value: string): string {
  const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(value);
  if (!match) return value;
  return `${match[1]}年${Number(match[2])}月${Number(match[3])}日`;
}

function travelNightCount(checkIn: string, checkOut: string): number | null {
  const start = Date.parse(`${checkIn}T00:00:00Z`);
  const end = Date.parse(`${checkOut}T00:00:00Z`);
  if (!Number.isFinite(start) || !Number.isFinite(end) || end <= start) return null;
  return Math.round((end - start) / 86_400_000);
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
      <details className="agent-runtime-collapse">
        <summary>
          查看模型回执与请求统计（{summary.stage_count} 个阶段 ·{" "}
          {summary.logical_request_count} 轮逻辑请求）
        </summary>
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
      </details>
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

function unresolvedItemLabel(value: string): string {
  if (value.startsWith("部分住宿来源未形成合格报价：")) {
    return "部分平台本次没有返回可公平比较的住宿报价。";
  }
  if (value.startsWith("部分已连接来源未返回可用结果：")) {
    return "部分已连接平台本次没有返回可用结果。";
  }
  return value;
}

function priceBasisLabel(
  basis: string,
  partyTotalKnown: boolean,
  partyCount: number,
): string {
  if (!partyTotalKnown) return "页面展示金额，完整同行总价待平台确认";
  if (basis === "comparison_only") {
    return `${partyCount} 人当前页面比较金额，余位与成交价待平台确认`;
  }
  if (basis === "per_person") return "每人价格";
  return `${partyCount} 人合计`;
}

function PlanActionLink({
  url,
  label,
}: {
  url: string | null | undefined;
  label: string;
}) {
  if (!url) return <small className="component-link-missing">平台未提供可用入口</small>;
  return (
    <a className="component-action-link" href={url} target="_blank" rel="noreferrer">
      {label} <span aria-hidden="true">↗</span>
    </a>
  );
}

function PlanInlineActionLink({
  url,
  label,
}: {
  url: string | null | undefined;
  label: string;
}) {
  if (!url) return <span className="trip-inline-link unavailable">入口待确认</span>;
  return (
    <a className="trip-inline-link" href={url} target="_blank" rel="noreferrer">
      {label} <span aria-hidden="true">↗</span>
    </a>
  );
}

export function selectPlanForCard(response: LiveFlexibleFromTextResponse) {
  const plan = response.final_plan ?? response.best_available_plan ?? null;
  return {
    plan,
    isBestAvailable: !response.final_plan && Boolean(response.best_available_plan),
  };
}

function FlightLeg({
  direction,
  flightNumbers,
  departAt,
  arriveAt,
  from,
  to,
  segments,
  groundTransfers,
}: {
  direction: "去程" | "返程";
  flightNumbers: string[];
  departAt: string;
  arriveAt: string;
  from: string;
  to: string;
  segments: NonNullable<FinalPlanProjection["flight"]>["outbound_segments"];
  groundTransfers: NonNullable<FinalPlanProjection["flight"]>["outbound_ground_transfers"];
}) {
  return (
    <div className="flight-leg-card">
      <div className="flight-leg-title">
        <div>
          <span>{direction}</span>
          <strong>{flightNumbers.join(" + ") || "航班号待确认"}</strong>
        </div>
        <small>{from} → {to}</small>
      </div>
      {segments.length > 0 ? (
        <div className="flight-segments">
          {segments.map((segment, index) => (
            <div className="flight-segment" key={`${direction}-${segment.flight_number}-${index}`}>
              <strong>{segment.flight_number}</strong>
              <span>
                {airportLabel(segment.departure_airport_code, segment.departure_airport_code)}
                {" "}{formatTravelLocalDateTime(segment.departure_at)}
              </span>
              <span>
                → {airportLabel(segment.arrival_airport_code, segment.arrival_airport_code)}
                {" "}{formatTravelLocalDateTime(segment.arrival_at)}
              </span>
            </div>
          ))}
          {groundTransfers.map((transfer, index) => (
            <div className="ground-transfer-warning" key={`${direction}-ground-${index}`}>
              <strong>需要换机场</strong>
              <span>
                {airportLabel(transfer.from_airport_code, transfer.from_airport_code)} →
                {" "}{airportLabel(transfer.to_airport_code, transfer.to_airport_code)} ·
                {" "}{transfer.mode} · 预留 {transfer.actual_buffer_minutes} 分钟
              </span>
            </div>
          ))}
        </div>
      ) : (
        <div className="flight-summary-route">
          <span>{from}</span>
          <strong>{formatTravelLocalDateTime(departAt)}</strong>
          <span aria-hidden="true">→</span>
          <span>{to}</span>
          <strong>{formatTravelLocalDateTime(arriveAt)}</strong>
          {flightNumbers.length > 1 && (
            <p>
              本次来源没有可靠返回每段中转机场和分段时间。购买前请在平台页核对中转地点、
              是否换机场、行李直挂和衔接时间。
            </p>
          )}
        </div>
      )}
    </div>
  );
}

function FinalTripCard({
  response,
  plan,
  isBestAvailable,
}: {
  response: LiveFlexibleFromTextResponse;
  plan: FinalPlanProjection;
  isBestAvailable: boolean;
}) {
  const flight = plan.flight;
  const partyCount =
    (plan.party.adults ?? 0) +
    (plan.party.children ?? 0) +
    (plan.party.infants ?? 0);
  const estimatedTotal =
    plan.estimated_total_cny_cents ??
    response.best_available_plan?.estimated_total_cny_cents ??
    null;
  const totalCents = plan.total_budget_cents ?? estimatedTotal;
  const totalIsEstimate = plan.total_budget_cents === null;
  const originLabel = flight
    ? airportLabel(flight.origin_airport_code ?? null, flight.origin)
    : "出发地";
  const destinationLabel = flight
    ? airportLabel(flight.destination_airport_code ?? null, flight.destination)
    : "目的地";
  const lodgingPrices = plan.lodgings.map((lodging) => {
    const sourceComparison = (plan.lodging_source_comparisons ?? []).find(
      (item) =>
        item.provider === lodging.provider &&
        item.property_name === lodging.property_name &&
        item.room_name === lodging.room_name &&
        item.check_in === lodging.check_in &&
        item.check_out === lodging.check_out,
    );
    return lodging.reference_cny_cents ??
      (sourceComparison?.reference_currency === "CNY"
        ? sourceComparison.reference_total_cents
        : null);
  });
  const lodgingTotalCents = lodgingPrices.every((value) => typeof value === "number")
    ? lodgingPrices.reduce((sum, value) => sum + (value ?? 0), 0)
    : null;
  const transferPrices = plan.transfers.map((transfer) => transfer.reference_cny_cents);
  const transferTotalCents = transferPrices.length > 0 &&
    transferPrices.every((value) => typeof value === "number")
    ? transferPrices.reduce<number>((sum, value) => sum + (value ?? 0), 0)
    : null;
  const flightTotalCents = flight?.currency === "CNY"
    ? flight.total_for_party_cents
    : null;
  const knownCostTotal =
    (flightTotalCents ?? 0) + (lodgingTotalCents ?? 0) + (transferTotalCents ?? 0);
  const costSegments = [
    { label: "机票", value: flightTotalCents, className: "flight" },
    { label: "住宿", value: lodgingTotalCents, className: "stay" },
    { label: "接驳", value: transferTotalCents, className: "transfer" },
  ];
  const timelineEvents = [
    ...(plan.transfers ?? []).map((transfer, index) => ({
      kind: "transfer" as const,
      sortAt: transfer.depart_at ?? `${transfer.service_date}T12:00:00`,
      eyebrow: `第 ${index + 1} 段接驳`,
      title: `${placeLabel(transfer.origin_place_key ?? null, transfer.origin_area)} → ${placeLabel(transfer.destination_place_key ?? null, transfer.destination_area)}`,
      detail: transfer.depart_at
        ? `${formatTravelLocalDateTime(transfer.depart_at)}${transfer.arrive_at ? ` → ${formatTravelLocalDateTime(transfer.arrive_at)}` : ""}`
        : formatTravelDate(transfer.service_date),
      price: transfer.reference_cny_cents,
      provider: providerLabel(transfer.provider),
      url: transfer.official_view_url,
    })),
    ...(plan.lodgings ?? []).map((lodging, index) => ({
      kind: "lodging" as const,
      sortAt: `${lodging.check_in}T23:59:00`,
      eyebrow: "住宿",
      title: lodging.property_name,
      detail: `${formatTravelDate(lodging.check_in)} 入住 · ${formatTravelDate(lodging.check_out)} 退房 · ${roomNameLabel(lodging.room_name)}`,
      price: lodgingPrices[index],
      provider: providerLabel(lodging.provider),
      url: lodging.official_view_url,
    })),
  ].sort((left, right) => left.sortAt.localeCompare(right.sortAt));
  const selectedLodging = plan.lodgings[0] ?? null;
  const tripDestinationLabel = selectedLodging
    ? placeLabel(selectedLodging.place_key ?? null, selectedLodging.area)
    : destinationLabel;
  const comparableLodgings = (plan.lodging_source_comparisons ?? []).filter(
    (comparison) =>
      !selectedLodging ||
      (comparison.check_in === selectedLodging.check_in &&
        comparison.check_out === selectedLodging.check_out),
  );
  const transferPurchaseLinks = plan.transfers.filter(
    (transfer, index, transfers) =>
      Boolean(transfer.official_view_url) &&
      transfers.findIndex(
        (candidate) => candidate.official_view_url === transfer.official_view_url,
      ) === index,
  );

  return (
    <section
      className="final-plan-card"
      aria-label={isBestAvailable ? "当前最佳行程候选" : "最终最佳行程"}
    >
      <div className="final-plan-heading">
        <div>
          <p className="eyebrow">TRIPCHORD 行程决策卡</p>
          <h2>{isBestAvailable ? "当前最佳行程候选" : "最终最佳行程"}</h2>
          <p className="final-plan-subtitle">
            交通、住宿和接驳已放在同一张卡里；点击各项入口后由你确认并购买。
          </p>
        </div>
        <strong>
          {isBestAvailable
            ? "航班余位待确认"
            : plan.optimality_status === "optimality_proven"
              ? "本次覆盖范围内总价最优"
              : "当前已验证最优"}
        </strong>
      </div>

      <div className="final-price-hero">
        <div>
          <span>{totalIsEstimate ? "这次同行预计总费用" : "这次同行总费用"}</span>
          <strong>{totalCents === null ? "总价待确认" : formatCents(totalCents)}</strong>
          <small>
            {plan.party.adults ?? 0} 位成人 · {plan.party.children ?? 0} 位儿童 ·
            {" "}{plan.party.infants ?? 0} 位婴儿 · {plan.party.rooms ?? 0} 间房
          </small>
        </div>
        <p>
          {totalIsEstimate
            ? "含当前航班比较金额、住宿人民币参考金额与接驳人民币参考金额；不是锁价。"
            : "按本次来源确认的同币种同行价格汇总。"}
        </p>
      </div>

      <div className="trip-route-hero">
        <div className="trip-route-endpoint">
          <span>出发</span>
          <strong>{originLabel}</strong>
          <small>{flight ? formatTravelLocalDateTime(flight.outbound_depart_at) : "时间待确认"}</small>
        </div>
        <div className="trip-route-line" aria-hidden="true"><i />✈<i /></div>
        <div className="trip-route-endpoint destination">
          <span>目的地</span>
          <strong>{tripDestinationLabel}</strong>
          <small>{flight ? `经 ${destinationLabel} · ${formatTravelLocalDateTime(flight.outbound_arrive_at)} 落地` : "时间待确认"}</small>
        </div>
        <div className="trip-route-return">
          <span>返航</span>
          <strong>{flight ? formatTravelLocalDateTime(flight.return_depart_at) : "时间待确认"}</strong>
          <small>{flight ? `抵达 ${originLabel} · ${formatTravelLocalDateTime(flight.return_arrive_at)}` : "返程时间待确认"}</small>
        </div>
      </div>

      <div className="trip-cost-strip" aria-label="费用组成">
        {costSegments.map((segment) => (
          <div className="trip-cost-segment" key={segment.label}>
            <div className="trip-cost-bar"><i className={segment.className} style={{ width: `${knownCostTotal && segment.value ? Math.max(4, (segment.value / knownCostTotal) * 100) : 0}%` }} /></div>
            <span>{segment.label}</span>
            <strong>{segment.value ? formatCents(segment.value) : "待确认"}</strong>
          </div>
        ))}
      </div>

      <div className="trip-card-main-grid">
        <section className="trip-timeline-panel" aria-label="完整行程时间轴">
          <div className="trip-section-title"><div><span className="eyebrow">按时间顺序</span><h3>完整行程</h3></div><small>{plan.departure_date} 起</small></div>
          <ol className="trip-timeline">
            {flight && (
              <li className="trip-timeline-event flight-event">
                <div className="trip-timeline-dot">✈</div>
                <div className="trip-timeline-content">
                  <span className="timeline-eyebrow">去程航班 · {flight.outbound_flight_numbers.join(" + ") || "航班号待确认"}</span>
                  <strong>{originLabel} → {destinationLabel}</strong>
                  <small>{formatTravelLocalDateTime(flight.outbound_depart_at)} 出发 · {formatTravelLocalDateTime(flight.outbound_arrive_at)} 抵达</small>
                  <PlanInlineActionLink url={flight.official_view_url} label={`${providerLabel(flight.provider)} · 往返 ${flightTotalCents ? formatCents(flightTotalCents) : "价格待确认"}`} />
                </div>
              </li>
            )}
            {timelineEvents.map((event, index) => (
              <li className={`trip-timeline-event ${event.kind}-event`} key={`${event.kind}-${event.sortAt}-${index}`}>
                <div className="trip-timeline-dot">{event.kind === "lodging" ? "⌂" : "↔"}</div>
                <div className="trip-timeline-content">
                  <span className="timeline-eyebrow">{event.eyebrow}</span>
                  <strong>{event.title}</strong>
                  <small>{event.detail}</small>
                  <PlanInlineActionLink url={event.url} label={`${event.provider} · ${event.price ? formatCents(event.price) : "价格待确认"}`} />
                </div>
              </li>
            ))}
            {flight && (
              <li className="trip-timeline-event return-event">
                <div className="trip-timeline-dot">↩</div>
                <div className="trip-timeline-content">
                  <span className="timeline-eyebrow">返程航班 · {flight.return_flight_numbers.join(" + ") || "航班号待确认"}</span>
                  <strong>{destinationLabel} → {originLabel}</strong>
                  <small>{formatTravelLocalDateTime(flight.return_depart_at)} 起飞 · {formatTravelLocalDateTime(flight.return_arrive_at)} 抵达</small>
                  <PlanInlineActionLink url={flight.official_view_url} label={`${providerLabel(flight.provider)} · 已计入往返价`} />
                </div>
              </li>
            )}
          </ol>
        </section>

        <aside className="trip-card-sidebar" aria-label="住宿比较和重新核价入口">
          <div className="trip-sidebar-block selected-stay">
            <span className="eyebrow">{plan.lodgings.length > 1 ? "已选住宿组合" : "已选住宿"}</span>
            {plan.lodgings.length > 0 ? plan.lodgings.map((lodging, index) => (
              <article
                className="selected-stay-item"
                key={`${lodging.provider}:${lodging.property_name}:${lodging.check_in}`}
              >
                <h3>{lodging.property_name}</h3>
                <p>{formatTravelDate(lodging.check_in)} – {formatTravelDate(lodging.check_out)} · {roomNameLabel(lodging.room_name)}</p>
                <PlanInlineActionLink url={lodging.official_view_url} label={`${providerLabel(lodging.provider)} · ${lodgingPrices[index] ? formatCents(lodgingPrices[index]) : "价格待确认"}`} />
              </article>
            )) : <p>暂无已选住宿</p>}
          </div>
          {comparableLodgings.length > 0 && (
            <div className="trip-sidebar-block">
              <span className="eyebrow">同日期价格比较</span>
              <div className="stay-comparison-list">
                {comparableLodgings.slice(0, 3).map((comparison, index) => {
                  const isSelected = Boolean(
                    selectedLodging &&
                    comparison.provider === selectedLodging.provider &&
                    comparison.property_name === selectedLodging.property_name &&
                    comparison.room_name === selectedLodging.room_name,
                  );
                  return (
                  <div className="stay-comparison-row" key={`${comparison.provider}-${comparison.property_name}-${index}`}>
                    <span>
                      {providerLabel(comparison.provider)}
                      <small>{comparison.property_name}</small>
                      <em>{isSelected ? "已选" : comparison.eligible ? "可比较" : "未满足住宿要求"}</em>
                    </span>
                    <strong>{comparison.reference_currency === "CNY" && comparison.reference_total_cents ? formatCents(comparison.reference_total_cents) : "待确认"}</strong>
                  </div>
                  );
                })}
              </div>
            </div>
          )}
          <div className="trip-sidebar-block reprice-block">
            <span className="eyebrow">准备购买前</span>
            <strong>重新核对最新价格与余位</strong>
            <p>打开入口后核对同行人数、税费、房型和取消规则。</p>
            <nav className="reprice-actions" aria-label="分项重新核价入口">
              {flight && (
                <PlanActionLink
                  url={flight.official_view_url}
                  label={`机票 · ${providerLabel(flight.provider)}`}
                />
              )}
              {plan.lodgings.map((lodging) => (
                <PlanActionLink
                  key={`${lodging.provider}:${lodging.property_name}:${lodging.check_in}`}
                  url={lodging.official_view_url}
                  label={`住宿 · ${lodging.property_name}`}
                />
              ))}
              {transferPurchaseLinks.map((transfer) => (
                <PlanActionLink
                  key={`${transfer.provider}:${transfer.official_view_url}`}
                  url={transfer.official_view_url}
                  label={`接驳 · ${providerLabel(transfer.provider)}`}
                />
              ))}
            </nav>
          </div>
        </aside>
      </div>

      <details className="final-trip-details">
        <summary>查看航班、房型和接驳完整信息</summary>
      {flight && (
        <section className="trip-component-card flight-component" aria-label="往返航班详情">
          <div className="trip-component-heading">
            <div>
              <span>往返航班</span>
              <h3>{providerLabel(flight.provider)}</h3>
            </div>
            <div className="component-price">
              <strong>
                {typeof flight.total_for_party_cents === "number"
                  ? formatCents(flight.total_for_party_cents, flight.currency ?? "CNY")
                  : typeof flight.display_amount_cents === "number"
                    ? formatCents(flight.display_amount_cents, flight.currency ?? "CNY")
                    : "价格待确认"}
              </strong>
              <small>
                {priceBasisLabel(
                  flight.price_basis ?? "comparison_only",
                  flight.party_total_known ?? false,
                  partyCount,
                )}
              </small>
            </div>
          </div>
          <div className="flight-leg-grid">
            <FlightLeg
              direction="去程"
              flightNumbers={flight.outbound_flight_numbers}
              departAt={flight.outbound_depart_at}
              arriveAt={flight.outbound_arrive_at}
              from={originLabel}
              to={destinationLabel}
              segments={flight.outbound_segments ?? []}
              groundTransfers={flight.outbound_ground_transfers ?? []}
            />
            <FlightLeg
              direction="返程"
              flightNumbers={flight.return_flight_numbers}
              departAt={flight.return_depart_at}
              arriveAt={flight.return_arrive_at}
              from={destinationLabel}
              to={originLabel}
              segments={flight.return_segments ?? []}
              groundTransfers={flight.return_ground_transfers ?? []}
            />
          </div>
          <div className="component-meta-row">
            <span>
              {flight.taxes_and_fees_included === true ? "页面标注含税" : "税费待平台确认"}
            </span>
            <span>
              {flight.party_availability_confirmed
                ? "查询时同行余位已确认，未锁库存"
                : "同行余位尚未确认，未锁库存"}
            </span>
            {flight.captured_at && <span>采集于 {formatDateTime(flight.captured_at)}</span>}
            <PlanActionLink
              url={flight.official_view_url}
              label={`打开${providerLabel(flight.provider)}确认并购买`}
            />
          </div>
        </section>
      )}

      {plan.lodgings.map((lodging) => {
        const sourceComparison = (plan.lodging_source_comparisons ?? []).find(
          (item) =>
            item.provider === lodging.provider &&
            item.property_name === lodging.property_name &&
            item.room_name === lodging.room_name &&
            item.check_in === lodging.check_in &&
            item.check_out === lodging.check_out,
        );
        const nights = travelNightCount(lodging.check_in, lodging.check_out);
        const cnyPrice =
          lodging.reference_cny_cents ??
          (sourceComparison?.reference_currency === "CNY"
            ? sourceComparison.reference_total_cents
            : null);
        const originalPrice =
          lodging.total_for_party_cents ??
          lodging.display_total_cents ??
          sourceComparison?.total_for_party_cents;
        const originalCurrency = lodging.currency ?? sourceComparison?.currency ?? "CNY";
        const taxesIncluded =
          lodging.taxes_and_fees_included ?? sourceComparison?.taxes_and_fees_included;
        const capturedAt = lodging.captured_at ?? sourceComparison?.captured_at;
        return (
          <section
            className="trip-component-card"
            aria-label={`住宿 ${lodging.property_name}`}
            key={`${lodging.provider}:${lodging.property_name}:${lodging.check_in}`}
          >
            <div className="trip-component-heading">
              <div>
                <span>住宿 · {placeLabel(lodging.place_key ?? null, lodging.area)}</span>
                <h3>{lodging.property_name}</h3>
                <small>{providerLabel(lodging.provider)}</small>
              </div>
              <div className="component-price">
                <strong>
                  {typeof cnyPrice === "number"
                    ? `${originalCurrency === "CNY" ? "" : "约 "}${formatCents(cnyPrice)}`
                    : typeof originalPrice === "number"
                      ? formatCents(originalPrice, originalCurrency)
                      : "价格待确认"}
                </strong>
                <small>
                  {nights === null ? "住宿价格" : `${nights} 晚 · ${lodging.rooms} 间房合计`}
                  {taxesIncluded === true ? " · 含税费" : " · 税费待确认"}
                </small>
              </div>
            </div>
            <div className="lodging-detail-grid">
              <div><span>入住</span><strong>{formatTravelDate(lodging.check_in)}</strong></div>
              <div><span>退房</span><strong>{formatTravelDate(lodging.check_out)}</strong></div>
              <div><span>房型</span><strong>{roomNameLabel(lodging.room_name)}</strong></div>
              <div>
                <span>餐食与取消</span>
                <strong>
                  {lodging.breakfast_included === true ? "含早餐" : "早餐待确认"} ·
                  {" "}{lodging.cancellation_policy ?? "取消规则待确认"}
                </strong>
              </div>
            </div>
            {lodging.location_address && (
              <p className="component-location">地址：{lodging.location_address}</p>
            )}
            {lodging.location_evidence_summary && (
              <p className="component-location">
                位置：{locationEvidenceLabel(lodging.location_evidence_summary)}
              </p>
            )}
            <div className="component-meta-row">
              <span>查询时可用，未锁房</span>
              {originalCurrency !== "CNY" && typeof originalPrice === "number" && (
                <span>
                  官网原价 {formatCents(originalPrice, originalCurrency)}，人民币为参考折算
                </span>
              )}
              {capturedAt && <span>采集于 {formatDateTime(capturedAt)}</span>}
              <PlanActionLink
                url={lodging.official_view_url}
                label={`打开${providerLabel(lodging.provider)}确认并购买`}
              />
            </div>
          </section>
        );
      })}

      {plan.transfers.length > 0 && (
        <section className="trip-component-card" aria-label="行程接驳详情">
          <div className="trip-component-heading">
            <div><span>接驳</span><h3>全部行程接驳</h3></div>
            <div className="component-price">
              <strong>
                {typeof plan.estimated_icom_transfer_cny_cents === "number"
                  ? `往返约 ${formatCents(plan.estimated_icom_transfer_cny_cents)}`
                  : providerLabel(plan.transfers[0].provider)}
              </strong>
              <small>{partyCount} 人全部接驳</small>
            </div>
          </div>
          <div className="transfer-list">
            {plan.transfers.map((transfer, index) => {
              const originalPrice = transfer.total_for_party_cents;
              return (
                <article key={`${transfer.provider}:${transfer.service_date}:${index}`}>
                  <div>
                    <span>第 {index + 1} 段</span>
                    <strong>
                      {placeLabel(transfer.origin_place_key ?? null, transfer.origin_area)} →
                      {" "}{placeLabel(transfer.destination_place_key ?? null, transfer.destination_area)}
                    </strong>
                    <small>
                      {transfer.depart_at
                        ? formatTravelLocalDateTime(transfer.depart_at)
                        : formatTravelDate(transfer.service_date)}
                      {transfer.arrive_at
                        ? ` → ${formatTravelLocalDateTime(transfer.arrive_at)}`
                        : " · 到达时间待确认"}
                    </small>
                  </div>
                  <div className="component-price">
                    <strong>
                      {typeof transfer.reference_cny_cents === "number"
                        ? `约 ${formatCents(transfer.reference_cny_cents)}`
                        : typeof originalPrice === "number" && transfer.currency
                          ? formatCents(originalPrice, transfer.currency)
                          : "价格待确认"}
                    </strong>
                    <small>
                      {partyCount} 人单程基础价
                      {transfer.taxes_and_fees_included === true ? " · 含税" : " · 税费待确认"}
                    </small>
                  </div>
                </article>
              );
            })}
          </div>
          <div className="component-meta-row">
            <span>查询时班次可用，未锁座</span>
            <span>人民币金额按本次参考汇率换算</span>
            {plan.transfers[0].captured_at && (
              <span>采集于 {formatDateTime(plan.transfers[0].captured_at)}</span>
            )}
            <PlanActionLink
              url={plan.transfers[0].official_view_url}
              label={`打开${providerLabel(plan.transfers[0].provider)}确认并购买`}
            />
          </div>
        </section>
      )}
      </details>

      <div className="journey-date-grid">
        <article><span>出发</span><strong>{formatTravelDate(plan.departure_date)}</strong></article>
        {flight && (
          <>
            <article>
              <span>抵达目的地</span>
              <strong>{formatTravelLocalDateTime(flight.outbound_arrive_at)}</strong>
            </article>
            <article>
              <span>返程起飞</span>
              <strong>{formatTravelLocalDateTime(flight.return_depart_at)}</strong>
            </article>
            <article>
              <span>回到 {flight.origin}</span>
              <strong>{formatTravelLocalDateTime(flight.return_arrive_at)}</strong>
            </article>
          </>
        )}
      </div>

      {plan.unresolved_items.length > 0 && (
        <div className="final-plan-unresolved">
          <strong>购买前必须确认</strong>
          <ul>
            {Array.from(new Set(plan.unresolved_items.map(unresolvedItemLabel))).map((item) => (
              <li key={item}>{item}</li>
            ))}
          </ul>
        </div>
      )}
      <p className="final-purchase-boundary">
        价格和余位会变化。打开入口后请再次核对日期、同行人数、航班、房型、税费和取消规则；
        TripChord 只查询、比较和建议，不会替你下单或付款。
      </p>
      <details className="result-boundary-details">
        <summary>查看这份结果的覆盖范围</summary>
        {isBestAvailable && response.best_available_plan && (
          <p>{response.best_available_plan.advisory_note}</p>
        )}
        <p>{plan.claim_boundary}</p>
      </details>
    </section>
  );
}

export function FlexiblePlanningSummary({
  response,
}: {
  response: LiveFlexibleFromTextResponse;
}) {
  if (response.trip_cards && response.trip_cards.length > 0) {
    if (response.trip_cards.length === 1) {
      return <UnifiedTripCard card={response.trip_cards[0]} />;
    }
    return (
      <TripCardsSummary
        cards={response.trip_cards}
        personalization={response.personalization}
      />
    );
  }
  if (response.trip_card) {
    return <UnifiedTripCard card={response.trip_card} />;
  }
  const interpretation = response.interpretation;
  if (!interpretation) {
    return <p className="claim-boundary">当前请求尚未形成可展示的统一方案卡。</p>;
  }
  const window = interpretation.window;
  const run = response.run;
  const { plan: finalPlan, isBestAvailable } = selectPlanForCard(response);
  const breakfastApplication = getBreakfastPreferenceApplication(response);
  const modelParticipation = modelParticipationLabel(response);
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
            <p className="eyebrow">需求解析与约束确认</p>
            <h2>
              {interpretation.state === "ready"
                ? "需求已转成可执行约束"
                : "需要补充关键信息"}
            </h2>
          </div>
          <strong>{modelParticipation}</strong>
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

      {finalPlan ? (
        <FinalTripCard
          response={response}
          plan={finalPlan}
          isBestAvailable={isBestAvailable}
        />
      ) : (
        <section className="issue-box final-plan-missing" aria-label="最终方案不可用">
          <strong>暂未形成唯一最终方案</strong>
          <p>后端没有发布可验证的最终方案，页面不会把诊断候选冒充成推荐结果。</p>
        </section>
      )}

      {run && (
        <details className="diagnostics-disclosure">
          <summary>查看内部诊断：查询覆盖、候选排序与核价轨迹</summary>
          <div className="query-observability" aria-label="查询策略 Agent 与自适应核价轨迹">
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
          </div>
        </details>
      )}

      {run && (
        <details className="diagnostics-disclosure option-diagnostics">
          <summary>查看内部诊断：日期处理与预算记录</summary>
          <section className="option-comparison" aria-label="灵活日期处理记录">
          <div className="stage-title">
            <div><span>00</span><h3>已处理日期与预算记录</h3></div>
            <strong>
              {run.ranked_options.length} 个日期组合已处理（含实际查询与按本轮平台状态跳过）
            </strong>
          </div>
          <div className="option-grid">
            {run.ranked_options.map((option) => {
              const pair = pairById.get(option.date_pair_id);
              return (
                <button
                  type="button"
                  className={`${option.recommendable ? "recommendable" : "blocked"}`}
                  disabled
                  key={option.date_pair_id}
                >
                  <span>
                    {option.complete_cny_party_total ? `本轮结果第 ${option.rank} 名` : "金额不完整"}
                  </span>
                  <strong>
                    {!option.complete_cny_party_total
                      ? "仅已确认小计"
                      : option.total_budget_cents === null
                      ? "未形成安全预算"
                      : formatCents(option.total_budget_cents)}
                  </strong>
                  <p>{option.departure_date} → {option.return_date}</p>
                  <small>
                    {pair?.date_pair.night_count ?? "?"} 晚 ·{" "}
                    {option.all_platforms_complete ? "全部已选平台完整" : "平台覆盖不完整"} ·
                    证据 {(Number(option.evidence_completeness) * 100).toFixed(0)}% ·
                    {option.complete_cny_party_total ? "完整人数总价" : "仅已确认小计，未参与最终价格排序"}
                  </small>
                  {!option.complete_cny_party_total && (
                    <em>金额不完整/仅已确认小计，未参与最终价格排序</em>
                  )}
                  <em>{option.recommendable ? "已通过内部完整性检查" : "未通过内部完整性检查"}</em>
                </button>
              );
            })}
          </div>
          <p className="sampling-boundary">
            {run.sampled_not_exhaustive
              ? `本轮处理了 ${run.ranked_options.length} 个精确日期对，是抽样结果，不是全月或全网最低价。`
              : "排序只在有完整日历支持并完成本轮处理的候选日期对内有效；被跳过的日期不会被当作已获得实时价格。"}
          </p>
          <p className="claim-boundary">{run.claim_boundary}</p>
          </section>
        </details>
      )}
    </div>
  );
}

const representativeLabels: Record<NonNullable<NonNullable<LiveFlexibleFromTextResponse["trip_card"]>["representative_kind"]>, string> = {
  saver: "省钱代表",
  balanced: "均衡代表",
  experience: "体验代表",
  personalized: "个性化代表",
};

function TripCardsSummary({
  cards,
  personalization,
}: {
  cards: NonNullable<LiveFlexibleFromTextResponse["trip_cards"]>;
  personalization: LiveFlexibleFromTextResponse["personalization"];
}) {
  return (
    <section className="final-trip-card" aria-label="多方案旅行决策卡">
      <div className="interpretation-head">
        <div>
          <p className="eyebrow">TripChord 方案对比</p>
          <h2>当前代表方案</h2>
        </div>
        <strong>{cards.length} 个代表方案</strong>
      </div>
      <p className="claim-boundary">
        以下方案都来自同一次查询的当前有界目录；价格、交通耗时和便利性只在这个目录内比较，未锁票或下单。
      </p>
      <div className="trip-card-options">
        {cards.map((card) => (
          <UnifiedTripCard key={`${card.representative_kind ?? "plan"}-${card.title}`} card={card} compact />
        ))}
      </div>
      {personalization && (
        <details className="result-boundary-details">
          <summary>查看个性化决策记录</summary>
          <p>{personalization.boundary}</p>
          {personalization.agent_runs.some((run) => run.applied) && (
            <p>参与的 Agent：{personalization.agent_runs.filter((run) => run.applied).map((run) => run.role).join("、")}</p>
          )}
          {personalization.skill_applications.some((skill) => skill.applicable) && (
            <p>应用的 Skill：{personalization.skill_applications.filter((skill) => skill.applicable).map((skill) => skill.skill_id).join("、")}</p>
          )}
        </details>
      )}
    </section>
  );
}

export function UnifiedTripCard({ card, compact = false }: { card: NonNullable<LiveFlexibleFromTextResponse["trip_card"]>; compact?: boolean }) {
  const money = (value: number) =>
    `¥${(value / 100).toLocaleString("zh-CN", { minimumFractionDigits: 2 })}`;
  const travelerNames = new Map(card.travelers.map((item) => [item.id, item.name]));
  const participantLabel = (participantIds: string[]) =>
    participantIds.map((item) => travelerNames.get(item) || item).join("、");
  return (
    <section className={compact ? "final-trip-card trip-card-option" : "final-trip-card"} aria-label="统一旅行方案卡">
      <div className="interpretation-head"><div><p className="eyebrow">TripChord 方案</p><h2>{card.title}</h2></div><strong>{card.status === "final" ? "最终方案" : card.status === "candidate" ? "当前候选" : card.status === "no_solution" ? "暂无可行方案" : "等待来源"}</strong></div>
      {(card.representative_kind || card.selection_reason || card.decision_metrics) && (
        <div className="trip-card-decision" aria-label="方案选择依据">
          <strong>{card.representative_kind ? representativeLabels[card.representative_kind] : "方案选择依据"}</strong>
          {card.selection_reason && <p>选择理由：{card.selection_reason}</p>}
          {card.decision_metrics && (
            <p>
              目录内指标：{money(card.decision_metrics.total_cny_cents)} · 交通耗时 {card.decision_metrics.transport_duration_minutes} 分钟 ·
              不便利 {card.decision_metrics.schedule_inconvenience_minutes} 分钟 · 换乘 {card.decision_metrics.transfer_count} 次
            </p>
          )}
          {(card.participating_agent_roles?.length || card.applied_skill_ids?.length) ? (
            <p>
              {card.participating_agent_roles?.length ? `Agent：${card.participating_agent_roles.join("、")}` : ""}
              {card.participating_agent_roles?.length && card.applied_skill_ids?.length ? " · " : ""}
              {card.applied_skill_ids?.length ? `Skill：${card.applied_skill_ids.join("、")}` : ""}
            </p>
          ) : null}
        </div>
      )}
      <div className="window-summary"><strong>{card.total_cny_cents === null ? "当前没有可用总价" : money(card.total_cny_cents)}</strong><span>{card.start_date} 至 {card.end_date} · {card.city_order.join(" → ")} · {card.traveler_count} 人</span></div>
      <div className="complex-trip-components">
        {card.components.map((item) => <article key={item.offer_id}><strong>{item.label}</strong><span>{item.place_from || ""}{item.place_to ? ` → ${item.place_to}` : ""} · {item.start} → {item.end}</span>{item.participant_ids.length > 0 && <small>同行者：{participantLabel(item.participant_ids)}</small>}<small>{item.provider} · {item.price_cny_cents !== null ? money(item.price_cny_cents) : item.shared_price_contract ? "费用包含在共享组合价中" : "费用未提供"}</small>{item.detail_url && <PlanInlineActionLink url={item.detail_url} label="查看来源" />}</article>)}
      </div>
      {card.shared_components.length > 0 && <div className="final-plan-unresolved"><strong>共同安排</strong>{card.shared_components.map((item) => <p key={item.offer_id}>{item.label} · {participantLabel(item.participant_ids)} · {item.price_cny_cents === null ? "共享合同计价" : money(item.price_cny_cents)}</p>)}</div>}
      {card.traveler_itineraries.length > 1 && <div className="final-plan-unresolved"><strong>每人行程</strong>{card.traveler_itineraries.map((traveler) => <div key={traveler.traveler_id}><p><b>{traveler.traveler_name}</b> · 从{traveler.origin.name}出发</p>{traveler.components.map((item) => <p key={`${traveler.traveler_id}:${item.offer_id}`}>{item.start} · {item.label}</p>)}</div>)}</div>}
      {card.traveler_costs.length > 0 && card.total_cny_cents !== null && <div className="final-plan-unresolved"><strong>费用对账</strong><p>共享费用：{money(card.shared_cost_cny_cents)}</p>{card.traveler_costs.map((item) => <p key={item.traveler_id}>{item.traveler_name}：个人费用 {money(item.direct_cny_cents)} + 分摊共享费用 {money(item.allocated_shared_cny_cents)} = {money(item.attributable_total_cny_cents)}</p>)}<small>费用归属仅用于展示对账，不代表实际结算方式。</small></div>}
      {card.fixed_activities.length > 0 && <div className="final-plan-unresolved"><strong>固定活动</strong>{card.fixed_activities.map((item) => <p key={item.offer_id}>{item.label} · {item.start} → {item.end}{item.participant_ids.length > 0 ? ` · ${participantLabel(item.participant_ids)}` : ""} · {item.price_cny_cents === null ? "费用未提供" : money(item.price_cny_cents)}</p>)}</div>}
      <div className="final-plan-unresolved"><strong>来源状态</strong>{card.source_statuses.map((item) => <p key={item.source_id}>{item.provider} · {item.state} · {item.detail}</p>)}<small>查询时间：{card.query_captured_at}</small></div>
      {card.unresolved_items.map((item) => <p className="claim-boundary" key={item}>{item}</p>)}
      <p className="claim-boundary">{card.source_boundary}</p>
    </section>
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
      <div className="handoff-status" aria-live="polite">
        {result && (
          <small className={`reprice-outcome ${result.outcome}`} role="status">
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
          <small className="reprice-outcome unchanged" role="status">
            handoff 已使用（单次有效）
          </small>
        )}
        {error && (
          <small className="reprice-error" role="alert">
            {error}
          </small>
        )}
      </div>
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
        <details className="pipeline-collapse">
          <summary>展开查看 Agent 流水线（Planner → Verifier → Repair → 主控）</summary>
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
        </details>
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

function LivePlanModifier({
  run,
  runId,
  onRunAdvanced,
  onFinalPlanChanged,
}: {
  run: LivePackageAgentRun;
  runId: string;
  onRunAdvanced: (run: LivePackageAgentRun) => void;
  onFinalPlanChanged: (finalPlan: FinalPlanProjection | null) => void;
}) {
  const candidate = run.package?.final_candidate;
  const [instruction, setInstruction] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState("");
  const [receipt, setReceipt] = useState<LivePlanModificationReceipt | null>(null);

  async function modifyPlan(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const requested = instruction.trim();
    if (!requested) {
      setError("请先用一句话说明你想怎么改");
      return;
    }
    setSubmitting(true);
    setError("");
    setReceipt(null);
    try {
      const response = await modifyLivePackage(runId, {
        instruction: requested,
        timeout_seconds: 120,
      });
      setReceipt(response.modification);
      onFinalPlanChanged(response.final_plan ?? null);
      onRunAdvanced(response.run);
    } catch (caught) {
      setError(
        caught instanceof Error
          ? caught.message
          : "这次修改没有完成，原方案保持不变。",
      );
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <section className="live-stage live-plan-modifier">
      <div className="stage-title">
        <div><span>05</span><h3>告诉 TripChord 你想怎么改</h3></div>
        <strong>未提及的部分默认保留</strong>
      </div>
      {!candidate ? (
        <div className="issue-box">
          <strong>当前没有可修改的完整方案</strong>
          <p>TripChord 不会在缺少完整候选时猜测要保留哪些航班、住宿或接驳。</p>
        </div>
      ) : (
        <form className="live-modification-form" onSubmit={(event) => void modifyPlan(event)}>
          <label htmlFor={`modify-${runId}`}>一句话修改当前方案</label>
          <div>
            <textarea
              id={`modify-${runId}`}
              value={instruction}
              onChange={(event) => setInstruction(event.target.value)}
              placeholder="例如：酒店换成海景房，航班和接驳保持不变"
              rows={3}
              maxLength={2000}
              disabled={submitting}
            />
            <button type="submit" disabled={submitting || !instruction.trim()}>
              {submitting ? "正在只重查受影响部分…" : "按这句话修改"}
            </button>
          </div>
          <p>
            住宿修改只刷新同日期、同地点、同人数的住宿来源；航班和接驳不会重查。
            同时改全套日期时，系统会明确升级为完整规划。TripChord 永不下单或付款。
          </p>
        </form>
      )}
      {error && (
        <div className="error-banner">{error}</div>
      )}
      {receipt && (
        <div
          className={`modification-result status-${receipt.status}`}
          role="status"
          aria-live="polite"
        >
          <header>
            <span>
              {livePlanModificationHeading(
                receipt.status,
                receipt.intent.affected_scope,
              )}
            </span>
            <strong>{receipt.summary}</strong>
          </header>
          <div className="modification-facts">
            <article>
              <span>改了什么</span>
              <strong>
                {receipt.status === "modified"
                  ? `仅住宿 · ${receipt.changed_component_ids.length} 个新旧组件`
                  : receipt.status === "global_replan"
                    ? "日期变化 · 完整重新规划"
                    : "原方案未改动"}
              </strong>
            </article>
            <article>
              <span>保留什么</span>
              <strong>
                {receipt.intent.preserve_scopes.includes("flight") &&
                receipt.intent.preserve_scopes.includes("transfer")
                  ? `航班与 ${candidate?.transfers.length ?? 0} 段接驳`
                  : receipt.preserved_component_ids.length > 0
                    ? `${receipt.preserved_component_ids.length} 个原组件`
                    : "完整规划不承诺保留旧组件"}
              </strong>
            </article>
            <article>
              <span>新同行人民币已确认小计</span>
              <strong>
                {receipt.after_confirmed_cny_cents === null
                  ? "尚未形成"
                  : formatCents(receipt.after_confirmed_cny_cents, "CNY")}
              </strong>
            </article>
            <article>
              <span>与修改前相比</span>
              <strong>
                {receipt.difference_cny_cents === null
                  ? "不可比较"
                  : receipt.difference_cny_cents === 0
                    ? "金额未变"
                    : `${receipt.difference_cny_cents > 0 ? "+" : "−"}${formatCents(
                        Math.abs(receipt.difference_cny_cents),
                        "CNY",
                      )}`}
              </strong>
            </article>
          </div>
          {receipt.source_outcomes.length > 0 && (
            <details className="modification-sources">
              <summary>查看本次住宿来源结果</summary>
              <ul>
                {receipt.source_outcomes.map((outcome) => (
                  <li key={`${outcome.provider}-${outcome.state}`}>
                    <strong>{providerLabel(outcome.provider)}</strong>
                    <span>
                      {outcome.state === "succeeded" ||
                      outcome.state === "historical_replay" ||
                      outcome.state === "quote_found"
                        ? `观察到 ${outcome.quote_count} 条，合格 ${outcome.eligible_quote_count} 条`
                        : `未取得可用结果：${outcome.detail ?? outcome.state}`}
                    </span>
                  </li>
                ))}
              </ul>
            </details>
          )}
          <small className="modification-boundary">{receipt.boundary}</small>
        </div>
      )}
    </section>
  );
}

function LiveMonitorPanel({
  run,
  runId,
  onRunAdvanced,
  onFinalPlanChanged,
}: {
  run: LivePackageAgentRun;
  runId: string;
  onRunAdvanced: (run: LivePackageAgentRun) => void;
  onFinalPlanChanged: (finalPlan: FinalPlanProjection | null) => void;
}) {
  const [monitor, setMonitor] = useState<LiveMonitorStatus | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const canStart = Boolean(run.package && run.decision.state === "accept");

  async function refreshCurrentRun() {
    const current = await getLivePackage(runId);
    onRunAdvanced(current.run);
    onFinalPlanChanged(current.final_plan ?? null);
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
        <div className="event-result-facts" role="status" aria-live="polite">
          <strong>第 {monitor.last_check.sequence} 轮 · {monitor.last_check.decision_state}</strong>
          <span>{monitor.last_check.summary}</span>
          <small>{new Date(monitor.last_check.checked_at).toLocaleString()}</small>
        </div>
      )}
      {monitor?.last_error && (
        <div className="error-banner" role="alert">
          {monitor.last_error}
        </div>
      )}
      {error && (
        <div className="error-banner" role="alert">
          {error}
        </div>
      )}
    </section>
  );
}

type LiveSubmissionIdentity = {
  tripId: string;
  idempotencyKey: string;
  storageKey: string;
  jobId?: string;
};

export function canonicalLiveInput(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(canonicalLiveInput);
  if (value !== null && typeof value === "object") {
    return Object.fromEntries(
      Object.entries(value as Record<string, unknown>)
        .sort(([left], [right]) => left.localeCompare(right))
        .map(([key, item]) => [key, canonicalLiveInput(item)]),
    );
  }
  return value;
}

export async function liveSubmissionIdentity(
  input: LiveFlexibleFromTextInput,
): Promise<LiveSubmissionIdentity> {
  const identityInput = {
    ...input,
    requirement: { ...input.requirement, trip_id: "pending" },
  };
  const normalized = JSON.stringify(canonicalLiveInput(identityInput));
  const digestBytes = await crypto.subtle.digest(
    "SHA-256",
    new TextEncoder().encode(normalized),
  );
  const digest = Array.from(new Uint8Array(digestBytes), (byte) =>
    byte.toString(16).padStart(2, "0"),
  ).join("");
  const storageKey = `tripchord-live-submission:${digest}`;
  const existing = window.localStorage.getItem(storageKey);
  if (existing) {
    try {
      const parsed = JSON.parse(existing) as LiveSubmissionIdentity;
      if (parsed.tripId && parsed.idempotencyKey) {
        return { ...parsed, storageKey };
      }
    } catch {
      // Replace malformed local state with a fresh active identity.
    }
  }
  const identity = {
    tripId: `ui-live-flexible-${crypto.randomUUID()}`,
    idempotencyKey: `live-${crypto.randomUUID()}`,
    storageKey,
  };
  window.localStorage.setItem(storageKey, JSON.stringify(identity));
  return identity;
}

export function clearLiveSubmissionIdentity(storageKey: string | null): void {
  if (storageKey) window.localStorage.removeItem(storageKey);
}

export function shouldClearLiveSubmissionIdentity(error: unknown): boolean {
  return error instanceof ApiError && [404, 410].includes(error.status ?? 0);
}

export type LiveJobApplication = {
  terminal: boolean;
  clearIdentity: boolean;
  response: LiveFlexibleFromTextResponse | null;
  selectedDatePairId: string;
  error: string | null;
};

export function applyLivePlanningJob(job: LivePlanningJobSnapshot): LiveJobApplication {
  if (job.state === "succeeded") {
    if (!job.result) {
      return {
        terminal: true,
        clearIdentity: true,
        response: null,
        selectedDatePairId: "",
        error: "实时任务已结束，但没有返回可验证的规划结果。",
      };
    }
    return {
      terminal: true,
      clearIdentity: true,
      response: job.result,
      selectedDatePairId:
        job.result.final_plan?.date_pair_id ??
        job.result.run?.recommended_option_ids[0] ??
        job.result.run?.ranked_options[0]?.date_pair_id ??
        "",
      error: null,
    };
  }
  if (job.state === "failed") {
    return { terminal: true, clearIdentity: true, response: null, selectedDatePairId: "", error: job.error ?? "实时多平台规划失败" };
  }
  if (job.state === "cancelled") {
    return { terminal: true, clearIdentity: true, response: null, selectedDatePairId: "", error: "实时多平台规划已取消，未生成或发布部分方案。" };
  }
  return { terminal: false, clearIdentity: false, response: null, selectedDatePairId: "", error: null };
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
  const liveMaxPairs = 400;
  const [bridgeToken, setBridgeToken] = useState("");
  const [liveBridgeHealth, setLiveBridgeHealth] = useState<LiveBridgeHealth | null>(null);
  const [liveSubmissionStorageKey, setLiveSubmissionStorageKey] = useState<string | null>(null);
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

  useEffect(() => {
    let cancelled = false;
    const restore = async () => {
      const prefix = "tripchord-live-submission:";
      const records: Array<[string, LiveSubmissionIdentity]> = [];
      for (let index = 0; index < window.localStorage.length; index += 1) {
        const key = window.localStorage.key(index);
        if (!key?.startsWith(prefix)) continue;
        try {
          const value = JSON.parse(window.localStorage.getItem(key) ?? "null") as LiveSubmissionIdentity;
          if (value.jobId) records.push([key, value]);
        } catch {
          window.localStorage.removeItem(key);
        }
      }
      const active = records.at(-1);
      if (!active) return;
      try {
        const restored = await getLiveFlexiblePlanningJob(active[1].jobId as string);
        if (cancelled) return;
        setPlanningMode("live");
        setLiveSubmissionStorageKey(active[0]);
        setLivePlanningJob(restored);
        const applied = applyLivePlanningJob(restored);
        if (applied.terminal) {
          clearLiveSubmissionIdentity(active[0]);
          if (applied.response) setLiveFlexibleResponse(applied.response);
          setSelectedLiveDatePairId(applied.selectedDatePairId);
          if (applied.error) setError(applied.error);
          return;
        }
      } catch (caught) {
        if (shouldClearLiveSubmissionIdentity(caught)) {
          clearLiveSubmissionIdentity(active[0]);
        }
      }
    };
    void restore();
    return () => {
      cancelled = true;
    };
  }, []);
  const selectedFlexibleOption = useMemo(
    () =>
      liveFlexibleResponse?.final_plan
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
        const applied = applyLivePlanningJob(nextJob);
        if (applied.clearIdentity) {
          clearLiveSubmissionIdentity(liveSubmissionStorageKey);
          setLiveSubmissionStorageKey(null);
        }
        if (applied.response) setLiveFlexibleResponse(applied.response);
        setSelectedLiveDatePairId(applied.selectedDatePairId);
        if (applied.error) setError(applied.error);
      },
      (message: string, status?: number | null) => {
        if (shouldClearLiveSubmissionIdentity(new ApiError(message, status ?? null))) {
          clearLiveSubmissionIdentity(liveSubmissionStorageKey);
          setLiveSubmissionStorageKey(null);
          setLivePlanningJob(null);
          setError("实时任务已失效，可以重新提交需求。");
          return;
        }
        setError(message);
      },
    );
  }, [livePlanningJob?.id, livePlanningJob?.state, liveSubmissionStorageKey]);

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
        const liveInput: LiveFlexibleFromTextInput = {
          requirement: {
            text: liveRequirementText,
            trip_id: "pending",
            breakfast_mode: breakfastMode,
            breakfast_weight: normalizeBreakfastWeight(breakfastMode, breakfastWeight),
          },
          // Logged-out or CAPTCHA-gated providers are skipped while the
          // accessible sources still produce a clearly scoped best plan.
          coverage_mode: "degraded",
          timeout_seconds: 120,
          total_timeout_seconds: 600,
          max_pairs: liveMaxPairs,
        };
        const identity = await liveSubmissionIdentity(liveInput);
        liveInput.requirement.trip_id = identity.tripId;
        const started = await startLiveFlexiblePlanningFromTextJob(
          liveInput,
          identity.idempotencyKey,
        );
        window.localStorage.setItem(
          identity.storageKey,
          JSON.stringify({ ...identity, jobId: started.job.id }),
        );
        setLiveSubmissionStorageKey(identity.storageKey);
        setLivePlanningJob(started.job);
        const applied = applyLivePlanningJob(started.job);
        if (applied.terminal) {
          clearLiveSubmissionIdentity(identity.storageKey);
          setLiveSubmissionStorageKey(null);
          if (applied.response) setLiveFlexibleResponse(applied.response);
          setSelectedLiveDatePairId(applied.selectedDatePairId);
          if (applied.error) setError(applied.error);
        }
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
      const cancelled = await cancelLiveFlexiblePlanningJob(livePlanningJob.id);
      setLivePlanningJob(cancelled);
      const applied = applyLivePlanningJob(cancelled);
      if (applied.clearIdentity) {
        clearLiveSubmissionIdentity(liveSubmissionStorageKey);
        setLiveSubmissionStorageKey(null);
      }
      if (applied.response) setLiveFlexibleResponse(applied.response);
      setSelectedLiveDatePairId(applied.selectedDatePairId);
      if (applied.error) setError(applied.error);
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

      <main id="top">
        <WorkflowSteps
          current={deriveWorkflowStep(
            planningMode,
            workspace,
            job,
            livePlanningJob,
            liveFlexibleResponse,
          )}
        />
        <div className="workspace">
        <section className="intro-panel" aria-label="第一步：需求与模式">
          <p className="eyebrow">STEP 1 · 需求 · CONSTRAINT-AWARE TRAVEL PLANNING</p>
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
                  <div className="live-policy-card">
                    <span>日期与平台覆盖</span>
                    <strong>覆盖支持范围内的合法日期组合 · 可用平台并行查询</strong>
                    <small>只有完成硬约束校验且总价可完整比较的方案，才能进入最终排序。</small>
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
          <p className="plan-panel-step">
            {planningMode === "live"
              ? liveFlexibleResponse
                ? "STEP 4 · 方案 · 最终候选、自然语言修改与预订"
                : livePlanningJob &&
                    !["succeeded", "failed", "cancelled"].includes(
                      livePlanningJob.state,
                    )
                  ? "STEP 3 · 进度 · 搜索与 Agent 调度"
                  : "STEP 2 · 平台 · 能力矩阵与授权"
              : !workspace
                ? "STEP 1 · 需求 · 提交后进入搜索与方案"
                : job && job.status !== "succeeded" && job.status !== "failed"
                  ? "STEP 3 · 进度 · 搜索与 Agent 调度"
                  : "STEP 4 · 方案 · 候选、事件与预订"}
          </p>
          {error && <div className="error-banner">{error}</div>}
          {planningMode === "live" ? (
            liveFlexibleResponse ? (
              <>
                <FlexiblePlanningSummary
                  response={liveFlexibleResponse}
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
                        <LivePlanModifier
                          key={`modify-${selectedLiveDatePairId}-${liveRunId}`}
                          run={liveRun}
                          runId={liveRunId}
                          onRunAdvanced={(advancedRun) =>
                            setLiveRunOverrides((current) => ({
                              ...current,
                              [liveRunId]: advancedRun,
                            }))
                          }
                          onFinalPlanChanged={(finalPlan) =>
                            setLiveFlexibleResponse((current) =>
                              current ? { ...current, final_plan: finalPlan } : current,
                            )
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
                          onFinalPlanChanged={(finalPlan) =>
                            setLiveFlexibleResponse((current) =>
                              current ? { ...current, final_plan: finalPlan } : current,
                            )
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
                <div className={`job-badge ${job?.status ?? "queued"}`} role="status"><strong>{job?.progress ?? 0}%</strong><span>{stageLabels[job?.stage ?? "queued"] ?? job?.stage}</span></div>
              </div>
              {job && job.status !== "succeeded" && job.status !== "failed" && (
                <div
                  className="progress-track"
                  role="progressbar"
                  aria-label="规划进度"
                  aria-valuemin={0}
                  aria-valuemax={100}
                  aria-valuenow={job.progress}
                >
                  <span style={{ width: `${job.progress}%` }} />
                </div>
              )}

              {agentRun && (
                <details className={`agent-console ${agentRun.decision.state}`}>
                  <summary className="agent-console-head">
                    <div>
                      <p className="eyebrow">REPLAY MULTI-AGENT TRACE · 非实时</p>
                      <h3>
                        {agentRun.decision.state === "accept"
                          ? "直接接受"
                          : agentRun.decision.state === "accept_with_exception"
                            ? "确认例外后接受"
                            : "重新规划或暂停"}
                      </h3>
                    </div>
                    <strong>
                      回放 · {agentRun.scheduler.max_parallel_tasks} 路并发
                    </strong>
                  </summary>
                  <p>{agentRun.decision.summary}</p>
                  {agentRun.decision.verifier_violations.length > 0 && (
                    <div className="violation-list">
                      {agentRun.decision.verifier_violations.map((item) => (
                        <span key={item}>{item}</span>
                      ))}
                    </div>
                  )}
                  <div className="agent-facts">
                    <span>{agentRun.scheduler.graph.tasks.length} 个动态任务</span>
                    <span>{agentRun.evidence.length} 条证据</span>
                    <span>{Math.round(agentRun.scheduler.wall_time_seconds * 1000)} ms 回放耗时</span>
                  </div>
                  <details>
                    <summary>查看回放 Agent 轨迹</summary>
                    <ol>
                      {agentRun.scheduler.trace
                        .filter(
                          (item) =>
                            item.kind === "task_finished" || item.kind === "task_spawned",
                        )
                        .map((item) => (
                          <li key={item.sequence}>
                            <b>{item.kind === "task_spawned" ? "动态创建" : "完成"}</b>{" "}
                            {item.task_id} · {item.agent_role}
                          </li>
                        ))}
                    </ol>
                  </details>
                  <small>{agentBoundary}</small>
                </details>
              )}

              {plan && (
                <>
                  <div className="version-bar"><div><strong>计划版本</strong><span>{workspace.plans.length} 个可追溯版本</span></div><select value={selectedVersion} onChange={(e) => setSelectedVersion(Number(e.target.value))}>{workspace.plans.map((version) => <option key={version.id} value={version.version}>v{version.version} · {version.status}</option>)}</select></div>
                  <div className="facts"><article><span>预算上限</span><strong>{formatMoney(budget)}</strong></article><article><span>已知活动成本</span><strong>{formatMoney(String(knownCost))}</strong></article><article><span>已应用事件</span><strong className="good">{plan.applied_event_ids.length}</strong></article></div>

                  {offers.length > 0 && <div className="offer-grid">{offers.map((offer) => <article className="price-card" key={offer.id}><div><span className="source-logo">{offer.kind === "flight" ? "航" : "住"}</span><p><strong>{offer.title}</strong><small>{offer.source.provider} · {offer.source.mode}</small></p></div><div className="price"><strong>{formatMoney(offer.price.total.amount, offer.price.total.currency)}</strong><span>{priceLabel(offer.price_state as PriceState)}</span></div></article>)}</div>}
                  <p className="truth-note">回放/沙箱报价仅用于可重复评测；确认前必须调用供应商复价或由用户回原平台核验。</p>

                  {diff && <div className="diff-strip"><strong>v{selectedVersion - 1} → v{selectedVersion}</strong><span>新增 {diff.added_item_ids.length}</span><span>移除 {diff.removed_item_ids.length}</span><span>修改 {diff.changed_items.length}</span></div>}

                  {groupedItems.map(([date, items], index) => <div className="day-block" key={date}><div className="day-heading"><span>DAY {index + 1}</span><strong>{date}</strong></div><div className="timeline">{items.map((item) => <article className="timeline-item" key={item.id}><time>{formatTime(item.starts_at)}–{formatTime(item.ends_at)}</time><span className={`timeline-dot ${item.kind}`} /><div><strong>{item.title}</strong><p>{item.location_name ?? "位置待导航确认"} · {item.source_refs[0] ?? "用户偏好项"}</p></div></article>)}</div></div>)}

                  <div className="event-lab">
                    <div><p className="eyebrow">EVENT INJECTION LAB</p><h3>模拟异常，在合格候选中选择恢复策略</h3></div>
                    <label>
                      受影响活动
                      <select value={eventTarget} onChange={(e) => setEventTarget(e.target.value)}>
                        {plan.items.map((item) => <option key={item.id} value={item.id}>{item.title}</option>)}
                      </select>
                    </label>
                    <label>
                      事件类型
                      <select value={eventKind} onChange={(e) => setEventKind(e.target.value)}>
                        <option value="place_closed">临时闭园</option>
                        <option value="weather_alert">天气预警</option>
                        <option value="transport_delayed">延误 60 分钟</option>
                        <option value="price_changed">价格变为 ¥300</option>
                      </select>
                    </label>
                    <label>
                      恢复策略
                      <select value={replanPreference} onChange={(e) => setReplanPreference(e.target.value as typeof replanPreference)}>
                        <option value="minimum_change">最少改动</option>
                        <option value="balanced">平衡策略</option>
                        <option value="quality_first">质量优先</option>
                      </select>
                    </label>
                    <button type="button" onClick={injectEvent}>注入并重规划</button>
                  </div>
                  {replanResult && <div className={`replan-result ${replanResult.status}`}><strong>{replanResult.status === "ready" ? `${replanResult.selected_mode === "local" ? "局部修复" : "全局重优化"}完成` : "自动恢复已阻塞"}</strong><p>{replanResult.message}</p><span>未受影响项保留率 {(replanResult.unaffected_preservation_ratio * 100).toFixed(0)}% · {replanResult.candidates.length} 个候选通过策略比较</span></div>}

                  <div className="verification-bar"><div><span>✓</span><p><strong>确定性 Verifier 已检查</strong><small>日期、时间窗、移动间隔、预算、必去项与来源</small></p></div><em>{plan.status}</em></div>
                </>
              )}
            </>
          )}
        </section>
        </div>
      </main>
    </div>
  );
}

export default App;
