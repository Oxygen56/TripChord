# TripChord Web WCAG 2.2 AA 审查记录（v0.8）

> 审查对象：`apps/web` 主 SPA（`App.tsx` / `styles.css` / `api.ts`）。
> 结论：**有条件符合（部分）** —— 键盘可操作与焦点可见、非颜色状态、语义化结构已满足或补强；可读字号（≥12px）、`aria-live` 实时区域、表单标签、目标最小尺寸已按审计整改；**对比度全量自动化测量**与**真实浏览器（Lighthouse + NVDA/VoiceOver）人工复核**仍为待办，不伪装为完整通过。
> 本记录不构成 WCAG 合规证书；发布前需在真实浏览器做人工/自动化复核。

## 逐项审查

### 1. 可感知（Perceivable）

| 标准 | 状态 | 证据 / 说明 |
|---|---|---|
| 1.4.1 非颜色传达信息（AA） | 通过 | 报价新鲜度用 `em.fresh / em.expired / em.unknown` 同时输出文字标签；handoff 结果用文字（"价格未变 / 价格有变化…"）而非仅颜色；decision 徽标带文字。 |
| 1.4.3 对比度（AA） | 部分 | 正文在浅底 `#0d3b3b` 上可读；本轮已将全部辅助字号从 6–11px 提到 **≥12px**（`styles.css` 150 处），可读性缺口已补；对比度比值仍未全量自动化测量。 |
| 1.4.4 调整文字大小（AA） | 通过 | 全部文字不小于 12px；`h1` 使用 `clamp()`，布局不依赖固定字号溢出。 |
| 1.4.11 非文本对比度（AA） | 部分 | 新增 `focus-visible` 轮廓 `#2563eb` 对比明显；其余控件沿用既有样式，未逐一测量。 |

### 2. 可操作（Operable）

| 标准 | 状态 | 证据 / 说明 |
|---|---|---|
| 2.1.1 键盘（AA） | 通过 | 全部交互元素为原生 `<button>`/`<input>`/`<details>`，可 Tab 聚焦并回车触发；handoff 两步流按钮为原生按钮。 |
| 2.4.7 焦点可见（AA） | 通过 | 新增 `.handoff-actions button:focus-visible { outline: 2px solid #2563eb }`；既有控件依赖浏览器默认焦点环（未全局关闭 outline）。 |
| 2.5.8 目标最小尺寸（2.2 AA） | 通过 | 小按钮（handoff / event-lab / live-event / memory / live-setup / mode-switch / option-grid）统一补 `min-width: 24px; min-height: 24px`。 |

### 3. 可理解（Understandable）

| 标准 | 状态 | 证据 / 说明 |
|---|---|---|
| 3.2.2 输入时无意外变化（AA） | 通过 | 表单提交与按钮动作均为用户显式触发；handoff "去官方页面" 仅在用户点击后 `window.open`，不自动打开/聚焦。 |
| 3.3.1/3.3.2 错误识别与标签（AA） | 通过 | 错误用 `reprice-error` / `error-banner` 文字呈现；事件注入三个 `<select>` 已补显式 `<label>`（受影响活动 / 事件类型 / 恢复策略），其余输入均以隐式标签包裹。 |

### 4. 健壮（Robust）

| 标准 | 状态 | 证据 / 说明 |
|---|---|---|
| 4.1.2 名称/角色/值（AA） | 通过 | 原生控件具备语义角色；动态覆盖区已接 `aria-live`：实时规划 job 进度 `aria-live="polite"`、回放 job 徽标 `role="status"` + 进度条 `role="progressbar"`、重核价结果 `aria-live="polite" role="status"`（错误 `role="alert"`）、事件重规划结果与周期核价轮次 `aria-live="polite" role="status"`。 |

## 已随本轮落实的修复

- `apps/web/src/styles.css`：全部辅助字号从 6–11px 提到 **≥12px**（150 处声明，覆盖 1.4.3/1.4.4）。
- `apps/web/src/styles.css`：`.handoff-actions button:focus-visible` 焦点环（2.4.7）。
- `apps/web/src/styles.css`：小按钮 `min-width/min-height: 24px`（2.5.8）。
- `apps/web/src/App.tsx`：`aria-live`/`role` 覆盖实时 job 进度、回放 job 进度、重核价结果、事件重规划结果、周期核价轮次与错误横幅（4.1.2 / 3.3.1）。
- `apps/web/src/App.tsx`：事件注入三个 `<select>` 补显式 `<label>`（3.3.2）。
- 非颜色状态：handoff 结果以文字呈现（1.4.1）。
- 交互全部使用原生按钮，未引入点击劫持（2.1.1 / 2.5.1）。
- 首页旅行工作流拆分：新增 `WorkflowSteps` 步骤条（需求 → 平台 → 进度 → 方案）与 `plan-panel-step` 步骤标签；高技术细节（回放 Agent 轨迹、live Agent 流水线、模型回执统计）默认折叠为 `<details>`，普通用户先看到"发生了什么、接下来能做什么"。

## 已知缺口（不伪装完成）

1. **对比度测量**：未对全部配色做自动化对比度计算，仍为人工抽查。
2. **真实浏览器人工复核**：Lighthouse Accessibility 审计 + NVDA/VoiceOver 走查尚未执行，发布前必须完成。

## 复核命令

```bash
cd apps/web
npm run build && npm test
```

人工复核（发布前必须）：浏览器开发者工具 Lighthouse Accessibility 审计 + NVDA/VoiceOver 走查上述 1–2 项。
