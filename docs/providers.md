# Provider implementation matrix

Last verified against official pages/runtime receipts and user scope decision: 2026-08-05.

| Provider path | Implemented | Contract tested | Production verified | Truth label |
|---|---:|---:|---:|---|
| Amadeus Flight Offers Search | yes | yes | no credentials | sandbox or live search by environment |
| Amadeus Flight Offers Price | yes | yes | no credentials | sandbox estimate or revalidated |
| Booking Demand accommodation search | yes | yes | no partner credentials | sandbox or live search by environment |
| Booking Demand availability | yes | yes | no partner credentials | sandbox estimate or revalidated |
| AMap geocode, POI, route, weather | yes | yes | no Web Service key | production data after key verification |
| Open-Meteo geocode + forecast | yes | yes | 2026-07-30 live canary passed | production weather only |
| Allowlisted official-page research | yes | yes | 2026-07-30 dpm.org.cn canary passed | production public-page evidence |
| RTL R4/R9 public route observation | yes | yes | 2026-07-30 anonymous GET passed | feasibility hint only; never Planner inventory or confirmed price |
| Ctrip international round-trip exact URL | yes | yes | exact live quotes observed in authorised Chrome runs | route, dates, requested adults and quote evidence remain run-scoped; no inventory lock or lowest-price guarantee |
| Fliggy international round-trip exact URL | yes | yes | canonical HGH/MLE URL reached the official result page | visible fare is per-person comparison evidence multiplied by requested adults; party availability remains unconfirmed |
| Qunar international round-trip exact URL | yes | yes | authorised live runs reached exact result pages | audited route/date/adult readback and typed quote/inventory receipt remain separate; a negative receipt is not a price |
| Tongcheng international round-trip exact URL | yes | yes | result shell reached; latest focused run exposed account-risk login gate | **flight only**; account-security/login state remains an external blocking condition |
| Tongcheng overseas lodging adapter prototype | yes | partial | 2026-08-04 single-source Maafushi canary returned `login_required`, 0 quotes; user skipped this source on 2026-08-05 | disabled and out of current scope; no retry, no coverage credit, no production exact/all-in lodging-price claim |
| Replay fixtures | yes | yes | not applicable | replay/estimated |
| User-confirmed quote import | yes | yes | user supplied | user snapshot/estimated |

Production verification is a separate gate from code completeness. A mocked
official response proves parsing and request construction, not live coverage.
The Open-Meteo and browser canaries prove only their named read-only paths; they
do not prove live airfare, rail inventory, lodging inventory, or lowest-price coverage.
The Qunar URL contract accepts only the audited Hangzhou/HGH and Malé/MLE
name-code identities, standard percent encoding, and the fixed official
parameter order. Its provider-owned result redirect is allowlisted as a flight
page, but only a subsequently normalized visible quote can prove a fare.

The enabled browser flight paths share a fail-closed round-trip evidence contract.
Ctrip and Tongcheng may click only the exact visible outbound-selection control and
must then bind the selected outbound summary, return leg, and final round-trip
price into one combination. Qunar reads both legs from one combined card and
must not click a booking control. The API rejects outbound previews, missing or
timezone-free leg timestamps, non-final price scopes, and any browser action
outside the read-only search/filter/outbound-selection allowlist.

## 2026-08-04 browser capability boundary

| Provider | Flight | Lodging | Strict coverage |
|---|---:|---:|---|
| Ctrip | enabled | enabled | flight + five lodging segments in current frozen-stay profile |
| Qunar | enabled | enabled | flight + five lodging segments in current frozen-stay profile |
| Tongcheng | enabled | disabled | flight only |

Tongcheng lodging is intentionally disabled in the frozen v4 DAG. The extension
contains a background read-only list/detail adapter under already authorised
official domains, but a 2026-08-04 single-source Maafushi canary returned
`login_required` with zero quotes. Historical visible prices also exposed
per-night-average and unknown/additional-tax boundaries, so no result may become
`QUOTE_FOUND` without an exact full-stay, party-bound, tax-scoped receipt. It is
not submitted by v4, counted as success, or used by Planner. Enabling it would
require a newly frozen capability profile rather than silently changing v4. On
2026-08-05 the user explicitly chose to skip Tongcheng overseas lodging. It is
therefore not a pending user-login task and TripChord will not probe it again in
the current scope; re-entry would require a new explicit user decision.
Fliggy has been removed from the active live matrix because its
repeated verification gate made unattended read-only evidence collection
unreliable. These are capability boundaries, not fallback success states.

For each exact date pair the current profile therefore creates 13 browser Source
tasks (Ctrip 6, Qunar 6, Tongcheng flight 1) plus four iCom public reads. The 11
browser + four iCom graph belongs to the older pre-frozen-stay profile and is
retained only as historical evidence.

### Lodging inventory evidence states

The lodging parser and backend share four mutually exclusive typed outcomes:

| State | What it proves | What it does not prove |
|---|---|---|
| `QUOTE_FOUND` | An exact, normalisable quote was observed for the bound place, dates, adults and room count | Availability remains observation-time evidence, not a locked room |
| `CONFIRMED_EMPTY` | A v2 receipt binds two parser-v1 observations for the same query and tab/window/runtime lineage, separated by at least two seconds, with independently recomputed canonical hashes | Not a second comparable price; not permanent or platform-wide no inventory |
| `BOUNDED_NO_EXACT_QUOTE` | The frozen scan bound completed without an exact quote | Not exhaustive empty inventory |
| `BOUNDED_PROVIDER_PENDING` | The provider still displayed its live-search/pending state at the observation bound | Not empty inventory and not a completed price search |

Strict publication separates Source execution completeness from exact-price
comparison coverage. Every required Source can reach a valid typed terminal
state while the selected lodging segment still fails because fewer than two
distinct providers returned `QUOTE_FOUND`.

The audited Qunar Hulhumalé place contract accepts only `胡鲁马累` or
`胡鲁马累岛` and the exact HTTPS `/city/i-hulhumale` path; a second conflicting
place input or a Maafushi path fails closed. In an earlier 2026-08-04 focused
run, Ctrip returned exact lodging prices while Qunar produced a v2
`confirmed_empty` receipt for the selected segment. In the post-Round-17
policy-fix focused run and a separate same-date canary, Qunar instead produced
`bounded_provider_pending` while the page still showed real-time search. Both
observations yielded one comparable lodging provider out of the required two;
neither state may be converted into a quote.

## Browser authority and background reload

The user grants Chrome host permissions for named official provider domains and
establishes the login session; TripChord never derives those rights from an LLM
key. After the unpacked Companion is installed and paired, build `0.1.16` can be
reloaded in the background only through the bounded control protocol. The target
must match the locally audited source SHA, manifest/runtime identity and a
current-user-owned `0600` release seal; the request also binds the existing
runtime instance, uses an idempotency key and requires a new-instance receipt.
The protocol does not open or focus a Chrome page.

This reload authority cannot install or enable an extension, expand provider
host permissions, alter accounts, restore a login session, bypass CAPTCHA or
load an unsealed build. A missing/stale companion, mismatched hash, active task
lease, expired request or exhausted retry budget fails closed.

## Official contracts

- Amadeus OAuth client credentials:
  <https://developers.amadeus.com/self-service/apis-docs/guides/developer-guides/API-Keys/authorization/>
- Amadeus flight search and price confirmation:
  <https://developers.amadeus.com/self-service/apis-docs/guides/developer-guides/resources/flights/>
- Booking accommodation search:
  <https://developers.booking.com/demand/docs/accommodations/search-for-available-properties>
- Booking Demand v3.2 availability migration:
  <https://developers.booking.com/demand/docs/migration-guide/v3.2/accommodations/availability>
- AMap POI search:
  <https://lbs.amap.com/api/webservice/guide/api-advanced/search>
- AMap route planning:
  <https://lbs.amap.com/api/webservice/guide/api/direction>
- AMap geocoding:
  <https://lbs.amap.com/api/webservice/guide/api/georegeo/>
- AMap weather:
  <https://lbs.amap.com/api/webservice/guide/api/weatherinfo>
- Open-Meteo forecast and geocoding:
  <https://open-meteo.com/en/docs> and <https://open-meteo.com/en/docs/geocoding-api>
- Palace Museum public visitor page used by the allowlisted browser canary:
  <https://www.dpm.org.cn/Visit.html>
- RTL customer portal and airport transport corroboration:
  <https://app.rtl.mv/> and <https://velana.macl.aero/guide/transport>

The RTL route-details GET consumed by the current official portal is public but
undocumented and can change without notice. Its exact evidence boundary and
six-leg split audit are recorded in
[rtl-airport-hulhumale-audit.md](rtl-airport-hulhumale-audit.md).

## Railway boundary

TripChord does not implement an undocumented 12306 adapter. Rail candidates
will use published schedule/fare references and user-confirmed snapshots, with
final availability and purchase remaining on the official channel.
# 智行候选来源边界

智行官方域名 `*.suanya.com` 与 `*.suanya.cn` 已获得用户只读授权，但截至 2026-08-02，官方 PC 网页的机票/酒店入口仅引导到 App/小程序，未发现可审计的网页报价结果面。因此智行目前只登记为候选来源，不进入 Planner、预算或 Done-Gate 平台计数。完整证据见 `benchmarks/results/zhixing-browser-capability-audit-2026-08-02.md`。
