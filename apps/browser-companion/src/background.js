importScripts("build-meta.js");

const DEFAULT_BRIDGE_URL = "http://127.0.0.1:8000/browser-bridge";
const POLL_ALARM = "tripchord-read-only-poll";
const CONTENT_RUNTIME_VERSION = "2026-08-05.18";
const OFFSCREEN_DOCUMENT_PATH = "offscreen.html";
const COMPANION_ID = `chrome-mv3-${chrome.runtime.id}`;
const SOURCE_EXECUTION_ATTESTATION_SCHEMA =
  "tripchord-browser-source-execution-attestation-v1";
const PRODUCTION_VISIBLE_DOM_PARSER_VERSION = "tripchord-visible-dom-v3";
const CONTROL_RECEIPT_PATH = "/v1/companions/control/receipt";
const PENDING_RELOAD_STORAGE_KEY = "tripchordPendingCompanionReload";
const LAST_RELOAD_RECEIPT_STORAGE_KEY = "tripchordLastCompanionReloadReceipt";
const BLOCKED_RELOAD_TARGET_STORAGE_KEY = "tripchordBlockedCompanionReloadTarget";
const LAST_RELOAD_DIAGNOSTIC_STORAGE_KEY =
  "tripchordLastCompanionReloadDiagnostic";
const RELOAD_MARKER_SCHEMA_VERSION = "tripchord-companion-reload-marker-v1";
const MAX_RELOAD_RECEIPT_ATTEMPTS = 3;
const MAX_RELOAD_RECOVERY_GENERATIONS = 3;
const RELOAD_RECOVERY_COOLDOWN_MS = 30000;
const CONTROL_PROTOCOL_VERSION = "tripchord-companion-control-v1";
const BUILD_META = globalThis.TRIPCHORD_COMPANION_BUILD_META;

if (
  !BUILD_META ||
  BUILD_META.protocol_version !== CONTROL_PROTOCOL_VERSION ||
  BUILD_META.manifest_version !== chrome.runtime.getManifest().version ||
  BUILD_META.content_runtime_version !== CONTENT_RUNTIME_VERSION ||
  !/^[0-9a-f]{64}$/.test(String(BUILD_META.build_sha256 || ""))
) {
  throw new Error("TripChord companion build metadata is invalid or stale");
}

function createRuntimeInstanceId() {
  const bytes = new Uint8Array(16);
  crypto.getRandomValues(bytes);
  const suffix = [...bytes]
    .map((value) => value.toString(16).padStart(2, "0"))
    .join("");
  return `tripchord-runtime-${suffix}`;
}

const RUNTIME_INSTANCE_ID = createRuntimeInstanceId();

function isMicrosoftEdgeRuntime() {
  const brands = globalThis.navigator?.userAgentData?.brands || [];
  return (
    brands.some((brand) => /Microsoft Edge/i.test(String(brand?.brand || ""))) ||
    /\bEdg(?:A|iOS)?\//.test(String(globalThis.navigator?.userAgent || ""))
  );
}

function assertChromeExecutionRuntime() {
  if (isMicrosoftEdgeRuntime()) {
    throw new Error("TripChord 实时执行已停用 Edge，请改用 Google Chrome");
  }
}
const MAX_CONCURRENT_LEASES = 6;
// Qunar's overseas-lodging result endpoint self-throttles when two searches
// hydrate at once: both tabs can remain on the visible `实时搜索中` shell. Keep
// this provider/vertical serial while other providers still use the global
// six-lease pool.
const MAX_CONCURRENT_QUNAR_LODGING_LEASES = 1;
const ACTIVE_LEASE_HEARTBEAT_INTERVAL_MS = 10000;
const QUNAR_LODGING_ISOLATION_SCOPE =
  "companion_owned_unfocused_normal_window_active_tab";
const QUNAR_LODGING_WINDOW_CLEANUP_ATTEMPTS = 2;
const MAX_OUTBOUND_SELECTION_ATTEMPTS = 3;
const MAX_OUTBOUND_SELECTION_REVALIDATION_MISSES = 3;
const MAX_LODGING_DETAIL_PAGES_PER_LEASE = 2;
const MAX_QUNAR_LODGING_DETAIL_PAGES_PER_LEASE = 2;
const MAX_FLIGGY_LODGING_DETAIL_PAGES_PER_LEASE = 3;
const MAX_CONCURRENT_FLIGGY_LODGING_DETAILS = 3;
const MAX_CTRIP_LODGING_PREVIEW_CANDIDATES = 12;
const MAX_CTRIP_LODGING_CAPTURE_CONTROLS = 6;
const LODGING_INVENTORY_RECEIPT_SCHEMA_VERSION =
  "tripchord-lodging-inventory-receipt-v1";
const LODGING_INVENTORY_SEALED_RECEIPT_SCHEMA_VERSION =
  "tripchord-lodging-inventory-receipt-v2";
const LODGING_INVENTORY_RECEIPT_PARSER_VERSION =
  "tripchord-visible-dom-v3";
const SAFE_LODGING_RECEIPT_SEGMENTS = new Set([
  "full",
  "first",
  "middle",
  "last",
  "hulhumale-full",
]);
const SAFE_LODGING_RECEIPT_PACKAGE_AREAS = new Set([
  "airport_island",
  "destination_island",
]);
const CTRIP_LODGING_DETAIL_WORKFLOW_CAP_MS = 55000;
const CTRIP_LODGING_DETAIL_LOAD_CAP_MS = 15000;
const CTRIP_LODGING_DETAIL_EXTRACT_CAP_MS = 10000;
const LEASE_COMPLETION_REQUEST_CAP_MS = 4000;
const LEASE_COMPLETION_MIN_RESERVE_MS = 1000;
const LEASE_COMPLETION_MAX_RESERVE_MS = 20000;
const LEASE_COMPLETION_RESERVE_RATIO = 1 / 6;
const LEASE_COMPLETION_SHORT_LEASE_MAX_RATIO = 0.4;
const LEASE_COMPLETION_NETWORK_GUARD_MS = 500;
const INITIAL_LANDING_STAGE_CAP_MS = 40000;
const INITIAL_LANDING_INNER_GUARD_MS = 1000;
const MAX_CONCURRENT_INITIAL_LANDINGS = 3;
const READY_STATE_PROBE_CAP_MS = 1000;
const CONTENT_BOOTSTRAP_STAGE_CAP_MS = 25000;
const PREPARE_SEARCH_STAGE_CAP_MS = 35000;
const TRIGGER_SEARCH_STAGE_CAP_MS = 30000;
const SEARCH_RESULT_BOOTSTRAP_STAGE_CAP_MS = 8000;
// Qunar can keep its visible `实时搜索中` shell for more than 13 seconds even
// after the tab reports complete. Round 9 (Done-Gate 方案 B) extends this
// bounded stage from 45s to 90s so a realtime search can settle into a quote
// or a conclusive empty/pending state instead of giving up at ~28s. 90s stays
// comfortably inside the 120s observed-duration contract ceiling and the
// 120-second lease (the completion reserve leaves a ~98s work deadline, and
// extraction starts ~1.5s after claim).
const LODGING_EXTRACTION_STAGE_CAP_MS = 90000;
const LODGING_DOM_DRIFT_POLL_INTERVAL_MS = 2000;
const LODGING_EXTRACTION_RETRY_MIN_BUDGET_MS = 15000;
const FLIGHT_EXTRACTION_BASE_CAP_MS = 30000;
const FLIGHT_OUTBOUND_ATTEMPT_INCREMENT_MS = 20000;
const FLIGHT_EXTRACTION_STAGE_CAP_MS =
  FLIGHT_EXTRACTION_BASE_CAP_MS +
  MAX_OUTBOUND_SELECTION_ATTEMPTS * FLIGHT_OUTBOUND_ATTEMPT_INCREMENT_MS;
const TRANSFER_ENRICHMENT_STAGE_CAP_MS = 20000;
const TAB_INTERACTIVE_POLL_INTERVAL_MS = 250;
const MAX_STAGE_TRACE_ENTRIES = 16;
const BRIDGE_FAILURE_CODES = new Set([
  "captcha_required",
  "login_required",
  "dom_drift",
  "navigation_error",
  "timeout",
  "permission_denied",
  "unsupported_query",
  "extraction_error",
  "no_inventory",
]);
const SEARCH_TRANSITION_GRACE_MS = 3000;
const NAVIGATION_OBSERVER_SLACK_MS = 1000;
const MAX_NAVIGATION_TRACE_ENTRIES = 12;
const VISIBLE_CONTENT_SETTLE_MS = 400;
// Provider search shells can remain in a visible loading state well after the
// tab is interactive.  A diagnostic from that state is not a terminal
// observation; keep polling within the existing bounded extraction lease.
const FLIGHT_LOADING_DOM_DRIFT_MAX_POLLS = 60;
const FLIGHT_STAGED_DOM_DRIFT_MAX_POLLS = 4;
// Ctrip's return-flight list is hydrated after the exact outbound transition;
// live pages have taken more than two seconds even after the tab reports ready.
// Round-10 live evidence: the outbound list can still be empty at 6s of warmup
// (12 x 500ms polls) and only later hydrates its price cards.  Widen the warmup
// window to ~30s so a slow-hydrating outbound list has a real chance to render
// before the extraction falls back to the bounded no-exact-quote receipt.  The
// polling loop is still deadline-bounded (FLIGHT_DOM_DRIFT_POLL_INTERVAL_MS),
// so the extraction never overruns the frozen lease contract.
const CTRIP_OUTBOUND_STAGE_WARMUP_MAX_POLLS = 60;
// Tongcheng reports the document as complete before its international-flight
// result XHR has hydrated any price cards.  Keep this bounded and only retry
// the recognizable result shell; an empty or unrelated page remains terminal.
const TONGCHENG_FLIGHT_RESULT_WARMUP_MAX_POLLS = 16;
const QUNAR_GEOMETRY_STABILITY_MAX_POLLS = 2;
const QUNAR_GEOMETRY_STABILITY_POLL_INTERVAL_MS = 300;
const QUNAR_RESULT_QUERY_READBACK_STAGE_CAP_MS = 8000;
const QUNAR_RESULT_QUERY_READBACK_POLL_MS = 400;
const QUNAR_EXPLICIT_EMPTY_STABILITY_MIN_INTERVAL_MS = 2000;
const QUNAR_EXPLICIT_EMPTY_OBSERVATION_CHAIN_VERSION =
  "tripchord-qunar-empty-observation-chain-v1";
const QUNAR_OBSERVATION_LINEAGE_VERSION =
  "tripchord-browser-lineage-hash-v1";
const QUNAR_DETAIL_FALLBACK_SUMMARY_VERSION =
  "tripchord-qunar-detail-fallback-summary-v2";
const QUNAR_DETAIL_SEED_SELECTION_POLICY =
  "query-fingerprint-rotation-v1";
const QUNAR_EXPLICIT_EMPTY_CONTRACT_VERSION =
  "qunar-visible-zero-inventory-v1";
const QUNAR_EXPLICIT_EMPTY_RESULT_COUNT_TEXT =
  "共 0 家酒店满足条件";
const QUNAR_EXPLICIT_EMPTY_MESSAGE =
  "很抱歉，没有找到相关的酒店";
const QUNAR_PENDING_CONTRACT_VERSION =
  "qunar-visible-search-pending-v1";
const QUNAR_PENDING_RESULT_COUNT_TEXT =
  "共 家酒店满足条件";
const QUNAR_PENDING_MESSAGE =
  "请稍等,您查询的结果正在实时搜索中...";
const QUNAR_PENDING_MIN_OBSERVED_MS = 25000;
const QUNAR_PENDING_MAX_OBSERVED_MS = 120000;
// A lodging extraction must retain enough budget to reach a terminal receipt:
// >= QUNAR_PENDING_MIN_OBSERVED_MS of pending observation, one bounded
// re-read pass (LODGING_EXTRACTION_RETRY_MIN_BUDGET_MS), and a small margin.
// If less remains, the lease fails fast with a retryable timeout and the
// established result tab is preserved for reuse instead of being squandered
// into a native lease timeout with no receipt.
const LODGING_EXTRACTION_MIN_BUDGET_MS =
  QUNAR_PENDING_MIN_OBSERVED_MS + LODGING_EXTRACTION_RETRY_MIN_BUDGET_MS + 5000;
// A preserved result tab is claimed by the API retry within a few poll cycles.
// Close it if it is never claimed so a Qunar isolation window cannot leak.
const PRESERVED_EXACT_RESULT_TAB_MAX_AGE_MS = 120000;
const FLIGHT_DOM_DRIFT_POLL_INTERVAL_MS = 500;
const VISIBLE_CONTENT_MESSAGE_TYPES = new Set([
  "tripchord:prepare-search",
  "tripchord:trigger-search",
  "tripchord:read-result-query",
  "tripchord:extract",
  "tripchord:safe-select-outbound",
  "tripchord:safe-select-return",
  "tripchord:safe-expand-qunar-flight-detail",
  "tripchord:extract-transfer-detail",
  "tripchord:tongcheng-detail-candidates",
]);
const INTERNAL_SKIP_PROVIDER_MODE_SWITCH =
  "__tripchord_skip_provider_mode_switch";
const PROVIDER_ORIGINS = {
  ctrip: "https://*.ctrip.com/*",
  fliggy: [
    "https://*.fliggy.com/*",
    "https://*.fliggy.hk/*",
  ],
  qunar: "https://*.qunar.com/*",
  tongcheng: [
    "https://*.ly.com/*",
    "https://*.elong.com/*",
  ],
  zhixing: [
    "https://*.suanya.com/*",
    "https://*.suanya.cn/*",
  ],
};
const LANDING_URLS = {
  ctrip: {
    flight: "https://flights.ctrip.com/online/channel/domestic",
    lodging: "https://hotels.ctrip.com/",
  },
  fliggy: {
    flight: "https://www.fliggy.com/?tab=flight",
    lodging: "https://www.fliggy.com/?tab=hotel",
  },
  qunar: {
    flight: "https://flight.qunar.com/",
    lodging: "https://hotel.qunar.com/global/",
  },
  tongcheng: {
    flight: "https://www.ly.com/eliflight/",
    lodging: "https://www.ly.com/international",
  },
};
const CTRIP_LODGING_PLACE_ALIASES = Object.freeze({
  hulhumale: Object.freeze([
    "胡鲁马累",
    "hulhumale",
    "hulhumalé",
  ]),
  maafushi: Object.freeze([
    "马富施",
    "马富士",
    "maafushi",
  ]),
});
const EXACT_LODGING_CONFIRMATION_SCOPES = new Set([
  "confirmed_visible_search",
]);

function comparableCtripLodgingPlace(value) {
  return String(value || "")
    .normalize("NFKD")
    .replace(/\p{M}/gu, "")
    .toLowerCase()
    .replace(/[·•\-_/（）()，,。.]/g, "")
    .replace(/\s+/g, "")
    .replace(/(?:岛|island)$/i, "");
}

function canonicalCtripLodgingPlaceKey(value) {
  const comparable = comparableCtripLodgingPlace(value);
  if (!comparable) {
    return null;
  }
  for (const [placeKey, aliases] of Object.entries(
    CTRIP_LODGING_PLACE_ALIASES,
  )) {
    if (
      comparable === comparableCtripLodgingPlace(placeKey) ||
      aliases.some(
        (alias) => comparable === comparableCtripLodgingPlace(alias),
      )
    ) {
      return placeKey;
    }
  }
  return null;
}

let polling = false;
let offscreenCreationPromise = null;
let followupTimer = null;
let reloadPreparing = false;
let reloadReconciliationPromise = null;
let bootstrapPromise = null;
let controlLifecycleState = "ready";
let visibleInteractionTail = Promise.resolve();
let activeInitialLandings = 0;
const initialLandingQueue = [];
const leasedExistingTabIds = new Set();
const activeLeaseIds = new Set();
// Result tabs preserved across a retryable lease timeout so the API retry can
// reuse the established search-result page with a fresh full-budget extraction.
// tabId -> { window_id, provider, kind, preserved_at_ms, isolation_window }
const preservedExactResultTabs = new Map();

function navigationPathShape(pathname) {
  const segments = String(pathname || "")
    .split("/")
    .filter(Boolean)
    .slice(0, 12)
    .map((rawSegment) => {
      let segment = rawSegment;
      try {
        segment = decodeURIComponent(rawSegment);
      } catch {
        // Keep malformed percent-encoding opaque.
      }
      if (/^\d{5,}$/.test(segment)) {
        return ":id";
      }
      if (
        segment.length > 48 ||
        /^[A-Za-z0-9_-]{20,}$/.test(segment)
      ) {
        return ":opaque";
      }
      if (!/^[A-Za-z0-9._~-]+$/.test(segment)) {
        return ":text";
      }
      return segment;
    });
  return `/${segments.join("/")}`;
}

function navigationUrlEvidence(rawUrl) {
  try {
    const parsed = new URL(rawUrl);
    const allQueryKeys = [...parsed.searchParams.keys()];
    const queryKeys = [...new Set(
      allQueryKeys.map((key) => {
        const normalized = String(key).toLowerCase();
        return /^[a-z0-9_.-]{1,40}$/.test(normalized)
          ? normalized
          : ":other";
      }),
    )].sort();
    return {
      parseable: true,
      scheme: parsed.protocol.replace(/:$/, "").toLowerCase(),
      host: parsed.hostname.toLowerCase() || null,
      path_shape: navigationPathShape(parsed.pathname),
      query_keys: queryKeys.slice(0, 24),
      query_keys_truncated: queryKeys.length > 24,
      has_fragment: Boolean(parsed.hash),
    };
  } catch {
    return {
      parseable: false,
      scheme: null,
      host: null,
      path_shape: null,
      query_keys: [],
      query_keys_truncated: false,
      has_fragment: false,
    };
  }
}

function sanitizeInventoryDiagnosticText(value, maxLength = 180) {
  if (typeof value !== "string") {
    return null;
  }
  const sanitized = value
    .replace(/\s+/g, " ")
    .trim()
    .replace(/\[账户信息\]/g, "\uE000")
    .replace(/https?:\/\/[^\s]+/gi, "[网址]")
    .replace(
      /(?:账号|账户|用户名|会员号|account(?:\s*id)?|member\s*id|username)\s*[:：]?\s*[^\s，,;；]{1,64}/gi,
      "[账户信息]",
    )
    .replace(
      /\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b/gi,
      "[邮箱]",
    )
    .replace(/(?:\+?86[-\s]?)?1[3-9]\d{9}\b/g, "[手机号]")
    .replace(/\b\d{15,17}[0-9Xx]\b/g, "[证件号]")
    .replace(/\b(?:\d[-\s]?){8,}\d\b/g, "[长数字]")
    .replace(/\uE000/g, "[账户信息]")
    .slice(0, Math.max(0, Math.min(600, Number(maxLength) || 0)));
  return sanitized || null;
}

function canonicalInventoryJson(value) {
  if (value === null || typeof value !== "object") {
    return JSON.stringify(value);
  }
  if (Array.isArray(value)) {
    return `[${value.map((item) => canonicalInventoryJson(item)).join(",")}]`;
  }
  return `{${Object.keys(value)
    .sort()
    .filter((key) => value[key] !== undefined)
    .map(
      (key) =>
        `${JSON.stringify(key)}:${canonicalInventoryJson(value[key])}`,
    )
    .join(",")}}`;
}

async function inventoryReceiptSha256(value) {
  const bytes = new TextEncoder().encode(value);
  const digest = await crypto.subtle.digest("SHA-256", bytes);
  return [...new Uint8Array(digest)]
    .map((byte) => byte.toString(16).padStart(2, "0"))
    .join("");
}

function comparableLodgingPlace(value) {
  return String(value || "")
    .toLowerCase()
    .replace(/[·•\-_/（）()，,。.]/g, "")
    .replace(/\s+/g, "")
    .replace(/(?:岛|island)$/i, "");
}

function exactLodgingQueryConfirmed(query, driver) {
  const confirmed =
    driver &&
    driver.confirmed_query &&
    typeof driver.confirmed_query === "object" &&
    !Array.isArray(driver.confirmed_query)
      ? driver.confirmed_query
      : null;
  const destination = comparableLodgingPlace(
    query && query.destination,
  );
  const confirmedDestination = comparableLodgingPlace(
    confirmed && confirmed.destination,
  );
  const startTimestamp = calendarDateQueryValue(
    query && query.start_date,
  );
  const endTimestamp = calendarDateQueryValue(
    query && query.end_date,
  );
  const destinationMatches =
    destination &&
    confirmedDestination &&
    (
      destination === confirmedDestination ||
      (
        Math.min(destination.length, confirmedDestination.length) >= 4 &&
        (
          destination.includes(confirmedDestination) ||
          confirmedDestination.includes(destination)
        )
      )
    );
  return Boolean(
    confirmed &&
    driver.triggered === true &&
    (
      driver.provider !== "qunar" ||
      driver.result_query_readback_confirmed === true
    ) &&
    EXACT_LODGING_CONFIRMATION_SCOPES.has(
      String(driver.confirmation_scope || "").trim(),
    ) &&
    destinationMatches &&
    startTimestamp !== null &&
    endTimestamp !== null &&
    endTimestamp > startTimestamp &&
    query.start_date === confirmed.start_date &&
    query.end_date === confirmed.end_date &&
    Number.isInteger(query.adults) &&
    query.adults > 0 &&
    query.adults === confirmed.adults &&
    Number.isInteger(query.rooms) &&
    query.rooms > 0 &&
    query.rooms === confirmed.rooms
  );
}

function calendarDateQueryValue(rawValue) {
  const match = /^(\d{4})[-/](\d{2})[-/](\d{2})$/.exec(
    String(rawValue || ""),
  );
  if (!match) {
    return null;
  }
  const [, rawYear, rawMonth, rawDay] = match;
  const year = Number(rawYear);
  const month = Number(rawMonth);
  const day = Number(rawDay);
  const timestamp = Date.UTC(year, month - 1, day);
  const parsed = new Date(timestamp);
  return (
    parsed.getUTCFullYear() === year &&
    parsed.getUTCMonth() === month - 1 &&
    parsed.getUTCDate() === day
  )
    ? timestamp
    : null;
}

function isLodgingSearchCheckoutDate(provider, parsed, queryKey) {
  const path = parsed.pathname.toLowerCase().replace(/\/+$/, "");
  if (queryKey !== "checkout") {
    return false;
  }
  const host = parsed.hostname.toLowerCase();
  const isCtripSearch =
    provider === "ctrip" &&
    host === "hotels.ctrip.com" &&
    (path === "/hotels/list" || path === "/hotels/detail");
  const isFliggySearch =
    provider === "fliggy" &&
    host === "hotel.fliggy.com" &&
    (path === "/hotel_list3.htm" || path === "/hotel_detail2.htm");
  if (!isCtripSearch && !isFliggySearch) {
    return false;
  }
  const query = new Map(
    [...parsed.searchParams.entries()].map(([key, value]) => [
      key.toLowerCase(),
      value,
    ]),
  );
  const checkin = calendarDateQueryValue(query.get("checkin"));
  const checkout = calendarDateQueryValue(query.get("checkout"));
  return checkin !== null && checkout !== null && checkout > checkin;
}

function providerHostDecision(provider, rawUrl) {
  const url = navigationUrlEvidence(rawUrl);
  let parsed;
  try {
    parsed = new URL(rawUrl);
  } catch {
    return { allowed: false, reason: "invalid_url", url };
  }
  const suffixes = {
    ctrip: ["ctrip.com"],
    fliggy: ["fliggy.com", "fliggy.hk"],
    qunar: ["qunar.com"],
    tongcheng: ["ly.com", "elong.com"],
    zhixing: ["suanya.com", "suanya.cn"],
  }[provider];
  if (!suffixes) {
    return { allowed: false, reason: "unsupported_provider", url };
  }
  if (parsed.protocol !== "https:") {
    return { allowed: false, reason: "non_https", url };
  }
  if (parsed.username || parsed.password) {
    return { allowed: false, reason: "embedded_credentials", url };
  }
  if (!suffixes.some(
    (suffix) =>
      parsed.hostname === suffix ||
      parsed.hostname.endsWith(`.${suffix}`),
  )) {
    return { allowed: false, reason: "outside_provider_host", url };
  }
  const forbidden = new Set(["cashier", "checkout", "coupon", "order", "payment"]);
  const pathSegments = parsed.pathname
    .toLowerCase()
    .replaceAll("-", "/")
    .replaceAll("_", "/")
    .split("/")
    .filter(Boolean);
  const queryKeys = [...parsed.searchParams.keys()].map((key) => key.toLowerCase());
  if (
    pathSegments.some((segment) =>
      [...forbidden].some(
        (marker) =>
          segment.includes(marker) &&
          !(
            provider === "tongcheng" &&
            parsed.hostname.toLowerCase() === "www.ly.com" &&
            parsed.pathname.toLowerCase() === "/eliflight/book1.html" &&
            segment === "book1.html"
          ),
      )
    ) ||
    queryKeys.some(
      (key) =>
        forbidden.has(key) &&
        !isLodgingSearchCheckoutDate(provider, parsed, key),
    )
  ) {
    return { allowed: false, reason: "transaction_surface", url };
  }
  return { allowed: true, reason: "allowed", url };
}

function providerHostAllowed(provider, rawUrl) {
  return providerHostDecision(provider, rawUrl).allowed;
}

function providerVerticalDecision(provider, kind, rawUrl) {
  const hostDecision = providerHostDecision(provider, rawUrl);
  if (!hostDecision.allowed) {
    return hostDecision;
  }
  const parsed = new URL(rawUrl);
  const host = parsed.hostname.toLowerCase();
  let allowed = false;
  if (provider === "ctrip") {
    allowed = kind === "flight"
      ? host === "flights.ctrip.com"
      : host === "hotels.ctrip.com";
  } else if (provider === "qunar") {
    allowed = kind === "flight"
      ? host === "flight.qunar.com"
      : host === "hotel.qunar.com";
  } else if (provider === "tongcheng") {
    const path = parsed.pathname.toLowerCase();
    allowed = kind === "flight"
      ? host === "www.ly.com" && /^\/(?:e?liflight)(?:\/|$)/.test(path)
      : (
          (host === "www.ly.com" && /^\/international(?:\/|$)/.test(path)) ||
          (host === "www.ly.com" && /^\/hotel\/(?:hotellist|hoteldetail)(?:\/|$)/.test(path)) ||
          (host === "m.elong.com" && /^\/ihotel\/(?:hotellist|hoteldetail)(?:\/|$)/.test(path)) ||
          (host === "m.ly.com" && /^\/hotel\/(?:hotellist|hoteldetail)(?:\/|$)/.test(path)) ||
          /(^|\.)(?:hotel|ihotel)\.(?:ly|elong)\.com$/.test(host)
        );
  } else if (provider === "zhixing") {
    // The official PC site currently exposes flight and lodging only through
    // app/mini-program QR entry points. Keep the authorised domains navigable
    // for capability discovery, but fail closed until an auditable web search
    // surface exists for the requested vertical.
    allowed = false;
  } else if (
    provider === "fliggy" &&
    (host === "www.fliggy.com" || host === "www.fliggy.hk")
  ) {
    const surface = `${parsed.pathname} ${parsed.search}`.toLowerCase();
    const neutralOfficialHome =
      parsed.pathname === "/" &&
      parsed.search === "";
    allowed = neutralOfficialHome || (
      kind === "flight"
        ? /flight|jipiao/.test(surface)
        : /hotel|jiudian/.test(surface)
    );
  } else if (provider === "fliggy") {
    allowed = kind === "flight"
      ? /(^|\.)(flight|jipiao|sjipiao|sijipiao)\./.test(host)
      : /(^|\.)(hotel|hotels|jiudian)\./.test(host);
  }
  return {
    allowed,
    reason: allowed ? "allowed" : "wrong_vertical",
    url: hostDecision.url,
  };
}

function providerVerticalUrlAllowed(provider, kind, rawUrl) {
  return providerVerticalDecision(provider, kind, rawUrl).allowed;
}

const FLIGGY_LODGING_DESTINATIONS = Object.freeze({
  maafushi: Object.freeze({ city: "933081", cityName: "马富士" }),
  hulhumale: Object.freeze({ city: "934358", cityName: "哈尔胡梅尔" }),
});
const QUNAR_LODGING_DESTINATIONS = Object.freeze({
  maafushi: Object.freeze({ cityurl: "i-ka_maafushi", toCity: "马富施" }),
  hulhumale: Object.freeze({ cityurl: "i-hulhumale", toCity: "胡鲁马累" }),
});
const QUNAR_AUDITED_LODGING_DETAILS = Object.freeze({
  maafushi: Object.freeze({
    city_slug: "i-ka_maafushi",
    properties: Object.freeze([
      Object.freeze({
        hotel_seq: "i-ka_maafushi_2112",
        property_id: "2112",
        property_name: "Kaani Palm Beach",
      }),
      Object.freeze({
        hotel_seq: "i-ka_maafushi_2055",
        property_id: "2055",
        property_name: "Kaani Grand Seaview",
      }),
      Object.freeze({
        hotel_seq: "i-ka_maafushi_2071",
        property_id: "2071",
        property_name: "Maafushi View",
      }),
      Object.freeze({
        hotel_seq: "i-ka_maafushi_2072",
        property_id: "2072",
        property_name: "Maafushi Village",
      }),
      Object.freeze({
        hotel_seq: "i-ka_maafushi_2075",
        property_id: "2075",
        property_name: "Maafushi Veli",
      }),
      Object.freeze({
        hotel_seq: "i-ka_maafushi_2142",
        property_id: "2142",
        property_name: "SEASUNBEACH",
      }),
    ]),
  }),
});
const TONGCHENG_LODGING_DESTINATIONS = Object.freeze({
  maafushi: Object.freeze({ city: "110018575", cityName: "马富施" }),
  hulhumale: Object.freeze({ city: "110018578", cityName: "胡鲁马累" }),
});
const CTRIP_AUDITED_LODGING_SEEDS = Object.freeze({
  maafushi: Object.freeze({
    cityId: "35851",
    cityEnName: "Maafushi",
    properties: Object.freeze([
      Object.freeze({ hotelId: "47330536", label: "Kaani Palm Beach" }),
      Object.freeze({ hotelId: "131576087", label: "KUE Hotel Maafushi" }),
    ]),
  }),
  hulhumale: Object.freeze({
    cityId: "705784",
    cityEnName: "Hulhumale",
    properties: Object.freeze([
      Object.freeze({ hotelId: "29935473", label: "Huvan Beach Hotel at Hulhumale" }),
      Object.freeze({ hotelId: "1948695", label: "Hotel Ocean Grand at Hulhumale" }),
    ]),
  }),
});

function fliggyLodgingResultUrlDecision(rawUrl, query = {}) {
  const rejected = (reason, parsed = null) => ({
    allowed: false,
    reason,
    url: navigationUrlEvidence(parsed ? parsed.href : rawUrl),
  });
  let parsed;
  try {
    parsed = new URL(rawUrl);
  } catch {
    return rejected("invalid_url");
  }
  if (
    parsed.protocol !== "https:" ||
    parsed.hostname.toLowerCase() !== "hotel.fliggy.com" ||
    parsed.port ||
    parsed.username ||
    parsed.password ||
    parsed.pathname !== "/hotel_list3.htm" ||
    parsed.hash
  ) {
    return rejected("wrong_surface", parsed);
  }
  const expectedPlaceKey = String(
    query &&
    query.options &&
    query.options.expected_lodging_place_key ||
    "",
  ).trim().toLowerCase();
  const expectedDestination =
    FLIGGY_LODGING_DESTINATIONS[expectedPlaceKey];
  const adults = Number(query && query.adults);
  if (
    !expectedDestination ||
    calendarDateQueryValue(query && query.start_date) === null ||
    calendarDateQueryValue(query && query.end_date) === null ||
    String(query.end_date) <= String(query.start_date) ||
    !Number.isInteger(adults) ||
    adults < 1 ||
    adults > 9 ||
    Number(query && query.rooms) !== 1
  ) {
    return rejected("invalid_requested_query", parsed);
  }
  const entries = [...parsed.searchParams.entries()];
  const requiredKeys = new Set([
    "spm",
    "city",
    "cityName",
    "checkIn",
    "checkOut",
    "keywords",
    "aNum_1",
    "cNum_1",
  ]);
  const allowedKeys = new Set([...requiredKeys, "_output_charset"]);
  if (
    entries.length < requiredKeys.size ||
    entries.length > allowedKeys.size ||
    entries.some(([key]) => !allowedKeys.has(key)) ||
    [...requiredKeys].some(
      (key) => entries.filter(([candidate]) => candidate === key).length !== 1,
    ) ||
    entries.filter(([candidate]) => candidate === "_output_charset").length > 1 ||
    (
      parsed.searchParams.has("_output_charset") &&
      parsed.searchParams.get("_output_charset") !== "utf8"
    )
  ) {
    return rejected("query_shape_mismatch", parsed);
  }
  const expected = new Map([
    ["spm", "181.11358650.hotelModule.internationalSearch"],
    ["city", expectedDestination.city],
    ["cityName", expectedDestination.cityName],
    ["checkIn", String(query.start_date)],
    ["checkOut", String(query.end_date)],
    ["keywords", ""],
    ["aNum_1", String(adults)],
    ["cNum_1", "0"],
  ]);
  if (
    [...expected].some(
      ([key, value]) => parsed.searchParams.get(key) !== value,
    )
  ) {
    return rejected("query_value_mismatch", parsed);
  }
  return {
    allowed: true,
    reason: "allowed",
    href: parsed.href,
    url: navigationUrlEvidence(parsed.href),
  };
}

function qunarLodgingResultUrlDecision(rawUrl, query = {}) {
  const rejected = (reason, parsed = null) => ({
    allowed: false,
    reason,
    url: navigationUrlEvidence(parsed ? parsed.href : rawUrl),
  });
  let parsed;
  try {
    parsed = new URL(rawUrl);
  } catch {
    return rejected("invalid_url");
  }
  if (
    parsed.protocol !== "https:" ||
    parsed.hostname.toLowerCase() !== "hotel.qunar.com" ||
    parsed.port ||
    parsed.username ||
    parsed.password ||
    parsed.pathname !== "/intl/search.jsp" ||
    parsed.hash
  ) {
    return rejected("wrong_surface", parsed);
  }
  const expectedPlaceKey = String(
    query &&
    query.options &&
    query.options.expected_lodging_place_key ||
    "",
  ).trim().toLowerCase();
  const expectedDestination =
    QUNAR_LODGING_DESTINATIONS[expectedPlaceKey];
  const adults = Number(query && query.adults);
  if (
    !expectedDestination ||
    calendarDateQueryValue(query && query.start_date) === null ||
    calendarDateQueryValue(query && query.end_date) === null ||
    String(query.end_date) <= String(query.start_date) ||
    !Number.isInteger(adults) ||
    adults < 1 ||
    adults > 9 ||
    Number(query && query.rooms) !== 1
  ) {
    return rejected("invalid_requested_query", parsed);
  }
  const entries = [...parsed.searchParams.entries()];
  const allowedKeys = new Set([
    "toCity",
    "fromDate",
    "toDate",
    "cityurl",
    "from",
  ]);
  if (
    entries.length !== allowedKeys.size ||
    entries.some(([key]) => !allowedKeys.has(key)) ||
    [...allowedKeys].some(
      (key) => entries.filter(([candidate]) => candidate === key).length !== 1,
    )
  ) {
    return rejected("query_shape_mismatch", parsed);
  }
  const expected = new Map([
    ["toCity", expectedDestination.toCity],
    ["fromDate", String(query.start_date)],
    ["toDate", String(query.end_date)],
    ["cityurl", expectedDestination.cityurl],
    ["from", "globalhotelpages"],
  ]);
  if (
    [...expected].some(
      ([key, value]) => parsed.searchParams.get(key) !== value,
    )
  ) {
    return rejected("query_value_mismatch", parsed);
  }
  return {
    allowed: true,
    reason: "allowed",
    href: parsed.href,
    url: navigationUrlEvidence(parsed.href),
  };
}

function qunarAuditedLodgingDetailTargets(query = {}) {
  const placeKey = String(
    query && query.options && query.options.expected_lodging_place_key || "",
  ).trim().toLowerCase();
  const contract = QUNAR_AUDITED_LODGING_DETAILS[placeKey];
  if (
    !contract ||
    calendarDateQueryValue(query.start_date) === null ||
    calendarDateQueryValue(query.end_date) === null ||
    String(query.end_date) <= String(query.start_date) ||
    Number(query.adults) !== 2 ||
    Number(query.rooms) !== 1
  ) {
    return [];
  }
  const seedOffset = qunarLodgingDetailSeedOffset(
    query,
    contract.properties.length,
  );
  const rotatedProperties = contract.properties.map(
    (_property, index) =>
      contract.properties[(seedOffset + index) % contract.properties.length],
  );
  return rotatedProperties
    .slice(0, MAX_QUNAR_LODGING_DETAIL_PAGES_PER_LEASE)
    .map((property) => {
      const hash = [
        `fromDate=${encodeURIComponent(String(query.start_date))}`,
        `toDate=${encodeURIComponent(String(query.end_date))}`,
        "q=",
        "showMap=0",
      ].join("&");
      const href =
        `https://hotel.qunar.com/city/${contract.city_slug}/` +
        `dt-${property.property_id}/?#${hash}`;
      return Object.freeze({
        ...property,
        city_slug: contract.city_slug,
        seed_selection_policy: QUNAR_DETAIL_SEED_SELECTION_POLICY,
        seed_selection_offset: seedOffset,
        href,
        url: navigationUrlEvidence(href),
      });
    });
}

function qunarLodgingDetailSeedOffset(query = {}, propertyCount = 0) {
  if (!Number.isInteger(propertyCount) || propertyCount < 1) {
    return 0;
  }
  const options = query && query.options &&
    typeof query.options === "object" && !Array.isArray(query.options)
    ? query.options
    : {};
  const fingerprint = canonicalInventoryJson({
    adults: Number(query && query.adults),
    destination: String(query && query.destination || "").trim().toLowerCase(),
    end_date: String(query && query.end_date || ""),
    expected_lodging_place_key: String(
      options.expected_lodging_place_key || "",
    ).trim().toLowerCase(),
    expected_package_area: String(
      options.expected_package_area || "",
    ).trim().toLowerCase(),
    rooms: Number(query && query.rooms),
    segment: String(options.segment || "").trim().toLowerCase(),
    start_date: String(query && query.start_date || ""),
  });
  // FNV-1a is used only to distribute an already audited seed set. It is
  // synchronous and stable across service-worker restarts; it is not a trust
  // or evidence hash and cannot expand the provider/property allowlist.
  let hash = 0x811c9dc5;
  for (let index = 0; index < fingerprint.length; index += 1) {
    hash ^= fingerprint.charCodeAt(index);
    hash = Math.imul(hash, 0x01000193) >>> 0;
  }
  return hash % propertyCount;
}

function qunarLodgingDetailUrlDecision(
  rawUrl,
  query = {},
  expectedTarget = null,
) {
  const rejected = (reason, parsed = null) => ({
    allowed: false,
    reason,
    url: navigationUrlEvidence(parsed ? parsed.href : rawUrl),
  });
  let parsed;
  try {
    parsed = new URL(rawUrl);
  } catch {
    return rejected("invalid_url");
  }
  const placeKey = String(
    query && query.options && query.options.expected_lodging_place_key || "",
  ).trim().toLowerCase();
  const contract = QUNAR_AUDITED_LODGING_DETAILS[placeKey];
  if (
    !contract ||
    parsed.protocol !== "https:" ||
    parsed.hostname.toLowerCase() !== "hotel.qunar.com" ||
    parsed.port ||
    parsed.username ||
    parsed.password ||
    parsed.search !== "" ||
    !String(rawUrl).includes("/?#") ||
    calendarDateQueryValue(query.start_date) === null ||
    calendarDateQueryValue(query.end_date) === null ||
    String(query.end_date) <= String(query.start_date) ||
    Number(query.adults) !== 2 ||
    Number(query.rooms) !== 1
  ) {
    return rejected("wrong_surface_or_query", parsed);
  }
  const pathMatch = new RegExp(
    `^/city/${contract.city_slug}/dt-([1-9]\\d*)/$`,
  ).exec(parsed.pathname);
  if (!pathMatch) {
    return rejected("detail_path_mismatch", parsed);
  }
  const property = contract.properties.find(
    (candidate) => candidate.property_id === pathMatch[1],
  );
  if (!property) {
    return rejected("property_not_allowlisted", parsed);
  }
  const hashEntries = [...new URLSearchParams(parsed.hash.slice(1)).entries()];
  const requiredHashKeys = new Set([
    "fromDate",
    "toDate",
    "q",
    "showMap",
  ]);
  if (
    hashEntries.length !== requiredHashKeys.size ||
    hashEntries.some(([key]) => !requiredHashKeys.has(key)) ||
    [...requiredHashKeys].some(
      (key) => hashEntries.filter(([candidate]) => candidate === key).length !== 1,
    )
  ) {
    return rejected("detail_hash_shape_mismatch", parsed);
  }
  const hash = new URLSearchParams(parsed.hash.slice(1));
  if (
    hash.get("fromDate") !== String(query.start_date) ||
    hash.get("toDate") !== String(query.end_date) ||
    hash.get("q") !== "" ||
    hash.get("showMap") !== "0"
  ) {
    return rejected("detail_hash_value_mismatch", parsed);
  }
  if (
    expectedTarget &&
    (
      expectedTarget.city_slug !== contract.city_slug ||
      expectedTarget.hotel_seq !== property.hotel_seq ||
      expectedTarget.property_id !== property.property_id ||
      expectedTarget.property_name !== property.property_name
    )
  ) {
    return rejected("detail_target_mismatch", parsed);
  }
  return {
    allowed: true,
    reason: "allowed",
    href: parsed.href,
    url: navigationUrlEvidence(parsed.href),
    city_slug: contract.city_slug,
    hotel_seq: property.hotel_seq,
    property_id: property.property_id,
    property_name: property.property_name,
  };
}

function qunarLodgingResultQueryReadbackDecision(
  rawUrl,
  query = {},
  result = null,
) {
  const diagnosticReadback =
    result && result.readback_query &&
    typeof result.readback_query === "object" &&
    !Array.isArray(result.readback_query)
      ? {
          destination: sanitizeInventoryDiagnosticText(
            result.readback_query.destination,
            80,
          ),
          start_date: sanitizeInventoryDiagnosticText(
            result.readback_query.start_date,
            32,
          ),
          end_date: sanitizeInventoryDiagnosticText(
            result.readback_query.end_date,
            32,
          ),
          adults: Number.isInteger(result.readback_query.adults)
            ? result.readback_query.adults
            : null,
          rooms: Number.isInteger(result.readback_query.rooms)
            ? result.readback_query.rooms
            : null,
        }
      : null;
  const rejected = (reason) => ({
    allowed: false,
    reason,
    diagnostic: {
      reason,
      page_url: navigationUrlEvidence(rawUrl),
      readback_query: diagnosticReadback,
      gates:
        result && result.gates &&
        typeof result.gates === "object" &&
        !Array.isArray(result.gates)
          ? Object.fromEntries(
              Object.entries(result.gates)
                .slice(0, 16)
                .map(([key, value]) => [key, value === true]),
            )
          : null,
      controls:
        result && result.diagnostics &&
        Array.isArray(result.diagnostics.visible_controls)
          ? result.diagnostics.visible_controls
              .slice(0, 12)
              .map((control) => ({
                tag: sanitizeInventoryDiagnosticText(control && control.tag, 20),
                id: sanitizeInventoryDiagnosticText(control && control.id, 80),
                class: sanitizeInventoryDiagnosticText(control && control.class, 120),
                name: sanitizeInventoryDiagnosticText(control && control.name, 80),
                type: sanitizeInventoryDiagnosticText(control && control.type, 40),
                placeholder: sanitizeInventoryDiagnosticText(
                  control && control.placeholder,
                  100,
                ),
                aria_label: sanitizeInventoryDiagnosticText(
                  control && control.aria_label,
                  100,
                ),
                value_kind: [
                  "calendar_date",
                  "audited_destination",
                  "other_non_empty",
                  "empty",
                ].includes(control && control.value_kind)
                  ? control.value_kind
                  : "unknown",
              }))
          : [],
      occupancy_surfaces:
        result && result.diagnostics &&
        Array.isArray(result.diagnostics.visible_occupancy_surfaces)
          ? result.diagnostics.visible_occupancy_surfaces
              .slice(0, 8)
              .map((surface) => ({
                tag: sanitizeInventoryDiagnosticText(surface && surface.tag, 20),
                class: sanitizeInventoryDiagnosticText(
                  surface && surface.class,
                  120,
                ),
                text: sanitizeInventoryDiagnosticText(surface && surface.text, 120),
              }))
          : [],
    },
  });
  let parsed;
  try {
    parsed = new URL(rawUrl);
  } catch {
    return rejected("invalid_result_url");
  }
  const expectedPlaceKey = String(
    query && query.options && query.options.expected_lodging_place_key || "",
  ).trim().toLowerCase();
  const expectedDestination = QUNAR_LODGING_DESTINATIONS[expectedPlaceKey];
  const expectedPath = expectedDestination
    ? `/city/${expectedDestination.cityurl}`
    : null;
  if (
    !expectedDestination ||
    parsed.protocol !== "https:" ||
    parsed.hostname.toLowerCase() !== "hotel.qunar.com" ||
    parsed.port ||
    parsed.username ||
    parsed.password ||
    parsed.pathname.replace(/\/+$/, "") !== expectedPath
  ) {
    return rejected("result_surface_mismatch");
  }
  if (
    !result ||
    typeof result !== "object" ||
    Array.isArray(result) ||
    result.confirmed !== true ||
    !result.confirmed_query ||
    typeof result.confirmed_query !== "object" ||
    Array.isArray(result.confirmed_query) ||
    !result.readback_query ||
    typeof result.readback_query !== "object" ||
    Array.isArray(result.readback_query) ||
    !result.gates ||
    typeof result.gates !== "object" ||
    Array.isArray(result.gates)
  ) {
    return rejected(
      sanitizeInventoryDiagnosticText(
        result && result.reason,
        120,
      ) || "result_readback_unconfirmed",
    );
  }
  const requiredGates = [
    "path_confirmed",
    "search_form_visible",
    "destination_control_unambiguous",
    "destination_confirmed",
    "date_controls_unambiguous",
    "start_date_confirmed",
    "end_date_confirmed",
    "occupancy_control_unambiguous",
    "adults_confirmed",
    "children_confirmed",
    "single_room_surface_confirmed",
  ];
  if (requiredGates.some((gate) => result.gates[gate] !== true)) {
    return rejected("result_readback_gate_failed");
  }
  const confirmed = result.confirmed_query;
  const readback = result.readback_query;
  const evidence = result.evidence;
  const normalizedDestination = String(readback.destination || "")
    .normalize("NFKD")
    .replace(/\p{M}/gu, "")
    .trim()
    .toLowerCase();
  const destinationAliases = expectedPlaceKey === "maafushi"
    ? new Set(["马富施", "maafushi"])
    : new Set(["胡鲁马累", "胡鲁马累岛", "hulhumale", "hulhumalé"]);
  if (
    confirmed.destination !== query.destination ||
    confirmed.start_date !== query.start_date ||
    confirmed.end_date !== query.end_date ||
    confirmed.adults !== query.adults ||
    confirmed.rooms !== query.rooms ||
    !destinationAliases.has(normalizedDestination) ||
    readback.start_date !== query.start_date ||
    readback.end_date !== query.end_date ||
    readback.adults !== query.adults ||
    readback.rooms !== query.rooms ||
    !evidence ||
    typeof evidence !== "object" ||
    Array.isArray(evidence) ||
    evidence.provider_destination_id !== expectedDestination.cityurl ||
    evidence.result_path !== expectedPath ||
    evidence.room_scope !== "audited_qunar_single_room_search_surface"
  ) {
    return rejected("result_readback_value_mismatch");
  }
  return {
    allowed: true,
    reason: "allowed",
    confirmed_query: {
      destination: query.destination,
      start_date: query.start_date,
      end_date: query.end_date,
      adults: query.adults,
      rooms: query.rooms,
    },
    readback_query: {
      destination: readback.destination,
      start_date: readback.start_date,
      end_date: readback.end_date,
      adults: readback.adults,
      rooms: readback.rooms,
    },
    evidence: {
      provider_destination_id: expectedDestination.cityurl,
      result_path: expectedPath,
      destination_text: sanitizeInventoryDiagnosticText(
        evidence.destination_text,
        80,
      ),
      start_date_text: sanitizeInventoryDiagnosticText(
        evidence.start_date_text,
        32,
      ),
      end_date_text: sanitizeInventoryDiagnosticText(
        evidence.end_date_text,
        32,
      ),
      occupancy_text: sanitizeInventoryDiagnosticText(
        evidence.occupancy_text,
        120,
      ),
      room_scope: evidence.room_scope,
    },
  };
}

function qunarResultQueryReadbackIsTransient(result) {
  return Boolean(
    result &&
    result.confirmed !== true &&
    result.reason === "qunar_result_search_form_missing",
  );
}

async function readQunarResultQueryWithRetry(
  tabId,
  lease,
  deadline,
  ownedTabIds,
  contentCaller = visibleContentCall,
  wait = delay,
  clock = Date.now,
) {
  let result = null;
  do {
    result = await contentCaller(
      tabId,
      {
        type: "tripchord:read-result-query",
        provider: lease.provider,
        kind: lease.kind,
        query: lease.query,
      },
      {
        lease,
        deadline,
        ownedTabIds,
        timeoutCapMs: Math.min(
          2000,
          Math.max(1, deadline - clock()),
        ),
      },
    );
    if (!qunarResultQueryReadbackIsTransient(result)) {
      return result;
    }
    const remainingMs = deadline - clock();
    if (remainingMs <= QUNAR_RESULT_QUERY_READBACK_POLL_MS) {
      return result;
    }
    await wait(QUNAR_RESULT_QUERY_READBACK_POLL_MS);
  } while (clock() < deadline);
  return result;
}

function tongchengLodgingResultUrlDecision(rawUrl, query = {}) {
  const rejected = (reason, parsed = null) => ({
    allowed: false,
    reason,
    url: navigationUrlEvidence(parsed ? parsed.href : rawUrl),
  });
  let parsed;
  try {
    parsed = new URL(rawUrl);
  } catch {
    return rejected("invalid_url");
  }
  const host = parsed.hostname.toLowerCase();
  const path = parsed.pathname.toLowerCase().replace(/\/+$/, "");
  const primarySurface = host === "www.ly.com" && path === "/hotel/hotellist";
  const fallbackSurface =
    (host === "m.elong.com" && path === "/ihotel/hotellist") ||
    (host === "m.ly.com" && path === "/hotel/hotellist");
  if (
    parsed.protocol !== "https:" ||
    (!primarySurface && !fallbackSurface) ||
    parsed.port || parsed.username || parsed.password || parsed.hash
  ) {
    return rejected("wrong_surface", parsed);
  }
  const placeKey = String(
    query && query.options && query.options.expected_lodging_place_key || "",
  ).trim().toLowerCase();
  const destination = TONGCHENG_LODGING_DESTINATIONS[placeKey];
  const adults = Number(query && query.adults);
  if (
    !destination ||
    calendarDateQueryValue(query && query.start_date) === null ||
    calendarDateQueryValue(query && query.end_date) === null ||
    String(query.end_date) <= String(query.start_date) ||
    !Number.isInteger(adults) || adults < 1 || adults > 9 ||
    Number(query && query.rooms) !== 1
  ) {
    return rejected("invalid_requested_query", parsed);
  }
  const expected = new Map(primarySurface ? [
    ["city", destination.city],
    ["inDate", String(query.start_date)],
    ["outDate", String(query.end_date)],
    ["adultsNumber", String(adults)],
    ["roomNum", "1"],
    ["intl", "1"],
  ] : [
    ["city", destination.city],
    ["indate", String(query.start_date)],
    ["outdate", String(query.end_date)],
    ["adultsNumber", String(adults)],
    ["roomNum", "1"],
    ["intl", "1"],
  ]);
  const entries = [...parsed.searchParams.entries()];
  if (
    entries.length !== expected.size ||
    [...expected].some(
      ([key, value]) =>
        entries.filter(([candidate]) => candidate === key).length !== 1 ||
        parsed.searchParams.get(key) !== value,
    )
  ) {
    return rejected("query_value_mismatch", parsed);
  }
  return {
    allowed: true,
    reason: "allowed",
    href: parsed.href,
    url: navigationUrlEvidence(parsed.href),
  };
}

function tongchengElongFallbackResultUrl(query = {}) {
  const placeKey = String(
    query && query.options && query.options.expected_lodging_place_key || "",
  ).trim().toLowerCase();
  const destination = TONGCHENG_LODGING_DESTINATIONS[placeKey];
  const adults = Number(query && query.adults);
  if (
    !destination ||
    calendarDateQueryValue(query && query.start_date) === null ||
    calendarDateQueryValue(query && query.end_date) === null ||
    String(query.end_date) <= String(query.start_date) ||
    !Number.isInteger(adults) || adults < 1 || adults > 9 ||
    Number(query && query.rooms) !== 1
  ) {
    return null;
  }
  const url = new URL("https://m.elong.com/ihotel/hotellist");
  url.searchParams.set("city", destination.city);
  url.searchParams.set("indate", String(query.start_date));
  url.searchParams.set("outdate", String(query.end_date));
  url.searchParams.set("adultsNumber", String(adults));
  url.searchParams.set("roomNum", "1");
  url.searchParams.set("intl", "1");
  return url.href;
}

function tongchengLyFallbackResultUrl(query = {}) {
  const elongUrl = tongchengElongFallbackResultUrl(query);
  if (!elongUrl) return null;
  const url = new URL(elongUrl);
  url.hostname = "m.ly.com";
  url.pathname = "/hotel/hotellist";
  return url.href;
}

function auditedLodgingResultUrlDecision(provider, rawUrl, query = {}) {
  if (provider === "fliggy") {
    return fliggyLodgingResultUrlDecision(rawUrl, query);
  }
  if (provider === "qunar") {
    return qunarLodgingResultUrlDecision(rawUrl, query);
  }
  if (provider === "tongcheng") {
    return tongchengLodgingResultUrlDecision(rawUrl, query);
  }
  return {
    allowed: false,
    reason: "unsupported_provider",
    url: navigationUrlEvidence(rawUrl),
  };
}

function auditedLodgingResultUrl(provider, query = {}) {
  const placeKey = String(
    query && query.options && query.options.expected_lodging_place_key || "",
  ).trim().toLowerCase();
  const adults = Number(query && query.adults);
  if (
    calendarDateQueryValue(query && query.start_date) === null ||
    calendarDateQueryValue(query && query.end_date) === null ||
    String(query.end_date) <= String(query.start_date) ||
    !Number.isInteger(adults) || adults < 1 || adults > 9 ||
    Number(query && query.rooms) !== 1
  ) {
    return null;
  }
  if (provider === "fliggy") {
    const destination = FLIGGY_LODGING_DESTINATIONS[placeKey];
    if (!destination) return null;
    const url = new URL("https://hotel.fliggy.com/hotel_list3.htm");
    url.searchParams.set("spm", "181.11358650.hotelModule.internationalSearch");
    url.searchParams.set("city", destination.city);
    url.searchParams.set("cityName", destination.cityName);
    url.searchParams.set("checkIn", String(query.start_date));
    url.searchParams.set("checkOut", String(query.end_date));
    url.searchParams.set("keywords", "");
    url.searchParams.set("aNum_1", String(adults));
    url.searchParams.set("cNum_1", "0");
    return url.href;
  }
  if (provider === "qunar") {
    const destination = QUNAR_LODGING_DESTINATIONS[placeKey];
    if (!destination) return null;
    const url = new URL("https://hotel.qunar.com/intl/search.jsp");
    url.searchParams.set("toCity", destination.toCity);
    url.searchParams.set("fromDate", String(query.start_date));
    url.searchParams.set("toDate", String(query.end_date));
    url.searchParams.set("cityurl", destination.cityurl);
    url.searchParams.set("from", "globalhotelpages");
    return url.href;
  }
  if (provider === "tongcheng") {
    const destination = TONGCHENG_LODGING_DESTINATIONS[placeKey];
    if (!destination) return null;
    const url = new URL("https://www.ly.com/hotel/hotellist");
    url.searchParams.set("city", destination.city);
    url.searchParams.set("inDate", String(query.start_date));
    url.searchParams.set("outDate", String(query.end_date));
    url.searchParams.set("adultsNumber", String(adults));
    url.searchParams.set("roomNum", "1");
    url.searchParams.set("intl", "1");
    return url.href;
  }
  return null;
}

function preservedLodgingResultQueryKey(query = {}) {
  const placeKey = String(
    query && query.options && query.options.expected_lodging_place_key || "",
  ).trim().toLowerCase();
  return [
    placeKey,
    String(query && query.start_date || ""),
    String(query && query.end_date || ""),
    Number(query && query.adults),
    Number(query && query.rooms),
  ].join("|");
}

async function preserveExactLodgingResultTab(
  lease,
  tabId,
  ownedTabIds,
  ownedWindowIds,
) {
  if (
    !lease ||
    lease.kind !== "lodging" ||
    lease.query.search_url ||
    !["fliggy", "qunar"].includes(lease.provider) ||
    !Number.isInteger(tabId) ||
    !chrome.tabs ||
    typeof chrome.tabs.get !== "function"
  ) {
    return null;
  }
  let tab;
  try {
    tab = await chrome.tabs.get(tabId);
  } catch {
    return null;
  }
  const tabUrl = String(tab && (tab.url || tab.pendingUrl) || "");
  const decision = auditedLodgingResultUrlDecision(
    lease.provider,
    tabUrl,
    lease.query,
  );
  if (!decision || !decision.allowed) {
    // The tab is not on a reusable exact result page, so closing it (the
    // lease default) is the honest outcome — there is nothing to reuse.
    return null;
  }
  const windowId = Number.isInteger(tab.windowId) ? tab.windowId : null;
  ownedTabIds.delete(tabId);
  if (windowId !== null) {
    ownedWindowIds.delete(windowId);
  }
  const isolationWindow = lease.provider === "qunar";
  preservedExactResultTabs.set(tabId, {
    window_id: windowId,
    provider: lease.provider,
    kind: lease.kind,
    preserved_at_ms: Date.now(),
    query_key: preservedLodgingResultQueryKey(lease.query),
    isolation_window: isolationWindow,
  });
  // Reserve the tab in the reuse pool so no other lease can claim it and so
  // the API retry's exact-result reuse path finds it.
  leasedExistingTabIds.add(tabId);
  return {
    tab_id: tabId,
    window_id: windowId,
    isolation_window: isolationWindow,
    url: decision.href,
  };
}

async function sweepExpiredPreservedResultTabs() {
  const now = Date.now();
  for (const [tabId, record] of preservedExactResultTabs) {
    if (
      !record ||
      now - record.preserved_at_ms < PRESERVED_EXACT_RESULT_TAB_MAX_AGE_MS
    ) {
      continue;
    }
    preservedExactResultTabs.delete(tabId);
    leasedExistingTabIds.delete(tabId);
    if (chrome.tabs && typeof chrome.tabs.remove === "function") {
      chrome.tabs.remove(tabId).catch(() => {});
    }
    if (
      record.isolation_window &&
      record.window_id !== null &&
      chrome.windows &&
      typeof chrome.windows.remove === "function"
    ) {
      chrome.windows.remove(record.window_id).catch(() => {});
    }
  }
}

async function claimReusableExactLodgingResultTab(lease) {
  if (
    !lease ||
    lease.kind !== "lodging" ||
    lease.query.search_url ||
    !(
      lease.query.options &&
      lease.query.options.__tripchord_reuse_exact_result_tab === true
    ) ||
    !["fliggy", "qunar"].includes(lease.provider) ||
    !chrome.tabs ||
    typeof chrome.tabs.query !== "function"
  ) {
    return null;
  }
  const queryKey = preservedLodgingResultQueryKey(lease.query);
  for (const [tabId, record] of preservedExactResultTabs) {
    if (
      !record ||
      record.provider !== lease.provider ||
      record.kind !== "lodging" ||
      record.query_key !== queryKey
    ) {
      continue;
    }
    let currentUrl = null;
    try {
      const current = await chrome.tabs.get(tabId);
      currentUrl = String(current && (current.url || current.pendingUrl) || "");
    } catch {
      currentUrl = null;
    }
    if (!currentUrl) {
      preservedExactResultTabs.delete(tabId);
      leasedExistingTabIds.delete(tabId);
      continue;
    }
    const decision = auditedLodgingResultUrlDecision(
      lease.provider,
      currentUrl,
      lease.query,
    );
    if (!decision || !decision.allowed) {
      preservedExactResultTabs.delete(tabId);
      leasedExistingTabIds.delete(tabId);
      continue;
    }
    preservedExactResultTabs.delete(tabId);
    leasedExistingTabIds.add(tabId);
    return {
      tab_id: tabId,
      url: decision.href,
      preserved_exact_result: true,
      isolation_window: Boolean(record.isolation_window),
      window_id: Number.isInteger(record.window_id) ? record.window_id : null,
    };
  }
  const expectedUrl = auditedLodgingResultUrl(
    lease.provider,
    lease.query,
  );
  const expectedDecision = expectedUrl &&
    auditedLodgingResultUrlDecision(
      lease.provider,
      expectedUrl,
      lease.query,
    );
  if (!expectedDecision || !expectedDecision.allowed) {
    return null;
  }
  const tabs = await chrome.tabs.query({});
  for (const tab of tabs) {
    if (
      !tab ||
      !Number.isInteger(tab.id) ||
      leasedExistingTabIds.has(tab.id)
    ) {
      continue;
    }
    const decision = auditedLodgingResultUrlDecision(
      lease.provider,
      tab.url || tab.pendingUrl || "",
      lease.query,
    );
    if (decision.allowed) {
      leasedExistingTabIds.add(tab.id);
      return { tab_id: tab.id, url: decision.href };
    }
  }
  return null;
}

async function claimReusableExactFlightResultTab(lease) {
  if (
    !lease ||
    lease.kind !== "flight" ||
    !lease.query.search_url ||
    !(
      lease.query.options &&
      lease.query.options.__tripchord_reuse_exact_result_tab === true
    ) ||
    !chrome.tabs ||
    typeof chrome.tabs.query !== "function"
  ) {
    return null;
  }
  const expected = trustedSearchUrlDriverEvidence(
    lease.provider,
    lease.kind,
    lease.query.search_url,
    lease.query,
  );
  if (expected.confirmation_scope !== "trusted_exact_search_url") {
    return null;
  }
  const tabs = await chrome.tabs.query({});
  for (const tab of tabs) {
    if (
      !tab ||
      !Number.isInteger(tab.id) ||
      leasedExistingTabIds.has(tab.id)
    ) {
      continue;
    }
    const tabUrl = String(tab.url || tab.pendingUrl || "");
    const exactSearchUrl = tabUrl === lease.query.search_url;
    const resultDecision = exactSearchUrl
      ? {
          allowed: true,
          href: tabUrl,
          confirmation_scope: "trusted_exact_search_url",
          readback_query: expected.readback_query,
          url_confirmed_fields: expected.url_confirmed_fields,
        }
      : auditedFlightResultUrlDecision(lease.provider, tabUrl, lease.query);
    if (!resultDecision.allowed) {
      continue;
    }
    leasedExistingTabIds.add(tab.id);
    return {
      tab_id: tab.id,
      url: resultDecision.href,
      result_url_readback: resultDecision.readback_query,
      result_url_confirmed_fields: resultDecision.url_confirmed_fields,
      confirmation_scope: resultDecision.confirmation_scope,
    };
  }
  return null;
}

function auditedProviderLoginRedirect(provider, kind, rawUrl) {
  let parsed;
  try {
    parsed = new URL(rawUrl);
  } catch {
    return false;
  }
  if (provider === "tongcheng" && kind === "flight") {
    if (
      parsed.protocol !== "https:" ||
      parsed.hostname.toLowerCase() !== "secure.elong.com" ||
      parsed.pathname.toLowerCase() !== "/passport/login_cn.html"
    ) {
      return false;
    }
    let rawNextUrl = String(parsed.searchParams.get("nexturl") || "");
    for (let index = 0; index < 3 && !/^https?:\/\//i.test(rawNextUrl); index += 1) {
      try {
        const decoded = decodeURIComponent(rawNextUrl);
        if (decoded === rawNextUrl) break;
        rawNextUrl = decoded;
      } catch {
        return false;
      }
    }
    let nextUrl;
    try {
      nextUrl = new URL(rawNextUrl, "https://www.ly.com");
    } catch {
      return false;
    }
    const keys = [...nextUrl.searchParams.keys()].sort();
    const allowedKeys = new Set([
      "arrivalCity",
      "departureCity",
      "para",
      "refid",
    ]);
    return (
      ["http:", "https:"].includes(nextUrl.protocol) &&
      nextUrl.hostname.toLowerCase() === "www.ly.com" &&
      nextUrl.pathname.toLowerCase() === "/eliflight/book1.html" &&
      nextUrl.searchParams.has("para") &&
      keys.every((key) => allowedKeys.has(key))
    );
  }
  if (kind !== "lodging") {
    return false;
  }
  if (provider === "fliggy") {
    if (
      parsed.protocol !== "https:" ||
      parsed.hostname.toLowerCase() !== "login.taobao.com" ||
      parsed.pathname !== "/havanaone/login/login.htm" ||
      parsed.searchParams.get("bizName") !== "taobao"
    ) {
      return false;
    }
    const redirect = String(parsed.searchParams.get("redirectURL") || "");
    return (
      /^https:\/\/hotel\.fliggy\.com(?::443)?\/hotel_list3\.htm\//i.test(
        redirect,
      ) &&
      /\/page\/login_jump(?:[/?]|$)/i.test(redirect)
    );
  }
  if (
    provider !== "qunar" ||
    parsed.protocol !== "https:" ||
    parsed.hostname.toLowerCase() !== "user.qunar.com" ||
    parsed.pathname !== "/passport/login.jsp"
  ) {
    return false;
  }
  let returnUrl = String(parsed.searchParams.get("ret") || "");
  // URLSearchParams removes one layer. Qunar has also emitted nested encodings,
  // so unwrap a small bounded number of URL-like layers before applying the
  // exact provider-owned lodging contract below.
  for (let index = 0; index < 3 && !/^https?:\/\//i.test(returnUrl); index += 1) {
    if (!/^https?%/i.test(returnUrl)) {
      return false;
    }
    try {
      const decoded = decodeURIComponent(returnUrl);
      if (decoded === returnUrl) {
        return false;
      }
      returnUrl = decoded;
    } catch {
      return false;
    }
  }
  let returnTarget;
  try {
    returnTarget = new URL(returnUrl);
  } catch {
    return false;
  }
  if (
    !["http:", "https:"].includes(returnTarget.protocol) ||
    returnTarget.hostname.toLowerCase() !== "hotel.qunar.com" ||
    returnTarget.port ||
    returnTarget.username ||
    returnTarget.password
  ) {
    return false;
  }
  if (returnTarget.pathname === "/intl/search.jsp") {
    return true;
  }
  const exactCityPaths = new Set(
    Object.values(QUNAR_LODGING_DESTINATIONS).map(
      (destination) => `/city/${destination.cityurl}`,
    ),
  );
  const normalizedReturnPath =
    returnTarget.pathname === "/"
      ? "/"
      : returnTarget.pathname.replace(/\/+$/, "");
  if (
    exactCityPaths.has(normalizedReturnPath) &&
    !returnTarget.search
  ) {
    return true;
  }
  // Qunar may replace the exact international-results return target with its
  // provider-owned international landing while constructing the login jump.
  // Treat only that query-free landing as the same typed login gate; broader
  // Qunar pages and external/credential-bearing return targets remain rejected.
  return (
    returnTarget.pathname === "/global/" &&
    !returnTarget.search &&
    !returnTarget.hash
  );
}

function trustedSearchUrlDriverEvidence(provider, kind, rawUrl, query = {}) {
  const unverified = {
    confirmed_query: null,
    readback_query: null,
    confirmation_scope: "provider_url_only_unverified",
    url_confirmed_fields: [],
    party_availability_confirmed: false,
    pricing_context: "unverified",
  };
  if (
    kind !== "flight" ||
    !providerVerticalUrlAllowed(provider, kind, rawUrl) ||
    typeof query.origin !== "string" ||
    typeof query.destination !== "string" ||
    typeof query.origin_code !== "string" ||
    typeof query.destination_code !== "string" ||
    typeof query.start_date !== "string" ||
    typeof query.end_date !== "string" ||
    !Number.isInteger(query.adults)
  ) {
    return unverified;
  }

  const originCode = query.origin_code.toUpperCase();
  const destinationCode = query.destination_code.toUpperCase();
  const validIata = (value) => /^[A-Z]{3}$/.test(value);
  const validDate = (value) => /^\d{4}-\d{2}-\d{2}$/.test(value);
  if (
    !validIata(originCode) ||
    !validIata(destinationCode) ||
    !validDate(query.start_date) ||
    !validDate(query.end_date) ||
    query.adults < 1 ||
    query.adults > 9
  ) {
    return unverified;
  }

  let expectedUrl;
  let partyAvailabilityConfirmed;
  let pricingContext;
  let readbackQuery;
  let urlConfirmedFields;
  if (provider === "ctrip") {
    expectedUrl =
      "https://flights.ctrip.com/international/search/" +
      `round-${originCode.toLowerCase()}-${destinationCode.toLowerCase()}` +
      `?depdate=${query.start_date}_${query.end_date}` +
      `&cabin=y_s&adult=${query.adults}&child=0&infant=0`;
    partyAvailabilityConfirmed = true;
    pricingContext = "requested_adults_in_search_url";
    readbackQuery = {
      origin_code: originCode,
      destination_code: destinationCode,
      start_date: query.start_date,
      end_date: query.end_date,
      adults: query.adults,
    };
    urlConfirmedFields = [
      "origin_code",
      "destination_code",
      "start_date",
      "end_date",
      "adults",
    ];
  } else if (provider === "fliggy") {
    expectedUrl =
      "https://sijipiao.fliggy.com/ie/flight_search_result.htm" +
      `?tripType=1&depCity=${originCode}&arrCity=${destinationCode}` +
      `&depDate=${query.start_date}&arrDate=${query.end_date}`;
    partyAvailabilityConfirmed = false;
    pricingContext = "per_person_x_requested_adults";
    readbackQuery = {
      origin_code: originCode,
      destination_code: destinationCode,
      start_date: query.start_date,
      end_date: query.end_date,
    };
    urlConfirmedFields = [
      "origin_code",
      "destination_code",
      "start_date",
      "end_date",
    ];
  } else if (provider === "qunar") {
    const normalizedAlias = (value) =>
      String(value)
        .trim()
        .normalize("NFKD")
        .replace(/[\u0300-\u036f]/g, "")
        .toLowerCase();
    const auditedIdentities = {
      HGH: {
        canonicalName: "杭州",
        aliases: new Set(["杭州", "hangzhou", "hgh"]),
      },
      MLE: {
        canonicalName: "马累",
        aliases: new Set(["马累", "马尔代夫", "male", "mle"]),
      },
    };
    const originIdentity = auditedIdentities[originCode];
    const destinationIdentity = auditedIdentities[destinationCode];
    if (
      !originIdentity ||
      !destinationIdentity ||
      !originIdentity.aliases.has(normalizedAlias(query.origin)) ||
      !destinationIdentity.aliases.has(normalizedAlias(query.destination))
    ) {
      return unverified;
    }
    const parameters = [
      ["from", "flight_int_search"],
      ["showTotalPr", "0"],
      ["searchType", "RoundTripFlight"],
      ["fromCity", originIdentity.canonicalName],
      ["toCity", destinationIdentity.canonicalName],
      ["adultNum", String(query.adults)],
      ["childNum", "0"],
      ["fromDate", query.start_date],
      ["toDate", query.end_date],
    ];
    expectedUrl =
      "https://flight.qunar.com/twell/flight/Search.jsp?" +
      parameters
        .map(([key, value]) => `${encodeURIComponent(key)}=${encodeURIComponent(value)}`)
        .join("&");
    partyAvailabilityConfirmed = true;
    pricingContext = "requested_adults_in_search_url";
    readbackQuery = {
      origin: originIdentity.canonicalName,
      destination: query.destination,
      start_date: query.start_date,
      end_date: query.end_date,
      adults: query.adults,
    };
    urlConfirmedFields = [
      "origin",
      "destination",
      "start_date",
      "end_date",
      "adults",
    ];
  } else if (provider === "tongcheng") {
    const normalizedAlias = (value) =>
      String(value)
        .trim()
        .normalize("NFKD")
        .replace(/[\u0300-\u036f]/g, "")
        .toLowerCase();
    const auditedIdentities = {
      HGH: {
        canonicalName: "杭州",
        aliases: new Set(["杭州", "hangzhou", "hgh"]),
      },
      MLE: {
        canonicalName: "马累",
        aliases: new Set(["马累", "马尔代夫", "male", "mle"]),
      },
    };
    const originIdentity = auditedIdentities[originCode];
    const destinationIdentity = auditedIdentities[destinationCode];
    if (
      !originIdentity ||
      !destinationIdentity ||
      !originIdentity.aliases.has(normalizedAlias(query.origin)) ||
      !destinationIdentity.aliases.has(normalizedAlias(query.destination))
    ) {
      return unverified;
    }
    const para = [
      originCode,
      destinationCode,
      query.start_date,
      query.end_date,
      "RT",
      `${query.adults}_0_0`,
      "Y|S|C|F",
    ].join("*");
    expectedUrl =
      "https://www.ly.com/eliflight/book1.html" +
      `?para=${encodeURIComponent(para)}` +
      `&departureCity=${encodeURIComponent(originIdentity.canonicalName)}` +
      `&arrivalCity=${encodeURIComponent(destinationIdentity.canonicalName)}`;
    partyAvailabilityConfirmed = true;
    pricingContext = "requested_adults_in_search_url";
    readbackQuery = {
      origin: originIdentity.canonicalName,
      destination: query.destination,
      origin_code: originCode,
      destination_code: destinationCode,
      start_date: query.start_date,
      end_date: query.end_date,
      adults: query.adults,
    };
    urlConfirmedFields = [
      "origin",
      "destination",
      "origin_code",
      "destination_code",
      "start_date",
      "end_date",
      "adults",
    ];
  } else {
    return unverified;
  }

  // Keep this byte-for-byte contract in sync with the API's trusted URL
  // generator. Alternate hosts, parameter order, fragments, and additions do
  // not inherit the trusted direct-search scope.
  if (rawUrl !== expectedUrl) {
    return unverified;
  }
  return {
    confirmed_query: {
      origin: query.origin,
      destination: query.destination,
      start_date: query.start_date,
      end_date: query.end_date,
      adults: query.adults,
    },
    readback_query: readbackQuery,
    confirmation_scope: "trusted_exact_search_url",
    url_confirmed_fields: urlConfirmedFields,
    party_availability_confirmed: partyAvailabilityConfirmed,
    pricing_context: pricingContext,
  };
}

function auditedFlightResultUrlDecision(provider, rawUrl, query = {}) {
  const rejected = (reason, parsed = null) => ({
    allowed: false,
    reason,
    url: navigationUrlEvidence(parsed ? parsed.href : rawUrl),
  });
  if (provider !== "qunar") {
    return rejected("unsupported_provider");
  }
  let parsed;
  try {
    parsed = new URL(rawUrl);
  } catch {
    return rejected("invalid_url");
  }
  if (
    parsed.protocol !== "https:" ||
    parsed.hostname.toLowerCase() !== "flight.qunar.com" ||
    parsed.port ||
    parsed.username ||
    parsed.password ||
    parsed.hash ||
    !new Set([
      "/twell/flight/Search.jsp",
      "/site/interroundtrip_compare.htm",
    ]).has(parsed.pathname)
  ) {
    return rejected("wrong_surface", parsed);
  }
  const originCode = String(query.origin_code || "").toUpperCase();
  const destinationCode = String(query.destination_code || "").toUpperCase();
  const originIdentity = {
    HGH: { canonicalName: "杭州", aliases: new Set(["杭州", "hangzhou", "hgh"]) },
  }[originCode];
  const destinationIdentity = {
    MLE: { canonicalName: "马累", aliases: new Set(["马累", "马尔代夫", "male", "mle"]) },
  }[destinationCode];
  const normalizedAlias = (value) =>
    String(value)
      .trim()
      .normalize("NFKD")
      .replace(/[\u0300-\u036f]/g, "")
      .toLowerCase();
  const adults = Number(query.adults);
  if (
    !originIdentity ||
    !destinationIdentity ||
    !originIdentity.aliases.has(normalizedAlias(query.origin)) ||
    !destinationIdentity.aliases.has(normalizedAlias(query.destination)) ||
    calendarDateQueryValue(query.start_date) === null ||
    calendarDateQueryValue(query.end_date) === null ||
    String(query.end_date) <= String(query.start_date) ||
    !Number.isInteger(adults) ||
    adults < 1 ||
    adults > 9
  ) {
    return rejected("invalid_requested_query", parsed);
  }

  const isCompareRoute = parsed.pathname === "/site/interroundtrip_compare.htm";
  const requiredKeys = isCompareRoute
    ? [
        "fromCity",
        "toCity",
        "fromDate",
        "toDate",
        "from",
        "lowestPrice",
        "isInter",
        "favoriteKey",
        "showTotalPr",
        "adultNum",
        "childNum",
        "cabinClass",
      ]
    : [
        "from",
        "showTotalPr",
        "searchType",
        "fromCity",
        "toCity",
        "adultNum",
        "childNum",
        "fromDate",
        "toDate",
      ];
  const entries = [...parsed.searchParams.entries()];
  if (
    entries.length !== requiredKeys.length ||
    entries.some(([key]) => !requiredKeys.includes(key)) ||
    requiredKeys.some(
      (key) => entries.filter(([candidate]) => candidate === key).length !== 1,
    )
  ) {
    return rejected("query_shape_mismatch", parsed);
  }
  const expected = new Map([
    ["fromCity", originIdentity.canonicalName],
    ["toCity", destinationIdentity.canonicalName],
    ["fromDate", String(query.start_date)],
    ["toDate", String(query.end_date)],
    ["from", "flight_int_search"],
    ["showTotalPr", "0"],
    ["adultNum", String(adults)],
    ["childNum", "0"],
  ]);
  if (isCompareRoute) {
    expected.set("lowestPrice", "null");
    expected.set("isInter", "true");
    expected.set("favoriteKey", "");
    expected.set("cabinClass", "");
  } else {
    expected.set("searchType", "RoundTripFlight");
  }
  if ([...expected].some(([key, value]) => parsed.searchParams.get(key) !== value)) {
    return rejected("query_value_mismatch", parsed);
  }
  return {
    allowed: true,
    reason: "allowed",
    href: parsed.href,
    url: navigationUrlEvidence(parsed.href),
    confirmation_scope: "audited_visible_result_url",
    readback_query: {
      origin: originIdentity.canonicalName,
      destination: destinationIdentity.canonicalName,
      origin_code: originCode,
      destination_code: destinationCode,
      start_date: String(query.start_date),
      end_date: String(query.end_date),
      adults,
    },
    url_confirmed_fields: [
      "origin_code",
      "destination_code",
      "start_date",
      "end_date",
      "adults",
    ],
  };
}

function isTransientNavigationUrl(rawUrl) {
  return (
    !rawUrl ||
    rawUrl === "about:blank" ||
    rawUrl.startsWith("chrome://newtab") ||
    rawUrl === "edge://newtab" ||
    rawUrl === "edge://newtab/"
  );
}

function isMessagePortClosedError(error) {
  return /(?:message (?:channel|port) closed before a response was received|could not establish connection\.\s*receiving end does not exist)/i.test(
    String(error && error.message || error),
  );
}

function lifecycleError(code, message, details = {}) {
  const error = new Error(message);
  error.tripchordCode = code;
  error.tripchordDetails = details;
  return error;
}

function navigationFailure(error) {
  return error && error.tripchordCode === "navigation_error";
}

function loginRequiredFailure(error) {
  return error && error.tripchordCode === "login_required";
}

function lifecycleFailureDetails(error) {
  return (
    error &&
    error.tripchordDetails &&
    typeof error.tripchordDetails === "object"
  )
    ? { navigation_diagnostic: error.tripchordDetails }
    : {};
}

function navigationTraceSnapshot(trace) {
  return trace.map((entry) => ({
    ...entry,
    url: {
      ...entry.url,
      query_keys: [...entry.url.query_keys],
    },
  }));
}

function appendNavigationTrace(
  trace,
  {
    phase,
    tabRole,
    status = null,
    rawUrl = "",
  },
) {
  const entry = {
    sequence: trace.length
      ? trace[trace.length - 1].sequence + 1
      : 0,
    phase,
    tab_role: tabRole,
    status,
    transient: isTransientNavigationUrl(rawUrl),
    url: navigationUrlEvidence(rawUrl),
  };
  const previous = trace[trace.length - 1];
  if (
    previous &&
    previous.phase === entry.phase &&
    previous.tab_role === entry.tab_role &&
    previous.status === entry.status &&
    previous.transient === entry.transient &&
    JSON.stringify(previous.url) === JSON.stringify(entry.url)
  ) {
    return;
  }
  if (trace.length >= MAX_NAVIGATION_TRACE_ENTRIES) {
    // Preserve observer start and the most recent redirect events.
    trace.splice(1, 1);
  }
  trace.push(entry);
}

function loginRedirectReturnEvidence(rawUrl) {
  let parsed;
  try {
    parsed = new URL(rawUrl);
  } catch {
    return null;
  }
  if (parsed.protocol !== "https:") {
    return null;
  }
  const isQunar =
    parsed.hostname.toLowerCase() === "user.qunar.com" &&
    parsed.pathname === "/passport/login.jsp";
  const isTongchengFlight =
    parsed.hostname.toLowerCase() === "secure.elong.com" &&
    parsed.pathname.toLowerCase() === "/passport/login_cn.html";
  if (!isQunar && !isTongchengFlight) {
    return null;
  }
  let returnUrl = String(
    parsed.searchParams.get(isQunar ? "ret" : "nexturl") || "",
  );
  for (let index = 0; index < 3 && !/^https?:\/\//i.test(returnUrl); index += 1) {
    try {
      const decoded = decodeURIComponent(returnUrl);
      if (decoded === returnUrl) break;
      returnUrl = decoded;
    } catch {
      return null;
    }
  }
  try {
    return navigationUrlEvidence(
      new URL(returnUrl, isTongchengFlight ? "https://www.ly.com" : undefined).href,
    );
  } catch {
    return {
      parseable: false,
      scheme: null,
      host: null,
      path_shape: null,
      query_keys: [],
      query_keys_truncated: false,
      has_fragment: false,
    };
  }
}

function navigationDiagnostic({
  provider,
  kind,
  stage,
  reason,
  rawUrl = "",
  trace = [],
}) {
  return {
    provider,
    vertical: kind,
    stage,
    reason,
    rejected_url: navigationUrlEvidence(rawUrl),
    login_return_url: loginRedirectReturnEvidence(rawUrl),
    redirect_trace: navigationTraceSnapshot(trace),
  };
}

async function connectionStorage() {
  const area = chrome.storage.local || chrome.storage.session;
  if (typeof area.setAccessLevel === "function") {
    await area.setAccessLevel({ accessLevel: "TRUSTED_CONTEXTS" });
  }
  return area;
}

async function sessionConfig() {
  const storage = await connectionStorage();
  const stored = await storage.get([
    "tripchordBridgeUrl",
    "tripchordBridgeToken",
    "tripchordConnected",
  ]);
  return {
    connected: stored.tripchordConnected === true,
    bridgeUrl: String(stored.tripchordBridgeUrl || DEFAULT_BRIDGE_URL).replace(/\/+$/, ""),
    token: String(stored.tripchordBridgeToken || ""),
  };
}

async function offscreenDocumentPresent() {
  if (!chrome.offscreen) {
    return false;
  }
  if (typeof chrome.offscreen.hasDocument === "function") {
    return chrome.offscreen.hasDocument();
  }
  if (typeof chrome.runtime.getContexts === "function") {
    const contexts = await chrome.runtime.getContexts({
      contextTypes: ["OFFSCREEN_DOCUMENT"],
      documentUrls: [chrome.runtime.getURL(OFFSCREEN_DOCUMENT_PATH)],
    });
    return Array.isArray(contexts) && contexts.length > 0;
  }
  return false;
}

async function ensureKeepaliveHost() {
  if (
    !chrome.offscreen ||
    typeof chrome.offscreen.createDocument !== "function"
  ) {
    return false;
  }
  if (await offscreenDocumentPresent()) {
    return true;
  }
  if (!offscreenCreationPromise) {
    offscreenCreationPromise = chrome.offscreen.createDocument({
      url: OFFSCREEN_DOCUMENT_PATH,
      reasons: ["WORKERS"],
      justification:
        "Keep the read-only browser task worker alive while a bounded provider lease is running.",
    }).finally(() => {
      offscreenCreationPromise = null;
    });
  }
  try {
    await offscreenCreationPromise;
    return true;
  } catch (error) {
    if (await offscreenDocumentPresent()) {
      return true;
    }
    throw error;
  }
}

async function closeKeepaliveHost() {
  if (
    !chrome.offscreen ||
    typeof chrome.offscreen.closeDocument !== "function" ||
    !await offscreenDocumentPresent()
  ) {
    return;
  }
  await chrome.offscreen.closeDocument();
}

function bridgeValidationDiagnostic(payload) {
  const detail =
    payload &&
    typeof payload === "object" &&
    Array.isArray(payload.detail)
      ? payload.detail
      : [];
  return detail.slice(0, 8).map((item) => ({
    location: Array.isArray(item && item.loc)
      ? item.loc.slice(0, 8).map((part) => String(part).slice(0, 80))
      : [],
    type: String(item && item.type || "unknown").slice(0, 120),
    message: String(item && item.msg || "validation rejected").slice(0, 240),
  }));
}

function completionContractRejected(error) {
  if (!error) {
    return false;
  }
  if (error.status === 400 || error.status === 422) {
    return true;
  }
  return (
    error.status === 409 &&
    /(?:quote provider or kind does not match|failure page_url does not match)/i.test(
      String(error.bridgeDetail || ""),
    )
  );
}

function terminalOrphanedReloadReceipt(error) {
  if (!error) {
    return false;
  }
  if (error.status === 404) {
    return true;
  }
  return (
    error.status === 409 &&
    /(?:reload request has expired|conflicts with the terminal observation)/i.test(
      String(error.bridgeDetail || ""),
    )
  );
}

function pageLocationDiagnostic(rawUrl) {
  try {
    const parsed = new URL(String(rawUrl || ""));
    return {
      scheme: parsed.protocol.replace(/:$/, "").slice(0, 16),
      host: parsed.hostname.toLowerCase().slice(0, 160),
      path: parsed.pathname.slice(0, 240),
    };
  } catch {
    return null;
  }
}

async function bridgeFetch(path, options = {}) {
  const config = await sessionConfig();
  if (!config.connected || config.token.length < 32) {
    throw new Error("browser companion is not paired");
  }
  const {
    timeoutMs = 15000,
    ...requestOptions
  } = options;
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const response = await fetch(`${config.bridgeUrl}${path}`, {
      ...requestOptions,
      signal: controller.signal,
      headers: {
        "Content-Type": "application/json",
        "X-TripChord-Bridge-Token": config.token,
        ...(requestOptions.headers || {}),
      },
    });
    if (!response.ok) {
      const error = new Error(`bridge returned HTTP ${response.status}`);
      error.status = response.status;
      if ([400, 409, 422].includes(response.status)) {
        let payload = null;
        try {
          payload = await response.json();
        } catch {
          payload = null;
        }
        error.validationDiagnostic =
          bridgeValidationDiagnostic(payload);
        error.bridgeDetail =
          payload &&
          typeof payload === "object" &&
          typeof payload.detail === "string"
            ? payload.detail.slice(0, 500)
            : null;
      }
      throw error;
    }
    return response.json();
  } finally {
    clearTimeout(timer);
  }
}

async function markPairingRejected() {
  const storage = await connectionStorage();
  await storage.remove([
    "tripchordBridgeToken",
    "tripchordConnected",
  ]);
  await storage.set({
    tripchordPairingStatus: "reauth_required",
  });
  await chrome.alarms.clear(POLL_ALARM);
  clearTimeout(followupTimer);
  followupTimer = null;
  await closeKeepaliveHost();
}

function currentBuildIdentity() {
  return {
    protocol_version: BUILD_META.protocol_version,
    manifest_version: BUILD_META.manifest_version,
    build_sha256: BUILD_META.build_sha256,
    content_runtime_version: BUILD_META.content_runtime_version,
  };
}

function validRuntimeInstanceId(value) {
  return /^[A-Za-z0-9][A-Za-z0-9._:-]{15,127}$/.test(String(value || ""));
}

function validReloadRequestId(value) {
  return /^companion-reload-[A-Za-z0-9_-]+$/.test(String(value || "")) &&
    String(value).length <= 128;
}

function validBuildIdentity(value) {
  return Boolean(
    value &&
    typeof value === "object" &&
    value.protocol_version === CONTROL_PROTOCOL_VERSION &&
    /^[A-Za-z0-9][A-Za-z0-9._+-]{0,63}$/.test(
      String(value.manifest_version || ""),
    ) &&
    /^[0-9a-f]{64}$/.test(String(value.build_sha256 || "")) &&
    /^[A-Za-z0-9][A-Za-z0-9._+-]{0,63}$/.test(
      String(value.content_runtime_version || ""),
    )
  );
}

function validReloadReceipt(value) {
  if (
    !value ||
    typeof value !== "object" ||
    value.companion_id !== COMPANION_ID ||
    !validReloadRequestId(value.request_id) ||
    typeof value.receipt_token !== "string" ||
    value.receipt_token.length < 32 ||
    value.receipt_token.length > 128 ||
    !Number.isInteger(value.delivery_generation) ||
    value.delivery_generation < 1 ||
    value.delivery_generation > 32 ||
    !["accepted", "applied", "failed"].includes(value.state) ||
    !validBuildIdentity(value.build_identity) ||
    !validRuntimeInstanceId(value.runtime_instance_id) ||
    (
      value.previous_runtime_instance_id !== null &&
      value.previous_runtime_instance_id !== undefined &&
      !validRuntimeInstanceId(value.previous_runtime_instance_id)
    )
  ) {
    return false;
  }
  if (value.state === "failed") {
    return /^[a-z][a-z0-9_]{0,79}$/.test(String(value.failure_code || ""));
  }
  return value.failure_code === null || value.failure_code === undefined;
}

async function reloadReceiptForClaim() {
  const storage = await connectionStorage();
  const stored = await storage.get(LAST_RELOAD_RECEIPT_STORAGE_KEY);
  const receipt = stored[LAST_RELOAD_RECEIPT_STORAGE_KEY];
  return validReloadReceipt(receipt) ? receipt : null;
}

function controlContractError(code) {
  const error = new Error(`browser companion control rejected: ${code}`);
  error.tripchordControlCode = code;
  return error;
}

function validateReloadControl(control, leases, nowMs = Date.now()) {
  if (!control || typeof control !== "object") {
    throw controlContractError("invalid_control_shape");
  }
  if (
    control.action !== "reload_extension" ||
    (
      control.protocol_version !== undefined &&
      control.protocol_version !== CONTROL_PROTOCOL_VERSION
    )
  ) {
    throw controlContractError("unsupported_control_action");
  }
  if (!validReloadRequestId(control.request_id)) {
    throw controlContractError("invalid_request_id");
  }
  if (!/^[0-9a-f]{64}$/.test(String(control.target_build_sha256 || ""))) {
    throw controlContractError("invalid_target_build_sha256");
  }
  if (control.target_build_sha256 === BUILD_META.build_sha256) {
    throw controlContractError("target_build_already_loaded");
  }
  if (control.expected_runtime_instance_id !== RUNTIME_INSTANCE_ID) {
    throw controlContractError("runtime_instance_mismatch");
  }
  if (
    !Number.isInteger(control.delivery_generation) ||
    control.delivery_generation < 1 ||
    control.delivery_generation > 32
  ) {
    throw controlContractError("invalid_delivery_generation");
  }
  if (
    typeof control.receipt_token !== "string" ||
    control.receipt_token.length < 32 ||
    control.receipt_token.length > 128
  ) {
    throw controlContractError("invalid_receipt_token");
  }
  const expiresAtMs = Date.parse(String(control.expires_at || ""));
  if (
    !Number.isFinite(expiresAtMs) ||
    expiresAtMs <= nowMs ||
    expiresAtMs - nowMs > 10 * 60 * 1000
  ) {
    throw controlContractError("invalid_control_expiry");
  }
  if (Array.isArray(leases) && leases.length > 0) {
    throw controlContractError("control_mixed_with_leases");
  }
  if (activeLeaseIds.size > 0) {
    throw controlContractError("active_leases_not_drained");
  }
  return {
    protocol_version: CONTROL_PROTOCOL_VERSION,
    request_id: control.request_id,
    action: "reload_extension",
    target_build_sha256: control.target_build_sha256,
    expected_runtime_instance_id: control.expected_runtime_instance_id,
    delivery_generation: control.delivery_generation,
    receipt_token: control.receipt_token,
    expires_at: control.expires_at,
    expires_at_ms: expiresAtMs,
  };
}

function validPendingReloadMarker(marker) {
  return Boolean(
    marker &&
    typeof marker === "object" &&
    marker.schema_version === RELOAD_MARKER_SCHEMA_VERSION &&
    marker.protocol_version === CONTROL_PROTOCOL_VERSION &&
    validReloadRequestId(marker.request_id) &&
    marker.action === "reload_extension" &&
    /^[0-9a-f]{64}$/.test(String(marker.target_build_sha256 || "")) &&
    validRuntimeInstanceId(marker.expected_runtime_instance_id) &&
    Number.isInteger(marker.delivery_generation) &&
    marker.delivery_generation >= 1 &&
    marker.delivery_generation <= 32 &&
    typeof marker.receipt_token === "string" &&
    marker.receipt_token.length >= 32 &&
    marker.receipt_token.length <= 128 &&
    Number.isFinite(Number(marker.expires_at_ms)) &&
    validBuildIdentity(marker.previous_build_identity) &&
    ["accepting", "accepted"].includes(marker.state) &&
    Number.isInteger(marker.receipt_attempts) &&
    marker.receipt_attempts >= 0 &&
    marker.receipt_attempts <= MAX_RELOAD_RECEIPT_ATTEMPTS &&
    Number.isInteger(marker.recovery_generation) &&
    marker.recovery_generation >= 1 &&
    marker.recovery_generation <= MAX_RELOAD_RECOVERY_GENERATIONS &&
    typeof marker.reload_attempted === "boolean"
  );
}

function validBlockedReloadTarget(value) {
  return Boolean(
    value &&
    typeof value === "object" &&
    validReloadRequestId(value.request_id) &&
    /^[0-9a-f]{64}$/.test(String(value.target_build_sha256 || "")) &&
    Number.isInteger(value.delivery_generation) &&
    value.delivery_generation >= 1 &&
    value.delivery_generation <= 32 &&
    Number.isInteger(value.recovery_generation) &&
    value.recovery_generation >= 1 &&
    value.recovery_generation <= MAX_RELOAD_RECOVERY_GENERATIONS &&
    Number.isFinite(Number(value.retry_not_before_ms))
  );
}

function reloadReceiptFromMarker(marker, state, failureCode = null) {
  const runtimeRotated =
    RUNTIME_INSTANCE_ID !== marker.expected_runtime_instance_id;
  const receipt = {
    companion_id: COMPANION_ID,
    request_id: marker.request_id,
    receipt_token: marker.receipt_token,
    delivery_generation: marker.delivery_generation,
    state,
    build_identity: currentBuildIdentity(),
    runtime_instance_id: RUNTIME_INSTANCE_ID,
    previous_runtime_instance_id: runtimeRotated
      ? marker.expected_runtime_instance_id
      : null,
    failure_code: state === "failed" ? failureCode : null,
  };
  if (!validReloadReceipt(receipt)) {
    throw controlContractError("invalid_reload_receipt");
  }
  return receipt;
}

async function postReloadReceipt(receipt) {
  return bridgeFetch(CONTROL_RECEIPT_PATH, {
    method: "POST",
    body: JSON.stringify(receipt),
    timeoutMs: 5000,
  });
}

async function recordReloadDiagnostic(values) {
  const storage = await connectionStorage();
  await storage.set({
    [LAST_RELOAD_DIAGNOSTIC_STORAGE_KEY]: {
      request_id: values.request_id || null,
      target_build_sha256: values.target_build_sha256 || null,
      state: values.state,
      failure_code: values.failure_code || null,
      runtime_instance_id: RUNTIME_INSTANCE_ID,
      build_sha256: BUILD_META.build_sha256,
      recorded_at: new Date().toISOString(),
    },
  });
}

async function scheduleReloadReceiptRetry() {
  chrome.alarms.create(POLL_ALARM, { delayInMinutes: 0.5 });
}

async function failPendingReload(marker, failureCode) {
  const storage = await connectionStorage();
  const receipt = reloadReceiptFromMarker(marker, "failed", failureCode);
  await storage.set({
    [LAST_RELOAD_RECEIPT_STORAGE_KEY]: receipt,
    [BLOCKED_RELOAD_TARGET_STORAGE_KEY]: {
      request_id: marker.request_id,
      target_build_sha256: marker.target_build_sha256,
      delivery_generation: marker.delivery_generation,
      recovery_generation: marker.recovery_generation,
      retry_not_before_ms: Date.now() + RELOAD_RECOVERY_COOLDOWN_MS,
    },
  });
  try {
    await postReloadReceipt(receipt);
    await storage.remove(LAST_RELOAD_RECEIPT_STORAGE_KEY);
  } catch {
    // The next authenticated claim carries the same failed receipt. Never
    // retry the extension reload for this request or target.
  }
  await storage.remove(PENDING_RELOAD_STORAGE_KEY);
  reloadPreparing = false;
  controlLifecycleState = "failed";
  await recordReloadDiagnostic({
    request_id: marker.request_id,
    target_build_sha256: marker.target_build_sha256,
    state: "failed",
    failure_code: failureCode,
  });
  return false;
}

async function failSuppressedReloadControl(control, blocked, failureCode) {
  const storage = await connectionStorage();
  const marker = {
    ...control,
    previous_build_identity: currentBuildIdentity(),
    recovery_generation: blocked.recovery_generation,
  };
  const receipt = reloadReceiptFromMarker(marker, "failed", failureCode);
  await storage.set({ [LAST_RELOAD_RECEIPT_STORAGE_KEY]: receipt });
  try {
    await postReloadReceipt(receipt);
    await storage.remove(LAST_RELOAD_RECEIPT_STORAGE_KEY);
  } catch {
    // The next authenticated claim delivers the same terminal failure. The
    // target remains blocked, so a process restart cannot create a reload loop.
  }
  reloadPreparing = false;
  controlLifecycleState = "failed";
  await recordReloadDiagnostic({
    request_id: control.request_id,
    target_build_sha256: control.target_build_sha256,
    state: "failed",
    failure_code: failureCode,
  });
  return false;
}

async function acceptPendingReload(marker) {
  const storage = await connectionStorage();
  const nextAttempts = marker.receipt_attempts + 1;
  const attempting = {
    ...marker,
    receipt_attempts: nextAttempts,
  };
  await storage.set({ [PENDING_RELOAD_STORAGE_KEY]: attempting });
  const receipt = reloadReceiptFromMarker(attempting, "accepted");
  try {
    await postReloadReceipt(receipt);
  } catch (error) {
    controlLifecycleState = "accepted_receipt_pending";
    await recordReloadDiagnostic({
      request_id: marker.request_id,
      target_build_sha256: marker.target_build_sha256,
      state: "accepted_receipt_pending",
      failure_code: "receipt_delivery_failed",
    });
    if (nextAttempts >= MAX_RELOAD_RECEIPT_ATTEMPTS) {
      return failPendingReload(attempting, "accepted_receipt_delivery_failed");
    }
    await scheduleReloadReceiptRetry();
    return false;
  }
  const accepted = {
    ...attempting,
    state: "accepted",
    reload_attempted: true,
    accepted_at: new Date().toISOString(),
  };
  await storage.set({
    [PENDING_RELOAD_STORAGE_KEY]: accepted,
  });
  controlLifecycleState = "reloading";
  await recordReloadDiagnostic({
    request_id: marker.request_id,
    target_build_sha256: marker.target_build_sha256,
    state: "accepted",
  });
  clearTimeout(followupTimer);
  followupTimer = null;
  await chrome.alarms.clear(POLL_ALARM);
  await closeKeepaliveHost();
  chrome.runtime.reload();
  return true;
}

async function applyPendingReload(marker) {
  const storage = await connectionStorage();
  const receipt = reloadReceiptFromMarker(marker, "applied");
  await storage.set({ [LAST_RELOAD_RECEIPT_STORAGE_KEY]: receipt });
  try {
    await postReloadReceipt(receipt);
  } catch {
    controlLifecycleState = "applied_receipt_pending";
    await recordReloadDiagnostic({
      request_id: marker.request_id,
      target_build_sha256: marker.target_build_sha256,
      state: "applied_receipt_pending",
      failure_code: "receipt_delivery_failed",
    });
    await scheduleReloadReceiptRetry();
    return false;
  }
  await storage.remove([
    PENDING_RELOAD_STORAGE_KEY,
    BLOCKED_RELOAD_TARGET_STORAGE_KEY,
    LAST_RELOAD_RECEIPT_STORAGE_KEY,
  ]);
  reloadPreparing = false;
  controlLifecycleState = "ready";
  await recordReloadDiagnostic({
    request_id: marker.request_id,
    target_build_sha256: marker.target_build_sha256,
    state: "applied",
  });
  return true;
}

async function reconcilePendingReload() {
  if (reloadReconciliationPromise) {
    return reloadReconciliationPromise;
  }
  reloadReconciliationPromise = (async () => {
    const storage = await connectionStorage();
    const stored = await storage.get(PENDING_RELOAD_STORAGE_KEY);
    const marker = stored[PENDING_RELOAD_STORAGE_KEY];
    if (!marker) {
      reloadPreparing = false;
      return true;
    }
    reloadPreparing = true;
    if (!validPendingReloadMarker(marker)) {
      await storage.remove(PENDING_RELOAD_STORAGE_KEY);
      reloadPreparing = false;
      controlLifecycleState = "failed";
      await recordReloadDiagnostic({
        state: "failed",
        failure_code: "invalid_pending_reload_marker",
      });
      return true;
    }
    if (Number(marker.expires_at_ms) <= Date.now()) {
      return failPendingReload(marker, "reload_control_expired");
    }
    if (RUNTIME_INSTANCE_ID === marker.expected_runtime_instance_id) {
      if (marker.reload_attempted) {
        controlLifecycleState = "reloading";
        return false;
      }
      if (marker.target_build_sha256 === BUILD_META.build_sha256) {
        return failPendingReload(marker, "target_build_already_loaded");
      }
      return acceptPendingReload(marker);
    }
    if (
      marker.reload_attempted &&
      marker.target_build_sha256 === BUILD_META.build_sha256
    ) {
      return applyPendingReload(marker);
    }
    return failPendingReload(marker, "target_build_not_loaded");
  })();
  try {
    return await reloadReconciliationPromise;
  } finally {
    reloadReconciliationPromise = null;
  }
}

async function stageReloadControl(control) {
  const storage = await connectionStorage();
  const stored = await storage.get([
    PENDING_RELOAD_STORAGE_KEY,
    LAST_RELOAD_RECEIPT_STORAGE_KEY,
    BLOCKED_RELOAD_TARGET_STORAGE_KEY,
  ]);
  const pending = stored[PENDING_RELOAD_STORAGE_KEY];
  if (pending) {
    if (pending.request_id !== control.request_id) {
      throw controlContractError("another_reload_control_is_pending");
    }
    reloadPreparing = true;
    return reconcilePendingReload();
  }
  const lastReceipt = stored[LAST_RELOAD_RECEIPT_STORAGE_KEY];
  if (
    validReloadReceipt(lastReceipt) &&
    lastReceipt.request_id === control.request_id
  ) {
    controlLifecycleState = lastReceipt.state;
    return false;
  }
  const blocked = stored[BLOCKED_RELOAD_TARGET_STORAGE_KEY];
  let recoveryGeneration = 1;
  if (validBlockedReloadTarget(blocked)) {
    if (blocked.target_build_sha256 === control.target_build_sha256) {
      if (blocked.recovery_generation >= MAX_RELOAD_RECOVERY_GENERATIONS) {
        return failSuppressedReloadControl(
          control,
          blocked,
          "reload_retry_exhausted",
        );
      }
      if (Date.now() < Number(blocked.retry_not_before_ms)) {
        return failSuppressedReloadControl(
          control,
          blocked,
          "reload_retry_cooldown",
        );
      }
      recoveryGeneration = blocked.recovery_generation + 1;
    }
  } else if (blocked) {
    await storage.remove(BLOCKED_RELOAD_TARGET_STORAGE_KEY);
  }
  const marker = {
    schema_version: RELOAD_MARKER_SCHEMA_VERSION,
    protocol_version: CONTROL_PROTOCOL_VERSION,
    request_id: control.request_id,
    action: control.action,
    target_build_sha256: control.target_build_sha256,
    expected_runtime_instance_id: control.expected_runtime_instance_id,
    delivery_generation: control.delivery_generation,
    receipt_token: control.receipt_token,
    expires_at: control.expires_at,
    expires_at_ms: control.expires_at_ms,
    previous_build_identity: currentBuildIdentity(),
    state: "accepting",
    receipt_attempts: 0,
    recovery_generation: recoveryGeneration,
    reload_attempted: false,
    staged_at: new Date().toISOString(),
  };
  await storage.set({ [PENDING_RELOAD_STORAGE_KEY]: marker });
  reloadPreparing = true;
  controlLifecycleState = "accepting";
  return reconcilePendingReload();
}

async function companionRuntimeStatus() {
  const storage = await connectionStorage();
  const stored = await storage.get([
    PENDING_RELOAD_STORAGE_KEY,
    LAST_RELOAD_DIAGNOSTIC_STORAGE_KEY,
  ]);
  const pending = stored[PENDING_RELOAD_STORAGE_KEY];
  return {
    ok: true,
    build_identity: currentBuildIdentity(),
    runtime_instance_id: RUNTIME_INSTANCE_ID,
    control_state: controlLifecycleState,
    active_lease_count: activeLeaseIds.size,
    pending_request_id: validPendingReloadMarker(pending)
      ? pending.request_id
      : null,
    last_reload_diagnostic:
      stored[LAST_RELOAD_DIAGNOSTIC_STORAGE_KEY] || null,
  };
}

function parseLeaseTimestamp(value) {
  const parsed = Date.parse(String(value || ""));
  return Number.isFinite(parsed) ? parsed : null;
}

function dynamicLeaseCompletionReserveMs(durationMs) {
  const normalizedDurationMs = Math.max(0, Number(durationMs) || 0);
  if (normalizedDurationMs <= 0) {
    return 0;
  }
  const shortLeaseFloorMs = Math.min(
    LEASE_COMPLETION_MIN_RESERVE_MS,
    normalizedDurationMs * LEASE_COMPLETION_SHORT_LEASE_MAX_RATIO,
  );
  return Math.round(
    Math.max(
      shortLeaseFloorMs,
      Math.min(
        LEASE_COMPLETION_MAX_RESERVE_MS,
        normalizedDurationMs * LEASE_COMPLETION_RESERVE_RATIO,
      ),
    ),
  );
}

function leaseTiming(lease, receivedAtMs = Date.now()) {
  const configuredDurationMs = Math.max(
    0,
    Number(lease && lease.timeout_seconds || 0) * 1000,
  );
  const claimedAtMs = parseLeaseTimestamp(lease && lease.claimed_at);
  const serverExpiresAtMs = parseLeaseTimestamp(
    lease && lease.lease_expires_at,
  );
  const serverDurationMs =
    claimedAtMs !== null &&
    serverExpiresAtMs !== null &&
    serverExpiresAtMs > claimedAtMs
      ? serverExpiresAtMs - claimedAtMs
      : null;
  // Durable task ownership is renewed by Companion heartbeats in short
  // server-side slices (currently 30s).  That renewable lease is not the
  // provider execution budget carried by timeout_seconds.  Treating it as the
  // work deadline made every real lodging search fail before extraction even
  // though the heartbeat kept ownership valid.  Use the configured execution
  // budget when it is longer, while the server continues to fence completion
  // against the independently renewed ownership lease.
  const renewableServerLease =
    serverDurationMs !== null && configuredDurationMs > serverDurationMs;
  const durationMs = renewableServerLease
    ? configuredDurationMs
    : serverDurationMs === null
      ? configuredDurationMs
      : serverDurationMs;
  const expiresAtMs = renewableServerLease || serverExpiresAtMs === null
    ? receivedAtMs + durationMs
    : serverExpiresAtMs;
  const completionReserveMs = dynamicLeaseCompletionReserveMs(durationMs);
  return {
    deadline_source: renewableServerLease
      ? "renewable_server_lease"
      : serverExpiresAtMs === null
        ? "receipt_fallback"
        : "server_absolute",
    received_at_ms: receivedAtMs,
    claimed_at_ms: claimedAtMs,
    lease_expires_at_ms: expiresAtMs,
    lease_duration_ms: durationMs,
    completion_reserve_ms: completionReserveMs,
    work_deadline_ms: expiresAtMs - completionReserveMs,
  };
}

function leaseDeadline(lease) {
  return leaseTiming(lease).work_deadline_ms;
}

function completionRequestTimeoutMs(timing, nowMs = Date.now()) {
  const remainingLeaseMs = Math.max(
    0,
    timing.lease_expires_at_ms - nowMs,
  );
  const reserveRequestBudgetMs = Math.max(
    250,
    timing.completion_reserve_ms - LEASE_COMPLETION_NETWORK_GUARD_MS,
  );
  return Math.max(
    250,
    Math.min(
      LEASE_COMPLETION_REQUEST_CAP_MS,
      reserveRequestBudgetMs,
      Math.max(250, remainingLeaseMs - LEASE_COMPLETION_NETWORK_GUARD_MS),
    ),
  );
}

function leaseTimingDiagnostic(timing) {
  return {
    deadline_source: timing.deadline_source,
    lease_duration_ms: timing.lease_duration_ms,
    completion_reserve_ms: timing.completion_reserve_ms,
    lease_expires_at: new Date(timing.lease_expires_at_ms).toISOString(),
    work_deadline_at: new Date(timing.work_deadline_ms).toISOString(),
  };
}

function remainingTimeout(deadline, capMs) {
  const remaining = deadline - Date.now();
  if (remaining <= 0) {
    throw new Error("provider task timed out before completion");
  }
  return Math.max(250, Math.min(capMs, remaining));
}

function stageTraceSnapshot(stageTrace) {
  return stageTrace
    .slice(-MAX_STAGE_TRACE_ENTRIES)
    .map((entry) => ({ ...entry }));
}

function appendStageTrace(stageTrace, entry) {
  stageTrace.push({
    sequence: stageTrace.length + 1,
    ...entry,
  });
  if (stageTrace.length > MAX_STAGE_TRACE_ENTRIES) {
    stageTrace.splice(0, stageTrace.length - MAX_STAGE_TRACE_ENTRIES);
  }
}

function releaseInitialLandingSlot() {
  activeInitialLandings = Math.max(0, activeInitialLandings - 1);
  while (
    activeInitialLandings < MAX_CONCURRENT_INITIAL_LANDINGS &&
    initialLandingQueue.length
  ) {
    const queued = initialLandingQueue.shift();
    if (queued && queued.grant()) {
      break;
    }
  }
}

function acquireInitialLandingSlot(deadline) {
  return new Promise((resolve, reject) => {
    let settled = false;
    let timer = null;
    const grant = () => {
      if (settled) {
        return false;
      }
      settled = true;
      clearTimeout(timer);
      activeInitialLandings += 1;
      let released = false;
      resolve(() => {
        if (!released) {
          released = true;
          releaseInitialLandingSlot();
        }
      });
      return true;
    };
    if (activeInitialLandings < MAX_CONCURRENT_INITIAL_LANDINGS) {
      grant();
      return;
    }
    const queued = { grant };
    initialLandingQueue.push(queued);
    const remainingMs = Math.max(0, deadline - Date.now());
    timer = setTimeout(() => {
      if (settled) {
        return;
      }
      settled = true;
      const index = initialLandingQueue.indexOf(queued);
      if (index >= 0) {
        initialLandingQueue.splice(index, 1);
      }
      reject(
        lifecycleError(
          "stage_timeout",
          "initial landing queue exhausted its remaining lease budget",
          {
            stage: "initial_landing_queue",
            budget_ms: remainingMs,
            active_landings: activeInitialLandings,
            queued_landings: initialLandingQueue.length,
            concurrency_limit: MAX_CONCURRENT_INITIAL_LANDINGS,
          },
        ),
      );
    }, remainingMs);
  });
}

async function withInitialLandingSlot(
  stageTrace,
  leaseDeadlineValue,
  operation,
) {
  const startedAt = Date.now();
  let release;
  try {
    release = await acquireInitialLandingSlot(leaseDeadlineValue);
  } catch (error) {
    appendStageTrace(stageTrace, {
      stage: "initial_landing_queue",
      status: "timed_out",
      budget_ms: Math.max(0, leaseDeadlineValue - startedAt),
      elapsed_ms: Math.max(0, Date.now() - startedAt),
      remaining_lease_ms: Math.max(
        0,
        leaseDeadlineValue - Date.now(),
      ),
      failure_code:
        error && typeof error.tripchordCode === "string"
          ? error.tripchordCode
          : "untyped_error",
    });
    throw error;
  }
  try {
    return await operation();
  } finally {
    release();
  }
}

function completionWithStageTrace(
  completion,
  stageTrace,
  timingDiagnostic = null,
) {
  if (!completion || typeof completion !== "object") {
    return completion;
  }
  if (!completion.failure) {
    return {
      state: completion.state,
      quotes: Array.isArray(completion.quotes) ? completion.quotes : [],
    };
  }
  const rawFailureCode = String(
    completion.failure.code || "extraction_error",
  );
  const bridgeFailureCode = BRIDGE_FAILURE_CODES.has(rawFailureCode)
    ? rawFailureCode
    : "extraction_error";
  return {
    state: completion.state,
    quotes: [],
    failure: {
      ...completion.failure,
      code: bridgeFailureCode,
      details: {
        ...(completion.failure.details || {}),
        ...(
          bridgeFailureCode !== rawFailureCode
            ? { diagnostic_code: rawFailureCode }
            : {}
        ),
        ...(timingDiagnostic ? { lease_timing: timingDiagnostic } : {}),
        stage_trace: stageTraceSnapshot(stageTrace),
      },
    },
  };
}

async function withStageBudget(
  stageTrace,
  stage,
  leaseDeadlineValue,
  capMs,
  operation,
) {
  const startedAt = Date.now();
  const effectiveBudgetMs = Math.max(
    0,
    Math.min(capMs, leaseDeadlineValue - startedAt),
  );
  const stageDeadlineValue = startedAt + effectiveBudgetMs;
  let timer = null;
  let operationPromise = null;
  try {
    if (effectiveBudgetMs <= 0) {
      throw lifecycleError(
        "stage_timeout",
        `${stage} had no remaining lease budget`,
        {
          stage,
          budget_ms: 0,
        },
      );
    }
    const timeout = new Promise((_, reject) => {
      timer = setTimeout(
        () =>
          reject(
            lifecycleError(
              "stage_timeout",
              `${stage} exceeded its ${effectiveBudgetMs}ms budget`,
              {
                stage,
                budget_ms: effectiveBudgetMs,
              },
            ),
          ),
        effectiveBudgetMs,
      );
    });
    operationPromise = Promise.resolve().then(
      () => operation(stageDeadlineValue),
    );
    // If the timeout wins, absorb a later Chrome API rejection from the
    // abandoned operation. The owned-tab cleanup remains authoritative.
    operationPromise.catch(() => {});
    const result = await Promise.race([operationPromise, timeout]);
    appendStageTrace(stageTrace, {
      stage,
      status: "completed",
      budget_ms: effectiveBudgetMs,
      elapsed_ms: Math.max(0, Date.now() - startedAt),
      remaining_lease_ms: Math.max(0, leaseDeadlineValue - Date.now()),
      failure_code: null,
    });
    return result;
  } catch (error) {
    const causeCode =
      error && typeof error.tripchordCode === "string"
        ? error.tripchordCode
        : null;
    const timedOut =
      causeCode === "stage_timeout" ||
      causeCode === "tab_interactive_timeout" ||
      causeCode === "navigation_not_observed" ||
      /timed out|timeout/i.test(String(error && error.message || error));
    const elapsedMs = Math.max(0, Date.now() - startedAt);
    const typedError = timedOut && causeCode !== "stage_timeout"
      ? lifecycleError(
        "stage_timeout",
        `${stage} did not complete within its bounded stage budget`,
        {
          ...(
            error &&
            error.tripchordDetails &&
            typeof error.tripchordDetails === "object"
              ? error.tripchordDetails
              : {}
          ),
          stage,
          budget_ms: effectiveBudgetMs,
          elapsed_ms: elapsedMs,
          cause_code: causeCode,
        },
      )
      : error;
    appendStageTrace(stageTrace, {
      stage,
      status: timedOut ? "timed_out" : "failed",
      budget_ms: effectiveBudgetMs,
      elapsed_ms: elapsedMs,
      remaining_lease_ms: Math.max(0, leaseDeadlineValue - Date.now()),
      failure_code:
        typedError && typeof typedError.tripchordCode === "string"
          ? typedError.tripchordCode
          : "untyped_error",
    });
    throw typedError;
  } finally {
    clearTimeout(timer);
  }
}

async function assertLeaseActive(lease, timeoutMs = 5000) {
  const snapshot = await bridgeFetch(
    `/v1/tasks/${encodeURIComponent(lease.task_id)}`,
    { timeoutMs },
  );
  if (
    !snapshot ||
    snapshot.state !== "claimed" ||
    snapshot.claimed_by !== COMPANION_ID
  ) {
    const error = new Error("provider task was cancelled or its lease was replaced");
    error.status = 409;
    throw error;
  }
}

async function hasProviderPermission(provider) {
  const configuredOrigins = PROVIDER_ORIGINS[provider];
  const origins = Array.isArray(configuredOrigins)
    ? configuredOrigins
    : configuredOrigins
      ? [configuredOrigins]
      : [];
  return Boolean(
    origins.length &&
    await chrome.permissions.contains({ origins }),
  );
}

// Scope map mirrors the backend capability profile (tripchord-capability-v1).
// Each provider grants all its declared scopes when the official origins are
// authorised; the heartbeat reports only the scopes actually granted so the
// backend never assumes a provider is available without evidence.
const PROVIDER_SCOPES = {
  ctrip: ["ctrip:flight", "ctrip:lodging"],
  qunar: ["qunar:flight", "qunar:lodging"],
  tongcheng: ["tongcheng:flight", "tongcheng:lodging"],
  fliggy: ["fliggy:flight", "fliggy:lodging"],
  zhixing: ["zhixing:flight", "zhixing:lodging"],
};

async function authorizedScopeKeys() {
  const result = [];
  for (const [provider, scopes] of Object.entries(PROVIDER_SCOPES)) {
    if (await hasProviderPermission(provider)) {
      result.push(...scopes);
    }
  }
  return result;
}

function waitForTabComplete(tabId, timeoutMs) {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => {
      chrome.tabs.onUpdated.removeListener(listener);
      reject(new Error("provider page navigation timed out"));
    }, timeoutMs);
    const listener = (updatedId, changeInfo, tab) => {
      if (updatedId === tabId && changeInfo.status === "complete") {
        clearTimeout(timer);
        chrome.tabs.onUpdated.removeListener(listener);
        resolve(tab);
      }
    };
    chrome.tabs.onUpdated.addListener(listener);
    chrome.tabs.get(tabId)
      .then((tab) => {
        if (tab.status === "complete") {
          clearTimeout(timer);
          chrome.tabs.onUpdated.removeListener(listener);
          resolve(tab);
        }
      })
      .catch((error) => {
        clearTimeout(timer);
        chrome.tabs.onUpdated.removeListener(listener);
        reject(error);
      });
  });
}

function readDocumentReadyState() {
  return document.readyState;
}

async function probeDocumentReadyState(
  tabId,
  timeoutMs = READY_STATE_PROBE_CAP_MS,
) {
  let timer = null;
  const probe = chrome.scripting.executeScript({
    target: { tabId },
    func: readDocumentReadyState,
  });
  probe.catch(() => {});
  try {
    const timeout = new Promise((_, reject) => {
      timer = setTimeout(
        () => reject(new Error("document ready-state probe timed out")),
        Math.max(1, Number(timeoutMs) || 1),
      );
    });
    const results = await Promise.race([probe, timeout]);
    return Array.isArray(results) && results[0]
      ? results[0].result
      : null;
  } finally {
    clearTimeout(timer);
  }
}

async function waitForTabInteractive(tabId, timeoutMs) {
  const probeDeadline = Date.now() + Math.max(0, Number(timeoutMs) || 0);
  let lastTab = null;
  let lastReadyState = null;
  let lastProbeError = null;
  do {
    lastTab = await chrome.tabs.get(tabId);
    if (lastTab.status === "complete") {
      return {
        tab: lastTab,
        mode: "tab_complete",
        document_ready_state: "complete",
      };
    }
    try {
      lastReadyState = await probeDocumentReadyState(
        tabId,
        Math.min(
          READY_STATE_PROBE_CAP_MS,
          Math.max(1, probeDeadline - Date.now()),
        ),
      );
      lastProbeError = null;
    } catch (error) {
      lastProbeError = String(error && error.message || error).slice(0, 200);
    }
    if (
      lastReadyState === "interactive" ||
      lastReadyState === "complete"
    ) {
      return {
        tab: lastTab,
        mode: "document_interactive",
        document_ready_state: lastReadyState,
      };
    }
    const waitMs = Math.min(
      TAB_INTERACTIVE_POLL_INTERVAL_MS,
      Math.max(0, probeDeadline - Date.now()),
    );
    if (waitMs > 0) {
      await delay(waitMs);
    }
  } while (Date.now() < probeDeadline);
  throw lifecycleError(
    "tab_interactive_timeout",
    "provider landing page did not become interactive",
    {
      stage: "initial_landing",
      tab_status:
        lastTab && typeof lastTab.status === "string"
          ? lastTab.status
          : null,
      document_ready_state:
        typeof lastReadyState === "string" ? lastReadyState : null,
      probe_error: lastProbeError,
    },
  );
}

async function waitForExactTabUrl(tabId, expectedUrl, timeoutMs) {
  const deadline = Date.now() + Math.max(0, Number(timeoutMs) || 0);
  let last = null;
  do {
    const tab = await chrome.tabs.get(tabId);
    last = String(tab.pendingUrl || tab.url || "");
    if (last === expectedUrl) {
      return tab;
    }
    const waitMs = Math.min(
      TAB_INTERACTIVE_POLL_INTERVAL_MS,
      Math.max(0, deadline - Date.now()),
    );
    if (waitMs > 0) await delay(waitMs);
  } while (Date.now() < deadline);
  throw lifecycleError(
    "tab_exact_url_timeout",
    "provider fallback did not reach the exact audited result URL",
    {
      stage: "tongcheng_lodging_fallback_navigation",
      expected: navigationUrlEvidence(expectedUrl),
      observed: navigationUrlEvidence(last || ""),
    },
  );
}

function observeTrustedProviderNavigation({
  provider,
  kind,
  sourceTabId,
  previousUrl,
  ownedTabIds,
  timeoutMs,
}) {
  let finished = false;
  let outcome = null;
  const observedNavigation = new Set();
  const navigationTrace = [];
  appendNavigationTrace(navigationTrace, {
    phase: "observer_started",
    tabRole: "source",
    status: null,
    rawUrl: previousUrl,
  });
  let resolvePromise;
  let rejectPromise;
  const promise = new Promise((resolve, reject) => {
    resolvePromise = resolve;
    rejectPromise = reject;
  });
  // Some callers receive the content-port failure before awaiting navigation.
  // Attach a handler immediately without changing the original promise.
  promise.catch(() => {});

  const cleanup = () => {
    clearTimeout(timer);
    chrome.tabs.onCreated.removeListener(createdListener);
    chrome.tabs.onUpdated.removeListener(updatedListener);
    chrome.tabs.onRemoved.removeListener(removedListener);
  };
  const rejectWith = (error) => {
    if (finished) {
      return;
    }
    finished = true;
    outcome = { error };
    cleanup();
    rejectPromise(error);
  };
  const resolveWith = (transition) => {
    if (finished) {
      return;
    }
    finished = true;
    outcome = { transition };
    cleanup();
    resolvePromise(transition);
  };
  const inspect = (tabId, changeInfo = {}, tab = {}, eventType = "tab_updated") => {
    if (!ownedTabIds.has(tabId) || finished) {
      return;
    }
    const rawUrl =
      changeInfo.url ||
      tab.pendingUrl ||
      tab.url ||
      "";
    const isOpenerTab = tabId !== sourceTabId;
    const phase = eventType === "tab_created"
      ? "tab_created"
      : changeInfo.status === "loading"
        ? "navigation_loading"
        : changeInfo.status === "complete"
          ? "navigation_complete"
          : changeInfo.url
            ? "url_changed"
            : "tab_updated";
    appendNavigationTrace(navigationTrace, {
      phase,
      tabRole: isOpenerTab ? "opener_child" : "source",
      status: changeInfo.status || tab.status || null,
      rawUrl,
    });
    if (
      isOpenerTab ||
      changeInfo.status === "loading" ||
      (rawUrl && rawUrl !== previousUrl)
    ) {
      observedNavigation.add(tabId);
    }
    if (!isTransientNavigationUrl(rawUrl)) {
      if (auditedProviderLoginRedirect(provider, kind, rawUrl)) {
        rejectWith(
          lifecycleError(
            "login_required",
            `${provider} 酒店结果页要求用户重新登录`,
            navigationDiagnostic({
              provider,
              kind,
              stage: "observe_navigation",
              reason: "audited_login_redirect",
              rawUrl,
              trace: navigationTrace,
            }),
          ),
        );
        return;
      }
      const hostDecision = providerHostDecision(provider, rawUrl);
      if (!hostDecision.allowed) {
        rejectWith(
          lifecycleError(
            "navigation_error",
            `provider navigation left the allowed ${provider} host`,
            navigationDiagnostic({
              provider,
              kind,
              stage: "observe_navigation",
              reason: hostDecision.reason,
              rawUrl,
              trace: navigationTrace,
            }),
          ),
        );
        return;
      }
      const verticalDecision = providerVerticalDecision(provider, kind, rawUrl);
      if (!verticalDecision.allowed) {
        rejectWith(
          lifecycleError(
            "navigation_error",
            `provider navigation reached the wrong ${provider}/${kind} vertical`,
            navigationDiagnostic({
              provider,
              kind,
              stage: "observe_navigation",
              reason: verticalDecision.reason,
              rawUrl,
              trace: navigationTrace,
            }),
          ),
        );
        return;
      }
    }
    if (
      changeInfo.status === "complete" &&
      observedNavigation.has(tabId) &&
      !isTransientNavigationUrl(rawUrl) &&
      providerVerticalUrlAllowed(provider, kind, rawUrl)
    ) {
      resolveWith({
        mode: isOpenerTab ? "opener_tab_navigation" : "navigation",
        tabId,
        url: rawUrl,
        navigation_trace: navigationTraceSnapshot(navigationTrace),
      });
    }
  };
  const createdListener = (tab) => {
    if (
      tab &&
      Number.isInteger(tab.id) &&
      Number.isInteger(tab.openerTabId) &&
      ownedTabIds.has(tab.openerTabId)
    ) {
      ownedTabIds.add(tab.id);
      inspect(
        tab.id,
        { status: tab.status, url: tab.pendingUrl || tab.url },
        tab,
        "tab_created",
      );
    }
  };
  const updatedListener = (tabId, changeInfo, tab) => {
    inspect(tabId, changeInfo, tab);
  };
  const removedListener = (tabId) => {
    if (!ownedTabIds.has(tabId)) {
      return;
    }
    ownedTabIds.delete(tabId);
    if (tabId === sourceTabId && ownedTabIds.size === 0) {
      appendNavigationTrace(navigationTrace, {
        phase: "tab_removed",
        tabRole: "source",
      });
      rejectWith(
        lifecycleError(
          "navigation_error",
          "provider source tab closed during navigation",
          navigationDiagnostic({
            provider,
            kind,
            stage: "observe_navigation",
            reason: "source_tab_closed",
            trace: navigationTrace,
          }),
        ),
      );
    }
  };
  chrome.tabs.onCreated.addListener(createdListener);
  chrome.tabs.onUpdated.addListener(updatedListener);
  chrome.tabs.onRemoved.addListener(removedListener);
  const timer = setTimeout(
    () =>
      rejectWith(
        lifecycleError(
          "navigation_not_observed",
          "trusted provider navigation was not observed",
          navigationDiagnostic({
            provider,
            kind,
            stage: "observe_navigation",
            reason: "navigation_not_observed",
            trace: navigationTrace,
          }),
        ),
      ),
    timeoutMs,
  );

  return {
    promise,
    peek: () => outcome,
    trace: () => navigationTraceSnapshot(navigationTrace),
    cancel: () => {
      if (!finished) {
        finished = true;
        cleanup();
      }
    },
  };
}

async function waitForSearchTransition(
  observer,
  tabId,
  previousUrl,
  timeoutMs,
) {
  let fallbackTimer;
  const fallback = new Promise((resolve) => {
    fallbackTimer = setTimeout(
      () =>
        resolve({
          mode: "spa_or_delayed_navigation",
          tabId,
          url: previousUrl,
        }),
      Math.min(SEARCH_TRANSITION_GRACE_MS, timeoutMs),
    );
  });
  try {
    const transition = await Promise.race([observer.promise, fallback]);
    if (transition.mode === "spa_or_delayed_navigation") {
      observer.cancel();
    }
    return transition;
  } finally {
    clearTimeout(fallbackTimer);
  }
}

function tripchordContentRuntimePresent(expectedVersion) {
  return Boolean(
    globalThis.TripChordQuoteParser &&
    globalThis.TripChordVisibleSearchDriver &&
    globalThis.TripChordContentRuntimeVersion === expectedVersion,
  );
}

async function installContent(tabId) {
  try {
    const probe = await chrome.scripting.executeScript({
      target: { tabId },
      func: tripchordContentRuntimePresent,
      args: [CONTENT_RUNTIME_VERSION],
    });
    if (Array.isArray(probe) && probe[0] && probe[0].result === true) {
      return;
    }
  } catch {
    // A freshly navigated provider page may reject the probe until its
    // document is ready. The normal audited injection remains the fallback.
  }
  await chrome.scripting.executeScript({
    target: { tabId },
    files: ["src/parser.js", "src/content.js"],
  });
}

async function contentCall(tabId, message, timeoutMs = 15000) {
  let timer;
  try {
    const timeout = new Promise((_, reject) => {
      timer = setTimeout(
        () => reject(new Error("content command timed out")),
        timeoutMs,
      );
    });
    const response = await Promise.race([
      chrome.tabs.sendMessage(tabId, message),
      timeout,
    ]);
    if (!response || response.ok !== true) {
      throw new Error(response && response.error || "content command failed");
    }
    return response.result;
  } finally {
    clearTimeout(timer);
  }
}

function delay(milliseconds) {
  return new Promise((resolve) => setTimeout(resolve, milliseconds));
}

function assertVisibleContentUrl(tab, provider, kind) {
  const rawUrl = tab && (tab.pendingUrl || tab.url) || "";
  const hostDecision = providerHostDecision(provider, rawUrl);
  if (!hostDecision.allowed) {
    throw lifecycleError(
      "navigation_error",
      `visible content call left the allowed ${provider} host`,
      navigationDiagnostic({
        provider,
        kind,
        stage: "visible_content_call",
        reason: hostDecision.reason,
        rawUrl,
      }),
    );
  }
  const verticalDecision = providerVerticalDecision(provider, kind, rawUrl);
  if (!verticalDecision.allowed) {
    throw lifecycleError(
      "navigation_error",
      `visible content call reached the wrong ${provider}/${kind} vertical`,
      navigationDiagnostic({
        provider,
        kind,
        stage: "visible_content_call",
        reason: verticalDecision.reason,
        rawUrl,
      }),
    );
  }
}

function enqueueVisibleInteraction(operation) {
  const run = visibleInteractionTail
    .catch(() => undefined)
    .then(operation);
  visibleInteractionTail = run.then(
    () => undefined,
    () => undefined,
  );
  return run;
}

function withVisibleTab(
  {
    tabId,
    provider,
    kind,
    lease,
    deadline,
    ownedTabIds,
  },
  operation,
) {
  return enqueueVisibleInteraction(async () => {
    remainingTimeout(deadline, 15000);
    await assertLeaseActive(
      lease,
      remainingTimeout(deadline, 5000),
    );
    remainingTimeout(deadline, 15000);
    const targetTab = await chrome.tabs.get(tabId);
    const targetUrl = targetTab && (targetTab.pendingUrl || targetTab.url) || "";
    if (auditedProviderLoginRedirect(provider, kind, targetUrl)) {
      throw lifecycleError(
        "login_required",
        `${provider} ${kind} 结果页要求用户重新登录`,
        navigationDiagnostic({
          provider,
          kind,
          stage: "background_content_call",
          reason: "audited_login_redirect",
          rawUrl: targetUrl,
        }),
      );
    }
    assertVisibleContentUrl(targetTab, provider, kind);
    // Read-only searches must never steal the user's foreground. Content
    // scripts can inspect and interact with a fully loaded background tab;
    // `document.hidden` is expected for that execution mode and is not an
    // extraction failure. Provider-specific failures still fail closed at the
    // URL, DOM, login and quote-evidence boundaries below.
    const result = await operation();
    const currentTab = await chrome.tabs.get(tabId);
    const currentUrl = currentTab && (currentTab.pendingUrl || currentTab.url) || "";
    if (!auditedProviderLoginRedirect(provider, kind, currentUrl)) {
      assertVisibleContentUrl(currentTab, provider, kind);
    }
    return result;
  });
}

async function visibleContentCall(
  tabId,
  message,
  {
    lease,
    deadline,
    ownedTabIds,
    timeoutCapMs = 15000,
    postSettleMs = 0,
    beforeSend = null,
    returnInteraction = false,
  },
) {
  if (
    !message ||
    !VISIBLE_CONTENT_MESSAGE_TYPES.has(message.type)
  ) {
    throw new Error("content message is not allowed in the visible interaction slot");
  }
  const interaction = await withVisibleTab(
    {
      tabId,
      provider: lease.provider,
      kind: lease.kind,
      lease,
      deadline,
      ownedTabIds,
    },
    async () => {
      let context = null;
      let result = null;
      let error = null;
      try {
        context = beforeSend ? await beforeSend() : null;
        result = await contentCall(
          tabId,
          message,
          remainingTimeout(deadline, timeoutCapMs),
        );
      } catch (caught) {
        error = caught;
      } finally {
        const waitMs = Math.min(
          postSettleMs,
          Math.max(0, deadline - Date.now()),
        );
        if (waitMs > 0) {
          await delay(waitMs);
        }
      }
      return { context, result, error };
    },
  );
  if (returnInteraction) {
    return interaction;
  }
  if (interaction.error) {
    throw interaction.error;
  }
  return interaction.result;
}

async function visibleDetailExtractionWithRetry(
  tabId,
  message,
  {
    lease,
    deadline,
    ownedTabIds,
    timeoutCapMs = CTRIP_LODGING_DETAIL_EXTRACT_CAP_MS,
    attempts = 3,
    retrySettleMs = 1800,
  },
) {
  let lastExtraction = null;
  for (let attempt = 0; attempt < attempts; attempt += 1) {
    if (deadline - Date.now() < 2500) {
      break;
    }
    if (attempt > 0) {
      await delay(Math.min(retrySettleMs, Math.max(0, deadline - Date.now())));
    }
    await installContent(tabId);
    lastExtraction = await visibleContentCall(
      tabId,
      message,
      {
        lease,
        deadline,
        ownedTabIds,
        timeoutCapMs,
      },
    );
    if (
      lastExtraction &&
      lastExtraction.state === "succeeded" &&
      Array.isArray(lastExtraction.quotes) &&
      lastExtraction.quotes.length > 0
    ) {
      return lastExtraction;
    }
    const failureCode = lastExtraction && lastExtraction.failure &&
      lastExtraction.failure.code;
    if (failureCode && failureCode !== "dom_drift") {
      break;
    }
  }
  return lastExtraction;
}

function isCtripLodgingListPageUrl(rawUrl) {
  if (!providerVerticalUrlAllowed("ctrip", "lodging", rawUrl)) {
    return false;
  }
  const parsed = new URL(rawUrl);
  return (
    parsed.hostname.toLowerCase() === "hotels.ctrip.com" &&
    parsed.pathname.toLowerCase().replace(/\/+$/, "") === "/hotels/list"
  );
}

function isFliggyLodgingListPageUrl(rawUrl) {
  if (!providerVerticalUrlAllowed("fliggy", "lodging", rawUrl)) {
    return false;
  }
  const parsed = new URL(rawUrl);
  return (
    parsed.hostname.toLowerCase() === "hotel.fliggy.com" &&
    parsed.pathname.toLowerCase().replace(/\/+$/, "") ===
      "/hotel_list3.htm"
  );
}

// Serialized into Fliggy's MAIN world. It only reads visible hotel-detail
// anchors; transactional booking links are deliberately excluded.
function fliggyCaptureVisibleLodgingDetailUrls(maxControls = 6) {
  const limit = Math.max(0, Math.min(6, Number(maxControls) || 0));
  const visible = (node) => {
    if (!node) return false;
    const style = window.getComputedStyle(node);
    const rect = node.getBoundingClientRect();
    return Boolean(
      !node.hidden &&
      !node.inert &&
      style.display !== "none" &&
      style.visibility !== "hidden" &&
      Number(style.opacity) !== 0 &&
      rect.width > 0 &&
      rect.height > 0
    );
  };
  const compact = (node, max = 240) =>
    String(node && (node.innerText || node.textContent) || "")
      .replace(/\s+/g, " ")
      .trim()
      .slice(0, max);
  const captures = [];
  const seen = new Set();
  for (const link of document.querySelectorAll("a[href]")) {
    if (!visible(link)) continue;
    let parsed;
    try {
      parsed = new URL(link.getAttribute("href") || "", location.href);
    } catch {
      continue;
    }
    if (
      parsed.hostname.toLowerCase() !== "hotel.fliggy.com" ||
      parsed.pathname.toLowerCase().replace(/\/+$/, "") !==
        "/hotel_detail2.htm" ||
      seen.has(parsed.href)
    ) {
      continue;
    }
    let card = link;
    let depth = 0;
    while (card.parentElement && depth < 8) {
      const parent = card.parentElement;
      const text = compact(parent, 1200);
      if (
        ["LI", "ARTICLE", "SECTION"].includes(parent.tagName) ||
        (
          text.length <= 1200 &&
          /马富施|马富士|maafushi/i.test(text) &&
          /(?:RMB|CNY|¥|￥)\s*\d+/i.test(text)
        )
      ) {
        card = parent;
      }
      if (["LI", "ARTICLE", "SECTION"].includes(parent.tagName)) {
        break;
      }
      depth += 1;
    }
    const cardText = compact(card, 800);
    const title = compact(link, 180) ||
      compact(card.querySelector("h1,h2,h3,h4,[class*='name'],[class*='Name']"), 180);
    if (!title || !/马富施|马富士|maafushi/i.test(cardText)) {
      continue;
    }
    seen.add(parsed.href);
    captures.push({
      raw_url: parsed.href,
      property_name: title,
      location_evidence: cardText,
    });
    if (captures.length >= limit) break;
  }
  return {
    capture_code: captures.length ? "captured_visible_detail_urls" : "detail_url_not_found",
    controls_seen: captures.length,
    captures,
  };
}

// Serialized into Tongcheng's MAIN world. Hotel list cards use a Vue click
// handler rather than an anchor, so window.open is intercepted and no popup is
// allowed to reach Chrome while the provider-owned detail URL is captured.
async function tongchengCaptureVisibleLodgingDetailUrls(
  candidateIndices = [],
  maxControls = 3,
) {
  const limit = Math.max(0, Math.min(3, Number(maxControls) || 0));
  const visible = (node) => {
    // Background tabs may report zero geometry and incomplete computed style.
    // Hidden/inert DOM state is the only reliable exclusion here; the strict
    // hotel-card text contract and intercepted provider URL remain the safety
    // boundary.
    return Boolean(node && !node.hidden && !node.inert);
  };
  const compact = (node, max = 300) =>
    String(node && (node.textContent || node.innerText) || "")
      .replace(/\s+/g, " ").trim().slice(0, max);
  const allListItems = [...new Set(document.querySelectorAll(
    "li,[data-hotelid],[data-hotel-id]," +
    "[class*='hotel-item' i],[class*='hotel_card' i],[class*='hotel-card' i]",
  ))];
  const cards = [...new Set(
    (Array.isArray(candidateIndices) ? candidateIndices : [])
      .filter((index) => Number.isInteger(index) && index >= 0)
      .map((index) => allListItems[index])
      .filter((node) => visible(node)),
  )]
    .slice(0, limit);
  const captures = [];
  let activeIndex = null;
  const record = (rawUrl) => {
    if (rawUrl === undefined || rawUrl === null || !String(rawUrl).trim()) return;
    captures.push({
      control_index: activeIndex,
      raw_url: String(rawUrl),
      property_name:
        compact(cards[activeIndex]?.querySelector(
          "h1,h2,h3,h4,[class*='hotelName'],[class*='hotel-name'],[class*='name']",
        ), 180) || compact(cards[activeIndex], 180),
      preview_text: compact(cards[activeIndex], 600),
    });
  };
  const originalOpen = window.open;
  let patched = false;
  try {
    window.open = (rawUrl) => {
      record(rawUrl);
      return { blur() {}, close() {}, focus() {}, closed: false };
    };
    patched = window.open !== originalOpen;
    if (!patched) {
      return {
        capture_code: "window_open_interception_failed",
        li_count: allListItems.length,
        candidate_samples: [],
        captures: [],
      };
    }
    cards.forEach((card, index) => {
      activeIndex = index;
      for (const anchor of card.querySelectorAll("a[href]")) {
        const href = anchor.href || anchor.getAttribute("href");
        if (href) record(href);
      }
      try {
        card.click();
      } catch {
        // A missing provider click handler is reported by the empty capture.
      }
    });
    await new Promise((resolve) => setTimeout(resolve, 250));
    return {
      capture_code: captures.length ? "captured" : "detail_url_not_observed",
      li_count: allListItems.length,
      controls_seen: cards.length,
      candidate_samples: cards.slice(0, 3).map((card) => compact(card, 300)),
      captures,
    };
  } finally {
    if (patched) window.open = originalOpen;
  }
}

function tongchengLodgingDetailUrlDecision(rawUrl, listPageUrl, query = {}) {
  const rejected = (reason, parsed = null) => ({
    allowed: false,
    reason,
    url: parsed ? navigationUrlEvidence(parsed.href) : navigationUrlEvidence(""),
  });
  if (!tongchengLodgingResultUrlDecision(listPageUrl, query).allowed) {
    return rejected("invalid_list_surface");
  }
  let parsed;
  try {
    parsed = new URL(String(rawUrl || "").trim(), listPageUrl);
  } catch {
    return rejected("invalid_detail_url");
  }
  const detailHost = parsed.hostname.toLowerCase();
  const detailPath = parsed.pathname.toLowerCase().replace(/\/+$/, "");
  const exactDetailSurface =
    (detailHost === "www.ly.com" && detailPath === "/hotel/hoteldetail") ||
    (detailHost === "m.ly.com" && detailPath === "/hotel/hoteldetail") ||
    (detailHost === "m.elong.com" && detailPath === "/ihotel/hoteldetail");
  if (
    parsed.protocol !== "https:" ||
    !exactDetailSurface ||
    parsed.port || parsed.username || parsed.password || parsed.hash
  ) {
    return rejected("detail_surface_rejected", parsed);
  }
  const entries = [...parsed.searchParams.entries()];
  const values = (name) => entries
    .filter(([key]) => key.toLowerCase() === name.toLowerCase())
    .map(([, value]) => value);
  const hotelIds = values("hotelId");
  const allowedKeys = new Set([
    "hotelid", "indate", "outdate", "tracetoken",
    "cityname", "countryname", "intl", "adultsnumber",
    "beforeprice", "cury", "listhotelminpriceexcltax", "prc",
    "productlabeltype200", "isfirst",
  ]);
  const optionalSingle = (name, validator = () => true) => {
    const found = values(name);
    return found.length <= 1 && (found.length === 0 || validator(found[0]));
  };
  const boundedText = (value) => value.length > 0 && value.length <= 1024;
  const nonNegativePrice = (value) => /^\d+(?:\.\d{1,2})?$/.test(value);
  if (
    hotelIds.length !== 1 || !/^[1-9]\d*$/.test(hotelIds[0]) ||
    values("inDate").length !== 1 || values("inDate")[0] !== query.start_date ||
    values("outDate").length !== 1 || values("outDate")[0] !== query.end_date ||
    values("intl").length !== 1 || values("intl")[0] !== "1" ||
    values("adultsNumber").length !== 1 ||
    values("adultsNumber")[0] !== String(query.adults) ||
    !optionalSingle("traceToken", boundedText) ||
    !optionalSingle("cityName", boundedText) ||
    !optionalSingle("countryName", boundedText) ||
    !optionalSingle("cury", (value) => /^[A-Za-z0-9_-]{1,16}$/.test(value)) ||
    !optionalSingle("beforePrice", nonNegativePrice) ||
    !optionalSingle("listHotelMinPriceExclTax", nonNegativePrice) ||
    !optionalSingle("prc", nonNegativePrice) ||
    !optionalSingle("productLabelType200", (value) => value.length <= 120) ||
    !optionalSingle("isFirst", (value) => /^(?:0|1|true|false)$/i.test(value)) ||
    entries.some(([key]) => !allowedKeys.has(key.toLowerCase()))
  ) {
    return rejected("detail_query_contract_mismatch", parsed);
  }
  return {
    allowed: true,
    reason: "allowed",
    href: parsed.href,
    property_id: hotelIds[0],
    url: navigationUrlEvidence(parsed.href),
  };
}

async function captureTongchengLodgingDetailTargets(
  listTabId,
  lease,
  deadline,
  ownedTabIds,
) {
  const current = await chrome.tabs.get(listTabId);
  const listPageUrl = current.url || current.pendingUrl || "";
  if (!tongchengLodgingResultUrlDecision(listPageUrl, lease.query).allowed) {
    throw lifecycleError(
      "lodging_detail_wrong_surface",
      "Tongcheng lodging detail capture requires the exact lodging list surface",
      { stage: "capture_detail_targets", reason: "invalid_list_surface" },
    );
  }
  const candidateMessage = {
    type: "tripchord:tongcheng-detail-candidates",
    provider: lease.provider,
    kind: lease.kind,
  };
  let candidateScan = await contentCall(
    listTabId,
    candidateMessage,
    remainingTimeout(deadline, 10000),
  );
  const mobileTongchengSurface = (() => {
    try {
      const parsed = new URL(listPageUrl);
      return ["m.elong.com", "m.ly.com"].includes(
        parsed.hostname.toLowerCase(),
      );
    } catch {
      return false;
    }
  })();
  for (let poll = 0; poll < 10; poll += 1) {
    const indices = candidateScan && Array.isArray(candidateScan.indices)
      ? candidateScan.indices
      : [];
    const sample = String(candidateScan && candidateScan.document_sample || "");
    if (!mobileTongchengSurface || indices.length || !sample.includes("正在加载")) {
      break;
    }
    if (remainingTimeout(deadline, 2500) < 2000) break;
    await delay(2000);
    candidateScan = await contentCall(
      listTabId,
      candidateMessage,
      remainingTimeout(deadline, 10000),
    );
  }
  const candidateIndices =
    candidateScan && Array.isArray(candidateScan.indices)
      ? candidateScan.indices
      : [];
  const execution = await withVisibleTab(
    {
      tabId: listTabId,
      provider: lease.provider,
      kind: lease.kind,
      lease,
      deadline,
      ownedTabIds,
    },
    () => chrome.scripting.executeScript({
      target: { tabId: listTabId },
      world: "MAIN",
      func: tongchengCaptureVisibleLodgingDetailUrls,
      args: [candidateIndices, 3],
    }),
  );
  const raw = Array.isArray(execution) && execution[0] && execution[0].result
    ? execution[0].result
    : { capture_code: "capture_result_missing", captures: [] };
  const targets = [];
  const rejections = [];
  const seen = new Set();
  for (const candidate of Array.isArray(raw.captures) ? raw.captures : []) {
    const decision = tongchengLodgingDetailUrlDecision(
      candidate.raw_url,
      listPageUrl,
      lease.query,
    );
    if (!decision.allowed || seen.has(decision.href)) {
      rejections.push({ reason: decision.reason, url: decision.url });
      continue;
    }
    seen.add(decision.href);
    targets.push({
      ...decision,
      property_name: String(candidate.property_name || "").slice(0, 180),
      preview_text: String(candidate.preview_text || "").slice(0, 600),
    });
  }
  return {
    capture_code: raw.capture_code,
    li_count: Number(raw.li_count) || 0,
    controls_seen: Number(raw.controls_seen) || 0,
    candidate_samples:
      candidateScan && Array.isArray(candidateScan.samples)
        ? candidateScan.samples.map((value) => String(value).slice(0, 300)).slice(0, 3)
        : [],
    candidate_runtime_version:
      String(candidateScan && candidateScan.runtime_version || "").slice(0, 40),
    candidate_scan_samples:
      candidateScan && Array.isArray(candidateScan.scan_samples)
        ? candidateScan.scan_samples.slice(0, 8).map((item) => ({
          index: Number(item && item.index),
          sample: String(item && item.sample || "").slice(0, 300),
        }))
        : [],
    document_title: String(candidateScan && candidateScan.document_title || "").slice(0, 200),
    document_sample: String(candidateScan && candidateScan.document_sample || "").slice(0, 1200),
    element_count: Number(candidateScan && candidateScan.element_count) || 0,
    list_page_url: listPageUrl,
    targets,
    rejections,
  };
}

function fliggyLodgingDetailUrlDecision(rawUrl, listPageUrl, query = {}) {
  const rejected = (reason, parsed = null) => ({
    allowed: false,
    reason,
    url: parsed ? navigationUrlEvidence(parsed.href) : navigationUrlEvidence(""),
  });
  if (!isFliggyLodgingListPageUrl(listPageUrl)) {
    return rejected("invalid_list_surface");
  }
  let parsed;
  try {
    parsed = new URL(String(rawUrl || "").trim(), listPageUrl);
  } catch {
    return rejected("invalid_detail_url");
  }
  if (
    parsed.protocol !== "https:" ||
    parsed.hostname.toLowerCase() !== "hotel.fliggy.com" ||
    parsed.port ||
    parsed.username ||
    parsed.password ||
    parsed.pathname.toLowerCase().replace(/\/+$/, "") !==
      "/hotel_detail2.htm" ||
    parsed.hash
  ) {
    return rejected("detail_surface_rejected", parsed);
  }
  const entries = [...parsed.searchParams.entries()];
  const exact = (name, expected) => {
    const values = entries.filter(([key]) => key === name);
    return values.length === 1 && values[0][1] === expected;
  };
  const adults = Number(query.adults);
  const rooms = Number(query.rooms);
  if (
    !Number.isInteger(adults) || adults <= 0 ||
    !Number.isInteger(rooms) || rooms <= 0 ||
    !exact("checkIn", query.start_date) ||
    !exact("checkOut", query.end_date) ||
    !exact("aNum_1", String(adults)) ||
    !exact("cNum_1", "0") ||
    !exact("roomNum", String(rooms)) ||
    !exact("city", "933081")
  ) {
    return rejected("detail_query_contract_mismatch", parsed);
  }
  const propertyIds = entries.filter(([name]) => name === "shid");
  if (
    propertyIds.length !== 1 ||
    !/^[1-9]\d*$/.test(propertyIds[0][1])
  ) {
    return rejected("detail_property_id_invalid", parsed);
  }
  const forbidden = /(?:cashier|coupon|order|payment|paynow|booking|booknow|redirect|returnurl|callback)/i;
  if (
    entries.some(([name, value]) =>
      (name !== "checkOut" && forbidden.test(name.replace(/[^a-z0-9]/gi, ""))) ||
      (/^(?:https?:)?\/\//i.test(String(value).trim()) && /url|target|return|redirect|callback/i.test(name))
    )
  ) {
    return rejected("detail_transaction_marker", parsed);
  }
  return {
    allowed: true,
    reason: "allowed",
    href: parsed.href,
    property_id: propertyIds[0][1],
    url: navigationUrlEvidence(parsed.href),
  };
}

function auditedFliggyLodgingDetailCandidate(rawUrl, listPageUrl, query = {}) {
  const direct = fliggyLodgingDetailUrlDecision(rawUrl, listPageUrl, query);
  if (direct.allowed || direct.reason !== "detail_query_contract_mismatch") {
    return { ...direct, augmented_from_visible_property_link: false };
  }
  let parsed;
  try {
    parsed = new URL(String(rawUrl || "").trim(), listPageUrl);
  } catch {
    return direct;
  }
  if (
    parsed.protocol !== "https:" ||
    parsed.hostname.toLowerCase() !== "hotel.fliggy.com" ||
    parsed.port || parsed.username || parsed.password || parsed.hash ||
    parsed.pathname.toLowerCase().replace(/\/+$/, "") !==
      "/hotel_detail2.htm"
  ) {
    return direct;
  }
  const entries = [...parsed.searchParams.entries()];
  const propertyIds = entries.filter(([name]) => name === "shid");
  if (
    propertyIds.length !== 1 ||
    !/^[1-9]\d*$/.test(propertyIds[0][1])
  ) {
    return direct;
  }
  const adults = Number(query.adults);
  const rooms = Number(query.rooms);
  const required = new Map([
    ["checkIn", String(query.start_date || "")],
    ["checkOut", String(query.end_date || "")],
    ["aNum_1", String(adults)],
    ["cNum_1", "0"],
    ["roomNum", String(rooms)],
    ["city", "933081"],
  ]);
  if (
    calendarDateQueryValue(query.start_date) === null ||
    calendarDateQueryValue(query.end_date) === null ||
    String(query.end_date) <= String(query.start_date) ||
    !Number.isInteger(adults) || adults <= 0 ||
    !Number.isInteger(rooms) || rooms <= 0
  ) {
    return direct;
  }
  for (const [name, expected] of required) {
    const values = entries.filter(([candidate]) => candidate === name);
    if (
      values.length > 1 ||
      (values.length === 1 && values[0][1] !== expected)
    ) {
      return direct;
    }
    parsed.searchParams.set(name, expected);
  }
  const augmented = fliggyLodgingDetailUrlDecision(
    parsed.href,
    listPageUrl,
    query,
  );
  return augmented.allowed
    ? { ...augmented, augmented_from_visible_property_link: true }
    : augmented;
}

async function captureFliggyLodgingDetailTargets(
  listTabId,
  lease,
  deadline,
  ownedTabIds,
) {
  const current = await chrome.tabs.get(listTabId);
  const listPageUrl = current.url || current.pendingUrl || "";
  if (!isFliggyLodgingListPageUrl(listPageUrl)) {
    throw lifecycleError(
      "lodging_detail_wrong_surface",
      "Fliggy lodging detail capture requires the exact lodging list surface",
      { stage: "capture_detail_targets", reason: "invalid_list_surface" },
    );
  }
  const execution = await withVisibleTab(
    {
      tabId: listTabId,
      provider: lease.provider,
      kind: lease.kind,
      lease,
      deadline,
      ownedTabIds,
    },
    () => chrome.scripting.executeScript({
      target: { tabId: listTabId },
      world: "MAIN",
      func: fliggyCaptureVisibleLodgingDetailUrls,
      args: [MAX_CTRIP_LODGING_CAPTURE_CONTROLS],
    }),
  );
  const raw =
    Array.isArray(execution) && execution[0] && execution[0].result
      ? execution[0].result
      : { capture_code: "capture_result_missing", captures: [] };
  const targets = [];
  const rejections = [];
  const seen = new Set();
  for (const candidate of Array.isArray(raw.captures) ? raw.captures : []) {
    const decision = auditedFliggyLodgingDetailCandidate(
      candidate && candidate.raw_url,
      listPageUrl,
      lease.query,
    );
    if (!decision.allowed) {
      rejections.push({ reason: decision.reason, url: decision.url });
      continue;
    }
    if (seen.has(decision.href)) continue;
    seen.add(decision.href);
    targets.push({
      href: decision.href,
      property_id: decision.property_id,
      url: decision.url,
      augmented_from_visible_property_link:
        decision.augmented_from_visible_property_link === true,
      property_name: sanitizeInventoryDiagnosticText(
        candidate && candidate.property_name,
        180,
      ),
      location_evidence: sanitizeInventoryDiagnosticText(
        candidate && candidate.location_evidence,
        360,
      ),
    });
  }
  return {
    capture_code: raw.capture_code || "capture_result_missing",
    controls_seen: Number(raw.controls_seen) || 0,
    list_page_url: listPageUrl,
    targets,
    rejections: rejections.slice(0, 12),
  };
}

// This function is serialized into Ctrip's MAIN world. Keep it self-contained:
// it may not close over service-worker constants or helpers.
async function ctripCaptureVisibleLodgingDetailUrls(
  maxControls = 6,
  expectedPlaceKey = null,
  expectedPlaceAliases = [],
  maxPreviewCandidates = 12,
) {
  const clickLimit = Math.max(0, Math.min(6, Number(maxControls) || 0));
  const previewLimit = Math.max(
    0,
    Math.min(12, Number(maxPreviewCandidates) || 0),
  );
  const aliases = Array.isArray(expectedPlaceAliases)
    ? expectedPlaceAliases
      .map((value) => String(value || "").trim().toLowerCase())
      .filter(Boolean)
      .slice(0, 12)
    : [];
  const normalizeLabel = (value) =>
    String(value || "").replace(/\s+/g, "").trim();
  const compactText = (node, maxLength = 240) =>
    String(
      node && (node.innerText || node.textContent) || "",
    )
      .replace(/\s+/g, " ")
      .trim()
      .slice(0, maxLength);
  const isVisible = (control) => {
    if (
      !control ||
      control.disabled === true ||
      String(control.getAttribute && control.getAttribute("aria-disabled"))
        .toLowerCase() === "true"
    ) {
      return false;
    }
    let node = control;
    let depth = 0;
    while (node && depth < 12) {
      if (node.hidden === true || node.inert === true) {
        return false;
      }
      const style =
        typeof window.getComputedStyle === "function"
          ? window.getComputedStyle(node)
          : null;
      if (
        style &&
        (
          style.display === "none" ||
          style.visibility === "hidden" ||
          Number(style.opacity) === 0
        )
      ) {
        return false;
      }
      node = node.parentElement;
      depth += 1;
    }
    const rect =
      typeof control.getBoundingClientRect === "function"
        ? control.getBoundingClientRect()
        : null;
    return Boolean(rect && rect.width > 0 && rect.height > 0);
  };
  const candidates = Array.from(
    document.querySelectorAll(".room-right .book-btn"),
  );
  const exactControls = candidates.filter(
    (control) =>
      normalizeLabel(control.innerText || control.textContent) === "查看详情" &&
      isVisible(control),
  );
  const cardForControl = (control) => {
    if (control && typeof control.closest === "function") {
      const explicit = control.closest(
        [
          ".right-card",
          "[data-hotelid]",
          "[data-hotel-id]",
          "[class*='hotel-card']",
          "[class*='hotelCard']",
          "[class*='hotel-item']",
          "[class*='hotelItem']",
          "li",
        ].join(","),
      );
      if (explicit && explicit !== document.body) {
        return explicit;
      }
    }
    let node = control && control.parentElement;
    let selected = node || control;
    let depth = 0;
    while (node && node !== document.body && depth < 10) {
      if (typeof node.querySelectorAll === "function") {
        const detailControls = Array.from(
          node.querySelectorAll(".room-right .book-btn"),
        ).filter(
          (candidate) =>
            normalizeLabel(candidate.innerText || candidate.textContent) ===
            "查看详情",
        );
        if (detailControls.length > 1) {
          break;
        }
        if (detailControls.length === 1) {
          selected = node;
        }
      }
      node = node.parentElement;
      depth += 1;
    }
    return selected;
  };
  const previewForControl = (control, controlIndex) => {
    const card = cardForControl(control);
    const propertySelectors = [
      ".hotelName",
      "[data-testid*='hotel-name']",
      "[class*='hotel-name']",
      "[class*='hotelName']",
      "[class*='name-info']",
      "h2",
      "h3",
    ];
    const locationSelectors = [
      ".position-desc",
      "[data-testid*='location']",
      "[data-testid*='address']",
      "[class*='hotel-position']",
      "[class*='hotelPosition']",
      "[class*='location']",
      "[class*='Location']",
      "[class*='address']",
      "[class*='Address']",
      "[class*='district']",
      "[class*='District']",
      "[class*='landmark']",
      "[class*='Landmark']",
      "[class*='zone']",
      "[class*='Zone']",
    ];
    const subtitleSelectors = [
      ".hotel-subtitle",
      "[class*='hotel-subtitle']",
      "[class*='hotelSubtitle']",
    ];
    const firstText = (selectors) => {
      if (!card || typeof card.querySelector !== "function") {
        return null;
      }
      for (const selector of selectors) {
        const node = card.querySelector(selector);
        const value = compactText(node, 180);
        if (value) {
          return value;
        }
      }
      return null;
    };
    const locationEvidence = [];
    const seenLocation = new Set();
    if (card && typeof card.querySelectorAll === "function") {
      for (const selector of locationSelectors) {
        for (const node of card.querySelectorAll(selector)) {
          const value = compactText(node, 240);
          if (value && !seenLocation.has(value)) {
            seenLocation.add(value);
            locationEvidence.push(value);
            if (locationEvidence.length >= 8) {
              break;
            }
          }
        }
        if (locationEvidence.length >= 8) {
          break;
        }
      }
    }
    const referencesPlace = (value) => {
      const normalized = String(value || "").toLowerCase();
      if (!aliases.some((alias) => normalized.includes(alias))) {
        return false;
      }
      return (
        /(?:距|距离|附近|周边|靠近|临近|邻近)/i.test(normalized) ||
        /\d+(?:\.\d+)?\s*(?:公里|千米|米|km\b|m\b)/i.test(normalized) ||
        /\b(?:near|nearby|distance|away|from)\b/i.test(normalized)
      );
    };
    const explicitPropertyAlias = (value) => {
      const normalized = String(value || "")
        .normalize("NFKD")
        .replace(/\p{M}/gu, "")
        .toLowerCase();
      if (!normalized || referencesPlace(normalized)) {
        return null;
      }
      for (const rawAlias of aliases) {
        const alias = String(rawAlias || "")
          .normalize("NFKD")
          .replace(/\p{M}/gu, "")
          .toLowerCase();
        if (!alias) {
          continue;
        }
        if (/^[a-z0-9]+$/i.test(alias)) {
          const escaped = alias.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
          if (
            new RegExp(
              `(?:^|[^a-z0-9])${escaped}(?:\\s+island)?(?:$|[^a-z0-9])`,
              "i",
            ).test(normalized)
          ) {
            return rawAlias;
          }
          continue;
        }
        let index = normalized.indexOf(alias);
        while (index >= 0) {
          const prefix = normalized.slice(0, index);
          const previous = index > 0 ? normalized[index - 1] : "";
          if (
            index === 0 ||
            prefix.endsWith("马尔代夫") ||
            /[\s·•\-_/（）()，,。.【】\[\]]/.test(previous)
          ) {
            return rawAlias;
          }
          index = normalized.indexOf(alias, index + alias.length);
        }
      }
      return null;
    };
    const propertyName = firstText(propertySelectors);
    const subtitle = firstText(subtitleSelectors);
    const positionSummary =
      firstText([".position-desc"]) ||
      locationEvidence[0] ||
      null;
    const actualLocationPrefix = positionSummary
      ? positionSummary.split("·", 1)[0].trim()
      : null;
    const comparablePlace = (value) =>
      String(value || "")
        .toLowerCase()
        .replace(/[·•\-_/（）()，,。.]/g, "")
        .replace(/\s+/g, "")
        .replace(/(?:岛|island)$/i, "");
    const comparableActual = comparablePlace(actualLocationPrefix);
    const exactAlias = aliases.find(
      (alias) =>
        comparableActual &&
        comparableActual === comparablePlace(alias),
    ) || null;
    const exactLocationEvidence =
      exactAlias && actualLocationPrefix
        ? actualLocationPrefix
        : null;
    const propertyNameAlias = explicitPropertyAlias(propertyName);
    const subtitleAlias = explicitPropertyAlias(subtitle);
    const exactEvidence =
      exactLocationEvidence ||
      (propertyNameAlias
        ? `property_name: ${propertyName}`
        : subtitleAlias
          ? `subtitle: ${subtitle}`
          : null);
    const referenceEvidence =
      (positionSummary && referencesPlace(positionSummary)
        ? positionSummary
        : locationEvidence.find(referencesPlace)) ||
      null;
    let placeMatch = "not_requested";
    if (expectedPlaceKey && aliases.length) {
      placeMatch = exactEvidence
        ? "exact"
        : referenceEvidence
          ? "distance_only"
          : "unknown";
    }
    return {
      control,
      card,
      preview: {
        control_index: controlIndex,
        property_name: propertyName,
        subtitle,
        location_evidence: locationEvidence,
        position_summary: positionSummary,
        actual_location_prefix: actualLocationPrefix,
        preview_text: compactText(card, 600) || null,
        expected_place_key:
          typeof expectedPlaceKey === "string" ? expectedPlaceKey : null,
        place_match: placeMatch,
        exact_place_evidence: exactEvidence,
        distance_reference_evidence: referenceEvidence,
      },
    };
  };
  const previewsWithControls = exactControls
    .slice(0, previewLimit)
    .map(previewForControl);
  const placePriority = {
    exact: 0,
    not_requested: 1,
    unknown: 2,
    distance_only: 3,
  };
  const ranked = [...previewsWithControls].sort(
    (left, right) =>
      (placePriority[left.preview.place_match] ?? 9) -
        (placePriority[right.preview.place_match] ?? 9) ||
      left.preview.control_index - right.preview.control_index,
  );
  const eligible = expectedPlaceKey && aliases.length
    ? ranked.filter(({ preview }) => preview.place_match === "exact")
    : ranked;
  const controls = eligible.slice(0, clickLimit);
  const previews = previewsWithControls.map(({ preview }) => preview);
  if (expectedPlaceKey && aliases.length && !eligible.length) {
    return {
      capture_code: "expected_place_preview_not_found",
      controls_seen: candidates.length,
      exact_visible_controls: exactControls.length,
      clicked_controls: 0,
      popup_interceptions: 0,
      previews,
      ranked_control_indices: ranked.map(
        ({ preview }) => preview.control_index,
      ),
      captures: [],
      click_errors: [],
    };
  }
  const captures = [];
  const clickErrors = [];
  let activeControlIndex = null;
  const record = (rawUrl, controlIndex = activeControlIndex) => {
    if (rawUrl === undefined || rawUrl === null || String(rawUrl).trim() === "") {
      return;
    }
    captures.push({
      control_index: controlIndex,
      raw_url: String(rawUrl),
    });
  };
  const popupLocation = {};
  Object.defineProperty(popupLocation, "href", {
    configurable: true,
    get: () => "about:blank",
    set: record,
  });
  const harmlessPopup = {
    blur() {},
    close() {},
    closed: false,
    focus() {},
    postMessage() {},
  };
  Object.defineProperty(harmlessPopup, "location", {
    configurable: true,
    get: () => popupLocation,
    set: record,
  });
  const interceptedOpen = (rawUrl) => {
    record(rawUrl);
    return harmlessPopup;
  };
  const hadOwnOpen = Object.prototype.hasOwnProperty.call(window, "open");
  const previousDescriptor = Object.getOwnPropertyDescriptor(window, "open");
  const originalOpen = window.open;
  let patched = false;
  try {
    try {
      Object.defineProperty(window, "open", {
        configurable: true,
        writable: true,
        value: interceptedOpen,
      });
    } catch {
      try {
        window.open = interceptedOpen;
      } catch {
        // The result below remains typed; no control is clicked without a
        // proven interception because that could create a real popup.
      }
    }
    patched = window.open === interceptedOpen;
    if (!patched) {
      return {
        capture_code: "window_open_interception_failed",
        controls_seen: candidates.length,
        exact_visible_controls: exactControls.length,
        clicked_controls: 0,
        popup_interceptions: 0,
        previews,
        ranked_control_indices: ranked.map(
          ({ preview }) => preview.control_index,
        ),
        captures: [],
        click_errors: [],
      };
    }
    controls.forEach(({ control, card, preview }) => {
      activeControlIndex = preview.control_index;
      const directUrls = new Set();
      const addDirectUrl = (value) => {
        if (typeof value === "string" && value.trim()) {
          directUrls.add(value.trim());
        }
      };
      addDirectUrl(control.href);
      if (typeof control.getAttribute === "function") {
        for (const attribute of ["href", "data-href", "data-url"]) {
          addDirectUrl(control.getAttribute(attribute));
        }
      }
      if (typeof control.closest === "function") {
        const anchor = control.closest("a[href]");
        if (anchor) {
          addDirectUrl(anchor.href);
          if (typeof anchor.getAttribute === "function") {
            addDirectUrl(anchor.getAttribute("href"));
          }
        }
      }
      if (card && typeof card.querySelectorAll === "function") {
        for (const anchor of card.querySelectorAll("a[href]")) {
          const href = anchor && (
            anchor.href ||
            (typeof anchor.getAttribute === "function" &&
              anchor.getAttribute("href"))
          );
          if (/\/hotels\/detail(?:\/|\?|$)/i.test(String(href || ""))) {
            addDirectUrl(href);
          }
        }
      }
      for (const directUrl of directUrls) {
        record(directUrl, preview.control_index);
      }
      const preventDefault = (event) => {
        if (event && typeof event.preventDefault === "function") {
          event.preventDefault();
        }
      };
      try {
        if (typeof control.addEventListener === "function") {
          control.addEventListener("click", preventDefault, {
            capture: true,
            once: true,
          });
        }
        control.click();
      } catch (error) {
        clickErrors.push({
          control_index: preview.control_index,
          code:
            error && typeof error.name === "string"
              ? error.name
              : "control_click_failed",
        });
      } finally {
        if (typeof control.removeEventListener === "function") {
          control.removeEventListener("click", preventDefault, true);
        }
      }
    });
    activeControlIndex = null;
    // Keep the interception across the current event-loop turn. A deferred
    // window.open from the exact click is still captured, while longer delayed
    // popups have no synthetic user activation and remain browser-blocked.
    await new Promise((resolve) => setTimeout(resolve, 200));
    return {
      capture_code: captures.length
        ? "captured"
        : "detail_url_not_observed",
      controls_seen: candidates.length,
      exact_visible_controls: exactControls.length,
      clicked_controls: controls.length,
      popup_interceptions: captures.length,
      previews,
      ranked_control_indices: ranked.map(
        ({ preview }) => preview.control_index,
      ),
      captures,
      click_errors: clickErrors,
    };
  } finally {
    if (patched) {
      try {
        if (hadOwnOpen && previousDescriptor) {
          Object.defineProperty(window, "open", previousDescriptor);
        } else {
          delete window.open;
          if (window.open !== originalOpen) {
            window.open = originalOpen;
          }
        }
      } catch {
        try {
          window.open = originalOpen;
        } catch {
          // MAIN-world cleanup is best effort after every popup was intercepted.
        }
      }
    }
  }
}

function ctripLodgingDetailUrlDecision(rawUrl, listPageUrl, query = {}) {
  const rejected = (reason, parsed = null) => ({
    allowed: false,
    reason,
    url: parsed ? navigationUrlEvidence(parsed.href) : navigationUrlEvidence(""),
  });
  if (typeof rawUrl !== "string" || !rawUrl.trim()) {
    return rejected("missing_detail_url");
  }
  if (
    typeof listPageUrl !== "string" ||
    !isCtripLodgingListPageUrl(listPageUrl)
  ) {
    return rejected("invalid_list_surface");
  }
  const adults = Number(query.adults);
  const rooms = Number(query.rooms);
  if (
    calendarDateQueryValue(query.start_date) === null ||
    calendarDateQueryValue(query.end_date) === null ||
    !Number.isInteger(adults) ||
    adults <= 0 ||
    !Number.isInteger(rooms) ||
    rooms <= 0
  ) {
    return rejected("invalid_requested_query");
  }
  let parsed;
  try {
    parsed = new URL(rawUrl.trim(), listPageUrl);
  } catch {
    return rejected("invalid_detail_url");
  }
  if (parsed.protocol !== "https:") {
    return rejected("detail_non_https", parsed);
  }
  if (
    parsed.hostname.toLowerCase() !== "hotels.ctrip.com" ||
    parsed.port
  ) {
    return rejected("detail_wrong_host", parsed);
  }
  if (parsed.username || parsed.password) {
    return rejected("detail_embedded_credentials", parsed);
  }
  if (
    parsed.pathname.toLowerCase().replace(/\/+$/, "") !== "/hotels/detail"
  ) {
    return rejected("detail_wrong_path", parsed);
  }
  if (parsed.hash) {
    return rejected("detail_fragment_not_allowed", parsed);
  }
  const entries = [...parsed.searchParams.entries()];
  const required = new Map([
    ["hotelId", null],
    ["checkIn", query.start_date],
    ["checkOut", query.end_date],
    ["adult", String(adults)],
    ["crn", String(rooms)],
  ]);
  for (const expectedName of required.keys()) {
    const variants = entries.filter(
      ([name]) => name.toLowerCase() === expectedName.toLowerCase(),
    );
    if (
      variants.length !== 1 ||
      variants[0][0] !== expectedName
    ) {
      return rejected("detail_query_contract_mismatch", parsed);
    }
  }
  const forbiddenQueryMarker =
    /(?:cashier|coupon|order|payment|paynow|booking|booknow|redirect|returnurl|callback)/;
  for (const [name, value] of entries) {
    const normalizedName = name.toLowerCase().replace(/[^a-z0-9]/g, "");
    if (
      name !== "checkOut" &&
      forbiddenQueryMarker.test(normalizedName)
    ) {
      return rejected("detail_transaction_marker", parsed);
    }
    if (
      /^(?:https?:)?\/\//i.test(String(value).trim()) &&
      /(?:url|target|return|redirect|callback)/i.test(name)
    ) {
      return rejected("detail_redirect_marker", parsed);
    }
  }
  const hotelId = parsed.searchParams.get("hotelId");
  if (!/^[1-9]\d*$/.test(String(hotelId || ""))) {
    return rejected("detail_hotel_id_invalid", parsed);
  }
  const exactFields = [
    ["checkIn", query.start_date, "detail_check_in_mismatch"],
    ["checkOut", query.end_date, "detail_check_out_mismatch"],
    ["adult", String(adults), "detail_adults_mismatch"],
    ["crn", String(rooms), "detail_rooms_mismatch"],
  ];
  for (const [name, expected, reason] of exactFields) {
    if (parsed.searchParams.get(name) !== expected) {
      return rejected(reason, parsed);
    }
  }
  return {
    allowed: true,
    reason: "allowed",
    href: parsed.href,
    hotel_id: hotelId,
    url: navigationUrlEvidence(parsed.href),
  };
}

async function captureCtripLodgingDetailTargets(
  listTabId,
  lease,
  deadline,
  ownedTabIds,
) {
  const current = await chrome.tabs.get(listTabId);
  const listPageUrl = current.url || current.pendingUrl || "";
  if (!isCtripLodgingListPageUrl(listPageUrl)) {
    throw lifecycleError(
      "lodging_detail_wrong_surface",
      "Ctrip lodging detail capture requires the exact lodging list surface",
      {
        stage: "capture_detail_targets",
        reason: "invalid_list_surface",
        list_url: navigationUrlEvidence(listPageUrl),
      },
    );
  }
  const requestedPlaceKey =
    lease &&
    lease.query &&
    lease.query.options &&
    canonicalCtripLodgingPlaceKey(
      lease.query.options.expected_lodging_place_key,
    );
  const requestedPlaceAliases =
    requestedPlaceKey &&
    CTRIP_LODGING_PLACE_ALIASES[requestedPlaceKey]
      ? [...CTRIP_LODGING_PLACE_ALIASES[requestedPlaceKey]]
      : [];
  const execution = await withVisibleTab(
    {
      tabId: listTabId,
      provider: lease.provider,
      kind: lease.kind,
      lease,
      deadline,
      ownedTabIds,
    },
    () =>
      chrome.scripting.executeScript({
        target: { tabId: listTabId },
        world: "MAIN",
        func: ctripCaptureVisibleLodgingDetailUrls,
        args: [
          MAX_CTRIP_LODGING_CAPTURE_CONTROLS,
          requestedPlaceKey,
          requestedPlaceAliases,
          MAX_CTRIP_LODGING_PREVIEW_CANDIDATES,
        ],
      }),
  );
  const capture =
    Array.isArray(execution) &&
    execution[0] &&
    execution[0].result &&
    typeof execution[0].result === "object"
      ? execution[0].result
      : {
        capture_code: "capture_result_missing",
        captures: [],
      };
  const afterCapture = await chrome.tabs.get(listTabId);
  const afterCaptureUrl =
    afterCapture.url || afterCapture.pendingUrl || "";
  if (afterCaptureUrl !== listPageUrl) {
    throw lifecycleError(
      "lodging_detail_source_navigated",
      "Ctrip detail capture changed the source list tab unexpectedly",
      {
        stage: "capture_detail_targets",
        reason: "source_navigation_not_allowed",
        before_url: navigationUrlEvidence(listPageUrl),
        after_url: navigationUrlEvidence(afterCaptureUrl),
      },
    );
  }
  const rawCaptures = Array.isArray(capture.captures)
    ? capture.captures.slice(
      0,
      MAX_CTRIP_LODGING_CAPTURE_CONTROLS * 3,
    )
    : [];
  const safePreviewText = (value, maxLength) =>
    sanitizeInventoryDiagnosticText(value, maxLength);
  const allowedPlaceMatches = new Set([
    "exact",
    "not_requested",
    "unknown",
    "distance_only",
  ]);
  const previews = (Array.isArray(capture.previews)
    ? capture.previews
    : [])
    .slice(0, MAX_CTRIP_LODGING_PREVIEW_CANDIDATES)
    .map((preview) => ({
      control_index: Number.isInteger(preview && preview.control_index)
        ? preview.control_index
        : null,
      property_name: safePreviewText(preview && preview.property_name, 180),
      subtitle: safePreviewText(preview && preview.subtitle, 240),
      location_evidence: Array.isArray(preview && preview.location_evidence)
        ? preview.location_evidence
          .map((value) => safePreviewText(value, 240))
          .filter(Boolean)
          .slice(0, 8)
        : [],
      position_summary:
        safePreviewText(preview && preview.position_summary, 240),
      actual_location_prefix:
        safePreviewText(preview && preview.actual_location_prefix, 120),
      preview_text: safePreviewText(preview && preview.preview_text, 600),
      expected_place_key: requestedPlaceKey,
      place_match:
        allowedPlaceMatches.has(preview && preview.place_match)
          ? preview.place_match
          : "unknown",
      exact_place_evidence:
        safePreviewText(preview && preview.exact_place_evidence, 240),
      distance_reference_evidence:
        safePreviewText(
          preview && preview.distance_reference_evidence,
          240,
        ),
    }))
    .filter(({ control_index }) => control_index !== null);
  const previewByControl = new Map(
    previews.map((preview) => [preview.control_index, preview]),
  );
  const targets = [];
  const rejections = [];
  const seenControls = new Set();
  const seenUrls = new Set();
  for (const candidate of rawCaptures) {
    const controlIndex = Number.isInteger(candidate && candidate.control_index)
      ? candidate.control_index
      : null;
    if (controlIndex !== null && seenControls.has(controlIndex)) {
      continue;
    }
    const decision = ctripLodgingDetailUrlDecision(
      candidate && candidate.raw_url,
      listPageUrl,
      lease.query,
    );
    if (!decision.allowed) {
      rejections.push({
        control_index: controlIndex,
        reason: decision.reason,
        url: decision.url,
      });
      continue;
    }
    if (seenUrls.has(decision.href)) {
      continue;
    }
    if (controlIndex !== null) {
      seenControls.add(controlIndex);
    }
    seenUrls.add(decision.href);
    targets.push({
      control_index: controlIndex,
      href: decision.href,
      hotel_id: decision.hotel_id,
      url: decision.url,
      preview:
        controlIndex !== null && previewByControl.has(controlIndex)
          ? previewByControl.get(controlIndex)
          : null,
    });
  }
  const placePriority = {
    exact: 0,
    not_requested: 1,
    unknown: 2,
    distance_only: 3,
  };
  targets.sort(
    (left, right) =>
      (
        placePriority[
          left.preview && left.preview.place_match || "unknown"
        ] ?? 9
      ) -
        (
          placePriority[
            right.preview && right.preview.place_match || "unknown"
          ] ?? 9
        ) ||
      (
        Number.isInteger(left.control_index)
          ? left.control_index
          : Number.MAX_SAFE_INTEGER
      ) -
        (
          Number.isInteger(right.control_index)
            ? right.control_index
            : Number.MAX_SAFE_INTEGER
        ),
  );
  const expectedPlaceTargets = requestedPlaceKey
    ? targets.filter(
      (target) =>
        target.preview &&
        target.preview.place_match === "exact",
    )
    : targets;
  const selectedTargets = expectedPlaceTargets.slice(
    0,
    MAX_LODGING_DETAIL_PAGES_PER_LEASE,
  );
  const hasExpectedPlacePreview = requestedPlaceKey
    ? previews.some((preview) => preview.place_match === "exact")
    : true;
  let captureCode = typeof capture.capture_code === "string"
    ? capture.capture_code
    : "capture_result_invalid";
  if (requestedPlaceKey && !hasExpectedPlacePreview) {
    captureCode = "expected_place_preview_not_found";
  } else if (!selectedTargets.length && rejections.length) {
    captureCode = "detail_url_rejected";
  } else if (!selectedTargets.length && captureCode === "captured") {
    captureCode = "detail_url_not_observed";
  }
  return {
    capture_code: captureCode,
    controls_seen: Number(capture.controls_seen) || 0,
    exact_visible_controls: Number(capture.exact_visible_controls) || 0,
    clicked_controls: Number(capture.clicked_controls) || 0,
    popup_interceptions: Number(capture.popup_interceptions) || 0,
    click_errors: Array.isArray(capture.click_errors)
      ? capture.click_errors.slice(0, MAX_CTRIP_LODGING_CAPTURE_CONTROLS)
      : [],
    list_page_url: listPageUrl,
    expected_place_key: requestedPlaceKey,
    previews,
    ranked_control_indices: Array.isArray(capture.ranked_control_indices)
      ? capture.ranked_control_indices
        .filter(Number.isInteger)
        .slice(0, MAX_CTRIP_LODGING_PREVIEW_CANDIDATES)
      : [],
    targets: selectedTargets,
    validated_target_count: targets.length,
    rejections,
  };
}

async function closeOwnedTabs(ownedTabIds) {
  const tabIds = [...ownedTabIds];
  await Promise.allSettled(
    tabIds.map(async (tabId) => {
      try {
        await chrome.tabs.remove(tabId);
      } finally {
        ownedTabIds.delete(tabId);
      }
    }),
  );
}

function isQunarLodgingLease(lease) {
  return Boolean(
    lease && lease.provider === "qunar" && lease.kind === "lodging",
  );
}

function qunarLodgingIsolationEvidence(overrides = {}) {
  return {
    scope: QUNAR_LODGING_ISOLATION_SCOPE,
    owner: "browser_companion",
    window_type: "normal",
    requested_window_state: "normal",
    requested_focused: false,
    tab_active_in_isolated_window: true,
    reused_user_window: false,
    minimized: false,
    cleanup_policy: "close_before_lease_completion_and_retry_in_finally",
    fallback_policy: "fail_closed_without_activating_a_user_window",
    ...overrides,
  };
}

async function closeOwnedWindows(ownedWindowIds, ownedTabIds = new Set()) {
  const failures = [];
  for (const windowId of [...ownedWindowIds]) {
    let memberTabIds = [];
    try {
      if (chrome.tabs && typeof chrome.tabs.query === "function") {
        memberTabIds = (await chrome.tabs.query({ windowId }))
          .map((tab) => tab && tab.id)
          .filter(Number.isInteger);
      }
    } catch {
      // Window removal remains authoritative; the tab set is also retried by
      // the lease-level cleanup path.
    }
    let removed = false;
    let lastError = null;
    for (
      let attempt = 0;
      attempt < QUNAR_LODGING_WINDOW_CLEANUP_ATTEMPTS;
      attempt += 1
    ) {
      try {
        await chrome.windows.remove(windowId);
        removed = true;
        break;
      } catch (error) {
        lastError = error;
        try {
          await chrome.windows.get(windowId);
        } catch {
          removed = true;
          break;
        }
      }
    }
    if (removed) {
      ownedWindowIds.delete(windowId);
      for (const tabId of memberTabIds) {
        ownedTabIds.delete(tabId);
      }
    } else {
      failures.push({
        window_id: windowId,
        message: String(lastError && lastError.message || lastError),
      });
    }
  }
  if (failures.length) {
    throw lifecycleError(
      "qunar_lodging_isolation_cleanup_failed",
      "Companion-owned Qunar lodging window could not be closed",
      {
        browser_isolation: qunarLodgingIsolationEvidence({
          lifecycle_state: "cleanup_failed",
          cleanup_failures: failures,
        }),
      },
    );
  }
}

async function createQunarLodgingIsolationWindow(
  requestedUrl,
  ownedWindowIds,
  ownedTabIds,
) {
  const windowsApi = chrome.windows;
  if (
    !windowsApi ||
    typeof windowsApi.create !== "function" ||
    typeof windowsApi.remove !== "function" ||
    typeof windowsApi.get !== "function" ||
    typeof windowsApi.getAll !== "function" ||
    !chrome.tabs ||
    typeof chrome.tabs.query !== "function"
  ) {
    throw lifecycleError(
      "qunar_lodging_isolation_unavailable",
      "Chrome windows API is unavailable; refusing to activate a user window",
      {
        browser_isolation: qunarLodgingIsolationEvidence({
          lifecycle_state: "unavailable",
        }),
      },
    );
  }
  const existingWindowIds = new Set(
    (await windowsApi.getAll({ populate: false }))
      .map((window) => window && window.id)
      .filter(Number.isInteger),
  );
  let created;
  try {
    created = await windowsApi.create({
      url: requestedUrl,
      focused: false,
      state: "normal",
      type: "normal",
    });
  } catch (error) {
    throw lifecycleError(
      "qunar_lodging_isolation_create_failed",
      `Could not create an unfocused Qunar lodging window: ${String(
        error && error.message || error,
      )}`,
      {
        browser_isolation: qunarLodgingIsolationEvidence({
          lifecycle_state: "create_failed",
        }),
      },
    );
  }
  const windowId = created && created.id;
  if (!Number.isInteger(windowId) || existingWindowIds.has(windowId)) {
    throw lifecycleError(
      "qunar_lodging_isolation_existing_window_rejected",
      "Chrome did not return a new Companion-owned window",
      {
        browser_isolation: qunarLodgingIsolationEvidence({
          lifecycle_state: "existing_or_unidentified_window_rejected",
        }),
      },
    );
  }
  ownedWindowIds.add(windowId);
  try {
    const inspected = await windowsApi.get(windowId, { populate: true });
    const windowTabs = await chrome.tabs.query({ windowId });
    const activeTabs = windowTabs.filter(
      (tab) => tab && tab.active === true && tab.windowId === windowId,
    );
    if (
      !inspected ||
      inspected.focused !== false ||
      inspected.state !== "normal" ||
      activeTabs.length !== 1 ||
      !Number.isInteger(activeTabs[0].id)
    ) {
      throw lifecycleError(
        "qunar_lodging_isolation_contract_rejected",
        "Qunar lodging window violated the unfocused active-tab contract",
        {
          browser_isolation: qunarLodgingIsolationEvidence({
            lifecycle_state: "post_create_contract_rejected",
            observed_focused:
              inspected && typeof inspected.focused === "boolean"
                ? inspected.focused
                : null,
            observed_window_state:
              inspected && typeof inspected.state === "string"
                ? inspected.state
                : null,
            observed_active_tab_count: activeTabs.length,
          }),
        },
      );
    }
    const tab = activeTabs[0];
    ownedTabIds.add(tab.id);
    return {
      tab,
      isolation_evidence: qunarLodgingIsolationEvidence({
        lifecycle_state: "active",
        observed_focused: false,
        observed_window_state: "normal",
        observed_active_tab_count: 1,
      }),
    };
  } catch (error) {
    try {
      await closeOwnedWindows(ownedWindowIds, ownedTabIds);
    } catch (cleanupError) {
      throw cleanupError;
    }
    throw error;
  }
}

function attachBrowserIsolationEvidence(completion, evidence) {
  if (!completion || !evidence) {
    return completion;
  }
  return {
    ...completion,
    quotes: Array.isArray(completion.quotes)
      ? completion.quotes.map((quote) => ({
          ...quote,
          details: {
            ...(quote && quote.details || {}),
            driver: {
              ...(quote && quote.details && quote.details.driver || {}),
              browser_isolation: evidence,
            },
          },
        }))
      : completion.quotes,
    ...(completion.failure
      ? {
          failure: {
            ...completion.failure,
            details: {
              ...(completion.failure.details || {}),
              browser_isolation: evidence,
            },
          },
        }
      : {}),
  };
}

async function retainHumanActionTab(tabId, ownedTabIds) {
  if (!Number.isInteger(tabId)) {
    return false;
  }
  try {
    await chrome.tabs.get(tabId);
    // A CAPTCHA is a real human-in-the-loop handoff, not a disposable parser
    // failure. Stop cleanup from closing the only actionable page, but leave
    // it in the background. The user decides when to open it.
    ownedTabIds.delete(tabId);
    return true;
  } catch {
    return false;
  }
}

async function retainOnlyOwnedTab(ownedTabIds, retainedTabId) {
  const removable = [...ownedTabIds].filter((tabId) => tabId !== retainedTabId);
  await Promise.allSettled(
    removable.map(async (tabId) => {
      try {
        await chrome.tabs.remove(tabId);
        ownedTabIds.delete(tabId);
      } catch {
        // Keep failed removals owned so the lease-level finally block retries.
      }
    }),
  );
  ownedTabIds.add(retainedTabId);
}

async function adoptTrustedNavigation(
  transition,
  provider,
  kind,
  ownedTabIds,
) {
  if (!transition || !Number.isInteger(transition.tabId)) {
    throw lifecycleError(
      "navigation_not_observed",
      "trusted provider navigation did not identify a tab",
      navigationDiagnostic({
        provider,
        kind,
        stage: "adopt_navigation",
        reason: "missing_navigation_tab",
        trace: transition && transition.navigation_trace || [],
      }),
    );
  }
  const tab = await chrome.tabs.get(transition.tabId);
  const pageUrl = tab.url || transition.url || "";
  const trace = Array.isArray(transition.navigation_trace)
    ? transition.navigation_trace
    : [];
  const hostDecision = providerHostDecision(provider, pageUrl);
  if (!hostDecision.allowed) {
    throw lifecycleError(
      "navigation_error",
      `provider navigation left the allowed ${provider} host`,
      navigationDiagnostic({
        provider,
        kind,
        stage: "adopt_navigation",
        reason: hostDecision.reason,
        rawUrl: pageUrl,
        trace,
      }),
    );
  }
  const verticalDecision = providerVerticalDecision(provider, kind, pageUrl);
  if (!verticalDecision.allowed) {
    throw lifecycleError(
      "navigation_error",
      `provider navigation reached the wrong ${provider}/${kind} vertical`,
      navigationDiagnostic({
        provider,
        kind,
        stage: "adopt_navigation",
        reason: verticalDecision.reason,
        rawUrl: pageUrl,
        trace,
      }),
    );
  }
  await retainOnlyOwnedTab(ownedTabIds, transition.tabId);
  return {
    tabId: transition.tabId,
    pageUrl,
    mode: transition.mode,
    navigationTrace: trace,
  };
}

function prepareQueryForAttempt(query, attempt) {
  if (attempt === 0) {
    return query;
  }
  return {
    ...query,
    options: {
      ...(query.options || {}),
      [INTERNAL_SKIP_PROVIDER_MODE_SWITCH]: true,
    },
  };
}

async function prepareSearchWithLifecycle(
  tabId,
  pageUrl,
  lease,
  deadline,
  ownedTabIds,
) {
  let activeTabId = tabId;
  let activePageUrl = pageUrl;
  let navigationTrace = [];
  for (let attempt = 0; attempt < 2; attempt += 1) {
    let observer = null;
    try {
      const interaction = await visibleContentCall(
        activeTabId,
        {
          type: "tripchord:prepare-search",
          provider: lease.provider,
          kind: lease.kind,
          query: prepareQueryForAttempt(lease.query, attempt),
        },
        {
          lease,
          deadline,
          ownedTabIds,
          timeoutCapMs: 15000,
          returnInteraction: true,
          beforeSend: () => {
            const observer = observeTrustedProviderNavigation({
              provider: lease.provider,
              kind: lease.kind,
              sourceTabId: activeTabId,
              previousUrl: activePageUrl,
              ownedTabIds,
              timeoutMs: remainingTimeout(
                deadline,
                15000 + NAVIGATION_OBSERVER_SLACK_MS,
              ),
            });
            return observer;
          },
        },
      );
      observer = interaction.context;
      if (interaction.error) {
        throw interaction.error;
      }
      const prepared = interaction.result;
      const observed = observer.peek();
      if (observed && navigationFailure(observed.error)) {
        throw observed.error;
      }
      if (observed && observed.transition) {
        if (attempt > 0) {
          throw lifecycleError(
            "navigation_recovery_exhausted",
            "prepare-search navigated again after its single recovery",
          );
        }
        const adopted = await adoptTrustedNavigation(
          observed.transition,
          lease.provider,
          lease.kind,
          ownedTabIds,
        );
        activeTabId = adopted.tabId;
        activePageUrl = adopted.pageUrl;
        navigationTrace = adopted.navigationTrace;
        await installContent(activeTabId);
        await assertLeaseActive(lease);
        continue;
      }
      observer.cancel();
      return {
        prepared,
        tabId: activeTabId,
        pageUrl: activePageUrl,
        recovered: attempt > 0,
        navigationTrace,
      };
    } catch (error) {
      const observed = observer && observer.peek();
      if (observed && navigationFailure(observed.error)) {
        observer.cancel();
        throw observed.error;
      }
      if (!isMessagePortClosedError(error)) {
        if (observer) {
          observer.cancel();
        }
        throw error;
      }
      if (attempt > 0) {
        if (observer) {
          observer.cancel();
        }
        throw lifecycleError(
          "navigation_recovery_exhausted",
          "prepare-search message port closed again after its single recovery",
        );
      }
      if (!observer) {
        throw error;
      }
      const transition = await observer.promise;
      const adopted = await adoptTrustedNavigation(
        transition,
        lease.provider,
        lease.kind,
        ownedTabIds,
      );
      activeTabId = adopted.tabId;
      activePageUrl = adopted.pageUrl;
      navigationTrace = adopted.navigationTrace;
      await installContent(activeTabId);
      await assertLeaseActive(lease);
    }
  }
  throw lifecycleError(
    "navigation_recovery_exhausted",
    "prepare-search exhausted its navigation recovery",
  );
}

async function triggerSearchWithLifecycle(
  tabId,
  pageUrl,
  lease,
  deadline,
  ownedTabIds,
) {
  let observer = null;
  let triggered;
  let transition = null;
  let recovered = false;
  try {
    const interaction = await visibleContentCall(
      tabId,
      {
        type: "tripchord:trigger-search",
        provider: lease.provider,
        kind: lease.kind,
      },
      {
        lease,
        deadline,
        ownedTabIds,
        timeoutCapMs: 15000,
        postSettleMs: VISIBLE_CONTENT_SETTLE_MS,
        returnInteraction: true,
        beforeSend: () => {
          const observer = observeTrustedProviderNavigation({
            provider: lease.provider,
            kind: lease.kind,
            sourceTabId: tabId,
            previousUrl: pageUrl,
            ownedTabIds,
            timeoutMs: remainingTimeout(deadline, 90000),
          });
          return observer;
        },
      },
    );
    observer = interaction.context;
    if (interaction.error) {
      throw interaction.error;
    }
    triggered = interaction.result;
    if (triggered && triggered.audited_navigation_url) {
      const decision = auditedLodgingResultUrlDecision(
        lease.provider,
        triggered.audited_navigation_url,
        lease.query,
      );
      if (!decision.allowed) {
        throw lifecycleError(
          "navigation_error",
          "只读酒店结果地址未通过冻结查询合同",
          navigationDiagnostic({
            provider: lease.provider,
            kind: lease.kind,
            stage: "trigger_search",
            reason: decision.reason,
            rawUrl: triggered.audited_navigation_url,
            trace: observer.trace(),
          }),
        );
      }
      await chrome.tabs.update(tabId, { url: decision.href });
    }
  } catch (error) {
    const observed = observer && observer.peek();
    if (observed && navigationFailure(observed.error)) {
      observer.cancel();
      throw observed.error;
    }
    if (!isMessagePortClosedError(error)) {
      if (observer) {
        observer.cancel();
      }
      throw error;
    }
    if (!observer) {
      throw error;
    }
    // A closed response port is not success by itself. Only the already
    // registered observer can prove that the same provider/vertical navigated.
    transition = await observer.promise;
    triggered = {
      triggered: true,
      confirmation_scope: "trusted_provider_navigation_after_port_close",
    };
    recovered = true;
  }
  const responseObservation = observer.peek();
  if (responseObservation && navigationFailure(responseObservation.error)) {
    observer.cancel();
    throw responseObservation.error;
  }
  if (!triggered || triggered.triggered !== true) {
    observer.cancel();
    return {
      triggered,
      tabId,
      pageUrl,
      recovered,
    };
  }
  if (!transition) {
    const observed = observer.peek();
    if (observed && navigationFailure(observed.error)) {
      observer.cancel();
      throw observed.error;
    }
    transition =
      observed && observed.transition ||
      await waitForSearchTransition(
        observer,
        tabId,
        pageUrl,
        remainingTimeout(deadline, 90000),
      );
  }
  const adopted = await adoptTrustedNavigation(
    transition,
    lease.provider,
    lease.kind,
    ownedTabIds,
  );
  return {
    triggered,
    tabId: adopted.tabId,
    pageUrl: adopted.pageUrl,
    recovered,
    transitionMode: adopted.mode,
    navigationTrace: adopted.navigationTrace,
  };
}

function ctripOutboundStageNeedsWarmup(extraction) {
  const diagnostic =
    extraction &&
    extraction.failure &&
    extraction.failure.details &&
    extraction.failure.details.flight_diagnostic;
  const counts = diagnostic && diagnostic.counts;
  return Boolean(
    extraction &&
    extraction.state === "failed" &&
    extraction.failure &&
    ["dom_drift", "extraction_error"].includes(
      extraction.failure.code,
    ) &&
    diagnostic &&
    diagnostic.outcome ===
      "outbound_results_empty_or_unavailable" &&
    diagnostic.stage === "outbound_result_discovery" &&
    counts &&
    Number(counts.outbound_stage_anchor_count) > 0 &&
    Number(counts.visible_price_anchor_count) === 0 &&
    Number(counts.profile_card_count) === 0 &&
    Number(counts.safe_outbound_control_count) === 0
  );
}

function tongchengFlightResultNeedsWarmup(extraction) {
  const details =
    extraction && extraction.failure && extraction.failure.details;
  const diagnostics = details && details.dom_diagnostics;
  const candidates = diagnostics && Array.isArray(diagnostics.candidates)
    ? diagnostics.candidates
    : [];
  return Boolean(
    extraction &&
    extraction.state === "failed" &&
    extraction.failure &&
    extraction.failure.code === "dom_drift" &&
    !(details && details.flight_diagnostic) &&
    candidates.some((candidate) => {
      const className = String(candidate && candidate.class || "");
      const text = String(candidate && candidate.text_summary || "");
      return (
        /(?:^|\s)eliflight(?:\s|$)/i.test(className) &&
        /(?:单程|往返)/.test(text) &&
        Number(candidate.price_anchor_hits || 0) === 0
      );
    })
  );
}

function qunarGeometryStabilityKeys(extraction) {
  const keys =
    extraction &&
    extraction.state === "price_evidence_preview" &&
    extraction.stability &&
    extraction.stability.evidence_source ===
      "geometry_clipped_visible_digit_sequence" &&
    Array.isArray(extraction.stability.keys)
      ? extraction.stability.keys
      : [];
  if (
    !keys.length ||
    keys.length > 3 ||
    keys.some(
      (key) =>
        typeof key !== "string" ||
        !/^[a-f0-9]{64}$/.test(key),
    )
  ) {
    return null;
  }
  return [...new Set(keys)].sort();
}

async function restartTrustedFlightSearch(tabId, lease, deadline) {
  const searchUrl =
    lease &&
    lease.kind === "flight" &&
    ["ctrip", "fliggy"].includes(lease.provider) &&
    lease.query &&
    typeof lease.query.search_url === "string"
      ? lease.query.search_url
      : "";
  const decision = providerVerticalDecision(
    lease && lease.provider,
    "flight",
    searchUrl,
  );
  if (!searchUrl || !decision.allowed) {
    return false;
  }
  await chrome.tabs.update(tabId, { url: searchUrl });
  await waitForTabInteractive(
    tabId,
    remainingTimeout(deadline, SEARCH_RESULT_BOOTSTRAP_STAGE_CAP_MS),
  );
  await installContent(tabId);
  await delay(700);
  return true;
}

async function extractWithRetry(
  tabId,
  lease,
  driver,
  deadline,
  ownedTabIds = new Set([tabId]),
) {
  const extractionDeadline = Math.min(
    deadline,
    Date.now() +
      (
        lease.kind === "flight"
          ? FLIGHT_EXTRACTION_STAGE_CAP_MS
          : LODGING_EXTRACTION_STAGE_CAP_MS
      ),
  );
  const extractionStartedAt = Date.now();
  if (
    lease.kind === "lodging" &&
    extractionDeadline - extractionStartedAt < LODGING_EXTRACTION_MIN_BUDGET_MS
  ) {
    // A realtime search cannot settle into a terminal quote/empty/pending
    // receipt with less than the minimum observation budget. Failing fast here
    // (instead of attempting a doomed extraction) lets the caller preserve the
    // result tab for a full-budget retry rather than burn the lease into a
    // native timeout with no receipt.
    throw lifecycleError(
      "stage_timeout",
      `lodging extraction has insufficient remaining lease budget for a terminal receipt`,
      {
        stage: "list_extraction",
        required_budget_ms: LODGING_EXTRACTION_MIN_BUDGET_MS,
        available_budget_ms: Math.max(0, extractionDeadline - extractionStartedAt),
      },
    );
  }
  let qunarPendingSealAttempted = false;
  let workflowDriver = driver;
  if (lease.kind === "flight") {
    workflowDriver = {
      ...(driver || {}),
      action_trace: [
        {
          action: "search",
          provider: lease.provider,
          evidence:
            driver && driver.confirmation_scope ||
            "provider_search_context",
        },
      ],
    };
  }
  let extraction;
  let outboundSelections = 0;
  let outboundSelectionRevalidationMisses = 0;
  let returnSelections = 0;
  let retainedFlightSearchReceipt = null;
  let qunarDetailCandidateFingerprint = null;
  const outboundSelectionQueue = [];
  const attemptedOutboundSelectionIds = new Set();
  let readyForNextOutboundSelection = false;
  let postSelectionPreviewPolls = 0;
  let contentRecoveryAttempts = 0;
  let flightLoadingDriftPolls = 0;
  let flightStagedDriftPolls = 0;
  let ctripOutboundStageWarmupPolls = 0;
  let tongchengFlightResultWarmupPolls = 0;
  let qunarGeometryStabilityPolls = 0;
  const qunarFlightDetailExpandAttempts = {
    outbound: false,
    return: false,
  };
  const qunarFlightDetailExpansionEvidence = [];
  const retainFlightSearchReceipt = (result) => {
    const failureDetails =
      result &&
      result.failure &&
      result.failure.details &&
      typeof result.failure.details === "object"
        ? result.failure.details
        : {};
    const receipt =
      result && result.flight_search_receipt ||
      failureDetails.flight_search_receipt;
    const receiptSha256 =
      result && result.flight_search_receipt_sha256 ||
      failureDetails.flight_search_receipt_sha256;
    if (
      !receipt ||
      typeof receipt !== "object" ||
      Array.isArray(receipt) ||
      !["comparison_price_only", "bounded_no_exact_quote"].includes(
        receipt.state,
      ) ||
      typeof receipt.page_url !== "string" ||
      typeof receipt.captured_at !== "string" ||
      typeof receiptSha256 !== "string" ||
      !/^[a-f0-9]{64}$/.test(receiptSha256)
    ) {
      return;
    }
    const score = (
      receipt.state === "comparison_price_only" ? 100 : 0
    ) + (
      Number.isInteger(receipt.scanned_count)
        ? receipt.scanned_count
        : 0
    );
    if (
      !retainedFlightSearchReceipt ||
      score > retainedFlightSearchReceipt.score
    ) {
      retainedFlightSearchReceipt = {
        receipt,
        receipt_sha256: receiptSha256,
        score,
      };
    }
  };
  const withRetainedFlightSearchReceipt = (result) => {
    if (
      lease.kind !== "flight" ||
      !result ||
      result.state !== "failed" ||
      !result.failure
    ) {
      return result;
    }
    const details =
      result.failure.details &&
      typeof result.failure.details === "object"
        ? result.failure.details
        : {};
    const hasRetainedReceipt = Boolean(
      details.flight_search_receipt &&
      details.flight_search_receipt_sha256,
    );
    if (!retainedFlightSearchReceipt && !qunarFlightDetailExpansionEvidence.length) {
      return result;
    }
    const receipt = retainedFlightSearchReceipt && retainedFlightSearchReceipt.receipt;
    const nextDetails = {
      ...details,
      ...(qunarFlightDetailExpansionEvidence.length
        ? {
            qunar_safe_detail_expansion:
              qunarFlightDetailExpansionEvidence.slice(),
          }
        : {}),
    };
    if (hasRetainedReceipt || !receipt) {
      return {
        ...result,
        failure: {
          ...result.failure,
          details: nextDetails,
        },
      };
    }
    return {
      ...result,
      failure: {
        ...result.failure,
        code: "extraction_error",
        page_url: receipt.page_url,
        captured_at: receipt.captured_at,
        details: {
          ...nextDetails,
          flight_search_receipt: receipt,
          flight_search_receipt_sha256:
            retainedFlightSearchReceipt.receipt_sha256,
        },
      },
    };
  };
  const expandOneQunarFlightDetail = async () => {
    if (lease.provider !== "qunar" || lease.kind !== "flight") {
      return false;
    }
    const direction = qunarFlightDetailExpandAttempts.outbound
      ? "return"
      : "outbound";
    if (qunarFlightDetailExpandAttempts[direction]) {
      return false;
    }
    qunarFlightDetailExpandAttempts[direction] = true;
    const probe = () => visibleContentCall(
      tabId,
      {
        type: "tripchord:safe-expand-qunar-flight-detail",
        provider: "qunar",
        direction,
        query: lease.query,
        candidate_fingerprint: qunarDetailCandidateFingerprint,
        observed_candidates: Array.isArray(expanded && expanded.observed_candidates)
          ? expanded.observed_candidates
          : [],
      },
      {
        lease,
        deadline: extractionDeadline,
        ownedTabIds,
        timeoutCapMs: 15000,
        postSettleMs: 700,
      },
    );
    let expanded;
    try {
      expanded = await probe();
    } catch (error) {
      if (error && error.status === 409) {
        throw error;
      }
      qunarFlightDetailExpansionEvidence.push({
        direction,
        candidate_fingerprint: qunarDetailCandidateFingerprint,
        observed_candidates: Array.isArray(expanded && expanded.observed_candidates)
          ? expanded.observed_candidates
          : [],
        result_code: "safe_detail_probe_error",
        probe_attempt: 1,
        clicked: false,
        read_only: true,
        transaction_controls_excluded: true,
      });
      return false;
    }
    const receipt = retainedFlightSearchReceipt && retainedFlightSearchReceipt.receipt;
    const receiptHasCards = Boolean(
      receipt && Number.isInteger(receipt.scanned_count) && receipt.scanned_count > 0,
    );
    const receiptExplicitlyEmpty = Boolean(
      receipt && (
        receipt.state === "confirmed_empty" ||
        receipt.explicit_empty_evidence
      ),
    );
    let retriedEmptyScope = false;
    if (
      expanded &&
      expanded.expanded !== true &&
      expanded.inspected_card_count === 0 &&
      receiptHasCards &&
      !receiptExplicitlyEmpty &&
      Date.now() + 350 < extractionDeadline
    ) {
      retriedEmptyScope = true;
      qunarFlightDetailExpansionEvidence.push({
        direction,
        candidate_fingerprint: qunarDetailCandidateFingerprint,
        result_code: "transient_empty_card_scope",
        initial_result_code:
          typeof expanded.code === "string"
            ? expanded.code
            : "safe_detail_control_unavailable",
        inspected_card_count: 0,
        matching_target_card_count: 0,
        probe_attempt: 1,
        clicked: false,
        read_only: true,
        transaction_controls_excluded: true,
      });
      await new Promise((resolve) => setTimeout(resolve, 300));
      try {
        expanded = await probe();
      } catch (error) {
        if (error && error.status === 409) {
          throw error;
        }
        qunarFlightDetailExpansionEvidence.push({
          direction,
          candidate_fingerprint: qunarDetailCandidateFingerprint,
          observed_candidates: Array.isArray(expanded && expanded.observed_candidates)
            ? expanded.observed_candidates
            : [],
          result_code: "safe_detail_probe_error",
          probe_attempt: 2,
          retry_of: "transient_empty_card_scope",
          clicked: false,
          read_only: true,
          transaction_controls_excluded: true,
        });
        return false;
      }
    }
    if (!expanded || expanded.expanded !== true) {
      qunarFlightDetailExpansionEvidence.push({
        direction,
        candidate_fingerprint: qunarDetailCandidateFingerprint,
        observed_candidates: Array.isArray(expanded && expanded.observed_candidates)
          ? expanded.observed_candidates
          : [],
        result_code:
          expanded && typeof expanded.code === "string"
            ? expanded.code
            : "safe_detail_control_unavailable",
        inspected_card_count:
          expanded && Number.isInteger(expanded.inspected_card_count)
            ? expanded.inspected_card_count
            : null,
        matching_target_card_count:
          expanded && Number.isInteger(expanded.matching_target_card_count)
            ? expanded.matching_target_card_count
            : null,
        probe_attempt: retriedEmptyScope ? 2 : 1,
        ...(retriedEmptyScope ? { retry_of: "transient_empty_card_scope" } : {}),
        clicked: false,
        read_only: true,
        transaction_controls_excluded: true,
      });
      return false;
    }
    if (retriedEmptyScope) {
      qunarFlightDetailExpansionEvidence.push({
        direction,
        candidate_fingerprint: qunarDetailCandidateFingerprint,
        observed_candidates: Array.isArray(expanded && expanded.observed_candidates)
          ? expanded.observed_candidates
          : [],
        result_code: "retry_succeeded",
        inspected_card_count:
          Number.isInteger(expanded.inspected_card_count)
            ? expanded.inspected_card_count
            : null,
        matching_target_card_count:
          Number.isInteger(expanded.matching_target_card_count)
            ? expanded.matching_target_card_count
            : null,
        probe_attempt: 2,
        retry_of: "transient_empty_card_scope",
        clicked: true,
        read_only: true,
        transaction_controls_excluded: true,
      });
    }
    workflowDriver = {
      ...workflowDriver,
      action_trace: [
        ...(Array.isArray(workflowDriver && workflowDriver.action_trace)
          ? workflowDriver.action_trace
          : []),
        expanded.action,
      ],
    };
    if (!qunarDetailCandidateFingerprint && expanded.candidate_fingerprint) {
      qunarDetailCandidateFingerprint = expanded.candidate_fingerprint;
    }
    return true;
  };
  const restartForNextOutbound = async () => {
    const hasUntriedCandidate = outboundSelectionQueue.some(
      (candidate) =>
        candidate &&
        typeof candidate.selection_id === "string" &&
        !attemptedOutboundSelectionIds.has(candidate.selection_id),
    );
    if (
      !hasUntriedCandidate ||
      outboundSelections >= MAX_OUTBOUND_SELECTION_ATTEMPTS ||
      !await restartTrustedFlightSearch(tabId, lease, extractionDeadline)
    ) {
      return false;
    }
    workflowDriver = {
      ...workflowDriver,
      selected_outbound: null,
      outbound_navigation_recovered: false,
    };
    readyForNextOutboundSelection = true;
    postSelectionPreviewPolls = 0;
    contentRecoveryAttempts = 0;
    flightLoadingDriftPolls = 0;
    flightStagedDriftPolls = 0;
    ctripOutboundStageWarmupPolls = 0;
    tongchengFlightResultWarmupPolls = 0;
    return true;
  };
  do {
    try {
      extraction = await visibleContentCall(
        tabId,
        {
          type: "tripchord:extract",
          provider: lease.provider,
          kind: lease.kind,
          query: lease.query,
          driver: workflowDriver,
        },
        {
          lease,
          deadline: extractionDeadline,
          ownedTabIds,
          timeoutCapMs: 15000,
        },
      );
      retainFlightSearchReceipt(extraction);
    } catch (error) {
      if (
        error &&
        (
          error.status === 409 ||
          navigationFailure(error)
        )
      ) {
        throw error;
      }
      if (!isMessagePortClosedError(error)) {
        throw error;
      }
      if (
        contentRecoveryAttempts >= 1 ||
        Date.now() >= extractionDeadline
      ) {
        throw lifecycleError(
          "navigation_recovery_exhausted",
          "content extraction message port closed again after its single recovery",
        );
      }
      // A full-page navigation destroys the previous content-script context.
      // Only a real message-port close receives one idempotent reinstall;
      // content command timeouts are terminal because Promise.race cannot
      // cancel the parser that is still running in the page.
      contentRecoveryAttempts += 1;
      await new Promise((resolve) => setTimeout(resolve, 500));
      await installContent(tabId);
      continue;
    }
    if (
      lease.provider === "tongcheng" &&
      lease.kind === "flight" &&
      extraction &&
      extraction.state === "return_preview"
    ) {
      const selection = extraction.selection;
      if (
        returnSelections >= 1 ||
        !selection ||
        typeof selection.selection_id !== "string"
      ) {
        return withRetainedFlightSearchReceipt(failure(
          "failed",
          "round_trip_incomplete",
          "同程返程报价详情未形成精确完整往返价",
          null,
          false,
          {
            workflow_kind: "staged_outbound_return",
            return_selections: returnSelections,
          },
        ));
      }
      let selected;
      try {
        selected = await visibleContentCall(
          tabId,
          {
            type: "tripchord:safe-select-return",
            provider: lease.provider,
            query: lease.query,
            driver: workflowDriver,
            selection_id: selection.selection_id,
          },
          {
            lease,
            deadline: extractionDeadline,
            ownedTabIds,
            timeoutCapMs: 15000,
            postSettleMs: VISIBLE_CONTENT_SETTLE_MS,
          },
        );
      } catch (error) {
        if (!isMessagePortClosedError(error)) {
          throw error;
        }
        throw lifecycleError(
          "unsafe_return_navigation",
          "返程报价查看触发了整页导航，已停止以避免进入订单流程",
        );
      }
      if (
        !selected ||
        selected.selected !== true ||
        !selected.selection ||
        selected.selection.selection_id !== selection.selection_id
      ) {
        return withRetainedFlightSearchReceipt(failure(
          "failed",
          "round_trip_incomplete",
          "返程选择控件未通过二次可见证据校验",
          null,
          false,
          {
            code: selected && selected.code || "safe_return_selection_failed",
          },
        ));
      }
      returnSelections += 1;
      workflowDriver = {
        ...workflowDriver,
        selected_return: selected.selection,
        action_trace: [
          ...workflowDriver.action_trace,
          {
            action: "select_return",
            provider: lease.provider,
            evidence: selected.selection.selection_evidence,
          },
        ],
      };
      await new Promise((resolve) => setTimeout(resolve, 900));
      continue;
    }
    if (
      lease.kind === "flight" &&
      extraction &&
      extraction.state === "outbound_preview"
    ) {
      const previewSelections = Array.isArray(extraction.selections)
        ? extraction.selections
        : [extraction.selection];
      for (const candidate of previewSelections) {
        if (
          candidate &&
          typeof candidate.selection_id === "string" &&
          !outboundSelectionQueue.some(
            (queued) => queued.selection_id === candidate.selection_id,
          )
        ) {
          outboundSelectionQueue.push(candidate);
        }
      }
      if (!outboundSelectionQueue.length) {
        return withRetainedFlightSearchReceipt(failure(
          "failed",
          "round_trip_incomplete",
          "页面没有形成可复核的去程候选集合",
          null,
          false,
          {
            workflow_kind: "staged_outbound_return",
            outbound_selections: outboundSelections,
          },
        ));
      }
      if (outboundSelections >= 1 && !readyForNextOutboundSelection) {
        if (
          postSelectionPreviewPolls < 5 &&
          Date.now() + 1000 < extractionDeadline
        ) {
          postSelectionPreviewPolls += 1;
          await new Promise((resolve) => setTimeout(resolve, 1000));
          continue;
        }
        if (await restartForNextOutbound()) {
          continue;
        }
        return withRetainedFlightSearchReceipt(failure(
          "failed",
          "round_trip_incomplete",
          "所有有界去程候选均未形成完整往返组合",
          null,
          false,
          {
            workflow_kind: "staged_outbound_return",
            outbound_selections: outboundSelections,
            attempted_selection_ids: [
              ...attemptedOutboundSelectionIds,
            ],
            post_selection_preview_polls: postSelectionPreviewPolls,
          },
        ));
      }
      const selection = outboundSelectionQueue.find(
        (candidate) =>
          !attemptedOutboundSelectionIds.has(candidate.selection_id),
      );
      if (!selection) {
        return withRetainedFlightSearchReceipt(failure(
          "failed",
          "round_trip_incomplete",
          "所有有界去程候选均已尝试",
          null,
          false,
          {
            workflow_kind: "staged_outbound_return",
            outbound_selections: outboundSelections,
            attempted_selection_ids: [
              ...attemptedOutboundSelectionIds,
            ],
          },
        ));
      }
      attemptedOutboundSelectionIds.add(selection.selection_id);
      let selected;
      let navigationRecovered = false;
      try {
        selected = await visibleContentCall(
          tabId,
          {
            type: "tripchord:safe-select-outbound",
            provider: lease.provider,
            query: lease.query,
            selection_id: selection.selection_id,
          },
          {
            lease,
            deadline: extractionDeadline,
            ownedTabIds,
            timeoutCapMs: 15000,
            postSettleMs: VISIBLE_CONTENT_SETTLE_MS,
          },
        );
      } catch (error) {
        if (!isMessagePortClosedError(error)) {
          throw error;
        }
        // Some result pages navigate immediately after the exact outbound
        // selection click.  The next parser pass must still verify the
        // selected-flight summary, so treating this only as an attempted
        // transition cannot fabricate a quote.
        selected = {
          selected: true,
          confirmation_scope: "outbound_navigation_after_port_close",
          selection,
        };
        navigationRecovered = true;
        await new Promise((resolve) => setTimeout(resolve, 700));
        await installContent(tabId);
      }
      if (
        !selected ||
        selected.selected !== true ||
        !selected.selection ||
        selected.selection.selection_id !== selection.selection_id
      ) {
        if (
          selected &&
          selected.code === "outbound_selection_evidence_changed" &&
          outboundSelectionRevalidationMisses <
            MAX_OUTBOUND_SELECTION_REVALIDATION_MISSES &&
          Date.now() + 1000 < extractionDeadline
        ) {
          outboundSelectionRevalidationMisses += 1;
          continue;
        }
        return withRetainedFlightSearchReceipt(failure(
          "failed",
          "round_trip_incomplete",
          "精确的去程选择控件未通过二次可见证据校验",
          null,
          false,
          {
            code: selected && selected.code || "safe_outbound_selection_failed",
            requested_selection: {
              selection_id: selection.selection_id,
              carrier_text: selection.carrier_text,
              outbound_departure_at: selection.outbound_departure_at,
              outbound_arrival_at: selection.outbound_arrival_at,
              expected_departure_code:
                selection.outbound_route_evidence &&
                selection.outbound_route_evidence.expected_departure_code,
              expected_arrival_code:
                selection.outbound_route_evidence &&
                selection.outbound_route_evidence.expected_arrival_code,
            },
            available_candidates:
              selected && Array.isArray(selected.available_candidates)
                ? selected.available_candidates
                : [],
            outbound_selection_revalidation_misses:
              outboundSelectionRevalidationMisses,
          },
        ));
      }
      const action =
        outboundSelections === 0
          ? "select_outbound"
          : "reselect_outbound";
      outboundSelections += 1;
      readyForNextOutboundSelection = false;
      postSelectionPreviewPolls = 0;
      workflowDriver = {
        ...workflowDriver,
        selected_outbound: selected.selection,
        outbound_navigation_recovered: navigationRecovered,
        action_trace: [
          ...workflowDriver.action_trace,
          {
            action,
            provider: lease.provider,
            evidence: selected.selection.selection_evidence,
          },
        ],
      };
      await new Promise((resolve) => setTimeout(resolve, 900));
      continue;
    }
    if (
      lease.provider === "qunar" &&
      lease.kind === "flight" &&
      extraction &&
      extraction.state === "price_evidence_preview"
    ) {
      const stabilityKeys = qunarGeometryStabilityKeys(extraction);
      if (!stabilityKeys) {
        workflowDriver = {
          ...workflowDriver,
          qunar_geometry_price_disabled: true,
          qunar_geometry_stability_keys: [],
        };
        continue;
      }
      if (
        qunarGeometryStabilityPolls <
          QUNAR_GEOMETRY_STABILITY_MAX_POLLS &&
        Date.now() + QUNAR_GEOMETRY_STABILITY_POLL_INTERVAL_MS <
          extractionDeadline
      ) {
        qunarGeometryStabilityPolls += 1;
        workflowDriver = {
          ...workflowDriver,
          qunar_geometry_price_disabled: false,
          qunar_geometry_stability_keys: stabilityKeys,
        };
        await new Promise((resolve) =>
          setTimeout(
            resolve,
            QUNAR_GEOMETRY_STABILITY_POLL_INTERVAL_MS,
          )
        );
        continue;
      }
      workflowDriver = {
        ...workflowDriver,
        qunar_geometry_price_disabled: true,
        qunar_geometry_stability_keys: [],
      };
      continue;
    }
    if (
      lease.provider === "qunar" &&
      lease.kind === "flight" &&
      extraction &&
      extraction.state === "failed" &&
      extraction.failure &&
      ["dom_drift", "extraction_error"].includes(extraction.failure.code)
    ) {
      // Probe both directions in one bounded failure pass.  A missing
      // outbound control must not short-circuit the independent return probe;
      // otherwise the old branch reported DOM drift without inspecting the
      // return-side details at all.
      let expanded = await expandOneQunarFlightDetail();
      if (!expanded && !qunarFlightDetailExpandAttempts.return) {
        expanded = await expandOneQunarFlightDetail();
      }
      if (expanded) {
        continue;
      }
    }
    if (
      lease.provider === "ctrip" &&
      lease.kind === "flight" &&
      ctripOutboundStageNeedsWarmup(extraction) &&
      ctripOutboundStageWarmupPolls <
        CTRIP_OUTBOUND_STAGE_WARMUP_MAX_POLLS &&
      Date.now() + FLIGHT_DOM_DRIFT_POLL_INTERVAL_MS <
        extractionDeadline
    ) {
      ctripOutboundStageWarmupPolls += 1;
      await new Promise((resolve) =>
        setTimeout(resolve, FLIGHT_DOM_DRIFT_POLL_INTERVAL_MS)
      );
      continue;
    }
    if (
      lease.provider === "tongcheng" &&
      lease.kind === "flight" &&
      tongchengFlightResultNeedsWarmup(extraction) &&
      tongchengFlightResultWarmupPolls <
        TONGCHENG_FLIGHT_RESULT_WARMUP_MAX_POLLS &&
      Date.now() + FLIGHT_DOM_DRIFT_POLL_INTERVAL_MS <
        extractionDeadline
    ) {
      tongchengFlightResultWarmupPolls += 1;
      await new Promise((resolve) =>
        setTimeout(resolve, FLIGHT_DOM_DRIFT_POLL_INTERVAL_MS)
      );
      continue;
    }
    if (
      lease.kind === "flight" &&
      extraction &&
      extraction.state === "failed" &&
      extraction.failure &&
      ["dom_drift", "extraction_error"].includes(
        extraction.failure.code,
      ) &&
      outboundSelections > 0 &&
      await restartForNextOutbound()
    ) {
      continue;
    }
    if (
      extraction.state !== "failed" ||
      !extraction.failure ||
      extraction.failure.code !== "dom_drift"
    ) {
      return withRetainedFlightSearchReceipt(extraction);
    }
    if (
      lease.provider === "ctrip" &&
      lease.kind === "lodging"
    ) {
      const current = await chrome.tabs.get(tabId);
      const currentUrl = current.url || current.pendingUrl || "";
      if (isCtripLodgingListPageUrl(currentUrl)) {
        // A Ctrip list price is only a preview. Hand off immediately to the
        // background's read-only list→detail workflow instead of spending the
        // extraction budget retrying the same non-final list cards.
        return extraction;
      }
    }
    if (lease.kind === "flight") {
      const stagedWorkflowCanProgress =
        outboundSelections > 0 ||
        Boolean(workflowDriver && workflowDriver.selected_outbound);
      if (
        stagedWorkflowCanProgress &&
        flightStagedDriftPolls < FLIGHT_STAGED_DOM_DRIFT_MAX_POLLS &&
        Date.now() + FLIGHT_DOM_DRIFT_POLL_INTERVAL_MS <
          extractionDeadline
      ) {
        flightStagedDriftPolls += 1;
        await new Promise((resolve) =>
          setTimeout(resolve, FLIGHT_DOM_DRIFT_POLL_INTERVAL_MS)
        );
        continue;
      }
      const current = await chrome.tabs.get(tabId);
      const pageStillLoading =
        current &&
        (
          current.status === "loading" ||
          current.status === "interactive"
        );
      const flightDiagnostic =
        extraction.failure.details &&
        extraction.failure.details.flight_diagnostic;
      const providerResultsStillLoading =
        flightDiagnostic &&
        flightDiagnostic.outcome === "flight_results_loading";
      if (
        (providerResultsStillLoading || pageStillLoading) &&
        flightLoadingDriftPolls < FLIGHT_LOADING_DOM_DRIFT_MAX_POLLS &&
        Date.now() + FLIGHT_DOM_DRIFT_POLL_INTERVAL_MS <
          extractionDeadline
      ) {
        flightLoadingDriftPolls += 1;
        await new Promise((resolve) =>
          setTimeout(resolve, FLIGHT_DOM_DRIFT_POLL_INTERVAL_MS)
        );
        continue;
      }
      // Typed flight diagnostics describe terminal visible evidence, such as
      // starting-price-only or a missing round-trip contract. Re-reading the
      // same completed DOM cannot strengthen that evidence.
      return withRetainedFlightSearchReceipt(extraction);
    }
    if (
      lease.provider === "qunar" &&
      lease.kind === "lodging" &&
      !qunarPendingSealAttempted &&
      qunarLodgingPendingDomSignature(extraction) &&
      Date.now() - extractionStartedAt >= QUNAR_PENDING_MIN_OBSERVED_MS
    ) {
      // The search list has shown the realtime-search shell for the full
      // minimum observation window.  Seal the bounded-pending receipt now and
      // hand the remaining lease to the audited detail-page fallback instead of
      // re-reading the same non-terminal shell until the extraction deadline.
      qunarPendingSealAttempted = true;
      workflowDriver = {
        ...workflowDriver,
        bounded_pending_observed_ms: Date.now() - extractionStartedAt,
      };
      continue;
    }
    if (
      Date.now() +
        LODGING_DOM_DRIFT_POLL_INTERVAL_MS +
        LODGING_EXTRACTION_RETRY_MIN_BUDGET_MS >=
        extractionDeadline
    ) {
      if (
        lease.provider === "qunar" &&
        lease.kind === "lodging" &&
        !qunarPendingSealAttempted
      ) {
        qunarPendingSealAttempted = true;
        workflowDriver = {
          ...workflowDriver,
          bounded_pending_observed_ms:
            Date.now() - extractionStartedAt,
        };
        continue;
      }
      break;
    }
    await new Promise((resolve) =>
      setTimeout(resolve, LODGING_DOM_DRIFT_POLL_INTERVAL_MS)
    );
  } while (Date.now() < extractionDeadline);
  return withRetainedFlightSearchReceipt(extraction);
}

async function enrichOneLodgingQuoteWithTransferDetail(
  quote,
  lease,
  reusableTabId,
  deadline,
  ownedTabIds,
) {
  const detailUrl =
    quote &&
    quote.details &&
    typeof quote.details.transfer_detail_url === "string"
      ? quote.details.transfer_detail_url
      : null;
  if (!detailUrl || !providerHostAllowed(lease.provider, detailUrl)) {
    return quote;
  }
  try {
    await assertLeaseActive(lease);
    await chrome.tabs.update(reusableTabId, { url: detailUrl, active: false });
    await waitForTabComplete(
      reusableTabId,
      remainingTimeout(deadline, 30000),
    );
    const current = await chrome.tabs.get(reusableTabId);
    const currentUrl = current.url || detailUrl;
    if (!providerHostAllowed(lease.provider, currentUrl)) {
      return {
        ...quote,
        details: {
          ...quote.details,
          transfer_detail_status: "unsafe_navigation",
        },
      };
    }
    await installContent(reusableTabId);
    const detail = await visibleContentCall(
      reusableTabId,
      {
        type: "tripchord:extract-transfer-detail",
        provider: lease.provider,
        query: lease.query,
      },
      {
        lease,
        deadline,
        ownedTabIds,
        timeoutCapMs: 15000,
      },
    );
    if (
      detail &&
      detail.state === "succeeded" &&
      Array.isArray(detail.transfers) &&
      detail.transfers.length
    ) {
      return {
        ...quote,
        details: {
          ...quote.details,
          transfers: detail.transfers,
          transfer_detail_url: detail.detail_url || currentUrl,
          transfer_detail_status: "succeeded",
        },
      };
    }
    return {
      ...quote,
      details: {
        ...quote.details,
        transfer_detail_status:
          detail && detail.state || "missing_explicit_contract",
      },
    };
  } catch (error) {
    if (error && error.status === 409) {
      throw error;
    }
    return {
      ...quote,
      details: {
        ...quote.details,
        transfer_detail_status: "detail_read_failed",
      },
    };
  }
}

async function enrichLodgingTransferDetails(
  extraction,
  lease,
  reusableTabId,
  deadline,
  ownedTabIds,
) {
  if (
    lease.kind !== "lodging" ||
    !extraction ||
    extraction.state !== "succeeded" ||
    !Array.isArray(extraction.quotes)
  ) {
    return extraction;
  }
  let remaining = MAX_LODGING_DETAIL_PAGES_PER_LEASE;
  const quotes = [];
  for (const quote of extraction.quotes) {
    const hasDetail =
      quote &&
      quote.details &&
      typeof quote.details.transfer_detail_url === "string" &&
      quote.details.transfer_detail_url.length > 0;
    if (!hasDetail || remaining <= 0) {
      quotes.push(quote);
      continue;
    }
    remaining -= 1;
    quotes.push(
      await enrichOneLodgingQuoteWithTransferDetail(
        quote,
        lease,
        reusableTabId,
        deadline,
        ownedTabIds,
      ),
    );
  }
  return {
    ...extraction,
    quotes,
  };
}

function failure(
  state,
  code,
  message,
  pageUrl = null,
  retryable = false,
  details = {},
  capturedAt = new Date().toISOString(),
) {
  return {
    state,
    quotes: [],
    failure: {
      code,
      message,
      retryable,
      page_url: pageUrl,
      captured_at: capturedAt,
      details,
    },
  };
}

function shouldTryCtripLodgingDetailOrchestration(
  extraction,
  lease,
  pageUrl,
) {
  if (
    !lease ||
    lease.provider !== "ctrip" ||
    lease.kind !== "lodging" ||
    !isCtripLodgingListPageUrl(pageUrl) ||
    !extraction
  ) {
    return false;
  }
  if (
    extraction.state === "failed" &&
    extraction.failure &&
    extraction.failure.code === "dom_drift"
  ) {
    return true;
  }
  return (
    extraction.state === "succeeded" &&
    Array.isArray(extraction.quotes) &&
    extraction.quotes.length === 0
  );
}

function shouldTryFliggyLodgingDetailOrchestration(
  extraction,
  lease,
  pageUrl,
) {
  return Boolean(
    lease &&
    lease.provider === "fliggy" &&
    lease.kind === "lodging" &&
    isFliggyLodgingListPageUrl(pageUrl) &&
    extraction &&
    (
      (
        extraction.state === "failed" &&
        extraction.failure &&
        extraction.failure.code === "dom_drift"
      ) ||
      (
        extraction.state === "succeeded" &&
        Array.isArray(extraction.quotes) &&
        extraction.quotes.length === 0
      )
    )
  );
}

// A qunar lodging extraction that is still waiting on the platform's visible
// realtime-search shell returns a `dom_drift` failure whose DOM diagnostics
// carry the pending signature (empty hotel count placeholder + the
// "正在实时搜索中" message).  Detecting that signature in the drift loop lets
// the extraction seal the bounded-pending receipt as soon as it has satisfied
// the minimum observation window, instead of squandering the whole lease
// re-reading the same non-terminal shell.  Sealing early is what leaves budget
// for the audited detail-page fallback (orchestrateQunarLodgingDetails).
function qunarLodgingPendingDomSignature(extraction) {
  const failureDetails =
    extraction && extraction.failure && extraction.failure.details;
  const diagnostics =
    failureDetails && failureDetails.dom_diagnostics;
  const resultStateEvidence =
    diagnostics &&
    Array.isArray(diagnostics.result_state_evidence)
      ? diagnostics.result_state_evidence
      : [];
  const texts = resultStateEvidence
    .map((entry) => String(
      entry && entry.text_summary || entry && entry.text || "",
    ))
    .filter(Boolean);
  return (
    texts.some((text) => /共\s*家酒店满足条件/.test(text)) &&
    texts.some((text) => /请稍等[，,]\s*您查询的结果正在实时搜索中/.test(text))
  );
}

function qunarInventoryExtractionFingerprint(extraction) {
  const failureDetails =
    extraction && extraction.failure && extraction.failure.details;
  const receipt =
    failureDetails && failureDetails.inventory_receipt;
  const inventoryState = receipt && receipt.state;
  const confirmedEmpty = inventoryState === "confirmed_empty";
  const boundedProviderPending =
    inventoryState === "bounded_provider_pending";
  if (
    !extraction ||
    extraction.state !== "failed" ||
    !extraction.failure ||
    !failureDetails ||
    !receipt ||
    receipt.provider !== "qunar" ||
    receipt.scanned_count !== 0 ||
    !Array.isArray(receipt.candidate_summaries) ||
    receipt.candidate_summaries.length !== 0 ||
    failureDetails.inventory_result_state !== inventoryState ||
    !(
      confirmedEmpty &&
      extraction.failure.code === "no_inventory" &&
      failureDetails.confirmed_exhaustive === true &&
      receipt.explicit_empty_evidence &&
      receipt.provider_pending_evidence === null
    ) &&
    !(
      boundedProviderPending &&
      extraction.failure.code === "extraction_error" &&
      failureDetails.confirmed_exhaustive === false &&
      receipt.explicit_empty_evidence === null &&
      receipt.provider_pending_evidence
    )
  ) {
    return null;
  }
  return canonicalInventoryJson({
    provider: receipt.provider,
    state: receipt.state,
    confirmed_query: receipt.confirmed_query,
    confirmation_scope: receipt.confirmation_scope,
    explicit_empty_evidence: receipt.explicit_empty_evidence,
    provider_pending_evidence: receipt.provider_pending_evidence,
    page_url: receipt.page_url,
  });
}

function qunarConfirmedEmptyExtractionFingerprint(extraction) {
  const receipt = extraction && extraction.failure &&
    extraction.failure.details &&
    extraction.failure.details.inventory_receipt;
  return receipt && receipt.state === "confirmed_empty"
    ? qunarInventoryExtractionFingerprint(extraction)
    : null;
}

function qunarBoundedProviderPendingExtractionFingerprint(extraction) {
  const receipt = extraction && extraction.failure &&
    extraction.failure.details &&
    extraction.failure.details.inventory_receipt;
  return receipt && receipt.state === "bounded_provider_pending"
    ? qunarInventoryExtractionFingerprint(extraction)
    : null;
}

function qunarExactResultReadbackDriverValid(driver, query, citySlug) {
  const confirmed = driver && driver.confirmed_query;
  const readback = driver && driver.readback_query;
  const evidence = driver && driver.result_query_readback_evidence;
  const exactEvidenceKeys = [
    "provider_destination_id",
    "result_path",
    "destination_text",
    "start_date_text",
    "end_date_text",
    "occupancy_text",
    "room_scope",
  ];
  const normalizedReadbackDestination = String(
    readback && readback.destination || "",
  ).normalize("NFKD").replace(/\p{M}/gu, "").trim().toLowerCase();
  const normalizedEvidenceDestination = String(
    evidence && evidence.destination_text || "",
  ).normalize("NFKD").replace(/\p{M}/gu, "").trim().toLowerCase();
  const destinationAliases = new Set(["马富施", "马富士", "maafushi"]);
  const dateTextMatches = (value, expected) => {
    const actualDigits = String(value || "").replace(/\D/g, "");
    const expectedDigits = String(expected || "").replace(/\D/g, "");
    return Boolean(expectedDigits && actualDigits.includes(expectedDigits));
  };
  const occupancyText = String(evidence && evidence.occupancy_text || "");
  const adultsPattern = new RegExp(
    `(?:${Number(query && query.adults)}\\s*(?:名|位|个)?\\s*成人|` +
      `${Number(query && query.adults)}\\s*adults?)`,
    "i",
  );
  return Boolean(
    driver &&
    driver.provider === "qunar" &&
    driver.triggered === true &&
    driver.confirmation_scope === "confirmed_visible_search" &&
    driver.result_query_readback_confirmed === true &&
    driver.result_query_readback_scope ===
      "qunar_visible_result_form_fields" &&
    confirmed &&
    readback &&
    evidence &&
    canonicalInventoryJson(Object.keys(evidence).sort()) ===
      canonicalInventoryJson(exactEvidenceKeys.sort()) &&
    confirmed.destination === query.destination &&
    confirmed.start_date === query.start_date &&
    confirmed.end_date === query.end_date &&
    confirmed.adults === query.adults &&
    confirmed.rooms === query.rooms &&
    destinationAliases.has(normalizedReadbackDestination) &&
    readback.start_date === query.start_date &&
    readback.end_date === query.end_date &&
    readback.adults === query.adults &&
    readback.rooms === query.rooms &&
    evidence.provider_destination_id === citySlug &&
    evidence.result_path === `/city/${citySlug}` &&
    evidence.room_scope ===
      "audited_qunar_single_room_search_surface" &&
    destinationAliases.has(normalizedEvidenceDestination) &&
    dateTextMatches(evidence.start_date_text, query.start_date) &&
    dateTextMatches(evidence.end_date_text, query.end_date) &&
    adultsPattern.test(occupancyText) &&
    /(?:0\s*(?:名|位|个)?\s*儿童|0\s*children)/i.test(occupancyText)
  );
}

async function validateQunarParserInventoryReceipt(
  receipt,
  receiptSha256,
  query,
  driver,
) {
  const rejected = (reason) => ({ valid: false, reason });
  const exactKeys = (value, keys) => Boolean(
    value &&
    typeof value === "object" &&
    !Array.isArray(value) &&
    canonicalInventoryJson(Object.keys(value).sort()) ===
      canonicalInventoryJson([...keys].sort()),
  );
  if (
    !exactKeys(receipt, [
      "schema_version",
      "parser_version",
      "provider",
      "state",
      "confirmed_query",
      "confirmation_scope",
      "scan_limit",
      "scanned_count",
      "candidate_summaries",
      "explicit_empty_evidence",
      "provider_pending_evidence",
      "page_url",
      "captured_at",
    ]) ||
    !exactKeys(receipt && receipt.confirmed_query, [
      "destination",
      "start_date",
      "end_date",
      "adults",
      "rooms",
      "options",
    ]) ||
    !exactKeys(receipt && receipt.confirmed_query &&
      receipt.confirmed_query.options, [
      "expected_lodging_place_key",
      "expected_package_area",
      "segment",
    ])
  ) {
    return rejected("receipt_shape_invalid");
  }
  const confirmed = receipt.confirmed_query;
  const expectedPlaceKey = String(
    query && query.options && query.options.expected_lodging_place_key || "",
  ).trim().toLowerCase();
  const expectedDestination = QUNAR_AUDITED_LODGING_DETAILS[expectedPlaceKey];
  const confirmedEmpty = receipt.state === "confirmed_empty";
  const boundedProviderPending =
    receipt.state === "bounded_provider_pending";
  const emptyEvidenceShapeValid = confirmedEmpty &&
    exactKeys(receipt.explicit_empty_evidence, [
      "contract_version",
      "result_count_text",
      "empty_message",
    ]);
  const pendingEvidenceShapeValid = boundedProviderPending &&
    exactKeys(receipt.provider_pending_evidence, [
      "contract_version",
      "result_count_text",
      "pending_message",
      "observed_duration_ms",
    ]);
  if (
    receipt.schema_version !== LODGING_INVENTORY_RECEIPT_SCHEMA_VERSION ||
    receipt.parser_version !== LODGING_INVENTORY_RECEIPT_PARSER_VERSION ||
    receipt.provider !== "qunar" ||
    (!confirmedEmpty && !boundedProviderPending) ||
    receipt.confirmation_scope !== "confirmed_visible_search" ||
    !expectedDestination ||
    !exactLodgingQueryConfirmed(query, driver) ||
    !qunarExactResultReadbackDriverValid(
      driver,
      query,
      expectedDestination.city_slug,
    ) ||
    confirmed.destination !== query.destination ||
    confirmed.start_date !== query.start_date ||
    confirmed.end_date !== query.end_date ||
    confirmed.adults !== query.adults ||
    confirmed.rooms !== query.rooms ||
    confirmed.options.expected_lodging_place_key !== expectedPlaceKey ||
    confirmed.options.expected_package_area !==
      query.options.expected_package_area ||
    confirmed.options.segment !== query.options.segment ||
    receipt.scan_limit !== MAX_CTRIP_LODGING_PREVIEW_CANDIDATES ||
    receipt.scanned_count !== 0 ||
    !Array.isArray(receipt.candidate_summaries) ||
    receipt.candidate_summaries.length !== 0 ||
    !(
      emptyEvidenceShapeValid &&
      receipt.provider_pending_evidence === null &&
      receipt.explicit_empty_evidence.contract_version ===
        QUNAR_EXPLICIT_EMPTY_CONTRACT_VERSION &&
      receipt.explicit_empty_evidence.result_count_text ===
        QUNAR_EXPLICIT_EMPTY_RESULT_COUNT_TEXT &&
      receipt.explicit_empty_evidence.empty_message ===
        QUNAR_EXPLICIT_EMPTY_MESSAGE
    ) &&
    !(
      pendingEvidenceShapeValid &&
      receipt.explicit_empty_evidence === null &&
      receipt.provider_pending_evidence.contract_version ===
        QUNAR_PENDING_CONTRACT_VERSION &&
      receipt.provider_pending_evidence.result_count_text ===
        QUNAR_PENDING_RESULT_COUNT_TEXT &&
      receipt.provider_pending_evidence.pending_message ===
        QUNAR_PENDING_MESSAGE &&
      Number.isInteger(
        receipt.provider_pending_evidence.observed_duration_ms,
      ) &&
      receipt.provider_pending_evidence.observed_duration_ms >=
        QUNAR_PENDING_MIN_OBSERVED_MS &&
      receipt.provider_pending_evidence.observed_duration_ms <=
        QUNAR_PENDING_MAX_OBSERVED_MS
    ) ||
    receipt.page_url !==
      `https://hotel.qunar.com/city/${expectedDestination.city_slug}/` ||
    typeof receipt.captured_at !== "string" ||
    Number.isNaN(Date.parse(receipt.captured_at))
  ) {
    return rejected("inventory_receipt_contract_invalid");
  }
  const expectedSha256 = await inventoryReceiptSha256(
    canonicalInventoryJson(receipt),
  );
  if (
    !/^[a-f0-9]{64}$/.test(String(receiptSha256 || "")) ||
    receiptSha256 !== expectedSha256
  ) {
    return rejected("receipt_sha256_mismatch");
  }
  return {
    valid: true,
    reason: "valid",
    receipt_sha256: expectedSha256,
    inventory_state: receipt.state,
  };
}

async function validateQunarParserConfirmedEmptyReceipt(
  receipt,
  receiptSha256,
  query,
  driver,
) {
  if (!receipt || receipt.state !== "confirmed_empty") {
    return { valid: false, reason: "receipt_not_confirmed_empty" };
  }
  return validateQunarParserInventoryReceipt(
    receipt,
    receiptSha256,
    query,
    driver,
  );
}

function shouldTryQunarLodgingDetailOrchestration(
  extraction,
  lease,
  pageUrl,
  driver,
) {
  const placeKey = String(
    lease && lease.query && lease.query.options &&
    lease.query.options.expected_lodging_place_key || "",
  ).trim().toLowerCase();
  const destination = QUNAR_AUDITED_LODGING_DETAILS[placeKey];
  let parsed;
  try {
    parsed = new URL(pageUrl);
  } catch {
    return false;
  }
  return Boolean(
    lease &&
    lease.provider === "qunar" &&
    lease.kind === "lodging" &&
    destination &&
    parsed.protocol === "https:" &&
    parsed.hostname.toLowerCase() === "hotel.qunar.com" &&
    !parsed.port &&
    !parsed.username &&
    !parsed.password &&
    parsed.pathname.replace(/\/+$/, "") ===
      `/city/${destination.city_slug}` &&
    exactLodgingQueryConfirmed(lease.query, driver) &&
    driver.result_query_readback_scope ===
      "qunar_visible_result_form_fields" &&
    driver.result_query_readback_evidence &&
    driver.result_query_readback_evidence.provider_destination_id ===
      destination.city_slug &&
    qunarInventoryExtractionFingerprint(extraction)
  );
}

function validSha256(value) {
  return typeof value === "string" && /^[a-f0-9]{64}$/.test(value);
}

function exactInventoryObjectKeys(value, keys) {
  return Boolean(
    value &&
    typeof value === "object" &&
    !Array.isArray(value) &&
    canonicalInventoryJson(Object.keys(value).sort()) ===
      canonicalInventoryJson([...keys].sort()),
  );
}

function validQunarObservationLineage(lineage) {
  return Boolean(
    exactInventoryObjectKeys(lineage, [
      "schema_version",
      "isolation_scope",
      "runtime_lineage_sha256",
      "window_lineage_sha256",
      "tab_lineage_sha256",
    ]) &&
    lineage.schema_version === QUNAR_OBSERVATION_LINEAGE_VERSION &&
    lineage.isolation_scope === QUNAR_LODGING_ISOLATION_SCOPE &&
    validSha256(lineage.runtime_lineage_sha256) &&
    validSha256(lineage.window_lineage_sha256) &&
    validSha256(lineage.tab_lineage_sha256)
  );
}

async function qunarObservationLineage(tabId, ownedTabIds) {
  if (
    !Number.isInteger(tabId) ||
    !(ownedTabIds instanceof Set) ||
    !ownedTabIds.has(tabId)
  ) {
    return null;
  }
  let tab;
  let window;
  try {
    tab = await chrome.tabs.get(tabId);
    window = await chrome.windows.get(tab.windowId);
  } catch {
    return null;
  }
  if (
    !tab ||
    tab.id !== tabId ||
    !Number.isInteger(tab.windowId) ||
    tab.active !== true ||
    !window ||
    window.id !== tab.windowId ||
    window.focused !== false ||
    window.state !== "normal"
  ) {
    return null;
  }
  const runtimeLineageSha256 = await inventoryReceiptSha256(
    `runtime:${RUNTIME_INSTANCE_ID}`,
  );
  const windowLineageSha256 = await inventoryReceiptSha256(
    `runtime:${RUNTIME_INSTANCE_ID}:window:${tab.windowId}`,
  );
  const tabLineageSha256 = await inventoryReceiptSha256(
    `runtime:${RUNTIME_INSTANCE_ID}:window:${tab.windowId}:tab:${tab.id}`,
  );
  return {
    schema_version: QUNAR_OBSERVATION_LINEAGE_VERSION,
    isolation_scope: QUNAR_LODGING_ISOLATION_SCOPE,
    runtime_lineage_sha256: runtimeLineageSha256,
    window_lineage_sha256: windowLineageSha256,
    tab_lineage_sha256: tabLineageSha256,
  };
}

function qunarDetailFallbackSummary(
  detailResults,
  verifiedQuoteCount,
  query,
) {
  const targets = qunarAuditedLodgingDetailTargets(query);
  const expectedPropertyIds = targets
    .map(({ property_id: propertyId }) => propertyId);
  const byPropertyId = new Map(
    (Array.isArray(detailResults) ? detailResults : [])
      .filter((item) => item && typeof item === "object")
      .map((item) => [String(item.property_id || ""), item]),
  );
  const observedResults = expectedPropertyIds.map((propertyId) => {
    const item = byPropertyId.get(propertyId) || {};
    const state = [
      "succeeded",
      "failed",
      "missing",
      "target_rejected",
      "redirect_or_isolation_rejected",
    ].includes(item.state)
      ? item.state
      : "missing";
    return {
      property_id: propertyId,
      state,
      verified_quote_count:
        Number.isInteger(item.verified_quote_count) &&
        item.verified_quote_count >= 0
          ? item.verified_quote_count
          : 0,
    };
  });
  return {
    contract_version: QUNAR_DETAIL_FALLBACK_SUMMARY_VERSION,
    attempted: true,
    target_limit: MAX_QUNAR_LODGING_DETAIL_PAGES_PER_LEASE,
    seed_selection_policy: QUNAR_DETAIL_SEED_SELECTION_POLICY,
    seed_selection_offset: targets.length
      ? targets[0].seed_selection_offset
      : null,
    target_property_ids: expectedPropertyIds,
    observed_results: observedResults,
    verified_quote_count: Number.isInteger(verifiedQuoteCount)
      ? verifiedQuoteCount
      : 0,
  };
}

function validQunarDetailFallbackSummary(summary, query) {
  const expectedPropertyIds = qunarAuditedLodgingDetailTargets(query)
    .map(({ property_id: propertyId }) => propertyId);
  const allowedStates = new Set([
    "succeeded",
    "failed",
    "missing",
    "target_rejected",
    "redirect_or_isolation_rejected",
  ]);
  return Boolean(
    expectedPropertyIds.length ===
      MAX_QUNAR_LODGING_DETAIL_PAGES_PER_LEASE &&
    exactInventoryObjectKeys(summary, [
      "contract_version",
      "attempted",
      "target_limit",
      "seed_selection_policy",
      "seed_selection_offset",
      "target_property_ids",
      "observed_results",
      "verified_quote_count",
    ]) &&
    summary.contract_version === QUNAR_DETAIL_FALLBACK_SUMMARY_VERSION &&
    summary.attempted === true &&
    summary.target_limit === MAX_QUNAR_LODGING_DETAIL_PAGES_PER_LEASE &&
    summary.seed_selection_policy === QUNAR_DETAIL_SEED_SELECTION_POLICY &&
    summary.seed_selection_offset ===
      qunarLodgingDetailSeedOffset(
        query,
        QUNAR_AUDITED_LODGING_DETAILS.maafushi.properties.length,
      ) &&
    canonicalInventoryJson(summary.target_property_ids) ===
      canonicalInventoryJson(expectedPropertyIds) &&
    Array.isArray(summary.observed_results) &&
    summary.observed_results.length === expectedPropertyIds.length &&
    summary.observed_results.every(
      (item, index) =>
        exactInventoryObjectKeys(item, [
          "property_id",
          "state",
          "verified_quote_count",
        ]) &&
        item.property_id === expectedPropertyIds[index] &&
        allowedStates.has(item.state) &&
        Number.isInteger(item.verified_quote_count) &&
        item.verified_quote_count >= 0 &&
        item.verified_quote_count <= MAX_CTRIP_LODGING_PREVIEW_CANDIDATES,
    ) &&
    Number.isInteger(summary.verified_quote_count) &&
    summary.verified_quote_count >= 0 &&
    summary.verified_quote_count <= MAX_CTRIP_LODGING_PREVIEW_CANDIDATES &&
    summary.observed_results.reduce(
      (total, item) => total + item.verified_quote_count,
      0,
    ) === summary.verified_quote_count
  );
}

async function validateQunarSealedConfirmedEmptyReceipt(
  receipt,
  receiptSha256,
  query,
  driver,
) {
  const rejected = (reason) => ({ valid: false, reason });
  if (
    !exactInventoryObjectKeys(receipt, [
      "schema_version",
      "parser_version",
      "provider",
      "state",
      "confirmed_query",
      "confirmation_scope",
      "scan_limit",
      "scanned_count",
      "candidate_summaries",
      "explicit_empty_evidence",
      "provider_pending_evidence",
      "page_url",
      "captured_at",
      "observation_chain",
    ]) ||
    receipt.schema_version !==
      LODGING_INVENTORY_SEALED_RECEIPT_SCHEMA_VERSION ||
    receipt.state !== "confirmed_empty"
  ) {
    return rejected("sealed_receipt_shape_invalid");
  }
  const chain = receipt.observation_chain;
  if (
    !exactInventoryObjectKeys(chain, [
      "schema_version",
      "query_fingerprint_sha256",
      "observations",
      "observed_interval_ms",
      "detail_fallback",
      "sealed_at",
    ]) ||
    chain.schema_version !== QUNAR_EXPLICIT_EMPTY_OBSERVATION_CHAIN_VERSION ||
    !validSha256(chain.query_fingerprint_sha256) ||
    !Array.isArray(chain.observations) ||
    chain.observations.length !== 2 ||
    !Number.isInteger(chain.observed_interval_ms) ||
    chain.observed_interval_ms < QUNAR_EXPLICIT_EMPTY_STABILITY_MIN_INTERVAL_MS ||
    chain.observed_interval_ms > QUNAR_PENDING_MAX_OBSERVED_MS ||
    !validQunarDetailFallbackSummary(chain.detail_fallback, query) ||
    typeof chain.sealed_at !== "string" ||
    Number.isNaN(Date.parse(chain.sealed_at))
  ) {
    return rejected("observation_chain_contract_invalid");
  }
  const observations = chain.observations;
  for (const [index, observation] of observations.entries()) {
    if (
      !exactInventoryObjectKeys(observation, [
        "ordinal",
        "receipt",
        "receipt_sha256",
        "captured_at",
        "query_fingerprint_sha256",
        "lineage",
      ]) ||
      observation.ordinal !== index + 1 ||
      !validSha256(observation.receipt_sha256) ||
      typeof observation.captured_at !== "string" ||
      Number.isNaN(Date.parse(observation.captured_at)) ||
      !validSha256(observation.query_fingerprint_sha256) ||
      !validQunarObservationLineage(observation.lineage)
    ) {
      return rejected("observation_contract_invalid");
    }
    const childValidation = await validateQunarParserConfirmedEmptyReceipt(
      observation.receipt,
      observation.receipt_sha256,
      query,
      driver,
    );
    const queryFingerprint = await inventoryReceiptSha256(
      canonicalInventoryJson(observation.receipt.confirmed_query),
    );
    if (
      !childValidation.valid ||
      observation.captured_at !== observation.receipt.captured_at ||
      observation.query_fingerprint_sha256 !== queryFingerprint ||
      observation.query_fingerprint_sha256 !== chain.query_fingerprint_sha256
    ) {
      return rejected("observation_receipt_invalid");
    }
  }
  const first = observations[0];
  const second = observations[1];
  const intervalMs =
    Date.parse(second.captured_at) - Date.parse(first.captured_at);
  if (
    !Number.isFinite(intervalMs) ||
    intervalMs !== chain.observed_interval_ms ||
    intervalMs < QUNAR_EXPLICIT_EMPTY_STABILITY_MIN_INTERVAL_MS ||
    canonicalInventoryJson(first.lineage) !==
      canonicalInventoryJson(second.lineage) ||
    Date.parse(chain.sealed_at) < Date.parse(second.captured_at)
  ) {
    return rejected("observation_order_or_lineage_invalid");
  }
  const expectedParent = {
    ...second.receipt,
    schema_version: LODGING_INVENTORY_SEALED_RECEIPT_SCHEMA_VERSION,
    observation_chain: chain,
  };
  if (canonicalInventoryJson(receipt) !== canonicalInventoryJson(expectedParent)) {
    return rejected("parent_receipt_does_not_seal_second_observation");
  }
  const expectedSha256 = await inventoryReceiptSha256(
    canonicalInventoryJson(receipt),
  );
  if (!validSha256(receiptSha256) || expectedSha256 !== receiptSha256) {
    return rejected("sealed_receipt_sha256_mismatch");
  }
  return {
    valid: true,
    reason: "valid",
    receipt_sha256: expectedSha256,
  };
}

async function sealQunarConfirmedEmptyObservationChain({
  observation,
  query,
  driver,
  detailResults,
  verifiedQuoteCount,
  clock = Date.now,
}) {
  const seed = observation && observation.observation_chain_seed;
  if (
    !observation ||
    observation.state !== "stable_confirmed_empty" ||
    !seed
  ) {
    return null;
  }
  const fallbackSummary = qunarDetailFallbackSummary(
    detailResults,
    verifiedQuoteCount,
    query,
  );
  const secondCapturedMs = Date.parse(seed.second_receipt.captured_at);
  const sealedAt = new Date(Math.max(clock(), secondCapturedMs)).toISOString();
  const chain = {
    schema_version: QUNAR_EXPLICIT_EMPTY_OBSERVATION_CHAIN_VERSION,
    query_fingerprint_sha256: seed.query_fingerprint_sha256,
    observations: [
      {
        ordinal: 1,
        receipt: seed.first_receipt,
        receipt_sha256: seed.first_receipt_sha256,
        captured_at: seed.first_receipt.captured_at,
        query_fingerprint_sha256: seed.query_fingerprint_sha256,
        lineage: seed.first_lineage,
      },
      {
        ordinal: 2,
        receipt: seed.second_receipt,
        receipt_sha256: seed.second_receipt_sha256,
        captured_at: seed.second_receipt.captured_at,
        query_fingerprint_sha256: seed.query_fingerprint_sha256,
        lineage: seed.second_lineage,
      },
    ],
    observed_interval_ms: seed.receipt_interval_ms,
    detail_fallback: fallbackSummary,
    sealed_at: sealedAt,
  };
  const receipt = {
    ...seed.second_receipt,
    schema_version: LODGING_INVENTORY_SEALED_RECEIPT_SCHEMA_VERSION,
    observation_chain: chain,
  };
  const receiptSha256 = await inventoryReceiptSha256(
    canonicalInventoryJson(receipt),
  );
  const validation = await validateQunarSealedConfirmedEmptyReceipt(
    receipt,
    receiptSha256,
    query,
    driver,
  );
  return validation.valid
    ? { receipt, receipt_sha256: receiptSha256 }
    : null;
}

function attachQunarSealedReceiptToFailure(extraction, sealed) {
  if (!extraction || !extraction.failure || !sealed) {
    return null;
  }
  return {
    ...extraction,
    failure: {
      ...extraction.failure,
      page_url: sealed.receipt.page_url,
      captured_at: sealed.receipt.captured_at,
      details: {
        ...(extraction.failure.details || {}),
        inventory_result_state: "confirmed_empty",
        confirmed_exhaustive: true,
        inventory_receipt: sealed.receipt,
        inventory_receipt_sha256: sealed.receipt_sha256,
        inventory_observation_chain_schema_version:
          QUNAR_EXPLICIT_EMPTY_OBSERVATION_CHAIN_VERSION,
      },
    },
  };
}

function attachQunarSealedReceiptToQuote(quote, sealed) {
  const details = quote && quote.details;
  const driver = details && details.driver;
  const capture = driver && driver.qunar_detail_capture;
  if (!quote || !details || !driver || !capture || !sealed) {
    return null;
  }
  return {
    ...quote,
    details: {
      ...details,
      driver: {
        ...driver,
        qunar_detail_capture: {
          ...capture,
          list_inventory_receipt: sealed.receipt,
          list_inventory_receipt_sha256: sealed.receipt_sha256,
          list_inventory_receipt_schema_version:
            LODGING_INVENTORY_SEALED_RECEIPT_SCHEMA_VERSION,
          inventory_observation_chain_schema_version:
            QUNAR_EXPLICIT_EMPTY_OBSERVATION_CHAIN_VERSION,
        },
      },
    },
  };
}

async function observeQunarInventoryForDetailFallback({
  extraction,
  lease,
  driver,
  tabId,
  deadline,
  ownedTabIds,
  extractor = null,
  wait = delay,
  clock = Date.now,
  assertActive = assertLeaseActive,
  lineageResolver = qunarObservationLineage,
}) {
  const details =
    extraction && extraction.failure && extraction.failure.details || {};
  const receipt = details.inventory_receipt;
  const fingerprint = qunarInventoryExtractionFingerprint(extraction);
  const validation = fingerprint
    ? await validateQunarParserInventoryReceipt(
      receipt,
      details.inventory_receipt_sha256,
      lease.query,
      driver,
    )
    : { valid: false, reason: "inventory_observation_missing" };
  if (!fingerprint || !validation.valid) {
    return {
      state: "invalid_inventory_observation",
      extraction,
      diagnostic: {
        inventory_observation_state:
          receipt && typeof receipt.state === "string"
            ? receipt.state
            : null,
        observation_count: receipt ? 1 : 0,
        observed_duration_ms: null,
        receipt_validation: validation.reason,
      },
    };
  }
  if (receipt.state === "bounded_provider_pending") {
    return {
      state: "bounded_provider_pending",
      extraction,
      diagnostic: {
        inventory_observation_state: "bounded_provider_pending",
        observation_count: 1,
        observed_duration_ms:
          receipt.provider_pending_evidence.observed_duration_ms,
        receipt_validation: validation.reason,
        provider_pending_contract_version:
          receipt.provider_pending_evidence.contract_version,
      },
    };
  }
  const stable = await stabilizeQunarExplicitEmpty({
    extraction,
    lease,
    driver,
    tabId,
    deadline,
    ownedTabIds,
    extractor,
    wait,
    clock,
    assertActive,
    lineageResolver,
  });
  return {
    ...stable,
    diagnostic: {
      ...stable.diagnostic,
      inventory_observation_state: "confirmed_empty",
      observed_duration_ms:
        Number.isInteger(stable.diagnostic.receipt_interval_ms)
          ? stable.diagnostic.receipt_interval_ms
          : null,
      receipt_validation: validation.reason,
    },
  };
}

async function stabilizeQunarExplicitEmpty({
  extraction,
  lease,
  driver,
  tabId,
  deadline,
  ownedTabIds,
  extractor = null,
  wait = delay,
  clock = Date.now,
  assertActive = assertLeaseActive,
  lineageResolver = qunarObservationLineage,
}) {
  const firstFingerprint =
    qunarConfirmedEmptyExtractionFingerprint(extraction);
  const firstDetails =
    extraction && extraction.failure && extraction.failure.details || {};
  const firstValidation = firstFingerprint
    ? await validateQunarParserConfirmedEmptyReceipt(
      firstDetails.inventory_receipt,
      firstDetails.inventory_receipt_sha256,
      lease.query,
      driver,
    )
    : { valid: false, reason: "first_observation_not_confirmed_empty" };
  if (!firstFingerprint || !firstValidation.valid) {
    return {
      state: "invalid_first_observation",
      extraction,
      diagnostic: {
        observation_count: 1,
        minimum_interval_ms: QUNAR_EXPLICIT_EMPTY_STABILITY_MIN_INTERVAL_MS,
        first_receipt_validation: firstValidation.reason,
      },
    };
  }
  const firstLineage = await lineageResolver(tabId, ownedTabIds);
  if (!validQunarObservationLineage(firstLineage)) {
    return {
      state: "invalid_first_observation_lineage",
      extraction,
      diagnostic: {
        observation_count: 1,
        minimum_interval_ms: QUNAR_EXPLICIT_EMPTY_STABILITY_MIN_INTERVAL_MS,
        first_receipt_validation: firstValidation.reason,
        first_lineage_valid: false,
      },
    };
  }
  const firstQueryFingerprint = await inventoryReceiptSha256(
    canonicalInventoryJson(firstDetails.inventory_receipt.confirmed_query),
  );
  const startedAt = clock();
  await wait(QUNAR_EXPLICIT_EMPTY_STABILITY_MIN_INTERVAL_MS);
  const observedIntervalMs = Math.max(0, clock() - startedAt);
  if (observedIntervalMs < QUNAR_EXPLICIT_EMPTY_STABILITY_MIN_INTERVAL_MS) {
    return {
      state: "interval_unverified",
      extraction,
      diagnostic: {
        observation_count: 1,
        minimum_interval_ms: QUNAR_EXPLICIT_EMPTY_STABILITY_MIN_INTERVAL_MS,
        observed_interval_ms: observedIntervalMs,
        first_receipt_validation: firstValidation.reason,
      },
    };
  }
  await assertActive(
    lease,
    remainingTimeout(deadline, 5000),
  );
  const secondExtraction = extractor
    ? await extractor()
    : await extractWithRetry(
      tabId,
      lease,
      driver,
      deadline,
      ownedTabIds,
    );
  if (
    secondExtraction &&
    secondExtraction.state === "succeeded" &&
    Array.isArray(secondExtraction.quotes) &&
    secondExtraction.quotes.length > 0
  ) {
    return {
      state: "inventory_hydrated",
      extraction: secondExtraction,
      diagnostic: {
        observation_count: 2,
        minimum_interval_ms: QUNAR_EXPLICIT_EMPTY_STABILITY_MIN_INTERVAL_MS,
        observed_interval_ms: observedIntervalMs,
        first_receipt_validation: firstValidation.reason,
      },
    };
  }
  const secondFingerprint =
    qunarConfirmedEmptyExtractionFingerprint(secondExtraction);
  const secondDetails =
    secondExtraction && secondExtraction.failure &&
    secondExtraction.failure.details || {};
  const secondValidation = secondFingerprint
    ? await validateQunarParserConfirmedEmptyReceipt(
      secondDetails.inventory_receipt,
      secondDetails.inventory_receipt_sha256,
      lease.query,
      driver,
    )
    : { valid: false, reason: "second_observation_not_confirmed_empty" };
  const secondLineage = await lineageResolver(tabId, ownedTabIds);
  const secondQueryFingerprint = secondDetails.inventory_receipt
    ? await inventoryReceiptSha256(
      canonicalInventoryJson(
        secondDetails.inventory_receipt.confirmed_query,
      ),
    )
    : null;
  const firstCapturedAt = Date.parse(
    String(firstDetails.inventory_receipt.captured_at || ""),
  );
  const secondCapturedAt = Date.parse(
    String(
      secondDetails.inventory_receipt &&
      secondDetails.inventory_receipt.captured_at || "",
    ),
  );
  const receiptIntervalMs = secondCapturedAt - firstCapturedAt;
  const stable = Boolean(
    secondFingerprint &&
    secondValidation.valid &&
    firstFingerprint === secondFingerprint &&
    validQunarObservationLineage(secondLineage) &&
    canonicalInventoryJson(firstLineage) ===
      canonicalInventoryJson(secondLineage) &&
    validSha256(firstQueryFingerprint) &&
    firstQueryFingerprint === secondQueryFingerprint &&
    Number.isFinite(receiptIntervalMs) &&
    receiptIntervalMs >= QUNAR_EXPLICIT_EMPTY_STABILITY_MIN_INTERVAL_MS
  );
  return {
    state: stable ? "stable_confirmed_empty" : "unstable_empty_observation",
    extraction: secondExtraction,
    observation_chain_seed: stable
      ? {
        first_receipt: firstDetails.inventory_receipt,
        first_receipt_sha256:
          firstDetails.inventory_receipt_sha256,
        first_lineage: firstLineage,
        second_receipt: secondDetails.inventory_receipt,
        second_receipt_sha256:
          secondDetails.inventory_receipt_sha256,
        second_lineage: secondLineage,
        query_fingerprint_sha256: firstQueryFingerprint,
        receipt_interval_ms: receiptIntervalMs,
      }
      : null,
    diagnostic: {
      observation_count: 2,
      minimum_interval_ms: QUNAR_EXPLICIT_EMPTY_STABILITY_MIN_INTERVAL_MS,
      observed_interval_ms: observedIntervalMs,
      receipt_interval_ms: Number.isFinite(receiptIntervalMs)
        ? receiptIntervalMs
        : null,
      first_receipt_validation: firstValidation.reason,
      second_receipt_validation: secondValidation.reason,
      evidence_equal: firstFingerprint === secondFingerprint,
      query_fingerprint_equal:
        firstQueryFingerprint === secondQueryFingerprint,
      controlled_lineage_equal:
        validQunarObservationLineage(secondLineage) &&
        canonicalInventoryJson(firstLineage) ===
          canonicalInventoryJson(secondLineage),
    },
  };
}

function qunarExactLodgingDetailQuoteDecision(quote, target, query) {
  const details =
    quote && quote.details && typeof quote.details === "object"
      ? quote.details
      : {};
  const urlDecision = qunarLodgingDetailUrlDecision(
    quote && quote.page_url,
    query,
    target,
  );
  const checks = {
    detail_url_allowed: urlDecision.allowed === true,
    detail_url_reason: urlDecision.reason,
    provider_matches: quote && quote.provider === "qunar",
    kind_matches: quote && quote.kind === "lodging",
    positive_amount: Number(quote && quote.amount) > 0,
    currency_matches: quote && quote.currency === query.currency,
    visible_evidence_sealed:
      Boolean(quote && quote.visible_evidence) &&
      /^[a-f0-9]{64}$/i.test(String(quote && quote.evidence_sha256 || "")),
    usable_price_basis:
      quote && ["per_night", "total_stay"].includes(quote.price_basis),
    tax_contract:
      quote && quote.taxes_included === true &&
      Boolean(String(details.tax_evidence || "").trim()),
    extraction_contract:
      details.extraction === "visible_dom_qunar_lodging_detail",
    price_basis_contract:
      details.price_basis_source ===
        "audited_qunar_lodging_detail_rate_contract",
    price_finality: details.price_finality === "final_for_rate",
    city_slug_matches: details.city_slug === target.city_slug,
    hotel_seq_matches: details.hotel_seq === target.hotel_seq,
    property_id_matches: details.property_id === target.property_id,
    property_name_matches: details.property_name === target.property_name,
    room_rate_visible:
      Boolean(String(details.room_text || "").trim()) &&
      Boolean(String(details.rate_text || "").trim()) &&
      Boolean(String(details.price_unit_evidence || "").trim()) &&
      details.availability === "available" &&
      Boolean(String(details.availability_text || "").trim()),
    place_matches:
      details.expected_lodging_place_key === "maafushi" &&
      details.observed_lodging_place_key === "maafushi" &&
      details.lodging_place_matches_expected === true &&
      details.kaafu_area_confirmed === true,
    check_in_matches: details.check_in === query.start_date,
    check_out_matches: details.check_out === query.end_date,
    adults_match: details.adults === query.adults,
    rooms_match: details.rooms === query.rooms,
    booking_not_clicked: details.clicked_booking === false,
  };
  return {
    allowed: Object.entries(checks)
      .filter(([key]) => !key.endsWith("_reason"))
      .every(([, value]) => value === true),
    checks,
  };
}

function attachQunarDetailFailureDiagnostic(extraction, diagnostic) {
  if (!extraction || !extraction.failure) {
    return extraction;
  }
  return {
    ...extraction,
    failure: {
      ...extraction.failure,
      details: {
        ...(extraction.failure.details || {}),
        detail_orchestration: diagnostic,
      },
    },
  };
}

async function orchestrateQunarLodgingDetails(
  listTabId,
  lease,
  driver,
  deadline,
  ownedTabIds,
  listExtraction,
  stabilizationOverrides = {},
) {
  const detailExtractor =
    typeof stabilizationOverrides.detailExtractor === "function"
      ? stabilizationOverrides.detailExtractor
      : extractWithRetry;
  const leaseAssert =
    typeof stabilizationOverrides.assertActive === "function"
      ? stabilizationOverrides.assertActive
      : assertLeaseActive;
  const diagnostic = {
    source: "qunar_audited_read_only_lodging_detail",
    contract_scope: "audited_qunar_exact_detail_url",
    clicked_booking: false,
    same_controlled_tab: true,
    new_tab_created: false,
    new_window_created: false,
    target_limit: MAX_QUNAR_LODGING_DETAIL_PAGES_PER_LEASE,
    state: "pending",
    inventory_observation: null,
    detail_results: [],
  };
  let listTab;
  let isolatedWindow;
  try {
    listTab = await chrome.tabs.get(listTabId);
    isolatedWindow = await chrome.windows.get(listTab.windowId);
  } catch (error) {
    diagnostic.state = "isolation_read_failed";
    diagnostic.failure = String(error && error.message || error).slice(0, 240);
    return attachQunarDetailFailureDiagnostic(
      listExtraction,
      diagnostic,
    );
  }
  if (
    !listTab.active ||
    !isolatedWindow ||
    isolatedWindow.focused !== false ||
    isolatedWindow.state !== "normal" ||
    !driver.browser_isolation ||
    driver.browser_isolation.scope !== QUNAR_LODGING_ISOLATION_SCOPE ||
    driver.browser_isolation.reused_user_window !== false
  ) {
    diagnostic.state = "isolation_contract_rejected";
    diagnostic.isolation = {
      tab_active: listTab.active === true,
      window_focused:
        isolatedWindow && typeof isolatedWindow.focused === "boolean"
          ? isolatedWindow.focused
          : null,
      window_state:
        isolatedWindow && typeof isolatedWindow.state === "string"
          ? isolatedWindow.state
          : null,
    };
    return attachQunarDetailFailureDiagnostic(
      listExtraction,
      diagnostic,
    );
  }
  const observation = await observeQunarInventoryForDetailFallback({
    extraction: listExtraction,
    lease,
    driver,
    tabId: listTabId,
    deadline,
    ownedTabIds,
    ...stabilizationOverrides,
  });
  diagnostic.inventory_observation = observation.diagnostic;
  if (observation.state === "inventory_hydrated") {
    return {
      ...observation.extraction,
      detail_orchestration: {
        ...diagnostic,
        state: "list_inventory_hydrated_before_detail",
      },
    };
  }
  const fallbackEligible = [
    "stable_confirmed_empty",
    "bounded_provider_pending",
  ].includes(observation.state);
  if (!fallbackEligible) {
    diagnostic.state = observation.state;
    return failure(
      "failed",
      "dom_drift",
      "去哪儿酒店库存观察未通过严格状态与时长合同，未触发详情兜底",
      listExtraction && listExtraction.failure &&
        listExtraction.failure.page_url || null,
      false,
      {
        inventory_result_state: "bounded_no_exact_quote",
        confirmed_exhaustive: false,
        detail_orchestration: diagnostic,
      },
    );
  }
  const targets = qunarAuditedLodgingDetailTargets(lease.query);
  if (!targets.length) {
    diagnostic.state = "no_audited_targets";
    return attachQunarDetailFailureDiagnostic(
      listExtraction,
      diagnostic,
    );
  }
  diagnostic.seed_selection_policy = QUNAR_DETAIL_SEED_SELECTION_POLICY;
  diagnostic.seed_selection_offset = targets[0].seed_selection_offset;
  diagnostic.selected_seed_set = targets.map((target) => ({
    hotel_seq: target.hotel_seq,
    property_id: target.property_id,
    property_name: target.property_name,
  }));
  const quotes = [];
  for (const target of targets) {
    const detailDiagnostic = {
      hotel_seq: target.hotel_seq,
      property_id: target.property_id,
      property_name: target.property_name,
      target_url: target.url,
      state: "pending",
      verified_quote_count: 0,
      quote_checks: [],
    };
    try {
      const preNavigation = qunarLodgingDetailUrlDecision(
        target.href,
        lease.query,
        target,
      );
      if (!preNavigation.allowed) {
        detailDiagnostic.state = "target_rejected";
        detailDiagnostic.reason = preNavigation.reason;
        diagnostic.detail_results.push(detailDiagnostic);
        continue;
      }
      await chrome.tabs.update(listTabId, { url: target.href });
      await waitForTabInteractive(
        listTabId,
        remainingTimeout(deadline, 30000),
      );
      await installContent(listTabId);
      await leaseAssert(
        lease,
        remainingTimeout(deadline, 5000),
      );
      const current = await chrome.tabs.get(listTabId);
      const currentWindow = await chrome.windows.get(current.windowId);
      const finalUrl = current.pendingUrl || current.url || "";
      const postNavigation = qunarLodgingDetailUrlDecision(
        finalUrl,
        lease.query,
        target,
      );
      if (
        current.id !== listTabId ||
        current.windowId !== listTab.windowId ||
        current.active !== true ||
        currentWindow.focused !== false ||
        currentWindow.state !== "normal" ||
        !postNavigation.allowed
      ) {
        detailDiagnostic.state = "redirect_or_isolation_rejected";
        detailDiagnostic.reason = postNavigation.reason;
        detailDiagnostic.observed_url = navigationUrlEvidence(finalUrl);
        diagnostic.detail_results.push(detailDiagnostic);
        continue;
      }
      const detailDriver = {
        ...driver,
        mode: "captured_read_only_detail",
        qunar_detail_capture: {
          source: "qunar_audited_read_only_lodging_detail",
          contract_scope: "audited_qunar_exact_detail_url",
          clicked_booking: false,
          same_controlled_tab: true,
          city_slug: target.city_slug,
          hotel_seq: target.hotel_seq,
          property_id: target.property_id,
          property_name: target.property_name,
          seed_selection_policy: target.seed_selection_policy,
          seed_selection_offset: target.seed_selection_offset,
          target_property_ids: targets.map(
            ({ property_id: propertyId }) => propertyId,
          ),
          list_inventory_receipt:
            observation.extraction.failure.details.inventory_receipt,
          list_inventory_receipt_sha256:
            observation.extraction.failure.details.inventory_receipt_sha256,
          list_inventory_receipt_schema_version:
            observation.extraction.failure.details.inventory_receipt
              .schema_version,
          inventory_observation_chain_schema_version: null,
          inventory_observation_state:
            observation.diagnostic.inventory_observation_state,
          inventory_observation_count:
            observation.diagnostic.observation_count,
          inventory_observation_duration_ms:
            observation.diagnostic.observed_duration_ms,
        },
      };
      const detailExtraction = await detailExtractor(
        listTabId,
        lease,
        detailDriver,
        deadline,
        ownedTabIds,
      );
      detailDiagnostic.state =
        detailExtraction && detailExtraction.state || "missing";
      detailDiagnostic.failure_code =
        detailExtraction && detailExtraction.failure &&
        detailExtraction.failure.code || null;
      const parserFailureDetails =
        detailExtraction && detailExtraction.failure &&
        detailExtraction.failure.details &&
        typeof detailExtraction.failure.details === "object"
          ? detailExtraction.failure.details
          : null;
      const parserGates =
        parserFailureDetails && parserFailureDetails.gates &&
        typeof parserFailureDetails.gates === "object"
          ? parserFailureDetails.gates
          : null;
      detailDiagnostic.parser_gates = parserGates
        ? {
          provider_detail_url: parserGates.provider_detail_url === true,
          frozen_city_slug: parserGates.frozen_city_slug === true,
          allowlisted_hotel_seq: parserGates.allowlisted_hotel_seq === true,
          target_matches: parserGates.target_matches === true,
          result_list_lineage_matches:
            parserGates.result_list_lineage_matches === true,
          url_query_matches: parserGates.url_query_matches === true,
          exact_visible_search_confirmed:
            parserGates.exact_visible_search_confirmed === true,
          property_name_exact_visible:
            parserGates.property_name_exact_visible === true,
          maafushi_visible: parserGates.maafushi_visible === true,
          kaafu_atoll_visible: parserGates.kaafu_atoll_visible === true,
          visible_stay_readback: parserGates.visible_stay_readback === true,
          visible_occupancy_readback:
            parserGates.visible_occupancy_readback === true,
          clicked_booking: parserGates.clicked_booking === true,
        }
        : null;
      detailDiagnostic.property_samples =
        parserFailureDetails && Array.isArray(parserFailureDetails.property_samples)
          ? parserFailureDetails.property_samples.slice(0, 6)
          : [];
      detailDiagnostic.location_samples =
        parserFailureDetails && Array.isArray(parserFailureDetails.location_samples)
          ? parserFailureDetails.location_samples.slice(0, 6)
          : [];
      detailDiagnostic.occupancy_samples =
        parserFailureDetails && Array.isArray(parserFailureDetails.occupancy_samples)
          ? parserFailureDetails.occupancy_samples.slice(0, 8)
          : [];
      // The parser owns bounded redaction and sample limits. Preserve its
      // price-surface signature in the terminal bridge receipt so a live
      // `dom_drift` can be separated into provider-side no-price inventory,
      // safe selector drift, or a deliberately rejected non-final/tax-unknown
      // rate without retaining raw HTML.
      detailDiagnostic.rate_row_count =
        parserFailureDetails && Number.isInteger(parserFailureDetails.rate_row_count)
          ? parserFailureDetails.rate_row_count
          : null;
      detailDiagnostic.exact_price_row_count =
        parserFailureDetails &&
        Number.isInteger(parserFailureDetails.exact_price_row_count)
          ? parserFailureDetails.exact_price_row_count
          : null;
      detailDiagnostic.room_rate_contract =
        parserFailureDetails &&
        typeof parserFailureDetails.room_rate_contract === "boolean"
          ? parserFailureDetails.room_rate_contract
          : null;
      detailDiagnostic.rate_diagnostics =
        parserFailureDetails &&
        parserFailureDetails.rate_diagnostics &&
        typeof parserFailureDetails.rate_diagnostics === "object" &&
        !Array.isArray(parserFailureDetails.rate_diagnostics)
          ? parserFailureDetails.rate_diagnostics
          : null;
      detailDiagnostic.dom_diagnostics =
        parserFailureDetails &&
        parserFailureDetails.dom_diagnostics &&
        typeof parserFailureDetails.dom_diagnostics === "object" &&
        !Array.isArray(parserFailureDetails.dom_diagnostics)
          ? parserFailureDetails.dom_diagnostics
          : null;
      for (const quote of
        detailExtraction && Array.isArray(detailExtraction.quotes)
          ? detailExtraction.quotes
          : []) {
        const decision = qunarExactLodgingDetailQuoteDecision(
          quote,
          target,
          lease.query,
        );
        detailDiagnostic.quote_checks.push(decision.checks);
        if (decision.allowed) {
          detailDiagnostic.verified_quote_count += 1;
          quotes.push({
            ...quote,
            details: {
              ...(quote.details || {}),
              driver: detailDriver,
            },
          });
        }
      }
    } catch (error) {
      if (error && error.status === 409) throw error;
      detailDiagnostic.state = "failed";
      detailDiagnostic.failure_code =
        error && error.tripchordCode || "detail_read_failed";
      detailDiagnostic.failure =
        String(error && error.message || error).slice(0, 240);
    }
    diagnostic.detail_results.push(detailDiagnostic);
  }
  const finalDiagnostic = {
    ...diagnostic,
    state: quotes.length
      ? "verified_quote"
      : observation.state === "bounded_provider_pending"
        ? "bounded_pending_no_verified_detail_quote"
        : "stable_empty_no_verified_detail_quote",
    verified_quote_count: quotes.length,
  };
  const sealed = observation.state === "stable_confirmed_empty"
    ? await sealQunarConfirmedEmptyObservationChain({
      observation,
      query: lease.query,
      driver,
      detailResults: diagnostic.detail_results,
      verifiedQuoteCount: quotes.length,
    })
    : null;
  if (observation.state === "stable_confirmed_empty" && !sealed) {
    return failure(
      "failed",
      "dom_drift",
      "去哪儿双观察证据链封存或重算失败，拒绝升级为确认无房",
      observation.extraction && observation.extraction.failure &&
        observation.extraction.failure.page_url || null,
      false,
      {
        inventory_result_state: "bounded_no_exact_quote",
        confirmed_exhaustive: false,
        detail_orchestration: {
          ...finalDiagnostic,
          state: "confirmed_empty_chain_seal_failed",
        },
      },
    );
  }
  if (quotes.length) {
    const sealedQuotes = sealed
      ? quotes.map((quote) => attachQunarSealedReceiptToQuote(quote, sealed))
      : quotes;
    if (sealedQuotes.some((quote) => quote === null)) {
      return failure(
        "failed",
        "dom_drift",
        "去哪儿详情报价未能绑定双观察父回执，拒绝返回未封存报价",
        observation.extraction && observation.extraction.failure &&
          observation.extraction.failure.page_url || null,
        false,
        {
          inventory_result_state: "bounded_no_exact_quote",
          confirmed_exhaustive: false,
          detail_orchestration: {
            ...finalDiagnostic,
            state: "verified_quote_chain_binding_failed",
          },
        },
      );
    }
    return {
      state: "succeeded",
      quotes: sealedQuotes,
      detail_orchestration: {
        ...finalDiagnostic,
      },
    };
  }
  const terminalExtraction = sealed
    ? attachQunarSealedReceiptToFailure(observation.extraction, sealed)
    : observation.extraction;
  if (!terminalExtraction) {
    return failure(
      "failed",
      "dom_drift",
      "去哪儿终态未能绑定可重算库存回执",
      null,
      false,
      {
        inventory_result_state: "bounded_no_exact_quote",
        confirmed_exhaustive: false,
        detail_orchestration: finalDiagnostic,
      },
    );
  }
  return attachQunarDetailFailureDiagnostic(
    terminalExtraction,
    finalDiagnostic,
  );
}

function shouldTryTongchengLodgingDetailOrchestration(
  extraction,
  lease,
  pageUrl,
) {
  return Boolean(
    lease && lease.provider === "tongcheng" && lease.kind === "lodging" &&
    tongchengLodgingResultUrlDecision(pageUrl, lease.query).allowed &&
    extraction && extraction.state === "failed" && extraction.failure &&
    extraction.failure.code === "dom_drift"
  );
}

async function orchestrateTongchengLodgingDetails(
  listTabId,
  lease,
  driver,
  deadline,
  ownedTabIds,
  listExtraction = null,
) {
  let capture;
  let fallbackAttempt = null;
  try {
    capture = await captureTongchengLodgingDetailTargets(
      listTabId,
      lease,
      deadline,
      ownedTabIds,
    );
    const primaryUrl = capture && capture.list_page_url;
    const primaryDecision = tongchengLodgingResultUrlDecision(
      primaryUrl,
      lease.query,
    );
    const primaryHost = primaryDecision.allowed
      ? new URL(primaryDecision.href).hostname.toLowerCase()
      : null;
    const shellOnly =
      capture && !capture.targets.length &&
      Number(capture.controls_seen || 0) === 0 &&
      Number(capture.li_count || 0) > 0 &&
      Array.isArray(capture.candidate_scan_samples) &&
      capture.candidate_scan_samples.length > 0;
    const fallbackUrl = shellOnly && primaryHost === "www.ly.com"
      ? tongchengLyFallbackResultUrl(lease.query)
      : null;
    if (fallbackUrl) {
      fallbackAttempt = {
        attempted: true,
        source: "www.ly.com_shell_only",
        target: navigationUrlEvidence(fallbackUrl),
        state: "pending",
      };
      await chrome.tabs.update(listTabId, { url: fallbackUrl, active: false });
      await waitForExactTabUrl(
        listTabId,
        fallbackUrl,
        remainingTimeout(deadline, 30000),
      );
      await waitForTabInteractive(
        listTabId,
        remainingTimeout(deadline, 30000),
      );
      await installContent(listTabId);
      await delay(1500);
      const fallbackTab = await chrome.tabs.get(listTabId);
      fallbackAttempt.final_page = navigationUrlEvidence(
        fallbackTab.pendingUrl || fallbackTab.url || "",
      );
      try {
        capture = await captureTongchengLodgingDetailTargets(
          listTabId,
          lease,
          deadline,
          ownedTabIds,
        );
      } catch (error) {
        if (isMessagePortClosedError(error)) {
          fallbackAttempt.reinjection = "message_port_replaced_document";
          await installContent(listTabId);
          await delay(350);
          try {
            capture = await captureTongchengLodgingDetailTargets(
              listTabId,
              lease,
              deadline,
              ownedTabIds,
            );
          } catch (retryError) {
            error = retryError;
          }
        }
        if (!capture) {
          fallbackAttempt.state = "capture_failed";
          fallbackAttempt.error = String(error && error.message || error).slice(0, 300);
          fallbackAttempt.error_code = error && error.tripchordCode || null;
          fallbackAttempt.error_details =
            error && error.tripchordDetails && typeof error.tripchordDetails === "object"
              ? error.tripchordDetails
              : null;
          throw error;
        }
      }
      fallbackAttempt.state = capture.targets.length ? "targets_found" : "no_targets";
      fallbackAttempt.capture_code = capture.capture_code;
      fallbackAttempt.li_count = capture.li_count;
      fallbackAttempt.controls_seen = capture.controls_seen;
    }
  } catch (error) {
    if (error && error.status === 409) throw error;
    return failure(
      "failed",
      "lodging_detail_capture_failed",
      "同程住宿列表的后台详情地址读取失败",
      null,
      false,
      {
        ...lifecycleFailureDetails(error),
        tongcheng_fallback: fallbackAttempt,
      },
    );
  }
  if (!capture.targets.length) {
    return failure(
      "failed",
      "lodging_detail_control_not_found",
      "同程住宿列表没有形成可验证的只读详情地址",
      capture.list_page_url,
      false,
      {
        capture_code: capture.capture_code,
        li_count: capture.li_count,
        controls_seen: capture.controls_seen,
        candidate_samples: capture.candidate_samples,
        candidate_runtime_version: capture.candidate_runtime_version,
        candidate_scan_samples: capture.candidate_scan_samples,
        document_title: capture.document_title,
        document_sample: capture.document_sample,
        element_count: capture.element_count,
        rejections: capture.rejections,
        tongcheng_fallback: fallbackAttempt,
        list_failure:
          listExtraction && listExtraction.failure
            ? listExtraction.failure.code
            : null,
      },
    );
  }
  const quotes = [];
  const detailResults = [];
  for (const target of capture.targets.slice(0, 3)) {
    const diagnostic = {
      property_id: target.property_id,
      property_name: target.property_name,
      url: target.url,
      state: "pending",
      quote_checks: [],
    };
    try {
      const detailTab = await chrome.tabs.create({ url: target.href, active: false });
      if (!detailTab || !Number.isInteger(detailTab.id)) {
        throw lifecycleError("lodging_detail_tab_not_created", "detail tab missing");
      }
      ownedTabIds.add(detailTab.id);
      await waitForTabInteractive(
        detailTab.id,
        remainingTimeout(deadline, 30000),
      );
      await installContent(detailTab.id);
      const extraction = await extractWithRetry(
        detailTab.id,
        lease,
        driver,
        deadline,
        ownedTabIds,
      );
      diagnostic.state = extraction && extraction.state || "missing";
      diagnostic.failure_code =
        extraction && extraction.failure && extraction.failure.code || null;
      diagnostic.failure_details =
        extraction && extraction.failure && extraction.failure.details || null;
      for (const quote of extraction && Array.isArray(extraction.quotes)
        ? extraction.quotes
        : []) {
        const decision = tongchengLodgingDetailUrlDecision(
          quote.page_url,
          capture.list_page_url,
          lease.query,
        );
        const details = quote.details && typeof quote.details === "object"
          ? quote.details
          : {};
        const checks = {
          detail_url_allowed: decision.allowed === true,
          detail_url_reason: decision.reason,
          quote_page_url: navigationUrlEvidence(quote.page_url),
          property_id_matches: decision.property_id === target.property_id,
          details_property_id_matches:
            String(details.property_id || "") === target.property_id,
          parsed_property_id: decision.property_id || null,
          positive_amount: Number(quote.amount) > 0,
          amount: Number(quote.amount) || null,
          usable_price_basis:
            ["per_night", "total_stay"].includes(quote.price_basis),
          price_basis: quote.price_basis || null,
          check_in_matches: details.check_in === lease.query.start_date,
          check_out_matches: details.check_out === lease.query.end_date,
          adults_match: details.adults === lease.query.adults,
          rooms_match: details.rooms === lease.query.rooms,
        };
        diagnostic.quote_checks.push(checks);
        if (
          checks.detail_url_allowed && checks.property_id_matches &&
          checks.positive_amount && checks.usable_price_basis &&
          checks.check_in_matches && checks.check_out_matches &&
          checks.adults_match && checks.rooms_match
        ) {
          quotes.push(quote);
        }
      }
    } catch (error) {
      if (error && error.status === 409) throw error;
      diagnostic.state = "failed";
      diagnostic.failure_code = error && error.tripchordCode || "detail_read_failed";
    }
    detailResults.push(diagnostic);
  }
  if (quotes.length) {
    return { state: "succeeded", quotes };
  }
  return failure(
    "failed",
    "lodging_detail_quote_unverified",
    "同程详情页未形成可验证的数字房价合同",
    capture.list_page_url,
    false,
    {
      capture_code: capture.capture_code,
      controls_seen: capture.controls_seen,
      target_count: capture.targets.length,
      detail_results: detailResults,
    },
  );
}

function ctripExpectedPlaceEvidenceDecision(value, expectedPlaceKey) {
  const canonicalExpectedPlaceKey =
    canonicalCtripLodgingPlaceKey(expectedPlaceKey);
  const aliases =
    canonicalExpectedPlaceKey &&
    CTRIP_LODGING_PLACE_ALIASES[canonicalExpectedPlaceKey]
      ? CTRIP_LODGING_PLACE_ALIASES[canonicalExpectedPlaceKey]
      : [];
  const normalized = String(value || "")
    .replace(/\s+/g, " ")
    .trim()
    .toLowerCase();
  if (!normalized || !aliases.length) {
    return "unknown";
  }
  const comparablePlace = (rawValue) =>
    String(rawValue || "")
      .toLowerCase()
      .replace(/[·•\-_/（）()，,。.]/g, "")
      .replace(/\s+/g, "")
      .replace(/(?:岛|island)$/i, "");
  const locationPrefix = normalized.split("·", 1)[0].trim();
  if (
    aliases.some(
      (alias) =>
        comparablePlace(locationPrefix) === comparablePlace(alias),
    )
  ) {
    return "exact";
  }
  if (
    aliases.some((alias) => normalized.includes(alias.toLowerCase())) &&
    (
      /(?:距|距离|附近|周边|靠近|临近|邻近)/i.test(normalized) ||
      /\d+(?:\.\d+)?\s*(?:公里|千米|米|km\b|m\b)/i.test(normalized) ||
      /\b(?:near|nearby|distance|away|from)\b/i.test(normalized)
    )
  ) {
    return "distance_only";
  }
  return "unknown";
}

function ctripExactPreviewPlaceDecision(target, expectedPlaceKey) {
  const canonicalExpectedPlaceKey =
    canonicalCtripLodgingPlaceKey(expectedPlaceKey);
  const preview =
    target &&
    target.preview &&
    typeof target.preview === "object"
      ? target.preview
      : null;
  if (
    !canonicalExpectedPlaceKey ||
    !preview ||
    preview.expected_place_key !== canonicalExpectedPlaceKey ||
    preview.place_match !== "exact" ||
    typeof preview.exact_place_evidence !== "string" ||
    !preview.exact_place_evidence.trim() ||
    preview.distance_reference_evidence
  ) {
    return false;
  }
  return true;
}

function exactCtripLodgingDetailQuoteDecision(
  quote,
  target,
  listPageUrl,
  query,
) {
  const rejected = (reason) => ({ allowed: false, reason });
  if (
    !quote ||
    quote.provider !== "ctrip" ||
    quote.kind !== "lodging"
  ) {
    return rejected("quote_identity_mismatch");
  }
  const pageDecision = ctripLodgingDetailUrlDecision(
    quote.page_url,
    listPageUrl,
    query,
  );
  if (!pageDecision.allowed) {
    return rejected(`quote_${pageDecision.reason}`);
  }
  if (
    !target ||
    pageDecision.href !== target.href ||
    pageDecision.hotel_id !== target.hotel_id
  ) {
    return rejected("quote_detail_target_mismatch");
  }
  if (
    !Number.isFinite(Number(quote.amount)) ||
    Number(quote.amount) <= 0 ||
    typeof quote.currency !== "string" ||
    !quote.currency ||
    (
      quote.price_basis !== "per_night" &&
      quote.price_basis !== "total_stay"
    )
  ) {
    return rejected("quote_price_contract_incomplete");
  }
  const details =
    quote.details &&
    typeof quote.details === "object" &&
    !Array.isArray(quote.details)
      ? quote.details
      : {};
  if (
    details.check_in !== query.start_date ||
    details.check_out !== query.end_date ||
    details.adults !== query.adults ||
    details.rooms !== query.rooms
  ) {
    return rejected("quote_stay_context_mismatch");
  }
  if (
    typeof details.price_unit_evidence !== "string" ||
    !details.price_unit_evidence.trim() ||
    typeof quote.visible_evidence !== "string" ||
    !quote.visible_evidence ||
    !/^[a-f0-9]{64}$/i.test(String(quote.evidence_sha256 || ""))
  ) {
    return rejected("quote_visible_evidence_incomplete");
  }
  if (
    String(details.property_id || "") !== target.hotel_id ||
    typeof details.property_name !== "string" ||
    !details.property_name.trim() ||
    typeof details.room_text !== "string" ||
    !details.room_text.trim() ||
    typeof details.rate_text !== "string" ||
    !details.rate_text.trim() ||
    details.availability !== "available" ||
    typeof details.availability_text !== "string" ||
    !details.availability_text.trim() ||
    quote.taxes_included !== true ||
    typeof details.tax_evidence !== "string" ||
    !details.tax_evidence.trim() ||
    details.price_finality !== "final_for_rate"
  ) {
    return rejected("quote_detail_contract_incomplete");
  }
  const expectedPlaceKey =
    query &&
    query.options &&
    canonicalCtripLodgingPlaceKey(
      query.options.expected_lodging_place_key,
    );
  const detailAreaDecision = ctripExpectedPlaceEvidenceDecision(
    details.area_text,
    expectedPlaceKey,
  );
  const exactPreviewPlaceVerified =
    ctripExactPreviewPlaceDecision(target, expectedPlaceKey);
  if (
    expectedPlaceKey &&
    (
      details.expected_lodging_place_key !== expectedPlaceKey ||
      details.observed_lodging_place_key !== expectedPlaceKey ||
      details.lodging_place_matches_expected !== true ||
      details.area_matches_expected !== true ||
      details.area_source !== "visible_label" ||
      (
        detailAreaDecision !== "exact" &&
        !exactPreviewPlaceVerified
      )
    )
  ) {
    return rejected("quote_expected_place_unverified");
  }
  return {
    allowed: true,
    reason: "allowed",
    hotel_id: pageDecision.hotel_id,
  };
}

function exactFliggyLodgingDetailQuoteDecision(
  quote,
  target,
  listPageUrl,
  query,
) {
  const rejected = (reason) => ({ allowed: false, reason });
  if (
    !quote ||
    quote.provider !== "fliggy" ||
    quote.kind !== "lodging"
  ) {
    return rejected("quote_identity_mismatch");
  }
  const pageDecision = fliggyLodgingDetailUrlDecision(
    quote.page_url,
    listPageUrl,
    query,
  );
  if (!pageDecision.allowed) {
    return rejected(`quote_${pageDecision.reason}`);
  }
  if (
    !target ||
    pageDecision.property_id !== target.property_id
  ) {
    return rejected("quote_detail_target_mismatch");
  }
  const details =
    quote.details && typeof quote.details === "object" &&
    !Array.isArray(quote.details)
      ? quote.details
      : {};
  if (
    !Number.isFinite(Number(quote.amount)) ||
    Number(quote.amount) <= 0 ||
    quote.currency !== query.currency ||
    quote.price_basis !== "per_night" ||
    quote.taxes_included !== true ||
    details.check_in !== query.start_date ||
    details.check_out !== query.end_date ||
    details.adults !== query.adults ||
    details.rooms !== query.rooms ||
    String(details.property_id || "") !== target.property_id ||
    !String(details.property_name || "").trim() ||
    !String(details.room_text || "").trim() ||
    !String(details.rate_text || "").trim() ||
    details.availability !== "available" ||
    !String(details.availability_text || "").trim() ||
    !String(details.tax_evidence || "").trim() ||
    !String(details.price_unit_evidence || "").trim() ||
    details.price_basis_source !==
      "audited_fliggy_hotel_detail_rate_contract" ||
    details.price_finality !== "final_for_rate" ||
    details.area_matches_expected !== true ||
    details.lodging_place_matches_expected !== true ||
    typeof quote.visible_evidence !== "string" ||
    !quote.visible_evidence ||
    !/^[a-f0-9]{64}$/i.test(String(quote.evidence_sha256 || ""))
  ) {
    return rejected("quote_detail_contract_incomplete");
  }
  return {
    allowed: true,
    reason: "allowed",
    property_id: pageDecision.property_id,
  };
}

async function orchestrateFliggyLodgingDetails(
  listTabId,
  lease,
  driver,
  deadline,
  ownedTabIds,
  listExtraction = null,
) {
  const detailDeadline = Math.min(
    deadline,
    Date.now() + CTRIP_LODGING_DETAIL_WORKFLOW_CAP_MS,
  );
  let capture;
  try {
    capture = await captureFliggyLodgingDetailTargets(
      listTabId,
      lease,
      detailDeadline,
      ownedTabIds,
    );
  } catch (error) {
    if (error && error.status === 409) throw error;
    return failure(
      "failed",
      "lodging_detail_capture_failed",
      "飞猪住宿列表的只读详情地址读取失败",
      null,
      false,
      {
        stage: "capture_detail_targets",
        error_code: error && error.tripchordCode ||
          "detail_capture_execution_failed",
        ...lifecycleFailureDetails(error),
      },
    );
  }
  const captureDiagnostic = {
    capture_code: capture.capture_code,
    controls_seen: capture.controls_seen,
    target_count: capture.targets.length,
    targets: capture.targets.map((target) => ({
      property_id: target.property_id,
      property_name: target.property_name,
      location_evidence: target.location_evidence,
      url: target.url,
    })),
    rejections: capture.rejections,
  };
  if (!capture.targets.length) {
    return failure(
      "failed",
      "lodging_detail_control_not_found",
      "飞猪住宿列表没有可验证的只读酒店详情地址",
      capture.list_page_url,
      false,
      {
        stage: "capture_detail_targets",
        detail_orchestration: captureDiagnostic,
        parser_version:
          listExtraction && listExtraction.failure &&
          listExtraction.failure.details &&
          listExtraction.failure.details.parser_version || null,
      },
    );
  }
  const quotes = [];
  const seenEvidence = new Set();
  const detailResults = [];
  const detailTargets = capture.targets.slice(
    0,
    MAX_FLIGGY_LODGING_DETAIL_PAGES_PER_LEASE,
  );
  await Promise.all(detailTargets.map(async (target) => {
    const diagnostic = {
      property_id: target.property_id,
      property_name: target.property_name,
      url: target.url,
      state: "pending",
    };
    if (detailDeadline - Date.now() < 2500) {
      diagnostic.state = "skipped_budget_exhausted";
      detailResults.push(diagnostic);
      return;
    }
    try {
      await assertLeaseActive(
        lease,
        remainingTimeout(detailDeadline, 5000),
      );
      const detailTab = await chrome.tabs.create({
        url: target.href,
        active: false,
      });
      if (!detailTab || !Number.isInteger(detailTab.id)) {
        throw lifecycleError(
          "lodging_detail_tab_not_created",
          "Chrome did not return a controlled Fliggy detail tab",
          { stage: "open_detail_tab" },
        );
      }
      const controlledTabId = detailTab.id;
      ownedTabIds.add(controlledTabId);
      let loadCompleted = true;
      try {
        await waitForTabComplete(
          controlledTabId,
          remainingTimeout(detailDeadline, CTRIP_LODGING_DETAIL_LOAD_CAP_MS),
        );
      } catch (error) {
        if (!/timed out|timeout/i.test(String(error && error.message || error))) {
          throw error;
        }
        // Some provider pages keep long-lived analytics/network requests open
        // after their visible hotel rates are already usable. Continue with a
        // bounded DOM extraction instead of treating `tabs.status=loading` as
        // proof that the read-only detail surface is unavailable.
        loadCompleted = false;
      }
      const current = await chrome.tabs.get(controlledTabId);
      const currentUrl = current.url || current.pendingUrl || "";
      const currentDecision = fliggyLodgingDetailUrlDecision(
        currentUrl,
        capture.list_page_url,
        lease.query,
      );
      if (!currentDecision.allowed) {
        diagnostic.state = "rejected_navigation";
        diagnostic.reason = currentDecision.reason ||
          "detail_redirect_not_allowed";
        diagnostic.final_url = currentDecision.url;
        detailResults.push(diagnostic);
        return;
      }
      if (currentDecision.property_id !== target.property_id) {
        diagnostic.state = "rejected_navigation";
        diagnostic.reason = "detail_property_redirect_not_allowed";
        diagnostic.final_url = currentDecision.url;
        detailResults.push(diagnostic);
        return;
      }
      const extraction = await visibleDetailExtractionWithRetry(
        controlledTabId,
        {
          type: "tripchord:extract",
          provider: "fliggy",
          kind: "lodging",
          query: lease.query,
          driver: {
            ...(driver || {}),
            mode: "captured_read_only_detail",
            detail_capture: {
              source: "fliggy_visible_hotel_detail_link",
              property_id: target.property_id,
              clicked_booking: false,
            },
          },
        },
        {
          lease,
          deadline: detailDeadline,
          ownedTabIds,
          timeoutCapMs: CTRIP_LODGING_DETAIL_EXTRACT_CAP_MS,
          attempts: loadCompleted ? 3 : 2,
        },
      );
      if (
        !extraction ||
        extraction.state !== "succeeded" ||
        !Array.isArray(extraction.quotes)
      ) {
        diagnostic.state = "detail_extraction_failed";
        diagnostic.reason = extraction && extraction.failure &&
          extraction.failure.code || "detail_quote_missing";
        diagnostic.gates = extraction && extraction.failure &&
          extraction.failure.details && extraction.failure.details.gates || null;
        diagnostic.load_completed = loadCompleted;
        detailResults.push(diagnostic);
        return;
      }
      const rejections = [];
      let accepted = 0;
      for (const quote of extraction.quotes) {
        const decision = exactFliggyLodgingDetailQuoteDecision(
          quote,
          target,
          capture.list_page_url,
          lease.query,
        );
        if (!decision.allowed) {
          rejections.push(decision.reason);
          continue;
        }
        const evidenceKey = String(quote.evidence_sha256);
        if (seenEvidence.has(evidenceKey)) continue;
        seenEvidence.add(evidenceKey);
        quotes.push(quote);
        accepted += 1;
      }
      diagnostic.state = accepted ? "succeeded" : "detail_quotes_rejected";
      diagnostic.load_completed = loadCompleted;
      diagnostic.accepted_quotes = accepted;
      diagnostic.quote_rejections = rejections.slice(0, 8);
      detailResults.push(diagnostic);
    } catch (error) {
      if (error && error.status === 409) throw error;
      diagnostic.state = "detail_read_failed";
      diagnostic.reason = error && error.tripchordCode ||
        (/timed out|timeout/i.test(String(error && error.message || error))
          ? "detail_timeout" : "detail_read_error");
      diagnostic.error_details = lifecycleFailureDetails(error);
      detailResults.push(diagnostic);
    }
  }));
  detailResults.sort((left, right) =>
    detailTargets.findIndex((target) => target.property_id === left.property_id) -
    detailTargets.findIndex((target) => target.property_id === right.property_id)
  );
  const detailOrchestration = {
    ...captureDiagnostic,
    detail_results: detailResults,
    accepted_quote_count: quotes.length,
    clicked_booking: false,
    detail_page_limit: MAX_FLIGGY_LODGING_DETAIL_PAGES_PER_LEASE,
    detail_max_concurrency: MAX_CONCURRENT_FLIGGY_LODGING_DETAILS,
    workflow_budget_cap_ms: CTRIP_LODGING_DETAIL_WORKFLOW_CAP_MS,
  };
  if (!quotes.length) {
    return failure(
      "failed",
      "lodging_detail_quotes_unverified",
      "飞猪详情页未形成与日期、人数、房间数一致的精确住宿报价",
      capture.list_page_url,
      false,
      {
        stage: "extract_detail_quotes",
        detail_orchestration: detailOrchestration,
      },
    );
  }
  return {
    state: "succeeded",
    quotes,
    detail_orchestration: detailOrchestration,
  };
}

function ctripDetailCaptureDiagnostic(capture) {
  return {
    capture_code: capture.capture_code,
    controls_seen: capture.controls_seen,
    exact_visible_controls: capture.exact_visible_controls,
    clicked_controls: capture.clicked_controls,
    popup_interceptions: capture.popup_interceptions,
    expected_place_key: capture.expected_place_key,
    preview_count: capture.previews.length,
    previews: capture.previews,
    ranked_control_indices: capture.ranked_control_indices,
    validated_target_count: capture.validated_target_count,
    target_count: capture.targets.length,
    targets: capture.targets.map((target) => ({
      control_index: target.control_index,
      hotel_id: target.hotel_id,
      url: target.url,
      place_match:
        target.preview && target.preview.place_match || "unknown",
      exact_place_evidence:
        target.preview && target.preview.exact_place_evidence || null,
      distance_reference_evidence:
        target.preview &&
        target.preview.distance_reference_evidence ||
        null,
    })),
    rejections: capture.rejections,
    click_errors: capture.click_errors,
  };
}

function ctripLodgingCandidateSummaries(capture) {
  const previews = Array.isArray(capture && capture.previews)
    ? capture.previews
    : [];
  return previews
    .slice(0, MAX_CTRIP_LODGING_PREVIEW_CANDIDATES)
    .map((preview, index) => ({
      candidate_index: index,
      title:
        sanitizeInventoryDiagnosticText(
          preview && preview.property_name,
          180,
        ),
      area_evidence:
        sanitizeInventoryDiagnosticText(
          preview &&
          (
            preview.position_summary ||
            preview.actual_location_prefix
          ),
          240,
        ),
      room_evidence: null,
      price_evidence: null,
      price_basis: "unknown",
      price_finality: "unknown",
    }));
}

function normalizedBackgroundCandidateSummaries(candidateSummaries) {
  return (Array.isArray(candidateSummaries) ? candidateSummaries : [])
    .slice(0, MAX_CTRIP_LODGING_PREVIEW_CANDIDATES)
    .map((summary, index) => {
      const normalized = {
        candidate_index: index,
        title:
          sanitizeInventoryDiagnosticText(summary && summary.title, 180),
        area_evidence:
          sanitizeInventoryDiagnosticText(
            summary && summary.area_evidence,
            240,
          ),
        room_evidence:
          sanitizeInventoryDiagnosticText(
            summary && summary.room_evidence,
            180,
          ),
        price_evidence:
          sanitizeInventoryDiagnosticText(
            summary && summary.price_evidence,
            180,
          ),
        price_basis:
          ["per_night", "total_stay", "unknown"].includes(
            summary && summary.price_basis,
          )
            ? summary.price_basis
            : "unknown",
        price_finality:
          ["exact_candidate", "starting_or_estimated", "unknown"].includes(
            summary && summary.price_finality,
          )
            ? summary.price_finality
            : "unknown",
      };
      return normalized;
    });
}

function normalizedBackgroundEmptyEvidence(evidence) {
  if (evidence === null) {
    return null;
  }
  // No provider-level lodging empty marker has been audited yet.
  return undefined;
}

function backgroundReceiptConfirmedQuery(query, driver) {
  if (!exactLodgingQueryConfirmed(query, driver)) {
    return null;
  }
  const options =
    query &&
    query.options &&
    typeof query.options === "object" &&
    !Array.isArray(query.options)
      ? query.options
      : null;
  if (!options) {
    return null;
  }
  const expectedPlaceKey = canonicalCtripLodgingPlaceKey(
    options.expected_lodging_place_key,
  );
  if (
    !expectedPlaceKey ||
    !SAFE_LODGING_RECEIPT_SEGMENTS.has(options.segment) ||
    !SAFE_LODGING_RECEIPT_PACKAGE_AREAS.has(
      options.expected_package_area,
    )
  ) {
    return null;
  }
  return {
    destination: String(driver.confirmed_query.destination)
      .replace(/\s+/g, " ")
      .trim()
      .slice(0, 120),
    start_date: driver.confirmed_query.start_date,
    end_date: driver.confirmed_query.end_date,
    adults: driver.confirmed_query.adults,
    rooms: driver.confirmed_query.rooms,
    options: {
      expected_lodging_place_key: expectedPlaceKey,
      expected_package_area: options.expected_package_area,
      segment: options.segment,
    },
  };
}

function backgroundInventoryReceiptPageUrl(provider, rawUrl) {
  if (!providerVerticalUrlAllowed(provider, "lodging", rawUrl)) {
    return null;
  }
  try {
    const parsed = new URL(rawUrl);
    return `${parsed.origin}${parsed.pathname}`;
  } catch {
    return null;
  }
}

async function createBackgroundLodgingInventoryReceipt({
  provider,
  query,
  driver,
  parser_version: parserVersion,
  candidate_summaries: candidateSummaries,
  explicit_empty_evidence: explicitEmptyEvidence = null,
  page_url: pageUrl,
  captured_at: capturedAt,
}) {
  if (parserVersion !== LODGING_INVENTORY_RECEIPT_PARSER_VERSION) {
    return null;
  }
  const confirmedQuery = backgroundReceiptConfirmedQuery(query, driver);
  const summaries =
    normalizedBackgroundCandidateSummaries(candidateSummaries);
  const emptyEvidence =
    normalizedBackgroundEmptyEvidence(explicitEmptyEvidence);
  const safePageUrl = backgroundInventoryReceiptPageUrl(
    provider,
    pageUrl,
  );
  if (
    !confirmedQuery ||
    emptyEvidence === undefined ||
    (!summaries.length && emptyEvidence === null) ||
    summaries.some(
      (summary) =>
        !summary.title &&
        !summary.area_evidence &&
        !summary.room_evidence &&
        !summary.price_evidence,
    ) ||
    !safePageUrl ||
    typeof capturedAt !== "string" ||
    Number.isNaN(Date.parse(capturedAt))
  ) {
    return null;
  }
  const receipt = {
    schema_version: LODGING_INVENTORY_RECEIPT_SCHEMA_VERSION,
    parser_version: LODGING_INVENTORY_RECEIPT_PARSER_VERSION,
    provider,
    state: "bounded_no_exact_quote",
    confirmed_query: confirmedQuery,
    confirmation_scope: driver.confirmation_scope,
    scan_limit: MAX_CTRIP_LODGING_PREVIEW_CANDIDATES,
    scanned_count: summaries.length,
    candidate_summaries: summaries,
    explicit_empty_evidence: emptyEvidence,
    page_url: safePageUrl,
    captured_at: capturedAt,
  };
  return {
    receipt,
    receipt_sha256: await inventoryReceiptSha256(
      canonicalInventoryJson(receipt),
    ),
  };
}

async function validateBackgroundLodgingInventoryReceipt(
  receipt,
  receiptSha256,
) {
  const rejected = (reason) => ({ valid: false, reason });
  const hasExactKeys = (value, keys) =>
    value &&
    typeof value === "object" &&
    !Array.isArray(value) &&
    canonicalInventoryJson(Object.keys(value).sort()) ===
      canonicalInventoryJson([...keys].sort());
  if (!receipt || typeof receipt !== "object" || Array.isArray(receipt)) {
    return rejected("receipt_missing");
  }
  if (
    !hasExactKeys(receipt, [
      "schema_version",
      "parser_version",
      "provider",
      "state",
      "confirmed_query",
      "confirmation_scope",
      "scan_limit",
      "scanned_count",
      "candidate_summaries",
      "explicit_empty_evidence",
      "page_url",
      "captured_at",
    ]) ||
    !hasExactKeys(receipt.confirmed_query, [
      "destination",
      "start_date",
      "end_date",
      "adults",
      "rooms",
      "options",
    ]) ||
    !hasExactKeys(
      receipt.confirmed_query && receipt.confirmed_query.options,
      [
        "expected_lodging_place_key",
        "expected_package_area",
        "segment",
      ],
    ) ||
    (
      Array.isArray(receipt.candidate_summaries) &&
      receipt.candidate_summaries.some(
        (summary) =>
          !hasExactKeys(summary, [
            "candidate_index",
            "title",
            "area_evidence",
            "room_evidence",
            "price_evidence",
            "price_basis",
            "price_finality",
          ]),
      )
    )
  ) {
    return rejected("receipt_shape_invalid");
  }
  if (
    receipt.schema_version !== LODGING_INVENTORY_RECEIPT_SCHEMA_VERSION
  ) {
    return rejected("schema_version_mismatch");
  }
  if (
    receipt.parser_version !== LODGING_INVENTORY_RECEIPT_PARSER_VERSION
  ) {
    return rejected("parser_version_mismatch");
  }
  if (
    receipt.state !== "bounded_no_exact_quote" ||
    receipt.confirmation_scope !== "confirmed_visible_search" ||
    !receipt.confirmed_query ||
    typeof receipt.confirmed_query.destination !== "string" ||
    !receipt.confirmed_query.destination ||
    String(receipt.confirmed_query.destination)
      .replace(/\s+/g, " ")
      .trim()
      .slice(0, 120) !== receipt.confirmed_query.destination ||
    calendarDateQueryValue(receipt.confirmed_query.start_date) === null ||
    calendarDateQueryValue(receipt.confirmed_query.end_date) === null ||
    calendarDateQueryValue(receipt.confirmed_query.end_date) <=
      calendarDateQueryValue(receipt.confirmed_query.start_date) ||
    !Number.isInteger(receipt.confirmed_query.adults) ||
    receipt.confirmed_query.adults <= 0 ||
    !Number.isInteger(receipt.confirmed_query.rooms) ||
    receipt.confirmed_query.rooms <= 0
  ) {
    return rejected("confirmed_query_or_scope_missing");
  }
  const receiptOptions = receipt.confirmed_query.options;
  if (
    !SAFE_LODGING_RECEIPT_SEGMENTS.has(receiptOptions.segment) ||
    !SAFE_LODGING_RECEIPT_PACKAGE_AREAS.has(
      receiptOptions.expected_package_area,
    ) ||
    typeof receiptOptions.expected_lodging_place_key !== "string" ||
    canonicalCtripLodgingPlaceKey(
      receiptOptions.expected_lodging_place_key,
    ) !== receiptOptions.expected_lodging_place_key
  ) {
    return rejected("confirmed_query_options_invalid");
  }
  if (
    receipt.scan_limit !== MAX_CTRIP_LODGING_PREVIEW_CANDIDATES ||
    !Number.isInteger(receipt.scanned_count) ||
    receipt.scanned_count <= 0 ||
    !Array.isArray(receipt.candidate_summaries) ||
    receipt.candidate_summaries.length !== receipt.scanned_count ||
    receipt.scanned_count > receipt.scan_limit ||
    receipt.candidate_summaries.some(
      (summary) =>
        !summary ||
        (
          !summary.title &&
          !summary.area_evidence &&
          !summary.room_evidence &&
          !summary.price_evidence
        ),
    )
  ) {
    return rejected("scan_contract_invalid");
  }
  if (
    canonicalInventoryJson(
      normalizedBackgroundCandidateSummaries(
        receipt.candidate_summaries,
      ),
    ) !== canonicalInventoryJson(receipt.candidate_summaries)
  ) {
    return rejected("candidate_summaries_not_sanitized");
  }
  const emptyEvidence = normalizedBackgroundEmptyEvidence(
    receipt.explicit_empty_evidence,
  );
  if (
    emptyEvidence === undefined ||
    (!receipt.scanned_count && emptyEvidence === null)
  ) {
    return rejected("empty_receipt_without_evidence");
  }
  if (
    canonicalInventoryJson(emptyEvidence) !==
      canonicalInventoryJson(receipt.explicit_empty_evidence)
  ) {
    return rejected("explicit_empty_evidence_invalid");
  }
  if (
    backgroundInventoryReceiptPageUrl(
      receipt.provider,
      receipt.page_url,
    ) !== receipt.page_url ||
    typeof receipt.captured_at !== "string" ||
    Number.isNaN(Date.parse(receipt.captured_at))
  ) {
    return rejected("capture_context_missing");
  }
  const expectedSha256 = await inventoryReceiptSha256(
    canonicalInventoryJson(receipt),
  );
  if (
    !/^[a-f0-9]{64}$/.test(String(receiptSha256 || "")) ||
    expectedSha256 !== receiptSha256
  ) {
    return rejected("receipt_sha256_mismatch");
  }
  return { valid: true, reason: "valid", receipt_sha256: expectedSha256 };
}

async function boundedCtripLodgingInventoryDetails(
  capture,
  lease,
  driver,
  captureCode,
  pageUrl,
  capturedAt,
  parserVersion,
) {
  const candidateSummaries = ctripLodgingCandidateSummaries(capture);
  const built = await createBackgroundLodgingInventoryReceipt({
    provider: "ctrip",
    query: lease && lease.query,
    driver,
    parser_version: parserVersion,
    candidate_summaries: candidateSummaries,
    explicit_empty_evidence: null,
    page_url: pageUrl,
    captured_at: capturedAt,
  });
  if (!built) {
    return {};
  }
  return {
    inventory_result_state: "bounded_no_exact_quote",
    confirmed_exhaustive: false,
    scanned_count: candidateSummaries.length,
    candidate_summaries: candidateSummaries,
    capture_code: captureCode,
    inventory_receipt: built.receipt,
    inventory_receipt_sha256: built.receipt_sha256,
  };
}

async function orchestrateCtripLodgingDetails(
  listTabId,
  lease,
  driver,
  deadline,
  ownedTabIds,
  listExtraction = null,
) {
  const detailDeadline = Math.min(
    deadline,
    Date.now() + CTRIP_LODGING_DETAIL_WORKFLOW_CAP_MS,
  );
  let capture;
  try {
    capture = await captureCtripLodgingDetailTargets(
      listTabId,
      lease,
      detailDeadline,
      ownedTabIds,
    );
  } catch (error) {
    if (error && error.status === 409) {
      throw error;
    }
    return failure(
      "failed",
      "lodging_detail_capture_failed",
      "携程住宿列表的只读详情地址截获失败",
      null,
      false,
      {
        stage: "capture_detail_targets",
        error_code:
          error && error.tripchordCode ||
          "detail_capture_execution_failed",
        ...lifecycleFailureDetails(error),
      },
    );
  }
  const captureDiagnostic = ctripDetailCaptureDiagnostic(capture);
  const listParserVersion =
    listExtraction &&
    listExtraction.failure &&
    listExtraction.failure.details &&
    listExtraction.failure.details.parser_version;
  if (!capture.targets.length) {
    let code = "lodging_detail_url_rejected";
    let message = "携程“查看详情”返回的地址未通过严格查询校验";
    if (capture.capture_code === "expected_place_preview_not_found") {
      code = "lodging_expected_place_preview_not_found";
      message =
        "携程住宿列表没有实际位于目标地点的候选；距离目标中心的卡片不作为精确地点";
    } else if (capture.exact_visible_controls === 0) {
      code = "lodging_detail_control_not_found";
      message = "携程住宿列表没有可见且文案精确的“查看详情”控件";
    } else if (capture.popup_interceptions === 0) {
      code = "lodging_detail_url_not_observed";
      message = "已安全拦截“查看详情”动作，但没有观察到详情地址";
    }
    const capturedAt = new Date().toISOString();
    const inventoryDetails =
      await boundedCtripLodgingInventoryDetails(
        capture,
        lease,
        driver,
        capture.capture_code,
        capture.list_page_url,
        capturedAt,
        listParserVersion,
      );
    const failurePageUrl =
      inventoryDetails.inventory_receipt &&
      inventoryDetails.inventory_receipt.page_url ||
      capture.list_page_url;
    return failure(
      "failed",
      code,
      message,
      failurePageUrl,
      false,
      {
        stage: "capture_detail_targets",
        ...inventoryDetails,
        detail_orchestration: captureDiagnostic,
      },
      capturedAt,
    );
  }
  const detailResults = [];
  const quotes = [];
  const seenEvidence = new Set();
  for (const target of capture.targets.slice(
    0,
    MAX_LODGING_DETAIL_PAGES_PER_LEASE,
  )) {
    const targetDiagnostic = {
      control_index: target.control_index,
      hotel_id: target.hotel_id,
      url: target.url,
      preview_place_match:
        target.preview && target.preview.place_match || "unknown",
      state: "pending",
    };
    if (detailDeadline - Date.now() < 2500) {
      targetDiagnostic.state = "skipped_budget_exhausted";
      targetDiagnostic.reason = "detail_workflow_budget_exhausted";
      detailResults.push(targetDiagnostic);
      break;
    }
    try {
      await assertLeaseActive(
        lease,
        remainingTimeout(detailDeadline, 5000),
      );
      const detailTab = await chrome.tabs.create({
        url: target.href,
        active: false,
      });
      if (!detailTab || !Number.isInteger(detailTab.id)) {
        throw lifecycleError(
          "lodging_detail_tab_not_created",
          "Chrome did not return a controlled Ctrip detail tab",
          { stage: "open_detail_tab" },
        );
      }
      const controlledTabId = detailTab.id;
      ownedTabIds.add(controlledTabId);
      await waitForTabComplete(
        controlledTabId,
        remainingTimeout(
          detailDeadline,
          CTRIP_LODGING_DETAIL_LOAD_CAP_MS,
        ),
      );
      const current = await chrome.tabs.get(controlledTabId);
      const currentUrl = current.url || current.pendingUrl || "";
      const currentDecision = ctripLodgingDetailUrlDecision(
        currentUrl,
        capture.list_page_url,
        lease.query,
      );
      if (!currentDecision.allowed) {
        targetDiagnostic.state = "rejected_navigation";
        targetDiagnostic.reason = currentDecision.reason;
        targetDiagnostic.final_url = currentDecision.url;
        detailResults.push(targetDiagnostic);
        continue;
      }
      if (currentDecision.href !== target.href) {
        targetDiagnostic.state = "rejected_redirect";
        targetDiagnostic.reason = "detail_redirect_not_allowed";
        targetDiagnostic.final_url = currentDecision.url;
        detailResults.push(targetDiagnostic);
        continue;
      }
      const extraction = await visibleDetailExtractionWithRetry(
        controlledTabId,
        {
          type: "tripchord:extract",
          provider: "ctrip",
          kind: "lodging",
          query: lease.query,
          driver: {
            ...(driver || {}),
            mode: "captured_read_only_detail",
            detail_capture: {
              source: "ctrip_visible_exact_view_details",
              hotel_id: target.hotel_id,
              popup_opened: false,
              preview_place_match:
                target.preview && target.preview.place_match || "unknown",
              preview_location_evidence:
                target.preview &&
                target.preview.location_evidence ||
                [],
            },
          },
        },
        {
          lease,
          deadline: detailDeadline,
          ownedTabIds,
          timeoutCapMs: CTRIP_LODGING_DETAIL_EXTRACT_CAP_MS,
          attempts: 3,
        },
      );
      if (
        !extraction ||
        extraction.state !== "succeeded" ||
        !Array.isArray(extraction.quotes)
      ) {
        targetDiagnostic.state = "detail_extraction_failed";
        targetDiagnostic.reason =
          extraction &&
          extraction.failure &&
          extraction.failure.code ||
          "detail_quote_missing";
        targetDiagnostic.gates =
          extraction &&
          extraction.failure &&
          extraction.failure.details &&
          extraction.failure.details.gates || null;
        detailResults.push(targetDiagnostic);
        continue;
      }
      let accepted = 0;
      const quoteRejections = [];
      for (const quote of extraction.quotes) {
        const decision = exactCtripLodgingDetailQuoteDecision(
          quote,
          target,
          capture.list_page_url,
          lease.query,
        );
        if (!decision.allowed) {
          quoteRejections.push(decision.reason);
          continue;
        }
        const evidenceKey = String(quote.evidence_sha256);
        if (seenEvidence.has(evidenceKey)) {
          continue;
        }
        seenEvidence.add(evidenceKey);
        quotes.push(quote);
        accepted += 1;
      }
      targetDiagnostic.state = accepted
        ? "succeeded"
        : "detail_quotes_rejected";
      targetDiagnostic.accepted_quotes = accepted;
      targetDiagnostic.quote_rejections = quoteRejections.slice(0, 8);
      detailResults.push(targetDiagnostic);
    } catch (error) {
      if (error && error.status === 409) {
        throw error;
      }
      targetDiagnostic.state = "detail_read_failed";
      targetDiagnostic.reason =
        error && error.tripchordCode ||
        (/timed out|timeout/i.test(String(error && error.message || error))
          ? "detail_timeout"
          : "detail_read_error");
      targetDiagnostic.error_details = lifecycleFailureDetails(error);
      detailResults.push(targetDiagnostic);
    }
  }
  const diagnostic = {
    ...captureDiagnostic,
    detail_results: detailResults,
    accepted_quote_count: quotes.length,
    popup_opened: false,
    workflow_budget_cap_ms: CTRIP_LODGING_DETAIL_WORKFLOW_CAP_MS,
    workflow_budget_remaining_ms: Math.max(
      0,
      detailDeadline - Date.now(),
    ),
  };
  if (!quotes.length) {
    const capturedAt = new Date().toISOString();
    const inventoryDetails =
      await boundedCtripLodgingInventoryDetails(
        capture,
        lease,
        driver,
        "lodging_detail_quotes_unverified",
        capture.list_page_url,
        capturedAt,
        listParserVersion,
      );
    const failurePageUrl =
      inventoryDetails.inventory_receipt &&
      inventoryDetails.inventory_receipt.page_url ||
      capture.list_page_url;
    return failure(
      "failed",
      "lodging_detail_quotes_unverified",
      "携程详情页未形成与日期、人数、房间数一致的精确住宿报价",
      failurePageUrl,
      false,
      {
        stage: "extract_detail_quotes",
        ...inventoryDetails,
        detail_orchestration: diagnostic,
      },
      capturedAt,
    );
  }
  return {
    state: "succeeded",
    quotes,
    detail_orchestration: diagnostic,
  };
}

function ctripAuditedSeedTargets(query) {
  const placeKey = canonicalCtripLodgingPlaceKey(
    query && query.options && query.options.expected_lodging_place_key,
  );
  const seed = placeKey && CTRIP_AUDITED_LODGING_SEEDS[placeKey];
  if (
    !seed ||
    !/^\d+$/.test(seed.cityId) ||
    !/^\d{4}-\d{2}-\d{2}$/.test(String(query && query.start_date || "")) ||
    !/^\d{4}-\d{2}-\d{2}$/.test(String(query && query.end_date || "")) ||
    String(query.end_date) <= String(query.start_date) ||
    !Number.isInteger(query.adults) ||
    query.adults < 1 ||
    query.adults > 9 ||
    query.rooms !== 1 ||
    query.currency !== "CNY"
  ) {
    return [];
  }
  const listPageUrl = "https://hotels.ctrip.com/hotels/list";
  return seed.properties.map((property, controlIndex) => {
    const url = new URL("https://hotels.ctrip.com/hotels/detail/");
    url.searchParams.set("cityEnName", seed.cityEnName);
    url.searchParams.set("cityId", seed.cityId);
    url.searchParams.set("hotelId", property.hotelId);
    url.searchParams.set("checkIn", query.start_date);
    url.searchParams.set("checkOut", query.end_date);
    url.searchParams.set("adult", String(query.adults));
    url.searchParams.set("children", "0");
    url.searchParams.set("crn", String(query.rooms));
    url.searchParams.set("ages", "");
    url.searchParams.set("curr", query.currency);
    url.searchParams.set("barcurr", query.currency);
    url.searchParams.set("display", "exavg");
    url.searchParams.set("isCT", "true");
    url.searchParams.set("isFlexible", "F");
    url.searchParams.set("isFirstEnterDetail", "T");
    const decision = ctripLodgingDetailUrlDecision(
      url.href,
      listPageUrl,
      query,
    );
    if (!decision.allowed) return null;
    return {
      control_index: controlIndex,
      href: decision.href,
      hotel_id: decision.hotel_id,
      url: decision.url,
      preview: {
        expected_place_key: placeKey,
        place_match: "exact",
        exact_place_evidence: `audited_property_seed: ${property.label}`,
        distance_reference_evidence: null,
        location_evidence: [],
      },
    };
  }).filter(Boolean);
}

async function orchestrateCtripAuditedSeedDetails(
  lease,
  driver,
  deadline,
  ownedTabIds,
) {
  const listPageUrl = "https://hotels.ctrip.com/hotels/list";
  const targets = ctripAuditedSeedTargets(lease.query).slice(
    0,
    MAX_LODGING_DETAIL_PAGES_PER_LEASE,
  );
  if (!targets.length) {
    return failure(
      "failed",
      "dom_drift",
      "携程目的地联想失败，且没有通过冻结合同的公开酒店检索种子",
      listPageUrl,
      false,
      { stage: "audited_property_seed" },
    );
  }
  const detailDeadline = Math.min(
    deadline,
    Date.now() + CTRIP_LODGING_DETAIL_WORKFLOW_CAP_MS,
  );
  const quotes = [];
  const seenEvidence = new Set();
  const detailResults = [];
  for (const target of targets) {
    const diagnostic = {
      hotel_id: target.hotel_id,
      url: target.url,
      state: "pending",
    };
    try {
      await assertLeaseActive(
        lease,
        remainingTimeout(detailDeadline, 5000),
      );
      const detailTab = await chrome.tabs.create({
        url: target.href,
        active: false,
      });
      if (!detailTab || !Number.isInteger(detailTab.id)) {
        throw lifecycleError(
          "lodging_detail_tab_not_created",
          "Chrome did not return a controlled Ctrip seed detail tab",
          { stage: "audited_property_seed" },
        );
      }
      const controlledTabId = detailTab.id;
      ownedTabIds.add(controlledTabId);
      try {
        await waitForTabComplete(
          controlledTabId,
          remainingTimeout(detailDeadline, CTRIP_LODGING_DETAIL_LOAD_CAP_MS),
        );
      } catch (error) {
        if (!/timed out|timeout/i.test(String(error && error.message || error))) {
          throw error;
        }
        diagnostic.load_completed = false;
      }
      const current = await chrome.tabs.get(controlledTabId);
      const currentUrl = current.url || current.pendingUrl || "";
      const currentDecision = ctripLodgingDetailUrlDecision(
        currentUrl,
        listPageUrl,
        lease.query,
      );
      if (
        !currentDecision.allowed ||
        currentDecision.href !== target.href ||
        currentDecision.hotel_id !== target.hotel_id
      ) {
        diagnostic.state = "rejected_navigation";
        diagnostic.reason = currentDecision.reason || "detail_redirect_not_allowed";
        detailResults.push(diagnostic);
        continue;
      }
      const extraction = await visibleDetailExtractionWithRetry(
        controlledTabId,
        {
          type: "tripchord:extract",
          provider: "ctrip",
          kind: "lodging",
          query: lease.query,
          driver: {
            ...(driver || {}),
            mode: "audited_property_seed_detail_fallback",
            triggered: true,
            confirmed_query: {
              destination: lease.query.destination,
              start_date: lease.query.start_date,
              end_date: lease.query.end_date,
              adults: lease.query.adults,
              rooms: lease.query.rooms,
            },
            readback_query: {
              destination: lease.query.destination,
              start_date: lease.query.start_date,
              end_date: lease.query.end_date,
              adults: lease.query.adults,
              rooms: lease.query.rooms,
            },
            confirmation_scope: "confirmed_visible_seed_detail",
            detail_capture: {
              source: "public_audited_property_id",
              hotel_id: target.hotel_id,
              clicked_booking: false,
            },
          },
        },
        {
          lease,
          deadline: detailDeadline,
          ownedTabIds,
          timeoutCapMs: CTRIP_LODGING_DETAIL_EXTRACT_CAP_MS,
          attempts: 3,
        },
      );
      if (
        !extraction ||
        extraction.state !== "succeeded" ||
        !Array.isArray(extraction.quotes)
      ) {
        diagnostic.state = "detail_extraction_failed";
        diagnostic.reason = extraction && extraction.failure &&
          extraction.failure.code || "detail_quote_missing";
        diagnostic.gates = extraction && extraction.failure &&
          extraction.failure.details && extraction.failure.details.gates || null;
        detailResults.push(diagnostic);
        continue;
      }
      const rejections = [];
      const quoteCountBeforeTarget = quotes.length;
      for (const quote of extraction.quotes) {
        const decision = exactCtripLodgingDetailQuoteDecision(
          quote,
          target,
          listPageUrl,
          lease.query,
        );
        if (!decision.allowed) {
          rejections.push(decision.reason);
          continue;
        }
        const evidenceKey = String(quote.evidence_sha256);
        if (seenEvidence.has(evidenceKey)) continue;
        seenEvidence.add(evidenceKey);
        quotes.push(quote);
      }
      diagnostic.state = quotes.length > quoteCountBeforeTarget
        ? "succeeded"
        : "detail_quotes_rejected";
      diagnostic.quote_rejections = rejections.slice(0, 8);
      detailResults.push(diagnostic);
    } catch (error) {
      if (error && error.status === 409) throw error;
      diagnostic.state = "detail_read_failed";
      diagnostic.reason = error && error.tripchordCode ||
        (/timed out|timeout/i.test(String(error && error.message || error))
          ? "detail_timeout" : "detail_read_error");
      detailResults.push(diagnostic);
    }
  }
  const evidence = {
    stage: "audited_property_seed",
    seed_scope: "public_property_identity_only_fresh_visible_price_required",
    target_count: targets.length,
    accepted_quote_count: quotes.length,
    clicked_booking: false,
    detail_results: detailResults,
  };
  if (!quotes.length) {
    return failure(
      "failed",
      "dom_drift",
      "携程公开酒店检索种子的详情页未形成可验证实时报价",
      listPageUrl,
      false,
      evidence,
    );
  }
  return { state: "succeeded", quotes, detail_orchestration: evidence };
}

async function completeLease(
  lease,
  completion,
  timeoutMs = LEASE_COMPLETION_REQUEST_CAP_MS,
) {
  const parserVersions = new Set(
    (Array.isArray(completion && completion.quotes) ? completion.quotes : [])
      .map((quote) => String(quote && quote.parser_version || ""))
      .filter(Boolean),
  );
  const parserVersion = parserVersions.size === 1
    ? [...parserVersions][0]
    : PRODUCTION_VISIBLE_DOM_PARSER_VERSION;
  const querySha256 = await inventoryReceiptSha256(
    canonicalInventoryJson(lease.query),
  );
  const observation = {
    task_id: lease.task_id,
    provider: lease.provider,
    kind: lease.kind,
    query: lease.query,
    quote_evidence_sha256:
      Array.isArray(completion && completion.quotes)
        ? completion.quotes.map((quote) => quote.evidence_sha256)
        : [],
    parser_version: parserVersion,
  };
  const sourceExecutionAttestation = {
    schema_version: SOURCE_EXECUTION_ATTESTATION_SCHEMA,
    task_id: lease.task_id,
    provider: lease.provider,
    kind: lease.kind,
    companion_id: COMPANION_ID,
    runtime_instance_id: RUNTIME_INSTANCE_ID,
    build_identity: currentBuildIdentity(),
    execution_environment: "chrome_extension_service_worker",
    parser_version: parserVersion,
    query_sha256: querySha256,
    source_observation_sha256: await inventoryReceiptSha256(
      canonicalInventoryJson(observation),
    ),
    completed_at: new Date().toISOString(),
  };
  return bridgeFetch(`/v1/tasks/${encodeURIComponent(lease.task_id)}/complete`, {
    method: "POST",
    body: JSON.stringify({
      claim_token: lease.claim_token,
      completion,
      source_execution_attestation: sourceExecutionAttestation,
    }),
    timeoutMs,
  });
}

async function recordCompletionDiagnostic(
  error,
  completion,
  fallbackAttempted,
) {
  const status = Number(error && error.status);
  const failureDetails =
    completion &&
    completion.failure &&
    completion.failure.details &&
    typeof completion.failure.details === "object"
      ? completion.failure.details
      : {};
  const stageTrace = Array.isArray(failureDetails.stage_trace)
    ? failureDetails.stage_trace.slice(-4).map((entry) => ({
        stage: String(entry && entry.stage || "unknown").slice(0, 80),
        status: String(entry && entry.status || "unknown").slice(0, 40),
        budget_ms: Number(entry && entry.budget_ms) || 0,
        elapsed_ms: Number(entry && entry.elapsed_ms) || 0,
        remaining_lease_ms:
          Number(entry && entry.remaining_lease_ms) || 0,
        failure_code:
          entry && entry.failure_code
            ? String(entry.failure_code).slice(0, 80)
            : null,
      }))
    : [];
  const leaseTiming =
    failureDetails.lease_timing &&
    typeof failureDetails.lease_timing === "object"
      ? {
          deadline_source:
            String(
              failureDetails.lease_timing.deadline_source || "unknown",
            ).slice(0, 40),
          lease_duration_ms:
            Number(failureDetails.lease_timing.lease_duration_ms) || 0,
          completion_reserve_ms:
            Number(failureDetails.lease_timing.completion_reserve_ms) || 0,
          lease_expires_at:
            String(
              failureDetails.lease_timing.lease_expires_at || "",
            ).slice(0, 40),
          work_deadline_at:
            String(
              failureDetails.lease_timing.work_deadline_at || "",
            ).slice(0, 40),
        }
      : null;
  await chrome.storage.session.set({
    tripchordLastCompletionDiagnostic: {
      captured_at: new Date().toISOString(),
      http_status: Number.isFinite(status) ? status : null,
      error_kind:
        error && error.name === "AbortError"
          ? "request_timeout"
          : Number.isFinite(status)
            ? "http_error"
            : "network_error",
      completion_state:
        String(completion && completion.state || "unknown").slice(0, 40),
      completion_failure_code:
        completion &&
        completion.failure &&
        completion.failure.code
          ? String(completion.failure.code).slice(0, 80)
          : null,
      fallback_attempted: Boolean(fallbackAttempted),
      validation_diagnostic:
        Array.isArray(error && error.validationDiagnostic)
          ? error.validationDiagnostic
          : [],
      bridge_detail:
        error && typeof error.bridgeDetail === "string"
          ? error.bridgeDetail.slice(0, 500)
          : null,
      lease_timing: leaseTiming,
      stage_trace: stageTrace,
    },
  });
}

async function executeSingleLease(lease, executionOptions = {}) {
  const sharedTabId = Number.isInteger(executionOptions.sharedTabId)
    ? executionOptions.sharedTabId
    : null;
  const submitCompletion = executionOptions.submitCompletion !== false;
  let tabId = sharedTabId;
  let reusedTabId = null;
  let reusedExactResult = false;
  let reusedResultUrlReadback = null;
  let reusedResultUrlConfirmedFields = [];
  let reusedResultConfirmationScope = null;
  let reusedPreservedIsolationWindow = false;
  let reusedPreservedWindowId = null;
  let preservedResultTabId = null;
  let preservedResultEvidence = null;
  let pageUrl = null;
  const ownedTabIds = new Set();
  const ownedWindowIds = new Set();
  const qunarLodgingIsolationRequired = isQunarLodgingLease(lease);
  let browserIsolationEvidence = qunarLodgingIsolationRequired
    ? qunarLodgingIsolationEvidence({ lifecycle_state: "not_started" })
    : null;
  const stageTrace = [];
  const timing = leaseTiming(lease);
  const deadline = timing.work_deadline_ms;
  const timingDiagnostic = leaseTimingDiagnostic(timing);
  const finish = async (completion) => {
    let preparedCompletion = completion;
    if (qunarLodgingIsolationRequired && ownedWindowIds.size) {
      try {
        await closeOwnedWindows(ownedWindowIds, ownedTabIds);
        browserIsolationEvidence = qunarLodgingIsolationEvidence({
          lifecycle_state: "closed_before_lease_completion",
          observed_focused: false,
          observed_window_state: "normal",
          observed_active_tab_count: 1,
        });
      } catch (error) {
        browserIsolationEvidence = qunarLodgingIsolationEvidence({
          lifecycle_state: "cleanup_failed",
          cleanup_error: String(error && error.message || error),
        });
        preparedCompletion = failure(
          "failed",
          "extraction_error",
          "Companion-owned Qunar lodging window cleanup failed",
          pageUrl,
          false,
          lifecycleFailureDetails(error),
        );
      }
    }
    preparedCompletion = attachBrowserIsolationEvidence(
      preparedCompletion,
      browserIsolationEvidence,
    );
    if (
      !qunarLodgingIsolationRequired &&
      completion &&
      completion.state === "blocked" &&
      completion.failure &&
      completion.failure.code === "captcha_required" &&
      await retainHumanActionTab(tabId, ownedTabIds)
    ) {
      preparedCompletion = {
        ...completion,
        failure: {
          ...completion.failure,
          details: {
            ...(completion.failure.details || {}),
            human_action_tab_retained: true,
            human_action: "complete_captcha_then_resubmit_exact_query",
          },
        },
      };
    }
    const completionTimeout = completionRequestTimeoutMs(timing);
    const tracedCompletion = completionWithStageTrace(
      preparedCompletion,
      stageTrace,
      timingDiagnostic,
    );
    if (!submitCompletion) return tracedCompletion;
    try {
      return await completeLease(
        lease,
        tracedCompletion,
        completionTimeout,
      );
    } catch (error) {
      await recordCompletionDiagnostic(error, tracedCompletion, false);
      if (!completionContractRejected(error)) {
        throw error;
      }
      const fallbackCompletion = completionWithStageTrace(
        failure(
          "failed",
          "extraction_error",
          "扩展生成的结果未通过本地桥接契约校验",
          null,
          false,
          {
            rejected_completion_state:
              String(completion && completion.state || "unknown"),
            rejected_failure_code:
              completion &&
              completion.failure &&
              completion.failure.code
                ? String(completion.failure.code)
                : null,
            bridge_http_status: error.status,
            validation_diagnostic:
              Array.isArray(error.validationDiagnostic)
                ? error.validationDiagnostic
                : [],
            bridge_detail:
              typeof error.bridgeDetail === "string"
                ? error.bridgeDetail
                : null,
            rejected_page_location:
              pageLocationDiagnostic(pageUrl),
          },
        ),
        stageTrace,
        timingDiagnostic,
      );
      try {
        return await completeLease(
          lease,
          fallbackCompletion,
          completionRequestTimeoutMs(timing),
        );
      } catch (fallbackError) {
        await recordCompletionDiagnostic(
          fallbackError,
          fallbackCompletion,
          true,
        );
        throw fallbackError;
      }
    }
  };
  let driver = {
    mode: lease.query.search_url ? "search_url" : "visible_form",
    triggered: Boolean(lease.query.search_url),
    provider: lease.provider,
    vertical: lease.kind,
    confirmed_query: null,
    readback_query: null,
    confirmation_scope: lease.query.search_url
      ? "provider_url_only_unverified"
      : "not_started",
  };
  try {
    if (!LANDING_URLS[lease.provider] || !LANDING_URLS[lease.provider][lease.kind]) {
      return await finish(
        failure("failed", "unsupported_query", "不支持的平台或查询类型"),
      );
    }
    if (!await hasProviderPermission(lease.provider)) {
      return await finish(
        failure(
          "blocked",
          "permission_denied",
          "用户尚未授予该平台的可选域名权限",
        ),
      );
    }
    const requestedUrl = lease.query.search_url || LANDING_URLS[lease.provider][lease.kind];
    if (
      !providerHostAllowed(lease.provider, requestedUrl) ||
      !providerVerticalUrlAllowed(lease.provider, lease.kind, requestedUrl)
    ) {
      return await finish(
        failure(
          "failed",
          "unsupported_query",
          "搜索地址不属于任务指定平台或业务频道",
        ),
      );
    }
    const landing = await withInitialLandingSlot(
      stageTrace,
      deadline,
      () => withStageBudget(
        stageTrace,
        "initial_landing",
        deadline,
        INITIAL_LANDING_STAGE_CAP_MS,
        async (stageDeadlineValue) => {
          if (sharedTabId !== null) {
            await chrome.tabs.update(sharedTabId, { url: requestedUrl, active: false });
            const readiness = await waitForTabInteractive(
              sharedTabId,
              remainingTimeout(
                stageDeadlineValue - INITIAL_LANDING_INNER_GUARD_MS,
                INITIAL_LANDING_STAGE_CAP_MS - INITIAL_LANDING_INNER_GUARD_MS,
              ),
            );
            const current = await chrome.tabs.get(sharedTabId);
            return { current, readiness, shared_tab: true };
          }
          const reusable =
            await claimReusableExactLodgingResultTab(lease) ||
            await claimReusableExactFlightResultTab(lease);
          if (reusable) {
            tabId = reusable.tab_id;
            reusedTabId = reusable.tab_id;
            reusedExactResult = true;
            reusedResultUrlReadback = reusable.result_url_readback || null;
            reusedResultUrlConfirmedFields = Array.isArray(
              reusable.result_url_confirmed_fields,
            )
              ? reusable.result_url_confirmed_fields
              : [];
            reusedResultConfirmationScope =
              reusable.confirmation_scope || null;
            if (
              reusable.preserved_exact_result === true &&
              Number.isInteger(reusable.window_id)
            ) {
              reusedPreservedWindowId = reusable.window_id;
              reusedPreservedIsolationWindow =
                reusable.isolation_window === true;
            }
            try {
              const readiness = await waitForTabInteractive(
                tabId,
                remainingTimeout(
                  stageDeadlineValue - INITIAL_LANDING_INNER_GUARD_MS,
                  INITIAL_LANDING_STAGE_CAP_MS -
                    INITIAL_LANDING_INNER_GUARD_MS,
                ),
              );
              const current = await chrome.tabs.get(tabId);
              return { current, readiness, reused_exact_result: true };
            } catch {
              // A stale or crashed user tab must not poison the lease. Release
              // the reservation and fall back to the normal read-only landing
              // path, preserving the exact URL/readback gate above.
              leasedExistingTabIds.delete(tabId);
              tabId = null;
              reusedTabId = null;
              reusedExactResult = false;
              reusedResultUrlReadback = null;
              reusedResultUrlConfirmedFields = [];
              reusedResultConfirmationScope = null;
            }
          }
          let tab;
          if (qunarLodgingIsolationRequired) {
            const isolated = await createQunarLodgingIsolationWindow(
              requestedUrl,
              ownedWindowIds,
              ownedTabIds,
            );
            tab = isolated.tab;
            browserIsolationEvidence = isolated.isolation_evidence;
          } else {
            tab = await chrome.tabs.create({
              url: requestedUrl,
              active: false,
            });
          }
          tabId = tab.id;
          if (!tabId) {
            throw new Error("Chrome did not return a tab id");
          }
          ownedTabIds.add(tabId);
          const readiness = await waitForTabInteractive(
            tabId,
            remainingTimeout(
              stageDeadlineValue - INITIAL_LANDING_INNER_GUARD_MS,
              INITIAL_LANDING_STAGE_CAP_MS -
                INITIAL_LANDING_INNER_GUARD_MS,
            ),
          );
          const current = await chrome.tabs.get(tabId);
          return { current, readiness };
        },
      ),
    );
    let current = landing.current;
    pageUrl = current.url || requestedUrl;
    if (
      !providerHostAllowed(lease.provider, pageUrl) ||
      !providerVerticalUrlAllowed(lease.provider, lease.kind, pageUrl)
    ) {
      return await finish(
        failure(
          "failed",
          "navigation_error",
          "平台导航跳出了允许的域名或进入了错误业务频道",
          pageUrl,
        ),
      );
    }
    if (lease.query.search_url) {
      driver = {
        ...driver,
        ...trustedSearchUrlDriverEvidence(
          lease.provider,
          lease.kind,
          requestedUrl,
          lease.query,
        ),
      };
    }
    if (reusedExactResult) {
      browserIsolationEvidence = null;
      driver = {
        ...driver,
        mode:
          lease.kind === "flight"
            ? "reused_exact_flight_result"
            : "reused_exact_lodging_result",
        triggered: true,
        confirmed_query: {
          destination: lease.query.destination,
          start_date: lease.query.start_date,
          end_date: lease.query.end_date,
          adults: lease.query.adults,
          rooms: lease.query.rooms,
        },
        readback_query: {
          destination: lease.query.destination,
          start_date: lease.query.start_date,
          end_date: lease.query.end_date,
          adults: lease.query.adults,
          rooms: lease.query.rooms,
        },
        confirmation_scope: "confirmed_visible_search",
        reused_exact_result_url: true,
        reused_result_url_readback: reusedResultUrlReadback,
        reused_result_url_confirmed_fields: reusedResultUrlConfirmedFields,
        reused_result_url_confirmation_scope: reusedResultConfirmationScope,
        action_trace: [
          {
            action: "search",
            provider: lease.provider,
            evidence: "user_unlocked_exact_result_tab",
            read_only: true,
          },
        ],
      };
    }
    await withStageBudget(
      stageTrace,
      "content_bootstrap",
      deadline,
      CONTENT_BOOTSTRAP_STAGE_CAP_MS,
      async (stageDeadlineValue) => {
        await installContent(tabId);
        await assertLeaseActive(
          lease,
          remainingTimeout(stageDeadlineValue, 5000),
        );
      },
    );

    if (!lease.query.search_url && !reusedExactResult) {
      let preparedRun;
      try {
        preparedRun = await withStageBudget(
          stageTrace,
          "prepare_search",
          deadline,
          PREPARE_SEARCH_STAGE_CAP_MS,
          (stageDeadlineValue) =>
            prepareSearchWithLifecycle(
              tabId,
              pageUrl,
              lease,
              stageDeadlineValue,
              ownedTabIds,
            ),
        );
      } catch (error) {
        const timedOut = error && error.tripchordCode === "stage_timeout";
        const loginRequired = loginRequiredFailure(error);
        return await finish(
          failure(
            loginRequired ? "blocked" : "failed",
            timedOut
              ? "timeout"
              : loginRequired
                ? "login_required"
                : navigationFailure(error)
                  ? "navigation_error"
                  : "dom_drift",
            `可见搜索字段准备失败：${String(error && error.message || error)}`,
            pageUrl,
            timedOut,
            lifecycleFailureDetails(error),
          ),
        );
      }
      const prepared = preparedRun.prepared;
      tabId = preparedRun.tabId;
      pageUrl = preparedRun.pageUrl;
      if (!prepared || prepared.prepared !== true) {
        const missingFields = prepared && Array.isArray(prepared.missing)
          ? prepared.missing
          : [];
        if (
          lease.provider === "ctrip" &&
          lease.kind === "lodging" &&
          lease.query &&
          lease.query.options &&
          lease.query.options.stay_plan_candidate_set &&
          missingFields.includes("destination_suggestion_unconfirmed")
        ) {
          const seeded = await withStageBudget(
            stageTrace,
            "ctrip_audited_seed_detail_fallback",
            deadline,
            CTRIP_LODGING_DETAIL_WORKFLOW_CAP_MS,
            (stageDeadlineValue) =>
              orchestrateCtripAuditedSeedDetails(
                lease,
                driver,
                stageDeadlineValue,
                ownedTabIds,
              ),
          );
          return await finish(seeded);
        }
        return await finish(
          failure(
            "failed",
            "dom_drift",
            prepared.message || "没有找到可见搜索控件",
            pageUrl,
            false,
            {
              missing: prepared && prepared.missing || [],
              controls: prepared && prepared.controls || [],
              suggestions: prepared && prepared.suggestions || [],
            },
          ),
        );
      }
      let triggeredRun;
      try {
        triggeredRun = await withStageBudget(
          stageTrace,
          "trigger_search",
          deadline,
          TRIGGER_SEARCH_STAGE_CAP_MS,
          (stageDeadlineValue) =>
            triggerSearchWithLifecycle(
              tabId,
              pageUrl,
              lease,
              stageDeadlineValue,
              ownedTabIds,
            ),
        );
      } catch (error) {
        const timedOut = error && error.tripchordCode === "stage_timeout";
        const loginRequired = loginRequiredFailure(error);
        return await finish(
          failure(
            loginRequired ? "blocked" : "failed",
            timedOut
              ? "timeout"
              : loginRequired
                ? "login_required"
                : navigationFailure(error)
                  ? "navigation_error"
                  : "dom_drift",
            `可见搜索按钮触发失败：${String(error && error.message || error)}`,
            pageUrl,
            timedOut,
            lifecycleFailureDetails(error),
          ),
        );
      }
      const triggered = triggeredRun.triggered;
      if (!triggered || triggered.triggered !== true) {
        return await finish(
          failure(
            "failed",
            "dom_drift",
            triggered && triggered.message || "没有触发安全的可见搜索按钮",
            pageUrl,
            false,
            {
              missing: triggered && triggered.missing || ["search_button"],
              controls: triggered && triggered.controls || [],
            },
          ),
        );
      }
      tabId = triggeredRun.tabId;
      pageUrl = triggeredRun.pageUrl;
      driver = {
        mode: "visible_form",
        triggered: true,
        provider: lease.provider,
        vertical: lease.kind,
        confirmed_query: prepared.confirmed_query,
        readback_query: prepared.readback_query,
        confirmation_scope: "confirmed_visible_search",
        destination_confirmation_scope:
          prepared.destination_confirmation_scope || null,
        ...(prepared.lodging_search_strategy
          ? {
              lodging_search_strategy:
                prepared.lodging_search_strategy,
            }
          : {}),
        navigation_recovered:
          preparedRun.recovered || triggeredRun.recovered,
        transition_mode: triggeredRun.transitionMode || null,
        navigation_trace: [
          ...(preparedRun.navigationTrace || []),
          ...(triggeredRun.navigationTrace || []),
        ].slice(-MAX_NAVIGATION_TRACE_ENTRIES),
        ...(browserIsolationEvidence
          ? { browser_isolation: browserIsolationEvidence }
          : {}),
      };
      const searchResult = await withStageBudget(
        stageTrace,
        "search_result_bootstrap",
        deadline,
        SEARCH_RESULT_BOOTSTRAP_STAGE_CAP_MS,
        async (stageDeadlineValue) => {
          const current = await chrome.tabs.get(tabId);
          const nextPageUrl = current.url || pageUrl;
          if (
            !providerHostAllowed(lease.provider, nextPageUrl) ||
            !providerVerticalUrlAllowed(
              lease.provider,
              lease.kind,
              nextPageUrl,
            )
          ) {
            throw lifecycleError(
              "navigation_error",
              "搜索结果跳出了允许的平台域名或进入了错误业务频道",
            );
          }
          await installContent(tabId);
          await assertLeaseActive(
            lease,
            remainingTimeout(stageDeadlineValue, 5000),
          );
          return { current, pageUrl: nextPageUrl };
        },
      );
      current = searchResult.current;
      pageUrl = searchResult.pageUrl;
      if (
        !providerHostAllowed(lease.provider, pageUrl) ||
        !providerVerticalUrlAllowed(lease.provider, lease.kind, pageUrl)
      ) {
        return await finish(
          failure(
            "failed",
            "navigation_error",
            "搜索结果跳出了允许的平台域名或进入了错误业务频道",
            pageUrl,
          ),
        );
      }
    }

    if (lease.provider === "qunar" && lease.kind === "lodging") {
      let resultQueryReadback;
      try {
        resultQueryReadback = await withStageBudget(
          stageTrace,
          "qunar_result_query_readback",
          deadline,
          QUNAR_RESULT_QUERY_READBACK_STAGE_CAP_MS,
          (stageDeadlineValue) =>
            readQunarResultQueryWithRetry(
              tabId,
              lease,
              stageDeadlineValue,
              ownedTabIds,
            ),
        );
      } catch (error) {
        const timedOut = error && error.tripchordCode === "stage_timeout";
        return await finish(
          failure(
            "failed",
            timedOut ? "timeout" : "dom_drift",
            "去哪儿酒店结果页未能完成精确查询条件二次回读",
            pageUrl,
            timedOut,
            {
              result_query_readback: {
                reason: timedOut
                  ? "result_query_readback_timeout"
                  : "result_query_readback_call_failed",
              },
            },
          ),
        );
      }
      const readbackDecision = qunarLodgingResultQueryReadbackDecision(
        pageUrl,
        lease.query,
        resultQueryReadback,
      );
      if (!readbackDecision.allowed) {
        return await finish(
          failure(
            "failed",
            "dom_drift",
            "去哪儿酒店结果页的目的地、日期或入住人数二次回读不一致",
            pageUrl,
            false,
            {
              result_query_readback: readbackDecision.diagnostic,
            },
          ),
        );
      }
      driver = {
        ...driver,
        confirmed_query: readbackDecision.confirmed_query,
        readback_query: readbackDecision.readback_query,
        result_query_readback_confirmed: true,
        result_query_readback_scope:
          "qunar_visible_result_form_fields",
        result_query_readback_evidence: readbackDecision.evidence,
      };
    }

    const extractionRun = await withStageBudget(
      stageTrace,
      "list_extraction",
      deadline,
      lease.kind === "flight"
        ? FLIGHT_EXTRACTION_STAGE_CAP_MS
        : LODGING_EXTRACTION_STAGE_CAP_MS,
      async (stageDeadlineValue) => {
        const extracted = await extractWithRetry(
          tabId,
          lease,
          driver,
          stageDeadlineValue,
          ownedTabIds,
        );
        const current = await chrome.tabs.get(tabId);
        return { extracted, current };
      },
    );
    let extracted = extractionRun.extracted;
    current = extractionRun.current;
    pageUrl = current.url || current.pendingUrl || pageUrl;
    if (
      shouldTryQunarLodgingDetailOrchestration(
        extracted,
        lease,
        pageUrl,
        driver,
      )
    ) {
      extracted = await withStageBudget(
        stageTrace,
        "qunar_lodging_detail_orchestration",
        deadline,
        CTRIP_LODGING_DETAIL_WORKFLOW_CAP_MS,
        (stageDeadlineValue) =>
          orchestrateQunarLodgingDetails(
            tabId,
            lease,
            driver,
            stageDeadlineValue,
            ownedTabIds,
            extracted,
          ),
      );
    }
    if (
      shouldTryCtripLodgingDetailOrchestration(
        extracted,
        lease,
        pageUrl,
      )
    ) {
      extracted = await withStageBudget(
        stageTrace,
        "ctrip_lodging_detail_orchestration",
        deadline,
        CTRIP_LODGING_DETAIL_WORKFLOW_CAP_MS,
        (stageDeadlineValue) =>
          orchestrateCtripLodgingDetails(
            tabId,
            lease,
            driver,
            stageDeadlineValue,
            ownedTabIds,
            extracted,
          ),
      );
    }
    if (
      shouldTryFliggyLodgingDetailOrchestration(
        extracted,
        lease,
        pageUrl,
      )
    ) {
      extracted = await withStageBudget(
        stageTrace,
        "fliggy_lodging_detail_orchestration",
        deadline,
        CTRIP_LODGING_DETAIL_WORKFLOW_CAP_MS,
        (stageDeadlineValue) =>
          orchestrateFliggyLodgingDetails(
            tabId,
            lease,
            driver,
            stageDeadlineValue,
            ownedTabIds,
            extracted,
          ),
      );
    }
    if (
      shouldTryTongchengLodgingDetailOrchestration(
        extracted,
        lease,
        pageUrl,
      )
    ) {
      extracted = await withStageBudget(
        stageTrace,
        "tongcheng_lodging_detail_orchestration",
        deadline,
        CTRIP_LODGING_DETAIL_WORKFLOW_CAP_MS,
        (stageDeadlineValue) =>
          orchestrateTongchengLodgingDetails(
            tabId,
            lease,
            driver,
            stageDeadlineValue,
            ownedTabIds,
            extracted,
          ),
      );
    }
    const extraction = await withStageBudget(
      stageTrace,
      "transfer_enrichment",
      deadline,
      TRANSFER_ENRICHMENT_STAGE_CAP_MS,
      (stageDeadlineValue) =>
        enrichLodgingTransferDetails(
          extracted,
          lease,
          tabId,
          stageDeadlineValue,
          ownedTabIds,
        ),
    );
    return await finish(extraction);
  } catch (error) {
    if (error && error.status === 409) {
      // The controller cancelled or otherwise finalized this lease.
      return null;
    }
    const message = String(error && error.message || error);
    const code =
      error && error.tripchordCode === "stage_timeout" ||
      /timed out|timeout/i.test(message)
        ? "timeout"
        : error && error.tripchordCode === "login_required"
          ? "login_required"
          : error && error.tripchordCode === "navigation_error"
            ? "navigation_error"
            : "extraction_error";
    let failureDetails = lifecycleFailureDetails(error) || {};
    if (
      code === "timeout" &&
      tabId !== null &&
      lease.kind === "lodging" &&
      ["qunar", "fliggy"].includes(lease.provider)
    ) {
      // Fail fast with a clean retryable timeout while preserving the
      // established result tab, so the API retry can reuse it with a fresh
      // full-budget extraction instead of hitting a native lease timeout with
      // no receipt.
      const preserved = await preserveExactLodgingResultTab(
        lease,
        tabId,
        ownedTabIds,
        ownedWindowIds,
      );
      if (preserved) {
        preservedResultTabId = preserved.tab_id;
        preservedResultEvidence = preserved;
        failureDetails = {
          ...failureDetails,
          preserved_exact_result_tab: {
            provider: lease.provider,
            kind: lease.kind,
            tab_id: preserved.tab_id,
            url: preserved.url,
          },
        };
        if (lease.provider === "qunar") {
          browserIsolationEvidence = qunarLodgingIsolationEvidence({
            lifecycle_state: "retained_for_reuse",
            observed_focused: false,
            observed_active_tab_count: 1,
          });
        }
      }
    }
    try {
      return await finish(
        failure(
          code === "login_required" ? "blocked" : "failed",
          code,
          message,
          pageUrl,
          code === "timeout",
          failureDetails,
        ),
      );
    } catch {
      return null;
    }
  } finally {
    if (sharedTabId === null) {
      await closeOwnedWindows(ownedWindowIds, ownedTabIds);
      await closeOwnedTabs(ownedTabIds);
    }
    if (reusedTabId !== null) {
      leasedExistingTabIds.delete(reusedTabId);
    }
    if (reusedPreservedWindowId !== null) {
      const windowIdToClose = reusedPreservedWindowId;
      reusedPreservedWindowId = null;
      if (reusedPreservedIsolationWindow) {
        try {
          if (chrome.windows && typeof chrome.windows.remove === "function") {
            await chrome.windows.remove(windowIdToClose);
          }
        } catch {
          // The reused-tab window may already be closed by the user or by the
          // lease's own isolation cleanup; a leak-free completion is best-effort.
        }
      }
    }
    if (preservedResultTabId !== null) {
      preservedResultTabId = null;
    }
    sweepExpiredPreservedResultTabs();
  }
}

function ctripRangeQueryUrl(query, startDate, endDate) {
  const originCode = String(query.origin_code || "").toLowerCase();
  const destinationCode = String(query.destination_code || "").toLowerCase();
  return "https://flights.ctrip.com/international/search/" +
    `round-${originCode}-${destinationCode}` +
    `?depdate=${startDate}_${endDate}` +
    `&cabin=y_s&adult=${query.adults}&child=${query.children || 0}` +
    `&infant=${query.infants || 0}`;
}

async function executeLease(lease) {
  const range = lease && lease.range_query;
  if (!range || lease.provider !== "ctrip" || lease.kind !== "flight") {
    return executeSingleLease(lease);
  }
  const pairs = Array.isArray(range.requested_pairs) ? range.requested_pairs : [];
  if (!pairs.length) return executeSingleLease(lease);
  const firstStart = String(pairs[0][0]);
  const firstEnd = String(pairs[0][1]);
  const firstQuery = {
    ...lease.query,
    start_date: firstStart,
    end_date: firstEnd,
    search_url: ctripRangeQueryUrl(lease.query, firstStart, firstEnd),
  };
  const tab = await chrome.tabs.create({ url: firstQuery.search_url, active: false });
  const quotes = [];
  let firstFailure = null;
  try {
    for (const pair of pairs) {
      const startDate = String(pair[0]);
      const endDate = String(pair[1]);
      const query = {
        ...lease.query,
        start_date: startDate,
        end_date: endDate,
        search_url: ctripRangeQueryUrl(lease.query, startDate, endDate),
      };
      const completion = await executeSingleLease(
        { ...lease, query, range_query: null },
        { sharedTabId: tab.id, submitCompletion: false },
      );
      if (completion && Array.isArray(completion.quotes)) quotes.push(...completion.quotes);
      if (!completion || completion.state !== "succeeded") {
        firstFailure = completion && completion.failure || {
          code: "extraction_error",
          message: "批量日期中的一组查询未完成",
          retryable: true,
          page_url: null,
          details: { range_pair: [startDate, endDate] },
        };
        break;
      }
    }
  } catch (error) {
    firstFailure = {
      code: "extraction_error",
      message: `批量日期执行异常：${String(error && error.message || error)}`,
      retryable: true,
      page_url: null,
      details: {
        range_executor_error: String(error && error.stack || error).slice(0, 2000),
      },
    };
  } finally {
    if (Number.isInteger(tab.id)) {
      try { await chrome.tabs.remove(tab.id); } catch { /* best effort */ }
    }
  }
  if (firstFailure) {
    return completeLease(lease, {
      state: firstFailure.code === "captcha_required" || firstFailure.code === "login_required"
        ? "blocked" : "failed",
      quotes: [],
      failure: firstFailure,
    });
  }
  return completeLease(lease, { state: "succeeded", quotes, failure: null });
}

async function executeClaimedLeasesProviderAware(
  leases,
  executor = executeLease,
  leaseHeartbeat = null,
  heartbeatIntervalMs = ACTIVE_LEASE_HEARTBEAT_INTERVAL_MS,
) {
  const values = Array.isArray(leases) ? leases : [];
  let heartbeatTimer = null;
  const renewActiveLeases = () => {
    if (typeof leaseHeartbeat !== "function") return Promise.resolve();
    return Promise.resolve(leaseHeartbeat()).catch(() => null);
  };
  if (values.length && typeof leaseHeartbeat === "function") {
    await renewActiveLeases();
    heartbeatTimer = setInterval(
      renewActiveLeases,
      Math.max(1000, Number(heartbeatIntervalMs) || 0),
    );
  }
  const trackedExecutor = async (lease) => {
    const taskId = String(lease && lease.task_id || "");
    if (!taskId || activeLeaseIds.has(taskId)) {
      throw controlContractError("invalid_or_duplicate_active_lease");
    }
    activeLeaseIds.add(taskId);
    try {
      return await executor(lease);
    } finally {
      activeLeaseIds.delete(taskId);
    }
  };
  const qunarLodging = values.filter(
    (lease) => lease && lease.provider === "qunar" && lease.kind === "lodging",
  );
  const unrestricted = values.filter(
    (lease) => !(lease && lease.provider === "qunar" && lease.kind === "lodging"),
  );
  let cursor = 0;
  const qunarWorkers = Array.from(
    {
      length: Math.min(
        MAX_CONCURRENT_QUNAR_LODGING_LEASES,
        qunarLodging.length,
      ),
    },
    async () => {
      while (cursor < qunarLodging.length) {
        const lease = qunarLodging[cursor];
        cursor += 1;
        try {
          await trackedExecutor(lease);
        } catch {
          // Match Promise.allSettled semantics: one provider task must not
          // cancel the remaining claimed leases.
        }
      }
    },
  );
  try {
    await Promise.allSettled([
      ...unrestricted.map((lease) => trackedExecutor(lease)),
      ...qunarWorkers,
    ]);
  } finally {
    clearInterval(heartbeatTimer);
  }
}

async function pollOnce() {
  assertChromeExecutionRuntime();
  if (reloadPreparing) {
    await reconcilePendingReload();
    return;
  }
  if (polling) {
    return;
  }
  polling = true;
  try {
    sweepExpiredPreservedResultTabs();
    const config = await sessionConfig();
    if (!config.connected) {
      return;
    }
    // Every claim is also a privacy-minimal heartbeat, including when no task
    // is queued. The bridge records only this extension id, provider set, and
    // last-seen timestamp; it never receives Chrome profile or account data.
    const reloadReceipt = await reloadReceiptForClaim();
    const authorized = await authorizedScopeKeys();
    const availableLeaseSlots = Math.max(
      0,
      MAX_CONCURRENT_LEASES - activeLeaseIds.size,
    );
    if (availableLeaseSlots === 0) {
      await heartbeatOnce();
      return;
    }
    const claimBody = {
      companion_id: COMPANION_ID,
      providers: [...new Set(authorized.map((scope) => scope.split(":")[0]))],
      authorized_scope_keys: authorized,
      adapter_version: "0.2.0",
      contract_version: "tripchord-capability-v1",
      limit: availableLeaseSlots,
      build_identity: currentBuildIdentity(),
      runtime_instance_id: RUNTIME_INSTANCE_ID,
      reload_receipt: reloadReceipt,
    };
    const claimed = await bridgeFetch("/v1/tasks/claim", {
      method: "POST",
      body: JSON.stringify(claimBody),
    });
    if (reloadReceipt !== null) {
      await connectionStorage().then((storage) =>
        storage.remove(LAST_RELOAD_RECEIPT_STORAGE_KEY),
      );
    }
    if (claimed.formal_activation_request !== null &&
        claimed.formal_activation_request !== undefined) {
      await heartbeatOnce(claimed.formal_activation_request);
      return;
    }
    const leases = Array.isArray(claimed.leases) ? claimed.leases : [];
    if (claimed.control !== null && claimed.control !== undefined) {
      const control = validateReloadControl(claimed.control, leases);
      await stageReloadControl(control);
      return;
    }
    void executeClaimedLeasesProviderAware(
      leases,
      executeLease,
      heartbeatOnce,
    ).catch(() => null);
  } catch (error) {
    if (terminalOrphanedReloadReceipt(error)) {
      const storage = await connectionStorage();
      const stored = await storage.get(LAST_RELOAD_RECEIPT_STORAGE_KEY);
      if (validReloadReceipt(stored[LAST_RELOAD_RECEIPT_STORAGE_KEY])) {
        await storage.remove(LAST_RELOAD_RECEIPT_STORAGE_KEY);
        await recordReloadDiagnostic({
          state: "failed",
          failure_code: "orphaned_reload_receipt_dropped",
        });
      }
    }
    if (error && (error.status === 401 || error.status === 403)) {
      await markPairingRejected();
    }
    // Connection errors are retried; no provider action is taken without a claim.
  } finally {
    polling = false;
    clearTimeout(followupTimer);
    if (reloadPreparing) {
      followupTimer = null;
      return;
    }
    const config = await sessionConfig();
    followupTimer = config.connected ? setTimeout(pollOnce, 1500) : null;
  }
}

async function heartbeatOnce(formalActivationAck = null) {
  assertChromeExecutionRuntime();
  const config = await sessionConfig();
  if (!config.connected) return null;
  const authorized = await authorizedScopeKeys();
  const providers = [...new Set(authorized.map((scope) => scope.split(":")[0]))];
  const body = {
    companion_id: COMPANION_ID,
    providers,
    authorized_scope_keys: authorized,
    adapter_version: "0.2.0",
    contract_version: "tripchord-capability-v1",
    build_identity: currentBuildIdentity(),
    runtime_instance_id: RUNTIME_INSTANCE_ID,
  };
  const requestBody = formalActivationAck === null
    ? body
    : { ...body, formal_activation_ack: formalActivationAck };
  const heartbeat = await bridgeFetch("/v1/companions/heartbeat", {
    method: "POST",
    body: JSON.stringify(requestBody),
    timeoutMs: 5000,
  });
  if (formalActivationAck !== null) {
    return heartbeat;
  }
  const pending = heartbeat && heartbeat.formal_activation_request;
  if (pending === null || pending === undefined) {
    return heartbeat;
  }
  if (
    typeof pending !== "object" ||
    Array.isArray(pending) ||
    typeof pending.job_id !== "string" ||
    !pending.job_id ||
    typeof pending.challenge_id !== "string" ||
    !pending.challenge_id ||
    typeof pending.execution_capability !== "object" ||
    pending.execution_capability === null ||
    Array.isArray(pending.execution_capability) ||
    pending.execution_capability.terminal_job_id !== pending.job_id ||
    pending.execution_capability.challenge_id !== pending.challenge_id ||
    typeof pending.companion_binding !== "object" ||
    pending.companion_binding === null ||
    Array.isArray(pending.companion_binding) ||
    pending.companion_binding.companion_id !== body.companion_id ||
    pending.companion_binding.runtime_instance_id !== body.runtime_instance_id ||
    JSON.stringify(pending.companion_binding.build_identity) !==
      JSON.stringify(body.build_identity) ||
    !/^[0-9a-f]{64}$/.test(String(pending.companion_binding.identity_sha256 || ""))
  ) {
    throw new Error("formal activation request has an invalid signed identity");
  }
  return bridgeFetch("/v1/companions/heartbeat", {
    method: "POST",
    body: JSON.stringify({
      ...body,
      formal_activation_ack: pending,
    }),
    timeoutMs: 5000,
  });
}

chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  if (message && message.type === "tripchord:start") {
    (async () => {
      assertChromeExecutionRuntime();
      await bootstrapBackground();
      sendResponse({ ok: true });
    })().catch((error) => {
      sendResponse({
        ok: false,
        error: String(error && error.message || error),
      });
    });
    return true;
  }
  if (message && message.type === "tripchord:stop") {
    (async () => {
      await chrome.alarms.clear(POLL_ALARM);
      clearTimeout(followupTimer);
      followupTimer = null;
      await closeKeepaliveHost();
      sendResponse({ ok: true });
    })().catch((error) => {
      sendResponse({
        ok: false,
        error: String(error && error.message || error),
      });
    });
    return true;
  }
  if (message && message.type === "tripchord:status") {
    companionRuntimeStatus()
      .then((runtimeStatus) => sendResponse(runtimeStatus))
      .catch((error) => {
        sendResponse({
          ok: false,
          error: String(error && error.message || error),
        });
      });
    return true;
  }
  if (message && message.type === "tripchord:keepalive") {
    const keepaliveAction = reloadPreparing
      ? reconcilePendingReloadAndResume()
      : heartbeatOnce();
    keepaliveAction
      .then(() => sendResponse({ ok: true }))
      .catch((error) => {
        if (error && (error.status === 401 || error.status === 403)) {
          markPairingRejected().catch(() => {});
        }
        sendResponse({
          ok: false,
          error: String(error && error.message || error),
        });
      });
    return true;
  }
  return false;
});

chrome.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name === POLL_ALARM) {
    const action = reloadPreparing
      ? reconcilePendingReloadAndResume()
      : pollOnce();
    action.catch(() => {});
  }
});

async function resumePersistentConnection() {
  if (isMicrosoftEdgeRuntime()) {
    await chrome.alarms.clear(POLL_ALARM);
    clearTimeout(followupTimer);
    followupTimer = null;
    await closeKeepaliveHost();
    return;
  }
  const config = await sessionConfig();
  if (!config.connected) {
    return;
  }
  await ensureKeepaliveHost();
  chrome.alarms.create(POLL_ALARM, { periodInMinutes: 0.5 });
  pollOnce();
}

async function reconcilePendingReloadAndResume() {
  await reconcilePendingReload();
  if (!reloadPreparing) {
    await resumePersistentConnection();
  }
}

async function bootstrapBackground() {
  if (bootstrapPromise) {
    return bootstrapPromise;
  }
  bootstrapPromise = (async () => {
    if (isMicrosoftEdgeRuntime()) {
      await resumePersistentConnection();
      return;
    }
    await reconcilePendingReload();
    if (reloadPreparing) {
      return;
    }
    await resumePersistentConnection();
  })();
  try {
    return await bootstrapPromise;
  } finally {
    bootstrapPromise = null;
  }
}

chrome.runtime.onStartup.addListener(() => {
  bootstrapBackground().catch(() => {});
});

chrome.runtime.onInstalled.addListener(() => {
  bootstrapBackground().catch(() => {});
});

// An unpacked-extension reload does not emit runtime.onStartup. Resume once
// when the service worker itself is evaluated so developer reloads reconnect
// without another popup round-trip.
bootstrapBackground().catch(() => {});

if (globalThis.__TRIPCHORD_BACKGROUND_TEST_HOOKS__) {
  Object.assign(globalThis.__TRIPCHORD_BACKGROUND_TEST_HOOKS__, {
    LANDING_URLS,
    MAX_CONCURRENT_LEASES,
    MAX_CONCURRENT_QUNAR_LODGING_LEASES,
    QUNAR_LODGING_ISOLATION_SCOPE,
    QUNAR_LODGING_WINDOW_CLEANUP_ATTEMPTS,
    MAX_CTRIP_LODGING_CAPTURE_CONTROLS,
    MAX_CTRIP_LODGING_PREVIEW_CANDIDATES,
    MAX_OUTBOUND_SELECTION_ATTEMPTS,
    MAX_OUTBOUND_SELECTION_REVALIDATION_MISSES,
    LODGING_INVENTORY_RECEIPT_PARSER_VERSION,
    LODGING_INVENTORY_RECEIPT_SCHEMA_VERSION,
    LODGING_INVENTORY_SEALED_RECEIPT_SCHEMA_VERSION,
    LODGING_EXTRACTION_STAGE_CAP_MS,
    LODGING_EXTRACTION_RETRY_MIN_BUDGET_MS,
    FLIGHT_DOM_DRIFT_POLL_INTERVAL_MS,
    FLIGHT_LOADING_DOM_DRIFT_MAX_POLLS,
    FLIGHT_STAGED_DOM_DRIFT_MAX_POLLS,
    CTRIP_OUTBOUND_STAGE_WARMUP_MAX_POLLS,
    TONGCHENG_FLIGHT_RESULT_WARMUP_MAX_POLLS,
    QUNAR_GEOMETRY_STABILITY_MAX_POLLS,
    QUNAR_RESULT_QUERY_READBACK_STAGE_CAP_MS,
    QUNAR_RESULT_QUERY_READBACK_POLL_MS,
    QUNAR_EXPLICIT_EMPTY_STABILITY_MIN_INTERVAL_MS,
    QUNAR_EXPLICIT_EMPTY_OBSERVATION_CHAIN_VERSION,
    QUNAR_OBSERVATION_LINEAGE_VERSION,
    QUNAR_DETAIL_FALLBACK_SUMMARY_VERSION,
    QUNAR_DETAIL_SEED_SELECTION_POLICY,
    QUNAR_PENDING_MIN_OBSERVED_MS,
    QUNAR_PENDING_CONTRACT_VERSION,
    QUNAR_PENDING_RESULT_COUNT_TEXT,
    QUNAR_PENDING_MESSAGE,
    MAX_CONCURRENT_INITIAL_LANDINGS,
    MAX_LODGING_DETAIL_PAGES_PER_LEASE,
    MAX_QUNAR_LODGING_DETAIL_PAGES_PER_LEASE,
    MAX_FLIGGY_LODGING_DETAIL_PAGES_PER_LEASE,
    MAX_CONCURRENT_FLIGGY_LODGING_DETAILS,
    CTRIP_LODGING_DETAIL_WORKFLOW_CAP_MS,
    INITIAL_LANDING_STAGE_CAP_MS,
    LEASE_COMPLETION_MAX_RESERVE_MS,
    PREPARE_SEARCH_STAGE_CAP_MS,
    TRIGGER_SEARCH_STAGE_CAP_MS,
    completionWithStageTrace,
    completionRequestTimeoutMs,
    bridgeValidationDiagnostic,
    completionContractRejected,
    recordCompletionDiagnostic,
    boundedCtripLodgingInventoryDetails,
    backgroundInventoryReceiptPageUrl,
    closeKeepaliveHost,
    canonicalInventoryJson,
    canonicalCtripLodgingPlaceKey,
    captureCtripLodgingDetailTargets,
    captureFliggyLodgingDetailTargets,
    captureTongchengLodgingDetailTargets,
    closeOwnedTabs,
    closeOwnedWindows,
    createQunarLodgingIsolationWindow,
    isQunarLodgingLease,
    qunarLodgingIsolationEvidence,
    attachBrowserIsolationEvidence,
    retainHumanActionTab,
    ctripCaptureVisibleLodgingDetailUrls,
    ctripOutboundStageNeedsWarmup,
    tongchengFlightResultNeedsWarmup,
    ctripExpectedPlaceEvidenceDecision,
    ctripAuditedSeedTargets,
    ctripLodgingCandidateSummaries,
    ctripLodgingDetailUrlDecision,
    fliggyCaptureVisibleLodgingDetailUrls,
    tongchengCaptureVisibleLodgingDetailUrls,
    fliggyLodgingDetailUrlDecision,
    tongchengLodgingDetailUrlDecision,
    auditedFliggyLodgingDetailCandidate,
    createBackgroundLodgingInventoryReceipt,
    exactCtripLodgingDetailQuoteDecision,
    exactFliggyLodgingDetailQuoteDecision,
    exactLodgingQueryConfirmed,
    ensureKeepaliveHost,
    heartbeatOnce,
    bootstrapBackground,
    companionRuntimeStatus,
    currentBuildIdentity,
    reconcilePendingReload,
    reconcilePendingReloadAndResume,
    reloadReceiptForClaim,
    stageReloadControl,
    validateReloadControl,
    validPendingReloadMarker,
    validReloadReceipt,
    installContent,
    tripchordContentRuntimePresent,
    executeLease,
    executeClaimedLeasesProviderAware,
    extractWithRetry,
    isCtripLodgingListPageUrl,
    isFliggyLodgingListPageUrl,
    isTransientNavigationUrl,
    isMessagePortClosedError,
    isMicrosoftEdgeRuntime,
    inventoryReceiptSha256,
    leaseDeadline,
    leaseTiming,
    lifecycleFailureDetails,
    navigationUrlEvidence,
    offscreenDocumentPresent,
    pageLocationDiagnostic,
    pollOnce,
    observeTrustedProviderNavigation,
    orchestrateCtripLodgingDetails,
    orchestrateFliggyLodgingDetails,
    orchestrateTongchengLodgingDetails,
    prepareQueryForAttempt,
    prepareSearchWithLifecycle,
    providerHostDecision,
    providerHostAllowed,
    providerVerticalDecision,
    providerVerticalUrlAllowed,
    fliggyLodgingResultUrlDecision,
    qunarLodgingResultUrlDecision,
    qunarLodgingPendingDomSignature,
    qunarAuditedLodgingDetailTargets,
    qunarLodgingDetailSeedOffset,
    qunarLodgingDetailUrlDecision,
    qunarLodgingResultQueryReadbackDecision,
    qunarResultQueryReadbackIsTransient,
    readQunarResultQueryWithRetry,
    tongchengLodgingResultUrlDecision,
    tongchengElongFallbackResultUrl,
    tongchengLyFallbackResultUrl,
    auditedLodgingResultUrlDecision,
    auditedLodgingResultUrl,
    auditedFlightResultUrlDecision,
    claimReusableExactLodgingResultTab,
    claimReusableExactFlightResultTab,
    preserveExactLodgingResultTab,
    preservedLodgingResultQueryKey,
    sweepExpiredPreservedResultTabs,
    preservedExactResultTabs,
    auditedProviderLoginRedirect,
    qunarGeometryStabilityKeys,
    restartTrustedFlightSearch,
    sanitizeInventoryDiagnosticText,
    shouldTryCtripLodgingDetailOrchestration,
    shouldTryFliggyLodgingDetailOrchestration,
    shouldTryQunarLodgingDetailOrchestration,
    shouldTryTongchengLodgingDetailOrchestration,
    stabilizeQunarExplicitEmpty,
    observeQunarInventoryForDetailFallback,
    qunarInventoryExtractionFingerprint,
    qunarConfirmedEmptyExtractionFingerprint,
    qunarBoundedProviderPendingExtractionFingerprint,
    qunarExactLodgingDetailQuoteDecision,
    qunarObservationLineage,
    qunarDetailFallbackSummary,
    validQunarDetailFallbackSummary,
    validateQunarSealedConfirmedEmptyReceipt,
    sealQunarConfirmedEmptyObservationChain,
    orchestrateQunarLodgingDetails,
    adoptTrustedNavigation,
    trustedSearchUrlDriverEvidence,
    triggerSearchWithLifecycle,
    validateBackgroundLodgingInventoryReceipt,
    validateQunarParserInventoryReceipt,
    validateQunarParserConfirmedEmptyReceipt,
    visibleContentCall,
    waitForTabInteractive,
    waitForExactTabUrl,
    withInitialLandingSlot,
    withStageBudget,
    withVisibleTab,
    dynamicLeaseCompletionReserveMs,
    BUILD_META,
    CONTROL_PROTOCOL_VERSION,
    CONTROL_RECEIPT_PATH,
    RUNTIME_INSTANCE_ID,
    activeLeaseIds,
  });
}
