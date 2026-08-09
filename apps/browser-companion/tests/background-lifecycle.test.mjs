import assert from "node:assert/strict";
import { webcrypto } from "node:crypto";
import { readFile } from "node:fs/promises";
import vm from "node:vm";

class FakeEvent {
  constructor() {
    this.listeners = new Set();
  }

  addListener(listener) {
    this.listeners.add(listener);
  }

  removeListener(listener) {
    this.listeners.delete(listener);
  }

  emit(...args) {
    for (const listener of [...this.listeners]) {
      listener(...args);
    }
  }
}

const onCreated = new FakeEvent();
const onUpdated = new FakeEvent();
const onRemoved = new FakeEvent();
const tabs = new Map();
const removedTabs = [];
const injectedTabs = [];
const tabUpdates = [];
const windowUpdates = [];
const createdWindowRequests = [];
const removedWindows = [];
const sentMessages = [];
const createdTabRequests = [];
const mainWorldExecutions = [];
const browserWindows = new Map([
  [1, { id: 1, focused: true, state: "normal", type: "normal" }],
]);
const storage = {
  tripchordConnected: false,
  tripchordBridgeUrl: "http://127.0.0.1:8000/browser-bridge",
  tripchordBridgeToken: "",
};
let sendMessageImpl = async () => {
  throw new Error("unexpected content message");
};
let createTabImpl = async () => {
  throw new Error("unexpected tab creation");
};
let createWindowImpl = async () => {
  throw new Error("unexpected window creation");
};
let removeWindowImpl = async () => {
  throw new Error("unexpected window removal");
};
let mainWorldScriptImpl = async () => {
  throw new Error("unexpected MAIN-world script");
};
let documentReadyStateProbeImpl = async (tabId) =>
  tabs.get(tabId)?.status === "complete" ? "complete" : "loading";
let visibilityProbeImpl = async (tabId) => {
  const tab = { windowId: 1, active: false, ...tabs.get(tabId) };
  const window = browserWindows.get(tab.windowId);
  const visible = Boolean(tab.active && window && window.focused);
  return {
    visibilityState: visible ? "visible" : "hidden",
    hidden: !visible,
  };
};
let contentRuntimePresentImpl = async () => false;
let runtimeReloadCount = 0;
const clearedAlarms = [];
const createdAlarms = [];

const storageArea = {
  async get(keys) {
    const values = Array.isArray(keys) ? keys : [keys];
    return Object.fromEntries(values.map((key) => [key, storage[key]]));
  },
  async remove(keys) {
    const values = Array.isArray(keys) ? keys : [keys];
    for (const key of values) {
      delete storage[key];
    }
  },
  async set(values) {
    Object.assign(storage, values);
  },
};

const chrome = {
  runtime: {
    id: "test-extension",
    onMessage: new FakeEvent(),
    onInstalled: new FakeEvent(),
    onStartup: new FakeEvent(),
    getManifest() {
      return { version: "0.1.16" };
    },
    reload() {
      runtimeReloadCount += 1;
    },
  },
  storage: {
    local: storageArea,
    session: storageArea,
  },
  alarms: {
    onAlarm: new FakeEvent(),
    async clear(name) {
      clearedAlarms.push(name);
    },
    create(name, options) {
      createdAlarms.push({ name, options: { ...options } });
    },
  },
  permissions: {
    async contains() {
      return true;
    },
  },
  scripting: {
    async executeScript({ target, files, func, args, world }) {
      if (files) {
        injectedTabs.push(target.tabId);
        return [];
      }
      if (func && world === "MAIN") {
        mainWorldExecutions.push({
          tabId: target.tabId,
          func,
          args: Array.isArray(args) ? [...args] : [],
        });
        return mainWorldScriptImpl({
          target,
          func,
          args: Array.isArray(args) ? args : [],
          world,
        });
      }
      if (func && func.name === "readDocumentReadyState") {
        return [{
          result: await documentReadyStateProbeImpl(target.tabId),
        }];
      }
      if (func && func.name === "tripchordContentRuntimePresent") {
        return [{ result: await contentRuntimePresentImpl(target.tabId) }];
      }
      if (func) {
        return [{ result: await visibilityProbeImpl(target.tabId) }];
      }
      return [];
    },
  },
  tabs: {
    onCreated,
    onUpdated,
    onRemoved,
    async create(options) {
      createdTabRequests.push({ ...options });
      return createTabImpl(options);
    },
    async get(tabId) {
      const tab = tabs.get(tabId);
      if (!tab) {
        throw new Error(`missing fake tab ${tabId}`);
      }
      return { windowId: 1, active: false, ...tab };
    },
    async query({ active, windowId }) {
      return [...tabs.values()]
        .map((tab) => ({ windowId: 1, active: false, ...tab }))
        .filter(
          (tab) =>
            (!active || tab.active === true) &&
            (windowId === undefined || tab.windowId === windowId),
        );
    },
    async update(tabId, update) {
      const existing = { windowId: 1, active: false, ...tabs.get(tabId) };
      if (update.active === true) {
        for (const [otherId, rawTab] of tabs.entries()) {
          const other = { windowId: 1, active: false, ...rawTab };
          if (otherId !== tabId && other.windowId === existing.windowId) {
            tabs.set(otherId, { ...other, active: false });
          }
        }
      }
      const tab = { ...existing, ...update };
      tabs.set(tabId, tab);
      tabUpdates.push({ tabId, update: { ...update } });
      return { ...tab };
    },
    async remove(tabId) {
      if (!tabs.has(tabId)) {
        throw new Error(`missing fake tab ${tabId}`);
      }
      tabs.delete(tabId);
      removedTabs.push(tabId);
      onRemoved.emit(tabId, { windowId: 1, isWindowClosing: false });
    },
    async sendMessage(tabId, message) {
      sentMessages.push({ tabId, message });
      return sendMessageImpl(tabId, message);
    },
  },
  windows: {
    async create(options) {
      createdWindowRequests.push({ ...options });
      return createWindowImpl(options);
    },
    async getAll() {
      return [...browserWindows.values()].map((window) => ({ ...window }));
    },
    async get(windowId) {
      const window = browserWindows.get(windowId);
      if (!window) {
        throw new Error(`missing fake window ${windowId}`);
      }
      return { ...window };
    },
    async getLastFocused() {
      const focused = [...browserWindows.values()].find(
        (window) => window.focused,
      );
      return focused ? { ...focused } : null;
    },
    async update(windowId, update) {
      const existing = browserWindows.get(windowId);
      if (!existing) {
        throw new Error(`missing fake window ${windowId}`);
      }
      if (update.focused === true) {
        for (const [otherId, window] of browserWindows.entries()) {
          browserWindows.set(otherId, {
            ...window,
            focused: otherId === windowId,
          });
        }
      }
      const next = { ...browserWindows.get(windowId), ...update };
      browserWindows.set(windowId, next);
      windowUpdates.push({ windowId, update: { ...update } });
      return { ...next };
    },
    async remove(windowId) {
      const result = await removeWindowImpl(windowId);
      removedWindows.push(windowId);
      return result;
    },
  },
};

const hooks = {};
const buildMetaSource = await readFile(
  new URL("../src/build-meta.js", import.meta.url),
  "utf8",
);
let context;
context = vm.createContext({
  AbortController,
  URL,
  URLSearchParams,
  chrome,
  clearTimeout,
  console,
  Date,
  TextEncoder,
  crypto: webcrypto,
  encodeURIComponent,
  importScripts(path) {
    assert.equal(path, "build-meta.js");
    vm.runInContext(buildMetaSource, context, { filename: "build-meta.js" });
  },
  fetch: async () => {
    throw new Error("unexpected bridge fetch");
  },
  Promise,
  setTimeout,
  __TRIPCHORD_BACKGROUND_TEST_HOOKS__: hooks,
});
const source = await readFile(
  new URL("../src/background.js", import.meta.url),
  "utf8",
);
vm.runInContext(source, context, { filename: "background.js" });
await new Promise((resolve) => setTimeout(resolve, 0));
tabs.set(900, {
  id: 900,
  windowId: 1,
  active: true,
  status: "complete",
  url: "https://example.test/user-tab",
});

assert.equal(hooks.MAX_CONCURRENT_LEASES, 6);
assert.equal(hooks.MAX_CONCURRENT_QUNAR_LODGING_LEASES, 1);
assert.equal(
  hooks.QUNAR_LODGING_ISOLATION_SCOPE,
  "companion_owned_unfocused_normal_window_active_tab",
);
assert.equal(hooks.QUNAR_LODGING_WINDOW_CLEANUP_ATTEMPTS, 2);
assert.equal(
  hooks.QUNAR_DETAIL_SEED_SELECTION_POLICY,
  "query-fingerprint-rotation-v1",
);
assert.equal(
  hooks.isQunarLodgingLease({ provider: "qunar", kind: "lodging" }),
  true,
);
assert.equal(
  hooks.isQunarLodgingLease({ provider: "qunar", kind: "flight" }),
  false,
);
{
  const isolatedWindowId = 2;
  const isolatedTabId = 902;
  let cleanupAttempts = 0;
  createWindowImpl = async (options) => {
    assert.equal(options.url, "https://hotel.qunar.com/global/");
    assert.equal(options.focused, false);
    assert.equal(options.state, "normal");
    assert.equal(options.type, "normal");
    const window = {
      id: isolatedWindowId,
      focused: false,
      state: "normal",
      type: "normal",
    };
    browserWindows.set(isolatedWindowId, window);
    tabs.set(isolatedTabId, {
      id: isolatedTabId,
      windowId: isolatedWindowId,
      active: true,
      status: "complete",
      url: options.url,
    });
    return { ...window };
  };
  removeWindowImpl = async (windowId) => {
    cleanupAttempts += 1;
    assert.equal(windowId, isolatedWindowId);
    if (cleanupAttempts === 1) {
      throw new Error("transient close failure");
    }
    browserWindows.delete(windowId);
    for (const [tabId, tab] of tabs.entries()) {
      if (tab.windowId === windowId) tabs.delete(tabId);
    }
  };
  const ownedWindows = new Set();
  const ownedTabs = new Set();
  const tabUpdateCount = tabUpdates.length;
  const windowUpdateCount = windowUpdates.length;
  const isolated = await hooks.createQunarLodgingIsolationWindow(
    "https://hotel.qunar.com/global/",
    ownedWindows,
    ownedTabs,
  );
  assert.equal(isolated.tab.id, isolatedTabId);
  assert.equal(isolated.tab.active, true);
  assert.equal(isolated.isolation_evidence.requested_focused, false);
  assert.equal(isolated.isolation_evidence.minimized, false);
  assert.equal(isolated.isolation_evidence.reused_user_window, false);
  assert.equal(browserWindows.get(1).focused, true);
  assert.equal(tabs.get(900).active, true);
  assert.equal(tabUpdates.length, tabUpdateCount);
  assert.equal(windowUpdates.length, windowUpdateCount);
  await hooks.closeOwnedWindows(ownedWindows, ownedTabs);
  assert.equal(cleanupAttempts, 2);
  assert.equal(ownedWindows.size, 0);
  assert.equal(ownedTabs.size, 0);
  assert.equal(browserWindows.has(isolatedWindowId), false);
  assert.equal(tabs.has(isolatedTabId), false);
}
{
  const removedBefore = removedWindows.length;
  createWindowImpl = async () => ({ ...browserWindows.get(1) });
  await assert.rejects(
    hooks.createQunarLodgingIsolationWindow(
      "https://hotel.qunar.com/global/",
      new Set(),
      new Set(),
    ),
    (error) =>
      error.tripchordCode ===
        "qunar_lodging_isolation_existing_window_rejected",
  );
  assert.equal(removedWindows.length, removedBefore);
  assert.equal(browserWindows.has(1), true);
  assert.equal(browserWindows.get(1).focused, true);
}
{
  const isolatedWindowId = 3;
  const isolatedTabId = 903;
  createWindowImpl = async (options) => {
    const window = {
      id: isolatedWindowId,
      focused: true,
      state: "normal",
      type: "normal",
    };
    browserWindows.set(isolatedWindowId, window);
    tabs.set(isolatedTabId, {
      id: isolatedTabId,
      windowId: isolatedWindowId,
      active: true,
      status: "complete",
      url: options.url,
    });
    return { ...window };
  };
  removeWindowImpl = async (windowId) => {
    browserWindows.delete(windowId);
    for (const [tabId, tab] of tabs.entries()) {
      if (tab.windowId === windowId) tabs.delete(tabId);
    }
  };
  await assert.rejects(
    hooks.createQunarLodgingIsolationWindow(
      "https://hotel.qunar.com/global/",
      new Set(),
      new Set(),
    ),
    (error) =>
      error.tripchordCode === "qunar_lodging_isolation_contract_rejected" &&
      error.tripchordDetails.browser_isolation.observed_focused === true,
  );
  assert.equal(browserWindows.has(isolatedWindowId), false);
  assert.equal(tabs.has(isolatedTabId), false);
}
{
  const savedCreate = chrome.windows.create;
  chrome.windows.create = undefined;
  await assert.rejects(
    hooks.createQunarLodgingIsolationWindow(
      "https://hotel.qunar.com/global/",
      new Set(),
      new Set(),
    ),
    (error) =>
      error.tripchordCode === "qunar_lodging_isolation_unavailable" &&
      error.tripchordDetails.browser_isolation.fallback_policy ===
        "fail_closed_without_activating_a_user_window",
  );
  chrome.windows.create = savedCreate;
}
assert.equal(
  createdWindowRequests.every(
    (request) =>
      request.focused === false &&
      request.state === "normal" &&
      request.type === "normal",
  ),
  true,
);
assert.equal(hooks.MAX_LODGING_DETAIL_PAGES_PER_LEASE, 2);
assert.equal(hooks.MAX_FLIGGY_LODGING_DETAIL_PAGES_PER_LEASE, 3);
assert.equal(hooks.MAX_CONCURRENT_FLIGGY_LODGING_DETAILS, 3);
assert.equal(hooks.MAX_CTRIP_LODGING_PREVIEW_CANDIDATES, 12);
assert.equal(hooks.MAX_CTRIP_LODGING_CAPTURE_CONTROLS, 6);
assert.equal(hooks.CTRIP_LODGING_DETAIL_WORKFLOW_CAP_MS, 55000);
assert.equal(hooks.INITIAL_LANDING_STAGE_CAP_MS, 40000);
assert.equal(hooks.MAX_CONCURRENT_INITIAL_LANDINGS, 3);
assert.equal(hooks.PREPARE_SEARCH_STAGE_CAP_MS, 35000);
assert.equal(hooks.TRIGGER_SEARCH_STAGE_CAP_MS, 30000);
assert.match(
  source,
  /Date\.now\(\) \+\s*LODGING_DOM_DRIFT_POLL_INTERVAL_MS \+\s*LODGING_EXTRACTION_RETRY_MIN_BUDGET_MS >=\s*extractionDeadline/,
);
assert.equal(hooks.LODGING_EXTRACTION_RETRY_MIN_BUDGET_MS, 15000);
assert.equal(hooks.LODGING_EXTRACTION_STAGE_CAP_MS, 90000);
assert.equal(hooks.LEASE_COMPLETION_MAX_RESERVE_MS, 20000);
assert.equal(hooks.dynamicLeaseCompletionReserveMs(120000), 20000);
{
  const releases = [];
  const started = [];
  let activeQunarLodging = 0;
  let maxActiveQunarLodging = 0;
  const leases = [
    { task_id: "q1", provider: "qunar", kind: "lodging" },
    { task_id: "q2", provider: "qunar", kind: "lodging" },
    { task_id: "q3", provider: "qunar", kind: "lodging" },
    { task_id: "c1", provider: "ctrip", kind: "lodging" },
  ];
  const running = hooks.executeClaimedLeasesProviderAware(
    leases,
    async (lease) => {
      started.push(lease.task_id);
      if (lease.provider !== "qunar") return;
      activeQunarLodging += 1;
      maxActiveQunarLodging = Math.max(
        maxActiveQunarLodging,
        activeQunarLodging,
      );
      await new Promise((resolve) => releases.push(resolve));
      activeQunarLodging -= 1;
    },
  );
  await new Promise((resolve) => setTimeout(resolve, 0));
  assert.equal(started.includes("c1"), true);
  assert.equal(started.filter((id) => id.startsWith("q")).length, 1);
  assert.equal(maxActiveQunarLodging, 1);
  releases.shift()();
  await new Promise((resolve) => setTimeout(resolve, 0));
  assert.equal(started.filter((id) => id.startsWith("q")).length, 2);
  releases.shift()();
  await new Promise((resolve) => setTimeout(resolve, 0));
  assert.equal(started.filter((id) => id.startsWith("q")).length, 3);
  while (releases.length) releases.shift()();
  await running;
  assert.equal(maxActiveQunarLodging, 1);
}
{
  tabs.set(901, {
    id: 901,
    windowId: 1,
    active: true,
    status: "complete",
    url: "https://hotel.fliggy.com/hotel_list3.htm",
  });
  const injectedBefore = injectedTabs.length;
  contentRuntimePresentImpl = async () => true;
  await hooks.installContent(901);
  assert.equal(injectedTabs.length, injectedBefore);
  contentRuntimePresentImpl = async () => false;
  await hooks.installContent(901);
  assert.deepEqual(injectedTabs.slice(injectedBefore), [901]);
  tabs.delete(901);
}
{
  const query = {
    start_date: "2026-08-01",
    end_date: "2026-08-08",
    adults: 2,
    rooms: 1,
  };
  const listUrl =
    "https://hotel.fliggy.com/hotel_list3.htm?spm=181.11358650.hotelModule.internationalSearch&city=933081&cityName=%E9%A9%AC%E5%AF%8C%E5%A3%AB&checkIn=2026-08-01&checkOut=2026-08-08&keywords=&aNum_1=2&cNum_1=0&_output_charset=utf8";
  const augmented = hooks.auditedFliggyLodgingDetailCandidate(
    "/hotel_detail2.htm?shid=52229176&checkIn=2026-08-01&checkOut=2026-08-08",
    listUrl,
    query,
  );
  assert.equal(augmented.allowed, true);
  assert.equal(augmented.property_id, "52229176");
  assert.equal(augmented.augmented_from_visible_property_link, true);
  const hydrated = new URL(augmented.href);
  assert.equal(hydrated.searchParams.get("aNum_1"), "2");
  assert.equal(hydrated.searchParams.get("cNum_1"), "0");
  assert.equal(hydrated.searchParams.get("roomNum"), "1");
  assert.equal(hydrated.searchParams.get("city"), "933081");

  const mismatched = hooks.auditedFliggyLodgingDetailCandidate(
    "/hotel_detail2.htm?shid=52229176&checkIn=2026-08-02&checkOut=2026-08-08",
    listUrl,
    query,
  );
  assert.equal(mismatched.allowed, false);
  assert.equal(mismatched.reason, "detail_query_contract_mismatch");

  const unsafe = hooks.auditedFliggyLodgingDetailCandidate(
    "/hotel_detail2.htm?shid=52229176&checkIn=2026-08-01&checkOut=2026-08-08&returnUrl=https%3A%2F%2Fevil.example",
    listUrl,
    query,
  );
  assert.equal(unsafe.allowed, false);
  assert.equal(unsafe.reason, "detail_transaction_marker");
}
assert.deepEqual(
  JSON.parse(JSON.stringify(hooks.bridgeValidationDiagnostic({
    detail: [
      {
        loc: ["body", "completion", "quotes", 0, "details", "unsafe"],
        type: "value_error",
        msg: "quote failed its exact evidence contract",
        input: {
          account: "must-not-survive",
          visible_evidence: "must-not-survive",
        },
      },
    ],
  }))),
  [
    {
      location: [
        "body",
        "completion",
        "quotes",
        "0",
        "details",
        "unsafe",
      ],
      type: "value_error",
      message: "quote failed its exact evidence contract",
    },
  ],
);
assert.equal(
  hooks.completionContractRejected({
    status: 409,
    bridgeDetail:
      "failure page_url does not match the claimed provider",
  }),
  true,
);
assert.equal(
  hooks.completionContractRejected({
    status: 409,
    bridgeDetail: "task lease has expired",
  }),
  false,
);
assert.deepEqual(
  JSON.parse(JSON.stringify(
    hooks.pageLocationDiagnostic(
      "https://login.example.test/path/to/login?ticket=secret#fragment",
    ),
  )),
  {
    scheme: "https",
    host: "login.example.test",
    path: "/path/to/login",
  },
);
assert.equal(hooks.dynamicLeaseCompletionReserveMs(15000), 2500);
assert.equal(hooks.dynamicLeaseCompletionReserveMs(1200), 480);

{
  const claimedAtMs = Date.parse("2026-08-01T00:00:00.000Z");
  const expiresAtMs = Date.parse("2026-08-01T00:02:00.000Z");
  const receivedAtMs = Date.parse("2026-08-01T00:00:30.000Z");
  const timing = hooks.leaseTiming(
    {
      timeout_seconds: 120,
      claimed_at: new Date(claimedAtMs).toISOString(),
      lease_expires_at: new Date(expiresAtMs).toISOString(),
    },
    receivedAtMs,
  );
  assert.equal(timing.deadline_source, "server_absolute");
  assert.equal(timing.lease_duration_ms, 120000);
  assert.equal(timing.completion_reserve_ms, 20000);
  assert.equal(timing.work_deadline_ms, expiresAtMs - 20000);
  // The live MV3 canary delivered a timer roughly five seconds late. A
  // 120-second lease must still retain at least fifteen seconds for /complete.
  const delayedTimerFireAt = timing.work_deadline_ms + 5000;
  assert.equal(expiresAtMs - delayedTimerFireAt, 15000);
  const receiptBasedDeadline =
    receivedAtMs + 120000 - timing.completion_reserve_ms;
  assert.equal(receiptBasedDeadline - timing.work_deadline_ms, 30000);
  assert.equal(
    hooks.completionRequestTimeoutMs(
      timing,
      timing.work_deadline_ms,
    ),
    4000,
  );

  const fallback = hooks.leaseTiming(
    { timeout_seconds: 15 },
    receivedAtMs,
  );
  assert.equal(fallback.deadline_source, "receipt_fallback");
  assert.equal(fallback.completion_reserve_ms, 2500);
  assert.equal(
    fallback.work_deadline_ms,
    receivedAtMs + 15000 - 2500,
  );
}
assert.equal(
  hooks.LANDING_URLS.qunar.lodging,
  "https://hotel.qunar.com/global/",
);

{
  tabs.set(901, {
    id: 901,
    windowId: 1,
    active: false,
    status: "loading",
    url: "https://hotels.ctrip.com/",
  });
  documentReadyStateProbeImpl = async (tabId) =>
    tabId === 901 ? "interactive" : "loading";
  const ready = await hooks.waitForTabInteractive(901, 1000);
  assert.equal(ready.mode, "document_interactive");
  assert.equal(ready.document_ready_state, "interactive");
  tabs.delete(901);
  documentReadyStateProbeImpl = async (tabId) =>
    tabs.get(tabId)?.status === "complete" ? "complete" : "loading";
}

{
  tabs.set(902, {
    id: 902,
    windowId: 1,
    active: false,
    status: "loading",
    url: "https://flight.qunar.com/twell/flight/Search.jsp",
  });
  documentReadyStateProbeImpl = async (tabId) =>
    tabId === 902 ? new Promise(() => {}) : "loading";
  await assert.rejects(
    hooks.waitForTabInteractive(902, 50),
    (error) =>
      error.tripchordCode === "tab_interactive_timeout" &&
      error.tripchordDetails.tab_status === "loading" &&
      error.tripchordDetails.document_ready_state === null &&
      /ready-state probe timed out/.test(
        error.tripchordDetails.probe_error,
      ),
  );
  tabs.delete(902);
  documentReadyStateProbeImpl = async (tabId) =>
    tabs.get(tabId)?.status === "complete" ? "complete" : "loading";
}

{
  let runningLandings = 0;
  let peakLandings = 0;
  const landingReleases = [];
  const traces = Array.from({ length: 4 }, () => []);
  const landingRuns = traces.map((trace, index) =>
    hooks.withInitialLandingSlot(
      trace,
      Date.now() + 2000,
      async () => {
        runningLandings += 1;
        peakLandings = Math.max(peakLandings, runningLandings);
        await new Promise((resolve) => {
          landingReleases[index] = resolve;
        });
        runningLandings -= 1;
        return index;
      },
    ),
  );
  await new Promise((resolve) => setTimeout(resolve, 0));
  assert.equal(runningLandings, 3);
  assert.equal(landingReleases[3], undefined);
  landingReleases[0]();
  await new Promise((resolve) => setTimeout(resolve, 0));
  assert.equal(runningLandings, 3);
  assert.equal(typeof landingReleases[3], "function");
  landingReleases[1]();
  landingReleases[2]();
  landingReleases[3]();
  assert.deepEqual(
    Array.from(await Promise.all(landingRuns)),
    [0, 1, 2, 3],
  );
  assert.equal(peakLandings, 3);
  assert.equal(
    traces.every((trace) => trace.length === 0),
    true,
  );
}

{
  const stageTrace = [];
  const value = await hooks.withStageBudget(
    stageTrace,
    "bounded_fixture",
    Date.now() + 1000,
    250,
    async () => "done",
  );
  assert.equal(value, "done");
  assert.equal(stageTrace[0].stage, "bounded_fixture");
  assert.equal(stageTrace[0].status, "completed");

  const timeoutTrace = [];
  await assert.rejects(
    hooks.withStageBudget(
      timeoutTrace,
      "hung_fixture",
      Date.now() + 1000,
      25,
      () => new Promise(() => {}),
    ),
    (error) =>
      error.tripchordCode === "stage_timeout" &&
      error.tripchordDetails.stage === "hung_fixture",
  );
  assert.equal(timeoutTrace[0].stage, "hung_fixture");
  assert.equal(timeoutTrace[0].status, "timed_out");
  assert.equal(timeoutTrace[0].failure_code, "stage_timeout");

  const normalized = hooks.completionWithStageTrace(
    {
      state: "failed",
      quotes: [],
      failure: {
        code: "lodging_expected_place_preview_not_found",
        message: "fixture",
        retryable: false,
        page_url: "https://hotels.ctrip.com/hotels/list",
        captured_at: new Date().toISOString(),
        details: { source: "fixture" },
      },
      internal_diagnostic: "must-not-cross-the-bridge",
    },
    timeoutTrace,
  );
  assert.equal(normalized.failure.code, "extraction_error");
  assert.equal(
    normalized.failure.details.diagnostic_code,
    "lodging_expected_place_preview_not_found",
  );
  assert.equal(normalized.failure.details.source, "fixture");
  assert.equal(normalized.failure.details.stage_trace.length, 1);
  assert.equal("internal_diagnostic" in normalized, false);
  const confirmedEmpty = hooks.completionWithStageTrace(
    {
      state: "failed",
      quotes: [],
      failure: {
        code: "no_inventory",
        message: "exact query returned zero hotels",
        retryable: false,
        page_url: "https://hotel.qunar.com/city/i-ka_maafushi/",
        captured_at: new Date().toISOString(),
        details: { inventory_result_state: "confirmed_empty" },
      },
    },
    stageTrace,
  );
  assert.equal(confirmedEmpty.failure.code, "no_inventory");
  assert.equal(
    confirmedEmpty.failure.details.inventory_result_state,
    "confirmed_empty",
  );
  assert.equal(
    Object.prototype.hasOwnProperty.call(
      confirmedEmpty.failure.details,
      "diagnostic_code",
    ),
    false,
  );
  const normalizedSuccess = hooks.completionWithStageTrace(
    {
      state: "succeeded",
      quotes: [{ evidence_sha256: "fixture" }],
      detail_orchestration: { internal: true },
    },
    stageTrace,
  );
  assert.deepEqual(
    Object.keys(normalizedSuccess).sort(),
    ["quotes", "state"],
  );
}
assert.equal(
  hooks.providerVerticalUrlAllowed(
    "qunar",
    "lodging",
    "https://hotel.qunar.com/global/list",
  ),
  true,
);
assert.equal(
  hooks.providerVerticalUrlAllowed(
    "qunar",
    "lodging",
    "https://www.qunar.com/",
  ),
  false,
);
assert.equal(
  hooks.providerVerticalUrlAllowed(
    "fliggy",
    "flight",
    "https://sjipiao.fliggy.com/search",
  ),
  true,
);
assert.equal(
  hooks.providerVerticalUrlAllowed(
    "fliggy",
    "lodging",
    "https://sjipiao.fliggy.com/search",
  ),
  false,
);
assert.equal(
  hooks.providerVerticalUrlAllowed(
    "qunar",
    "flight",
    "https://flight.qunar.com/site/interroundtrip_compare.htm" +
      "?fromCode=HGH&toCode=MLE&adultNum=2",
  ),
  true,
);
assert.equal(
  hooks.providerVerticalUrlAllowed(
    "fliggy",
    "flight",
    "https://sijipiao.fliggy.com/ie/flight_search_result.htm",
  ),
  true,
);
assert.equal(
  hooks.providerVerticalUrlAllowed(
    "fliggy",
    "lodging",
    "https://sijipiao.fliggy.com/ie/flight_search_result.htm",
  ),
  false,
);
const fliggyHkHostDecision = hooks.providerHostDecision(
  "fliggy",
  "https://www.fliggy.hk/",
);
assert.equal(fliggyHkHostDecision.allowed, true);
assert.equal(fliggyHkHostDecision.reason, "allowed");
assert.equal(fliggyHkHostDecision.url.scheme, "https");
assert.equal(fliggyHkHostDecision.url.host, "www.fliggy.hk");
assert.equal(fliggyHkHostDecision.url.path_shape, "/");
const zhixingHostDecision = hooks.providerHostDecision(
  "zhixing",
  "https://m.suanya.com/h5/",
);
assert.equal(zhixingHostDecision.allowed, true);
assert.equal(zhixingHostDecision.reason, "allowed");
assert.equal(zhixingHostDecision.url.host, "m.suanya.com");
assert.equal(
  hooks.providerHostDecision(
    "zhixing",
    "https://market.suanya.cn/activity/public/",
  ).allowed,
  true,
);
assert.equal(
  hooks.providerHostDecision(
    "zhixing",
    "https://m.suanya.com/order/list",
  ).reason,
  "transaction_surface",
);
for (const kind of ["flight", "lodging"]) {
  assert.equal(
    hooks.providerVerticalUrlAllowed(
      "zhixing",
      kind,
      "https://m.suanya.com/h5/",
    ),
    false,
  );
}
assert.equal(
  hooks.providerVerticalUrlAllowed(
    "fliggy",
    "flight",
    "https://www.fliggy.hk/",
  ),
  true,
);
assert.equal(
  hooks.providerVerticalUrlAllowed(
    "fliggy",
    "lodging",
    "https://www.fliggy.hk/",
  ),
  true,
);
assert.equal(
  hooks.providerVerticalUrlAllowed(
    "fliggy",
    "flight",
    "https://flight.fliggy.hk/results",
  ),
  true,
);
assert.equal(
  hooks.providerVerticalUrlAllowed(
    "fliggy",
    "lodging",
    "https://hotel.fliggy.hk/results",
  ),
  true,
);
const fliggyLodgingQuery = {
  destination: "Maafushi",
  start_date: "2026-08-12",
  end_date: "2026-08-18",
  adults: 2,
  rooms: 1,
  options: { expected_lodging_place_key: "maafushi" },
};
const fliggyLodgingResultUrl =
  "https://hotel.fliggy.com/hotel_list3.htm" +
  "?spm=181.11358650.hotelModule.internationalSearch" +
  "&city=933081&cityName=%E9%A9%AC%E5%AF%8C%E5%A3%AB" +
  "&checkIn=2026-08-12&checkOut=2026-08-18" +
  "&keywords=&aNum_1=2&cNum_1=0";
const fliggyLodgingDecision = hooks.fliggyLodgingResultUrlDecision(
  fliggyLodgingResultUrl,
  fliggyLodgingQuery,
);
assert.equal(fliggyLodgingDecision.allowed, true);
assert.equal(fliggyLodgingDecision.reason, "allowed");
for (const [mutated, reason] of [
  [
    fliggyLodgingResultUrl.replace("city=933081", "city=934358"),
    "query_value_mismatch",
  ],
  [
    fliggyLodgingResultUrl.replace("aNum_1=2", "aNum_1=1"),
    "query_value_mismatch",
  ],
  [
    fliggyLodgingResultUrl + "&order=price",
    "query_shape_mismatch",
  ],
  [
    fliggyLodgingResultUrl.replace(
      "hotel.fliggy.com",
      "hotel.fliggy.com.evil.example",
    ),
    "wrong_surface",
  ],
]) {
  assert.equal(
    hooks.fliggyLodgingResultUrlDecision(
      mutated,
      fliggyLodgingQuery,
    ).reason,
    reason,
  );
}
const fliggyLoginRedirect =
  "https://login.taobao.com/havanaone/login/login.htm" +
  "?bizName=taobao&redirectURL=" +
  encodeURIComponent(
    "https://hotel.fliggy.com:443/hotel_list3.htm/" +
      "_____tmd_____/page/login_jump?rand=test",
  );
assert.equal(
  hooks.auditedProviderLoginRedirect(
    "fliggy",
    "lodging",
    fliggyLoginRedirect,
  ),
  true,
);
assert.equal(
  hooks.auditedProviderLoginRedirect(
    "fliggy",
    "flight",
    fliggyLoginRedirect,
  ),
  false,
);
assert.equal(
  hooks.auditedProviderLoginRedirect(
    "fliggy",
    "lodging",
    fliggyLoginRedirect.replace(
      "hotel.fliggy.com",
      "hotel.fliggy.com.evil.example",
    ),
  ),
  false,
);
const qunarLodgingQuery = {
  destination: "Maafushi",
  start_date: "2026-08-12",
  end_date: "2026-08-18",
  adults: 2,
  rooms: 1,
  options: { expected_lodging_place_key: "maafushi" },
};
const qunarLodgingResultUrl =
  "https://hotel.qunar.com/intl/search.jsp" +
  "?toCity=%E9%A9%AC%E5%AF%8C%E6%96%BD" +
  "&fromDate=2026-08-12&toDate=2026-08-18" +
  "&cityurl=i-ka_maafushi&from=globalhotelpages";
assert.equal(
  hooks.qunarLodgingResultUrlDecision(
    qunarLodgingResultUrl,
    qunarLodgingQuery,
  ).allowed,
  true,
);
for (const [mutated, reason] of [
  [
    qunarLodgingResultUrl.replace(
      "cityurl=i-ka_maafushi",
      "cityurl=i-hulhumale",
    ),
    "query_value_mismatch",
  ],
  [
    qunarLodgingResultUrl.replace(
      "toCity=%E9%A9%AC%E5%AF%8C%E6%96%BD",
      "toCity=Maafushi",
    ),
    "query_value_mismatch",
  ],
  [
    qunarLodgingResultUrl + "&q=hotel",
    "query_shape_mismatch",
  ],
  [
    qunarLodgingResultUrl.replace(
      "hotel.qunar.com",
      "hotel.qunar.com.evil.example",
    ),
    "wrong_surface",
  ],
]) {
  assert.equal(
    hooks.qunarLodgingResultUrlDecision(
      mutated,
      qunarLodgingQuery,
    ).reason,
    reason,
  );
}
const qunarResultPageUrl =
  "https://hotel.qunar.com/city/i-ka_maafushi/";
const qunarResultQueryReadback = {
  confirmed: true,
  reason: null,
  confirmed_query: {
    destination: "Maafushi",
    start_date: "2026-08-12",
    end_date: "2026-08-18",
    adults: 2,
    rooms: 1,
  },
  readback_query: {
    destination: "马富施",
    start_date: "2026-08-12",
    end_date: "2026-08-18",
    adults: 2,
    rooms: 1,
  },
  gates: {
    path_confirmed: true,
    search_form_visible: true,
    destination_control_unambiguous: true,
    destination_confirmed: true,
    date_controls_unambiguous: true,
    start_date_confirmed: true,
    end_date_confirmed: true,
    occupancy_control_unambiguous: true,
    adults_confirmed: true,
    children_confirmed: true,
    single_room_surface_confirmed: true,
  },
  evidence: {
    provider_destination_id: "i-ka_maafushi",
    result_path: "/city/i-ka_maafushi",
    destination_text: "马富施",
    start_date_text: "2026-08-12",
    end_date_text: "2026-08-18",
    occupancy_text: "每间人数 2成人 0儿童",
    room_scope: "audited_qunar_single_room_search_surface",
  },
};
assert.equal(
  hooks.qunarLodgingResultQueryReadbackDecision(
    qunarResultPageUrl,
    qunarLodgingQuery,
    qunarResultQueryReadback,
  ).allowed,
  true,
);
const qunarHulhumaleQuery = {
  ...qunarLodgingQuery,
  destination: "Hulhumalé",
  options: { expected_lodging_place_key: "hulhumale" },
};
const qunarHulhumaleReadback = {
  ...qunarResultQueryReadback,
  confirmed_query: {
    ...qunarResultQueryReadback.confirmed_query,
    destination: "Hulhumalé",
  },
  readback_query: {
    ...qunarResultQueryReadback.readback_query,
    destination: "胡鲁马累岛",
  },
  evidence: {
    ...qunarResultQueryReadback.evidence,
    provider_destination_id: "i-hulhumale",
    result_path: "/city/i-hulhumale",
    destination_text: "胡鲁马累岛",
  },
};
assert.equal(
  hooks.qunarLodgingResultQueryReadbackDecision(
    "https://hotel.qunar.com/city/i-hulhumale/",
    qunarHulhumaleQuery,
    qunarHulhumaleReadback,
  ).allowed,
  true,
);
assert.equal(
  hooks.qunarLodgingResultQueryReadbackDecision(
    "https://hotel.qunar.com/city/i-hulhumale/",
    qunarHulhumaleQuery,
    {
      ...qunarHulhumaleReadback,
      readback_query: {
        ...qunarHulhumaleReadback.readback_query,
        destination: "马富施",
      },
    },
  ).reason,
  "result_readback_value_mismatch",
);
for (const [mutatedUrl, mutatedReadback, reason] of [
  [
    "https://hotel.qunar.com/city/i-hulhumale/",
    qunarResultQueryReadback,
    "result_surface_mismatch",
  ],
  [
    qunarResultPageUrl,
    {
      ...qunarResultQueryReadback,
      readback_query: {
        ...qunarResultQueryReadback.readback_query,
        start_date: "2026-08-13",
      },
    },
    "result_readback_value_mismatch",
  ],
  [
    qunarResultPageUrl,
    {
      ...qunarResultQueryReadback,
      confirmed: false,
      reason: "qunar_result_adults_confirmed_failed",
    },
    "qunar_result_adults_confirmed_failed",
  ],
  [
    qunarResultPageUrl,
    {
      ...qunarResultQueryReadback,
      gates: {
        ...qunarResultQueryReadback.gates,
        children_confirmed: false,
      },
    },
    "result_readback_gate_failed",
  ],
]) {
  assert.equal(
    hooks.qunarLodgingResultQueryReadbackDecision(
      mutatedUrl,
      qunarLodgingQuery,
      mutatedReadback,
    ).reason,
    reason,
  );
}
{
  let nowMs = 0;
  let callCount = 0;
  const waits = [];
  const readback = await hooks.readQunarResultQueryWithRetry(
    71,
    {
      provider: "qunar",
      kind: "lodging",
      query: qunarLodgingQuery,
    },
    2000,
    new Set([71]),
    async () => {
      callCount += 1;
      return callCount < 3
        ? {
            confirmed: false,
            reason: "qunar_result_search_form_missing",
          }
        : qunarResultQueryReadback;
    },
    async (waitMs) => {
      waits.push(waitMs);
      nowMs += waitMs;
    },
    () => nowMs,
  );
  assert.equal(readback.confirmed, true);
  assert.equal(callCount, 3);
  assert.deepEqual(waits, [
    hooks.QUNAR_RESULT_QUERY_READBACK_POLL_MS,
    hooks.QUNAR_RESULT_QUERY_READBACK_POLL_MS,
  ]);
}
{
  let callCount = 0;
  let waitCount = 0;
  const mismatch = await hooks.readQunarResultQueryWithRetry(
    72,
    {
      provider: "qunar",
      kind: "lodging",
      query: qunarLodgingQuery,
    },
    2000,
    new Set([72]),
    async () => {
      callCount += 1;
      return {
        confirmed: false,
        reason: "qunar_result_start_date_confirmed_failed",
      };
    },
    async () => {
      waitCount += 1;
    },
    () => 0,
  );
  assert.equal(mismatch.reason, "qunar_result_start_date_confirmed_failed");
  assert.equal(callCount, 1);
  assert.equal(waitCount, 0);
}
assert.equal(
  hooks.exactLodgingQueryConfirmed(qunarLodgingQuery, {
    provider: "qunar",
    triggered: true,
    confirmation_scope: "confirmed_visible_search",
    confirmed_query: { ...qunarLodgingQuery },
  }),
  false,
);
assert.equal(
  hooks.exactLodgingQueryConfirmed(qunarLodgingQuery, {
    provider: "qunar",
    triggered: true,
    confirmation_scope: "confirmed_visible_search",
    confirmed_query: { ...qunarLodgingQuery },
    result_query_readback_confirmed: true,
  }),
  true,
);

{
  const query = {
    ...qunarLodgingQuery,
    currency: "CNY",
    options: {
      expected_lodging_place_key: "maafushi",
      expected_package_area: "destination_island",
      segment: "full",
    },
  };
  const driver = {
    provider: "qunar",
    mode: "visible_form",
    triggered: true,
    confirmation_scope: "confirmed_visible_search",
    confirmed_query: { ...query },
    readback_query: { ...query, destination: "马富施" },
    result_query_readback_confirmed: true,
    result_query_readback_scope: "qunar_visible_result_form_fields",
    result_query_readback_evidence: {
      provider_destination_id: "i-ka_maafushi",
      result_path: "/city/i-ka_maafushi",
      destination_text: "马富施",
      start_date_text: "2026-08-12",
      end_date_text: "2026-08-18",
      occupancy_text: "每间人数 2成人 0儿童",
      room_scope: "audited_qunar_single_room_search_surface",
    },
    browser_isolation: hooks.qunarLodgingIsolationEvidence({
      lifecycle_state: "active",
    }),
  };
  const receiptAt = async (capturedAt) => {
    const receipt = {
      schema_version: hooks.LODGING_INVENTORY_RECEIPT_SCHEMA_VERSION,
      parser_version: hooks.LODGING_INVENTORY_RECEIPT_PARSER_VERSION,
      provider: "qunar",
      state: "confirmed_empty",
      confirmed_query: {
        destination: query.destination,
        start_date: query.start_date,
        end_date: query.end_date,
        adults: query.adults,
        rooms: query.rooms,
        options: { ...query.options },
      },
      confirmation_scope: "confirmed_visible_search",
      scan_limit: 12,
      scanned_count: 0,
      candidate_summaries: [],
      explicit_empty_evidence: {
        contract_version: "qunar-visible-zero-inventory-v1",
        result_count_text: "共 0 家酒店满足条件",
        empty_message: "很抱歉，没有找到相关的酒店",
      },
      provider_pending_evidence: null,
      page_url: "https://hotel.qunar.com/city/i-ka_maafushi/",
      captured_at: capturedAt,
    };
    const receiptSha256 = await hooks.inventoryReceiptSha256(
      hooks.canonicalInventoryJson(receipt),
    );
    return {
      state: "failed",
      quotes: [],
      failure: {
        code: "no_inventory",
        message: "fixture exact empty",
        retryable: false,
        page_url: receipt.page_url,
        captured_at: capturedAt,
        details: {
          parser_version: hooks.LODGING_INVENTORY_RECEIPT_PARSER_VERSION,
          inventory_result_state: "confirmed_empty",
          confirmed_exhaustive: true,
          scanned_count: 0,
          candidate_summaries: [],
          inventory_receipt: receipt,
          inventory_receipt_sha256: receiptSha256,
        },
      },
    };
  };
  const first = await receiptAt("2026-08-04T12:00:00.000Z");
  const second = await receiptAt("2026-08-04T12:00:02.000Z");
  const fixedObservationLineage = {
    schema_version: hooks.QUNAR_OBSERVATION_LINEAGE_VERSION,
    isolation_scope: hooks.QUNAR_LODGING_ISOLATION_SCOPE,
    runtime_lineage_sha256: "1".repeat(64),
    window_lineage_sha256: "2".repeat(64),
    tab_lineage_sha256: "3".repeat(64),
  };
  const fixedLineageResolver = async () => fixedObservationLineage;
  const pendingReceiptAt = async (
    capturedAt,
    observedDurationMs = 28391,
  ) => {
    const receipt = {
      schema_version: hooks.LODGING_INVENTORY_RECEIPT_SCHEMA_VERSION,
      parser_version: hooks.LODGING_INVENTORY_RECEIPT_PARSER_VERSION,
      provider: "qunar",
      state: "bounded_provider_pending",
      confirmed_query: {
        destination: query.destination,
        start_date: query.start_date,
        end_date: query.end_date,
        adults: query.adults,
        rooms: query.rooms,
        options: { ...query.options },
      },
      confirmation_scope: "confirmed_visible_search",
      scan_limit: 12,
      scanned_count: 0,
      candidate_summaries: [],
      explicit_empty_evidence: null,
      provider_pending_evidence: {
        contract_version: "qunar-visible-search-pending-v1",
        result_count_text: "共 家酒店满足条件",
        pending_message: "请稍等,您查询的结果正在实时搜索中...",
        observed_duration_ms: observedDurationMs,
      },
      page_url: "https://hotel.qunar.com/city/i-ka_maafushi/",
      captured_at: capturedAt,
    };
    const receiptSha256 = await hooks.inventoryReceiptSha256(
      hooks.canonicalInventoryJson(receipt),
    );
    return {
      state: "failed",
      quotes: [],
      failure: {
        code: "extraction_error",
        message: "fixture bounded provider pending",
        retryable: false,
        page_url: receipt.page_url,
        captured_at: capturedAt,
        details: {
          parser_version: hooks.LODGING_INVENTORY_RECEIPT_PARSER_VERSION,
          inventory_result_state: "bounded_provider_pending",
          confirmed_exhaustive: false,
          scanned_count: 0,
          candidate_summaries: [],
          inventory_receipt: receipt,
          inventory_receipt_sha256: receiptSha256,
        },
      },
    };
  };
  const pending = await pendingReceiptAt(
    "2026-08-04T12:00:28.391Z",
  );
  assert.equal(
    (
      await hooks.validateQunarParserConfirmedEmptyReceipt(
        first.failure.details.inventory_receipt,
        first.failure.details.inventory_receipt_sha256,
        query,
        driver,
      )
    ).valid,
    true,
  );
  assert.equal(
    hooks.shouldTryQunarLodgingDetailOrchestration(
      first,
      { provider: "qunar", kind: "lodging", query },
      "https://hotel.qunar.com/city/i-ka_maafushi/",
      driver,
    ),
    true,
  );
  assert.equal(
    hooks.shouldTryQunarLodgingDetailOrchestration(
      first,
      { provider: "qunar", kind: "lodging", query },
      "https://hotel.qunar.com/city/i-ka_maafushi/",
      { ...driver, result_query_readback_confirmed: false },
    ),
    false,
  );
  const targets = hooks.qunarAuditedLodgingDetailTargets(query);
  assert.equal(
    targets.map(({ property_id }) => property_id).join(","),
    "2075,2142",
  );
  assert.equal(targets.length, 2);
  assert.equal(targets[0].seed_selection_offset, 4);
  assert.equal(
    targets.every(
      ({ seed_selection_policy }) =>
        seed_selection_policy === hooks.QUNAR_DETAIL_SEED_SELECTION_POLICY,
    ),
    true,
  );
  assert.deepEqual(
    hooks.qunarAuditedLodgingDetailTargets({
      ...query,
      options: { ...query.options, ignored_diagnostic_hint: "same-seed" },
    }).map(({ property_id }) => property_id),
    targets.map(({ property_id }) => property_id),
  );
  const nextDateTargets = hooks.qunarAuditedLodgingDetailTargets({
    ...query,
    start_date: "2026-08-13",
    end_date: "2026-08-19",
  });
  assert.equal(
    nextDateTargets.map(({ property_id }) => property_id).join(","),
    "2112,2055",
  );
  assert.notEqual(
    nextDateTargets.map(({ property_id }) => property_id).join(","),
    targets.map(({ property_id }) => property_id).join(","),
  );
  const augustAuditedSeeds = new Set();
  for (let day = 1; day <= 24; day += 1) {
    const startDay = String(day).padStart(2, "0");
    const endDay = String(day + 6).padStart(2, "0");
    for (const target of hooks.qunarAuditedLodgingDetailTargets({
      ...query,
      start_date: `2026-08-${startDay}`,
      end_date: `2026-08-${endDay}`,
    })) {
      augustAuditedSeeds.add(target.property_id);
    }
  }
  assert.equal(
    [...augustAuditedSeeds].sort().join(","),
    ["2055", "2071", "2072", "2075", "2112", "2142"].join(","),
  );
  assert.equal(
    targets[0].href,
    "https://hotel.qunar.com/city/i-ka_maafushi/dt-2075/" +
      "?#fromDate=2026-08-12&toDate=2026-08-18&q=&showMap=0",
  );
  assert.equal(
    hooks.qunarLodgingDetailUrlDecision(
      targets[0].href,
      query,
      targets[0],
    ).allowed,
    true,
  );
  for (const [url, reason] of [
    [targets[0].href.replace("dt-2075", "dt-9999"), "property_not_allowlisted"],
    [targets[0].href.replace("q=", "q=hotel"), "detail_hash_value_mismatch"],
    [`${targets[0].href}&tracking=1`, "detail_hash_shape_mismatch"],
    [targets[0].href.replace("hotel.qunar.com", "hotel.qunar.com.evil.test"), "wrong_surface_or_query"],
  ]) {
    assert.equal(
      hooks.qunarLodgingDetailUrlDecision(url, query, targets[0]).reason,
      reason,
    );
  }
  let clockMs = 0;
  const stable = await hooks.stabilizeQunarExplicitEmpty({
    extraction: first,
    lease: { task_id: "qunar-empty-stable", query },
    driver,
    tabId: 1,
    deadline: Date.now() + 10000,
    ownedTabIds: new Set([1]),
    extractor: async () => second,
    wait: async (ms) => {
      clockMs += ms;
    },
    clock: () => clockMs,
    assertActive: async () => {},
    lineageResolver: fixedLineageResolver,
  });
  assert.equal(stable.state, "stable_confirmed_empty");
  assert.equal(stable.diagnostic.observation_count, 2);
  assert.equal(stable.diagnostic.receipt_interval_ms, 2000);

  clockMs = 0;
  const hydrated = await hooks.stabilizeQunarExplicitEmpty({
    extraction: first,
    lease: { task_id: "qunar-empty-hydrated", query },
    driver,
    tabId: 1,
    deadline: Date.now() + 10000,
    ownedTabIds: new Set([1]),
    extractor: async () => ({
      state: "succeeded",
      quotes: [{ provider: "qunar", amount: 888 }],
    }),
    wait: async (ms) => {
      clockMs += ms;
    },
    clock: () => clockMs,
    assertActive: async () => {},
    lineageResolver: fixedLineageResolver,
  });
  assert.equal(hydrated.state, "inventory_hydrated");
  assert.equal(hydrated.extraction.quotes.length, 1);
  assert.equal(
    (
      await hooks.validateQunarParserInventoryReceipt(
        pending.failure.details.inventory_receipt,
        pending.failure.details.inventory_receipt_sha256,
        query,
        driver,
      )
    ).inventory_state,
    "bounded_provider_pending",
  );
  assert.equal(
    hooks.shouldTryQunarLodgingDetailOrchestration(
      pending,
      { provider: "qunar", kind: "lodging", query },
      "https://hotel.qunar.com/city/i-ka_maafushi/",
      driver,
    ),
    true,
  );
  let pendingWaitCount = 0;
  let pendingExtractorCount = 0;
  const pendingObservation =
    await hooks.observeQunarInventoryForDetailFallback({
      extraction: pending,
      lease: { task_id: "qunar-pending-observation", query },
      driver,
      tabId: 1,
      deadline: Date.now() + 10000,
      ownedTabIds: new Set([1]),
      extractor: async () => {
        pendingExtractorCount += 1;
        return second;
      },
      wait: async () => {
        pendingWaitCount += 1;
      },
      assertActive: async () => {},
    });
  assert.equal(pendingObservation.state, "bounded_provider_pending");
  assert.equal(
    pendingObservation.diagnostic.inventory_observation_state,
    "bounded_provider_pending",
  );
  assert.equal(pendingObservation.diagnostic.observed_duration_ms, 28391);
  assert.equal(pendingWaitCount, 0);
  assert.equal(pendingExtractorCount, 0);
  for (const invalidPending of [
    await pendingReceiptAt("2026-08-04T12:00:24.999Z", 24999),
    await pendingReceiptAt("2026-08-04T12:02:00.001Z", 120001),
  ]) {
    assert.equal(
      (
        await hooks.validateQunarParserInventoryReceipt(
          invalidPending.failure.details.inventory_receipt,
          invalidPending.failure.details.inventory_receipt_sha256,
          query,
          driver,
        )
      ).valid,
      false,
    );
  }
  const tamperedPendingSha = JSON.parse(JSON.stringify(pending));
  tamperedPendingSha.failure.details.inventory_receipt_sha256 = "0".repeat(64);
  assert.equal(
    (
      await hooks.observeQunarInventoryForDetailFallback({
        extraction: tamperedPendingSha,
        lease: { task_id: "qunar-pending-sha-tamper", query },
        driver,
        tabId: 1,
        deadline: Date.now() + 10000,
        ownedTabIds: new Set([1]),
        wait: async () => {},
        assertActive: async () => {},
      })
    ).state,
    "invalid_inventory_observation",
  );
  const wrongReadbackPendingObservation =
    await hooks.observeQunarInventoryForDetailFallback({
      extraction: pending,
      lease: { task_id: "qunar-pending-readback-tamper", query },
      driver: {
        ...driver,
        readback_query: { ...driver.readback_query, adults: 1 },
      },
      tabId: 1,
      deadline: Date.now() + 10000,
      ownedTabIds: new Set([1]),
      wait: async () => {},
      assertActive: async () => {},
    });
  assert.equal(
    wrongReadbackPendingObservation.state,
    "invalid_inventory_observation",
  );

  const isolatedWindowId = 88;
  const isolatedTabId = 188;
  browserWindows.set(isolatedWindowId, {
    id: isolatedWindowId,
    focused: false,
    state: "normal",
    type: "normal",
  });
  tabs.set(isolatedTabId, {
    id: isolatedTabId,
    windowId: isolatedWindowId,
    active: true,
    status: "complete",
    url: "https://hotel.qunar.com/city/i-ka_maafushi/",
  });
  const createdTabsBefore = createdTabRequests.length;
  const createdWindowsBefore = createdWindowRequests.length;
  const windowUpdatesBefore = windowUpdates.length;
  const tabUpdatesBefore = tabUpdates.length;
  clockMs = 0;
  const detailTabIds = [];
  const orchestrated = await hooks.orchestrateQunarLodgingDetails(
    isolatedTabId,
    {
      task_id: "qunar-stable-empty-detail-fallback",
      provider: "qunar",
      kind: "lodging",
      query,
    },
    driver,
    Date.now() + 30000,
    new Set([isolatedTabId]),
    first,
    {
      extractor: async () => second,
      wait: async (ms) => {
        clockMs += ms;
      },
      clock: () => clockMs,
      assertActive: async () => {},
      lineageResolver: fixedLineageResolver,
      detailExtractor: async (tabId, _lease, detailDriver) => {
        detailTabIds.push(tabId);
        assert.equal(tabId, isolatedTabId);
        assert.equal(detailDriver.mode, "captured_read_only_detail");
        assert.equal(detailDriver.qunar_detail_capture.clicked_booking, false);
        assert.equal(
          detailDriver.qunar_detail_capture.seed_selection_policy,
          hooks.QUNAR_DETAIL_SEED_SELECTION_POLICY,
        );
        assert.equal(
          detailDriver.qunar_detail_capture.seed_selection_offset,
          targets[0].seed_selection_offset,
        );
        assert.deepEqual(
          detailDriver.qunar_detail_capture.target_property_ids,
          targets.map(({ property_id }) => property_id),
        );
        assert.equal(
          detailDriver.qunar_detail_capture.inventory_observation_state,
          "confirmed_empty",
        );
        assert.equal(
          detailDriver.qunar_detail_capture.inventory_observation_count,
          2,
        );
        assert.equal(
          detailDriver.qunar_detail_capture.inventory_observation_duration_ms,
          2000,
        );
        const pageUrl = (await chrome.tabs.get(tabId)).url;
        const propertyId = /\/dt-(\d+)\//.exec(pageUrl)[1];
        const selectedTarget = targets.find(
          (target) => target.property_id === propertyId,
        );
        assert.ok(selectedTarget);
        const propertyName = selectedTarget.property_name;
        const targetIndex = targets.findIndex(
          (target) => target.property_id === propertyId,
        );
        return {
          state: "succeeded",
          quotes: [{
            provider: "qunar",
            kind: "lodging",
            page_url: pageUrl,
            amount: 888 + targetIndex * 11,
            currency: "CNY",
            price_basis: "per_night",
            taxes_included: true,
            title: propertyName,
            visible_evidence: `fixture-${propertyId}`,
            evidence_sha256: "b".repeat(64),
            details: {
              extraction: "visible_dom_qunar_lodging_detail",
              price_basis_source:
                "audited_qunar_lodging_detail_rate_contract",
              price_finality: "final_for_rate",
              tax_evidence: "含税最终价",
              city_slug: "i-ka_maafushi",
              hotel_seq: `i-ka_maafushi_${propertyId}`,
              property_id: propertyId,
              property_name: propertyName,
              room_text: "Deluxe Double Room",
              rate_text: "含税最终价 CNY 888 每晚",
              price_unit_evidence: "含税最终价 CNY 888 每晚",
              availability: "available",
              availability_text: "预订",
              expected_lodging_place_key: "maafushi",
              observed_lodging_place_key: "maafushi",
              lodging_place_matches_expected: true,
              kaafu_area_confirmed: true,
              check_in: query.start_date,
              check_out: query.end_date,
              adults: query.adults,
              rooms: query.rooms,
              clicked_booking: false,
            },
          }],
        };
      },
    },
  );
  assert.equal(
    orchestrated.state,
    "succeeded",
    JSON.stringify(orchestrated, null, 2),
  );
  assert.equal(orchestrated.quotes.length, 2);
  assert.equal(
    orchestrated.detail_orchestration.seed_selection_policy,
    hooks.QUNAR_DETAIL_SEED_SELECTION_POLICY,
  );
  assert.equal(
    orchestrated.detail_orchestration.selected_seed_set.map(
      ({ property_id }) => property_id,
    ).join(","),
    targets.map(({ property_id }) => property_id).join(","),
  );
  const sealedCapture =
    orchestrated.quotes[0].details.driver.qunar_detail_capture;
  assert.equal(
    sealedCapture.list_inventory_receipt.schema_version,
    hooks.LODGING_INVENTORY_SEALED_RECEIPT_SCHEMA_VERSION,
  );
  assert.equal(
    sealedCapture.inventory_observation_chain_schema_version,
    hooks.QUNAR_EXPLICIT_EMPTY_OBSERVATION_CHAIN_VERSION,
  );
  assert.equal(
    (
      await hooks.validateQunarSealedConfirmedEmptyReceipt(
        sealedCapture.list_inventory_receipt,
        sealedCapture.list_inventory_receipt_sha256,
        query,
        driver,
      )
    ).valid,
    true,
  );
  const sealedObservations =
    sealedCapture.list_inventory_receipt.observation_chain.observations;
  assert.equal(sealedObservations.length, 2);
  assert.equal(
    sealedObservations[0].receipt_sha256,
    first.failure.details.inventory_receipt_sha256,
  );
  assert.equal(
    sealedObservations[1].receipt_sha256,
    second.failure.details.inventory_receipt_sha256,
  );
  assert.deepEqual(detailTabIds, [isolatedTabId, isolatedTabId]);
  assert.equal(createdTabRequests.length, createdTabsBefore);
  assert.equal(createdWindowRequests.length, createdWindowsBefore);
  assert.equal(windowUpdates.length, windowUpdatesBefore);
  assert.equal(
    tabUpdates.slice(tabUpdatesBefore).every(
      ({ tabId, update }) =>
        tabId === isolatedTabId &&
        update.active === undefined &&
        typeof update.url === "string",
    ),
    true,
  );
  assert.equal(browserWindows.get(isolatedWindowId).focused, false);
  await chrome.tabs.update(isolatedTabId, {
    url: "https://hotel.qunar.com/city/i-ka_maafushi/",
  });
  const pendingCreatedTabsBefore = createdTabRequests.length;
  const pendingCreatedWindowsBefore = createdWindowRequests.length;
  const pendingWindowUpdatesBefore = windowUpdates.length;
  const pendingTabUpdatesBefore = tabUpdates.length;
  const pendingDetailDrivers = [];
  let pendingDetailActive = 0;
  let pendingDetailMaxActive = 0;
  const expectedRateDiagnostics = {
    schema_version: "tripchord-qunar-rate-diagnostics-v1",
    scanned_node_count: 24,
    scan_truncated: false,
    rate_row_count: 0,
    atomic_final_price_row_count: 0,
    strict_availability_control_count: 0,
    diagnostic_visible_control_count: 2,
    diagnostic_control_samples: ["查看房型", "选择房间"],
    visible_currency_amount_node_count: 1,
    visible_currency_amount_samples: ["CNY 888 起"],
    rejection_counts: {
      atomic_currency_amount: 1,
      price_basis: 0,
      final_marker: 0,
      tax_inclusion: 0,
      availability: 0,
      room_text: 0,
    },
  };
  const expectedDomDiagnostics = {
    scope: "visible_candidate_cards_only",
    candidates: [{
      tag: "section",
      text_summary: "房型价格区域",
      price_anchor_hits: 1,
      action_anchor_hits: 2,
    }],
  };
  const pendingOrchestrated = await hooks.orchestrateQunarLodgingDetails(
    isolatedTabId,
    {
      task_id: "qunar-bounded-pending-detail-fallback",
      provider: "qunar",
      kind: "lodging",
      query,
    },
    driver,
    Date.now() + 30000,
    new Set([isolatedTabId]),
    pending,
    {
      wait: async () => {
        throw new Error("bounded pending must not masquerade as empty stabilization");
      },
      assertActive: async () => {},
      detailExtractor: async (tabId, _lease, detailDriver) => {
        assert.equal(tabId, isolatedTabId);
        pendingDetailActive += 1;
        pendingDetailMaxActive = Math.max(
          pendingDetailMaxActive,
          pendingDetailActive,
        );
        pendingDetailDrivers.push(detailDriver.qunar_detail_capture);
        await Promise.resolve();
        pendingDetailActive -= 1;
        return {
          state: "failed",
          quotes: [],
          failure: {
            code: "no_inventory",
            message: "fixture detail has no verified rate",
            retryable: false,
            details: {
              rate_row_count: 0,
              exact_price_row_count: 0,
              room_rate_contract: false,
              rate_diagnostics: expectedRateDiagnostics,
              dom_diagnostics: expectedDomDiagnostics,
            },
          },
        };
      },
    },
  );
  assert.equal(pendingOrchestrated.state, "failed");
  assert.equal(
    pendingOrchestrated.failure.details.inventory_result_state,
    "bounded_provider_pending",
  );
  assert.equal(
    pendingOrchestrated.failure.details.detail_orchestration.state,
    "bounded_pending_no_verified_detail_quote",
  );
  assert.equal(pendingDetailDrivers.length, 2);
  assert.equal(pendingDetailMaxActive, 1);
  assert.equal(
    pendingOrchestrated.failure.details.detail_orchestration
      .seed_selection_policy,
    hooks.QUNAR_DETAIL_SEED_SELECTION_POLICY,
  );
  assert.equal(
    pendingOrchestrated.failure.details.detail_orchestration
      .selected_seed_set.map(({ property_id }) => property_id).join(","),
    targets.map(({ property_id }) => property_id).join(","),
  );
  for (const detailResult of
    pendingOrchestrated.failure.details.detail_orchestration.detail_results) {
    assert.equal(detailResult.rate_row_count, 0);
    assert.equal(detailResult.exact_price_row_count, 0);
    assert.equal(detailResult.room_rate_contract, false);
    assert.deepEqual(detailResult.rate_diagnostics, expectedRateDiagnostics);
    assert.deepEqual(detailResult.dom_diagnostics, expectedDomDiagnostics);
  }
  for (const capture of pendingDetailDrivers) {
    assert.equal(capture.clicked_booking, false);
    assert.equal(capture.same_controlled_tab, true);
    assert.equal(
      capture.seed_selection_policy,
      hooks.QUNAR_DETAIL_SEED_SELECTION_POLICY,
    );
    assert.equal(capture.seed_selection_offset, targets[0].seed_selection_offset);
    assert.deepEqual(
      capture.target_property_ids,
      targets.map(({ property_id }) => property_id),
    );
    assert.equal(
      capture.inventory_observation_state,
      "bounded_provider_pending",
    );
    assert.equal(capture.inventory_observation_count, 1);
    assert.equal(capture.inventory_observation_duration_ms, 28391);
  }
  assert.equal(createdTabRequests.length, pendingCreatedTabsBefore);
  assert.equal(createdWindowRequests.length, pendingCreatedWindowsBefore);
  assert.equal(windowUpdates.length, pendingWindowUpdatesBefore);
  assert.equal(
    tabUpdates.slice(pendingTabUpdatesBefore).every(
      ({ tabId, update }) =>
        tabId === isolatedTabId &&
        update.active === undefined &&
        typeof update.url === "string",
    ),
    true,
  );
  assert.equal(browserWindows.get(isolatedWindowId).focused, false);
  tabs.delete(isolatedTabId);
  browserWindows.delete(isolatedWindowId);
}

const tongchengLodgingQuery = {
  destination: "Maafushi",
  start_date: "2026-08-19",
  end_date: "2026-08-25",
  adults: 2,
  rooms: 1,
  options: { expected_lodging_place_key: "maafushi" },
};
const tongchengLodgingResultUrl =
  "https://www.ly.com/hotel/hotellist" +
  "?city=110018575&inDate=2026-08-19&outDate=2026-08-25" +
  "&adultsNumber=2&roomNum=1&intl=1";
assert.equal(
  hooks.tongchengLodgingResultUrlDecision(
    tongchengLodgingResultUrl,
    tongchengLodgingQuery,
  ).allowed,
  true,
);
const tongchengElongFallbackUrl =
  "https://m.elong.com/ihotel/hotellist" +
  "?city=110018575&indate=2026-08-19&outdate=2026-08-25" +
  "&adultsNumber=2&roomNum=1&intl=1";
assert.equal(
  hooks.tongchengElongFallbackResultUrl(tongchengLodgingQuery),
  tongchengElongFallbackUrl,
);
const tongchengLyFallbackUrl = tongchengElongFallbackUrl
  .replace("m.elong.com/ihotel", "m.ly.com/hotel");
assert.equal(
  hooks.tongchengLyFallbackResultUrl(tongchengLodgingQuery),
  tongchengLyFallbackUrl,
);
assert.equal(
  hooks.tongchengLodgingResultUrlDecision(
    tongchengLyFallbackUrl,
    tongchengLodgingQuery,
  ).allowed,
  true,
);
assert.equal(
  hooks.tongchengLodgingResultUrlDecision(
    tongchengElongFallbackUrl,
    tongchengLodgingQuery,
  ).allowed,
  true,
);
for (const mutated of [
  tongchengElongFallbackUrl.replace("adultsNumber=2", "adultsNumber=1"),
  tongchengElongFallbackUrl.replace("indate=2026-08-19", "indate=2026-08-20"),
  `${tongchengElongFallbackUrl}&coupon=1`,
  tongchengElongFallbackUrl.replace("m.elong.com", "m.elong.com.evil.example"),
]) {
  assert.equal(
    hooks.tongchengLodgingResultUrlDecision(
      mutated,
      tongchengLodgingQuery,
    ).allowed,
    false,
  );
}
const tongchengDetailUrl =
  "https://www.ly.com/hotel/hoteldetail" +
  "?hotelId=123456&inDate=2026-08-19&outDate=2026-08-25" +
  "&traceToken=opaque-read-only&cityName=Maafushi" +
  "&countryName=Maldives&intl=1&adultsNumber=2" +
  "&beforePrice=779&prc=739&listHotelMinPriceExclTax=739" +
  "&cury=CNY&productLabelType200=read-only-label&isFirst=1";
assert.equal(
  hooks.tongchengLodgingDetailUrlDecision(
    tongchengDetailUrl,
    tongchengLodgingResultUrl,
    tongchengLodgingQuery,
  ).allowed,
  true,
);
const tongchengLowercaseDetailUrl = (() => {
  const parsed = new URL(tongchengDetailUrl);
  const lowered = new URLSearchParams();
  for (const [key, value] of parsed.searchParams) {
    lowered.append(key.toLowerCase(), value);
  }
  parsed.search = lowered.toString();
  return parsed.href;
})();
assert.equal(
  hooks.tongchengLodgingDetailUrlDecision(
    tongchengLowercaseDetailUrl,
    tongchengLodgingResultUrl,
    tongchengLodgingQuery,
  ).allowed,
  true,
);
for (const mutated of [
  tongchengDetailUrl.replace("hotelId=123456", "hotelId=bad"),
  tongchengDetailUrl.replace("outDate=2026-08-25", "outDate=2026-08-24"),
  tongchengDetailUrl.replace("adultsNumber=2", "adultsNumber=1"),
  tongchengDetailUrl.replace("isFirst=1", "isFirst=maybe"),
  `${tongchengDetailUrl}&coupon=1`,
  tongchengDetailUrl.replace("www.ly.com", "www.ly.com.evil.example"),
]) {
  assert.equal(
    hooks.tongchengLodgingDetailUrlDecision(
      mutated,
      tongchengLodgingResultUrl,
      tongchengLodgingQuery,
    ).allowed,
    false,
  );
}
for (const [mutated, reason] of [
  [
    tongchengLodgingResultUrl.replace("city=110018575", "city=110018578"),
    "query_value_mismatch",
  ],
  [
    tongchengLodgingResultUrl.replace("adultsNumber=2", "adultsNumber=1"),
    "query_value_mismatch",
  ],
  [tongchengLodgingResultUrl + "&coupon=1", "query_value_mismatch"],
  [
    tongchengLodgingResultUrl.replace("www.ly.com", "www.ly.com.evil.example"),
    "wrong_surface",
  ],
]) {
  assert.equal(
    hooks.tongchengLodgingResultUrlDecision(mutated, tongchengLodgingQuery).reason,
    reason,
  );
}
const tongchengFlightResultUrl =
  "https://www.ly.com/eliflight/book1.html" +
  "?para=HGH*MLE*2026-08-19*2026-08-25*RT*2_0_0*Y%7CS%7CC%7CF" +
  "&departureCity=%E6%9D%AD%E5%B7%9E" +
  "&arrivalCity=%E9%A9%AC%E7%B4%AF";
const tongchengFlightLoginUrl =
  "https://secure.elong.com/passport/login_cn.html?nexturl=" +
  encodeURIComponent(tongchengFlightResultUrl);
assert.equal(
  hooks.auditedProviderLoginRedirect(
    "tongcheng",
    "flight",
    tongchengFlightLoginUrl,
  ),
  true,
);
assert.equal(
  hooks.auditedProviderLoginRedirect(
    "tongcheng",
    "flight",
    tongchengFlightLoginUrl.replace("www.ly.com", "www.ly.com.evil.example"),
  ),
  false,
);
const qunarLoginRedirect =
  "https://user.qunar.com/passport/login.jsp?ret=" +
  encodeURIComponent(qunarLodgingResultUrl);
assert.equal(
  hooks.auditedProviderLoginRedirect(
    "qunar",
    "lodging",
    qunarLoginRedirect,
  ),
  true,
);
assert.equal(
  hooks.auditedProviderLoginRedirect(
    "qunar",
    "lodging",
    "https://user.qunar.com/passport/login.jsp?ret=" +
      encodeURIComponent(encodeURIComponent(qunarLodgingResultUrl)),
  ),
  true,
);
assert.equal(
  hooks.auditedProviderLoginRedirect(
    "qunar",
    "lodging",
    "https://user.qunar.com/passport/login.jsp?ret=" +
      encodeURIComponent(
        encodeURIComponent(encodeURIComponent(qunarLodgingResultUrl)),
      ),
  ),
  true,
);
assert.equal(
  hooks.auditedProviderLoginRedirect(
    "qunar",
    "flight",
    qunarLoginRedirect,
  ),
  false,
);
const qunarGlobalLoginRedirect =
  "https://user.qunar.com/passport/login.jsp?ret=" +
  encodeURIComponent("https://hotel.qunar.com/global/");
assert.equal(
  hooks.auditedProviderLoginRedirect(
    "qunar",
    "lodging",
    qunarGlobalLoginRedirect,
  ),
  true,
);
for (const exactCityTarget of [
  "https://hotel.qunar.com/city/i-ka_maafushi#checkIn=2026-08-12",
  "https://hotel.qunar.com/city/i-ka_maafushi/#checkIn=2026-08-12",
  "https://hotel.qunar.com/city/i-hulhumale#checkIn=2026-08-12",
]) {
  assert.equal(
    hooks.auditedProviderLoginRedirect(
      "qunar",
      "lodging",
      "https://user.qunar.com/passport/login.jsp?ret=" +
        encodeURIComponent(exactCityTarget),
    ),
    true,
  );
}
for (const unsafeReturnTarget of [
  "https://hotel.qunar.com/global/?next=order",
  "https://hotel.qunar.com/global/#account",
  "https://hotel.qunar.com/city/i-unknown#checkIn=2026-08-12",
  "https://hotel.qunar.com.evil.example/global/",
  "https://user:secret@hotel.qunar.com/global/",
]) {
  assert.equal(
    hooks.auditedProviderLoginRedirect(
      "qunar",
      "lodging",
      "https://user.qunar.com/passport/login.jsp?ret=" +
        encodeURIComponent(unsafeReturnTarget),
    ),
    false,
  );
}
for (const outsideUrl of [
  "https://fliggy.hk.evil.example/",
  "https://www.taobao.com/",
  "https://travel.alibaba.com/",
]) {
  assert.equal(
    hooks.providerHostDecision("fliggy", outsideUrl).reason,
    "outside_provider_host",
  );
}
assert.equal(
  hooks.providerHostDecision(
    "fliggy",
    "https://www.fliggy.hk/order/create",
  ).reason,
  "transaction_surface",
);
const ctripLodgingListUrl =
  "https://hotels.ctrip.com/hotels/list" +
  "?countryId=1&cityId=705914&checkin=2026-08-23" +
  "&checkout=2026-08-30";
const ctripLodgingListDecision = hooks.providerHostDecision(
  "ctrip",
  ctripLodgingListUrl,
);
assert.equal(ctripLodgingListDecision.allowed, true);
assert.equal(ctripLodgingListDecision.reason, "allowed");
assert.equal(ctripLodgingListDecision.url.host, "hotels.ctrip.com");
assert.equal(ctripLodgingListDecision.url.path_shape, "/hotels/list");
assert.equal(
  hooks.providerVerticalUrlAllowed(
    "ctrip",
    "lodging",
    ctripLodgingListUrl,
  ),
  true,
);
assert.equal(
  hooks.providerHostDecision(
    "ctrip",
    "https://hotels.ctrip.com/checkout" +
      "?checkin=2026-08-23&checkout=2026-08-30",
  ).reason,
  "transaction_surface",
);
assert.equal(
  hooks.providerHostDecision(
    "ctrip",
    "https://hotels.ctrip.com/hotels/list" +
      "?checkin=2026-08-23&checkout=pay-now",
  ).reason,
  "transaction_surface",
);
assert.equal(
  hooks.providerHostDecision(
    "ctrip",
    ctripLodgingListUrl + "&order=price",
  ).reason,
  "transaction_surface",
);
assert.equal(
  hooks.providerHostDecision(
    "ctrip",
    "https://hotel.fliggy.com/search" +
      "?checkin=2026-08-23&checkout=2026-08-30",
  ).reason,
  "outside_provider_host",
);

const trustedFlightQuery = {
  origin: "杭州",
  destination: "马累",
  origin_code: "HGH",
  destination_code: "MLE",
  start_date: "2026-08-23",
  end_date: "2026-08-30",
  adults: 2,
};
const ctripTrustedUrl =
  "https://flights.ctrip.com/international/search/round-hgh-mle" +
  "?depdate=2026-08-23_2026-08-30&cabin=y_s&adult=2&child=0&infant=0";
const ctripUrlEvidence = hooks.trustedSearchUrlDriverEvidence(
  "ctrip",
  "flight",
  ctripTrustedUrl,
  trustedFlightQuery,
);
assert.equal(ctripUrlEvidence.confirmation_scope, "trusted_exact_search_url");
assert.equal(ctripUrlEvidence.confirmed_query.origin, "杭州");
assert.equal(ctripUrlEvidence.confirmed_query.adults, 2);
assert.equal(ctripUrlEvidence.readback_query.origin_code, "HGH");
assert.equal(ctripUrlEvidence.readback_query.adults, 2);
assert.equal(ctripUrlEvidence.party_availability_confirmed, true);
assert.equal(
  ctripUrlEvidence.pricing_context,
  "requested_adults_in_search_url",
);
assert.equal(
  ctripUrlEvidence.url_confirmed_fields.join(","),
  "origin_code,destination_code,start_date,end_date,adults",
);

const reorderedCtripEvidence = hooks.trustedSearchUrlDriverEvidence(
  "ctrip",
  "flight",
  ctripTrustedUrl.replace(
    "?depdate=2026-08-23_2026-08-30&cabin=y_s",
    "?cabin=y_s&depdate=2026-08-23_2026-08-30",
  ),
  trustedFlightQuery,
);
assert.equal(
  reorderedCtripEvidence.confirmation_scope,
  "provider_url_only_unverified",
);
assert.equal(reorderedCtripEvidence.confirmed_query, null);

const fliggyTrustedUrl =
  "https://sijipiao.fliggy.com/ie/flight_search_result.htm" +
  "?tripType=1&depCity=HGH&arrCity=MLE" +
  "&depDate=2026-08-23&arrDate=2026-08-30";
const fliggyUrlEvidence = hooks.trustedSearchUrlDriverEvidence(
  "fliggy",
  "flight",
  fliggyTrustedUrl,
  trustedFlightQuery,
);
assert.equal(fliggyUrlEvidence.confirmation_scope, "trusted_exact_search_url");
assert.equal(fliggyUrlEvidence.confirmed_query.destination, "马累");
assert.equal(fliggyUrlEvidence.confirmed_query.adults, 2);
assert.equal(fliggyUrlEvidence.readback_query.destination_code, "MLE");
assert.equal(fliggyUrlEvidence.readback_query.adults, undefined);
assert.equal(fliggyUrlEvidence.party_availability_confirmed, false);
assert.equal(
  fliggyUrlEvidence.pricing_context,
  "per_person_x_requested_adults",
);
assert.equal(
  fliggyUrlEvidence.url_confirmed_fields.includes("adults"),
  false,
);

const augmentedFliggyEvidence = hooks.trustedSearchUrlDriverEvidence(
  "fliggy",
  "flight",
  `${fliggyTrustedUrl}&adult=2`,
  trustedFlightQuery,
);
assert.equal(
  augmentedFliggyEvidence.confirmation_scope,
  "provider_url_only_unverified",
);

const qunarTrustedUrl =
  "https://flight.qunar.com/twell/flight/Search.jsp" +
  "?from=flight_int_search&showTotalPr=0&searchType=RoundTripFlight" +
  "&fromCity=%E6%9D%AD%E5%B7%9E&toCity=%E9%A9%AC%E7%B4%AF" +
  "&adultNum=2&childNum=0&fromDate=2026-08-23&toDate=2026-08-30";
const qunarUrlEvidence = hooks.trustedSearchUrlDriverEvidence(
  "qunar",
  "flight",
  qunarTrustedUrl,
  trustedFlightQuery,
);
assert.equal(qunarUrlEvidence.confirmation_scope, "trusted_exact_search_url");
assert.equal(qunarUrlEvidence.confirmed_query.origin, "杭州");
assert.equal(qunarUrlEvidence.readback_query.origin, "杭州");
assert.equal(qunarUrlEvidence.readback_query.destination, "马累");
assert.equal(qunarUrlEvidence.readback_query.adults, 2);
assert.equal(qunarUrlEvidence.party_availability_confirmed, true);
assert.equal(
  qunarUrlEvidence.pricing_context,
  "requested_adults_in_search_url",
);
assert.equal(
  qunarUrlEvidence.url_confirmed_fields.join(","),
  "origin,destination,start_date,end_date,adults",
);

const tongchengTrustedUrl =
  "https://www.ly.com/eliflight/book1.html" +
  "?para=HGH*MLE*2026-08-23*2026-08-30*RT*2_0_0*Y%7CS%7CC%7CF" +
  "&departureCity=%E6%9D%AD%E5%B7%9E" +
  "&arrivalCity=%E9%A9%AC%E7%B4%AF";
const tongchengUrlEvidence = hooks.trustedSearchUrlDriverEvidence(
  "tongcheng",
  "flight",
  tongchengTrustedUrl,
  trustedFlightQuery,
);
assert.equal(tongchengUrlEvidence.confirmation_scope, "trusted_exact_search_url");
assert.equal(tongchengUrlEvidence.readback_query.origin, "杭州");
assert.equal(tongchengUrlEvidence.readback_query.destination_code, "MLE");
assert.equal(tongchengUrlEvidence.readback_query.adults, 2);
assert.equal(tongchengUrlEvidence.party_availability_confirmed, true);
assert.equal(
  tongchengUrlEvidence.url_confirmed_fields.join(","),
  "origin,destination,origin_code,destination_code,start_date,end_date,adults",
);
assert.equal(
  hooks.trustedSearchUrlDriverEvidence(
    "tongcheng",
    "flight",
    `${tongchengTrustedUrl}&tracking=unexpected`,
    trustedFlightQuery,
  ).confirmation_scope,
  "provider_url_only_unverified",
);

for (const tamperedUrl of [
  qunarTrustedUrl.replace("flight.qunar.com", "evil.test"),
  qunarTrustedUrl.replace(
    "/twell/flight/Search.jsp",
    "/site/interroundtrip_compare.htm",
  ),
  qunarTrustedUrl.replace("%E9%A9%AC%E7%B4%AF", "%E4%B8%9C%E4%BA%AC"),
  qunarTrustedUrl.replace("toDate=2026-08-30", "toDate=2026-08-29"),
  qunarTrustedUrl.replace("adultNum=2", "adultNum=1"),
  `${qunarTrustedUrl}&tracking=unexpected`,
  qunarTrustedUrl.replace(
    "?from=flight_int_search&showTotalPr=0",
    "?showTotalPr=0&from=flight_int_search",
  ),
]) {
  assert.equal(
    hooks.trustedSearchUrlDriverEvidence(
      "qunar",
      "flight",
      tamperedUrl,
      trustedFlightQuery,
    ).confirmation_scope,
    "provider_url_only_unverified",
  );
}
assert.equal(
  hooks.trustedSearchUrlDriverEvidence(
    "qunar",
    "flight",
    qunarTrustedUrl,
    { ...trustedFlightQuery, destination: "东京" },
  ).confirmation_scope,
  "provider_url_only_unverified",
);
assert.equal(
  hooks.trustedSearchUrlDriverEvidence(
    "qunar",
    "flight",
    "https://flight.qunar.com/site/interroundtrip_compare.htm" +
      "?fromCode=HGH&toCode=MLE&adultNum=2",
    trustedFlightQuery,
  ).confirmation_scope,
  "provider_url_only_unverified",
);

assert.equal(
  hooks.isMessagePortClosedError(
    new Error(
      "A listener indicated an asynchronous response by returning true, " +
        "but the message channel closed before a response was received",
    ),
  ),
  true,
);
assert.equal(
  hooks.isMessagePortClosedError(
    new Error(
      "Could not establish connection. Receiving end does not exist.",
    ),
  ),
  true,
);
assert.equal(
  hooks.isMessagePortClosedError(new Error("content command timed out")),
  false,
);
assert.equal(hooks.isTransientNavigationUrl("edge://newtab"), true);
assert.equal(hooks.isTransientNavigationUrl("edge://newtab/"), true);
assert.equal(hooks.isTransientNavigationUrl("edge://settings"), false);
assert.equal(hooks.isTransientNavigationUrl("edge://newtab.evil"), false);
assert.equal(
  hooks.isTransientNavigationUrl("edge://newtab/?source=unexpected"),
  false,
);

{
  const tabId = 901;
  const sourceUrl = "https://www.fliggy.com/?tab=flight";
  const redirectedUrl = "https://www.fliggy.hk/";
  tabs.set(tabId, {
    id: tabId,
    status: "complete",
    url: sourceUrl,
  });
  const observer = hooks.observeTrustedProviderNavigation({
    provider: "fliggy",
    kind: "flight",
    sourceTabId: tabId,
    previousUrl: sourceUrl,
    ownedTabIds: new Set([tabId]),
    timeoutMs: 100,
  });
  onUpdated.emit(
    tabId,
    { status: "loading", url: redirectedUrl },
    {
      id: tabId,
      status: "loading",
      url: redirectedUrl,
    },
  );
  tabs.set(tabId, {
    id: tabId,
    status: "complete",
    url: redirectedUrl,
  });
  onUpdated.emit(tabId, { status: "complete" }, tabs.get(tabId));
  const transition = await observer.promise;
  assert.equal(transition.mode, "navigation");
  assert.equal(transition.tabId, tabId);
  assert.equal(transition.url, redirectedUrl);
}

{
  tabs.set(1, {
    id: 1,
    status: "complete",
    url: "https://www.fliggy.com/?tab=flight",
  });
  const owned = new Set([1]);
  const observer = hooks.observeTrustedProviderNavigation({
    provider: "fliggy",
    kind: "flight",
    sourceTabId: 1,
    previousUrl: "https://www.fliggy.com/?tab=flight",
    ownedTabIds: owned,
    timeoutMs: 100,
  });
  onUpdated.emit(
    1,
    { status: "loading", url: "https://sjipiao.fliggy.com/search" },
    {
      id: 1,
      status: "loading",
      url: "https://sjipiao.fliggy.com/search",
    },
  );
  tabs.set(1, {
    id: 1,
    status: "complete",
    url: "https://sjipiao.fliggy.com/search",
  });
  onUpdated.emit(
    1,
    { status: "complete" },
    tabs.get(1),
  );
  const transition = await observer.promise;
  assert.equal(transition.mode, "navigation");
  assert.equal(transition.tabId, 1);
  assert.equal(transition.url, "https://sjipiao.fliggy.com/search");
}

{
  tabs.set(2, {
    id: 2,
    status: "complete",
    url: "https://www.fliggy.com/?tab=hotel",
  });
  const owned = new Set([2]);
  const observer = hooks.observeTrustedProviderNavigation({
    provider: "fliggy",
    kind: "lodging",
    sourceTabId: 2,
    previousUrl: "https://www.fliggy.com/?tab=hotel",
    ownedTabIds: owned,
    timeoutMs: 100,
  });
  const child = {
    id: 3,
    openerTabId: 2,
    pendingUrl: "https://hotel.fliggy.com/search",
    status: "loading",
  };
  tabs.set(3, child);
  onCreated.emit(child);
  tabs.set(3, {
    id: 3,
    openerTabId: 2,
    url: "https://hotel.fliggy.com/search",
    status: "complete",
  });
  onUpdated.emit(3, { status: "complete" }, tabs.get(3));
  const transition = await observer.promise;
  assert.equal(transition.mode, "opener_tab_navigation");
  assert.equal(transition.tabId, 3);
  assert.equal(owned.has(3), true);
}

{
  tabs.set(4, {
    id: 4,
    status: "complete",
    url: "https://www.fliggy.com/?tab=flight",
  });
  const observer = hooks.observeTrustedProviderNavigation({
    provider: "fliggy",
    kind: "flight",
    sourceTabId: 4,
    previousUrl: "https://www.fliggy.com/?tab=flight",
    ownedTabIds: new Set([4]),
    timeoutMs: 100,
  });
  onUpdated.emit(
    4,
    {
      status: "loading",
      url: "https://evil.example/search?token=secret-value",
    },
    {
      id: 4,
      status: "loading",
      url: "https://evil.example/search?token=secret-value",
    },
  );
  await assert.rejects(
    observer.promise,
    (error) => {
      assert.equal(error.tripchordCode, "navigation_error");
      assert.equal(
        error.tripchordDetails.stage,
        "observe_navigation",
      );
      assert.equal(
        error.tripchordDetails.reason,
        "outside_provider_host",
      );
      assert.equal(
        error.tripchordDetails.rejected_url.host,
        "evil.example",
      );
      assert.equal(
        error.tripchordDetails.redirect_trace[0].phase,
        "observer_started",
      );
      assert.equal(
        JSON.stringify(error.tripchordDetails).includes("secret-value"),
        false,
      );
      assert.equal(
        hooks.lifecycleFailureDetails(error)
          .navigation_diagnostic.reason,
        "outside_provider_host",
      );
      return true;
    },
  );
}

{
  tabs.set(5, {
    id: 5,
    status: "complete",
    url: "https://hotel.qunar.com/global/",
  });
  const observer = hooks.observeTrustedProviderNavigation({
    provider: "qunar",
    kind: "lodging",
    sourceTabId: 5,
    previousUrl: "https://hotel.qunar.com/global/",
    ownedTabIds: new Set([5]),
    timeoutMs: 100,
  });
  onUpdated.emit(
    5,
    { status: "loading", url: "https://www.qunar.com/" },
    { id: 5, status: "loading", url: "https://www.qunar.com/" },
  );
  await assert.rejects(
    observer.promise,
    (error) =>
      error.tripchordCode === "navigation_error" &&
      /wrong qunar\/lodging vertical/.test(error.message) &&
      error.tripchordDetails.stage === "observe_navigation" &&
      error.tripchordDetails.reason === "wrong_vertical" &&
      error.tripchordDetails.rejected_url.host === "www.qunar.com",
  );
}

{
  tabs.set(6, {
    id: 6,
    status: "complete",
    url: "https://www.fliggy.com/?tab=hotel",
  });
  const observer = hooks.observeTrustedProviderNavigation({
    provider: "fliggy",
    kind: "lodging",
    sourceTabId: 6,
    previousUrl: "https://www.fliggy.com/?tab=hotel",
    ownedTabIds: new Set([6]),
    timeoutMs: 10,
  });
  await assert.rejects(
    observer.promise,
    (error) =>
      error.tripchordCode === "navigation_not_observed" &&
      error.tripchordDetails.stage === "observe_navigation" &&
      error.tripchordDetails.reason === "navigation_not_observed" &&
      error.tripchordDetails.redirect_trace[0].phase === "observer_started",
  );
}

{
  const sourceUrl = "https://hotels.ctrip.com/";
  const resultUrl =
    "https://hotels.ctrip.com/hotels/list" +
    "?countryId=1&cityId=705914&checkin=2026-08-23" +
    "&checkout=2026-08-30&token=do-not-leak";
  tabs.set(7, {
    id: 7,
    status: "complete",
    url: sourceUrl,
  });
  const owned = new Set([7]);
  const observer = hooks.observeTrustedProviderNavigation({
    provider: "ctrip",
    kind: "lodging",
    sourceTabId: 7,
    previousUrl: sourceUrl,
    ownedTabIds: owned,
    timeoutMs: 100,
  });
  const child = {
    id: 8,
    openerTabId: 7,
    pendingUrl: "edge://newtab/",
    status: "loading",
  };
  tabs.set(8, child);
  onCreated.emit(child);
  assert.equal(observer.peek(), null);
  tabs.set(8, {
    id: 8,
    openerTabId: 7,
    url: resultUrl,
    status: "loading",
  });
  onUpdated.emit(
    8,
    { status: "loading", url: resultUrl },
    tabs.get(8),
  );
  tabs.set(8, {
    id: 8,
    openerTabId: 7,
    url: resultUrl,
    status: "complete",
  });
  onUpdated.emit(8, { status: "complete" }, tabs.get(8));
  const transition = await observer.promise;
  const edgeTrace = transition.navigation_trace.find(
    (entry) => entry.url.scheme === "edge",
  );
  const finalTrace = transition.navigation_trace.at(-1);
  assert.equal(transition.mode, "opener_tab_navigation");
  assert.equal(transition.tabId, 8);
  assert.equal(owned.has(8), true);
  assert.equal(edgeTrace.phase, "tab_created");
  assert.equal(edgeTrace.transient, true);
  assert.equal(edgeTrace.url.host, "newtab");
  assert.equal(finalTrace.phase, "navigation_complete");
  assert.equal(finalTrace.url.host, "hotels.ctrip.com");
  assert.equal(finalTrace.url.path_shape, "/hotels/list");
  assert.equal(finalTrace.url.query_keys.includes("cityid"), true);
  assert.equal(finalTrace.url.query_keys.includes("token"), true);
  const serializedTrace = JSON.stringify(transition.navigation_trace);
  assert.equal(serializedTrace.includes("705914"), false);
  assert.equal(serializedTrace.includes("2026-08-23"), false);
  assert.equal(serializedTrace.includes("do-not-leak"), false);
}

{
  tabs.set(70, {
    id: 70,
    status: "complete",
    url: "https://hotels.ctrip.com/",
  });
  const observer = hooks.observeTrustedProviderNavigation({
    provider: "ctrip",
    kind: "lodging",
    sourceTabId: 70,
    previousUrl: "https://hotels.ctrip.com/",
    ownedTabIds: new Set([70]),
    timeoutMs: 100,
  });
  const child = {
    id: 71,
    openerTabId: 70,
    pendingUrl: "edge://settings",
    status: "loading",
  };
  tabs.set(71, child);
  onCreated.emit(child);
  await assert.rejects(
    observer.promise,
    (error) =>
      error.tripchordCode === "navigation_error" &&
      error.tripchordDetails.reason === "non_https" &&
      error.tripchordDetails.rejected_url.scheme === "edge" &&
      error.tripchordDetails.rejected_url.host === "settings",
  );
}

{
  tabs.set(72, {
    id: 72,
    status: "complete",
    url: "https://evil.example/redirect?token=adoption-secret",
  });
  await assert.rejects(
    hooks.adoptTrustedNavigation(
      {
        mode: "opener_tab_navigation",
        tabId: 72,
        url: "https://hotels.ctrip.com/hotels/list",
        navigation_trace: [],
      },
      "ctrip",
      "lodging",
      new Set([72]),
    ),
    (error) =>
      error.tripchordCode === "navigation_error" &&
      error.tripchordDetails.stage === "adopt_navigation" &&
      error.tripchordDetails.reason === "outside_provider_host" &&
      error.tripchordDetails.rejected_url.host === "evil.example" &&
      !JSON.stringify(error.tripchordDetails).includes("adoption-secret"),
  );
}

const portClosed = () =>
  new Error(
    "A listener indicated an asynchronous response by returning true, " +
      "but the message channel closed before a response was received",
  );

{
  tabs.set(10, {
    id: 10,
    status: "complete",
    url: "https://www.fliggy.com/?tab=flight",
  });
  const originalQuery = {
    origin: "杭州",
    destination: "马累",
    options: { fixture: true },
  };
  const lease = {
    task_id: "prepare-recovery",
    provider: "fliggy",
    kind: "flight",
    query: originalQuery,
  };
  const messages = [];
  let calls = 0;
  sendMessageImpl = async (_tabId, message) => {
    messages.push(message);
    calls += 1;
    if (calls === 1) {
      setTimeout(() => {
        const next = "https://sjipiao.fliggy.com/search";
        onUpdated.emit(
          10,
          { status: "loading", url: next },
          { id: 10, status: "loading", url: next },
        );
        tabs.set(10, { id: 10, status: "complete", url: next });
        onUpdated.emit(10, { status: "complete" }, tabs.get(10));
      }, 0);
      throw portClosed();
    }
    return {
      ok: true,
      result: {
        prepared: true,
        confirmed_query: { destination: "马累" },
        readback_query: { destination: "马累" },
      },
    };
  };
  storage.tripchordConnected = true;
  storage.tripchordBridgeToken = "fixture-token-that-is-at-least-32-chars";
  context.fetch = async () => ({
    ok: true,
    status: 200,
    async json() {
      return {
        state: "claimed",
        claimed_by: "chrome-mv3-test-extension",
      };
    },
  });
  const result = await hooks.prepareSearchWithLifecycle(
    10,
    "https://www.fliggy.com/?tab=flight",
    lease,
    Date.now() + 5000,
    new Set([10]),
  );
  assert.equal(result.recovered, true);
  assert.equal(calls, 2);
  assert.equal(
    messages[1].query.options.__tripchord_skip_provider_mode_switch,
    true,
  );
  assert.deepEqual(originalQuery.options, { fixture: true });
  assert.equal(injectedTabs.includes(10), true);
}

{
  tabs.set(20, {
    id: 20,
    status: "complete",
    url: "https://www.fliggy.com/?tab=hotel",
  });
  const owned = new Set([20]);
  sendMessageImpl = async () => {
    setTimeout(() => {
      const child = {
        id: 21,
        openerTabId: 20,
        pendingUrl: "https://hotel.fliggy.com/search",
        status: "loading",
      };
      tabs.set(21, child);
      onCreated.emit(child);
      tabs.set(21, {
        id: 21,
        openerTabId: 20,
        url: "https://hotel.fliggy.com/search",
        status: "complete",
      });
      onUpdated.emit(21, { status: "complete" }, tabs.get(21));
    }, 0);
    throw portClosed();
  };
  const result = await hooks.triggerSearchWithLifecycle(
    20,
    "https://www.fliggy.com/?tab=hotel",
    {
      task_id: "trigger-opener-recovery",
      provider: "fliggy",
      kind: "lodging",
      query: {},
    },
    Date.now() + 5000,
    owned,
  );
  assert.equal(result.recovered, true);
  assert.equal(result.tabId, 21);
  assert.equal(result.transitionMode, "opener_tab_navigation");
  assert.deepEqual([...owned], [21]);
  assert.equal(removedTabs.includes(20), true);
  await hooks.closeOwnedTabs(owned);
  assert.equal(owned.size, 0);
  assert.equal(removedTabs.includes(21), true);
}

{
  tabs.set(22, {
    id: 22,
    windowId: 1,
    active: false,
    status: "complete",
    url: "https://hotel.fliggy.com/hotel_list3.htm",
  });
  tabs.set(23, {
    id: 23,
    windowId: 1,
    active: true,
    status: "complete",
    url: "https://example.com/previous-tab",
  });
  const owned = new Set([22]);
  const tabUpdateCount = tabUpdates.length;
  const windowUpdateCount = windowUpdates.length;
  const retained = await hooks.retainHumanActionTab(22, owned);
  assert.equal(retained, true);
  assert.equal(owned.has(22), false);
  assert.equal(tabs.get(22).active, false);
  assert.equal(tabs.get(23).active, true);
  assert.equal(removedTabs.includes(22), false);
  assert.equal(tabUpdates.length, tabUpdateCount);
  assert.equal(windowUpdates.length, windowUpdateCount);
  tabs.delete(22);
  tabs.delete(23);
}

{
  const exactUrl =
    "https://flight.qunar.com/twell/flight/Search.jsp?from=flight_int_search&showTotalPr=0&searchType=RoundTripFlight&fromCity=%E6%9D%AD%E5%B7%9E&toCity=%E9%A9%AC%E7%B4%AF&adultNum=2&childNum=0&fromDate=2026-08-01&toDate=2026-08-05";
  tabs.set(24, {
    id: 24,
    windowId: 1,
    active: true,
    status: "complete",
    url: exactUrl,
  });
  const reusable = await hooks.claimReusableExactFlightResultTab({
    provider: "qunar",
    kind: "flight",
    query: {
      origin: "杭州",
      destination: "马累",
      origin_code: "HGH",
      destination_code: "MLE",
      start_date: "2026-08-01",
      end_date: "2026-08-05",
      adults: 2,
      search_url: exactUrl,
      options: { __tripchord_reuse_exact_result_tab: true },
    },
  });
  assert.equal(reusable.tab_id, 24);
  assert.equal(reusable.url, exactUrl);
  tabs.delete(24);
}

{
  // A Qunar lodging result tab that runs out of lease budget is preserved
  // (not closed) so a retryable-timeout retry can reuse it with a fresh
  // full-budget extraction.  This is the cancel/retry half of the 90s-phase /
  // 120s-lease redesign: without it, the retry restarts from a fresh landing
  // and can hit a native lease timeout with no receipt.
  const qunarResultUrl =
    "https://hotel.qunar.com/intl/search.jsp?toCity=%E8%83%A1%E9%B2%81%E9%A9%AC%E7%B4%AF&fromDate=2026-08-20&toDate=2026-08-27&cityurl=i-hulhumale&from=globalhotelpages";
  tabs.set(25, {
    id: 25,
    windowId: 7,
    status: "complete",
    url: qunarResultUrl,
  });
  const ownedTabs = new Set([25]);
  const ownedWindows = new Set([7]);
  const lease = {
    task_id: "preserve-qunar-hulhumale",
    provider: "qunar",
    kind: "lodging",
    query: {
      destination: "Hulhumalé",
      start_date: "2026-08-20",
      end_date: "2026-08-27",
      adults: 2,
      rooms: 1,
      search_url: null,
      options: {
        expected_lodging_place_key: "hulhumale",
      },
    },
  };
  const preserved = await hooks.preserveExactLodgingResultTab(
    lease,
    25,
    ownedTabs,
    ownedWindows,
  );
  assert.equal(preserved.tab_id, 25);
  assert.equal(preserved.isolation_window, true);
  assert.equal(ownedTabs.has(25), false);
  assert.equal(ownedWindows.has(7), false);
  assert.equal(hooks.preservedExactResultTabs.has(25), true);

  // A retry submitted with the exact-result reuse flag claims the preserved
  // tab instead of scanning for a generic one.
  const reusable = await hooks.claimReusableExactLodgingResultTab({
    ...lease,
    query: {
      ...lease.query,
      options: {
        expected_lodging_place_key: "hulhumale",
        __tripchord_reuse_exact_result_tab: true,
      },
    },
  });
  assert.equal(reusable.tab_id, 25);
  assert.equal(reusable.preserved_exact_result, true);
  assert.equal(reusable.isolation_window, true);
  assert.equal(hooks.preservedExactResultTabs.has(25), false);

  // Sweep with an artificially expired record closes the tab and window.
  tabs.set(26, {
    id: 26,
    windowId: 8,
    status: "complete",
    url: qunarResultUrl,
  });
  hooks.preservedExactResultTabs.set(26, {
    window_id: 8,
    provider: "qunar",
    kind: "lodging",
    preserved_at_ms: Date.now() - 200000,
    query_key: hooks.preservedLodgingResultQueryKey(lease.query),
    isolation_window: true,
  });
  await hooks.sweepExpiredPreservedResultTabs();
  assert.equal(hooks.preservedExactResultTabs.has(26), false);
  assert.equal(removedTabs.includes(26), true);
  assert.equal(removedWindows.includes(8), true);

  tabs.delete(25);
  tabs.delete(26);
}

{
  tabs.set(30, {
    id: 30,
    status: "complete",
    url: "https://www.fliggy.com/?tab=hotel",
  });
  const owned = new Set([30]);
  let calls = 0;
  sendMessageImpl = async () => {
    calls += 1;
    if (calls === 1) {
      setTimeout(() => {
        const next = "https://hotel.fliggy.com/search";
        onUpdated.emit(
          30,
          { status: "loading", url: next },
          { id: 30, status: "loading", url: next },
        );
        tabs.set(30, { id: 30, status: "complete", url: next });
        onUpdated.emit(30, { status: "complete" }, tabs.get(30));
      }, 0);
    }
    throw portClosed();
  };
  await assert.rejects(
    hooks.prepareSearchWithLifecycle(
      30,
      "https://www.fliggy.com/?tab=hotel",
      {
        task_id: "prepare-second-failure",
        provider: "fliggy",
        kind: "lodging",
        query: { options: {} },
      },
      Date.now() + 5000,
      owned,
    ),
    (error) =>
      error.tripchordCode === "navigation_recovery_exhausted" &&
      /closed again/.test(error.message),
  );
  assert.equal(calls, 2);
  await hooks.closeOwnedTabs(owned);
  assert.equal(owned.size, 0);
}

{
  const sourceUrl = "https://www.fliggy.com/?tab=hotel";
  tabs.set(31, {
    id: 31,
    status: "complete",
    url: sourceUrl,
  });
  const owned = new Set([31]);
  const query = {
    destination: "Maafushi",
    start_date: "2026-08-12",
    end_date: "2026-08-18",
    adults: 2,
    rooms: 1,
    options: { expected_lodging_place_key: "maafushi" },
  };
  const resultUrl =
    "https://hotel.fliggy.com/hotel_list3.htm" +
    "?spm=181.11358650.hotelModule.internationalSearch" +
    "&city=933081&cityName=%E9%A9%AC%E5%AF%8C%E5%A3%AB" +
    "&checkIn=2026-08-12&checkOut=2026-08-18" +
    "&keywords=&aNum_1=2&cNum_1=0";
  sendMessageImpl = async () => {
    setTimeout(() => {
      tabs.set(31, {
        id: 31,
        status: "complete",
        url: resultUrl,
      });
      onUpdated.emit(
        31,
        { status: "complete", url: resultUrl },
        tabs.get(31),
      );
    }, 0);
    return {
      ok: true,
      result: {
        triggered: true,
        audited_navigation_url: resultUrl,
        trigger_mode: "audited_read_only_search_url",
      },
    };
  };
  const result = await hooks.triggerSearchWithLifecycle(
    31,
    sourceUrl,
    {
      task_id: "fliggy-audited-direct-result",
      provider: "fliggy",
      kind: "lodging",
      query,
    },
    Date.now() + 5000,
    owned,
  );
  assert.equal(result.pageUrl, resultUrl);
  assert.equal(result.transitionMode, "navigation");
  assert.equal(
    tabUpdates.some(
      (entry) => entry.tabId === 31 && entry.update.url === resultUrl,
    ),
    true,
  );
}

{
  const sourceUrl = "https://hotel.qunar.com/global/";
  tabs.set(32, {
    id: 32,
    status: "complete",
    url: sourceUrl,
  });
  const query = {
    destination: "Maafushi",
    start_date: "2026-08-12",
    end_date: "2026-08-18",
    adults: 2,
    rooms: 1,
    options: { expected_lodging_place_key: "maafushi" },
  };
  const resultUrl =
    "https://hotel.qunar.com/intl/search.jsp" +
    "?toCity=%E9%A9%AC%E5%AF%8C%E6%96%BD" +
    "&fromDate=2026-08-12&toDate=2026-08-18" +
    "&cityurl=i-ka_maafushi&from=globalhotelpages";
  const loginUrl =
    "https://user.qunar.com/passport/login.jsp?ret=" +
    encodeURIComponent(resultUrl);
  sendMessageImpl = async () => {
    setTimeout(() => {
      tabs.set(32, {
        id: 32,
        status: "complete",
        url: loginUrl,
      });
      onUpdated.emit(
        32,
        { status: "complete", url: loginUrl },
        tabs.get(32),
      );
    }, 0);
    return {
      ok: true,
      result: {
        triggered: true,
        audited_navigation_url: resultUrl,
        trigger_mode: "audited_read_only_search_url",
      },
    };
  };
  await assert.rejects(
    hooks.triggerSearchWithLifecycle(
      32,
      sourceUrl,
      {
        task_id: "qunar-audited-login-redirect",
        provider: "qunar",
        kind: "lodging",
        query,
      },
      Date.now() + 5000,
      new Set([32]),
    ),
    (error) =>
      error.tripchordCode === "login_required" &&
      error.tripchordDetails.reason === "audited_login_redirect",
  );
}

{
  tabs.set(35, {
    id: 35,
    status: "complete",
    url: "https://www.fliggy.com/?tab=flight",
  });
  const owned = new Set([35]);
  sendMessageImpl = async () => {
    const next = "https://sjipiao.fliggy.com/search";
    onUpdated.emit(
      35,
      { status: "loading", url: next },
      { id: 35, status: "loading", url: next },
    );
    tabs.set(35, { id: 35, status: "complete", url: next });
    onUpdated.emit(35, { status: "complete" }, tabs.get(35));
    return {
      ok: true,
      result: {
        triggered: true,
        confirmation_scope: "visible_search_triggered",
      },
    };
  };
  const result = await hooks.triggerSearchWithLifecycle(
    35,
    "https://www.fliggy.com/?tab=flight",
    {
      task_id: "trigger-pre-registered",
      provider: "fliggy",
      kind: "flight",
      query: {},
    },
    Date.now() + 5000,
    owned,
  );
  assert.equal(result.recovered, false);
  assert.equal(result.tabId, 35);
  assert.equal(result.transitionMode, "navigation");
  await hooks.closeOwnedTabs(owned);
  assert.equal(owned.size, 0);
}

{
  tabs.set(40, {
    id: 40,
    status: "complete",
    url: "https://www.fliggy.com/?tab=flight",
  });
  const owned = new Set([40]);
  sendMessageImpl = async () => {
    throw portClosed();
  };
  await assert.rejects(
    hooks.triggerSearchWithLifecycle(
      40,
      "https://www.fliggy.com/?tab=flight",
      {
        task_id: "trigger-without-navigation",
        provider: "fliggy",
        kind: "flight",
        query: {},
      },
      Date.now() + 1600,
      owned,
    ),
    (error) => error.tripchordCode === "navigation_not_observed",
  );
  await hooks.closeOwnedTabs(owned);
  assert.equal(owned.size, 0);
}

{
  const messages = [];
  let extractionCalls = 0;
  tabs.set(50, {
    id: 50,
    status: "complete",
    url: "https://sjipiao.fliggy.com/search",
  });
  const selection = {
    selection_id: "a".repeat(64),
    carrier_text: "亚洲航空",
    outbound_departure_at: "2026-08-23T07:10:00+08:00",
    outbound_arrival_at: "2026-08-23T17:20:00+05:00",
    selection_evidence: "亚洲航空 07:10 杭州 17:20 马累 选为去程",
  };
  sendMessageImpl = async (_tabId, message) => {
    messages.push(message);
    if (message.type === "tripchord:extract") {
      extractionCalls += 1;
      if (extractionCalls === 1) {
        return {
          ok: true,
          result: {
            state: "outbound_preview",
            combination_status: "outbound_preview",
            selection,
            quotes: [],
          },
        };
      }
      return {
        ok: true,
        result: {
          state: "succeeded",
          quotes: [{ details: { combination_status: "round_trip_complete" } }],
        },
      };
    }
    if (message.type === "tripchord:safe-select-outbound") {
      return {
        ok: true,
        result: {
          selected: true,
          confirmation_scope: "exact_visible_select_outbound",
          selection,
        },
      };
    }
    throw new Error(`unexpected state-machine message ${message.type}`);
  };
  const result = await hooks.extractWithRetry(
    50,
    {
      task_id: "flight-state-machine",
      provider: "fliggy",
      kind: "flight",
      query: {
        origin: "杭州",
        destination: "马累",
        origin_code: "HGH",
        destination_code: "MLE",
        start_date: "2026-08-23",
        end_date: "2026-08-30",
        adults: 2,
      },
    },
    {
      confirmation_scope: "trusted_exact_search_url",
      confirmed_query: {
        origin: "杭州",
        destination: "马累",
        start_date: "2026-08-23",
        end_date: "2026-08-30",
        adults: 2,
      },
      party_availability_confirmed: false,
    },
    Date.now() + 5000,
  );
  assert.equal(result.state, "succeeded");
  assert.equal(extractionCalls, 2);
  assert.deepEqual(
    messages.map((message) => message.type),
    [
      "tripchord:extract",
      "tripchord:safe-select-outbound",
      "tripchord:extract",
    ],
  );
  assert.equal(
    messages[1].selection_id,
    selection.selection_id,
  );
  const finalDriver = messages[2].driver;
  assert.equal(finalDriver.selected_outbound.selection_id, selection.selection_id);
  assert.equal(
    JSON.stringify(finalDriver.action_trace.map((item) => item.action)),
    JSON.stringify(["search", "select_outbound"]),
  );
  assert.equal(
    messages.some((message) => /book|order|pay|预订|下单|支付/i.test(message.type)),
    false,
  );
}

{
  let extractionCalls = 0;
  const selectedIds = [];
  const updateCountBefore = tabUpdates.length;
  const searchUrl =
    "https://flights.ctrip.com/international/search/round-hgh-mle" +
    "?depdate=2026-08-23_2026-08-30" +
    "&cabin=y_s&adult=2&child=0&infant=0";
  tabs.set(52, {
    id: 52,
    status: "complete",
    url: searchUrl,
  });
  const firstSelection = {
    selection_id: "a".repeat(64),
    carrier_text: "阿联酋航空",
    outbound_departure_at: "2026-08-23T18:10:00+08:00",
    outbound_arrival_at: "2026-08-24T11:35:00+05:00",
    selection_evidence: "阿联酋航空 杭州 马累 选为去程",
  };
  const secondSelection = {
    selection_id: "b".repeat(64),
    carrier_text: "新加坡航空",
    outbound_departure_at: "2026-08-23T20:55:00+08:00",
    outbound_arrival_at: "2026-08-24T11:50:00+05:00",
    selection_evidence: "新加坡航空 杭州 马累 选为去程",
  };
  const staleSelection = {
    selection_id: "c".repeat(64),
    carrier_text: "阿联酋航空",
    outbound_departure_at: "2026-08-23T18:10:00+08:00",
    outbound_arrival_at: "2026-08-24T11:00:00+05:00",
    selection_evidence: "尚未水合完成的旧去程候选",
  };
  let finalDriver = null;
  sendMessageImpl = async (_tabId, message) => {
    if (message.type === "tripchord:extract") {
      extractionCalls += 1;
      if (
        extractionCalls === 1 ||
        extractionCalls === 3 ||
        extractionCalls === 4
      ) {
        return {
          ok: true,
          result: {
            state: "outbound_preview",
            combination_status: "outbound_preview",
            selection: firstSelection,
            selections:
              extractionCalls === 1
                ? [firstSelection, staleSelection, secondSelection]
                : [firstSelection, secondSelection],
            quotes: [],
          },
        };
      }
      if (extractionCalls === 2) {
        return {
          ok: true,
          result: {
            state: "failed",
            quotes: [],
            failure: {
              code: "extraction_error",
              message: "首个去程没有形成可验证的返程组合",
              details: {},
            },
          },
        };
      }
      finalDriver = message.driver;
      return {
        ok: true,
        result: {
          state: "succeeded",
          quotes: [{ details: { combination_status: "round_trip_complete" } }],
        },
      };
    }
    if (message.type === "tripchord:safe-select-outbound") {
      selectedIds.push(message.selection_id);
      if (message.selection_id === staleSelection.selection_id) {
        return {
          ok: true,
          result: {
            selected: false,
            code: "outbound_selection_evidence_changed",
            available_candidates: [firstSelection, secondSelection],
          },
        };
      }
      const selection =
        message.selection_id === firstSelection.selection_id
          ? firstSelection
          : secondSelection;
      return {
        ok: true,
        result: {
          selected: true,
          confirmation_scope: "exact_visible_select_outbound",
          selection,
        },
      };
    }
    throw new Error(`unexpected fallback state-machine message ${message.type}`);
  };
  const result = await hooks.extractWithRetry(
    52,
    {
      task_id: "flight-bounded-outbound-fallback",
      provider: "ctrip",
      kind: "flight",
      query: {
        origin: "杭州",
        destination: "马累",
        origin_code: "HGH",
        destination_code: "MLE",
        start_date: "2026-08-23",
        end_date: "2026-08-30",
        adults: 2,
        search_url: searchUrl,
      },
    },
    {
      confirmation_scope: "trusted_exact_search_url",
      confirmed_query: {
        origin: "杭州",
        destination: "马累",
        start_date: "2026-08-23",
        end_date: "2026-08-30",
        adults: 2,
      },
    },
    Date.now() + 12000,
  );
  assert.equal(result.state, "succeeded");
  assert.deepEqual(selectedIds, [
    firstSelection.selection_id,
    staleSelection.selection_id,
    secondSelection.selection_id,
  ]);
  assert.equal(
    tabUpdates
      .slice(updateCountBefore)
      .some((item) => item.tabId === 52 && item.update.url === searchUrl),
    true,
  );
  assert.equal(
    JSON.stringify(finalDriver.action_trace.map((item) => item.action)),
    JSON.stringify(["search", "select_outbound", "reselect_outbound"]),
  );
  assert.equal(
    finalDriver.selected_outbound.selection_id,
    secondSelection.selection_id,
  );
}

{
  let extractionCalls = 0;
  const comparisonReceipt = {
    state: "comparison_price_only",
    page_url:
      "https://flights.ctrip.com/international/search/round-hgh-mle",
    captured_at: "2026-07-31T05:00:00.000Z",
    scanned_count: 3,
  };
  const comparisonReceiptSha = "d".repeat(64);
  const selection = {
    selection_id: "e".repeat(64),
    carrier_text: "阿联酋航空",
    outbound_departure_at: "2026-08-23T00:10:00+08:00",
    outbound_arrival_at: "2026-08-23T15:30:00+05:00",
    selection_evidence: "阿联酋航空 HGH→MLE 可见去程起价",
  };
  tabs.set(54, {
    id: 54,
    status: "complete",
    url:
      "https://flights.ctrip.com/international/search/round-hgh-mle",
  });
  sendMessageImpl = async (_tabId, message) => {
    if (message.type === "tripchord:extract") {
      extractionCalls += 1;
      if (extractionCalls === 1) {
        return {
          ok: true,
          result: {
            state: "outbound_preview",
            workflow_kind: "staged_outbound_return",
            combination_status: "outbound_preview",
            selection,
            selections: [selection],
            quotes: [],
            flight_search_receipt: comparisonReceipt,
            flight_search_receipt_sha256: comparisonReceiptSha,
          },
        };
      }
      return {
        ok: true,
        result: {
          state: "failed",
          quotes: [],
          failure: {
            code: "dom_drift",
            message: "返程页只有起价，未形成最终组合",
            retryable: false,
            page_url:
              "https://flights.ctrip.com/online/list/round-hgh-mle",
            captured_at: "2026-07-31T05:01:00.000Z",
            details: {
              flight_diagnostic: {
                outcome: "starting_price_only",
                stage: "price_finality_validation",
              },
            },
          },
        },
      };
    }
    if (message.type === "tripchord:safe-select-outbound") {
      return {
        ok: true,
        result: {
          selected: true,
          confirmation_scope: "exact_visible_select_outbound",
          selection,
        },
      };
    }
    throw new Error(`unexpected receipt retention message ${message.type}`);
  };
  const retained = await hooks.extractWithRetry(
    54,
    {
      task_id: "ctrip-comparison-receipt-retained-across-repair",
      provider: "ctrip",
      kind: "flight",
      query: {
        origin: "杭州",
        destination: "马累",
        origin_code: "HGH",
        destination_code: "MLE",
        start_date: "2026-08-23",
        end_date: "2026-08-30",
        adults: 2,
      },
    },
    {
      triggered: true,
      confirmation_scope: "trusted_exact_search_url",
      confirmed_query: {
        origin: "杭州",
        destination: "马累",
        start_date: "2026-08-23",
        end_date: "2026-08-30",
        adults: 2,
      },
    },
    Date.now() + 8000,
  );
  assert.equal(retained.state, "failed");
  assert.equal(retained.failure.code, "extraction_error");
  assert.equal(retained.failure.page_url, comparisonReceipt.page_url);
  assert.equal(retained.failure.captured_at, comparisonReceipt.captured_at);
  assert.deepEqual(
    retained.failure.details.flight_search_receipt,
    comparisonReceipt,
  );
  assert.equal(
    retained.failure.details.flight_search_receipt_sha256,
    comparisonReceiptSha,
  );
  assert.equal(
    retained.failure.details.flight_diagnostic.outcome,
    "starting_price_only",
  );
}

{
  let extractionCalls = 0;
  tabs.set(51, {
    id: 51,
    status: "complete",
    url: "https://sjipiao.fliggy.com/search",
  });
  const injectedBefore = injectedTabs.length;
  sendMessageImpl = async (_tabId, message) => {
    if (message.type !== "tripchord:extract") {
      throw new Error(`unexpected timeout fixture message ${message.type}`);
    }
    extractionCalls += 1;
    throw new Error("content command timed out");
  };
  await assert.rejects(
    hooks.extractWithRetry(
      51,
      {
        task_id: "flight-content-timeout-terminal",
        provider: "fliggy",
        kind: "flight",
        query: {},
      },
      null,
      Date.now() + 5000,
    ),
    /content command timed out/,
  );
  assert.equal(extractionCalls, 1);
  assert.equal(injectedTabs.length, injectedBefore);
}

{
  let extractionCalls = 0;
  tabs.set(52, {
    id: 52,
    status: "complete",
    url: "https://sjipiao.fliggy.com/search",
  });
  const injectedBefore = injectedTabs.length;
  sendMessageImpl = async (_tabId, message) => {
    if (message.type !== "tripchord:extract") {
      throw new Error(`unexpected port-close fixture message ${message.type}`);
    }
    extractionCalls += 1;
    if (extractionCalls === 1) {
      throw portClosed();
    }
    return {
      ok: true,
      result: {
        state: "succeeded",
        quotes: [{ details: { combination_status: "round_trip_complete" } }],
      },
    };
  };
  const recovered = await hooks.extractWithRetry(
    52,
    {
      task_id: "flight-port-close-single-recovery",
      provider: "fliggy",
      kind: "flight",
      query: {},
    },
    null,
    Date.now() + 5000,
  );
  assert.equal(recovered.state, "succeeded");
  assert.equal(extractionCalls, 2);
  assert.equal(injectedTabs.length, injectedBefore + 1);
}

{
  let extractionCalls = 0;
  tabs.set(53, {
    id: 53,
    status: "complete",
    url: "https://flights.ctrip.com/international/search/round-hgh-mle",
  });
  sendMessageImpl = async (_tabId, message) => {
    assert.equal(message.type, "tripchord:extract");
    extractionCalls += 1;
    return {
      ok: true,
      result: {
        state: "failed",
        quotes: [],
        failure: {
          code: "dom_drift",
          message: "visible starting price is not a final quote",
          retryable: false,
          details: {
            flight_diagnostic: {
              outcome: "starting_price_only",
              stage: "price_finality_validation",
              blocking_contract_fields: ["price_finality"],
            },
          },
        },
      },
    };
  };
  const startedAt = Date.now();
  const terminalDrift = await hooks.extractWithRetry(
    53,
    {
      task_id: "ctrip-flight-terminal-drift",
      provider: "ctrip",
      kind: "flight",
      query: {
        origin: "杭州",
        destination: "马累",
        start_date: "2026-08-23",
        end_date: "2026-08-30",
        adults: 2,
      },
    },
    null,
    Date.now() + 45000,
  );
  assert.equal(terminalDrift.failure.code, "dom_drift");
  assert.equal(
    terminalDrift.failure.details.flight_diagnostic.outcome,
    "starting_price_only",
  );
  assert.equal(extractionCalls, 1);
  assert.equal(Date.now() - startedAt < 1000, true);
}

{
  const warmupFailure = {
    state: "failed",
    quotes: [],
    failure: {
      code: "extraction_error",
      retryable: false,
      details: {
        flight_diagnostic: {
          outcome: "outbound_results_empty_or_unavailable",
          stage: "outbound_result_discovery",
          counts: {
            outbound_stage_anchor_count: 1,
            visible_price_anchor_count: 0,
            profile_card_count: 0,
            safe_outbound_control_count: 0,
          },
        },
        flight_search_receipt: {
          state: "bounded_no_exact_quote",
        },
      },
    },
  };
  assert.equal(
    hooks.ctripOutboundStageNeedsWarmup(warmupFailure),
    true,
  );
  assert.equal(
    hooks.ctripOutboundStageNeedsWarmup({
      ...warmupFailure,
      failure: {
        ...warmupFailure.failure,
        details: {
          flight_diagnostic: {
            ...warmupFailure.failure.details.flight_diagnostic,
            counts: {
              ...warmupFailure.failure.details.flight_diagnostic.counts,
              profile_card_count: 1,
            },
          },
        },
      },
    }),
    false,
  );
  let extractionCalls = 0;
  tabs.set(54, {
    id: 54,
    status: "complete",
    url: "https://flights.ctrip.com/international/search/round-hgh-mle",
  });
  sendMessageImpl = async (_tabId, message) => {
    assert.equal(message.type, "tripchord:extract");
    extractionCalls += 1;
    return {
      ok: true,
      result:
        extractionCalls === 1
          ? warmupFailure
          : {
              state: "succeeded",
              quotes: [{
                provider: "ctrip",
                details: {
                  combination_status: "round_trip_complete",
                },
              }],
            },
    };
  };
  const recovered = await hooks.extractWithRetry(
    54,
    {
      task_id: "ctrip-outbound-evidence-warmup",
      provider: "ctrip",
      kind: "flight",
      query: {},
    },
    null,
    Date.now() + 5000,
  );
  assert.equal(recovered.state, "succeeded");
  assert.equal(extractionCalls, 2);
}

{
  const tongchengShellFailure = {
    state: "failed",
    quotes: [],
    failure: {
      code: "dom_drift",
      retryable: false,
      details: {
        dom_diagnostics: {
          candidates: [{
            tag: "section",
            class: "page-section page-view eliflight",
            text_summary: "单程 往返 08-08 周六 08-12 周三 点击查看",
            price_anchor_hits: 0,
            action_anchor_hits: 1,
          }],
        },
      },
    },
  };
  assert.equal(
    hooks.tongchengFlightResultNeedsWarmup(tongchengShellFailure),
    true,
  );
  assert.equal(
    hooks.tongchengFlightResultNeedsWarmup({
      ...tongchengShellFailure,
      failure: {
        ...tongchengShellFailure.failure,
        details: {
          ...tongchengShellFailure.failure.details,
          flight_diagnostic: { outcome: "starting_price_only" },
        },
      },
    }),
    false,
  );
  let extractionCalls = 0;
  tabs.set(56, {
    id: 56,
    status: "complete",
    url: "https://www.ly.com/eliflight/book1.html",
  });
  sendMessageImpl = async (_tabId, message) => {
    assert.equal(message.type, "tripchord:extract");
    extractionCalls += 1;
    return {
      ok: true,
      result:
        extractionCalls === 1
          ? tongchengShellFailure
          : {
              state: "succeeded",
              quotes: [{
                provider: "tongcheng",
                details: {
                  combination_status: "round_trip_complete",
                },
              }],
            },
    };
  };
  const recovered = await hooks.extractWithRetry(
    56,
    {
      task_id: "tongcheng-result-xhr-warmup",
      provider: "tongcheng",
      kind: "flight",
      query: {},
    },
    null,
    Date.now() + 5000,
  );
  assert.equal(recovered.state, "succeeded");
  assert.equal(extractionCalls, 2);
}

{
  const stableKey = "a".repeat(64);
  assert.deepEqual(
    Array.from(hooks.qunarGeometryStabilityKeys({
      state: "price_evidence_preview",
      stability: {
        evidence_source:
          "geometry_clipped_visible_digit_sequence",
        keys: [stableKey],
      },
    })),
    [stableKey],
  );
  assert.equal(
    hooks.qunarGeometryStabilityKeys({
      state: "price_evidence_preview",
      stability: {
        evidence_source:
          "geometry_clipped_visible_digit_sequence",
        keys: ["unsafe"],
      },
    }),
    null,
  );
  let extractionCalls = 0;
  const messages = [];
  tabs.set(55, {
    id: 55,
    status: "complete",
    url: "https://flight.qunar.com/site/interroundtrip_compare.htm",
  });
  sendMessageImpl = async (_tabId, message) => {
    messages.push(message);
    extractionCalls += 1;
    return {
      ok: true,
      result:
        extractionCalls === 1
          ? {
              state: "price_evidence_preview",
              quotes: [],
              stability: {
                evidence_source:
                  "geometry_clipped_visible_digit_sequence",
                keys: [stableKey],
              },
            }
          : {
              state: "succeeded",
              quotes: [{
                provider: "qunar",
                amount: 6600,
                price_basis: "total_party",
              }],
            },
    };
  };
  const stable = await hooks.extractWithRetry(
    55,
    {
      task_id: "qunar-geometry-stable-two-read",
      provider: "qunar",
      kind: "flight",
      query: {},
    },
    {
      triggered: true,
      confirmation_scope: "trusted_exact_search_url",
    },
    Date.now() + 5000,
  );
  assert.equal(stable.state, "succeeded");
  assert.equal(extractionCalls, 2);
  assert.deepEqual(
    Array.from(messages[1].driver.qunar_geometry_stability_keys),
    [stableKey],
  );
  assert.equal(
    messages[1].driver.qunar_geometry_price_disabled,
    false,
  );
}

{
  let extractionCalls = 0;
  const messages = [];
  tabs.set(56, {
    id: 56,
    status: "complete",
    url: "https://flight.qunar.com/site/interroundtrip_compare.htm",
  });
  sendMessageImpl = async (_tabId, message) => {
    messages.push(message);
    extractionCalls += 1;
    if (message.driver.qunar_geometry_price_disabled === true) {
      return {
        ok: true,
        result: {
          state: "failed",
          quotes: [],
          failure: {
            code: "extraction_error",
            retryable: false,
            details: {
              flight_search_receipt: {
                state: "bounded_no_exact_quote",
              },
            },
          },
        },
      };
    }
    return {
      ok: true,
      result: {
        state: "price_evidence_preview",
        quotes: [],
        stability: {
          evidence_source:
            "geometry_clipped_visible_digit_sequence",
          keys: [String(extractionCalls).repeat(64)],
        },
      },
    };
  };
  const bounded = await hooks.extractWithRetry(
    56,
    {
      task_id: "qunar-geometry-unstable-bounded",
      provider: "qunar",
      kind: "flight",
      query: {},
    },
    {
      triggered: true,
      confirmation_scope: "trusted_exact_search_url",
    },
    Date.now() + 5000,
  );
  assert.equal(bounded.failure.code, "extraction_error");
  assert.equal(
    bounded.failure.details.flight_search_receipt.state,
    "bounded_no_exact_quote",
  );
  assert.equal(extractionCalls, 4);
  assert.equal(
    messages[3].driver.qunar_geometry_price_disabled,
    true,
  );
}

{
  tabs.set(910, {
    id: 910,
    windowId: 1,
    status: "complete",
    url: "https://example.test/fifo-user",
  });
  tabs.set(101, {
    id: 101,
    windowId: 1,
    status: "complete",
    url: "https://hotels.ctrip.com/hotels/list",
  });
  tabs.set(102, {
    id: 102,
    windowId: 1,
    status: "complete",
    url: "https://hotels.ctrip.com/hotels/list",
  });
  await chrome.tabs.update(910, { active: true });
  tabUpdates.length = 0;
  sentMessages.length = 0;
  let releaseFirst;
  const firstGate = new Promise((resolve) => {
    releaseFirst = resolve;
  });
  let markFirstEntered;
  const firstEntered = new Promise((resolve) => {
    markFirstEntered = resolve;
  });
  let contentInFlight = 0;
  let maxContentInFlight = 0;
  const order = [];
  sendMessageImpl = async (tabId) => {
    contentInFlight += 1;
    maxContentInFlight = Math.max(maxContentInFlight, contentInFlight);
    order.push(`start:${tabId}`);
    if (tabId === 101) {
      markFirstEntered();
      await firstGate;
    }
    order.push(`end:${tabId}`);
    contentInFlight -= 1;
    return { ok: true, result: { tab_id: tabId } };
  };
  const lease101 = {
    task_id: "visible-fifo-101",
    provider: "ctrip",
    kind: "lodging",
  };
  const lease102 = {
    task_id: "visible-fifo-102",
    provider: "ctrip",
    kind: "lodging",
  };
  const first = hooks.visibleContentCall(
    101,
    { type: "tripchord:extract" },
    {
      lease: lease101,
      deadline: Date.now() + 8000,
      ownedTabIds: new Set([101]),
    },
  );
  await firstEntered;
  const second = hooks.visibleContentCall(
    102,
    { type: "tripchord:extract" },
    {
      lease: lease102,
      deadline: Date.now() + 8000,
      ownedTabIds: new Set([102]),
    },
  );
  await new Promise((resolve) => setTimeout(resolve, 50));
  assert.deepEqual(
    sentMessages.map(({ tabId }) => tabId),
    [101],
  );
  assert.equal((await chrome.tabs.get(101)).active, false);
  assert.equal((await chrome.tabs.get(102)).active, false);
  releaseFirst();
  const [firstResult, secondResult] = await Promise.all([first, second]);
  assert.equal(firstResult.tab_id, 101);
  assert.equal(secondResult.tab_id, 102);
  assert.equal(maxContentInFlight, 1);
  assert.deepEqual(order, [
    "start:101",
    "end:101",
    "start:102",
    "end:102",
  ]);
  assert.equal((await chrome.tabs.get(910)).active, true);
  assert.deepEqual(
    tabUpdates
      .filter(({ update }) => update.active === true)
      .map(({ tabId }) => tabId),
    [],
  );
}

{
  tabs.set(920, {
    id: 920,
    windowId: 1,
    status: "complete",
    url: "https://example.test/user-before-switch",
  });
  tabs.set(921, {
    id: 921,
    windowId: 1,
    status: "complete",
    url: "https://example.test/user-chosen-tab",
  });
  tabs.set(103, {
    id: 103,
    windowId: 1,
    status: "complete",
    url: "https://hotels.ctrip.com/hotels/list",
  });
  await chrome.tabs.update(920, { active: true });
  tabUpdates.length = 0;
  sendMessageImpl = async (tabId) => {
    assert.equal(tabId, 103);
    await chrome.tabs.update(921, { active: true });
    return { ok: true, result: { state: "failed" } };
  };
  await hooks.visibleContentCall(
    103,
    { type: "tripchord:extract" },
    {
      lease: {
        task_id: "visible-user-switch",
        provider: "ctrip",
        kind: "lodging",
      },
      deadline: Date.now() + 5000,
      ownedTabIds: new Set([103]),
    },
  );
  assert.equal((await chrome.tabs.get(921)).active, true);
  assert.equal((await chrome.tabs.get(920)).active, false);
  assert.equal(
    tabUpdates.some(
      ({ tabId, update }) => tabId === 920 && update.active === true,
    ),
    false,
  );
}

{
  browserWindows.set(2, { id: 2, focused: false });
  tabs.set(930, {
    id: 930,
    windowId: 2,
    status: "complete",
    url: "https://example.test/window-two-user",
  });
  tabs.set(104, {
    id: 104,
    windowId: 2,
    status: "complete",
    url: "https://hotel.qunar.com/global/list",
  });
  await chrome.tabs.update(930, { active: true });
  await chrome.windows.update(1, { focused: true });
  tabUpdates.length = 0;
  windowUpdates.length = 0;
  sendMessageImpl = async () => ({
    ok: true,
    result: { state: "failed" },
  });
  await hooks.visibleContentCall(
    104,
    { type: "tripchord:extract" },
    {
      lease: {
        task_id: "visible-window-restore",
        provider: "qunar",
        kind: "lodging",
      },
      deadline: Date.now() + 5000,
      ownedTabIds: new Set([104]),
    },
  );
  assert.equal((await chrome.tabs.get(930)).active, true);
  assert.equal((await chrome.windows.get(1)).focused, true);
  assert.equal((await chrome.windows.get(2)).focused, false);
  assert.deepEqual(
    windowUpdates.map(({ windowId }) => windowId),
    [],
  );
}

{
  await chrome.tabs.update(900, { active: true });
  tabs.set(105, {
    id: 105,
    windowId: 1,
    status: "complete",
    url: "https://hotels.ctrip.com/hotels/list",
  });
  tabs.set(106, {
    id: 106,
    windowId: 1,
    status: "complete",
    url: "https://hotels.ctrip.com/hotels/list",
  });
  sendMessageImpl = async (tabId) => {
    if (tabId === 105) {
      throw new Error("fixture content failure");
    }
    return { ok: true, result: { state: "succeeded" } };
  };
  await assert.rejects(
    hooks.visibleContentCall(
      105,
      { type: "tripchord:extract" },
      {
        lease: {
          task_id: "visible-error-release",
          provider: "ctrip",
          kind: "lodging",
        },
        deadline: Date.now() + 5000,
        ownedTabIds: new Set([105]),
      },
    ),
    /fixture content failure/,
  );
  const afterFailure = await hooks.visibleContentCall(
    106,
    { type: "tripchord:extract-transfer-detail" },
    {
      lease: {
        task_id: "visible-after-error",
        provider: "ctrip",
        kind: "lodging",
      },
      deadline: Date.now() + 5000,
      ownedTabIds: new Set([106]),
    },
  );
  assert.equal(afterFailure.state, "succeeded");
  assert.equal((await chrome.tabs.get(900)).active, true);
}

{
  tabs.set(107, {
    id: 107,
    windowId: 1,
    status: "complete",
    url: "https://hotels.ctrip.com/hotels/list",
  });
  tabs.set(108, {
    id: 108,
    windowId: 1,
    status: "complete",
    url: "https://hotels.ctrip.com/hotels/list",
  });
  await chrome.tabs.update(900, { active: true });
  sentMessages.length = 0;
  const defaultVisibilityProbe = visibilityProbeImpl;
  visibilityProbeImpl = async (tabId) => {
    if (tabId === 107) {
      return { visibilityState: "hidden", hidden: true };
    }
    return defaultVisibilityProbe(tabId);
  };
  const hiddenBackgroundResult = await hooks.visibleContentCall(
    107,
    { type: "tripchord:extract" },
    {
      lease: {
        task_id: "background-hidden-document",
        provider: "ctrip",
        kind: "lodging",
      },
      deadline: Date.now() + 5000,
      ownedTabIds: new Set([107]),
    },
  );
  assert.equal(hiddenBackgroundResult.state, "succeeded");
  assert.equal(
    sentMessages.some(({ tabId }) => tabId === 107),
    true,
  );
  assert.equal((await chrome.tabs.get(107)).active, false);
  assert.equal((await chrome.tabs.get(900)).active, true);
  visibilityProbeImpl = defaultVisibilityProbe;
  sendMessageImpl = async () => ({
    ok: true,
    result: { state: "succeeded" },
  });
  const afterVisibilityFailure = await hooks.visibleContentCall(
    108,
    { type: "tripchord:extract" },
    {
      lease: {
        task_id: "visible-after-probe-timeout",
        provider: "ctrip",
        kind: "lodging",
      },
      deadline: Date.now() + 5000,
      ownedTabIds: new Set([108]),
    },
  );
  assert.equal(afterVisibilityFailure.state, "succeeded");
}

{
  tabs.set(109, {
    id: 109,
    windowId: 1,
    status: "complete",
    url: "https://evil.example/redirect?secret=do-not-send",
  });
  tabs.set(110, {
    id: 110,
    windowId: 1,
    status: "complete",
    url: "https://hotels.ctrip.com/hotels/list",
  });
  await chrome.tabs.update(900, { active: true });
  sentMessages.length = 0;
  tabUpdates.length = 0;
  await assert.rejects(
    hooks.visibleContentCall(
      109,
      { type: "tripchord:extract" },
      {
        lease: {
          task_id: "visible-url-drift",
          provider: "ctrip",
          kind: "lodging",
        },
        deadline: Date.now() + 5000,
        ownedTabIds: new Set([109]),
      },
    ),
    (error) =>
      error.tripchordCode === "navigation_error" &&
      error.tripchordDetails.reason === "outside_provider_host" &&
      !JSON.stringify(error.tripchordDetails).includes("do-not-send"),
  );
  assert.equal(
    sentMessages.some(({ tabId }) => tabId === 109),
    false,
  );
  assert.equal(
    tabUpdates.some(
      ({ tabId, update }) => tabId === 109 && update.active === true,
    ),
    false,
  );
  sendMessageImpl = async () => ({
    ok: true,
    result: { state: "succeeded" },
  });
  assert.equal(
    (
      await hooks.visibleContentCall(
        110,
        { type: "tripchord:extract" },
        {
          lease: {
            task_id: "visible-after-url-drift",
            provider: "ctrip",
            kind: "lodging",
          },
          deadline: Date.now() + 5000,
          ownedTabIds: new Set([110]),
        },
      )
    ).state,
    "succeeded",
  );
}

{
  tabs.set(111, {
    id: 111,
    windowId: 1,
    status: "complete",
    url: "https://www.fliggy.com/?tab=hotel",
  });
  tabs.set(112, {
    id: 112,
    windowId: 1,
    status: "complete",
    url: "https://hotels.ctrip.com/hotels/list",
  });
  await chrome.tabs.update(900, { active: true });
  let markTriggerSent;
  const triggerSent = new Promise((resolve) => {
    markTriggerSent = resolve;
  });
  let triggerSettled = false;
  sendMessageImpl = async (tabId, message) => {
    if (tabId === 111) {
      assert.equal(message.type, "tripchord:trigger-search");
      assert.equal(onUpdated.listeners.size > 0, true);
      markTriggerSent();
      return {
        ok: true,
        result: {
          triggered: true,
          confirmation_scope: "visible_search_triggered",
        },
      };
    }
    return { ok: true, result: { state: "succeeded" } };
  };
  const triggerRun = hooks.triggerSearchWithLifecycle(
    111,
    "https://www.fliggy.com/?tab=hotel",
    {
      task_id: "visible-trigger-wait-outside-slot",
      provider: "fliggy",
      kind: "lodging",
      query: {},
    },
    Date.now() + 6000,
    new Set([111]),
  ).finally(() => {
    triggerSettled = true;
  });
  await triggerSent;
  const concurrentExtraction = await hooks.visibleContentCall(
    112,
    { type: "tripchord:extract" },
    {
      lease: {
        task_id: "visible-during-navigation-wait",
        provider: "ctrip",
        kind: "lodging",
      },
      deadline: Date.now() + 5000,
      ownedTabIds: new Set([112]),
    },
  );
  assert.equal(concurrentExtraction.state, "succeeded");
  assert.equal(triggerSettled, false);
  const triggerResult = await triggerRun;
  assert.equal(triggerResult.transitionMode, "spa_or_delayed_navigation");
}

{
  for (const tabId of [113, 114, 115]) {
    tabs.set(tabId, {
      id: tabId,
      windowId: 1,
      status: "complete",
      url: "https://hotels.ctrip.com/hotels/list",
    });
  }
  await chrome.tabs.update(900, { active: true });
  sentMessages.length = 0;
  tabUpdates.length = 0;
  let releaseQueuedHead;
  const queuedHeadGate = new Promise((resolve) => {
    releaseQueuedHead = resolve;
  });
  let markQueuedHeadEntered;
  const queuedHeadEntered = new Promise((resolve) => {
    markQueuedHeadEntered = resolve;
  });
  sendMessageImpl = async (tabId) => {
    if (tabId === 113) {
      markQueuedHeadEntered();
      await queuedHeadGate;
    }
    return { ok: true, result: { state: "succeeded" } };
  };
  const queuedHead = hooks.visibleContentCall(
    113,
    { type: "tripchord:extract" },
    {
      lease: {
        task_id: "visible-queue-head",
        provider: "ctrip",
        kind: "lodging",
      },
      deadline: Date.now() + 5000,
      ownedTabIds: new Set([113]),
    },
  );
  await queuedHeadEntered;
  const expiredFollower = hooks.visibleContentCall(
    114,
    { type: "tripchord:extract" },
    {
      lease: {
        task_id: "visible-expired-follower",
        provider: "ctrip",
        kind: "lodging",
      },
      deadline: Date.now() + 50,
      ownedTabIds: new Set([114]),
    },
  );
  const expiredFollowerAssertion = assert.rejects(
    expiredFollower,
    /timed out before completion/,
  );
  await new Promise((resolve) => setTimeout(resolve, 80));
  releaseQueuedHead();
  await queuedHead;
  await expiredFollowerAssertion;
  assert.equal(
    sentMessages.some(({ tabId }) => tabId === 114),
    false,
  );
  assert.equal(
    tabUpdates.some(
      ({ tabId, update }) => tabId === 114 && update.active === true,
    ),
    false,
  );
  const afterExpiredFollower = await hooks.visibleContentCall(
    115,
    { type: "tripchord:extract" },
    {
      lease: {
        task_id: "visible-after-expired-follower",
        provider: "ctrip",
        kind: "lodging",
      },
      deadline: Date.now() + 5000,
      ownedTabIds: new Set([115]),
    },
  );
  assert.equal(afterExpiredFollower.state, "succeeded");
}

{
  tabs.set(116, {
    id: 116,
    windowId: 1,
    status: "complete",
    url: "https://hotels.ctrip.com/hotels/list",
  });
  await chrome.tabs.update(900, { active: true });
  sentMessages.length = 0;
  tabUpdates.length = 0;
  const claimedFetch = context.fetch;
  context.fetch = async (url, options) => {
    if (String(url).includes("/v1/tasks/visible-cancelled-before-slot")) {
      return {
        ok: true,
        status: 200,
        async json() {
          return {
            state: "cancelled",
            claimed_by: null,
          };
        },
      };
    }
    return claimedFetch(url, options);
  };
  await assert.rejects(
    hooks.visibleContentCall(
      116,
      { type: "tripchord:extract" },
      {
        lease: {
          task_id: "visible-cancelled-before-slot",
          provider: "ctrip",
          kind: "lodging",
        },
        deadline: Date.now() + 5000,
        ownedTabIds: new Set([116]),
      },
    ),
    (error) => error.status === 409,
  );
  context.fetch = claimedFetch;
  assert.equal(
    sentMessages.some(({ tabId }) => tabId === 116),
    false,
  );
  assert.equal(
    tabUpdates.some(
      ({ tabId, update }) => tabId === 116 && update.active === true,
    ),
    false,
  );
}

const ctripDetailQuery = {
  origin: "杭州",
  destination: "Maafushi",
  start_date: "2026-08-01",
  end_date: "2026-08-05",
  adults: 2,
  rooms: 1,
  currency: "CNY",
  options: {
    expected_package_area: "destination_island",
    expected_lodging_place_key: "马富施",
    segment: "full",
  },
};
const ctripDetailListPage =
  "https://hotels.ctrip.com/hotels/list" +
  "?checkin=2026-08-01&checkout=2026-08-05";
const ctripDetailRelative =
  "/hotels/detail/?cityEnName=Kandima&cityId=705796&hotelId=6210622" +
  "&checkIn=2026-08-01&checkOut=2026-08-05" +
  "&adult=2&children=0&crn=1";
{
  const detailUrl = new URL(
    ctripDetailRelative,
    ctripDetailListPage,
  ).href;
  const target = {
    href: detailUrl,
    hotel_id: "6210622",
    preview: {
      expected_place_key: "maafushi",
      place_match: "exact",
      exact_place_evidence:
        "property_name: Maafushi Kaani Palm Beach",
      distance_reference_evidence: null,
    },
  };
  const quote = {
    provider: "ctrip",
    kind: "lodging",
    page_url: detailUrl,
    amount: 1291,
    currency: "CNY",
    price_basis: "per_night",
    taxes_included: true,
    visible_evidence: "fixture exact detail evidence",
    evidence_sha256: "a".repeat(64),
    details: {
      check_in: ctripDetailQuery.start_date,
      check_out: ctripDetailQuery.end_date,
      adults: ctripDetailQuery.adults,
      rooms: ctripDetailQuery.rooms,
      property_id: "6210622",
      property_name: "Maafushi Kaani Palm Beach",
      room_text: "Deluxe room",
      rate_text: "含税/费后 均¥1,291 预订",
      area_text: "近 Bikini Beach · Sunrise Beach",
      area_source: "visible_label",
      area_matches_expected: true,
      expected_lodging_place_key: "maafushi",
      observed_lodging_place_key: "maafushi",
      lodging_place_matches_expected: true,
      availability: "available",
      availability_text: "预订",
      tax_evidence: "含税/费后 均¥1,291",
      price_finality: "final_for_rate",
      price_unit_evidence: "¥1,291/晚",
    },
  };
  assert.equal(
    hooks.exactCtripLodgingDetailQuoteDecision(
      quote,
      target,
      ctripDetailListPage,
      ctripDetailQuery,
    ).allowed,
    true,
  );
  assert.equal(
    hooks.exactCtripLodgingDetailQuoteDecision(
      quote,
      {
        ...target,
        preview: {
          ...target.preview,
          place_match: "distance_only",
          exact_place_evidence: null,
          distance_reference_evidence:
            "Dhaalu Atoll · 距马富士岛中心区域18.2公里",
        },
      },
      ctripDetailListPage,
      ctripDetailQuery,
    ).reason,
    "quote_expected_place_unverified",
  );
}
{
  const receiptDriver = {
    triggered: true,
    confirmation_scope: "confirmed_visible_search",
    confirmed_query: { ...ctripDetailQuery },
  };
  const candidateSummaries = [{
    candidate_index: 99,
    title: "Kaani owner@example.com 13912345678",
    area_evidence: "Maafushi https://private.example/guest",
    room_evidence: null,
    price_evidence: null,
    price_basis: "unknown",
    price_finality: "unknown",
    place_match: "exact",
  }];
  const built = await hooks.createBackgroundLodgingInventoryReceipt({
    provider: "ctrip",
    query: ctripDetailQuery,
    driver: receiptDriver,
    parser_version: hooks.LODGING_INVENTORY_RECEIPT_PARSER_VERSION,
    candidate_summaries: candidateSummaries,
    explicit_empty_evidence: null,
    page_url: `${ctripDetailListPage}&tracking=private#secret`,
    captured_at: "2026-07-30T12:00:00.000Z",
  });
  assert.ok(built);
  assert.equal(built.receipt.scan_limit, 12);
  assert.equal(built.receipt.scanned_count, 1);
  assert.equal(built.receipt.candidate_summaries[0].candidate_index, 0);
  assert.equal(built.receipt.explicit_empty_evidence, null);
  assert.equal(
    built.receipt.page_url,
    "https://hotels.ctrip.com/hotels/list",
  );
  assert.equal(
    built.receipt.confirmed_query.options.expected_lodging_place_key,
    "maafushi",
  );
  assert.equal(
    (
      await hooks.validateBackgroundLodgingInventoryReceipt(
        built.receipt,
        built.receipt_sha256,
      )
    ).valid,
    true,
  );
  const hulhumaleBuilt =
    await hooks.createBackgroundLodgingInventoryReceipt({
      provider: "ctrip",
      query: {
        ...ctripDetailQuery,
        options: {
          expected_lodging_place_key: "hulhumale",
          expected_package_area: "airport_island",
          segment: "hulhumale-full",
        },
      },
      driver: receiptDriver,
      parser_version: hooks.LODGING_INVENTORY_RECEIPT_PARSER_VERSION,
      candidate_summaries: candidateSummaries,
      page_url: ctripDetailListPage,
      captured_at: "2026-07-30T12:00:00.000Z",
    });
  assert.equal(
    hulhumaleBuilt.receipt.confirmed_query.options.segment,
    "hulhumale-full",
  );
  assert.equal(
    await hooks.createBackgroundLodgingInventoryReceipt({
      provider: "ctrip",
      query: {
        ...ctripDetailQuery,
        options: {
          ...ctripDetailQuery.options,
          segment: "hulhumale-full-typo",
        },
      },
      driver: receiptDriver,
      parser_version: hooks.LODGING_INVENTORY_RECEIPT_PARSER_VERSION,
      candidate_summaries: candidateSummaries,
      page_url: ctripDetailListPage,
      captured_at: "2026-07-30T12:00:00.000Z",
    }),
    null,
  );
  for (const optionName of [
    "expected_lodging_place_key",
    "expected_package_area",
    "segment",
  ]) {
    const missingOptions = { ...ctripDetailQuery.options };
    delete missingOptions[optionName];
    assert.equal(
      await hooks.createBackgroundLodgingInventoryReceipt({
        provider: "ctrip",
        query: { ...ctripDetailQuery, options: missingOptions },
        driver: receiptDriver,
        parser_version: hooks.LODGING_INVENTORY_RECEIPT_PARSER_VERSION,
        candidate_summaries: candidateSummaries,
        page_url: ctripDetailListPage,
        captured_at: "2026-07-30T12:00:00.000Z",
      }),
      null,
      `missing ${optionName} must not sign a receipt`,
    );
    for (const invalidOptionValue of [null, "  "]) {
      assert.equal(
        await hooks.createBackgroundLodgingInventoryReceipt({
          provider: "ctrip",
          query: {
            ...ctripDetailQuery,
            options: {
              ...ctripDetailQuery.options,
              [optionName]: invalidOptionValue,
            },
          },
          driver: receiptDriver,
          parser_version: hooks.LODGING_INVENTORY_RECEIPT_PARSER_VERSION,
          candidate_summaries: candidateSummaries,
          page_url: ctripDetailListPage,
          captured_at: "2026-07-30T12:00:00.000Z",
        }),
        null,
        `blank ${optionName} must not sign a receipt`,
      );
    }
  }
  assert.equal(
    await hooks.createBackgroundLodgingInventoryReceipt({
      provider: "ctrip",
      query: ctripDetailQuery,
      driver: receiptDriver,
      parser_version: hooks.LODGING_INVENTORY_RECEIPT_PARSER_VERSION,
      candidate_summaries: [{}],
      explicit_empty_evidence: null,
      page_url: ctripDetailListPage,
      captured_at: "2026-07-30T12:00:00.000Z",
    }),
    null,
  );
  assert.equal(
    await hooks.inventoryReceiptSha256(
      hooks.canonicalInventoryJson({ z: 1, a: { y: 2, x: 1 } }),
    ),
    "b5d361a1c0dc5ed1dab76fcbaa2c270ff891ced6fba0ae3d69a2c72e36a302aa",
  );
  assert.equal(
    await hooks.createBackgroundLodgingInventoryReceipt({
      provider: "ctrip",
      query: ctripDetailQuery,
      driver: null,
      parser_version: hooks.LODGING_INVENTORY_RECEIPT_PARSER_VERSION,
      candidate_summaries: candidateSummaries,
      page_url: ctripDetailListPage,
      captured_at: "2026-07-30T12:00:00.000Z",
    }),
    null,
  );
  assert.equal(
    await hooks.createBackgroundLodgingInventoryReceipt({
      provider: "ctrip",
      query: ctripDetailQuery,
      driver: {
        ...receiptDriver,
        confirmation_scope: "provider_url_only_unverified",
      },
      parser_version: hooks.LODGING_INVENTORY_RECEIPT_PARSER_VERSION,
      candidate_summaries: candidateSummaries,
      page_url: ctripDetailListPage,
      captured_at: "2026-07-30T12:00:00.000Z",
    }),
    null,
  );
  assert.equal(
    await hooks.createBackgroundLodgingInventoryReceipt({
      provider: "ctrip",
      query: ctripDetailQuery,
      driver: receiptDriver,
      parser_version: "tripchord-visible-dom-v2",
      candidate_summaries: candidateSummaries,
      page_url: ctripDetailListPage,
      captured_at: "2026-07-30T12:00:00.000Z",
    }),
    null,
  );
  assert.equal(
    await hooks.createBackgroundLodgingInventoryReceipt({
      provider: "ctrip",
      query: ctripDetailQuery,
      driver: receiptDriver,
      parser_version: hooks.LODGING_INVENTORY_RECEIPT_PARSER_VERSION,
      candidate_summaries: [],
      explicit_empty_evidence: null,
      page_url: ctripDetailListPage,
      captured_at: "2026-07-30T12:00:00.000Z",
    }),
    null,
  );
  assert.equal(
    await hooks.createBackgroundLodgingInventoryReceipt({
      provider: "ctrip",
      query: ctripDetailQuery,
      driver: receiptDriver,
      parser_version: hooks.LODGING_INVENTORY_RECEIPT_PARSER_VERSION,
      candidate_summaries: [],
      explicit_empty_evidence: {
        code: "unaudited_empty",
        text_summary: "暂无酒店",
      },
      page_url: ctripDetailListPage,
      captured_at: "2026-07-30T12:00:00.000Z",
    }),
    null,
  );
  const tampered = {
    ...built.receipt,
    scanned_count: 0,
  };
  assert.equal(
    (
      await hooks.validateBackgroundLodgingInventoryReceipt(
        tampered,
        built.receipt_sha256,
      )
    ).valid,
    false,
  );
  assert.equal(
    (
      await hooks.validateBackgroundLodgingInventoryReceipt(
        built.receipt,
        "0".repeat(64),
      )
    ).reason,
    "receipt_sha256_mismatch",
  );
  const unknownSegmentReceipt = JSON.parse(JSON.stringify(built.receipt));
  unknownSegmentReceipt.confirmed_query.options.segment =
    "hulhumale-full-typo";
  const unknownSegmentSha = await hooks.inventoryReceiptSha256(
    hooks.canonicalInventoryJson(unknownSegmentReceipt),
  );
  assert.equal(
    (
      await hooks.validateBackgroundLodgingInventoryReceipt(
        unknownSegmentReceipt,
        unknownSegmentSha,
      )
    ).reason,
    "confirmed_query_options_invalid",
  );
  for (const optionName of [
    "expected_lodging_place_key",
    "expected_package_area",
    "segment",
  ]) {
    for (const invalidOptionValue of [null, "  "]) {
      const invalidOptionReceipt = JSON.parse(JSON.stringify(built.receipt));
      invalidOptionReceipt.confirmed_query.options[optionName] =
        invalidOptionValue;
      const invalidOptionSha = await hooks.inventoryReceiptSha256(
        hooks.canonicalInventoryJson(invalidOptionReceipt),
      );
      assert.equal(
        (
          await hooks.validateBackgroundLodgingInventoryReceipt(
            invalidOptionReceipt,
            invalidOptionSha,
          )
        ).reason,
        "confirmed_query_options_invalid",
        `validator must reject blank ${optionName}`,
      );
    }
  }
}
assert.equal(
  hooks.ctripExpectedPlaceEvidenceDecision(
    "马富士岛 · 距马富士岛中心区域 0.5 公里",
    "maafushi",
  ),
  "exact",
);
assert.equal(
  hooks.ctripExpectedPlaceEvidenceDecision(
    "康迪马岛 · 距马富士岛中心区域 138.4 公里",
    "maafushi",
  ),
  "distance_only",
);
assert.equal(
  hooks.canonicalCtripLodgingPlaceKey("马富施"),
  "maafushi",
);
assert.equal(
  hooks.canonicalCtripLodgingPlaceKey("马富士岛"),
  "maafushi",
);
assert.equal(
  hooks.canonicalCtripLodgingPlaceKey("胡鲁马累"),
  "hulhumale",
);
assert.equal(
  hooks.ctripExpectedPlaceEvidenceDecision(
    "Maafushi · Kaafu Atoll",
    "马富施",
  ),
  "exact",
);
assert.equal(
  hooks.ctripExpectedPlaceEvidenceDecision(
    "Hulhumale · Maldives",
    "胡鲁马累",
  ),
  "exact",
);
assert.equal(
  hooks.ctripExpectedPlaceEvidenceDecision(
    "提马拉富士 · 距马富士岛中心区域 500 公里",
    "maafushi",
  ),
  "distance_only",
);
const safeCtripDetail = hooks.ctripLodgingDetailUrlDecision(
  ctripDetailRelative,
  ctripDetailListPage,
  ctripDetailQuery,
);
assert.equal(safeCtripDetail.allowed, true);
assert.equal(safeCtripDetail.hotel_id, "6210622");
assert.equal(
  safeCtripDetail.href,
  `https://hotels.ctrip.com${ctripDetailRelative}`,
);
assert.equal(
  hooks.providerHostDecision("ctrip", safeCtripDetail.href).allowed,
  true,
);
for (const [mutated, reason] of [
  [null, "missing_detail_url"],
  [
    ctripDetailRelative.replace(
      "hotels/detail/",
      "hotels/list/",
    ),
    "detail_wrong_path",
  ],
  [
    `https://evil.example${ctripDetailRelative}`,
    "detail_wrong_host",
  ],
  [
    ctripDetailRelative.replace("checkIn=2026-08-01", "checkIn=2026-08-02"),
    "detail_check_in_mismatch",
  ],
  [
    ctripDetailRelative.replace("adult=2", "adult=1"),
    "detail_adults_mismatch",
  ],
  [
    ctripDetailRelative.replace("crn=1", "crn=2"),
    "detail_rooms_mismatch",
  ],
  [
    `${ctripDetailRelative}&redirectUrl=https%3A%2F%2Fevil.example`,
    "detail_transaction_marker",
  ],
  [
    `${ctripDetailRelative}&order=pay-now`,
    "detail_transaction_marker",
  ],
]) {
  const decision = hooks.ctripLodgingDetailUrlDecision(
    mutated,
    ctripDetailListPage,
    ctripDetailQuery,
  );
  assert.equal(decision.allowed, false);
  assert.equal(decision.reason, reason);
}

{
  const seeded = hooks.ctripAuditedSeedTargets(ctripDetailQuery);
  assert.equal(seeded.length, 2);
  assert.equal(
    seeded.map((item) => item.hotel_id).join(","),
    "47330536,131576087",
  );
  assert.equal(
    seeded.every(
      (item) =>
        item.preview.place_match === "exact" &&
        item.preview.expected_place_key === "maafushi" &&
        hooks.ctripLodgingDetailUrlDecision(
          item.href,
          ctripDetailListPage,
          ctripDetailQuery,
        ).allowed,
    ),
    true,
  );
  const hulhumaleSeeded = hooks.ctripAuditedSeedTargets({
    ...ctripDetailQuery,
    destination: "Hulhumalé",
    options: {
      ...ctripDetailQuery.options,
      expected_lodging_place_key: "hulhumale",
      expected_package_area: "airport_island",
      segment: "hulhumale-full",
    },
  });
  assert.equal(hulhumaleSeeded.length, 2);
  assert.equal(
    hulhumaleSeeded.map((item) => item.hotel_id).join(","),
    "29935473,1948695",
  );
}

{
  const originalPopupCalls = [];
  const originalOpen = (...args) => {
    originalPopupCalls.push(args);
    return {};
  };
  const clickCounts = new Map();
  const makeCard = (propertyName, subtitle, position) => {
    const nodes = {
      ".hotelName": { innerText: propertyName },
      ".hotel-subtitle": { innerText: subtitle },
      ".position-desc": { innerText: position },
    };
    return {
      innerText: `${propertyName} ${subtitle} ${position}`,
      querySelector(selector) {
        return nodes[selector] || null;
      },
      querySelectorAll(selector) {
        return nodes[selector] ? [nodes[selector]] : [];
      },
    };
  };
  const makeControl = (id, label, url, visible, card) => ({
    innerText: label,
    textContent: label,
    disabled: false,
    closest(selector) {
      return selector.includes(".right-card") ? card : null;
    },
    getAttribute() {
      return null;
    },
    getBoundingClientRect() {
      return {
        width: visible ? 120 : 0,
        height: visible ? 32 : 0,
      };
    },
    addEventListener() {},
    removeEventListener() {},
    click() {
      clickCounts.set(id, (clickCounts.get(id) || 0) + 1);
      context.window.open(url);
    },
  });
  const distanceCard = makeCard(
    "康迪马岛酒店",
    "Kandima Maldives",
    "康迪马岛 · 距马富士岛中心区域 138.4 公里",
  );
  const exactCard = makeCard(
    "Maafushi Seaview Hotel",
    "Maafushi Seaview",
    "卡夫环礁 · 国际酒店区",
  );
  const substringCard = makeCard(
    "提马拉富士酒店",
    "Thimarafushi Resort",
    "提马拉富士 · 距马富士岛中心区域 500 公里",
  );
  const firstDetail = makeControl(
    "distance",
    "查看详情",
    ctripDetailRelative,
    true,
    distanceCard,
  );
  const purchase = makeControl(
    "purchase",
    "预订",
    "/checkout?order=unsafe",
    true,
    distanceCard,
  );
  const hiddenDetail = makeControl(
    "hidden",
    "查看详情",
    "/hidden",
    false,
    distanceCard,
  );
  const secondDetail = makeControl(
    "exact",
    "查 看 详 情",
    ctripDetailRelative.replace("hotelId=6210622", "hotelId=6210623"),
    true,
    exactCard,
  );
  const thirdDetail = makeControl(
    "substring",
    "查看详情",
    ctripDetailRelative.replace("hotelId=6210622", "hotelId=6210624"),
    true,
    substringCard,
  );
  let observedSelector = null;
  context.window = {
    open: originalOpen,
    getComputedStyle() {
      return {
        display: "block",
        visibility: "visible",
        opacity: "1",
      };
    },
  };
  context.document = {
    querySelectorAll(selector) {
      observedSelector = selector;
      return [
        firstDetail,
        purchase,
        hiddenDetail,
        secondDetail,
        thirdDetail,
      ];
    },
  };
  const capture = await hooks.ctripCaptureVisibleLodgingDetailUrls(
    6,
    "maafushi",
    ["马富施", "马富士", "maafushi"],
    12,
  );
  assert.equal(observedSelector, ".room-right .book-btn");
  assert.equal(capture.capture_code, "captured");
  assert.equal(capture.controls_seen, 5);
  assert.equal(capture.exact_visible_controls, 3);
  assert.equal(capture.clicked_controls, 1);
  assert.equal(capture.captures.length, 1);
  assert.equal(
    capture.captures[0].control_index,
    1,
  );
  assert.equal(capture.previews.length, 3);
  assert.equal(capture.previews[0].place_match, "distance_only");
  assert.equal(capture.previews[1].place_match, "exact");
  assert.equal(capture.previews[1].actual_location_prefix, "卡夫环礁");
  assert.equal(
    capture.previews[1].exact_place_evidence,
    "property_name: Maafushi Seaview Hotel",
  );
  assert.equal(capture.previews[2].place_match, "distance_only");
  assert.equal(clickCounts.get("purchase") || 0, 0);
  assert.equal(clickCounts.get("distance") || 0, 0);
  assert.equal(clickCounts.get("exact"), 1);
  assert.equal(clickCounts.get("substring") || 0, 0);
  assert.equal(originalPopupCalls.length, 0);
  assert.equal(context.window.open, originalOpen);
  context.document = {
    querySelectorAll() {
      return [firstDetail, thirdDetail];
    },
  };
  const noExactCapture =
    await hooks.ctripCaptureVisibleLodgingDetailUrls(
      6,
      "maafushi",
      ["马富施", "马富士", "maafushi"],
      12,
    );
  assert.equal(
    noExactCapture.capture_code,
    "expected_place_preview_not_found",
  );
  assert.equal(noExactCapture.clicked_controls, 0);
  assert.equal(noExactCapture.popup_interceptions, 0);
  assert.equal(noExactCapture.previews.length, 2);
  assert.equal(
    noExactCapture.previews.every(
      ({ place_match }) => place_match === "distance_only",
    ),
    true,
  );
  assert.equal(originalPopupCalls.length, 0);
  const countryPrefixedCard = makeCard(
    "马尔代夫马富士纳尼亚酒店",
    "Narnia Maldives",
    "卡夫环礁 · 南部",
  );
  const countryPrefixedDetail = makeControl(
    "country-prefixed",
    "查看详情",
    ctripDetailRelative.replace("hotelId=6210622", "hotelId=6210625"),
    true,
    countryPrefixedCard,
  );
  context.document = {
    querySelectorAll() {
      return [countryPrefixedDetail];
    },
  };
  const countryPrefixedCapture =
    await hooks.ctripCaptureVisibleLodgingDetailUrls(
      6,
      "maafushi",
      ["马富施", "马富士", "maafushi"],
      12,
    );
  assert.equal(countryPrefixedCapture.capture_code, "captured");
  assert.equal(countryPrefixedCapture.clicked_controls, 1);
  assert.equal(
    countryPrefixedCapture.previews[0].exact_place_evidence,
    "property_name: 马尔代夫马富士纳尼亚酒店",
  );
  assert.equal(clickCounts.get("country-prefixed"), 1);
  assert.equal(originalPopupCalls.length, 0);
  const directHrefDetail = {
    ...makeControl(
      "direct-href",
      "查看详情",
      ctripDetailRelative,
      true,
      exactCard,
    ),
    getAttribute(name) {
      return name === "data-href" ? ctripDetailRelative : null;
    },
    click() {
      clickCounts.set(
        "direct-href",
        (clickCounts.get("direct-href") || 0) + 1,
      );
    },
  };
  context.document = {
    querySelectorAll() {
      return [directHrefDetail];
    },
  };
  const directHrefCapture =
    await hooks.ctripCaptureVisibleLodgingDetailUrls(
      6,
      "maafushi",
      ["马富施", "马富士", "maafushi"],
      12,
    );
  assert.equal(directHrefCapture.capture_code, "captured");
  assert.equal(directHrefCapture.captures.length, 1);
  assert.equal(
    directHrefCapture.captures[0].raw_url,
    ctripDetailRelative,
  );
  assert.equal(clickCounts.get("direct-href"), 1);
  delete context.document;
  delete context.window;
}

{
  tabs.set(117, {
    id: 117,
    windowId: 1,
    status: "complete",
    url: ctripDetailListPage,
  });
  tabs.set(118, {
    id: 118,
    windowId: 1,
    status: "complete",
    url: ctripDetailListPage,
  });
  await chrome.tabs.update(900, { active: true });
  mainWorldScriptImpl = async ({ target }) => {
    if (target.tabId === 117) {
      throw new Error("fixture MAIN capture failure");
    }
    return [{ result: { capture_code: "detail_url_not_observed" } }];
  };
  await assert.rejects(
    hooks.captureCtripLodgingDetailTargets(
      117,
      {
        task_id: "ctrip-capture-queue-release",
        provider: "ctrip",
        kind: "lodging",
        query: ctripDetailQuery,
      },
      Date.now() + 5000,
      new Set([117]),
    ),
    /fixture MAIN capture failure/,
  );
  sendMessageImpl = async () => ({
    ok: true,
    result: { state: "succeeded" },
  });
  const afterCaptureFailure = await hooks.visibleContentCall(
    118,
    { type: "tripchord:extract" },
    {
      lease: {
        task_id: "after-ctrip-capture-failure",
        provider: "ctrip",
        kind: "lodging",
      },
      deadline: Date.now() + 5000,
      ownedTabIds: new Set([118]),
    },
  );
  assert.equal(afterCaptureFailure.state, "succeeded");
}

{
  tabs.set(124, {
    id: 124,
    windowId: 1,
    status: "complete",
    url: ctripDetailListPage,
  });
  await chrome.tabs.update(900, { active: true });
  let extractionCalls = 0;
  sendMessageImpl = async (_tabId, message) => {
    assert.equal(message.type, "tripchord:extract");
    extractionCalls += 1;
    return {
      ok: true,
      result: {
        state: "failed",
        quotes: [],
        failure: {
          code: "dom_drift",
          message: "list contains starting prices only",
          retryable: false,
        },
      },
    };
  };
  const extraction = await hooks.extractWithRetry(
    124,
    {
      task_id: "ctrip-list-first-drift",
      provider: "ctrip",
      kind: "lodging",
      query: ctripDetailQuery,
    },
    {
      triggered: true,
      confirmation_scope: "confirmed_visible_search",
      confirmed_query: { ...ctripDetailQuery },
    },
    Date.now() + 60000,
    new Set([124]),
  );
  assert.equal(extraction.failure.code, "dom_drift");
  assert.equal(extractionCalls, 1);
}

{
  // A qunar lodging search list that keeps showing the realtime-search shell
  // must seal the bounded-pending receipt as soon as the minimum observation
  // window has elapsed (QUNAR_PENDING_MIN_OBSERVED_MS), instead of squandering
  // the lease re-reading the same non-terminal DOM until near the extraction
  // deadline.  Sealing early is what leaves budget for the audited detail-page
  // fallback (orchestrateQunarLodgingDetails).
  tabs.set(126, {
    id: 126,
    windowId: 1,
    status: "complete",
    url: "https://hotel.qunar.com/city/i-ka_maafushi/",
  });
  await chrome.tabs.update(900, { active: true });
  let extractionCalls = 0;
  const capturedObservedDurations = [];
  sendMessageImpl = async (_tabId, message) => {
    assert.equal(message.type, "tripchord:extract");
    extractionCalls += 1;
    const baseDetails = {
      parser_version: hooks.LODGING_INVENTORY_RECEIPT_PARSER_VERSION,
      dom_diagnostics: {
        scope: "visible_candidate_cards_only",
        max_candidates: 6,
        candidates: [],
        result_state_evidence: [
          { tag: "div", class: "hotels-num", text_summary: "共 家酒店满足条件" },
          { tag: "a", class: "clear js_clear", text_summary: "清空筛选条件" },
          { tag: "p", class: "msg", text_summary: "请稍等,您查询的结果正在实时搜索中..." },
          { tag: "span", class: "nopr", text_summary: "暂无报价" },
        ],
      },
    };
    const observed = message.driver && message.driver.bounded_pending_observed_ms;
    if (Number.isInteger(observed) && observed >= hooks.QUNAR_PENDING_MIN_OBSERVED_MS) {
      capturedObservedDurations.push(observed);
      const receipt = {
        schema_version: hooks.LODGING_INVENTORY_RECEIPT_SCHEMA_VERSION,
        parser_version: hooks.LODGING_INVENTORY_RECEIPT_PARSER_VERSION,
        provider: "qunar",
        state: "bounded_provider_pending",
        confirmed_query: {
          destination: qunarLodgingQuery.destination,
          start_date: qunarLodgingQuery.start_date,
          end_date: qunarLodgingQuery.end_date,
          adults: qunarLodgingQuery.adults,
          rooms: qunarLodgingQuery.rooms,
          options: { ...qunarLodgingQuery.options },
        },
        confirmation_scope: "confirmed_visible_search",
        scan_limit: 12,
        scanned_count: 0,
        candidate_summaries: [],
        explicit_empty_evidence: null,
        provider_pending_evidence: {
          contract_version: hooks.QUNAR_PENDING_CONTRACT_VERSION,
          result_count_text: hooks.QUNAR_PENDING_RESULT_COUNT_TEXT,
          pending_message: hooks.QUNAR_PENDING_MESSAGE,
          observed_duration_ms: observed,
        },
        page_url: "https://hotel.qunar.com/city/i-ka_maafushi/",
        captured_at: new Date().toISOString(),
      };
      const receiptSha256 = await hooks.inventoryReceiptSha256(
        hooks.canonicalInventoryJson(receipt),
      );
      return {
        ok: true,
        result: {
          state: "failed",
          quotes: [],
          failure: {
            code: "extraction_error",
            message: "精确住宿查询有界等待后仍处于平台实时搜索中",
            retryable: false,
            page_url: receipt.page_url,
            captured_at: receipt.captured_at,
            details: {
              ...baseDetails,
              inventory_result_state: "bounded_provider_pending",
              confirmed_exhaustive: false,
              scanned_count: 0,
              candidate_summaries: [],
              capture_code: "audited_qunar_bounded_realtime_search_pending",
              inventory_receipt: receipt,
              inventory_receipt_sha256: receiptSha256,
            },
          },
        },
      };
    }
    return {
      ok: true,
      result: {
        state: "failed",
        quotes: [],
        failure: {
          code: "dom_drift",
          message: "页面已加载，但没有找到可验证的报价卡片",
          retryable: false,
          page_url: "https://hotel.qunar.com/city/i-ka_maafushi/",
          captured_at: new Date().toISOString(),
          details: baseDetails,
        },
      },
    };
  };
  const extraction = await hooks.extractWithRetry(
    126,
    {
      task_id: "qunar-lodging-early-pending-seal",
      provider: "qunar",
      kind: "lodging",
      query: qunarLodgingQuery,
    },
    {
      triggered: true,
      confirmation_scope: "confirmed_visible_search",
      confirmed_query: { ...qunarLodgingQuery },
    },
    Date.now() + 60000,
    new Set([126]),
  );
  assert.equal(extraction.state, "failed");
  assert.equal(extraction.failure.code, "extraction_error");
  assert.equal(
    extraction.failure.details.inventory_result_state,
    "bounded_provider_pending",
  );
  assert.equal(capturedObservedDurations.length, 1);
  assert.ok(
    capturedObservedDurations[0] >= hooks.QUNAR_PENDING_MIN_OBSERVED_MS,
    "pending receipt must be sealed only after the minimum observation window",
  );
  assert.ok(
    capturedObservedDurations[0] < 40000,
    "pending receipt must be sealed at the minimum window, not near the deadline",
  );
  assert.equal(
    extraction.failure.details.inventory_receipt.provider_pending_evidence
      .observed_duration_ms,
    capturedObservedDurations[0],
  );
}

{
  tabs.set(125, {
    id: 125,
    windowId: 1,
    status: "complete",
    url: ctripDetailListPage,
  });
  await chrome.tabs.update(900, { active: true });
  const distancePreviews = Array.from({ length: 10 }, (_, control_index) => ({
    control_index,
    property_name:
      control_index === 0
        ? "Off-island owner@example.com 13912345678"
        : `Off-island hotel ${control_index}`,
    subtitle: `Fixture subtitle ${control_index}`,
    location_evidence: [
      `迪古拉岛 · 距马富士岛中心区域 ${50 + control_index} 公里`,
    ],
    position_summary:
      `迪古拉岛 · 距马富士岛中心区域 ${50 + control_index} 公里`,
    actual_location_prefix: "迪古拉岛",
    expected_place_key: "maafushi",
    place_match: "distance_only",
    exact_place_evidence: null,
    distance_reference_evidence:
      `迪古拉岛 · 距马富士岛中心区域 ${50 + control_index} 公里`,
    preview_text:
      control_index === 0
        ? "会员号 123456789012 https://private.example/guest"
        : null,
  }));
  mainWorldScriptImpl = async () => [{
    result: {
      capture_code: "expected_place_preview_not_found",
      controls_seen: 10,
      exact_visible_controls: 10,
      clicked_controls: 0,
      popup_interceptions: 0,
      previews: distancePreviews,
      ranked_control_indices: distancePreviews.map(
        ({ control_index }) => control_index,
      ),
      captures: [],
      click_errors: [],
    },
  }];
  createdTabRequests.length = 0;
  createTabImpl = async () => {
    throw new Error("distance-only candidates must not open detail tabs");
  };
  const owned = new Set([125]);
  const result = await hooks.orchestrateCtripLodgingDetails(
    125,
    {
      task_id: "ctrip-distance-only-fail-fast",
      provider: "ctrip",
      kind: "lodging",
      query: ctripDetailQuery,
    },
    {
      triggered: true,
      confirmation_scope: "confirmed_visible_search",
      confirmed_query: { ...ctripDetailQuery },
    },
    Date.now() + 8000,
    owned,
    {
      state: "failed",
      failure: {
        details: {
          parser_version:
            hooks.LODGING_INVENTORY_RECEIPT_PARSER_VERSION,
        },
      },
    },
  );
  assert.equal(result.state, "failed");
  assert.equal(
    result.failure.code,
    "lodging_expected_place_preview_not_found",
  );
  assert.equal(
    result.failure.details.detail_orchestration.preview_count,
    10,
  );
  assert.equal(
    result.failure.details.inventory_result_state,
    "bounded_no_exact_quote",
  );
  assert.equal(result.failure.details.confirmed_exhaustive, false);
  assert.equal(result.failure.details.scanned_count, 10);
  assert.equal(result.failure.details.candidate_summaries.length, 10);
  assert.equal(
    result.failure.details.capture_code,
    "expected_place_preview_not_found",
  );
  const distanceReceipt = result.failure.details.inventory_receipt;
  assert.equal(
    (
      await hooks.validateBackgroundLodgingInventoryReceipt(
        distanceReceipt,
        result.failure.details.inventory_receipt_sha256,
      )
    ).valid,
    true,
  );
  assert.equal(distanceReceipt.parser_version, "tripchord-visible-dom-v3");
  assert.equal(distanceReceipt.confirmation_scope, "confirmed_visible_search");
  assert.equal(distanceReceipt.scan_limit, 12);
  assert.equal(distanceReceipt.scanned_count, 10);
  assert.equal(distanceReceipt.explicit_empty_evidence, null);
  assert.equal(distanceReceipt.captured_at, result.failure.captured_at);
  assert.equal(distanceReceipt.page_url, result.failure.page_url);
  assert.equal(
    distanceReceipt.page_url,
    "https://hotels.ctrip.com/hotels/list",
  );
  assert.deepEqual(
    distanceReceipt.candidate_summaries,
    result.failure.details.candidate_summaries,
  );
  const serializedResult = JSON.stringify(result);
  assert.equal(serializedResult.includes("owner@example.com"), false);
  assert.equal(serializedResult.includes("13912345678"), false);
  assert.equal(serializedResult.includes("123456789012"), false);
  assert.equal(serializedResult.includes("https://private.example"), false);
  assert.equal(createdTabRequests.length, 0);
  await hooks.closeOwnedTabs(owned);
}

{
  tabs.set(119, {
    id: 119,
    windowId: 1,
    status: "complete",
    url: ctripDetailListPage,
  });
  await chrome.tabs.update(900, { active: true });
  const relativeTargets = [6210622, 6210623, 6210624].map((hotelId) =>
    ctripDetailRelative.replace("hotelId=6210622", `hotelId=${hotelId}`)
  );
  mainWorldScriptImpl = async ({ target, args, world }) => {
    assert.equal(target.tabId, 119);
    assert.equal(args[0], 6);
    assert.equal(args[1], "maafushi");
    assert.equal(args[2].includes("maafushi"), true);
    assert.equal(args[3], 12);
    assert.equal(world, "MAIN");
    return [{
      result: {
        capture_code: "captured",
        controls_seen: 3,
        exact_visible_controls: 3,
        clicked_controls: 3,
        popup_interceptions: 4,
        previews: relativeTargets.map((_rawUrl, control_index) => ({
          control_index,
          property_name: `Maafushi fixture ${control_index}`,
          subtitle: `Fixture subtitle ${control_index}`,
          location_evidence: ["马富士岛 · 卡夫环礁"],
          position_summary: "马富士岛 · 卡夫环礁",
          actual_location_prefix: "马富士岛",
          expected_place_key: "maafushi",
          place_match: "exact",
          exact_place_evidence: "马富士岛",
          distance_reference_evidence: null,
        })),
        ranked_control_indices: [0, 1, 2],
        captures: [
          { control_index: 0, raw_url: null },
          ...relativeTargets.map((raw_url, control_index) => ({
            control_index,
            raw_url,
          })),
        ],
        click_errors: [],
      },
    }];
  };
  let nextDetailTabId = 120;
  createdTabRequests.length = 0;
  createTabImpl = async ({ url, active }) => {
    assert.equal(active, false);
    const tab = {
      id: nextDetailTabId,
      windowId: 1,
      active: false,
      status: "complete",
      url,
    };
    nextDetailTabId += 1;
    tabs.set(tab.id, tab);
    return { ...tab };
  };
  const detailMessages = [];
  sendMessageImpl = async (tabId, message) => {
    detailMessages.push({ tabId, message });
    const pageUrl = (await chrome.tabs.get(tabId)).url;
    const hotelId = new URL(pageUrl).searchParams.get("hotelId");
    return {
      ok: true,
      result: {
        state: "succeeded",
        quotes: [{
          provider: "ctrip",
          kind: "lodging",
          page_url: pageUrl,
          amount: 1171 + tabId,
          currency: "CNY",
          price_basis: "per_night",
          taxes_included: true,
          title: `Fixture hotel ${tabId}`,
          visible_evidence: `fixture-evidence-${tabId}`,
          evidence_sha256: String(tabId).padStart(64, "a").slice(-64),
          details: {
            check_in: ctripDetailQuery.start_date,
            check_out: ctripDetailQuery.end_date,
            adults: ctripDetailQuery.adults,
            rooms: ctripDetailQuery.rooms,
            property_id: hotelId,
            property_name: `Fixture hotel ${tabId}`,
            room_text: "Deluxe room",
            rate_text: "含税/费后 均¥1,291 预订",
            area_text: "马富士岛",
            area_source: "visible_label",
            area_matches_expected: true,
            expected_lodging_place_key: "maafushi",
            observed_lodging_place_key: "maafushi",
            lodging_place_matches_expected: true,
            availability: "available",
            availability_text: "预订",
            tax_evidence: "含税/费后 均¥1,291",
            price_finality: "final_for_rate",
            price_unit_evidence: `¥${1171 + tabId}/晚`,
          },
        }],
      },
    };
  };
  const owned = new Set([119]);
  const result = await hooks.orchestrateCtripLodgingDetails(
    119,
    {
      task_id: "ctrip-list-detail-success",
      provider: "ctrip",
      kind: "lodging",
      query: ctripDetailQuery,
    },
    {
      triggered: true,
      confirmation_scope: "confirmed_visible_search",
      confirmed_query: { ...ctripDetailQuery },
      readback_query: { ...ctripDetailQuery },
    },
    Date.now() + 12000,
    owned,
  );
  assert.equal(result.state, "succeeded");
  assert.equal(result.quotes.length, 2);
  assert.equal(result.detail_orchestration.target_count, 2);
  assert.equal(result.detail_orchestration.popup_opened, false);
  assert.equal(
    result.detail_orchestration.workflow_budget_cap_ms,
    55000,
  );
  assert.equal(
    result.detail_orchestration.workflow_budget_remaining_ms <= 12000,
    true,
  );
  assert.equal(createdTabRequests.length, 2);
  assert.equal(
    createdTabRequests.every(({ active }) => active === false),
    true,
  );
  assert.equal(
    detailMessages.every(
      ({ message }) =>
        message.type === "tripchord:extract" &&
        !/预订|下单|支付|checkout|cashier|coupon|order|payment|paynow|booknow/i.test(
          JSON.stringify(message),
        ),
    ),
    true,
  );
  assert.deepEqual(
    detailMessages.map(({ tabId }) => tabId),
    [120, 121],
  );
  await hooks.closeOwnedTabs(owned);
  assert.equal(tabs.has(119), false);
  assert.equal(tabs.has(120), false);
  assert.equal(tabs.has(121), false);
}

{
  tabs.set(122, {
    id: 122,
    windowId: 1,
    status: "complete",
    url: ctripDetailListPage,
  });
  await chrome.tabs.update(900, { active: true });
  mainWorldScriptImpl = async () => [{
    result: {
      capture_code: "captured",
      controls_seen: 1,
      exact_visible_controls: 1,
      clicked_controls: 1,
      popup_interceptions: 1,
      previews: [{
        control_index: 0,
        property_name: "Maafushi redirect fixture",
        subtitle: "Fixture",
        location_evidence: ["马富士岛 · 卡夫环礁"],
        position_summary: "马富士岛 · 卡夫环礁",
        actual_location_prefix: "马富士岛",
        expected_place_key: "maafushi",
        place_match: "exact",
        exact_place_evidence: "马富士岛",
        distance_reference_evidence: null,
      }],
      ranked_control_indices: [0],
      captures: [{
        control_index: 0,
        raw_url: ctripDetailRelative,
      }],
      click_errors: [],
    },
  }];
  createTabImpl = async ({ url, active }) => {
    assert.equal(active, false);
    const tab = {
      id: 123,
      windowId: 1,
      active: false,
      status: "complete",
      url: `${url}&tracking=provider-redirect`,
    };
    tabs.set(tab.id, tab);
    return { ...tab };
  };
  sendMessageImpl = async () => {
    throw new Error("redirected detail must not reach content extraction");
  };
  const owned = new Set([122]);
  const result = await hooks.orchestrateCtripLodgingDetails(
    122,
    {
      task_id: "ctrip-list-detail-redirect-rejected",
      provider: "ctrip",
      kind: "lodging",
      query: ctripDetailQuery,
    },
    {
      triggered: true,
      confirmation_scope: "confirmed_visible_search",
      confirmed_query: { ...ctripDetailQuery },
    },
    Date.now() + 8000,
    owned,
    {
      state: "failed",
      failure: {
        details: {
          parser_version:
            hooks.LODGING_INVENTORY_RECEIPT_PARSER_VERSION,
        },
      },
    },
  );
  assert.equal(result.state, "failed");
  assert.equal(result.failure.code, "lodging_detail_quotes_unverified");
  assert.equal(
    result.failure.details.inventory_result_state,
    "bounded_no_exact_quote",
  );
  assert.equal(result.failure.details.confirmed_exhaustive, false);
  assert.equal(result.failure.details.scanned_count, 1);
  assert.equal(result.failure.details.candidate_summaries.length, 1);
  assert.equal(
    result.failure.details.capture_code,
    "lodging_detail_quotes_unverified",
  );
  const redirectReceipt = result.failure.details.inventory_receipt;
  assert.equal(
    (
      await hooks.validateBackgroundLodgingInventoryReceipt(
        redirectReceipt,
        result.failure.details.inventory_receipt_sha256,
      )
    ).valid,
    true,
  );
  assert.equal(redirectReceipt.scan_limit, 12);
  assert.equal(redirectReceipt.scanned_count, 1);
  assert.equal(redirectReceipt.explicit_empty_evidence, null);
  assert.equal(redirectReceipt.captured_at, result.failure.captured_at);
  assert.equal(redirectReceipt.page_url, result.failure.page_url);
  assert.equal(
    result.failure.details.detail_orchestration.detail_results[0].state,
    "rejected_redirect",
  );
  await hooks.closeOwnedTabs(owned);
}

{
  let completionPayload = null;
  let completionPostedAt = null;
  let tabCreationAttempted = false;
  storage.tripchordConnected = true;
  storage.tripchordBridgeToken =
    "fixture-token-that-is-at-least-32-characters";
  createTabImpl = async () => {
    tabCreationAttempted = true;
    throw new Error("expired work budget must stop before tab creation");
  };
  documentReadyStateProbeImpl = async () => "loading";
  context.fetch = async (url, options = {}) => {
    if (String(url).endsWith("/complete")) {
      completionPostedAt = Date.now();
      completionPayload = JSON.parse(options.body);
      return {
        ok: true,
        status: 200,
        async json() {
          return { state: "failed" };
        },
      };
    }
    throw new Error(`unexpected bridge request ${url}`);
  };
  const claimedAtMs = Date.now() - 12800;
  const leaseExpiresAtMs = claimedAtMs + 15000;
  const startedAt = Date.now();
  await hooks.executeLease({
    task_id: "initial-landing-budget-fixture",
    claim_token: "claim-token",
    timeout_seconds: 15,
    claimed_at: new Date(claimedAtMs).toISOString(),
    lease_expires_at: new Date(leaseExpiresAtMs).toISOString(),
    provider: "ctrip",
    kind: "lodging",
    query: {
      destination: "马富士",
      start_date: "2026-08-01",
      end_date: "2026-08-05",
      adults: 2,
      rooms: 1,
    },
  });
  assert.ok(Date.now() - startedAt < 1000);
  assert.equal(tabCreationAttempted, false);
  assert.equal(completionPayload.completion.state, "failed");
  assert.equal(completionPayload.completion.failure.code, "timeout");
  assert.equal(completionPayload.completion.failure.retryable, true);
  const stageTrace =
    completionPayload.completion.failure.details.stage_trace;
  assert.equal(stageTrace.length, 1);
  assert.equal(stageTrace[0].stage, "initial_landing");
  assert.equal(stageTrace[0].status, "timed_out");
  assert.equal(stageTrace[0].failure_code, "stage_timeout");
  assert.equal(stageTrace[0].budget_ms, 0);
  const leaseTiming =
    completionPayload.completion.failure.details.lease_timing;
  assert.equal(leaseTiming.deadline_source, "server_absolute");
  assert.equal(leaseTiming.lease_duration_ms, 15000);
  assert.equal(leaseTiming.completion_reserve_ms, 2500);
  assert.ok(leaseExpiresAtMs - completionPostedAt >= 1500);
  documentReadyStateProbeImpl = async (tabId) =>
    tabs.get(tabId)?.status === "complete" ? "complete" : "loading";
}

{
  const completionPayloads = [];
  storage.tripchordConnected = true;
  storage.tripchordBridgeToken =
    "fixture-token-that-is-at-least-32-characters";
  context.fetch = async (url, options = {}) => {
    if (!String(url).endsWith("/complete")) {
      throw new Error(`unexpected bridge request ${url}`);
    }
    completionPayloads.push(JSON.parse(options.body));
    if (completionPayloads.length === 1) {
      return {
        ok: false,
        status: 422,
        async json() {
          return {
            detail: [
              {
                loc: ["body", "completion", "quotes", 0, "details"],
                type: "value_error",
                msg: "live quote failed its exact contract",
                input: { account: "must-not-survive" },
              },
            ],
          };
        },
      };
    }
    return {
      ok: true,
      status: 200,
      async json() {
        return { state: "failed" };
      },
    };
  };
  const now = Date.now();
  await hooks.executeLease({
    task_id: "completion-contract-fallback-fixture",
    claim_token: "claim-token",
    timeout_seconds: 120,
    claimed_at: new Date(now).toISOString(),
    lease_expires_at: new Date(now + 120000).toISOString(),
    provider: "unsupported",
    kind: "lodging",
    query: {
      destination: "Maafushi",
      start_date: "2026-08-01",
      end_date: "2026-08-05",
      adults: 2,
      rooms: 1,
    },
  });
  assert.equal(completionPayloads.length, 2);
  const fallback = completionPayloads[1].completion;
  assert.equal(fallback.state, "failed");
  assert.equal(fallback.failure.code, "extraction_error");
  assert.equal(fallback.failure.details.bridge_http_status, 422);
  assert.equal(
    fallback.failure.details.validation_diagnostic[0].message,
    "live quote failed its exact contract",
  );
  assert.equal(
    JSON.stringify(fallback).includes("must-not-survive"),
    false,
  );
  assert.equal(
    storage.tripchordLastCompletionDiagnostic.http_status,
    422,
  );
  assert.equal(
    storage.tripchordLastCompletionDiagnostic.fallback_attempted,
    false,
  );
  assert.equal(
    storage.tripchordLastCompletionDiagnostic.lease_timing.deadline_source,
    "server_absolute",
  );
  assert.ok(
    Array.isArray(
      storage.tripchordLastCompletionDiagnostic.stage_trace,
    ),
  );
  assert.equal(
    JSON.stringify(storage.tripchordLastCompletionDiagnostic).includes(
      "must-not-survive",
    ),
    false,
  );
}

function fixtureReloadControl({
  requestId,
  targetBuildSha256,
  expectedRuntimeInstanceId = hooks.RUNTIME_INSTANCE_ID,
}) {
  return {
    action: "reload_extension",
    protocol_version: hooks.CONTROL_PROTOCOL_VERSION,
    request_id: requestId,
    target_build_sha256: targetBuildSha256,
    expected_runtime_instance_id: expectedRuntimeInstanceId,
    delivery_generation: 1,
    receipt_token: "receipt-token-that-is-at-least-32-characters",
    expires_at: new Date(Date.now() + 120000).toISOString(),
  };
}

{
  const release = {};
  release.promise = new Promise((resolve) => {
    release.resolve = resolve;
  });
  const execution = hooks.executeClaimedLeasesProviderAware(
    [{ task_id: "reload-drain-lease", provider: "ctrip", kind: "flight" }],
    async () => release.promise,
  );
  await new Promise((resolve) => setTimeout(resolve, 0));
  assert.equal(hooks.activeLeaseIds.has("reload-drain-lease"), true);
  assert.throws(
    () => hooks.validateReloadControl(
      fixtureReloadControl({
        requestId: "companion-reload-active-lease",
        targetBuildSha256: "a".repeat(64),
      }),
      [],
    ),
    (error) => error.tripchordControlCode === "active_leases_not_drained",
  );
  release.resolve();
  await execution;
  assert.equal(hooks.activeLeaseIds.size, 0);
  assert.throws(
    () => hooks.validateReloadControl(
      fixtureReloadControl({
        requestId: "companion-reload-mixed-lease",
        targetBuildSha256: "a".repeat(64),
      }),
      [{ task_id: "must-not-run" }],
    ),
    (error) => error.tripchordControlCode === "control_mixed_with_leases",
  );
}

{
  delete storage.tripchordPendingCompanionReload;
  delete storage.tripchordLastCompanionReloadReceipt;
  delete storage.tripchordBlockedCompanionReloadTarget;
  storage.tripchordConnected = true;
  storage.tripchordBridgeToken =
    "fixture-token-that-is-at-least-32-characters";
  const receiptStates = [];
  const tabActionCount = tabUpdates.length + createdTabRequests.length;
  const windowActionCount =
    windowUpdates.length + createdWindowRequests.length + removedWindows.length;
  context.fetch = async (url, options = {}) => {
    assert.ok(String(url).endsWith(hooks.CONTROL_RECEIPT_PATH));
    const receipt = JSON.parse(options.body);
    receiptStates.push(receipt.state);
    assert.equal(receipt.runtime_instance_id, hooks.RUNTIME_INSTANCE_ID);
    assert.equal(receipt.build_identity.build_sha256, hooks.BUILD_META.build_sha256);
    return {
      ok: true,
      status: 200,
      async json() {
        return { state: receipt.state };
      },
    };
  };
  const control = hooks.validateReloadControl(
    fixtureReloadControl({
      requestId: "companion-reload-accepted-order",
      targetBuildSha256: "b".repeat(64),
    }),
    [],
  );
  const reloadsBefore = runtimeReloadCount;
  await hooks.stageReloadControl(control);
  assert.deepEqual(receiptStates, ["accepted"]);
  assert.equal(runtimeReloadCount, reloadsBefore + 1);
  assert.equal(storage.tripchordPendingCompanionReload.state, "accepted");
  assert.equal(
    storage.tripchordPendingCompanionReload.reload_attempted,
    true,
  );
  assert.equal(clearedAlarms.includes("tripchord-read-only-poll"), true);
  assert.equal(tabUpdates.length + createdTabRequests.length, tabActionCount);
  assert.equal(
    windowUpdates.length + createdWindowRequests.length + removedWindows.length,
    windowActionCount,
  );
  await hooks.stageReloadControl(control);
  assert.equal(runtimeReloadCount, reloadsBefore + 1);
  delete storage.tripchordPendingCompanionReload;
  await hooks.reconcilePendingReload();
}

{
  delete storage.tripchordPendingCompanionReload;
  delete storage.tripchordLastCompanionReloadReceipt;
  delete storage.tripchordBlockedCompanionReloadTarget;
  const failedTarget = "c".repeat(64);
  let receiptAttempts = 0;
  context.fetch = async () => {
    receiptAttempts += 1;
    return {
      ok: false,
      status: 503,
      async json() {
        return {};
      },
    };
  };
  const reloadsBefore = runtimeReloadCount;
  const firstControl = hooks.validateReloadControl(
    fixtureReloadControl({
      requestId: "companion-reload-receipt-failure",
      targetBuildSha256: failedTarget,
    }),
    [],
  );
  await hooks.stageReloadControl(firstControl);
  await hooks.reconcilePendingReload();
  await hooks.reconcilePendingReload();
  assert.equal(receiptAttempts, 4);
  assert.equal(runtimeReloadCount, reloadsBefore);
  assert.equal(storage.tripchordPendingCompanionReload, undefined);
  assert.equal(
    storage.tripchordBlockedCompanionReloadTarget.target_build_sha256,
    failedTarget,
  );
  const secondControl = hooks.validateReloadControl(
    fixtureReloadControl({
      requestId: "companion-reload-same-failed-target",
      targetBuildSha256: failedTarget,
    }),
    [],
  );
  await hooks.stageReloadControl(secondControl);
  assert.equal(runtimeReloadCount, reloadsBefore);
  assert.equal(storage.tripchordPendingCompanionReload, undefined);
  assert.equal(storage.tripchordLastCompanionReloadReceipt.state, "failed");
  assert.equal(
    storage.tripchordLastCompanionReloadReceipt.failure_code,
    "reload_retry_cooldown",
  );
  assert.equal(
    storage.tripchordBlockedCompanionReloadTarget.recovery_generation,
    1,
  );
  delete storage.tripchordLastCompanionReloadReceipt;
}

{
  delete storage.tripchordPendingCompanionReload;
  delete storage.tripchordLastCompanionReloadReceipt;
  const retryTarget = "f".repeat(64);
  storage.tripchordBlockedCompanionReloadTarget = {
    request_id: "companion-reload-cooldown-previous",
    target_build_sha256: retryTarget,
    delivery_generation: 1,
    recovery_generation: 1,
    retry_not_before_ms: Date.now() - 1,
  };
  const receiptStates = [];
  context.fetch = async (_url, options = {}) => {
    const receipt = JSON.parse(options.body);
    receiptStates.push(receipt.state);
    return {
      ok: true,
      status: 200,
      async json() {
        return { state: receipt.state };
      },
    };
  };
  const reloadsBefore = runtimeReloadCount;
  const recoveryControl = hooks.validateReloadControl(
    fixtureReloadControl({
      requestId: "companion-reload-cooldown-recovery",
      targetBuildSha256: retryTarget,
    }),
    [],
  );
  await hooks.stageReloadControl(recoveryControl);
  assert.deepEqual(receiptStates, ["accepted"]);
  assert.equal(runtimeReloadCount, reloadsBefore + 1);
  assert.equal(storage.tripchordPendingCompanionReload.recovery_generation, 2);
  delete storage.tripchordPendingCompanionReload;
  delete storage.tripchordBlockedCompanionReloadTarget;
  await hooks.reconcilePendingReload();
}

{
  delete storage.tripchordPendingCompanionReload;
  delete storage.tripchordLastCompanionReloadReceipt;
  const exhaustedTarget = "e".repeat(64);
  storage.tripchordBlockedCompanionReloadTarget = {
    request_id: "companion-reload-exhausted-previous",
    target_build_sha256: exhaustedTarget,
    delivery_generation: 1,
    recovery_generation: 3,
    retry_not_before_ms: Date.now() - 1,
  };
  const receiptStates = [];
  context.fetch = async (_url, options = {}) => {
    const receipt = JSON.parse(options.body);
    receiptStates.push([receipt.state, receipt.failure_code]);
    return {
      ok: true,
      status: 200,
      async json() {
        return { state: receipt.state };
      },
    };
  };
  const reloadsBefore = runtimeReloadCount;
  const exhaustedControl = hooks.validateReloadControl(
    fixtureReloadControl({
      requestId: "companion-reload-exhausted-next",
      targetBuildSha256: exhaustedTarget,
    }),
    [],
  );
  await hooks.stageReloadControl(exhaustedControl);
  assert.deepEqual(receiptStates, [["failed", "reload_retry_exhausted"]]);
  assert.equal(runtimeReloadCount, reloadsBefore);
  assert.equal(storage.tripchordPendingCompanionReload, undefined);
  assert.equal(storage.tripchordLastCompanionReloadReceipt, undefined);
}

{
  delete storage.tripchordPendingCompanionReload;
  delete storage.tripchordLastCompanionReloadReceipt;
  delete storage.tripchordBlockedCompanionReloadTarget;
  const previousRuntimeInstanceId =
    "tripchord-runtime-00000000000000000000000000000000";
  storage.tripchordPendingCompanionReload = {
    schema_version: "tripchord-companion-reload-marker-v1",
    protocol_version: hooks.CONTROL_PROTOCOL_VERSION,
    request_id: "companion-reload-applied-bootstrap",
    action: "reload_extension",
    target_build_sha256: hooks.BUILD_META.build_sha256,
    expected_runtime_instance_id: previousRuntimeInstanceId,
    delivery_generation: 1,
    receipt_token: "receipt-token-that-is-at-least-32-characters",
    expires_at: new Date(Date.now() + 120000).toISOString(),
    expires_at_ms: Date.now() + 120000,
    previous_build_identity: {
      ...hooks.currentBuildIdentity(),
      build_sha256: "d".repeat(64),
    },
    state: "accepted",
    receipt_attempts: 1,
    recovery_generation: 1,
    reload_attempted: true,
    staged_at: new Date().toISOString(),
  };
  const receiptStates = [];
  let reconnectClaims = 0;
  storage.tripchordConnected = true;
  context.fetch = async (url, options = {}) => {
    let payload;
    if (String(url).endsWith(hooks.CONTROL_RECEIPT_PATH)) {
      const receipt = JSON.parse(options.body);
      receiptStates.push(receipt.state);
      assert.equal(receipt.previous_runtime_instance_id, previousRuntimeInstanceId);
      assert.notEqual(receipt.runtime_instance_id, previousRuntimeInstanceId);
      payload = { state: receipt.state };
    } else {
      assert.ok(String(url).endsWith("/v1/tasks/claim"));
      reconnectClaims += 1;
      storage.tripchordConnected = false;
      payload = { leases: [], control: null };
    }
    return {
      ok: true,
      status: 200,
      async json() {
        return payload;
      },
    };
  };
  await hooks.reconcilePendingReloadAndResume();
  await new Promise((resolve) => setTimeout(resolve, 0));
  assert.deepEqual(receiptStates, ["applied"]);
  assert.equal(reconnectClaims, 1);
  assert.equal(storage.tripchordPendingCompanionReload, undefined);
  assert.equal(storage.tripchordLastCompanionReloadReceipt, undefined);
  assert.equal((await hooks.companionRuntimeStatus()).control_state, "ready");
}

{
  let claimBody = null;
  storage.tripchordConnected = true;
  storage.tripchordBridgeToken =
    "fixture-token-that-is-at-least-32-characters";
  storage.tripchordLastCompanionReloadReceipt = {
    companion_id: "chrome-mv3-test-extension",
    request_id: "companion-reload-fallback-delivery",
    receipt_token: "receipt-token-that-is-at-least-32-characters",
    delivery_generation: 1,
    state: "applied",
    build_identity: hooks.currentBuildIdentity(),
    runtime_instance_id: hooks.RUNTIME_INSTANCE_ID,
    previous_runtime_instance_id:
      "tripchord-runtime-00000000000000000000000000000000",
    failure_code: null,
  };
  context.fetch = async (url, options = {}) => {
    assert.ok(String(url).endsWith("/v1/tasks/claim"));
    claimBody = JSON.parse(options.body);
    storage.tripchordConnected = false;
    return {
      ok: true,
      status: 200,
      async json() {
        return { leases: [], control: null };
      },
    };
  };
  await hooks.pollOnce();
  assert.equal(
    JSON.stringify(claimBody.build_identity),
    JSON.stringify(hooks.currentBuildIdentity()),
  );
  assert.equal(claimBody.runtime_instance_id, hooks.RUNTIME_INSTANCE_ID);
  assert.equal(claimBody.reload_receipt.state, "applied");
  assert.equal(storage.tripchordLastCompanionReloadReceipt, undefined);
}

{
  storage.tripchordConnected = true;
  storage.tripchordBridgeToken =
    "fixture-token-that-is-at-least-32-characters";
  storage.tripchordLastCompanionReloadReceipt = {
    companion_id: "chrome-mv3-test-extension",
    request_id: "companion-reload-orphaned-receipt",
    receipt_token: "receipt-token-that-is-at-least-32-characters",
    delivery_generation: 1,
    state: "applied",
    build_identity: hooks.currentBuildIdentity(),
    runtime_instance_id: hooks.RUNTIME_INSTANCE_ID,
    previous_runtime_instance_id:
      "tripchord-runtime-00000000000000000000000000000000",
    failure_code: null,
  };
  context.fetch = async () => {
    storage.tripchordConnected = false;
    return {
      ok: false,
      status: 404,
      async json() {
        return { detail: "reload request not found" };
      },
    };
  };
  await hooks.pollOnce();
  assert.equal(storage.tripchordLastCompanionReloadReceipt, undefined);
  assert.equal(
    storage.tripchordLastCompanionReloadDiagnostic.failure_code,
    "orphaned_reload_receipt_dropped",
  );
}

{
  storage.tripchordConnected = true;
  storage.tripchordBridgeToken =
    "fixture-token-that-is-at-least-32-characters";
  storage.tripchordLastCompanionReloadReceipt = {
    companion_id: "chrome-mv3-test-extension",
    request_id: "companion-reload-expired-receipt",
    receipt_token: "receipt-token-that-is-at-least-32-characters",
    delivery_generation: 1,
    state: "failed",
    build_identity: hooks.currentBuildIdentity(),
    runtime_instance_id: hooks.RUNTIME_INSTANCE_ID,
    previous_runtime_instance_id: null,
    failure_code: "reload_retry_cooldown",
  };
  context.fetch = async () => {
    storage.tripchordConnected = false;
    return {
      ok: false,
      status: 409,
      async json() {
        return { detail: "reload request has expired" };
      },
    };
  };
  await hooks.pollOnce();
  assert.equal(storage.tripchordLastCompanionReloadReceipt, undefined);
  assert.equal(
    storage.tripchordLastCompanionReloadDiagnostic.failure_code,
    "orphaned_reload_receipt_dropped",
  );
}

console.log("background lifecycle contract: background FIFO assertions passed");
