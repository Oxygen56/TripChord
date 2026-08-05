import assert from "node:assert/strict";

globalThis.__TRIPCHORD_PARSER_TEST_HOOKS__ = {};
await import("../src/parser.js");

const parser = globalThis.TripChordQuoteParser;
const parserHooks = globalThis.__TRIPCHORD_PARSER_TEST_HOOKS__;

assert.equal(
  parser.flightPriceFinality("¥5995起含税总价"),
  "starting_or_estimated",
);
assert.equal(parser.flightPriceContract("¥4962起含税总价").valid, false);

{
  const crossDayCard =
    "阿联酋航空 EK311 EK656 00:10 HGH 杭州萧山 T4 " +
    "34时30分 转 迪拜 22时 07:40 8月20日 周四 MLE " +
    "韦拉纳国际机场 T1 ¥5699 起 含税总价 选择";
  assert.equal(
    parserHooks.legFromVisibleText(
      crossDayCard,
      "2026-08-19",
      "+08:00",
      "+05:00",
    ),
    null,
  );
  assert.deepEqual(
    parserHooks.tongchengLegFromVisibleText(
      crossDayCard,
      "2026-08-19",
      "+08:00",
      "+05:00",
    ),
    {
      departure_at: "2026-08-19T00:10:00+08:00",
      arrival_at: "2026-08-20T07:40:00+05:00",
      departure_local_date: "2026-08-19",
      arrival_local_date: "2026-08-20",
      departure_local_time: "00:10",
      arrival_local_time: "07:40",
      arrival_day_offset: 1,
      timezone_source: "audited_airport_code_mapping",
      visible_evidence: crossDayCard,
    },
  );
  assert.equal(
    parserHooks.tongchengLegFromVisibleText(
      crossDayCard.replace("8月20日", "8月25日"),
      "2026-08-19",
      "+08:00",
      "+05:00",
    ),
    null,
  );
  const selectedOutboundSummary =
    "去程已选东方航空MU565908-19 09:15杭州萧山国际机场T3" +
    "转昆明08-19 19:35韦拉纳国际机场T1重选去程";
  const selectedRoute = parserHooks.flightLegRouteEvidence(
    selectedOutboundSummary,
    {
      origin: "杭州",
      destination: "马累",
      origin_code: "HGH",
      destination_code: "MLE",
    },
    "outbound",
    "tongcheng_selected_outbound_test",
  );
  assert.equal(selectedRoute.matches_expected, true);
  assert.deepEqual(
    parserHooks.tongchengLegFromVisibleText(
      selectedOutboundSummary,
      "2026-08-19",
      "+08:00",
      "+05:00",
    ),
    {
      departure_at: "2026-08-19T09:15:00+08:00",
      arrival_at: "2026-08-19T19:35:00+05:00",
      departure_local_date: "2026-08-19",
      arrival_local_date: "2026-08-19",
      departure_local_time: "09:15",
      arrival_local_time: "19:35",
      arrival_day_offset: 0,
      timezone_source: "audited_airport_code_mapping",
      visible_evidence: selectedOutboundSummary,
    },
  );
  const crossDaySelectedOutboundSummary =
    "去程已选新海航｜首都航空JD590708-20 21:45杭州萧山国际机场T3" +
    "转北京08-21 12:20韦拉纳国际机场T1重选去程";
  assert.deepEqual(
    parserHooks.tongchengLegFromVisibleText(
      crossDaySelectedOutboundSummary,
      "2026-08-20",
      "+08:00",
      "+05:00",
    ),
    {
      departure_at: "2026-08-20T21:45:00+08:00",
      arrival_at: "2026-08-21T12:20:00+05:00",
      departure_local_date: "2026-08-20",
      arrival_local_date: "2026-08-21",
      departure_local_time: "21:45",
      arrival_local_time: "12:20",
      arrival_day_offset: 1,
      timezone_source: "audited_airport_code_mapping",
      visible_evidence: crossDaySelectedOutboundSummary,
    },
  );
  assert.equal(
    parserHooks.tongchengLegFromVisibleText(
      crossDaySelectedOutboundSummary.replace("08-21", "08-25"),
      "2026-08-20",
      "+08:00",
      "+05:00",
    ),
    null,
  );
  const ownerDocument = {
    defaultView: null,
    querySelectorAll(selector) {
      return selector === ".s-trip" ? [returnStage] : [];
    },
  };
  const element = (textContent) => ({
    nodeType: 1,
    parentElement: null,
    ownerDocument,
    textContent,
    hidden: false,
    isConnected: false,
    getAttribute() {
      return null;
    },
  });
  const title = element("去程已选");
  const reselect = element("重选去程");
  const carrier = element("首都航空");
  const returnStage = element("选择返程：马累-杭州");
  const summary = {
    ...element(crossDaySelectedOutboundSummary),
    matches(selector) {
      return selector === ".repeatChooseGo";
    },
    querySelector(selector) {
      if (selector === ".hasChooseTitle") return title;
      if (selector === ".repeatButton") return reselect;
      return null;
    },
    querySelectorAll(selector) {
      return selector === ".airways-title" ? [carrier] : [];
    },
  };
  const query = {
    origin: "杭州",
    destination: "马累",
    origin_code: "HGH",
    destination_code: "MLE",
    start_date: "2026-08-20",
    end_date: "2026-08-26",
    adults: 2,
  };
  const confirmedQuery = {
    origin: query.origin,
    destination: query.destination,
    origin_code: query.origin_code,
    destination_code: query.destination_code,
    start_date: query.start_date,
    end_date: query.end_date,
    adults: query.adults,
  };
  const driver = {
    mode: "search_url",
    triggered: true,
    confirmation_scope: "trusted_exact_search_url",
    party_availability_confirmed: true,
    confirmed_query: confirmedQuery,
    readback_query: confirmedQuery,
    action_trace: [
      { action: "search", provider: "tongcheng", evidence: "trusted URL" },
    ],
  };
  const autoDriver = parserHooks.tongchengAutoSelectedOutboundDriver(
    summary,
    query,
    driver,
  );
  assert.equal(
    autoDriver.selected_outbound.outbound_departure_at,
    "2026-08-20T21:45:00+08:00",
  );
  assert.equal(
    autoDriver.selected_outbound.outbound_arrival_at,
    "2026-08-21T12:20:00+05:00",
  );
  assert.deepEqual(
    autoDriver.action_trace.map((item) => item.action),
    ["search", "provider_auto_selected_outbound"],
  );
  const returnCard =
    "阿联酋航空EK659 11:15 MLE 韦拉纳T1 25时40分转迪拜 " +
    "15:558月28日 周五 HGH 杭州萧山T4 ¥6359起含税总价选择";
  assert.deepEqual(
    parserHooks.tongchengLegFromVisibleText(
      returnCard,
      "2026-08-27",
      "+05:00",
      "+08:00",
    ),
    {
      departure_at: "2026-08-27T11:15:00+05:00",
      arrival_at: "2026-08-28T15:55:00+08:00",
      departure_local_date: "2026-08-27",
      arrival_local_date: "2026-08-28",
      departure_local_time: "11:15",
      arrival_local_time: "15:55",
      arrival_day_offset: 1,
      timezone_source: "audited_airport_code_mapping",
      visible_evidence: returnCard,
    },
  );
}

{
  const ownerDocument = { defaultView: null };
  const control = {
    nodeType: 1,
    parentElement: null,
    ownerDocument,
    textContent: "余 7 张 选择",
    disabled: false,
    getAttribute() {
      return null;
    },
  };
  const card = {
    textContent: "马累 MLE 至 杭州 HGH 余 7 张 选择",
    querySelectorAll(selector) {
      return selector === ".flight-btn" ? [control] : [];
    },
  };
  control.parentElement = card;
  assert.equal(
    parserHooks.tongchengFlightAvailabilityEvidence(card),
    "余 7 张 选择",
  );
}

{
  const ownerDocument = { defaultView: null };
  const card = {
    nodeType: 1,
    tagName: "DIV",
    parentElement: null,
    ownerDocument,
    textContent:
      "阿联酋航空 EK311 EK656 00:10 HGH 杭州萧山 " +
      "07:40 MLE 韦拉纳国际机场 ¥5699 起 含税总价 选择",
    getAttribute() {
      return null;
    },
    querySelectorAll(selector) {
      if (selector === ".airways-title") {
        return [{
          nodeType: 1,
          parentElement: card,
          ownerDocument,
          textContent: "阿联酋航空",
          getAttribute() {
            return null;
          },
        }];
      }
      return [];
    },
  };
  const control = {
    nodeType: 1,
    parentElement: card,
    ownerDocument,
    textContent: "余7张 选择",
    disabled: false,
    getAttribute() {
      return null;
    },
    matches(selector) {
      return selector === ".flight-btn";
    },
  };
  const root = {
    querySelectorAll(selector) {
      return selector.includes(".flight-item .flight-btn")
        ? [control]
        : [];
    },
  };
  const query = {
    origin: "杭州",
    destination: "马累",
    origin_code: "HGH",
    destination_code: "MLE",
    start_date: "2026-08-19",
    end_date: "2026-08-24",
    adults: 2,
  };
  assert.deepEqual(
    parserHooks.exactOutboundControls("tongcheng", root, query),
    [control],
  );
  assert.equal(
    parserHooks.flightCarrierText(card),
    "阿联酋航空",
  );
}

{
  const ownerDocument = { defaultView: null };
  const control = {
    nodeType: 1,
    parentElement: null,
    ownerDocument,
    textContent: "余8张选择",
    disabled: false,
    hidden: false,
    getAttribute() {
      return null;
    },
  };
  const price = {
    nodeType: 1,
    parentElement: null,
    ownerDocument,
    textContent: "¥8046起含税总价",
    hidden: false,
    getAttribute() {
      return null;
    },
  };
  const canonicalCard = {
    nodeType: 1,
    parentElement: null,
    ownerDocument,
    textContent:
      "阿联酋航空 EK659 11:15 MLE 韦拉纳T1 转迪拜 " +
      "15:55 8月13日 HGH 杭州萧山T4 ¥8046起含税总价余8张选择",
    hidden: false,
    getAttribute() {
      return null;
    },
    querySelectorAll(selector) {
      if (selector === ".flight-btn") return [control];
      if (selector.includes("price")) return [price];
      return [];
    },
  };
  control.parentElement = canonicalCard;
  price.parentElement = canonicalCard;
  const nestedContent = { ...canonicalCard, parentElement: canonicalCard };
  const root = {
    querySelectorAll(selector) {
      if (selector === ".flight-item") return [canonicalCard];
      if (selector === "[class*='flight-item']") {
        return [canonicalCard, nestedContent];
      }
      return [];
    },
  };
  const cards = parserHooks.stagedReturnCards("tongcheng", root, {
    origin: "杭州",
    destination: "马累",
    origin_code: "HGH",
    destination_code: "MLE",
    start_date: "2026-08-08",
    end_date: "2026-08-12",
  });
  assert.deepEqual(cards, [canonicalCard]);
}

{
  const emptyRoot = {
    querySelectorAll() {
      return [];
    },
  };
  const query = {
    origin_code: "HGH",
    destination_code: "MLE",
    start_date: "2026-08-08",
    end_date: "2026-08-12",
  };
  const tongcheng = await parser.safeSelectOutbound(
    "tongcheng",
    emptyRoot,
    query,
    "missing-selection",
  );
  assert.equal(tongcheng.selected, false);
  assert.equal(
    tongcheng.code,
    "outbound_selection_evidence_changed",
  );
  const qunar = await parser.safeSelectOutbound(
    "qunar",
    emptyRoot,
    query,
    "missing-selection",
  );
  assert.equal(qunar.code, "provider_has_no_safe_outbound_stage");
}

assert.match(
  String(parser.qunarPriceEvidence || ""),
  /numeric_attribute_samples/,
);

{
  const gate = parser.pageGate({
    body: {
      innerText:
        "亲，请拖动下方滑块完成验证。通过验证以确保正常访问。",
      textContent: "",
    },
  });
  assert.equal(gate.state, "blocked");
  assert.equal(gate.code, "captcha_required");
  assert.equal(gate.details.evidence_kind, "actionable_copy");
  assert.equal(gate.details.matched_text, "拖动下方滑块完成验证");
}

{
  const gate = parser.pageGate({
    body: {
      innerText:
        "马富士酒店搜索结果 共 32 家酒店。账户与安全验证说明。",
      textContent: "",
    },
    querySelectorAll() {
      return [];
    },
  });
  assert.equal(gate, null);
}

{
  const gate = parser.pageGate({
    body: {
      innerText:
        "您的账号可能存在风险，为了您的账号安全请验证通过后使用。前往验证",
      textContent: "",
    },
    querySelectorAll() {
      return [];
    },
  });
  assert.equal(gate.state, "blocked");
  assert.equal(gate.code, "login_required");
  assert.equal(gate.message, "平台要求用户本人完成账号安全验证");
  assert.equal(gate.details.detector_version, "visible-login-gate-v2");
  assert.equal(gate.details.human_action_required, true);
}

{
  const ownerDocument = { defaultView: null };
  const visibleSlider = {
    nodeType: 1,
    parentElement: null,
    hidden: false,
    isConnected: true,
    ownerDocument,
    getAttribute() {
      return null;
    },
  };
  const gate = parser.pageGate({
    body: {
      innerText: "安全验证",
      textContent: "",
    },
    querySelectorAll(selector) {
      return selector === "[class*='slider']" ? [visibleSlider] : [];
    },
  });
  assert.equal(gate.state, "blocked");
  assert.equal(gate.code, "captcha_required");
  assert.equal(
    gate.details.evidence_kind,
    "context_copy_with_visible_control",
  );
}

{
  let styleChecks = 0;
  let rectChecks = 0;
  const ownerDocument = {
    defaultView: {
      getComputedStyle() {
        styleChecks += 1;
        return { display: "block", visibility: "visible" };
      },
    },
  };
  const node = (textContent, { width = 120, height = 24 } = {}) => ({
    nodeType: 1,
    parentElement: null,
    hidden: false,
    isConnected: true,
    ownerDocument,
    textContent,
    getAttribute() {
      return null;
    },
    getBoundingClientRect() {
      rectChecks += 1;
      return { width, height };
    },
  });
  const irrelevant = Array.from(
    { length: 2000 },
    (_, index) => node(`无关节点 ${index}`),
  );
  const hiddenExact = node("选为去程", { width: 0, height: 0 });
  const visibleExact = node("选为去程");
  const matches = parserHooks.matchingVisibleNodes(
    {
      querySelectorAll() {
        return [...irrelevant, hiddenExact, visibleExact];
      },
    },
    "*",
    /^选为去程$/,
    80,
    10,
  );
  assert.deepEqual(matches, [visibleExact]);
  assert.equal(styleChecks, 2);
  assert.equal(rectChecks, 2);
}

{
  const ownerDocument = { defaultView: null };
  const nodes = Array.from(
    { length: parserHooks.MAX_VISIBLE_NODE_SCAN_NODES + 1 },
    () => ({
      nodeType: 1,
      parentElement: null,
      hidden: false,
      isConnected: true,
      ownerDocument,
      textContent: "可见节点",
      getAttribute() {
        return null;
      },
    }),
  );
  assert.throws(
    () =>
      parserHooks.visibleNodes(
        { querySelectorAll: () => nodes },
        ["*"],
        nodes.length,
      ),
    (error) =>
      error.tripchordParserCode === "dom_scan_budget_exhausted" &&
      error.tripchordParserDetails.scanned_nodes === nodes.length,
  );
}

{
  const ownerDocument = {
    defaultView: {
      getComputedStyle() {
        return { display: "block", visibility: "visible" };
      },
    },
  };
  const oversizedHiddenSurface = Array.from(
    { length: parserHooks.MAX_VISIBLE_NODE_SCAN_NODES + 1 },
    () => ({
      nodeType: 1,
      tagName: "DIV",
      parentElement: null,
      hidden: false,
      isConnected: true,
      ownerDocument,
      textContent: "无可验证航班证据",
      getAttribute() {
        return null;
      },
      getBoundingClientRect() {
        return { width: 0, height: 0 };
      },
    }),
  );
  const root = {
    body: { innerText: "", textContent: "" },
    documentElement: {},
    querySelectorAll() {
      return oversizedHiddenSurface;
    },
  };
  const output = await parser.inspectFlightPage(
    "ctrip",
    root,
    "https://flights.ctrip.com/results",
    new Date("2026-07-31T00:00:00Z"),
    {
      origin: "杭州",
      destination: "马累",
      origin_code: "HGH",
      destination_code: "MLE",
      start_date: "2026-08-12",
      end_date: "2026-08-18",
    },
  );
  assert.equal(output.state, "failed");
  assert.deepEqual(output.quotes, []);
  assert.equal(output.failure.code, "extraction_error");
  assert.equal(
    output.failure.details.diagnostic_code,
    "dom_scan_budget_exhausted",
  );
}

{
  const body = { nodeType: 1, tagName: "BODY", parentElement: null };
  const documentElement = {
    nodeType: 1,
    tagName: "HTML",
    parentElement: null,
  };
  const candidate = {
    nodeType: 1,
    tagName: "DIV",
    parentElement: body,
    textContent: "航班结果仍在加载",
    querySelectorAll() {
      throw new Error("implausible ancestors must not trigger subtree scans");
    },
  };
  const control = {
    parentElement: candidate,
    ownerDocument: { body, documentElement },
  };
  assert.equal(
    parserHooks.semanticFlightCardFromControl(
      "ctrip",
      control,
      {
        origin: "杭州",
        destination: "马累",
        origin_code: "HGH",
        destination_code: "MLE",
        start_date: "2026-08-12",
        end_date: "2026-08-18",
      },
      "outbound",
    ),
    null,
  );
}

assert.deepEqual(
  parser.safeQuery({
    destination: "Maafushi",
    options: {
      segment: "middle",
      expected_package_area: "destination_island",
      expected_lodging_place_key: "Maafushi",
      password: "must-not-survive",
      arbitrary: true,
    },
  }).options,
  {
    segment: "middle",
    expected_package_area: "destination_island",
    expected_lodging_place_key: "maafushi",
  },
);
assert.equal(
  parser.safeQuery({
    options: { expected_lodging_place_key: "马富施" },
  }).options.expected_lodging_place_key,
  "maafushi",
);
assert.equal(
  parser.safeQuery({
    options: { expected_lodging_place_key: "胡鲁马累" },
  }).options.expected_lodging_place_key,
  "hulhumale",
);

assert.equal(
  parser.stableTitledDigitAmount(
    ["6600", "6600", "6600", "6600"],
    ["6", "6", "0", "0"],
  ),
  "6600",
);
assert.equal(
  parser.stableTitledDigitAmount(
    ["6600", "6600", "6600", "6600"],
    ["0", "6", "0", "6"],
  ),
  null,
);

{
  const makeNode = ({ className = "", text = "", title = null } = {}) => ({
    nodeType: 1,
    parentElement: null,
    hidden: false,
    isConnected: false,
    ownerDocument: { defaultView: null },
    children: [],
    textContent: text,
    getAttribute(name) {
      if (name === "class") return className;
      if (name === "title") return title;
      return null;
    },
  });
  const container = makeNode({ text: "人均含税价 ¥ 4708" });
  const priceSurface = makeNode({
    className: "fix_price",
    text: "470812",
    title: "4708",
  });
  priceSurface.parentElement = container;
  const rollers = [..."470812"].map((digit) => {
    const node = makeNode({ text: digit, title: "4708" });
    node.parentElement = priceSurface;
    return node;
  });
  priceSurface.children = rollers;
  container.children = [priceSurface];
  container.querySelectorAll = (selector) =>
    selector === "[title]" ? [priceSurface, ...rollers] : [];
  const recovered = parser.qunarTitledDigitPriceEvidence(container);
  assert.equal(recovered.amount_text, "4708");
  assert.equal(
    recovered.evidence_source,
    "consistent_visible_price_surface_title",
  );
  rollers[0].getAttribute = (name) =>
    name === "title" ? "5708" : name === "class" ? "" : null;
  assert.equal(parser.qunarTitledDigitPriceEvidence(container), null);
}
assert.equal(
  parser.stableTitledDigitAmount(
    ["6600", "6600", "6600", "6600", "6600"],
    ["6", "6", "0", "0", "6"],
  ),
  null,
);

{
  const rect = (left, top, right, bottom) => ({
    left,
    top,
    right,
    bottom,
    width: right - left,
    height: bottom - top,
  });
  const candidates = [];
  for (const [index, digit] of [..."6600"].entries()) {
    const left = index * 12;
    const column = `column-${index}`;
    const columnRect = rect(left, 0, left + 10, 20);
    candidates.push(
      {
        column,
        digit,
        glyph_rect: rect(left, 0, left + 10, 20),
        column_rect: columnRect,
        clip_rects: [columnRect],
        opacity: 1,
        display: "block",
        visibility: "visible",
      },
      {
        column,
        digit: String((Number(digit) + 1) % 10),
        glyph_rect: rect(left, 21, left + 10, 41),
        column_rect: columnRect,
        clip_rects: [columnRect],
        opacity: 1,
        display: "block",
        visibility: "visible",
      },
    );
  }
  assert.equal(
    parserHooks.geometryClippedDigitAmount(candidates),
    "6600",
  );
  const overlappingGlyphs = structuredClone(candidates);
  overlappingGlyphs[1].glyph_rect = rect(0, 0, 10, 20);
  assert.equal(
    parserHooks.geometryClippedDigitAmount(overlappingGlyphs),
    null,
  );
  const missingColumn = candidates.filter(
    (candidate) => candidate.column !== "column-2",
  );
  assert.equal(
    parserHooks.geometryClippedDigitAmount(missingColumn),
    "660",
  );
  const wrongOrder = structuredClone(candidates);
  wrongOrder
    .filter((candidate) => candidate.column === "column-2")
    .forEach((candidate) => {
      candidate.column_rect.left = 8;
      candidate.column_rect.right = 18;
    });
  assert.equal(
    parserHooks.geometryClippedDigitAmount(wrongOrder),
    null,
  );
}

{
  const rect = (left, top, right, bottom) => ({
    left,
    top,
    right,
    bottom,
    width: right - left,
    height: bottom - top,
  });
  const liveShape = ({
    overlappingGlyph = false,
    sharedOverflow = "hidden",
  } = {}) => {
    const ownerDocument = { defaultView: null };
    const makeNode = ({
      className = "",
      textContent = "",
      box = rect(0, 0, 46, 20),
      overflow = "visible",
    } = {}) => ({
      nodeType: 1,
      hidden: false,
      isConnected: true,
      ownerDocument,
      parentElement: null,
      children: [],
      textContent,
      _className: className,
      _box: box,
      _overflow: overflow,
      getAttribute(name) {
        if (name === "class") {
          return this._className;
        }
        return null;
      },
      getBoundingClientRect() {
        return this._box;
      },
      querySelectorAll() {
        return [];
      },
    });
    ownerDocument.defaultView = {
      getComputedStyle(node) {
        return {
          display: "block",
          visibility: "visible",
          opacity: "1",
          overflow: node._overflow,
          overflowX: node._overflow,
          overflowY: node._overflow,
          transform: "none",
        };
      },
    };

    const container = makeNode({
      className: "col-price",
      textContent: "¥6600 含税总价",
    });
    const sharedClip = makeNode({
      className: "price-roller",
      overflow: sharedOverflow,
    });
    sharedClip.parentElement = container;
    container.children = [sharedClip];
    const descendants = [sharedClip];
    for (const [index, digit] of [..."6600"].entries()) {
      const left = index * 12;
      const columnBox = rect(left, 0, left + 10, 20);
      const column = makeNode({
        className: "prc",
        box: columnBox,
      });
      column.parentElement = sharedClip;
      const visibleGlyph = makeNode({
        className: "digit",
        textContent: digit,
        box: columnBox,
      });
      visibleGlyph.parentElement = column;
      const hiddenGlyph = makeNode({
        className: "digit",
        textContent: String((Number(digit) + 1) % 10),
        box:
          overlappingGlyph && index === 0
            ? columnBox
            : rect(left, 22, left + 10, 42),
      });
      hiddenGlyph.parentElement = column;
      column.children = [visibleGlyph, hiddenGlyph];
      sharedClip.children.push(column);
      descendants.push(column, visibleGlyph, hiddenGlyph);
    }
    container.querySelectorAll = (selector) =>
      selector === "*" ? descendants : [];
    return container;
  };

  const liveContainer = liveShape();
  const evidence =
    parserHooks.qunarGeometryDigitPriceEvidence(liveContainer);
  assert.deepEqual(
    evidence && {
      price_text: evidence.price_text,
      amount_text: evidence.amount_text,
      digit_leaf_count: evidence.digit_leaf_count,
      evidence_source: evidence.evidence_source,
    },
    {
      price_text: "含税总价 ¥6600",
      amount_text: "6600",
      digit_leaf_count: 4,
      evidence_source:
        "geometry_clipped_visible_digit_sequence",
    },
  );
  assert.equal(
    parserHooks.qunarGeometryDigitPriceEvidence(
      liveShape({ overlappingGlyph: true }),
    ),
    null,
  );
  assert.equal(
    parserHooks.qunarGeometryDigitPriceEvidence(
      liveShape({ sharedOverflow: "visible" }),
    ),
    null,
  );
}

{
  const ownerDocument = { defaultView: null };
  ownerDocument.defaultView = {
    getComputedStyle() {
      return {
        display: "block",
        visibility: "visible",
      };
    },
  };
  const node = (attributes) => ({
    nodeType: 1,
    hidden: false,
    isConnected: true,
    ownerDocument,
    parentElement: null,
    children: [],
    textContent: "",
    getAttribute(name) {
      return attributes[name] || null;
    },
    getAttributeNames() {
      return Object.keys(attributes);
    },
    getBoundingClientRect() {
      return {
        left: 0,
        top: 0,
        right: 120,
        bottom: 24,
        width: 120,
        height: 24,
      };
    },
    querySelectorAll() {
      return [];
    },
  });
  const container = node({
    "aria-label": "含税总价 ¥6600",
    "data-price-id": "opaque-price-row",
  });
  const numericTitle = node({ title: "6600" });
  numericTitle.parentElement = container;
  container.children = [numericTitle];
  container.querySelectorAll = () => [numericTitle];
  const diagnostic =
    parserHooks.qunarSingleAttributePriceDiagnostic(container);
  assert.deepEqual(diagnostic, {
    outcome: "strong_single_attribute_contract_found",
    scanned_node_count: 2,
    aria_label_attribute_count: 1,
    aria_value_attribute_count: 0,
    title_attribute_count: 1,
    alt_attribute_count: 0,
    price_named_data_attribute_count: 1,
    numeric_only_attribute_count: 1,
    numeric_attribute_samples: [
      {
        attribute: "title",
        value_length: 4,
        text_digit_length: 0,
        value_matches_text: false,
        class_name: "",
      },
    ],
    single_currency_amount_attribute_count: 1,
    final_tax_total_attribute_count: 1,
    final_per_person_tax_attribute_count: 0,
    nonfinal_price_attribute_count: 0,
    negative_tax_attribute_count: 0,
  });
  assert.equal(JSON.stringify(diagnostic).includes("6600"), false);
  assert.equal(JSON.stringify(diagnostic).includes("含税总价"), false);
}

{
  const element = (textContent) => ({
    nodeType: 1,
    parentElement: null,
    hidden: false,
    isConnected: false,
    ownerDocument: { defaultView: null },
    textContent,
    getAttribute() {
      return null;
    },
  });
  const trip = (
    textContent,
    {
      departureScopeText = null,
      arrivalScopeText = null,
    } = {},
  ) => ({
    textContent,
    querySelectorAll(selector) {
      if (
        selector === ".col-time .sep-lf" &&
        departureScopeText !== null
      ) {
        return [element(departureScopeText)];
      }
      if (
        selector === ".col-time .sep-rt" &&
        arrivalScopeText !== null
      ) {
        return [element(arrivalScopeText)];
      }
      if (selector === ".col-time .sep-lf h2") {
        return [element("20:40")];
      }
      if (selector === ".col-time .sep-rt h2") {
        return [element("23:35")];
      }
      if (selector === ".col-time .sep-lf .airport") {
        return [element("MLE 马累机场")];
      }
      if (selector === ".col-time .sep-rt .airport") {
        return [element("HGH 萧山机场")];
      }
      return [];
    },
  });
  const returnLeg = parserHooks.legFromQunarTrip(
    trip("2026年8月18日 20:40 MLE 马累机场 23:35+1 HGH 萧山机场"),
    "2026-08-18",
    "+05:00",
    "+08:00",
  );
  assert.equal(
    returnLeg.departure_at,
    "2026-08-18T20:40:00+05:00",
  );
  assert.equal(
    returnLeg.arrival_at,
    "2026-08-19T23:35:00+08:00",
  );
  assert.equal(
    parserHooks.legFromQunarTrip(
      trip("2026年8月19日 20:40 MLE 马累机场 23:35+1 HGH 萧山机场"),
      "2026-08-18",
      "+05:00",
      "+08:00",
    ),
    null,
  );
  const arrivalDatedReturnLeg = parserHooks.legFromQunarTrip(
    trip(
      "20:40 MLE 马累机场 23:35+1 HGH 萧山机场 2026年8月19日",
      {
        departureScopeText: "20:40 MLE 马累机场",
        arrivalScopeText:
          "23:35+1 HGH 萧山机场 2026年8月19日",
      },
    ),
    "2026-08-18",
    "+05:00",
    "+08:00",
  );
  assert.equal(
    arrivalDatedReturnLeg.departure_at,
    "2026-08-18T20:40:00+05:00",
  );
  assert.equal(
    arrivalDatedReturnLeg.arrival_at,
    "2026-08-19T23:35:00+08:00",
  );
  assert.equal(
    parserHooks.legFromQunarTrip(
      trip(
        "20:40 MLE 马累机场 23:35+1 HGH 萧山机场 2026年8月20日",
        {
          departureScopeText: "20:40 MLE 马累机场",
          arrivalScopeText:
            "23:35+1 HGH 萧山机场 2026年8月20日",
        },
      ),
      "2026-08-18",
      "+05:00",
      "+08:00",
    ),
    null,
  );
}

{
  const query = {
    origin: "杭州",
    destination: "马累",
    start_date: "2026-08-12",
    end_date: "2026-08-18",
    adults: 2,
    origin_code: "HGH",
    destination_code: "MLE",
  };
  const receipt = {
    schema_version: "tripchord-flight-search-receipt-v1",
    parser_version: parser.PARSER_VERSION,
    provider: "ctrip",
    state: "comparison_price_only",
    confirmed_query: { ...query },
    confirmation_scope: "confirmed_visible_search",
    scan_limit: 20,
    scanned_count: 1,
    candidate_summaries: [
      {
        candidate_index: 0,
        title: "泰国亚航 + 马来西亚亚航",
        route_evidence: "去程 杭州→马累(匹配)；返程 马累→杭州(匹配)",
        schedule_evidence:
          "去程 2026-08-12T18:10:00+08:00→2026-08-13T11:35:00+05:00",
        price_evidence: "往返含税价 ¥5159 起",
        currency: "CNY",
        amount: 5159,
        price_basis: "per_person",
        price_classification: "starting_or_estimated",
      },
    ],
    explicit_empty_evidence: null,
    page_url: "https://flights.ctrip.com/online/list/round-hgh-mle",
    captured_at: "2026-07-31T01:00:00.000Z",
  };
  const digest = await parserHooks.sha256(
    parser.canonicalJson(receipt),
  );
  assert.deepEqual(
    await parser.validateFlightSearchReceipt(
      receipt,
      digest,
      {
        provider: "ctrip",
        page_url: receipt.page_url,
        query,
      },
    ),
    { valid: true, reason: null },
  );
  assert.deepEqual(
    Object.keys(receipt).sort(),
    [
      "candidate_summaries",
      "captured_at",
      "confirmation_scope",
      "confirmed_query",
      "explicit_empty_evidence",
      "page_url",
      "parser_version",
      "provider",
      "scan_limit",
      "scanned_count",
      "schema_version",
      "state",
    ],
  );
  assert.deepEqual(
    Object.keys(receipt.candidate_summaries[0]).sort(),
    [
      "amount",
      "candidate_index",
      "currency",
      "price_basis",
      "price_classification",
      "price_evidence",
      "route_evidence",
      "schedule_evidence",
      "title",
    ],
  );

  for (const field of Object.keys(receipt)) {
    const missing = structuredClone(receipt);
    delete missing[field];
    const missingDigest = await parserHooks.sha256(
      parser.canonicalJson(missing),
    );
    assert.equal(
      (
        await parser.validateFlightSearchReceipt(
          missing,
          missingDigest,
        )
      ).valid,
      false,
      `flight receipt missing ${field} must fail closed`,
    );
  }
  const nonNullEmptyEvidence = structuredClone(receipt);
  nonNullEmptyEvidence.explicit_empty_evidence = {
    code: "visible_empty",
  };
  const nonNullEmptyEvidenceDigest = await parserHooks.sha256(
    parser.canonicalJson(nonNullEmptyEvidence),
  );
  assert.equal(
    (
      await parser.validateFlightSearchReceipt(
        nonNullEmptyEvidence,
        nonNullEmptyEvidenceDigest,
      )
    ).valid,
    false,
    "flight receipt explicit_empty_evidence must be null",
  );

  for (const field of [
    "schema_version",
    "parser_version",
    "provider",
    "state",
    "confirmed_query",
    "confirmation_scope",
    "scan_limit",
    "scanned_count",
    "candidate_summaries",
    "page_url",
    "captured_at",
  ]) {
    const nulled = structuredClone(receipt);
    nulled[field] = null;
    const nulledDigest = await parserHooks.sha256(
      parser.canonicalJson(nulled),
    );
    assert.equal(
      (
        await parser.validateFlightSearchReceipt(
          nulled,
          nulledDigest,
        )
      ).valid,
      false,
      `flight receipt null ${field} must fail closed`,
    );
  }

  const queryMismatch = structuredClone(receipt);
  queryMismatch.confirmed_query.adults = 1;
  const queryMismatchDigest = await parserHooks.sha256(
    parser.canonicalJson(queryMismatch),
  );
  assert.equal(
    (
      await parser.validateFlightSearchReceipt(
        queryMismatch,
        queryMismatchDigest,
        {
          provider: "ctrip",
          page_url: receipt.page_url,
          query,
        },
      )
    ).reason,
    "receipt_query_mismatch",
  );

  const extraCandidateKey = structuredClone(receipt);
  extraCandidateKey.candidate_summaries[0].availability = "available";
  const extraCandidateDigest = await parserHooks.sha256(
    parser.canonicalJson(extraCandidateKey),
  );
  assert.equal(
    (
      await parser.validateFlightSearchReceipt(
        extraCandidateKey,
        extraCandidateDigest,
      )
    ).reason,
    "receipt_shape_invalid",
  );

  const discontinuous = structuredClone(receipt);
  discontinuous.candidate_summaries[0].candidate_index = 1;
  const discontinuousDigest = await parserHooks.sha256(
    parser.canonicalJson(discontinuous),
  );
  assert.equal(
    (
      await parser.validateFlightSearchReceipt(
        discontinuous,
        discontinuousDigest,
      )
    ).reason,
    "candidate_evidence_invalid",
  );

  const bounded = structuredClone(receipt);
  bounded.state = "bounded_no_exact_quote";
  bounded.candidate_summaries[0] = {
    ...bounded.candidate_summaries[0],
    price_evidence: null,
    currency: null,
    amount: null,
    price_basis: "unknown",
    price_classification: "no_visible_price",
  };
  const boundedDigest = await parserHooks.sha256(
    parser.canonicalJson(bounded),
  );
  assert.equal(
    (
      await parser.validateFlightSearchReceipt(
        bounded,
        boundedDigest,
      )
    ).valid,
    true,
  );

  const tampered = structuredClone(receipt);
  tampered.candidate_summaries[0].amount = 1;
  assert.equal(
    (
      await parser.validateFlightSearchReceipt(
        tampered,
        digest,
      )
    ).reason,
    "receipt_sha256_invalid",
  );

  const oldParser = structuredClone(receipt);
  oldParser.parser_version = "tripchord-visible-dom-v2";
  const oldParserDigest = await parserHooks.sha256(
    parser.canonicalJson(oldParser),
  );
  assert.equal(
    (
      await parser.validateFlightSearchReceipt(
        oldParser,
        oldParserDigest,
      )
    ).valid,
    false,
  );

  const offProvider = structuredClone(receipt);
  offProvider.page_url = "https://evil.test/results";
  const offProviderDigest = await parserHooks.sha256(
    parser.canonicalJson(offProvider),
  );
  assert.equal(
    (
      await parser.validateFlightSearchReceipt(
        offProvider,
        offProviderDigest,
      )
    ).valid,
    false,
  );

  const built = await parserHooks.createFlightSearchReceiptFromCandidates({
    provider: "ctrip",
    page_url: receipt.page_url,
    captured_at: receipt.captured_at,
    query,
    driver: {
      triggered: true,
      confirmation_scope: "confirmed_visible_search",
      confirmed_query: { ...query },
      readback_query: { ...query },
    },
    candidate_summaries: structuredClone(receipt.candidate_summaries),
  });
  assert.ok(built);
  assert.equal(built.receipt.explicit_empty_evidence, null);
  assert.equal(
    built.receipt.candidate_summaries[0].price_classification,
    "starting_or_estimated",
  );
  assert.deepEqual(
    await parser.validateFlightSearchReceipt(
      built.receipt,
      built.receipt_sha256,
      {
        provider: "ctrip",
        page_url: receipt.page_url,
        query,
      },
    ),
    { valid: true, reason: null },
  );
  assert.equal(
    parserHooks.flightTerminalFailureCode(built),
    "extraction_error",
  );
  assert.equal(
    parserHooks.flightTerminalFailureCode(null),
    "dom_drift",
  );
}

assert.equal(parser.checkedBaggageKg("无免费托运行李"), 0);
assert.equal(parser.checkedBaggageKg("每位成人免费托运行李 23kg"), 23);
assert.equal(parser.checkedBaggageKg("手提行李 7kg"), null);
assert.equal(parser.checkedBaggageKg("行李额以详情页为准"), null);

assert.equal(parser.breakfastIncluded("含早餐"), true);
assert.equal(parser.breakfastIncluded("2份早餐"), true);
assert.equal(parser.breakfastIncluded("含早晚餐"), true);
assert.equal(parser.breakfastIncluded("不含早餐，可到店加购"), false);
assert.equal(parser.breakfastIncluded("早餐详情以页面为准"), null);
assert.equal(parser.priceBasis("flight", "含税 ¥4,692"), "unknown");
assert.equal(parser.priceBasis("flight", "人均含税价 ¥4,880"), "per_person");
assert.equal(parser.priceBasis("flight", "往返总价 ¥4,858"), "total_party");
assert.equal(parser.priceBasis("lodging", "含税 ¥673"), "unknown");
assert.equal(parser.priceBasis("lodging", "每晚含税 ¥673"), "per_night");
assert.equal(parser.priceBasis("lodging", "全程总价 ¥4,711"), "total_stay");
assert.deepEqual(
  parser.flightPriceContract("人均往返含税价 ¥4,858"),
  {
    valid: true,
    amount: 4858,
    currency: "CNY",
    price_basis: "per_person",
    finality: "exact_candidate",
    evidence: "人均往返含税价 ¥4,858",
  },
);
assert.equal(
  parser.flightPriceFinality("往返含税价 ¥5,159 起"),
  "starting_or_estimated",
);
assert.equal(
  parser.flightPriceFinality("¥6292起往返含税价"),
  "starting_or_estimated",
);
assert.equal(
  parser.flightPriceContract("往返含税价 ¥5,159 起").valid,
  false,
);
assert.equal(
  parser.flightPriceContract("¥6292起往返含税价").valid,
  false,
);
assert.equal(
  parser.flightPriceContract("往返含税价 ¥5,159 起 /人").valid,
  false,
);
assert.deepEqual(
  parser.flightPriceContract("往返含税价 ¥4,692 /人"),
  {
    valid: true,
    amount: 4692,
    currency: "CNY",
    price_basis: "per_person",
    finality: "exact_candidate",
    evidence: "往返含税价 ¥4,692 /人",
  },
);
for (const text of [
  "往返总价 含税 ¥4,858",
  "预估往返价 ¥4,858 /人",
  "参考价 CNY 4,858 /人",
  "人均含税价 4,858",
  "人均含税价 ¥4,858，另有 USD 100",
]) {
  assert.equal(parser.flightPriceContract(text).valid, false, text);
}
assert.equal(
  parser.flightPriceContract("2名成人往返总价 CNY 9,716").price_basis,
  "total_party",
);
for (const text of [
  "含税价 ¥1,171 起",
  "含税价 ¥1,171 起/晚",
  "最低价 CNY 1,171 每晚",
  "from CNY 1,171 per night",
  "starting at USD 171 per night",
  "参考价 ¥1,171 每晚",
  "预估价 ¥1,171 每晚",
]) {
  assert.equal(
    parser.lodgingPriceFinality(text),
    "starting_or_estimated",
    text,
  );
}
assert.equal(
  parser.lodgingPriceFinality("含税及服务费 ¥1,171 每晚"),
  "exact_candidate",
);

const ctripDetailContext = parser.ctripLodgingDetailUrlContext(
  "https://hotels.ctrip.com/hotels/detail/?" +
    "hotelId=6210622&checkIn=2026-08-01&checkOut=2026-08-05&adult=2&crn=1",
  {
    start_date: "2026-08-01",
    end_date: "2026-08-05",
    adults: 2,
    rooms: 1,
  },
);
assert.equal(ctripDetailContext.recognized, true);
assert.equal(ctripDetailContext.property_id, "6210622");
assert.equal(ctripDetailContext.url_query_matches, true);
assert.equal(
  ctripDetailContext.safe_url,
  "https://hotels.ctrip.com/hotels/detail/?" +
    "hotelId=6210622&checkIn=2026-08-01&checkOut=2026-08-05&adult=2&crn=1",
);
assert.equal(
  parser.ctripLodgingDetailUrlContext(
    "https://hotels.ctrip.com/hotels/detail/?" +
      "hotelId=6210622&checkIn=2026-08-02&checkOut=2026-08-05&adult=2&crn=1",
    {
      start_date: "2026-08-01",
      end_date: "2026-08-05",
      adults: 2,
      rooms: 1,
    },
  ).url_query_matches,
  false,
);

const qunarDetailQuery = {
  destination: "Maafushi",
  start_date: "2026-08-21",
  end_date: "2026-08-26",
  adults: 2,
  rooms: 1,
  currency: "CNY",
  options: {
    expected_lodging_place_key: "maafushi",
    expected_package_area: "destination_island",
    segment: "full",
  },
};
const qunarDetailDriver = {
  mode: "captured_read_only_detail",
  provider: "qunar",
  triggered: true,
  confirmation_scope: "confirmed_visible_search",
  confirmed_query: { ...qunarDetailQuery },
  readback_query: { ...qunarDetailQuery, destination: "马富施" },
  result_query_readback_confirmed: true,
  result_query_readback_scope: "qunar_visible_result_form_fields",
  result_query_readback_evidence: {
    provider_destination_id: "i-ka_maafushi",
    result_path: "/city/i-ka_maafushi",
    room_scope: "audited_qunar_single_room_search_surface",
  },
  qunar_detail_capture: {
    source: "qunar_audited_read_only_lodging_detail",
    contract_scope: "audited_qunar_exact_detail_url",
    clicked_booking: false,
    same_controlled_tab: true,
    city_slug: "i-ka_maafushi",
    hotel_seq: "i-ka_maafushi_2112",
    property_id: "2112",
    property_name: "Kaani Palm Beach",
    list_inventory_receipt_sha256: "a".repeat(64),
    inventory_observation_state: "confirmed_empty",
    inventory_observation_count: 2,
    inventory_observation_duration_ms: 2000,
  },
};
const qunarDetailUrl =
  "https://hotel.qunar.com/city/i-ka_maafushi/dt-2112/" +
  "?#fromDate=2026-08-21&toDate=2026-08-26&q=&showMap=0";
const qunarDetailContext = parser.qunarLodgingDetailUrlContext(
  qunarDetailUrl,
  qunarDetailQuery,
  qunarDetailDriver,
);
assert.equal(qunarDetailContext.recognized, true);
assert.equal(qunarDetailContext.safe_url, qunarDetailUrl);
assert.equal(qunarDetailContext.city_slug, "i-ka_maafushi");
assert.equal(qunarDetailContext.hotel_seq, "i-ka_maafushi_2112");
assert.equal(qunarDetailContext.property_id, "2112");
assert.equal(qunarDetailContext.target_matches, true);
assert.equal(qunarDetailContext.lineage_matches, true);
assert.equal(qunarDetailContext.url_query_matches, true);
const qunarPendingDetailDriver = {
  ...qunarDetailDriver,
  qunar_detail_capture: {
    ...qunarDetailDriver.qunar_detail_capture,
    inventory_observation_state: "bounded_provider_pending",
    inventory_observation_count: 1,
    inventory_observation_duration_ms: 28391,
  },
};
assert.equal(
  parser.qunarLodgingDetailUrlContext(
    qunarDetailUrl,
    qunarDetailQuery,
    qunarPendingDetailDriver,
  ).safe_url,
  qunarDetailUrl,
);
for (const mutation of [
  { inventory_observation_state: "confirmed_empty" },
  { inventory_observation_count: 2 },
  { inventory_observation_duration_ms: 24999 },
  { inventory_observation_duration_ms: 120001 },
]) {
  const invalidPendingDriver = {
    ...qunarPendingDetailDriver,
    qunar_detail_capture: {
      ...qunarPendingDetailDriver.qunar_detail_capture,
      ...mutation,
    },
  };
  assert.equal(
    parser.qunarLodgingDetailUrlContext(
      qunarDetailUrl,
      qunarDetailQuery,
      invalidPendingDriver,
    ).safe_url,
    null,
  );
}
for (const [url, driver] of [
  [
    qunarDetailUrl.replace("dt-2112", "dt-9999"),
    qunarDetailDriver,
  ],
  [
    qunarDetailUrl.replace("fromDate=2026-08-21", "fromDate=2026-08-22"),
    qunarDetailDriver,
  ],
  [
    qunarDetailUrl.replace("q=", "q=Kaani"),
    qunarDetailDriver,
  ],
  [
    `${qunarDetailUrl}&tracking=1`,
    qunarDetailDriver,
  ],
  [
    qunarDetailUrl,
    {
      ...qunarDetailDriver,
      qunar_detail_capture: {
        ...qunarDetailDriver.qunar_detail_capture,
        clicked_booking: true,
      },
    },
  ],
  [
    qunarDetailUrl,
    {
      ...qunarDetailDriver,
      result_query_readback_confirmed: false,
    },
  ],
]) {
  assert.equal(
    parser.qunarLodgingDetailUrlContext(
      url,
      qunarDetailQuery,
      driver,
    ).safe_url,
    null,
    url,
  );
}

assert.deepEqual(
  parser.lodgingPlaceEvidence(
    "maafushi",
    "坎迪玛马尔代夫酒店(Kandima Maldives)",
    "Dhaalu Atoll, 康迪马岛, 马尔代夫",
  ),
  {
    expected_key: "maafushi",
    observed_key: "kandima",
    matches_expected: false,
    evidence:
      "坎迪玛马尔代夫酒店(Kandima Maldives) Dhaalu Atoll, 康迪马岛, 马尔代夫",
  },
);
assert.deepEqual(
  parser.lodgingPlaceEvidence(
    "马富施",
    "Maafushi Seaview Hotel",
    "Maafushi · Kaafu Atoll",
  ),
  {
    expected_key: "maafushi",
    observed_key: "maafushi",
    matches_expected: true,
    evidence: "Maafushi Seaview Hotel Maafushi · Kaafu Atoll",
  },
);
assert.deepEqual(
  parser.lodgingPlaceEvidence(
    "胡鲁马累",
    "Hulhumale Beach Hotel",
    "Hulhumale · Maldives",
  ),
  {
    expected_key: "hulhumale",
    observed_key: "hulhumale",
    matches_expected: true,
    evidence: "Hulhumale Beach Hotel Hulhumale · Maldives",
  },
);
assert.equal(parser.canonicalJson({ z: 1, a: { y: 2, x: 1 } }), '{"a":{"x":1,"y":2},"z":1}');

const sanitizedDiagnostic = parser.sanitizeDiagnosticText(
  "往返含税价 ¥4,692；联系 owner@example.com、13912345678；" +
    "会员号 123456789012；详情 https://example.com/private",
);
assert.equal(sanitizedDiagnostic.includes("owner@example.com"), false);
assert.equal(sanitizedDiagnostic.includes("13912345678"), false);
assert.equal(sanitizedDiagnostic.includes("123456789012"), false);
assert.equal(sanitizedDiagnostic.includes("https://example.com/private"), false);
assert.equal(
  parser.sanitizeDiagnosticText("可见候选 ".repeat(100)).length <= 180,
  true,
);
assert.equal(
  parser.safeProviderDetailUrl(
    "fliggy",
    "https://www.fliggy.hk/",
    "https://www.fliggy.hk/",
  ),
  "https://www.fliggy.hk/",
);
assert.equal(
  parser.safeProviderDetailUrl(
    "fliggy",
    "https://hotel.fliggy.hk/hotel_list3.htm",
    "https://www.fliggy.hk/",
  ),
  "https://hotel.fliggy.hk/hotel_list3.htm",
);
for (const outsideUrl of [
  "https://fliggy.hk.evil.example/collect",
  "https://www.taobao.com/travel",
  "https://travel.alibaba.com/",
]) {
  assert.equal(
    parser.safeProviderDetailUrl(
      "fliggy",
      outsideUrl,
      "https://www.fliggy.hk/",
    ),
    null,
  );
}
const exactLodgingQuery = {
  destination: "马累",
  start_date: "2026-08-23",
  end_date: "2026-08-30",
  adults: 2,
  rooms: 1,
};
const exactLodgingDriver = {
  triggered: true,
  confirmation_scope: "confirmed_visible_search",
  confirmed_query: { ...exactLodgingQuery },
};
assert.equal(
  parser.exactLodgingQueryConfirmed(
    exactLodgingQuery,
    exactLodgingDriver,
  ),
  true,
);
assert.equal(
  parser.exactLodgingQueryConfirmed(
    exactLodgingQuery,
    {
      ...exactLodgingDriver,
      confirmation_scope: "provider_url_only_unverified",
    },
  ),
  false,
);
assert.equal(
  parser.exactLodgingQueryConfirmed(
    exactLodgingQuery,
    {
      ...exactLodgingDriver,
      provider: "qunar",
    },
  ),
  false,
);
assert.equal(
  parser.exactLodgingQueryConfirmed(
    exactLodgingQuery,
    {
      ...exactLodgingDriver,
      provider: "qunar",
      result_query_readback_confirmed: true,
    },
  ),
  true,
);
assert.equal(
  parser.exactLodgingQueryConfirmed(
    exactLodgingQuery,
    {
      ...exactLodgingDriver,
      confirmed_query: {
        ...exactLodgingQuery,
        rooms: 2,
      },
    },
  ),
  false,
);
{
  const receiptQuery = {
    ...exactLodgingQuery,
    options: {
      expected_lodging_place_key: "胡鲁马累",
      expected_package_area: "airport_island",
      segment: "hulhumale-full",
    },
  };
  const sourceSummaries = [{
    candidate_index: 99,
    title: "Kaani owner@example.com 13912345678",
    area_evidence: "Maafushi https://private.example/guest",
    room_evidence: "会员号 123456789012",
    price_evidence: "每晚 ￥673 起",
    price_basis: "per_night",
    price_finality: "starting_or_estimated",
  }];
  const built = await parser.createLodgingInventoryReceipt({
    provider: "fliggy",
    query: receiptQuery,
    driver: exactLodgingDriver,
    candidate_summaries: sourceSummaries,
    explicit_empty_evidence: null,
    page_url:
      "https://hotel.fliggy.com/hotel_list3.htm?tracking=private#secret",
    captured_at: "2026-07-30T12:00:00.000Z",
  });
  assert.ok(built);
  assert.equal(built.receipt.scan_limit, 12);
  assert.equal(built.receipt.scanned_count, 1);
  assert.equal(built.receipt.candidate_summaries[0].candidate_index, 0);
  assert.equal(built.receipt.explicit_empty_evidence, null);
  assert.equal(
    built.receipt.page_url,
    "https://hotel.fliggy.com/hotel_list3.htm",
  );
  assert.equal(
    built.receipt.confirmed_query.options.expected_lodging_place_key,
    "hulhumale",
  );
  assert.equal(
    built.receipt.confirmed_query.options.expected_package_area,
    "airport_island",
  );
  assert.equal(
    built.receipt.confirmed_query.options.segment,
    "hulhumale-full",
  );
  assert.equal(
    (
      await parser.validateLodgingInventoryReceipt(
        built.receipt,
        built.receipt_sha256,
      )
    ).valid,
    true,
  );
  const serializedReceipt = JSON.stringify(built.receipt);
  assert.equal(serializedReceipt.includes("owner@example.com"), false);
  assert.equal(serializedReceipt.includes("13912345678"), false);
  assert.equal(serializedReceipt.includes("123456789012"), false);
  assert.equal(serializedReceipt.includes("https://private.example"), false);
  sourceSummaries[0].title = "mutated";
  exactLodgingDriver.confirmed_query.destination = "mutated";
  assert.equal(JSON.stringify(built.receipt).includes("mutated"), false);
  exactLodgingDriver.confirmed_query.destination = "马累";
  assert.equal(
    await parserHooks.sha256(
      parser.canonicalJson({ z: 1, a: { y: 2, x: 1 } }),
    ),
    "b5d361a1c0dc5ed1dab76fcbaa2c270ff891ced6fba0ae3d69a2c72e36a302aa",
  );
  assert.equal(
    await parser.createLodgingInventoryReceipt({
      provider: "fliggy",
      query: exactLodgingQuery,
      driver: null,
      candidate_summaries: sourceSummaries,
      page_url: "https://hotel.fliggy.com/hotel_list3.htm",
      captured_at: "2026-07-30T12:00:00.000Z",
    }),
    null,
  );
  for (const optionName of [
    "expected_lodging_place_key",
    "expected_package_area",
    "segment",
  ]) {
    const missingOptions = { ...receiptQuery.options };
    delete missingOptions[optionName];
    assert.equal(
      await parser.createLodgingInventoryReceipt({
        provider: "fliggy",
        query: { ...receiptQuery, options: missingOptions },
        driver: exactLodgingDriver,
        candidate_summaries: sourceSummaries,
        page_url: "https://hotel.fliggy.com/hotel_list3.htm",
        captured_at: "2026-07-30T12:00:00.000Z",
      }),
      null,
      `missing ${optionName} must not sign a receipt`,
    );
    for (const invalidOptionValue of [null, "  "]) {
      assert.equal(
        await parser.createLodgingInventoryReceipt({
          provider: "fliggy",
          query: {
            ...receiptQuery,
            options: {
              ...receiptQuery.options,
              [optionName]: invalidOptionValue,
            },
          },
          driver: exactLodgingDriver,
          candidate_summaries: sourceSummaries,
          page_url: "https://hotel.fliggy.com/hotel_list3.htm",
          captured_at: "2026-07-30T12:00:00.000Z",
        }),
        null,
        `blank ${optionName} must not sign a receipt`,
      );
    }
  }
  assert.equal(
    await parser.createLodgingInventoryReceipt({
      provider: "fliggy",
      query: exactLodgingQuery,
      driver: exactLodgingDriver,
      candidate_summaries: [{}],
      explicit_empty_evidence: null,
      page_url: "https://hotel.fliggy.com/hotel_list3.htm",
      captured_at: "2026-07-30T12:00:00.000Z",
    }),
    null,
  );
  assert.equal(
    await parser.createLodgingInventoryReceipt({
      provider: "fliggy",
      query: {
        ...receiptQuery,
        options: {
          ...receiptQuery.options,
          segment: "hulhumale-full-typo",
        },
      },
      driver: exactLodgingDriver,
      candidate_summaries: sourceSummaries,
      page_url: "https://hotel.fliggy.com/hotel_list3.htm",
      captured_at: "2026-07-30T12:00:00.000Z",
    }),
    null,
  );
  assert.equal(
    await parser.createLodgingInventoryReceipt({
      provider: "fliggy",
      query: exactLodgingQuery,
      driver: {
        ...exactLodgingDriver,
        confirmation_scope: "provider_url_only_unverified",
      },
      candidate_summaries: sourceSummaries,
      page_url: "https://hotel.fliggy.com/hotel_list3.htm",
      captured_at: "2026-07-30T12:00:00.000Z",
    }),
    null,
  );
  assert.equal(
    await parser.createLodgingInventoryReceipt({
      provider: "fliggy",
      query: exactLodgingQuery,
      driver: exactLodgingDriver,
      parser_version: "tripchord-visible-dom-v2",
      candidate_summaries: sourceSummaries,
      page_url: "https://hotel.fliggy.com/hotel_list3.htm",
      captured_at: "2026-07-30T12:00:00.000Z",
    }),
    null,
  );
  assert.equal(
    await parser.createLodgingInventoryReceipt({
      provider: "fliggy",
      query: exactLodgingQuery,
      driver: exactLodgingDriver,
      candidate_summaries: [],
      explicit_empty_evidence: null,
      page_url: "https://hotel.fliggy.com/hotel_list3.htm",
      captured_at: "2026-07-30T12:00:00.000Z",
    }),
    null,
  );
  assert.equal(
    await parser.createLodgingInventoryReceipt({
      provider: "fliggy",
      query: exactLodgingQuery,
      driver: exactLodgingDriver,
      candidate_summaries: [],
      explicit_empty_evidence: {
        code: "unaudited_empty",
        text_summary: "暂无酒店",
      },
      page_url: "https://hotel.fliggy.com/hotel_list3.htm",
      captured_at: "2026-07-30T12:00:00.000Z",
    }),
    null,
  );
  const exactQunarLodgingDriver = {
    ...exactLodgingDriver,
    provider: "qunar",
    result_query_readback_confirmed: true,
  };
  assert.equal(
    await parser.createLodgingInventoryReceipt({
      provider: "qunar",
      query: receiptQuery,
      driver: { ...exactLodgingDriver, provider: "qunar" },
      candidate_summaries: [],
      explicit_empty_evidence: {
        contract_version: "qunar-visible-zero-inventory-v1",
        result_count_text: "共 0 家酒店满足条件",
        empty_message: "很抱歉，没有找到相关的酒店",
      },
      page_url: "https://hotel.qunar.com/city/i-ka_maafushi/",
      captured_at: "2026-07-30T12:00:00.000Z",
    }),
    null,
  );
  const confirmedEmpty = await parser.createLodgingInventoryReceipt({
    provider: "qunar",
    query: receiptQuery,
    driver: exactQunarLodgingDriver,
    candidate_summaries: [],
    explicit_empty_evidence: {
      contract_version: "qunar-visible-zero-inventory-v1",
      result_count_text: "共 0 家酒店满足条件",
      empty_message: "很抱歉，没有找到相关的酒店",
    },
    page_url:
      "https://hotel.qunar.com/city/i-ka_maafushi/?tracking=private#secret",
    captured_at: "2026-07-30T12:00:00.000Z",
  });
  assert.ok(confirmedEmpty);
  assert.equal(confirmedEmpty.receipt.state, "confirmed_empty");
  assert.equal(confirmedEmpty.receipt.scanned_count, 0);
  assert.deepEqual(confirmedEmpty.receipt.candidate_summaries, []);
  assert.equal(
    confirmedEmpty.receipt.page_url,
    "https://hotel.qunar.com/city/i-ka_maafushi/",
  );
  assert.equal(
    (
      await parser.validateLodgingInventoryReceipt(
        confirmedEmpty.receipt,
        confirmedEmpty.receipt_sha256,
      )
    ).valid,
    true,
  );
  const boundedPending = await parser.createLodgingInventoryReceipt({
    provider: "qunar",
    query: receiptQuery,
    driver: exactQunarLodgingDriver,
    candidate_summaries: [],
    provider_pending_evidence: {
      contract_version: "qunar-visible-search-pending-v1",
      result_count_text: "共 家酒店满足条件",
      pending_message: "请稍等,您查询的结果正在实时搜索中...",
      observed_duration_ms: 28000,
    },
    page_url: "https://hotel.qunar.com/city/i-ka_maafushi/#private",
    captured_at: "2026-07-30T12:00:00.000Z",
  });
  assert.ok(boundedPending);
  assert.equal(boundedPending.receipt.state, "bounded_provider_pending");
  assert.equal(boundedPending.receipt.scanned_count, 0);
  assert.equal(boundedPending.receipt.explicit_empty_evidence, null);
  assert.equal(
    (
      await parser.validateLodgingInventoryReceipt(
        boundedPending.receipt,
        boundedPending.receipt_sha256,
      )
    ).valid,
    true,
  );
  const boundedPendingDetails =
    await parserHooks.boundedLodgingInventoryDetails(
      "qunar",
      [],
      receiptQuery,
      exactQunarLodgingDriver,
      "https://hotel.qunar.com/city/i-ka_maafushi/#private",
      "2026-07-30T12:00:00.000Z",
      null,
      {
        contract_version: "qunar-visible-search-pending-v1",
        result_count_text: "共 家酒店满足条件",
        pending_message: "请稍等,您查询的结果正在实时搜索中...",
        observed_duration_ms: 28000,
      },
    );
  assert.equal(
    boundedPendingDetails.inventory_result_state,
    "bounded_provider_pending",
  );
  assert.equal(boundedPendingDetails.confirmed_exhaustive, false);
  assert.equal(
    boundedPendingDetails.capture_code,
    "audited_qunar_bounded_realtime_search_pending",
  );
  const tooShortPending = JSON.parse(JSON.stringify(boundedPending.receipt));
  tooShortPending.provider_pending_evidence.observed_duration_ms = 24000;
  const tooShortPendingSha = await parserHooks.sha256(
    parser.canonicalJson(tooShortPending),
  );
  assert.equal(
    (
      await parser.validateLodgingInventoryReceipt(
        tooShortPending,
        tooShortPendingSha,
      )
    ).valid,
    false,
  );
  const tamperedEmpty = JSON.parse(JSON.stringify(confirmedEmpty.receipt));
  tamperedEmpty.explicit_empty_evidence.empty_message = "暂无酒店";
  const tamperedEmptySha = await parserHooks.sha256(
    parser.canonicalJson(tamperedEmpty),
  );
  assert.equal(
    (
      await parser.validateLodgingInventoryReceipt(
        tamperedEmpty,
        tamperedEmptySha,
      )
    ).reason,
    "empty_receipt_without_evidence",
  );
  assert.equal(
    (
      await parser.validateLodgingInventoryReceipt(
        built.receipt,
        "0".repeat(64),
      )
    ).reason,
    "receipt_sha256_mismatch",
  );
  assert.equal(
    (
      await parser.validateLodgingInventoryReceipt(
        { ...built.receipt, parser_version: "tripchord-visible-dom-v2" },
        built.receipt_sha256,
      )
    ).reason,
    "parser_version_mismatch",
  );
  const unknownSegmentReceipt = JSON.parse(JSON.stringify(built.receipt));
  unknownSegmentReceipt.confirmed_query.options.segment =
    "hulhumale-full-typo";
  const unknownSegmentSha = await parserHooks.sha256(
    parser.canonicalJson(unknownSegmentReceipt),
  );
  assert.equal(
    (
      await parser.validateLodgingInventoryReceipt(
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
      const invalidOptionSha = await parserHooks.sha256(
        parser.canonicalJson(invalidOptionReceipt),
      );
      assert.equal(
        (
          await parser.validateLodgingInventoryReceipt(
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

const alternateOrigin = parser.flightRouteObservation(
  "上海 - 马累 8月23日 ¥6984 票面 + ¥1154 税费",
  {
    origin: "杭州",
    destination: "马累",
    origin_code: "HGH",
    destination_code: "MLE",
  },
);
assert.equal(alternateOrigin.origin_label, "上海");
assert.equal(alternateOrigin.destination_label, "马累");
assert.equal(alternateOrigin.observed_origin_code, null);
assert.equal(alternateOrigin.origin_matches_requested, false);
assert.equal(alternateOrigin.destination_matches_requested, true);

const outboundRoute = parser.flightLegRouteEvidence(
  "2026年8月23日 08:30 杭州 HGH — 18:35 马累 MLE",
  {
    origin: "杭州",
    destination: "马累",
    origin_code: "HGH",
    destination_code: "MLE",
  },
  "outbound",
  "selected_outbound_summary",
);
assert.equal(outboundRoute.matches_expected, true);
assert.equal(outboundRoute.expected_departure_code, "HGH");
assert.equal(outboundRoute.expected_arrival_code, "MLE");
assert.equal(
  parser.flightLegRouteEvidence(
    "2026年8月23日 08:30 上海 PVG — 18:35 马累 MLE",
    {
      origin: "杭州",
      destination: "马累",
      origin_code: "HGH",
      destination_code: "MLE",
    },
    "outbound",
    "selected_outbound_summary",
  ).matches_expected,
  false,
);
assert.equal(
  parser.flightLegRouteEvidence(
    "2026年8月30日 10:45 马累 MLE — 次日 09:10 北京 PEK",
    {
      origin: "杭州",
      destination: "马累",
      origin_code: "HGH",
      destination_code: "MLE",
    },
    "return",
    "return_card",
  ).matches_expected,
  false,
);

const splitPrice = parser.atomicPriceStructure([
  "人均含税价",
  "¥",
  "4",
  "880",
]);
assert.equal(splitPrice.safe_amount_fragment, null);
assert.equal(splitPrice.complete_currency_amount_fragment_count, 0);
assert.equal(splitPrice.split_numeric_sequence_count > 0, true);
assert.deepEqual(
  splitPrice.fragment_shapes,
  ["basis_label", "currency", "digits", "digits"],
);
const atomicPrice = parser.atomicPriceStructure([
  "人均含税价",
  "¥4,880",
]);
assert.equal(atomicPrice.safe_amount_fragment, "¥4,880");

{
  const ownerDocument = { defaultView: null };
  const textNode = { nodeType: 3, textContent: "含税/费后 ¥1,087" };
  const priceNode = {
    nodeType: 1,
    parentElement: null,
    ownerDocument,
    childNodes: [textNode],
    textContent: textNode.textContent,
    hidden: false,
    getAttribute() {
      return null;
    },
    querySelectorAll() {
      return [];
    },
    contains() {
      return false;
    },
  };
  const row = {
    querySelectorAll() {
      return [priceNode];
    },
  };
  assert.deepEqual(
    parser.ctripAtomicTaxPriceCandidates(row),
    [],
  );
  assert.deepEqual(
    parser.ctripAtomicTaxPriceCandidates(row, {
      allowSingleNightTotal: true,
    }).map(({ node, ...item }) => item),
    [{
      evidence: "含税/费后 ¥1,087",
      amount: 1087,
      currency: "CNY",
      price_basis: "total_stay",
      price_basis_source: "audited_exact_single_night_tax_total",
    }],
  );
}

assert.deepEqual(
  parser.packageAreaEvidence(
    "胡鲁马累",
    {
      destination: "马累",
      options: { expected_package_area: "destination_island" },
    },
    {
      triggered: true,
      confirmed_query: { destination: "马累" },
      confirmation_scope: "confirmed_visible_search",
    },
  ),
  {
    area: "airport_island",
    source: "visible_label",
    matches_expected: false,
  },
);

assert.deepEqual(
  parser.packageAreaEvidence(
    "South Ari Atoll",
    {
      destination: "South Ari Atoll",
      options: { expected_package_area: "destination_island" },
    },
    {
      triggered: true,
      confirmed_query: { destination: "South Ari Atoll" },
      confirmation_scope: "confirmed_visible_search",
    },
  ),
  {
    area: "destination_island",
    source: "confirmed_exact_search_area",
    matches_expected: true,
  },
);

assert.deepEqual(
  parser.packageAreaEvidence(
    "位置以酒店最终确认为准",
    {
      destination: "马累",
      options: { expected_package_area: "destination_island" },
    },
    {
      triggered: true,
      confirmed_query: { destination: "马累" },
      confirmation_scope: "confirmed_visible_search",
    },
  ),
  { area: null, source: null, matches_expected: null },
);

{
  const makeQunarDetailRoot = ({
    propertyName = "Kaani Palm Beach",
    areaText = "Maafushi, Kaafu Atoll, Maldives",
    stayText = "2026-08-21 至 2026-08-26 · 5晚",
    occupancyText = "1间房 / 2成人 / 0儿童",
    roomText = "Deluxe Double Room",
    rateText =
      "Deluxe Double Room 含税及服务费 最终价 CNY 888 每晚 含早餐 免费取消 预订",
  } = {}) => {
    const ownerDocument = { defaultView: null };
    const node = (textContent, tagName = "DIV", attributes = {}) => ({
      nodeType: 1,
      tagName,
      parentElement: null,
      ownerDocument,
      isConnected: false,
      hidden: false,
      disabled: false,
      textContent,
      innerText: textContent,
      childNodes: [{ nodeType: 3, textContent }],
      getAttribute(name) {
        return Object.hasOwn(attributes, name) ? attributes[name] : null;
      },
      querySelectorAll() {
        return [];
      },
      contains(other) {
        return other === this;
      },
    });
    const property = node(propertyName, "H1");
    const area = node(areaText);
    const stay = node(stayText);
    const occupancy = node(occupancyText);
    const room = node(roomText, "H3");
    const book = node("预订", "BUTTON");
    const rate = node(rateText, "SECTION");
    room.parentElement = rate;
    book.parentElement = rate;
    rate.querySelectorAll = (selector) => {
      if (selector === "a, button, [role='button']") return [book];
      if (selector === "[data-tripchord-fixture='room-title']") {
        return roomText ? [room] : [];
      }
      return [];
    };
    rate.contains = (other) => other === rate || other === room || other === book;
    const body = node(
      `${propertyName} ${areaText} ${stayText} ${occupancyText} ${rateText}`,
      "BODY",
    );
    const documentElement = node("", "HTML");
    ownerDocument.body = body;
    ownerDocument.documentElement = documentElement;
    const root = {
      title: "",
      body,
      documentElement,
      querySelectorAll(selector) {
        if (selector === "[data-tripchord-fixture='property-title']") {
          return [property];
        }
        if (selector === "[data-tripchord-fixture='property-address']") {
          return [area];
        }
        if (selector === "body *") return [area];
        if (selector === "[data-tripchord-fixture='stay-readback']") {
          return [stay];
        }
        if (selector === "[data-tripchord-fixture='occupancy-readback']") {
          return [occupancy];
        }
        if (selector === "[data-tripchord-fixture='rate-row']") {
          return [rate];
        }
        if (selector === "a, button, [role='button']") return [book];
        return [];
      },
    };
    return root;
  };

  const exact = await parser.extractQunarLodgingDetailPage(
    makeQunarDetailRoot(),
    qunarDetailUrl,
    "2026-08-04T12:00:00.000Z",
    qunarDetailQuery,
    qunarDetailDriver,
  );
  assert.equal(exact.state, "succeeded");
  assert.equal(exact.quotes.length, 1);
  assert.equal(exact.quotes[0].amount, 888);
  assert.equal(exact.quotes[0].currency, "CNY");
  assert.equal(exact.quotes[0].price_basis, "per_night");
  assert.equal(exact.quotes[0].taxes_included, true);
  assert.equal(exact.quotes[0].details.hotel_seq, "i-ka_maafushi_2112");
  assert.equal(exact.quotes[0].details.room_text, "Deluxe Double Room");
  assert.match(exact.quotes[0].details.rate_text, /最终价 CNY 888 每晚/);
  assert.equal(exact.quotes[0].details.kaafu_area_confirmed, true);
  assert.equal(exact.quotes[0].details.clicked_booking, false);
  assert.match(exact.quotes[0].evidence_sha256, /^[a-f0-9]{64}$/);

  for (const [root, expectedGate] of [
    [
      makeQunarDetailRoot({ areaText: "Hulhumale, Maldives" }),
      "kaafu_atoll_visible",
    ],
    [
      makeQunarDetailRoot({ occupancyText: "2间房 / 2成人 / 0儿童" }),
      "visible_occupancy_readback",
    ],
    [
      makeQunarDetailRoot({ propertyName: "Unrelated Beach Hotel" }),
      "property_name_exact_visible",
    ],
  ]) {
    const rejected = await parser.extractQunarLodgingDetailPage(
      root,
      qunarDetailUrl,
      "2026-08-04T12:00:00.000Z",
      qunarDetailQuery,
      qunarDetailDriver,
    );
    assert.equal(rejected.state, "failed");
    assert.equal(rejected.quotes.length, 0);
    assert.equal(rejected.failure.details.gates[expectedGate], false);
  }
  const starting = await parser.extractQunarLodgingDetailPage(
    makeQunarDetailRoot({
      rateText:
        "Deluxe Double Room 含税价 CNY 888 起/晚 预订",
    }),
    qunarDetailUrl,
    "2026-08-04T12:00:00.000Z",
    qunarDetailQuery,
    qunarDetailDriver,
  );
  assert.equal(starting.state, "failed");
  assert.equal(starting.quotes.length, 0);
  assert.equal(starting.failure.details.room_rate_contract, false);
  assert.equal(
    starting.failure.details.dom_diagnostics.scope,
    "qunar_lodging_rate_candidates_only",
  );
  assert.equal(
    starting.failure.details.rate_diagnostics.scope,
    "qunar_lodging_rate_candidates_only",
  );

  const diagnosticNode = (tagName, ownText = "", attributes = {}) => {
    const value = {
      nodeType: 1,
      tagName,
      parentElement: null,
      ownerDocument: { defaultView: null },
      isConnected: false,
      hidden: false,
      disabled: false,
      ownText,
      textContent: ownText,
      innerText: ownText,
      childNodes: ownText ? [{ nodeType: 3, textContent: ownText }] : [],
      children: [],
      descendants: [],
      getAttribute(name) {
        return Object.hasOwn(attributes, name) ? attributes[name] : null;
      },
      querySelectorAll() {
        return this.descendants;
      },
      contains(other) {
        return other === this || this.descendants.includes(other);
      },
    };
    return value;
  };
  const attachDiagnosticChildren = (parent, children) => {
    parent.children = children;
    parent.descendants = [];
    for (const child of children) {
      child.parentElement = parent;
      parent.descendants.push(child, ...child.descendants);
    }
    parent.textContent = [parent.ownText, ...children.map(
      (child) => child.textContent,
    )].filter(Boolean).join(" ");
    parent.innerText = parent.textContent;
  };
  const privateNickname = diagnosticNode(
    "SPAN",
    "海风旅客私密昵称",
    { class: "profile-nickname" },
  );
  const privateBalance = diagnosticNode(
    "SPAN",
    "账户余额 CNY 99888",
    { class: "wallet-balance" },
  );
  const privateHeader = diagnosticNode(
    "HEADER",
    "",
    { class: "account-header" },
  );
  attachDiagnosticChildren(privateHeader, [
    privateNickname,
    privateBalance,
  ]);
  const scopedPrice = diagnosticNode(
    "SPAN",
    "含税价 CNY 888 起/晚",
    { class: "room-price" },
  );
  const scopedAction = diagnosticNode("BUTTON", "预订");
  const scopedRate = diagnosticNode(
    "SECTION",
    "",
    { class: "room-rate-panel" },
  );
  attachDiagnosticChildren(scopedRate, [scopedPrice, scopedAction]);
  const trustedMain = diagnosticNode("MAIN");
  attachDiagnosticChildren(trustedMain, [privateHeader, scopedRate]);
  const scopedRoot = {
    querySelectorAll(selector) {
      return selector === "main" ? [trustedMain] : [];
    },
  };
  const scopedDiagnostics = {
    dom: parserHooks.qunarLodgingDetailDomDiagnostics(scopedRoot, []),
    rate: parserHooks.qunarRateDiagnostics(scopedRoot, []),
  };
  const scopedDiagnosticsJson = JSON.stringify(scopedDiagnostics);
  assert.equal(
    scopedDiagnostics.dom.scope,
    "qunar_lodging_detail_main_content_only",
  );
  assert.equal(
    scopedDiagnostics.rate.scope,
    "qunar_lodging_detail_main_content_only",
  );
  for (const privateValue of [
    "海风旅客私密昵称",
    "账户余额",
    "99888",
    "profile-nickname",
    "wallet-balance",
    "account-header",
  ]) {
    assert.equal(scopedDiagnosticsJson.includes(privateValue), false);
  }
  assert.equal(
    scopedDiagnostics.rate.visible_currency_amount_samples.length,
    1,
  );

  const untrustedSelectors = [];
  const untrustedRoot = {
    querySelectorAll(selector) {
      untrustedSelectors.push(selector);
      return selector === "body *" ? [privateBalance] : [];
    },
  };
  const untrustedDomDiagnostics =
    parserHooks.qunarLodgingDetailDomDiagnostics(untrustedRoot, []);
  const untrustedRateDiagnostics =
    parserHooks.qunarRateDiagnostics(untrustedRoot, []);
  assert.equal(
    untrustedDomDiagnostics.scope,
    "qunar_lodging_detail_scope_unavailable_fail_closed",
  );
  assert.equal(untrustedDomDiagnostics.scanned_node_count, 0);
  assert.deepEqual(untrustedDomDiagnostics.candidates, []);
  assert.equal(
    untrustedRateDiagnostics.scope,
    "qunar_lodging_detail_scope_unavailable_fail_closed",
  );
  assert.equal(untrustedRateDiagnostics.scanned_node_count, 0);
  assert.deepEqual(untrustedRateDiagnostics.visible_currency_amount_samples, []);
  assert.equal(untrustedSelectors.includes("body *"), false);
}

const transferContract = parser.transferContractsFromEvidence(
  "往返接送：马累机场 ↔ 胡鲁马累；24小时服务（UTC+05:00）；单程20分钟；" +
    "需提前预约；含税总价 CNY 108（2名成人）",
  [],
  {
    start_date: "2026-08-23",
    end_date: "2026-08-30",
  },
  "https://hotels.ctrip.com/hotels/detail/terminal-27",
);
assert.equal(transferContract.length, 2);
assert.deepEqual(
  transferContract.map((item) => [item.origin_area, item.destination_area]),
  [
    ["airport", "airport_island"],
    ["airport_island", "airport"],
  ],
);
assert.deepEqual(
  transferContract.map((item) => item.service_date),
  ["2026-08-23", "2026-08-30"],
);
assert.equal(transferContract[0].schedule_mode, "service_window");
assert.equal(transferContract[0].duration_minutes, 20);
assert.equal(transferContract[0].amount, 108);
assert.equal(transferContract[0].taxes_included, true);
assert.equal(transferContract[0].price_scope, "round_trip");
assert.equal(transferContract[0].purchase_scope, "hotel_bound");
assert.equal(
  parser.transferPurchaseScope("公共接驳可单独预订，含税每人 CNY 50"),
  "public_independent",
);

const directionUnknown = parser.transferContractsFromEvidence(
  "提供机场接送；24小时服务（UTC+05:00）；单程20分钟；含税总价 CNY 108",
  [],
  { start_date: "2026-08-23", end_date: "2026-08-30" },
  "https://hotels.ctrip.com/hotels/detail/unknown",
);
assert.equal(directionUnknown[0].origin_area, null);
assert.equal(directionUnknown[0].destination_area, null);

assert.equal(
  parser.safeProviderDetailUrl(
    "ctrip",
    "https://hotels.ctrip.com/hotels/detail/terminal-27",
    "https://hotels.ctrip.com/list",
  ),
  "https://hotels.ctrip.com/hotels/detail/terminal-27",
);
assert.equal(
  parser.safeProviderDetailUrl(
    "ctrip",
    "https://hotels.ctrip.com/order/create",
    "https://hotels.ctrip.com/list",
  ),
  null,
);
for (const unsafeDetailUrl of [
  "https://hotels.ctrip.com/hotels/detail/?" +
    "hotelId=6210622&checkIn=2026-08-01&checkOut=payment&adult=2&crn=1",
  "https://hotels.ctrip.com/hotels/detail/?" +
    "hotelId=not-numeric&checkIn=2026-08-01&checkOut=2026-08-05",
  "https://hotels.ctrip.com/hotels/detail/?" +
    "hotelId=6210622&checkIn=2026-08-05&checkOut=2026-08-01",
  "https://hotels.ctrip.com/hotels/detail/?" +
    "hotelId=6210622&checkIn=2026-02-28&checkOut=2026-02-30",
  "https://hotels.ctrip.com/hotels/detail/?" +
    "hotelId=6210622&checkIn=2026-08-01&checkOut=2026-08-05" +
    "&checkOut=2026-08-06",
  "https://hotels.ctrip.com/hotels/detail/?" +
    "hotelId=6210622&checkIn=2026-08-01&checkOut=2026-08-05&order=create",
  "https://hotels.ctrip.com/hotels/detail/?" +
    "hotelId=6210622&checkIn=2026-08-01&checkOut=2026-08-05&payment=card",
  "https://hotels.ctrip.com/hotels/detail/?" +
    "hotelId=6210622&checkIn=2026-08-01&checkOut=2026-08-05&coupon=apply",
  "https://hotels.ctrip.com/hotels/detail/?" +
    "hotelId=6210622&checkIn=2026-08-01&checkOut=2026-08-05&cashier=open",
  "https://hotels.ctrip.com/checkout/?" +
    "hotelId=6210622&checkIn=2026-08-01&checkOut=2026-08-05",
]) {
  assert.equal(
    parser.safeProviderDetailUrl(
      "ctrip",
      unsafeDetailUrl,
      "https://hotels.ctrip.com/list",
    ),
    null,
    unsafeDetailUrl,
  );
}

const exactTransfer = parser.transferContractsFromEvidence(
  "单程 马富施岛 → 胡鲁马累；单程45分钟；需提前预约；" +
    "含税每人 CNY 50",
  [
    "2026-08-29T16:00:00+05:00",
    "2026-08-29T16:45:00+05:00",
  ],
  { start_date: "2026-08-29", end_date: "2026-08-30" },
  "https://hotels.ctrip.com/hotels/detail/exact",
);
assert.equal(exactTransfer.length, 1);
assert.equal(exactTransfer[0].schedule_mode, "exact_departure");
assert.equal(exactTransfer[0].service_date, "2026-08-29");
assert.equal(exactTransfer[0].price_basis, "per_person");
assert.equal(exactTransfer[0].operates_24_hours, false);

console.log("parser contract: strict detail assertions passed");
