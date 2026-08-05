import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";

const source = await readFile(
  new URL("../src/content.js", import.meta.url),
  "utf8",
);
const backgroundSource = await readFile(
  new URL("../src/background.js", import.meta.url),
  "utf8",
);
const parserSource = await readFile(
  new URL("../src/parser.js", import.meta.url),
  "utf8",
);

assert.match(
  source,
  /#trip_main_content input#destinationInput\[placeholder='目的地'\]/,
);
assert.match(source, /#trip_main_content input#checkInInput/);
assert.match(source, /#trip_main_content input#checkOutInput/);
assert.match(source, /\[role='checkbox'\]\[aria-label\^=/);
assert.match(source, /function ctripCalendarMonthOrdinal\(value\)/);
assert.match(source, /function visibleCtripCalendarMonths\(\)/);
assert.match(source, /function ctripCalendarNavigationDirection\(/);
assert.match(source, /function ctripCalendarNavigationControl\(direction\)/);
assert.match(source, /navigation_count: navigationCount/);
assert.match(
  source,
  /#ifsForm \.js-suggestcontainer \.q-suggest tr\[data-sug_type='0'\]/,
);
assert.match(
  source,
  /#interForm \.m-suggest-container table\.suggest-list tr\.item/,
);
assert.match(source, /#interForm \.m-suggest-container \.item/);
assert.match(
  source,
  /\["pointerdown", "mousedown", "mouseup", "click"\]/,
);
assert.match(source, /dispatchPointerClick\(candidate\)/);
assert.match(source, /HTMLElement\.prototype\.click\.call\(candidate\)/);
assert.match(source, /kind === "lodging" \? 4000 : 1200/);
assert.match(
  source,
  /provider === "ctrip" \|\|\s*provider === "qunar" \|\|\s*\(provider === "fliggy" && identityMatch\.matched\)/,
);
assert.match(source, /auditedDismissedSuggestionReadback/);
assert.match(source, /audited_exact_destination_label/);
assert.match(source, /audited_exact_destination_ancestor/);
assert.match(source, /lodging_candidate_not_destination/);
assert.match(
  source,
  /function suggestionCandidatePairs\(provider, kind, matchTokens = \[\]\)/,
);
assert.match(source, /function suggestionSearchTokens\(identity, value, code = null\)/);
assert.match(source, /function suggestionCandidateMentionsTokens\(candidate, tokens\)/);
assert.match(source, /function structurallyVisibleWithin\(element, ancestor\)/);
assert.match(
  source,
  /function auditedLodgingSuggestionAncestor\(\s*provider,\s*kind,\s*evidenceCandidate,/,
);
assert.match(source, /optionNodes\.length === 1/);
assert.match(source, /optionNodes\.length <= 2/);
assert.match(source, /nodeIsAuditedRow/);
assert.match(source, /MAX_MATCHED_SUGGESTION_ROOTS = 32/);
assert.match(source, /MAX_SUGGESTION_EVIDENCE_PER_ROOT = 24/);
assert.match(source, /MAX_SUGGESTION_CANDIDATE_PAIRS = 96/);
assert.match(source, /CTRIP_LODGING_SUGGESTION_POLL_TIMEOUT_MS = 5000/);
assert.match(
  source,
  /Date\.now\(\) \+ suggestionPollTimeoutMs\(provider, kind\)/,
);
assert.match(source, /left\.evidenceCandidate\.children\.length/);
assert.match(
  source,
  /candidates: suggestion\?\.selected\s*\?\s*\[\]\s*:\s*suggestionAttemptDiagnostics\(/,
);
const suggestionDiagnosticsSource = source.slice(
  source.indexOf("function suggestionAttemptDiagnostics("),
  source.indexOf("function suggestionIdentity("),
);
assert.match(
  suggestionDiagnosticsSource,
  /suggestionCandidatePairs\(provider, kind, tokens\)/,
);
assert.doesNotMatch(
  suggestionDiagnosticsSource,
  /\.flatMap\(|"div"|"span"/,
);
assert.match(
  source,
  /suggestionIdentityMatches\(\s*clickCandidate,\s*identity,\s*evidenceCandidate,/,
);
assert.match(source, /activateAuditedSuggestion\(\s*provider,\s*kind,\s*clickCandidate,/);
assert.match(source, /suggestion_candidate_not_element/);
assert.match(source, /function isElementNode\(value\)/);
assert.match(source, /typeof value\.getAttribute === "function"/);
assert.match(
  source,
  /exactCandidateLabels: \["Maafushi", "马富施", "马富士"\]/,
);
assert.match(source, /requiredAreaLabels: \["卡夫环礁", "kaafu atoll"\]/);
assert.match(source, /function auditedRequiredAreaMatches\(candidate, identity\)/);
assert.match(source, /audited_destination_area_not_visible/);
assert.match(source, /selectedLabels: \["马富施", "maafushi"\]/);
assert.match(source, /selectedLabels: \["胡鲁马累", "胡鲁马累岛"\]/);
assert.match(source, /currentVisibleInput\(provider, kind, field/);
assert.match(source, /i-ka_maafushi/);
assert.match(source, /i-hulhumale/);
assert.match(source, /933081/);
assert.match(source, /934358/);
assert.match(source, /expected_lodging_place_key/);
assert.match(source, /audited_selected_city_identity/);
assert.match(source, /audited_semantic_option_identity/);
assert.match(source, /selected_city_readback_unconfirmed/);
assert.match(
  source,
  /readback_identity: suggestion\?\.readback_identity \|\| null/,
);
assert.match(source, /suggestionAttemptDiagnostics/);
assert.match(source, /privacySafeSuggestionText/);
assert.match(source, /MAX_SUGGESTION_DIAGNOSTICS = 8/);
assert.match(source, /visible_occupancy_default/);
assert.match(source, /audited_visible_occupancy_surface/);
assert.match(source, /implicit_single_room_surface/);
assert.match(source, /rooms_visible_default_unconfirmed/);
assert.match(source, /rooms_above_provider_single_room_surface/);
assert.match(source, /__tripchord_skip_provider_mode_switch/);
assert.match(source, /button\[data-testid='flight-tab-international'\]/);
assert.match(source, /input\[data-testid='international-city-input'\]/);
assert.match(
  source,
  /input\[data-testid='international-checkin-date-input'\]/,
);
assert.match(
  source,
  /input\[data-testid='international-checkout-date-input'\]/,
);
assert.match(source, /\[data-testid='international-date-picker'\]/);
assert.match(
  source,
  /\[data-testid='international-search-button'\]\[role='button'\]/,
);
assert.match(source, /data-agent-current-value/);
assert.match(source, /tripchord:safe-select-outbound/);
assert.match(source, /invalid_outbound_selection_id/);
assert.match(source, /TripChordQuoteParser\.safeSelectOutbound/);
assert.match(source, /providerDestination: identity\.selectedLabels\[0\]/);
assert.match(source, /providerDestinationId: identity\.id/);
assert.match(source, /keyword: null/);
assert.match(
  source,
  /provider_audited_exact_city_id_then_place_revalidation/,
);
assert.match(source, /function fliggyLodgingResultUrl\(query, strategy\)/);
assert.match(source, /https:\/\/hotel\.fliggy\.com\/hotel_list3\.htm/);
assert.match(
  source,
  /prefrozen_city_id_with_visible_dates_and_occupancy/,
);
assert.match(
  source,
  /prefrozen_city_slug_with_visible_dates_and_occupancy/,
);
assert.match(source, /audited-city-id:/);
assert.match(source, /function qunarLodgingResultUrl\(query, strategy\)/);
assert.match(source, /https:\/\/hotel\.qunar\.com\/intl\/search\.jsp/);
assert.match(
  source,
  /function qunarLodgingResultQueryReadback\(/,
);
assert.match(source, /root\.querySelectorAll\("input\.textbox"\)/);
assert.match(source, /root\.querySelectorAll\("input\.inputText\.date"\)/);
assert.match(source, /root\.querySelectorAll\("\.adult-children"\)/);
assert.match(source, /const destinationControlUnambiguous =/);
assert.match(source, /conflictingDestinationInputs\.length === 0/);
assert.match(
  source,
  /audited_exact_visible_label_plus_https_city_path_v1/,
);
assert.match(source, /children_confirmed: childrenReadback === 0/);
assert.match(source, /audited_qunar_single_room_search_surface/);
assert.match(source, /tripchord:read-result-query/);
assert.match(source, /provider_audited_exact_city_slug_then_place_revalidation/);
assert.match(source, /provider_audited_exact_overseas_city_id_then_place_revalidation/);
assert.match(source, /function tongchengLodgingResultUrl\(query, strategy\)/);
assert.match(source, /https:\/\/www\.ly\.com\/hotel\/hotellist/);
assert.match(source, /url\.searchParams\.set\("adultsNumber"/);
assert.match(source, /audited_result_url_adults_parameter/);
assert.match(source, /provider !== "tongcheng"/);
assert.match(source, /url\.searchParams\.set\("cityurl"/);
assert.match(source, /url\.searchParams\.set\("from", "globalhotelpages"\)/);
assert.match(source, /auditedNavigationUrl/);
assert.match(source, /audited_navigation_url: context\.auditedNavigationUrl/);
assert.match(source, /trigger_mode: "audited_read_only_search_url"/);
assert.match(source, /provider_destination_id:/);
assert.match(source, /rooms !== 1/);
assert.match(source, /audited_qunar_city_destination_row/);
assert.match(source, /audited_qunar_exact_city_destination/);
assert.match(
  backgroundSource,
  /lodging_search_strategy:\s*prepared\.lodging_search_strategy/,
);
assert.match(
  backgroundSource,
  /"qunar_result_query_readback"/,
);
assert.match(
  backgroundSource,
  /result_query_readback_confirmed: true/,
);
assert.match(
  parserSource,
  /extraction: "qunar_lodging_detail",[\s\S]{0,1200}dom_diagnostics: qunarLodgingDetailDomDiagnostics\(/,
);
const qunarDomDiagnosticsSource = parserSource.slice(
  parserSource.indexOf("function qunarLodgingDetailDomDiagnostics"),
  parserSource.indexOf("function qunarSemanticRateRows"),
);
const qunarRateDiagnosticsSource = parserSource.slice(
  parserSource.indexOf("function qunarRateDiagnostics"),
  parserSource.indexOf("function qunarAtomicFinalPriceCandidate"),
);
assert.match(
  parserSource,
  /qunar_lodging_detail_scope_unavailable_fail_closed/,
);
assert.match(
  parserSource,
  /\["aside", "footer", "header", "nav"\]\.includes\(tag\)/,
);
assert.match(parserSource, /QUNAR_DIAGNOSTIC_PRIVATE_REGION_PATTERN/);
assert.doesNotMatch(qunarDomDiagnosticsSource, /querySelectorAll\("body \*"\)/);
assert.doesNotMatch(qunarRateDiagnosticsSource, /querySelectorAll\("body \*"\)/);
assert.match(source, /function auditedCtripFlightRecoveryNotice/);
assert.match(source, /text\.includes\("您终于回来了"\)/);
assert.match(source, /text\.includes\("航班可能有变"\)/);
assert.match(source, /window\.scrollTo\(0, 0\)/);
assert.match(
  source,
  /audited_non_transactional_flight_requery_notice/,
);
assert.match(
  source,
  /await normalizeCtripFlightExtractionSurface\(/,
);

console.log("content adapter contract: passed");
