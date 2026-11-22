# Data coverage: research list vs. simulator

The research list was written for an **ad exchange** (publishers, MMPs, bid streams, eCPM). Our track is an
**Amazon-like marketplace** (buyers, sellers, sponsored products). Items were mapped to their marketplace
equivalent where one exists ("publisher" -> seller, "auction" -> sponsored-products slot auction) and skipped
where they would only add noise without a link in the metric graph.

Legend: **had** = already in the simulator, **added** = new in this round, **adapted** = added in marketplace form,
**skip** = deliberately not simulated (reason given), **engine** = a property of the agent, not of the data.

## Tier 1 - money and marketplace core
| Item | Status | Where |
|---|---|---|
| Revenue / GMV per hour | had | `facts.gmv` |
| "Ad spend (GMV)" | adapted | the list conflates the two: GMV = merchandise value, ad spend = what advertisers pay us -> new `facts.ad_revenue` |
| Take rate, net revenue | added | `facts.gmv_3p`, `facts.marketplace_fees` (commission 8-15 % on 3P GMV), `sellers.csv` |
| vs. forecast / same hour last week | engine | baselines are computed by the agent from history |
| Fill rate, clearing price, eCPM/RPM | adapted | `ads_auctions.csv`: fill_rate, avg_cpc (clearing price), ctr, rpm |
| Match rate, bid density, win rate | skip | bid-stream concepts of an open exchange; our sponsored slots are first-party pages with one internal auction |
| Budget pacing (under/over delivery, early exhaustion) | had + added | `ads_advertisers.csv`: + expected_spent_today, pacing_index, status, exhausted_at |
| Auction health: no-bid reasons, timeouts, floor | added | `ads_auctions.csv`: no_fill_no_eligible / budget_exhausted / below_floor, timeouts, floor_cpc |
| Payments: success rate, refunds | had | `facts.orders / payment_attempts`, `facts.refunds` |
| Chargebacks | added | `facts.chargebacks` (arrive ~1 week after the order) |
| Payout runs, wallet balances, cash position, payout liability | skip | treasury / finance, daily batches, no causal path to an hourly marketplace incident |
| Revenue concentration (top accounts, share shifts) | added | `sellers.csv` (1P + 22 sellers), `ads_advertisers.csv` (12 advertisers) |

## Tier 2 - trust and measurement
| Item | Status | Where |
|---|---|---|
| Invalid traffic / fraud rate, fraud loss | added | `facts.ivt_sessions`, `ads_advertisers.invalid_clicks` (loss = invalid_clicks x cpc), bot sessions in the user panel (`sessions.ivt_flag`), scenario `click_fraud` |
| Policy / brand safety, malicious creatives, account takeover | skip | trust & safety moderation; no edge to business metrics in the graph |
| Discrepancy vs. advertiser / MMP numbers | skip (MMP) | mobile-ad-network concept. Cross-system mismatch is covered: clickstream vs `orders.csv` (late orders), `inventory.csv` vs `products.csv` (frozen snapshot) |
| Postback / pixel / SDK health | adapted | `app_health.csv` (app + payment SDK versions, crashes), `service_health.csv` per platform |
| Signal loss, consent / opt-in rates | skip | for a first-party marketplace this is one more data-completeness case; already tested by `late_orders_data` and `frozen_inventory` |

## Tier 3 - platform and ML
| Item | Status | Where |
|---|---|---|
| p99 latency, error rate, QPS vs capacity | added | `service_health.csv` (8 services x platform + catalog-ingest), scenario `checkout_capacity` |
| Pipeline lag, event loss | had + added | `completeness.csv`; new `pipeline_health.csv` (lag, last_update, heartbeat) |
| Log-vs-billing reconciliation | skip | ad-billing specific |
| pCTR calibration / drift, feature freshness | added | `feed_exposure.csv`: predicted_ctr, actual_ctr, feature_freshness_h; scenario `grocery_feed_drift` |
| Cost per million requests, cloud spend | skip | infrastructure cost, no path to marketplace outcomes |
| App crashes, SDK version problems | added | `app_health.csv` |

## Tier 4 - customers on both sides (buyers, sellers, advertisers)
| Item | Status | Where |
|---|---|---|
| Advertiser funnel, time-to-first-dollar; publisher (seller) onboarding funnel | skip | weekly / monthly growth KPIs, no hourly anomaly to detect |
| Churn precursors | adapted | advertiser `status`, `roas`; customer complaint tone via ticket sentiment |
| Silence of top partners | adapted | seller feed health in `sellers.csv` (feed_status, last_feed_sync, feed_errors), scenario `seller_outage` |
| Support volume, sentiment, resolution time | added | `support_tickets.csv` (country x topic, incl. advertiser + seller topics), `support_ticket_samples.csv` (subjects in the customer's language) |
| Approval-queue delays | skip | listing moderation, slow-moving |
| ROAS / CPA delivered vs expected | added | `ads_advertisers.csv`: attributed_orders, attributed_sales, roas |

## Tier 5 - context for explanations
| Item | Status | Where |
|---|---|---|
| Change events: deploys, flags, config, model updates, SDK releases | had + added | `events.csv`, new types `config`, `flag`, `maintenance` |
| Known-events calendar | added | `calendar.csv`; holidays, TV match and maintenance have real effects in the data |
| Third-party status (cloud, CDN, PSP) | added | `status_feed.csv` (external), scenario `psp_outage`, CDN notice in another region as a decoy |
| "Who already knows?" (incident channels) | added | `status_feed.csv` (internal channels, with realistic delays) |
| Platform policy changes, regulatory actions | skip | rare, no hourly signal |
| Competitor pricing, macro ad spend | skip | cannot be validated in a simulation; would be unexplainable noise |
| Press / social / review sentiment | skip | customer voice is covered by support tickets |

## Tier 6 - self-checks
All **engine** properties (dead-man's switch, provisional/final labels, restatements, metric definition changes,
brief delivered on time, cost per brief, explanation accuracy, feedback). The data provides what they need:
`pipeline_health.csv` (freshness, heartbeat) and `injected.json` (ground truth for scoring the monitoring system).

## Anomaly patterns -> a scenario for each
| Pattern | Scenario(s) |
|---|---|
| Step change | payment_failure, psp_outage, search_relevance, checkout_capacity, ... |
| Drift | grocery_feed_drift (For You CTR -4.5 %/day, predicted CTR flat) |
| Variance change | frozen_inventory (values stop moving, completeness still 100 %) |
| Mix-shift | fr_campaign (low-intent traffic), holiday_fr / holiday_pl (daytime shift) |
| Silence | seller_outage (seller feed), frozen_inventory (source) |
| Divergence | foryou_unavailable (feed clicks up, carts down), click_fraud (sessions up, orders flat) |
| Cross-system mismatch | late_orders_data (clickstream orders vs orders.csv), frozen_inventory (inventory vs products) |
| Novelty | new error codes LOCAL_METHOD_SDK_ERROR, AUTH_GATEWAY_TIMEOUT, CHECKOUT_TIMEOUT; bot sessions |
| Concentration | seller / advertiser shares (sellers.csv, ads_advertisers.csv) |
| Money that should exist | advertiser expected_spent_today vs spent; seller GMV during its outage |

## Scenario timeline (`python -m marketplace.run`, 18 scenarios; `--scenarios core` = the original 8)
| When (Nov 2026) | Scenario | Kind | Decoy / context |
|---|---|---|---|
| 08 02:00-04:00 | search_maintenance | innocent (planned) | calendar + change log |
| 09 13:00-10 01:00 | click_fraud (PL web, home + electronics) | fault, external | web autocomplete release |
| 10 19:00-21:00 | psp_outage (NL cards) | fault, external | NL checkout copy release; PayFlow status post +25 min |
| 11 all day | holiday_fr, holiday_pl | innocent (calendar) | St. Martin's Day DE (no effect) |
| 12 10:00-22:00 | foryou_unavailable | fault | web footer release |
| 13 06:00-24:00 | ads_pacing (home) | fault | home landing page refresh |
| 14 03:00 -> end | grocery_feed_drift | fault, slow | airflow upgrade 5 h earlier is the first cause |
| 14 09:00-15:00 | seller_outage (Urbanfit, fashion) | fault, silence | fashion banner swap |
| 14 21:00-23:00 | football_de | innocent (calendar) | |
| 15 19:00-22:10 | checkout_capacity | fault (config) | iOS gift-wrap feature flag |
| 16 09:00-17 06:00 | margin_squeeze (NL) | fault | NL header strings release |
| 17 14:00-18 02:00 | payment_failure (DE android) | fault | web checkout restyle; #inc-payments posts after 2.5 h |
| 18 00:00-08:00 | frozen_inventory | data | |
| 18 18:00-20 00:00 | fr_campaign | innocent | |
| 19 09:00-21:00 | search_relevance (DE) | fault | recommendations model refresh 1.5 h later |
| 20 06:00 -> | stockout (electronics) | fault | normal-volume reorder run |
| 21 21:00-24:00 | late_orders_data | data | |

Isolation: the baseline funnel always draws from the main random stream; segments whose conditions differ are
re-drawn from the operations stream. `--scenarios core` reproduces the original data exactly, and with all
scenarios only the affected segments and hours change.

## Draft "Monitoring agent" (layer 1 + investigation data) vs simulator
| Draft item | Status | Where |
|---|---|---|
| Business health: revenue, orders, conversion, traffic | had | `facts.csv`; metrics gmv, orders, conversion_rate, sessions |
| Cart abandonment | added | metric `cart_conversion_rate` (abandonment = 1 - it); per session `sessions.cart_abandoned` |
| vs previous days / weeks / averages | added | every measurement carries vs yesterday, vs last week, vs 7-day average (printed with each incident). Months: not possible with 3 weeks of data |
| Funnel: view product, add to cart, begin checkout, purchase rate | had + added | product_view_rate (new), cart_rate, checkout_rate, payment_attempt_rate, payment_success_rate |
| Segmentation: device, country | had | facts dims platform, country |
| Segmentation: browser, new / returning | added | `audience.csv` (exact split of facts by browser x user_type); used for segment isolation |
| Product data (categories, products) | had + added | category dim; `products.csv`; new `product_sales.csv` (top-50 SKUs: price, price change, views, carts, orders) |
| Behavioral: session duration, exit rate, search terms, clickstream | had (panel) | `sessions.csv`, `clickstream.csv`; now read by the investigator (new error codes, exit shift, zero-result queries, versions) |
| Internal changes: UI, marketing, operations (stock) | had | `events.csv` (releases, campaigns, supply, config, flags) |
| Internal changes: pricing team, product team (new / removed products) | added | event types `pricing`, `catalog`; scenarios `price_increase`, `catalog_launch` |
| External changes: market, competitors (separate data sets) | added | `market.csv` (price index vs competitors, demand index), `external_events.csv` (competitor promos incl. a decoy); scenario `competitor_promo` |
| Temporal causality | had | onset vs first exposure, per component lag |
| Segment isolation | had + added | localization + control groups; browser / user type isolation (rate effect only, other values must be normal); scenario `safari_checkout` |
| Counterfactual | had + added | DiD vs rest of the market; on/off test on announced / deploy windows; repriced vs unchanged SKUs; pages served by the new version vs other versions |
| Multi factor | added | attribution: exact share of the change per factor (decomposition), each with its cause and evidence label. Shares are measured; confidence is the evidence label, not a made-up probability |

New scenarios (22 in total): `price_increase` (10 Nov, home), `competitor_promo` (15 Nov, DE electronics),
`catalog_launch` (19 Nov, fashion, innocent), `safari_checkout` (21 Nov, web Safari).
