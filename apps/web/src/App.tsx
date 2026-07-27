import { FormEvent, useEffect, useMemo, useState } from "react";

import {
  comparePlans,
  type Job,
  loadWorkspace,
  type Offer,
  type PlanDiff,
  type PlanItem,
  type ReplanResult,
  replanWorkspace,
  setApiCredential,
  searchOffers,
  startPlanning,
  subscribeToJob,
  type TripSpec,
  type Workspace,
} from "./api";
import { priceLabel, type PriceState } from "./domain";

const stageLabels: Record<string, string> = {
  queued: "任务已入队",
  optimizing: "约束求解中",
  verifying: "逐项校验中",
  complete: "规划完成",
  failed: "规划失败",
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

function App() {
  const [origin, setOrigin] = useState("上海");
  const [destination, setDestination] = useState("北京");
  const [startDate, setStartDate] = useState("2026-10-02");
  const [endDate, setEndDate] = useState("2026-10-03");
  const [budget, setBudget] = useState("5000");
  const [interests, setInterests] = useState("历史，博物馆，胡同");
  const [mustVisit, setMustVisit] = useState("故宫");
  const [maxActivities, setMaxActivities] = useState(2);
  const [apiKey, setApiKey] = useState(
    () => sessionStorage.getItem("tripchord-api-key") ?? "",
  );
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
    if (!workspace || selectedVersion <= 1) {
      setDiff(null);
      return;
    }
    void comparePlans(workspace.id, selectedVersion - 1, selectedVersion).then(setDiff);
  }, [selectedVersion, workspace?.id]);

  async function handlePlan(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setSubmitting(true);
    setError("");
    setWorkspace(null);
    setReplanResult(null);
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
      const [started, foundOffers] = await Promise.all([
        startPlanning(spec),
        searchOffers(spec).catch(() => []),
      ]);
      setWorkspace(started.workspace);
      setJob(started.job);
      setOffers(foundOffers);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "无法启动规划");
    } finally {
      setSubmitting(false);
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
        <div className="status-pill"><span /> 规划证据可追溯</div>
      </header>

      <main id="top" className="workspace">
        <section className="intro-panel">
          <p className="eyebrow">CONSTRAINT-AWARE TRAVEL PLANNING</p>
          <h1>不是生成旅行文案，<br />而是求解一趟可执行的旅行。</h1>
          <p className="lede">交通、住宿、景点、路线、预算和变化事件进入同一条计划链。每次修改都有来源、校验结果和版本差异。</p>
          <form className="trip-form" onSubmit={handlePlan}>
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
            <label>部署 API Key（本地演示可留空）<input type="password" autoComplete="off" value={apiKey} onChange={(e) => setApiKey(e.target.value)} placeholder="仅保存在当前浏览器会话" /></label>
            <button type="submit" disabled={submitting}>{submitting ? "正在创建工作区…" : "开始可验证规划 →"}</button>
          </form>
          <div className="truth-banner"><strong>当前演示数据边界</strong><p>景点与路线使用明确标注的离线回放场景；配置授权供应商密钥后，报价与地点接口可切换到生产数据。回放价不是可预订实时价。</p></div>
        </section>

        <section className="plan-panel" aria-label="规划工作区">
          {!workspace ? (
            <div className="empty-state"><span>⌁</span><h2>等待一组旅行约束</h2><p>提交后可以看到求解进度、报价真实性、逐日安排、版本差异和事件恢复。</p></div>
          ) : (
            <>
              <div className="plan-header">
                <div><p className="eyebrow">PERSISTED WORKSPACE</p><h2>{workspace.title}</h2><small className="workspace-id">{workspace.id}</small></div>
                <div className={`job-badge ${job?.status ?? "queued"}`}><strong>{job?.progress ?? 0}%</strong><span>{stageLabels[job?.stage ?? "queued"] ?? job?.stage}</span></div>
              </div>
              {job && job.status !== "succeeded" && job.status !== "failed" && <div className="progress-track"><span style={{ width: `${job.progress}%` }} /></div>}
              {error && <div className="error-banner">{error}</div>}

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
