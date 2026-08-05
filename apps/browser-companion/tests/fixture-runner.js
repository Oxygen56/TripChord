(async () => {
  const results = document.querySelector("#results");
  const summary = document.querySelector("#summary");
  const parser = globalThis.TripChordQuoteParser;
  const fixtures = globalThis.TripChordFixtures;
  const visibleDriver = globalThis.TripChordVisibleSearchDriver;
  const contracts = [
    ["ctrip", "flight", 4692, "per_person", true, 0],
    ["ctrip", "lodging", 396, "per_night", true, "airport_island"],
    ["fliggy", "flight", 4858, "per_person", true, null],
    ["fliggy", "lodging", 673, "per_night", true, "destination_island"],
    ["qunar", "flight", 4880, "per_person", true, null],
    ["qunar", "lodging", 15519, "total_stay", false, "destination_island"],
  ];
  let passed = 0;
  let failed = 0;

  function record(name, ok, detail = "") {
    const item = document.createElement("li");
    item.className = ok ? "pass" : "fail";
    item.textContent = `${ok ? "PASS" : "FAIL"} — ${name}${detail ? `: ${detail}` : ""}`;
    results.append(item);
    ok ? passed++ : failed++;
  }

  function searchForm(kind, { includeRooms = true } = {}) {
    const form = document.createElement("form");
    if (kind === "flight") {
      const roundTrip = document.createElement("input");
      roundTrip.id = "searchTypeRnd";
      roundTrip.type = "radio";
      roundTrip.value = "RoundTripFlight";
      const roundTripLabel = document.createElement("label");
      roundTripLabel.htmlFor = roundTrip.id;
      roundTripLabel.textContent = "往返";
      form.append(roundTrip, roundTripLabel);
    }
    const fields = kind === "flight"
      ? [
          ["出发城市", "杭州"],
          ["目的地", "马累"],
          ["出发日期", "2026-08-23"],
          ["返程日期", "2026-08-30"],
          ["成人", "1", "number"],
        ]
      : [
          ["目的地", "马累"],
          ["入住日期", "2026-08-23"],
          ["离店日期", "2026-08-30"],
          ["成人", "1", "number"],
          ...(includeRooms ? [["房间", "2", "number"]] : []),
        ];
    for (const [label, value, type = "text"] of fields) {
      const input = document.createElement("input");
      input.setAttribute("aria-label", label);
      input.type = type;
      input.value = value;
      form.append(input);
    }
    if (kind === "lodging") {
      const datePicker = document.createElement("div");
      datePicker.setAttribute("data-testid", "international-date-picker");
      datePicker.setAttribute("data-agent-checkin", "2026-08-23");
      datePicker.setAttribute("data-agent-checkout", "2026-08-30");
      datePicker.style.display = "inline-block";
      datePicker.textContent = "2026-08-23 至 2026-08-30";
      form.append(datePicker);
    }
    const suggestions = document.createElement("div");
    suggestions.setAttribute("role", "listbox");
    for (const label of ["杭州(HGH)", "马累(MLE)"]) {
      const option = document.createElement("button");
      option.type = "button";
      option.setAttribute("role", "option");
      option.textContent = label;
      suggestions.append(option);
    }
    form.append(suggestions);
    const button = document.createElement("button");
    button.type = "button";
    button.textContent = kind === "flight" ? "搜索机票" : "搜索酒店";
    form.append(button);
    document.body.append(form);
    return { form, button };
  }

  for (const [provider, kind, amount, basis, taxes, derivedValue] of contracts) {
    const root = new DOMParser().parseFromString(
      fixtures[`${provider}-${kind}`],
      "text/html",
    );
    const stagedOutbound = provider === "ctrip"
      ? {
          carrier_text: "香港航空",
          outbound_departure_at: "2026-08-23T08:30:00+08:00",
          outbound_arrival_at: "2026-08-23T18:35:00+05:00",
          selection_id: "fixture-ctrip-selection",
          selection_evidence: "香港航空 08:30 杭州 18:35 马累",
        }
      : provider === "fliggy"
        ? {
            carrier_text: "亚洲航空",
            outbound_departure_at: "2026-08-23T07:10:00+08:00",
            outbound_arrival_at: "2026-08-23T17:20:00+05:00",
            selection_id: "fixture-fliggy-selection",
            selection_evidence: "亚洲航空 07:10 杭州 17:20 马累",
          }
        : null;
    const output = await parser.extractPage(
      provider,
      kind,
      root,
      `https://fixture.${provider}.com/results`,
      new Date("2026-07-30T12:00:00Z"),
      {
        origin: kind === "flight" ? "杭州" : null,
        destination: "马累",
        start_date: "2026-08-23",
        end_date: "2026-08-30",
        adults: 2,
        rooms: 1,
        currency: "CNY",
        origin_code: kind === "flight" ? "HGH" : null,
        destination_code: "MLE",
        search_url: null,
        options: {
          segment: kind === "lodging" ? "full" : "ignored",
          expected_package_area:
            kind === "lodging" ? derivedValue : "ignored",
          ignored_option: "must-not-cross-the-parser-boundary",
        },
      },
      {
        mode: "fixture",
        triggered: true,
        confirmed_query: {
          ...(kind === "flight" ? { origin: "杭州" } : {}),
          destination: "马累",
          start_date: "2026-08-23",
          end_date: "2026-08-30",
          adults: 2,
          ...(kind === "lodging" ? { rooms: 1 } : {}),
        },
        confirmation_scope: "fixture",
        party_availability_confirmed: provider !== "fliggy",
        action_trace: [
          {
            action: "search",
            provider,
            evidence: "fixture_exact_round_trip_search",
          },
          ...(
            stagedOutbound
              ? [
                  {
                    action: "select_outbound",
                    provider,
                    evidence: stagedOutbound.selection_evidence,
                  },
                ]
              : []
          ),
        ],
        ...(stagedOutbound ? { selected_outbound: stagedOutbound } : {}),
      },
    );
    const quote = output.quotes && output.quotes[0];
    const detailContract = kind === "flight"
      ? quote &&
        quote.details.query.destination === "马累" &&
        quote.details.adults === 2 &&
        quote.details.carrier_text &&
        quote.details.connection_text &&
        quote.details.baggage_text &&
        quote.details.outbound_departure_at &&
        quote.details.outbound_arrival_at &&
        quote.details.return_departure_at &&
        quote.details.return_arrival_at &&
        quote.details.combination_status === "round_trip_complete" &&
        quote.details.journey_price_scope === "round_trip" &&
        quote.details.price_finality === "final_for_combination" &&
        quote.details.availability === "available" &&
        quote.details.availability_evidence &&
        quote.details.outbound_route_evidence.matches_expected === true &&
        quote.details.return_route_evidence.matches_expected === true &&
        quote.details.outbound_route_evidence.source_scope !== "query" &&
        quote.details.return_route_evidence.source_scope !== "query" &&
        quote.details.action_trace.every((item) =>
          ["search", "filter", "select_outbound", "reselect_outbound"]
            .includes(item.action)
        ) &&
        quote.details.checked_baggage_per_adult_kg === derivedValue
      : quote &&
        quote.details.query.destination === "马累" &&
        quote.details.check_in === "2026-08-23" &&
        quote.details.check_out === "2026-08-30" &&
        quote.details.adults === 2 &&
        quote.details.rooms === 1 &&
        quote.details.room_text &&
        quote.details.area_text &&
        quote.details.area === derivedValue &&
        quote.details.area_source === "visible_label" &&
        quote.details.area_matches_expected === true &&
        quote.details.breakfast_text &&
        quote.details.breakfast_included === true &&
        quote.details.cancellation_text &&
        quote.details.transfer_text &&
        (
          !["ctrip", "fliggy"].includes(provider) ||
          (
            quote.details.transfers.length === 2 &&
            quote.details.transfers.every((transfer) =>
              transfer.price_scope === "round_trip" &&
              transfer.schedule_mode === "service_window" &&
              transfer.taxes_included === true &&
              transfer.amount > 0 &&
              transfer.duration_minutes > 0 &&
              transfer.evidence_sha256 &&
              transfer.detail_url
            )
          )
        );
    record(
      `${provider}/${kind}`,
      output.state === "succeeded" &&
        quote.amount === amount &&
        quote.price_basis === basis &&
        quote.taxes_included === taxes &&
        /^[a-f0-9]{64}$/.test(quote.evidence_sha256) &&
        detailContract &&
        (provider !== "ctrip" ||
          kind !== "flight" ||
          quote.details.outbound_departure_at ===
            "2026-08-23T08:30:00+08:00") &&
        quote.details.driver.triggered === true &&
        !("ignored_option" in quote.details.query.options),
      JSON.stringify(output),
    );
  }

  {
    const semantic = await parser.extractPage(
      "ctrip",
      "flight",
      new DOMParser().parseFromString(
        fixtures["ctrip-flight-semantic"],
        "text/html",
      ),
      "https://flights.ctrip.com/results",
      new Date("2026-07-30T12:00:00Z"),
      {
        origin: "杭州",
        destination: "马累",
        start_date: "2026-08-23",
        end_date: "2026-08-30",
        adults: 2,
        currency: "CNY",
        origin_code: "HGH",
        destination_code: "MLE",
        options: {},
      },
      {
        mode: "fixture",
        triggered: true,
        confirmed_query: {
          origin: "杭州",
          destination: "马累",
          start_date: "2026-08-23",
          end_date: "2026-08-30",
          adults: 2,
        },
        confirmation_scope: "fixture",
      },
    );
    record(
      "ctrip outbound preview cannot become a BrowserQuote",
      semantic.state === "outbound_preview" &&
        semantic.combination_status === "outbound_preview" &&
        semantic.quotes.length === 0 &&
        semantic.selection.label === "选为去程" &&
        semantic.selection.carrier_text === "香港航空",
      JSON.stringify(semantic),
    );
  }

  {
    const query = {
      origin: "杭州",
      destination: "马累",
      start_date: "2026-08-12",
      end_date: "2026-08-18",
      adults: 2,
      currency: "CNY",
      origin_code: "HGH",
      destination_code: "MLE",
      options: {},
    };
    const outboundRoot = new DOMParser().parseFromString(
      fixtures["ctrip-flight-live-outbound-starting-semantic"],
      "text/html",
    );
    const outbound = await parser.extractPage(
      "ctrip",
      "flight",
      outboundRoot,
      "https://flights.ctrip.com/online/list/round-hgh-mle",
      new Date("2026-07-31T00:00:00Z"),
      query,
      {
        mode: "fixture",
        triggered: true,
        confirmed_query: { ...query },
        confirmation_scope: "fixture",
        action_trace: [
          {
            action: "search",
            provider: "ctrip",
            evidence: "fixture_exact_round_trip_search",
          },
        ],
      },
    );
    record(
      "Ctrip renamed live ancestor yields only an outbound preview when fare is 起",
      outbound.state === "outbound_preview" &&
        outbound.quotes.length === 0 &&
        outbound.selection.label === "选择" &&
        outbound.selection.carrier_text === "泰国亚航" &&
        outbound.selection.outbound_departure_at ===
          "2026-08-12T18:10:00+08:00" &&
        outbound.selection.outbound_arrival_at ===
          "2026-08-13T11:35:00+05:00" &&
        outbound.selection.outbound_route_evidence.matches_expected === true,
      JSON.stringify(outbound),
    );
    const outboundButtons = [...outboundRoot.querySelectorAll("button")];
    const clickedOutboundControls = [];
    outboundButtons[0].addEventListener(
      "click",
      () => clickedOutboundControls.push("header"),
    );
    outboundButtons[1].addEventListener(
      "click",
      () => clickedOutboundControls.push("matching-card"),
    );
    const safelySelected = await parser.safeSelectOutbound(
      "ctrip",
      outboundRoot,
      query,
      outbound.selection.selection_id,
    );
    record(
      "Ctrip generic 选择 clicks only the route/time/carrier/price-audited outbound card",
      safelySelected.selected === true &&
        safelySelected.selection.label === "选择" &&
        JSON.stringify(clickedOutboundControls) ===
          JSON.stringify(["matching-card"]),
      JSON.stringify({ safelySelected, clickedOutboundControls }),
    );

    const comparisonOnly = await parser.extractPage(
      "ctrip",
      "flight",
      new DOMParser().parseFromString(
        fixtures["ctrip-flight-live-outbound-comparison-only"],
        "text/html",
      ),
      "https://flights.ctrip.com/online/list/round-hgh-mle",
      new Date("2026-07-31T00:00:00Z"),
      query,
      {
        mode: "search_url",
        triggered: true,
        confirmed_query: { ...query },
        readback_query: {
          origin_code: query.origin_code,
          destination_code: query.destination_code,
          start_date: query.start_date,
          end_date: query.end_date,
          adults: query.adults,
        },
        confirmation_scope: "trusted_exact_search_url",
        action_trace: [
          {
            action: "search",
            provider: "ctrip",
            evidence: "fixture_exact_round_trip_search_url",
          },
        ],
      },
    );
    const comparisonReceipt =
      comparisonOnly.failure &&
      comparisonOnly.failure.details &&
      comparisonOnly.failure.details.flight_search_receipt;
    record(
      "Ctrip exact search can sign a starting-price comparison receipt without clicking an unaudited card",
      comparisonOnly.state === "failed" &&
        comparisonOnly.failure.code === "extraction_error" &&
        comparisonOnly.quotes.length === 0 &&
        comparisonReceipt.state === "comparison_price_only" &&
        comparisonReceipt.scanned_count === 1 &&
        comparisonReceipt.candidate_summaries[0].amount === 5159 &&
        comparisonReceipt.candidate_summaries[0].price_basis ===
          "per_person" &&
        comparisonReceipt.candidate_summaries[0].price_classification ===
          "starting_or_estimated" &&
        comparisonReceipt.candidate_summaries[0].route_evidence.includes(
          "杭州→马累(匹配)",
        ) &&
        comparisonReceipt.candidate_summaries[0].schedule_evidence.includes(
          "2026-08-12T18:10:00+08:00",
        ) &&
        /^[a-f0-9]{64}$/.test(
          comparisonOnly.failure.details.flight_search_receipt_sha256,
      ),
      JSON.stringify(comparisonOnly),
    );

    const styledRoot = new DOMParser().parseFromString(
      fixtures["ctrip-flight-live-outbound-styled-control-safe"],
      "text/html",
    );
    const styledControl = await parser.extractPage(
      "ctrip",
      "flight",
      styledRoot,
      "https://flights.ctrip.com/online/list/round-hgh-mle",
      new Date("2026-07-31T00:00:00Z"),
      query,
      {
        mode: "search_url",
        triggered: true,
        confirmed_query: { ...query },
        readback_query: {
          origin_code: query.origin_code,
          destination_code: query.destination_code,
          start_date: query.start_date,
          end_date: query.end_date,
          adults: query.adults,
        },
        confirmation_scope: "trusted_exact_search_url",
        action_trace: [
          {
            action: "search",
            provider: "ctrip",
            evidence: "fixture_exact_round_trip_search_url",
          },
        ],
      },
    );
    const styledClicked = [];
    styledRoot
      .querySelector(".flight-operate .btn.btn-book")
      .addEventListener("click", () => styledClicked.push("audited"));
    const styledSelected =
      styledControl.selection &&
      await parser.safeSelectOutbound(
        "ctrip",
        styledRoot,
        query,
        styledControl.selection.selection_id,
      );
    record(
      "Ctrip exact styled 选为去程 is clickable only inside an audited flight-operate card",
      styledControl.state === "outbound_preview" &&
        styledControl.quotes.length === 0 &&
        styledControl.flight_search_receipt.state ===
          "comparison_price_only" &&
        styledControl.flight_search_receipt.scanned_count === 1 &&
        styledControl.flight_search_receipt
          .candidate_summaries[0].price_classification ===
            "starting_or_estimated" &&
        /^[a-f0-9]{64}$/.test(
          styledControl.flight_search_receipt_sha256,
        ) &&
        styledControl.selection.label === "选为去程" &&
        styledControl.selection.carrier_text === "新加坡航空" &&
        styledControl.selection.outbound_departure_at ===
          "2026-08-12T20:55:00+08:00" &&
        styledControl.selection.outbound_arrival_at ===
          "2026-08-13T11:50:00+05:00" &&
        styledSelected.selected === true &&
        styledSelected.selection.selection_id ===
          styledControl.selection.selection_id &&
        JSON.stringify(styledClicked) === JSON.stringify(["audited"]),
      JSON.stringify({ styledControl, styledSelected, styledClicked }),
    );

    const repricedRoot = new DOMParser().parseFromString(
      fixtures["ctrip-flight-live-outbound-styled-control-safe"],
      "text/html",
    );
    const repricedPreview = await parser.extractPage(
      "ctrip",
      "flight",
      repricedRoot,
      "https://flights.ctrip.com/online/list/round-hgh-mle",
      new Date("2026-07-31T00:00:00Z"),
      query,
      {
        mode: "search_url",
        triggered: true,
        confirmed_query: { ...query },
        confirmation_scope: "trusted_exact_search_url",
        action_trace: [
          {
            action: "search",
            provider: "ctrip",
            evidence: "fixture_exact_round_trip_search_url",
          },
        ],
      },
    );
    const repricedClicks = [];
    repricedRoot
      .querySelector(".flight-operate .btn.btn-book")
      .addEventListener("click", () => repricedClicks.push("audited"));
    repricedRoot.querySelector(".price-main").textContent =
      "¥5261起往返含税价";
    repricedRoot.querySelector(".carrier-name").textContent =
      "新加坡航空 SQ831 空客350(大) SQ438 波音737(中)";
    for (const routeLabel of repricedRoot.querySelectorAll(
      ".flight-box > div:not(.flight-operate) span",
    )) {
      routeLabel.textContent = routeLabel.textContent
        .replace("杭州", "HGH")
        .replace("马累", "MLE");
    }
    const repricedSelection = await parser.safeSelectOutbound(
      "ctrip",
      repricedRoot,
      query,
      repricedPreview.selection.selection_id,
    );
    record(
      "Ctrip stable outbound identity survives price, carrier-detail, and route-label normalization",
      repricedSelection.selected === true &&
        repricedSelection.selection.selection_id ===
          repricedPreview.selection.selection_id &&
        repricedSelection.selection.carrier_text.includes("SQ831") &&
        repricedSelection.selection.selection_evidence.includes("5261") &&
        JSON.stringify(repricedClicks) === JSON.stringify(["audited"]),
      JSON.stringify({ repricedPreview, repricedSelection, repricedClicks }),
    );

    const ambiguousIdentityRoot = new DOMParser().parseFromString(
      fixtures["ctrip-flight-live-outbound-styled-control-safe"],
      "text/html",
    );
    const duplicatedCard =
      ambiguousIdentityRoot.querySelector(".flight-box").cloneNode(true);
    ambiguousIdentityRoot.querySelector("section").append(duplicatedCard);
    const ambiguousIdentity = await parser.extractPage(
      "ctrip",
      "flight",
      ambiguousIdentityRoot,
      "https://flights.ctrip.com/online/list/round-hgh-mle",
      new Date("2026-07-31T00:00:00Z"),
      query,
      {
        mode: "search_url",
        triggered: true,
        confirmed_query: { ...query },
        confirmation_scope: "trusted_exact_search_url",
        action_trace: [
          {
            action: "search",
            provider: "ctrip",
            evidence: "fixture_exact_round_trip_search_url",
          },
        ],
      },
    );
    record(
      "Ctrip duplicate stable outbound identities fail closed as ambiguous",
      ambiguousIdentity.state === "failed" &&
        ambiguousIdentity.quotes.length === 0 &&
        !ambiguousIdentity.selection,
      JSON.stringify(ambiguousIdentity),
    );

    const exactUrlOnlyRoot = new DOMParser().parseFromString(
      fixtures["ctrip-flight-live-outbound-styled-control-safe"],
      "text/html",
    );
    const datedText = [...exactUrlOnlyRoot.querySelectorAll("span")].find(
      (node) => /2026年8月12日 20:55/.test(node.textContent),
    );
    datedText.textContent = "20:55";
    const exactSearchUrl =
      "https://flights.ctrip.com/international/search/round-hgh-mle" +
      "?depdate=2026-08-12_2026-08-18" +
      "&cabin=y_s&adult=2&child=0&infant=0";
    const exactUrlOnly = await parser.extractPage(
      "ctrip",
      "flight",
      exactUrlOnlyRoot,
      exactSearchUrl,
      new Date("2026-07-31T00:00:00Z"),
      { ...query, search_url: exactSearchUrl },
      {
        mode: "search_url",
        triggered: true,
        confirmed_query: { ...query },
        confirmation_scope: "trusted_exact_search_url",
        action_trace: [
          {
            action: "search",
            provider: "ctrip",
            evidence: "fixture_exact_round_trip_search_url",
          },
        ],
      },
    );
    record(
      "Ctrip exact audited search URL can confirm an omitted per-card service date",
      exactUrlOnly.state === "outbound_preview" &&
        exactUrlOnly.selection.outbound_departure_at ===
          "2026-08-12T20:55:00+08:00",
      JSON.stringify(exactUrlOnly),
    );

    const tamperedUrlOnly = await parser.extractPage(
      "ctrip",
      "flight",
      exactUrlOnlyRoot,
      exactSearchUrl,
      new Date("2026-07-31T00:00:00Z"),
      {
        ...query,
        search_url: exactSearchUrl.replace("adult=2", "adult=1"),
      },
      {
        mode: "search_url",
        triggered: true,
        confirmed_query: { ...query },
        confirmation_scope: "trusted_exact_search_url",
        action_trace: [
          {
            action: "search",
            provider: "ctrip",
            evidence: "fixture_tampered_round_trip_search_url",
          },
        ],
      },
    );
    record(
      "Ctrip omitted card date refuses a tampered audited-search adult count",
      tamperedUrlOnly.state === "failed" &&
        tamperedUrlOnly.quotes.length === 0,
      JSON.stringify(tamperedUrlOnly),
    );

    const changedRoot = new DOMParser().parseFromString(
      fixtures["ctrip-flight-live-outbound-styled-control-safe"],
      "text/html",
    );
    const changedPreview = await parser.extractPage(
      "ctrip",
      "flight",
      changedRoot,
      "https://flights.ctrip.com/online/list/round-hgh-mle",
      new Date("2026-07-31T00:00:00Z"),
      query,
      {
        mode: "search_url",
        triggered: true,
        confirmed_query: { ...query },
        readback_query: {
          origin_code: query.origin_code,
          destination_code: query.destination_code,
          start_date: query.start_date,
          end_date: query.end_date,
          adults: query.adults,
        },
        confirmation_scope: "trusted_exact_search_url",
        action_trace: [
          {
            action: "search",
            provider: "ctrip",
            evidence: "fixture_exact_round_trip_search_url",
          },
        ],
      },
    );
    const changedClicks = [];
    changedRoot
      .querySelector(".flight-operate .btn.btn-book")
      .addEventListener("click", () => changedClicks.push("unsafe"));
    changedRoot
      .querySelector(".flight-operate")
      .setAttribute("data-action", "payment");
    const changedSelection = await parser.safeSelectOutbound(
      "ctrip",
      changedRoot,
      query,
      changedPreview.selection.selection_id,
    );
    record(
      "Ctrip safe selection recomputes evidence and refuses a transaction mutation before clicking",
      changedPreview.state === "outbound_preview" &&
        changedSelection.selected === false &&
        changedSelection.code === "outbound_selection_evidence_changed" &&
        changedClicks.length === 0,
      JSON.stringify({ changedPreview, changedSelection, changedClicks }),
    );

    const transactionControl = await parser.extractPage(
      "ctrip",
      "flight",
      new DOMParser().parseFromString(
        fixtures[
          "ctrip-flight-live-outbound-styled-control-transaction"
        ],
        "text/html",
      ),
      "https://flights.ctrip.com/online/list/round-hgh-mle",
      new Date("2026-07-31T00:00:00Z"),
      query,
      {
        mode: "search_url",
        triggered: true,
        confirmed_query: { ...query },
        readback_query: {
          origin_code: query.origin_code,
          destination_code: query.destination_code,
          start_date: query.start_date,
          end_date: query.end_date,
          adults: query.adults,
        },
        confirmation_scope: "trusted_exact_search_url",
        action_trace: [
          {
            action: "search",
            provider: "ctrip",
            evidence: "fixture_exact_round_trip_search_url",
          },
        ],
      },
    );
    const transactionDiagnostic =
      transactionControl.failure &&
      transactionControl.failure.details &&
      transactionControl.failure.details.flight_diagnostic;
    record(
      "Ctrip booking/payment href ancestry is never promoted to a safe styled outbound control",
      transactionControl.state === "failed" &&
        transactionControl.quotes.length === 0 &&
        transactionDiagnostic.counts.safe_outbound_control_count === 0,
      JSON.stringify(transactionControl),
    );

    const conflictingPromotion = await parser.extractPage(
      "ctrip",
      "flight",
      new DOMParser().parseFromString(
        fixtures[
          "ctrip-flight-live-outbound-styled-control-promo-conflict"
        ],
        "text/html",
      ),
      "https://flights.ctrip.com/online/list/round-hgh-mle",
      new Date("2026-07-31T00:00:00Z"),
      query,
      {
        mode: "search_url",
        triggered: true,
        confirmed_query: { ...query },
        readback_query: {
          origin_code: query.origin_code,
          destination_code: query.destination_code,
          start_date: query.start_date,
          end_date: query.end_date,
          adults: query.adults,
        },
        confirmation_scope: "trusted_exact_search_url",
        action_trace: [
          {
            action: "search",
            provider: "ctrip",
            evidence: "fixture_exact_round_trip_search_url",
          },
        ],
      },
    );
    const conflictingDiagnostic =
      conflictingPromotion.failure &&
      conflictingPromotion.failure.details &&
      conflictingPromotion.failure.details.flight_diagnostic;
    record(
      "Ctrip card-local promotion price blocks comparison receipt instead of being mixed with the fare",
      conflictingPromotion.state === "failed" &&
        conflictingPromotion.failure.code === "dom_drift" &&
        conflictingPromotion.quotes.length === 0 &&
        conflictingDiagnostic.counts.safe_outbound_control_count === 0 &&
        conflictingDiagnostic.counts.semantic_outbound_card_count === 1 &&
        conflictingDiagnostic.counts.priced_comparison_candidate_count ===
          0 &&
        conflictingDiagnostic.structures[0]
          .comparison_candidate_accepted === false &&
        !Object.prototype.hasOwnProperty.call(
          conflictingPromotion.failure.details,
          "flight_search_receipt",
        ),
      JSON.stringify(conflictingPromotion),
    );

    const selectedDriver = {
      mode: "visible_form",
      triggered: true,
      confirmed_query: { ...query },
      readback_query: {
        origin: query.origin,
        destination: query.destination,
        start_date: query.start_date,
        end_date: query.end_date,
        adults: query.adults,
      },
      confirmation_scope: "confirmed_visible_search",
      party_availability_confirmed: true,
      action_trace: [
        {
          action: "search",
          provider: "ctrip",
          evidence: "fixture_exact_round_trip_search",
        },
        {
          action: "select_outbound",
          provider: "ctrip",
          evidence: "fixture_selected_outbound",
        },
      ],
      selected_outbound: {
        carrier_text: "泰国亚航",
        outbound_departure_at: "2026-08-12T18:10:00+08:00",
        outbound_arrival_at: "2026-08-13T11:35:00+05:00",
        selection_id: "fixture-live-ctrip-selection",
        selection_evidence: "fixture selected outbound",
      },
    };
    const starting = await parser.extractPage(
      "ctrip",
      "flight",
      new DOMParser().parseFromString(
        fixtures["ctrip-flight-live-return-starting-semantic"],
        "text/html",
      ),
      "https://flights.ctrip.com/online/list/round-hgh-mle",
      new Date("2026-07-31T00:00:00Z"),
      query,
      selectedDriver,
    );
    const startingDiagnostic =
      starting.failure &&
      starting.failure.details &&
      starting.failure.details.flight_diagnostic;
    const startingReceipt =
      starting.failure &&
      starting.failure.details &&
      starting.failure.details.flight_search_receipt;
    record(
      "Ctrip complete routes and enabled 订票 still fail closed on 起 price",
      starting.state === "failed" &&
        starting.failure.code === "extraction_error" &&
        starting.quotes.length === 0 &&
        startingDiagnostic.outcome === "starting_price_only" &&
        startingDiagnostic.stage === "price_finality_validation" &&
        startingDiagnostic.counts.parsed_return_leg_count === 1 &&
        startingDiagnostic.counts.matching_return_route_count === 1 &&
        startingDiagnostic.counts.explicit_tax_evidence_count === 1 &&
        startingDiagnostic.counts.availability_evidence_count === 1 &&
        startingDiagnostic.counts.starting_price_count === 1 &&
        startingDiagnostic.counts.valid_final_price_contract_count === 0 &&
        startingDiagnostic.blocking_contract_fields.includes(
          "price_finality",
        ) &&
        startingDiagnostic.blocking_contract_fields.includes("price_basis") &&
        startingReceipt.state === "comparison_price_only" &&
        startingReceipt.scanned_count === 1 &&
        startingReceipt.candidate_summaries[0].amount === 5159 &&
        startingReceipt.candidate_summaries[0].price_basis ===
          "per_person" &&
        startingReceipt.candidate_summaries[0].price_classification ===
          "starting_or_estimated" &&
        /^[a-f0-9]{64}$/.test(
          starting.failure.details.flight_search_receipt_sha256,
        ),
      JSON.stringify(starting),
    );

    const final = await parser.extractPage(
      "ctrip",
      "flight",
      new DOMParser().parseFromString(
        fixtures["ctrip-flight-live-return-final-semantic"],
        "text/html",
      ),
      "https://flights.ctrip.com/online/list/round-hgh-mle",
      new Date("2026-07-31T00:00:00Z"),
      query,
      selectedDriver,
    );
    record(
      "Ctrip semantic return ancestor can quote only one atomic final per-person fare",
      final.state === "succeeded" &&
        final.quotes.length === 1 &&
        final.quotes[0].amount === 4692 &&
        final.quotes[0].price_basis === "per_person" &&
        final.quotes[0].taxes_included === true &&
        final.quotes[0].details.availability_evidence === "订票" &&
        final.quotes[0].details.outbound_route_evidence.matches_expected ===
          true &&
        final.quotes[0].details.return_route_evidence.matches_expected ===
          true,
      JSON.stringify(final),
    );
  }

  {
    const query = {
      origin: "杭州",
      destination: "马累",
      start_date: "2026-08-23",
      end_date: "2026-08-30",
      adults: 2,
      currency: "CNY",
      origin_code: "HGH",
      destination_code: "MLE",
      options: {},
    };
    const root = new DOMParser().parseFromString(
      fixtures["fliggy-flight-outbound-preview"],
      "text/html",
    );
    const clicked = [];
    for (const button of root.querySelectorAll("button")) {
      button.addEventListener("click", () =>
        clicked.push(button.textContent.trim())
      );
    }
    const preview = await parser.extractPage(
      "fliggy",
      "flight",
      root,
      "https://sijipiao.fliggy.com/ie/flight_search_result.htm",
      new Date("2026-07-30T12:00:00Z"),
      query,
      {
        mode: "fixture",
        triggered: true,
        confirmed_query: {
          origin: "杭州",
          destination: "马累",
          start_date: "2026-08-23",
          end_date: "2026-08-30",
          adults: 2,
        },
        confirmation_scope: "fixture",
        party_availability_confirmed: false,
        action_trace: [
          {
            action: "search",
            provider: "fliggy",
            evidence: "fixture_exact_round_trip_search",
          },
        ],
      },
    );
    const selection = preview.selection;
    const selected = selection &&
      await parser.safeSelectOutbound(
        "fliggy",
        root,
        query,
        selection.selection_id,
      );
    record(
      "fliggy preview clicks only exact 选为去程",
      preview.state === "outbound_preview" &&
        preview.quotes.length === 0 &&
        selected.selected === true &&
        JSON.stringify(clicked) === JSON.stringify(["选为去程"]),
      JSON.stringify({ preview, selected, clicked }),
    );
  }

  {
    const query = {
      origin: "杭州",
      destination: "马累",
      start_date: "2026-08-12",
      end_date: "2026-08-18",
      adults: 2,
      currency: "CNY",
      origin_code: "HGH",
      destination_code: "MLE",
      options: {},
    };
    const root = new DOMParser().parseFromString(
      fixtures["fliggy-flight-live-outbound-semantic"],
      "text/html",
    );
    const output = await parser.extractPage(
      "fliggy",
      "flight",
      root,
      "https://sijipiao.fliggy.com/ie/flight_search_result.htm",
      new Date("2026-07-31T00:00:00Z"),
      query,
      {
        mode: "fixture",
        triggered: true,
        confirmed_query: { ...query },
        confirmation_scope: "fixture",
        party_availability_confirmed: false,
        action_trace: [
          {
            action: "search",
            provider: "fliggy",
            evidence: "fixture_exact_round_trip_search",
          },
        ],
      },
    );
    record(
      "Fliggy renamed live outbound button proves a candidate, never a round-trip quote",
      output.state === "outbound_preview" &&
        output.quotes.length === 0 &&
        output.selection.carrier_text === "泰国亚航" &&
        output.selection.outbound_route_evidence.matches_expected === true &&
        parser.flightPriceContract("¥5718 起").valid === false,
      JSON.stringify(output),
    );
  }

  {
    const incomplete = await parser.extractPage(
      "ctrip",
      "flight",
      new DOMParser().parseFromString(fixtures["ctrip-flight"], "text/html"),
      "https://flights.ctrip.com/results",
      new Date("2026-07-30T12:00:00Z"),
      {
        origin: "杭州",
        destination: "马累",
        start_date: "2026-08-23",
        end_date: "2026-08-30",
        adults: 2,
        currency: "CNY",
        origin_code: "HGH",
        destination_code: "MLE",
        options: {},
      },
      {
        mode: "fixture",
        triggered: true,
        confirmed_query: {
          origin: "杭州",
          destination: "马累",
          start_date: "2026-08-23",
          end_date: "2026-08-30",
          adults: 2,
        },
        confirmation_scope: "fixture",
        party_availability_confirmed: true,
        action_trace: [
          {
            action: "search",
            provider: "ctrip",
            evidence: "fixture_exact_round_trip_search",
          },
        ],
      },
    );
    record(
      "selected summary without select-outbound trace fails closed",
      incomplete.state === "failed" &&
        incomplete.failure.code === "dom_drift" &&
        Array.isArray(incomplete.quotes) &&
        incomplete.quotes.length === 0,
      JSON.stringify(incomplete),
    );
  }

  {
    const strictQuery = {
      origin: "杭州",
      destination: "马累",
      start_date: "2026-08-23",
      end_date: "2026-08-30",
      adults: 2,
      rooms: 1,
      currency: "CNY",
      origin_code: "HGH",
      destination_code: "MLE",
      search_url: null,
      options: {},
    };
    const strictDriver = {
      mode: "fixture",
      triggered: true,
      confirmed_query: {
        origin: "杭州",
        destination: "马累",
        start_date: "2026-08-23",
        end_date: "2026-08-30",
        adults: 2,
      },
      confirmation_scope: "fixture",
      party_availability_confirmed: true,
      action_trace: [
        {
          action: "search",
          provider: "ctrip",
          evidence: "fixture_exact_round_trip_search",
        },
        {
          action: "select_outbound",
          provider: "ctrip",
          evidence: "fixture_selected_outbound",
        },
      ],
      selected_outbound: {
        carrier_text: "香港航空",
        outbound_departure_at: "2026-08-23T08:30:00+08:00",
        outbound_arrival_at: "2026-08-23T18:35:00+05:00",
        selection_id: "fixture-ctrip-selection",
        selection_evidence: "fixture selected outbound",
      },
    };
    for (const [name, fixtureName] of [
      ["wrong outbound route", "ctrip-flight-wrong-outbound-route"],
      ["wrong return route", "ctrip-flight-wrong-return-route"],
      ["conflicting tax text", "ctrip-flight-tax-conflict"],
      ["missing availability", "ctrip-flight-no-availability"],
    ]) {
      const rejected = await parser.extractPage(
        "ctrip",
        "flight",
        new DOMParser().parseFromString(fixtures[fixtureName], "text/html"),
        "https://flights.ctrip.com/results",
        new Date("2026-07-30T12:00:00Z"),
        strictQuery,
        strictDriver,
      );
      record(
        `flight contract rejects ${name}`,
        rejected.state === "failed" &&
          rejected.failure.code === "dom_drift" &&
          Array.isArray(rejected.quotes) &&
          rejected.quotes.length === 0,
        JSON.stringify(rejected),
      );
    }
    const ambiguousFliggy = await parser.extractPage(
      "fliggy",
      "flight",
      new DOMParser().parseFromString(
        fixtures["fliggy-flight-ambiguous-total"],
        "text/html",
      ),
      "https://sijipiao.fliggy.com/ie/flight_search_result.htm",
      new Date("2026-07-30T12:00:00Z"),
      strictQuery,
      {
        ...strictDriver,
        party_availability_confirmed: false,
        action_trace: strictDriver.action_trace.map((item) => ({
          ...item,
          provider: "fliggy",
        })),
        selected_outbound: {
          carrier_text: "亚洲航空",
          outbound_departure_at: "2026-08-23T07:10:00+08:00",
          outbound_arrival_at: "2026-08-23T17:20:00+05:00",
          selection_id: "fixture-fliggy-selection",
          selection_evidence: "fixture selected outbound",
        },
      },
    );
    record(
      "Fliggy 往返总价 is not guessed as a per-person fare",
      parser.flightPriceContract("往返总价 含税 ¥4,858").valid === false &&
        ambiguousFliggy.state === "failed" &&
        ambiguousFliggy.failure.code === "dom_drift" &&
        ambiguousFliggy.quotes.length === 0,
      JSON.stringify(ambiguousFliggy),
    );
  }

  record(
    "real flight fixtures use visible text rather than datetime attributes",
    ["ctrip-flight", "fliggy-flight", "qunar-flight"].every(
      (name) => !/<time\b|data-datetime=/i.test(fixtures[name]),
    ),
  );

  {
    const drift = await parser.extractPage(
      "ctrip",
      "flight",
      new DOMParser().parseFromString(
        fixtures["ctrip-flight-dom-drift-diagnostic"],
        "text/html",
      ),
      "https://flights.ctrip.com/results",
      new Date("2026-07-30T12:00:00Z"),
      {
        origin: "杭州",
        destination: "马累",
        start_date: "2026-08-23",
        end_date: "2026-08-30",
        adults: 2,
        currency: "CNY",
        origin_code: "HGH",
        destination_code: "MLE",
        options: {},
      },
    );
    const diagnostic =
      drift.failure &&
      drift.failure.details &&
      drift.failure.details.dom_diagnostics;
    const candidates = (diagnostic && diagnostic.candidates) || [];
    const candidate = candidates[0] || {};
    const serialized = JSON.stringify(diagnostic);
    record(
      "dom drift diagnostics are visible, bounded, and privacy-minimal",
      drift.state === "failed" &&
        drift.failure.code === "dom_drift" &&
        diagnostic.scope === "visible_candidate_cards_only" &&
        diagnostic.max_candidates === 6 &&
        candidates.length === 1 &&
        candidate.tag === "article" &&
        candidate.class.length <= 120 &&
        candidate.text_summary.length <= 180 &&
        candidate.price_anchor_hits === 1 &&
        candidate.action_anchor_hits === 1 &&
        !serialized.includes("owner@example.com") &&
        !serialized.includes("13912345678") &&
        !serialized.includes("123456789012") &&
        !serialized.includes("top-secret-password") &&
        !serialized.includes("session-secret-must-not-survive") &&
        !serialized.includes("<main"),
      JSON.stringify(drift),
    );
  }

  for (const provider of ["ctrip", "fliggy", "qunar"]) {
    for (const kind of ["flight", "lodging"]) {
      const { form, button } = searchForm(kind);
      let clicked = false;
      button.addEventListener("click", () => {
        clicked = true;
      });
      const prepared = await visibleDriver.prepareSearch(provider, kind, {
        origin: kind === "flight" ? "杭州" : null,
        destination: "马累",
        origin_code: kind === "flight" ? "HGH" : null,
        destination_code: "MLE",
        start_date: "2026-08-23",
        end_date: "2026-08-30",
        adults: 2,
        rooms: 1,
      });
      const triggered = visibleDriver.triggerSearch(provider, kind);
      await new Promise((resolve) => setTimeout(resolve, 80));
      const expectedFields = kind === "flight"
        ? ["origin", "destination", "start_date", "end_date", "adults"]
        : ["destination", "start_date", "end_date", "adults", "rooms"];
      record(
        `${provider}/${kind} visible-form occupancy confirmation`,
        prepared.prepared === true &&
          prepared.confirmation_scope === "visible_form_fields_readback" &&
          JSON.stringify(Object.keys(prepared.confirmed_query).sort()) ===
            JSON.stringify(expectedFields.sort()) &&
          prepared.confirmed_query.adults === 2 &&
          (kind === "flight" || prepared.confirmed_query.rooms === 1) &&
          prepared.readback_query.adults === 2 &&
          (kind === "flight" || prepared.readback_query.rooms === 1) &&
          triggered.triggered === true &&
          clicked,
        JSON.stringify({ prepared, triggered, clicked }),
      );
      form.remove();
    }
  }

  function mountFixture(name) {
    const shell = document.createElement("section");
    shell.style.display = "block";
    shell.innerHTML = fixtures[name];
    document.body.append(shell);
    return shell;
  }

  {
    const shell = mountFixture("ctrip-flight-recovery-notice");
    const button = shell.querySelector("button");
    let clicked = false;
    button.addEventListener("click", () => {
      clicked = true;
    });
    const notice =
      visibleDriver.auditedCtripFlightRecoveryNotice(shell);
    if (notice) {
      notice.control.click();
    }
    const unsafe = shell.cloneNode(true);
    unsafe.querySelector("p").textContent += " 请确认订单并支付";
    document.body.append(unsafe);
    const rejected =
      visibleDriver.auditedCtripFlightRecoveryNotice(unsafe);
    record(
      "Ctrip only dismisses the audited non-transactional flight requery notice",
      notice &&
        notice.control === button &&
        clicked === true &&
        rejected === null,
      JSON.stringify({ clicked, rejected: Boolean(rejected) }),
    );
    shell.remove();
    unsafe.remove();
  }

  {
    const shell = mountFixture("ctrip-lodging-suggestion");
    const input = shell.querySelector("#destinationInput");
    const hotel = shell.querySelector(
      "[data-tripchord-suggestion-kind='hotel']",
    );
    const city = shell.querySelector(
      "[data-tripchord-suggestion-kind='city']",
    );
    const clicked = [];
    hotel.addEventListener("click", () => clicked.push("hotel"));
    city.addEventListener("click", () => {
      clicked.push("city");
      input.value = "马富施";
    });
    const identity = visibleDriver.suggestionIdentity(
      "ctrip",
      "lodging",
      "Maafushi",
      "maafushi",
    );
    const hotelMatch =
      visibleDriver.suggestionIdentityMatches(hotel, identity);
    const cityMatch =
      visibleDriver.suggestionIdentityMatches(
        city,
        identity,
        city.querySelector("span"),
      );
    const selected = await visibleDriver.selectVisibleSuggestion(
      "ctrip",
      "lodging",
      "Maafushi",
      null,
      input,
      "maafushi",
    );
    record(
      "Ctrip rejects hotel-name substring and selects only exact Maafushi city",
      hotelMatch.matched === false &&
        hotelMatch.evidence === "lodging_candidate_not_destination" &&
        cityMatch.matched === true &&
        cityMatch.evidence === "audited_exact_destination_ancestor" &&
        selected.selected === true &&
        selected.identity_evidence === "audited_exact_destination_ancestor" &&
        selected.readback_value === "马富施" &&
        JSON.stringify(clicked) === JSON.stringify(["city"]),
      JSON.stringify({ hotelMatch, cityMatch, selected, clicked }),
    );
    shell.remove();
  }

  {
    const shell = mountFixture("ctrip-lodging-suggestion-no-readback");
    const input = shell.querySelector("#destinationInput");
    const city = shell.querySelector(
      "[data-tripchord-suggestion-kind='city']",
    );
    city.innerHTML = "<span>Maafushi</span><span>Kaafu Atoll, Maldives</span>";
    city.addEventListener("click", () => {
      shell.querySelector(".ctrip-suggestion-list").style.display = "none";
    });
    const selected = await visibleDriver.selectVisibleSuggestion(
      "ctrip",
      "lodging",
      "Maafushi",
      null,
      input,
      "maafushi",
    );
    record(
      "Ctrip exact English city is confirmed when the dropdown dismisses",
      selected.selected === true &&
        selected.identity_evidence === "audited_exact_destination_ancestor" &&
        selected.readback_value === "Maafushi",
      JSON.stringify(selected),
    );
    shell.remove();
  }

  {
    const shell = document.createElement("main");
    shell.id = "trip_main_content";
    shell.innerHTML = `
      <input
        id="destinationInput"
        aria-label="目的地"
        placeholder="目的地"
        value="Maafushi"
      >`;
    document.body.append(shell);
    const input = shell.querySelector("#destinationInput");
    const delayedCandidate = window.setTimeout(() => {
      const row = document.createElement("div");
      row.tabIndex = -1;
      row.innerHTML =
        "<span>Maafushi</span><span>Kaafu Atoll, Maldives</span>";
      row.addEventListener("click", () => {
        input.value = "马富施";
        row.style.display = "none";
      });
      shell.append(row);
    }, 2700);
    const selected = await visibleDriver.selectVisibleSuggestion(
      "ctrip",
      "lodging",
      "Maafushi",
      null,
      input,
      "maafushi",
    );
    window.clearTimeout(delayedCandidate);
    record(
      "Ctrip waits for a delayed audited overseas-city suggestion",
      selected.selected === true &&
        selected.menu_observed === true &&
        selected.identity_evidence === "audited_exact_destination_ancestor" &&
        selected.readback_value === "马富施",
      JSON.stringify(selected),
    );
    shell.remove();
  }

  {
    const shell = mountFixture("ctrip-lodging-suggestion-no-readback");
    const input = shell.querySelector("#destinationInput");
    const selected = await visibleDriver.selectVisibleSuggestion(
      "ctrip",
      "lodging",
      "Maafushi",
      null,
      input,
      "maafushi",
    );
    record(
      "Ctrip unchanged typed value without dropdown acknowledgement remains rejected",
      selected.selected === false &&
        selected.identity_evidence === "selected_city_readback_unconfirmed" &&
        input.value === "Maafushi",
      JSON.stringify(selected),
    );
    shell.remove();
  }

  {
    const shell = document.createElement("main");
    shell.id = "trip_main_content";
    shell.innerHTML = `
      <input
        id="destinationInput"
        aria-label="目的地"
        placeholder="目的地"
        value="Maafushi"
      >
      <div class="ctrip-suggestion-list">
        <div
          tabindex="-1"
          data-test-area="wrong"
          data-tripchord-suggestion-kind="city"
        ><span>Maafushi</span><span>Dhaalu Atoll, Maldives</span></div>
        <div
          tabindex="-1"
          data-test-area="right"
          data-tripchord-suggestion-kind="city"
        ><span>Maafushi</span><span>Kaafu Atoll, Maldives</span></div>
      </div>`;
    document.body.append(shell);
    const input = shell.querySelector("#destinationInput");
    const clicked = [];
    shell.querySelector("[data-test-area='wrong']").addEventListener(
      "click",
      () => clicked.push("wrong"),
    );
    shell.querySelector("[data-test-area='right']").addEventListener(
      "click",
      () => {
        clicked.push("right");
        input.value = "Maafushi";
        shell.querySelector(".ctrip-suggestion-list").style.display = "none";
      },
    );
    const selected = await visibleDriver.selectVisibleSuggestion(
      "ctrip",
      "lodging",
      "Maafushi",
      null,
      input,
      "maafushi",
    );
    record(
      "Ctrip rejects Dhaalu and selects only the audited Kaafu Maafushi row",
      selected.selected === true &&
        selected.identity_evidence === "audited_exact_destination_ancestor" &&
        JSON.stringify(clicked) === JSON.stringify(["right"]),
      JSON.stringify({ selected, clicked }),
    );
    shell.remove();
  }

  {
    const shell = mountFixture("ctrip-lodging-suggestion-inner-label");
    const input = shell.querySelector("#destinationInput");
    const hotel = shell.querySelector(
      "[data-tripchord-suggestion-kind='hotel']",
    );
    const city = shell.querySelector(
      "[data-tripchord-suggestion-kind='city']",
    );
    const clicked = [];
    hotel.addEventListener("click", () => clicked.push("hotel"));
    city.addEventListener("click", () => {
      clicked.push("city");
      input.value = "马富施";
    });
    const selected = await visibleDriver.selectVisibleSuggestion(
      "ctrip",
      "lodging",
      "Maafushi",
      null,
      input,
      "maafushi",
    );
    record(
      "Ctrip exact inner label clicks only its audited destination ancestor",
      selected.selected === true &&
        selected.identity_evidence === "audited_exact_destination_ancestor" &&
        selected.readback_value === "马富施" &&
        JSON.stringify(clicked) === JSON.stringify(["city"]),
      JSON.stringify({ selected, clicked }),
    );
    shell.remove();
  }

  {
    const shell = document.createElement("section");
    shell.style.display = "block";
    shell.innerHTML = `
      <main id="trip_main_content">
        <input
          id="destinationInput"
          aria-label="目的地"
          placeholder="目的地"
          value="Maafushi"
        >
        <div class="ctrip-suggestion-list">
          ${Array.from(
            { length: 3000 },
            (_, index) =>
              `<div tabindex="-1">无关目的地 ${index}</div>`,
          ).join("")}
          <div
            tabindex="-1"
            data-tripchord-suggestion-kind="city"
          ><span>马富施岛</span><span>Maafushi</span><span>Kaafu Atoll, Maldives</span></div>
        </div>
      </main>`;
    document.body.append(shell);
    const input = shell.querySelector("#destinationInput");
    shell
      .querySelector("[data-tripchord-suggestion-kind='city']")
      .addEventListener("click", () => {
        input.value = "马富施";
      });
    const startedAt = performance.now();
    const selected = await visibleDriver.selectVisibleSuggestion(
      "ctrip",
      "lodging",
      "Maafushi",
      null,
      input,
      "maafushi",
    );
    const elapsedMs = performance.now() - startedAt;
    record(
      "Ctrip suggestion selection filters a large irrelevant DOM before visibility and subtree work",
      selected.selected === true &&
        selected.identity_evidence === "audited_exact_destination_ancestor" &&
        selected.readback_value === "马富施" &&
        elapsedMs < 2000,
      JSON.stringify({ selected, elapsed_ms: Math.round(elapsedMs) }),
    );
    shell.remove();
  }

  {
    const shell = mountFixture("fliggy-lodging-suggestion");
    const input = shell.querySelector(
      "input[data-testid='international-city-input']",
    );
    const option = shell.querySelector("[data-agent-type='city-option']");
    const events = [];
    for (const type of ["pointerdown", "mousedown", "mouseup", "click"]) {
      option.addEventListener(type, () => events.push(type));
    }
    option.addEventListener("click", () => {
      const replacement = input.cloneNode(true);
      replacement.value = "马富士";
      input.replaceWith(replacement);
      shell.querySelector("[role='listbox']").style.display = "none";
    });
    const selected = await visibleDriver.selectVisibleSuggestion(
      "fliggy",
      "lodging",
      "Maafushi",
      null,
      input,
      "maafushi",
    );
    record(
      "Fliggy audited semantic option and exact visible input are read back",
      selected.selected === true &&
        selected.identity_evidence === "audited_semantic_option_identity" &&
        selected.readback_value === "马富士" &&
        selected.activation_mode === "native_html_click" &&
        JSON.stringify(events) === JSON.stringify(["click"]) &&
        !visibleDriver.auditedInputIdentity(
          shell.querySelector(
            "input[data-testid='international-city-input']",
          ),
        ).includes("933081"),
      JSON.stringify({ selected, events }),
    );
    shell.remove();
  }

  {
    const shell = mountFixture("fliggy-lodging-suggestion");
    const input = shell.querySelector(
      "input[data-testid='international-city-input']",
    );
    const option = shell.querySelector("[data-agent-type='city-option']");
    const events = [];
    for (const type of ["pointerdown", "mousedown", "mouseup", "click"]) {
      option.addEventListener(type, () => events.push(type));
    }
    const selected = await visibleDriver.selectVisibleSuggestion(
      "fliggy",
      "lodging",
      "Maafushi",
      null,
      input,
      "maafushi",
    );
    record(
      "Fliggy native click without exact input readback remains rejected",
      selected.selected === false &&
        selected.activation_mode === "native_html_click" &&
        JSON.stringify(events) === JSON.stringify(["click"]) &&
        input.value === "Maafushi",
      JSON.stringify({ selected, events }),
    );
    shell.remove();
  }

  {
    const shell = mountFixture("fliggy-lodging-suggestion");
    const input = shell.querySelector(
      "input[data-testid='international-city-input']",
    );
    const option = shell.querySelector("[data-agent-type='city-option']");
    option.addEventListener("click", () => {
      input.value = "马富士";
    });
    const selected = await visibleDriver.selectVisibleSuggestion(
      "fliggy",
      "lodging",
      "Maafushi",
      null,
      input,
      "maafushi",
    );
    record(
      "Fliggy exact input mutation without dropdown dismissal remains rejected",
      selected.selected === false &&
        selected.activation_mode === "native_html_click" &&
        selected.readback_value === "马富士",
      JSON.stringify(selected),
    );
    shell.remove();
  }

  {
    const shell = mountFixture("fliggy-lodging-country-inner-label");
    const input = shell.querySelector(
      "input[data-testid='international-city-input']",
    );
    const option = shell.querySelector("[role='option']");
    const events = [];
    for (const type of ["pointerdown", "mousedown", "mouseup", "click"]) {
      option.addEventListener(type, () => events.push(type));
    }
    option.addEventListener("click", () => {
      shell.querySelector("[role='listbox']").style.display = "none";
    });
    const selected = await visibleDriver.selectVisibleSuggestion(
      "fliggy",
      "lodging",
      "马尔代夫",
      null,
      input,
      "maafushi",
    );
    record(
      "Fliggy exact country inner label clicks and reads back its option ancestor",
      selected.selected === true &&
        selected.identity_evidence === "audited_exact_destination_ancestor" &&
        selected.readback_value === "马尔代夫" &&
        JSON.stringify(events) ===
          JSON.stringify(["pointerdown", "mousedown", "mouseup", "click"]),
      JSON.stringify({ selected, events }),
    );
    shell.remove();
  }

  {
    const shell = mountFixture("fliggy-lodging-country-inner-label");
    const input = shell.querySelector(
      "input[data-testid='international-city-input']",
    );
    const control = shell.querySelector(
      "[data-tripchord-fixture='destination-control']",
    );
    const option = shell.querySelector("[role='option']");
    option.addEventListener("click", () => {
      input.value = "";
      input.style.display = "none";
      shell.querySelector("[role='listbox']").style.display = "none";
      const selectedValue = document.createElement("span");
      selectedValue.textContent = "马尔代夫";
      control.append(selectedValue);
    });
    const selected = await visibleDriver.selectVisibleSuggestion(
      "fliggy",
      "lodging",
      "马尔代夫",
      null,
      input,
      "maafushi",
    );
    record(
      "Fliggy reads an exact selected country from the original audited destination control",
      selected.selected === true &&
        selected.identity_evidence === "audited_exact_destination_ancestor" &&
        selected.readback_value === "马尔代夫",
      JSON.stringify(selected),
    );
    shell.remove();
  }

  {
    const shell = mountFixture("fliggy-lodging-country-inner-label");
    const input = shell.querySelector(
      "input[data-testid='international-city-input']",
    );
    const control = shell.querySelector(
      "[data-tripchord-fixture='destination-control']",
    );
    const option = shell.querySelector("[role='option']");
    option.addEventListener("click", () => {
      input.value = "";
      input.style.display = "none";
      shell.querySelector("[role='listbox']").style.display = "none";
      const wrongValue = document.createElement("span");
      wrongValue.textContent = "泰国";
      control.append(wrongValue);
    });
    const selected = await visibleDriver.selectVisibleSuggestion(
      "fliggy",
      "lodging",
      "马尔代夫",
      null,
      input,
      "maafushi",
    );
    record(
      "Fliggy rejects a dismissed option when the audited destination control has no exact readback",
      selected.selected === false &&
        selected.identity_evidence === "selected_city_readback_unconfirmed" &&
        selected.readback_value === null,
      JSON.stringify(selected),
    );
    shell.remove();
  }

  {
    const shell = mountFixture("fliggy-lodging-suggestion-wrong-id");
    const candidate = shell.querySelector("[data-agent-type='city-option']");
    const identity = visibleDriver.suggestionIdentity(
      "fliggy",
      "lodging",
      "Maafushi",
      "maafushi",
    );
    const match = visibleDriver.suggestionIdentityMatches(candidate, identity);
    record(
      "Fliggy wrong suggestion ID is rejected despite the Chinese label",
      match.matched === false &&
        match.evidence === "conflicting_suggestion_id",
      JSON.stringify(match),
    );
    const missingPlaceKeyIdentity = visibleDriver.suggestionIdentity(
      "fliggy",
      "lodging",
      "Maafushi",
      null,
    );
    record(
      "Fliggy audited destinations require expected_lodging_place_key",
      missingPlaceKeyIdentity.unresolved === true &&
        visibleDriver.suggestionIdentityMatches(
          candidate,
          missingPlaceKeyIdentity,
        ).matched === false,
    );
    const hulhumaleIdentity = visibleDriver.suggestionIdentity(
      "fliggy",
      "lodging",
      "Hulhumalé",
      "hulhumale",
    );
    record(
      "Fliggy Hulhumale audited identity is fixed to cityCode 934358",
      hulhumaleIdentity.id === "934358" &&
        hulhumaleIdentity.visibleLabels.includes("哈尔胡梅尔"),
      JSON.stringify(hulhumaleIdentity),
    );
    const maafushiStrategy = visibleDriver.fliggyLodgingSearchStrategy(
      "fliggy",
      "lodging",
      {
        destination: "Maafushi",
        options: { expected_lodging_place_key: "maafushi" },
      },
    );
    const resultUrl = visibleDriver.fliggyLodgingResultUrl(
      {
        start_date: "2026-08-12",
        end_date: "2026-08-18",
        adults: 2,
        rooms: 1,
      },
      maafushiStrategy,
    );
    const parsedResultUrl = new URL(resultUrl);
    record(
      "Fliggy lodging builds the exact audited read-only result URL",
      parsedResultUrl.origin === "https://hotel.fliggy.com" &&
        parsedResultUrl.pathname === "/hotel_list3.htm" &&
        parsedResultUrl.searchParams.get("city") === "933081" &&
        parsedResultUrl.searchParams.get("cityName") === "马富士" &&
        parsedResultUrl.searchParams.get("checkIn") === "2026-08-12" &&
        parsedResultUrl.searchParams.get("checkOut") === "2026-08-18" &&
        parsedResultUrl.searchParams.get("aNum_1") === "2",
      resultUrl,
    );
    record(
      "Fliggy lodging refuses direct result URLs for unsupported room counts",
      visibleDriver.fliggyLodgingResultUrl(
        {
          start_date: "2026-08-12",
          end_date: "2026-08-18",
          adults: 2,
          rooms: 2,
        },
        maafushiStrategy,
      ) === null,
    );
    const fallback =
      visibleDriver.prefrozenLodgingDestinationFallback(
        "fliggy",
        "lodging",
        {
          destination: "Maafushi",
          start_date: "2026-08-12",
          end_date: "2026-08-18",
          adults: 2,
          rooms: 1,
          options: { expected_lodging_place_key: "maafushi" },
        },
        maafushiStrategy,
      );
    record(
      "Fliggy can bind a prefrozen city ID when the homepage exposes no suggestion menu",
      fallback.provider_destination === "马富士" &&
        fallback.provider_destination_id === "933081" &&
        fallback.evidence_scope ===
          "prefrozen_city_id_with_visible_dates_and_occupancy",
      JSON.stringify(fallback),
    );
    shell.remove();
  }

  {
    const identity = visibleDriver.suggestionIdentity(
      "qunar",
      "lodging",
      "Maafushi",
      "maafushi",
    );
    let match = null;
    let inputIdentity = null;
    let threw = false;
    try {
      match = visibleDriver.suggestionIdentityMatches(
        document.createTextNode("Maafushi"),
        identity,
      );
      inputIdentity = visibleDriver.auditedInputIdentity(
        document.createTextNode("not-an-input"),
      );
    } catch (_error) {
      threw = true;
    }
    record(
      "Qunar suggestion identity helpers reject non-Element nodes without throwing",
      threw === false &&
        match?.matched === false &&
        match?.evidence === "suggestion_candidate_not_element" &&
        inputIdentity === "",
      JSON.stringify({ match, inputIdentity, threw }),
    );
    const strategy = visibleDriver.qunarLodgingSearchStrategy(
      "qunar",
      "lodging",
      {
        destination: "Maafushi",
        options: { expected_lodging_place_key: "maafushi" },
      },
    );
    const resultUrl = visibleDriver.qunarLodgingResultUrl(
      {
        start_date: "2026-08-12",
        end_date: "2026-08-18",
        adults: 2,
        rooms: 1,
      },
      strategy,
    );
    const parsedResultUrl = new URL(resultUrl);
    record(
      "Qunar lodging builds the exact official read-only result URL",
      parsedResultUrl.origin === "https://hotel.qunar.com" &&
        parsedResultUrl.pathname === "/intl/search.jsp" &&
        parsedResultUrl.searchParams.get("toCity") === "马富施" &&
        parsedResultUrl.searchParams.get("cityurl") === "i-ka_maafushi" &&
        parsedResultUrl.searchParams.get("fromDate") === "2026-08-12" &&
        parsedResultUrl.searchParams.get("toDate") === "2026-08-18" &&
        parsedResultUrl.searchParams.get("from") === "globalhotelpages",
      resultUrl,
    );
    const resultSearch = document.createElement("section");
    resultSearch.className = "b_search_box_area";
    resultSearch.innerHTML = `
      <div class="b_search_box">
        <div class="b_city"><input class="textbox" value="马富施"></div>
        <div class="live input_container">
          <div class="check"><input class="inputText date" value="2026-08-12"></div>
          <div class="check"><input class="inputText date" value="2026-08-18"></div>
        </div>
        <div class="adult-children">
          <div class="title">每间人数</div>
          <div class="content">2成人 0儿童</div>
        </div>
      </div>`;
    document.body.append(resultSearch);
    const resultQuery = {
      destination: "Maafushi",
      start_date: "2026-08-12",
      end_date: "2026-08-18",
      adults: 2,
      rooms: 1,
      options: { expected_lodging_place_key: "maafushi" },
    };
    const confirmedResultReadback =
      visibleDriver.qunarLodgingResultQueryReadback(
        "qunar",
        "lodging",
        resultQuery,
        resultSearch,
        "https://hotel.qunar.com/city/i-ka_maafushi/",
      );
    resultSearch.querySelector(".adult-children .content").textContent =
      "1成人 0儿童";
    const mismatchedAdultReadback =
      visibleDriver.qunarLodgingResultQueryReadback(
        "qunar",
        "lodging",
        resultQuery,
        resultSearch,
        "https://hotel.qunar.com/city/i-ka_maafushi/",
      );
    record(
      "Qunar result page re-reads destination, dates and occupancy before extraction",
      confirmedResultReadback.confirmed === true &&
        confirmedResultReadback.readback_query.destination === "马富施" &&
        confirmedResultReadback.readback_query.start_date === "2026-08-12" &&
        confirmedResultReadback.readback_query.end_date === "2026-08-18" &&
        confirmedResultReadback.readback_query.adults === 2 &&
        confirmedResultReadback.readback_query.rooms === 1 &&
        mismatchedAdultReadback.confirmed === false &&
        mismatchedAdultReadback.gates.adults_confirmed === false,
      JSON.stringify({ confirmedResultReadback, mismatchedAdultReadback }),
    );
    resultSearch.remove();
    const fallback =
      visibleDriver.prefrozenLodgingDestinationFallback(
        "qunar",
        "lodging",
        {
          destination: "Maafushi",
          start_date: "2026-08-12",
          end_date: "2026-08-18",
          adults: 2,
          rooms: 1,
          options: { expected_lodging_place_key: "maafushi" },
        },
        strategy,
      );
    record(
      "Qunar can bind a prefrozen city slug when the homepage exposes no suggestion menu",
      fallback.provider_destination === "马富施" &&
        fallback.provider_destination_id === "i-ka_maafushi" &&
        fallback.evidence_scope ===
          "prefrozen_city_slug_with_visible_dates_and_occupancy",
      JSON.stringify(fallback),
    );
  }

  {
    const resultSearch = document.createElement("section");
    resultSearch.className = "b_search_box_area";
    resultSearch.innerHTML = `
      <div class="b_search_box">
        <div class="b_city"><input class="textbox" value="胡鲁马累岛"></div>
        <div class="live input_container">
          <div class="check"><input class="inputText date" value="2026-08-20"></div>
          <div class="check"><input class="inputText date" value="2026-08-25"></div>
        </div>
        <div class="adult-children">
          <div class="title">每间人数</div>
          <div class="content">2成人 0儿童</div>
        </div>
        <div class="keyword"><input class="textbox" value=""></div>
      </div>`;
    document.body.append(resultSearch);
    const resultQuery = {
      destination: "Hulhumale",
      start_date: "2026-08-20",
      end_date: "2026-08-25",
      adults: 2,
      rooms: 1,
      options: { expected_lodging_place_key: "hulhumale" },
    };
    const officialAliasReadback =
      visibleDriver.qunarLodgingResultQueryReadback(
        "qunar",
        "lodging",
        resultQuery,
        resultSearch,
        "https://hotel.qunar.com/city/i-hulhumale/",
      );
    const wrongPathReadback =
      visibleDriver.qunarLodgingResultQueryReadback(
        "qunar",
        "lodging",
        resultQuery,
        resultSearch,
        "https://hotel.qunar.com/city/i-ka_maafushi/",
      );
    record(
      "Qunar Hulhumale accepts only the official exact alias plus exact city path",
      officialAliasReadback.confirmed === true &&
        officialAliasReadback.readback_query.destination === "胡鲁马累岛" &&
        officialAliasReadback.gates.path_confirmed === true &&
        officialAliasReadback.gates.destination_confirmed === true &&
        officialAliasReadback.gates.conflicting_destination_control_absent ===
          true &&
        officialAliasReadback.evidence.destination_identity_scope ===
          "audited_exact_visible_label_plus_https_city_path_v1" &&
        wrongPathReadback.confirmed === false &&
        wrongPathReadback.gates.path_confirmed === false,
      JSON.stringify({ officialAliasReadback, wrongPathReadback }),
    );

    const destinationInput = resultSearch.querySelector(
      ".b_city input.textbox",
    );
    destinationInput.value = "马富施";
    const maafushiImpersonation =
      visibleDriver.qunarLodgingResultQueryReadback(
        "qunar",
        "lodging",
        resultQuery,
        resultSearch,
        "https://hotel.qunar.com/city/i-hulhumale/",
      );
    record(
      "Qunar Hulhumale path cannot promote a Maafushi visible destination",
      maafushiImpersonation.confirmed === false &&
        maafushiImpersonation.gates.path_confirmed === true &&
        maafushiImpersonation.gates.destination_control_unambiguous === false &&
        maafushiImpersonation.gates.conflicting_destination_control_absent ===
          false,
      JSON.stringify(maafushiImpersonation),
    );

    destinationInput.value = "胡鲁马累岛";
    resultSearch.querySelector(".keyword input.textbox").value = "新加坡";
    const conflictingDestinations =
      visibleDriver.qunarLodgingResultQueryReadback(
        "qunar",
        "lodging",
        resultQuery,
        resultSearch,
        "https://hotel.qunar.com/city/i-hulhumale/",
      );
    record(
      "Qunar rejects a second conflicting non-empty textbox even when one identity matches the path",
      conflictingDestinations.confirmed === false &&
        conflictingDestinations.gates.path_confirmed === true &&
        conflictingDestinations.gates.destination_control_unambiguous ===
          false &&
        conflictingDestinations.gates.conflicting_destination_control_absent ===
          false,
      JSON.stringify(conflictingDestinations),
    );
    resultSearch.remove();
  }

  {
    const confirmedEmpty = await parser.extractPage(
      "qunar",
      "lodging",
      new DOMParser().parseFromString(
        fixtures["qunar-lodging-confirmed-empty"],
        "text/html",
      ),
      "https://hotel.qunar.com/city/i-ka_maafushi/#fromDate=2026-08-23&toDate=2026-08-30",
      new Date("2026-07-30T12:00:00Z"),
      {
        ...flightQuery,
        origin: null,
        options: {
          expected_lodging_place_key: "马富施",
          expected_package_area: "destination_island",
          segment: "full",
        },
      },
      {
        ...fixtureDriver,
        provider: "qunar",
        confirmation_scope: "confirmed_visible_search",
        result_query_readback_confirmed: true,
      },
    );
    const details = confirmedEmpty.failure && confirmedEmpty.failure.details;
    const receipt = details && details.inventory_receipt;
    const validation = await parser.validateLodgingInventoryReceipt(
      receipt,
      details && details.inventory_receipt_sha256,
    );
    record(
      "Qunar audited visible zero-result copy becomes confirmed empty inventory",
      confirmedEmpty.state === "failed" &&
        confirmedEmpty.failure.code === "no_inventory" &&
        confirmedEmpty.quotes.length === 0 &&
        details.inventory_result_state === "confirmed_empty" &&
        details.confirmed_exhaustive === true &&
        details.scanned_count === 0 &&
        details.candidate_summaries.length === 0 &&
        details.capture_code ===
          "audited_qunar_explicit_empty_inventory" &&
        receipt.provider === "qunar" &&
        receipt.state === "confirmed_empty" &&
        receipt.scanned_count === 0 &&
        receipt.candidate_summaries.length === 0 &&
        receipt.explicit_empty_evidence.contract_version ===
          "qunar-visible-zero-inventory-v1" &&
        receipt.explicit_empty_evidence.result_count_text ===
          "共 0 家酒店满足条件" &&
        receipt.explicit_empty_evidence.empty_message ===
          "很抱歉，没有找到相关的酒店" &&
        validation.valid === true,
      JSON.stringify(confirmedEmpty),
    );
  }

  {
    const shell = mountFixture("qunar-lodging-suggestion");
    const input = shell.querySelector("#interForm input.textbox");
    const option = shell.querySelector("tr.item");
    const events = [];
    for (const type of ["pointerdown", "mousedown", "mouseup", "click"]) {
      option.addEventListener(type, () => events.push(type));
    }
    option.addEventListener("click", () => {
      const replacement = input.cloneNode(true);
      replacement.value = "马富施";
      input.replaceWith(replacement);
    });
    const selected = await visibleDriver.selectVisibleSuggestion(
      "qunar",
      "lodging",
      "Maafushi",
      null,
      input,
      "maafushi",
    );
    record(
      "Qunar dispatches the full pointer click and confirms exact visible identity",
      selected.selected === true &&
        JSON.stringify(events) ===
          JSON.stringify(["pointerdown", "mousedown", "mouseup", "click"]) &&
        selected.readback_value === "马富施" &&
        shell.querySelector("#interForm input.textbox").value === "马富施" &&
        selected.identity_evidence ===
          "audited_qunar_city_destination_row" &&
        !visibleDriver.auditedInputIdentity(
          shell.querySelector("#interForm input.textbox"),
        ).includes("i-ka_maafushi"),
      JSON.stringify({ selected, events }),
    );
    shell.remove();
  }

  {
    const shell = mountFixture("qunar-lodging-zero-rect-inner-label");
    const input = shell.querySelector("#interForm input.textbox");
    const row = shell.querySelector(
      "[data-tripchord-suggestion-row='maafushi']",
    );
    const innerLabel = row.querySelector("span.item");
    const events = [];
    for (const type of ["pointerdown", "mousedown", "mouseup", "click"]) {
      row.addEventListener(type, () => events.push(type));
    }
    row.addEventListener("click", () => {
      input.value = "Maafushi";
    });
    const selected = await visibleDriver.selectVisibleSuggestion(
      "qunar",
      "lodging",
      "Maafushi",
      null,
      input,
      "maafushi",
    );
    record(
      "Qunar exact zero-rect inner label uses only its visible audited row ancestor",
      innerLabel.getBoundingClientRect().width === 0 &&
        selected.selected === true &&
        selected.identity_evidence ===
          "audited_qunar_exact_city_destination" &&
        selected.readback_value === "Maafushi" &&
        JSON.stringify(events) ===
          JSON.stringify(["pointerdown", "mousedown", "mouseup", "click"]),
      JSON.stringify({ selected, events }),
    );
    shell.remove();
  }

  {
    const shell = mountFixture("qunar-lodging-zero-rect-inner-label");
    const input = shell.querySelector("#interForm input.textbox");
    const row = shell.querySelector(
      "[data-tripchord-suggestion-row='maafushi']",
    );
    row.addEventListener("click", () => {
      shell.querySelector(".m-suggest-container").style.display = "none";
    });
    const selected = await visibleDriver.selectVisibleSuggestion(
      "qunar",
      "lodging",
      "Maafushi",
      null,
      input,
      "maafushi",
    );
    record(
      "Qunar exact inner label without exact destination input readback remains rejected",
      selected.selected === false &&
        selected.identity_evidence === "selected_city_readback_unconfirmed" &&
        input.value === "新加坡",
      JSON.stringify(selected),
    );
    shell.remove();
  }

  {
    const shell = mountFixture("qunar-lodging-suggestion");
    const input = shell.querySelector("#interForm input.textbox");
    const option = shell.querySelector("tr.item");
    input.value = "Maafushi";
    option.addEventListener("click", () => {
      shell.querySelector(".m-suggest-container").style.display = "none";
    });
    const selected = await visibleDriver.selectVisibleSuggestion(
      "qunar",
      "lodging",
      "Maafushi",
      null,
      input,
      "maafushi",
    );
    record(
      "Qunar accepts exact English readback only after the audited option closes",
      selected.selected === true &&
        selected.readback_value === "Maafushi" &&
        selected.identity_evidence ===
          "audited_qunar_city_destination_row",
      JSON.stringify(selected),
    );
    shell.remove();
  }

  {
    const shell = mountFixture("qunar-lodging-suggestion");
    const input = shell.querySelector("#interForm input.textbox");
    input.value = "Maafushi";
    const selected = await visibleDriver.selectVisibleSuggestion(
      "qunar",
      "lodging",
      "Maafushi",
      null,
      input,
      "maafushi",
    );
    record(
      "Qunar typed English without native option dismissal remains rejected",
      selected.selected === false &&
        selected.identity_evidence === "selected_city_readback_unconfirmed" &&
        selected.readback_value === "Maafushi",
      JSON.stringify(selected),
    );
    shell.remove();
  }

  {
    const shell = mountFixture("qunar-lodging-suggestion-no-readback");
    const input = shell.querySelector("#interForm input.textbox");
    const selected = await visibleDriver.selectVisibleSuggestion(
      "qunar",
      "lodging",
      "Maafushi",
      null,
      input,
      "maafushi",
    );
    record(
      "Qunar click without city input/state mutation is rejected",
      selected.selected === false &&
        selected.identity_evidence === "selected_city_readback_unconfirmed" &&
        input.value === "新加坡",
      JSON.stringify(selected),
    );
    shell.remove();
  }

  {
    const shell = mountFixture("qunar-lodging-suggestion");
    const input = shell.querySelector("#interForm input.textbox");
    shell.querySelector("tr.item").addEventListener("click", () => {
      input.value = "卡夫环礁";
    });
    const selected = await visibleDriver.selectVisibleSuggestion(
      "qunar",
      "lodging",
      "Maafushi",
      null,
      input,
      "maafushi",
    );
    record(
      "Qunar area-only input readback cannot impersonate the selected city",
      selected.selected === false &&
        selected.identity_evidence === "selected_city_readback_unconfirmed" &&
        input.value === "卡夫环礁",
      JSON.stringify(selected),
    );
    shell.remove();
  }

  {
    const form = document.createElement("form");
    form.id = "interForm";
    form.innerHTML = `
      <input class="textbox" aria-label="目的地" value="新加坡">
      <div class="m-suggest-container">
        <table class="suggest-list"><tbody>
          <tr class="item" data-test-area="wrong">
            <td>马富士岛</td>
            <td>Maafushi, Dhaalu Atoll, Maldives</td>
          </tr>
          <tr class="item" data-test-area="right">
            <td>马富施</td>
            <td>Maafushi, Kaafu Atoll, Maldives</td>
          </tr>
        </tbody></table>
      </div>`;
    document.body.append(form);
    const input = form.querySelector("input");
    const clicked = [];
    form.querySelector("[data-test-area='wrong']").addEventListener(
      "click",
      () => clicked.push("wrong"),
    );
    form.querySelector("[data-test-area='right']").addEventListener(
      "click",
      () => {
        clicked.push("right");
        input.value = "马富施";
      },
    );
    const selected = await visibleDriver.selectVisibleSuggestion(
      "qunar",
      "lodging",
      "Maafushi",
      null,
      input,
      "maafushi",
    );
    record(
      "Qunar rejects Dhaalu and selects only the audited Kaafu row",
      selected.selected === true &&
        JSON.stringify(clicked) === JSON.stringify(["right"]) &&
        input.value === "马富施",
      JSON.stringify({ selected, clicked }),
    );
    form.remove();
  }

  {
    const form = document.createElement("form");
    form.innerHTML = `
      <input data-testid="international-city-input" value="Maafushi">
      <div data-testid="search-city-dropdown" role="listbox">
        <button
          type="button"
          role="option"
          data-testid="search-city-马弗施瓦鲁"
          data-agent-id="search-city-马弗施瓦鲁"
          data-agent-type="city-option"
        >马富士,马尔代夫,马尔代夫</button>
      </div>`;
    document.body.append(form);
    const input = form.querySelector("input");
    const selected = await visibleDriver.selectVisibleSuggestion(
      "fliggy",
      "lodging",
      "Maafushi",
      null,
      input,
      "maafushi",
    );
    record(
      "Fliggy rejects an unaudited semantic city option",
      selected.selected === false &&
        input.value === "Maafushi",
      JSON.stringify(selected),
    );
    form.remove();
  }

  {
    const root = document.createElement("form");
    const surface = document.createElement("div");
    surface.setAttribute("role", "button");
    surface.innerHTML =
      `<i class="hotel_desktop_ctrip- ic ic-user ic_user"></i>` +
      `<span>1间, 2成人, 0儿童</span>`;
    document.body.append(root, surface);
    const adults = await visibleDriver.setVisibleCount(
      "ctrip",
      "lodging",
      "adults",
      2,
      root,
    );
    const rooms = await visibleDriver.setVisibleCount(
      "ctrip",
      "lodging",
      "rooms",
      1,
      root,
    );
    record(
      "Ctrip reads exact party counts from its audited global user surface",
      adults.ok === true &&
        rooms.ok === true &&
        adults.evidence === "audited_visible_occupancy_surface" &&
        rooms.evidence === "audited_visible_occupancy_surface",
      JSON.stringify({ adults, rooms }),
    );
    surface.remove();
    const decoy = document.createElement("div");
    decoy.setAttribute("role", "button");
    decoy.textContent = "1间, 2成人, 0儿童";
    document.body.append(decoy);
    const rejected = await visibleDriver.setVisibleCount(
      "ctrip",
      "lodging",
      "adults",
      2,
      root,
    );
    record(
      "Ctrip does not accept same-text decoys without its user icon",
      rejected.ok === false,
      JSON.stringify(rejected),
    );
    root.remove();
    decoy.remove();
  }

  {
    const qunarForm = document.createElement("form");
    qunarForm.id = "interForm";
    qunarForm.innerHTML =
      `<div class="adult-children">每间人数 2成人 0儿童</div>`;
    document.body.append(qunarForm);
    const adults = await visibleDriver.setVisibleCount(
      "qunar",
      "lodging",
      "adults",
      2,
      document.createElement("form"),
    );
    const oneRoom = await visibleDriver.setVisibleCount(
      "qunar",
      "lodging",
      "rooms",
      1,
      document.createElement("form"),
    );
    const twoRooms = await visibleDriver.setVisibleCount(
      "qunar",
      "lodging",
      "rooms",
      2,
      document.createElement("form"),
    );
    record(
      "Qunar confirms adults and only an implicit single room from audited occupancy",
      adults.ok === true &&
        oneRoom.ok === true &&
        oneRoom.evidence === "implicit_single_room_surface" &&
        twoRooms.ok === false &&
        twoRooms.reason === "rooms_above_provider_single_room_surface",
      JSON.stringify({ adults, oneRoom, twoRooms }),
    );
    qunarForm.remove();
  }

  {
    const fliggySurface = document.createElement("div");
    fliggySurface.setAttribute(
      "data-agent-id",
      "international-adult-select",
    );
    fliggySurface.setAttribute(
      "data-agent-type",
      "adult-count-select",
    );
    fliggySurface.textContent = "成人2位";
    document.body.append(fliggySurface);
    const root = document.createElement("form");
    const adults = await visibleDriver.setVisibleCount(
      "fliggy",
      "lodging",
      "adults",
      2,
      root,
    );
    const oneRoom = await visibleDriver.setVisibleCount(
      "fliggy",
      "lodging",
      "rooms",
      1,
      root,
    );
    record(
      "Fliggy confirms exact adults and an implicit single room from audited occupancy",
      adults.ok === true &&
        oneRoom.ok === true &&
        oneRoom.evidence === "implicit_single_room_surface",
      JSON.stringify({ adults, oneRoom }),
    );
    fliggySurface.remove();
  }

  {
    const form = document.createElement("form");
    form.innerHTML = `<div class="adult-children">2成人，0儿童</div>`;
    document.body.append(form);
    const missingRoom = await visibleDriver.setVisibleCount(
      "qunar",
      "lodging",
      "rooms",
      1,
      form,
    );
    record(
      "implicit one-room default without visible room evidence is rejected",
      missingRoom.ok === false &&
        missingRoom.reason === "rooms_visible_default_unconfirmed",
      JSON.stringify(missingRoom),
    );
    form.querySelector(".adult-children").textContent =
      "1间，2成人，0儿童";
    const visibleRoom = await visibleDriver.setVisibleCount(
      "qunar",
      "lodging",
      "rooms",
      1,
      form,
    );
    record(
      "visible one-room default is accepted",
      visibleRoom.ok === true &&
        visibleRoom.readback === 1 &&
        visibleRoom.evidence === "visible_occupancy_default",
      JSON.stringify(visibleRoom),
    );
    form.remove();
  }

  {
    const { form } = searchForm("lodging", { includeRooms: false });
    const prepared = await visibleDriver.prepareSearch("ctrip", "lodging", {
      destination: "马累",
      start_date: "2026-08-23",
      end_date: "2026-08-30",
      adults: 2,
      rooms: 1,
    });
    record(
      "missing visible room control is rejected with bounded diagnostics",
      prepared.prepared === false &&
        prepared.missing.some((item) => String(item).includes("rooms")) &&
        prepared.controls.length <= 12,
      JSON.stringify(prepared),
    );
    form.remove();
  }

  const flightQuery = {
    origin: "杭州",
    destination: "马累",
    start_date: "2026-08-23",
    end_date: "2026-08-30",
    adults: 2,
    rooms: 1,
    currency: "CNY",
    origin_code: "HGH",
    destination_code: "MLE",
    search_url: null,
    options: {},
  };
  const fixtureDriver = {
    mode: "fixture",
    triggered: true,
    confirmed_query: {
      origin: "杭州",
      destination: "马累",
      start_date: "2026-08-23",
      end_date: "2026-08-30",
      adults: 2,
      rooms: 1,
    },
    confirmation_scope: "fixture",
    party_availability_confirmed: true,
    action_trace: [
      {
        action: "search",
        provider: "ctrip",
        evidence: "fixture_exact_round_trip_search",
      },
    ],
  };

  {
    const alternate = await parser.extractPage(
      "fliggy",
      "flight",
      new DOMParser().parseFromString(
        fixtures["fliggy-flight-alternate-origin-only"],
        "text/html",
      ),
      "https://sijipiao.fliggy.com/ie/flight_search_result.htm",
      new Date("2026-07-30T12:00:00Z"),
      flightQuery,
      {
        ...fixtureDriver,
        mode: "search_url",
        confirmation_scope: "trusted_exact_search_url",
        readback_query: {
          origin_code: "HGH",
          destination_code: "MLE",
          start_date: flightQuery.start_date,
          end_date: flightQuery.end_date,
        },
        party_availability_confirmed: false,
        action_trace: [
          {
            action: "search",
            provider: "fliggy",
            evidence: "fixture_exact_round_trip_search",
          },
        ],
      },
    );
    const diagnostic =
      alternate.failure &&
      alternate.failure.details.flight_diagnostic;
    const observed = diagnostic && diagnostic.observed_routes[0];
    const alternateReceipt =
      alternate.failure &&
      alternate.failure.details.flight_search_receipt;
    record(
      "Fliggy alternate-origin cards remain typed non-quotes",
      alternate.state === "failed" &&
        alternate.failure.code === "extraction_error" &&
        Array.isArray(alternate.quotes) &&
        alternate.quotes.length === 0 &&
        diagnostic.outcome === "alternate_origin_only" &&
        diagnostic.stage === "alternate_origin_suggestions" &&
        diagnostic.counts.nearby_item_count === 1 &&
        diagnostic.counts.requested_origin_match_count === 0 &&
        observed.origin_label === "上海" &&
        observed.destination_label === "马累" &&
        observed.observed_origin_code === null &&
        observed.origin_matches_requested === false &&
        observed.destination_matches_requested === true &&
        alternateReceipt.state === "bounded_no_exact_quote" &&
        alternateReceipt.confirmed_query.origin_code === "HGH" &&
        alternateReceipt.confirmed_query.destination_code === "MLE" &&
        alternateReceipt.candidate_summaries[0].route_evidence.includes(
          "上海→马累",
        ) &&
        alternateReceipt.candidate_summaries[0].price_classification ===
          "no_visible_price" &&
        alternateReceipt.candidate_summaries[0].amount === null,
      JSON.stringify(alternate),
    );
  }

  {
    const emptyOutbound = await parser.extractPage(
      "ctrip",
      "flight",
      new DOMParser().parseFromString(
        fixtures["ctrip-flight-outbound-empty"],
        "text/html",
      ),
      "https://flights.ctrip.com/results",
      new Date("2026-07-30T12:00:00Z"),
      flightQuery,
      fixtureDriver,
    );
    const diagnostic =
      emptyOutbound.failure &&
      emptyOutbound.failure.details.flight_diagnostic;
    record(
      "Ctrip empty outbound stage is distinguished from unknown DOM drift",
      emptyOutbound.state === "failed" &&
        emptyOutbound.failure.code === "dom_drift" &&
        Array.isArray(emptyOutbound.quotes) &&
        emptyOutbound.quotes.length === 0 &&
        diagnostic.outcome ===
          "outbound_results_empty_or_unavailable" &&
        diagnostic.stage === "outbound_result_discovery" &&
        diagnostic.counts.outbound_stage_anchor_count === 1 &&
        diagnostic.counts.visible_price_anchor_count === 0 &&
        diagnostic.stage_evidence[0].class === "segment_tab active",
      JSON.stringify(emptyOutbound),
    );
  }

  {
    const splitPrice = await parser.extractPage(
      "qunar",
      "flight",
      new DOMParser().parseFromString(
        fixtures["qunar-flight-split-price-nodes"],
        "text/html",
      ),
      "https://flight.qunar.com/results",
      new Date("2026-07-30T12:00:00Z"),
      flightQuery,
      {
        ...fixtureDriver,
        mode: "search_url",
        triggered: true,
        confirmation_scope: "trusted_exact_search_url",
        confirmed_query: {
          origin: flightQuery.origin,
          destination: flightQuery.destination,
          start_date: flightQuery.start_date,
          end_date: flightQuery.end_date,
          adults: flightQuery.adults,
        },
        readback_query: {
          origin: flightQuery.origin,
          destination: flightQuery.destination,
          start_date: flightQuery.start_date,
          end_date: flightQuery.end_date,
          adults: flightQuery.adults,
        },
        url_confirmed_fields: [
          "origin",
          "destination",
          "start_date",
          "end_date",
          "adults",
        ],
        party_availability_confirmed: true,
        action_trace: [
          {
            action: "search",
            provider: "qunar",
            evidence: "fixture_exact_round_trip_search",
          },
        ],
      },
    );
    const diagnostic =
      splitPrice.failure &&
      splitPrice.failure.details.flight_diagnostic;
    const counts = diagnostic && diagnostic.counts;
    record(
      "Qunar split price digits are diagnosed but never assembled",
      splitPrice.state === "failed" &&
        splitPrice.failure.code === "extraction_error" &&
        Array.isArray(splitPrice.quotes) &&
        splitPrice.quotes.length === 0 &&
        diagnostic.outcome === "price_structure_incomplete" &&
        diagnostic.stage === "price_structure_validation" &&
        counts.combination_card_count === 1 &&
        counts.exactly_two_trip_card_count === 1 &&
        counts.parsed_outbound_leg_count === 1 &&
        counts.parsed_return_leg_count === 1 &&
        counts.safe_price_evidence_count === 0 &&
        counts.complete_currency_amount_fragment_count === 0 &&
        counts.split_price_structure_count > 0 &&
        !JSON.stringify(splitPrice).includes('"amount":4'),
      JSON.stringify(splitPrice),
    );
  }

  {
    const root = new DOMParser().parseFromString(
      fixtures["qunar-flight-consistent-digit-titles"],
      "text/html",
    );
    const priceEvidence = parser.qunarTitledDigitPriceEvidence(
      root.querySelector(".col-price"),
    );
    const conflictingRoot = new DOMParser().parseFromString(
      fixtures["qunar-flight-consistent-digit-titles"],
      "text/html",
    );
    conflictingRoot.querySelectorAll(".col-price i")[1]
      .setAttribute("title", "7557");
    const duplicateCurrencyRoot = new DOMParser().parseFromString(
      fixtures["qunar-flight-consistent-digit-titles"],
      "text/html",
    );
    duplicateCurrencyRoot.querySelector(".col-price")
      .append(duplicateCurrencyRoot.createTextNode(" ¥"));
    const output = await parser.extractPage(
      "qunar",
      "flight",
      root,
      "https://flight.qunar.com/results",
      new Date("2026-07-30T12:00:00Z"),
      flightQuery,
      {
        ...fixtureDriver,
        mode: "search_url",
        triggered: true,
        confirmation_scope: "trusted_exact_search_url",
        confirmed_query: {
          origin: flightQuery.origin,
          destination: flightQuery.destination,
          start_date: flightQuery.start_date,
          end_date: flightQuery.end_date,
          adults: flightQuery.adults,
        },
        readback_query: {
          origin: flightQuery.origin,
          destination: flightQuery.destination,
          start_date: flightQuery.start_date,
          end_date: flightQuery.end_date,
          adults: flightQuery.adults,
        },
        url_confirmed_fields: [
          "origin",
          "destination",
          "start_date",
          "end_date",
          "adults",
        ],
        party_availability_confirmed: true,
        action_trace: [
          {
            action: "search",
            provider: "qunar",
            evidence: "fixture_exact_round_trip_search",
          },
        ],
      },
    );
    record(
      "Qunar stable title digits plus exact visible party context recover one total-party quote without booking",
      priceEvidence.price_text === "含税总价 ¥6600" &&
        priceEvidence.amount_text === "6600" &&
        priceEvidence.digit_leaf_count === 4 &&
        priceEvidence.evidence_source ===
          "consistent_visible_digit_title" &&
        parser.qunarTitledDigitPriceEvidence(
          conflictingRoot.querySelector(".col-price"),
        ) === null &&
        parser.qunarTitledDigitPriceEvidence(
          duplicateCurrencyRoot.querySelector(".col-price"),
        ) === null &&
        parser.flightPriceContract(priceEvidence.price_text).valid === false &&
        output.state === "succeeded" &&
        output.quotes.length === 1 &&
        output.quotes[0].amount === 6600 &&
        output.quotes[0].price_basis === "total_party" &&
        output.quotes[0].details.availability_evidence.includes(
          "exact_party_search_context",
        ) &&
        output.quotes[0].details.selection_evidence.includes(
          "未点击订票",
        ),
      JSON.stringify({ priceEvidence, output }),
    );
  }

  {
    const query = {
      ...flightQuery,
      start_date: "2026-08-12",
      end_date: "2026-08-18",
    };
    const output = await parser.extractPage(
      "qunar",
      "flight",
      new DOMParser().parseFromString(
        fixtures["qunar-flight-title-price-no-availability"],
        "text/html",
      ),
      "https://flight.qunar.com/site/interroundtrip_compare.htm",
      new Date("2026-07-31T00:00:00Z"),
      query,
      {
        ...fixtureDriver,
        confirmed_query: {
          ...fixtureDriver.confirmed_query,
          start_date: query.start_date,
          end_date: query.end_date,
        },
        action_trace: [
          {
            action: "search",
            provider: "qunar",
            evidence: "fixture_exact_round_trip_search",
          },
        ],
      },
    );
    const diagnostic =
      output.failure &&
      output.failure.details &&
      output.failure.details.flight_diagnostic;
    record(
      "Qunar title-backed amount keeps price basis and availability as independent blockers",
      output.state === "failed" &&
        output.quotes.length === 0 &&
        diagnostic.counts.matching_round_trip_route_count === 1 &&
        diagnostic.counts.safe_price_evidence_count === 1 &&
        diagnostic.counts.valid_flight_price_contract_count === 0 &&
        diagnostic.counts.explicit_tax_evidence_count === 1 &&
        diagnostic.counts.availability_evidence_count === 0 &&
        diagnostic.blocking_contract_fields.includes("price_basis") &&
        diagnostic.blocking_contract_fields.includes("availability") &&
        !diagnostic.blocking_contract_fields.includes("round_trip_route"),
      JSON.stringify(output),
    );
  }

  record(
    "explicit checked baggage",
    parser.checkedBaggageKg("每位成人免费托运行李 23kg") === 23,
  );

  record(
    "flight price basis without visible unit stays unknown",
    parser.priceBasis("flight", "含税 ¥5,100") === "unknown",
  );

  const unknownLodgingBasis = await parser.extractPage(
    "ctrip",
    "lodging",
    new DOMParser().parseFromString(
      fixtures["ctrip-lodging-no-price-unit"],
      "text/html",
    ),
    "https://hotels.ctrip.com/results",
    new Date("2026-07-30T12:00:00Z"),
    { ...flightQuery, origin: null },
    fixtureDriver,
  );
  record(
    "Ctrip lodging card without a visible price unit is rejected",
    unknownLodgingBasis.state === "failed" &&
      unknownLodgingBasis.failure.code === "dom_drift" &&
      Array.isArray(unknownLodgingBasis.quotes) &&
      unknownLodgingBasis.quotes.length === 0,
    JSON.stringify(unknownLodgingBasis),
  );

  const startingPerNightLodging = await parser.extractPage(
    "ctrip",
    "lodging",
    new DOMParser().parseFromString(
      fixtures["ctrip-lodging-starting-per-night"],
      "text/html",
    ),
    "https://hotels.ctrip.com/results",
    new Date("2026-07-30T12:00:00Z"),
    { ...flightQuery, origin: null },
    fixtureDriver,
  );
  record(
    "Ctrip lodging starting-per-night price never becomes a BrowserQuote",
    parser.priceBasis("lodging", "含税价 ¥1,171 起/晚") === "per_night" &&
      parser.lodgingPriceFinality("含税价 ¥1,171 起/晚") ===
        "starting_or_estimated" &&
      startingPerNightLodging.state === "failed" &&
      startingPerNightLodging.failure.code === "dom_drift" &&
      (
        !Array.isArray(startingPerNightLodging.quotes) ||
        startingPerNightLodging.quotes.length === 0
      ),
    JSON.stringify(startingPerNightLodging),
  );

  for (const provider of ["ctrip", "fliggy", "qunar"]) {
    const receiptPageUrl = {
      ctrip: "https://hotels.ctrip.com/hotels/list?tracking=private#secret",
      fliggy:
        "https://hotel.fliggy.com/hotel_list3.htm?tracking=private#secret",
      qunar: "https://hotel.qunar.com/global/?tracking=private#secret",
    }[provider];
    const bounded = await parser.extractPage(
      provider,
      "lodging",
      new DOMParser().parseFromString(
        fixtures[`${provider}-lodging-bounded-no-exact`],
        "text/html",
      ),
      receiptPageUrl,
      new Date("2026-07-30T12:00:00Z"),
      {
        ...flightQuery,
        origin: null,
        options: {
          expected_lodging_place_key: "马富施",
          expected_package_area: "destination_island",
          segment: "full",
        },
      },
      {
        ...fixtureDriver,
        confirmation_scope: "confirmed_visible_search",
      },
    );
    const details = bounded.failure && bounded.failure.details;
    const receipt = details && details.inventory_receipt;
    const receiptValidation =
      await parser.validateLodgingInventoryReceipt(
        receipt,
        details && details.inventory_receipt_sha256,
      );
    const serialized = JSON.stringify(bounded);
    record(
      `${provider} exact lodging scan reports bounded non-exhaustive inventory`,
      bounded.state === "failed" &&
        bounded.failure.code === "dom_drift" &&
        Array.isArray(bounded.quotes) &&
        bounded.quotes.length === 0 &&
        details.inventory_result_state === "bounded_no_exact_quote" &&
        details.inventory_result_state !== "confirmed_empty" &&
        details.confirmed_exhaustive === false &&
        details.scanned_count === 1 &&
        Array.isArray(details.candidate_summaries) &&
        details.candidate_summaries.length === 1 &&
        details.capture_code ===
          "bounded_lodging_candidates_no_exact_quote" &&
        receiptValidation.valid === true &&
        receipt.schema_version ===
          "tripchord-lodging-inventory-receipt-v1" &&
        receipt.parser_version === parser.PARSER_VERSION &&
        receipt.provider === provider &&
        receipt.state === "bounded_no_exact_quote" &&
        receipt.confirmation_scope === "confirmed_visible_search" &&
        receipt.scan_limit === 12 &&
        receipt.scanned_count === 1 &&
        receipt.candidate_summaries.length === 1 &&
        receipt.explicit_empty_evidence === null &&
        receipt.captured_at === bounded.failure.captured_at &&
        receipt.page_url === bounded.failure.page_url &&
        !receipt.page_url.includes("?") &&
        !receipt.page_url.includes("#") &&
        receipt.confirmed_query.destination === "马累" &&
        receipt.confirmed_query.start_date === "2026-08-23" &&
        receipt.confirmed_query.end_date === "2026-08-30" &&
        receipt.confirmed_query.adults === 2 &&
        receipt.confirmed_query.rooms === 1 &&
        receipt.confirmed_query.options.expected_lodging_place_key ===
          "maafushi" &&
        receipt.confirmed_query.options.expected_package_area ===
          "destination_island" &&
        receipt.confirmed_query.options.segment === "full" &&
        details.scanned_count === receipt.scanned_count &&
        JSON.stringify(details.candidate_summaries) ===
          JSON.stringify(receipt.candidate_summaries) &&
        /^[a-f0-9]{64}$/.test(details.inventory_receipt_sha256) &&
        !serialized.includes("owner@example.com") &&
        !serialized.includes("13912345678") &&
        !serialized.includes("123456789012") &&
        !serialized.includes("https://private.example"),
      serialized,
    );
  }

  {
    const unconfirmed = await parser.extractPage(
      "fliggy",
      "lodging",
      new DOMParser().parseFromString(
        fixtures["fliggy-lodging-bounded-no-exact"],
        "text/html",
      ),
      "https://fixture.fliggy.com/results",
      new Date("2026-07-30T12:00:00Z"),
      { ...flightQuery, origin: null },
      {
        ...fixtureDriver,
        confirmation_scope: "provider_url_only_unverified",
      },
    );
    const zeroCandidate = await parser.extractPage(
      "ctrip",
      "lodging",
      new DOMParser().parseFromString(
        "<main><p>当前搜索条件下暂无可预订酒店</p></main>",
        "text/html",
      ),
      "https://hotels.ctrip.com/hotels/list",
      new Date("2026-07-30T12:00:00Z"),
      { ...flightQuery, origin: null },
      {
        ...fixtureDriver,
        confirmation_scope: "confirmed_visible_search",
      },
    );
    const scanBudgetRoot = new DOMParser().parseFromString(
      `<main>${"<article class='hotel-card' hidden></article>".repeat(3001)}</main>`,
      "text/html",
    );
    const scanBudgetExceeded = await parser.extractPage(
      "ctrip",
      "lodging",
      scanBudgetRoot,
      "https://hotels.ctrip.com/hotels/list",
      new Date("2026-07-30T12:00:00Z"),
      { ...flightQuery, origin: null },
      {
        ...fixtureDriver,
        confirmation_scope: "confirmed_visible_search",
      },
    );
    record(
      "unconfirmed, zero-candidate and scan-exhausted lodging never forge a receipt",
      unconfirmed.state === "failed" &&
        unconfirmed.failure.code === "dom_drift" &&
        !Object.prototype.hasOwnProperty.call(
          unconfirmed.failure.details,
          "inventory_result_state",
        ) &&
        !Object.prototype.hasOwnProperty.call(
          unconfirmed.failure.details,
          "confirmed_exhaustive",
        ) &&
        zeroCandidate.state === "failed" &&
        zeroCandidate.failure.code === "dom_drift" &&
        !Object.prototype.hasOwnProperty.call(
          zeroCandidate.failure.details,
          "inventory_receipt",
        ) &&
        scanBudgetExceeded.state === "failed" &&
        scanBudgetExceeded.failure.code === "extraction_error" &&
        !Object.prototype.hasOwnProperty.call(
          scanBudgetExceeded.failure.details,
          "inventory_result_state",
        ) &&
        !Object.prototype.hasOwnProperty.call(
          scanBudgetExceeded.failure.details,
          "inventory_receipt",
        ),
      JSON.stringify({
        unconfirmed,
        zeroCandidate,
        scanBudgetExceeded,
      }),
    );
  }

  const ctripDetailUrl =
    "https://hotels.ctrip.com/hotels/detail/?" +
    "hotelId=6210622&checkIn=2026-08-01&checkOut=2026-08-05&adult=2&crn=1";
  const ctripDetailQuery = {
    ...flightQuery,
    origin: null,
    destination: "康迪马岛",
    start_date: "2026-08-01",
    end_date: "2026-08-05",
    origin_code: null,
    destination_code: null,
    options: {
      segment: "full",
      expected_package_area: "destination_island",
    },
  };
  const ctripDetailDriver = {
    ...fixtureDriver,
    confirmed_query: {
      destination: "康迪马岛",
      start_date: "2026-08-01",
      end_date: "2026-08-05",
      adults: 2,
      rooms: 1,
    },
  };
  const ctripExactDetail = await parser.extractPage(
    "ctrip",
    "lodging",
    new DOMParser().parseFromString(
      fixtures["ctrip-lodging-detail-exact"],
      "text/html",
    ),
    ctripDetailUrl,
    new Date("2026-07-30T12:00:00Z"),
    ctripDetailQuery,
    ctripDetailDriver,
  );
  {
    const quote = ctripExactDetail.quotes && ctripExactDetail.quotes[0];
    const sealed = quote && JSON.parse(quote.visible_evidence);
    record(
      "Ctrip detail uses atomic tax-inclusive nightly amount, never pre-tax display price",
      ctripExactDetail.state === "succeeded" &&
        ctripExactDetail.quotes.length === 1 &&
        quote.amount === 1669 &&
        quote.price_basis === "per_night" &&
        quote.taxes_included === true &&
        quote.page_url === ctripDetailUrl &&
        quote.details.page_url === ctripDetailUrl &&
        quote.details.property_id === "6210622" &&
        quote.details.property_name ===
          "坎迪玛马尔代夫酒店(Kandima Maldives)" &&
        quote.details.room_text === "天空一室房" &&
        quote.details.breakfast_text === "2份早餐" &&
        quote.details.breakfast_included === true &&
        quote.details.cancellation_text === "不可取消" &&
        quote.details.availability === "available" &&
        quote.details.availability_text === "预订" &&
        quote.details.area === "destination_island" &&
        quote.details.area_source === "visible_label" &&
        quote.details.tax_evidence === "含税/费后 均¥1,669" &&
        quote.details.display_price_text.includes("¥1,171") &&
        sealed.amount === "1669" &&
        sealed.details.tax_evidence === "含税/费后 均¥1,669",
      JSON.stringify(ctripExactDetail),
    );
  }

  const ctripDetailAdversaries = [
    [
      "URL stay mismatch",
      "ctrip-lodging-detail-exact",
      ctripDetailUrl.replace("checkIn=2026-08-01", "checkIn=2026-08-02"),
      ctripDetailQuery,
      "url_query_matches",
    ],
    [
      "non-numeric property id",
      "ctrip-lodging-detail-exact",
      ctripDetailUrl.replace("hotelId=6210622", "hotelId=not-a-property"),
      ctripDetailQuery,
      "numeric_property_id",
    ],
    [
      "split tax price",
      "ctrip-lodging-detail-split-tax-price",
      ctripDetailUrl,
      ctripDetailQuery,
      "room_rate_contract",
    ],
    [
      "missing availability control",
      "ctrip-lodging-detail-no-availability",
      ctripDetailUrl,
      ctripDetailQuery,
      "room_rate_contract",
    ],
    [
      "starting tax price",
      "ctrip-lodging-detail-starting-tax-price",
      ctripDetailUrl,
      ctripDetailQuery,
      "room_rate_contract",
    ],
    [
      "wrong expected lodging place",
      "ctrip-lodging-detail-exact",
      ctripDetailUrl,
      {
        ...ctripDetailQuery,
        destination: "Maafushi",
        options: {
          ...ctripDetailQuery.options,
          expected_lodging_place_key: "maafushi",
        },
      },
      "lodging_place_matches",
    ],
  ];
  for (const [
    name,
    fixtureName,
    detailUrl,
    detailQuery,
    failedGate,
  ] of ctripDetailAdversaries) {
    const rejected = await parser.extractPage(
      "ctrip",
      "lodging",
      new DOMParser().parseFromString(fixtures[fixtureName], "text/html"),
      detailUrl,
      new Date("2026-07-30T12:00:00Z"),
      detailQuery,
      ctripDetailDriver,
    );
    const gates =
      rejected.failure &&
      rejected.failure.details &&
      rejected.failure.details.gates;
    record(
      `Ctrip detail ${name} fails closed`,
      rejected.state === "failed" &&
        rejected.failure.code === "dom_drift" &&
        Array.isArray(rejected.quotes) &&
        rejected.quotes.length === 0 &&
        gates &&
        gates[failedGate] === false &&
        !JSON.stringify(rejected).includes('"amount":1669'),
      JSON.stringify(rejected),
    );
  }

  const fliggyDetailUrl =
    "https://hotel.fliggy.com/hotel_detail2.htm?" +
    "shid=50420706&city=933081&checkIn=2026-08-01&checkOut=2026-08-05&" +
    "roomNum=1&aNum_1=2&cNum_1=0";
  const fliggyDetailQuery = {
    ...flightQuery,
    origin: null,
    destination: "Maafushi",
    start_date: "2026-08-01",
    end_date: "2026-08-05",
    origin_code: null,
    destination_code: null,
    options: {
      segment: "full",
      expected_package_area: "destination_island",
      expected_lodging_place_key: "maafushi",
    },
  };
  const fliggyDetailDriver = {
    ...fixtureDriver,
    confirmed_query: {
      destination: "Maafushi",
      start_date: "2026-08-01",
      end_date: "2026-08-05",
      adults: 2,
      rooms: 1,
    },
  };
  const fliggyExactDetail = await parser.extractPage(
    "fliggy",
    "lodging",
    new DOMParser().parseFromString(
      fixtures["fliggy-lodging-detail-exact"],
      "text/html",
    ),
    fliggyDetailUrl,
    new Date("2026-07-30T12:00:00Z"),
    fliggyDetailQuery,
    fliggyDetailDriver,
  );
  {
    const quote = fliggyExactDetail.quotes && fliggyExactDetail.quotes[0];
    record(
      "Fliggy detail seals exact tax-inclusive nightly room rate without clicking booking",
      fliggyExactDetail.state === "succeeded" &&
        fliggyExactDetail.quotes.length === 1 &&
        quote.amount === 579 &&
        quote.price_basis === "per_night" &&
        quote.taxes_included === true &&
        quote.details.property_id === "50420706" &&
        quote.details.room_text === "Standard Room" &&
        quote.details.availability === "available" &&
        quote.details.availability_text === "预订" &&
        quote.details.tax_evidence === "已含税" &&
        quote.details.price_basis_source ===
          "audited_fliggy_hotel_detail_rate_contract" &&
        quote.details.lodging_place_matches_expected === true,
      JSON.stringify(fliggyExactDetail),
    );
  }

  const fliggyStartingDetail = await parser.extractPage(
    "fliggy",
    "lodging",
    new DOMParser().parseFromString(
      fixtures["fliggy-lodging-detail-exact"].replace(
        "RMB 579",
        "RMB 579 起",
      ),
      "text/html",
    ),
    fliggyDetailUrl,
    new Date("2026-07-30T12:00:00Z"),
    fliggyDetailQuery,
    fliggyDetailDriver,
  );
  record(
    "Fliggy detail starting price fails closed",
    fliggyStartingDetail.state === "failed" &&
      fliggyStartingDetail.failure.code === "dom_drift" &&
      fliggyStartingDetail.quotes.length === 0,
    JSON.stringify(fliggyStartingDetail),
  );

  const qunarDetailUrl =
    "https://hotel.qunar.com/city/i-ka_maafushi/dt-2112/" +
    "?#fromDate=2026-08-21&toDate=2026-08-26&q=&showMap=0";
  const qunarDetailQuery = {
    ...flightQuery,
    origin: null,
    destination: "Maafushi",
    start_date: "2026-08-21",
    end_date: "2026-08-26",
    origin_code: null,
    destination_code: null,
    options: {
      segment: "full",
      expected_package_area: "destination_island",
      expected_lodging_place_key: "maafushi",
    },
  };
  const qunarDetailDriver = {
    provider: "qunar",
    mode: "captured_read_only_detail",
    triggered: true,
    confirmation_scope: "confirmed_visible_search",
    confirmed_query: {
      destination: "Maafushi",
      start_date: "2026-08-21",
      end_date: "2026-08-26",
      adults: 2,
      rooms: 1,
    },
    readback_query: {
      destination: "Maafushi",
      start_date: "2026-08-21",
      end_date: "2026-08-26",
      adults: 2,
      rooms: 1,
    },
    result_query_readback_confirmed: true,
    result_query_readback_scope: "qunar_visible_result_form_fields",
    result_query_readback_evidence: {
      provider_destination_id: "i-ka_maafushi",
      result_path: "/city/i-ka_maafushi",
      destination_text: "Maafushi",
      start_date_text: "2026-08-21",
      end_date_text: "2026-08-26",
      occupancy_text: "2成人 0儿童",
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
      list_inventory_receipt: {},
      list_inventory_receipt_sha256: "a".repeat(64),
      list_inventory_receipt_schema_version:
        "tripchord-lodging-inventory-receipt-v1",
      inventory_observation_chain_schema_version: null,
      inventory_observation_state: "bounded_provider_pending",
      inventory_observation_count: 1,
      inventory_observation_duration_ms: 25000,
    },
  };
  const qunarExactDetail = await parser.extractPage(
    "qunar",
    "lodging",
    new DOMParser().parseFromString(
      fixtures["qunar-lodging-detail-exact"],
      "text/html",
    ),
    qunarDetailUrl,
    new Date("2026-08-05T04:00:00Z"),
    qunarDetailQuery,
    qunarDetailDriver,
  );
  record(
    "Qunar audited detail seals one exact final tax-inclusive rate",
    qunarExactDetail.state === "succeeded" &&
      qunarExactDetail.quotes.length === 1 &&
      qunarExactDetail.quotes[0].amount === 888 &&
      qunarExactDetail.quotes[0].price_basis === "per_night" &&
      qunarExactDetail.quotes[0].taxes_included === true,
    JSON.stringify(qunarExactDetail),
  );
  const qunarStartingDetail = await parser.extractPage(
    "qunar",
    "lodging",
    new DOMParser().parseFromString(
      fixtures["qunar-lodging-detail-starting-price"],
      "text/html",
    ),
    qunarDetailUrl,
    new Date("2026-08-05T04:00:00Z"),
    qunarDetailQuery,
    qunarDetailDriver,
  );
  const qunarStartingDiagnostics = JSON.stringify({
    dom: qunarStartingDetail.failure &&
      qunarStartingDetail.failure.details.dom_diagnostics,
    rate: qunarStartingDetail.failure &&
      qunarStartingDetail.failure.details.rate_diagnostics,
  });
  record(
    "Qunar non-final rate diagnostics stay in lodging scope and exclude account header",
    qunarStartingDetail.state === "failed" &&
      qunarStartingDetail.failure.code === "dom_drift" &&
      qunarStartingDetail.quotes.length === 0 &&
      qunarStartingDetail.failure.details.dom_diagnostics.scope ===
        "qunar_lodging_rate_candidates_only" &&
      qunarStartingDetail.failure.details.dom_diagnostics
        .trusted_scope_found === true &&
      qunarStartingDetail.failure.details.rate_diagnostics.scope ===
        "qunar_lodging_rate_candidates_only" &&
      qunarStartingDetail.failure.details.rate_diagnostics
        .trusted_scope_found === true &&
      qunarStartingDetail.failure.details.rate_row_count === 1 &&
      !qunarStartingDiagnostics.includes("海风旅客私密昵称") &&
      !qunarStartingDiagnostics.includes("账户余额") &&
      !qunarStartingDiagnostics.includes("99888") &&
      !qunarStartingDiagnostics.includes("account-header") &&
      !qunarStartingDiagnostics.includes("profile-nickname") &&
      !qunarStartingDiagnostics.includes("wallet-balance"),
    JSON.stringify(qunarStartingDetail),
  );

  const memberLoginLodging = await parser.extractPage(
    "ctrip",
    "lodging",
    new DOMParser().parseFromString(
      fixtures["ctrip-lodging-member-login"],
      "text/html",
    ),
    "https://hotels.ctrip.com/results",
    new Date("2026-07-30T12:00:00Z"),
    { ...flightQuery, origin: null },
    fixtureDriver,
  );
  record(
    "Ctrip member-only hotel price without a number is login-blocked",
    memberLoginLodging.state === "blocked" &&
      memberLoginLodging.failure.code === "login_required" &&
      !memberLoginLodging.quotes,
    JSON.stringify(memberLoginLodging),
  );

  const tongchengAccountRisk = await parser.extractPage(
    "tongcheng",
    "lodging",
    new DOMParser().parseFromString(
      fixtures["tongcheng-account-risk"],
      "text/html",
    ),
    "https://www.ly.com/hotel/hotellist?city=110018575",
    new Date("2026-08-01T17:00:00Z"),
    { ...flightQuery, origin: null },
    fixtureDriver,
  );
  record(
    "Tongcheng account-risk verification is a human-only login gate",
    tongchengAccountRisk.state === "blocked" &&
      tongchengAccountRisk.failure.code === "login_required" &&
      tongchengAccountRisk.failure.message ===
        "平台要求用户本人完成账号安全验证" &&
      tongchengAccountRisk.failure.details.matched_text ===
        "账号可能存在风险" &&
      tongchengAccountRisk.failure.details.human_action_required === true,
    JSON.stringify(tongchengAccountRisk),
  );

  const unknownLodging = await parser.extractPage(
    "ctrip",
    "lodging",
    new DOMParser().parseFromString(fixtures["unknown-lodging"], "text/html"),
    "https://hotels.ctrip.com/results",
    new Date("2026-07-30T12:00:00Z"),
    {
      ...flightQuery,
      origin: null,
      options: {
        segment: "middle",
        expected_package_area: "destination_island",
      },
    },
    fixtureDriver,
  );
  record(
    "unknown lodging facts stay unknown",
    unknownLodging.quotes[0].details.area === null &&
      unknownLodging.quotes[0].details.area_source === null &&
      unknownLodging.quotes[0].details.breakfast_included === null,
    JSON.stringify(unknownLodging),
  );

  const noBreakfast = await parser.extractPage(
    "ctrip",
    "lodging",
    new DOMParser().parseFromString(fixtures["no-breakfast-lodging"], "text/html"),
    "https://hotels.ctrip.com/results",
    new Date("2026-07-30T12:00:00Z"),
    {
      ...flightQuery,
      origin: null,
      options: {
        segment: "first",
        expected_package_area: "airport_island",
      },
    },
    fixtureDriver,
  );
  record(
    "explicit no-breakfast text",
    noBreakfast.quotes[0].details.area === "airport_island" &&
      noBreakfast.quotes[0].details.breakfast_included === false,
    JSON.stringify(noBreakfast),
  );

  const exactArea = await parser.extractPage(
    "ctrip",
    "lodging",
    new DOMParser().parseFromString(
      fixtures["confirmed-exact-area-lodging"],
      "text/html",
    ),
    "https://hotels.ctrip.com/results",
    new Date("2026-07-30T12:00:00Z"),
    {
      ...flightQuery,
      origin: null,
      destination: "South Ari Atoll",
      options: {
        segment: "middle",
        expected_package_area: "destination_island",
      },
    },
    {
      mode: "fixture",
      triggered: true,
      confirmed_query: {
        destination: "South Ari Atoll",
        start_date: "2026-08-23",
        end_date: "2026-08-30",
        adults: 2,
        rooms: 1,
      },
      confirmation_scope: "fixture_exact_area",
    },
  );
  record(
    "confirmed exact search area",
    exactArea.quotes[0].details.area === "destination_island" &&
      exactArea.quotes[0].details.area_source ===
        "confirmed_exact_search_area",
    JSON.stringify(exactArea),
  );

  const transferQuery = {
    ...flightQuery,
    origin: null,
    options: {
      segment: "first",
      expected_package_area: "airport_island",
    },
  };
  const explicitTransfer = await parser.extractTransferDetail(
    "ctrip",
    new DOMParser().parseFromString(
      fixtures["transfer-detail-24h"],
      "text/html",
    ),
    "https://hotels.ctrip.com/hotels/detail/terminal-27",
    transferQuery,
  );
  record(
    "explicit 24-hour round-trip transfer contract",
    explicitTransfer.state === "succeeded" &&
      explicitTransfer.transfers.length === 2 &&
      explicitTransfer.transfers[0].origin_area === "airport" &&
      explicitTransfer.transfers[0].destination_area === "airport_island" &&
      explicitTransfer.transfers[1].origin_area === "airport_island" &&
      explicitTransfer.transfers[1].destination_area === "airport" &&
      explicitTransfer.transfers.every((transfer) =>
        transfer.taxes_included === true &&
        transfer.price_basis === "total_party" &&
        transfer.price_scope === "round_trip" &&
        transfer.amount === 108 &&
        transfer.duration_minutes === 20 &&
        transfer.operates_24_hours === true &&
        transfer.requires_reservation === true &&
        /^[a-f0-9]{64}$/.test(transfer.evidence_sha256)
      ),
    JSON.stringify(explicitTransfer),
  );

  for (const name of [
    "transfer-detail-missing-tax",
    "transfer-detail-missing-price",
    "transfer-detail-missing-time",
    "transfer-detail-direction-unknown",
  ]) {
    const detail = await parser.extractTransferDetail(
      "ctrip",
      new DOMParser().parseFromString(fixtures[name], "text/html"),
      "https://hotels.ctrip.com/hotels/detail/partial-contract",
      transferQuery,
    );
    const transfer = detail.transfers[0];
    record(
      `${name} remains incomplete`,
      detail.state === "succeeded" &&
        transfer &&
        (
          transfer.taxes_included !== true ||
          transfer.amount === null ||
          transfer.schedule_mode === null ||
          transfer.origin_area === null ||
          transfer.destination_area === null
        ),
      JSON.stringify(detail),
    );
  }

  for (const [name, expectedState, expectedCode] of [
    ["captcha", "blocked", "captcha_required"],
    ["login", "blocked", "login_required"],
    ["empty", "failed", "dom_drift"],
  ]) {
    const root = new DOMParser().parseFromString(fixtures[name], "text/html");
    const output = await parser.extractPage(
      "ctrip",
      "flight",
      root,
      "https://flights.ctrip.com/results",
      new Date("2026-07-30T12:00:00Z"),
      flightQuery,
      fixtureDriver,
    );
    record(
      `${name} gate`,
      output.state === expectedState && output.failure.code === expectedCode,
      JSON.stringify(output),
    );
  }

  for (const [name, expectedCode] of [
    ["captcha", "captcha_required"],
    ["fliggy-captcha-live-copy", "captcha_required"],
    ["login", "login_required"],
  ]) {
    const output = await parser.extractPage(
      "fliggy",
      "lodging",
      new DOMParser().parseFromString(fixtures[name], "text/html"),
      "https://hotel.fliggy.com/hotel_list3.htm",
      new Date("2026-07-30T12:00:00Z"),
      { ...flightQuery, origin: null },
      fixtureDriver,
    );
    record(
      `Fliggy lodging ${name} gate remains blocked`,
      output.state === "blocked" &&
        output.failure.code === expectedCode &&
        !output.quotes,
      JSON.stringify(output),
    );
  }

  summary.textContent = `${passed} passed, ${failed} failed`;
  summary.className = failed ? "fail" : "pass";
})();
