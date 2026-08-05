# TripChord Read-only Browser Companion

This unpacked Manifest V3 extension is the user-controlled browser half of the
TripChord local quote bridge. It uses the current Chrome profile, so a site can
see only the login state that the user already established in that profile.

## Security boundary

- Active provider profile: Ctrip, Qunar, and Tongcheng. The historical v4
  Fliggy adapter remains in source but is not advertised or scheduled by v5.
- Supported verticals: flight and lodging search.
- The extension may open provider search tabs, fill visible search fields,
  press a visible search button, and read visible quote cards.
- It has no cookie permission and never reads webpage cookies, storage,
  passwords, payment data, or account profile data.
- It does not request Chrome's broad `tabs` metadata permission. Tab lifecycle
  methods are used only for extension-created/claimed tabs, while URL access is
  limited by the four active optional host patterns below.
- It has no purchase, booking, coupon, checkout, or payment command.
- CAPTCHA and login gates are returned as `blocked`; they are never bypassed.
- Search, extraction, login and CAPTCHA tabs remain in the background. The
  extension never activates a tab or focuses a Chrome window; the user decides
  when to open a retained human-action tab.
- Unknown page structures are returned as `dom_drift`, not guessed.
- Provider host access is optional. Chrome asks the user to approve those
  domains only after the user presses **授权三个平台查询域名** in the popup.
  The production grant contains only `*.ctrip.com`, `*.qunar.com`, `*.ly.com`,
  and `*.elong.com`. Historical Fliggy and Zhixing adapters cannot receive host
  access from this extension build.
- The pairing token is kept only in the extension's private
  `chrome.storage.local` area, restricted to trusted extension contexts. It is
  never exposed to provider pages. Developer reloads and Chrome restarts resume
  the local-only connection automatically; disconnecting removes the token.
- A separate local control token authorizes only the optional external
  loopback HTTP reconcile endpoint. It is not the pairing credential and is
  not a prerequisite for the API's internal Runtime Supervisor. The token is
  held by the API only; it is never stored in the extension, passed to a
  provider page, or placed in an LLM context.

User permission to search is necessary but does not override a provider's
terms. Do not use this extension to call undocumented private endpoints or to
evade platform controls.

## Start the main API and shared bridge

From the TripChord repository, use the loopback launcher:

```bash
uv run python scripts/start_live_api.py
```

On the first run it creates `.runtime/browser-bridge-token` with mode `0600`;
later runs securely reuse the same local secret. The secret is never printed
and is deliberately retained when the API exits, so an already paired
Companion reconnects after an API restart without another paste. Copy it only
for the initial pairing, without echoing it into terminal history, with
`pbcopy < '.runtime/browser-bridge-token'`. An existing token file that is not
a current-user-owned regular file with exact mode `0600` is rejected rather
than silently repaired.

This standard launcher also explicitly enables the API's internal Browser
Companion Runtime Supervisor. No external control token is needed for that
background build reconciliation; a control token is required only when a local
HTTP client calls the optional reconcile endpoint directly.

The main API mounts the bridge at
`http://127.0.0.1:8000/browser-bridge`. The live planning endpoints and the
extension must use this mounted bridge because they share the same in-memory
task queue. The bridge rejects non-loopback clients. Do not bind it to a LAN or
public address. The extension accepts the numeric `127.0.0.1` host and exact
`/browser-bridge` path only; it does not request access to the `localhost`
hostname.

The standalone bridge factory may still be run on port `8766` for isolated
protocol diagnostics. It creates a separate in-memory queue, so it cannot drive
the main API's live planning endpoints and must not be used as the companion
URL for a real planning run.

## Install and pair

TripChord 的实时执行端固定为 **Google Chrome**。Edge 中的旧扩展必须关闭；
即使误加载，新版本也会拒绝启动轮询，避免 Edge 抢占查询任务或弹到前台。

1. Open `chrome://extensions`.
2. Enable Developer mode.
3. Choose **Load unpacked** and select this `apps/browser-companion` folder.
4. Open the extension popup.
5. Press **授权三个平台查询域名** and inspect Chrome's permission prompt.
6. Paste the bridge token, keep the default URL
   `http://127.0.0.1:8000/browser-bridge`, and press **连接只读查询**.
7. Log in to each provider yourself if the provider asks. The extension does
   not automate authentication.

Disconnecting clears the private pairing token and stops polling. Removing the
optional site permissions in `chrome://extensions` immediately disables page
access.

## Agent-controlled background reload

After this control-capable worker has been loaded once, source updates no
longer require the user to revisit `chrome://extensions`:

1. The API validates `src/build-meta.js` against a fixed nine-file release
   allowlist and independently recomputes its SHA-256 build identity. It also
   requires the owner-only `.tripchord-release-seal.json` written atomically by
   the deterministic release gate after every contract has passed. Matching
   metadata without that seal is a candidate build, never a reload target.
2. The extension heartbeat reports its loaded build identity and a fresh,
   per-worker runtime instance ID.
3. When the local verified build differs, a deterministic Runtime Executor
   Agent invokes one bounded `reload_verified_browser_companion` tool. Its only
   caller/model argument is an audited reason-code enum; it cannot receive a
   path, URL, script, target hash, or credential.
4. The bridge stops issuing new leases and waits for already claimed searches
   to finish. A task lease and a reload command are never returned together.
5. The old worker persists a private marker, posts an `accepted` receipt,
   closes its timer/alarm/offscreen keepalive, and calls
   `chrome.runtime.reload()`.
6. The new worker must prove both the exact target build and a different
   runtime instance before its `applied` receipt is accepted. It then resumes
   polling without opening or focusing a Chrome window.

Commands and receipts are idempotent. An expired, forged, mismatched, or failed
target is rejected and the background supervisor does not loop on the same
target. A successfully delivered fallback receipt is cleared; if the API lost
its old in-memory record after a restart, one orphaned receipt is discarded and
normal polling resumes.

The SHA-256 value is a deterministic local build identity, not a publisher code
signature. Command authorization comes from the loopback-only two-token control
boundary; the extension still cannot install code from a URL or choose a build
path.

Before treating an extension tree as releasable, run the portable fail-closed
gate from the repository root:

```bash
uv run python scripts/browser_companion_release_gate.py
```

The default gate is read-only. It rejects a stale `src/build-meta.js` or a
missing/stale/tampered release seal, then runs all Browser Companion JavaScript
contracts plus the targeted API control and launcher-security tests. It never
changes `manifest.json`, a runtime version, build metadata, or the seal. After a
release author has explicitly changed the versioned source, metadata
regeneration must also be explicit:

```bash
uv run python scripts/browser_companion_release_gate.py --update-build-meta
```

That option first durably removes the previous seal, regenerates only
`src/build-meta.js`, and tests the resulting unsealed candidate. The background
supervisor therefore cannot observe a half-tested candidate as publishable. If
every contract passes and the candidate identity stayed byte-stable, the gate
commits a mode-`0600` seal with a same-directory fsync and atomic replace. A
caught failure restores the previous metadata/seal; an uncatchable process
exit leaves no seal and is therefore fail-closed. The option still does not
choose or increment any version.

The seal is a local deterministic test receipt, not a publisher signature. It
binds the manifest version, content runtime version, fixed-manifest build SHA,
and build-meta SHA. The Runtime Agent exposes no tool argument that can choose,
write, or bypass it; a local actor with filesystem access remains inside the
trusted workstation boundary.

Chrome must load the control-capable service worker once when upgrading from an
older unpacked build that does not contain this protocol. That is a one-time
bootstrap boundary of the already-running old worker, not a recurring release
step. Every subsequent verified build change is handled by the background
Runtime Agent.

## Connection heartbeat and live preflight

Every 1.5-second task claim is also a heartbeat, even when the bridge has no
queued task. The bridge keeps only `companion_id`, the advertised provider set,
and `last_seen` in process memory. It never records cookies, Chrome profile or
account fields, or tab URLs in heartbeat state. A heartbeat older than **45
seconds** is stale.

The token- and loopback-protected read-only status endpoint is:

```text
GET http://127.0.0.1:8000/browser-bridge/v1/companions/status
```

With the pairing token in `X-TripChord-Bridge-Token`, the response shape is:

```json
{
  "status": "connected",
  "server_time": "2026-07-30T12:00:00Z",
  "stale_after_seconds": 45,
  "companions": [
    {
      "companion_id": "chrome-mv3-<extension-id>",
      "providers": ["ctrip", "qunar", "tongcheng"],
      "last_seen": "2026-07-30T11:59:59Z",
      "age_seconds": 1.0,
      "is_fresh": true
    }
  ]
}
```

The v5 live runner checks this endpoint with a five-second
request timeout before it submits the long-running live plan. The runner fails
immediately if no fresh Companion advertises Ctrip, Qunar, and Tongcheng; it does
not wait for the live plan's 1,000-second timeout.

If the bridge returns HTTP 401 or 403 (for example, after the API restarts with
a new token), the Companion stops polling, clears the session token, and the
popup shows **“本地桥已拒绝旧令牌，需重新配对”**. It does not silently retry an
invalid token forever.

## Search behaviour

The companion claims up to six tasks at once and executes them with
`Promise.allSettled`, so the active Ctrip/Qunar/Tongcheng searches can run
concurrently. Tongcheng is flight-only in the current provider matrix. A task
may provide a provider-owned `search_url`. Otherwise the
companion opens the provider's public flight or hotel landing page and attempts
to fill only visible, labelled fields. If the current page cannot be safely
driven, the task returns `dom_drift`.

While paired, a permission-scoped offscreen document sends a privacy-minimal
heartbeat every 15 seconds. It carries no query, token, provider URL, cookie or
account field; its only job is to prevent Chrome Manifest V3 from
discarding the service worker and its bounded lease state during a long
read-only query. Disconnecting or rejecting the pairing closes the offscreen
document.

Prepare and search commands register a navigation observer before sending the
content-script message. A closed MV3 response port is never treated as success
by itself. Recovery is allowed only when Chrome also reports a real navigation
to the same provider and the correct flight or lodging vertical. The companion
then reinjects the read-only scripts and retries `prepare-search` once. The
second request receives an in-memory-only
`__tripchord_skip_provider_mode_switch` flag so the provider mode link is not
clicked twice; that flag is not written into bridge queries or quote evidence.

Provider search pages may open a result in a new tab. The companion accepts it
only when `openerTabId` belongs to the current lease and the new URL passes the
same provider-and-vertical allowlist. All such tabs are tracked in
`ownedTabIds`; superseded tabs are closed after takeover and every remaining
owned tab is closed in the lease's `finally` block. Cross-provider navigation,
wrong-vertical navigation, no observed navigation, or a second prepare-port
failure remains fail-closed. The scheduler still claims at most six leases at a
time.

Qunar lodging starts at `https://hotel.qunar.com/global/`. A redirect to
`https://www.qunar.com/` is the wrong lodging vertical and is reported as
`navigation_error`, not accepted as a hotel result page.

Qunar's international-hotel form deliberately puts destination and dates in
the result route while keeping the per-room adult/child selection in its
`HotelMemHistory` provider cookie. TripChord never reads that cookie. It first
sets and reads back the visible source form, follows only the frozen official
result URL, and then performs a second visible readback on the resulting exact
city page. The destination control, both date controls, `2 成人 / 0 儿童`, and
the provider's audited single-room surface must all match the submitted query
before extraction begins. A missing, ambiguous, or mismatched result control
fails closed as `dom_drift`; a long-lived “实时搜索中” message is not allowed to
stand in for an exact-query confirmation.

Direct flight URLs receive trusted query evidence only when they match the
API-generated provider contract byte for byte. Ctrip's audited URL explicitly
encodes route, dates, and adult count, so all five canonical flight fields can
be confirmed. The historical, disabled Fliggy adapter's audited international URL uses
`sijipiao.fliggy.com/ie/flight_search_result.htm` and encodes route and dates
but not the requested adult count. Its driver evidence therefore records
`party_availability_confirmed: false` and
`pricing_context: per_person_x_requested_adults`; `readback_query` and
`url_confirmed_fields` do not claim an adult-count readback. The canonical
`confirmed_query` remains the submitted planning context consumed by the
parser. Qunar's audited international round-trip URL is enabled only for an
audited city-name/IATA identity pair. It percent-encodes the canonical route
names, dates, and requested adult count, so party availability is confirmed
for that requested search context. Qunar may redirect this canonical request
to a provider-owned result path; the redirect receives only the existing
provider/vertical allowlist check and is never reclassified as a second exact
URL contract. Alternate hosts, reordered or added parameters, lodging URLs,
and unaudited Qunar identities retain `provider_url_only_unverified`.

Selectors intentionally prefer visible labels, placeholders, and ARIA names.
They will require maintenance when a provider changes its public UI. Every
accepted quote and bounded search receipt must carry parser version
`tripchord-visible-dom-v3`. Unknown structures fail with `dom_drift` and include
bounded, sanitized DOM diagnostics; they are never silently accepted by a
fallback selector. A parser update therefore requires a new version plus parser,
fixture, and receipt-contract tests.

## Optional bridge-state recovery and retention

The bridge queue is memory-only unless an absolute local path is explicitly
configured before API startup:

```bash
export TRIPCHORD_BROWSER_BRIDGE_STATE_PATH="$PWD/.runtime/browser-bridge-state.json"
uv run python scripts/start_live_api.py
```

The store is atomically replaced and forced to mode `0600`. It may contain the
submitted query, sanitized visible quote evidence, provider page URL, failures,
and task timestamps. It never contains the pairing token, claim token, lease or
Companion heartbeat/browser identity. Terminal records are retained for at most
256 records; records older than one hour are pruned on startup and on every
subsequent queue operation. The file is a single-API-process recovery store, not
a shared/high-availability queue. A task that was claimed during a crash is
requeued on startup when attempts remain, with a new claim token.

Browser-task recovery and live-plan recovery use separate stores. The
live-planning `run_id` cache expires after 30 minutes and can restore an
unexpired tenant-partitioned run from the API's atomic checksummed local
snapshot without extending its original TTL. Both adapters remain
single-process/single-writer; neither is a distributed queue. Opt-in periodic
quote monitors are process-local and must be started again after an API restart.

## Parser fixture check

Open `tests/fixture-runner.html` in Chrome. It exercises active and historical
provider/vertical parser contracts plus CAPTCHA, login, and DOM-drift detection.
The test is buildless and makes no network requests; a historical regression
fixture does not grant or advertise that provider in production.

Run the buildless background lifecycle contracts with:

```bash
node tests/background-lifecycle.test.mjs
```

They cover same-tab navigation, `openerTabId` takeover, off-provider rejection,
wrong-vertical rejection, no-navigation timeout, bounded prepare recovery,
in-memory retry flags, owned-tab cleanup, and exact Ctrip/Fliggy direct-URL
evidence boundaries (the Fliggy cases are retained historical regressions).

Run the minimum-permission/runtime configuration contract with:

```bash
node tests/companion-config.test.mjs
```
