(() => {
  if (globalThis.__tripchordReadOnlyContentInstalled) {
    return;
  }
  globalThis.__tripchordReadOnlyContentInstalled = true;

  const INPUT_HINTS = {
    flight: {
      origin: ["出发城市", "出发地", "从哪里出发", "from"],
      destination: ["到达城市", "目的地", "到哪里", "to"],
      start_date: ["出发日期", "去程日期", "departure"],
      end_date: ["返程日期", "回程日期", "return"],
    },
    lodging: {
      destination: ["目的地", "城市、位置、酒店", "酒店目的地", "destination"],
      start_date: ["入住日期", "入住", "check-in", "checkin"],
      end_date: ["离店日期", "退房", "check-out", "checkout"],
      keyword: ["关键词", "酒店名称", "keyword", "hotel name"],
    },
  };
  const SEARCH_LABELS = {
    flight: ["搜索机票", "查询机票", "搜索航班", "立即搜索", "搜索"],
    lodging: ["搜索酒店", "查询酒店", "查找酒店", "立即搜索", "搜索"],
  };
  const FIELD_SELECTORS = {
    ctrip: {
      flight: {
        origin: ["input[name='owDCity']"],
        destination: ["input[name='owACity']"],
      },
      lodging: {
        destination: [
          "#trip_main_content input#destinationInput[placeholder='目的地']",
          "input#destinationInput[placeholder='目的地']",
          "input[aria-label='目的地']",
        ],
        start_date: [
          "#trip_main_content input#checkInInput",
          "input#checkInInput",
        ],
        end_date: [
          "#trip_main_content input#checkOutInput",
          "input#checkOutInput",
        ],
      },
    },
    fliggy: {
      flight: {
        origin: ["input[data-testid='dep-city-input']"],
        destination: ["input[data-testid='arr-city-input']"],
      },
      lodging: {
        destination: [
          "input[data-testid='international-city-input']",
          "input[role='combobox'][aria-label*='目的地']",
          "[role='combobox'][aria-label*='目的地']",
        ],
        start_date: [
          "input[data-testid='international-checkin-date-input']",
          "input[aria-label='入住日期']",
          "input[aria-label='开始日期']",
        ],
        end_date: [
          "input[data-testid='international-checkout-date-input']",
          "input[aria-label='离店日期']",
          "input[aria-label='结束日期']",
        ],
        keyword: [
          "input[data-testid='international-keyword-input']",
          "input[aria-label='关键词']",
          "input[placeholder*='关键词']",
          "input[placeholder*='酒店名称']",
        ],
      },
    },
    qunar: {
      flight: {
        origin: ["input[name='fromCity']"],
        destination: ["input[name='toCity']"],
        start_date: ["input#fromDate", "input[name='fromDate']"],
        end_date: ["input#toDate", "input[name='toDate']"],
      },
      lodging: {
        destination: [
          "#interForm .city-input input.textbox",
          "input[name='toCity']",
          "input[name='city']",
        ],
        start_date: [
          "#interForm .live .check:first-child input.inputText.date",
          "#interForm input.inputText.date",
          "input[name='fromDate']",
          "input[name='checkInDate']",
        ],
        end_date: [
          "#interForm .live .check:last-child input.inputText.date",
          "input[name='toDate']",
          "input[name='checkOutDate']",
        ],
      },
    },
    tongcheng: {
      flight: {
        origin: ["#depCity input.v-input_field"],
        destination: ["#arrCity input.v-input_field"],
        start_date: [".date-info > dl.m-right50 input.v-date-editor"],
        end_date: ["#backDateId input.v-date-editor"],
      },
      lodging: {
        destination: ["#addressBox input.searchInput.address"],
        start_date: ["input.date.start"],
        end_date: ["input.date.end"],
        keyword: ["#keyWordBox input.keyWordInput"],
      },
    },
  };
  const ROUND_TRIP_SELECTORS = {
    ctrip: [
      "input[value='RoundTrip']",
      "input[value='roundtrip']",
      "input[value='RT']",
    ],
    fliggy: [
      "input[value='roundTrip']",
      "input[value='roundtrip']",
      "input[value='RT']",
    ],
    qunar: [
      "input#searchTypeRnd",
      "input#searchTypeInterRnd",
      "input[value='RoundTripFlight']",
    ],
    tongcheng: ["#tabId .return-btn"],
  };
  const SEARCH_BUTTON_SELECTORS = {
    ctrip: {
      flight: ["button.search-btn", "button[type='submit']"],
      lodging: [
        "#trip_main_content [role='button'][aria-label='搜索'] button",
        "#trip_main_content [role='button'][aria-label='搜索']",
        "button.search-btn",
        "button[type='submit']",
      ],
    },
    fliggy: {
      flight: [
        "button[data-testid='search-flight-button']",
        "button[type='submit']",
      ],
      lodging: [
        "[data-testid='international-search-button'][role='button']",
        "button[type='submit']",
      ],
    },
    qunar: {
      flight: ["button.btn_search", "button[type='submit']"],
      lodging: [
        "#interForm .search-button",
        "button.btn_search",
        "button[type='submit']",
      ],
    },
    tongcheng: {
      flight: ["button.search-btn"],
      lodging: [".searchBox .searchBtn"],
    },
  };
  const OCCUPANCY_PROFILES = {
    ctrip: {
      flight: {
        trigger: ["乘机人", "乘客", "成人"],
        adults: ["成人", "adult"],
      },
      lodging: {
        trigger: ["房间及住客", "住客", "人数", "房间", "成人", "间"],
        adults: ["成人", "住客", "adult"],
        rooms: ["房间", "客房", "room"],
      },
    },
    fliggy: {
      flight: {
        trigger: ["乘机人", "乘客类型", "成人"],
        adults: ["成人", "adult"],
      },
      lodging: {
        trigger: ["房间住客", "住客", "人数", "房间"],
        adults: ["成人", "住客", "adult"],
        rooms: ["房间", "客房", "room"],
      },
    },
    qunar: {
      flight: {
        trigger: ["乘机人", "乘客", "成人"],
        adults: ["成人", "adult"],
      },
      lodging: {
        trigger: ["入住人数", "住客", "人数", "房间"],
        adults: ["成人", "住客", "adult"],
        rooms: ["房间", "客房", "room"],
      },
    },
    tongcheng: {
      flight: {
        trigger: ["乘客类型", "成人"],
        adults: ["成人", "adult"],
      },
      lodging: {
        trigger: ["人数", "成人", "房间"],
        adults: ["成人", "住客", "adult"],
        rooms: ["房间", "客房", "room"],
      },
    },
  };
  const AUDITED_LODGING_IDENTITIES = Object.freeze({
    ctrip: Object.freeze({
      maafushi: Object.freeze({
        placeKey: "maafushi",
        englishLabels: ["maafushi"],
        visibleLabels: ["马富施", "马富士"],
        selectedLabels: ["Maafushi", "马富施", "马富士", "马富士岛"],
        exactCandidateLabels: ["Maafushi", "马富施", "马富士"],
        requiredAreaLabels: ["卡夫环礁", "kaafu atoll"],
      }),
      hulhumale: Object.freeze({
        placeKey: "hulhumale",
        englishLabels: ["hulhumale", "hulhumalé"],
        visibleLabels: ["胡鲁马累"],
        // Ctrip suggestion text is `胡鲁马累`, while the selected input is
        // canonically read back as `胡鲁马累岛`. Both are the same audited
        // destination; candidate matching remains limited to the shorter
        // exact suggestion labels below.
        selectedLabels: [
          "Hulhumale",
          "Hulhumalé",
          "胡鲁马累",
          "胡鲁马累岛",
        ],
        exactCandidateLabels: ["Hulhumale", "Hulhumalé", "胡鲁马累"],
        requiredAreaLabels: [],
      }),
    }),
    fliggy: Object.freeze({
      maafushi: Object.freeze({
        placeKey: "maafushi",
        id: "933081",
        englishLabels: ["maafushi"],
        visibleLabels: ["马富士"],
        selectedLabels: ["马富士"],
        semanticOptionIds: ["search-city-马富士"],
        requiredAreaLabels: ["马尔代夫", "maldives"],
      }),
      hulhumale: Object.freeze({
        placeKey: "hulhumale",
        id: "934358",
        englishLabels: ["hulhumale"],
        visibleLabels: ["哈尔胡梅尔"],
        selectedLabels: ["哈尔胡梅尔"],
        semanticOptionIds: ["search-city-哈尔胡梅尔"],
        requiredAreaLabels: ["马尔代夫", "maldives"],
      }),
    }),
    qunar: Object.freeze({
      maafushi: Object.freeze({
        placeKey: "maafushi",
        id: "i-ka_maafushi",
        matchMode: "qunar_city_destination_row",
        englishLabels: ["maafushi"],
        visibleLabels: ["马富施", "卡夫环礁", "kaafu atoll"],
        selectedLabels: ["马富施", "maafushi"],
        requiredAreaLabels: ["卡夫环礁", "kaafu atoll"],
      }),
      hulhumale: Object.freeze({
        placeKey: "hulhumale",
        id: "i-hulhumale",
        matchMode: "qunar_city_destination_row",
        englishLabels: ["hulhumale"],
        visibleLabels: ["胡鲁马累", "胡鲁马累岛", "hulhumale"],
        // Qunar's official international-city suggestion contract currently
        // names `i-hulhumale` as `胡鲁马累岛`, while the navigation seed is
        // `胡鲁马累`.  These exact aliases are accepted only together with the
        // independently verified `/city/i-hulhumale` result path below.
        selectedLabels: ["胡鲁马累", "胡鲁马累岛"],
        requiredAreaLabels: [],
      }),
    }),
    tongcheng: Object.freeze({
      maafushi: Object.freeze({
        placeKey: "maafushi",
        id: "110018575",
        englishLabels: ["maafushi"],
        visibleLabels: ["马富施", "马富士"],
        selectedLabels: ["马富施"],
        requiredAreaLabels: ["卡夫环礁", "kaafu atoll", "马尔代夫", "maldives"],
      }),
      hulhumale: Object.freeze({
        placeKey: "hulhumale",
        id: "110018578",
        englishLabels: ["hulhumale", "hulhumalé"],
        visibleLabels: ["胡鲁马累"],
        selectedLabels: ["胡鲁马累"],
        requiredAreaLabels: ["马尔代夫", "maldives"],
      }),
    }),
  });
  const AUDITED_FLIGGY_COUNTRY_IDENTITIES = Object.freeze({
    maldives: Object.freeze({
      placeKey: "maafushi",
      identityKind: "country_destination",
      englishLabels: ["maldives"],
      visibleLabels: ["马尔代夫"],
      selectedLabels: ["Maldives", "马尔代夫"],
      exactCandidateLabels: ["Maldives", "马尔代夫"],
      requiredAreaLabels: [],
    }),
  });
  const MAX_CONTROL_DIAGNOSTICS = 12;
  const MAX_SUGGESTION_DIAGNOSTICS = 8;
  const MAX_MATCHED_SUGGESTION_ROOTS = 32;
  const MAX_SUGGESTION_EVIDENCE_PER_ROOT = 24;
  const MAX_SUGGESTION_CANDIDATE_PAIRS = 96;
  const DEFAULT_SUGGESTION_POLL_TIMEOUT_MS = 2500;
  // The authenticated Ctrip overseas-hotel surface hydrates its exact city
  // rows after a provider-side request.  The 2026-08-04 private canary saw an
  // audited Maafushi/Kaafu row immediately after the old 2.5 s poll expired.
  // Keep the longer wait scoped to this one read-only field; candidate
  // identity and post-click readback remain mandatory below.
  const CTRIP_LODGING_SUGGESTION_POLL_TIMEOUT_MS = 5000;
  let preparedSearchContext = null;

  function normalized(value) {
    return String(value || "")
      .normalize("NFKD")
      .replace(/\p{M}/gu, "")
      .toLowerCase()
      .replace(/\s+/g, "");
  }

  function isElementNode(value) {
    return Boolean(
      value &&
      value.nodeType === 1 &&
      typeof value.getAttribute === "function" &&
      typeof value.querySelectorAll === "function" &&
      typeof value.matches === "function",
    );
  }

  function visible(element) {
    if (
      !isElementNode(element) ||
      typeof element.getBoundingClientRect !== "function"
    ) {
      return false;
    }
    const style = getComputedStyle(element);
    const rect = element.getBoundingClientRect();
    return (
      style.visibility !== "hidden" &&
      style.display !== "none" &&
      rect.width > 0 &&
      rect.height > 0
    );
  }

  function descriptor(element) {
    if (!isElementNode(element)) {
      return "";
    }
    return normalized(
      [
        element.getAttribute("aria-label"),
        element.getAttribute("placeholder"),
        element.getAttribute("name"),
        element.getAttribute("id"),
        element.getAttribute("type"),
        element.getAttribute("value"),
        element.getAttribute("data-testid"),
      ].filter(Boolean).join(" "),
    );
  }

  function nearbyDescriptor(element) {
    if (!isElementNode(element)) {
      return "";
    }
    const labelledBy = element.getAttribute("aria-labelledby");
    const labelledText = labelledBy
      ? labelledBy
          .split(/\s+/)
          .map((id) => document.getElementById(id))
          .filter(Boolean)
          .map((node) => node.textContent)
          .join(" ")
      : "";
    const explicitLabel = element.id
      ? document.querySelector(`label[for="${CSS.escape(element.id)}"]`)
      : null;
    const container = element.closest(
      "label, [role='group'], [class*='field'], [class*='Field'], [class*='item'], [class*='Item']",
    );
    const containerText = container && String(container.textContent || "").length <= 240
      ? container.textContent
      : "";
    return normalized(
      [
        descriptor(element),
        labelledText,
        explicitLabel && explicitLabel.textContent,
        containerText,
      ].filter(Boolean).join(" "),
    );
  }

  function queryVisible(selectors, root = document) {
    for (const selector of selectors || []) {
      const match = [...root.querySelectorAll(selector)].find(visible);
      if (match) {
        return match;
      }
    }
    return null;
  }

  function dateInputs(root = document) {
    return [...root.querySelectorAll("input")]
      .filter(visible)
      .filter((element) => {
        const value = `${descriptor(element)} ${nearbyDescriptor(element)}`;
        return (
          element.type === "date" ||
          /日期|入住|离店|返回|返程|出发|checkin|checkout|departure|return|yyyy/.test(
            value,
          )
        );
      });
  }

  function findInput(provider, kind, field, hints, root = document) {
    const configured =
      FIELD_SELECTORS[provider]?.[kind]?.[field] || [];
    const exact = queryVisible(configured, root);
    if (exact) {
      return exact;
    }
    if (field === "start_date" || field === "end_date") {
      const candidates = dateInputs(root);
      const index = field === "start_date" ? 0 : 1;
      if (candidates[index]) {
        return candidates[index];
      }
    }
    return [...root.querySelectorAll("input, [contenteditable='true']")].find(
      (element) =>
        visible(element) &&
        hints.some((hint) =>
          nearbyDescriptor(element).includes(normalized(hint))
        ),
    );
  }

  function setVisibleValue(element, value, { blur = true } = {}) {
    element.focus();
    if (element instanceof HTMLInputElement) {
      const setter = Object.getOwnPropertyDescriptor(
        HTMLInputElement.prototype,
        "value",
      ).set;
      setter.call(element, value);
    } else {
      element.textContent = value;
    }
    for (const type of ["input", "change"]) {
      element.dispatchEvent(new Event(type, { bubbles: true }));
    }
    if (blur) {
      element.dispatchEvent(new Event("blur", { bubbles: true }));
    }
  }

  function readVisibleValue(element) {
    if (!isElementNode(element)) {
      return "";
    }
    return element instanceof HTMLInputElement
      ? element.value
      : element.textContent || "";
  }

  function visibleFieldMatches(field, readback, expected) {
    const actual = normalized(readback);
    const target = normalized(expected);
    if (!actual || !target) {
      return false;
    }
    if (field === "start_date" || field === "end_date") {
      const actualDigits = actual.replace(/\D/g, "");
      const targetDigits = target.replace(/\D/g, "");
      return actualDigits.includes(targetDigits);
    }
    return actual.includes(target) || target.includes(actual);
  }

  function controlDiagnostics() {
    return [...document.querySelectorAll("input, [contenteditable='true'], button, [role='button'], [role='spinbutton']")]
      .filter(visible)
      .sort((left, right) => {
        const priority = (element) =>
          element.matches("input, [contenteditable='true'], [role='spinbutton']")
            ? 0
            : /搜索|查询|search/.test(textDescriptor(element))
              ? 1
              : 2;
        return priority(left) - priority(right);
      })
      .slice(0, MAX_CONTROL_DIAGNOSTICS)
      .map((element) => ({
        tag: element.tagName.toLowerCase(),
        descriptor: descriptor(element).slice(0, 120),
        text: textDescriptor(element).slice(0, 120),
      }));
  }

  function delay(milliseconds) {
    return new Promise((resolve) => setTimeout(resolve, milliseconds));
  }

  function suggestionSelectors(provider, kind) {
    if (provider === "qunar" && kind === "flight") {
      return [
        "#ifsForm .js-suggestcontainer .q-suggest tr[data-sug_type='0']",
        ".js-suggestcontainer .q-suggest tr[data-sug_type='0']",
      ];
    }
    if (provider === "qunar" && kind === "lodging") {
      return [
        "#interForm .m-suggest-container table.suggest-list tr.item",
        "#interForm table.suggest-list tr.item",
        "#interForm .m-suggest-container .item",
        "#interForm [class*='suggest'] .item",
      ];
    }
    if (provider === "ctrip" && kind === "lodging") {
      return [
        "#trip_main_content div[tabindex='-1']",
        "div[tabindex='-1']",
      ];
    }
    if (provider === "fliggy" && kind === "lodging") {
      return [
        "[data-testid='search-city-dropdown'] [role='option']",
        "[data-agent-type='city-option'][role='option']",
      ];
    }
    return [
      "[role='option']",
      "[role='listbox'] li",
      "[class*='suggest'] li",
      "[class*='Suggest'] li",
      "[class*='autocomplete'] li",
      "[class*='Autocomplete'] li",
      "[class*='city'] li",
      "[class*='City'] li",
    ];
  }

  function privacySafeSuggestionText(value) {
    return String(value || "")
      .replace(
        /\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b/gi,
        "[redacted-email]",
      )
      .replace(/\b1[3-9]\d{9}\b/g, "[redacted-phone]")
      .replace(/\b\d{12,}\b/g, "[redacted-number]")
      .replace(/\s+/g, " ")
      .trim();
  }

  function suggestionAttemptDiagnostics(
    provider,
    kind,
    value,
    expectedPlaceKey = null,
  ) {
    const identity = suggestionIdentity(
      provider,
      kind,
      value,
      expectedPlaceKey,
    );
    const tokens = suggestionSearchTokens(identity, value);
    const seen = new Set();
    return suggestionCandidatePairs(provider, kind, tokens)
      .filter(({ clickCandidate, evidenceCandidate }) => {
        if (seen.has(evidenceCandidate)) {
          return false;
        }
        seen.add(evidenceCandidate);
        return isElementNode(clickCandidate);
      })
      .slice(0, MAX_SUGGESTION_DIAGNOSTICS)
      .map(({ clickCandidate, evidenceCandidate }) => ({
        tag: evidenceCandidate.tagName.toLowerCase(),
        role: clickCandidate.getAttribute("role"),
        class: String(clickCandidate.className || "").slice(0, 120),
        text_summary: privacySafeSuggestionText(
          cleanCounterText(evidenceCandidate),
        ).slice(0, 180),
        identity_evidence: suggestionIdentityMatches(
          clickCandidate,
          identity,
          evidenceCandidate,
        ).evidence,
      }));
  }

  function suggestionIdentity(
    provider,
    kind,
    value,
    expectedPlaceKey = null,
  ) {
    const target = normalized(value);
    if (kind !== "lodging" || !AUDITED_LODGING_IDENTITIES[provider]) {
      return null;
    }
    const identities = AUDITED_LODGING_IDENTITIES[provider];
    const placeKey = normalized(expectedPlaceKey);
    if (
      provider === "fliggy" &&
      placeKey === "maafushi" &&
      ["maldives", "马尔代夫"].some(
        (label) => target === normalized(label),
      )
    ) {
      return AUDITED_FLIGGY_COUNTRY_IDENTITIES.maldives;
    }
    if (placeKey && identities[placeKey]) {
      return identities[placeKey];
    }
    const targetIdentity = Object.values(identities).find((identity) =>
      [...identity.englishLabels, ...identity.visibleLabels].some((label) =>
        target.includes(normalized(label))
      )
    );
    if (
      targetIdentity &&
      ["ctrip", "fliggy", "tongcheng"].includes(provider) &&
      !identities[placeKey]
    ) {
      return {
        ...targetIdentity,
        unresolved: true,
      };
    }
    return targetIdentity || null;
  }

  function suggestionAttributeValues(element) {
    if (!isElementNode(element)) {
      return [];
    }
    return [
      ["data-key", element.getAttribute("data-key")],
      ["data-id", element.getAttribute("data-id")],
      ["data-value", element.getAttribute("data-value")],
      ["data-code", element.getAttribute("data-code")],
      ["data-city-code", element.getAttribute("data-city-code")],
      ["data-city-id", element.getAttribute("data-city-id")],
      ["data-agent-city-code", element.getAttribute("data-agent-city-code")],
      ["data-agent-city-id", element.getAttribute("data-agent-city-id")],
      ["data-agent-english-name", element.getAttribute("data-agent-english-name")],
      ["data-agent-id", element.getAttribute("data-agent-id")],
      ["data-agent-type", element.getAttribute("data-agent-type")],
      ["data-testid", element.getAttribute("data-testid")],
      ["value", element.getAttribute("value")],
      ["id", element.id],
    ].filter(([, value]) => Boolean(value));
  }

  function suggestionAttributeText(element) {
    return suggestionAttributeValues(element)
      .map(([, value]) => value)
      .join(" ");
  }

  function explicitSuggestionIds(candidate) {
    const idAttributes = new Set([
      "data-key",
      "data-id",
      "data-code",
      "data-city-code",
      "data-city-id",
      "data-agent-city-code",
      "data-agent-city-id",
    ]);
    return suggestionAttributeValues(candidate)
      .filter(([name]) => idAttributes.has(name))
      .map(([, value]) => normalized(value))
      .filter((value) => /^\d+$|^i[-_]/i.test(value));
  }

  const LODGING_NAME_PATTERN =
    /酒店|旅馆|客栈|民宿|公寓|度假村|青旅|hotel|resort|guest\s*house|guesthouse|hostel|villa|suites?|\binn\b/i;

  function auditedRequiredAreaMatches(candidate, identity) {
    const requiredAreaLabels = identity?.requiredAreaLabels || [];
    if (!requiredAreaLabels.length) {
      return true;
    }
    const text = normalized(cleanCounterText(candidate));
    const attributes = normalized(suggestionAttributeText(candidate));
    return requiredAreaLabels.some(
      (label) =>
        text.includes(normalized(label)) ||
        attributes.includes(normalized(label)),
    );
  }

  function strictExactDestinationAncestorMatches(
    candidate,
    evidenceCandidate,
    identity,
  ) {
    if (
      !isElementNode(candidate) ||
      !isElementNode(evidenceCandidate) ||
      candidate === evidenceCandidate ||
      !candidate.contains(evidenceCandidate)
    ) {
      return false;
    }
    const exactCandidateLabels = identity?.exactCandidateLabels || [];
    const evidenceText = normalized(cleanCounterText(evidenceCandidate));
    if (
      !exactCandidateLabels.some(
        (label) => evidenceText === normalized(label),
      )
    ) {
      return false;
    }
    const candidateText = cleanCounterText(candidate);
    const semanticText = [
      candidateText,
      candidate.className,
      suggestionAttributeText(candidate),
      candidate.getAttribute("data-tripchord-suggestion-kind"),
    ].filter(Boolean).join(" ");
    if (
      LODGING_NAME_PATTERN.test(semanticText) ||
      /预订|下单|支付|购买|优惠券|订单|去付款/.test(semanticText)
    ) {
      return false;
    }
    if (!auditedRequiredAreaMatches(candidate, identity)) {
      return false;
    }
    // Provider rows may repeat the same label or add region/category text.
    // Exact identity comes from the structurally visible inner label; this
    // bounded visible ancestor is only the click target. Hotel/transaction
    // semantics were rejected above, and post-click input readback remains
    // mandatory before the destination is confirmed.
    return candidateText.length <= 240;
  }

  function suggestionIdentityMatches(
    candidate,
    identity,
    evidenceCandidate = candidate,
  ) {
    if (!isElementNode(candidate) || !isElementNode(evidenceCandidate)) {
      return {
        matched: false,
        evidence: "suggestion_candidate_not_element",
      };
    }
    if (!identity) {
      return { matched: true, evidence: null };
    }
    if (identity.unresolved) {
      return { matched: false, evidence: "expected_lodging_place_key_missing" };
    }
    const attributes = normalized(suggestionAttributeText(candidate));
    const text = normalized(cleanCounterText(candidate));
    const expectedId = normalized(identity.id);
    const explicitIds = explicitSuggestionIds(candidate);
    const conflictingId =
      Boolean(expectedId) &&
      explicitIds.length > 0 &&
      !explicitIds.some((value) => value.includes(expectedId));
    if (conflictingId) {
      return { matched: false, evidence: "conflicting_suggestion_id" };
    }
    if (expectedId && attributes.includes(expectedId)) {
      return { matched: true, evidence: "audited_suggestion_id" };
    }
    if (identity.matchMode === "qunar_city_destination_row") {
      const cityLabels = [
        ...(identity.englishLabels || []),
        ...(identity.selectedLabels || []),
      ]
        .map(normalized)
        .filter(Boolean);
      const exactEvidenceText = normalized(
        cleanCounterText(evidenceCandidate),
      );
      const exactEvidenceVisible =
        candidate.contains(evidenceCandidate) &&
        cityLabels.some((label) => exactEvidenceText === label);
      const rawCells = [
        ...candidate.querySelectorAll("td, [role='cell']"),
      ].map((node) => cleanCounterText(node));
      const cellTexts = rawCells.length
        ? rawCells
        : [cleanCounterText(candidate)];
      if (
        LODGING_NAME_PATTERN.test(cleanCounterText(candidate)) ||
        cellTexts.some((value) => LODGING_NAME_PATTERN.test(value))
      ) {
        return {
          matched: false,
          evidence: "lodging_candidate_not_destination",
        };
      }
      if (exactEvidenceVisible) {
        return {
          matched: true,
          evidence: "audited_qunar_exact_city_destination",
        };
      }
      const normalizedCells = cellTexts
        .map((value) => normalized(value))
        .filter(Boolean);
      const exactCityVisible = normalizedCells.some((value) =>
        cityLabels.some((label) => value === label)
      );
      const requiredAreas = (identity.requiredAreaLabels || [])
        .map(normalized)
        .filter(Boolean);
      const areaVisible = requiredAreas.length === 0 ||
        requiredAreas.some((label) =>
          normalizedCells.some((value) => value.includes(label)) ||
          attributes.includes(label)
        );
      const qualifiedCityVisible =
        areaVisible &&
        normalizedCells.some((value) =>
          cityLabels.some(
            (label) =>
              value === label ||
              value.startsWith(`${label},`) ||
              value.startsWith(`${label}，`),
          )
        );
      return exactCityVisible || qualifiedCityVisible
        ? {
            matched: true,
            evidence: exactCityVisible
              ? "audited_qunar_exact_city_destination"
              : "audited_qunar_city_destination_row",
          }
        : {
            matched: false,
            evidence: "audited_city_destination_row_not_visible",
          };
    }
    const exactCandidateLabels = identity.exactCandidateLabels || [];
    if (exactCandidateLabels.length) {
      if (
        strictExactDestinationAncestorMatches(
          candidate,
          evidenceCandidate,
          identity,
        )
      ) {
        return {
          matched: true,
          evidence: "audited_exact_destination_ancestor",
        };
      }
      const exactLabelMatch = exactCandidateLabels.some(
        (label) => text === normalized(label),
      );
      if (!exactLabelMatch) {
        return {
          matched: false,
          evidence: LODGING_NAME_PATTERN.test(
            cleanCounterText(candidate),
          )
            ? "lodging_candidate_not_destination"
            : "audited_exact_destination_label_not_visible",
        };
      }
      if (!auditedRequiredAreaMatches(candidate, identity)) {
        return {
          matched: false,
          evidence: "audited_destination_area_not_visible",
        };
      }
      return {
        matched: true,
        evidence: "audited_exact_destination_label",
      };
    }
    const requiredAreaLabels = identity.requiredAreaLabels || [];
    const areaMatch =
      !requiredAreaLabels.length ||
      requiredAreaLabels.some((label) =>
        text.includes(normalized(label)) ||
        attributes.includes(normalized(label))
      );
    const visibleLabelMatch = identity.visibleLabels.some((label) =>
      text.includes(normalized(label)) ||
      attributes.includes(normalized(label))
    );
    const semanticOptionIds = identity.semanticOptionIds || [];
    const semanticOptionMatch = suggestionAttributeValues(candidate).some(
      ([name, value]) =>
        (name === "data-agent-id" || name === "data-testid") &&
        semanticOptionIds.some(
          (semanticId) => normalized(value) === normalized(semanticId),
        ),
    );
    const semanticRoleMatch =
      candidate.getAttribute("role") === "option" &&
      candidate.getAttribute("data-agent-type") === "city-option";
    if (
      semanticOptionMatch &&
      semanticRoleMatch &&
      visibleLabelMatch &&
      areaMatch
    ) {
      return {
        matched: true,
        evidence: "audited_semantic_option_identity",
      };
    }
    const englishMatch = identity.englishLabels.some((label) =>
      text.includes(normalized(label)) ||
      attributes.includes(normalized(label))
    );
    return englishMatch && areaMatch
      ? { matched: true, evidence: "audited_english_identity" }
      : { matched: false, evidence: "audited_identity_not_visible" };
  }

  function dispatchPointerClick(candidate) {
    if (!isElementNode(candidate)) {
      return false;
    }
    for (const type of ["pointerdown", "mousedown", "mouseup", "click"]) {
      const EventType =
        type === "pointerdown" && typeof PointerEvent === "function"
          ? PointerEvent
          : MouseEvent;
      candidate.dispatchEvent(
        new EventType(type, {
          bubbles: true,
          cancelable: true,
          view: window,
          button: 0,
          buttons: type === "pointerdown" || type === "mousedown" ? 1 : 0,
          pointerId: 1,
          pointerType: "mouse",
          isPrimary: true,
        }),
      );
    }
    return true;
  }

  function activateAuditedSuggestion(
    provider,
    kind,
    candidate,
    identityMatch,
  ) {
    if (
      provider === "fliggy" &&
      kind === "lodging" &&
      identityMatch?.evidence === "audited_semantic_option_identity" &&
      candidate instanceof HTMLElement
    ) {
      HTMLElement.prototype.click.call(candidate);
      return "native_html_click";
    }
    dispatchPointerClick(candidate);
    return "pointer_sequence";
  }

  function auditedInputIdentity(input) {
    if (!isElementNode(input)) {
      return "";
    }
    const root = input.closest("#interForm, form") || input.parentElement;
    const nodes = [
      input,
      ...(
        root
          ? root.querySelectorAll(
              "input[name='cityUrl'], input[name='cityurl'], " +
              "[data-city-url], [data-cityurl], [data-city-code], " +
              "[data-city-id], [data-agent-city-code], [data-agent-city-id]",
            )
          : []
      ),
    ];
    return normalized(
      nodes
        .flatMap((node) => [
          node.getAttribute("cityUrl"),
          node.getAttribute("cityurl"),
          node.getAttribute("data-city-url"),
          node.getAttribute("data-cityurl"),
          node.getAttribute("data-city-code"),
          node.getAttribute("data-city-id"),
          node.getAttribute("data-agent-city-code"),
          node.getAttribute("data-agent-city-id"),
          node instanceof HTMLInputElement ? node.value : null,
        ])
        .filter(Boolean)
        .join(" "),
    );
  }

  function auditedVisibleValueMatches(input, identity) {
    if (!isElementNode(input) || !identity) {
      return false;
    }
    const value = normalized(readVisibleValue(input));
    const selectedLabels = identity.selectedLabels || identity.visibleLabels;
    return selectedLabels.some(
      (label) => value === normalized(label),
    );
  }

  function auditedDestinationReadbackScopes(input) {
    if (!isElementNode(input)) {
      return [];
    }
    const scopes = [];
    let node = input;
    let depth = 0;
    while (node && depth < 6) {
      scopes.push(node);
      if (
        node.matches(
          "#trip_main_content, #interForm, form, " +
            "[role='search'], [data-testid*='search-form']",
        )
      ) {
        break;
      }
      node = node.parentElement;
      depth += 1;
    }
    return scopes;
  }

  function exactSelectedDestinationLabel(element, identity) {
    if (!isElementNode(element) || !identity) {
      return null;
    }
    const selectedLabels = identity.selectedLabels || identity.visibleLabels;
    const values = [
      element instanceof HTMLInputElement ? element.value : null,
      element.getAttribute("aria-valuetext"),
      element.getAttribute("data-value"),
      element.getAttribute("data-selected-value"),
      cleanCounterText(element),
    ]
      .map((value) => String(value || "").trim())
      .filter(Boolean);
    return values.find((value) =>
      selectedLabels.some(
        (label) => normalized(value) === normalized(label),
      )
    ) || null;
  }

  function auditedDestinationControlRoots(scopes) {
    const roots = [];
    const seen = new Set();
    const add = (node) => {
      if (!isElementNode(node) || seen.has(node)) {
        return;
      }
      seen.add(node);
      roots.push(node);
    };
    for (const scope of scopes || []) {
      if (!isElementNode(scope) || !scope.isConnected) {
        continue;
      }
      if (cleanCounterText(scope).length <= 240) {
        add(scope);
      }
      const anchors = [
        ...scope.querySelectorAll(
          "input[data-testid='international-city-input'], " +
            "[data-agent-id*='international-city'], " +
            "[data-agent-type*='city-input'], " +
            "[data-testid*='international-city'], " +
            "[role='combobox'][aria-label*='目的地'], " +
            "input[aria-label*='目的地']",
        ),
      ].slice(0, 16);
      for (const anchor of anchors) {
        let node = anchor;
        let depth = 0;
        while (node && scope.contains(node) && depth < 4) {
          if (cleanCounterText(node).length <= 240) {
            add(node);
          }
          node = node.parentElement;
          depth += 1;
        }
      }
    }
    return roots.slice(0, 32);
  }

  function auditedSelectedDestinationSurface(scopes, identity) {
    if (!identity) {
      return null;
    }
    const seen = new Set();
    for (const scope of auditedDestinationControlRoots(scopes)) {
      if (!visible(scope)) {
        continue;
      }
      const candidates = [
        scope,
        ...scope.querySelectorAll(
          "input, [role='combobox'], [aria-valuetext], " +
            "[data-value], [data-selected-value], span, strong, p, div",
        ),
      ].slice(0, 96);
      for (const candidate of candidates) {
        if (
          seen.has(candidate) ||
          !visible(candidate) ||
          candidate.closest(
            "[role='listbox'], [data-testid='search-city-dropdown'], " +
              ".m-suggest-container, [class*='suggest-list']",
          )
        ) {
          continue;
        }
        seen.add(candidate);
        const readbackValue = exactSelectedDestinationLabel(
          candidate,
          identity,
        );
        if (readbackValue) {
          return {
            confirmed: true,
            value: readbackValue,
            evidence: "audited_selected_destination_surface",
          };
        }
      }
    }
    return null;
  }

  function auditedDestinationSurfaceDiagnostics(scopes) {
    return auditedDestinationControlRoots(scopes)
      .filter((node) => visible(node))
      .slice(0, MAX_SUGGESTION_DIAGNOSTICS)
      .map((node) => ({
        tag: node.tagName.toLowerCase(),
        role: node.getAttribute("role"),
        class: String(node.className || "").slice(0, 120),
        text_summary: privacySafeSuggestionText(
          cleanCounterText(node),
        ).slice(0, 120),
        value_summary: privacySafeSuggestionText(
          readVisibleValue(node),
        ).slice(0, 120),
        test_id: privacySafeSuggestionText(
          node.getAttribute("data-testid"),
        ).slice(0, 120) || null,
        agent_type: privacySafeSuggestionText(
          node.getAttribute("data-agent-type"),
        ).slice(0, 120) || null,
      }));
  }

  function auditedDismissedSuggestionReadback(
    provider,
    candidate,
    input,
    identity,
    readbackScopes = [],
  ) {
    if (
      (
        !["ctrip", "qunar", "fliggy", "tongcheng"].includes(provider)
      ) ||
      !candidate ||
      !identity
    ) {
      return null;
    }
    const candidateDismissed =
      !candidate.isConnected || !visible(candidate);
    if (!candidateDismissed) {
      return null;
    }
    const selectedSurface = auditedSelectedDestinationSurface(
      readbackScopes,
      identity,
    );
    if (selectedSurface) {
      return selectedSurface;
    }
    if (!input || !input.isConnected || !visible(input)) {
      return null;
    }
    if (
      provider === "ctrip" ||
      (
        provider === "fliggy" &&
        identity.identityKind === "country_destination"
      )
    ) {
      return auditedVisibleValueMatches(input, identity)
        ? {
            confirmed: true,
            value: readVisibleValue(input),
            evidence: "audited_dismissed_destination_input",
          }
        : null;
    }
    const value = normalized(readVisibleValue(input));
    return identity.englishLabels.some(
      (label) => value === normalized(label),
    )
      ? {
          confirmed: true,
          value: readVisibleValue(input),
          evidence: "audited_dismissed_destination_input",
        }
      : null;
  }

  function currentVisibleInput(provider, kind, field, fallback = null) {
    const hints = INPUT_HINTS[kind] && INPUT_HINTS[kind][field];
    const current = hints
      ? findInput(provider, kind, field, hints)
      : null;
    if (isElementNode(current)) {
      return current;
    }
    return isElementNode(fallback) ? fallback : null;
  }

  function selectedAuditedIdentity(input, identity) {
    if (!isElementNode(input) || !identity || identity.unresolved) {
      return false;
    }
    const expectedId = normalized(identity.id);
    if (!expectedId) {
      return false;
    }
    return (
      auditedInputIdentity(input).includes(expectedId) &&
      auditedVisibleValueMatches(input, identity)
    );
  }

  function suggestionSearchTokens(identity, value, code = null) {
    return [
      value,
      code,
      identity?.placeKey,
      ...(identity?.englishLabels || []),
      ...(identity?.visibleLabels || []),
      ...(identity?.exactCandidateLabels || []),
      ...(identity?.requiredAreaLabels || []),
    ]
      .map(normalized)
      .filter(Boolean)
      .filter((token, index, all) => all.indexOf(token) === index);
  }

  function suggestionCandidateMentionsTokens(candidate, tokens) {
    if (!isElementNode(candidate)) {
      return false;
    }
    if (!tokens.length) {
      return true;
    }
    const text = normalized(cleanCounterText(candidate));
    const attributes = normalized(suggestionAttributeText(candidate));
    return tokens.some(
      (token) => text.includes(token) || attributes.includes(token),
    );
  }

  function structurallyVisibleWithin(element, ancestor) {
    if (
      !isElementNode(element) ||
      !isElementNode(ancestor) ||
      !ancestor.contains(element)
    ) {
      return false;
    }
    let node = element;
    let depth = 0;
    while (node && depth < 8) {
      if (
        node.hidden === true ||
        node.inert === true ||
        node.getAttribute("aria-hidden") === "true"
      ) {
        return false;
      }
      const style = getComputedStyle(node);
      if (
        style.display === "none" ||
        style.visibility === "hidden" ||
        Number(style.opacity) === 0
      ) {
        return false;
      }
      if (node === ancestor) {
        return true;
      }
      node = node.parentElement;
      depth += 1;
    }
    return false;
  }

  function auditedLodgingSuggestionAncestor(
    provider,
    kind,
    evidenceCandidate,
  ) {
    if (kind !== "lodging" || !isElementNode(evidenceCandidate)) {
      return visible(evidenceCandidate) ? evidenceCandidate : null;
    }
    const insideAuditedContainer = (node) => {
      if (provider === "ctrip") {
        return Boolean(node.closest("#trip_main_content"));
      }
      if (provider === "fliggy") {
        return Boolean(
          node.closest("[data-testid='search-city-dropdown']"),
        );
      }
      if (provider === "qunar") {
        return Boolean(
          node.closest(
            "#interForm .m-suggest-container, " +
              "#interForm [class*='suggest']",
          ),
        );
      }
      return false;
    };
    const hasAuditedOptionSemantics = (node) => {
      if (provider === "ctrip") {
        return node.matches("div[tabindex='-1']");
      }
      if (provider === "fliggy") {
        const optionNodes = node.matches("[role='option']")
          ? [node]
          : [...node.querySelectorAll("[role='option']")];
        return (
          optionNodes.length === 1 &&
          optionNodes[0].contains(evidenceCandidate)
        );
      }
      if (provider === "qunar") {
        const nodeIsAuditedRow =
          node.matches("tr.item, li.item, [role='option']") ||
          (
            node.matches(".item") &&
            /^(?:TR|LI|DIV|BUTTON)$/.test(node.tagName)
          );
        const optionNodes = nodeIsAuditedRow
          ? [node]
          : [
              ...node.querySelectorAll(
                "tr.item, li.item, .item, [role='option']",
              ),
            ];
        return (
          optionNodes.length <= 2 &&
          optionNodes.some(
            (option) =>
              option === evidenceCandidate ||
              option.contains(evidenceCandidate),
          )
        );
      }
      return false;
    };
    let node = evidenceCandidate;
    let depth = 0;
    while (node && depth < 6) {
      const text = cleanCounterText(node);
      if (
        text.length > 0 &&
        text.length <= 240 &&
        insideAuditedContainer(node) &&
        hasAuditedOptionSemantics(node) &&
        visible(node) &&
        structurallyVisibleWithin(evidenceCandidate, node) &&
        !LODGING_NAME_PATTERN.test(text) &&
        !/预订|下单|支付|购买|优惠券|订单|去付款/.test(text)
      ) {
        return node;
      }
      node = node.parentElement;
      depth += 1;
    }
    return null;
  }

  function suggestionCandidatePairs(provider, kind, matchTokens = []) {
    const roots = new Map();
    const seenRoots = new Set();
    rootScan:
    for (const selector of suggestionSelectors(provider, kind)) {
      for (const evidenceCandidate of document.querySelectorAll(selector)) {
        if (
          !isElementNode(evidenceCandidate) ||
          !suggestionCandidateMentionsTokens(
            evidenceCandidate,
            matchTokens,
          )
        ) {
          continue;
        }
        const clickCandidate = auditedLodgingSuggestionAncestor(
          provider,
          kind,
          evidenceCandidate,
        );
        if (!clickCandidate) {
          continue;
        }
        if (!roots.has(clickCandidate)) {
          roots.set(clickCandidate, []);
        }
        roots.get(clickCandidate).push(evidenceCandidate);
        if (!seenRoots.has(clickCandidate)) {
          seenRoots.add(clickCandidate);
        }
        if (seenRoots.size >= MAX_MATCHED_SUGGESTION_ROOTS) {
          break rootScan;
        }
      }
    }
    const rankedRoots = [...roots.entries()].sort(
      ([left], [right]) =>
        cleanCounterText(left).length - cleanCounterText(right).length,
    );
    const pairs = [];
    for (const [clickCandidate, seededEvidence] of rankedRoots) {
      const evidenceCandidates = [
        clickCandidate,
        ...seededEvidence,
        ...clickCandidate.querySelectorAll(
          "span, div, p, strong, b, em, mark, [data-testid], [data-value]",
        ),
      ];
      const seenEvidence = new Set();
      let evidenceMatches = 0;
      for (const evidenceCandidate of evidenceCandidates) {
        if (
          seenEvidence.has(evidenceCandidate) ||
          !isElementNode(evidenceCandidate) ||
          !suggestionCandidateMentionsTokens(
            evidenceCandidate,
            matchTokens,
          ) ||
          (
            !visible(evidenceCandidate) &&
            !structurallyVisibleWithin(
              evidenceCandidate,
              clickCandidate,
            )
          )
        ) {
          continue;
        }
        seenEvidence.add(evidenceCandidate);
        const textLength = cleanCounterText(evidenceCandidate).length;
        if (textLength === 0 || textLength > 240) {
          continue;
        }
        pairs.push({ clickCandidate, evidenceCandidate });
        evidenceMatches += 1;
        if (
          evidenceMatches >= MAX_SUGGESTION_EVIDENCE_PER_ROOT ||
          pairs.length >= MAX_SUGGESTION_CANDIDATE_PAIRS
        ) {
          break;
        }
      }
      if (pairs.length >= MAX_SUGGESTION_CANDIDATE_PAIRS) {
        break;
      }
    }
    return pairs.sort(
      (left, right) =>
        left.evidenceCandidate.children.length -
          right.evidenceCandidate.children.length ||
        cleanCounterText(left.evidenceCandidate).length -
          cleanCounterText(right.evidenceCandidate).length,
    );
  }

  function suggestionPollTimeoutMs(provider, kind) {
    return provider === "ctrip" && kind === "lodging"
      ? CTRIP_LODGING_SUGGESTION_POLL_TIMEOUT_MS
      : DEFAULT_SUGGESTION_POLL_TIMEOUT_MS;
  }

  async function selectVisibleSuggestion(
    provider,
    kind,
    value,
    code = null,
    input = null,
    expectedPlaceKey = null,
    field = "destination",
  ) {
    const target = normalized(value);
    const targetCode = normalized(code);
    const identity = suggestionIdentity(
      provider,
      kind,
      value,
      expectedPlaceKey,
    );
    const matchTokens = suggestionSearchTokens(identity, value, code);
    const deadline = Date.now() + suggestionPollTimeoutMs(provider, kind);
    let menuObserved = false;
    do {
      const candidates = suggestionCandidatePairs(
        provider,
        kind,
        matchTokens,
      );
      for (const { clickCandidate, evidenceCandidate } of candidates) {
        menuObserved = true;
        const text = textDescriptor(evidenceCandidate);
        const identityMatch = suggestionIdentityMatches(
          clickCandidate,
          identity,
          evidenceCandidate,
        );
        const targetMatches = identity
          ? identityMatch.matched
          : text.includes(target) || (targetCode && text.includes(targetCode));
        if (
          targetMatches &&
          !/预订|下单|支付|购买|优惠券|订单|去付款/.test(text)
        ) {
          const beforeInput = currentVisibleInput(
            provider,
            kind,
            field,
            input,
          );
          const beforeValue = beforeInput ? readVisibleValue(beforeInput) : null;
          const destinationReadbackScopes =
            auditedDestinationReadbackScopes(beforeInput);
          let activationMode = "native_element_click";
          if (
            provider === "ctrip" ||
            provider === "qunar" ||
            (provider === "fliggy" && identityMatch.matched)
          ) {
            activationMode = activateAuditedSuggestion(
              provider,
              kind,
              clickCandidate,
              identityMatch,
            );
          } else {
            clickCandidate.click();
          }
          if (identity) {
            const readbackDeadline =
              Date.now() + (kind === "lodging" ? 4000 : 1200);
            let visibleReadback = false;
            let identityReadback = false;
            let optionSelected = false;
            let inputChanged = false;
            let selected = false;
            let readbackInput = beforeInput;
            let selectedSurface = null;
            do {
              await delay(100);
              readbackInput = currentVisibleInput(
                provider,
                kind,
                field,
                beforeInput,
              );
              visibleReadback = auditedVisibleValueMatches(
                readbackInput,
                identity,
              );
              identityReadback = selectedAuditedIdentity(
                readbackInput,
                identity,
              );
              optionSelected =
                clickCandidate.getAttribute("aria-selected") === "true";
              inputChanged =
                Boolean(readbackInput) &&
                readVisibleValue(readbackInput) !== beforeValue;
              const providerSelectionAcknowledged =
                auditedDismissedSuggestionReadback(
                  provider,
                  clickCandidate,
                  readbackInput,
                  identity,
                  destinationReadbackScopes,
                );
              selectedSurface = providerSelectionAcknowledged;
              selected =
                identityMatch.matched &&
                (visibleReadback || Boolean(selectedSurface?.confirmed)) &&
                (
                  provider === "fliggy" && kind === "lodging"
                    ? Boolean(providerSelectionAcknowledged?.confirmed)
                    : (
                        inputChanged ||
                        optionSelected ||
                        identityReadback ||
                        Boolean(providerSelectionAcknowledged?.confirmed)
                      )
                );
            } while (!selected && Date.now() < readbackDeadline);
            return {
              selected,
              menu_observed: true,
              selected_text: cleanCounterText(evidenceCandidate).slice(0, 120),
              selected_id:
                suggestionAttributeText(clickCandidate).slice(0, 120) || null,
              identity_evidence: selected
                ? (
                    identityReadback
                      ? "audited_selected_city_identity"
                      : identityMatch.evidence
                  )
                : "selected_city_readback_unconfirmed",
              readback_value:
                selectedSurface?.value ||
                (readbackInput
                  ? readVisibleValue(readbackInput).slice(0, 120) || null
                  : null),
              readback_identity:
                readbackInput
                  ? auditedInputIdentity(readbackInput).slice(0, 160)
                  : null,
              readback_surfaces: selected
                ? []
                : auditedDestinationSurfaceDiagnostics(
                    destinationReadbackScopes,
                  ),
              activation_mode: activationMode,
            };
          }
          return {
            selected: true,
            menu_observed: true,
            selected_text: cleanCounterText(evidenceCandidate).slice(0, 120),
            selected_id:
              suggestionAttributeText(clickCandidate).slice(0, 120) || null,
          };
        }
      }
      await delay(100);
    } while (Date.now() < deadline);
    return {
      selected: false,
      menu_observed: menuObserved,
      selected_text: null,
      selected_id: null,
      identity_evidence: identity && identity.unresolved
        ? "expected_lodging_place_key_missing"
        : null,
    };
  }

  function ctripCalendarAriaPrefix(isoDate) {
    const match = String(isoDate || "").match(/^(\d{4})-(\d{2})-(\d{2})$/);
    if (!match) {
      return null;
    }
    return `${Number(match[1])}年${Number(match[2])}月${Number(match[3])}日`;
  }

  function ctripCalendarMonthOrdinal(value) {
    const match = String(value || "").match(/(\d{4})年\s*(\d{1,2})月/);
    if (!match) {
      return null;
    }
    const year = Number(match[1]);
    const month = Number(match[2]);
    return Number.isInteger(year) && month >= 1 && month <= 12
      ? year * 12 + month - 1
      : null;
  }

  function visibleCtripCalendarMonths() {
    const months = new Set();
    const nodes = document.querySelectorAll(
      "[aria-label*='年'][aria-label*='月'], [title*='年'][title*='月'], " +
      "h2, h3, [class*='month'], [class*='Month']",
    );
    for (const node of [...nodes].slice(0, 600)) {
      if (!visible(node)) {
        continue;
      }
      const evidence = [
        node.getAttribute("aria-label"),
        node.getAttribute("title"),
        node.textContent,
      ].filter(Boolean).join(" ");
      const ordinal = ctripCalendarMonthOrdinal(evidence);
      if (ordinal !== null) {
        months.add(ordinal);
      }
    }
    return [...months].sort((left, right) => left - right);
  }

  function ctripCalendarNavigationDirection(targetOrdinal, visibleMonths) {
    if (!Number.isInteger(targetOrdinal) || !visibleMonths.length) {
      return null;
    }
    if (targetOrdinal < visibleMonths[0]) {
      return "previous";
    }
    if (targetOrdinal > visibleMonths[visibleMonths.length - 1]) {
      return "next";
    }
    return null;
  }

  function ctripCalendarNavigationControl(direction) {
    const patterns = direction === "previous"
      ? ["上个月", "上一月", "前一个月", "previousmonth", "prevmonth"]
      : ["下个月", "下一月", "后一个月", "nextmonth"];
    return [...document.querySelectorAll(
      "button, [role='button'], a[aria-label], a[title]",
    )].find((node) => {
      if (!visible(node)) {
        return false;
      }
      const evidence = normalized([
        node.getAttribute("aria-label"),
        node.getAttribute("title"),
        node.textContent,
      ].filter(Boolean).join(" "));
      return patterns.some(
        (pattern) => evidence === pattern || evidence.endsWith(pattern),
      );
    }) || null;
  }

  async function selectCtripHotelDate(input, isoDate) {
    const ariaPrefix = ctripCalendarAriaPrefix(isoDate);
    if (!ariaPrefix) {
      return { selected: false, reason: "invalid_iso_date" };
    }
    input.click();
    const targetMonth = ctripCalendarMonthOrdinal(ariaPrefix);
    const deadline = Date.now() + 7000;
    let navigationCount = 0;
    do {
      const day = [
        ...document.querySelectorAll(
          `[role='checkbox'][aria-label^="${CSS.escape(ariaPrefix)}"], ` +
          `[role='button'][aria-label^="${CSS.escape(ariaPrefix)}"]`,
        ),
      ].find(visible);
      if (day) {
        const selectedText =
          day.getAttribute("aria-label") || cleanCounterText(day);
        day.click();
        await delay(160);
        return {
          selected: true,
          selected_text: String(selectedText).slice(0, 120),
        };
      }
      const visibleMonths = visibleCtripCalendarMonths();
      const direction = ctripCalendarNavigationDirection(
        targetMonth,
        visibleMonths,
      );
      if (direction && navigationCount < 12) {
        const control = ctripCalendarNavigationControl(direction);
        if (control) {
          control.click();
          navigationCount += 1;
          await delay(220);
          continue;
        }
      }
      await delay(100);
    } while (Date.now() < deadline);
    return {
      selected: false,
      reason: "calendar_day_not_found",
      target_month: targetMonth,
      visible_months: visibleCtripCalendarMonths(),
      navigation_count: navigationCount,
    };
  }

  function textDescriptor(element) {
    if (!isElementNode(element)) {
      return "";
    }
    return normalized(
      [
        element.textContent,
        element.getAttribute("aria-label"),
        element.getAttribute("title"),
      ].filter(Boolean).join(" "),
    );
  }

  function safeButton(labels, root = document) {
    const prohibited = /预订|下单|支付|购买|优惠券|订单|去付款/;
    return [...root.querySelectorAll(
      "button, [role='button'], [role='tab'], [role='radio'], a, label",
    )].find(
      (element) => {
        const text = textDescriptor(element);
        return (
          visible(element) &&
          !prohibited.test(text) &&
          labels.some((label) => text.includes(normalized(label)))
        );
      },
    );
  }

  function selectVisibleOption(labels, root = document) {
    const option = safeButton(labels, root);
    if (option) {
      option.click();
      return true;
    }
    return false;
  }

  function safeModeControl(labels) {
    const prohibited = /预订|下单|支付|购买|优惠券|订单|去付款/;
    return [...document.querySelectorAll(
      "label, button, a, [role='radio'], [role='tab'], " +
        "[class*='radio'], [class*='Radio'], [class*='trip'], " +
        "[class*='Trip'], li, span, div",
    )]
      .filter((element) => {
        if (!visible(element)) {
          return false;
        }
        const text = textDescriptor(element);
        return (
          text.length > 0 &&
          text.length <= 40 &&
          !prohibited.test(text) &&
          labels.some((label) => text === normalized(label))
        );
      })
      .sort(
        (left, right) =>
          left.querySelectorAll("*").length - right.querySelectorAll("*").length,
      )[0] || null;
  }

  function findOccupancyTrigger(labels, root = document) {
    const selectors = [
      "input",
      "button",
      "[role='button']",
      "[role='combobox']",
      "[tabindex]",
      "[class*='passenger']",
      "[class*='Passenger']",
      "[class*='guest']",
      "[class*='Guest']",
    ].join(", ");
    return [...root.querySelectorAll(selectors)]
      .filter((element) => {
        const text = cleanCounterText(element);
        return text.length <= 160;
      })
      .find(
      (element) => {
        const description = `${descriptor(element)} ${textDescriptor(element)}`;
        return (
          visible(element) &&
          labels.some((label) => description.includes(normalized(label)))
        );
      });
  }

  function explicitCount(element, field) {
    if (!isElementNode(element) || !visible(element)) {
      return null;
    }
    const agentCurrentValue = element.getAttribute("data-agent-current-value");
    if (agentCurrentValue !== null) {
      const parsed = Number.parseInt(agentCurrentValue, 10);
      if (Number.isInteger(parsed)) {
        return parsed;
      }
    }
    const valued = element.matches("input, [role='spinbutton']")
      ? element
      : [...element.querySelectorAll("input, [role='spinbutton']")].find(visible);
    if (valued) {
      const raw =
        valued.getAttribute("aria-valuenow") ||
        (valued instanceof HTMLInputElement ? valued.value : valued.textContent);
      const parsed = Number.parseInt(String(raw || ""), 10);
      if (Number.isInteger(parsed)) {
        return parsed;
      }
    }
    const text = cleanCounterText(element);
    const patterns = field === "rooms"
      ? [/(?:房间|客房|room)\D{0,8}(\d+)/i, /(\d+)\s*(?:间|rooms?)/i]
      : [
          /(\d+)\s*(?:位\s*)?成人/i,
          /(?:成人|住客|adult)\s*[:：]?\s*(\d+)/i,
          /(\d+)\s*adults?/i,
        ];
    for (const pattern of patterns) {
      const match = text.match(pattern);
      if (match) {
        return Number.parseInt(match[1], 10);
      }
    }
    return null;
  }

  function visibleCountEvidence(root, field, target) {
    const selectors = [
      "input",
      "[role='spinbutton']",
      "[aria-label]",
      "[data-testid]",
      "[class*='adult']",
      "[class*='Adult']",
      "[class*='guest']",
      "[class*='Guest']",
      "[class*='room']",
      "[class*='Room']",
      ".adult-children",
    ].join(", ");
    const seen = new Set();
    const candidates = [
      ...(root && root.nodeType === 1 ? [root] : []),
      ...(
        root && typeof root.querySelectorAll === "function"
          ? root.querySelectorAll(selectors)
          : []
      ),
    ];
    for (const candidate of candidates) {
      if (seen.has(candidate) || !visible(candidate)) {
        continue;
      }
      seen.add(candidate);
      const text = cleanCounterText(candidate);
      if (text.length > 180) {
        continue;
      }
      if (explicitCount(candidate, field) === target) {
        return {
          text: text.slice(0, 120),
          descriptor: descriptor(candidate).slice(0, 120),
        };
      }
    }
    return null;
  }

  function auditedProviderOccupancySurface(provider, kind) {
    if (kind !== "lodging") {
      return null;
    }
    let candidates = [];
    if (provider === "ctrip") {
      candidates = [...document.querySelectorAll("[role='button']")].filter(
        (element) =>
          element.querySelector(
            "[class*='ic-user'], [class*='ic_user']",
          ),
      );
    } else if (provider === "fliggy") {
      candidates = [
        ...document.querySelectorAll(
          "[data-agent-id='international-adult-select']" +
          "[data-agent-type='adult-count-select']",
        ),
      ];
    } else if (provider === "qunar") {
      candidates = [
        ...document.querySelectorAll("#interForm .adult-children"),
      ];
    }
    return candidates.find((element) => {
      if (!visible(element)) {
        return false;
      }
      const text = cleanCounterText(element);
      if (!text || text.length > 160) {
        return false;
      }
      if (provider === "ctrip") {
        return /(?:\d+\s*间).*(?:\d+\s*(?:位)?成人)/.test(text);
      }
      if (provider === "fliggy") {
        return /成人\D{0,8}\d+|\d+\s*(?:位)?成人/.test(text);
      }
      return /每间人数.*\d+\s*(?:位)?成人/.test(text);
    }) || null;
  }

  function auditedProviderCountEvidence(provider, kind, field, target) {
    const surface = auditedProviderOccupancySurface(provider, kind);
    if (!surface || explicitCount(surface, field) !== target) {
      return null;
    }
    return {
      text: cleanCounterText(surface).slice(0, 120),
      descriptor: descriptor(surface).slice(0, 120),
      provider,
      scope: "provider_audited_occupancy_surface",
    };
  }

  function auditedImplicitSingleRoomEvidence(provider, kind, field, target) {
    if (
      !fixedSingleRoomSurface(provider, kind, field) ||
      target !== 1
    ) {
      return null;
    }
    const surface = auditedProviderOccupancySurface(provider, kind);
    const adults = explicitCount(surface, "adults");
    if (!surface || !Number.isInteger(adults) || adults < 1) {
      return null;
    }
    return {
      text: cleanCounterText(surface).slice(0, 120),
      descriptor: descriptor(surface).slice(0, 120),
      provider,
      scope: "provider_audited_single_room_surface",
    };
  }

  function fixedSingleRoomSurface(provider, kind, field) {
    return (
      kind === "lodging" &&
      field === "rooms" &&
      (provider === "qunar" || provider === "fliggy" || provider === "tongcheng")
    );
  }

  function cleanCounterText(element) {
    if (!element) {
      return "";
    }
    const textContent =
      typeof element.textContent === "string" ? element.textContent : "";
    const ariaLabel =
      isElementNode(element) ? element.getAttribute("aria-label") : "";
    return String(textContent || ariaLabel || "")
      .replace(/\s+/g, " ")
      .trim();
  }

  function occupancyRow(hints, root = document) {
    return [...root.querySelectorAll(
      "[data-testid], [role='group'], [class*='guest'], [class*='Guest'], [class*='passenger'], [class*='Passenger'], li, div",
    )]
      .filter((element) => {
        const text = `${descriptor(element)} ${textDescriptor(element)}`;
        return (
          visible(element) &&
          text.length <= 240 &&
          hints.some((hint) => text.includes(normalized(hint))) &&
          element.querySelector("input, [role='spinbutton'], button, [role='button']")
        );
      })
      .sort(
        (left, right) =>
          cleanCounterText(left).length - cleanCounterText(right).length,
      )[0] || null;
  }

  function deltaButton(row, delta) {
    const pattern = delta > 0
      ? /增加|添加|加一|plus|increment|^\+$/
      : /减少|减一|minus|decrement|^−$|^-$|^－$/;
    return [...row.querySelectorAll("button, [role='button']")].find((element) => {
      const label = [
        element.textContent,
        element.getAttribute("aria-label"),
        element.getAttribute("title"),
      ].filter(Boolean).join(" ").trim();
      return visible(element) && pattern.test(label);
    });
  }

  async function setVisibleCount(
    provider,
    kind,
    field,
    target,
    root = document,
  ) {
    const profile = OCCUPANCY_PROFILES[provider] &&
      OCCUPANCY_PROFILES[provider][kind];
    const hints = profile && profile[field];
    if (!profile || !hints || !Number.isInteger(target)) {
      return { ok: false, reason: "unsupported_occupancy_control" };
    }
    if (fixedSingleRoomSurface(provider, kind, field) && target !== 1) {
      return {
        ok: false,
        readback: null,
        reason: "rooms_above_provider_single_room_surface",
      };
    }
    if (
      provider === "tongcheng" &&
      kind === "lodging" &&
      (
        (field === "adults" && target >= 1 && target <= 9) ||
        (field === "rooms" && target === 1)
      )
    ) {
      return {
        ok: true,
        readback: target,
        reason: null,
        evidence:
          field === "adults"
            ? "audited_result_url_adults_parameter"
            : "audited_single_room_result_contract",
        visible_evidence: {
          text:
            field === "adults"
              ? `adultsNumber=${target}`
              : "roomNum=1",
          descriptor: "tongcheng_audited_read_only_result_url",
          provider,
          scope: "provider_audited_result_url_contract",
        },
      };
    }
    const providerCountEvidence = auditedProviderCountEvidence(
      provider,
      kind,
      field,
      target,
    );
    if (providerCountEvidence) {
      return {
        ok: true,
        readback: target,
        reason: null,
        evidence: "audited_visible_occupancy_surface",
        visible_evidence: providerCountEvidence,
      };
    }
    const implicitSingleRoomEvidence = auditedImplicitSingleRoomEvidence(
      provider,
      kind,
      field,
      target,
    );
    if (implicitSingleRoomEvidence) {
      return {
        ok: true,
        readback: 1,
        reason: null,
        evidence: "implicit_single_room_surface",
        visible_evidence: implicitSingleRoomEvidence,
      };
    }
    let row = [...root.querySelectorAll("input, [role='spinbutton']")].find(
      (element) =>
        visible(element) &&
        hints.some((hint) => descriptor(element).includes(normalized(hint))),
    );
    if (!row) {
      const trigger = findOccupancyTrigger(profile.trigger, root);
      if (!trigger) {
        const visibleEvidence = visibleCountEvidence(root, field, target);
        if (visibleEvidence) {
          return {
            ok: true,
            readback: target,
            reason: null,
            evidence: "visible_occupancy_default",
            visible_evidence: visibleEvidence,
          };
        }
        if (fixedSingleRoomSurface(provider, kind, field)) {
          return {
            ok: false,
            readback: null,
            reason: "rooms_visible_default_unconfirmed",
          };
        }
        return { ok: false, reason: `${field}_occupancy_trigger_missing` };
      }
      const triggerReadback = explicitCount(trigger, field);
      if (triggerReadback === target) {
        return {
          ok: true,
          readback: target,
          reason: null,
          evidence: "visible_occupancy_trigger",
        };
      }
      trigger.click();
      const popupDeadline = Date.now() + 2000;
      do {
        await delay(100);
        row = occupancyRow(hints, root) ||
          (root === document ? null : occupancyRow(hints, document));
      } while (!row && Date.now() < popupDeadline);
    }
    if (!row) {
      const visibleEvidence = visibleCountEvidence(root, field, target);
      if (visibleEvidence) {
        return {
          ok: true,
          readback: target,
          reason: null,
          evidence: "visible_occupancy_default",
          visible_evidence: visibleEvidence,
        };
      }
      if (fixedSingleRoomSurface(provider, kind, field)) {
        return {
          ok: false,
          readback: null,
          reason: "rooms_visible_default_unconfirmed",
        };
      }
      return { ok: false, reason: `${field}_control_missing` };
    }
    const input = row.matches("input, [role='spinbutton']")
      ? row
      : [...row.querySelectorAll("input, [role='spinbutton']")].find(visible);
    if (input instanceof HTMLInputElement) {
      setVisibleValue(input, String(target));
      const readback = explicitCount(input, field);
      return {
        ok: readback === target,
        readback,
        reason: readback === target ? null : `${field}_readback_mismatch`,
      };
    }
    let readback = explicitCount(row, field);
    if (!Number.isInteger(readback)) {
      return { ok: false, reason: `${field}_readback_missing` };
    }
    for (let step = 0; step < 20 && readback !== target; step += 1) {
      const button = deltaButton(row, target > readback ? 1 : -1);
      if (!button) {
        return { ok: false, readback, reason: `${field}_adjust_button_missing` };
      }
      button.click();
      await delay(80);
      readback = explicitCount(row, field);
      if (!Number.isInteger(readback)) {
        return { ok: false, reason: `${field}_readback_missing` };
      }
    }
    return {
      ok: readback === target,
      readback,
      reason: readback === target ? null : `${field}_adjustment_limit`,
    };
  }

  function commonSearchRoot(elements) {
    const values = elements.filter(Boolean);
    if (!values.length) {
      return document;
    }
    const firstAncestors = [];
    for (
      let node = values[0];
      node && node !== document.documentElement;
      node = node.parentElement
    ) {
      firstAncestors.push(node);
    }
    for (const ancestor of firstAncestors) {
      if (
        values.every((element) => ancestor.contains(element)) &&
        ancestor.querySelector("button, [role='button']")
      ) {
        return ancestor;
      }
    }
    return document;
  }

  function searchButton(provider, kind, root) {
    const configured =
      SEARCH_BUTTON_SELECTORS[provider]?.[kind] || [];
    const exact = queryVisible(configured, root);
    if (
      exact &&
      !/预订|下单|支付|购买|优惠券|订单|去付款/.test(
        textDescriptor(exact),
      )
    ) {
      return exact;
    }
    return safeButton(SEARCH_LABELS[kind], root);
  }

  async function selectFlightMode(
    provider,
    { skipProviderModeSwitch = false } = {},
  ) {
    if (provider === "fliggy" && !skipProviderModeSwitch) {
      const internationalTab = queryVisible([
        "button[data-testid='flight-tab-international']",
      ]);
      if (
        internationalTab &&
        internationalTab.getAttribute("aria-selected") !== "true" &&
        !internationalTab.classList.contains("selected-tab-item")
      ) {
        internationalTab.click();
        await delay(160);
      }
    }
    if (!skipProviderModeSwitch && provider !== "tongcheng") {
      selectVisibleOption(
        ["国际/中国港澳台", "国际·港澳台机票", "国际机票", "国际"],
      );
      await delay(120);
    }
    const roundTrip = queryVisible(ROUND_TRIP_SELECTORS[provider] || []);
    if (roundTrip) {
      if (!roundTrip.checked) {
        roundTrip.click();
        await delay(120);
      }
      return roundTrip.checked !== false;
    }
    const selected =
      selectVisibleOption(["往返", "往返行程", "round trip"]) ||
      (() => {
        const control = safeModeControl(["往返", "往返行程", "round trip"]);
        if (!control) {
          return false;
        }
        control.click();
        return true;
      })();
    await delay(120);
    return selected || dateInputs().length >= 2;
  }

  function cityCode(field, query) {
    return field === "origin"
      ? query.origin_code || null
      : query.destination_code || null;
  }

  function selectedCityIsExplicit(
    provider,
    kind,
    input,
    value,
    code,
    suggestion,
    expectedPlaceKey = null,
  ) {
    if (!isElementNode(input)) {
      return false;
    }
    if (suggestion && suggestion.selected) {
      return true;
    }
    const identity = suggestionIdentity(
      provider,
      kind,
      value,
      expectedPlaceKey,
    );
    if (identity) {
      return selectedAuditedIdentity(input, identity);
    }
    const context = normalized(
      [
        readVisibleValue(input),
        input.getAttribute("data-code"),
        input.getAttribute("data-city-code"),
        input.getAttribute("data-airport-code"),
        input.parentElement && input.parentElement.textContent,
      ].filter(Boolean).join(" "),
    );
    const normalizedCode = normalized(code);
    return (
      visibleFieldMatches("destination", readVisibleValue(input), value) &&
      Boolean(normalizedCode && context.includes(normalizedCode))
    );
  }

  function fliggyLodgingSearchStrategy(provider, kind, query) {
    const expectedPlaceKey = normalized(
      query &&
      query.options &&
      query.options.expected_lodging_place_key,
    );
    const identity =
      AUDITED_LODGING_IDENTITIES.fliggy &&
      AUDITED_LODGING_IDENTITIES.fliggy[expectedPlaceKey];
    if (
      provider !== "fliggy" ||
      kind !== "lodging" ||
      !identity ||
      !/^\d{6}$/.test(String(identity.id || "")) ||
      !Array.isArray(identity.selectedLabels) ||
      identity.selectedLabels.length !== 1
    ) {
      return null;
    }
    return {
      providerDestination: identity.selectedLabels[0],
      providerDestinationId: identity.id,
      keyword: null,
      plannedDestination: String(query.destination || "Maafushi"),
      evidenceScope:
        "provider_audited_exact_city_id_then_place_revalidation",
    };
  }

  function qunarLodgingSearchStrategy(provider, kind, query) {
    const expectedPlaceKey = normalized(
      query &&
      query.options &&
      query.options.expected_lodging_place_key,
    );
    const identity =
      AUDITED_LODGING_IDENTITIES.qunar &&
      AUDITED_LODGING_IDENTITIES.qunar[expectedPlaceKey];
    if (
      provider !== "qunar" ||
      kind !== "lodging" ||
      !identity ||
      !/^i-[a-z0-9_-]+$/.test(String(identity.id || "")) ||
      !Array.isArray(identity.selectedLabels) ||
      !identity.selectedLabels.length
    ) {
      return null;
    }
    return {
      providerDestination: identity.selectedLabels[0],
      providerDestinationId: identity.id,
      keyword: null,
      plannedDestination: String(query.destination || ""),
      evidenceScope:
        "provider_audited_exact_city_slug_then_place_revalidation",
    };
  }

  function tongchengLodgingSearchStrategy(provider, kind, query) {
    const expectedPlaceKey = normalized(
      query &&
      query.options &&
      query.options.expected_lodging_place_key,
    );
    const identity =
      AUDITED_LODGING_IDENTITIES.tongcheng &&
      AUDITED_LODGING_IDENTITIES.tongcheng[expectedPlaceKey];
    if (
      provider !== "tongcheng" ||
      kind !== "lodging" ||
      !identity ||
      !/^110\d{6}$/.test(String(identity.id || "")) ||
      !Array.isArray(identity.selectedLabels) ||
      identity.selectedLabels.length !== 1
    ) {
      return null;
    }
    return {
      providerDestination: identity.selectedLabels[0],
      providerDestinationId: identity.id,
      keyword: null,
      plannedDestination: String(query.destination || ""),
      evidenceScope:
        "provider_audited_exact_overseas_city_id_then_place_revalidation",
    };
  }

  function fliggyLodgingResultUrl(query, strategy) {
    const validDate = (value) => /^\d{4}-\d{2}-\d{2}$/.test(
      String(value || ""),
    );
    const adults = Number(query && query.adults);
    const rooms = Number(query && query.rooms);
    if (
      !strategy ||
      !/^\d{6}$/.test(String(strategy.providerDestinationId || "")) ||
      !String(strategy.providerDestination || "").trim() ||
      !validDate(query && query.start_date) ||
      !validDate(query && query.end_date) ||
      String(query.end_date) <= String(query.start_date) ||
      !Number.isInteger(adults) ||
      adults < 1 ||
      adults > 9 ||
      rooms !== 1
    ) {
      return null;
    }
    const url = new URL("https://hotel.fliggy.com/hotel_list3.htm");
    url.searchParams.set(
      "spm",
      "181.11358650.hotelModule.internationalSearch",
    );
    url.searchParams.set("city", strategy.providerDestinationId);
    url.searchParams.set("cityName", strategy.providerDestination);
    url.searchParams.set("checkIn", String(query.start_date));
    url.searchParams.set("checkOut", String(query.end_date));
    url.searchParams.set("keywords", "");
    url.searchParams.set("aNum_1", String(adults));
    url.searchParams.set("cNum_1", "0");
    return url.href;
  }

  function qunarLodgingResultUrl(query, strategy) {
    const validDate = (value) => /^\d{4}-\d{2}-\d{2}$/.test(
      String(value || ""),
    );
    const adults = Number(query && query.adults);
    const rooms = Number(query && query.rooms);
    if (
      !strategy ||
      !/^i-[a-z0-9_-]+$/.test(
        String(strategy.providerDestinationId || ""),
      ) ||
      !String(strategy.providerDestination || "").trim() ||
      !validDate(query && query.start_date) ||
      !validDate(query && query.end_date) ||
      String(query.end_date) <= String(query.start_date) ||
      !Number.isInteger(adults) ||
      adults < 1 ||
      adults > 9 ||
      rooms !== 1
    ) {
      return null;
    }
    const url = new URL("https://hotel.qunar.com/intl/search.jsp");
    url.searchParams.set("toCity", strategy.providerDestination);
    url.searchParams.set("fromDate", String(query.start_date));
    url.searchParams.set("toDate", String(query.end_date));
    url.searchParams.set("cityurl", strategy.providerDestinationId);
    url.searchParams.set("from", "globalhotelpages");
    return url.href;
  }

  function qunarLodgingResultQueryReadback(
    provider,
    kind,
    query,
    root = document,
    pageUrl = location.href,
  ) {
    const resultControlDiagnostics = () => ({
      visible_controls: [
        ...(
          root && typeof root.querySelectorAll === "function"
            ? root.querySelectorAll(
                "input, button, [role='button'], [role='spinbutton']",
              )
            : []
        ),
      ]
        .filter(visible)
        .slice(0, MAX_CONTROL_DIAGNOSTICS)
        .map((element) => {
          const rawValue = readVisibleValue(element).trim();
          const normalizedValue = normalized(rawValue);
          const valueKind = /^\d{4}[-/]\d{2}[-/]\d{2}$/.test(rawValue)
            ? "calendar_date"
            : [
                "马富施",
                "maafushi",
                "胡鲁马累",
                "胡鲁马累岛",
                "hulhumale",
              ]
                .some((label) => normalizedValue === normalized(label))
              ? "audited_destination"
              : rawValue
                ? "other_non_empty"
                : "empty";
          return {
            tag: element.tagName.toLowerCase(),
            id: String(element.id || "").slice(0, 80) || null,
            class: String(element.className || "").slice(0, 120) || null,
            name: String(element.getAttribute("name") || "").slice(0, 80) || null,
            type: String(element.getAttribute("type") || "").slice(0, 40) || null,
            placeholder:
              String(element.getAttribute("placeholder") || "").slice(0, 100) ||
              null,
            aria_label:
              String(element.getAttribute("aria-label") || "").slice(0, 100) ||
              null,
            value_kind: valueKind,
          };
        }),
      visible_occupancy_surfaces: [
        ...(
          root && typeof root.querySelectorAll === "function"
            ? root.querySelectorAll(
                ".adult-children, [class*='adult'], [class*='guest'], [class*='person']",
              )
            : []
        ),
      ]
        .filter(visible)
        .filter((element) => /成人|儿童|每间人数|adult|child|guest/i.test(
          cleanCounterText(element),
        ))
        .slice(0, 8)
        .map((element) => ({
          tag: element.tagName.toLowerCase(),
          class: String(element.className || "").slice(0, 120) || null,
          text: cleanCounterText(element).slice(0, 120),
        })),
    });
    const rejected = (reason, readbackQuery = {}, gates = {}) => ({
      confirmed: false,
      reason,
      confirmed_query: null,
      readback_query: readbackQuery,
      gates,
      diagnostics: resultControlDiagnostics(),
    });
    const expectedPlaceKey = normalized(
      query && query.options && query.options.expected_lodging_place_key,
    );
    const identity =
      AUDITED_LODGING_IDENTITIES.qunar &&
      AUDITED_LODGING_IDENTITIES.qunar[expectedPlaceKey];
    if (
      provider !== "qunar" ||
      kind !== "lodging" ||
      !identity ||
      !/^i-[a-z0-9_-]+$/.test(String(identity.id || "")) ||
      !root ||
      typeof root.querySelectorAll !== "function"
    ) {
      return rejected("unsupported_qunar_result_query");
    }
    let parsed;
    try {
      parsed = new URL(pageUrl);
    } catch {
      return rejected("invalid_qunar_result_url");
    }
    const expectedPath = `/city/${identity.id}`;
    const observedPath = parsed.pathname.replace(/\/+$/, "");
    const pathConfirmed =
      parsed.protocol === "https:" &&
      parsed.hostname.toLowerCase() === "hotel.qunar.com" &&
      !parsed.port &&
      !parsed.username &&
      !parsed.password &&
      observedPath === expectedPath;

    const visibleTextboxes = [
      ...root.querySelectorAll("input.textbox"),
    ].filter(visible);
    const destinationInputs = visibleTextboxes.filter((input) =>
      identity.selectedLabels.some(
        (label) => normalized(readVisibleValue(input)) === normalized(label),
      )
    );
    const conflictingDestinationInputs = visibleTextboxes.filter((input) => {
      const value = normalized(readVisibleValue(input));
      if (!value) {
        return false;
      }
      return !identity.selectedLabels.some(
        (label) => value === normalized(label),
      );
    });
    const destinationControlUnambiguous =
      destinationInputs.length === 1 &&
      conflictingDestinationInputs.length === 0;
    const dateInputs = [
      ...root.querySelectorAll("input.inputText.date"),
    ].filter(visible);
    const occupancySurfaces = [
      ...root.querySelectorAll(".adult-children"),
    ].filter(visible);
    if (
      !destinationControlUnambiguous ||
      dateInputs.length !== 2 ||
      occupancySurfaces.length !== 1
    ) {
      return rejected(
        "qunar_result_search_form_missing",
        {},
        {
          path_confirmed: pathConfirmed,
          search_form_visible: false,
          destination_control_unambiguous: destinationControlUnambiguous,
          conflicting_destination_control_absent:
            conflictingDestinationInputs.length === 0,
          date_controls_unambiguous: dateInputs.length === 2,
          occupancy_control_unambiguous: occupancySurfaces.length === 1,
        },
      );
    }
    const destinationInput =
      destinationInputs.length === 1 ? destinationInputs[0] : null;
    const startInput = dateInputs.length === 2 ? dateInputs[0] : null;
    const endInput = dateInputs.length === 2 ? dateInputs[1] : null;
    const occupancySurface =
      occupancySurfaces.length === 1 ? occupancySurfaces[0] : null;
    const destinationReadback = readVisibleValue(destinationInput).trim();
    const startDateReadback = readVisibleValue(startInput).trim();
    const endDateReadback = readVisibleValue(endInput).trim();
    const occupancyText = cleanCounterText(occupancySurface);
    const adultsReadback = explicitCount(occupancySurface, "adults");
    const childrenMatch = occupancyText.match(
      /(?:儿童|child(?:ren)?)\s*[:：]?\s*(\d+)|(\d+)\s*(?:位\s*)?(?:儿童|child(?:ren)?)/i,
    );
    const childrenReadback = childrenMatch
      ? Number.parseInt(childrenMatch[1] || childrenMatch[2], 10)
      : null;
    const singleRoomSurface = Boolean(
      occupancySurface &&
      /每间人数/.test(occupancyText) &&
      !/(?:房间|客房|rooms?)\s*[:：]?\s*[2-9]|[2-9]\s*(?:间|rooms?)/i.test(
        occupancyText,
      ),
    );
    const destinationConfirmed = Boolean(
      destinationInput &&
      identity.selectedLabels.some(
        (label) => normalized(destinationReadback) === normalized(label),
      ),
    );
    const gates = {
      path_confirmed: pathConfirmed,
      search_form_visible: true,
      destination_control_unambiguous: destinationControlUnambiguous,
      conflicting_destination_control_absent:
        conflictingDestinationInputs.length === 0,
      destination_confirmed: destinationConfirmed,
      date_controls_unambiguous: dateInputs.length === 2,
      start_date_confirmed: visibleFieldMatches(
        "start_date",
        startDateReadback,
        query && query.start_date,
      ),
      end_date_confirmed: visibleFieldMatches(
        "end_date",
        endDateReadback,
        query && query.end_date,
      ),
      occupancy_control_unambiguous: occupancySurfaces.length === 1,
      adults_confirmed:
        Number.isInteger(query && query.adults) &&
        adultsReadback === query.adults,
      children_confirmed: childrenReadback === 0,
      single_room_surface_confirmed:
        query && query.rooms === 1 && singleRoomSurface,
    };
    const readbackQuery = {
      destination: destinationReadback || null,
      start_date: startDateReadback || null,
      end_date: endDateReadback || null,
      adults: Number.isInteger(adultsReadback) ? adultsReadback : null,
      rooms: singleRoomSurface ? 1 : null,
    };
    const failedGate = Object.entries(gates).find(([, passed]) => !passed);
    if (failedGate) {
      return rejected(
        `qunar_result_${failedGate[0]}_failed`,
        readbackQuery,
        gates,
      );
    }
    return {
      confirmed: true,
      reason: null,
      confirmed_query: {
        destination: String(query.destination || ""),
        start_date: String(query.start_date || ""),
        end_date: String(query.end_date || ""),
        adults: Number(query.adults),
        rooms: 1,
      },
      readback_query: readbackQuery,
      gates,
      evidence: {
        provider_destination_id: identity.id,
        result_path: expectedPath,
        destination_identity_scope:
          "audited_exact_visible_label_plus_https_city_path_v1",
        destination_text: destinationReadback.slice(0, 80),
        start_date_text: startDateReadback.slice(0, 32),
        end_date_text: endDateReadback.slice(0, 32),
        occupancy_text: occupancyText.slice(0, 120),
        room_scope: "audited_qunar_single_room_search_surface",
      },
    };
  }

  function tongchengLodgingResultUrl(query, strategy) {
    const validDate = (value) => /^\d{4}-\d{2}-\d{2}$/.test(
      String(value || ""),
    );
    const adults = Number(query && query.adults);
    const rooms = Number(query && query.rooms);
    if (
      !strategy ||
      !/^110\d{6}$/.test(String(strategy.providerDestinationId || "")) ||
      !validDate(query && query.start_date) ||
      !validDate(query && query.end_date) ||
      String(query.end_date) <= String(query.start_date) ||
      !Number.isInteger(adults) || adults < 1 || adults > 9 ||
      rooms !== 1
    ) {
      return null;
    }
    const url = new URL("https://www.ly.com/hotel/hotellist");
    url.searchParams.set("city", strategy.providerDestinationId);
    url.searchParams.set("inDate", String(query.start_date));
    url.searchParams.set("outDate", String(query.end_date));
    url.searchParams.set("adultsNumber", String(adults));
    url.searchParams.set("roomNum", "1");
    url.searchParams.set("intl", "1");
    return url.href;
  }

  function auditedLodgingResultUrl(provider, query, strategy) {
    if (provider === "fliggy") {
      return fliggyLodgingResultUrl(query, strategy);
    }
    if (provider === "qunar") {
      return qunarLodgingResultUrl(query, strategy);
    }
    if (provider === "tongcheng") {
      return tongchengLodgingResultUrl(query, strategy);
    }
    return null;
  }

  function prefrozenLodgingDestinationFallback(
    provider,
    kind,
    query,
    strategy,
  ) {
    const auditedResultUrl =
      (provider === "fliggy" || provider === "qunar" || provider === "tongcheng") &&
      kind === "lodging" &&
      strategy
        ? auditedLodgingResultUrl(provider, query, strategy)
        : null;
    if (!auditedResultUrl) {
      return null;
    }
    return {
      provider_destination: strategy.providerDestination,
      provider_destination_id: strategy.providerDestinationId,
      evidence_scope:
        provider === "fliggy"
          ? "prefrozen_city_id_with_visible_dates_and_occupancy"
          : provider === "qunar"
            ? "prefrozen_city_slug_with_visible_dates_and_occupancy"
            : "prefrozen_overseas_city_id_with_audited_party_url",
    };
  }

  async function prepareSearch(provider, kind, query) {
    if (!INPUT_HINTS[kind] || !OCCUPANCY_PROFILES[provider]?.[kind]) {
      throw new Error("unsupported vertical");
    }
    const skipProviderModeSwitch =
      query &&
      query.options &&
      query.options.__tripchord_skip_provider_mode_switch === true;
    const modeConfirmed =
      kind !== "flight" ||
      await selectFlightMode(provider, { skipProviderModeSwitch });
    if (
      kind === "lodging" &&
      provider === "fliggy" &&
      !skipProviderModeSwitch
    ) {
      const international = queryVisible([
        "[data-testid='tab-国际'][role='tab']",
        "[aria-label='切换到国际酒店搜索'][role='tab']",
      ]) ||
        safeButton(["切换到国际酒店搜索", "国际酒店", "国际"]) ||
        safeModeControl(["切换到国际酒店搜索", "国际酒店", "国际"]);
      if (international) {
        if (international.getAttribute("aria-selected") !== "true") {
          international.click();
          await delay(220);
        }
      }
    }
    const lodgingSearchStrategy =
      fliggyLodgingSearchStrategy(provider, kind, query) ||
      qunarLodgingSearchStrategy(provider, kind, query) ||
      tongchengLodgingSearchStrategy(provider, kind, query);
    const values = {
      origin: query.origin,
      destination:
        lodgingSearchStrategy?.providerDestination ||
        query.destination,
      start_date: query.start_date,
      end_date: query.end_date,
      keyword: lodgingSearchStrategy?.keyword || null,
    };
    const filled = [];
    const confirmedQuery = {};
    const readbackQuery = {};
    const missing = [];
    const suggestionDiagnostics = [];
    const auditedDestinationFallbacks = [];
    if (!modeConfirmed) {
      missing.push("round_trip_mode_unconfirmed");
    }
    const fieldElements = [];
    for (const [field, hints] of Object.entries(INPUT_HINTS[kind])) {
      if (!values[field]) {
        continue;
      }
      const input = findInput(provider, kind, field, hints);
      if (!input) {
        missing.push(field);
        continue;
      }
      if (
        provider === "ctrip" &&
        kind === "lodging" &&
        (field === "start_date" || field === "end_date") &&
        input instanceof HTMLInputElement &&
        input.readOnly
      ) {
        fieldElements.push(input);
        const selection = await selectCtripHotelDate(input, values[field]);
        if (!selection.selected) {
          missing.push(`${field}_${selection.reason || "calendar_unconfirmed"}`);
          continue;
        }
        filled.push(field);
        confirmedQuery[field] = String(values[field]);
        readbackQuery[field] = selection.selected_text;
        continue;
      }
      if (
        provider === "fliggy" &&
        kind === "lodging" &&
        (field === "start_date" || field === "end_date")
      ) {
        fieldElements.push(input);
        setVisibleValue(input, String(values[field]));
        await delay(160);
        const picker = queryVisible([
          "[data-testid='international-date-picker']",
        ]);
        const attribute = field === "start_date"
          ? "data-agent-checkin"
          : "data-agent-checkout";
        const selectedDate = picker && picker.getAttribute(attribute);
        if (selectedDate !== String(values[field])) {
          missing.push(`${field}_date_state_unconfirmed`);
          continue;
        }
        filled.push(field);
        confirmedQuery[field] = String(values[field]);
        readbackQuery[field] = selectedDate;
        continue;
      }
      const code = cityCode(field, query);
      const expectedPlaceKey =
        kind === "lodging" &&
        field === "destination" &&
        query &&
        query.options
          ? query.options.expected_lodging_place_key || null
          : null;
      const prefrozenDestinationFallback =
        field === "destination"
          ? prefrozenLodgingDestinationFallback(
              provider,
              kind,
              query,
              lodgingSearchStrategy,
            )
          : null;
      const alreadySelected =
        (field === "origin" || field === "destination") &&
        selectedCityIsExplicit(
          provider,
          kind,
          input,
          values[field],
          code,
          null,
          expectedPlaceKey,
        );
      if (!alreadySelected && !prefrozenDestinationFallback) {
        setVisibleValue(input, String(values[field]), {
          blur: field !== "origin" && field !== "destination",
        });
      }
      filled.push(field);
      fieldElements.push(input);
      let suggestion = null;
      let auditedDestinationFallback =
        prefrozenDestinationFallback;
      if (auditedDestinationFallback) {
        auditedDestinationFallbacks.push(auditedDestinationFallback);
      }
      if (
        (field === "origin" || field === "destination") &&
        !alreadySelected &&
        !auditedDestinationFallback
      ) {
        suggestion = await selectVisibleSuggestion(
          provider,
          kind,
          values[field],
          code,
          input,
          expectedPlaceKey,
          field,
        );
      }
      if (
        (field === "origin" || field === "destination") &&
        !alreadySelected
      ) {
        suggestionDiagnostics.push({
          field,
          selected: Boolean(suggestion?.selected),
          menu_observed: Boolean(suggestion?.menu_observed),
          selected_text: suggestion?.selected_text || null,
          selected_id: suggestion?.selected_id || null,
          identity_evidence: suggestion?.identity_evidence || null,
          readback_value: suggestion?.readback_value || null,
          readback_identity: suggestion?.readback_identity || null,
          readback_surfaces: suggestion?.readback_surfaces || [],
          activation_mode: suggestion?.activation_mode || null,
          audited_destination_fallback:
            auditedDestinationFallback,
          candidates: suggestion?.selected
            ? []
            : suggestionAttemptDiagnostics(
                provider,
                kind,
                values[field],
                expectedPlaceKey,
              ),
        });
      }
      const readbackInput =
        field === "origin" || field === "destination"
          ? currentVisibleInput(provider, kind, field, input)
          : input;
      if (readbackInput !== input) {
        const index = fieldElements.lastIndexOf(input);
        if (index >= 0) {
          fieldElements[index] = readbackInput;
        }
      }
      const visibleValue = readVisibleValue(readbackInput).trim();
      const destinationConfirmedByFrozenId =
        field === "destination" &&
        Boolean(auditedDestinationFallback);
      const cityConfirmed =
        field !== "origin" && field !== "destination"
          ? true
          : destinationConfirmedByFrozenId ||
            selectedCityIsExplicit(
                provider,
                kind,
                input,
                values[field],
                code,
                suggestion,
                expectedPlaceKey,
              );
      const readbackConfirmed =
        destinationConfirmedByFrozenId ||
        (
          (field === "origin" || field === "destination") &&
          suggestion &&
          suggestion.selected
            ? true
            : visibleFieldMatches(field, visibleValue, values[field])
        );
      if (
        readbackConfirmed &&
        cityConfirmed
      ) {
        confirmedQuery[field] =
          field === "destination" && lodgingSearchStrategy
            ? lodgingSearchStrategy.plannedDestination
            : String(values[field]);
        readbackQuery[field] = destinationConfirmedByFrozenId
          ? (
              "audited-city-id:" +
              auditedDestinationFallback.provider_destination_id
            )
          : visibleValue;
      } else {
        missing.push(
          cityConfirmed
            ? `${field}_readback`
            : `${field}_suggestion_unconfirmed`,
        );
      }
    }
    const root = commonSearchRoot(fieldElements);
    const required = kind === "flight"
      ? ["origin", "destination", "start_date", "end_date"]
      : [
          "destination",
          "start_date",
          "end_date",
          ...(lodgingSearchStrategy?.keyword ? ["keyword"] : []),
        ];
    missing.push(
      ...required.filter(
        (field) =>
          values[field] &&
          (!filled.includes(field) || !(field in confirmedQuery)),
      ),
    );
    const countFields = kind === "flight" ? ["adults"] : ["adults", "rooms"];
    for (const field of countFields) {
      const result = await setVisibleCount(
        provider,
        kind,
        field,
        Number(query[field]),
        root,
      );
      if (!result.ok) {
        missing.push(result.reason || `${field}_unconfirmed`);
        continue;
      }
      confirmedQuery[field] = Number(query[field]);
      readbackQuery[field] = result.readback;
    }
    const uniqueMissing = [...new Set(missing)];
    const button = searchButton(provider, kind, root);
    if (!button) {
      uniqueMissing.push("search_button");
    }
    if (uniqueMissing.length) {
      preparedSearchContext = null;
      return {
        prepared: false,
        triggered: false,
        missing: uniqueMissing,
        controls: controlDiagnostics(),
        suggestions: suggestionDiagnostics,
        message: "没有设置并回读全部可见搜索字段",
      };
    }
    const auditedNavigationUrl = lodgingSearchStrategy
      ? auditedLodgingResultUrl(
          provider,
          query,
          lodgingSearchStrategy,
        )
      : null;
    if (lodgingSearchStrategy && !auditedNavigationUrl) {
      preparedSearchContext = null;
      return {
        prepared: false,
        triggered: false,
        missing: ["audited_lodging_result_url"],
        controls: controlDiagnostics(),
        suggestions: suggestionDiagnostics,
        message: "已回读搜索字段，但无法形成安全的只读酒店结果地址",
      };
    }
    preparedSearchContext = {
      auditedNavigationUrl,
      button,
      kind,
      provider,
      root,
    };
    return {
      prepared: true,
      triggered: false,
      filled: [...required, ...countFields],
      confirmed_query: confirmedQuery,
      readback_query: readbackQuery,
      confirmation_scope: "visible_form_fields_readback",
      destination_confirmation_scope:
        auditedDestinationFallbacks.length
          ? auditedDestinationFallbacks[0].evidence_scope
          : "visible_destination_selection_readback",
      ...(lodgingSearchStrategy
        ? {
            lodging_search_strategy: {
              provider_destination:
                lodgingSearchStrategy.providerDestination,
              provider_destination_id:
                lodgingSearchStrategy.providerDestinationId,
              keyword: lodgingSearchStrategy.keyword,
              evidence_scope: lodgingSearchStrategy.evidenceScope,
            },
          }
        : {}),
      controls: controlDiagnostics(),
    };
  }

  function triggerSearch(provider, kind) {
    const context = preparedSearchContext;
    const button =
      context &&
      context.provider === provider &&
      context.kind === kind &&
      context.button &&
      context.button.isConnected &&
      visible(context.button)
        ? context.button
        : null;
    if (!button) {
      return {
        triggered: false,
        missing: ["search_button"],
        controls: controlDiagnostics(),
        message: "没有找到安全的可见搜索按钮",
      };
    }
    preparedSearchContext = null;
    if (context.auditedNavigationUrl) {
      return {
        triggered: true,
        confirmation_scope: "audited_visible_form_direct_navigation",
        audited_navigation_url: context.auditedNavigationUrl,
        trigger_mode: "audited_read_only_search_url",
      };
    }
    button.click();
    return {
      triggered: true,
      confirmation_scope: "visible_search_triggered",
    };
  }

  async function safeSelectOutbound(provider, query, selectionId) {
    if (!["ctrip", "fliggy", "tongcheng"].includes(provider)) {
      return {
        selected: false,
        code: "provider_has_no_safe_outbound_stage",
      };
    }
    if (
      typeof selectionId !== "string" ||
      !/^[a-f0-9]{64}$/.test(selectionId)
    ) {
      return {
        selected: false,
        code: "invalid_outbound_selection_id",
      };
    }
    return globalThis.TripChordQuoteParser.safeSelectOutbound(
      provider,
      document,
      query || {},
      selectionId,
    );
  }

  async function safeSelectReturn(provider, query, driver, selectionId) {
    if (provider !== "tongcheng") {
      return {
        selected: false,
        code: "provider_has_no_safe_return_stage",
      };
    }
    if (
      typeof selectionId !== "string" ||
      !/^[a-f0-9]{64}$/.test(selectionId)
    ) {
      return {
        selected: false,
        code: "invalid_return_selection_id",
      };
    }
    return globalThis.TripChordQuoteParser.safeSelectReturn(
      provider,
      document,
      query || {},
      driver || {},
      selectionId,
    );
  }

  function auditedCtripFlightRecoveryNotice(root = document) {
    const controls = [
      ...root.querySelectorAll(
        "button, [role='button'], a, [class*='btn'], [class*='button']",
      ),
    ].filter(
      (node) =>
        isElementNode(node) &&
        visible(node) &&
        cleanCounterText(node) === "我知道了",
    );
    const matches = [];
    for (const control of controls) {
      let container = control.parentElement;
      let depth = 0;
      while (container && depth < 7) {
        const text = cleanCounterText(container);
        if (
          text.length <= 600 &&
          text.includes("温馨提示") &&
          text.includes("您终于回来了") &&
          text.includes("航班可能有变") &&
          text.includes("为您重新查询") &&
          !/订票|预订|下单|支付|优惠券|订单|确认购买/.test(text)
        ) {
          matches.push({ control, container });
          break;
        }
        container = container.parentElement;
        depth += 1;
      }
    }
    const unique = matches.filter(
      (item, index, all) =>
        all.findIndex((other) => other.control === item.control) ===
        index,
    );
    return unique.length === 1 ? unique[0] : null;
  }

  async function normalizeCtripFlightExtractionSurface(
    provider,
    kind,
  ) {
    if (provider !== "ctrip" || kind !== "flight") {
      return {
        normalized: false,
        scrolled_to_top: false,
        recovery_notice_dismissed: false,
      };
    }
    const scrolledToTop =
      Number(window.scrollX || 0) !== 0 ||
      Number(window.scrollY || 0) !== 0;
    if (typeof window.scrollTo === "function") {
      window.scrollTo(0, 0);
    }
    const notice = auditedCtripFlightRecoveryNotice(document);
    if (notice) {
      notice.control.click();
    }
    if (scrolledToTop || notice) {
      await new Promise((resolve) => setTimeout(resolve, notice ? 220 : 80));
    }
    return {
      normalized: scrolledToTop || Boolean(notice),
      scrolled_to_top: scrolledToTop,
      recovery_notice_dismissed: Boolean(notice),
      evidence: notice
        ? "audited_non_transactional_flight_requery_notice"
        : null,
    };
  }

  function tongchengLodgingDetailCandidates(provider, kind, root = document) {
    if (provider !== "tongcheng" || kind !== "lodging") {
      return {
        indices: [],
        samples: [],
        li_count: 0,
        runtime_version: globalThis.TripChordContentRuntimeVersion || "",
      };
    }
    const listItems = [...new Set(root.querySelectorAll(
      "li,[data-hotelid],[data-hotel-id]," +
      "[class*='hotel-item' i],[class*='hotel_card' i],[class*='hotel-card' i]",
    ))];
    const mobileTongchengList =
      (
        window.location.hostname.toLowerCase() === "m.elong.com" &&
        window.location.pathname.toLowerCase().replace(/\/+$/, "") ===
          "/ihotel/hotellist"
      ) || (
        window.location.hostname.toLowerCase() === "m.ly.com" &&
        window.location.pathname.toLowerCase().replace(/\/+$/, "") ===
          "/hotel/hotellist"
      );
    const selected = [];
    const scanSamples = [];
    listItems.forEach((node, index) => {
      const text = String(node.textContent || node.innerText || "")
        .replace(/\s+/g, " ")
        .trim();
      if (text.length >= 12 && scanSamples.length < 8) {
        scanSamples.push({ index, sample: text.slice(0, 300) });
      }
      if (
        text &&
        /(?:¥|￥)[^起]{0,32}起/.test(text) &&
        /(?:条点评|很好|不错|舒适|高档)/.test(text) &&
        (
          /查看详情/.test(text) ||
          (
            mobileTongchengList &&
            Boolean(node.querySelector("a[href*='hoteldetail' i]"))
          )
        ) &&
        !/(?:立即预订|下单|支付|购买|优惠券|订单)/.test(text)
      ) {
        selected.push({ index, sample: text.slice(0, 300) });
      }
    });
    return {
      indices: selected.slice(0, 3).map((item) => item.index),
      samples: selected.slice(0, 3).map((item) => item.sample),
      scan_samples: scanSamples,
      li_count: listItems.length,
      document_title: String(root.title || "").slice(0, 200),
      document_sample: String(
        root.body && (root.body.innerText || root.body.textContent) || "",
      ).replace(/\s+/g, " ").trim().slice(0, 1200),
      element_count: root.querySelectorAll("*").length,
      runtime_version: globalThis.TripChordContentRuntimeVersion || "",
    };
  }

  async function waitForTongchengFlightCards(provider, kind, query = {}) {
    if (
      provider !== "tongcheng" ||
      kind !== "flight" ||
      !/(?:^|\.)ly\.com$/i.test(location.hostname) ||
      !/\/eliflight\/book1\.html$/i.test(location.pathname)
    ) {
      return { waited_ms: 0, ready: true, reason: "not_tongcheng_flight" };
    }
    const startedAt = Date.now();
    const deadline = startedAt + 10000;
    let requestedDateTriggered = false;
    while (Date.now() < deadline) {
      if (document.querySelector(".flight-item .flight-btn")) {
        return {
          waited_ms: Date.now() - startedAt,
          ready: true,
          reason: "flight_cards_ready",
        };
      }
      if (!requestedDateTriggered) {
        const startToken = String(query.start_date || "").slice(5);
        const endToken = String(query.end_date || "").slice(5);
        const checkedDate = document.querySelector(
          ".if-calendar_tab-item.checked",
        );
        const checkedText = String(
          checkedDate && (checkedDate.innerText || checkedDate.textContent) || "",
        ).replace(/\s+/g, " ").trim();
        const exactControl = checkedDate && [...checkedDate.querySelectorAll("a, button")]
          .find((node) =>
            String(node.innerText || node.textContent || "")
              .replace(/\s+/g, " ")
              .trim() === "点击查看",
          );
        if (
          startToken &&
          endToken &&
          checkedText.includes(startToken) &&
          checkedText.includes(endToken) &&
          exactControl &&
          isVisible(exactControl)
        ) {
          exactControl.click();
          requestedDateTriggered = true;
        }
      }
      const visibleText = String(
        document.body && (document.body.innerText || document.body.textContent) || "",
      ).replace(/\s+/g, " ").trim().slice(0, 4000);
      if (/(?:滑块|验证码|安全验证|登录后查看)/.test(visibleText)) {
        return {
          waited_ms: Date.now() - startedAt,
          ready: false,
          reason: "human_action_required",
        };
      }
      await delay(400);
    }
    return {
      waited_ms: Date.now() - startedAt,
      ready: false,
      reason: "bounded_wait_exhausted",
    };
  }

  globalThis.TripChordContentRuntimeVersion = "2026-08-05.16";
  globalThis.TripChordVisibleSearchDriver = {
    ctripCalendarMonthOrdinal,
    ctripCalendarNavigationDirection,
    fliggyLodgingResultUrl,
    fliggyLodgingSearchStrategy,
    prefrozenLodgingDestinationFallback,
    qunarLodgingResultUrl,
    qunarLodgingResultQueryReadback,
    qunarLodgingSearchStrategy,
    prepareSearch,
    triggerSearch,
    safeSelectOutbound,
    setVisibleCount,
    selectVisibleSuggestion,
    suggestionIdentity,
    suggestionIdentityMatches,
    suggestionPollTimeoutMs,
    auditedInputIdentity,
    auditedCtripFlightRecoveryNotice,
    controlDiagnostics,
    normalizeCtripFlightExtractionSurface,
    tongchengLodgingDetailCandidates,
    waitForTongchengFlightCards,
    OCCUPANCY_PROFILES,
  };
  if (!globalThis.chrome?.runtime?.onMessage) {
    return;
  }

  chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
    if (!message || !String(message.type || "").startsWith("tripchord:")) {
      return false;
    }
    (async () => {
      if (message.type === "tripchord:prepare-search") {
        return prepareSearch(message.provider, message.kind, message.query);
      }
      if (message.type === "tripchord:trigger-search") {
        return triggerSearch(message.provider, message.kind);
      }
      if (message.type === "tripchord:read-result-query") {
        return qunarLodgingResultQueryReadback(
          message.provider,
          message.kind,
          message.query || {},
        );
      }
      if (message.type === "tripchord:extract") {
        await waitForTongchengFlightCards(
          message.provider,
          message.kind,
          message.query || {},
        );
        await normalizeCtripFlightExtractionSurface(
          message.provider,
          message.kind,
        );
        return globalThis.TripChordQuoteParser.extractPage(
          message.provider,
          message.kind,
          document,
          location.href,
          new Date(),
          message.query || {},
          message.driver || null,
        );
      }
      if (message.type === "tripchord:safe-select-outbound") {
        return safeSelectOutbound(
          message.provider,
          message.query || {},
          message.selection_id,
        );
      }
      if (message.type === "tripchord:safe-select-return") {
        return safeSelectReturn(
          message.provider,
          message.query || {},
          message.driver || {},
          message.selection_id,
        );
      }
      if (message.type === "tripchord:extract-transfer-detail") {
        return globalThis.TripChordQuoteParser.extractTransferDetail(
          message.provider,
          document,
          location.href,
          message.query || {},
        );
      }
      if (message.type === "tripchord:tongcheng-detail-candidates") {
        return tongchengLodgingDetailCandidates(
          message.provider,
          message.kind,
          document,
        );
      }
      throw new Error("unsupported TripChord content command");
    })()
      .then((result) => sendResponse({ ok: true, result }))
      .catch((error) =>
        sendResponse({ ok: false, error: String(error && error.message || error) }),
      );
    return true;
  });

})();
