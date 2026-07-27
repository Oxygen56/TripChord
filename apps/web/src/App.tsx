import { useMemo, useState } from "react";

import { priceLabel, type PriceState } from "./domain";

type PlanItem = {
  time: string;
  title: string;
  detail: string;
  tone: "transport" | "activity" | "meal";
};

const sampleItems: PlanItem[] = [
  {
    time: "08:12–12:38",
    title: "上海虹桥 → 北京南",
    detail: "高铁候选 · 行李条件一致 · 到站后预留 45 分钟",
    tone: "transport",
  },
  {
    time: "14:30–17:00",
    title: "景山与什刹海步行线",
    detail: "路线 5.2 km · 室外活动 · 预留日落时间",
    tone: "activity",
  },
  {
    time: "18:00–19:20",
    title: "鼓楼附近晚餐",
    detail: "用户偏好：本地菜 · 人均预算 ¥120",
    tone: "meal",
  },
];

function App() {
  const [destination, setDestination] = useState("北京");
  const [budget, setBudget] = useState("5000");
  const [priceState] = useState<PriceState>("user_snapshot");

  const budgetLabel = useMemo(
    () => new Intl.NumberFormat("zh-CN", { style: "currency", currency: "CNY" }).format(Number(budget) || 0),
    [budget],
  );

  return (
    <div className="app-shell">
      <header className="topbar">
        <a className="brand" href="#top" aria-label="TripChord 首页">
          <span className="brand-mark">弦</span>
          <span>
            <strong>TripChord</strong>
            <small>旅弦 · 让每个选择彼此协调</small>
          </span>
        </a>
        <div className="status-pill"><span /> 规划证据可追溯</div>
      </header>

      <main id="top" className="workspace">
        <section className="intro-panel">
          <p className="eyebrow">PRICE-AWARE TRIP PLANNING</p>
          <h1>不是生成一段旅行文案，<br />而是编排一趟可执行的旅行。</h1>
          <p className="lede">
            TripChord 把交通、住宿、开放时间、路线、预算和个人偏好放进同一张计划图，
            自动发现冲突，并在价格或外部条件变化后只修改受影响的部分。
          </p>

          <div className="trip-form">
            <label>
              想去哪里
              <input value={destination} onChange={(event) => setDestination(event.target.value)} />
            </label>
            <div className="form-row">
              <label>
                出行日期
                <input type="text" defaultValue="10月1日 — 10月4日" />
              </label>
              <label>
                总预算
                <input value={budget} inputMode="numeric" onChange={(event) => setBudget(event.target.value)} />
              </label>
            </div>
            <label>
              旅行偏好
              <textarea defaultValue="第一次去，喜欢历史街区和本地菜；每天不要超过两个主要景点。" />
            </label>
            <button type="button">生成可验证行程 <span>→</span></button>
          </div>
        </section>

        <section className="plan-panel" aria-label="行程预览">
          <div className="plan-header">
            <div>
              <p className="eyebrow">PLAN PREVIEW</p>
              <h2>{destination} · 4 日自由行</h2>
            </div>
            <div className="score"><strong>96</strong><span>约束通过</span></div>
          </div>

          <div className="facts">
            <article><span>预算上限</span><strong>{budgetLabel}</strong></article>
            <article><span>当前估算</span><strong>¥4,620</strong></article>
            <article><span>未解决冲突</span><strong className="good">0</strong></article>
          </div>

          <div className="price-card">
            <div>
              <span className="source-logo">U</span>
              <p><strong>住宿报价 · 后海区域</strong><small>1 间房 · 3 晚 · 可免费取消</small></p>
            </div>
            <div className="price"><strong>¥1,286</strong><span>{priceLabel(priceState)}</span></div>
          </div>
          <p className="truth-note">此报价由用户导入，选定前仍需返回原平台复核。</p>

          <div className="day-heading"><span>DAY 1</span><strong>抵达与城市北线</strong></div>
          <div className="timeline">
            {sampleItems.map((item) => (
              <article className="timeline-item" key={item.title}>
                <time>{item.time}</time>
                <span className={`timeline-dot ${item.tone}`} />
                <div><strong>{item.title}</strong><p>{item.detail}</p></div>
              </article>
            ))}
          </div>

          <div className="verification-bar">
            <div><span>✓</span><p><strong>Verifier 已检查</strong><small>时间、路线、预算、数据来源</small></p></div>
            <button type="button">查看依据</button>
          </div>
        </section>
      </main>
    </div>
  );
}

export default App;

