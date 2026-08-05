globalThis.TripChordFixtures = Object.freeze({
  "ctrip-flight": `
    <main>
      <section class="selected-flight">
        <strong class="carrier-name">香港航空</strong>
        <span>已选去程航班</span>
        <span>2026年8月23日 08:30 杭州 HGH — 18:35 马累 MLE</span>
        <button>重选去程航班</button>
      </section>
      <article data-tripchord-fixture="return-card">
        <strong class="carrier-name">香港航空</strong>
        <span>返程航班 2026年8月30日 10:45 马累 MLE — 次日 09:10 杭州 HGH</span>
        <span data-tripchord-fixture="price">往返含税价 ¥4,692 /人</span>
        <button>选择返程</button>
        <span data-tripchord-fixture="connection">香港中转 4小时15分</span>
        <span data-tripchord-fixture="baggage">无免费托运行李</span>
        <span data-tripchord-fixture="terms">含税</span>
        <span data-tripchord-fixture="terms">香港中转 4小时15分</span>
        <span data-tripchord-fixture="terms">无免费托运行李</span>
      </article>
    </main>`,
  "ctrip-flight-semantic": `
    <main>
      <header>
        <span class="carrier-name">页面导航航空</span>
        <span>往返含税价</span>
        <strong class="semantic-price">¥99 /人</strong>
        <button>选为去程</button>
      </header>
      <section class="renamed-result-shell">
        <div class="renamed-itinerary-row alpha">
          <span class="carrier-name">香港航空</span>
          <span>2026年8月23日 08:30 杭州 — 18:35 马累</span>
          <span>参考起价</span>
          <strong class="semantic-price">往返含税价 ¥4,692起 /人</strong>
          <button>选为去程</button>
          <span class="choose-duplicate">选为去程</span>
        </div>
        <div class="renamed-itinerary-row beta">
          <span class="carrier-name">马来西亚航空</span>
          <span>2026年8月23日 09:15 杭州 — 20:30 马累</span>
          <span>参考起价</span>
          <strong class="semantic-price">往返含税价 ¥5,200起 /人</strong>
          <button>选为去程</button>
        </div>
        <div class="renamed-itinerary-row booking-only">
          <span class="carrier-name">误识别航空</span>
          <span>往返含税价</span>
          <strong class="semantic-price">¥888 /人</strong>
          <button>立即预订</button>
        </div>
      </section>
    </main>`,
  "ctrip-flight-live-outbound-starting-semantic": `
    <main>
      <header>
        <span>往返含税价</span>
        <span>¥99 起</span>
        <button>选择</button>
      </header>
      <section class="live-result-shell-with-renamed-classes">
        <div class="live-renamed-row">
          <div class="carrier-name-live">泰国亚航</div>
          <div class="live-flight-route">
            <span>2026年8月12日 18:10</span>
            <span>杭州 萧山国际机场 T4</span>
            <span>曼谷中转 12小时</span>
            <span>11:35+1</span>
            <span>马累 维拉纳国际机场 T1</span>
          </div>
          <div class="flight-operate-live">
            <span>¥</span><span>5159 起</span>
            <span>往返含税价</span>
            <button type="button">选择</button>
          </div>
        </div>
      </section>
    </main>`,
  "ctrip-flight-live-outbound-comparison-only": `
    <main>
      <header>
        <span>往返含税价</span>
        <span>¥99 起</span>
        <button>立即预订</button>
      </header>
      <section class="live-result-shell-with-renamed-classes">
        <div class="opaque-result-row-without-audited-carrier-class">
          <div class="carrier-name">泰国亚洲航空</div>
          <div>
            <span>2026年8月12日 18:10</span>
            <span>杭州 萧山国际机场 T4</span>
            <span>曼谷中转 12小时</span>
            <span>11:35+1</span>
            <span>马累 维拉纳国际机场 T1</span>
          </div>
          <div class="flight-operate-live price">
            <span>¥</span><span>5159 起</span>
            <span>往返含税价</span>
            <button type="button" disabled>选为去程</button>
          </div>
        </div>
      </section>
    </main>`,
  "ctrip-flight-live-outbound-styled-control-safe": `
    <main>
      <header>
        <span>广告特惠 ¥99 起</span>
        <button>立即预订</button>
      </header>
      <section>
        <div class="flight-box">
          <span class="carrier-name">新加坡航空</span>
          <div>
            <span>2026年8月12日 20:55</span>
            <span>杭州 萧山国际机场 T4</span>
            <span>新加坡中转 2小时55分</span>
            <span>11:50+1</span>
            <span>马累 维拉纳国际机场 T1</span>
          </div>
          <div class="flight-operate outbound-booking-shell">
            <span class="price-main">¥5161起往返含税价</span>
            <div class="btn btn-book">选为去程</div>
          </div>
        </div>
      </section>
    </main>`,
  "ctrip-flight-live-outbound-styled-control-transaction": `
    <main>
      <section>
        <div class="flight-box">
          <span class="carrier-name">新加坡航空</span>
          <div>
            <span>2026年8月12日 20:55</span>
            <span>杭州 萧山国际机场 T4</span>
            <span>新加坡中转 2小时55分</span>
            <span>11:50+1</span>
            <span>马累 维拉纳国际机场 T1</span>
          </div>
          <div class="flight-operate" data-action="payment">
            <span class="price-main">¥5161起往返含税价</span>
            <a class="select-btn" href="/checkout">
              <span>选为去程</span>
            </a>
          </div>
        </div>
      </section>
    </main>`,
  "ctrip-flight-live-outbound-styled-control-promo-conflict": `
    <main>
      <section>
        <div class="flight-box">
          <span class="carrier-name">新加坡航空</span>
          <div>
            <span>2026年8月12日 20:55</span>
            <span>杭州 萧山国际机场 T4</span>
            <span>新加坡中转 2小时55分</span>
            <span>11:50+1</span>
            <span>马累 维拉纳国际机场 T1</span>
          </div>
          <div class="flight-operate">
            <div class="price-main">
              <span>¥</span><span>5161</span><span>起</span>
            </div>
            <div class="price-desc">往返含税价</div>
            <span class="campaign-price">新客立减 ¥99</span>
            <div class="select-btn"><span>选为去程</span></div>
          </div>
        </div>
      </section>
    </main>`,
  "ctrip-flight-live-return-starting-semantic": `
    <main>
      <section class="selected-flight-live">
        <span>已选去程：杭州萧山T4 → 马累T1 08-12 18:10 — 11:35+1</span>
        <button type="button">修改去程</button>
      </section>
      <section class="live-return-shell-with-renamed-classes">
        <div class="live-renamed-return-row">
          <strong class="carrier-name">马来西亚亚航</strong>
          <div>
            <span>2026年8月18日 10:45</span>
            <span>马累 维拉纳国际机场 T1</span>
            <span>吉隆坡中转 13小时15分</span>
            <span>13:10+1</span>
            <span>杭州 萧山国际机场 T4</span>
          </div>
          <div class="flight-operate-live">
            <span>¥</span><span>5159 起</span>
            <span>往返含税价</span>
            <button type="button">订票</button>
          </div>
        </div>
      </section>
    </main>`,
  "ctrip-flight-live-return-final-semantic": `
    <main>
      <section class="selected-flight-live">
        <span>已选去程：杭州 HGH → 马累 MLE 08-12 18:10 — 11:35+1</span>
        <button type="button">修改去程</button>
      </section>
      <section class="live-return-shell-with-renamed-classes">
        <div class="live-renamed-return-row">
          <strong class="carrier-name">马来西亚亚航</strong>
          <div>
            <span>2026年8月18日 10:45</span>
            <span>马累 MLE 维拉纳国际机场 T1</span>
            <span>吉隆坡中转 13小时15分</span>
            <span>13:10+1</span>
            <span>杭州 HGH 萧山国际机场 T4</span>
          </div>
          <div class="flight-operate-live">
            <span>往返含税价 ¥4,692 /人</span>
            <button type="button">订票</button>
          </div>
        </div>
      </section>
    </main>`,
  "ctrip-flight-dom-drift-diagnostic": `
    <main>
      <nav>
        <span>账户 owner@example.com，电话 13912345678</span>
      </nav>
      <section class="result-shell">
        <article
          class="renamed-fare-row user-13912345678 extra-long-class-token-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
          data-cookie="session-secret-must-not-survive"
        >
          <span class="fare-cost">往返含税价 ¥5,888 /人</span>
          <span>香港航空</span>
          <button>查看详情</button>
          <span>联系 owner@example.com 或 13912345678，会员号 123456789012</span>
          <input type="password" value="top-secret-password">
        </article>
        <article class="hidden-renamed-fare-row" hidden>
          <span>往返含税价 ¥1 /人</span>
          <button>查看详情</button>
        </article>
      </section>
    </main>`,
  "ctrip-flight-outbound-empty": `
    <main>
      <nav class="flight-progress">
        <div class="segment_tab active" role="tab">
          <span>1 选择去程</span>
        </div>
      </nav>
      <section class="outbound-results">
        <p>当前条件暂无可用去程结果</p>
      </section>
    </main>`,
  "ctrip-flight-recovery-notice": `
    <main>
      <section class="flight-requery-notice">
        <h2>温馨提示</h2>
        <p>您终于回来了~ 航班可能有变，为您重新查询</p>
        <button type="button">我知道了</button>
      </section>
    </main>`,
  "ctrip-lodging": `
    <main>
      <section class="list-item">
        <article class="hotel-card">
          <h2 class="hotelName">Terminal 27</h2>
          <div class="hotel-position">
            <span class="position-desc">胡鲁马累 · 近维拉纳国际机场</span>
          </div>
          <span class="room-name">标准大床房</span>
          <div class="room-price">
            <span class="price-line">¥396 每晚</span>
          </div>
          <span data-tripchord-fixture="terms">含税及服务费</span>
          <span data-tripchord-fixture="terms">含早餐</span>
          <span data-tripchord-fixture="terms">入住前可免费取消</span>
          <span data-tripchord-fixture="terms">机场接送 US$15</span>
          <div data-tripchord-fixture="transfer-contract">
            往返接送：马累机场 ↔ 胡鲁马累；24小时服务（当地时间 UTC+05:00）；
            单程20分钟；需提前预约；含税总价 CNY 108（2名成人）
          </div>
          <a
            data-tripchord-fixture="detail-link"
            href="https://hotels.ctrip.com/hotels/detail/terminal-27"
          >查看酒店详情</a>
        </article>
      </section>
    </main>`,
  "ctrip-lodging-no-price-unit": `
    <main>
      <section class="list-item">
        <article class="hotel-card">
          <h2 class="hotelName">Kaani Palm Beach</h2>
          <div class="hotel-position">
            <span class="position-desc">马富施 · 近 Bikini Beach</span>
          </div>
          <span class="room-name">豪华双人间</span>
          <div class="room-price">
            <span class="price-line">含税价 ¥673 起</span>
          </div>
          <span data-tripchord-fixture="terms">含早餐</span>
        </article>
      </section>
    </main>`,
  "ctrip-lodging-starting-per-night": `
    <main>
      <section class="list-item">
        <article class="hotel-card">
          <h2 class="hotelName">Kaani Palm Beach</h2>
          <div class="hotel-position">
            <span class="position-desc">马富施 · 近 Bikini Beach</span>
          </div>
          <span class="room-name">豪华双人间</span>
          <div class="room-price">
            <span class="price-line">含税价 ¥1,171 起/晚</span>
          </div>
          <span data-tripchord-fixture="terms">含税及服务费</span>
          <span data-tripchord-fixture="terms">含早餐</span>
        </article>
      </section>
    </main>`,
  "ctrip-lodging-bounded-no-exact": `
    <main>
      <section class="list-item">
        <article class="hotel-card">
          <h2 class="hotelName">
            Kaani Palm Beach owner@example.com 13912345678
          </h2>
          <div class="hotel-position">
            <span class="position-desc">
              马富施 · https://private.example/guest/123456789012
            </span>
          </div>
          <span class="room-name">豪华双人间 会员号 123456789012</span>
          <div class="room-price">
            <span class="price-line">含税价 ¥1,171 起/晚</span>
          </div>
        </article>
      </section>
    </main>`,
  "ctrip-lodging-member-login": `
    <main>
      <section class="list-item">
        <article class="hotel-card">
          <h2 class="hotelName">Kaani Palm Beach</h2>
          <div class="hotel-position">
            <span class="position-desc">马富施 · 近 Bikini Beach</span>
          </div>
          <span class="room-name">豪华双人间</span>
          <div class="room-price">
            <span class="price-line">登录以查看会员价</span>
          </div>
        </article>
      </section>
    </main>`,
  "ctrip-lodging-detail-exact": `
    <main>
      <header class="hotelDetailHeader__live">
        <h1
          class="hotelName__live"
          data-tripchord-fixture="property-title"
        >坎迪玛马尔代夫酒店(Kandima Maldives)</h1>
        <div
          class="hotelAddress__live"
          data-tripchord-fixture="property-address"
        >Dhaalu Atoll, 康迪马岛, 马尔代夫</div>
        <div
          class="datePicker-readback__live"
          data-tripchord-fixture="stay-readback"
        >8/1–8/5 · 4晚</div>
        <button
          class="guestRoom-readback__live"
          data-tripchord-fixture="occupancy-readback"
        >1间/2成人</button>
      </header>
      <section
        class="commonRoomCard__live"
        data-tripchord-fixture="room-group"
      >
        <h2
          class="commonRoomCard-roomName__live"
          data-tripchord-fixture="room-title"
        >天空一室房</h2>
        <div
          class="saleRoomItemBox__live"
          data-tripchord-fixture="rate-row"
        >
          <span>2份早餐</span>
          <span>不可取消</span>
          <span>立即确认</span>
          <span>在线付</span>
          <div class="saleRoomItemBox-priceBox__live">
            <span>黄金贵宾价</span>
            <span>均</span>
            <div
              class="saleRoomItemBox-priceBox-displayPrice__live"
              aria-label="Current price ¥1,171"
            >¥1,171</div>
            <div
              class="saleRoomItemBox-priceBox-afterTax__live"
              data-tripchord-fixture="tax-inclusive-price"
            >含税/费后 均¥1,669</div>
          </div>
          <button type="button">预订</button>
        </div>
      </section>
    </main>`,
  "ctrip-lodging-detail-split-tax-price": `
    <main>
      <header>
        <h1 data-tripchord-fixture="property-title">坎迪玛马尔代夫酒店(Kandima Maldives)</h1>
        <div data-tripchord-fixture="property-address">Dhaalu Atoll, 康迪马岛, 马尔代夫</div>
        <div data-tripchord-fixture="stay-readback">8/1–8/5 · 4晚</div>
        <div data-tripchord-fixture="occupancy-readback">1间/2成人</div>
      </header>
      <section class="commonRoomCard__split" data-tripchord-fixture="room-group">
        <h2 data-tripchord-fixture="room-title">天空一室房</h2>
        <div class="saleRoomItemBox__split" data-tripchord-fixture="rate-row">
          <span>2份早餐</span>
          <span>不可取消</span>
          <div data-tripchord-fixture="tax-inclusive-price">
            <span>含税/费后 均</span><span>¥</span><span>1,669</span>
          </div>
          <button type="button">预订</button>
        </div>
      </section>
    </main>`,
  "ctrip-lodging-detail-no-availability": `
    <main>
      <header>
        <h1 data-tripchord-fixture="property-title">坎迪玛马尔代夫酒店(Kandima Maldives)</h1>
        <div data-tripchord-fixture="property-address">Dhaalu Atoll, 康迪马岛, 马尔代夫</div>
        <div data-tripchord-fixture="stay-readback">8/1–8/5 · 4晚</div>
        <div data-tripchord-fixture="occupancy-readback">1间/2成人</div>
      </header>
      <section class="commonRoomCard__unavailable" data-tripchord-fixture="room-group">
        <h2 data-tripchord-fixture="room-title">天空一室房</h2>
        <div class="saleRoomItemBox__unavailable" data-tripchord-fixture="rate-row">
          <span>2份早餐</span>
          <span>不可取消</span>
          <div data-tripchord-fixture="tax-inclusive-price">含税/费后 均¥1,669</div>
          <button type="button" disabled aria-disabled="true">已售罄</button>
        </div>
      </section>
    </main>`,
  "ctrip-lodging-detail-starting-tax-price": `
    <main>
      <header>
        <h1 data-tripchord-fixture="property-title">坎迪玛马尔代夫酒店(Kandima Maldives)</h1>
        <div data-tripchord-fixture="property-address">Dhaalu Atoll, 康迪马岛, 马尔代夫</div>
        <div data-tripchord-fixture="stay-readback">8/1–8/5 · 4晚</div>
        <div data-tripchord-fixture="occupancy-readback">1间/2成人</div>
      </header>
      <section class="commonRoomCard__starting" data-tripchord-fixture="room-group">
        <h2 data-tripchord-fixture="room-title">天空一室房</h2>
        <div class="saleRoomItemBox__starting" data-tripchord-fixture="rate-row">
          <span>2份早餐</span>
          <span>不可取消</span>
          <div data-tripchord-fixture="tax-inclusive-price">含税/费后 均¥1,669 起/晚</div>
          <button type="button">预订</button>
        </div>
      </section>
    </main>`,
  "fliggy-lodging-detail-exact": `
    <main>
      <header>
        <h2 data-tripchord-fixture="property-title">马富士阿里纳滩酒店 (Arena Beach Hotel)</h2>
        <div data-tripchord-fixture="property-address">WFRQ+GRW, Ziyaaraiy Magu Road, 马富士</div>
        <div data-tripchord-fixture="stay-readback">2026-08-01 至 2026-08-05 · 共4晚</div>
        <div data-tripchord-fixture="occupancy-readback">成人2</div>
      </header>
      <section class="room-group">
        <h3 class="room-name" data-tripchord-fixture="room-title">Standard Room</h3>
        <div class="rate-row" data-tripchord-fixture="rate-row">
          <span>不可取消</span>
          <span data-tripchord-fixture="tax-inclusive-price">RMB 579</span>
          <span>已含税</span>
          <a href="https://fbuy.fliggy.hk/travel/confirm_order.htm">预订</a>
        </div>
      </section>
    </main>`,
  "qunar-lodging-detail-exact": `
    <main>
      <header>
        <h1 data-tripchord-fixture="property-title">Kaani Palm Beach</h1>
        <div data-tripchord-fixture="property-address">
          Maafushi, Kaafu Atoll, Maldives
        </div>
        <div data-tripchord-fixture="stay-readback">
          2026-08-21 至 2026-08-26 · 5晚
        </div>
        <div data-tripchord-fixture="occupancy-readback">
          1间房 / 2成人 / 0儿童
        </div>
      </header>
      <section data-tripchord-fixture="rate-row">
        <h3 data-tripchord-fixture="room-title">Deluxe Double Room</h3>
        <span>含税及服务费 最终价 CNY 888 每晚</span>
        <span>含早餐</span>
        <span>免费取消</span>
        <button type="button">预订</button>
      </section>
    </main>`,
  "qunar-lodging-detail-wrong-area": `
    <main>
      <header>
        <h1 data-tripchord-fixture="property-title">Kaani Palm Beach</h1>
        <div data-tripchord-fixture="property-address">
          Hulhumale, Maldives
        </div>
        <div data-tripchord-fixture="stay-readback">
          2026-08-21 至 2026-08-26 · 5晚
        </div>
        <div data-tripchord-fixture="occupancy-readback">
          1间房 / 2成人 / 0儿童
        </div>
      </header>
      <section data-tripchord-fixture="rate-row">
        <h3 data-tripchord-fixture="room-title">Deluxe Double Room</h3>
        <span>含税及服务费 最终价 CNY 888 每晚</span>
        <button type="button">预订</button>
      </section>
    </main>`,
  "qunar-lodging-detail-starting-price": `
    <header class="account-header">
      <nav class="member-nav">
        <span class="profile-nickname">海风旅客私密昵称</span>
        <span class="wallet-balance">账户余额 CNY 99888</span>
      </nav>
    </header>
    <main>
      <header>
        <h1 data-tripchord-fixture="property-title">Kaani Palm Beach</h1>
        <div data-tripchord-fixture="property-address">
          Maafushi, Kaafu Atoll, Maldives
        </div>
        <div data-tripchord-fixture="stay-readback">
          2026-08-21 至 2026-08-26 · 5晚
        </div>
        <div data-tripchord-fixture="occupancy-readback">
          1间房 / 2成人 / 0儿童
        </div>
      </header>
      <section data-tripchord-fixture="rate-row">
        <h3 data-tripchord-fixture="room-title">Deluxe Double Room</h3>
        <span>含税价 CNY 888 起/晚</span>
        <button type="button">预订</button>
      </section>
    </main>`,
  "ctrip-lodging-suggestion": `
    <main id="trip_main_content">
      <input
        id="destinationInput"
        aria-label="目的地"
        placeholder="目的地"
        value="Maafushi"
      >
      <div class="ctrip-suggestion-list">
        <div
          tabindex="-1"
          data-tripchord-suggestion-kind="hotel"
        >七珊瑚酒店Maafushi</div>
        <div
          tabindex="-1"
          data-tripchord-suggestion-kind="city"
        ><span>马富施</span><span>Kaafu Atoll, Maldives</span></div>
      </div>
    </main>`,
  "ctrip-lodging-suggestion-no-readback": `
    <main id="trip_main_content">
      <input
        id="destinationInput"
        aria-label="目的地"
        placeholder="目的地"
        value="Maafushi"
      >
      <div class="ctrip-suggestion-list">
        <div
          tabindex="-1"
          data-tripchord-suggestion-kind="hotel"
        >七珊瑚酒店Maafushi</div>
        <div
          tabindex="-1"
          data-tripchord-suggestion-kind="city"
        ><span>马富施</span><span>Kaafu Atoll, Maldives</span></div>
      </div>
    </main>`,
  "ctrip-lodging-suggestion-inner-label": `
    <main id="trip_main_content">
      <input
        id="destinationInput"
        aria-label="目的地"
        placeholder="目的地"
        value="Maafushi"
      >
      <div class="ctrip-suggestion-list">
        <div
          tabindex="-1"
          data-tripchord-suggestion-kind="hotel"
        ><span>七珊瑚酒店</span><span>Maafushi</span></div>
        <div
          tabindex="-1"
          data-tripchord-suggestion-kind="city"
        ><span>马富施岛</span><span>Maafushi</span><span>Kaafu Atoll, Maldives</span></div>
      </div>
    </main>`,
  "fliggy-lodging-suggestion": `
    <form data-tripchord-suggestion-fixture="fliggy">
      <input
        data-testid="international-city-input"
        aria-label="目的地"
        value="Maafushi"
      >
      <div data-testid="search-city-dropdown" role="listbox">
        <button
          type="button"
          role="option"
          data-testid="search-city-马富士"
          data-agent-id="search-city-马富士"
          data-agent-type="city-option"
        >马富士,马尔代夫,马尔代夫</button>
      </div>
    </form>`,
  "fliggy-lodging-country-inner-label": `
    <form data-tripchord-suggestion-fixture="fliggy-country">
      <div data-tripchord-fixture="destination-control">
        <input
          data-testid="international-city-input"
          aria-label="目的地"
          value="马尔代夫"
        >
        <div data-testid="search-city-dropdown" role="listbox">
          <button type="button" role="option">
            <span class="associationalWord" style="display: contents">马尔代夫</span>
            <span class="associationalWord" style="display: contents">Maldives</span>
          </button>
        </div>
      </div>
    </form>`,
  "fliggy-lodging-suggestion-wrong-id": `
    <form data-tripchord-suggestion-fixture="fliggy-wrong-id">
      <input
        data-testid="international-city-input"
        aria-label="目的地"
        value="Maafushi"
      >
      <div data-testid="search-city-dropdown" role="listbox">
        <button
          type="button"
          role="option"
          data-testid="search-city-马富士"
          data-agent-id="search-city-马富士"
          data-agent-type="city-option"
          data-city-code="999999"
        >马富士,马尔代夫,马尔代夫</button>
      </div>
    </form>`,
  "qunar-lodging-suggestion": `
    <form id="interForm" data-tripchord-suggestion-fixture="qunar">
      <div class="city-input">
        <input
          class="textbox"
          aria-label="目的地"
          value="新加坡"
        >
      </div>
      <div class="m-suggest-container">
        <table class="suggest-list">
          <tbody>
            <tr class="item">
              <td>马富施，卡夫环礁</td>
              <td>Maafushi, Kaafu Atoll</td>
            </tr>
          </tbody>
        </table>
      </div>
    </form>`,
  "qunar-lodging-suggestion-no-readback": `
    <form id="interForm" data-tripchord-suggestion-fixture="qunar-no-readback">
      <div class="city-input">
        <input
          class="textbox"
          aria-label="目的地"
          value="新加坡"
        >
      </div>
      <div class="m-suggest-container">
        <table class="suggest-list">
          <tbody>
            <tr class="item">
              <td>马富施，卡夫环礁</td>
              <td>Maafushi, Kaafu Atoll</td>
            </tr>
          </tbody>
        </table>
      </div>
    </form>`,
  "qunar-lodging-zero-rect-inner-label": `
    <form id="interForm" data-tripchord-suggestion-fixture="qunar-inner-label">
      <div class="city-input">
        <input
          class="textbox"
          aria-label="目的地"
          value="新加坡"
        >
      </div>
      <div class="m-suggest-container">
        <div data-tripchord-suggestion-row="maafushi">
          <span class="item" style="display: contents">Maafushi</span>
          <span>, Kaafu Atoll</span>
        </div>
      </div>
    </form>`,
  "fliggy-flight": `
    <main>
      <section class="selected-flight">
        <div class="selected-flight-info">
          <strong class="airline">亚洲航空</strong>
          <span>已选去程航班</span>
          <span>2026年8月23日 07:10 杭州 HGH — 17:20 马累 MLE</span>
          <button>重选去程航班</button>
        </div>
      </section>
      <article class="J_FlightItem">
        <strong class="airline">亚洲航空</strong>
        <span>返程航班 2026年8月30日 12:15 马累 MLE — 次日 10:30 杭州 HGH</span>
        <span class="price">人均往返含税价 ¥4,858</span>
        <button class="J_Btn_Select">选为返程</button>
        <span class="tag">含税</span>
        <span class="tag">吉隆坡中转</span>
        <span class="baggage">手提行李 7kg</span>
      </article>
    </main>`,
  "fliggy-flight-ambiguous-total": `
    <main>
      <section class="selected-flight">
        <div class="selected-flight-info">
          <strong class="airline">亚洲航空</strong>
          <span>已选去程航班</span>
          <span>2026年8月23日 07:10 杭州 HGH — 17:20 马累 MLE</span>
          <button>重选去程航班</button>
        </div>
      </section>
      <article class="J_FlightItem">
        <strong class="airline">亚洲航空</strong>
        <span>返程航班 2026年8月30日 12:15 马累 MLE — 次日 10:30 杭州 HGH</span>
        <span class="price">往返总价 含税 ¥4,858</span>
        <button class="J_Btn_Select">选为返程</button>
        <span class="tag">含税</span>
      </article>
    </main>`,
  "ctrip-flight-wrong-outbound-route": `
    <main>
      <section class="selected-flight">
        <strong class="carrier-name">香港航空</strong>
        <span>已选去程航班</span>
        <span>2026年8月23日 08:30 上海 PVG — 18:35 马累 MLE</span>
        <button>重选去程航班</button>
      </section>
      <article data-tripchord-fixture="return-card">
        <strong class="carrier-name">香港航空</strong>
        <span>返程航班 2026年8月30日 10:45 马累 MLE — 次日 09:10 杭州 HGH</span>
        <span data-tripchord-fixture="price">往返含税价 ¥4,692 /人</span>
        <button>选择返程</button>
        <span data-tripchord-fixture="terms">含税</span>
      </article>
    </main>`,
  "ctrip-flight-wrong-return-route": `
    <main>
      <section class="selected-flight">
        <strong class="carrier-name">香港航空</strong>
        <span>已选去程航班</span>
        <span>2026年8月23日 08:30 杭州 HGH — 18:35 马累 MLE</span>
        <button>重选去程航班</button>
      </section>
      <article data-tripchord-fixture="return-card">
        <strong class="carrier-name">香港航空</strong>
        <span>返程航班 2026年8月30日 10:45 马累 MLE — 次日 09:10 北京 PEK</span>
        <span data-tripchord-fixture="price">往返含税价 ¥4,692 /人</span>
        <button>选择返程</button>
        <span data-tripchord-fixture="terms">含税</span>
      </article>
    </main>`,
  "ctrip-flight-tax-conflict": `
    <main>
      <section class="selected-flight">
        <strong class="carrier-name">香港航空</strong>
        <span>已选去程航班</span>
        <span>2026年8月23日 08:30 杭州 HGH — 18:35 马累 MLE</span>
        <button>重选去程航班</button>
      </section>
      <article data-tripchord-fixture="return-card">
        <strong class="carrier-name">香港航空</strong>
        <span>返程航班 2026年8月30日 10:45 马累 MLE — 次日 09:10 杭州 HGH</span>
        <span data-tripchord-fixture="price">往返含税价 ¥4,692 /人</span>
        <button>选择返程</button>
        <span data-tripchord-fixture="terms">含税</span>
        <span data-tripchord-fixture="terms">部分税费另付</span>
      </article>
    </main>`,
  "ctrip-flight-no-availability": `
    <main>
      <section class="selected-flight">
        <strong class="carrier-name">香港航空</strong>
        <span>已选去程航班</span>
        <span>2026年8月23日 08:30 杭州 HGH — 18:35 马累 MLE</span>
        <button>重选去程航班</button>
      </section>
      <article data-tripchord-fixture="return-card">
        <strong class="carrier-name">香港航空</strong>
        <span>返程航班 2026年8月30日 10:45 马累 MLE — 次日 09:10 杭州 HGH</span>
        <span data-tripchord-fixture="price">往返含税价 ¥4,692 /人</span>
        <button disabled aria-disabled="true">已售罄</button>
        <span data-tripchord-fixture="terms">含税</span>
      </article>
    </main>`,
  "fliggy-flight-outbound-preview": `
    <main>
      <article class="J_FlightItem outbound">
        <strong class="airline">亚洲航空</strong>
        <span>2026年8月23日 去程 07:10 杭州 HGH — 17:20 马累 MLE</span>
        <span class="price">预估往返价 ¥4,858 /人</span>
        <button class="J_Btn_Select">选为去程</button>
      </article>
      <article class="J_FlightItem forbidden-controls">
        <strong class="airline">不可操作航空</strong>
        <span>2026年8月30日 返程 12:15 马累 — 10:30 杭州</span>
        <button class="J_Btn_Select">选为返程</button>
        <button class="J_Btn_Select">预订</button>
      </article>
    </main>`,
  "fliggy-flight-live-outbound-semantic": `
    <main>
      <section class="renamed-live-flight-results">
        <div class="renamed-live-flight-row">
          <strong class="airline-live">泰国亚航</strong>
          <span>2026年8月12日 去程</span>
          <span>18:10 杭州 HGH 萧山国际机场 T4</span>
          <span>11:35 +1天 马累 MLE 维拉纳国际机场 T1</span>
          <span class="fare-live">¥5718 起</span>
          <button type="button">选为去程</button>
        </div>
      </section>
    </main>`,
  "fliggy-flight-alternate-origin-only": `
    <main>
      <section class="nearby-results">
        <ul>
          <li class="nearby-item">
            <span class="route">上海 - 马累</span>
            <span class="fare"><strong>¥6984</strong> 票面 + <em>¥1154</em> 税费</span>
          </li>
        </ul>
      </section>
    </main>`,
  "fliggy-lodging": `
    <main>
      <article data-tripchord-fixture="quote">
        <h2 data-tripchord-fixture="title">Kaani Village &amp; Spa</h2>
        <span data-tripchord-fixture="room">豪华双人间</span>
        <span data-tripchord-fixture="area">马富施岛</span>
        <span data-tripchord-fixture="price">每晚 ￥673</span>
        <span data-tripchord-fixture="terms">税费已含</span>
        <span data-tripchord-fixture="terms">含早餐</span>
        <span data-tripchord-fixture="terms">入住前可免费取消</span>
        <span data-tripchord-fixture="terms">往返快艇 US$50/成人</span>
        <div data-tripchord-fixture="transfer-contract">
          往返快艇：胡鲁马累 ↔ 马富施岛；每日 06:00-22:00（UTC+05:00）；
          单程45分钟；需提前预约；含税总价 CNY 720（2名成人）
        </div>
      </article>
    </main>`,
  "fliggy-lodging-bounded-no-exact": `
    <main>
      <article data-testid="hotel-card">
        <h2 data-tripchord-fixture="title">
          Kaani Village owner@example.com 13912345678
        </h2>
        <span data-tripchord-fixture="room">
          豪华双人间 会员号 123456789012
        </span>
        <span data-tripchord-fixture="area">
          马富施 https://private.example/guest/123456789012
        </span>
        <span data-tripchord-fixture="price">每晚 ￥673 起</span>
      </article>
    </main>`,
  "qunar-flight": `
    <main>
      <section class="m-airfly-lst">
        <article class="b-airfly">
          <strong class="air">马来西亚航空</strong>
          <section class="s-trip">
            <div class="col-time">
              <div class="sep-lf"><h2>09:15</h2><span class="airport">HGH 杭州萧山</span></div>
              <div class="sep-rt"><h2>20:30</h2><span class="airport">MLE 维拉纳</span></div>
            </div>
            <span>2026年8月23日 去程</span>
          </section>
          <section class="s-trip">
            <div class="col-time">
              <div class="sep-lf"><h2>11:20</h2><span class="airport">MLE 维拉纳</span></div>
              <div class="sep-rt"><h2>11:05</h2><span class="airport">HGH 杭州萧山</span></div>
            </div>
            <span>2026年8月30日 返程 +1天</span>
          </section>
          <div class="col-price">人均含税价 ¥4,880</div>
          <span class="tag">含税</span>
          <span class="tag">吉隆坡中转 13小时15分</span>
          <span class="baggage">行李额以详情页为准</span>
          <button class="btn-book">预订</button>
        </article>
      </section>
    </main>`,
  "qunar-flight-split-price-nodes": `
    <main>
      <section class="m-airfly-lst">
        <article class="b-airfly split-price-live-shape">
          <strong class="air">马来西亚航空</strong>
          <section class="s-trip">
            <div class="col-time">
              <div class="sep-lf"><h2>09:15</h2><span class="airport">HGH 杭州萧山</span></div>
              <div class="sep-rt"><h2>20:30</h2><span class="airport">MLE 维拉纳</span></div>
            </div>
            <span>2026年8月23日 去程</span>
          </section>
          <section class="s-trip">
            <div class="col-time">
              <div class="sep-lf"><h2>11:20</h2><span class="airport">MLE 维拉纳</span></div>
              <div class="sep-rt"><h2>11:05</h2><span class="airport">HGH 杭州萧山</span></div>
            </div>
            <span>2026年8月30日 返程 +1天</span>
          </section>
          <div class="col-price">
            <span class="basis">人均含税价</span>
            <span class="symbol">¥</span>
            <span class="digit-part">4</span>
            <span class="digit-part">880</span>
          </div>
          <span class="tag">含税</span>
          <button class="btn-book">预订</button>
        </article>
      </section>
    </main>`,
  "qunar-flight-consistent-digit-titles": `
    <main>
      <form class="qunar-visible-search-context">
        <input id="fromCity" name="fromCity" value="杭州(HGH)">
        <input id="toCity" name="toCity" value="马累(MLE)">
        <input id="fromDate" name="fromDate" value="2026-08-23">
        <input id="toDate" name="toDate" value="2026-08-30">
        <input name="adultNum" value="2" aria-label="成人">
      </form>
      <section class="m-airfly-lst">
        <article class="b-airfly title-backed-price-live-shape">
          <strong class="air">马来西亚航空</strong>
          <section class="s-trip">
            <div class="col-time">
              <div class="sep-lf"><h2>09:15</h2><span class="airport">HGH 杭州萧山</span></div>
              <div class="sep-rt"><h2>20:30</h2><span class="airport">MLE 维拉纳</span></div>
            </div>
            <span>2026年8月23日 去程</span>
          </section>
          <section class="s-trip">
            <div class="col-time">
              <div class="sep-lf"><h2>11:20</h2><span class="airport">MLE 维拉纳</span></div>
              <div class="sep-rt"><h2>11:05</h2><span class="airport">HGH 杭州萧山</span></div>
            </div>
            <span>2026年8月30日 返程 +1天</span>
          </section>
          <div class="col-price">
            <span class="basis">含税总价</span>
            <span class="symbol">¥</span>
            <i title="6600">6</i>
            <i title="6600">6</i>
            <i title="6600">0</i>
            <i title="6600">0</i>
          </div>
          <span class="tag">含税</span>
        </article>
      </section>
    </main>`,
  "qunar-flight-title-price-no-availability": `
    <main>
      <section class="m-airfly-lst">
        <article class="b-airfly title-backed-price-no-availability">
          <strong class="air">马来西亚航空</strong>
          <section class="s-trip">
            <div class="col-time">
              <div class="sep-lf"><h2>09:15</h2><span class="airport">HGH 杭州萧山</span></div>
              <div class="sep-rt"><h2>20:30</h2><span class="airport">MLE 维拉纳</span></div>
            </div>
            <span>2026年8月12日 去程</span>
          </section>
          <section class="s-trip">
            <div class="col-time">
              <div class="sep-lf"><h2>11:20</h2><span class="airport">MLE 维拉纳</span></div>
              <div class="sep-rt"><h2>11:05</h2><span class="airport">HGH 杭州萧山</span></div>
            </div>
            <span>2026年8月18日 返程 +1天</span>
          </section>
          <div class="col-price">
            <span class="basis">含税总价</span>
            <span class="symbol">¥</span>
            <i title="6600">6</i>
            <i title="6600">6</i>
            <i title="6600">0</i>
            <i title="6600">0</i>
          </div>
          <span class="tag">含税</span>
        </article>
      </section>
    </main>`,
  "qunar-lodging": `
    <main>
      <article data-tripchord-fixture="quote">
        <h2 data-tripchord-fixture="title">Bandos Maldives</h2>
        <span data-tripchord-fixture="room">高级海景房</span>
        <span data-tripchord-fixture="area">班度士岛</span>
        <span data-tripchord-fixture="price">全程总价 CNY 15,519</span>
        <span data-tripchord-fixture="terms">未含税</span>
        <span data-tripchord-fixture="terms">含早晚餐</span>
        <span data-tripchord-fixture="terms">不可取消</span>
        <span data-tripchord-fixture="terms">快艇接送 US$110/成人</span>
      </article>
    </main>`,
  "qunar-lodging-bounded-no-exact": `
    <main>
      <article class="hotel-item">
        <h2 data-tripchord-fixture="title">
          Bandos Maldives owner@example.com 13912345678
        </h2>
        <span data-tripchord-fixture="room">
          高级海景房 会员号 123456789012
        </span>
        <span data-tripchord-fixture="area">
          班度士岛 https://private.example/guest/123456789012
        </span>
        <span data-tripchord-fixture="price">
          全程总价 CNY 15,519 起
        </span>
      </article>
    </main>`,
  "qunar-lodging-confirmed-empty": `
    <main data-tripchord-fixture="qunar-empty-evidence">
      <section class="result-summary">共 0 家酒店满足条件</section>
      <section class="empty-result">
        <p>很抱歉，没有找到相关的酒店</p>
        <p>适当减少已选择的条件</p>
      </section>
    </main>`,
  "explicit-baggage-flight": `
    <main>
      <article data-tripchord-fixture="quote">
        <h2 data-tripchord-fixture="title">明确托运行李航班</h2>
        <span data-tripchord-fixture="carrier">Fixture Air</span>
        <time datetime="2026-08-23T08:00:00+08:00">08:00</time>
        <time datetime="2026-08-23T18:00:00+05:00">18:00</time>
        <time datetime="2026-08-30T10:00:00+05:00">10:00</time>
        <time datetime="2026-08-31T08:00:00+08:00">08:00</time>
        <span data-tripchord-fixture="price">含税 ¥5,100 /人</span>
        <span data-tripchord-fixture="baggage">每位成人免费托运行李 23kg</span>
      </article>
    </main>`,
  "unknown-basis-flight": `
    <main>
      <article data-tripchord-fixture="quote">
        <h2 data-tripchord-fixture="title">口径未知航班</h2>
        <span data-tripchord-fixture="carrier">Fixture Air</span>
        <time datetime="2026-08-23T08:00:00+08:00">08:00</time>
        <time datetime="2026-08-23T18:00:00+05:00">18:00</time>
        <time datetime="2026-08-30T10:00:00+05:00">10:00</time>
        <time datetime="2026-08-31T08:00:00+08:00">08:00</time>
        <span data-tripchord-fixture="price">含税 ¥5,100</span>
        <span data-tripchord-fixture="baggage">无免费托运行李</span>
      </article>
    </main>`,
  "unknown-basis-lodging": `
    <main>
      <article data-tripchord-fixture="quote">
        <h2 data-tripchord-fixture="title">口径未知酒店</h2>
        <span data-tripchord-fixture="room">标准房</span>
        <span data-tripchord-fixture="area">胡鲁马累</span>
        <span data-tripchord-fixture="price">含税 ¥800</span>
        <span data-tripchord-fixture="terms">含早餐</span>
      </article>
    </main>`,
  "unknown-lodging": `
    <main>
      <article data-tripchord-fixture="quote">
        <h2 data-tripchord-fixture="title">区域待确认酒店</h2>
        <span data-tripchord-fixture="room">标准房</span>
        <span data-tripchord-fixture="area">位置以酒店最终确认为准</span>
        <span data-tripchord-fixture="price">含税 ¥500 每晚</span>
        <span data-tripchord-fixture="terms">早餐详情以页面为准</span>
      </article>
    </main>`,
  "no-breakfast-lodging": `
    <main>
      <article data-tripchord-fixture="quote">
        <h2 data-tripchord-fixture="title">胡鲁马累不含早酒店</h2>
        <span data-tripchord-fixture="room">标准房</span>
        <span data-tripchord-fixture="area">胡鲁马累</span>
        <span data-tripchord-fixture="price">含税 ¥480 每晚</span>
        <span data-tripchord-fixture="terms">不含早餐</span>
      </article>
    </main>`,
  "confirmed-exact-area-lodging": `
    <main>
      <article data-tripchord-fixture="quote">
        <h2 data-tripchord-fixture="title">South Ari Atoll Stay</h2>
        <span data-tripchord-fixture="room">Beach Villa</span>
        <span data-tripchord-fixture="area">South Ari Atoll</span>
        <span data-tripchord-fixture="price">含税 ¥980 每晚</span>
        <span data-tripchord-fixture="terms">含早餐</span>
      </article>
    </main>`,
  "transfer-detail-24h": `
    <main>
      <section data-tripchord-fixture="transfer-contract">
        往返接送：马累机场 ↔ 胡鲁马累；24小时服务（当地时间 UTC+05:00）；
        单程20分钟；需提前预约；含税总价 CNY 108（2名成人）
      </section>
    </main>`,
  "transfer-detail-missing-tax": `
    <main>
      <section data-tripchord-fixture="transfer-contract">
        往返接送：马累机场 ↔ 胡鲁马累；24小时服务（当地时间 UTC+05:00）；
        单程20分钟；需提前预约；总价 CNY 108（2名成人）
      </section>
    </main>`,
  "transfer-detail-missing-price": `
    <main>
      <section data-tripchord-fixture="transfer-contract">
        往返接送：马累机场 ↔ 胡鲁马累；24小时服务（当地时间 UTC+05:00）；
        单程20分钟；需提前预约；含税，价格以酒店确认为准
      </section>
    </main>`,
  "transfer-detail-missing-time": `
    <main>
      <section data-tripchord-fixture="transfer-contract">
        往返接送：马累机场 ↔ 胡鲁马累；需提前预约；
        含税总价 CNY 108（2名成人）
      </section>
    </main>`,
  "transfer-detail-direction-unknown": `
    <main>
      <section data-tripchord-fixture="transfer-contract">
        提供机场接送；24小时服务（当地时间 UTC+05:00）；
        单程20分钟；含税总价 CNY 108（2名成人）
      </section>
    </main>`,
  captcha: `<main><h1>请完成安全验证</h1><p>拖动滑块</p></main>`,
  "fliggy-captcha-live-copy": `
    <main>
      <p>亲，请拖动下方滑块完成验证</p>
      <p>通过验证以确保正常访问</p>
      <button>请按住滑块，拖动到最右边</button>
    </main>
  `,
  login: `<main><div>请先登录后查看报价</div></main>`,
  "tongcheng-account-risk": `
    <main>
      <div>您的账号可能存在风险</div>
      <div>为了您的账号安全请验证通过后使用</div>
      <button>前往验证</button>
    </main>`,
  empty: `<main><h1>航班搜索结果</h1><p>页面结构已经变化</p></main>`,
});
