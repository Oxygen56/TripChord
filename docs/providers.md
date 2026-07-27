# Provider implementation matrix

Last verified against official documentation: 2026-07-27.

| Provider path | Implemented | Contract tested | Production verified | Truth label |
|---|---:|---:|---:|---|
| Amadeus Flight Offers Search | yes | yes | no credentials | sandbox or live search by environment |
| Amadeus Flight Offers Price | yes | yes | no credentials | sandbox estimate or revalidated |
| Booking Demand accommodation search | yes | yes | no partner credentials | sandbox or live search by environment |
| Booking Demand availability | yes | yes | no partner credentials | sandbox estimate or revalidated |
| AMap geocode, POI, route, weather | yes | yes | no Web Service key | production data after key verification |
| Replay fixtures | yes | yes | not applicable | replay/estimated |
| User-confirmed quote import | yes | yes | user supplied | user snapshot/estimated |

Production verification is a separate gate from code completeness. A mocked
official response proves parsing and request construction, not live coverage.

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

## Railway boundary

TripChord does not implement an undocumented 12306 adapter. Rail candidates
will use published schedule/fare references and user-confirmed snapshots, with
final availability and purchase remaining on the official channel.

