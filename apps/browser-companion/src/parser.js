(() => {
  if (globalThis.TripChordQuoteParser) {
    return;
  }

  const PARSER_VERSION = "tripchord-visible-dom-v3";
  const MAX_VISIBLE_EVIDENCE_CHARS = 100000;
  const MAX_DOM_DIAGNOSTIC_CANDIDATES = 6;
  const MAX_DOM_DIAGNOSTIC_CLASS_CHARS = 120;
  const MAX_DOM_DIAGNOSTIC_TEXT_CHARS = 180;
  const MAX_DOM_DIAGNOSTIC_ANCHORS = 80;
  const MAX_DOM_DIAGNOSTIC_SCAN_NODES = 3000;
  const MAX_VISIBLE_NODE_SCAN_NODES = 3000;
  const MAX_LODGING_INVENTORY_CANDIDATES = 12;
  const LODGING_INVENTORY_RECEIPT_SCHEMA_VERSION =
    "tripchord-lodging-inventory-receipt-v1";
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
  const SAFE_LODGING_SEGMENTS = new Set([
    "full",
    "first",
    "middle",
    "last",
    "hulhumale-full",
  ]);
  const SAFE_PACKAGE_AREAS = new Set(["airport_island", "destination_island"]);
  const CTRIP_LODGING_DETAIL_ROOM_SELECTORS = [
    "[data-tripchord-fixture='room-group']",
    "[class*='commonRoomCard__']",
  ];
  const CTRIP_LODGING_DETAIL_RATE_SELECTORS = [
    "[data-tripchord-fixture='rate-row']",
    "[class*='saleRoomItemBox__']",
  ];
  const CTRIP_LODGING_DETAIL_ROOM_TITLE_SELECTORS = [
    "[data-tripchord-fixture='room-title']",
    "[class*='commonRoomCard-roomName__']",
    "[class*='commonRoomCard-title__']",
    "[class*='roomName__']",
    "[class*='room-name']",
    "h2",
    "h3",
    "h4",
  ];
  const CTRIP_LODGING_DETAIL_PROPERTY_TITLE_SELECTORS = [
    "[data-tripchord-fixture='property-title']",
    "h1[class*='hotelName']",
    "h1[class*='name']",
    "h1",
  ];
  const CTRIP_LODGING_DETAIL_AREA_SELECTORS = [
    "[data-tripchord-fixture='property-address']",
    "[class*='hotelAddress']",
    "[class*='hotel-address']",
    "[class*='address']",
    "[class*='location']",
  ];
  const CTRIP_LODGING_DETAIL_TAX_PRICE_SELECTORS = [
    "[data-tripchord-fixture='tax-inclusive-price']",
    "[class*='tax']",
    "[class*='Tax']",
    "[class*='price']",
    "[class*='Price']",
    "span",
    "strong",
    "em",
    "b",
    "p",
    "div",
  ];
  const FLIGGY_LODGING_DETAIL_PROPERTY_TITLE_SELECTORS = [
    "[data-tripchord-fixture='property-title']",
    ".hotel-name",
    "[class*='hotelName']",
    "[class*='hotel-name']",
    "h1",
    "h2",
  ];
  const FLIGGY_LODGING_DETAIL_AREA_SELECTORS = [
    "[data-tripchord-fixture='property-address']",
    "[class*='address']",
    "[class*='Address']",
    "[class*='location']",
    "[class*='position']",
  ];
  const FLIGGY_LODGING_DETAIL_PRICE_SELECTORS = [
    "[data-tripchord-fixture='tax-inclusive-price']",
    "[class*='price']",
    "[class*='Price']",
    "span",
    "strong",
    "em",
    "b",
    "p",
    "div",
  ];
  const QUNAR_AUDITED_LODGING_DETAILS = Object.freeze({
    "2112": Object.freeze({
      city_slug: "i-ka_maafushi",
      hotel_seq: "i-ka_maafushi_2112",
      property_name: "Kaani Palm Beach",
    }),
    "2055": Object.freeze({
      city_slug: "i-ka_maafushi",
      hotel_seq: "i-ka_maafushi_2055",
      property_name: "Kaani Grand Seaview",
    }),
    "2071": Object.freeze({
      city_slug: "i-ka_maafushi",
      hotel_seq: "i-ka_maafushi_2071",
      property_name: "Maafushi View",
    }),
    "2072": Object.freeze({
      city_slug: "i-ka_maafushi",
      hotel_seq: "i-ka_maafushi_2072",
      property_name: "Maafushi Village",
    }),
    "2075": Object.freeze({
      city_slug: "i-ka_maafushi",
      hotel_seq: "i-ka_maafushi_2075",
      property_name: "Maafushi Veli",
    }),
    "2142": Object.freeze({
      city_slug: "i-ka_maafushi",
      hotel_seq: "i-ka_maafushi_2142",
      property_name: "SEASUNBEACH",
    }),
  });
  const QUNAR_LODGING_DETAIL_RATE_SELECTORS = [
    "[data-tripchord-fixture='rate-row']",
  ];
  const QUNAR_LODGING_DETAIL_MAIN_SCOPE_SELECTORS = [
    "[data-tripchord-fixture='qunar-detail-main']",
    "main",
    "#hotel-detail",
    "#hotelDetail",
    "[data-page='hotel-detail']",
    "[data-testid='hotel-detail-main']",
    "[class~='hotel-detail-main']",
    "[class~='hotelDetailMain']",
    "[class~='hotel-detail']",
    "[class~='hotelDetail']",
  ];
  const QUNAR_LODGING_DETAIL_RATE_SCOPE_SELECTORS = [
    ...QUNAR_LODGING_DETAIL_RATE_SELECTORS,
    "[data-testid='room-rate-row']",
    "[data-testid='room-rate-list']",
    "[class~='room-rate-row']",
    "[class~='roomRateRow']",
    "[class~='rate-row']",
    "[class~='rateRow']",
    "[class~='room-list']",
    "[class~='roomList']",
  ];
  const QUNAR_DIAGNOSTIC_PRIVATE_REGION_PATTERN =
    /(?:account|profile|member|login|avatar|wallet|balance|header|navigation|navbar|(?:^|[-_\s])nav(?:$|[-_\s])|user[-_]?(?:center|info|profile|menu))/i;
  const QUNAR_FINAL_PRICE_MARKER_PATTERN =
    /(?:最终价|含税(?:总)?价|税费已含|含税及服务费|实时预订价格|final\s+price|tax(?:es)?\s+included)/i;
  const CTRIP_LODGING_PLACE_ALIASES = Object.freeze({
    maafushi: Object.freeze(["马富施", "马富士", "maafushi"]),
    hulhumale: Object.freeze(["胡鲁马累", "hulhumale", "hulhumalé"]),
    kandima: Object.freeze(["康迪马", "坎迪玛", "kandima"]),
  });
  const CTRIP_TAX_INCLUDED_PRICE_PATTERN =
    /含税\s*(?:[/／]\s*)?费后|含税费后|含税及服务费后|tax(?:es)?\s+(?:and\s+fees\s+)?included/i;
  const CTRIP_PER_NIGHT_AVERAGE_PATTERN =
    /(?:^|[\s/／，,])均\s*(?=(?:¥|￥|CNY|RMB|USD|\$))|每晚|[/／]\s*晚|average\s+per\s+night/i;
  const PRICE_ANCHOR_PATTERN =
    /(?:¥|￥|CNY|RMB|USD|\$)\s*[0-9][0-9,]*(?:\.[0-9]{1,2})?/i;
  const ACTION_ANCHOR_PATTERN =
    /选为去程|选择去程|(?:^选择$)|查看详情|查看航班|选择航班|立即预订|预订|book|select/i;
  const CTRIP_FLIGHT_SELECTION_PATTERN =
    /选为去程|选择去程|(?:^选择$)/;
  const CTRIP_STYLED_OUTBOUND_SELECTION_PATTERN =
    /^(?:选为去程|选择去程)$/;
  const CTRIP_FLIGHT_ROUND_TRIP_PRICE_PATTERN = /往返含税价/;
  const UNSAFE_OUTBOUND_TRANSACTION_PATTERN =
    /(?:立即)?预订|订票|下单|支付|购买|优惠券|订单|确认购买|\b(?:book(?:ing)?|order|pay(?:ment)?|checkout)\b/i;
  const SAFE_OUTBOUND_ACTIONS = new Set([
    "search",
    "filter",
    "select_outbound",
    "reselect_outbound",
    "provider_auto_selected_outbound",
    "select_return",
    "expand_flight_detail",
  ]);
  const FLIGHT_TIMEZONE_OFFSETS = Object.freeze({
    HGH: "+08:00",
    MLE: "+05:00",
    SIN: "+08:00",
    DXB: "+04:00",
    PEK: "+08:00",
    PKX: "+08:00",
    PVG: "+08:00",
    KMG: "+08:00",
    CAN: "+08:00",
  });
  const AUDITED_FLIGHT_CITY_ALIASES = Object.freeze({
    HGH: Object.freeze([
      "杭州",
      "杭州萧山",
      "杭州萧山国际机场",
      "萧山",
      "hangzhou",
      "hgh",
    ]),
    MLE: Object.freeze([
      "马累",
      "维拉纳",
      "维拉纳国际机场",
      "韦拉纳",
      "韦拉纳国际机场",
      "male",
      "mle",
    ]),
  });
  const MAX_RETURN_COMBINATIONS = 3;
  const MAX_OUTBOUND_SELECTION_CANDIDATES = 3;
  const FLIGHT_SEARCH_RECEIPT_SCHEMA_VERSION =
    "tripchord-flight-search-receipt-v1";
  const MAX_FLIGHT_SEARCH_RECEIPT_CANDIDATES = 20;
  const FLIGHT_SEARCH_RECEIPT_KEYS = Object.freeze([
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
  ]);
  const FLIGHT_SEARCH_CONFIRMED_QUERY_KEYS = Object.freeze([
    "adults",
    "destination",
    "destination_code",
    "end_date",
    "origin",
    "origin_code",
    "start_date",
  ]);
  const FLIGHT_SEARCH_CANDIDATE_KEYS = Object.freeze([
    "amount",
    "candidate_index",
    "destination_airport_code",
    "currency",
    "outbound_flight_numbers",
    "outbound_segments",
    "price_basis",
    "price_classification",
    "price_evidence",
    "return_flight_numbers",
    "return_segments",
    "origin_airport_code",
    "route_evidence",
    "schedule_evidence",
    "title",
  ]);
  const DIAGNOSTIC_CONTAINER_TAGS = new Set([
    "article",
    "div",
    "li",
    "section",
    "tr",
  ]);
  const DIAGNOSTIC_BOUNDARY_TAGS = new Set([
    "body",
    "footer",
    "form",
    "header",
    "html",
    "main",
    "nav",
  ]);
  const PROFILES = {
    ctrip: {
      flight: {
        cards: [
          "[data-testid*='flight-card']",
          "[class*='flight-item']",
          "[class*='flightListItem']",
          ".flight-list-item",
          "[data-tripchord-fixture='quote']",
        ],
        title: [
          "[data-testid*='airline']",
          "[class*='airline-name']",
          "[class*='flight-name']",
          "[data-tripchord-fixture='title']",
        ],
      },
      lodging: {
        cards: [
          ".list-item > .hotel-card",
          ".hotel-card",
          "[data-testid*='hotel-card']",
          "[class*='hotel-list-item']",
          "[class*='hotelItem']",
          "[data-tripchord-fixture='quote']",
        ],
        title: [
          ".hotelName",
          "[data-testid*='hotel-name']",
          "[class*='hotel-name']",
          "[class*='name']",
          "[data-tripchord-fixture='title']",
        ],
      },
    },
    fliggy: {
      flight: {
        cards: [
          "[data-testid*='flight-card']",
          "[class*='flight-item']",
          "[class*='flightItem']",
          "[data-tripchord-fixture='quote']",
        ],
        title: [
          "[class*='airline']",
          "[class*='flight-name']",
          "[data-tripchord-fixture='title']",
        ],
      },
      lodging: {
        cards: [
          "[data-testid*='hotel-card']",
          "[class*='hotel-item']",
          "[class*='hotelItem']",
          "[data-tripchord-fixture='quote']",
        ],
        title: [
          "[class*='hotel-name']",
          "[class*='hotelName']",
          "[data-tripchord-fixture='title']",
        ],
      },
    },
    qunar: {
      flight: {
        cards: [
          "[data-testid*='flight-card']",
          ".b-airfly",
          "[class*='flight-item']",
          "[class*='flightItem']",
          "[data-tripchord-fixture='quote']",
        ],
        title: [
          ".air",
          "[class*='airline']",
          "[class*='flight-name']",
          "[data-tripchord-fixture='title']",
        ],
      },
      lodging: {
        cards: [
          "[data-testid*='hotel-card']",
          "[class*='hotel-item']",
          "[class*='hotelItem']",
          "[data-tripchord-fixture='quote']",
        ],
        title: [
          "[class*='hotel-name']",
          "[class*='hotelName']",
          "[data-tripchord-fixture='title']",
        ],
      },
    },
    tongcheng: {
      flight: {
        cards: [
          ".flight-item",
          "[class*='flight-item']",
          "[class*='flightItem']",
          "[class*='flight-list'] > li",
          "[data-tripchord-fixture='quote']",
        ],
        title: [
          ".airways-title",
          "[class*='airline']",
          "[class*='flight-name']",
          "[data-tripchord-fixture='title']",
        ],
      },
      lodging: {
        cards: [
          "[class*='hotel-item']",
          "[class*='hotelItem']",
          "[class*='hotel-card']",
          "[class*='room-item']",
          "[class*='roomItem']",
          "[class*='room-card']",
          "[class*='roomCard']",
          "[data-tripchord-fixture='quote']",
        ],
        title: [
          "[class*='hotel-name']",
          "[class*='hotelName']",
          "[class*='room-name']",
          "[class*='roomName']",
          "[data-tripchord-fixture='title']",
        ],
      },
    },
  };
  const PRICE_SELECTORS = [
    ".room-price .price-line",
    "[data-testid*='price']",
    "[class*='price']",
    "[class*='Price']",
    "[data-tripchord-fixture='price']",
  ];
  const TERMS_SELECTORS = [
    "[data-testid*='tag']",
    "[class*='tag']",
    "[class*='policy']",
    "[class*='baggage']",
    "[class*='meal']",
    "[data-tripchord-fixture='terms']",
  ];
  const FLIGHT_DETAIL_SELECTORS = {
    carrier: [
      "[data-testid*='airline']",
      "[class*='airline']",
      "[class*='carrier']",
      "[data-tripchord-fixture='carrier']",
    ],
    connection: [
      "[data-testid*='transfer']",
      "[class*='transfer']",
      "[class*='stop']",
      "[data-tripchord-fixture='connection']",
    ],
    baggage: [
      "[data-testid*='baggage']",
      "[class*='baggage']",
      "[data-tripchord-fixture='baggage']",
    ],
  };
  const LODGING_DETAIL_SELECTORS = {
    room: [
      ".room-name",
      "[data-testid*='room']",
      "[class*='room-name']",
      "[class*='roomName']",
      "[data-tripchord-fixture='room']",
    ],
    area: [
      ".hotel-position .position-desc",
      ".hotel-position",
      "[data-testid*='area']",
      "[class*='location']",
      "[class*='area']",
      "[data-tripchord-fixture='area']",
    ],
  };
  const TRANSFER_CONTRACT_SELECTORS = [
    "[data-tripchord-fixture='transfer-contract']",
    "[data-testid*='transfer']",
    "[data-testid*='shuttle']",
    "[class*='transfer-detail']",
    "[class*='transferInfo']",
    "[class*='shuttle']",
    "[class*='transport']",
  ];
  const DETAIL_LINK_SELECTORS = [
    "a[data-tripchord-fixture='detail-link']",
    "a[data-testid*='detail']",
    "a[class*='detail']",
    "a[href]",
  ];
  const PROVIDER_HOST_SUFFIXES = {
    ctrip: ["ctrip.com"],
    fliggy: ["fliggy.com", "fliggy.hk"],
    qunar: ["qunar.com"],
    tongcheng: ["ly.com", "elong.com"],
  };
  const CAPTCHA_ACTIONABLE_PATTERNS = [
    "请完成安全验证",
    "访问过于频繁",
    "请输入验证码",
    "拖动滑块",
    "拖动下方滑块完成验证",
    "请按住滑块，拖动到最右边",
    "通过验证以确保正常访问",
  ];
  const CAPTCHA_CONTEXT_PATTERNS = ["人机验证", "安全验证"];
  const CAPTCHA_CONTROL_SELECTORS = [
    "iframe[src*='captcha' i]",
    "iframe[src*='verify' i]",
    "input[name*='captcha' i]",
    "input[id*='captcha' i]",
    "input[placeholder*='验证码']",
    "[class*='captcha' i]",
    "[id*='captcha' i]",
    "[class*='nc_scale']",
    "[class*='nc_wrapper']",
    "[class*='slider']",
  ];
  const LOGIN_PATTERNS = [
    "请先登录后查看",
    "登录后查看价格",
    "登录后查询",
    "当前登录已失效",
    "请重新登录",
    "账号可能存在风险",
    "为了您的账号安全请验证通过后使用",
  ];
  const LODGING_PRICE_LOGIN_PATTERN =
    /登录(?:后|以)?(?:查看|查询)?(?:会员价|价格|报价)|请先登录/i;
  const NON_FINAL_LODGING_PRICE_PATTERN =
    /(?:起价|最低价|参考价|预估价|(?:^|[\s\d,.¥￥$])起(?=$|[\s/，,。.;；])|\b(?:from|starting\s+(?:at|from)|estimated(?:\s+price)?|reference\s+price|lowest\s+price)\b)/i;
  const NON_FINAL_FLIGHT_PRICE_PATTERN =
    /(?:起价|最低价|参考价|预估(?:价|往返价)?|估算价|(?:^|[\s\d,.¥￥$])起(?=$|[\s/，,。.;；]|往返含税价|含税总价)|\b(?:from|starting\s+(?:at|from)|estimated(?:\s+price)?|reference\s+price|lowest\s+price)\b)/i;
  const NEGATIVE_TAX_PATTERN =
    /未含税|不含税|税费另付|另付税|tax(?:es)?\s+(?:not\s+included|excluded)|excludes?\s+tax(?:es)?/i;
  const POSITIVE_TAX_PATTERN =
    /含税|税费已含|含税及服务费|税费全包|tax(?:es)?\s+included|all\s+tax(?:es)?/i;
  const FLIGHT_UNAVAILABLE_PATTERN =
    /售罄|无票|不可预订|无法预订|已下架|sold\s*out|unavailable|not\s+available/i;
  const FLIGHT_AVAILABLE_CONTROL_PATTERN =
    /^(?:选为返程|选择返程|选择航班|查看航班|预订|立即预订|订票(?:\s*剩\s*\d+\s*张)?|book|select)$/i;
  const CTRIP_FLIGHT_RETURN_CONTROL_PATTERN =
    /^订票(?:\s*剩\s*\d+\s*张)?$/;

  function cleanText(value) {
    return String(value || "").replace(/\s+/g, " ").trim();
  }

  function shortTextHash(value) {
    let hash = 2166136261;
    for (const character of String(value || "")) {
      hash ^= character.codePointAt(0);
      hash = Math.imul(hash, 16777619);
    }
    return (hash >>> 0).toString(16).padStart(8, "0");
  }

  function boundedText(value, maxLength = 240) {
    const text = cleanText(value);
    return text.length > maxLength ? `${text.slice(0, maxLength)}…` : text;
  }

  function comparableLodgingAlias(value) {
    return cleanText(value)
      .normalize("NFKD")
      .replace(/\p{M}/gu, "")
      .toLowerCase()
      .replace(/[·•\-_/（）()，,。.]/g, "")
      .replace(/\s+/g, "")
      .replace(/(?:岛|island)$/i, "");
  }

  function canonicalLodgingPlaceKey(value) {
    const comparable = comparableLodgingAlias(value);
    if (!comparable) {
      return null;
    }
    for (const [placeKey, aliases] of Object.entries(
      CTRIP_LODGING_PLACE_ALIASES,
    )) {
      if (
        comparable === comparableLodgingAlias(placeKey) ||
        aliases.some(
          (alias) => comparable === comparableLodgingAlias(alias),
        )
      ) {
        return placeKey;
      }
    }
    return null;
  }

  function domScanBudgetExceeded(scope, scannedNodes) {
    const error = new Error("visible DOM scan budget exhausted");
    error.tripchordParserCode = "dom_scan_budget_exhausted";
    error.tripchordParserDetails = {
      scope,
      scanned_nodes: scannedNodes,
      max_scan_nodes: MAX_VISIBLE_NODE_SCAN_NODES,
    };
    return error;
  }

  function visibleEvidence(node) {
    for (
      let element = node;
      element && element.nodeType === 1;
      element = element.parentElement
    ) {
      if (
        element.hidden ||
        element.getAttribute("aria-hidden") === "true" ||
        /(?:^|;)\s*(?:display\s*:\s*none|visibility\s*:\s*hidden)/i.test(
          element.getAttribute("style") || "",
        )
      ) {
        return false;
      }
    }
    const view = node.ownerDocument && node.ownerDocument.defaultView;
    if (view && node.isConnected) {
      const style = view.getComputedStyle(node);
      const rect = node.getBoundingClientRect();
      return (
        style.display !== "none" &&
        style.visibility !== "hidden" &&
        rect.width > 0 &&
        rect.height > 0
      );
    }
    return true;
  }

  function firstText(root, selectors) {
    for (const selector of selectors) {
      for (const node of root.querySelectorAll(selector)) {
        const value = cleanText(
          visibleEvidence(node) ? node.textContent : "",
        );
        if (value) {
          return value;
        }
      }
    }
    return "";
  }

  function allText(root, selectors) {
    const values = [];
    for (const selector of selectors) {
      for (const node of root.querySelectorAll(selector)) {
        if (!visibleEvidence(node)) {
          continue;
        }
        const value = cleanText(node.textContent);
        if (value && !values.includes(value)) {
          values.push(value);
        }
      }
    }
    return values.slice(0, 12);
  }

  function matchingVisibleNodes(
    root,
    selector,
    pattern,
    maxTextChars = 120,
    maxMatches = MAX_DOM_DIAGNOSTIC_ANCHORS,
  ) {
    const matches = [];
    let scanned = 0;
    for (const node of root.querySelectorAll(selector)) {
      scanned += 1;
      if (
        scanned > MAX_DOM_DIAGNOSTIC_SCAN_NODES ||
        matches.length >= maxMatches
      ) {
        break;
      }
      const text = cleanText(node.textContent);
      pattern.lastIndex = 0;
      const matchesPattern =
        text &&
        text.length <= maxTextChars &&
        pattern.test(text);
      pattern.lastIndex = 0;
      if (!matchesPattern || !visibleEvidence(node)) {
        continue;
      }
      matches.push(node);
    }
    return matches;
  }

  function visiblePriceAnchors(root) {
    return matchingVisibleNodes(
      root,
      `${PRICE_SELECTORS.join(",")}, span, strong, em, b`,
      PRICE_ANCHOR_PATTERN,
      120,
    );
  }

  function visibleActionAnchors(root, pattern = ACTION_ANCHOR_PATTERN) {
    return matchingVisibleNodes(
      root,
      [
        ".flight-btn",
        "button",
        "a",
        "[role='button']",
        "[onclick]",
        "[class*='btn']",
        "[class*='button']",
        "[class*='choose']",
        "[class*='select']",
        "span",
      ].join(","),
      pattern,
      80,
    );
  }

  function visibleRoundTripPriceLabels(root) {
    return matchingVisibleNodes(
      root,
      "span, strong, em, b, p, div",
      CTRIP_FLIGHT_ROUND_TRIP_PRICE_PATTERN,
      120,
      20,
    );
  }

  function ctripFlightCarrierText(root) {
    const selected = firstText(root, FLIGHT_DETAIL_SELECTORS.carrier);
    if (selected) {
      return selected;
    }
    const semantic = matchingVisibleNodes(
      root,
      "span, strong, p, div",
      /航空|航司|airlines?|airways?/i,
      100,
      1,
    );
    return semantic.length ? cleanText(semantic[0].textContent) : "";
  }

  function addMinimalContainer(containers, candidate) {
    for (let index = 0; index < containers.length; index += 1) {
      const current = containers[index];
      if (current === candidate || candidate.contains(current)) {
        return;
      }
      if (current.contains(candidate)) {
        containers[index] = candidate;
        return;
      }
    }
    containers.push(candidate);
  }

  function ctripFlightSemanticCards(root) {
    const selections = visibleActionAnchors(
      root,
      CTRIP_FLIGHT_SELECTION_PATTERN,
    );
    const cards = [];
    for (const selection of selections) {
      let candidate = selection.parentElement;
      let depth = 0;
      while (candidate && depth < 9) {
        const tag = cleanText(candidate.tagName).toLowerCase();
        if (
          candidate === root.body ||
          candidate === root.documentElement ||
          DIAGNOSTIC_BOUNDARY_TAGS.has(tag)
        ) {
          break;
        }
        if (
          DIAGNOSTIC_CONTAINER_TAGS.has(tag) &&
          visibleEvidence(candidate)
        ) {
          const text = cleanText(candidate.textContent);
          const priceAnchors = visiblePriceAnchors(candidate);
          const selectionAnchors = visibleActionAnchors(
            candidate,
            CTRIP_FLIGHT_SELECTION_PATTERN,
          );
          if (
            text.length <= 5000 &&
            priceAnchors.length &&
            selectionAnchors.length &&
            visibleRoundTripPriceLabels(candidate).length &&
            ctripFlightCarrierText(candidate)
          ) {
            addMinimalContainer(cards, candidate);
            break;
          }
        }
        candidate = candidate.parentElement;
        depth += 1;
      }
    }
    return cards.slice(0, 30);
  }

  function visibleFlightCurrencyAmountCount(value) {
    return [
      ...cleanText(value).matchAll(
        /(?:¥|￥|CNY|RMB|USD|\$)\s*(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d{1,2})?/gi,
      ),
    ].length;
  }

  function ctripFlightPriceEvidence(root) {
    const candidates = [];
    for (const node of root.querySelectorAll(
      [
        "[class*='flight-operate']",
        "[class*='flightOperate']",
        "[class*='price']",
        "[class*='Price']",
        "[data-tripchord-fixture='price']",
        "span",
        "strong",
        "div",
      ].join(","),
    )) {
      const text = cleanText(node.textContent);
      if (
        !text ||
        text.length > 240 ||
        !CTRIP_FLIGHT_ROUND_TRIP_PRICE_PATTERN.test(text) ||
        visibleFlightCurrencyAmountCount(text) !== 1
      ) {
        continue;
      }
      if (!visibleEvidence(node)) {
        continue;
      }
      candidates.push(text);
    }
    candidates.sort((left, right) => left.length - right.length);
    return candidates[0] || null;
  }

  function sanitizeDiagnosticText(value) {
    return cleanText(value)
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
      .slice(0, MAX_DOM_DIAGNOSTIC_TEXT_CHARS);
  }

  function diagnosticClassName(node) {
    return sanitizeDiagnosticText(node.getAttribute("class") || "")
      .slice(0, MAX_DOM_DIAGNOSTIC_CLASS_CHARS);
  }

  function visibleDiagnosticText(node) {
    const parts = [];
    const seen = new Set();
    const textNodes = [
      node,
      ...node.querySelectorAll(
        "h1, h2, h3, h4, p, span, strong, em, b, small, button, a, time",
      ),
    ];
    for (const item of textNodes) {
      if (
        parts.join(" ").length >= MAX_DOM_DIAGNOSTIC_TEXT_CHARS ||
        !visibleEvidence(item) ||
        (
          item.children &&
          item.children.length &&
          !/^(?:A|BUTTON)$/.test(item.tagName || "")
        )
      ) {
        continue;
      }
      const value = sanitizeDiagnosticText(item.textContent);
      if (value && !seen.has(value)) {
        seen.add(value);
        parts.push(value);
      }
    }
    return sanitizeDiagnosticText(parts.join(" "));
  }

  function nearestDiagnosticContainer(
    anchor,
    root,
    priceAnchors,
    actionAnchors,
  ) {
    let candidate = anchor.parentElement;
    let fallback = null;
    let depth = 0;
    while (candidate && depth < 8) {
      const tag = cleanText(candidate.tagName).toLowerCase();
      if (
        candidate === root.body ||
        candidate === root.documentElement ||
        DIAGNOSTIC_BOUNDARY_TAGS.has(tag)
      ) {
        break;
      }
      if (
        DIAGNOSTIC_CONTAINER_TAGS.has(tag) &&
        visibleEvidence(candidate)
      ) {
        const text = cleanText(candidate.textContent);
        if (text && text.length <= 5000) {
          if (!fallback) {
            fallback = candidate;
          }
          const priceHits = priceAnchors.filter((item) =>
            candidate.contains(item)
          ).length;
          const actionHits = actionAnchors.filter((item) =>
            candidate.contains(item)
          ).length;
          if (priceHits && actionHits) {
            return candidate;
          }
          if (["article", "li", "section", "tr"].includes(tag)) {
            fallback = candidate;
          }
        }
      }
      candidate = candidate.parentElement;
      depth += 1;
    }
    return fallback;
  }

  function domDriftDiagnostics(root) {
    const priceAnchors = visiblePriceAnchors(root);
    const actionAnchors = visibleActionAnchors(root);
    const containers = [];
    for (const anchor of [...priceAnchors, ...actionAnchors]) {
      const container = nearestDiagnosticContainer(
        anchor,
        root,
        priceAnchors,
        actionAnchors,
      );
      if (container) {
        addMinimalContainer(containers, container);
      }
    }
    const diagnosticPriceNodes = (container) =>
      [...container.querySelectorAll("*")]
        .filter((node) => visibleEvidence(node))
        .map((node) => {
          const text = cleanText(node.innerText || node.textContent);
          const className = diagnosticClassName(node);
          return {
            node,
            text,
            className,
          };
        })
        .filter(({ text, className }) =>
          /(?:price|money|amount|num|room|tax)/i.test(className) ||
          /(?:¥|￥|含税|到店另付|每晚|每间|总价)/.test(text)
        )
        .slice(0, 12)
        .map(({ node, text, className }) => ({
          tag: cleanText(node.tagName).toLowerCase(),
          class: className,
          text_summary: sanitizeDiagnosticText(text).slice(0, 160),
          aria_label:
            sanitizeDiagnosticText(node.getAttribute("aria-label")).slice(0, 120) || null,
          title:
            sanitizeDiagnosticText(node.getAttribute("title")).slice(0, 120) || null,
          data_price:
            sanitizeDiagnosticText(
              node.getAttribute("data-price") || node.getAttribute("data-value"),
            ).slice(0, 80) || null,
          inline_style:
            sanitizeDiagnosticText(node.getAttribute("style")).slice(0, 160) || null,
        }));
    const candidates = containers
      .slice(0, MAX_DOM_DIAGNOSTIC_CANDIDATES)
      .map((container) => ({
        tag: cleanText(container.tagName).toLowerCase(),
        class: diagnosticClassName(container),
        text_summary: visibleDiagnosticText(container),
        price_anchor_hits: Math.min(
          MAX_DOM_DIAGNOSTIC_ANCHORS,
          priceAnchors.filter((item) => container.contains(item)).length,
        ),
        action_anchor_hits: Math.min(
          MAX_DOM_DIAGNOSTIC_ANCHORS,
          actionAnchors.filter((item) => container.contains(item)).length,
        ),
        price_node_diagnostics: diagnosticPriceNodes(container),
      }));
    const lodgingUnitEvidence = [...root.querySelectorAll("body *")]
      .filter((node) => visibleEvidence(node))
      .map((node) => ({
        node,
        text: directVisibleNodeText(node),
      }))
      .filter(({ text }) =>
        text && /(?:每晚|每间|\/晚|晚均|均价|总价|合计|全程)/.test(text)
      )
      .slice(0, 12)
      .map(({ node, text }) => ({
        tag: cleanText(node.tagName).toLowerCase(),
        class: diagnosticClassName(node),
        text_summary: sanitizeDiagnosticText(text).slice(0, 180),
      }));
    const resultStateEvidence = [...root.querySelectorAll("body *")]
      .slice(0, MAX_VISIBLE_NODE_SCAN_NODES)
      .filter((node) => visibleEvidence(node))
      .map((node) => ({ node, text: directVisibleNodeText(node) }))
      .filter(({ text }) =>
        text &&
        text.length <= 240 &&
        /(?:共\s*\d+\s*家酒店|没有找到|暂无|无符合|酒店满足条件|加载|搜索中|请稍候|换个条件|筛选)/.test(text)
      )
      .slice(0, 20)
      .map(({ node, text }) => ({
        tag: cleanText(node.tagName).toLowerCase(),
        class: diagnosticClassName(node),
        text_summary: sanitizeDiagnosticText(text).slice(0, 240),
      }));
    return {
      scope: "visible_candidate_cards_only",
      max_candidates: MAX_DOM_DIAGNOSTIC_CANDIDATES,
      candidates,
      lodging_unit_evidence: lodgingUnitEvidence,
      result_state_evidence: resultStateEvidence,
      truncated: containers.length > candidates.length,
    };
  }

  function parseAmount(text) {
    const normalized = cleanText(text).replace(/,/g, "");
    const currencyMatch = normalized.match(
      /(?:¥|￥|CNY|RMB|USD|\$)\s*(\d+(?:\.\d{1,2})?)/i,
    );
    if (currencyMatch) {
      return Number(currencyMatch[1]);
    }
    const matches = [...normalized.matchAll(/(\d+(?:\.\d{1,2})?)/g)];
    if (!matches.length) {
      return null;
    }
    const value = Number(matches[0][1]);
    return Number.isFinite(value) && value >= 0 ? value : null;
  }

  function priceBasis(kind, text) {
    const value = cleanText(text);
    if (/人均|每人|\/人|起\/人|per\s+(?:person|adult)/i.test(value)) {
      return "per_person";
    }
    if (/总价|合计|全程|total(?:\s+(?:price|stay|trip|party))?/i.test(value)) {
      return kind === "flight" ? "total_party" : "total_stay";
    }
    if (/每晚|\/晚|起\/晚|per\s+night/i.test(value)) {
      return "per_night";
    }
    return "unknown";
  }

  function lodgingPriceFinality(text) {
    const value = cleanText(text);
    if (!value) {
      return "unknown";
    }
    return NON_FINAL_LODGING_PRICE_PATTERN.test(value)
      ? "starting_or_estimated"
      : "exact_candidate";
  }

  function flightPriceFinality(text) {
    const value = cleanText(text);
    if (!value) {
      return "unknown";
    }
    return NON_FINAL_FLIGHT_PRICE_PATTERN.test(value)
      ? "starting_or_estimated"
      : "exact_candidate";
  }

  function flightPriceContract(text) {
    const value = cleanText(text);
    const amountMatches = [
      ...value.matchAll(
        /(?:¥|￥|CNY|RMB|USD|\$)\s*(\d{1,3}(?:,\d{3})+|\d+)(?:\.(\d{1,2}))?/gi,
      ),
    ];
    const basis = priceBasis("flight", value);
    const finality = flightPriceFinality(value);
    const explicitPartyTotal =
      /(?:(?:全部|全体|所有|订单|旅客|乘客|\d+\s*(?:名|位)?成人|\d+\s*人)[^¥￥$]{0,18}(?:总价|合计)|(?:总价|合计)[^¥￥$]{0,18}(?:全部|全体|所有|订单|旅客|乘客|\d+\s*(?:名|位)?成人|\d+\s*人))/i.test(
        value,
      );
    if (
      finality !== "exact_candidate" ||
      amountMatches.length !== 1 ||
      !["per_person", "total_party"].includes(basis) ||
      (basis === "total_party" && !explicitPartyTotal)
    ) {
      return {
        valid: false,
        amount: null,
        currency: null,
        price_basis: "unknown",
        finality,
        evidence: value || null,
      };
    }
    const match = amountMatches[0];
    const amount = Number(
      `${match[1].replace(/,/g, "")}${match[2] ? `.${match[2]}` : ""}`,
    );
    const currencyToken = cleanText(match[0]).match(
      /^(¥|￥|CNY|RMB|USD|\$)/i,
    );
    const currency =
      currencyToken && /^(?:USD|\$)$/i.test(currencyToken[1])
        ? "USD"
        : currencyToken
          ? "CNY"
          : null;
    return {
      valid:
        Number.isFinite(amount) &&
        amount > 0 &&
        Boolean(currency),
      amount:
        Number.isFinite(amount) && amount > 0 ? amount : null,
      currency,
      price_basis: basis,
      finality,
      evidence: value || null,
    };
  }

  function taxesIncluded(text) {
    const value = cleanText(text);
    if (NEGATIVE_TAX_PATTERN.test(value)) {
      return false;
    }
    if (POSITIVE_TAX_PATTERN.test(value)) {
      return true;
    }
    return null;
  }

  function pageGate(root) {
    const body = cleanText(
      root.body && (root.body.innerText || root.body.textContent),
    ).slice(0, 40000);
    const actionableMatch = CAPTCHA_ACTIONABLE_PATTERNS.find((pattern) =>
      body.includes(pattern),
    );
    let visibleControl = null;
    if (!actionableMatch && root && typeof root.querySelectorAll === "function") {
      for (const selector of CAPTCHA_CONTROL_SELECTORS) {
        const node = [...root.querySelectorAll(selector)].find(visibleEvidence);
        if (node) {
          visibleControl = selector;
          break;
        }
      }
    }
    const contextualMatch = CAPTCHA_CONTEXT_PATTERNS.find((pattern) =>
      body.includes(pattern),
    );
    if (actionableMatch || (contextualMatch && visibleControl)) {
      return {
        state: "blocked",
        code: "captcha_required",
        message: "平台要求用户完成验证码或安全验证",
        retryable: false,
        details: {
          detector_version: "visible-actionable-captcha-v2",
          evidence_kind: actionableMatch
            ? "actionable_copy"
            : "context_copy_with_visible_control",
          matched_text: actionableMatch || contextualMatch,
          visible_control_selector: visibleControl,
        },
      };
    }
    const loginMatch = LOGIN_PATTERNS.find((pattern) => body.includes(pattern));
    if (loginMatch) {
      return {
        state: "blocked",
        code: "login_required",
        message: /账号.*风险|账号安全.*验证/.test(loginMatch)
          ? "平台要求用户本人完成账号安全验证"
          : "当前 Chrome 标签页需要用户登录",
        retryable: false,
        details: {
          detector_version: "visible-login-gate-v2",
          matched_text: loginMatch,
          human_action_required: true,
        },
      };
    }
    return null;
  }

  async function sha256(value) {
    const bytes = new TextEncoder().encode(value);
    const digest = await crypto.subtle.digest("SHA-256", bytes);
    return [...new Uint8Array(digest)]
      .map((byte) => byte.toString(16).padStart(2, "0"))
      .join("");
  }

  function canonicalJson(value) {
    if (value === null || typeof value !== "object") {
      return JSON.stringify(value);
    }
    if (Array.isArray(value)) {
      return `[${value.map((item) => canonicalJson(item)).join(",")}]`;
    }
    return `{${Object.keys(value)
      .sort()
      .filter((key) => value[key] !== undefined)
      .map((key) => `${JSON.stringify(key)}:${canonicalJson(value[key])}`)
      .join(",")}}`;
  }

  function firstMatching(values, pattern) {
    return values.find((value) => pattern.test(value)) || null;
  }

  function safeOptions(options) {
    const value =
      options && typeof options === "object" && !Array.isArray(options)
        ? options
        : {};
    const safe = {};
    if (SAFE_LODGING_SEGMENTS.has(value.segment)) {
      safe.segment = value.segment;
    }
    if (SAFE_PACKAGE_AREAS.has(value.expected_package_area)) {
      safe.expected_package_area = value.expected_package_area;
    }
    const expectedPlaceKey = canonicalLodgingPlaceKey(
      value.expected_lodging_place_key,
    );
    if (expectedPlaceKey) {
      safe.expected_lodging_place_key = expectedPlaceKey;
    }
    return safe;
  }

  function safeQuery(query) {
    const value = query || {};
    return {
      origin: value.origin || null,
      destination: value.destination || null,
      start_date: value.start_date || null,
      end_date: value.end_date || null,
      adults: Number.isInteger(value.adults) ? value.adults : null,
      children: Number.isInteger(value.children) ? value.children : null,
      children_ages: Array.isArray(value.children_ages)
        ? value.children_ages.filter((age) => Number.isInteger(age))
        : [],
      infants: Number.isInteger(value.infants) ? value.infants : null,
      party_shape_supported:
        typeof value.party_shape_supported === "boolean"
          ? value.party_shape_supported
          : null,
      party_shape_failure:
        typeof value.party_shape_failure === "string"
          ? value.party_shape_failure
          : null,
      rooms: Number.isInteger(value.rooms) ? value.rooms : null,
      currency: value.currency || null,
      origin_code: value.origin_code || null,
      destination_code: value.destination_code || null,
      search_url: value.search_url || null,
      options: safeOptions(value.options),
    };
  }

  function checkedBaggageKg(text) {
    const value = cleanText(text);
    if (!value) {
      return null;
    }
    if (
      /(?:无|不含|未含|没有)(?:免费)?托运行李/.test(value) ||
      /托运行李(?:额)?[^0-9]{0,8}0\s*(?:kg|公斤|千克)/i.test(value) ||
      /(?:no|without)\s+(?:free\s+)?checked baggage/i.test(value) ||
      /checked baggage[^.;,]{0,20}(?:not included|none|0\s*kg)/i.test(value)
    ) {
      return 0;
    }
    const patterns = [
      /(?:免费)?托运行李(?:额)?[^0-9]{0,16}(\d{1,3})\s*(?:kg|公斤|千克)/i,
      /(\d{1,3})\s*(?:kg|公斤|千克)[^。；;,]{0,16}(?:免费)?托运行李/i,
      /checked baggage[^0-9]{0,16}(\d{1,3})\s*kg/i,
      /(\d{1,3})\s*kg[^.;,]{0,16}checked baggage/i,
    ];
    for (const pattern of patterns) {
      const match = value.match(pattern);
      if (!match) {
        continue;
      }
      const kilograms = Number(match[1]);
      if (Number.isInteger(kilograms) && kilograms > 0 && kilograms <= 100) {
        return kilograms;
      }
    }
    return null;
  }

  function breakfastIncluded(text) {
    const value = cleanText(text);
    if (!value) {
      return null;
    }
    if (
      /不含(?:早|早餐)|未含(?:早|早餐)|无早|不提供早餐|早餐不含/.test(value) ||
      /without breakfast|breakfast (?:is )?not included|room only/i.test(value)
    ) {
      return false;
    }
    if (
      /含(?:\d+份)?早|含早餐|\d+\s*份早餐|早餐已含|包早餐|含早晚餐/.test(value) ||
      /with breakfast|breakfast included/i.test(value)
    ) {
      return true;
    }
    return null;
  }

  function comparablePlace(value) {
    return cleanText(value)
      .toLowerCase()
      .replace(/[·•\-_/（）()，,。.]/g, "")
      .replace(/\s+/g, "")
      .replace(/(?:岛|island)$/i, "");
  }

  function sameVisiblePlace(left, right) {
    const first = comparablePlace(left);
    const second = comparablePlace(right);
    if (!first || !second) {
      return false;
    }
    return (
      first === second ||
      (Math.min(first.length, second.length) >= 4 &&
        (first.includes(second) || second.includes(first)))
    );
  }

  function exactLodgingQueryConfirmed(query, driver) {
    const normalizedQuery = safeQuery(query);
    const confirmed =
      driver &&
      driver.confirmed_query &&
      typeof driver.confirmed_query === "object" &&
      !Array.isArray(driver.confirmed_query)
        ? driver.confirmed_query
        : null;
    const confirmationScope = cleanText(
      driver && driver.confirmation_scope,
    );
    const startTimestamp = strictCalendarDateTimestamp(
      normalizedQuery.start_date,
    );
    const endTimestamp = strictCalendarDateTimestamp(
      normalizedQuery.end_date,
    );
    if (
      !confirmed ||
      driver.triggered !== true ||
      (
        driver.provider === "qunar" &&
        driver.result_query_readback_confirmed !== true
      ) ||
      confirmationScope !== "confirmed_visible_search"
    ) {
      return false;
    }
    return (
      sameVisiblePlace(
        normalizedQuery.destination,
        confirmed.destination,
      ) &&
      startTimestamp !== null &&
      endTimestamp !== null &&
      endTimestamp > startTimestamp &&
      normalizedQuery.start_date === confirmed.start_date &&
      normalizedQuery.end_date === confirmed.end_date &&
      Number.isInteger(normalizedQuery.adults) &&
      normalizedQuery.adults > 0 &&
      normalizedQuery.adults === confirmed.adults &&
      Number.isInteger(normalizedQuery.rooms) &&
      normalizedQuery.rooms > 0 &&
      normalizedQuery.rooms === confirmed.rooms
    );
  }

  function explicitPackageArea(areaText) {
    const value = cleanText(areaText);
    if (!value) {
      return null;
    }
    if (/胡鲁马累|hulhumal[eé]|机场岛|airport island/i.test(value)) {
      return "airport_island";
    }
    if (
      /马富施|马富士|maafushi|班度士|bandos|度假岛|resort island/i.test(value) ||
      (/(?:岛|island)/i.test(value) &&
        !/机场|airport|胡鲁马累|hulhumal[eé]/i.test(value))
    ) {
      return "destination_island";
    }
    return null;
  }

  function packageAreaEvidence(areaText, query, driver) {
    const expected = query.options.expected_package_area || null;
    const explicit = explicitPackageArea(areaText);
    if (explicit) {
      return {
        area: explicit,
        source: "visible_label",
        matches_expected: expected ? explicit === expected : null,
      };
    }
    const confirmed =
      driver &&
      driver.triggered === true &&
      driver.confirmed_query &&
      driver.confirmed_query.destination;
    const confirmationScope = cleanText(driver && driver.confirmation_scope);
    if (
      expected &&
      confirmed &&
      /confirmed_visible_search|fixture_exact_area/.test(
        confirmationScope,
      ) &&
      sameVisiblePlace(query.destination, confirmed) &&
      sameVisiblePlace(areaText, confirmed)
    ) {
      return {
        area: expected,
        source: "confirmed_exact_search_area",
        matches_expected: true,
      };
    }
    return { area: null, source: null, matches_expected: null };
  }

  function strictCalendarDateTimestamp(value) {
    const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(cleanText(value));
    if (!match) {
      return null;
    }
    const year = Number(match[1]);
    const month = Number(match[2]);
    const day = Number(match[3]);
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

  function isCtripLodgingCheckoutDateParameter(
    provider,
    parsed,
    queryKey,
  ) {
    if (
      queryKey !== "checkout" ||
      !(
        (
          provider === "ctrip" &&
          (
            parsed.hostname === "hotels.ctrip.com" ||
            parsed.hostname.endsWith(".hotels.ctrip.com")
          ) &&
          /^\/hotels\/detail\/?$/i.test(parsed.pathname)
        ) ||
        (
          provider === "fliggy" &&
          parsed.hostname === "hotel.fliggy.com" &&
          /^\/hotel_detail2\.htm\/?$/i.test(parsed.pathname)
        )
      )
    ) {
      return false;
    }
    if (
      provider === "ctrip" &&
      !(
        parsed.hostname === "hotels.ctrip.com" ||
        parsed.hostname.endsWith(".hotels.ctrip.com")
      )
    ) {
      return false;
    }
    const propertyIds = searchParamValues(
      parsed.searchParams,
      provider === "ctrip" ? "hotelId" : "shid",
    );
    const checkIns = searchParamValues(parsed.searchParams, "checkIn");
    const checkOuts = searchParamValues(parsed.searchParams, "checkOut");
    if (
      propertyIds.length !== 1 ||
      !/^[1-9]\d*$/.test(propertyIds[0]) ||
      checkIns.length !== 1 ||
      checkOuts.length !== 1
    ) {
      return false;
    }
    const checkIn = strictCalendarDateTimestamp(checkIns[0]);
    const checkOut = strictCalendarDateTimestamp(checkOuts[0]);
    return (
      checkIn !== null &&
      checkOut !== null &&
      checkOut > checkIn
    );
  }

  function safeProviderDetailUrl(provider, rawUrl, baseUrl) {
    try {
      const parsed = new URL(rawUrl, baseUrl);
      const suffixes = PROVIDER_HOST_SUFFIXES[provider];
      const forbidden = new Set(["cashier", "checkout", "coupon", "order", "payment"]);
      const pathSegments = parsed.pathname
        .toLowerCase()
        .replaceAll("-", "/")
        .replaceAll("_", "/")
        .split("/")
        .filter(Boolean);
      const queryKeys = [...parsed.searchParams.keys()].map((key) => key.toLowerCase());
      if (
        parsed.protocol !== "https:" ||
        !suffixes ||
        !suffixes.some(
          (suffix) =>
            parsed.hostname === suffix ||
            parsed.hostname.endsWith(`.${suffix}`),
        ) ||
        parsed.username ||
        parsed.password ||
        pathSegments.some((segment) =>
          [...forbidden].some((marker) => segment.includes(marker))
        ) ||
        queryKeys.some(
          (key) =>
            forbidden.has(key) &&
            !isCtripLodgingCheckoutDateParameter(provider, parsed, key),
        )
      ) {
        return null;
      }
      return parsed.href;
    } catch {
      return null;
    }
  }

  function lodgingDetailUrl(provider, card, pageUrl) {
    for (const selector of DETAIL_LINK_SELECTORS) {
      for (const link of card.querySelectorAll(selector)) {
        if (!visibleEvidence(link)) {
          continue;
        }
        const label = cleanText(
          [
            link.textContent,
            link.getAttribute("aria-label"),
            link.getAttribute("title"),
          ].filter(Boolean).join(" "),
        );
        if (
          !/详情|查看房型|酒店|房型|details?|rooms?/i.test(label) ||
          /预订|下单|支付|购买|优惠券|订单|去付款/.test(label)
        ) {
          continue;
        }
        const safe = safeProviderDetailUrl(
          provider,
          link.getAttribute("href") || "",
          pageUrl,
        );
        if (safe) {
          return safe;
        }
      }
    }
    return null;
  }

  function searchParamValues(searchParams, expectedName) {
    const normalizedName = expectedName.toLowerCase();
    const values = [];
    for (const [name, value] of searchParams.entries()) {
      if (name.toLowerCase() === normalizedName) {
        values.push(cleanText(value));
      }
    }
    return values;
  }

  function ctripLodgingDetailUrlContext(pageUrl, query) {
    let parsed;
    try {
      parsed = new URL(pageUrl);
    } catch {
      return { recognized: false };
    }
    const recognized = /^\/hotels\/detail\/?$/i.test(parsed.pathname);
    if (!recognized) {
      return { recognized: false };
    }
    const safeUrl = safeProviderDetailUrl("ctrip", pageUrl, pageUrl);
    const safeHost =
      parsed.hostname === "hotels.ctrip.com" ||
      parsed.hostname.endsWith(".hotels.ctrip.com");
    const hotelIds = searchParamValues(parsed.searchParams, "hotelId");
    const checkIns = searchParamValues(parsed.searchParams, "checkIn");
    const checkOuts = searchParamValues(parsed.searchParams, "checkOut");
    const adults = searchParamValues(parsed.searchParams, "adult");
    const rooms = searchParamValues(parsed.searchParams, "crn");
    const propertyId =
      hotelIds.length === 1 && /^[1-9]\d*$/.test(hotelIds[0])
        ? hotelIds[0]
        : null;
    const requestedAdults = Number.isInteger(query.adults)
      ? String(query.adults)
      : null;
    const requestedRooms = Number.isInteger(query.rooms)
      ? String(query.rooms)
      : null;
    const urlQueryMatches =
      checkIns.length === 1 &&
      checkIns[0] === query.start_date &&
      checkOuts.length === 1 &&
      checkOuts[0] === query.end_date &&
      adults.length === 1 &&
      adults[0] === requestedAdults &&
      rooms.length === 1 &&
      rooms[0] === requestedRooms;
    return {
      recognized: true,
      safe_url: safeUrl && safeHost ? safeUrl : null,
      safe_host: Boolean(safeHost),
      property_id: propertyId,
      url_query_matches: Boolean(urlQueryMatches),
      url_values: {
        check_in: checkIns.length === 1 ? checkIns[0] : null,
        check_out: checkOuts.length === 1 ? checkOuts[0] : null,
        adults: adults.length === 1 ? adults[0] : null,
        rooms: rooms.length === 1 ? rooms[0] : null,
      },
    };
  }

  function fliggyLodgingDetailUrlContext(pageUrl, query) {
    let parsed;
    try {
      parsed = new URL(pageUrl);
    } catch {
      return { recognized: false };
    }
    const recognized =
      parsed.hostname.toLowerCase() === "hotel.fliggy.com" &&
      parsed.pathname.toLowerCase().replace(/\/+$/, "") ===
        "/hotel_detail2.htm";
    if (!recognized) {
      return { recognized: false };
    }
    const safeUrl = safeProviderDetailUrl("fliggy", pageUrl, pageUrl);
    const propertyIds = searchParamValues(parsed.searchParams, "shid");
    const cityIds = searchParamValues(parsed.searchParams, "city");
    const checkIns = searchParamValues(parsed.searchParams, "checkIn");
    const checkOuts = searchParamValues(parsed.searchParams, "checkOut");
    const adults = searchParamValues(parsed.searchParams, "aNum_1");
    const children = searchParamValues(parsed.searchParams, "cNum_1");
    const rooms = searchParamValues(parsed.searchParams, "roomNum");
    const propertyId =
      propertyIds.length === 1 && /^[1-9]\d*$/.test(propertyIds[0])
        ? propertyIds[0]
        : null;
    const cityId =
      cityIds.length === 1 && /^\d{6}$/.test(cityIds[0])
        ? cityIds[0]
        : null;
    const requestedAdults = Number.isInteger(query.adults)
      ? String(query.adults)
      : null;
    const requestedRooms = Number.isInteger(query.rooms)
      ? String(query.rooms)
      : null;
    const urlQueryMatches =
      checkIns.length === 1 &&
      checkIns[0] === query.start_date &&
      checkOuts.length === 1 &&
      checkOuts[0] === query.end_date &&
      adults.length === 1 &&
      adults[0] === requestedAdults &&
      children.length === 1 &&
      children[0] === "0" &&
      rooms.length === 1 &&
      rooms[0] === requestedRooms;
    return {
      recognized: true,
      safe_url: safeUrl,
      property_id: propertyId,
      city_id: cityId,
      url_query_matches: Boolean(urlQueryMatches),
      url_values: {
        check_in: checkIns.length === 1 ? checkIns[0] : null,
        check_out: checkOuts.length === 1 ? checkOuts[0] : null,
        adults: adults.length === 1 ? adults[0] : null,
        children: children.length === 1 ? children[0] : null,
        rooms: rooms.length === 1 ? rooms[0] : null,
      },
    };
  }

  function qunarInventoryObservationCaptureValid(capture) {
    if (!capture || typeof capture !== "object" || Array.isArray(capture)) {
      return false;
    }
    const state = capture.inventory_observation_state;
    const observationCount = capture.inventory_observation_count;
    const observedDurationMs = capture.inventory_observation_duration_ms;
    if (
      !Number.isInteger(observationCount) ||
      !Number.isInteger(observedDurationMs)
    ) {
      return false;
    }
    if (state === "confirmed_empty") {
      return (
        observationCount === 2 &&
        observedDurationMs >= 2000 &&
        observedDurationMs <= 120000
      );
    }
    if (state === "bounded_provider_pending") {
      return (
        observationCount === 1 &&
        observedDurationMs >= QUNAR_PENDING_MIN_OBSERVED_MS &&
        observedDurationMs <= 120000
      );
    }
    return false;
  }

  function qunarLodgingDetailUrlContext(pageUrl, query, driver = null) {
    let parsed;
    try {
      parsed = new URL(pageUrl);
    } catch {
      return { recognized: false };
    }
    const pathMatch =
      /^\/city\/(i-ka_maafushi)\/dt-([1-9]\d*)\/$/.exec(
        parsed.pathname,
      );
    if (!pathMatch) {
      return { recognized: false };
    }
    const property = QUNAR_AUDITED_LODGING_DETAILS[pathMatch[2]] || null;
    const hashEntries = [
      ...new URLSearchParams(parsed.hash.slice(1)).entries(),
    ];
    const requiredHashKeys = new Set([
      "fromDate",
      "toDate",
      "q",
      "showMap",
    ]);
    const hashShapeMatches =
      hashEntries.length === requiredHashKeys.size &&
      hashEntries.every(([key]) => requiredHashKeys.has(key)) &&
      [...requiredHashKeys].every(
        (key) => hashEntries.filter(([candidate]) => candidate === key).length === 1,
      );
    const hash = new URLSearchParams(parsed.hash.slice(1));
    const capture =
      driver && driver.qunar_detail_capture &&
      typeof driver.qunar_detail_capture === "object" &&
      !Array.isArray(driver.qunar_detail_capture)
        ? driver.qunar_detail_capture
        : null;
    const targetMatches = Boolean(
      property &&
      capture &&
      capture.city_slug === property.city_slug &&
      capture.hotel_seq === property.hotel_seq &&
      capture.property_id === pathMatch[2] &&
      capture.property_name === property.property_name
    );
    const resultPath = `/city/${property && property.city_slug || ""}`;
    const lineageMatches = Boolean(
      driver &&
      driver.provider === "qunar" &&
      driver.triggered === true &&
      driver.confirmation_scope === "confirmed_visible_search" &&
      driver.result_query_readback_confirmed === true &&
      driver.result_query_readback_scope === "qunar_visible_result_form_fields" &&
      driver.result_query_readback_evidence &&
      driver.result_query_readback_evidence.provider_destination_id ===
        (property && property.city_slug) &&
      driver.result_query_readback_evidence.result_path === resultPath &&
      driver.result_query_readback_evidence.room_scope ===
        "audited_qunar_single_room_search_surface" &&
      capture &&
      capture.source === "qunar_audited_read_only_lodging_detail" &&
      capture.contract_scope === "audited_qunar_exact_detail_url" &&
      capture.clicked_booking === false &&
      capture.same_controlled_tab === true &&
      qunarInventoryObservationCaptureValid(capture) &&
      /^[a-f0-9]{64}$/.test(
        String(capture.list_inventory_receipt_sha256 || ""),
      )
    );
    const safeUrl = safeProviderDetailUrl("qunar", pageUrl, pageUrl);
    const urlQueryMatches = Boolean(
      hashShapeMatches &&
      hash.get("fromDate") === query.start_date &&
      hash.get("toDate") === query.end_date &&
      hash.get("q") === "" &&
      hash.get("showMap") === "0"
    );
    return {
      recognized: true,
      safe_url:
        parsed.protocol === "https:" &&
        parsed.hostname.toLowerCase() === "hotel.qunar.com" &&
        !parsed.port &&
        !parsed.username &&
        !parsed.password &&
        parsed.search === "" &&
        String(pageUrl).includes("/?#") &&
        safeUrl &&
        property &&
        targetMatches &&
        lineageMatches &&
        urlQueryMatches
          ? safeUrl
          : null,
      city_slug: property ? property.city_slug : null,
      hotel_seq: property ? property.hotel_seq : null,
      property_id: property ? pathMatch[2] : null,
      property_name: property ? property.property_name : null,
      target_matches: targetMatches,
      lineage_matches: lineageMatches,
      url_query_matches: urlQueryMatches,
      url_values: {
        check_in: hashShapeMatches ? hash.get("fromDate") : null,
        check_out: hashShapeMatches ? hash.get("toDate") : null,
        q: hashShapeMatches ? hash.get("q") : null,
        show_map: hashShapeMatches ? hash.get("showMap") : null,
      },
    };
  }

  function tongchengLodgingDetailUrlContext(pageUrl, query) {
    let parsed;
    try {
      parsed = new URL(pageUrl);
    } catch {
      return { recognized: false };
    }
    const host = parsed.hostname.toLowerCase();
    const path = parsed.pathname.toLowerCase().replace(/\/+$/, "");
    const recognized =
      (host === "www.ly.com" && path === "/hotel/hoteldetail") ||
      (host === "m.ly.com" && path === "/hotel/hoteldetail") ||
      (host === "m.elong.com" && path === "/ihotel/hoteldetail");
    if (!recognized) {
      return { recognized: false };
    }
    const propertyIds = searchParamValues(parsed.searchParams, "hotelId");
    const checkIns = searchParamValues(parsed.searchParams, "inDate");
    const checkOuts = searchParamValues(parsed.searchParams, "outDate");
    const adults = searchParamValues(parsed.searchParams, "adultsNumber");
    const international = searchParamValues(parsed.searchParams, "intl");
    const propertyId =
      propertyIds.length === 1 && /^[1-9]\d*$/.test(propertyIds[0])
        ? propertyIds[0]
        : null;
    return {
      recognized: true,
      safe_url: safeProviderDetailUrl("tongcheng", pageUrl, pageUrl),
      property_id: propertyId,
      url_query_matches:
        checkIns.length === 1 && checkIns[0] === query.start_date &&
        checkOuts.length === 1 && checkOuts[0] === query.end_date &&
        adults.length === 1 && adults[0] === String(query.adults) &&
        international.length === 1 && international[0] === "1",
      url_values: {
        check_in: checkIns.length === 1 ? checkIns[0] : null,
        check_out: checkOuts.length === 1 ? checkOuts[0] : null,
        adults: adults.length === 1 ? adults[0] : null,
        intl: international.length === 1 ? international[0] : null,
      },
    };
  }

  function visibleNodeEvidenceText(node) {
    return cleanText(
      [
        node.textContent,
        "value" in node ? node.value : null,
        node.getAttribute && node.getAttribute("aria-label"),
        node.getAttribute && node.getAttribute("title"),
      ].filter(Boolean).join(" "),
    );
  }

  function compactVisibleTexts(root, selectors, maxLength = 240, limit = 400) {
    const seenNodes = new Set();
    const seenText = new Set();
    const values = [];
    for (const selector of selectors) {
      for (const node of root.querySelectorAll(selector)) {
        if (seenNodes.has(node) || !visibleEvidence(node)) {
          continue;
        }
        seenNodes.add(node);
        const value = visibleNodeEvidenceText(node);
        if (
          value &&
          value.length <= maxLength &&
          !seenText.has(value)
        ) {
          seenText.add(value);
          values.push(value);
          if (values.length >= limit) {
            return values;
          }
        }
      }
    }
    return values;
  }

  function detailVisibleDateTokens(value, fallbackYear) {
    const tokens = visibleDateTokens(value, fallbackYear);
    const add = (month, day) => {
      const result =
        `${fallbackYear}-${String(month).padStart(2, "0")}-` +
        String(day).padStart(2, "0");
      const parsed = new Date(`${result}T00:00:00Z`);
      if (
        !Number.isNaN(parsed.getTime()) &&
        parsed.toISOString().slice(0, 10) === result &&
        !tokens.includes(result)
      ) {
        tokens.push(result);
      }
    };
    for (const match of cleanText(value).matchAll(
      /(?:^|[^\d])(\d{1,2})\s*[/.-]\s*(\d{1,2})(?!\d)/g,
    )) {
      add(match[1], match[2]);
    }
    return tokens;
  }

  function stayNightCount(checkIn, checkOut) {
    const start = new Date(`${checkIn}T00:00:00Z`);
    const end = new Date(`${checkOut}T00:00:00Z`);
    const days = (end.getTime() - start.getTime()) / 86400000;
    return Number.isInteger(days) && days > 0 ? days : null;
  }

  function ctripDetailStayReadback(root, query) {
    const fallbackYear = cleanText(query.start_date).slice(0, 4);
    const candidates = compactVisibleTexts(root, [
      "[data-tripchord-fixture='stay-readback']",
      "[class*='date']",
      "[class*='Date']",
      "[class*='check']",
      "[class*='Check']",
      "input",
      "button",
      "span",
      "p",
      "div",
    ]);
    const dateEvidence = candidates.filter((value) => {
      const tokens = detailVisibleDateTokens(value, fallbackYear);
      return (
        tokens.includes(query.start_date) ||
        tokens.includes(query.end_date)
      );
    });
    const tokens = new Set(
      dateEvidence.flatMap((value) =>
        detailVisibleDateTokens(value, fallbackYear)
      ),
    );
    const nights = stayNightCount(query.start_date, query.end_date);
    const nightEvidence =
      nights === null
        ? null
        : candidates.find((value) =>
            new RegExp(`(?:^|\\D)${nights}\\s*晚(?:\\D|$)`).test(value)
          ) || null;
    return {
      matched:
        tokens.has(query.start_date) &&
        tokens.has(query.end_date) &&
        Boolean(nightEvidence),
      evidence: [...dateEvidence.slice(0, 4), nightEvidence]
        .filter(Boolean)
        .join(" | "),
      nights,
    };
  }

  function ctripDetailOccupancyReadback(root, query) {
    if (!Number.isInteger(query.rooms) || !Number.isInteger(query.adults)) {
      return { matched: false, evidence: null };
    }
    const candidates = compactVisibleTexts(root, [
      "[data-tripchord-fixture='occupancy-readback']",
      "[class*='guest']",
      "[class*='Guest']",
      "[class*='occupancy']",
      "[class*='roomCount']",
      "input",
      "button",
      "span",
      "p",
      "div",
    ]);
    const pattern = new RegExp(
      `(?:^|\\D)${query.rooms}\\s*间(?:房)?\\s*` +
        `(?:[/／,，|·]\\s*)${query.adults}\\s*(?:位\\s*)?成人(?:\\D|$)`,
    );
    const evidence = candidates.find((value) => pattern.test(value)) || null;
    return { matched: Boolean(evidence), evidence };
  }

  function fliggyDetailOccupancyReadback(root, query) {
    if (!Number.isInteger(query.rooms) || !Number.isInteger(query.adults)) {
      return { matched: false, evidence: null };
    }
    const candidates = compactVisibleTexts(root, [
      "[data-tripchord-fixture='occupancy-readback']",
      "[data-agent-type='adult-count-select']",
      "[class*='adult']",
      "[class*='Adult']",
      "select",
      "option:checked",
      "input",
      "button",
      "span",
      "div",
    ]);
    const adultPattern = new RegExp(
      `(?:成人\\s*${query.adults}(?:\\D|$)|` +
        `(?:^|\\D)${query.adults}\\s*(?:位\\s*)?成人(?:\\D|$))`,
    );
    const evidence = candidates.find((value) => adultPattern.test(value)) || null;
    return { matched: Boolean(evidence), evidence };
  }

  function lodgingPlaceEvidence(expectedPlaceKey, propertyTitle, areaText) {
    const canonicalExpectedPlaceKey =
      canonicalLodgingPlaceKey(expectedPlaceKey);
    if (!canonicalExpectedPlaceKey) {
      return {
        expected_key: null,
        observed_key: null,
        matches_expected: null,
        evidence: null,
      };
    }
    const evidence = cleanText(`${propertyTitle || ""} ${areaText || ""}`);
    const comparable = evidence.toLowerCase();
    let observedKey = null;
    for (const [placeKey, aliases] of Object.entries(
      CTRIP_LODGING_PLACE_ALIASES,
    )) {
      if (aliases.some((alias) => comparable.includes(alias.toLowerCase()))) {
        observedKey = placeKey;
        break;
      }
    }
    return {
      expected_key: canonicalExpectedPlaceKey,
      observed_key: observedKey,
      matches_expected: observedKey === canonicalExpectedPlaceKey,
      evidence,
    };
  }

  function completeCurrencyAmountFragments(value) {
    return [
      ...cleanText(value).matchAll(
        /(?:¥|￥|\$|CNY|RMB|USD)\s*[0-9]+(?:,[0-9]{3})*(?:\.[0-9]{1,2})?/gi,
      ),
    ].map((match) => match[0]);
  }

  function directVisibleNodeText(node) {
    return cleanText(
      [...node.childNodes]
        .filter((child) => child.nodeType === 3)
        .map((child) => child.textContent)
        .join(" "),
    );
  }

  function ctripAtomicTaxPriceCandidates(
    rateRow,
    { allowSingleNightTotal = false } = {},
  ) {
    const candidates = [];
    const seen = new Set();
    for (const selector of CTRIP_LODGING_DETAIL_TAX_PRICE_SELECTORS) {
      for (const node of rateRow.querySelectorAll(selector)) {
        if (seen.has(node) || !visibleEvidence(node)) {
          continue;
        }
        seen.add(node);
        // Fail closed unless the tax marker, per-night average marker and the
        // complete currency amount are atomic in this exact visible node.
        // A parent assembled from separate "¥" and digit descendants is not
        // sufficient evidence.
        const evidence = directVisibleNodeText(node);
        const fragments = completeCurrencyAmountFragments(evidence);
        const isPerNight = CTRIP_PER_NIGHT_AVERAGE_PATTERN.test(evidence);
        if (
          !evidence ||
          evidence.length > 180 ||
          !CTRIP_TAX_INCLUDED_PRICE_PATTERN.test(evidence) ||
          (!isPerNight && !allowSingleNightTotal) ||
          lodgingPriceFinality(evidence) !== "exact_candidate" ||
          taxesIncluded(evidence) !== true ||
          fragments.length !== 1
        ) {
          continue;
        }
        const amount = parseAmount(fragments[0]);
        if (amount === null) {
          continue;
        }
        candidates.push({
          node,
          evidence,
          amount,
          currency: /(?:USD|\$)/i.test(fragments[0]) ? "USD" : "CNY",
          price_basis: isPerNight ? "per_night" : "total_stay",
          price_basis_source: isPerNight
            ? "visible_per_night_average_marker"
            : "audited_exact_single_night_tax_total",
        });
      }
    }
    return candidates.filter(
      (candidate) =>
        !candidates.some(
          (other) =>
            other !== candidate &&
            candidate.node.contains(other.node),
        ),
    );
  }

  function ctripDetailAvailabilityText(rateRow) {
    for (const node of rateRow.querySelectorAll(
      [
        "button",
        "a",
        "[role='button']",
        "[class*='bookBtn']",
        "[class*='BookBtn']",
        "[class*='bookingBtn']",
        "[class*='reserveBtn']",
      ].join(","),
    )) {
      if (
        !visibleEvidence(node) ||
        node.disabled ||
        node.getAttribute("aria-disabled") === "true"
      ) {
        continue;
      }
      const label = cleanText(
        [
          node.textContent,
          node.getAttribute("aria-label"),
          node.getAttribute("title"),
        ].filter(Boolean).join(" "),
      );
      if (
        /^(?:预订|立即预订|可预订|book(?:\s+now)?)$/i.test(label) &&
        !/不可|售罄|无房|sold\s*out|unavailable/i.test(label)
      ) {
        return label;
      }
    }
    return null;
  }

  function fliggyDetailAvailabilityText(rateRow) {
    for (const node of rateRow.querySelectorAll(
      "a, button, [role='button']",
    )) {
      if (
        !visibleEvidence(node) ||
        node.disabled ||
        node.getAttribute("aria-disabled") === "true"
      ) {
        continue;
      }
      const label = cleanText(
        [
          node.textContent,
          node.getAttribute("aria-label"),
          node.getAttribute("title"),
        ].filter(Boolean).join(" "),
      );
      if (
        /^(?:预订|立即预订|可预订)$/i.test(label) &&
        !/不可|售罄|无房|sold\s*out|unavailable/i.test(label)
      ) {
        return label;
      }
    }
    return null;
  }

  function fliggySemanticRateRows(root) {
    const rows = [];
    for (const control of root.querySelectorAll("a, button, [role='button']")) {
      if (!visibleEvidence(control)) {
        continue;
      }
      const label = cleanText(
        [
          control.textContent,
          control.getAttribute("aria-label"),
          control.getAttribute("title"),
        ].filter(Boolean).join(" "),
      );
      if (!/^(?:预订|立即预订|可预订)$/i.test(label)) {
        continue;
      }
      let candidate = control.parentElement;
      let depth = 0;
      while (candidate && depth < 8) {
        const tag = cleanText(candidate.tagName).toLowerCase();
        if (
          candidate === root.body ||
          candidate === root.documentElement ||
          DIAGNOSTIC_BOUNDARY_TAGS.has(tag)
        ) {
          break;
        }
        const text = cleanText(candidate.innerText || candidate.textContent);
        const fragments = completeCurrencyAmountFragments(text);
        if (
          visibleEvidence(candidate) &&
          text.length >= 8 &&
          text.length <= 1200 &&
          fragments.length === 1 &&
          /已含税|含税费|tax(?:es)?\s+included/i.test(text) &&
          lodgingPriceFinality(text) === "exact_candidate"
        ) {
          rows.push(candidate);
          break;
        }
        candidate = candidate.parentElement;
        depth += 1;
      }
    }
    return rows.filter(
      (row, index) =>
        rows.indexOf(row) === index &&
        !rows.some((other) => other !== row && row.contains(other)),
    ).slice(0, 30);
  }

  function fliggyAtomicTaxPriceCandidate(rateRow) {
    const rowText = cleanText(rateRow.innerText || rateRow.textContent);
    if (
      !rowText ||
      lodgingPriceFinality(rowText) !== "exact_candidate" ||
      taxesIncluded(rowText) !== true
    ) {
      return null;
    }
    const candidates = [];
    const seen = new Set();
    for (const selector of FLIGGY_LODGING_DETAIL_PRICE_SELECTORS) {
      for (const node of rateRow.querySelectorAll(selector)) {
        if (seen.has(node) || !visibleEvidence(node)) {
          continue;
        }
        seen.add(node);
        const evidence = directVisibleNodeText(node);
        const fragments = completeCurrencyAmountFragments(evidence);
        if (
          !evidence ||
          evidence.length > 120 ||
          fragments.length !== 1 ||
          lodgingPriceFinality(evidence) !== "exact_candidate"
        ) {
          continue;
        }
        const amount = parseAmount(fragments[0]);
        if (amount === null) {
          continue;
        }
        candidates.push({
          node,
          evidence: fragments[0],
          amount,
          currency: /(?:USD|\$)/i.test(fragments[0]) ? "USD" : "CNY",
        });
      }
    }
    const atomic = candidates.filter(
      (candidate) =>
        !candidates.some(
          (other) =>
            other !== candidate && candidate.node.contains(other.node),
        ),
    );
    const unique = new Map(
      atomic.map((candidate) => [
        `${candidate.currency}:${candidate.amount}`,
        candidate,
      ]),
    );
    return unique.size === 1 ? [...unique.values()][0] : null;
  }

  function fliggyDetailRoomText(rateRow) {
    let scope = rateRow;
    let depth = 0;
    while (scope && depth < 5) {
      const titled = firstText(scope, [
        "[data-tripchord-fixture='room-title']",
        ".room-name",
        "[class*='roomName']",
        "[class*='room-name']",
        "h3",
        "h4",
      ]);
      if (titled) {
        return titled;
      }
      scope = scope.parentElement;
      depth += 1;
    }
    const candidates = compactVisibleTexts(rateRow, [
      "[data-tripchord-fixture='room-title']",
      "[class*='room']",
      "[class*='Room']",
      "[class*='name']",
      "[class*='Name']",
      "td",
      "span",
      "div",
    ], 220, 80);
    return candidates.find((value) =>
      !PRICE_ANCHOR_PATTERN.test(value) &&
      !/^(?:预订|立即预订|可预订|已含税|不含税)$/i.test(value) &&
      !/卖家|报价列表|预订详情|取消|退订/.test(value) &&
      value.length >= 3
    ) || null;
  }

  function ctripDetailTerm(text, pattern) {
    const match = cleanText(text).match(pattern);
    return match ? cleanText(match[0]) : null;
  }

  function ctripDetailFailure(
    pageUrl,
    capturedAt,
    message,
    gateDetails,
  ) {
    return {
      state: "failed",
      quotes: [],
      failure: {
        code: "dom_drift",
        message,
        retryable: false,
        page_url: pageUrl,
        captured_at: capturedAt,
        details: {
          parser_version: PARSER_VERSION,
          extraction: "ctrip_lodging_detail",
          gates: gateDetails,
        },
      },
    };
  }

  async function extractCtripLodgingDetailPage(
    root,
    pageUrl,
    capturedAt,
    query,
    driver,
  ) {
    const normalizedQuery = safeQuery(query);
    const urlContext = ctripLodgingDetailUrlContext(
      pageUrl,
      normalizedQuery,
    );
    if (!urlContext.recognized) {
      return null;
    }
    const propertyTitle = firstText(
      root,
      CTRIP_LODGING_DETAIL_PROPERTY_TITLE_SELECTORS,
    );
    const areaText =
      firstText(root, CTRIP_LODGING_DETAIL_AREA_SELECTORS) || null;
    const areaEvidence = packageAreaEvidence(
      areaText,
      normalizedQuery,
      driver,
    );
    const placeEvidence = lodgingPlaceEvidence(
      normalizedQuery.options.expected_lodging_place_key || null,
      propertyTitle,
      areaText,
    );
    const stayReadback = ctripDetailStayReadback(root, normalizedQuery);
    const occupancyReadback = ctripDetailOccupancyReadback(
      root,
      normalizedQuery,
    );
    const expectedArea =
      normalizedQuery.options.expected_package_area || null;
    const packageAreaMatches =
      expectedArea === null
        ? areaEvidence.area !== null
        : areaEvidence.area === expectedArea;
    const placeMatches =
      placeEvidence.expected_key === null
        ? true
        : placeEvidence.matches_expected === true;
    const baseGates = {
      provider_detail_url: Boolean(urlContext.safe_url),
      numeric_property_id: Boolean(urlContext.property_id),
      url_query_matches: urlContext.url_query_matches === true,
      visible_stay_readback: stayReadback.matched === true,
      visible_occupancy_readback: occupancyReadback.matched === true,
      property_title: Boolean(propertyTitle),
      visible_area: Boolean(areaText && areaEvidence.area),
      package_area_matches: packageAreaMatches,
      lodging_place_matches: placeMatches,
    };
    if (Object.values(baseGates).some((value) => value !== true)) {
      return ctripDetailFailure(
        urlContext.safe_url || pageUrl,
        capturedAt,
        "携程酒店详情页的查询、入住条件或区域证据不完整",
        {
          ...baseGates,
          url_values: urlContext.url_values || {},
          expected_lodging_place_key:
            placeEvidence.expected_key,
          observed_lodging_place_key:
            placeEvidence.observed_key,
          area_matches_expected:
            packageAreaMatches && placeMatches,
        },
      );
    }

    const roomGroups = visibleNodes(
      root,
      CTRIP_LODGING_DETAIL_ROOM_SELECTORS,
      30,
    );
    const parsed = [];
    let visibleRateRows = 0;
    let atomicTaxPriceRows = 0;
    let availableRows = 0;
    for (const roomGroup of roomGroups) {
      const roomText = firstText(
        roomGroup,
        CTRIP_LODGING_DETAIL_ROOM_TITLE_SELECTORS,
      );
      if (!roomText) {
        continue;
      }
      const rateRows = visibleNodes(
        roomGroup,
        CTRIP_LODGING_DETAIL_RATE_SELECTORS,
        30,
      );
      visibleRateRows += rateRows.length;
      for (const rateRow of rateRows) {
        const rateText = cleanText(rateRow.innerText || rateRow.textContent);
        if (
          !rateText ||
          lodgingPriceFinality(rateText) !== "exact_candidate"
        ) {
          continue;
        }
        const taxPrices = ctripAtomicTaxPriceCandidates(rateRow, {
          allowSingleNightTotal: stayReadback.nights === 1,
        });
        if (taxPrices.length !== 1) {
          continue;
        }
        atomicTaxPriceRows += 1;
        const availabilityText = ctripDetailAvailabilityText(rateRow);
        if (!availabilityText) {
          continue;
        }
        availableRows += 1;
        const taxPrice = taxPrices[0];
        if (taxPrice.currency !== normalizedQuery.currency) {
          continue;
        }
        const breakfastText = ctripDetailTerm(
          rateText,
          /(?:不含早餐|未含早餐|无早|\d+\s*份早餐|含早餐|含早)/,
        );
        const cancellationText = ctripDetailTerm(
          rateText,
          /(?:不可取消|不可退订|免费取消|限时取消|取消政策)/,
        );
        const displayPriceNode = visibleNodes(
          rateRow,
          [
            "[class*='saleRoomItemBox-priceBox-displayPrice__']",
            "[aria-label*='Current price']",
          ],
          1,
        )[0];
        const displayPriceText = displayPriceNode
          ? visibleNodeEvidenceText(displayPriceNode)
          : null;
        const visibleTerms = [
          breakfastText,
          cancellationText,
          ctripDetailTerm(rateText, /立即确认/),
          ctripDetailTerm(rateText, /在线付/),
          taxPrice.evidence,
          availabilityText,
        ].filter(Boolean);
        const details = {
          query: normalizedQuery,
          driver: driver || null,
          destination: normalizedQuery.destination,
          check_in: normalizedQuery.start_date,
          check_out: normalizedQuery.end_date,
          adults: normalizedQuery.adults,
          rooms: normalizedQuery.rooms,
          property_id: urlContext.property_id,
          property_name: propertyTitle,
          room_text: roomText,
          rate_text: rateText,
          area_text: areaText,
          area: areaEvidence.area,
          area_source: "visible_label",
          area_matches_expected: true,
          expected_lodging_place_key:
            placeEvidence.expected_key,
          observed_lodging_place_key:
            placeEvidence.observed_key,
          lodging_place_matches_expected:
            placeEvidence.expected_key === null
              ? null
              : placeEvidence.matches_expected,
          breakfast_text: breakfastText,
          breakfast_included: breakfastIncluded(breakfastText),
          cancellation_text: cancellationText,
          availability: "available",
          availability_text: availabilityText,
          tax_evidence: taxPrice.evidence,
          price_text: taxPrice.evidence,
          display_price_text: displayPriceText,
          price_unit_evidence: taxPrice.evidence,
          price_basis_source: taxPrice.price_basis_source,
          price_finality: "final_for_rate",
          visible_terms: visibleTerms,
          stay_readback_evidence: stayReadback.evidence,
          occupancy_readback_evidence: occupancyReadback.evidence,
          extraction: "visible_dom_ctrip_lodging_detail",
          page_url: urlContext.safe_url,
          transfer_text: null,
          transfer_detail_url: null,
          transfer_detail_status: null,
          transfers: [],
        };
        const evidence = canonicalJson({
          amount: String(taxPrice.amount),
          currency: taxPrice.currency,
          details,
          provider: "ctrip",
          kind: "lodging",
          page_url: urlContext.safe_url,
          price_basis: taxPrice.price_basis,
          taxes_included: true,
          title: propertyTitle,
        });
        if (evidence.length > MAX_VISIBLE_EVIDENCE_CHARS) {
          continue;
        }
        parsed.push({
          provider: "ctrip",
          kind: "lodging",
          page_url: urlContext.safe_url,
          captured_at: capturedAt,
          parser_version: PARSER_VERSION,
          visible_evidence: evidence,
          evidence_sha256: await sha256(evidence),
          currency: taxPrice.currency,
          amount: taxPrice.amount,
          price_basis: taxPrice.price_basis,
          taxes_included: true,
          title: propertyTitle,
          details,
        });
      }
    }
    if (!parsed.length) {
      return ctripDetailFailure(
        urlContext.safe_url,
        capturedAt,
        "携程酒店详情页没有形成房型、税后每晚价与可预订状态完整的报价行",
        {
          ...baseGates,
          room_group_count: roomGroups.length,
          rate_row_count: visibleRateRows,
          atomic_tax_price_row_count: atomicTaxPriceRows,
          available_rate_row_count: availableRows,
          room_rate_contract: false,
        },
      );
    }
    return { state: "succeeded", quotes: parsed.slice(0, 30) };
  }

  async function extractFliggyLodgingDetailPage(
    root,
    pageUrl,
    capturedAt,
    query,
    driver,
  ) {
    const normalizedQuery = safeQuery(query);
    const urlContext = fliggyLodgingDetailUrlContext(
      pageUrl,
      normalizedQuery,
    );
    if (!urlContext.recognized) {
      return null;
    }
    const propertyTitle = firstText(
      root,
      FLIGGY_LODGING_DETAIL_PROPERTY_TITLE_SELECTORS,
    );
    const areaText =
      firstText(root, FLIGGY_LODGING_DETAIL_AREA_SELECTORS) || null;
    const areaEvidence = packageAreaEvidence(
      areaText,
      normalizedQuery,
      driver,
    );
    const placeEvidence = lodgingPlaceEvidence(
      normalizedQuery.options.expected_lodging_place_key || null,
      propertyTitle,
      areaText,
    );
    const stayReadback = ctripDetailStayReadback(root, normalizedQuery);
    const occupancyReadback = fliggyDetailOccupancyReadback(
      root,
      normalizedQuery,
    );
    const expectedArea =
      normalizedQuery.options.expected_package_area || null;
    const packageAreaMatches =
      expectedArea === null
        ? areaEvidence.area !== null
        : areaEvidence.area === expectedArea;
    const placeMatches =
      placeEvidence.expected_key === null
        ? true
        : placeEvidence.matches_expected === true;
    const baseGates = {
      provider_detail_url: Boolean(urlContext.safe_url),
      numeric_property_id: Boolean(urlContext.property_id),
      frozen_city_id: urlContext.city_id === "933081",
      url_query_matches: urlContext.url_query_matches === true,
      visible_stay_readback: stayReadback.matched === true,
      visible_adults_readback: occupancyReadback.matched === true,
      property_title: Boolean(propertyTitle),
      visible_area: Boolean(areaText && areaEvidence.area),
      package_area_matches: packageAreaMatches,
      lodging_place_matches: placeMatches,
    };
    if (Object.values(baseGates).some((value) => value !== true)) {
      return {
        state: "failed",
        quotes: [],
        failure: {
          code: "dom_drift",
          message: "飞猪酒店详情页的查询、入住条件或区域证据不完整",
          retryable: false,
          page_url: urlContext.safe_url || pageUrl,
          captured_at: capturedAt,
          details: {
            parser_version: PARSER_VERSION,
            extraction: "fliggy_lodging_detail",
            gates: baseGates,
            url_values: urlContext.url_values || {},
          },
        },
      };
    }
    const rateRows = fliggySemanticRateRows(root);
    const parsed = [];
    let atomicTaxPriceRows = 0;
    let availableRows = 0;
    for (const rateRow of rateRows) {
      const rateText = cleanText(rateRow.innerText || rateRow.textContent);
      const taxPrice = fliggyAtomicTaxPriceCandidate(rateRow);
      if (!taxPrice) {
        continue;
      }
      atomicTaxPriceRows += 1;
      const availabilityText = fliggyDetailAvailabilityText(rateRow);
      if (!availabilityText) {
        continue;
      }
      availableRows += 1;
      if (taxPrice.currency !== normalizedQuery.currency) {
        continue;
      }
      const roomText = fliggyDetailRoomText(rateRow) || propertyTitle;
      const breakfastText = ctripDetailTerm(
        rateText,
        /(?:不含早餐|未含早餐|无早|\d+\s*份早餐|含早餐|含早)/,
      );
      const cancellationText = ctripDetailTerm(
        rateText,
        /(?:不可取消|不可退订|免费取消|限时取消|取消政策)/,
      );
      const visibleTerms = [
        breakfastText,
        cancellationText,
        taxPrice.evidence,
        "已含税",
        availabilityText,
      ].filter(Boolean);
      const details = {
        query: normalizedQuery,
        driver: driver || null,
        destination: normalizedQuery.destination,
        check_in: normalizedQuery.start_date,
        check_out: normalizedQuery.end_date,
        adults: normalizedQuery.adults,
        rooms: normalizedQuery.rooms,
        property_id: urlContext.property_id,
        property_name: propertyTitle,
        room_text: roomText,
        rate_text: rateText,
        area_text: areaText,
        area: areaEvidence.area,
        area_source: "visible_label",
        area_matches_expected: true,
        expected_lodging_place_key: placeEvidence.expected_key,
        observed_lodging_place_key: placeEvidence.observed_key,
        lodging_place_matches_expected:
          placeEvidence.expected_key === null
            ? null
            : placeEvidence.matches_expected,
        breakfast_text: breakfastText,
        breakfast_included: breakfastIncluded(breakfastText),
        cancellation_text: cancellationText,
        availability: "available",
        availability_text: availabilityText,
        tax_evidence: "已含税",
        price_text: taxPrice.evidence,
        price_unit_evidence:
          `${taxPrice.evidence} | 飞猪酒店详情报价列表 | ${stayReadback.nights}晚`,
        price_basis_source: "audited_fliggy_hotel_detail_rate_contract",
        price_finality: "final_for_rate",
        visible_terms: visibleTerms,
        stay_readback_evidence: stayReadback.evidence,
        occupancy_readback_evidence: occupancyReadback.evidence,
        extraction: "visible_dom_fliggy_lodging_detail",
        page_url: urlContext.safe_url,
        transfer_text: null,
        transfer_detail_url: null,
        transfer_detail_status: null,
        transfers: [],
      };
      const evidence = canonicalJson({
        amount: String(taxPrice.amount),
        currency: taxPrice.currency,
        details,
        provider: "fliggy",
        kind: "lodging",
        page_url: urlContext.safe_url,
        price_basis: "per_night",
        taxes_included: true,
        title: propertyTitle,
      });
      if (evidence.length > MAX_VISIBLE_EVIDENCE_CHARS) {
        continue;
      }
      parsed.push({
        provider: "fliggy",
        kind: "lodging",
        page_url: urlContext.safe_url,
        captured_at: capturedAt,
        parser_version: PARSER_VERSION,
        visible_evidence: evidence,
        evidence_sha256: await sha256(evidence),
        currency: taxPrice.currency,
        amount: taxPrice.amount,
        price_basis: "per_night",
        taxes_included: true,
        title: propertyTitle,
        details,
      });
    }
    if (!parsed.length) {
      return {
        state: "failed",
        quotes: [],
        failure: {
          code: "dom_drift",
          message: "飞猪酒店详情页没有形成房型、税后每晚价与可预订状态完整的报价行",
          retryable: false,
          page_url: urlContext.safe_url,
          captured_at: capturedAt,
          details: {
            parser_version: PARSER_VERSION,
            extraction: "fliggy_lodging_detail",
            dom_diagnostics: domDriftDiagnostics(root),
            gates: {
              ...baseGates,
              rate_row_count: rateRows.length,
              atomic_tax_price_row_count: atomicTaxPriceRows,
              available_rate_row_count: availableRows,
              room_rate_contract: false,
            },
          },
        },
      };
    }
    return { state: "succeeded", quotes: parsed.slice(0, 30) };
  }

  function comparableQunarPropertyName(value) {
    return cleanText(value)
      .normalize("NFKD")
      .replace(/\p{M}/gu, "")
      .toLowerCase()
      .replace(/[·•_（）()，,。.]/g, "")
      .replace(/\s+/g, " ")
      .trim();
  }

  function qunarDetailPropertyReadback(root, expectedPropertyName) {
    const candidates = compactVisibleTexts(root, [
      "[data-tripchord-fixture='property-title']",
      "h1",
      "h2",
    ], 240, 40);
    const title = cleanText(root && root.title);
    if (title && title.length <= 240 && !candidates.includes(title)) {
      candidates.push(title);
    }
    const expected = comparableQunarPropertyName(expectedPropertyName);
    const evidence = candidates.find(
      (value) => {
        const observed = comparableQunarPropertyName(value);
        return observed === expected || observed.includes(expected);
      },
    ) || null;
    return {
      matched: Boolean(evidence),
      evidence: evidence ? sanitizeDiagnosticText(evidence).slice(0, 240) : null,
      samples: candidates.slice(0, 6)
        .map((value) => sanitizeDiagnosticText(value).slice(0, 160)),
    };
  }

  function qunarDetailLocationReadback(root) {
    const candidates = visibleNodes(root, [
      "[data-tripchord-fixture='property-address']",
      "body *",
    ], 400)
      .map((node) => visibleNodeEvidenceText(node))
      .filter((value, index, values) =>
        value && value.length <= 320 && values.indexOf(value) === index
      );
    const maafushiEvidence = candidates.find((value) =>
      /(?:马富施|马富士|maafushi)/i.test(value)
    ) || null;
    const kaafuEvidence = candidates.find((value) =>
      /(?:卡夫环礁|kaafu\s+atoll)/i.test(value)
    ) || null;
    const conflictingEvidence = candidates.find((value) =>
      /(?:胡鲁马累|hulhumal[eé]|班度士|\bbandos\b|达鲁环礁|dhaalu\s+atoll)/i.test(value)
    ) || null;
    const evidence = maafushiEvidence && kaafuEvidence && !conflictingEvidence
      ? `${sanitizeDiagnosticText(maafushiEvidence).slice(0, 150)} | ` +
        sanitizeDiagnosticText(kaafuEvidence).slice(0, 150)
      : null;
    return {
      matched: Boolean(evidence),
      maafushi_confirmed: Boolean(maafushiEvidence && !conflictingEvidence),
      kaafu_confirmed: Boolean(kaafuEvidence && !conflictingEvidence),
      evidence: evidence
        ? evidence.slice(0, 320)
        : null,
      samples: candidates
        .filter((value) =>
          /(?:马富施|马富士|maafushi|卡夫环礁|kaafu)/i.test(value)
        )
        .slice(0, 6)
        .map((value) => sanitizeDiagnosticText(value).slice(0, 160)),
    };
  }

  function qunarDetailOccupancyReadback(root, query) {
    if (query.adults !== 2 || query.rooms !== 1) {
      return { matched: false, evidence: null, samples: [] };
    }
    const candidates = compactVisibleTexts(root, [
      "[data-tripchord-fixture='occupancy-readback']",
      "input",
      "button",
      "span",
      "p",
      "div",
    ], 240, 400);
    const combined = candidates.find((value) =>
      /(?:成人\s*2(?:\D|$)|(?:^|\D)2\s*(?:位\s*)?成人(?:\D|$))/.test(value) &&
      /(?:儿童\s*0|0\s*(?:位\s*)?儿童|无儿童)/.test(value) &&
      /(?:每间人数|每房人数|occupancy|guests?\s+per\s+room|(?:^|\D)1\s*间(?:房|客房)?(?:\D|$))/i.test(value) &&
      !/(?:^|\D)[2-9]\s*间(?:房|客房)?(?:\D|$)/.test(value)
    ) || null;
    return {
      matched: Boolean(combined),
      evidence: combined
        ? sanitizeDiagnosticText(combined).slice(0, 240)
        : null,
      samples: candidates
        .filter((value) =>
          /(?:成人|儿童|间房|客房|每间人数|adult|child|room)/i.test(value)
        )
        .slice(0, 8)
        .map((value) => sanitizeDiagnosticText(value).slice(0, 160)),
    };
  }

  function qunarDiagnosticQueryAll(root, selector) {
    if (!root || typeof root.querySelectorAll !== "function") {
      return [];
    }
    try {
      return [...root.querySelectorAll(selector)];
    } catch (_error) {
      return [];
    }
  }

  function qunarDiagnosticMetadata(node) {
    if (!node || typeof node.getAttribute !== "function") {
      return "";
    }
    return cleanText([
      node.getAttribute("id"),
      node.getAttribute("class"),
      node.getAttribute("role"),
      node.getAttribute("aria-label"),
      node.getAttribute("data-testid"),
      node.getAttribute("data-module"),
      node.getAttribute("data-component"),
      node.getAttribute("data-tripchord-fixture"),
    ].filter(Boolean).join(" "));
  }

  function qunarDiagnosticPrivateRegion(node, boundary = null) {
    for (
      let candidate = node;
      candidate && candidate.nodeType === 1;
      candidate = candidate.parentElement
    ) {
      const tag = cleanText(candidate.tagName).toLowerCase();
      if (["body", "html"].includes(tag)) {
        break;
      }
      if (["aside", "footer", "header", "nav"].includes(tag)) {
        return true;
      }
      if (
        QUNAR_DIAGNOSTIC_PRIVATE_REGION_PATTERN.test(
          qunarDiagnosticMetadata(candidate),
        )
      ) {
        return true;
      }
      if (candidate === boundary) {
        break;
      }
    }
    return false;
  }

  function qunarDiagnosticSafeText(node, scope, maxChars = 5000) {
    const parts = [];
    for (const candidate of [
      node,
      ...qunarDiagnosticQueryAll(node, "*"),
    ]) {
      if (
        parts.join(" ").length >= maxChars ||
        !visibleEvidence(candidate) ||
        qunarDiagnosticPrivateRegion(candidate, scope)
      ) {
        continue;
      }
      const text = directVisibleNodeText(candidate);
      if (text) {
        parts.push(text);
      }
    }
    return cleanText(parts.join(" ")).slice(0, maxChars);
  }

  function qunarDiagnosticContains(container, candidate) {
    return Boolean(
      container &&
      candidate &&
      (container === candidate ||
        (typeof container.contains === "function" &&
          container.contains(candidate))),
    );
  }

  function qunarDiagnosticScopeInfo(root, rateRows = []) {
    const mainScopes = [];
    for (const selector of QUNAR_LODGING_DETAIL_MAIN_SCOPE_SELECTORS) {
      for (const candidate of qunarDiagnosticQueryAll(root, selector)) {
        if (
          !visibleEvidence(candidate) ||
          qunarDiagnosticPrivateRegion(candidate)
        ) {
          continue;
        }
        addMinimalContainer(mainScopes, candidate);
      }
    }
    const rateScopes = [];
    for (const selector of QUNAR_LODGING_DETAIL_RATE_SCOPE_SELECTORS) {
      for (const candidate of qunarDiagnosticQueryAll(root, selector)) {
        if (
          !visibleEvidence(candidate) ||
          qunarDiagnosticPrivateRegion(candidate)
        ) {
          continue;
        }
        addMinimalContainer(rateScopes, candidate);
      }
    }
    for (const candidate of Array.isArray(rateRows) ? rateRows : []) {
      if (
        !visibleEvidence(candidate) ||
        qunarDiagnosticPrivateRegion(candidate) ||
        !mainScopes.some((scope) =>
          qunarDiagnosticContains(scope, candidate)
        )
      ) {
        continue;
      }
      addMinimalContainer(rateScopes, candidate);
    }
    if (rateScopes.length) {
      return {
        scope: "qunar_lodging_rate_candidates_only",
        trusted_scope_found: true,
        scopes: rateScopes,
      };
    }
    if (mainScopes.length) {
      return {
        scope: "qunar_lodging_detail_main_content_only",
        trusted_scope_found: true,
        scopes: mainScopes,
      };
    }
    return {
      scope: "qunar_lodging_detail_scope_unavailable_fail_closed",
      trusted_scope_found: false,
      scopes: [],
    };
  }

  function qunarDiagnosticScopedNodes(scopeInfo) {
    const visited = new Set();
    const nodes = [];
    let scannedNodeCount = 0;
    let excludedRegionNodeCount = 0;
    let scanTruncated = false;
    scanScopes:
    for (const scope of scopeInfo.scopes) {
      const candidates = [scope, ...qunarDiagnosticQueryAll(scope, "*")];
      for (const candidate of candidates) {
        if (visited.has(candidate)) {
          continue;
        }
        visited.add(candidate);
        if (scannedNodeCount >= MAX_VISIBLE_NODE_SCAN_NODES) {
          scanTruncated = true;
          break scanScopes;
        }
        scannedNodeCount += 1;
        if (
          !visibleEvidence(candidate) ||
          qunarDiagnosticPrivateRegion(candidate, scope)
        ) {
          excludedRegionNodeCount += 1;
          continue;
        }
        nodes.push(candidate);
      }
    }
    return {
      nodes,
      scanned_node_count: scannedNodeCount,
      excluded_region_node_count: excludedRegionNodeCount,
      scan_truncated: scanTruncated,
    };
  }

  function qunarDiagnosticScopeForNode(scopeInfo, node) {
    return scopeInfo.scopes.find((scope) =>
      qunarDiagnosticContains(scope, node)
    ) || null;
  }

  function qunarVisibleDiagnosticText(container, scope) {
    const parts = [];
    const seen = new Set();
    const textNodes = [
      container,
      ...qunarDiagnosticQueryAll(
        container,
        "h1, h2, h3, h4, p, span, strong, em, b, small, button, a, time",
      ),
    ];
    for (const item of textNodes) {
      if (
        parts.join(" ").length >= MAX_DOM_DIAGNOSTIC_TEXT_CHARS ||
        !visibleEvidence(item) ||
        qunarDiagnosticPrivateRegion(item, scope) ||
        (
          item.children &&
          item.children.length &&
          !/^(?:A|BUTTON)$/.test(item.tagName || "")
        )
      ) {
        continue;
      }
      const value = sanitizeDiagnosticText(
        qunarDiagnosticSafeText(
          item,
          scope,
          MAX_DOM_DIAGNOSTIC_TEXT_CHARS,
        ),
      );
      if (value && !seen.has(value)) {
        seen.add(value);
        parts.push(value);
      }
    }
    return sanitizeDiagnosticText(parts.join(" "));
  }

  function qunarLodgingDetailDomDiagnostics(root, rateRows = []) {
    const scopeInfo = qunarDiagnosticScopeInfo(root, rateRows);
    if (!scopeInfo.trusted_scope_found) {
      return {
        scope: scopeInfo.scope,
        trusted_scope_found: false,
        trusted_scope_count: 0,
        scanned_node_count: 0,
        excluded_region_node_count: 0,
        scan_truncated: false,
        max_candidates: MAX_DOM_DIAGNOSTIC_CANDIDATES,
        candidates: [],
        lodging_unit_evidence: [],
        result_state_evidence: [],
        truncated: false,
      };
    }
    const scoped = qunarDiagnosticScopedNodes(scopeInfo);
    const priceAnchors = scoped.nodes
      .filter((node) => {
        const tag = cleanText(node.tagName).toLowerCase();
        const text = qunarDiagnosticSafeText(
          node,
          qunarDiagnosticScopeForNode(scopeInfo, node),
          120,
        );
        return (
          ["b", "div", "em", "p", "span", "strong"].includes(tag) &&
          text.length <= 120 &&
          PRICE_ANCHOR_PATTERN.test(text)
        );
      })
      .slice(0, MAX_DOM_DIAGNOSTIC_ANCHORS);
    const actionAnchors = scoped.nodes
      .filter((node) => {
        const tag = cleanText(node.tagName).toLowerCase();
        const role = cleanText(
          typeof node.getAttribute === "function"
            ? node.getAttribute("role")
            : "",
        ).toLowerCase();
        const text = qunarDiagnosticSafeText(
          node,
          qunarDiagnosticScopeForNode(scopeInfo, node),
          80,
        );
        return (
          (["a", "button", "span"].includes(tag) || role === "button") &&
          text.length <= 80 &&
          ACTION_ANCHOR_PATTERN.test(text)
        );
      })
      .slice(0, MAX_DOM_DIAGNOSTIC_ANCHORS);
    const containers = [];
    for (const candidate of scoped.nodes) {
      const tag = cleanText(candidate.tagName).toLowerCase();
      if (!DIAGNOSTIC_CONTAINER_TAGS.has(tag)) {
        continue;
      }
      const scope = qunarDiagnosticScopeForNode(scopeInfo, candidate);
      const text = qunarVisibleDiagnosticText(candidate, scope);
      if (!text || text.length > 5000) {
        continue;
      }
      const priceHits = priceAnchors.filter((item) =>
        qunarDiagnosticContains(candidate, item)
      ).length;
      const actionHits = actionAnchors.filter((item) =>
        qunarDiagnosticContains(candidate, item)
      ).length;
      if (priceHits && actionHits) {
        addMinimalContainer(containers, candidate);
      }
    }
    const diagnosticPriceNodes = (container) => {
      const scope = qunarDiagnosticScopeForNode(scopeInfo, container);
      return scoped.nodes
        .filter((node) =>
          qunarDiagnosticContains(container, node)
        )
        .map((node) => {
          const text = qunarDiagnosticSafeText(node, scope, 1600);
          const className = diagnosticClassName(node);
          return { node, text, className };
        })
        .filter(({ node, text, className }) =>
          !qunarDiagnosticPrivateRegion(node, scope) &&
          (/(?:price|money|amount|num|room|tax)/i.test(className) ||
            /(?:¥|￥|含税|到店另付|每晚|每间|总价)/.test(text))
        )
        .slice(0, 12)
        .map(({ node, text, className }) => ({
          tag: cleanText(node.tagName).toLowerCase(),
          class: className,
          text_summary: sanitizeDiagnosticText(text).slice(0, 160),
          aria_label:
            sanitizeDiagnosticText(node.getAttribute("aria-label")).slice(0, 120) || null,
          title:
            sanitizeDiagnosticText(node.getAttribute("title")).slice(0, 120) || null,
          data_price:
            sanitizeDiagnosticText(
              node.getAttribute("data-price") || node.getAttribute("data-value"),
            ).slice(0, 80) || null,
          inline_style:
            sanitizeDiagnosticText(node.getAttribute("style")).slice(0, 160) || null,
        }));
    };
    const candidates = containers
      .slice(0, MAX_DOM_DIAGNOSTIC_CANDIDATES)
      .map((container) => {
        const scope = qunarDiagnosticScopeForNode(scopeInfo, container);
        return {
          tag: cleanText(container.tagName).toLowerCase(),
          class: diagnosticClassName(container),
          text_summary: qunarVisibleDiagnosticText(container, scope),
          price_anchor_hits: Math.min(
            MAX_DOM_DIAGNOSTIC_ANCHORS,
            priceAnchors.filter((item) =>
              qunarDiagnosticContains(container, item)
            ).length,
          ),
          action_anchor_hits: Math.min(
            MAX_DOM_DIAGNOSTIC_ANCHORS,
            actionAnchors.filter((item) =>
              qunarDiagnosticContains(container, item)
            ).length,
          ),
          price_node_diagnostics: diagnosticPriceNodes(container),
        };
      });
    const lodgingUnitEvidence = scoped.nodes
      .map((node) => ({ node, text: directVisibleNodeText(node) }))
      .filter(({ text }) =>
        text && /(?:每晚|每间|\/晚|晚均|均价|总价|合计|全程)/.test(text)
      )
      .slice(0, 12)
      .map(({ node, text }) => ({
        tag: cleanText(node.tagName).toLowerCase(),
        class: diagnosticClassName(node),
        text_summary: sanitizeDiagnosticText(text).slice(0, 180),
      }));
    const resultStateEvidence = scoped.nodes
      .map((node) => ({ node, text: directVisibleNodeText(node) }))
      .filter(({ text }) =>
        text &&
        text.length <= 240 &&
        /(?:共\s*\d+\s*家酒店|没有找到|暂无|无符合|酒店满足条件|加载|搜索中|请稍候|换个条件|筛选)/.test(text)
      )
      .slice(0, 20)
      .map(({ node, text }) => ({
        tag: cleanText(node.tagName).toLowerCase(),
        class: diagnosticClassName(node),
        text_summary: sanitizeDiagnosticText(text).slice(0, 240),
      }));
    return {
      scope: scopeInfo.scope,
      trusted_scope_found: true,
      trusted_scope_count: scopeInfo.scopes.length,
      scanned_node_count: scoped.scanned_node_count,
      excluded_region_node_count: scoped.excluded_region_node_count,
      scan_truncated: scoped.scan_truncated,
      max_candidates: MAX_DOM_DIAGNOSTIC_CANDIDATES,
      candidates,
      lodging_unit_evidence: lodgingUnitEvidence,
      result_state_evidence: resultStateEvidence,
      truncated: containers.length > candidates.length,
    };
  }

  function qunarSemanticRateRows(root) {
    const rows = visibleNodes(
      root,
      QUNAR_LODGING_DETAIL_RATE_SELECTORS,
      30,
    );
    for (const control of root.querySelectorAll("a, button, [role='button']")) {
      if (
        !visibleEvidence(control) ||
        control.disabled ||
        control.getAttribute("aria-disabled") === "true"
      ) {
        continue;
      }
      const label = cleanText(
        [
          control.textContent,
          control.getAttribute("aria-label"),
          control.getAttribute("title"),
        ].filter(Boolean).join(" "),
      );
      if (!/^(?:预订|可预订|book(?:\s+now)?)$/i.test(label)) {
        continue;
      }
      let candidate = control.parentElement;
      let depth = 0;
      while (candidate && depth < 7) {
        const tag = cleanText(candidate.tagName).toLowerCase();
        if (
          candidate === root.body ||
          candidate === root.documentElement ||
          DIAGNOSTIC_BOUNDARY_TAGS.has(tag)
        ) {
          break;
        }
        const text = cleanText(candidate.innerText || candidate.textContent);
        if (
          visibleEvidence(candidate) &&
          text.length >= 8 &&
          text.length <= 1600 &&
          completeCurrencyAmountFragments(text).length === 1 &&
          ["per_night", "total_stay"].includes(
            priceBasis("lodging", text),
          ) &&
          QUNAR_FINAL_PRICE_MARKER_PATTERN.test(text)
        ) {
          rows.push(candidate);
          break;
        }
        candidate = candidate.parentElement;
        depth += 1;
      }
    }
    return rows.filter(
      (row, index) =>
        rows.indexOf(row) === index &&
        !rows.some((other) => other !== row && row.contains(other)),
    ).slice(0, 30);
  }

  function qunarDiagnosticRateContext(node, scopeInfo, controls) {
    const scope = qunarDiagnosticScopeForNode(scopeInfo, node);
    if (!scope) {
      return false;
    }
    let candidate = node;
    let depth = 0;
    while (candidate && depth < 7) {
      if (qunarDiagnosticPrivateRegion(candidate, scope)) {
        return false;
      }
      const isBroadMainScope =
        candidate === scope &&
        cleanText(candidate.tagName).toLowerCase() === "main";
      if (!isBroadMainScope) {
        const text = cleanText(candidate.innerText || candidate.textContent);
        const metadata = qunarDiagnosticMetadata(candidate);
        const hasRateMetadata =
          /(?:^|[-_\s])(?:hotel|room|rate|price)(?:$|[-_\s])/i.test(metadata);
        const hasLodgingPriceTerms =
          ["per_night", "total_stay"].includes(
            priceBasis("lodging", text),
          ) || QUNAR_FINAL_PRICE_MARKER_PATTERN.test(text);
        const hasRateControl = controls.some(({ control, label }) =>
          qunarDiagnosticContains(candidate, control) &&
          /(?:预订|订房|查看(?:房型|价格|报价)|选择房型|book|reserve|view\s+(?:rates?|rooms?)|select\s+room)/i.test(label)
        );
        if (hasRateMetadata || hasLodgingPriceTerms || hasRateControl) {
          return true;
        }
      }
      if (candidate === scope) {
        break;
      }
      candidate = candidate.parentElement;
      depth += 1;
    }
    return false;
  }

  function qunarRateDiagnostics(root, rateRows) {
    const rows = Array.isArray(rateRows) ? rateRows : [];
    const strictAvailabilityPattern =
      /^(?:预订|可预订|book(?:\s+now)?)$/i;
    const diagnosticControlPattern =
      /(?:预订|订房|查看(?:房型|价格|报价)|选择房型|book|reserve|view\s+(?:rates?|rooms?)|select\s+room)/i;
    const scopeInfo = qunarDiagnosticScopeInfo(root, rows);
    const scoped = qunarDiagnosticScopedNodes(scopeInfo);
    const controls = scoped.nodes
      .filter((control) => {
        const tag = cleanText(control.tagName).toLowerCase();
        const role = cleanText(control.getAttribute("role")).toLowerCase();
        return ["a", "button"].includes(tag) || role === "button";
      })
      .filter((control) =>
        visibleEvidence(control) &&
        !control.disabled &&
        control.getAttribute("aria-disabled") !== "true"
      )
      .map((control) => ({
        control,
        label: cleanText([
          qunarDiagnosticSafeText(
            control,
            qunarDiagnosticScopeForNode(scopeInfo, control),
            240,
          ),
          control.getAttribute("aria-label"),
          control.getAttribute("title"),
        ].filter(Boolean).join(" ")),
      }));
    const amountNodes = scoped.nodes
      .map((node) => ({
        node,
        text: directVisibleNodeText(node),
      }))
      .filter(({ node, text }) =>
        text &&
        text.length <= 1200 &&
        completeCurrencyAmountFragments(text).length > 0 &&
        qunarDiagnosticRateContext(node, scopeInfo, controls)
      );
    const sampleKeys = new Set();
    const amountSamples = [];
    for (const { node, text } of amountNodes.sort(
      (left, right) => left.text.length - right.text.length,
    )) {
      const sample = {
        tag: cleanText(node.tagName).toLowerCase(),
        class: diagnosticClassName(node),
        text_summary: sanitizeDiagnosticText(text).slice(0, 180),
        currency_amount_fragment_count:
          completeCurrencyAmountFragments(text).length,
        price_basis: priceBasis("lodging", text),
        price_finality: lodgingPriceFinality(text),
        taxes_included: taxesIncluded(text),
      };
      const key = canonicalJson(sample);
      if (sampleKeys.has(key)) {
        continue;
      }
      sampleKeys.add(key);
      amountSamples.push(sample);
      if (amountSamples.length >= MAX_DOM_DIAGNOSTIC_CANDIDATES) {
        break;
      }
    }
    const rejectionCounts = {
      amount_not_atomic: 0,
      price_basis_missing: 0,
      final_marker_missing: 0,
      tax_inclusion_missing: 0,
      availability_control_missing: 0,
      room_text_missing: 0,
    };
    let atomicFinalRowCount = 0;
    const diagnosticRows = rows.filter((row) =>
      scopeInfo.scopes.some((scope) =>
        qunarDiagnosticContains(scope, row)
      ) &&
      !qunarDiagnosticPrivateRegion(
        row,
        qunarDiagnosticScopeForNode(scopeInfo, row),
      )
    );
    for (const row of diagnosticRows) {
      const text = cleanText(row.innerText || row.textContent);
      const fragments = completeCurrencyAmountFragments(text);
      const availability = [...row.querySelectorAll(
        "a, button, [role='button']",
      )].some((control) => {
        if (
          !visibleEvidence(control) ||
          control.disabled ||
          control.getAttribute("aria-disabled") === "true"
        ) {
          return false;
        }
        const label = cleanText([
          control.textContent,
          control.getAttribute("aria-label"),
          control.getAttribute("title"),
        ].filter(Boolean).join(" "));
        return strictAvailabilityPattern.test(label);
      });
      if (fragments.length !== 1) rejectionCounts.amount_not_atomic += 1;
      if (!["per_night", "total_stay"].includes(priceBasis("lodging", text))) {
        rejectionCounts.price_basis_missing += 1;
      }
      if (
        lodgingPriceFinality(text) !== "exact_candidate" ||
        !QUNAR_FINAL_PRICE_MARKER_PATTERN.test(text)
      ) {
        rejectionCounts.final_marker_missing += 1;
      }
      if (taxesIncluded(text) !== true) {
        rejectionCounts.tax_inclusion_missing += 1;
      }
      if (!availability) rejectionCounts.availability_control_missing += 1;
      if (!qunarDetailRoomText(row)) rejectionCounts.room_text_missing += 1;
      if (qunarAtomicFinalPriceCandidate(row)) atomicFinalRowCount += 1;
    }
    return {
      schema_version: "tripchord-qunar-rate-diagnostics-v1",
      scope: scopeInfo.scope,
      trusted_scope_found: scopeInfo.trusted_scope_found,
      trusted_scope_count: scopeInfo.scopes.length,
      scanned_node_count: scoped.scanned_node_count,
      excluded_region_node_count: scoped.excluded_region_node_count,
      scan_truncated: scoped.scan_truncated,
      rate_row_count: rows.length,
      scoped_rate_row_count: diagnosticRows.length,
      unscoped_rate_row_count: rows.length - diagnosticRows.length,
      atomic_final_rate_row_count: atomicFinalRowCount,
      strict_availability_control_count: controls.filter(({ label }) =>
        strictAvailabilityPattern.test(label)
      ).length,
      diagnostic_rate_control_count: controls.filter(({ label }) =>
        diagnosticControlPattern.test(label)
      ).length,
      diagnostic_rate_control_samples: controls
        .filter(({ label }) => diagnosticControlPattern.test(label))
        .slice(0, MAX_DOM_DIAGNOSTIC_CANDIDATES)
        .map(({ control, label }) => ({
          tag: cleanText(control.tagName).toLowerCase(),
          class: diagnosticClassName(control),
          label: sanitizeDiagnosticText(label).slice(0, 120),
        })),
      visible_currency_amount_node_count: amountNodes.length,
      visible_currency_amount_samples: amountSamples,
      rejection_counts: rejectionCounts,
    };
  }

  function qunarAtomicFinalPriceCandidate(rateRow) {
    const text = cleanText(rateRow.innerText || rateRow.textContent);
    const fragments = completeCurrencyAmountFragments(text);
    const basis = priceBasis("lodging", text);
    if (
      !text ||
      text.length > 1600 ||
      fragments.length !== 1 ||
      !["per_night", "total_stay"].includes(basis) ||
      lodgingPriceFinality(text) !== "exact_candidate" ||
      !QUNAR_FINAL_PRICE_MARKER_PATTERN.test(text) ||
      taxesIncluded(text) !== true
    ) {
      return null;
    }
    const amount = parseAmount(fragments[0]);
    if (amount === null || amount <= 0) {
      return null;
    }
    const availability = [...rateRow.querySelectorAll(
      "a, button, [role='button']",
    )].find((control) => {
      if (
        !visibleEvidence(control) ||
        control.disabled ||
        control.getAttribute("aria-disabled") === "true"
      ) {
        return false;
      }
      const label = cleanText(
        [
          control.textContent,
          control.getAttribute("aria-label"),
          control.getAttribute("title"),
        ].filter(Boolean).join(" "),
      );
      return /^(?:预订|可预订|book(?:\s+now)?)$/i.test(label);
    }) || null;
    if (!availability) {
      return null;
    }
    const currency = /(?:USD|\$)/i.test(fragments[0]) ? "USD" : "CNY";
    return {
      amount,
      currency,
      price_basis: basis,
      evidence: sanitizeDiagnosticText(text).slice(0, 1200),
      amount_evidence: fragments[0],
      availability_evidence: cleanText(availability.textContent),
    };
  }

  function qunarDetailRoomText(rateRow) {
    const candidates = compactVisibleTexts(rateRow, [
      "[data-tripchord-fixture='room-title']",
      "h3",
      "h4",
      "span",
      "div",
    ], 220, 100);
    const room = candidates.find((value) =>
      /(?:房|客房|大床|双床|套房|别墅|room|suite|villa|double|twin)/i.test(value) &&
      !PRICE_ANCHOR_PATTERN.test(value) &&
      !/^(?:预订|可预订|book(?:\s+now)?)$/i.test(value) &&
      value.length <= 120
    );
    return room ? sanitizeDiagnosticText(room).slice(0, 120) : null;
  }

  async function extractQunarLodgingDetailPage(
    root,
    pageUrl,
    capturedAt,
    query,
    driver,
  ) {
    const normalizedQuery = safeQuery(query);
    const urlContext = qunarLodgingDetailUrlContext(
      pageUrl,
      normalizedQuery,
      driver,
    );
    if (!urlContext.recognized) {
      return null;
    }
    const property = qunarDetailPropertyReadback(
      root,
      urlContext.property_name,
    );
    const location = qunarDetailLocationReadback(root);
    const stay = ctripDetailStayReadback(root, normalizedQuery);
    const occupancy = qunarDetailOccupancyReadback(root, normalizedQuery);
    const baseGates = {
      provider_detail_url: Boolean(urlContext.safe_url),
      frozen_city_slug: urlContext.city_slug === "i-ka_maafushi",
      allowlisted_hotel_seq: Boolean(urlContext.hotel_seq),
      target_matches: urlContext.target_matches === true,
      result_list_lineage_matches: urlContext.lineage_matches === true,
      url_query_matches: urlContext.url_query_matches === true,
      exact_visible_search_confirmed:
        exactLodgingQueryConfirmed(normalizedQuery, driver),
      property_name_exact_visible: property.matched === true,
      maafushi_visible: location.maafushi_confirmed === true,
      kaafu_atoll_visible: location.kaafu_confirmed === true,
      visible_stay_readback: stay.matched === true,
      visible_occupancy_readback: occupancy.matched === true,
      clicked_booking:
        driver && driver.qunar_detail_capture &&
        driver.qunar_detail_capture.clicked_booking === false,
    };
    const fail = (message, extra = {}, diagnosticRateRows = []) => ({
      state: "failed",
      quotes: [],
      failure: {
        code: "dom_drift",
        message,
        retryable: false,
        page_url: urlContext.safe_url || pageUrl,
        captured_at: capturedAt,
        details: {
          parser_version: PARSER_VERSION,
          extraction: "qunar_lodging_detail",
          // Qunar detail diagnostics never scan the body. They stay inside an
          // audited room-rate candidate or the lodging-detail main region and
          // fail closed when neither trusted boundary can be identified.
          dom_diagnostics: qunarLodgingDetailDomDiagnostics(
            root,
            diagnosticRateRows,
          ),
          gates: baseGates,
          property_samples: property.samples,
          location_samples: location.samples,
          occupancy_samples: occupancy.samples,
          url_values: urlContext.url_values || {},
          ...extra,
        },
      },
    });
    if (Object.values(baseGates).some((value) => value !== true)) {
      return fail("去哪儿酒店详情页的房源、地点、查询或只读链路证据不完整");
    }
    const rateRows = qunarSemanticRateRows(root);
    const quotes = [];
    for (const rateRow of rateRows) {
      const price = qunarAtomicFinalPriceCandidate(rateRow);
      const roomText = qunarDetailRoomText(rateRow);
      if (
        !price ||
        price.currency !== normalizedQuery.currency ||
        !roomText
      ) {
        continue;
      }
      const rowText = cleanText(rateRow.innerText || rateRow.textContent);
      const breakfastText = ctripDetailTerm(
        rowText,
        /(?:不含早餐|未含早餐|无早|\d+\s*份早餐|含早餐|含早)/,
      );
      const cancellationText = ctripDetailTerm(
        rowText,
        /(?:不可取消|不可退订|免费取消|限时取消|取消政策)/,
      );
      const details = {
        query: normalizedQuery,
        driver: driver || null,
        destination: normalizedQuery.destination,
        check_in: normalizedQuery.start_date,
        check_out: normalizedQuery.end_date,
        adults: normalizedQuery.adults,
        rooms: normalizedQuery.rooms,
        city_slug: urlContext.city_slug,
        hotel_seq: urlContext.hotel_seq,
        property_id: urlContext.property_id,
        property_name: urlContext.property_name,
        room_text: roomText,
        rate_text: price.evidence,
        location_evidence: location.evidence,
        area_text: location.evidence,
        area: "destination_island",
        area_source: "exact_visible_maafushi_kaafu",
        area_matches_expected: true,
        expected_lodging_place_key: "maafushi",
        observed_lodging_place_key: "maafushi",
        lodging_place_matches_expected: true,
        kaafu_area_confirmed: true,
        breakfast_text: breakfastText,
        breakfast_included: breakfastIncluded(breakfastText),
        cancellation_text: cancellationText,
        availability: "available",
        availability_text: price.availability_evidence,
        price_text: price.amount_evidence,
        price_unit_evidence: price.evidence,
        price_basis_source:
          "audited_qunar_lodging_detail_rate_contract",
        price_finality: "final_for_rate",
        tax_evidence: price.evidence,
        taxes_included: true,
        stay_readback_evidence: stay.evidence,
        occupancy_readback_evidence: occupancy.evidence,
        clicked_booking: false,
        extraction: "visible_dom_qunar_lodging_detail",
        page_url: urlContext.safe_url,
        transfer_text: null,
        transfer_detail_url: null,
        transfer_detail_status: null,
        transfers: [],
      };
      const evidence = canonicalJson({
        amount: String(price.amount),
        currency: price.currency,
        details,
        provider: "qunar",
        kind: "lodging",
        page_url: urlContext.safe_url,
        price_basis: price.price_basis,
        taxes_included: true,
        title: urlContext.property_name,
      });
      if (evidence.length > MAX_VISIBLE_EVIDENCE_CHARS) {
        continue;
      }
      quotes.push({
        provider: "qunar",
        kind: "lodging",
        page_url: urlContext.safe_url,
        captured_at: capturedAt,
        parser_version: PARSER_VERSION,
        visible_evidence: evidence,
        evidence_sha256: await sha256(evidence),
        currency: price.currency,
        amount: price.amount,
        price_basis: price.price_basis,
        taxes_included: true,
        title: urlContext.property_name,
        details,
      });
    }
    if (!quotes.length) {
      return fail(
        "去哪儿酒店详情页没有形成单一数值、含税、最终价和明确计价单位合同",
        {
          rate_row_count: rateRows.length,
          exact_price_row_count: 0,
          room_rate_contract: false,
          rate_diagnostics: qunarRateDiagnostics(root, rateRows),
        },
        rateRows,
      );
    }
    return { state: "succeeded", quotes: quotes.slice(0, 30) };
  }

  async function extractTongchengLodgingDetailPage(
    root,
    pageUrl,
    capturedAt,
    query,
    driver,
  ) {
    const normalizedQuery = safeQuery(query);
    const urlContext = tongchengLodgingDetailUrlContext(
      pageUrl,
      normalizedQuery,
    );
    if (!urlContext.recognized) {
      return null;
    }
    const propertyTitle =
      cleanText(root.title).slice(0, 240) || firstText(root, [
        "[class*='hotel-name']",
        "[class*='hotelName']",
        "h1",
        "h2",
      ]) || null;
    // Tongcheng reuses broad `location`/`hotel-name` class fragments in
    // unrelated widgets. The tab title is visible browser evidence and binds
    // this exact detail surface to the property and island without that drift.
    const areaText = propertyTitle;
    const areaEvidence = packageAreaEvidence(
      areaText,
      normalizedQuery,
      driver,
    );
    const placeEvidence = lodgingPlaceEvidence(
      normalizedQuery.options.expected_lodging_place_key || null,
      propertyTitle,
      null,
    );
    const disclosureNode = [...root.querySelectorAll("body *")]
      .filter((node) => visibleEvidence(node))
      .map((node) => ({
        node,
        text: cleanText(node.innerText || node.textContent),
      }))
      .filter(({ text }) =>
        text.length <= 2400 &&
        /未划线价格指当前实时预订价格/.test(text) &&
        /多间\/多晚，展示为实时均价/.test(text) &&
        /每晚具体价格以订单明细为准/.test(text)
      )
      .sort((left, right) => left.text.length - right.text.length)[0] || null;
    const expectedArea = normalizedQuery.options.expected_package_area || null;
    const baseGates = {
      provider_detail_url: Boolean(urlContext.safe_url),
      numeric_property_id: Boolean(urlContext.property_id),
      url_query_matches: urlContext.url_query_matches === true,
      exact_visible_search_confirmed:
        exactLodgingQueryConfirmed(normalizedQuery, driver),
      property_title: Boolean(propertyTitle),
      package_area_matches:
        expectedArea === null
          ? Boolean(areaEvidence.area)
          : areaEvidence.area === expectedArea,
      lodging_place_matches:
        placeEvidence.expected_key === null
          ? true
          : placeEvidence.matches_expected === true,
      visible_realtime_average_disclosure: Boolean(disclosureNode),
    };
    const fail = (message, extra = {}) => ({
      state: "failed",
      quotes: [],
      failure: {
        code: "dom_drift",
        message,
        retryable: false,
        page_url: urlContext.safe_url || pageUrl,
        captured_at: capturedAt,
        details: {
          parser_version: PARSER_VERSION,
          extraction: "tongcheng_lodging_detail",
          gates: baseGates,
          property_title_evidence: propertyTitle,
          area_evidence: areaText,
          expected_lodging_place_key: placeEvidence.expected_key,
          observed_lodging_place_key: placeEvidence.observed_key,
          url_values: urlContext.url_values || {},
          ...extra,
        },
      },
    });
    if (Object.values(baseGates).some((value) => value !== true)) {
      return fail("同程酒店详情页的查询、区域或均价说明证据不完整");
    }
    const panels = visibleNodes(root, ["div.right.mt20"], 40);
    const quotes = [];
    const seen = new Set();
    let atomicPriceRows = 0;
    let availableRows = 0;
    for (const panel of panels) {
      const priceLines = [...panel.querySelectorAll(".price-text")]
        .filter((node) => visibleEvidence(node))
        .map((node) => ({
          node,
          text: cleanText(node.innerText || node.textContent),
        }))
        .filter(({ text }) =>
          /含税\s*[/／]?\s*费/.test(text) &&
          lodgingPriceFinality(text) === "exact_candidate"
        );
      const priceLine = priceLines.find(({ text }) =>
        completeCurrencyAmountFragments(text).length >= 1
      );
      if (!priceLine) {
        continue;
      }
      const fragments = completeCurrencyAmountFragments(priceLine.text);
      const originalFragments = visibleNodes(
        priceLine.node,
        [".original-price"],
        4,
      ).flatMap((node) =>
        completeCurrencyAmountFragments(node.innerText || node.textContent)
      );
      if (
        fragments.length > 2 ||
        (fragments.length === 1 && originalFragments.includes(fragments[0]))
      ) {
        continue;
      }
      const currentFragment = fragments[fragments.length - 1];
      const amount = parseAmount(currentFragment);
      if (amount === null || amount <= 0 || /(?:USD|\$)/i.test(currentFragment)) {
        continue;
      }
      atomicPriceRows += 1;
      let rateRow = panel;
      let parent = panel.parentElement;
      let depth = 0;
      while (parent && depth < 5) {
        const text = cleanText(parent.innerText || parent.textContent);
        if (
          visibleEvidence(parent) &&
          text.length <= 3000 &&
          /预订/.test(text) &&
          text.length > cleanText(rateRow.innerText || rateRow.textContent).length
        ) {
          rateRow = parent;
        }
        parent = parent.parentElement;
        depth += 1;
      }
      const availabilityText = firstMatching(
        allText(rateRow, [
          "button",
          "a",
          "[role='button']",
          ".right-price",
        ]),
        /^(?:预订|立即预订|预订\s*在线付)$/,
      );
      if (!availabilityText || /不可|售罄|无房/.test(availabilityText)) {
        continue;
      }
      availableRows += 1;
      const rowText = cleanText(rateRow.innerText || rateRow.textContent);
      const roomText =
        firstText(rateRow, [
          "[class*='room-name']",
          "[class*='roomName']",
          "[class*='bed']",
          "h3",
          "h4",
        ]) || propertyTitle;
      const breakfastText = ctripDetailTerm(
        rowText,
        /(?:不含早餐|未含早餐|无早|\d+\s*份早餐|含早餐|含早)/,
      );
      const cancellationText = ctripDetailTerm(
        rowText,
        /(?:不可取消|不可退订|免费取消|限时取消|取消政策)/,
      );
      const atPropertyText = ctripDetailTerm(
        rowText,
        /(?:到店另付约\s*(?:¥|￥)\s*\d+(?:\.\d{1,2})?|需到店另付税费)/,
      );
      const atPropertyAmount = atPropertyText
        ? parseAmount(atPropertyText)
        : null;
      const dedupeKey = canonicalJson({
        amount,
        at_property: atPropertyText,
        room: roomText,
      });
      if (seen.has(dedupeKey)) {
        continue;
      }
      seen.add(dedupeKey);
      const disclosureEvidence = disclosureNode.text.slice(0, 900);
      const visibleTerms = [
        priceLine.text,
        atPropertyText,
        breakfastText,
        cancellationText,
        availabilityText,
        disclosureEvidence,
      ].filter(Boolean);
      const taxesIncludedValue = atPropertyText ? false : true;
      const details = {
        query: normalizedQuery,
        driver: driver || null,
        destination: normalizedQuery.destination,
        check_in: normalizedQuery.start_date,
        check_out: normalizedQuery.end_date,
        adults: normalizedQuery.adults,
        rooms: normalizedQuery.rooms,
        property_id: urlContext.property_id,
        property_name: propertyTitle,
        room_text: roomText,
        rate_text: rowText.slice(0, 2400),
        area_text: areaText,
        area: areaEvidence.area,
        area_source: areaEvidence.source,
        area_matches_expected: true,
        expected_lodging_place_key: placeEvidence.expected_key,
        observed_lodging_place_key: placeEvidence.observed_key,
        lodging_place_matches_expected:
          placeEvidence.expected_key === null
            ? null
            : placeEvidence.matches_expected,
        breakfast_text: breakfastText,
        breakfast_included: breakfastIncluded(breakfastText),
        cancellation_text: cancellationText,
        availability: "available",
        availability_text: availabilityText,
        price_text: priceLine.text,
        original_price_text: originalFragments.join(" ") || null,
        current_price_text: currentFragment,
        price_unit_evidence: disclosureEvidence,
        price_basis_source: "visible_tongcheng_realtime_average_disclosure",
        price_finality: "final_for_rate",
        tax_evidence: atPropertyText || priceLine.text,
        mandatory_at_property_text: atPropertyText,
        mandatory_at_property_amount: atPropertyAmount,
        visible_terms: visibleTerms,
        extraction: "visible_dom_tongcheng_lodging_detail",
        page_url: urlContext.safe_url,
        transfer_text: null,
        transfer_detail_url: null,
        transfer_detail_status: null,
        transfers: [],
      };
      const evidence = canonicalJson({
        amount: String(amount),
        currency: "CNY",
        details,
        provider: "tongcheng",
        kind: "lodging",
        page_url: urlContext.safe_url,
        price_basis: "per_night",
        taxes_included: taxesIncludedValue,
        title: propertyTitle,
      });
      if (evidence.length > MAX_VISIBLE_EVIDENCE_CHARS) {
        continue;
      }
      quotes.push({
        provider: "tongcheng",
        kind: "lodging",
        page_url: urlContext.safe_url,
        captured_at: capturedAt,
        parser_version: PARSER_VERSION,
        visible_evidence: evidence,
        evidence_sha256: await sha256(evidence),
        currency: "CNY",
        amount,
        price_basis: "per_night",
        taxes_included: taxesIncludedValue,
        title: propertyTitle,
        details,
      });
    }
    if (!quotes.length) {
      return fail(
        "同程酒店详情页没有形成实时均价、可预订状态和税费边界完整的报价行",
        {
          dom_diagnostics: domDriftDiagnostics(root),
          rate_panel_count: panels.length,
          atomic_price_row_count: atomicPriceRows,
          available_rate_row_count: availableRows,
        },
      );
    }
    return { state: "succeeded", quotes: quotes.slice(0, 30) };
  }

  function transferAreaMentions(text) {
    const pattern =
      /胡鲁马累|hulhumal[eé]|机场岛|airport\s+island|维拉纳国际机场|马累国际机场|velana\s+international\s+airport|mal[eé]\s+airport|马累机场|机场|airport|马富施(?:岛)?|maafushi(?:\s+island)?|班度士(?:岛)?|bandos(?:\s+island)?|目的地岛|destination\s+island|度假岛|resort\s+island/gi;
    const mentions = [];
    for (const match of cleanText(text).matchAll(pattern)) {
      const value = match[0];
      let area = "destination_island";
      if (/胡鲁马累|hulhumal[eé]|机场岛|airport\s+island/i.test(value)) {
        area = "airport_island";
      } else if (
        /维拉纳国际机场|马累国际机场|velana|mal[eé]\s+airport|马累机场|机场|airport/i.test(
          value,
        )
      ) {
        area = "airport";
      }
      if (!mentions.length || mentions[mentions.length - 1].area !== area) {
        mentions.push({ area, label: value, index: match.index });
      }
    }
    return mentions;
  }

  function transferDirection(text) {
    const value = cleanText(text);
    const mentions = transferAreaMentions(value);
    if (mentions.length < 2 || mentions[0].area === mentions[1].area) {
      return null;
    }
    const roundTrip =
      /往返|双向|round[\s-]?trip|return\s+transfer|↔|⇄/i.test(value);
    const oneWay = /单程|one[\s-]?way|→|->|(?:至|到)/i.test(value);
    if (!roundTrip && !oneWay) {
      return null;
    }
    return {
      scope: roundTrip ? "round_trip" : "one_way",
      origin_area: mentions[0].area,
      destination_area: mentions[1].area,
      evidence: value,
    };
  }

  function transferPrice(text) {
    const value = cleanText(text);
    const excludedTax =
      /未含税|不含税|税费另付|tax(?:es)?\s+(?:not\s+included|excluded)/i.test(value);
    const includedTax =
      !excludedTax &&
      /含税|税费已含|tax(?:es)?\s+included|all\s+tax(?:es)?/i.test(value);
    const prefixed = value.match(
      /(?:CNY|RMB|USD|¥|￥|\$)\s*([0-9]+(?:,[0-9]{3})*(?:\.[0-9]{1,2})?)/i,
    );
    const suffixed = value.match(
      /([0-9]+(?:,[0-9]{3})*(?:\.[0-9]{1,2})?)\s*(CNY|RMB|USD)/i,
    );
    const currencyToken = prefixed
      ? value.match(/CNY|RMB|USD|¥|￥|\$/i)[0]
      : suffixed && suffixed[2];
    const amountText = prefixed ? prefixed[1] : suffixed && suffixed[1];
    const currency =
      currencyToken && /USD|\$/i.test(currencyToken) ? "USD" :
        currencyToken ? "CNY" : null;
    const amount = amountText
      ? Number(amountText.replaceAll(",", ""))
      : null;
    let priceBasis = null;
    if (/总价|合计|全程|total(?:\s+party)?/i.test(value)) {
      priceBasis = "total_party";
    } else if (/每人|\/人|成人|per\s+(?:person|adult)/i.test(value)) {
      priceBasis = "per_person";
    }
    return {
      currency,
      amount: Number.isFinite(amount) ? amount : null,
      price_basis: priceBasis,
      taxes_included: includedTax ? true : excludedTax ? false : null,
      evidence: value,
    };
  }

  function transferDurationMinutes(text) {
    const value = cleanText(text);
    const minutes = value.match(
      /(?:单程|车程|船程|时长|duration)?\s*(\d{1,3})\s*(?:分钟|mins?|minutes?)/i,
    );
    if (minutes) {
      const result = Number(minutes[1]);
      return result > 0 && result <= 1440 ? result : null;
    }
    const hours = value.match(
      /(?:单程|车程|船程|时长|duration)?\s*(\d{1,2}(?:\.\d)?)\s*(?:小时|hours?|hrs?)/i,
    );
    if (!hours) {
      return null;
    }
    const result = Number(hours[1]) * 60;
    return Number.isInteger(result) && result > 0 && result <= 1440
      ? result
      : null;
  }

  function transferTimezoneOffset(text) {
    const match = cleanText(text).match(
      /(?:UTC|GMT)\s*([+-])\s*(\d{1,2})(?::?(\d{2}))?/i,
    );
    if (!match) {
      return null;
    }
    const hours = Number(match[2]);
    const minutes = Number(match[3] || 0);
    if (hours > 14 || minutes > 59) {
      return null;
    }
    return `${match[1]}${String(hours).padStart(2, "0")}:${String(minutes).padStart(2, "0")}`;
  }

  function transferWindow(text) {
    const value = cleanText(text);
    const offset = transferTimezoneOffset(value);
    if (!offset) {
      return null;
    }
    if (/24\s*(?:小时|h)|24\s*\/\s*7|全天/i.test(value)) {
      return {
        start: "00:00",
        end: "23:59",
        offset,
        operates_24_hours: true,
      };
    }
    const range = value.match(
      /(\d{1,2}):(\d{2})\s*(?:-|–|—|至|到|~)\s*(\d{1,2}):(\d{2})/,
    );
    if (!range || !/每日|每天|daily|every\s+day/i.test(value)) {
      return null;
    }
    const startHour = Number(range[1]);
    const startMinute = Number(range[2]);
    const endHour = Number(range[3]);
    const endMinute = Number(range[4]);
    if (
      startHour > 23 ||
      endHour > 23 ||
      startMinute > 59 ||
      endMinute > 59
    ) {
      return null;
    }
    const start = `${String(startHour).padStart(2, "0")}:${String(startMinute).padStart(2, "0")}`;
    const end = `${String(endHour).padStart(2, "0")}:${String(endMinute).padStart(2, "0")}`;
    if (end < start) {
      return null;
    }
    return { start, end, offset, operates_24_hours: false };
  }

  function transferReservation(text) {
    const value = cleanText(text);
    if (/无需预约|免预约|no\s+reservation/i.test(value)) {
      return false;
    }
    if (/需(?:要)?(?:提前)?预约|提前预订|reservation\s+required/i.test(value)) {
      return true;
    }
    return null;
  }

  function transferPurchaseScope(text) {
    const value = cleanText(text);
    if (
      /可(?:单独|独立)预订|无需入住|非住客可订|公共接驳|independent(?:ly)?\s+book|no\s+hotel\s+stay|public\s+transfer/i.test(
        value,
      )
    ) {
      return "public_independent";
    }
    return "hotel_bound";
  }

  function transferDatetimes(node) {
    if (!node || typeof node.querySelectorAll !== "function") {
      return [];
    }
    return [...node.querySelectorAll("time[datetime], [data-transfer-datetime]")]
      .filter(visibleEvidence)
      .map(
        (item) =>
          item.getAttribute("datetime") ||
          item.getAttribute("data-transfer-datetime"),
      )
      .map(cleanText)
      .filter(Boolean);
  }

  function transferContractsFromEvidence(text, datetimes, query, detailUrl) {
    const value = cleanText(text);
    const direction = transferDirection(value);
    const price = transferPrice(value);
    const duration = transferDurationMinutes(value);
    const visibleTimes = Array.isArray(datetimes)
      ? datetimes.map(cleanText).filter(Boolean)
      : [];
    const window = transferWindow(value);
    const dates =
      direction && direction.scope === "round_trip"
        ? [query.start_date || null, query.end_date || null]
        : [query.start_date || null];
    const directions = direction
      ? [
          [direction.origin_area, direction.destination_area],
          ...(
            direction.scope === "round_trip"
              ? [[direction.destination_area, direction.origin_area]]
              : []
          ),
        ]
      : [[null, null]];
    return directions.map(([originArea, destinationArea], index) => {
      const departAt = visibleTimes[index * 2] || null;
      const arriveAt = visibleTimes[index * 2 + 1] || null;
      const serviceDate =
        (departAt && departAt.slice(0, 10)) || dates[index] || null;
      const scheduleMode =
        departAt && arriveAt
          ? "exact_departure"
          : window && serviceDate
            ? "service_window"
            : null;
      return {
        currency: price.currency,
        taxes_included: price.taxes_included,
        tax_evidence: value,
        price_basis: price.price_basis,
        price_scope: direction && direction.scope,
        amount: price.amount,
        price_evidence: price.evidence,
        price_contract_key: value,
        origin_area: originArea,
        destination_area: destinationArea,
        direction_evidence: direction && direction.evidence,
        schedule_mode: scheduleMode,
        service_date: serviceDate,
        duration_minutes: duration,
        schedule_evidence: value,
        depart_at: scheduleMode === "exact_departure" ? departAt : null,
        arrive_at: scheduleMode === "exact_departure" ? arriveAt : null,
        service_window_start_at:
          scheduleMode === "service_window"
            ? `${serviceDate}T${window.start}:00${window.offset}`
            : null,
        service_window_end_at:
          scheduleMode === "service_window"
            ? `${serviceDate}T${window.end}:00${window.offset}`
            : null,
        operates_24_hours:
          scheduleMode === "service_window"
            ? window.operates_24_hours
            : scheduleMode === "exact_departure"
              ? false
              : null,
        requires_reservation: transferReservation(value),
        purchase_scope: transferPurchaseScope(value),
        purchase_scope_evidence: value,
        evidence_text: value,
        detail_url: detailUrl,
      };
    });
  }

  function transferEvidenceNodes(root) {
    const seen = new Set();
    const result = [];
    for (const selector of TRANSFER_CONTRACT_SELECTORS) {
      for (const node of root.querySelectorAll(selector)) {
        if (!seen.has(node) && visibleEvidence(node)) {
          const text = cleanText(node.textContent);
          if (text && /接送|机场|快艇|渡轮|船|shuttle|transfer|ferry|boat/i.test(text)) {
            seen.add(node);
            result.push(node);
          }
        }
      }
    }
    return result.slice(0, 6);
  }

  function rawTransferContracts(root, query, detailUrl, fallbackText = null) {
    const nodes = transferEvidenceNodes(root);
    if (nodes.length) {
      return nodes.flatMap((node) =>
        transferContractsFromEvidence(
          node.textContent,
          transferDatetimes(node),
          query,
          detailUrl,
        )
      );
    }
    if (fallbackText) {
      return transferContractsFromEvidence(
        fallbackText,
        [],
        query,
        detailUrl,
      );
    }
    return [];
  }

  async function sealTransferContracts(contracts) {
    const sealed = [];
    for (const contract of contracts) {
      const evidenceSha256 = await sha256(
        JSON.stringify({
          detail_url: contract.detail_url,
          evidence_text: contract.evidence_text,
        }),
      );
      sealed.push({
        ...contract,
        price_contract_key: evidenceSha256,
        evidence_sha256: evidenceSha256,
      });
    }
    return sealed;
  }

  function visibleDatetimes(card) {
    return [...card.querySelectorAll("time[datetime], [data-datetime]")]
      .filter(visibleEvidence)
      .map((node) => node.getAttribute("datetime") || node.getAttribute("data-datetime"))
      .map(cleanText)
      .filter(Boolean)
      .slice(0, 4);
  }

  function visibleNodes(root, selectors, limit = 30) {
    const scanned = new Set();
    const nodes = [];
    for (const selector of selectors) {
      for (const node of root.querySelectorAll(selector)) {
        if (scanned.has(node)) {
          continue;
        }
        scanned.add(node);
        if (scanned.size > MAX_VISIBLE_NODE_SCAN_NODES) {
          throw domScanBudgetExceeded("visible_nodes", scanned.size);
        }
        if (!visibleEvidence(node)) {
          continue;
        }
        nodes.push(node);
        if (nodes.length >= limit) {
          return nodes;
        }
      }
    }
    return nodes;
  }

  function visibleTimeTokens(value) {
    const tokens = [];
    for (const match of cleanText(value).matchAll(
      /(?:^|[^\d])((?:[01]?\d|2[0-3]):[0-5]\d)(?=$|[^\d]|\d{1,2}\s*月)/g,
    )) {
      const normalized = match[1].padStart(5, "0");
      if (!tokens.includes(normalized)) {
        tokens.push(normalized);
      }
    }
    return tokens;
  }

  function visibleDateTokens(value, fallbackYear) {
    const tokens = [];
    const text = cleanText(value);
    const add = (year, month, day) => {
      const result =
        `${String(year).padStart(4, "0")}-` +
        `${String(month).padStart(2, "0")}-` +
        `${String(day).padStart(2, "0")}`;
      const parsed = new Date(`${result}T00:00:00Z`);
      if (
        Number.isNaN(parsed.getTime()) ||
        parsed.toISOString().slice(0, 10) !== result ||
        tokens.includes(result)
      ) {
        return;
      }
      tokens.push(result);
    };
    for (const match of text.matchAll(
      /(\d{4})\s*(?:年|[-/.])\s*(\d{1,2})\s*(?:月|[-/.])\s*(\d{1,2})\s*日?/g,
    )) {
      add(match[1], match[2], match[3]);
    }
    for (const match of text.matchAll(
      /(\d{1,2})-(\d{1,2})(?=\s+(?:[01]?\d|2[0-3]):[0-5]\d)/g,
    )) {
      add(fallbackYear, match[1], match[2]);
    }
    for (const match of text.matchAll(
      /(?:[01]?\d|2[0-3]):[0-5]\d(\d{1,2})\s*月\s*(\d{1,2})\s*日/g,
    )) {
      add(fallbackYear, match[1], match[2]);
    }
    for (const match of text.matchAll(
      /(?:^|[^\d])(\d{1,2})\s*月\s*(\d{1,2})\s*日/g,
    )) {
      add(fallbackYear, match[1], match[2]);
    }
    return tokens;
  }

  function addLocalDays(date, days) {
    const parsed = new Date(`${date}T00:00:00Z`);
    if (Number.isNaN(parsed.getTime())) {
      return null;
    }
    parsed.setUTCDate(parsed.getUTCDate() + days);
    return parsed.toISOString().slice(0, 10);
  }

  function crossDayDelta(value) {
    const text = cleanText(value);
    const explicit = text.match(/\+\s*(\d{1,2})\s*(?:天|日|day)/i);
    if (explicit) {
      const days = Number(explicit[1]);
      return Number.isInteger(days) && days >= 0 && days <= 3 ? days : null;
    }
    const compactAfterArrival = text.match(
      /(?:[01]?\d|2[0-3]):[0-5]\d\s*\+\s*([0-3])(?:\b|$)/,
    );
    if (compactAfterArrival) {
      return Number(compactAfterArrival[1]);
    }
    return /次日|翌日|隔日|next\s+day/i.test(text) ? 1 : 0;
  }

  function localIso(date, time, offset) {
    if (
      !/^\d{4}-\d{2}-\d{2}$/.test(String(date || "")) ||
      !/^(?:[01]\d|2[0-3]):[0-5]\d$/.test(String(time || "")) ||
      !/^[+-]\d{2}:\d{2}$/.test(String(offset || ""))
    ) {
      return null;
    }
    const result = `${date}T${time}:00${offset}`;
    return Number.isNaN(new Date(result).getTime()) ? null : result;
  }

  function routeTimezones(query) {
    const originCode = cleanText(query && query.origin_code).toUpperCase();
    const destinationCode = cleanText(
      query && query.destination_code,
    ).toUpperCase();
    const originOffset = FLIGHT_TIMEZONE_OFFSETS[originCode];
    const destinationOffset = FLIGHT_TIMEZONE_OFFSETS[destinationCode];
    if (!originOffset || !destinationOffset) {
      return null;
    }
    return {
      origin_code: originCode,
      destination_code: destinationCode,
      origin_offset: originOffset,
      destination_offset: destinationOffset,
    };
  }

  function legFromVisibleText(
    value,
    serviceDate,
    departureOffset,
    arrivalOffset,
  ) {
    const text = cleanText(value);
    const times = visibleTimeTokens(text);
    if (times.length < 2 || !serviceDate) {
      return null;
    }
    const year = Number(String(serviceDate).slice(0, 4));
    const dates = visibleDateTokens(text, year);
    if (dates.length && dates[0] !== serviceDate) {
      return null;
    }
    const departureDate = serviceDate;
    const delta = crossDayDelta(text);
    if (delta === null) {
      return null;
    }
    const arrivalDate = addLocalDays(departureDate, delta);
    if (dates.length > 1 && dates[1] !== arrivalDate) {
      return null;
    }
    const departureAt = localIso(
      departureDate,
      times[0],
      departureOffset,
    );
    const arrivalAt = localIso(arrivalDate, times[1], arrivalOffset);
    if (!departureAt || !arrivalAt) {
      return null;
    }
    return {
      departure_at: departureAt,
      arrival_at: arrivalAt,
      departure_local_date: departureDate,
      arrival_local_date: arrivalDate,
      departure_local_time: times[0],
      arrival_local_time: times[1],
      arrival_day_offset: delta,
      timezone_source: "audited_airport_code_mapping",
      visible_evidence: sanitizeDiagnosticText(text),
    };
  }

  function tongchengLegFromVisibleText(
    value,
    serviceDate,
    departureOffset,
    arrivalOffset,
  ) {
    const direct = legFromVisibleText(
      value,
      serviceDate,
      departureOffset,
      arrivalOffset,
    );
    if (direct) {
      return direct;
    }
    const text = cleanText(value);
    const times = visibleTimeTokens(text);
    const year = Number(String(serviceDate).slice(0, 4));
    const dates = visibleDateTokens(text, year);
    if (times.length < 2 || !serviceDate) {
      return null;
    }
    // Tongcheng's selected-outbound summary has two audited shapes:
    //   1. only the cross-day arrival date is rendered; or
    //   2. both the requested departure date and arrival date are rendered.
    // The generic parser already handled same-day and explicit +N markers.
    // Here we accept only an explicit arrival date one to three days after the
    // signed service date.  Unrelated, reversed or wider date sets still fail
    // closed.
    let explicitArrivalDate = null;
    if (dates.length === 1) {
      explicitArrivalDate = dates[0];
    } else if (dates.length === 2 && dates[0] === serviceDate) {
      explicitArrivalDate = dates[1];
    } else {
      return null;
    }
    let arrivalDayOffset = null;
    for (let days = 1; days <= 3; days += 1) {
      if (addLocalDays(serviceDate, days) === explicitArrivalDate) {
        arrivalDayOffset = days;
        break;
      }
    }
    if (arrivalDayOffset === null) {
      return null;
    }
    const departureAt = localIso(
      serviceDate,
      times[0],
      departureOffset,
    );
    const arrivalAt = localIso(
      explicitArrivalDate,
      times[1],
      arrivalOffset,
    );
    if (!departureAt || !arrivalAt) {
      return null;
    }
    return {
      departure_at: departureAt,
      arrival_at: arrivalAt,
      departure_local_date: serviceDate,
      arrival_local_date: explicitArrivalDate,
      departure_local_time: times[0],
      arrival_local_time: times[1],
      arrival_day_offset: arrivalDayOffset,
      timezone_source: "audited_airport_code_mapping",
      visible_evidence: sanitizeDiagnosticText(text),
    };
  }

  function stagedProviderLegFromVisibleText(
    provider,
    value,
    serviceDate,
    departureOffset,
    arrivalOffset,
  ) {
    return provider === "tongcheng"
      ? tongchengLegFromVisibleText(
          value,
          serviceDate,
          departureOffset,
          arrivalOffset,
        )
      : legFromVisibleText(
          value,
          serviceDate,
          departureOffset,
          arrivalOffset,
        );
  }

  // Qunar renders identifiers in the visible carrier summary (for example
  // `MU6550 ... MU235`) rather than in the airport time nodes.  An identifier
  // is evidence only; it becomes a segment below only when the card proves a
  // direct, single-flight journey.
  function qunarVisibleFlightNumbers(value) {
    const text = cleanText(value);
    const matches = [];
    const pattern = /(?:^|[^A-Za-z0-9])([A-Z]{2})\s*([0-9]{2,4})(?![A-Za-z0-9])/g;
    let match;
    while ((match = pattern.exec(text)) !== null) {
      const flightNumber = `${match[1]}${match[2]}`;
      if (!matches.includes(flightNumber)) {
        matches.push(flightNumber);
      }
      if (matches.length >= 8) {
        break;
      }
    }
    return matches;
  }

  function qunarDirectFlightSegment(
    trip,
    leg,
    flightNumbers,
    originCode,
    destinationCode,
    routeEvidence,
  ) {
    const text = cleanText(trip && trip.textContent);
    if (
      !leg ||
      !Array.isArray(flightNumbers) ||
      flightNumbers.length !== 1 ||
      !routeEvidence ||
      routeEvidence.matches_expected !== true ||
      /中转|经停|转|经由|transfer|stopover|connection/i.test(text) ||
      !/^[A-Z]{3}$/.test(cleanText(originCode).toUpperCase()) ||
      !/^[A-Z]{3}$/.test(cleanText(destinationCode).toUpperCase())
    ) {
      return [];
    }
    return [{
      flight_number: flightNumbers[0],
      departure_airport_code: cleanText(originCode).toUpperCase(),
      arrival_airport_code: cleanText(destinationCode).toUpperCase(),
      departure_at: leg.departure_at,
      arrival_at: leg.arrival_at,
    }];
  }

  function qunarRawVisibleAirportCodes(value) {
    const text = cleanText(value);
    const observations = [];
    const knownCodes = new Set(Object.keys(FLIGHT_TIMEZONE_OFFSETS));
    let unknownCodeObserved = false;
    const pattern = /(?:^|[^A-Za-z])([A-Z]{3})(?![A-Za-z])/g;
    let match;
    while ((match = pattern.exec(text)) !== null) {
      const code = match[1];
      if (!knownCodes.has(code)) {
        unknownCodeObserved = true;
        continue;
      }
      observations.push({ index: match.index, code });
    }
    // Some live cards expose the airport as an unambiguous visible name but
    // omit the IATA code.  This remains source evidence: only audited names
    // with one airport mapping are accepted, and carrier names such as
    // “新加坡航空” are explicitly excluded.  Never map a bare ambiguous city
    // (for example “北京”) to an airport.
    const visibleNames = [
      ["北京大兴国际机场", "PKX"],
      ["北京大兴机场", "PKX"],
      ["大兴机场", "PKX"],
      ["北京首都国际机场", "PEK"],
      ["首都机场", "PEK"],
      ["萧山国际机场", "HGH"],
      ["萧山机场", "HGH"],
      ["杭州萧山国际机场", "HGH"],
      ["杭州萧山", "HGH"],
      ["杭州", "HGH"],
      ["新加坡樟宜国际机场", "SIN"],
      ["新加坡樟宜", "SIN"],
      ["樟宜机场", "SIN"],
      ["樟宜", "SIN"],
      ["新加坡(?!航空)", "SIN"],
      ["韦拉纳国际机场", "MLE"],
      ["韦拉纳", "MLE"],
      ["马累", "MLE"],
    ];
    const namePattern = new RegExp(
      visibleNames.map(([name]) => name).join("|"),
      "g",
    );
    while ((match = namePattern.exec(text)) !== null) {
      const matched = match[0];
      const entry = visibleNames.find(([name]) =>
        new RegExp(`^${name}$`).test(matched),
      );
      if (entry) {
        observations.push({ index: match.index, code: entry[1] });
      }
    }
    if (unknownCodeObserved) {
      return [];
    }
    observations.sort((left, right) => left.index - right.index);
    return observations.map((observation) => observation.code);
  }

  function qunarVisibleAirportCodes(value) {
    const codes = [];
    for (const code of qunarRawVisibleAirportCodes(value)) {
      if (codes[codes.length - 1] !== code) {
        codes.push(code);
      }
      if (codes.length >= 8) {
        return codes;
      }
    }
    return codes;
  }

  // The card text often repeats its summary route around the expandable
  // detail.  Bind airport codes to the visible flight-number spans instead
  // of treating every IATA token in the whole card as one route.  A complete
  // pair for each flight is required; otherwise the caller must keep the
  // route state unknown rather than infer a transfer from noisy text.
  function qunarAirportCodesAnchoredToFlights(
    value,
    flightNumbers,
    returnEvidence = false,
  ) {
    const text = cleanText(value);
    const numbers = Array.isArray(flightNumbers) ? flightNumbers : [];
    if (!text || !numbers.length) {
      return returnEvidence ? { pairs: [], chain: [] } : [];
    }
    const observations = [];
    const rawCodes = qunarRawVisibleAirportCodes(text);
    if (!rawCodes.length) {
      return returnEvidence ? { pairs: [], chain: [] } : [];
    }
    const codePattern = /(?:^|[^A-Za-z])([A-Z]{3})(?![A-Za-z])/g;
    let codeMatch;
    while ((codeMatch = codePattern.exec(text)) !== null) {
      if (Object.prototype.hasOwnProperty.call(FLIGHT_TIMEZONE_OFFSETS, codeMatch[1])) {
        observations.push({ index: codeMatch.index, code: codeMatch[1] });
      }
    }
    const positions = [];
    let cursor = 0;
    for (const number of numbers) {
      const escaped = cleanText(number).replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
      const match = new RegExp(`(?:^|[^A-Za-z0-9])${escaped}(?![A-Za-z0-9])`).exec(
        text.slice(cursor),
      );
      if (!match) {
        return returnEvidence ? { pairs: [], chain: [] } : [];
      }
      const start = cursor + match.index;
      positions.push(start);
      cursor = start + match[0].length;
    }
    const anchored = [];
    for (let index = 0; index < positions.length; index += 1) {
      const start = positions[index];
      const end = index + 1 < positions.length ? positions[index + 1] : text.length;
      const pair = observations
        .filter((observation) => observation.index >= start && observation.index < end)
        .map((observation) => observation.code);
      if (pair.length < 2) {
        return returnEvidence ? { pairs: [], chain: [] } : [];
      }
      anchored.push(pair[0], pair[1]);
    }
    const normalized = [];
    for (const code of anchored) {
      if (normalized[normalized.length - 1] !== code) {
        normalized.push(code);
      }
    }
    if (normalized.length < numbers.length + 1) {
      return returnEvidence ? { pairs: [], chain: [] } : [];
    }
    const evidence = {
      pairs: anchored.reduce((pairs, code, index) => {
        if (index % 2 === 0) {
          pairs.push([code, anchored[index + 1]]);
        }
        return pairs;
      }, []),
      chain: normalized,
    };
    return returnEvidence ? evidence : evidence.chain;
  }

  function qunarVisibleMultiFlightSegments(
    trip,
    leg,
    flightNumbers,
    originCode,
    destinationCode,
    serviceDate,
  ) {
    const normalizedNumbers = Array.isArray(flightNumbers)
      ? flightNumbers
      : [];
    if (
      !leg ||
      normalizedNumbers.length < 2 ||
      !/^\d{4}-\d{2}-\d{2}$/.test(cleanText(serviceDate))
    ) {
      return [];
    }
    const anchoredCodes = qunarAirportCodesAnchoredToFlights(
      trip && trip.textContent,
      normalizedNumbers,
    );
    const codes = anchoredCodes.length
      ? anchoredCodes
      : qunarVisibleAirportCodes(trip && trip.textContent);
    const expectedOrigin = cleanText(originCode).toUpperCase();
    const expectedDestination = cleanText(destinationCode).toUpperCase();
    if (
      codes.length !== normalizedNumbers.length + 1 ||
      codes[0] !== expectedOrigin ||
      codes[codes.length - 1] !== expectedDestination
    ) {
      return [];
    }
    const times = visibleTimeTokens(trip && trip.textContent);
    if (times.length !== normalizedNumbers.length * 2) {
      return [];
    }
    const segments = [];
    let currentDate = serviceDate;
    let previousArrival = null;
    for (let index = 0; index < normalizedNumbers.length; index += 1) {
      const departureOffset = FLIGHT_TIMEZONE_OFFSETS[codes[index]];
      const arrivalOffset = FLIGHT_TIMEZONE_OFFSETS[codes[index + 1]];
      if (!departureOffset || !arrivalOffset) {
        return [];
      }
      let departureAt = localIso(
        currentDate,
        times[index * 2],
        departureOffset,
      );
      if (!departureAt) {
        return [];
      }
      if (previousArrival && new Date(departureAt) <= new Date(previousArrival)) {
        currentDate = addLocalDays(currentDate, 1);
        departureAt = localIso(
          currentDate,
          times[index * 2],
          departureOffset,
        );
      }
      let arrivalAt = localIso(
        currentDate,
        times[index * 2 + 1],
        arrivalOffset,
      );
      if (!arrivalAt) {
        return [];
      }
      if (new Date(arrivalAt) <= new Date(departureAt)) {
        const nextDate = addLocalDays(currentDate, 1);
        arrivalAt = localIso(nextDate, times[index * 2 + 1], arrivalOffset);
        currentDate = nextDate;
      }
      if (!arrivalAt || new Date(arrivalAt) <= new Date(departureAt)) {
        return [];
      }
      segments.push({
        flight_number: normalizedNumbers[index],
        departure_airport_code: codes[index],
        arrival_airport_code: codes[index + 1],
        departure_at: departureAt,
        arrival_at: arrivalAt,
      });
      previousArrival = arrivalAt;
    }
    if (
      segments[0].departure_at !== leg.departure_at ||
      segments[segments.length - 1].arrival_at !== leg.arrival_at
    ) {
      return [];
    }
    return segments;
  }

  function qunarSafeFlightDetailControl(trip) {
    const controls = visibleNodes(
      trip,
      ["button", "[role='button']", "a"],
      24,
    );
    const safe = controls.filter((control) => {
      const label = cleanText(
        [
          control.textContent,
          control.getAttribute("aria-label"),
          control.getAttribute("title"),
        ].filter(Boolean).join(" "),
      );
      if (
        !label ||
        !/(?:航班详情|航段详情|查看详情|展开|中转|经停)/i.test(label) ||
        /预订|选择|支付|下单|出票|购买|立即订|book|buy|pay|checkout/i.test(label) ||
        control.disabled === true ||
        control.getAttribute("disabled") !== null ||
        control.getAttribute("aria-disabled") === "true"
      ) {
        return false;
      }
      if (cleanText(control.tagName).toLowerCase() !== "a") {
        return true;
      }
      const href = cleanText(control.getAttribute("href"));
      if (!href || href.startsWith("#")) {
        return true;
      }
      try {
        const current = new URL(
          cleanText(trip && trip.ownerDocument && trip.ownerDocument.location &&
            trip.ownerDocument.location.href) || "https://flight.qunar.com/",
        );
        const target = new URL(href, current.href);
        return (
          target.origin === current.origin &&
          target.pathname === current.pathname &&
          !/[?&](?:book|order|pay|checkout)=/i.test(target.search)
        );
      } catch {
        return false;
      }
    });
    return safe.length === 1 ? safe[0] : null;
  }

  function qunarFlightNodeEvidence(trip, flightNumbers) {
    const textContent = cleanText(trip && trip.textContent);
    const innerText = cleanText(
      trip && typeof trip.innerText === "string"
        ? trip.innerText
        : textContent,
    );
    const numbers = Array.isArray(flightNumbers)
      ? flightNumbers.map((number) => cleanText(number).toUpperCase()).filter(Boolean)
      : [];
    const evidence = {
      inner_text: boundedText(innerText),
      text_content: boundedText(textContent),
      inner_text_hash: shortTextHash(innerText),
      text_content_hash: shortTextHash(textContent),
      differs: innerText !== textContent,
      nodes: [],
    };
    if (!trip || typeof trip.querySelectorAll !== "function") {
      return evidence;
    }
    let descendants = [];
    try {
      descendants = Array.from(trip.querySelectorAll("*"));
    } catch {
      descendants = [];
    }
    const candidates = [];
    for (const node of descendants.slice(0, 600)) {
      const nodeTextContent = cleanText(node && node.textContent);
      const nodeInnerText = cleanText(
        node && typeof node.innerText === "string"
          ? node.innerText
          : nodeTextContent,
      );
      const combined = `${nodeTextContent} ${nodeInnerText}`.trim();
      if (!combined) {
        continue;
      }
      const flightHits = numbers.filter((number) => combined.includes(number));
      const airportHits = qunarRawVisibleAirportCodes(combined);
      const timeHits = visibleTimeTokens(combined);
      if (!flightHits.length && !airportHits.length && !timeHits.length) {
        continue;
      }
      let hidden = false;
      let style = "";
      try {
        hidden =
          node.hidden === true ||
          node.getAttribute("hidden") !== null ||
          node.getAttribute("aria-hidden") === "true";
        style = cleanText(node.getAttribute("style"));
      } catch {
        // Some test and extension DOM shims expose no attributes.
      }
      const invisibleByStyle = /(?:display\s*:\s*none|visibility\s*:\s*hidden)/i.test(
        style,
      );
      const attributes = (name) => {
        try {
          return cleanText(node.getAttribute(name)) || null;
        } catch {
          return null;
        }
      };
      candidates.push({
        tag: cleanText(node && node.tagName).toLowerCase() || null,
        class: attributes("class"),
        role: attributes("role"),
        aria_expanded: attributes("aria-expanded"),
        hidden: hidden || invisibleByStyle,
        visible: !(hidden || invisibleByStyle),
        text: boundedText(nodeInnerText || nodeTextContent, 180),
        text_hash: shortTextHash(nodeTextContent),
        match_flights: flightHits,
        match_airports: airportHits.slice(0, 8),
        match_times: timeHits.slice(0, 8),
      });
    }
    candidates.sort((left, right) => {
      const textLength = left.text.length - right.text.length;
      if (textLength !== 0) {
        return textLength;
      }
      return (left.tag || "").localeCompare(right.tag || "");
    });
    evidence.nodes = candidates.slice(0, 48);
    return evidence;
  }

  function qunarSegmentComponents(trip) {
    if (!trip || typeof trip.querySelectorAll !== "function") {
      return [];
    }
    for (const selector of [
      ".m-tips.m-trans-tips .mgbt.segment-comp",
      ".m-tips .mgbt.segment-comp",
      ".mgbt.segment-comp",
    ]) {
      try {
        const nodes = Array.from(trip.querySelectorAll(selector));
        if (nodes.length) {
          return nodes.slice(0, 8);
        }
      } catch {
        // Continue with the next bounded selector.
      }
    }
    return [];
  }

  function qunarSegmentFlightNumbers(trip) {
    if (!trip || typeof trip.querySelectorAll !== "function") {
      return null;
    }
    const selectors = [".col-airline .d-air", ".col-airline .num"];
    for (const selector of selectors) {
      let nodes = [];
      try {
        nodes = Array.from(trip.querySelectorAll(selector));
      } catch {
        nodes = [];
      }
      if (!nodes.length) {
        continue;
      }
      const numbers = [];
      let valid = true;
      for (const node of nodes.slice(0, 8)) {
        const nodeNumbers = qunarVisibleFlightNumbers(node && node.textContent);
        if (nodeNumbers.length !== 1) {
          valid = false;
          break;
        }
        numbers.push(nodeNumbers[0]);
      }
      if (valid && numbers.length) {
        return numbers;
      }
    }
    return null;
  }

  function qunarSegmentVisibleTimes(value) {
    const times = visibleTimeTokens(value);
    for (const match of cleanText(value).matchAll(
      /\d{1,2}-\d{1,2}((?:[01]?\d|2[0-3]):[0-5]\d)/g,
    )) {
      const normalized = match[1].padStart(5, "0");
      if (!times.includes(normalized)) {
        times.push(normalized);
      }
    }
    return times;
  }

  function qunarDistinctAirportCodes(value) {
    const distinct = [];
    for (const code of qunarRawVisibleAirportCodes(value)) {
      if (distinct[distinct.length - 1] !== code) {
        distinct.push(code);
      }
    }
    return distinct;
  }

  function qunarStructuredFlightSegments(
    trip,
    flightNumbers,
    originCode,
    destinationCode,
    serviceDate,
    leg,
  ) {
    const numbers = Array.isArray(flightNumbers) ? flightNumbers : [];
    const components = qunarSegmentComponents(trip);
    if (
      !numbers.length ||
      components.length !== numbers.length ||
      !leg ||
      !/^\d{4}-\d{2}-\d{2}$/.test(cleanText(serviceDate))
    ) {
      return null;
    }
    const nodeNumbers = qunarSegmentFlightNumbers(trip);
    if (
      !nodeNumbers ||
      nodeNumbers.length !== numbers.length ||
      nodeNumbers.some((number, index) => number !== numbers[index])
    ) {
      return {
        source: "embedded_dom_detail_unbound",
        pairs: [],
        chain: [],
        segments: [],
      };
    }
    const pairs = [];
    const componentTimes = [];
    for (const component of components) {
      const codes = qunarDistinctAirportCodes(component && component.textContent);
      const times = qunarSegmentVisibleTimes(component && component.textContent);
      if (codes.length !== 2 || times.length !== 2) {
        return null;
      }
      pairs.push([codes[0], codes[1]]);
      componentTimes.push(times);
    }
    const expectedOrigin = cleanText(originCode).toUpperCase();
    const expectedDestination = cleanText(destinationCode).toUpperCase();
    if (
      pairs[0][0] !== expectedOrigin ||
      pairs[pairs.length - 1][1] !== expectedDestination
    ) {
      return null;
    }
    const segments = [];
    let currentDate = serviceDate;
    let previousArrival = null;
    for (let index = 0; index < pairs.length; index += 1) {
      const [departureCode, arrivalCode] = pairs[index];
      const departureOffset = FLIGHT_TIMEZONE_OFFSETS[departureCode];
      const arrivalOffset = FLIGHT_TIMEZONE_OFFSETS[arrivalCode];
      if (!departureOffset || !arrivalOffset) {
        return null;
      }
      let departureAt = localIso(
        currentDate,
        componentTimes[index][0],
        departureOffset,
      );
      if (!departureAt) {
        return null;
      }
      if (previousArrival && new Date(departureAt) <= new Date(previousArrival)) {
        currentDate = addLocalDays(currentDate, 1);
        departureAt = localIso(
          currentDate,
          componentTimes[index][0],
          departureOffset,
        );
      }
      let arrivalAt = localIso(
        currentDate,
        componentTimes[index][1],
        arrivalOffset,
      );
      if (!arrivalAt) {
        return null;
      }
      if (new Date(arrivalAt) <= new Date(departureAt)) {
        const nextDate = addLocalDays(currentDate, 1);
        arrivalAt = localIso(nextDate, componentTimes[index][1], arrivalOffset);
        currentDate = nextDate;
      }
      if (!arrivalAt || new Date(arrivalAt) <= new Date(departureAt)) {
        return null;
      }
      segments.push({
        flight_number: numbers[index],
        departure_airport_code: departureCode,
        arrival_airport_code: arrivalCode,
        departure_at: departureAt,
        arrival_at: arrivalAt,
      });
      previousArrival = arrivalAt;
    }
    if (
      segments[0].departure_at !== leg.departure_at ||
      segments[segments.length - 1].arrival_at !== leg.arrival_at
    ) {
      return null;
    }
    return {
      source: "embedded_dom_detail",
      pairs,
      chain: pairs.reduce((chain, pair, index) => {
        if (index === 0) {
          return [pair[0], pair[1]];
        }
        return pair[0] === chain[chain.length - 1]
          ? [...chain, pair[1]]
          : [...chain, pair[0], pair[1]];
      }, []),
      segments,
    };
  }

  function qunarReceiptSegmentsFromStructured(
    outboundStructured,
    returnStructured,
    outboundFlightNumbers,
    returnFlightNumbers,
    originCode,
    destinationCode,
  ) {
    const structuredSegmentsContinuous = (structured, numbers, origin, destination) =>
      Boolean(
        structured &&
        structured.source === "embedded_dom_detail" &&
        structured.segments.length === numbers.length &&
        structured.pairs.length === numbers.length &&
        structured.pairs[0][0] === origin &&
        structured.pairs[structured.pairs.length - 1][1] === destination &&
        structured.pairs.every(
          (pair, index) =>
            index === 0 ||
            structured.pairs[index - 1][1] === pair[0],
        ),
      );
    const valid =
      structuredSegmentsContinuous(
        outboundStructured,
        outboundFlightNumbers,
        originCode,
        destinationCode,
      ) &&
      structuredSegmentsContinuous(
        returnStructured,
        returnFlightNumbers,
        destinationCode,
        originCode,
      );
    return {
      valid,
      outbound_segments: valid ? outboundStructured.segments : [],
      return_segments: valid ? returnStructured.segments : [],
    };
  }

  function qunarDetailCandidateFingerprint(card, query) {
    const trips = visibleNodes(card, [".s-trip"], 3);
    if (trips.length !== 2) {
      return null;
    }
    const outboundFlightNumbers = qunarVisibleFlightNumbers(
      trips[0].textContent,
    );
    const returnFlightNumbers = qunarVisibleFlightNumbers(
      trips[1].textContent,
    );
    if (!outboundFlightNumbers.length || !returnFlightNumbers.length) {
      return null;
    }
    const priceEvidence = qunarPriceEvidence(card, { allowGeometry: false });
    const priceText = cleanText(priceEvidence.priceText);
    if (query && !priceText) {
      return null;
    }
    let outboundLeg = null;
    let returnLeg = null;
    let outboundRoute = null;
    let returnRoute = null;
    let outboundSegments = [];
    let returnSegments = [];
    let detailEligibility = "unknown";
    let outboundAirportCodesRaw = [];
    let outboundAirportCodesNormalized = [];
    let returnAirportCodesRaw = [];
    let returnAirportCodesNormalized = [];
    let outboundNodeEvidence = null;
    let returnNodeEvidence = null;
    let outboundAirportStructure = null;
    let returnAirportStructure = null;
    if (query) {
      const timezones = routeTimezones(query);
      if (!timezones) {
        return null;
      }
      outboundLeg = legFromQunarTrip(
        trips[0],
        query.start_date,
        timezones.origin_offset,
        timezones.destination_offset,
      );
      returnLeg = legFromQunarTrip(
        trips[1],
        query.end_date,
        timezones.destination_offset,
        timezones.origin_offset,
      );
      outboundRoute = flightLegRouteEvidence(
        trips[0].textContent,
        query,
        "outbound",
        "same_dom_detail_candidate",
        outboundLeg && outboundLeg.departure_place,
        outboundLeg && outboundLeg.arrival_place,
      );
      returnRoute = flightLegRouteEvidence(
        trips[1].textContent,
        query,
        "return",
        "same_dom_detail_candidate",
        returnLeg && returnLeg.departure_place,
        returnLeg && returnLeg.arrival_place,
      );
      if (
        !outboundLeg ||
        !returnLeg ||
        !outboundRoute ||
        outboundRoute.matches_expected !== true ||
        !returnRoute ||
        returnRoute.matches_expected !== true
      ) {
        return null;
      }
      outboundNodeEvidence = qunarFlightNodeEvidence(
        trips[0],
        outboundFlightNumbers,
      );
      returnNodeEvidence = qunarFlightNodeEvidence(
        trips[1],
        returnFlightNumbers,
      );
      outboundAirportStructure = qunarStructuredFlightSegments(
        trips[0],
        outboundFlightNumbers,
        query.origin_code,
        query.destination_code,
        query.start_date,
        outboundLeg,
      );
      returnAirportStructure = qunarStructuredFlightSegments(
        trips[1],
        returnFlightNumbers,
        query.destination_code,
        query.origin_code,
        query.end_date,
        returnLeg,
      );
      outboundSegments = outboundAirportStructure
        ? outboundAirportStructure.segments
        : qunarVisibleMultiFlightSegments(
            trips[0],
            outboundLeg,
            outboundFlightNumbers,
            query.origin_code,
            query.destination_code,
            query.start_date,
          );
      returnSegments = returnAirportStructure
        ? returnAirportStructure.segments
        : qunarVisibleMultiFlightSegments(
            trips[1],
            returnLeg,
            returnFlightNumbers,
            query.destination_code,
            query.origin_code,
            query.end_date,
          );
      const airportChainIsContinuous = (segments) =>
        Array.isArray(segments) &&
        segments.length > 0 &&
        segments.every((segment, index) =>
          index === 0 ||
          segments[index - 1].arrival_airport_code ===
            segment.departure_airport_code,
        );
      const segmentState = (
        trip,
        numbers,
        segments,
        origin,
        destination,
        structuredEvidence,
      ) => {
        const rawCodes = qunarRawVisibleAirportCodes(trip && trip.textContent);
        const airportEvidence = structuredEvidence ||
          qunarAirportCodesAnchoredToFlights(
            trip && trip.textContent,
            numbers,
            true,
          );
        const codes = airportEvidence.chain;
        const pairsComplete = airportEvidence.pairs.length === numbers.length;
        const endpointsMatch =
          pairsComplete &&
          airportEvidence.pairs[0][0] === origin &&
          airportEvidence.pairs[airportEvidence.pairs.length - 1][1] === destination;
        let state = "unknown";
        if (pairsComplete) {
          if (!endpointsMatch) {
            state = "invalid_route";
          } else {
            const explicitCrossAirport = airportEvidence.pairs.some(
              (pair, index) =>
                index > 0 &&
                airportEvidence.pairs[index - 1][1] !== pair[0],
            );
            if (explicitCrossAirport) {
              state = "known_cross_airport";
            } else if (
              codes.length === numbers.length + 1 &&
              segments.length === numbers.length &&
              airportChainIsContinuous(segments)
            ) {
              state = "known_good";
            }
          }
        }
        return {
          state,
          raw_codes: rawCodes,
          normalized_codes: codes,
        };
      };
      const outboundState = segmentState(
        trips[0],
        outboundFlightNumbers,
        outboundSegments,
        query.origin_code,
        query.destination_code,
        outboundAirportStructure,
      );
      const returnState = segmentState(
        trips[1],
        returnFlightNumbers,
        returnSegments,
        query.destination_code,
        query.origin_code,
        returnAirportStructure,
      );
      outboundAirportCodesRaw = outboundState.raw_codes;
      outboundAirportCodesNormalized = outboundState.normalized_codes;
      returnAirportCodesRaw = returnState.raw_codes;
      returnAirportCodesNormalized = returnState.normalized_codes;
      detailEligibility =
        outboundState.state === "invalid_route" ||
        returnState.state === "invalid_route"
          ? "invalid_route"
          : outboundState.state === "known_cross_airport" ||
              returnState.state === "known_cross_airport"
            ? "known_cross_airport"
          : outboundState.state === "known_good" && returnState.state === "known_good"
            ? "known_good"
            : "unknown";
    }
    const fields = {
      outbound_flight_numbers: outboundFlightNumbers,
      return_flight_numbers: returnFlightNumbers,
      outbound_depart_at: outboundLeg && outboundLeg.departure_at,
      outbound_arrive_at: outboundLeg && outboundLeg.arrival_at,
      return_depart_at: returnLeg && returnLeg.departure_at,
      return_arrive_at: returnLeg && returnLeg.arrival_at,
      price_text: priceText || null,
      ...(query
        ? {
            outbound_airport_codes_raw: outboundAirportCodesRaw,
            outbound_airport_codes_normalized: outboundAirportCodesNormalized,
            return_airport_codes_raw: returnAirportCodesRaw,
            return_airport_codes_normalized: returnAirportCodesNormalized,
            outbound_airport_evidence_source: outboundAirportStructure
              ? outboundAirportStructure.source
              : null,
            return_airport_evidence_source: returnAirportStructure
              ? returnAirportStructure.source
              : null,
            outbound_node_evidence: outboundNodeEvidence,
            return_node_evidence: returnNodeEvidence,
          }
        : {}),
      outbound_airport_chain: outboundSegments.length
        ? outboundSegments.flatMap((segment, index) =>
            index === 0
              ? [segment.departure_airport_code, segment.arrival_airport_code]
              : [segment.arrival_airport_code],
          )
        : null,
      return_airport_chain: returnSegments.length
        ? returnSegments.flatMap((segment, index) =>
            index === 0
              ? [segment.departure_airport_code, segment.arrival_airport_code]
              : [segment.arrival_airport_code],
          )
        : null,
    };
    const stableIdentity = {
      outbound_flight_numbers: outboundFlightNumbers,
      return_flight_numbers: returnFlightNumbers,
      outbound_depart_at: outboundLeg && outboundLeg.departure_at,
      outbound_arrive_at: outboundLeg && outboundLeg.arrival_at,
      return_depart_at: returnLeg && returnLeg.departure_at,
      return_arrive_at: returnLeg && returnLeg.arrival_at,
    };
    return {
      key: canonicalJson(stableIdentity),
      detail_eligibility: detailEligibility,
      ...fields,
    };
  }

  function qunarSafeExpandFlightDetail(
    root,
    direction,
    options = {},
  ) {
    if (!root || !["outbound", "return"].includes(direction)) {
      return { expanded: false, code: "invalid_detail_direction" };
    }
    const cards = visibleNodes(
      root,
      [".m-airfly-lst .b-airfly", ".b-airfly"],
      20,
    );
    const config = options && !Array.isArray(options) ? options : {};
    const query = config.query || null;
    const expectedFingerprint = config.candidate_fingerprint || null;
    let matchingTargetCardCount = 0;
    const observedCandidates = [];
    for (const card of cards) {
      const trips = visibleNodes(card, [".s-trip"], 3);
      const candidate = qunarDetailCandidateFingerprint(card, query);
      if (!candidate) {
        continue;
      }
      const trip = trips[direction === "outbound" ? 0 : 1];
      if (!trip) {
        continue;
      }
      const control = qunarSafeFlightDetailControl(trip) ||
        qunarSafeFlightDetailControl(card);
      observedCandidates.push({
        candidate_fingerprint: candidate,
        detail_eligibility: candidate.detail_eligibility,
        control_observed: Boolean(control),
      });
      if (
        candidate.detail_eligibility === "known_cross_airport" ||
        candidate.detail_eligibility === "invalid_route" ||
        expectedFingerprint &&
        (
          candidate.key !== expectedFingerprint.key ||
          (
            expectedFingerprint.price_text &&
            candidate.price_text !== expectedFingerprint.price_text
          )
        )
      ) {
        continue;
      }
      matchingTargetCardCount += 1;
      if (!control) {
        continue;
      }
      const label = cleanText(
        [
          control.textContent,
          control.getAttribute("aria-label"),
          control.getAttribute("title"),
        ].filter(Boolean).join(" "),
      );
      control.click();
      return {
        expanded: true,
        direction,
        flight_numbers: direction === "outbound"
          ? candidate.outbound_flight_numbers
          : candidate.return_flight_numbers,
        candidate_fingerprint: candidate,
        control_label: sanitizeDiagnosticText(label),
        action: {
          action: "expand_flight_detail",
          direction,
          provider: "qunar",
          flight_numbers: direction === "outbound"
            ? candidate.outbound_flight_numbers
            : candidate.return_flight_numbers,
          candidate_fingerprint: candidate,
          evidence: sanitizeDiagnosticText(label),
          read_only: true,
        },
      };
    }
    return {
      expanded: false,
      direction,
      code: matchingTargetCardCount > 0
        ? "safe_detail_control_not_found"
        : "target_card_not_found",
      inspected_card_count: cards.length,
      matching_target_card_count: matchingTargetCardCount,
      candidate_fingerprint: expectedFingerprint || null,
      observed_candidates: observedCandidates.slice(0, 20),
    };
  }

  function legFromQunarTrip(
    trip,
    serviceDate,
    departureOffset,
    arrivalOffset,
  ) {
    const departureScopeText = firstText(
      trip,
      [".col-time .sep-lf"],
    );
    const arrivalScopeText = firstText(
      trip,
      [".col-time .sep-rt"],
    );
    const departureText =
      firstText(trip, [".col-time .sep-lf h2"]) ||
      departureScopeText;
    const arrivalText =
      firstText(trip, [".col-time .sep-rt h2"]) ||
      arrivalScopeText;
    const fallbackTimes = visibleTimeTokens(trip.textContent);
    const departureTime =
      visibleTimeTokens(departureText)[0] || fallbackTimes[0];
    const arrivalTime =
      visibleTimeTokens(arrivalText)[0] || fallbackTimes[1];
    if (!departureTime || !arrivalTime) {
      return null;
    }
    const text = cleanText(trip.textContent);
    const year = Number(String(serviceDate).slice(0, 4));
    const departureDate = serviceDate;
    const delta = crossDayDelta(text);
    if (delta === null) {
      return null;
    }
    const arrivalDate = addLocalDays(departureDate, delta);
    const departureDates = visibleDateTokens(
      departureScopeText,
      year,
    );
    const arrivalDates = visibleDateTokens(arrivalScopeText, year);
    if (
      (departureDates.length && departureDates[0] !== departureDate) ||
      (arrivalDates.length && arrivalDates[0] !== arrivalDate)
    ) {
      return null;
    }
    const unscopedDates =
      !departureDates.length && !arrivalDates.length
        ? visibleDateTokens(text, year)
        : [];
    if (
      unscopedDates.length &&
      (
        unscopedDates[0] !== departureDate ||
        (
          unscopedDates.length > 1 &&
          unscopedDates[1] !== arrivalDate
        )
      )
    ) {
      return null;
    }
    const departureAt = localIso(
      departureDate,
      departureTime,
      departureOffset,
    );
    const arrivalAt = localIso(
      arrivalDate,
      arrivalTime,
      arrivalOffset,
    );
    if (!departureAt || !arrivalAt) {
      return null;
    }
    return {
      departure_at: departureAt,
      arrival_at: arrivalAt,
      departure_local_date: departureDate,
      arrival_local_date: arrivalDate,
      departure_local_time: departureTime,
      arrival_local_time: arrivalTime,
      arrival_day_offset: delta,
      departure_place:
        firstText(trip, [".col-time .sep-lf .airport"]) || null,
      arrival_place:
        firstText(trip, [".col-time .sep-rt .airport"]) || null,
      timezone_source: "audited_airport_code_mapping",
      visible_evidence: sanitizeDiagnosticText(text),
    };
  }

  function validatedActionTrace(driver) {
    const source = driver && Array.isArray(driver.action_trace)
      ? driver.action_trace
      : [];
    if (!source.length || source.length > 8) {
      return null;
    }
    const result = [];
    for (const item of source) {
      if (
        !item ||
        typeof item !== "object" ||
        Array.isArray(item) ||
        !SAFE_OUTBOUND_ACTIONS.has(item.action)
      ) {
        return null;
      }
      result.push({
        action: item.action,
        provider: cleanText(item.provider) || null,
        evidence: sanitizeDiagnosticText(item.evidence) || null,
        read_only: true,
      });
    }
    return result;
  }

  function partyAvailabilityStatus(provider, query, driver) {
    if (provider === "fliggy") {
      return "comparison_only";
    }
    const adults = Number(query && query.adults);
    const confirmedAdults = Number(
      driver && driver.confirmed_query && driver.confirmed_query.adults,
    );
    const comparison = driver && driver.party_price_comparison;
    const comparisonVerified =
      comparison &&
      comparison.schema === "tripchord.flight_party_comparison.v1" &&
      comparison.verification === "server_owned_same_product" &&
      comparison.provider === provider &&
      comparison.one_adult && comparison.one_adult.adults === 1 &&
      comparison.two_adults && comparison.two_adults.adults === 2 &&
      Number.isInteger(adults) && adults === 2 &&
      comparison.two_adult_amount === comparison.two_adults.amount;
    return (
      driver &&
      Number.isInteger(confirmedAdults) && confirmedAdults === adults
    )
      ? comparisonVerified
        ? "confirmed_for_party"
        : "observed_party_context"
      : null;
  }

  function explicitTaxEvidence(root, priceText) {
    const terms = allText(root, TERMS_SELECTORS);
    const visibleScope = cleanText(root && root.textContent).slice(0, 40000);
    if (
      taxesIncluded(`${priceText || ""} ${terms.join(" ")} ${visibleScope}`) ===
      false
    ) {
      return null;
    }
    if (taxesIncluded(priceText) === true) {
      return cleanText(priceText);
    }
    return firstMatching(terms, POSITIVE_TAX_PATTERN);
  }

  function flightAvailabilityEvidence(root) {
    const scopeText = cleanText(root && root.textContent).slice(0, 40000);
    if (FLIGHT_UNAVAILABLE_PATTERN.test(scopeText)) {
      return null;
    }
    const controls = visibleNodes(
      root,
      [
        "button",
        "a",
        "[role='button']",
        "[class*='btn']",
        "[class*='button']",
        "[class*='select']",
      ],
      40,
    );
    for (const control of controls) {
      const label = cleanText(
        [
          control.textContent,
          control.getAttribute("aria-label"),
          control.getAttribute("title"),
        ].filter(Boolean).join(" "),
      );
      if (
        !FLIGHT_AVAILABLE_CONTROL_PATTERN.test(label) ||
        control.disabled === true ||
        control.getAttribute("disabled") !== null ||
        control.getAttribute("aria-disabled") === "true"
      ) {
        continue;
      }
      return sanitizeDiagnosticText(label);
    }
    return null;
  }

  function tongchengFlightAvailabilityEvidence(root) {
    const scopeText = cleanText(root && root.textContent).slice(0, 40000);
    if (FLIGHT_UNAVAILABLE_PATTERN.test(scopeText)) {
      return null;
    }
    for (const control of visibleNodes(root, [".flight-btn"], 12)) {
      const label = cleanText(
        [
          control.textContent,
          control.getAttribute("aria-label"),
          control.getAttribute("title"),
        ].filter(Boolean).join(" "),
      );
      const href = cleanText(control.getAttribute("href")).toLowerCase();
      if (
        !/^(?:余\s*\d+\s*张\s*)?选择$/.test(label) ||
        control.disabled === true ||
        control.getAttribute("disabled") !== null ||
        control.getAttribute("aria-disabled") === "true" ||
        UNSAFE_OUTBOUND_TRANSACTION_PATTERN.test(label) ||
        /order|book|pay|checkout|预订|下单|支付/.test(href)
      ) {
        continue;
      }
      return sanitizeDiagnosticText(label);
    }
    return null;
  }

  function flightCarrierText(root) {
    return (
      firstText(root, FLIGHT_DETAIL_SELECTORS.carrier) ||
      firstText(root, [
        ".air",
        ".airways-title",
        ".carrier-name",
        ".flight-name",
      ]) ||
      null
    );
  }

  function selectedOutboundSummary(provider, root, query = null) {
    const isConfirmedSummary = (node) => {
      if (!node || !visibleEvidence(node)) {
        return false;
      }
      const text = cleanText(node.textContent);
      if (
        !text ||
        text.length > 5000 ||
        !/(?:已选去程|去程已选)/.test(text) ||
        visibleTimeTokens(text).length < 2
      ) {
        return false;
      }
      if (!query) {
        return true;
      }
      const routeEvidence = flightLegRouteEvidence(
        text,
        query,
        "outbound",
        `${provider}_selected_outbound_summary`,
      );
      return Boolean(
        routeEvidence && routeEvidence.matches_expected === true,
      );
    };
    if (provider === "tongcheng" && query) {
      const exactSummaries = [...root.querySelectorAll(".repeatChooseGo")]
        .slice(0, 8);
      for (const exactSummary of exactSummaries) {
        const title = exactSummary.querySelector(".hasChooseTitle");
        const reselect = exactSummary.querySelector(".repeatButton");
        const text = cleanText(exactSummary.textContent);
        const routeEvidence = flightLegRouteEvidence(
          text,
          query,
          "outbound",
          "tongcheng_exact_selected_outbound_summary",
        );
        if (
          visibleEvidence(title) &&
          visibleEvidence(reselect) &&
          cleanText(title.textContent) === "去程已选" &&
          cleanText(reselect.textContent) === "重选去程" &&
          text.length <= 5000 &&
          visibleTimeTokens(text).length === 2 &&
          routeEvidence &&
          routeEvidence.matches_expected === true
        ) {
          return exactSummary;
        }
      }
      const selectedControls = visibleNodes(
        root,
        [".flight-item .flight-btn.currentSlt"],
        8,
      );
      for (const control of selectedControls) {
        const card = control.closest(".flight-item");
        if (!card || !visibleEvidence(card)) {
          continue;
        }
        const text = cleanText(card.textContent);
        const routeEvidence = flightLegRouteEvidence(
          text,
          query,
          "outbound",
          "tongcheng_current_selected_outbound_card",
        );
        if (
          text.length <= 5000 &&
          visibleTimeTokens(text).length >= 2 &&
          routeEvidence &&
          routeEvidence.matches_expected === true
        ) {
          return card;
        }
      }
    }
    const selectors = provider === "fliggy"
      ? [
          ".selected-flight",
          ".selected-flight-info",
          "[class*='selected-flight']",
          "[class*='selectedFlight']",
        ]
      : [
          ".repeatChooseGo",
          ".selected-flight",
          "[class*='selected-flight']",
          "[class*='selectedFlight']",
          "[data-testid*='selected-flight']",
        ];
    const known = visibleNodes(root, selectors, 8).find(isConfirmedSummary);
    if (known) {
      return known;
    }
    const anchors = matchingVisibleNodes(
      root,
      [
        "button",
        "a",
        "[role='button']",
        "[class*='modify']",
        "[class*='change']",
        "span",
        "div",
      ].join(","),
      /(?:已选去程|去程已选|^(?:修改去程|重选去程(?:航班)?)$)/,
      240,
      20,
    );
    for (const anchor of anchors) {
      let candidate = anchor;
      let depth = 0;
      while (candidate && depth < 9) {
        const tag = cleanText(candidate.tagName).toLowerCase();
        if (
          candidate === root.body ||
          candidate === root.documentElement ||
          DIAGNOSTIC_BOUNDARY_TAGS.has(tag)
        ) {
          break;
        }
        const text = cleanText(candidate.textContent);
        if (
          DIAGNOSTIC_CONTAINER_TAGS.has(tag) &&
          isConfirmedSummary(candidate)
        ) {
          return candidate;
        }
        candidate = candidate.parentElement;
        depth += 1;
      }
    }
    return null;
  }

  function ctripStyledOutboundAction(control, query) {
    if (!control || !query || !visibleEvidence(control)) {
      return null;
    }
    const label = cleanText(control.textContent);
    if (!CTRIP_STYLED_OUTBOUND_SELECTION_PATTERN.test(label)) {
      return null;
    }
    const operate = control.closest("div.flight-operate");
    if (!operate || !visibleEvidence(operate)) {
      return null;
    }
    const exactDescendants = matchingVisibleNodes(
      operate,
      "*",
      CTRIP_STYLED_OUTBOUND_SELECTION_PATTERN,
      80,
      12,
    ).filter((candidate) => {
      const nestedExact = [...candidate.querySelectorAll("*")].some(
        (descendant) => {
          const text = cleanText(descendant.textContent);
          return (
            text.length <= 80 &&
            CTRIP_STYLED_OUTBOUND_SELECTION_PATTERN.test(text) &&
            visibleEvidence(descendant)
          );
        },
      );
      return !nestedExact;
    });
    if (exactDescendants.length !== 1 || exactDescendants[0] !== control) {
      return null;
    }
    const card = semanticFlightCardFromControl(
      "ctrip",
      control,
      query,
      "outbound",
    );
    if (!card || !card.contains(operate)) {
      return null;
    }
    let ancestry = control;
    let reachedCard = false;
    while (ancestry) {
      const interactionEvidence = cleanText(
        [
          ancestry.textContent,
          ancestry.getAttribute("id"),
          ancestry.getAttribute("aria-label"),
          ancestry.getAttribute("title"),
          ancestry.getAttribute("data-action"),
          ancestry.getAttribute("formaction"),
        ].filter(Boolean).join(" "),
      );
      if (
        ancestry.getAttribute("href") !== null ||
        ancestry.getAttribute("formaction") !== null ||
        UNSAFE_OUTBOUND_TRANSACTION_PATTERN.test(interactionEvidence)
      ) {
        return null;
      }
      if (ancestry === card) {
        reachedCard = true;
        break;
      }
      ancestry = ancestry.parentElement;
    }
    if (!reachedCard) {
      return null;
    }
    const cardText = cleanText(card.textContent);
    const serviceYear = Number(String(query.start_date || "").slice(0, 4));
    const dates = visibleDateTokens(cardText, serviceYear);
    const timezones = routeTimezones(query);
    const leg = timezones
      ? legFromVisibleText(
          cardText,
          query.start_date,
          timezones.origin_offset,
          timezones.destination_offset,
        )
      : null;
    const routeEvidence = flightLegRouteEvidence(
      cardText,
      query,
      "outbound",
      "ctrip_flight_operate_audited_card",
    );
    const carrier = ctripFlightCarrierText(card);
    const priceEvidence = ctripFlightPriceEvidence(card);
    const comparisonPrice = flightComparisonPrice(
      "ctrip",
      priceEvidence,
    );
    if (
      (
        !dates.includes(query.start_date) &&
        !ctripExactFlightSearchUrlConfirmsDate(query)
      ) ||
      !leg ||
      !routeEvidence ||
      routeEvidence.matches_expected !== true ||
      !carrier ||
      visibleFlightCurrencyAmountCount(cardText) !== 1 ||
      !comparisonPrice ||
      comparisonPrice.price_classification !== "starting_or_estimated" ||
      taxesIncluded(priceEvidence) !== true
    ) {
      return null;
    }
    return { control, operate, card };
  }

  function ctripExactFlightSearchUrlConfirmsDate(query) {
    let parsed;
    try {
      parsed = new URL(String(query && query.search_url || ""));
    } catch {
      return false;
    }
    const originCode = cleanText(query && query.origin_code).toLowerCase();
    const destinationCode =
      cleanText(query && query.destination_code).toLowerCase();
    const expectedPath =
      `/international/search/round-${originCode}-${destinationCode}`;
    const entries = [...parsed.searchParams.entries()];
    const allowedKeys = new Set([
      "depdate",
      "cabin",
      "adult",
      "child",
      "infant",
    ]);
    return Boolean(
      parsed.protocol === "https:" &&
      parsed.hostname.toLowerCase() === "flights.ctrip.com" &&
      !parsed.port &&
      !parsed.username &&
      !parsed.password &&
      !parsed.hash &&
      parsed.pathname.toLowerCase() === expectedPath &&
      entries.length === allowedKeys.size &&
      entries.every(([key]) => allowedKeys.has(key)) &&
      [...allowedKeys].every(
        (key) =>
          entries.filter(([candidate]) => candidate === key).length === 1,
      ) &&
      parsed.searchParams.get("depdate") ===
        `${query.start_date}_${query.end_date}` &&
      parsed.searchParams.get("cabin") === "y_s" &&
      parsed.searchParams.get("adult") === String(query.adults) &&
      parsed.searchParams.get("child") === "0" &&
      parsed.searchParams.get("infant") === "0"
    );
  }

  function ctripStyledOutboundControlDiagnostic(control, query) {
    const label = cleanText(control && control.textContent);
    const operate = control && control.closest("div.flight-operate");
    const exactDescendants = operate
      ? matchingVisibleNodes(
          operate,
          "*",
          CTRIP_STYLED_OUTBOUND_SELECTION_PATTERN,
          80,
          12,
        ).filter((candidate) => {
          const nestedExact = [...candidate.querySelectorAll("*")].some(
            (descendant) => {
              const text = cleanText(descendant.textContent);
              return (
                text.length <= 80 &&
                CTRIP_STYLED_OUTBOUND_SELECTION_PATTERN.test(text) &&
                visibleEvidence(descendant)
              );
            },
          );
          return !nestedExact;
        })
      : [];
    const card =
      control && query
        ? semanticFlightCardFromControl(
            "ctrip",
            control,
            query,
            "outbound",
          )
        : null;
    const unsafeAncestry = [];
    let ancestry = control;
    while (ancestry) {
      const interactionEvidence = cleanText(
        [
          ancestry.textContent,
          ancestry.getAttribute("id"),
          ancestry.getAttribute("aria-label"),
          ancestry.getAttribute("title"),
          ancestry.getAttribute("data-action"),
          ancestry.getAttribute("formaction"),
        ].filter(Boolean).join(" "),
      );
      if (
        ancestry.getAttribute("href") !== null ||
        ancestry.getAttribute("formaction") !== null ||
        UNSAFE_OUTBOUND_TRANSACTION_PATTERN.test(interactionEvidence)
      ) {
        unsafeAncestry.push({
          tag: cleanText(ancestry.tagName).toLowerCase(),
          class: diagnosticClassName(ancestry),
          has_href: ancestry.getAttribute("href") !== null,
          has_formaction:
            ancestry.getAttribute("formaction") !== null,
          evidence: sanitizeDiagnosticText(interactionEvidence),
        });
      }
      if (ancestry === card) {
        break;
      }
      ancestry = ancestry.parentElement;
    }
    const cardText = cleanText(card && card.textContent);
    const serviceYear = Number(String(query && query.start_date || "").slice(0, 4));
    const dates = visibleDateTokens(cardText, serviceYear);
    const timezones = routeTimezones(query);
    const leg =
      timezones && card
        ? legFromVisibleText(
            cardText,
            query.start_date,
            timezones.origin_offset,
            timezones.destination_offset,
          )
        : null;
    const routeEvidence =
      card
        ? flightLegRouteEvidence(
            cardText,
            query,
            "outbound",
            "ctrip_flight_operate_diagnostic",
          )
        : null;
    const carrier = card ? ctripFlightCarrierText(card) : "";
    const priceEvidence = card ? ctripFlightPriceEvidence(card) : null;
    const comparisonPrice = flightComparisonPrice(
      "ctrip",
      priceEvidence,
    );
    return {
      label,
      control_visible: Boolean(control && visibleEvidence(control)),
      operate_present: Boolean(operate),
      operate_class: operate ? diagnosticClassName(operate) : "",
      exact_descendant_count: exactDescendants.length,
      exact_descendant_is_control:
        exactDescendants.length === 1 &&
        exactDescendants[0] === control,
      semantic_card_present: Boolean(card),
      operate_within_card: Boolean(card && operate && card.contains(operate)),
      unsafe_ancestry_count: unsafeAncestry.length,
      unsafe_ancestry: unsafeAncestry.slice(0, 3),
      service_date_visible: dates.includes(query && query.start_date),
      exact_search_url_confirms_date:
        ctripExactFlightSearchUrlConfirmsDate(query),
      service_date_confirmed: Boolean(
        dates.includes(query && query.start_date) ||
        ctripExactFlightSearchUrlConfirmsDate(query),
      ),
      outbound_leg_parsed: Boolean(leg),
      outbound_route_matches: Boolean(
        routeEvidence && routeEvidence.matches_expected === true,
      ),
      carrier_present: Boolean(carrier),
      visible_currency_amount_count:
        visibleFlightCurrencyAmountCount(cardText),
      comparison_price_parsed: Boolean(comparisonPrice),
      comparison_price_is_starting: Boolean(
        comparisonPrice &&
        comparisonPrice.price_classification === "starting_or_estimated",
      ),
      explicit_tax_included: taxesIncluded(priceEvidence) === true,
    };
  }

  function auditedCtripFlightOperateControls(root, query) {
    if (!query) {
      return [];
    }
    const controls = [];
    for (const operate of root.querySelectorAll("div.flight-operate")) {
      if (controls.length >= 20 || !visibleEvidence(operate)) {
        continue;
      }
      const exactDescendants = matchingVisibleNodes(
        operate,
        "*",
        CTRIP_STYLED_OUTBOUND_SELECTION_PATTERN,
        80,
        12,
      ).filter((candidate) => {
        const nestedExact = [...candidate.querySelectorAll("*")].some(
          (descendant) => {
            const text = cleanText(descendant.textContent);
            return (
              text.length <= 80 &&
              CTRIP_STYLED_OUTBOUND_SELECTION_PATTERN.test(text) &&
              visibleEvidence(descendant)
            );
          },
        );
        return !nestedExact;
      });
      if (exactDescendants.length !== 1) {
        continue;
      }
      const audited = ctripStyledOutboundAction(
        exactDescendants[0],
        query,
      );
      if (audited && audited.operate === operate) {
        controls.push(audited.control);
      }
    }
    return controls;
  }

  function exactOutboundControls(provider, root, query = null) {
    const selectors = provider === "fliggy"
      ? [
          ".J_FlightItem .J_Btn_Select",
          ".flightItem .J_Btn_Select",
          "button",
          "[role='button']",
        ]
      : provider === "tongcheng"
        ? [
            ".flight-item .flight-btn",
            "[class*='flight-item'] [class~='flight-btn']",
            "[data-testid*='flight-card'] button",
            "button",
            "[role='button']",
          ]
        : [
          "[data-testid*='flight-card'] button",
          "[class*='flight-item'] button",
          "[class*='flightListItem'] button",
          ".flight-list-item button",
          "button",
          "[role='button']",
        ];
    const exactPattern = provider === "fliggy"
      ? /^选为去程$/
      : provider === "tongcheng"
        ? /^(?:余\s*\d+\s*张\s*)?选择$/
        : /^(?:选为去程|选择去程|选择)$/;
    const textControls = matchingVisibleNodes(
      root,
      selectors.join(","),
      exactPattern,
      80,
      40,
    );
    const exactAttributeSelectors = provider === "fliggy"
      ? [
          "[aria-label='选为去程']",
          "[title='选为去程']",
        ]
      : [
          "[aria-label='选为去程']",
          "[aria-label='选择去程']",
          "[aria-label='选择']",
          "[title='选为去程']",
          "[title='选择去程']",
          "[title='选择']",
        ];
    const attributeControls = visibleNodes(
      root,
      exactAttributeSelectors,
      40,
    );
    const styledCtripControls = provider === "ctrip"
      ? auditedCtripFlightOperateControls(root, query)
      : [];
    const controls = [
      ...new Set([
        ...textControls,
        ...attributeControls,
        ...styledCtripControls,
      ]),
    ];
    return controls.filter((node) => {
      const label = cleanText(
        [
          node.textContent,
          node.getAttribute("aria-label"),
          node.getAttribute("title"),
        ].filter(Boolean).join(" "),
      );
      if (!exactPattern.test(label)) {
        return false;
      }
      if (
        node.disabled === true ||
        node.getAttribute("disabled") !== null ||
        node.getAttribute("aria-disabled") === "true"
      ) {
        return false;
      }
      const href = cleanText(node.getAttribute("href")).toLowerCase();
      if (/order|book|pay|checkout|预订|下单|支付/.test(href)) {
        return false;
      }
      if (provider === "ctrip" && label === "选择") {
        return Boolean(
          query &&
          semanticFlightCardFromControl(
            "ctrip",
            node,
            query,
            "outbound",
          ),
        );
      }
      if (provider === "tongcheng") {
        return Boolean(
          query &&
          node.matches(".flight-btn") &&
          semanticFlightCardFromControl(
            "tongcheng",
            node,
            query,
            "outbound",
          ),
        );
      }
      return true;
    });
  }

  function knownOutboundControlCard(control) {
    return control.closest(
        ".J_FlightItem, .flightItem, [data-testid*='flight-card'], " +
        "[class*='flight-item'], [class*='flightListItem'], " +
        ".flight-list-item, article, li",
      );
  }

  function semanticFlightCardFromControl(
    provider,
    control,
    query,
    direction,
  ) {
    const timezones = routeTimezones(query);
    if (!timezones || !["outbound", "return"].includes(direction)) {
      return null;
    }
    const outbound = direction === "outbound";
    const serviceDate = outbound ? query.start_date : query.end_date;
    const departureOffset = outbound
      ? timezones.origin_offset
      : timezones.destination_offset;
    const arrivalOffset = outbound
      ? timezones.destination_offset
      : timezones.origin_offset;
    let candidate = control.parentElement;
    let depth = 0;
    while (candidate && depth < 10) {
      const tag = cleanText(candidate.tagName).toLowerCase();
      if (
        candidate === control.ownerDocument.body ||
        candidate === control.ownerDocument.documentElement ||
        DIAGNOSTIC_BOUNDARY_TAGS.has(tag)
      ) {
        break;
      }
      const text = cleanText(candidate.textContent);
      if (
        DIAGNOSTIC_CONTAINER_TAGS.has(tag) &&
        text.length <= 5000
      ) {
        const leg = stagedProviderLegFromVisibleText(
          provider,
          text,
          serviceDate,
          departureOffset,
          arrivalOffset,
        );
        const routeEvidence = flightLegRouteEvidence(
          text,
          query,
          direction,
          `${provider}_semantic_action_ancestor`,
        );
        if (
          leg &&
          routeEvidence &&
          routeEvidence.matches_expected === true &&
          visibleEvidence(candidate)
        ) {
          const carrier = provider === "ctrip"
            ? ctripFlightCarrierText(candidate)
            : flightCarrierText(candidate);
          const priceEvidence =
            provider !== "ctrip" || ctripFlightPriceEvidence(candidate);
          if (carrier && priceEvidence) {
            return candidate;
          }
        }
      }
      candidate = candidate.parentElement;
      depth += 1;
    }
    return null;
  }

  function outboundControlCard(provider, control, query) {
    const known = knownOutboundControlCard(control);
    if (known) {
      const text = cleanText(known.textContent);
      const routeEvidence = flightLegRouteEvidence(
        text,
        query,
        "outbound",
        `${provider}_known_outbound_card`,
      );
      if (
        routeEvidence &&
        routeEvidence.matches_expected === true &&
        visibleTimeTokens(text).length >= 2 &&
        flightCarrierText(known)
      ) {
        return known;
      }
    }
    return (
      semanticFlightCardFromControl(
        provider,
        control,
        query,
        "outbound",
      ) ||
      known ||
      control.parentElement
    );
  }

  async function outboundSelectionCandidates(provider, root, query) {
    const timezones = routeTimezones(query);
    if (!timezones) {
      return [];
    }
    const controls = exactOutboundControls(
      provider,
      root,
      query,
    ).slice(0, 20);
    const results = [];
    for (const [index, control] of controls.entries()) {
      const card = outboundControlCard(provider, control, query);
      const cardText = cleanText(card && card.textContent);
      const leg = stagedProviderLegFromVisibleText(
        provider,
        cardText,
        query.start_date,
        timezones.origin_offset,
        timezones.destination_offset,
      );
      const routeEvidence = flightLegRouteEvidence(
        cardText,
        query,
        "outbound",
        "outbound_candidate_card",
      );
      const carrier = card && (
        provider === "ctrip"
          ? ctripFlightCarrierText(card)
          : flightCarrierText(card)
      );
      if (
        !card ||
        !leg ||
        !routeEvidence ||
        routeEvidence.matches_expected !== true ||
        !carrier
      ) {
        continue;
      }
      const label = cleanText(control.textContent);
      const selectionEvidence = sanitizeDiagnosticText(cardText);
      const routeIdentity = {
        direction: routeEvidence.direction,
        expected_departure_code:
          routeEvidence.expected_departure_code,
        expected_arrival_code:
          routeEvidence.expected_arrival_code,
      };
      const selectionId = await sha256(
        canonicalJson({
          label,
          outbound_arrival_at: leg.arrival_at,
          outbound_departure_at: leg.departure_at,
          provider,
          route_identity: routeIdentity,
        }),
      );
      results.push({
        index,
        provider,
        label,
        selection_id: selectionId,
        carrier_text: carrier,
        outbound_departure_at: leg.departure_at,
        outbound_arrival_at: leg.arrival_at,
        outbound_route_evidence: routeEvidence,
        selection_evidence: selectionEvidence,
      });
    }
    const identityCounts = new Map();
    for (const candidate of results) {
      identityCounts.set(
        candidate.selection_id,
        (identityCounts.get(candidate.selection_id) || 0) + 1,
      );
    }
    return results
      .filter(
        (candidate) =>
          identityCounts.get(candidate.selection_id) === 1,
      )
      .slice(0, MAX_OUTBOUND_SELECTION_CANDIDATES);
  }

  async function safeSelectOutbound(provider, root, query, selectionId) {
    if (!["ctrip", "fliggy", "tongcheng"].includes(provider)) {
      return {
        selected: false,
        code: "provider_has_no_safe_outbound_stage",
      };
    }
    const candidates = await outboundSelectionCandidates(
      provider,
      root,
      query,
    );
    const candidate = candidates.find(
      (item) => item.selection_id === selectionId,
    );
    if (!candidate) {
      return {
        selected: false,
        code: "outbound_selection_evidence_changed",
        available_candidates: candidates.map((item) => ({
          selection_id: item.selection_id,
          carrier_text: item.carrier_text,
          outbound_departure_at: item.outbound_departure_at,
          outbound_arrival_at: item.outbound_arrival_at,
          expected_departure_code:
            item.outbound_route_evidence.expected_departure_code,
          expected_arrival_code:
            item.outbound_route_evidence.expected_arrival_code,
        })),
      };
    }
    const controls = exactOutboundControls(
      provider,
      root,
      query,
    ).slice(0, 20);
    const control = controls[candidate.index];
    if (!control) {
      return {
        selected: false,
        code: "safe_outbound_control_missing",
      };
    }
    // This is the only click used by the round-trip extraction workflow.
    // exactOutboundControls has already rejected booking/order/payment labels
    // and URLs. The caller supplies a signed visible candidate id, so a retry
    // cannot silently click a different card after the DOM changes.
    control.click();
    return {
      selected: true,
      confirmation_scope: "exact_visible_select_outbound",
      selection: candidate,
    };
  }

  function selectedSummaryMatchesDriver(
    provider,
    summary,
    driver,
    leg,
    visibleCarrier,
  ) {
    const selected = driver && driver.selected_outbound;
    const trace = validatedActionTrace(driver);
    if (
      !selected ||
      !trace ||
      !trace.some((item) =>
        [
          "select_outbound",
          "provider_auto_selected_outbound",
        ].includes(item.action)
      ) ||
      selected.outbound_departure_at !== leg.departure_at ||
      selected.outbound_arrival_at !== leg.arrival_at
    ) {
      return false;
    }
    const expectedCarrier = cleanText(selected.carrier_text).toLowerCase();
    const summaryText = cleanText(summary.textContent).toLowerCase();
    if (!expectedCarrier) {
      return false;
    }
    const observedCarrier = cleanText(visibleCarrier).toLowerCase();
    if (observedCarrier) {
      return Boolean(
        observedCarrier === expectedCarrier &&
        summaryText.includes(expectedCarrier),
      );
    }
    return Boolean(
      ["ctrip", "tongcheng"].includes(provider) &&
      /(?:已选去程|去程已选)/.test(summaryText) &&
      summaryText.includes(expectedCarrier) &&
      cleanText(selected.selection_evidence),
    );
  }

  function tongchengSelectedOutboundCarrier(summary) {
    const text = cleanText(summary && summary.textContent);
    const match = text.match(
      /(?:去程已选|已选去程)([\u3400-\u9fffA-Za-z·\s]{2,30}?航空)(?=[A-Z0-9]{2,8}\d)/,
    );
    return match ? cleanText(match[1]) : flightCarrierText(summary);
  }

  function tongchengAutoSelectedOutboundDriver(summary, query, driver) {
    if (
      !summary ||
      !query ||
      !driver ||
      driver.mode !== "search_url" ||
      driver.confirmation_scope !== "trusted_exact_search_url" ||
      driver.party_availability_confirmed !== true ||
      !flightReceiptConfirmedQuery(query, driver) ||
      !summary.matches(".repeatChooseGo")
    ) {
      return null;
    }
    const title = summary.querySelector(".hasChooseTitle");
    const reselect = summary.querySelector(".repeatButton");
    const titleText = cleanText(title && title.textContent);
    const reselectText = cleanText(reselect && reselect.textContent);
    const documentRoot = summary.ownerDocument;
    const returnStage = documentRoot && visibleNodes(
      documentRoot,
      [".s-trip"],
      4,
    ).find((node) => {
      const text = cleanText(node.textContent);
      const route = flightLegRouteEvidence(
        text,
        query,
        "return",
        "tongcheng_return_stage_marker",
      );
      return (
        /^选择返程[:：]/.test(text) &&
        route &&
        route.matches_expected === true
      );
    });
    if (
      titleText !== "去程已选" ||
      reselectText !== "重选去程" ||
      !returnStage
    ) {
      return null;
    }
    const timezones = routeTimezones(query);
    const summaryText = cleanText(summary.textContent);
    const leg = timezones && stagedProviderLegFromVisibleText(
      "tongcheng",
      summaryText,
      query.start_date,
      timezones.origin_offset,
      timezones.destination_offset,
    );
    const route = flightLegRouteEvidence(
      summaryText,
      query,
      "outbound",
      "tongcheng_provider_auto_selected_outbound",
    );
    const carrier = tongchengSelectedOutboundCarrier(summary);
    if (
      !leg ||
      !route ||
      route.matches_expected !== true ||
      !carrier
    ) {
      return null;
    }
    const selectionEvidence = sanitizeDiagnosticText(summaryText);
    const sourceTrace = Array.isArray(driver.action_trace)
      ? driver.action_trace.filter(
          (item) => item.action !== "provider_auto_selected_outbound",
        )
      : [];
    const returnActionIndex = sourceTrace.findIndex(
      (item) => item.action === "select_return",
    );
    const autoAction = {
      action: "provider_auto_selected_outbound",
      provider: "tongcheng",
      evidence: selectionEvidence,
    };
    const actionTrace = returnActionIndex >= 0
      ? [
          ...sourceTrace.slice(0, returnActionIndex),
          autoAction,
          ...sourceTrace.slice(returnActionIndex),
        ]
      : [...sourceTrace, autoAction];
    return {
      ...driver,
      selected_outbound: {
        carrier_text: carrier,
        outbound_departure_at: leg.departure_at,
        outbound_arrival_at: leg.arrival_at,
        selection_evidence: selectionEvidence,
        outbound_route_evidence: route,
      },
      action_trace: actionTrace,
    };
  }

  function normalizedCityToken(value) {
    return cleanText(value)
      .normalize("NFKD")
      .replace(/[\u0300-\u036f]/g, "")
      .replace(/[\s._()（）-]+/g, "")
      .toLowerCase();
  }

  function cityMatchesRequest(label, observedCode, requestedName, requestedCode) {
    const expectedCode = cleanText(requestedCode).toUpperCase();
    const aliases = new Set(
      [
        requestedName,
        expectedCode,
        ...(AUDITED_FLIGHT_CITY_ALIASES[expectedCode] || []),
      ]
        .map(normalizedCityToken)
        .filter(Boolean),
    );
    const normalizedLabel = normalizedCityToken(label);
    const normalizedObservedCode = normalizedCityToken(observedCode);
    return Boolean(
      aliases.size &&
      (
        aliases.has(normalizedLabel) ||
        (
          normalizedObservedCode &&
          aliases.has(normalizedObservedCode)
        )
      )
    );
  }

  function exactVisibleSearchCityMatches(value, requestedName, requestedCode) {
    const observed = normalizedCityToken(value);
    const name = normalizedCityToken(requestedName);
    const code = normalizedCityToken(requestedCode);
    return Boolean(
      observed &&
      (
        observed === name ||
        observed === code ||
        (name && code && (
          observed === `${name}${code}` ||
          observed === `${code}${name}`
        ))
      )
    );
  }

  function visibleLocationMatch(value, requestedName, requestedCode) {
    const text = cleanText(value);
    const lower = text.toLowerCase();
    const code = cleanText(requestedCode).toUpperCase();
    const aliases = [
      requestedName,
      code,
      ...(AUDITED_FLIGHT_CITY_ALIASES[code] || []),
    ]
      .map(cleanText)
      .filter(Boolean);
    let best = null;
    for (const alias of aliases) {
      const index = lower.indexOf(alias.toLowerCase());
      if (index < 0 || (best && best.index <= index)) {
        continue;
      }
      best = {
        index,
        label: sanitizeDiagnosticText(alias),
        observed_code:
          alias.toUpperCase() === code ? code : null,
      };
    }
    return best;
  }

  function flightLegRouteEvidence(
    value,
    query,
    direction,
    sourceScope,
    departurePlace = null,
    arrivalPlace = null,
  ) {
    if (!["outbound", "return"].includes(direction)) {
      return null;
    }
    const outbound = direction === "outbound";
    const departureName = outbound ? query.origin : query.destination;
    const departureCode = outbound
      ? query.origin_code
      : query.destination_code;
    const arrivalName = outbound ? query.destination : query.origin;
    const arrivalCode = outbound
      ? query.destination_code
      : query.origin_code;
    const visibleText = cleanText(value);
    const explicitPlaces =
      cleanText(departurePlace) && cleanText(arrivalPlace);
    const departureSource = explicitPlaces
      ? cleanText(departurePlace)
      : visibleText;
    const arrivalSource = explicitPlaces
      ? cleanText(arrivalPlace)
      : visibleText;
    const departure = visibleLocationMatch(
      departureSource,
      departureName,
      departureCode,
    );
    const arrival = visibleLocationMatch(
      arrivalSource,
      arrivalName,
      arrivalCode,
    );
    const directionOrderConfirmed = Boolean(
      departure &&
      arrival &&
      (
        explicitPlaces ||
        departure.index < arrival.index
      ),
    );
    const evidence = {
      direction,
      source_scope: cleanText(sourceScope),
      expected_departure_code:
        cleanText(departureCode).toUpperCase() || null,
      expected_arrival_code:
        cleanText(arrivalCode).toUpperCase() || null,
      observed_departure_label:
        departure && departure.label || null,
      observed_arrival_label:
        arrival && arrival.label || null,
      observed_departure_code:
        departure && departure.observed_code || null,
      observed_arrival_code:
        arrival && arrival.observed_code || null,
      departure_matches_requested: Boolean(departure),
      arrival_matches_requested: Boolean(arrival),
      direction_order_confirmed: directionOrderConfirmed,
      visible_evidence: sanitizeDiagnosticText(
        explicitPlaces
          ? `${departureSource} → ${arrivalSource}`
          : visibleText,
      ),
    };
    return {
      ...evidence,
      matches_expected:
        evidence.departure_matches_requested &&
        evidence.arrival_matches_requested &&
        evidence.direction_order_confirmed &&
        Boolean(evidence.visible_evidence),
    };
  }

  function tongchengVisibleRouteEndpointCodesMatch(value, query, direction) {
    const tokens = cleanText(value).match(/\b[A-Z]{3}\b/g) || [];
    if (tokens.length < 2) {
      return true;
    }
    const outbound = direction === "outbound";
    const expectedDeparture = cleanText(
      outbound ? query.origin_code : query.destination_code,
    ).toUpperCase();
    const expectedArrival = cleanText(
      outbound ? query.destination_code : query.origin_code,
    ).toUpperCase();
    return (
      tokens[0] === expectedDeparture &&
      tokens[tokens.length - 1] === expectedArrival
    );
  }

  function flightRouteObservation(value, query = {}) {
    const text = cleanText(value);
    const match = text.match(
      /(?:^|[\s:：])([\u3400-\u9fffA-Za-z·]{2,24})(?:\s*\(([A-Z]{3})\))?\s*(?:-|—|–|→|至)\s*([\u3400-\u9fffA-Za-z·]{2,24})(?:\s*\(([A-Z]{3})\))?/i,
    );
    if (!match) {
      return null;
    }
    const originLabel = sanitizeDiagnosticText(match[1]);
    const destinationLabel = sanitizeDiagnosticText(match[3]);
    const observedOriginCode = cleanText(match[2]).toUpperCase() || null;
    const observedDestinationCode =
      cleanText(match[4]).toUpperCase() || null;
    return {
      origin_label: originLabel,
      destination_label: destinationLabel,
      observed_origin_code: observedOriginCode,
      observed_destination_code: observedDestinationCode,
      origin_matches_requested: cityMatchesRequest(
        originLabel,
        observedOriginCode,
        query.origin,
        query.origin_code,
      ),
      destination_matches_requested: cityMatchesRequest(
        destinationLabel,
        observedDestinationCode,
        query.destination,
        query.destination_code,
      ),
    };
  }

  function priceFragmentShape(value) {
    const text = cleanText(value);
    const shapes = [];
    if (/人均含税价/.test(text)) {
      shapes.push("basis_label");
    }
    if (
      /(?:¥|￥|CNY|RMB|USD|\$)\s*(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d{1,2})?(?![\d,])/i.test(
        text,
      )
    ) {
      shapes.push("currency_amount");
    } else if (/^(?:¥|￥|CNY|RMB|USD|\$)$/i.test(text)) {
      shapes.push("currency");
    } else if (/^[\d,.]+$/.test(text)) {
      shapes.push("digits");
    } else if (/\d/.test(text)) {
      shapes.push("mixed_numeric_text");
    }
    return shapes.join("+") || "text";
  }

  function atomicPriceStructure(sourceFragments) {
    const fragments = sourceFragments
      .map(cleanText)
      .filter(Boolean)
      .slice(0, 24);
    const shapes = fragments.map(priceFragmentShape);
    const completeIndexes = [];
    let splitNumericSequenceCount = 0;
    for (const [index, shape] of shapes.entries()) {
      if (shape.includes("currency_amount")) {
        completeIndexes.push(index);
        if (shapes[index + 1] === "digits") {
          splitNumericSequenceCount += 1;
        }
      }
      if (
        shape === "currency" &&
        shapes[index + 1] === "digits"
      ) {
        splitNumericSequenceCount += 1;
      }
      if (
        shape === "digits" &&
        shapes[index + 1] === "digits"
      ) {
        splitNumericSequenceCount += 1;
      }
    }
    const safeIndex = completeIndexes.find(
      (index) => shapes[index + 1] !== "digits",
    );
    return {
      fragment_shapes: shapes,
      atomic_fragment_count: fragments.length,
      complete_currency_amount_fragment_count: completeIndexes.length,
      split_numeric_sequence_count: splitNumericSequenceCount,
      safe_amount_fragment:
        safeIndex === undefined ? null : fragments[safeIndex],
    };
  }

  function visibleAtomicTextFragments(root, limit = 24) {
    const fragments = [];
    const visit = (node) => {
      if (!node || fragments.length >= limit) {
        return;
      }
      if (node.nodeType === 3) {
        const parent = node.parentElement;
        if (!parent || visibleEvidence(parent)) {
          const text = cleanText(node.textContent);
          if (text) {
            fragments.push(text);
          }
        }
        return;
      }
      if (node.nodeType === 1 && !visibleEvidence(node)) {
        return;
      }
      for (const child of node.childNodes || []) {
        visit(child);
        if (fragments.length >= limit) {
          break;
        }
      }
    };
    visit(root);
    return fragments;
  }

  function stableTitledDigitAmount(titleValues, digitValues) {
    if (
      !Array.isArray(titleValues) ||
      !Array.isArray(digitValues) ||
      titleValues.length < 2 ||
      titleValues.length !== digitValues.length
    ) {
      return null;
    }
    const titles = titleValues.map(cleanText);
    const digits = digitValues.map(cleanText);
    const uniqueTitles = [...new Set(titles)];
    if (
      uniqueTitles.length !== 1 ||
      !/^[1-9]\d{2,6}$/.test(uniqueTitles[0]) ||
      digits.some((digit) => !/^\d$/.test(digit)) ||
      digits.join("") !== uniqueTitles[0]
    ) {
      return null;
    }
    return uniqueTitles[0];
  }

  function qunarSingleAttributePriceDiagnostic(container) {
    const diagnostic = {
      outcome: "no_strong_single_attribute_contract",
      scanned_node_count: 0,
      aria_label_attribute_count: 0,
      aria_value_attribute_count: 0,
      title_attribute_count: 0,
      alt_attribute_count: 0,
      price_named_data_attribute_count: 0,
      numeric_only_attribute_count: 0,
      numeric_attribute_samples: [],
      single_currency_amount_attribute_count: 0,
      final_tax_total_attribute_count: 0,
      final_per_person_tax_attribute_count: 0,
      nonfinal_price_attribute_count: 0,
      negative_tax_attribute_count: 0,
    };
    if (!container || typeof container.querySelectorAll !== "function") {
      diagnostic.outcome = "container_unavailable";
      return diagnostic;
    }
    const nodes = [container, ...container.querySelectorAll("*")];
    const maxNodes = 161;
    for (const node of nodes.slice(0, maxNodes)) {
      diagnostic.scanned_node_count += 1;
      if (
        !node ||
        typeof node.getAttribute !== "function"
      ) {
        continue;
      }
      const names = new Set([
        "aria-label",
        "aria-valuetext",
        "aria-valuenow",
        "title",
        "alt",
      ]);
      if (typeof node.getAttributeNames === "function") {
        for (const name of node.getAttributeNames()) {
          if (
            /^data-/i.test(name) &&
            /(?:price|amount|fare|total|tax|prc|value|label|text)/i.test(
              name,
            )
          ) {
            names.add(name);
          }
        }
      }
      for (const name of names) {
        const value = cleanText(node.getAttribute(name));
        if (!value || value.length > 240) {
          continue;
        }
        if (name === "aria-label") {
          diagnostic.aria_label_attribute_count += 1;
        } else if (/^aria-value/.test(name)) {
          diagnostic.aria_value_attribute_count += 1;
        } else if (name === "title") {
          diagnostic.title_attribute_count += 1;
        } else if (name === "alt") {
          diagnostic.alt_attribute_count += 1;
        } else if (/^data-/i.test(name)) {
          diagnostic.price_named_data_attribute_count += 1;
        }
        if (/^[¥￥]?\s*[1-9]\d{2,6}(?:\.\d{1,2})?$/.test(value)) {
          diagnostic.numeric_only_attribute_count += 1;
          if (diagnostic.numeric_attribute_samples.length < 16) {
            diagnostic.numeric_attribute_samples.push({
              attribute: name,
              value_length: value.length,
              text_digit_length:
                cleanText(node.textContent).replace(/\D/g, "").length,
              value_matches_text:
                value === cleanText(node.textContent),
              class_name: diagnosticClassName(node),
            });
          }
        }
        if (visibleFlightCurrencyAmountCount(value) !== 1) {
          continue;
        }
        diagnostic.single_currency_amount_attribute_count += 1;
        if (NON_FINAL_FLIGHT_PRICE_PATTERN.test(value)) {
          diagnostic.nonfinal_price_attribute_count += 1;
          continue;
        }
        if (NEGATIVE_TAX_PATTERN.test(value)) {
          diagnostic.negative_tax_attribute_count += 1;
          continue;
        }
        if (!visibleEvidence(node)) {
          continue;
        }
        if (/含税总价/.test(value)) {
          diagnostic.final_tax_total_attribute_count += 1;
        }
        if (/人均含税价/.test(value)) {
          diagnostic.final_per_person_tax_attribute_count += 1;
        }
      }
    }
    if (nodes.length > maxNodes) {
      diagnostic.outcome = "scan_budget_exhausted";
    } else if (
      diagnostic.final_tax_total_attribute_count > 0 ||
      diagnostic.final_per_person_tax_attribute_count > 0
    ) {
      diagnostic.outcome = "strong_single_attribute_contract_found";
    }
    return diagnostic;
  }

  function qunarTitledDigitPriceEvidence(container) {
    const containerText = cleanText(container && container.textContent);
    const labelMatch = containerText.match(/人均含税价|含税总价/);
    const currencyTokens = containerText.match(/[¥￥]/g) || [];
    if (
      !labelMatch ||
      currencyTokens.length !== 1 ||
      NON_FINAL_FLIGHT_PRICE_PATTERN.test(containerText) ||
      NEGATIVE_TAX_PATTERN.test(containerText)
    ) {
      return null;
    }
    const titledNodes = [...container.querySelectorAll("[title]")].filter(
      (node) =>
        visibleEvidence(node) &&
        node.getAttribute("aria-hidden") !== "true" &&
        !/clone|ghost|previous|old|animate|transition/i.test(
          cleanText(node.getAttribute("class")),
        ),
    );
    const digitLeaves = [];
    for (const node of titledNodes) {
      if (
        node.children.length ||
        !/^\d$/.test(cleanText(node.textContent))
      ) {
        continue;
      }
      digitLeaves.push(node);
    }
    if (digitLeaves.length < 2) {
      return null;
    }
    const titles = digitLeaves.map((node) =>
      cleanText(node.getAttribute("title")),
    );
    const stableAmount = stableTitledDigitAmount(
      titles,
      digitLeaves.map((node) => cleanText(node.textContent)),
    );
    const numericTitleNodes = titledNodes.filter((node) =>
      /^[1-9]\d{2,6}$/.test(cleanText(node.getAttribute("title"))),
    );
    const numericTitles = [
      ...new Set(
        numericTitleNodes.map((node) =>
          cleanText(node.getAttribute("title")),
        ),
      ),
    ];
    const hasAuditedPriceSurface = numericTitleNodes.some((node) => {
      for (
        let current = node;
        current && current !== container.parentElement;
        current = current.parentElement
      ) {
        const classes = cleanText(current.getAttribute("class")).split(/\s+/);
        if (classes.includes("fix_price")) {
          return true;
        }
        if (current === container) {
          break;
        }
      }
      return false;
    });
    const consistentPriceTitle =
      numericTitleNodes.length >= 2 &&
      numericTitles.length === 1 &&
      hasAuditedPriceSurface
        ? numericTitles[0]
        : null;
    const amount = stableAmount || consistentPriceTitle;
    if (!amount) {
      return null;
    }
    return {
      price_text: `${labelMatch[0]} ¥${amount}`,
      amount_text: amount,
      label: labelMatch[0],
      currency: "CNY",
      digit_leaf_count:
        stableAmount ? digitLeaves.length : numericTitleNodes.length,
      visible_digit_sequence: stableAmount || null,
      evidence_source: stableAmount
        ? "consistent_visible_digit_title"
        : "consistent_visible_price_surface_title",
    };
  }

  function clippedIntersectionRatio(rect, clips) {
    if (
      !rect ||
      !Number.isFinite(rect.left) ||
      !Number.isFinite(rect.top) ||
      !Number.isFinite(rect.right) ||
      !Number.isFinite(rect.bottom) ||
      rect.right <= rect.left ||
      rect.bottom <= rect.top
    ) {
      return 0;
    }
    let left = rect.left;
    let top = rect.top;
    let right = rect.right;
    let bottom = rect.bottom;
    for (const clip of clips || []) {
      if (
        !clip ||
        !Number.isFinite(clip.left) ||
        !Number.isFinite(clip.top) ||
        !Number.isFinite(clip.right) ||
        !Number.isFinite(clip.bottom)
      ) {
        return 0;
      }
      left = Math.max(left, clip.left);
      top = Math.max(top, clip.top);
      right = Math.min(right, clip.right);
      bottom = Math.min(bottom, clip.bottom);
    }
    const visibleArea =
      Math.max(0, right - left) * Math.max(0, bottom - top);
    const area = (rect.right - rect.left) * (rect.bottom - rect.top);
    return area > 0 ? visibleArea / area : 0;
  }

  function geometryClippedDigitAmount(candidates) {
    if (!Array.isArray(candidates) || !candidates.length) {
      return null;
    }
    const columns = new Map();
    for (const candidate of candidates) {
      if (
        !candidate ||
        !candidate.column ||
        !/^\d$/.test(cleanText(candidate.digit)) ||
        !candidate.column_rect
      ) {
        continue;
      }
      if (!columns.has(candidate.column)) {
        columns.set(candidate.column, {
          rect: candidate.column_rect,
          active: [],
        });
      }
      const ratio = clippedIntersectionRatio(
        candidate.glyph_rect,
        candidate.clip_rects,
      );
      if (
        ratio < 0.8 ||
        Number(candidate.opacity) < 0.95 ||
        candidate.display === "none" ||
        candidate.visibility === "hidden"
      ) {
        continue;
      }
      columns.get(candidate.column).active.push(cleanText(candidate.digit));
    }
    const ordered = [...columns.values()].sort(
      (left, right) =>
        left.rect.left - right.rect.left ||
        left.rect.top - right.rect.top,
    );
    if (
      ordered.length < 3 ||
      ordered.length > 7 ||
      ordered.some((column) => column.active.length !== 1)
    ) {
      return null;
    }
    for (let index = 1; index < ordered.length; index += 1) {
      const previous = ordered[index - 1].rect;
      const current = ordered[index].rect;
      if (
        !Number.isFinite(previous.left) ||
        !Number.isFinite(previous.right) ||
        !Number.isFinite(current.left) ||
        current.left <= previous.left ||
        current.left < previous.right - 1
      ) {
        return null;
      }
    }
    const amount = ordered.map((column) => column.active[0]).join("");
    return /^[1-9]\d{2,6}$/.test(amount) ? amount : null;
  }

  function qunarGeometryDigitPriceEvidence(
    container,
    diagnosticOutput = null,
  ) {
    const containerText = cleanText(container && container.textContent);
    const labelMatch = containerText.match(/人均含税价|含税总价/);
    const currencyTokens = containerText.match(/[¥￥]/g) || [];
    const view =
      container &&
      container.ownerDocument &&
      container.ownerDocument.defaultView;
    const diagnostic = {
      outcome: "precondition_rejected",
      scanned_descendant_count: 0,
      digit_leaf_count: 0,
      digit_leaf_prc_count: 0,
      structural_prc_column_count: 0,
      fallback_column_count: 0,
      clipping_column_count: 0,
      positive_glyph_rect_count: 0,
      positive_column_rect_count: 0,
      candidate_count: 0,
      clipped_active_glyph_count: 0,
      digit_samples: [],
    };
    const publishDiagnostic = (outcome) => {
      diagnostic.outcome = outcome;
      if (
        diagnosticOutput &&
        typeof diagnosticOutput === "object"
      ) {
        Object.assign(diagnosticOutput, diagnostic);
      }
    };
    if (
      !container ||
      !view ||
      !labelMatch ||
      currencyTokens.length !== 1 ||
      NON_FINAL_FLIGHT_PRICE_PATTERN.test(containerText) ||
      NEGATIVE_TAX_PATTERN.test(containerText)
    ) {
      publishDiagnostic("precondition_rejected");
      return null;
    }
    const candidates = [];
    const structuralColumns = new Set();
    const fallbackColumns = new Set();
    const clippingColumns = new Set();
    let scanned = 0;
    for (const node of container.querySelectorAll("*")) {
      scanned += 1;
      diagnostic.scanned_descendant_count = scanned;
      if (scanned > 160) {
        publishDiagnostic("scan_budget_exhausted");
        return null;
      }
      if (
        node.children.length ||
        node.getAttribute("aria-hidden") === "true" ||
        /clone|ghost|previous|old/i.test(
          cleanText(node.getAttribute("class")),
        ) ||
        !/^\d$/.test(cleanText(node.textContent))
      ) {
        continue;
      }
      diagnostic.digit_leaf_count += 1;
      if (
        cleanText(node.getAttribute("class"))
          .split(/\s+/)
          .includes("prc")
      ) {
        diagnostic.digit_leaf_prc_count += 1;
      }
      const glyphStyle = view.getComputedStyle(node);
      let structuralColumn = node.parentElement;
      while (
        structuralColumn &&
        structuralColumn !== container &&
        !cleanText(structuralColumn.getAttribute("class"))
          .split(/\s+/)
          .includes("prc")
      ) {
        structuralColumn = structuralColumn.parentElement;
      }
      if (structuralColumn === container) {
        structuralColumn = null;
      }
      if (structuralColumn) {
        structuralColumns.add(structuralColumn);
      }
      let column = structuralColumn || node.parentElement;
      if (!structuralColumn) {
        while (column && column !== container) {
          const style = view.getComputedStyle(column);
          if (
            /hidden|clip/.test(
              `${style.overflow} ${style.overflowX} ${style.overflowY}`,
            )
          ) {
            break;
          }
          column = column.parentElement;
        }
        if (column && column !== container) {
          fallbackColumns.add(column);
        }
      }
      if (!column || column === container) {
        continue;
      }
      const clipRects = [];
      let clippingAncestorCount = 0;
      if (structuralColumn) {
        // Qunar's live price roller gives each digit an exact `.prc`
        // structural column while a shared ancestor performs the vertical
        // clipping. The column rectangle supplies the horizontal cell; the
        // real overflow ancestors below still prove which glyph is rendered.
        clipRects.push(structuralColumn.getBoundingClientRect());
      }
      for (
        let ancestor = column;
        ancestor && ancestor !== container.parentElement;
        ancestor = ancestor.parentElement
      ) {
        const style = view.getComputedStyle(ancestor);
        if (
          /hidden|clip/.test(
            `${style.overflow} ${style.overflowX} ${style.overflowY}`,
          )
        ) {
          clippingAncestorCount += 1;
          clipRects.push(ancestor.getBoundingClientRect());
        }
        if (ancestor === container) {
          break;
        }
      }
      if (!clippingAncestorCount) {
        continue;
      }
      clippingColumns.add(column);
      const glyphRect = node.getBoundingClientRect();
      const columnRect = column.getBoundingClientRect();
      if (diagnostic.digit_samples.length < 24) {
        diagnostic.digit_samples.push({
          digit: cleanText(node.textContent),
          title_length: cleanText(node.getAttribute("title")).length,
          title_is_single_digit:
            /^\d$/.test(cleanText(node.getAttribute("title"))),
          title_matches_digit:
            cleanText(node.getAttribute("title")) ===
              cleanText(node.textContent),
          class_name: diagnosticClassName(node),
          parent_class_name: diagnosticClassName(node.parentElement),
          column_class_name: diagnosticClassName(column),
          glyph_rect: [
            glyphRect.left,
            glyphRect.top,
            glyphRect.right,
            glyphRect.bottom,
          ].map((value) =>
            Number.isFinite(value) ? Math.round(value * 10) / 10 : null,
          ),
          column_rect: [
            columnRect.left,
            columnRect.top,
            columnRect.right,
            columnRect.bottom,
          ].map((value) =>
            Number.isFinite(value) ? Math.round(value * 10) / 10 : null,
          ),
          opacity: glyphStyle.opacity || "1",
          transform: glyphStyle.transform || "none",
        });
      }
      if (
        glyphRect &&
        glyphRect.right > glyphRect.left &&
        glyphRect.bottom > glyphRect.top
      ) {
        diagnostic.positive_glyph_rect_count += 1;
      }
      if (
        columnRect &&
        columnRect.right > columnRect.left &&
        columnRect.bottom > columnRect.top
      ) {
        diagnostic.positive_column_rect_count += 1;
      }
      candidates.push({
        column,
        digit: cleanText(node.textContent),
        glyph_rect: glyphRect,
        column_rect: columnRect,
        clip_rects: clipRects,
        opacity: glyphStyle.opacity === "" ? 1 : Number(glyphStyle.opacity),
        display: glyphStyle.display,
        visibility: glyphStyle.visibility,
        transform: glyphStyle.transform || "none",
      });
    }
    diagnostic.structural_prc_column_count = structuralColumns.size;
    diagnostic.fallback_column_count = fallbackColumns.size;
    diagnostic.clipping_column_count = clippingColumns.size;
    diagnostic.candidate_count = candidates.length;
    diagnostic.clipped_active_glyph_count = candidates.filter(
      (candidate) =>
        clippedIntersectionRatio(
          candidate.glyph_rect,
          candidate.clip_rects,
        ) >= 0.8 &&
        Number(candidate.opacity) >= 0.95 &&
        candidate.display !== "none" &&
        candidate.visibility !== "hidden",
    ).length;
    const amount = geometryClippedDigitAmount(candidates);
    if (!amount) {
      publishDiagnostic("visible_column_contract_rejected");
      return null;
    }
    publishDiagnostic("accepted");
    return {
      price_text: `${labelMatch[0]} ¥${amount}`,
      amount_text: amount,
      label: labelMatch[0],
      currency: "CNY",
      digit_leaf_count: amount.length,
      visible_digit_sequence: amount,
      evidence_source: "geometry_clipped_visible_digit_sequence",
    };
  }

  function exactVisibleInputValue(root, selectors) {
    const input = visibleNodes(root, selectors, 4)[0];
    if (!input) {
      return null;
    }
    return cleanText(
      typeof input.value === "string"
        ? input.value
        : input.getAttribute("value"),
    ) || null;
  }

  function qunarExactPartySearchContext(root, query, driver) {
    const confirmed = flightReceiptConfirmedQuery(query, driver);
    if (!confirmed) {
      return null;
    }
    const directUrlContext =
      driver.mode === "search_url" &&
      driver.confirmation_scope === "trusted_exact_search_url";
    if (
      driver.party_availability_confirmed !== true &&
      driver.mode !== "visible_form"
    ) {
      return null;
    }
    if (
      directUrlContext &&
      (
        !Array.isArray(driver.url_confirmed_fields) ||
        ![
          "origin",
          "destination",
          "start_date",
          "end_date",
          "adults",
        ].every((field) => driver.url_confirmed_fields.includes(field))
      )
    ) {
      return null;
    }
    if (directUrlContext) {
      return (
        `exact_trusted_url_party_context: ${query.origin_code}→` +
        `${query.destination_code}, ${query.start_date}→${query.end_date}, ` +
        `${query.adults}名成人；visible_result_card；inventory_not_locked`
      );
    }
    const originValue = exactVisibleInputValue(root, [
      "input#fromCity",
      "input[name='fromCity']",
      "input[data-testid*='from-city']",
    ]);
    const destinationValue = exactVisibleInputValue(root, [
      "input#toCity",
      "input[name='toCity']",
      "input[data-testid*='to-city']",
    ]);
    const startDateValue = exactVisibleInputValue(root, [
      "input#fromDate",
      "input[name='fromDate']",
    ]);
    const endDateValue = exactVisibleInputValue(root, [
      "input#toDate",
      "input[name='toDate']",
    ]);
    const adultValue = exactVisibleInputValue(root, [
      "input[name='adultNum']",
      "select[name='adultNum']",
      "[role='spinbutton'][aria-label*='成人']",
      "input[aria-label*='成人']",
    ]);
    const exactDate = (value, expected) =>
      cleanText(value).replace(/\D/g, "") ===
      cleanText(expected).replace(/\D/g, "");
    if (
      !originValue ||
      !destinationValue ||
      !startDateValue ||
      !endDateValue ||
      !adultValue ||
      !exactVisibleSearchCityMatches(
        originValue,
        query.origin,
        query.origin_code,
      ) ||
      !exactVisibleSearchCityMatches(
        destinationValue,
        query.destination,
        query.destination_code,
      ) ||
      !exactDate(startDateValue, query.start_date) ||
      !exactDate(endDateValue, query.end_date) ||
      Number(adultValue.replace(/\D/g, "")) !== Number(query.adults)
    ) {
      return null;
    }
    return (
      `exact_party_search_context: ${query.origin_code}→` +
      `${query.destination_code}, ${query.start_date}→${query.end_date}, ` +
      `${query.adults}名成人；visible_result_card；inventory_not_locked`
    );
  }

  function qunarPriceEvidence(
    card,
    { allowGeometry = true } = {},
  ) {
    const containers = visibleNodes(
      card,
      [".col-price"],
      12,
    );
    const structures = [];
    let priceText = null;
    let evidenceSource = null;
    for (const container of containers) {
      const structure = atomicPriceStructure(
        visibleAtomicTextFragments(container),
      );
      const attributeDiagnostic =
        qunarSingleAttributePriceDiagnostic(container);
      const titledDigitEvidence =
        qunarTitledDigitPriceEvidence(container);
      const geometryDiagnostic = {};
      const geometryDigitEvidence = allowGeometry
        ? qunarGeometryDigitPriceEvidence(
            container,
            geometryDiagnostic,
          )
        : null;
      structures.push({
        container_class: diagnosticClassName(container),
        fragment_shapes: structure.fragment_shapes,
        atomic_fragment_count: structure.atomic_fragment_count,
        complete_currency_amount_fragment_count:
          structure.complete_currency_amount_fragment_count,
        split_numeric_sequence_count:
          structure.split_numeric_sequence_count,
        safe_amount_fragment_present:
          Boolean(structure.safe_amount_fragment),
        consistent_digit_title_amount_present:
          Boolean(titledDigitEvidence),
        consistent_digit_title_leaf_count:
          titledDigitEvidence
            ? titledDigitEvidence.digit_leaf_count
            : 0,
        geometry_digit_amount_present:
          Boolean(geometryDigitEvidence),
        geometry_digit_column_count:
          geometryDigitEvidence
            ? geometryDigitEvidence.digit_leaf_count
            : 0,
        geometry_scan_outcome:
          allowGeometry
            ? geometryDiagnostic.outcome || "not_scanned"
            : "disabled_after_unstable_reads",
        geometry_scanned_descendant_count:
          geometryDiagnostic.scanned_descendant_count || 0,
        geometry_digit_leaf_count:
          geometryDiagnostic.digit_leaf_count || 0,
        geometry_digit_leaf_prc_count:
          geometryDiagnostic.digit_leaf_prc_count || 0,
        geometry_structural_prc_column_count:
          geometryDiagnostic.structural_prc_column_count || 0,
        geometry_fallback_column_count:
          geometryDiagnostic.fallback_column_count || 0,
        geometry_clipping_column_count:
          geometryDiagnostic.clipping_column_count || 0,
        geometry_positive_glyph_rect_count:
          geometryDiagnostic.positive_glyph_rect_count || 0,
        geometry_positive_column_rect_count:
          geometryDiagnostic.positive_column_rect_count || 0,
        geometry_candidate_count:
          geometryDiagnostic.candidate_count || 0,
        geometry_clipped_active_glyph_count:
          geometryDiagnostic.clipped_active_glyph_count || 0,
        attribute_scan_outcome: attributeDiagnostic.outcome,
        attribute_scanned_node_count:
          attributeDiagnostic.scanned_node_count,
        aria_label_attribute_count:
          attributeDiagnostic.aria_label_attribute_count,
        aria_value_attribute_count:
          attributeDiagnostic.aria_value_attribute_count,
        title_attribute_count:
          attributeDiagnostic.title_attribute_count,
        alt_attribute_count:
          attributeDiagnostic.alt_attribute_count,
        price_named_data_attribute_count:
          attributeDiagnostic.price_named_data_attribute_count,
        numeric_only_attribute_count:
          attributeDiagnostic.numeric_only_attribute_count,
        numeric_attribute_samples:
          attributeDiagnostic.numeric_attribute_samples,
        single_currency_amount_attribute_count:
          attributeDiagnostic.single_currency_amount_attribute_count,
        final_tax_total_attribute_count:
          attributeDiagnostic.final_tax_total_attribute_count,
        final_per_person_tax_attribute_count:
          attributeDiagnostic.final_per_person_tax_attribute_count,
        nonfinal_price_attribute_count:
          attributeDiagnostic.nonfinal_price_attribute_count,
        negative_tax_attribute_count:
          attributeDiagnostic.negative_tax_attribute_count,
        geometry_digit_samples:
          geometryDiagnostic.digit_samples || [],
      });
      const containerText = cleanText(container.textContent);
      if (
        !priceText &&
        /人均含税价/.test(containerText) &&
        structure.safe_amount_fragment
      ) {
        priceText = `人均含税价 ${structure.safe_amount_fragment}`;
        evidenceSource = "atomic_visible_text_fragment";
      } else if (!priceText && titledDigitEvidence) {
        priceText = titledDigitEvidence.price_text;
        evidenceSource = titledDigitEvidence.evidence_source;
      } else if (!priceText && geometryDigitEvidence) {
        priceText = geometryDigitEvidence.price_text;
        evidenceSource = geometryDigitEvidence.evidence_source;
      }
    }
    return { priceText, structures, evidenceSource };
  }

  function fliggyAlternateOriginDiagnostic(root, query) {
    const nodes = visibleNodes(
      root,
      ["li.nearby-item", "[class~='nearby-item']"],
      8,
    );
    if (!nodes.length) {
      return null;
    }
    const observations = nodes
      .map((node) => flightRouteObservation(node.textContent, query))
      .filter(Boolean);
    const alternateOnly =
      observations.length === nodes.length &&
      observations.every(
        (item) =>
          item.destination_matches_requested &&
          !item.origin_matches_requested,
      );
    if (!alternateOnly) {
      return null;
    }
    return {
      outcome: "alternate_origin_only",
      stage: "alternate_origin_suggestions",
      scope: "visible_nearby_items_only",
      counts: {
        nearby_item_count: nodes.length,
        parsed_route_count: observations.length,
        requested_origin_match_count: observations.filter(
          (item) => item.origin_matches_requested,
        ).length,
        requested_destination_match_count: observations.filter(
          (item) => item.destination_matches_requested,
        ).length,
        safe_outbound_control_count:
          exactOutboundControls("fliggy", root, query).length,
        visible_price_anchor_count: visiblePriceAnchors(root).length,
      },
      observed_routes: observations.slice(0, 8),
    };
  }

  function ctripOutboundAvailabilityDiagnostic(root, query) {
    const stageNodes = visibleNodes(
      root,
      [
        ".segment_tab",
        "[class*='segment_tab']",
        "[class*='segment-tab']",
        "[role='tab']",
      ],
      12,
    ).filter((node) => /选择去程/.test(cleanText(node.textContent)));
    const priceAnchors = visiblePriceAnchors(root);
    if (!stageNodes.length || priceAnchors.length) {
      return null;
    }
    return {
      outcome: "outbound_results_empty_or_unavailable",
      stage: "outbound_result_discovery",
      scope: "visible_outbound_stage_only",
      counts: {
        outbound_stage_anchor_count: stageNodes.length,
        visible_price_anchor_count: 0,
        profile_card_count: visibleNodes(
          root,
          PROFILES.ctrip.flight.cards,
          30,
        ).length,
        safe_outbound_control_count:
          exactOutboundControls("ctrip", root, query).length,
      },
      stage_evidence: stageNodes.slice(0, 6).map((node) => ({
        tag: cleanText(node.tagName).toLowerCase(),
        class: diagnosticClassName(node),
        text_summary: visibleDiagnosticText(node),
      })),
    };
  }

  function ctripOutboundComparisonDiagnostic(root, query) {
    const timezones = routeTimezones(query);
    if (!timezones) {
      return null;
    }
    const controls = exactOutboundControls("ctrip", root, query);
    const cards = ctripFlightSemanticCards(root);
    if (!controls.length && !cards.length) {
      return null;
    }
    const structures = cards.slice(0, 6).map((card) => {
      const text = cleanText(card.textContent);
      const priceText = ctripFlightPriceEvidence(card);
      const selectionAnchors = visibleActionAnchors(
        card,
        CTRIP_FLIGHT_SELECTION_PATTERN,
      );
      const comparisonPrice =
        flightComparisonPrice("ctrip", priceText);
      const taxStatus = taxesIncluded(priceText);
      const outboundLeg = legFromVisibleText(
        text,
        query.start_date,
        timezones.origin_offset,
        timezones.destination_offset,
      );
      const outboundRoute = flightLegRouteEvidence(
        text,
        query,
        "outbound",
        "flight_diagnostic_ctrip_outbound_comparison",
      );
      return {
        card_class: diagnosticClassName(card),
        visible_summary: visibleDiagnosticText(card),
        text_length: text.length,
        round_trip_label_count:
          visibleRoundTripPriceLabels(card).length,
        price_anchor_count: visiblePriceAnchors(card).length,
        selection_anchor_count: visibleActionAnchors(
          card,
          CTRIP_FLIGHT_SELECTION_PATTERN,
        ).length,
        selection_controls: selectionAnchors.slice(0, 4).map((node) => ({
          tag: cleanText(node.tagName).toLowerCase(),
          class: diagnosticClassName(node),
          text_summary: visibleDiagnosticText(node),
          aria_label: sanitizeDiagnosticText(node.getAttribute("aria-label")),
          title: sanitizeDiagnosticText(node.getAttribute("title")),
          styled_outbound_audit:
            ctripStyledOutboundControlDiagnostic(node, query),
        })),
        visible_currency_amount_count:
          visibleFlightCurrencyAmountCount(text),
        price_evidence: sanitizeDiagnosticText(priceText),
        comparison_price_parsed: Boolean(comparisonPrice),
        comparison_price_is_starting: Boolean(
          comparisonPrice &&
          comparisonPrice.price_classification ===
            "starting_or_estimated",
        ),
        explicit_tax_included: taxStatus === true,
        outbound_leg_parsed: Boolean(outboundLeg),
        outbound_route_matches: Boolean(
          outboundRoute && outboundRoute.matches_expected === true,
        ),
        comparison_candidate_accepted: Boolean(
          comparisonPrice &&
          comparisonPrice.price_classification ===
            "starting_or_estimated" &&
          taxStatus === true &&
          outboundLeg &&
          outboundRoute &&
          outboundRoute.matches_expected === true
        ),
      };
    });
    return {
      outcome: "outbound_comparison_evidence_incomplete",
      stage: "outbound_comparison_validation",
      scope: "visible_ctrip_outbound_controls_only",
      counts: {
        safe_outbound_control_count: controls.length,
        semantic_outbound_card_count: cards.length,
        priced_comparison_candidate_count:
          ctripOutboundComparisonCandidates(root, query).length,
      },
      blocking_contract_fields: ["comparison_price_evidence"],
      structures,
    };
  }

  function ctripFlightContractDiagnostic(root, query) {
    const summary = selectedOutboundSummary("ctrip", root, query);
    if (!summary) {
      return (
        ctripOutboundAvailabilityDiagnostic(root, query) ||
        ctripOutboundComparisonDiagnostic(root, query)
      );
    }
    const cards = stagedReturnCards("ctrip", root, query);
    if (!cards.length) {
      return {
        outcome: "round_trip_combination_incomplete",
        stage: "return_card_discovery",
        scope: "visible_ctrip_return_stage_only",
        counts: {
          selected_outbound_summary_count: 1,
          semantic_return_card_count: 0,
        },
        blocking_contract_fields: ["return_card"],
      };
    }
    const counts = {
      selected_outbound_summary_count: 1,
      semantic_return_card_count: cards.length,
      parsed_return_leg_count: 0,
      matching_return_route_count: 0,
      explicit_tax_evidence_count: 0,
      availability_evidence_count: 0,
      price_evidence_count: 0,
      starting_price_count: 0,
      explicit_price_basis_count: 0,
      valid_final_price_contract_count: 0,
    };
    const structures = [];
    for (const card of cards) {
      const text = cleanText(card.textContent);
      const priceText = stagedFlightPriceEvidence("ctrip", card);
      const contract = flightPriceContract(priceText);
      const basis = priceBasis("flight", priceText);
      const finality = flightPriceFinality(priceText);
      const taxEvidence = explicitTaxEvidence(card, priceText);
      const availabilityEvidence = flightAvailabilityEvidence(card);
      const timezones = routeTimezones(query);
      const leg = timezones
        ? legFromVisibleText(
            text,
            query.end_date,
            timezones.destination_offset,
            timezones.origin_offset,
          )
        : null;
      const routeEvidence = flightLegRouteEvidence(
        text,
        query,
        "return",
        "ctrip_semantic_return_card",
      );
      if (leg) {
        counts.parsed_return_leg_count += 1;
      }
      if (routeEvidence && routeEvidence.matches_expected === true) {
        counts.matching_return_route_count += 1;
      }
      if (taxEvidence) {
        counts.explicit_tax_evidence_count += 1;
      }
      if (availabilityEvidence) {
        counts.availability_evidence_count += 1;
      }
      if (priceText) {
        counts.price_evidence_count += 1;
      }
      if (finality === "starting_or_estimated") {
        counts.starting_price_count += 1;
      }
      if (["per_person", "total_party"].includes(basis)) {
        counts.explicit_price_basis_count += 1;
      }
      if (contract.valid === true) {
        counts.valid_final_price_contract_count += 1;
      }
      structures.push({
        card_class: diagnosticClassName(card),
        price_evidence: sanitizeDiagnosticText(priceText),
        price_finality: finality,
        price_basis: basis,
        tax_evidence_present: Boolean(taxEvidence),
        availability_evidence_present: Boolean(availabilityEvidence),
        return_leg_parsed: Boolean(leg),
        return_route_matches: Boolean(
          routeEvidence && routeEvidence.matches_expected === true,
        ),
      });
    }
    const blocking = [];
    if (counts.starting_price_count) {
      blocking.push("price_finality");
    }
    if (!counts.explicit_price_basis_count) {
      blocking.push("price_basis");
    }
    if (!counts.explicit_tax_evidence_count) {
      blocking.push("taxes");
    }
    if (!counts.availability_evidence_count) {
      blocking.push("availability");
    }
    if (!counts.matching_return_route_count) {
      blocking.push("return_route");
    }
    return {
      outcome:
        counts.starting_price_count &&
        !counts.valid_final_price_contract_count
          ? "starting_price_only"
          : "round_trip_combination_incomplete",
      stage:
        counts.starting_price_count &&
        !counts.valid_final_price_contract_count
          ? "price_finality_validation"
          : "quote_contract_validation",
      scope: "visible_ctrip_return_cards_only",
      counts,
      blocking_contract_fields: blocking,
      structures: structures.slice(0, 6),
    };
  }

  function qunarFlightStructureDiagnostic(root, query, driver) {
    const cards = visibleNodes(
      root,
      [".m-airfly-lst .b-airfly", ".b-airfly"],
      30,
    );
    const timezones = routeTimezones(query);
    const counts = {
      combination_card_count: cards.length,
      exactly_two_trip_card_count: 0,
      parsed_outbound_leg_count: 0,
      parsed_return_leg_count: 0,
      price_container_count: 0,
      atomic_price_fragment_count: 0,
      complete_currency_amount_fragment_count: 0,
      safe_price_evidence_count: 0,
      split_price_structure_count: 0,
      valid_flight_price_contract_count: 0,
      explicit_per_person_basis_count: 0,
      explicit_party_total_basis_count: 0,
      explicit_tax_evidence_count: 0,
      availability_evidence_count: 0,
      matching_round_trip_route_count: 0,
    };
    const structures = [];
    for (const card of cards) {
      const trips = visibleNodes(card, [".s-trip"], 3);
      let outboundRouteEvidence = null;
      let returnRouteEvidence = null;
      if (trips.length === 2) {
        counts.exactly_two_trip_card_count += 1;
        if (
          timezones &&
          legFromQunarTrip(
            trips[0],
            query.start_date,
            timezones.origin_offset,
            timezones.destination_offset,
          )
        ) {
          counts.parsed_outbound_leg_count += 1;
        }
        if (
          timezones &&
          legFromQunarTrip(
            trips[1],
            query.end_date,
            timezones.destination_offset,
            timezones.origin_offset,
          )
        ) {
          counts.parsed_return_leg_count += 1;
        }
        outboundRouteEvidence = flightLegRouteEvidence(
          trips[0].textContent,
          query,
          "outbound",
          "qunar_combined_card_leg",
        );
        returnRouteEvidence = flightLegRouteEvidence(
          trips[1].textContent,
          query,
          "return",
          "qunar_combined_card_leg",
        );
        if (
          outboundRouteEvidence &&
          outboundRouteEvidence.matches_expected === true &&
          returnRouteEvidence &&
          returnRouteEvidence.matches_expected === true
        ) {
          counts.matching_round_trip_route_count += 1;
        }
      }
      const priceEvidence = qunarPriceEvidence(card, {
        allowGeometry:
          !(driver && driver.qunar_geometry_price_disabled === true),
      });
      const priceContract = flightPriceContract(priceEvidence.priceText);
      const basis = priceBasis("flight", priceEvidence.priceText);
      const taxEvidence = explicitTaxEvidence(
        card,
        priceEvidence.priceText,
      );
      const availabilityEvidence = flightAvailabilityEvidence(card);
      if (priceEvidence.priceText) {
        counts.safe_price_evidence_count += 1;
      }
      if (priceContract.valid === true) {
        counts.valid_flight_price_contract_count += 1;
      }
      if (basis === "per_person") {
        counts.explicit_per_person_basis_count += 1;
      }
      if (
        basis === "total_party" &&
        priceContract.valid === true
      ) {
        counts.explicit_party_total_basis_count += 1;
      }
      if (taxEvidence) {
        counts.explicit_tax_evidence_count += 1;
      }
      if (availabilityEvidence) {
        counts.availability_evidence_count += 1;
      }
      for (const structure of priceEvidence.structures) {
        counts.price_container_count += 1;
        counts.atomic_price_fragment_count +=
          structure.atomic_fragment_count;
        counts.complete_currency_amount_fragment_count +=
          structure.complete_currency_amount_fragment_count;
        if (structure.split_numeric_sequence_count > 0) {
          counts.split_price_structure_count += 1;
        }
      }
      structures.push({
        card_class: diagnosticClassName(card),
        trip_count: trips.length,
        price_structures: priceEvidence.structures,
        recovered_price_evidence:
          sanitizeDiagnosticText(priceEvidence.priceText),
        price_basis: basis,
        price_contract_valid: priceContract.valid === true,
        tax_evidence_present: Boolean(taxEvidence),
        availability_evidence_present: Boolean(availabilityEvidence),
        round_trip_route_matches: Boolean(
          outboundRouteEvidence &&
          outboundRouteEvidence.matches_expected === true &&
          returnRouteEvidence &&
          returnRouteEvidence.matches_expected === true
        ),
      });
    }
    let stage = "combination_card_discovery";
    if (counts.combination_card_count) {
      stage = counts.exactly_two_trip_card_count
        ? "leg_time_validation"
        : "leg_pair_validation";
    }
    if (
      counts.exactly_two_trip_card_count &&
      counts.parsed_outbound_leg_count &&
      counts.parsed_return_leg_count
    ) {
      stage = counts.safe_price_evidence_count
        ? "quote_contract_validation"
        : "price_structure_validation";
    }
    const blocking = [];
    if (!counts.safe_price_evidence_count) {
      blocking.push("price_amount");
    }
    if (!counts.valid_flight_price_contract_count) {
      blocking.push("price_basis");
    }
    if (!counts.explicit_tax_evidence_count) {
      blocking.push("taxes");
    }
    if (!counts.availability_evidence_count) {
      blocking.push("availability");
    }
    if (!counts.matching_round_trip_route_count) {
      blocking.push("round_trip_route");
    }
    return {
      outcome:
        counts.split_price_structure_count &&
        !counts.safe_price_evidence_count
          ? "price_structure_incomplete"
          : "round_trip_combination_incomplete",
      stage,
      scope: "visible_qunar_combination_cards_only",
      counts,
      blocking_contract_fields: blocking,
      structures: structures.slice(0, 6),
    };
  }

  function qunarFlightLoadingDiagnostic(root) {
    const text = cleanText(
      root && root.body && root.body.textContent,
    );
    if (
      !text ||
      !/(正在加载|加载中|正在搜索|查询中|请稍等|loading)/i.test(text) ||
      /(?:暂无航班|没有符合条件|未找到航班|无航班)/.test(text)
    ) {
      return null;
    }
    return {
      outcome: "flight_results_loading",
      stage: "result_loading",
      scope: "visible_qunar_search_shell",
      counts: {
        visible_combination_card_count: visibleNodes(
          root,
          [".m-airfly-lst .b-airfly", ".b-airfly"],
          30,
        ).length,
      },
    };
  }

  function flightFailureDiagnostic(provider, root, query, driver) {
    if (provider === "fliggy") {
      return fliggyAlternateOriginDiagnostic(root, query);
    }
    if (provider === "ctrip") {
      return ctripFlightContractDiagnostic(root, query);
    }
    if (provider === "qunar") {
      const loading = qunarFlightLoadingDiagnostic(root);
      if (loading) {
        return loading;
      }
      return qunarFlightStructureDiagnostic(root, query, driver);
    }
    if (provider === "tongcheng") {
      const cards = visibleNodes(root, [".flight-item"], 20);
      const controls = visibleNodes(
        root,
        [".flight-item .flight-btn"],
        40,
      );
      const stageMarkers = [...root.querySelectorAll("body *")]
        .slice(0, MAX_VISIBLE_NODE_SCAN_NODES)
        .filter((node) => visibleEvidence(node))
        .map((node) => ({
          node,
          text: directVisibleNodeText(node),
        }))
        .filter(({ text }) =>
          text &&
          text.length <= 160 &&
          /(?:去程|返程|已选|重新选择|更换航班)/.test(text)
        )
        .slice(0, 20)
        .map(({ node, text }) => ({
          tag: cleanText(node.tagName).toLowerCase(),
          class: diagnosticClassName(node),
          text_summary: sanitizeDiagnosticText(text).slice(0, 160),
        }));
      const selectedTitle = root.querySelector(".hasChooseTitle");
      const selectedOutboundContexts = [];
      let selectedContext = selectedTitle;
      let selectedDepth = 0;
      while (
        selectedContext &&
        selectedDepth < 7 &&
        selectedContext !== root.body &&
        selectedContext !== root.documentElement
      ) {
        const text = cleanText(selectedContext.textContent);
        if (text && text.length <= 5000) {
          selectedOutboundContexts.push({
            depth: selectedDepth,
            tag: cleanText(selectedContext.tagName).toLowerCase(),
            class: diagnosticClassName(selectedContext),
            text_summary: sanitizeDiagnosticText(text).slice(0, 1200),
            direct_children: [...selectedContext.children]
              .slice(0, 20)
              .map((node) => ({
                tag: cleanText(node.tagName).toLowerCase(),
                class: diagnosticClassName(node),
                text_summary: sanitizeDiagnosticText(
                  directVisibleNodeText(node),
                ).slice(0, 200),
              })),
          });
        }
        selectedContext = selectedContext.parentElement;
        selectedDepth += 1;
      }
      const confirmedSummary = selectedOutboundSummary(
        "tongcheng",
        root,
        query,
      );
      const autoSelectedDriver = confirmedSummary
        ? tongchengAutoSelectedOutboundDriver(
            confirmedSummary,
            query,
            driver,
          )
        : null;
      const timezones = routeTimezones(query);
      const confirmedSummaryText = cleanText(
        confirmedSummary && confirmedSummary.textContent,
      );
      const confirmedSummaryLeg = timezones && confirmedSummary
        ? stagedProviderLegFromVisibleText(
            "tongcheng",
            confirmedSummaryText,
            query.start_date,
            timezones.origin_offset,
            timezones.destination_offset,
          )
        : null;
      const confirmedSummaryRoute = confirmedSummary
        ? flightLegRouteEvidence(
            confirmedSummaryText,
            query,
            "outbound",
            "tongcheng_failure_selected_summary",
          )
        : null;
      const returnCardDiagnostics = stagedReturnCards(
        "tongcheng",
        root,
        query,
      ).slice(0, 6).map((card, index) => {
        const text = cleanText(card.textContent);
        const priceText = stagedFlightPriceEvidence("tongcheng", card);
        const priceContract = flightPriceContract(priceText);
        const returnLeg = timezones
          ? stagedProviderLegFromVisibleText(
              "tongcheng",
              text,
              query.end_date,
              timezones.destination_offset,
              timezones.origin_offset,
            )
          : null;
        const returnRoute = flightLegRouteEvidence(
          text,
          query,
          "return",
          "tongcheng_failure_return_card",
        );
        return {
          candidate_index: index,
          leg_parsed: Boolean(returnLeg),
          route_matches_expected: Boolean(
            returnRoute && returnRoute.matches_expected === true,
          ),
          price_text: sanitizeDiagnosticText(priceText).slice(0, 160) || null,
          price_contract: {
            valid: priceContract.valid,
            price_basis: priceContract.price_basis,
            finality: priceContract.finality,
          },
          taxes_included: taxesIncluded(priceText),
          availability_evidence:
            tongchengFlightAvailabilityEvidence(card),
        };
      });
      const exactSelectedSummaryCandidates = [
        ...root.querySelectorAll(".repeatChooseGo"),
      ].slice(0, 8).map((node, index) => {
        const text = cleanText(node.textContent);
        const title = node.querySelector(".hasChooseTitle");
        const reselect = node.querySelector(".repeatButton");
        const route = flightLegRouteEvidence(
          text,
          query,
          "outbound",
          "tongcheng_failure_exact_summary_candidate",
        );
        return {
          candidate_index: index,
          title_visible: Boolean(title && visibleEvidence(title)),
          reselect_visible: Boolean(reselect && visibleEvidence(reselect)),
          title_text: cleanText(title && title.textContent) || null,
          reselect_text: cleanText(reselect && reselect.textContent) || null,
          time_tokens: visibleTimeTokens(text),
          route_matches_expected: Boolean(
            route && route.matches_expected === true,
          ),
          text_summary: sanitizeDiagnosticText(text).slice(0, 500),
        };
      });
      const expandedReturnDetails = visibleNodes(
        root,
        [".flight-item .flight-btn.currentSlt"],
        4,
      ).map((control, index) => {
        const card = control.closest(".flight-item");
        const nodes = card
          ? visibleNodes(card, ["*"], 180).filter((node) => {
              const text = cleanText(node.textContent);
              return (
                text.length > 0 &&
                text.length <= 500 &&
                (
                  PRICE_ANCHOR_PATTERN.test(text) ||
                  /预订|订票|选择|收起|退票|改期|行李/.test(text)
                )
              );
            }).slice(0, 40)
          : [];
        return {
          candidate_index: index,
          control_text: sanitizeDiagnosticText(control.textContent),
          card_class: cleanText(card && card.className) || null,
          evidence_nodes: nodes.map((node) => ({
            tag: cleanText(node.tagName).toLowerCase(),
            class: sanitizeDiagnosticText(node.className).slice(0, 160),
            text_summary: sanitizeDiagnosticText(node.textContent).slice(0, 500),
            href: sanitizeDiagnosticText(node.getAttribute("href")).slice(0, 240) || null,
            role: sanitizeDiagnosticText(node.getAttribute("role")) || null,
          })),
        };
      });
      return {
        schema_version: "tongcheng-flight-contract-diagnostic-v1",
        diagnostic_revision: "2026-08-02.11",
        outcome: cards.length
          ? "flight_cards_rejected_by_strict_contract"
          : "flight_cards_not_hydrated",
        counts: {
          visible_flight_card_count: cards.length,
          visible_flight_button_count: controls.length,
          safe_outbound_control_count: exactOutboundControls(
            "tongcheng",
            root,
            query,
          ).length,
          current_selected_button_count: visibleNodes(
            root,
            [".flight-item .flight-btn.currentSlt"],
            20,
          ).length,
        },
        stage_markers: stageMarkers,
        selected_outbound_contexts: selectedOutboundContexts,
        selected_outbound_contract: {
          summary_found: Boolean(confirmedSummary),
          provider_auto_driver_confirmed: Boolean(autoSelectedDriver),
          leg_parsed: Boolean(confirmedSummaryLeg),
          route_matches_expected: Boolean(
            confirmedSummaryRoute &&
            confirmedSummaryRoute.matches_expected === true,
          ),
          carrier_text: confirmedSummary
            ? tongchengSelectedOutboundCarrier(confirmedSummary)
            : null,
          date_tokens: confirmedSummary
            ? visibleDateTokens(
                confirmedSummaryText,
                Number(String(query.start_date).slice(0, 4)),
              )
            : [],
          time_tokens: confirmedSummary
            ? visibleTimeTokens(confirmedSummaryText)
            : [],
          requested_service_date: cleanText(query.start_date),
          cross_day_delta: confirmedSummary
            ? crossDayDelta(confirmedSummaryText)
            : null,
        },
        exact_selected_summary_candidates: exactSelectedSummaryCandidates,
        expanded_return_details: expandedReturnDetails,
        return_card_contracts: returnCardDiagnostics,
        candidates: cards.slice(0, 6).map((card, index) => {
          const text = cleanText(card.textContent);
          const route = flightLegRouteEvidence(
            text,
            query,
            "outbound",
            "tongcheng_failure_diagnostic",
          );
          const timezones = routeTimezones(query);
          const leg = timezones
            ? stagedProviderLegFromVisibleText(
                "tongcheng",
                text,
                query.start_date,
                timezones.origin_offset,
                timezones.destination_offset,
              )
            : null;
          const returnRoute = flightLegRouteEvidence(
            text,
            query,
            "return",
            "tongcheng_failure_return_diagnostic",
          );
          const returnLeg = timezones
            ? stagedProviderLegFromVisibleText(
                "tongcheng",
                text,
                query.end_date,
                timezones.destination_offset,
                timezones.origin_offset,
              )
            : null;
          const priceText = stagedFlightPriceEvidence("tongcheng", card);
          return {
            candidate_index: index,
            text_summary: sanitizeDiagnosticText(text).slice(0, 500),
            button_labels: [...card.querySelectorAll(".flight-btn")]
              .slice(0, 4)
              .map((node) => sanitizeDiagnosticText(node.textContent).slice(0, 80)),
            carrier_text: flightCarrierText(card) || null,
            route_matches_expected: Boolean(
              route && route.matches_expected === true,
            ),
            leg_parsed: Boolean(leg),
            return_route_matches_expected: Boolean(
              returnRoute && returnRoute.matches_expected === true,
            ),
            return_leg_parsed: Boolean(returnLeg),
            date_tokens: visibleDateTokens(
              text,
              Number(String(query.end_date).slice(0, 4)),
            ),
            time_tokens: visibleTimeTokens(text),
            price_text: sanitizeDiagnosticText(priceText).slice(0, 160) || null,
            availability_evidence:
              tongchengFlightAvailabilityEvidence(card),
          };
        }),
      };
    }
    return null;
  }

  function hasExactObjectKeys(value, keys) {
    return Boolean(
      value &&
      typeof value === "object" &&
      !Array.isArray(value) &&
      canonicalJson(Object.keys(value).sort()) ===
        canonicalJson([...keys].sort()),
    );
  }

  function flightReceiptConfirmedQuery(query, driver) {
    const expected = {
      origin: cleanText(query && query.origin),
      destination: cleanText(query && query.destination),
      start_date: cleanText(query && query.start_date),
      end_date: cleanText(query && query.end_date),
      adults: Number(query && query.adults),
      origin_code: cleanText(query && query.origin_code).toUpperCase(),
      destination_code:
        cleanText(query && query.destination_code).toUpperCase(),
    };
    const confirmed =
      driver &&
      driver.confirmed_query &&
      typeof driver.confirmed_query === "object" &&
      !Array.isArray(driver.confirmed_query)
        ? driver.confirmed_query
        : null;
    if (
      !expected.origin ||
      !expected.destination ||
      !/^\d{4}-\d{2}-\d{2}$/.test(expected.start_date) ||
      !/^\d{4}-\d{2}-\d{2}$/.test(expected.end_date) ||
      Date.parse(`${expected.end_date}T00:00:00Z`) <=
        Date.parse(`${expected.start_date}T00:00:00Z`) ||
      !Number.isInteger(expected.adults) ||
      expected.adults < 1 ||
      expected.adults > 9 ||
      !/^[A-Z]{3}$/.test(expected.origin_code) ||
      !/^[A-Z]{3}$/.test(expected.destination_code) ||
      !driver ||
      driver.triggered !== true ||
      ![
        "confirmed_visible_search",
        "trusted_exact_search_url",
      ].includes(driver.confirmation_scope) ||
      !confirmed ||
      cleanText(confirmed.origin) !== expected.origin ||
      cleanText(confirmed.destination) !== expected.destination ||
      cleanText(confirmed.start_date) !== expected.start_date ||
      cleanText(confirmed.end_date) !== expected.end_date ||
      Number(confirmed.adults) !== expected.adults
    ) {
      return null;
    }
    const readback =
      driver.readback_query &&
      typeof driver.readback_query === "object" &&
      !Array.isArray(driver.readback_query)
        ? driver.readback_query
        : {};
    const readbackChecks = [
      ["origin", expected.origin, (value) => cleanText(value)],
      ["destination", expected.destination, (value) => cleanText(value)],
      ["start_date", expected.start_date, (value) => cleanText(value)],
      ["end_date", expected.end_date, (value) => cleanText(value)],
      ["adults", expected.adults, Number],
      [
        "origin_code",
        expected.origin_code,
        (value) => cleanText(value).toUpperCase(),
      ],
      [
        "destination_code",
        expected.destination_code,
        (value) => cleanText(value).toUpperCase(),
      ],
    ];
    if (
      readbackChecks.some(
        ([field, value, normalize]) =>
          Object.prototype.hasOwnProperty.call(readback, field) &&
          normalize(readback[field]) !== value,
      )
    ) {
      return null;
    }
    return expected;
  }

  function flightReceiptText(value, limit) {
    const safe = sanitizeDiagnosticText(value);
    return safe ? safe.slice(0, limit) : null;
  }

  function strictVisibleFlightAmount(value) {
    const text = cleanText(value);
    const matches = [
      ...text.matchAll(
        /(?:¥|￥|CNY|RMB|USD|\$)\s*(\d{1,3}(?:,\d{3})+|\d+)(?:\.(\d{1,2}))?/gi,
      ),
    ];
    if (matches.length !== 1) {
      return null;
    }
    const amount = Number(
      `${matches[0][1].replace(/,/g, "")}` +
      `${matches[0][2] ? `.${matches[0][2]}` : ""}`,
    );
    const currency = /^(?:USD|\$)/i.test(cleanText(matches[0][0]))
      ? "USD"
      : "CNY";
    return Number.isFinite(amount) && amount > 0
      ? { amount, currency }
      : null;
  }

  function flightComparisonPrice(provider, value) {
    const text = cleanText(value);
    const amount = strictVisibleFlightAmount(text);
    if (!text || !amount) {
      return null;
    }
    let basis = priceBasis("flight", text);
    if (
      provider === "ctrip" &&
      basis === "unknown" &&
      /往返含税价/.test(text)
    ) {
      basis = "per_person";
    }
    if (!["per_person", "total_party"].includes(basis)) {
      return null;
    }
    const finality = flightPriceFinality(text);
    return {
      currency: amount.currency,
      amount: amount.amount,
      price_basis: basis,
      price_classification:
        finality === "starting_or_estimated"
          ? "starting_or_estimated"
          : "comparison_only",
      price_evidence: flightReceiptText(text, 180),
    };
  }

  function compactFlightRouteEvidence(
    outboundRoute,
    returnRoute = null,
  ) {
    if (!outboundRoute) {
      return null;
    }
    const leg = (value) =>
      `${value.observed_departure_label || value.expected_departure_code}` +
      `→${value.observed_arrival_label || value.expected_arrival_code}` +
      `(${value.matches_expected === true ? "匹配" : "不匹配"})`;
    return flightReceiptText(
      returnRoute
        ? `去程 ${leg(outboundRoute)}；返程 ${leg(returnRoute)}`
        : `可见候选 ${leg(outboundRoute)}`,
      240,
    );
  }

  function compactFlightScheduleEvidence(
    outboundLeg,
    returnLeg = null,
  ) {
    if (!outboundLeg) {
      return null;
    }
    return flightReceiptText(
      returnLeg
        ? (
            `去程 ${outboundLeg.departure_at}→${outboundLeg.arrival_at}；` +
            `返程 ${returnLeg.departure_at}→${returnLeg.arrival_at}`
          )
        : (
            `${outboundLeg.departure_at}→${outboundLeg.arrival_at}`
          ),
      240,
    );
  }

  function noVisiblePriceFlightCandidate({
    candidateIndex,
    title,
    routeEvidence,
    scheduleEvidence,
  }) {
    return {
      candidate_index: candidateIndex,
      destination_airport_code: null,
      outbound_flight_numbers: [],
      outbound_segments: [],
      title: flightReceiptText(title, 180),
      route_evidence: flightReceiptText(routeEvidence, 240),
      schedule_evidence: flightReceiptText(scheduleEvidence, 240),
      price_evidence: null,
      currency: null,
      amount: null,
      price_basis: "unknown",
      price_classification: "no_visible_price",
      return_flight_numbers: [],
      return_segments: [],
      origin_airport_code: null,
    };
  }

  function pricedFlightCandidate({
    candidateIndex,
    title,
    routeEvidence,
    scheduleEvidence,
    price,
    outboundFlightNumbers = [],
    returnFlightNumbers = [],
    outboundSegments = [],
    returnSegments = [],
    originAirportCode = null,
    destinationAirportCode = null,
  }) {
    if (!price) {
      return noVisiblePriceFlightCandidate({
        candidateIndex,
        title,
        routeEvidence,
        scheduleEvidence,
      });
    }
    return {
      candidate_index: candidateIndex,
      destination_airport_code: destinationAirportCode,
      outbound_flight_numbers: outboundFlightNumbers,
      outbound_segments: outboundSegments,
      title: flightReceiptText(title, 180),
      route_evidence: flightReceiptText(routeEvidence, 240),
      schedule_evidence: flightReceiptText(scheduleEvidence, 240),
      price_evidence: price.price_evidence,
      currency: price.currency,
      amount: price.amount,
      price_basis: price.price_basis,
      price_classification: price.price_classification,
      return_flight_numbers: returnFlightNumbers,
      return_segments: returnSegments,
      origin_airport_code: originAirportCode,
    };
  }

  function ctripOutboundComparisonCandidates(root, query) {
    const timezones = routeTimezones(query);
    if (!timezones) {
      return [];
    }
    const controls = exactOutboundControls(
      "ctrip",
      root,
      query,
    ).slice(0, MAX_FLIGHT_SEARCH_RECEIPT_CANDIDATES);
    const candidates = [];
    const seenContainers = new Set();
    const addCandidateFromContainer = (container) => {
      if (
        !container ||
        candidates.length >= MAX_FLIGHT_SEARCH_RECEIPT_CANDIDATES ||
        seenContainers.has(container)
      ) {
        return false;
      }
      const tag = cleanText(container.tagName).toLowerCase();
      const text = cleanText(container.textContent);
      if (
        !DIAGNOSTIC_CONTAINER_TAGS.has(tag) ||
        !text ||
        text.length > 5000 ||
        !visibleEvidence(container)
      ) {
        return false;
      }
      const priceText = ctripFlightPriceEvidence(container);
      const price = flightComparisonPrice("ctrip", priceText);
      const outboundLeg = legFromVisibleText(
        text,
        query.start_date,
        timezones.origin_offset,
        timezones.destination_offset,
      );
      const outboundRoute = flightLegRouteEvidence(
        text,
        query,
        "outbound",
        "flight_receipt_ctrip_outbound_comparison",
      );
      if (
        !price ||
        price.price_classification !== "starting_or_estimated" ||
        taxesIncluded(priceText) !== true ||
        !outboundLeg ||
        !outboundRoute ||
        outboundRoute.matches_expected !== true
      ) {
        return false;
      }
      seenContainers.add(container);
      candidates.push(
        pricedFlightCandidate({
          candidateIndex: candidates.length,
          title:
            flightCarrierText(container) ||
            "携程可见去程起价",
          routeEvidence: compactFlightRouteEvidence(
            outboundRoute,
          ),
          scheduleEvidence: compactFlightScheduleEvidence(
            outboundLeg,
          ),
          price,
        }),
      );
      return true;
    };
    for (const control of controls) {
      let container = control.parentElement;
      let depth = 0;
      while (container && depth < 10) {
        const tag = cleanText(container.tagName).toLowerCase();
        if (
          container === root.body ||
          container === root.documentElement ||
          DIAGNOSTIC_BOUNDARY_TAGS.has(tag)
        ) {
          break;
        }
        if (addCandidateFromContainer(container)) {
          break;
        }
        container = container.parentElement;
        depth += 1;
      }
      if (candidates.length >= MAX_FLIGHT_SEARCH_RECEIPT_CANDIDATES) {
        break;
      }
    }
    // A comparison receipt is read-only evidence and does not click an
    // outbound control. Styled "选为去程" controls may be promoted separately
    // only after exactOutboundControls has proved their non-transactional
    // flight-operate ancestry. Here we reuse only semantic cards that already
    // proved a same-card carrier, route, schedule, tax label, single atomic
    // currency amount, and outbound selection label.
    for (const card of ctripFlightSemanticCards(root)) {
      addCandidateFromContainer(card);
      if (candidates.length >= MAX_FLIGHT_SEARCH_RECEIPT_CANDIDATES) {
        break;
      }
    }
    return candidates;
  }

  function stagedFlightReceiptCandidates(provider, root, query, driver) {
    const timezones = routeTimezones(query);
    const summary = selectedOutboundSummary(provider, root, query);
    if (!timezones) {
      return [];
    }
    if (!summary) {
      const comparisonCandidates = provider === "ctrip"
        ? ctripOutboundComparisonCandidates(root, query)
        : [];
      if (comparisonCandidates.length) {
        return comparisonCandidates;
      }
      const diagnostic = provider === "ctrip"
        ? ctripOutboundAvailabilityDiagnostic(root, query)
        : null;
      return diagnostic
        ? diagnostic.stage_evidence
            .map((evidence, candidateIndex) =>
              noVisiblePriceFlightCandidate({
                candidateIndex,
                title: evidence.text_summary,
                routeEvidence: null,
                scheduleEvidence: null,
              }),
            )
            .filter((candidate) => candidate.title)
        : [];
    }
    const effectiveDriver = provider === "tongcheng"
      ? tongchengAutoSelectedOutboundDriver(summary, query, driver) || driver
      : driver;
    const summaryText = cleanText(summary.textContent);
    const outboundLeg = stagedProviderLegFromVisibleText(
      provider,
      summaryText,
      query.start_date,
      timezones.origin_offset,
      timezones.destination_offset,
    );
    const outboundRoute = flightLegRouteEvidence(
      summaryText,
      query,
      "outbound",
      "flight_receipt_selected_outbound",
    );
    const visibleCarrier = provider === "tongcheng"
      ? tongchengSelectedOutboundCarrier(summary)
      : flightCarrierText(summary);
    const selectedCarrier =
      effectiveDriver &&
      effectiveDriver.selected_outbound &&
      cleanText(effectiveDriver.selected_outbound.carrier_text);
    if (
      !outboundLeg ||
      !outboundRoute ||
      outboundRoute.matches_expected !== true ||
      !selectedSummaryMatchesDriver(
        provider,
        summary,
        effectiveDriver,
        outboundLeg,
        visibleCarrier,
      )
    ) {
      return [];
    }
    const candidates = [];
    for (const card of stagedReturnCards(provider, root, query)) {
      const returnLeg = stagedProviderLegFromVisibleText(
        provider,
        card.textContent,
        query.end_date,
        timezones.destination_offset,
        timezones.origin_offset,
      );
      const returnRoute = flightLegRouteEvidence(
        card.textContent,
        query,
        "return",
        "flight_receipt_return_card",
      );
      const priceText = stagedFlightPriceEvidence(provider, card);
      const price = flightComparisonPrice(provider, priceText);
      if (
        !returnLeg ||
        !returnRoute ||
        returnRoute.matches_expected !== true ||
        !price ||
        price.price_classification !== "starting_or_estimated" ||
        taxesIncluded(priceText) !== true
      ) {
        continue;
      }
      const returnCarrier = flightCarrierText(card);
      candidates.push(
        pricedFlightCandidate({
          candidateIndex: candidates.length,
          title:
            `${visibleCarrier || selectedCarrier || "已选去程"} + ` +
            `${returnCarrier || "可见返程"}`,
          routeEvidence: compactFlightRouteEvidence(
            outboundRoute,
            returnRoute,
          ),
          scheduleEvidence: compactFlightScheduleEvidence(
            outboundLeg,
            returnLeg,
          ),
          price,
        }),
      );
      if (candidates.length >= MAX_FLIGHT_SEARCH_RECEIPT_CANDIDATES) {
        break;
      }
    }
    return candidates;
  }

  function ctripFlightReceiptCandidates(root, query, driver) {
    return stagedFlightReceiptCandidates("ctrip", root, query, driver);
  }

  function fliggyFlightReceiptCandidates(root, query) {
    const diagnostic = fliggyAlternateOriginDiagnostic(root, query);
    if (!diagnostic || diagnostic.outcome !== "alternate_origin_only") {
      return [];
    }
    const nodes = visibleNodes(
      root,
      ["li.nearby-item", "[class~='nearby-item']"],
      MAX_FLIGHT_SEARCH_RECEIPT_CANDIDATES,
    );
    const candidates = [];
    for (const node of nodes) {
      const observation = flightRouteObservation(node.textContent, query);
      if (
        !observation ||
        observation.origin_matches_requested ||
        !observation.destination_matches_requested
      ) {
        continue;
      }
      const times = visibleTimeTokens(node.textContent);
      candidates.push(
        noVisiblePriceFlightCandidate({
          candidateIndex: candidates.length,
          title:
            flightCarrierText(node) ||
            "飞猪邻近出发地航班候选",
          routeEvidence:
            `${observation.origin_label || "未知出发地"}→` +
            `${observation.destination_label || "未知目的地"}；` +
            `请求 ${query.origin_code}→${query.destination_code}`,
          scheduleEvidence:
            times.length >= 2
              ? `可见时间 ${times[0]}→${times[1]}`
              : null,
        }),
      );
    }
    return candidates;
  }

  function qunarFlightReceiptCandidates(root, query, driver) {
    const timezones = routeTimezones(query);
    if (!timezones) {
      return [];
    }
    const candidates = [];
    const cards = visibleNodes(
      root,
      [".m-airfly-lst .b-airfly", ".b-airfly"],
      MAX_FLIGHT_SEARCH_RECEIPT_CANDIDATES,
    );
    for (const card of cards) {
      const trips = visibleNodes(card, [".s-trip"], 3);
      if (trips.length !== 2) {
        continue;
      }
      const outboundLeg = legFromQunarTrip(
        trips[0],
        query.start_date,
        timezones.origin_offset,
        timezones.destination_offset,
      );
      const returnLeg = legFromQunarTrip(
        trips[1],
        query.end_date,
        timezones.destination_offset,
        timezones.origin_offset,
      );
      const outboundFlightNumbers = qunarVisibleFlightNumbers(
        trips[0].textContent,
      );
      const returnFlightNumbers = qunarVisibleFlightNumbers(
        trips[1].textContent,
      );
      const outboundRoute = flightLegRouteEvidence(
        trips[0].textContent,
        query,
        "outbound",
        "flight_receipt_qunar_leg",
        outboundLeg && outboundLeg.departure_place,
        outboundLeg && outboundLeg.arrival_place,
      );
      const returnRoute = flightLegRouteEvidence(
        trips[1].textContent,
        query,
        "return",
        "flight_receipt_qunar_leg",
        returnLeg && returnLeg.departure_place,
        returnLeg && returnLeg.arrival_place,
      );
      if (
        !outboundLeg ||
        !returnLeg ||
        !outboundRoute ||
        outboundRoute.matches_expected !== true ||
        !returnRoute ||
        returnRoute.matches_expected !== true
      ) {
        continue;
      }
      const priceText = qunarPriceEvidence(card, {
        allowGeometry:
          !(driver && driver.qunar_geometry_price_disabled === true),
      }).priceText;
      const outboundStructured = qunarStructuredFlightSegments(
        trips[0],
        outboundFlightNumbers,
        query.origin_code,
        query.destination_code,
        query.start_date,
        outboundLeg,
      );
      const returnStructured = qunarStructuredFlightSegments(
        trips[1],
        returnFlightNumbers,
        query.destination_code,
        query.origin_code,
        query.end_date,
        returnLeg,
      );
      const directOutbound = qunarDirectFlightSegment(
        trips[0],
        outboundLeg,
        outboundFlightNumbers,
        query.origin_code,
        query.destination_code,
        outboundRoute,
      );
      const directReturn = qunarDirectFlightSegment(
        trips[1],
        returnLeg,
        returnFlightNumbers,
        query.destination_code,
        query.origin_code,
        returnRoute,
      );
      const structuredReceiptSegments = qunarReceiptSegmentsFromStructured(
        outboundStructured,
        returnStructured,
        outboundFlightNumbers,
        returnFlightNumbers,
        query.origin_code,
        query.destination_code,
      );
      const anyStructuredEvidence = Boolean(outboundStructured || returnStructured);
      const outboundSegments = anyStructuredEvidence
        ? structuredReceiptSegments.outbound_segments
        : directOutbound.length
          ? directOutbound
          : qunarVisibleMultiFlightSegments(
              trips[0],
              outboundLeg,
              outboundFlightNumbers,
              query.origin_code,
              query.destination_code,
              query.start_date,
            );
      const returnSegments = anyStructuredEvidence
        ? structuredReceiptSegments.return_segments
        : directReturn.length
          ? directReturn
          : qunarVisibleMultiFlightSegments(
              trips[1],
              returnLeg,
              returnFlightNumbers,
              query.destination_code,
              query.origin_code,
              query.end_date,
            );
      candidates.push(
        pricedFlightCandidate({
          candidateIndex: candidates.length,
          title:
            flightCarrierText(card) ||
            "去哪儿可见完整往返组合",
          routeEvidence: compactFlightRouteEvidence(
            outboundRoute,
            returnRoute,
          ),
          scheduleEvidence: compactFlightScheduleEvidence(
            outboundLeg,
            returnLeg,
          ),
          price: flightComparisonPrice("qunar", priceText),
          outboundFlightNumbers,
          returnFlightNumbers,
          outboundSegments,
          returnSegments,
          originAirportCode: query.origin_code,
          destinationAirportCode: query.destination_code,
        }),
      );
    }
    return candidates;
  }

  async function validateFlightSearchReceipt(
    receipt,
    receiptSha256,
    expected = null,
  ) {
    const rejected = (reason) => ({ valid: false, reason });
    if (
      !hasExactObjectKeys(receipt, FLIGHT_SEARCH_RECEIPT_KEYS) ||
      !hasExactObjectKeys(
        receipt && receipt.confirmed_query,
        FLIGHT_SEARCH_CONFIRMED_QUERY_KEYS,
      ) ||
      !Array.isArray(receipt && receipt.candidate_summaries) ||
      receipt.candidate_summaries.some(
        (candidate) =>
          !hasExactObjectKeys(
            candidate,
            FLIGHT_SEARCH_CANDIDATE_KEYS,
          ),
      )
    ) {
      return rejected("receipt_shape_invalid");
    }
    const query = receipt.confirmed_query;
    const safePageUrl = safeProviderDetailUrl(
      receipt.provider,
      receipt.page_url,
      receipt.page_url,
    );
    if (
      receipt.schema_version !==
        FLIGHT_SEARCH_RECEIPT_SCHEMA_VERSION ||
      receipt.parser_version !== PARSER_VERSION ||
      !["ctrip", "fliggy", "qunar", "tongcheng"].includes(receipt.provider) ||
      ![
        "comparison_price_only",
        "bounded_no_exact_quote",
      ].includes(receipt.state) ||
      receipt.confirmation_scope !== "confirmed_visible_search" ||
      receipt.explicit_empty_evidence !== null ||
      !Number.isInteger(receipt.scan_limit) ||
      receipt.scan_limit < 1 ||
      receipt.scan_limit > MAX_FLIGHT_SEARCH_RECEIPT_CANDIDATES ||
      !Number.isInteger(receipt.scanned_count) ||
      receipt.scanned_count < 1 ||
      receipt.scanned_count > receipt.scan_limit ||
      receipt.candidate_summaries.length !== receipt.scanned_count ||
      typeof receipt.page_url !== "string" ||
      !safePageUrl ||
      safePageUrl !== receipt.page_url ||
      typeof receipt.captured_at !== "string" ||
      !/(?:Z|[+-]\d{2}:\d{2})$/.test(receipt.captured_at) ||
      Number.isNaN(Date.parse(receipt.captured_at)) ||
      typeof query.origin !== "string" ||
      query.origin !== cleanText(query.origin) ||
      !query.origin ||
      typeof query.destination !== "string" ||
      query.destination !== cleanText(query.destination) ||
      !query.destination ||
      !/^\d{4}-\d{2}-\d{2}$/.test(query.start_date) ||
      !/^\d{4}-\d{2}-\d{2}$/.test(query.end_date) ||
      Date.parse(`${query.end_date}T00:00:00Z`) <=
        Date.parse(`${query.start_date}T00:00:00Z`) ||
      !Number.isInteger(query.adults) ||
      query.adults < 1 ||
      query.adults > 9 ||
      !/^[A-Z]{3}$/.test(query.origin_code) ||
      !/^[A-Z]{3}$/.test(query.destination_code)
    ) {
      return rejected("receipt_contract_invalid");
    }
    let priceBearingCount = 0;
    for (const [index, candidate] of
      receipt.candidate_summaries.entries()) {
      const textual = [
        candidate.title,
        candidate.route_evidence,
        candidate.schedule_evidence,
        candidate.price_evidence,
      ];
      const textualLimits = [180, 240, 240, 180];
      if (
        candidate.candidate_index !== index ||
        !textual.some(
          (value) =>
            typeof value === "string" && value.trim().length > 0,
        ) ||
        textual.some(
          (value, textIndex) =>
            value !== null &&
            (
              typeof value !== "string" ||
              value !== value.trim() ||
              value.length > textualLimits[textIndex]
            ),
        )
      ) {
        return rejected("candidate_evidence_invalid");
      }
      const priced = [
        "comparison_only",
        "starting_or_estimated",
      ].includes(candidate.price_classification);
      if (priced) {
        priceBearingCount += 1;
        if (
          typeof candidate.price_evidence !== "string" ||
          !candidate.price_evidence ||
          !/^[A-Z]{3}$/.test(candidate.currency) ||
          typeof candidate.amount !== "number" ||
          !Number.isFinite(candidate.amount) ||
          candidate.amount <= 0 ||
          !["per_person", "total_party"].includes(
            candidate.price_basis,
          )
        ) {
          return rejected("candidate_price_contract_invalid");
        }
      } else if (
        candidate.price_classification !== "no_visible_price" ||
        candidate.price_evidence !== null ||
        candidate.currency !== null ||
        candidate.amount !== null ||
        candidate.price_basis !== "unknown"
      ) {
        return rejected("candidate_empty_price_contract_invalid");
      }
    }
    if (
      (
        receipt.state === "comparison_price_only" &&
        priceBearingCount < 1
      ) ||
      (
        receipt.state === "bounded_no_exact_quote" &&
        priceBearingCount !== 0
      )
    ) {
      return rejected("receipt_state_evidence_mismatch");
    }
    if (expected) {
      if (
        expected.provider !== receipt.provider ||
        (
          expected.page_url &&
          expected.page_url !== receipt.page_url
        )
      ) {
        return rejected("receipt_context_mismatch");
      }
      for (const field of FLIGHT_SEARCH_CONFIRMED_QUERY_KEYS) {
        const expectedValue =
          field === "origin_code" || field === "destination_code"
            ? cleanText(expected.query && expected.query[field]).toUpperCase()
            : field === "adults"
              ? Number(expected.query && expected.query[field])
              : cleanText(expected.query && expected.query[field]);
        if (query[field] !== expectedValue) {
          return rejected("receipt_query_mismatch");
        }
      }
    }
    if (
      typeof receiptSha256 !== "string" ||
      !/^[a-f0-9]{64}$/.test(receiptSha256) ||
      await sha256(canonicalJson(receipt)) !== receiptSha256
    ) {
      return rejected("receipt_sha256_invalid");
    }
    return { valid: true, reason: null };
  }

  async function createFlightSearchReceiptFromCandidates({
    provider,
    page_url: pageUrl,
    captured_at: capturedAt,
    query,
    driver,
    candidate_summaries: summaries,
  }) {
    try {
      const confirmedQuery = flightReceiptConfirmedQuery(query, driver);
      const safePageUrl = safeProviderDetailUrl(
        provider,
        pageUrl,
        pageUrl,
      );
      if (
        !confirmedQuery ||
        !safePageUrl ||
        !Array.isArray(summaries) ||
        !summaries.length ||
        summaries.length > MAX_FLIGHT_SEARCH_RECEIPT_CANDIDATES
      ) {
        return null;
      }
      const candidateSummaries = summaries.map(
        (summary, candidateIndex) => ({
          candidate_index: candidateIndex,
          title: summary && summary.title,
          route_evidence: summary && summary.route_evidence,
          schedule_evidence: summary && summary.schedule_evidence,
          price_evidence: summary && summary.price_evidence,
          currency: summary && summary.currency,
          amount: summary && summary.amount,
          price_basis: summary && summary.price_basis,
          price_classification:
            summary && summary.price_classification,
          outbound_flight_numbers:
            summary && summary.outbound_flight_numbers || [],
          return_flight_numbers:
            summary && summary.return_flight_numbers || [],
          outbound_segments: summary && summary.outbound_segments || [],
          return_segments: summary && summary.return_segments || [],
          origin_airport_code:
            summary && summary.origin_airport_code || null,
          destination_airport_code:
            summary && summary.destination_airport_code || null,
        }),
      );
      const priceBearing = candidateSummaries.some((candidate) =>
        ["comparison_only", "starting_or_estimated"].includes(
          candidate.price_classification,
        )
      );
      const receipt = {
        schema_version: FLIGHT_SEARCH_RECEIPT_SCHEMA_VERSION,
        parser_version: PARSER_VERSION,
        provider,
        state: priceBearing
          ? "comparison_price_only"
          : "bounded_no_exact_quote",
        confirmed_query: confirmedQuery,
        confirmation_scope: "confirmed_visible_search",
        scan_limit: MAX_FLIGHT_SEARCH_RECEIPT_CANDIDATES,
        scanned_count: candidateSummaries.length,
        candidate_summaries: candidateSummaries,
        explicit_empty_evidence: null,
        page_url: safePageUrl,
        captured_at: capturedAt,
      };
      const receiptSha256 = await sha256(canonicalJson(receipt));
      const validation = await validateFlightSearchReceipt(
        receipt,
        receiptSha256,
        {
          provider,
          page_url: safePageUrl,
          query,
        },
      );
      return validation.valid
        ? { receipt, receipt_sha256: receiptSha256 }
        : null;
    } catch {
      return null;
    }
  }

  async function createFlightSearchReceipt(
    provider,
    root,
    pageUrl,
    capturedAt,
    query,
    driver,
  ) {
    try {
      const summaries = provider === "ctrip" || provider === "tongcheng"
        ? stagedFlightReceiptCandidates(provider, root, query, driver)
        : provider === "fliggy"
          ? fliggyFlightReceiptCandidates(root, query)
          : provider === "qunar"
            ? qunarFlightReceiptCandidates(root, query, driver)
            : [];
      return await createFlightSearchReceiptFromCandidates({
        provider,
        page_url: pageUrl,
        captured_at: capturedAt,
        query,
        driver,
        candidate_summaries: summaries,
      });
    } catch {
      return null;
    }
  }

  function flightTerminalFailureCode(validatedReceipt) {
    return validatedReceipt ? "extraction_error" : "dom_drift";
  }

  async function flightQuoteFromCombination({
    provider,
    root,
    pageUrl,
    capturedAt,
    query,
    driver,
    title,
    carrier,
    priceText,
    priceBasisValue,
    taxEvidence,
    availabilityEvidence,
    outboundLeg,
    returnLeg,
    outboundFlightNumbers = [],
    returnFlightNumbers = [],
    outboundSegments = [],
    returnSegments = [],
    outboundRouteEvidence,
    returnRouteEvidence,
    selectionEvidence,
    workflowKind,
    priceContractOverride = null,
    priceBasisSource = null,
  }) {
    const priceContract = priceContractOverride || flightPriceContract(priceText);
    const amount = priceContract.amount;
    const actionTrace = validatedActionTrace(driver);
    const partyStatus = partyAvailabilityStatus(provider, query, driver);
    const outboundDeparture = new Date(
      outboundLeg && outboundLeg.departure_at,
    );
    const outboundArrival = new Date(
      outboundLeg && outboundLeg.arrival_at,
    );
    const returnDeparture = new Date(
      returnLeg && returnLeg.departure_at,
    );
    const returnArrival = new Date(
      returnLeg && returnLeg.arrival_at,
    );
    const chronological =
      ![
        outboundDeparture,
        outboundArrival,
        returnDeparture,
        returnArrival,
      ].some((value) => Number.isNaN(value.getTime())) &&
      outboundArrival > outboundDeparture &&
      returnDeparture > outboundArrival &&
      returnArrival > returnDeparture;
    if (
      !priceContract.valid ||
      priceContract.price_basis !== priceBasisValue ||
      (
        cleanText(query && query.currency).toUpperCase() &&
        priceContract.currency !==
          cleanText(query && query.currency).toUpperCase()
      ) ||
      taxesIncluded(taxEvidence) !== true ||
      !availabilityEvidence ||
      !outboundLeg ||
      !returnLeg ||
      !outboundRouteEvidence ||
      outboundRouteEvidence.matches_expected !== true ||
      !returnRouteEvidence ||
      returnRouteEvidence.matches_expected !== true ||
      !chronological ||
      !actionTrace ||
      !selectionEvidence ||
      !partyStatus
    ) {
      return null;
    }
    const normalizedQuery = safeQuery(query);
    const combinationId = await sha256(
      canonicalJson({
        outbound_arrival_at: outboundLeg.arrival_at,
        outbound_departure_at: outboundLeg.departure_at,
        price_text: cleanText(priceText),
        provider,
        return_arrival_at: returnLeg.arrival_at,
        return_departure_at: returnLeg.departure_at,
        outbound_route_evidence: outboundRouteEvidence,
        return_route_evidence: returnRouteEvidence,
        selection_evidence: selectionEvidence,
      }),
    );
    const terms = allText(root, TERMS_SELECTORS);
    const baggageText =
      firstText(root, FLIGHT_DETAIL_SELECTORS.baggage) ||
      firstMatching(terms, /行李|baggage/i) ||
      null;
    const details = {
      query: normalizedQuery,
      driver: driver || null,
      price_text: cleanText(priceText),
      visible_terms: terms,
      extraction: "visible_dom",
      origin:
        driver && driver.confirmed_query && driver.confirmed_query.origin ||
        normalizedQuery.origin,
      destination:
        driver && driver.confirmed_query && driver.confirmed_query.destination ||
        normalizedQuery.destination,
      adults:
        driver && driver.confirmed_query &&
        Number.isInteger(driver.confirmed_query.adults)
          ? driver.confirmed_query.adults
          : normalizedQuery.adults,
      outbound_departure_at: outboundLeg.departure_at,
      outbound_arrival_at: outboundLeg.arrival_at,
      return_departure_at: returnLeg.departure_at,
      return_arrival_at: returnLeg.arrival_at,
      carrier_text: carrier,
      connection_text:
        firstText(root, FLIGHT_DETAIL_SELECTORS.connection) ||
        firstMatching(terms, /中转|经停|转机/) ||
        null,
      baggage_text: baggageText,
      checked_baggage_per_adult_kg: checkedBaggageKg(baggageText),
      workflow_kind: workflowKind,
      combination_status: "round_trip_complete",
      combination_id: combinationId,
      journey_price_scope: "round_trip",
      price_finality: "final_for_combination",
      price_basis_evidence: priceContract.evidence,
      price_basis_source: priceBasisSource,
      tax_evidence: cleanText(taxEvidence),
      availability: "available",
      availability_evidence: cleanText(availabilityEvidence),
      party_availability_status: partyStatus,
      selection_evidence: cleanText(selectionEvidence),
      action_trace: actionTrace,
      outbound_leg: outboundLeg,
      return_leg: returnLeg,
      ...(outboundFlightNumbers.length
        ? { outbound_flight_numbers: outboundFlightNumbers }
        : {}),
      ...(returnFlightNumbers.length
        ? { return_flight_numbers: returnFlightNumbers }
        : {}),
      ...(outboundSegments.length
        ? { outbound_segments: outboundSegments }
        : {}),
      ...(returnSegments.length
        ? { return_segments: returnSegments }
        : {}),
      outbound_route_evidence: outboundRouteEvidence,
      return_route_evidence: returnRouteEvidence,
    };
    const currency = priceContract.currency;
    const evidence = canonicalJson({
      amount: String(amount),
      currency,
      details,
      provider,
      kind: "flight",
      page_url: pageUrl,
      price_basis: priceBasisValue,
      taxes_included: true,
      title,
    });
    if (evidence.length > MAX_VISIBLE_EVIDENCE_CHARS) {
      return null;
    }
    return {
      provider,
      kind: "flight",
      page_url: pageUrl,
      captured_at: capturedAt,
      parser_version: PARSER_VERSION,
      visible_evidence: evidence,
      evidence_sha256: await sha256(evidence),
      currency,
      amount,
      price_basis: priceBasisValue,
      taxes_included: true,
      title,
      details,
    };
  }

  async function extractQunarRoundTrips(
    root,
    pageUrl,
    capturedAt,
    query,
    driver,
  ) {
    const timezones = routeTimezones(query);
    if (!timezones) {
      return [];
    }
    const exactPartySearchContext = qunarExactPartySearchContext(
      root,
      query,
      driver,
    );
    const quotes = [];
    const stabilityPreviews = [];
    const cards = visibleNodes(root, [".m-airfly-lst .b-airfly", ".b-airfly"]);
    for (const card of cards) {
      const trips = visibleNodes(card, [".s-trip"], 3);
      if (trips.length !== 2) {
        continue;
      }
      const outboundLeg = legFromQunarTrip(
        trips[0],
        query.start_date,
        timezones.origin_offset,
        timezones.destination_offset,
      );
      const returnLeg = legFromQunarTrip(
        trips[1],
        query.end_date,
        timezones.destination_offset,
        timezones.origin_offset,
      );
      const outboundFlightNumbers = qunarVisibleFlightNumbers(
        trips[0].textContent,
      );
      const returnFlightNumbers = qunarVisibleFlightNumbers(
        trips[1].textContent,
      );
      const priceEvidence = qunarPriceEvidence(card, {
        allowGeometry:
          !(driver && driver.qunar_geometry_price_disabled === true),
      });
      const visiblePriceText = priceEvidence.priceText;
      // The adult count in a URL/form is not proof that the visible amount is
      // a party total.  A separately captured same-product comparison is the
      // only accepted proof; never rewrite the visible text to manufacture it.
      const totalPartyPriceText = null;
      const priceText = visiblePriceText;
      const taxEvidence = explicitTaxEvidence(card, visiblePriceText);
      const priceContract = flightPriceContract(priceText);
      const visibleAvailability = flightAvailabilityEvidence(card);
      const availabilityEvidence =
        visibleAvailability;
      const outboundRouteEvidence = flightLegRouteEvidence(
        trips[0].textContent,
        query,
        "outbound",
        "combined_card_leg",
        outboundLeg && outboundLeg.departure_place,
        outboundLeg && outboundLeg.arrival_place,
      );
      const returnRouteEvidence = flightLegRouteEvidence(
        trips[1].textContent,
        query,
        "return",
        "combined_card_leg",
        returnLeg && returnLeg.departure_place,
        returnLeg && returnLeg.arrival_place,
      );
      const outboundSegments = qunarDirectFlightSegment(
        trips[0],
        outboundLeg,
        outboundFlightNumbers,
        query.origin_code,
        query.destination_code,
        outboundRouteEvidence,
      );
      const outboundSegmentsWithConnection = outboundSegments.length
        ? outboundSegments
        : qunarVisibleMultiFlightSegments(
            trips[0],
            outboundLeg,
            outboundFlightNumbers,
            query.origin_code,
            query.destination_code,
            query.start_date,
          );
      const returnSegments = qunarDirectFlightSegment(
        trips[1],
        returnLeg,
        returnFlightNumbers,
        query.destination_code,
        query.origin_code,
        returnRouteEvidence,
      );
      const returnSegmentsWithConnection = returnSegments.length
        ? returnSegments
        : qunarVisibleMultiFlightSegments(
            trips[1],
            returnLeg,
            returnFlightNumbers,
            query.destination_code,
            query.origin_code,
            query.end_date,
          );
      const geometryStabilityKey =
        priceEvidence.evidenceSource ===
          "geometry_clipped_visible_digit_sequence" &&
        false &&
        taxEvidence &&
        outboundLeg &&
        returnLeg &&
        outboundRouteEvidence &&
        outboundRouteEvidence.matches_expected === true &&
        returnRouteEvidence &&
        returnRouteEvidence.matches_expected === true
          ? await sha256(canonicalJson({
              amount: priceContract.amount,
              currency: priceContract.currency,
              label: "含税总价",
              outbound_departure_at: outboundLeg.departure_at,
              outbound_arrival_at: outboundLeg.arrival_at,
              return_departure_at: returnLeg.departure_at,
              return_arrival_at: returnLeg.arrival_at,
              route:
                `${query.origin_code}-${query.destination_code}-` +
                `${query.origin_code}`,
            }))
          : null;
      const geometryStable =
        geometryStabilityKey &&
        driver &&
        Array.isArray(driver.qunar_geometry_stability_keys) &&
        driver.qunar_geometry_stability_keys.includes(
          geometryStabilityKey,
        );
      if (geometryStabilityKey && !geometryStable) {
        stabilityPreviews.push({
          stability_key: geometryStabilityKey,
          evidence_source:
            "geometry_clipped_visible_digit_sequence",
        });
        continue;
      }
      const exactPerPerson =
        visiblePriceText &&
        /人均含税价/.test(cleanText(visiblePriceText)) &&
        priceContract.valid === true &&
        priceContract.price_basis === "per_person" &&
        Boolean(visibleAvailability);
      const exactPartyTotal = false;
      if (
        (!exactPerPerson && !exactPartyTotal) ||
        !taxEvidence ||
        !availabilityEvidence ||
        !outboundRouteEvidence ||
        outboundRouteEvidence.matches_expected !== true ||
        !returnRouteEvidence ||
        returnRouteEvidence.matches_expected !== true
      ) {
        continue;
      }
      const carrier =
        flightCarrierText(card) ||
        "去哪儿完整往返组合";
      const quote = await flightQuoteFromCombination({
        provider: "qunar",
        root: card,
        pageUrl,
        capturedAt,
        query,
        driver,
        title: `${carrier} ${query.origin}往返${query.destination}`,
        carrier,
        priceText,
        priceBasisValue: priceContract.price_basis,
        taxEvidence,
        availabilityEvidence,
        outboundLeg,
        returnLeg,
        outboundFlightNumbers,
        returnFlightNumbers,
        outboundSegments: outboundSegmentsWithConnection,
        returnSegments: returnSegmentsWithConnection,
        outboundRouteEvidence,
        returnRouteEvidence,
        selectionEvidence:
          exactPartyTotal
            ? (
                "exact_party_search_context；同一可见 .b-airfly 卡内双航段；" +
                (
                  priceEvidence.evidenceSource ===
                    "geometry_clipped_visible_digit_sequence"
                    ? "裁剪数字列每列仅一个可见字形且两次受控读取稳定一致；"
                    : "稳定 title 金额与可见单字符序列一致；"
                ) +
                "未点击订票"
              )
            : (
                "去哪儿组合卡内同时存在两个可见 .s-trip 航段；" +
                "未点击 .btn-book"
              ),
        workflowKind: "combined_roundtrip_card",
      });
      if (quote) {
        quotes.push(quote);
      }
      if (quotes.length >= MAX_RETURN_COMBINATIONS) {
        break;
      }
    }
    return {
      quotes,
      stability_previews: stabilityPreviews
        .filter(
          (item, index, all) =>
            all.findIndex(
              (other) =>
                other.stability_key === item.stability_key,
            ) === index,
        )
        .slice(0, MAX_RETURN_COMBINATIONS),
    };
  }

  function ctripSemanticReturnCards(root, query) {
    const controls = matchingVisibleNodes(
      root,
      [
        "button",
        "a",
        "[role='button']",
        "[class*='btn']",
        "[class*='button']",
      ].join(","),
      CTRIP_FLIGHT_RETURN_CONTROL_PATTERN,
      80,
      40,
    ).filter((control) => {
      const label = cleanText(
        [
          control.textContent,
          control.getAttribute("aria-label"),
          control.getAttribute("title"),
        ].filter(Boolean).join(" "),
      );
      return (
        CTRIP_FLIGHT_RETURN_CONTROL_PATTERN.test(label) &&
        control.disabled !== true &&
        control.getAttribute("disabled") === null &&
        control.getAttribute("aria-disabled") !== "true"
      );
    });
    const cards = [];
    for (const control of controls) {
      const card = semanticFlightCardFromControl(
        "ctrip",
        control,
        query,
        "return",
      );
      if (card && !cards.includes(card)) {
        cards.push(card);
      }
      if (cards.length >= 30) {
        break;
      }
    }
    return cards;
  }

  function stagedReturnCards(provider, root, query) {
    const selectors = provider === "fliggy"
      ? [".J_FlightItem", ".flightItem"]
      : provider === "tongcheng"
        // Tongcheng nests elements such as `.flight-item-content` inside the
        // canonical `.flight-item`. A substring selector observes the same
        // visible itinerary twice and makes the safe selection deliberately
        // fail as ambiguous. Keep the action scope on the canonical card.
        ? [".flight-item"]
        : [
          "[data-testid*='flight-card']",
          "[class*='flight-item']",
          "[class*='flightListItem']",
          ".flight-list-item",
          "[data-tripchord-fixture='return-card']",
        ];
    const cards = visibleNodes(root, selectors).filter((card) => {
      const text = cleanText(card.textContent);
      const availability = provider === "tongcheng"
        ? tongchengFlightAvailabilityEvidence(card)
        : flightAvailabilityEvidence(card);
      if (provider === "tongcheng") {
        const returnRoute = flightLegRouteEvidence(
          text,
          query,
          "return",
          "tongcheng_return_card_discovery",
        );
        return Boolean(
          availability &&
          returnRoute &&
          returnRoute.matches_expected === true &&
          tongchengVisibleRouteEndpointCodesMatch(
            text,
            query,
            "return",
          ) &&
          stagedFlightPriceEvidence(provider, card)
        );
      }
      return (
        /往返总价|往返含税价/.test(text) &&
        (
          /选为返程|选择返程|返程航班|返程/.test(text) ||
          (
            provider === "ctrip" &&
            availability &&
            CTRIP_FLIGHT_RETURN_CONTROL_PATTERN.test(availability)
          )
        )
      );
    });
    if (provider === "ctrip") {
      for (const semantic of ctripSemanticReturnCards(root, query)) {
        if (!cards.includes(semantic)) {
          cards.push(semantic);
        }
      }
    }
    return cards.slice(0, 30);
  }

  function stagedFlightPriceEvidence(provider, card) {
    if (provider === "ctrip") {
      return (
        ctripFlightPriceEvidence(card) ||
        firstText(card, PRICE_SELECTORS)
      );
    }
    return firstText(card, PRICE_SELECTORS);
  }

  async function tongchengReturnSelectionCandidates(root, query, driver) {
    const summary = selectedOutboundSummary("tongcheng", root, query);
    const effectiveDriver = summary
      ? tongchengAutoSelectedOutboundDriver(summary, query, driver)
      : null;
    const timezones = routeTimezones(query);
    if (!summary || !effectiveDriver || !timezones) {
      return [];
    }
    const summaryText = cleanText(summary.textContent);
    const outboundLeg = stagedProviderLegFromVisibleText(
      "tongcheng",
      summaryText,
      query.start_date,
      timezones.origin_offset,
      timezones.destination_offset,
    );
    const visibleCarrier = tongchengSelectedOutboundCarrier(summary);
    if (
      !outboundLeg ||
      !visibleCarrier ||
      !selectedSummaryMatchesDriver(
        "tongcheng",
        summary,
        effectiveDriver,
        outboundLeg,
        visibleCarrier,
      )
    ) {
      return [];
    }
    const results = [];
    for (const [cardIndex, card] of stagedReturnCards(
      "tongcheng",
      root,
      query,
    ).entries()) {
      const cardText = cleanText(card.textContent);
      const returnLeg = stagedProviderLegFromVisibleText(
        "tongcheng",
        cardText,
        query.end_date,
        timezones.destination_offset,
        timezones.origin_offset,
      );
      const route = flightLegRouteEvidence(
        cardText,
        query,
        "return",
        "tongcheng_return_selection_candidate",
      );
      const priceText = stagedFlightPriceEvidence("tongcheng", card);
      const comparisonAmount = strictVisibleFlightAmount(priceText);
      const control = visibleNodes(card, [".flight-btn"], 4).find((node) => {
        const label = cleanText(node.textContent);
        const href = cleanText(node.getAttribute("href")).toLowerCase();
        return (
          /^(?:余\s*\d+\s*张\s*)?选择$/.test(label) &&
          node.disabled !== true &&
          node.getAttribute("disabled") === null &&
          node.getAttribute("aria-disabled") !== "true" &&
          !UNSAFE_OUTBOUND_TRANSACTION_PATTERN.test(label) &&
          !/order|book|pay|checkout|预订|下单|支付/.test(href)
        );
      });
      const carrier = flightCarrierText(card) || visibleCarrier;
      if (
        !returnLeg ||
        !route ||
        route.matches_expected !== true ||
        !tongchengVisibleRouteEndpointCodesMatch(
          cardText,
          query,
          "return",
        ) ||
        !comparisonAmount ||
        flightPriceFinality(priceText) !== "starting_or_estimated" ||
        taxesIncluded(priceText) !== true ||
        !tongchengFlightAvailabilityEvidence(card) ||
        !control ||
        !carrier
      ) {
        continue;
      }
      const selectionId = await sha256(canonicalJson({
        carrier,
        label: cleanText(control.textContent),
        provider: "tongcheng",
        return_arrival_at: returnLeg.arrival_at,
        return_departure_at: returnLeg.departure_at,
        route_identity: {
          direction: route.direction,
          expected_departure_code: route.expected_departure_code,
          expected_arrival_code: route.expected_arrival_code,
        },
      }));
      results.push({
        _control: control,
        card_index: cardIndex,
        provider: "tongcheng",
        selection_id: selectionId,
        carrier_text: carrier,
        return_departure_at: returnLeg.departure_at,
        return_arrival_at: returnLeg.arrival_at,
        return_route_evidence: route,
        comparison_price_evidence: sanitizeDiagnosticText(priceText),
        availability_evidence: tongchengFlightAvailabilityEvidence(card),
        selection_evidence: sanitizeDiagnosticText(cardText),
      });
    }
    const groups = new Map();
    for (const candidate of results) {
      const group = groups.get(candidate.selection_id) || [];
      group.push(candidate);
      groups.set(candidate.selection_id, group);
    }
    const unambiguous = [];
    for (const group of groups.values()) {
      if (new Set(group.map((candidate) => candidate._control)).size === 1) {
        unambiguous.push(group[0]);
      }
    }
    return unambiguous.slice(0, MAX_RETURN_COMBINATIONS);
  }

  function publicReturnSelection(candidate) {
    const { _control, ...publicCandidate } = candidate;
    return publicCandidate;
  }

  async function safeSelectReturn(provider, root, query, driver, selectionId) {
    if (provider !== "tongcheng") {
      return { selected: false, code: "provider_has_no_safe_return_stage" };
    }
    const candidates = await tongchengReturnSelectionCandidates(
      root,
      query,
      driver,
    );
    const candidate = candidates.find(
      (item) => item.selection_id === selectionId,
    );
    if (!candidate) {
      return {
        selected: false,
        code: "return_selection_evidence_changed",
        available_candidates: candidates.map(publicReturnSelection),
      };
    }
    candidate._control.click();
    return {
      selected: true,
      confirmation_scope: "exact_visible_select_return_quote_detail",
      selection: publicReturnSelection(candidate),
    };
  }

  async function extractStagedRoundTrips(
    provider,
    root,
    pageUrl,
    capturedAt,
    query,
    driver,
  ) {
    const timezones = routeTimezones(query);
    const summary = selectedOutboundSummary(provider, root, query);
    if (!timezones || !summary) {
      return [];
    }
    const effectiveDriver = provider === "tongcheng"
      ? tongchengAutoSelectedOutboundDriver(summary, query, driver) || driver
      : driver;
    const summaryText = cleanText(summary.textContent);
    const outboundLeg = stagedProviderLegFromVisibleText(
      provider,
      summaryText,
      query.start_date,
      timezones.origin_offset,
      timezones.destination_offset,
    );
    const outboundRouteEvidence = flightLegRouteEvidence(
      summaryText,
      query,
      "outbound",
      "selected_outbound_summary",
    );
    const visibleCarrier = provider === "tongcheng"
      ? tongchengSelectedOutboundCarrier(summary)
      : flightCarrierText(summary);
    const driverCarrier =
      effectiveDriver &&
      effectiveDriver.selected_outbound &&
      cleanText(effectiveDriver.selected_outbound.carrier_text);
    const carrier =
      visibleCarrier ||
      (["ctrip", "tongcheng"].includes(provider) ? driverCarrier : null);
    const outboundFlightNumbers = qunarVisibleFlightNumbers(
      `${summaryText} ${cleanText(
        effectiveDriver &&
        effectiveDriver.selected_outbound &&
        effectiveDriver.selected_outbound.selection_evidence,
      )} ${cleanText(
        effectiveDriver &&
        Array.isArray(effectiveDriver.action_trace) &&
        effectiveDriver.action_trace.find(
          (entry) => entry && entry.action === "select_outbound",
        )?.evidence,
      )}`,
    );
    if (
      !outboundLeg ||
      !outboundRouteEvidence ||
      outboundRouteEvidence.matches_expected !== true ||
      !carrier ||
      !selectedSummaryMatchesDriver(
        provider,
        summary,
        effectiveDriver,
        outboundLeg,
        visibleCarrier,
      )
    ) {
      return [];
    }
    const quotes = [];
    for (const card of stagedReturnCards(provider, root, query)) {
      const returnLeg = stagedProviderLegFromVisibleText(
        provider,
        card.textContent,
        query.end_date,
        timezones.destination_offset,
        timezones.origin_offset,
      );
      const priceText = stagedFlightPriceEvidence(provider, card);
      const taxEvidence = explicitTaxEvidence(card, priceText);
      const priceContract = flightPriceContract(priceText);
      const availabilityEvidence = provider === "tongcheng"
        ? tongchengFlightAvailabilityEvidence(card)
        : flightAvailabilityEvidence(card);
      const returnRouteEvidence = flightLegRouteEvidence(
        card.textContent,
        query,
        "return",
        "return_card",
      );
      if (
        !returnLeg ||
        !taxEvidence ||
        !priceContract.valid ||
        !availabilityEvidence ||
        !returnRouteEvidence ||
        returnRouteEvidence.matches_expected !== true
      ) {
        continue;
      }
      const returnCarrier = flightCarrierText(card) || carrier;
      const returnFlightNumbers = qunarVisibleFlightNumbers(
        `${cleanText(card.textContent)} ${cleanText(
          effectiveDriver &&
          effectiveDriver.selected_return &&
          effectiveDriver.selected_return.selection_evidence,
        )}`,
      );
      const quote = await flightQuoteFromCombination({
        provider,
        root: card,
        pageUrl,
        capturedAt,
        query,
        driver: effectiveDriver,
        title: `${carrier} + ${returnCarrier} 完整往返组合`,
        carrier,
        priceText,
        priceBasisValue: priceContract.price_basis,
        taxEvidence,
        availabilityEvidence,
        outboundLeg,
        returnLeg,
        outboundFlightNumbers,
        returnFlightNumbers,
        outboundRouteEvidence,
        returnRouteEvidence,
        selectionEvidence: sanitizeDiagnosticText(summaryText),
        workflowKind: "staged_outbound_return",
      });
      if (quote) {
        quotes.push(quote);
      }
      if (quotes.length >= MAX_RETURN_COMBINATIONS) {
        break;
      }
    }
    return quotes;
  }

  async function extractTongchengExpandedRoundTrips(
    root,
    pageUrl,
    capturedAt,
    query,
    driver,
  ) {
    const summary = selectedOutboundSummary("tongcheng", root, query);
    const effectiveDriver = summary
      ? tongchengAutoSelectedOutboundDriver(summary, query, driver)
      : null;
    const selectedReturn = driver && driver.selected_return;
    const timezones = routeTimezones(query);
    if (!summary || !effectiveDriver || !selectedReturn || !timezones) {
      return [];
    }
    const summaryText = cleanText(summary.textContent);
    const outboundLeg = stagedProviderLegFromVisibleText(
      "tongcheng",
      summaryText,
      query.start_date,
      timezones.origin_offset,
      timezones.destination_offset,
    );
    const outboundRoute = flightLegRouteEvidence(
      summaryText,
      query,
      "outbound",
      "selected_outbound_summary",
    );
    const outboundCarrier = tongchengSelectedOutboundCarrier(summary);
    const outboundFlightNumbers = qunarVisibleFlightNumbers(
      `${summaryText} ${cleanText(
        effectiveDriver &&
        effectiveDriver.selected_outbound &&
        effectiveDriver.selected_outbound.selection_evidence,
      )} ${cleanText(
        effectiveDriver &&
        Array.isArray(effectiveDriver.action_trace) &&
        effectiveDriver.action_trace.find(
          (entry) => entry && entry.action === "select_outbound",
        )?.evidence,
      )}`,
    );
    if (
      !outboundLeg ||
      !outboundRoute ||
      outboundRoute.matches_expected !== true ||
      !outboundCarrier ||
      !selectedSummaryMatchesDriver(
        "tongcheng",
        summary,
        effectiveDriver,
        outboundLeg,
        outboundCarrier,
      )
    ) {
      return [];
    }
    const quotes = [];
    for (const control of visibleNodes(
      root,
      [".flight-item .flight-btn.currentSlt"],
      3,
    )) {
      const card = control.closest(".flight-item");
      const cardText = cleanText(card && card.textContent);
      const returnLeg = card && stagedProviderLegFromVisibleText(
        "tongcheng",
        cardText,
        query.end_date,
        timezones.destination_offset,
        timezones.origin_offset,
      );
      const returnRoute = card && flightLegRouteEvidence(
        cardText,
        query,
        "return",
        "return_card",
      );
      const returnCarrier = card && flightCarrierText(card);
      const returnFlightNumbers = qunarVisibleFlightNumbers(
        `${cardText} ${cleanText(selectedReturn.selection_evidence)}`,
      );
      if (
        !card ||
        !returnLeg ||
        !returnRoute ||
        returnRoute.matches_expected !== true ||
        !tongchengVisibleRouteEndpointCodesMatch(
          cardText,
          query,
          "return",
        ) ||
        cleanText(selectedReturn.return_departure_at) !==
          returnLeg.departure_at ||
        cleanText(selectedReturn.return_arrival_at) !== returnLeg.arrival_at ||
        cleanText(selectedReturn.carrier_text) !== cleanText(returnCarrier)
      ) {
        continue;
      }
      for (const product of visibleNodes(card, [".pro-item-box"], 12)) {
        const priceNode = visibleNodes(product, [".pro-price"], 2)[0];
        const priceText = cleanText(priceNode && priceNode.textContent);
        const amount = strictVisibleFlightAmount(priceText);
        const productControl = visibleNodes(product, [".pro-button"], 2)
          .find((node) => (
            cleanText(node.textContent) === "预订" &&
            node.disabled !== true &&
            node.getAttribute("disabled") === null &&
            node.getAttribute("aria-disabled") !== "true"
          ));
        if (
          !amount ||
          !/^(?:¥|￥|CNY|RMB)\s*[0-9][0-9,]*(?:\.[0-9]{1,2})?含税总价$/i.test(
            priceText,
          ) ||
          flightPriceFinality(priceText) !== "exact_candidate" ||
          !productControl
        ) {
          continue;
        }
        const quote = await flightQuoteFromCombination({
          provider: "tongcheng",
          root: product,
          pageUrl,
          capturedAt,
          query,
          driver: effectiveDriver,
          title: `${outboundCarrier} + ${returnCarrier} 精确往返产品`,
          carrier: outboundCarrier,
          priceText,
          // The visible row says "含税总价", but this run did not perform a
          // server-owned 1-adult/2-adult same-product comparison. Keep it as
          // an observed total label; the API normalizer will downgrade it to
          // comparison_only and leave total_for_party_cents unset.
          priceBasisValue: "total_party",
          priceContractOverride: {
            valid: true,
            amount: amount.amount,
            currency: amount.currency,
            price_basis: "total_party",
            finality: "exact_candidate",
            evidence: priceText,
          },
          priceBasisSource:
            "visible_total_label_unverified_party_v1",
          taxEvidence: priceText,
          availabilityEvidence:
            "visible_enabled_预订_control_observed_not_clicked",
          outboundLeg,
          returnLeg,
          outboundFlightNumbers,
          returnFlightNumbers,
          outboundRouteEvidence: outboundRoute,
          returnRouteEvidence: returnRoute,
          selectionEvidence:
            `${sanitizeDiagnosticText(summaryText)}；` +
            `${sanitizeDiagnosticText(selectedReturn.selection_evidence)}`,
          workflowKind: "staged_outbound_return",
        });
        if (quote) {
          quotes.push(quote);
        }
        if (quotes.length >= MAX_RETURN_COMBINATIONS) {
          return quotes;
        }
      }
    }
    return quotes;
  }

  async function inspectFlightPageWithinScanBudget(
    provider,
    root,
    pageUrl,
    now = new Date(),
    query = {},
    driver = null,
  ) {
    const capturedAt = now.toISOString();
    const gate = pageGate(root);
    if (gate) {
      return {
        state: gate.state,
        failure: {
          code: gate.code,
          message: gate.message,
          retryable: gate.retryable,
          page_url: pageUrl,
          captured_at: capturedAt,
          details: gate.details || {},
        },
      };
    }
    if (!routeTimezones(query)) {
      return {
        state: "failed",
        failure: {
          code: "unsupported_query",
          message: "当前只验证了 HGH 与 MLE 的可见本地时间和时区映射",
          retryable: false,
          page_url: pageUrl,
          captured_at: capturedAt,
          details: { parser_version: PARSER_VERSION },
        },
      };
    }
    const qunarExtraction = provider === "qunar"
      ? await extractQunarRoundTrips(
          root,
          pageUrl,
          capturedAt,
          query,
          driver,
        )
      : null;
    const complete = qunarExtraction
      ? qunarExtraction.quotes
      : provider === "tongcheng"
        ? [
            ...await extractTongchengExpandedRoundTrips(
              root,
              pageUrl,
              capturedAt,
              query,
              driver,
            ),
            ...await extractStagedRoundTrips(
              provider,
              root,
              pageUrl,
              capturedAt,
              query,
              driver,
            ),
          ].slice(0, MAX_RETURN_COMBINATIONS)
        : await extractStagedRoundTrips(
            provider,
            root,
            pageUrl,
            capturedAt,
            query,
            driver,
          );
    if (complete.length) {
      return { state: "succeeded", quotes: complete };
    }
    if (
      qunarExtraction &&
      qunarExtraction.stability_previews.length &&
      !(driver && driver.qunar_geometry_price_disabled === true)
    ) {
      return {
        state: "price_evidence_preview",
        workflow_kind: "combined_roundtrip_card",
        quotes: [],
        stability: {
          evidence_source:
            "geometry_clipped_visible_digit_sequence",
          keys: qunarExtraction.stability_previews.map(
            (item) => item.stability_key,
          ),
        },
      };
    }
    if (["ctrip", "fliggy", "tongcheng"].includes(provider)) {
      if (
        provider === "tongcheng" &&
        !(driver && driver.selected_return)
      ) {
        const returnCandidates = await tongchengReturnSelectionCandidates(
          root,
          query,
          driver,
        );
        if (returnCandidates.length) {
          return {
            state: "return_preview",
            workflow_kind: "staged_outbound_return",
            combination_status: "return_preview",
            selection: publicReturnSelection(returnCandidates[0]),
            selections: returnCandidates.map(publicReturnSelection),
            quotes: [],
          };
        }
      }
      const candidates = await outboundSelectionCandidates(
        provider,
        root,
        query,
      );
      if (candidates.length) {
        const flightReceipt = await createFlightSearchReceipt(
          provider,
          root,
          pageUrl,
          capturedAt,
          query,
          driver,
        );
        return {
          state: "outbound_preview",
          workflow_kind: "staged_outbound_return",
          combination_status: "outbound_preview",
          selection: candidates[0],
          selections: candidates,
          quotes: [],
          ...(flightReceipt
            ? {
                flight_search_receipt: flightReceipt.receipt,
                flight_search_receipt_sha256:
                  flightReceipt.receipt_sha256,
              }
            : {}),
        };
      }
    }
    const flightDiagnostic = flightFailureDiagnostic(
      provider,
      root,
      query,
      driver,
    );
    const flightReceipt = await createFlightSearchReceipt(
      provider,
      root,
      pageUrl,
      capturedAt,
      query,
      driver,
    );
    return {
      state: "failed",
      quotes: [],
      failure: {
        code: flightTerminalFailureCode(flightReceipt),
        message:
          "页面没有形成可验证的完整往返组合；去程预览不会作为报价输出",
        retryable: flightReceipt === null,
        page_url: pageUrl,
        captured_at: capturedAt,
        details: {
          parser_version: PARSER_VERSION,
          known_card_selectors: PROFILES[provider].flight.cards,
          dom_diagnostics: domDriftDiagnostics(root),
          ...(flightDiagnostic
            ? { flight_diagnostic: flightDiagnostic }
            : {}),
          ...(flightReceipt
            ? {
                flight_search_receipt: flightReceipt.receipt,
                flight_search_receipt_sha256:
                  flightReceipt.receipt_sha256,
              }
            : {}),
        },
      },
    };
  }

  async function inspectFlightPage(
    provider,
    root,
    pageUrl,
    now = new Date(),
    query = {},
    driver = null,
  ) {
    try {
      return await inspectFlightPageWithinScanBudget(
        provider,
        root,
        pageUrl,
        now,
        query,
        driver,
      );
    } catch (error) {
      if (
        !error ||
        error.tripchordParserCode !== "dom_scan_budget_exhausted"
      ) {
        throw error;
      }
      return {
        state: "failed",
        quotes: [],
        failure: {
          code: "extraction_error",
          message: "页面节点规模超过有界可见性扫描预算，未输出任何报价",
          retryable: false,
          page_url: pageUrl,
          captured_at: now.toISOString(),
          details: {
            parser_version: PARSER_VERSION,
            diagnostic_code: error.tripchordParserCode,
            scan_budget: error.tripchordParserDetails || {},
          },
        },
      };
    }
  }

  function typedDetails(
    kind,
    card,
    terms,
    query,
    driver,
    provider,
    pageUrl,
  ) {
    const normalizedQuery = safeQuery(query);
    const confirmedQuery =
      driver &&
      driver.confirmed_query &&
      typeof driver.confirmed_query === "object" &&
      !Array.isArray(driver.confirmed_query)
        ? driver.confirmed_query
        : {};
    const evidenceQuery = {
      ...normalizedQuery,
      origin: confirmedQuery.origin || null,
      destination: confirmedQuery.destination || null,
      start_date: confirmedQuery.start_date || null,
      end_date: confirmedQuery.end_date || null,
      adults: Number.isInteger(confirmedQuery.adults)
        ? confirmedQuery.adults
        : null,
      rooms: Number.isInteger(confirmedQuery.rooms)
        ? confirmedQuery.rooms
        : null,
    };
    if (kind === "flight") {
      const datetimes = visibleDatetimes(card);
      const baggageText =
        firstText(card, FLIGHT_DETAIL_SELECTORS.baggage) ||
        firstMatching(terms, /行李|baggage/i) ||
        null;
      return {
        query: normalizedQuery,
        driver: driver || null,
        origin: confirmedQuery.origin || null,
        destination: confirmedQuery.destination || null,
        adults: Number.isInteger(confirmedQuery.adults)
          ? confirmedQuery.adults
          : null,
        outbound_departure_at: datetimes[0] || null,
        outbound_arrival_at: datetimes[1] || null,
        return_departure_at: datetimes[2] || null,
        return_arrival_at: datetimes[3] || null,
        carrier_text: firstText(card, FLIGHT_DETAIL_SELECTORS.carrier) || null,
        connection_text:
          firstText(card, FLIGHT_DETAIL_SELECTORS.connection) ||
          firstMatching(terms, /中转|经停|转机/) ||
          null,
        baggage_text: baggageText,
        checked_baggage_per_adult_kg: checkedBaggageKg(baggageText),
      };
    }
    const areaText = firstText(card, LODGING_DETAIL_SELECTORS.area) || null;
    const breakfastText =
      firstMatching(
        terms,
        /早餐|早晚餐|含早|无早|不含早|未含早|餐食|breakfast|room only/i,
      ) || null;
    const areaEvidence = packageAreaEvidence(
      areaText,
      evidenceQuery,
      driver,
    );
    const transferText =
      firstMatching(terms, /接送|机场|快艇|渡轮|船|shuttle|transfer|ferry|boat/i) ||
      null;
    const detailUrl = lodgingDetailUrl(provider, card, pageUrl);
    const transfers = rawTransferContracts(
      card,
      evidenceQuery,
      detailUrl || pageUrl,
      transferText,
    );
    return {
      query: normalizedQuery,
      driver: driver || null,
      destination: confirmedQuery.destination || null,
      check_in: confirmedQuery.start_date || null,
      check_out: confirmedQuery.end_date || null,
      adults: Number.isInteger(confirmedQuery.adults)
        ? confirmedQuery.adults
        : null,
      rooms: Number.isInteger(confirmedQuery.rooms)
        ? confirmedQuery.rooms
        : null,
      room_text: firstText(card, LODGING_DETAIL_SELECTORS.room) || null,
      area_text: areaText,
      area: areaEvidence.area,
      area_source: areaEvidence.source,
      area_matches_expected: areaEvidence.matches_expected,
      breakfast_text: breakfastText,
      breakfast_included: breakfastIncluded(breakfastText),
      cancellation_text: firstMatching(terms, /取消|退订|不可退/) || null,
      transfer_text: transferText,
      transfer_detail_url: detailUrl,
      transfer_detail_status: transfers.length
        ? "card_evidence"
        : detailUrl
          ? "detail_available"
          : null,
      transfers,
    };
  }

  async function quoteFromCard(
    provider,
    kind,
    card,
    pageUrl,
    capturedAt,
    query,
    driver,
  ) {
    const profile = PROFILES[provider][kind];
    const cardText = cleanText(card.innerText || card.textContent);
    const title = firstText(card, profile.title) || cardText.slice(0, 180);
    const priceText = firstText(card, PRICE_SELECTORS);
    const amount = parseAmount(priceText);
    if (!title || amount === null) {
      return null;
    }
    // A lodging amount is not a usable quote until the same visible price
    // node establishes whether it is per night or for the whole stay.
    const basis = priceBasis(kind, priceText);
    if (
      kind === "lodging" &&
      (
        lodgingPriceFinality(priceText) !== "exact_candidate" ||
        (
          basis !== "per_night" &&
          basis !== "total_stay"
        )
      )
    ) {
      return null;
    }
    const terms = allText(card, TERMS_SELECTORS);
    const detailFields = typedDetails(
      kind,
      card,
      terms,
      query,
      driver,
      provider,
      pageUrl,
    );
    if (kind === "lodging") {
      detailFields.transfers = await sealTransferContracts(
        detailFields.transfers,
      );
    }
    const currency = /(?:USD|\$)/i.test(priceText) ? "USD" : "CNY";
    // Only the price node may establish the quote basis.  Card-wide text can
    // contain an unrelated transfer "total price", which must not turn a
    // per-night lodging quote into a total-stay quote.
    const includedTaxes = taxesIncluded(`${priceText} ${terms.join(" ")}`);
    const details = {
      price_text: priceText,
      price_unit_evidence:
        basis === "per_night" || basis === "total_stay" ? priceText : null,
      visible_terms: terms,
      extraction: "visible_dom",
      ...detailFields,
    };
    const evidence = canonicalJson({
      amount: String(amount),
      currency,
      details,
      provider,
      kind,
      page_url: pageUrl,
      price_basis: basis,
      taxes_included: includedTaxes,
      title,
    });
    if (evidence.length > MAX_VISIBLE_EVIDENCE_CHARS) {
      return null;
    }
    return {
      provider,
      kind,
      page_url: pageUrl,
      captured_at: capturedAt,
      parser_version: PARSER_VERSION,
      visible_evidence: evidence,
      evidence_sha256: await sha256(evidence),
      currency,
      amount,
      price_basis: basis,
      taxes_included: includedTaxes,
      title,
      details,
    };
  }

  function lodgingInventoryCandidateSummary(provider, card, index) {
    const profile = PROFILES[provider].lodging;
    const priceText = firstText(card, PRICE_SELECTORS);
    return {
      candidate_index: index,
      title:
        sanitizeDiagnosticText(firstText(card, profile.title)) || null,
      area_evidence:
        sanitizeDiagnosticText(
          firstText(card, LODGING_DETAIL_SELECTORS.area),
        ) || null,
      room_evidence:
        sanitizeDiagnosticText(
          firstText(card, LODGING_DETAIL_SELECTORS.room),
        ) || null,
      price_evidence: sanitizeDiagnosticText(priceText) || null,
      price_basis: priceBasis("lodging", priceText),
      price_finality: lodgingPriceFinality(priceText),
    };
  }

  function lodgingReceiptConfirmedQuery(query, driver) {
    if (!exactLodgingQueryConfirmed(query, driver)) {
      return null;
    }
    const rawOptions =
      query &&
      query.options &&
      typeof query.options === "object" &&
      !Array.isArray(query.options)
        ? query.options
        : null;
    if (!rawOptions) {
      return null;
    }
    const expectedPlaceKey = canonicalLodgingPlaceKey(
      rawOptions.expected_lodging_place_key,
    );
    if (
      !expectedPlaceKey ||
      !SAFE_LODGING_SEGMENTS.has(rawOptions.segment) ||
      !SAFE_PACKAGE_AREAS.has(rawOptions.expected_package_area)
    ) {
      return null;
    }
    const normalizedQuery = safeQuery(query);
    const confirmed = driver.confirmed_query;
    return {
      destination: cleanText(confirmed.destination).slice(0, 120),
      start_date: confirmed.start_date,
      end_date: confirmed.end_date,
      adults: confirmed.adults,
      rooms: confirmed.rooms,
      options: {
        expected_lodging_place_key: expectedPlaceKey,
        expected_package_area: normalizedQuery.options.expected_package_area,
        segment: normalizedQuery.options.segment,
      },
    };
  }

  function normalizedLodgingCandidateSummaries(candidateSummaries) {
    return (Array.isArray(candidateSummaries) ? candidateSummaries : [])
      .slice(0, MAX_LODGING_INVENTORY_CANDIDATES)
      .map((summary, index) => ({
        candidate_index: index,
        title:
          sanitizeDiagnosticText(summary && summary.title) || null,
        area_evidence:
          sanitizeDiagnosticText(summary && summary.area_evidence) || null,
        room_evidence:
          sanitizeDiagnosticText(summary && summary.room_evidence) || null,
        price_evidence:
          sanitizeDiagnosticText(summary && summary.price_evidence) || null,
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
      }));
  }

  function normalizedExplicitEmptyEvidence(provider, evidence) {
    if (evidence === null) {
      return null;
    }
    if (
      provider === "qunar" &&
      evidence &&
      typeof evidence === "object" &&
      !Array.isArray(evidence) &&
      canonicalJson(Object.keys(evidence).sort()) ===
        canonicalJson([
          "contract_version",
          "result_count_text",
          "empty_message",
        ].sort()) &&
      evidence.contract_version ===
        QUNAR_EXPLICIT_EMPTY_CONTRACT_VERSION &&
      evidence.result_count_text ===
        QUNAR_EXPLICIT_EMPTY_RESULT_COUNT_TEXT &&
      evidence.empty_message === QUNAR_EXPLICIT_EMPTY_MESSAGE
    ) {
      return {
        contract_version: QUNAR_EXPLICIT_EMPTY_CONTRACT_VERSION,
        result_count_text: QUNAR_EXPLICIT_EMPTY_RESULT_COUNT_TEXT,
        empty_message: QUNAR_EXPLICIT_EMPTY_MESSAGE,
      };
    }
    return undefined;
  }

  function normalizedProviderPendingEvidence(provider, evidence) {
    if (evidence === null) {
      return null;
    }
    if (
      provider === "qunar" &&
      evidence &&
      typeof evidence === "object" &&
      !Array.isArray(evidence) &&
      canonicalJson(Object.keys(evidence).sort()) ===
        canonicalJson([
          "contract_version",
          "result_count_text",
          "pending_message",
          "observed_duration_ms",
        ].sort()) &&
      evidence.contract_version === QUNAR_PENDING_CONTRACT_VERSION &&
      evidence.result_count_text === QUNAR_PENDING_RESULT_COUNT_TEXT &&
      evidence.pending_message === QUNAR_PENDING_MESSAGE &&
      Number.isInteger(evidence.observed_duration_ms) &&
      evidence.observed_duration_ms >= QUNAR_PENDING_MIN_OBSERVED_MS &&
      evidence.observed_duration_ms <= 120000
    ) {
      return {
        contract_version: QUNAR_PENDING_CONTRACT_VERSION,
        result_count_text: QUNAR_PENDING_RESULT_COUNT_TEXT,
        pending_message: QUNAR_PENDING_MESSAGE,
        observed_duration_ms: evidence.observed_duration_ms,
      };
    }
    return undefined;
  }

  function auditedLodgingExplicitEmptyEvidence(provider, root) {
    if (provider !== "qunar" || !root) {
      return null;
    }
    const bodyInnerText =
      root.body && typeof root.body.innerText === "string"
        ? root.body.innerText
        : "";
    const fixtureText = Array.from(
      root.querySelectorAll &&
        root.querySelectorAll(
          "[data-tripchord-fixture='qunar-empty-evidence']",
        ) ||
        [],
    ).map((node) => node.textContent || "").join(" ");
    const renderedText = cleanText(bodyInnerText || fixtureText);
    if (
      !/共\s*0\s*家酒店满足条件/.test(renderedText) ||
      !/很抱歉[，,]\s*没有找到相关的酒店/.test(renderedText)
    ) {
      return null;
    }
    return {
      contract_version: QUNAR_EXPLICIT_EMPTY_CONTRACT_VERSION,
      result_count_text: QUNAR_EXPLICIT_EMPTY_RESULT_COUNT_TEXT,
      empty_message: QUNAR_EXPLICIT_EMPTY_MESSAGE,
    };
  }

  function auditedLodgingProviderPendingEvidence(provider, root, driver) {
    if (
      provider !== "qunar" ||
      !root ||
      !driver ||
      !Number.isInteger(driver.bounded_pending_observed_ms) ||
      driver.bounded_pending_observed_ms < QUNAR_PENDING_MIN_OBSERVED_MS ||
      driver.bounded_pending_observed_ms > 120000
    ) {
      return null;
    }
    const visibleStateTexts = [...root.querySelectorAll("body *")]
      .slice(0, MAX_VISIBLE_NODE_SCAN_NODES)
      .filter((node) => visibleEvidence(node))
      .map((node) => directVisibleNodeText(node))
      .filter(Boolean);
    if (
      !visibleStateTexts.some((text) => /共\s*家酒店满足条件/.test(text)) ||
      !visibleStateTexts.some((text) =>
        /请稍等[，,]您查询的结果正在实时搜索中\.\.\./.test(text)
      )
    ) {
      return null;
    }
    return {
      contract_version: QUNAR_PENDING_CONTRACT_VERSION,
      result_count_text: QUNAR_PENDING_RESULT_COUNT_TEXT,
      pending_message: QUNAR_PENDING_MESSAGE,
      observed_duration_ms: driver.bounded_pending_observed_ms,
    };
  }

  function lodgingInventoryReceiptPageUrl(provider, rawUrl) {
    try {
      const parsed = new URL(rawUrl);
      const suffixes = PROVIDER_HOST_SUFFIXES[provider];
      if (
        !suffixes ||
        parsed.protocol !== "https:" ||
        parsed.username ||
        parsed.password ||
        !suffixes.some(
          (suffix) =>
            parsed.hostname === suffix ||
            parsed.hostname.endsWith(`.${suffix}`),
        )
      ) {
        return null;
      }
      return `${parsed.origin}${parsed.pathname}`;
    } catch {
      return null;
    }
  }

  async function createLodgingInventoryReceipt({
    provider,
    query,
    driver,
    candidate_summaries: candidateSummaries,
    explicit_empty_evidence: explicitEmptyEvidence = null,
    provider_pending_evidence: providerPendingEvidence = null,
    page_url: pageUrl,
    captured_at: capturedAt,
    parser_version: parserVersion = PARSER_VERSION,
  }) {
    if (
      parserVersion !== PARSER_VERSION ||
      !PROFILES[provider] ||
      !PROFILES[provider].lodging
    ) {
      return null;
    }
    const confirmedQuery = lodgingReceiptConfirmedQuery(query, driver);
    const summaries =
      normalizedLodgingCandidateSummaries(candidateSummaries);
    const emptyEvidence = normalizedExplicitEmptyEvidence(
      provider,
      explicitEmptyEvidence,
    );
    const pendingEvidence = normalizedProviderPendingEvidence(
      provider,
      providerPendingEvidence,
    );
    const safePageUrl = lodgingInventoryReceiptPageUrl(provider, pageUrl);
    if (
      !confirmedQuery ||
      emptyEvidence === undefined ||
      pendingEvidence === undefined ||
      (!summaries.length && emptyEvidence === null && pendingEvidence === null) ||
      (summaries.length > 0 &&
        (emptyEvidence !== null || pendingEvidence !== null)) ||
      (emptyEvidence !== null && pendingEvidence !== null) ||
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
    const confirmedEmpty =
      summaries.length === 0 && emptyEvidence !== null;
    const boundedProviderPending =
      summaries.length === 0 && pendingEvidence !== null;
    const receipt = {
      schema_version: LODGING_INVENTORY_RECEIPT_SCHEMA_VERSION,
      parser_version: PARSER_VERSION,
      provider,
      state: confirmedEmpty
        ? "confirmed_empty"
        : boundedProviderPending
          ? "bounded_provider_pending"
          : "bounded_no_exact_quote",
      confirmed_query: confirmedQuery,
      confirmation_scope: driver.confirmation_scope,
      scan_limit: MAX_LODGING_INVENTORY_CANDIDATES,
      scanned_count: summaries.length,
      candidate_summaries: summaries,
      explicit_empty_evidence: emptyEvidence,
      provider_pending_evidence: pendingEvidence,
      page_url: safePageUrl,
      captured_at: capturedAt,
    };
    return {
      receipt,
      receipt_sha256: await sha256(canonicalJson(receipt)),
    };
  }

  async function validateLodgingInventoryReceipt(
    receipt,
    receiptSha256,
  ) {
    const rejected = (reason) => ({ valid: false, reason });
    const hasExactKeys = (value, keys) =>
      value &&
      typeof value === "object" &&
      !Array.isArray(value) &&
      canonicalJson(Object.keys(value).sort()) ===
        canonicalJson([...keys].sort());
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
        "provider_pending_evidence",
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
      receipt.schema_version !==
        LODGING_INVENTORY_RECEIPT_SCHEMA_VERSION
    ) {
      return rejected("schema_version_mismatch");
    }
    if (receipt.parser_version !== PARSER_VERSION) {
      return rejected("parser_version_mismatch");
    }
    if (
      !PROFILES[receipt.provider] ||
      ![
        "bounded_no_exact_quote",
        "confirmed_empty",
        "bounded_provider_pending",
      ].includes(
        receipt.state,
      )
    ) {
      return rejected("receipt_identity_invalid");
    }
    if (
      receipt.confirmation_scope !== "confirmed_visible_search" ||
      !receipt.confirmed_query ||
      typeof receipt.confirmed_query.destination !== "string" ||
      !receipt.confirmed_query.destination ||
      cleanText(receipt.confirmed_query.destination).slice(0, 120) !==
        receipt.confirmed_query.destination ||
      strictCalendarDateTimestamp(receipt.confirmed_query.start_date) ===
        null ||
      strictCalendarDateTimestamp(receipt.confirmed_query.end_date) ===
        null ||
      strictCalendarDateTimestamp(receipt.confirmed_query.end_date) <=
        strictCalendarDateTimestamp(receipt.confirmed_query.start_date) ||
      !Number.isInteger(receipt.confirmed_query.adults) ||
      receipt.confirmed_query.adults <= 0 ||
      !Number.isInteger(receipt.confirmed_query.rooms) ||
      receipt.confirmed_query.rooms <= 0
    ) {
      return rejected("confirmed_query_or_scope_missing");
    }
    const receiptOptions = receipt.confirmed_query.options;
    if (
      !SAFE_LODGING_SEGMENTS.has(receiptOptions.segment) ||
      !SAFE_PACKAGE_AREAS.has(receiptOptions.expected_package_area) ||
      typeof receiptOptions.expected_lodging_place_key !== "string" ||
      canonicalLodgingPlaceKey(
        receiptOptions.expected_lodging_place_key,
      ) !== receiptOptions.expected_lodging_place_key
    ) {
      return rejected("confirmed_query_options_invalid");
    }
    const confirmedEmpty = receipt.state === "confirmed_empty";
    const boundedProviderPending =
      receipt.state === "bounded_provider_pending";
    if (
      receipt.scan_limit !== MAX_LODGING_INVENTORY_CANDIDATES ||
      !Number.isInteger(receipt.scanned_count) ||
      receipt.scanned_count < 0 ||
      !Array.isArray(receipt.candidate_summaries) ||
      receipt.candidate_summaries.length !== receipt.scanned_count ||
      receipt.scanned_count > receipt.scan_limit ||
      ((confirmedEmpty || boundedProviderPending)
        ? receipt.scanned_count !== 0
        : receipt.scanned_count <= 0) ||
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
    const normalizedSummaries = normalizedLodgingCandidateSummaries(
      receipt.candidate_summaries,
    );
    if (
      canonicalJson(normalizedSummaries) !==
        canonicalJson(receipt.candidate_summaries)
    ) {
      return rejected("candidate_summaries_not_sanitized");
    }
    const emptyEvidence = normalizedExplicitEmptyEvidence(
      receipt.provider,
      receipt.explicit_empty_evidence,
    );
    const pendingEvidence = normalizedProviderPendingEvidence(
      receipt.provider,
      receipt.provider_pending_evidence,
    );
    if (
      emptyEvidence === undefined ||
      pendingEvidence === undefined ||
      (confirmedEmpty
        ? emptyEvidence === null
        : emptyEvidence !== null) ||
      (boundedProviderPending
        ? pendingEvidence === null
        : pendingEvidence !== null)
    ) {
      return rejected("empty_receipt_without_evidence");
    }
    if (
      canonicalJson(emptyEvidence) !==
        canonicalJson(receipt.explicit_empty_evidence) ||
      canonicalJson(pendingEvidence) !==
        canonicalJson(receipt.provider_pending_evidence)
    ) {
      return rejected("explicit_empty_evidence_invalid");
    }
    if (
      lodgingInventoryReceiptPageUrl(
        receipt.provider,
        receipt.page_url,
      ) !== receipt.page_url ||
      typeof receipt.captured_at !== "string" ||
      Number.isNaN(Date.parse(receipt.captured_at))
    ) {
      return rejected("capture_context_missing");
    }
    const expectedSha256 = await sha256(canonicalJson(receipt));
    if (
      !/^[a-f0-9]{64}$/.test(String(receiptSha256 || "")) ||
      expectedSha256 !== receiptSha256
    ) {
      return rejected("receipt_sha256_mismatch");
    }
    return { valid: true, reason: "valid", receipt_sha256: expectedSha256 };
  }

  async function boundedLodgingInventoryDetails(
    provider,
    cards,
    query,
    driver,
    pageUrl,
    capturedAt,
    explicitEmptyEvidence = null,
    providerPendingEvidence = null,
    captureCode = "bounded_lodging_candidates_no_exact_quote",
  ) {
    const candidateSummaries = cards
      .slice(0, MAX_LODGING_INVENTORY_CANDIDATES)
      .map((card, index) =>
        lodgingInventoryCandidateSummary(provider, card, index),
      );
    const built = await createLodgingInventoryReceipt({
      provider,
      query,
      driver,
      candidate_summaries: candidateSummaries,
      explicit_empty_evidence: explicitEmptyEvidence,
      provider_pending_evidence: providerPendingEvidence,
      page_url: pageUrl,
      captured_at: capturedAt,
    });
    if (!built) {
      return {};
    }
    return {
      inventory_result_state: built.receipt.state,
      confirmed_exhaustive:
        built.receipt.state === "confirmed_empty",
      scanned_count: candidateSummaries.length,
      candidate_summaries: candidateSummaries,
      capture_code:
        built.receipt.state === "confirmed_empty"
          ? "audited_qunar_explicit_empty_inventory"
          : built.receipt.state === "bounded_provider_pending"
            ? "audited_qunar_bounded_realtime_search_pending"
            : captureCode,
      inventory_receipt: built.receipt,
      inventory_receipt_sha256: built.receipt_sha256,
    };
  }

  async function extractPage(
    provider,
    kind,
    root,
    pageUrl,
    now = new Date(),
    query = {},
    driver = null,
  ) {
    if (!PROFILES[provider] || !PROFILES[provider][kind]) {
      return {
        state: "failed",
        failure: {
          code: "unsupported_query",
          message: "不支持的平台或查询类型",
          retryable: false,
          page_url: pageUrl,
          captured_at: now.toISOString(),
          details: {},
        },
      };
    }
    if (kind === "flight") {
      return inspectFlightPage(
        provider,
        root,
        pageUrl,
        now,
        query,
        driver,
      );
    }
    const gate = pageGate(root);
    if (gate) {
      return {
        state: gate.state,
        failure: {
          code: gate.code,
          message: gate.message,
          retryable: gate.retryable,
          page_url: pageUrl,
          captured_at: now.toISOString(),
          details: gate.details || {},
        },
      };
    }
    if (provider === "ctrip" && kind === "lodging") {
      const detail = await extractCtripLodgingDetailPage(
        root,
        pageUrl,
        now.toISOString(),
        query,
        driver,
      );
      if (detail) {
        return detail;
      }
    }
    if (provider === "fliggy" && kind === "lodging") {
      const detail = await extractFliggyLodgingDetailPage(
        root,
        pageUrl,
        now.toISOString(),
        query,
        driver,
      );
      if (detail) {
        return detail;
      }
    }
    if (provider === "qunar" && kind === "lodging") {
      const detail = await extractQunarLodgingDetailPage(
        root,
        pageUrl,
        now.toISOString(),
        query,
        driver,
      );
      if (detail) {
        return detail;
      }
    }
    if (provider === "tongcheng" && kind === "lodging") {
      const detail = await extractTongchengLodgingDetailPage(
        root,
        pageUrl,
        now.toISOString(),
        query,
        driver,
      );
      if (detail) {
        return detail;
      }
    }
    const capturedAt = now.toISOString();
    const profile = PROFILES[provider][kind];
    let cards;
    try {
      cards = visibleNodes(
        root,
        profile.cards,
        MAX_LODGING_INVENTORY_CANDIDATES,
      );
    } catch (error) {
      if (
        !error ||
        error.tripchordParserCode !== "dom_scan_budget_exhausted"
      ) {
        throw error;
      }
      return {
        state: "failed",
        quotes: [],
        failure: {
          code: "extraction_error",
          message: "住宿候选节点超过有界可见性扫描预算，未输出任何报价",
          retryable: false,
          page_url: pageUrl,
          captured_at: capturedAt,
          details: {
            parser_version: PARSER_VERSION,
            diagnostic_code: error.tripchordParserCode,
            scan_budget: error.tripchordParserDetails || {},
          },
        },
      };
    }
    let parsed = (
      await Promise.all(
        cards.map((card) =>
          quoteFromCard(
            provider,
            kind,
            card,
            pageUrl,
            capturedAt,
            query,
            driver,
          ),
        ),
      )
    ).filter(Boolean);
    if (!parsed.length && provider === "ctrip" && kind === "flight") {
      const semanticCards = ctripFlightSemanticCards(root);
      parsed = (
        await Promise.all(
          semanticCards.map((card) =>
            quoteFromCard(
              provider,
              kind,
              card,
              pageUrl,
              capturedAt,
              query,
              driver,
            ),
          ),
        )
      ).filter(Boolean);
    }
    if (!parsed.length) {
      const priceLoginBlocked =
        kind === "lodging" &&
        cards.some((card) =>
          LODGING_PRICE_LOGIN_PATTERN.test(
            cleanText(card.innerText || card.textContent),
          )
        );
      if (priceLoginBlocked) {
        return {
          state: "blocked",
          failure: {
            code: "login_required",
            message: "酒店卡片要求登录后才能读取数值报价",
            retryable: false,
            page_url: pageUrl,
            captured_at: capturedAt,
            details: {
              parser_version: PARSER_VERSION,
              known_card_selectors: profile.cards,
            },
          },
        };
      }
      const explicitEmptyEvidence =
        cards.length === 0
          ? auditedLodgingExplicitEmptyEvidence(provider, root)
          : null;
      const providerPendingEvidence =
        cards.length === 0 && explicitEmptyEvidence === null
          ? auditedLodgingProviderPendingEvidence(
              provider,
              root,
              driver,
            )
          : null;
      const inventoryDetails = await boundedLodgingInventoryDetails(
        provider,
        cards,
        query,
        driver,
        pageUrl,
        capturedAt,
        explicitEmptyEvidence,
        providerPendingEvidence,
      );
      const confirmedEmpty =
        inventoryDetails.inventory_result_state === "confirmed_empty";
      const boundedProviderPending =
        inventoryDetails.inventory_result_state ===
          "bounded_provider_pending";
      const failurePageUrl =
        inventoryDetails.inventory_receipt &&
        inventoryDetails.inventory_receipt.page_url ||
        pageUrl;
      return {
        state: "failed",
        quotes: [],
        failure: {
          code: confirmedEmpty
            ? "no_inventory"
            : boundedProviderPending
              ? "extraction_error"
              : "dom_drift",
          message: confirmedEmpty
            ? "精确住宿查询已确认平台返回 0 家酒店"
            : boundedProviderPending
              ? "精确住宿查询有界等待后仍处于平台实时搜索中"
              : "页面已加载，但没有找到可验证的报价卡片",
          retryable: false,
          page_url: failurePageUrl,
          captured_at: capturedAt,
          details: {
            parser_version: PARSER_VERSION,
            known_card_selectors: profile.cards,
            dom_diagnostics: domDriftDiagnostics(root),
            bounded_pending_observed_ms:
              driver && Number.isInteger(driver.bounded_pending_observed_ms)
                ? driver.bounded_pending_observed_ms
                : null,
            ...inventoryDetails,
          },
        },
      };
    }
    return { state: "succeeded", quotes: parsed };
  }

  async function extractTransferDetail(
    provider,
    root,
    pageUrl,
    query = {},
  ) {
    if (!PROVIDER_HOST_SUFFIXES[provider]) {
      return {
        state: "failed",
        code: "unsupported_query",
        message: "不支持的平台",
        transfers: [],
      };
    }
    const safeUrl = safeProviderDetailUrl(provider, pageUrl, pageUrl);
    if (!safeUrl) {
      return {
        state: "failed",
        code: "navigation_error",
        message: "酒店详情页超出只读平台域名边界",
        transfers: [],
      };
    }
    const gate = pageGate(root);
    if (gate) {
      return {
        state: gate.state,
        code: gate.code,
        message: gate.message,
        transfers: [],
      };
    }
    const normalizedQuery = safeQuery(query);
    const transfers = await sealTransferContracts(
      rawTransferContracts(root, normalizedQuery, safeUrl),
    );
    if (!transfers.length) {
      return {
        state: "missing_explicit_contract",
        code: "missing_explicit_transfer_contract",
        message: "详情页未找到价格、税费、方向和时间均明确的可见接驳合同",
        transfers: [],
        detail_url: safeUrl,
      };
    }
    return {
      state: "succeeded",
      transfers,
      detail_url: safeUrl,
    };
  }

  globalThis.TripChordQuoteParser = Object.freeze({
    PARSER_VERSION,
    extractPage,
    inspectFlightPage,
    safeSelectOutbound,
    safeSelectReturn,
    qunarSafeExpandFlightDetail,
    extractTransferDetail,
    pageGate,
    parseAmount,
    priceBasis,
    lodgingPriceFinality,
    flightPriceFinality,
    flightPriceContract,
    canonicalJson,
    taxesIncluded,
    safeQuery,
    checkedBaggageKg,
    breakfastIncluded,
    explicitPackageArea,
    packageAreaEvidence,
    safeProviderDetailUrl,
    transferDirection,
    transferPrice,
    transferDurationMinutes,
    transferTimezoneOffset,
    transferWindow,
    transferPurchaseScope,
    transferContractsFromEvidence,
    sanitizeDiagnosticText,
    flightRouteObservation,
    flightLegRouteEvidence,
    atomicPriceStructure,
    qunarTitledDigitPriceEvidence,
    qunarPriceEvidence,
    stableTitledDigitAmount,
    ctripLodgingDetailUrlContext,
    qunarLodgingDetailUrlContext,
    extractQunarLodgingDetailPage,
    qunarAtomicFinalPriceCandidate,
    ctripDetailStayReadback,
    ctripDetailOccupancyReadback,
    lodgingPlaceEvidence,
    ctripAtomicTaxPriceCandidates,
    createLodgingInventoryReceipt,
    exactLodgingQueryConfirmed,
    validateLodgingInventoryReceipt,
    createFlightSearchReceipt,
    validateFlightSearchReceipt,
  });
  if (globalThis.__TRIPCHORD_PARSER_TEST_HOOKS__) {
    Object.assign(globalThis.__TRIPCHORD_PARSER_TEST_HOOKS__, {
      FLIGHT_SEARCH_RECEIPT_SCHEMA_VERSION,
      MAX_FLIGHT_SEARCH_RECEIPT_CANDIDATES,
      MAX_LODGING_INVENTORY_CANDIDATES,
      LODGING_INVENTORY_RECEIPT_SCHEMA_VERSION,
      MAX_VISIBLE_NODE_SCAN_NODES,
      boundedLodgingInventoryDetails,
      clippedIntersectionRatio,
      geometryClippedDigitAmount,
      qunarGeometryDigitPriceEvidence,
      qunarSingleAttributePriceDiagnostic,
      lodgingInventoryReceiptPageUrl,
      lodgingInventoryCandidateSummary,
      qunarInventoryObservationCaptureValid,
      qunarLodgingDetailDomDiagnostics,
      qunarRateDiagnostics,
      matchingVisibleNodes,
      semanticFlightCardFromControl,
      legFromQunarTrip,
      legFromVisibleText,
      tongchengLegFromVisibleText,
      createFlightSearchReceiptFromCandidates,
      ctripFlightReceiptCandidates,
      flightReceiptConfirmedQuery,
      flightTerminalFailureCode,
      exactOutboundControls,
      flightCarrierText,
      qunarVisibleFlightNumbers,
      qunarDirectFlightSegment,
      qunarRawVisibleAirportCodes,
      qunarVisibleAirportCodes,
      qunarAirportCodesAnchoredToFlights,
      qunarVisibleMultiFlightSegments,
      qunarFlightNodeEvidence,
      qunarStructuredFlightSegments,
      qunarReceiptSegmentsFromStructured,
      flightLegRouteEvidence,
      qunarFlightLoadingDiagnostic,
      tongchengFlightAvailabilityEvidence,
      selectedOutboundSummary,
      tongchengAutoSelectedOutboundDriver,
      stagedReturnCards,
      sha256,
      stableTitledDigitAmount,
      visibleNodes,
    });
  }
})();
