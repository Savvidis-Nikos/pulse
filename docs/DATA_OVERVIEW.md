# Simulated data: overview

A simulated Amazon-like marketplace, hour by hour, **1–21 Nov 2026**: 4 countries (DE, FR, NL, PL) × 3 platforms
(android, ios, web) × 4 categories (electronics, fashion, home, grocery). The first week is quiet history
(baselines), the next two weeks contain **22 planted scenarios**: faults, harmless changes and data problems.
Everything is regenerated with one command (`python -m marketplace.run`, ~25 s) into `data/eye/`.

Every number comes from the same simulated components (search ranker, recommendations, payments, checkout,
sellers, ads, stock, ...). A fault changes a component, so it shows up consistently in every table that
component touches - just like in a real company.

---

## 1. Business health (layer 1 - "is something wrong?")
**What:** revenue, orders, conversion, traffic, cart abandonment, margin, per hour and segment.
**Files:** `facts.csv` (main table). Metric definitions belong to the monitoring system, not the data layer.
**Key metrics:** `gmv`, `orders`, `sessions`, `conversion_rate`, `cart_conversion_rate` (abandonment = 1 - it),
`contribution_margin`, `margin_rate`, `aov`.
**Use:** the observer watches these; a change vs the same hours of the previous days (also shown as vs yesterday,
vs last week, vs 7-day average) starts an investigation.

## 2. Funnel (layer 1 - "where did customers drop out?")
**What:** sessions → searches → product views → add to cart → checkout → payment attempt → order.
**Files:** `facts.csv`.
**Key metrics:** `product_view_rate`, `search_ctr`, `cart_rate`, `checkout_rate`, `payment_attempt_rate`,
`payment_success_rate`, `feed_click_rate` (For You), `sponsored_share`.
**Use:** the investigator splits a business change into funnel steps to find the step that broke.

## 3. Segmentation ("who is affected?")
**What:** country, device/platform, category (in every table); browser and new vs returning customers.
**Files:** `facts.csv` (country, platform, category), `audience.csv` (adds browser, user_type; adds up exactly
to facts).
**Use:** segment isolation - "only Safari users", "only DE android". A narrow segment points to a specific
cause (UI bug, SDK release) rather than a broad one (marketing, market).

## 4. Products ("which products?")
**What:** top products per category with stock status; SKU-level price, price changes and sales.
**Files:** `products.csv`, `product_sales.csv`, `inventory.csv` (listed / in-stock SKUs per category).
**Use:** stock-outs, and the product-level counterfactual: did only the repriced SKUs lose sales?

## 5. Customer behavior ("what did customers experience?")
**What:** a 1 % sample of real sessions played out click by click: pages, dwell time, exits, searches,
errors, which software version served each page.
**Files:** `users.csv`, `sessions.csv`, `clickstream.csv`, `orders.csv`, `order_items.csv`.
**Use:** concrete evidence in a report ("new error LOCAL_METHOD_SDK_ERROR on SDK 7.4.0", "sessions end on the
payment page"), version comparison (users on the new version vs the old one). Quick look:
`python -m marketplace.user_report`.

## 6. Internal changes ("what did we change?")
**What:** every change by a team: releases (UI, apps, SDKs, models), config changes, feature flags, campaigns
(marketing), supply / purchasing (operations), pricing updates (pricing team), catalog launches (product team),
rollbacks. Includes harmless decoy changes at the same time as real faults.
**Files:** `events.csv` (owner, deploy time, first exposure, scope).
**Use:** the main source of explanations: timing, scope and component must fit the change.

## 7. External changes ("what happened outside?")
**What:** competitor promotions, our price position vs competitors, market demand; third-party status pages
(payment provider, CDN).
**Files:** `market.csv`, `external_events.csv`, `status_feed.csv` (source = external).
**Use:** explains changes that are not our fault (a competitor's -20 % promo, a payment provider outage).

## 8. Known events / calendar ("was this expected?")
**What:** public holidays, TV events, planned maintenance, sales periods, each with the expected impact range.
**Files:** `calendar.csv`.
**Use:** turns an expected change into an INFO note instead of an alert - but only if the size matches.

## 9. Operations ("which system shows the failure?")
**What:** service health (errors, latency, capacity) per service, app versions and crashes, payment providers,
sellers and their catalog feeds, ad auctions and advertiser budgets, wholesale purchase orders, invalid traffic.
**Files:** `service_health.csv`, `services.csv`, `app_health.csv`, `payments.csv`, `sellers.csv`,
`ads_auctions.csv`, `ads_advertisers.csv`, `wholesale.csv`, `feed_exposure.csv`; `facts.ivt_sessions`.
**Use:** mechanism evidence (checkout latency explodes after a config change), partner problems (a seller's feed
fails), bot traffic.

## 10. Customer voice and "who already knows"
**What:** support tickets by country and topic with sentiment and sample subjects; internal incident channels.
**Files:** `support_tickets.csv`, `support_ticket_samples.csv`, `status_feed.csv` (source = internal).
**Use:** corroboration and who to contact.

## 11. Data quality ("can we trust the numbers?")
**What:** completeness, lag, last update and heartbeat per data source.
**Files:** `completeness.csv`, `pipeline_health.csv`.
**Use:** the data-trust gate: late, stale, frozen or silent data is held back instead of raising false alarms.

## 12. Ground truth
**What:** what was planted, where, when, and the expected handling.
**Files:** `injected.json`. Used to score the monitoring system; the agents must never read it.

---

## The 22 scenarios
| When (Nov) | Scenario | Kind | What happens |
|---|---|---|---|
| 08 02-04 | search_maintenance | harmless | planned index rebuild, more empty searches (in the calendar) |
| 09 13 - 10 01 | click_fraud | fault | bots click ads on PL web: sessions up, conversion down |
| 10 08-18 | price_increase | fault | +25 % on half of the best-selling home products, cart rate down |
| 10 19-21 | psp_outage | fault | payment provider fails in NL (status page 25 min later) |
| 11 all day | holiday_fr, holiday_pl | harmless | public holidays: more daytime shopping |
| 12 10-22 | foryou_unavailable | fault | For You model shows out-of-stock items |
| 13 06-24 | ads_pacing | fault | ads budget pacer spends home budgets by 11:00 |
| 14 03 → | grocery_feed_drift | fault | stale features: For You clicks drift down slowly |
| 14 09-15 | seller_outage | fault | top fashion seller's catalog feed fails |
| 14 21-23 | football_de | harmless | national team match on TV: DE traffic down |
| 15 08-17 | competitor_promo | fault (external) | competitor -20 % on electronics in DE |
| 15 19-22 | checkout_capacity | fault | config change cuts checkout DB capacity at peak |
| 16 09 - 17 06 | margin_squeeze | fault | orders up but margin down (paid social + discount push) |
| 17 14 - 18 02 | payment_failure | fault | Android payment SDK breaks the DE local method |
| 18 00-08 | frozen_inventory | data | inventory feed re-sends the same snapshot |
| 18 18 - 20 00 | fr_campaign | harmless | FR email campaign: more but lower-intent traffic |
| 19 06 | catalog_launch | harmless | 1,500 new fashion products listed |
| 19 09-21 | search_relevance | fault | German search ranker bug |
| 20 06 → | stockout | fault | delayed wholesale delivery empties electronics |
| 21 09-15 | safari_checkout | fault | checkout redesign breaks the pay button on Safari |
| 21 21-24 | late_orders_data | data | order rows arrive late (looks like a drop) |

## Commands
```powershell
python -m marketplace.run                         # all 22 scenarios + 1 % user panel -> data/eye/
python -m marketplace.run --scenarios core        # only the original 8 scenarios
python -m marketplace.run --user-sample 0         # no user panel (faster)
python -m marketplace.user_report --country DE    # funnel, abandonment, top products, exits from the panel
python -m marketplace.live start | loop | inject <scenario> | list    # live demo (1 tick = 1 hour)
```

Details for agents and developers: [DATA_SYSTEM_AGENT_GUIDE.md](DATA_SYSTEM_AGENT_GUIDE.md).
Mapping to the research list and the monitoring-agent draft: [DATA_COVERAGE.md](DATA_COVERAGE.md).
