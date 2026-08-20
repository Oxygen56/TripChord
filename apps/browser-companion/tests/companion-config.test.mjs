import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { readFile } from "node:fs/promises";

const defaultBridgeUrl = "http://127.0.0.1:8000/browser-bridge";
const files = await Promise.all([
  readFile(new URL("../src/background.js", import.meta.url), "utf8"),
  readFile(new URL("../popup.js", import.meta.url), "utf8"),
  readFile(new URL("../popup.html", import.meta.url), "utf8"),
  readFile(new URL("../README.md", import.meta.url), "utf8"),
  readFile(new URL("../src/content.js", import.meta.url), "utf8"),
  readFile(new URL("../src/parser.js", import.meta.url), "utf8"),
  readFile(new URL("../manifest.json", import.meta.url), "utf8"),
  readFile(new URL("../offscreen.html", import.meta.url), "utf8"),
  readFile(new URL("../src/offscreen.js", import.meta.url), "utf8"),
  readFile(new URL("../../web/vite.config.ts", import.meta.url), "utf8"),
  readFile(new URL("../../../deploy/nginx.conf", import.meta.url), "utf8"),
  readFile(new URL("../../../compose.yaml", import.meta.url), "utf8"),
  readFile(new URL("../../../scripts/start_live_api.py", import.meta.url), "utf8"),
  readFile(new URL("../src/build-meta.js", import.meta.url), "utf8"),
  readFile(new URL("../scripts/update-build-meta.mjs", import.meta.url), "utf8"),
]);

for (const source of files.slice(0, 4)) {
  assert.match(source, new RegExp(defaultBridgeUrl.replaceAll("/", "\\/")));
}

assert.doesNotMatch(files[0], /127\.0\.0\.1:8766/);
assert.doesNotMatch(files[1], /127\.0\.0\.1:8766/);
assert.doesNotMatch(files[2], /127\.0\.0\.1:8766/);
assert.match(files[3], /separate in-memory queue/);
assert.match(files[3], /cannot drive\s+the main API's live planning endpoints/);
assert.match(files[0], /const MAX_CONCURRENT_LEASES = 6;/);
assert.match(files[0], /limit: MAX_CONCURRENT_LEASES/);
assert.match(files[0], /LEASE_COMPLETION_MAX_RESERVE_MS = 20000/);
assert.match(files[0], /LEASE_COMPLETION_RESERVE_RATIO = 1 \/ 6/);
assert.match(files[0], /lease && lease\.lease_expires_at/);
assert.match(files[0], /deadline_source:[\s\S]*"server_absolute"/);
assert.match(files[0], /snapshot\.state !== "claimed"/);
assert.match(files[0], /snapshot\.claimed_by !== COMPANION_ID/);
assert.match(files[0], /error\.status = response\.status/);
assert.match(files[0], /error && error\.status === 409/);
assert.match(files[0], /finally \{[\s\S]*await closeOwnedTabs\(ownedTabIds\)/);
assert.match(files[0], /const MAX_LODGING_DETAIL_PAGES_PER_LEASE = 2;/);
assert.doesNotMatch(files[0], /detailTabId/);
assert.match(files[0], /chrome\.tabs\.update\(reusableTabId/);
assert.match(
  files[0],
  /providerHostAllowed\(lease\.provider, detailUrl\)/,
);
assert.match(files[0], /type: "tripchord:extract-transfer-detail"/);
assert.match(files[0], /type: "tripchord:prepare-search"/);
assert.match(files[0], /type: "tripchord:trigger-search"/);
assert.match(files[0], /type: "tripchord:read-result-query"/);
assert.match(files[0], /waitForSearchTransition/);
assert.match(files[0], /function observeTrustedProviderNavigation/);
assert.match(files[0], /chrome\.tabs\.onCreated\.addListener\(createdListener\)/);
assert.match(files[0], /tab\.openerTabId/);
assert.match(files[0], /ownedTabIds\.add\(tab\.id\)/);
assert.match(files[0], /providerVerticalUrlAllowed/);
assert.match(files[0], /sjipiao\|sijipiao/);
assert.match(files[0], /https:\/\/hotel\.qunar\.com\/global\//);
assert.match(files[0], /trusted provider navigation was not observed/);
assert.match(files[0], /prepare-search message port closed again after its single recovery/);
assert.match(files[0], /__tripchord_skip_provider_mode_switch/);
assert.match(
  files[0],
  /const observer = observeTrustedProviderNavigation\([\s\S]*type: "tripchord:trigger-search"/,
);
assert.match(files[0], /trusted_provider_navigation_after_port_close/);
assert.match(files[0], /await retainOnlyOwnedTab\(ownedTabIds, transition\.tabId\)/);
assert.match(files[0], /function trustedSearchUrlDriverEvidence/);
assert.match(files[0], /confirmation_scope: "trusted_exact_search_url"/);
assert.match(files[0], /party_availability_confirmed: partyAvailabilityConfirmed/);
assert.match(files[0], /pricing_context: pricingContext/);
assert.match(files[0], /url_confirmed_fields: urlConfirmedFields/);
assert.match(files[0], /https:\/\/flight\.qunar\.com\/twell\/flight\/Search\.jsp/);
assert.match(files[0], /requested_adults_in_search_url/);
assert.match(files[0], /\["fromCity", originIdentity\.canonicalName\]/);
assert.match(files[0], /\["adultNum", String\(query\.adults\)\]/);
assert.match(
  files[0],
  /if \(lease\.query\.search_url\) \{[\s\S]*trustedSearchUrlDriverEvidence\([\s\S]*requestedUrl/,
);
assert.match(files[0], /A full-page navigation destroys the previous content-script context/);
assert.match(files[0], /confirmation_scope: "confirmed_visible_search"/);
assert.match(files[0], /readback_query: prepared\.readback_query/);
assert.doesNotMatch(files[0], /navigation_response_lost/);
for (const provider of ["ctrip", "fliggy", "qunar"]) {
  assert.match(files[4], new RegExp(`${provider}: \\{`));
}
assert.match(files[4], /const countFields = kind === "flight" \? \["adults"\]/);
assert.match(files[4], /\["adults", "rooms"\]/);
assert.match(files[4], /visible_form_fields_readback/);
assert.match(files[4], /round_trip_mode_unconfirmed/);
assert.match(files[4], /suggestion_unconfirmed/);
assert.match(files[4], /button\.click\(\);/);
assert.doesNotMatch(files[4], /setTimeout\(\(\) => button\.click/);
assert.match(files[4], /MAX_CONTROL_DIAGNOSTICS = 12/);
assert.match(files[5], /visible_evidence: evidence/);
assert.match(files[5], /return "unknown";/);
assert.match(files[0], /type: "tripchord:safe-select-outbound"/);
assert.match(files[0], /MAX_OUTBOUND_SELECTION_ATTEMPTS = 3/);
assert.match(
  files[0],
  /MAX_OUTBOUND_SELECTION_REVALIDATION_MISSES = 3/,
);
assert.match(
  files[0],
  /FLIGHT_EXTRACTION_STAGE_CAP_MS =\s*[\s\S]*MAX_OUTBOUND_SELECTION_ATTEMPTS \* FLIGHT_OUTBOUND_ATTEMPT_INCREMENT_MS/,
);
assert.match(files[0], /"select_outbound"/);
assert.match(files[0], /"reselect_outbound"/);
assert.match(files[4], /TripChordQuoteParser\.safeSelectOutbound/);
assert.match(files[5], /workflowKind: "combined_roundtrip_card"/);
assert.match(files[5], /workflowKind: "staged_outbound_return"/);
assert.match(files[5], /combination_status: "round_trip_complete"/);
assert.match(files[5], /journey_price_scope: "round_trip"/);
assert.match(files[5], /price_finality: "final_for_combination"/);
assert.match(files[5], /party_availability_status: partyStatus/);
assert.doesNotMatch(files[0], /type: "tripchord:(?:book|order|pay)/);
assert.match(files[1], /__tripchord_pairing_probe__/);
assert.match(files[1], /response\.status === 404/);
assert.match(files[1], /response\.status === 401 \|\| response\.status === 403/);
assert.match(files[0], /Every claim is also a privacy-minimal heartbeat/);
assert.match(files[0], /last-seen timestamp/);
assert.match(files[3], /heartbeat older than \*\*45\s+seconds\*\* is stale/);
assert.match(files[3], /Qunar's audited international round-trip URL/);
assert.match(files[0], /error\.status === 401 \|\| error\.status === 403/);
assert.match(files[0], /tripchordPairingStatus: "reauth_required"/);
assert.match(files[1], /本地桥已拒绝旧令牌，需重新配对/);
assert.match(files[0], /function isMicrosoftEdgeRuntime\(\)/);
assert.match(files[0], /TripChord 实时执行已停用 Edge/);
assert.match(files[1], /当前为 Edge：TripChord 已停用/);
assert.match(files[1], /if \(!started\?\.ok\)/);
const manifest = JSON.parse(files[6]);
assert.deepEqual(manifest.permissions, [
  "alarms",
  "offscreen",
  "scripting",
  "storage",
]);
assert.ok(!manifest.permissions.includes("tabs"));
assert.deepEqual(manifest.host_permissions, ["http://127.0.0.1/*"]);
assert.match(files[1], /parsed\.hostname === "127\.0\.0\.1"/);
assert.match(files[1], /parsed\.pathname\.replace\(\/\\\/\+\$\/, ""\) === "\/browser-bridge"/);
assert.doesNotMatch(files[1], /parsed\.hostname === "localhost"/);
assert.match(files[7], /src\/offscreen\.js/);
assert.match(files[8], /KEEPALIVE_INTERVAL_MS = 15000/);
assert.match(files[8], /type: "tripchord:keepalive"/);
assert.doesNotMatch(files[8], /(?:token|cookie|account|sessionStorage)/i);
assert.match(files[0], /reasons: \["WORKERS"\]/);
assert.match(files[0], /type === "tripchord:keepalive"/);
assert.match(files[0], /await ensureKeepaliveHost\(\)/);
assert.match(files[0], /await closeKeepaliveHost\(\)/);
assert.deepEqual(manifest.optional_host_permissions, [
  "https://*.ctrip.com/*",
  "https://*.qunar.com/*",
  "https://*.ly.com/*",
  "https://*.elong.com/*",
]);
for (const inactiveOrigin of ["fliggy", "suanya"]) {
  assert.doesNotMatch(files[1], new RegExp(inactiveOrigin));
  assert.doesNotMatch(JSON.stringify(manifest.optional_host_permissions), new RegExp(inactiveOrigin));
}
assert.match(files[0], /zhixing: \["suanya\.com", "suanya\.cn"\]/);
assert.match(files[0], /fail closed until an auditable web search/);
assert.doesNotMatch(files[1], /chrome\.runtime\.reload\(\)/);
assert.match(files[1], /type: "tripchord:status"/);
assert.match(files[0], /^importScripts\("build-meta\.js"\);/);
assert.match(files[0], /build_identity: currentBuildIdentity\(\)/);
assert.match(files[0], /runtime_instance_id: RUNTIME_INSTANCE_ID/);
assert.match(files[0], /reload_receipt: reloadReceipt/);
assert.match(files[0], /control\.action !== "reload_extension"/);
assert.match(files[0], /await postReloadReceipt\(receipt\)/);
assert.match(files[0], /await chrome\.alarms\.clear\(POLL_ALARM\)/);
assert.match(files[0], /await closeKeepaliveHost\(\)/);
assert.match(files[0], /chrome\.runtime\.reload\(\)/);
assert.match(files[2], /授权三个平台查询域名/);
assert.match(files[0], /fliggy: \["fliggy\.com", "fliggy\.hk"\]/);
assert.doesNotMatch(
  JSON.stringify([
    ...(manifest.host_permissions || []),
    ...(manifest.optional_host_permissions || []),
  ]),
  /(?:taobao|alibaba)\.(?:com|hk)/,
);
assert.match(files[0], /function auditedProviderLoginRedirect/);
assert.match(files[9], /"\/browser-bridge": "http:\/\/localhost:8000"/);
assert.match(files[10], /location \/browser-bridge\//);
assert.match(files[10], /rather than the SPA index/);
assert.match(files[11], /"127\.0\.0\.1:8080:80"/);
assert.doesNotMatch(files[11], /TRIPCHORD_BROWSER_BRIDGE_ENABLED/);
assert.match(files[12], /browser-bridge-token/);
assert.match(files[12], /os\.O_WRONLY \| os\.O_CREAT \| os\.O_EXCL/);
assert.match(files[12], /0o600/);
assert.match(files[12], /pbcopy < '\{token_file\}'/);
assert.doesNotMatch(files[12], /本次配对令牌：\{token\}/);

const buildAllowlist = [
  "manifest.json",
  "popup.html",
  "popup.css",
  "popup.js",
  "offscreen.html",
  "src/background.js",
  "src/content.js",
  "src/offscreen.js",
  "src/parser.js",
];
const metaMatch = files[13].match(/Object\.freeze\((\{[\s\S]*\})\);/);
assert.ok(metaMatch, "generated build metadata must be parseable");
const buildMeta = JSON.parse(metaMatch[1]);
assert.equal(buildMeta.protocol_version, "tripchord-companion-control-v1");
assert.equal(buildMeta.manifest_version, manifest.version);
assert.equal(buildMeta.content_runtime_version, "2026-08-05.18");
assert.match(buildMeta.build_sha256, /^[0-9a-f]{64}$/);
assert.notEqual(buildMeta.build_sha256, "0".repeat(64));
const buildDigest = createHash("sha256");
for (const relativePath of buildAllowlist) {
  const content = await readFile(new URL(`../${relativePath}`, import.meta.url));
  buildDigest.update(
    Buffer.from(`${relativePath}\0${content.byteLength}\0`, "utf8"),
  );
  buildDigest.update(content);
  buildDigest.update(Buffer.from("\0", "utf8"));
}
assert.equal(buildMeta.build_sha256, buildDigest.digest("hex"));
for (const relativePath of buildAllowlist) {
  assert.match(files[14], new RegExp(`"${relativePath.replaceAll("/", "\\/")}"`));
}
assert.doesNotMatch(files[14], /BUILD_ALLOWLIST[\s\S]*src\/build-meta\.js/);

console.log("companion config contract: active-provider least privilege assertions passed");
