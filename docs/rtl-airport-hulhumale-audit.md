# RTL / MTCC 机场 ↔ Hulhumalé 官方接驳证据审计

审计时间：2026-07-30（Asia/Shanghai）

## 结论

当前官方公开证据只能支持“机场与 Hulhumalé 存在双向公交连接”的
`feasibility_hint`，不能生成 Planner 可接受的 `TransferOption`。

TripChord 新增的 RTL provider 固定执行一个无需登录的官方 GET 请求，提取当前响应
中 R4、R9 的机场与 Hulhumalé 双向站点及时刻。它始终返回：

- `assurance=feasibility_hint`
- `planner_eligible=false`
- `service_date=null`
- `service_hours_confirmed=false`
- `price_currency_confirmed=false`
- `requires_live_schedule_confirmation=true`
- `operator_confirmation_window_hours=24`
- `operator_station_arrival_buffer_minutes=10`

不会声明 24 小时运营，不会创建 0 元接驳，也没有到 `TransferOption` 的转换函数。

## 当前可复现的一手来源

### MACL 当前机场交通页

官方页面：
<https://velana.macl.aero/guide/transport>

页面确认：

- RTL 运营往返 Velana International Airport 的公共公交；
- 公交连接机场、Hulhumalé 与 Malé；
- 当前路线、班表与实时信息应在 RTL Travel App 查看。

页面没有给出机场 ↔ Hulhumalé 公交的线路号、首末班、适用日期或票价。
同页的 `MVR 15` 是机场 ↔ Malé 公共渡轮，不能套用到 Hulhumalé 公交。
页面中的 `24/7` 描述出租车或帮助台，不是 RTL 公交营业时间。

### RTL 官方 Web 应用与公开 GET

官方客户门户：
<https://app.rtl.mv/>

门户公开前端使用：
<https://bo.rtl.mv:4455/maldives/api/booking/v2/bus/routedetails>

2026-07-30 实测该 GET：

- 无需登录、Cookie 或 Authorization；
- 返回 HTTP 200 JSON；
- 当前响应包含 R4（Hulhumalé Phase 1 ↔ VIA）与
  R9（Hulhumalé Phase 2 ↔ VIA）；
- 站点级 timing 可证明当前响应内的双向顺序；
- route 中存在 `fare` 数值。

当前官方门户前端把该数值渲染为 `MVR`，但 GET 本身没有币种、每人/每程口径、
税费或支付条件。Provider 因此只保留 raw fare 数值和前端来源审计，不把它写入
可汇总预算。

官方门户还提示班次可能临时调整，应在出发前 24 小时向 RTL 复核，并建议提前
10 分钟到站。Provider 将其保留为运营方确认窗口与到站缓冲提示，但公开 GET
本身不等价于运营方确认，也不因此升级为 Planner 合同。

但响应没有：

- 服务日期；
- 星期、节假日或适用周期；
- `fare` 的币种与税费口径；
- 目标酒店与公交站之间的最后一公里合同；
- 座位、库存或预订保证。

响应时刻还会随查询时点只剩下当日后续班次，因此不能把一次抓取中的时间外推到
2026 年 8 月某个旅行日。

## 不采用的来源

### MTCC Schedules 页面

<https://mtcc.mv/schedules-2/>

直接访问触发 Cloudflare challenge（HTTP 403）。本项目不绕过 Cloudflare。
搜索索引能看到 `Ramadan 1446` 的 R4/R9 条目，但这不是 2026 年 8 月适用合同。

### 旧版每 30 分钟说法

旧 MACL Arrivals 页面曾写“Airport-Hulhumalé-Airport Bus，每 30 分钟”，但当前 URL
已重定向到新版官网首页，新版交通页删除了该频率并要求使用 RTL App 查看实时班表。
因此该信息只能标为 stale reference，不能成为当前时刻、营业窗口或 Planner 约束。

### 2021 / 2022 PDF

MTCC 旧 PDF 只作为历史参考。没有当前生效日期的旧班表不能升级为 2026 实时合同。

## 对 split 六腿的支撑边界

| Split 腿 | 当前证据能证明什么 | 仍缺什么 |
|---|---|---|
| 1. Airport → 首晚 Hulhumalé 酒店 | 机场到 Hulhumalé 区域公交存在 | 目标日期班次、机场出关后可赶时刻、酒店最后一公里、可信价格 |
| 2. 首晚酒店 → Airport | Hulhumalé 到机场区域公交存在 | 酒店到站时间、目标日期班次、与 iCom 船班的 30 分钟衔接、可信价格 |
| 3. Airport → Maafushi | 不由 RTL 支撑 | 继续使用 iCom 当前官方证据 |
| 4. Maafushi → Airport | 不由 RTL 支撑 | 继续使用 iCom 当前官方证据 |
| 5. Airport → 末晚 Hulhumalé 酒店 | 与第 1 腿相同，仅区域级可行 | 船班到站后的实际可赶班次、酒店最后一公里、可信价格 |
| 6. 末晚酒店 → Airport | 与第 2 腿相同，仅区域级可行 | 目标日期班次、值机缓冲、酒店最后一公里、可信价格 |

因此当前 hint 可以帮助主控解释“为什么 split 结构在地理上可能成立”，但不能帮助
Planner 证明六腿时间连续、预算完整或实际可订。
