"""Actions on the marketplace (deploys, campaigns, supply events) and the demo timeline.

Every action changes a component and writes a change-log entry, like a real deploy or campaign would.
Scenario actions also carry ground truth for the scorecard (data/.../injected.json). Harmless actions are
decoys: real changes at the same time that the monitoring agent has to rule out.

Use from code or the CLI:
  python -m marketplace.run                      full demo period with all scenarios
  python -m marketplace.live inject <name>       trigger one scenario in the live loop
"""
from __future__ import annotations

import pandas as pd

H = pd.Timedelta(hours=1)


# ----------------------------------------------------------------------------- actions (component changes)
def deploy_ranker(mp, ts, version, country, locale_bug=False, planned=True, rollback=False):
    s = mp.state
    if locale_bug:
        s.ranker_locale_bug[version] = country
    s.ranker[country] = version
    name = f"search-ranker {version} rolled back" if rollback else f"search-ranker {version} ({country.lower()} locale)"
    mp.log(ts, "rollback" if rollback else "release", name, "search", "search-relevance",
           dict(country=country), planned=planned, deploy_lead_min=5 if rollback else 10)


def deploy_payment_sdk(mp, ts, platform, version, breaks=None, rollback_of=None):
    s = mp.state
    if breaks:
        s.pay_broken[version] = breaks
    s.pay_sdk[platform] = version
    if rollback_of:
        mp.log(ts, "rollback", f"payments-sdk {platform} {rollback_of} rolled back to {version}", "payments",
               "payments-mobile", dict(platform=platform), planned=False, deploy_lead_min=15)
    else:
        mp.log(ts, "release", f"payments-sdk {platform} {version}", "payments", "payments-mobile",
               dict(platform=platform), deploy_lead_min=70)


def start_campaign(mp, ts, name, country, uplift, intent, until, cps=0.0, owner="growth-marketing"):
    mp.state.campaigns.append(dict(name=name, country=country, uplift=uplift, intent=intent, cps=cps, until=until))
    mp.log(ts, "campaign", name, "marketing", owner, dict(country=country), end=until, deploy_lead_min=30)


def recs_release(mp, ts, name, model=None, inventory_filter=None, oos_bias=None, discount_affinity=None,
                 spec=None, planned=True, type_="release"):
    s = mp.state
    if model is not None:
        s.recs_model = model
    if inventory_filter is not None:
        s.recs_inventory_filter = inventory_filter
    if oos_bias is not None:
        s.recs_oos_bias = oos_bias
    if discount_affinity is not None:
        s.recs_discount_affinity.update(discount_affinity)
    mp.log(ts, type_, name, "recommendations", "recsys", spec or {}, planned=planned, deploy_lead_min=25)


def ads_pacer_release(mp, ts, version, category, broken, rollback=False):
    s = mp.state
    s.pacer = version
    if broken:
        s.pacer_ignores_schedule.add(category)
    else:
        s.pacer_ignores_schedule.discard(category)
    name = f"ads-pacer {version} rolled back" if rollback else f"ads-pacer {version} ({category} advertisers)"
    mp.log(ts, "rollback" if rollback else "release", name, "sponsored_ads", "ads-delivery",
           dict(category=category), planned=not rollback, deploy_lead_min=10)


def supplier_delay(mp, ts, category, from_ts, until, name):
    mp.state.delivery_delay[category] = (from_ts, until)
    mp.log(ts, "supply", name, "wholesale", "supply-chain", dict(category=category), planned=False,
           deploy_lead_min=0)


def harmless(mp, ts, type_, name, component, owner, spec=None, planned=True):
    mp.log(ts, type_, name, component, owner, spec or {}, planned=planned)


def late_data(mp, ts, source, shares):
    for i, sh in enumerate(shares):
        mp.state.late_rows[(source, pd.Timestamp(ts) + i * H)] = sh


# ----------------------------------------------------------------------------- demo timeline
# (when, function, kwargs). Supplier delay becomes effective at its from_ts (handled in run.py via `effective`).
def core_timeline():
    t = pd.Timestamp
    return [
        # --- For You v12 drops the inventory filter: feed clicks up, purchases down (Thu 12 Nov)
        (t("2026-11-12 10:00"), recs_release, dict(name="For You model v12 (inventory filter off)", model="v12",
                                                   inventory_filter=False, oos_bias=20.0)),
        (t("2026-11-12 10:00"), harmless, dict(type_="release", name="web footer links update", component="app",
                                               owner="web-platform", spec=dict(platform="web"))),
        (t("2026-11-12 22:00"), recs_release, dict(name="For You model v12 rolled back to v11", model="v11",
                                                   inventory_filter=True, oos_bias=1.0, planned=False,
                                                   type_="rollback")),
        # --- ads pacer ignores the schedule for home advertisers: budgets gone by late morning (Fri 13 Nov)
        (t("2026-11-13 06:00"), ads_pacer_release, dict(version="3.1", category="home", broken=True)),
        (t("2026-11-13 07:00"), harmless, dict(type_="release", name="home category landing page refresh",
                                               component="app", owner="web-platform", spec=dict(category="home"))),
        (t("2026-11-14 00:00"), ads_pacer_release, dict(version="3.0", category="home", broken=False, rollback=True)),
        # --- harmless iOS release + NL margin squeeze (Mon 16 Nov)
        (t("2026-11-16 11:00"), harmless, dict(type_="release", name="ios-app 5.2 (UI polish)", component="app",
                                               owner="mobile-ios", spec=dict(platform="ios"))),
        (t("2026-11-16 09:00"), start_campaign, dict(name="NL paid social push", country="NL", uplift=0.35,
                                                     intent=1.0, cps=0.40, until=t("2026-11-17 06:00"))),
        (t("2026-11-16 09:10"), recs_release, dict(name="For You: discount-affinity boost (NL)",
                                                   discount_affinity={"NL": 0.13}, spec=dict(country="NL"))),
        (t("2026-11-16 09:00"), harmless, dict(type_="release", name="web header i18n strings (NL)",
                                               component="app", owner="web-platform",
                                               spec=dict(country="NL", platform="web"))),
        (t("2026-11-17 06:00"), recs_release, dict(name="For You: discount-affinity boost (NL) switched off",
                                                   discount_affinity={"NL": 0.0}, spec=dict(country="NL"),
                                                   planned=False, type_="rollback")),
        # --- payment SDK breaks the DE local payment method on Android (Tue 17 Nov)
        (t("2026-11-17 12:05"), harmless, dict(type_="release", name="web checkout button restyle",
                                               component="checkout", owner="checkout-web",
                                               spec=dict(platform="web"))),
        (t("2026-11-17 13:40"), deploy_payment_sdk, dict(platform="android", version="7.4.0",
                                                         breaks=("DE", "local"))),
        (t("2026-11-18 02:00"), deploy_payment_sdk, dict(platform="android", version="7.3.9",
                                                         rollback_of="7.4.0")),
        # --- FR teaser campaign (planned, email + push, low intent: innocent) (Wed 18 Nov)
        (t("2026-11-18 18:00"), start_campaign, dict(name="FR Black Week teaser campaign", country="FR",
                                                     uplift=0.6, intent=0.25, cps=0.0, owner="crm-fr",
                                                     until=t("2026-11-20 00:00"))),
        # --- search ranker 2.3 breaks German queries (Thu 19 Nov)
        (t("2026-11-19 08:50"), deploy_ranker, dict(version="2.3", country="DE", locale_bug=True)),
        (t("2026-11-19 10:30"), recs_release, dict(name="recommendations model refresh (For You)")),
        (t("2026-11-19 21:00"), deploy_ranker, dict(version="2.2", country="DE", planned=False, rollback=True)),
        # --- supplier delay announced, takes effect at the next morning delivery (Thu 19 / Fri 20 Nov)
        (t("2026-11-19 16:00"), supplier_delay, dict(category="electronics", from_ts=t("2026-11-20 06:00"),
                                                     until=t("2026-11-23 06:00"),
                                                     name="Delivery delayed: Voltix wholesale PO #4471")),
        (t("2026-11-20 05:30"), harmless, dict(type_="purchasing",
                                               name="Wholesale reorder run (electronics) at normal volume",
                                               component="wholesale", owner="purchasing-system",
                                               spec=dict(category="electronics"))),
        # --- orders ingestion lag at the end of the period (Sat 21 Nov)
        (t("2026-11-21 21:00"), late_data, dict(source="orders_db", shares=[0.55, 0.42, 0.38])),
    ]


SCENARIOS = [
    dict(id="foryou_unavailable", kind="fault", metric="cart_rate", segment={},
         start="2026-11-12 10:00", end="2026-11-12 22:00",
         cause="For You model v12 shipped without the inventory filter: it ranks by click-through, unavailable "
               "items float up, feed clicks rise while add-to-cart and orders fall",
         first_cause_event="For You model v12 (inventory filter off)"),
    dict(id="ads_pacing", kind="fault", metric="ad_cost", segment={"category": "home"},
         start="2026-11-13 06:00", end="2026-11-14 00:00",
         cause="ads-pacer 3.1 ignores the delivery schedule for home advertisers: it bids at the maximum from "
               "06:00, daily budgets are gone by late morning, sponsored traffic disappears for the evening peak",
         first_cause_event="ads-pacer 3.1 (home advertisers)"),
    dict(id="margin_squeeze", kind="fault", metric="contribution_margin", segment={"country": "NL"},
         start="2026-11-16 09:00", end="2026-11-17 06:00",
         cause="local metric up, business down: paid social buys traffic at high cost and the For You "
               "discount-affinity boost pushes discounted products; orders rise, contribution margin falls",
         first_cause_event=["NL paid social push", "For You: discount-affinity boost (NL)"],
         drivers=["ad_cost", "discount_share"], trap="orders"),
    dict(id="payment_failure", kind="fault", metric="payment_success_rate",
         segment={"country": "DE", "platform": "android"}, start="2026-11-17 14:00", end="2026-11-18 02:00",
         cause="payments-sdk android 7.4.0 breaks the DE local payment method",
         first_cause_event="payments-sdk android 7.4.0"),
    dict(id="fr_campaign", kind="innocent", metric="conversion_rate", segment={"country": "FR"},
         start="2026-11-18 18:00", end="2026-11-20 00:00",
         cause="planned FR Black Week teaser (email + push, no paid media): +60% low-intent traffic, conversion "
               "rate dilutes, orders up",
         first_cause_event="FR Black Week teaser campaign"),
    dict(id="search_relevance", kind="fault", metric="search_ctr", segment={"country": "DE"},
         start="2026-11-19 09:00", end="2026-11-19 21:00",
         cause="search-ranker 2.3 returns irrelevant / empty results for German queries",
         first_cause_event="search-ranker 2.3 (de locale)"),
    dict(id="stockout", kind="fault", metric="in_stock_rate", segment={"category": "electronics"},
         start="2026-11-20 06:00", end="2026-11-22 00:00",
         cause="supplier delivery delayed: just-in-time electronics run out after the missed 06:00 delivery, "
               "demand stable",
         first_cause_event="Delivery delayed: Voltix wholesale PO #4471"),
    dict(id="late_orders_data", kind="data", metric="orders", segment={},
         start="2026-11-21 21:00", end="2026-11-22 00:00",
         cause="orders_db ingestion lag: only ~40% of order rows arrived for the last hours",
         first_cause_event=None),
]


# ============================================================================= operations layer (marketplace/ops.py)
# Faults and innocent events that act through sellers, payment providers, capacity, bots, the feature pipeline,
# the calendar and the data pipeline. Each builder takes the onset hour t0 and returns (actions, start, end), so the
# same scenario runs in the fixed timeline and in live mode. `announce` = when planned events become known.
def calendar_entry(mp, ts, name, kind, country, start, end, expected_impact, metrics="", low=None, high=None):
    """A known event. Besides the free text, the expected impact is machine-readable, the way an ops calendar
    annotates it: which metrics move (';'-separated) and the expected relative change range [low, high]."""
    mp.ops.calendar.append(dict(name=name, kind=kind, country=country, start=str(pd.Timestamp(start))[:16],
                                end=str(pd.Timestamp(end))[:16], expected_impact=expected_impact,
                                metrics=metrics, expected_low=low, expected_high=high,
                                announced_at=str(pd.Timestamp(ts))[:16]))


def status_post(mp, ts, source, channel, component, region, severity, state, title):
    mp.ops.status.append(dict(ts=str(pd.Timestamp(ts))[:16], source=source, channel=channel, component=component,
                              region=region, severity=severity, state=state, title=title))


def traffic_event(mp, ts, name, kind, country, start, end, mult, expected_impact, low, high):
    mp.state.traffic_mods.append(dict(country=country, start=start, end=end, mult=mult))
    calendar_entry(mp, ts, name, kind, country, start, end, expected_impact, "sessions", low, high)


def search_maintenance(mp, ts, start, end, name):
    mp.state.search_maint.append((start, end, 0.12, 0.80))
    mp.log(start, "maintenance", name, "search", "search-infra", {}, planned=True, end=end, deploy_lead_min=0)
    calendar_entry(mp, ts, name, "maintenance", "*", start, end, "search partially unavailable, zero-result rate up",
                   "search_ctr;zero_result_rate", -0.45, 0.0)


def bot_traffic(mp, ts, country, platform, categories, sessions_per_hour, until):
    mp.state.bots.append(dict(country=country, platform=platform, categories=categories,
                              sessions_per_hour=sessions_per_hour, start=ts, until=until))


def psp_degradation(mp, ts, country, fail, until):
    mp.state.psp_degraded[country] = (fail, until)


def seller_feed_outage(mp, ts, seller, until):
    mp.state.seller_offline[seller] = (ts, until)


def checkout_config(mp, ts, pool, name, planned=True, type_="config"):
    mp.state.checkout_pool = pool / 200
    mp.log(ts, type_, name, "checkout", "checkout-platform", {}, planned=planned, deploy_lead_min=5)


def feature_pipeline_stall(mp, ts, category):
    mp.state.recs_stale[category] = ts


def freeze_source(mp, ts, source, until):
    mp.state.frozen[source] = (ts, until)


HOLIDAY_SHAPE = [0.85] * 7 + [0.9, 1.05, 1.3, 1.45, 1.5, 1.5, 1.5, 1.45, 1.4, 1.3, 1.2, 1.1, 1.0, 1.0, 1.0, 0.95, 0.9]


def b_search_maintenance(t0, announce=None):
    acts = [(announce or t0, search_maintenance, dict(start=t0, end=t0 + 2 * H,
                                                      name="Search index rebuild (planned maintenance)"))]
    return acts, t0, t0 + 2 * H


def b_click_fraud(t0, announce=None):
    end = t0 + 12 * H
    acts = [(t0, harmless, dict(type_="release", name="web search autocomplete tweak (PL)", component="search",
                                owner="search-frontend", spec=dict(country="PL", platform="web"))),
            (t0, bot_traffic, dict(country="PL", platform="web", categories=["home", "electronics"],
                                   sessions_per_hour=650, until=end)),
            (t0 + 20 * H, status_post, dict(source="internal", channel="#ads-quality", component="sponsored_ads",
                                            region="PL", severity="minor", state="investigating",
                                            title="Advertisers report many clicks from Poland without sales"))]
    return acts, t0, end


def b_psp_outage(t0, announce=None):
    end = t0 + 2 * H
    acts = [(t0, harmless, dict(type_="release", name="web checkout copy update (NL)", component="checkout",
                                owner="checkout-web", spec=dict(country="NL", platform="web"))),
            (t0, psp_degradation, dict(country="NL", fail=0.70, until=end)),
            (t0 + pd.Timedelta(minutes=25), status_post, dict(
                source="external", channel="status.payflow.example", component="card authorisation", region="NL",
                severity="major", state="investigating", title="PayFlow: degraded card authorisations in the Netherlands")),
            (end + pd.Timedelta(minutes=15), status_post, dict(
                source="external", channel="status.payflow.example", component="card authorisation", region="NL",
                severity="major", state="resolved", title="PayFlow: card authorisations recovered"))]
    return acts, t0, end


def b_holiday(country, name):
    def build(t0, announce=None):
        day = t0.normalize()
        acts = [(announce or t0, traffic_event, dict(name=name, kind="public_holiday", country=country, start=day,
                                                     end=day + 24 * H, mult=HOLIDAY_SHAPE,
                                                     expected_impact="more daytime shopping, sessions +15-50% 10-17h",
                                                     low=-0.25, high=0.60))]
        return acts, day, day + 24 * H
    return build


def b_seller_outage(t0, announce=None):
    end = t0 + 6 * H
    acts = [(t0, harmless, dict(type_="release", name="fashion category banner swap", component="app",
                                owner="web-platform", spec=dict(category="fashion"))),
            (t0, seller_feed_outage, dict(seller="Urbanfit", until=end)),
            (t0 + pd.Timedelta(minutes=150), status_post, dict(
                source="internal", channel="#seller-support", component="catalog-ingest", region="*", severity="minor",
                state="investigating", title="Seller Urbanfit reports offers missing from the marketplace"))]
    return acts, t0, end


def b_football(t0, announce=None):
    start = t0.normalize() + pd.Timedelta(hours=20)
    acts = [(announce or t0, traffic_event, dict(name="Football: Germany national team match (TV, 20:45)",
                                                 kind="tv_event", country="DE", start=start, end=start + 3 * H,
                                                 mult=0.80, expected_impact="DE evening traffic -20% during the match",
                                                 low=-0.35, high=0.0))]
    return acts, start + H, start + 3 * H


def b_checkout_capacity(t0, announce=None):
    cfg = t0 - pd.Timedelta(minutes=20)
    end = t0 + pd.Timedelta(hours=3, minutes=10)
    acts = [(cfg, checkout_config, dict(pool=60, name="checkout-db connection pool 200 -> 60 (config)")),
            (t0, harmless, dict(type_="flag", name="ios-app feature flag: gift-wrap option", component="app",
                                owner="mobile-ios", spec=dict(platform="ios"))),
            (t0 + pd.Timedelta(minutes=80), status_post, dict(
                source="internal", channel="#inc-checkout", component="checkout", region="*", severity="major",
                state="investigating", title="Checkout slow for some customers")),
            (end, checkout_config, dict(pool=200, name="checkout-db connection pool restored to 200", planned=False,
                                        type_="rollback"))]
    return acts, t0, end


def b_feed_drift(t0, announce=None):
    acts = [(t0 - pd.Timedelta(hours=5), harmless, dict(type_="release", name="data-platform: airflow 2.9 upgrade",
                                                        component="data_pipeline", owner="data-platform")),
            (t0, feature_pipeline_stall, dict(category="grocery"))]
    return acts, t0, t0 + 24 * 8 * H


def b_frozen_inventory(t0, announce=None):
    return [(t0, freeze_source, dict(source="inventory", until=t0 + 8 * H))], t0, t0 + 8 * H


def b_cdn_notice(t0, announce=None):
    post = dict(source="external", channel="status.cloudcdn.example", component="edge network", region="us-east",
                severity="minor")
    acts = [(t0, status_post, dict(post, state="investigating", title="CloudCDN: elevated latency in us-east")),
            (t0 + 2 * H, status_post, dict(post, state="resolved", title="CloudCDN: latency back to normal"))]
    return acts, t0, t0 + 2 * H


# ============================================================================= context layer (marketplace/context.py)
# Internal changes of the pricing, product and UI teams, and an external competitor move.
def price_update(mp, ts, category, top, pct, name, until):
    mp.state.price_changes.append(dict(category=category, top=top, pct=pct, start=ts, end=until))
    mp.log(ts, "pricing", name, "pricing", "pricing-team", {"category": category}, planned=True, deploy_lead_min=10)


def catalog_launch(mp, ts, category, n, name):
    mp.state.catalog_launches.append(dict(category=category, n=n, start=ts))
    mp.log(ts, "catalog", name, "catalog", "product-team", {"category": category}, planned=True)


def browser_bug(mp, ts, browser, attempt_mult, until, name):
    mp.state.browser_bugs.append(dict(browser=browser, attempt_mult=attempt_mult, start=ts, end=until))
    mp.log(ts, "release", name, "checkout", "checkout-web", {"platform": "web"}, planned=True)


def competitor_promo(mp, ts, competitor, country, category, discount, until, cart_loss, traffic_loss, name):
    mp.state.competitor_promos.append(dict(competitor=competitor, country=country, category=category, discount=discount,
                                           start=ts, end=until, cart_loss=cart_loss, traffic_loss=traffic_loss))
    mp.ctx.external.append(dict(ts=str(pd.Timestamp(ts))[:16], name=name, kind="competitor_promo", competitor=competitor,
                                country=country, category=category, discount=discount,
                                start=str(pd.Timestamp(ts))[:16], end=str(pd.Timestamp(until))[:16],
                                source="price monitoring"))


def b_price_increase(t0, announce=None):
    end = t0 + 10 * H
    acts = [(t0, price_update, dict(category="home", top=400, pct=0.25, until=end,
                                    name="home price update: +25% on 200 best sellers")),
            (t0, harmless, dict(type_="release", name="home product page: new review widget", component="app",
                                owner="web-platform", spec=dict(category="home"))),
            (end, harmless, dict(type_="rollback", name="home price update reverted", component="pricing",
                                 owner="pricing-team", spec=dict(category="home"), planned=False))]
    return acts, t0, end


def b_catalog_launch(t0, announce=None):
    acts = [(t0, catalog_launch, dict(category="fashion", n=1500,
                                      name="Autumn collection: 1,500 new fashion SKUs listed"))]
    return acts, t0, t0 + 12 * H


def b_safari_checkout(t0, announce=None):
    end = t0 + 6 * H
    acts = [(t0, browser_bug, dict(browser="Safari", attempt_mult=0.35, until=end,
                                   name="web checkout redesign (one-page checkout)")),
            (t0, harmless, dict(type_="release", name="android-app 5.3 (dark mode)", component="app",
                                owner="mobile-android", spec=dict(platform="android"))),
            (end, harmless, dict(type_="rollback", name="web checkout redesign rolled back", component="checkout",
                                 owner="checkout-web", spec=dict(platform="web"), planned=False))]
    return acts, t0, end


def b_competitor_promo(t0, announce=None):
    end = t0 + 9 * H
    acts = [(t0, competitor_promo, dict(competitor="MegaMart", country="DE", category="electronics", discount=0.20,
                                        until=end, cart_loss=0.32, traffic_loss=0.05,
                                        name="MegaMart electronics promo (DE)")),
            (t0, competitor_promo, dict(competitor="ShopNow", country="PL", category="fashion", discount=0.10,
                                        until=end, cart_loss=0.0, traffic_loss=0.0, name="ShopNow fashion promo (PL)"))]
    return acts, t0, end


EXTRA_BUILDERS = {
    "search_maintenance": b_search_maintenance, "click_fraud": b_click_fraud, "psp_outage": b_psp_outage,
    "holiday_fr": b_holiday("FR", "Armistice Day (public holiday)"),
    "holiday_pl": b_holiday("PL", "Independence Day (public holiday)"),
    "seller_outage": b_seller_outage, "football_de": b_football, "checkout_capacity": b_checkout_capacity,
    "grocery_feed_drift": b_feed_drift, "frozen_inventory": b_frozen_inventory,
    "price_increase": b_price_increase, "catalog_launch": b_catalog_launch, "safari_checkout": b_safari_checkout,
    "competitor_promo": b_competitor_promo,
}
EXTRA_T0 = {
    "search_maintenance": "2026-11-08 02:00", "click_fraud": "2026-11-09 13:00", "psp_outage": "2026-11-10 19:00",
    "holiday_fr": "2026-11-11 00:00", "holiday_pl": "2026-11-11 00:00", "seller_outage": "2026-11-14 09:00",
    "football_de": "2026-11-14 00:00", "checkout_capacity": "2026-11-15 19:00", "grocery_feed_drift": "2026-11-14 03:00",
    "frozen_inventory": "2026-11-18 00:00",
    "price_increase": "2026-11-10 08:00", "catalog_launch": "2026-11-19 06:00", "safari_checkout": "2026-11-21 09:00",
    "competitor_promo": "2026-11-15 08:00",
}
START_ANNOUNCE = pd.Timestamp("2026-11-01 00:00")      # planned / calendar events are known from the start
KNOWN_EVENTS = [  # calendar entries without an effect in the data (context the agent can use)
    ("St. Martin's Day", "cultural", "DE", "2026-11-11 00:00", "2026-11-12 00:00", "none expected", "", None, None),
    ("Nightly search index refresh", "batch_job", "*", "2026-11-01 03:00", "2026-11-22 00:00",
     "daily 03:00-03:15, no customer impact", "", None, None),
    ("Black Week starts", "sales_event", "*", "2026-11-23 00:00", "2026-11-30 00:00", "traffic +40-80%, discounts up",
     "sessions;discount_share", 0.40, 0.80),
    ("Black Friday", "sales_event", "*", "2026-11-27 00:00", "2026-11-28 00:00", "peak day of the year",
     "sessions;orders;gmv", 0.50, 2.00),
]
INCIDENT_POSTS = [  # internal channels: who already knows about core faults (and when)
    ("2026-11-13 12:30", "#ads-delivery", "sponsored_ads", "*", "minor", "investigating",
     "Home advertisers complain budgets were exhausted before noon"),
    ("2026-11-17 16:10", "#inc-payments", "payments", "DE", "major", "investigating",
     "Android payment failures in Germany"),
    ("2026-11-18 02:20", "#inc-payments", "payments", "DE", "major", "resolved",
     "Android payments SDK rolled back, failures gone"),
]


def extra_timeline():
    acts = []
    for name, build in EXTRA_BUILDERS.items():
        acts += build(pd.Timestamp(EXTRA_T0[name]), announce=START_ANNOUNCE)[0]
    acts += b_cdn_notice(pd.Timestamp("2026-11-06 14:00"))[0]              # irrelevant region: a decoy
    acts += [(START_ANNOUNCE, calendar_entry, dict(name=n, kind=k, country=c, start=a, end=b, expected_impact=i,
                                                   metrics=m, low=lo, high=hi))
             for n, k, c, a, b, i, m, lo, hi in KNOWN_EVENTS]
    acts += [(pd.Timestamp(ts), status_post, dict(source="internal", channel=ch, component=comp, region=reg,
                                                  severity=sev, state=st, title=title))
             for ts, ch, comp, reg, sev, st, title in INCIDENT_POSTS]
    return acts


def timeline(extended=True):
    """Core demo faults (+ operations-layer scenarios unless extended=False, which reproduces the original data)."""
    return core_timeline() + (extra_timeline() if extended else [])


EXTRA_SCENARIOS = [
    dict(id="search_maintenance", kind="innocent", metric="search_ctr", segment={},
         start="2026-11-08 02:00", end="2026-11-08 04:00",
         cause="planned search index rebuild at night: partial results, zero-result rate up, announced in the calendar",
         first_cause_event="Search index rebuild (planned maintenance)"),
    dict(id="click_fraud", kind="fault", metric="sponsored_share", segment={"country": "PL", "platform": "web"},
         start="2026-11-09 13:00", end="2026-11-10 01:00",
         cause="bot network clicks sponsored ads (home, electronics) on PL web: sessions, ad clicks and ad revenue up, "
               "no carts, advertisers billed for invalid clicks; IVT detector flags only part of it",
         first_cause_event=None, evidence=["ivt_sessions", "ads_advertisers.invalid_clicks"]),
    dict(id="psp_outage", kind="fault", metric="payment_success_rate", segment={"country": "NL"},
         start="2026-11-10 19:00", end="2026-11-10 21:00",
         cause="external payment provider degrades card authorisations in NL (all platforms); status page posted "
               "25 min later; a NL web checkout release at the same time is a decoy",
         first_cause_event=None, evidence=["status_feed: PayFlow"]),
    dict(id="holiday_fr", kind="innocent", metric="sessions", segment={"country": "FR"},
         start="2026-11-11 00:00", end="2026-11-12 00:00",
         cause="public holiday (calendar): more daytime shopping", first_cause_event=None, evidence=["calendar"]),
    dict(id="holiday_pl", kind="innocent", metric="sessions", segment={"country": "PL"},
         start="2026-11-11 00:00", end="2026-11-12 00:00",
         cause="public holiday (calendar): more daytime shopping", first_cause_event=None, evidence=["calendar"]),
    dict(id="seller_outage", kind="fault", metric="cart_rate", segment={"category": "fashion"},
         start="2026-11-14 09:00", end="2026-11-14 15:00",
         cause="top fashion seller Urbanfit's catalog feed fails: offers go stale and are hidden, fashion availability "
               "and cart rate drop; seller silence, no change event (banner swap is a decoy)",
         first_cause_event=None, evidence=["sellers.feed_status", "service_health: catalog-ingest"]),
    dict(id="football_de", kind="innocent", metric="sessions", segment={"country": "DE"},
         start="2026-11-14 21:00", end="2026-11-14 23:00",
         cause="national team match on TV (calendar): DE evening traffic -20%", first_cause_event=None,
         evidence=["calendar"]),
    dict(id="checkout_capacity", kind="fault", metric="payment_attempt_rate", segment={},
         start="2026-11-15 19:00", end="2026-11-15 22:10",
         cause="config change cut the checkout DB connection pool 200 -> 60: at the Sunday evening peak checkout p99 "
               "latency explodes and customers abandon before paying",
         first_cause_event="checkout-db connection pool 200 -> 60 (config)", evidence=["service_health: checkout"]),
    dict(id="grocery_feed_drift", kind="fault", metric="feed_click_rate", segment={"category": "grocery"},
         start="2026-11-14 03:00", end="2026-11-22 00:00",
         cause="feature pipeline for grocery recommendations silently stopped after the airflow upgrade: features go "
               "stale, For You CTR drifts down ~4.5 %/day while predicted CTR stays (calibration gap)",
         first_cause_event="data-platform: airflow 2.9 upgrade", pattern="drift",
         evidence=["feed_exposure.feature_freshness_h", "feed_exposure.predicted_ctr"]),
    dict(id="frozen_inventory", kind="data", metric="in_stock_skus", segment={},
         start="2026-11-18 00:00", end="2026-11-18 08:00",
         cause="inventory pipeline re-sends the same snapshot: values stop moving, completeness says 100 %, only "
               "last_update / lag in pipeline_health reveals it", first_cause_event=None, pattern="variance_collapse"),
    dict(id="price_increase", kind="fault", metric="cart_rate", segment={"category": "home"},
         start="2026-11-10 08:00", end="2026-11-10 18:00",
         cause="pricing team raises prices 25% on every other of the 400 best-selling home SKUs: add-to-cart falls on "
               "exactly those SKUs (counterfactual: unchanged SKUs normal); review-widget release is a decoy",
         first_cause_event="home price update: +25% on 200 best sellers", evidence=["product_sales.price_change"]),
    dict(id="catalog_launch", kind="innocent", metric="listed_skus", segment={"category": "fashion"},
         start="2026-11-19 06:00", end="2026-11-19 18:00",
         cause="product team lists 1,500 new fashion SKUs (planned launch): catalog size jumps, nothing breaks",
         first_cause_event="Autumn collection: 1,500 new fashion SKUs listed"),
    dict(id="competitor_promo", kind="fault", metric="cart_rate", segment={"country": "DE", "category": "electronics"},
         start="2026-11-15 08:00", end="2026-11-15 17:00",
         cause="competitor MegaMart runs a -20% electronics promotion in DE: our price index jumps, customers compare "
               "and buy there; no internal change (a PL fashion promo by a small player at the same time is a decoy)",
         first_cause_event="MegaMart electronics promo (DE)", evidence=["market.price_index", "external_events"]),
    dict(id="safari_checkout", kind="fault", metric="payment_attempt_rate", segment={"platform": "web", "browser": "Safari"},
         start="2026-11-21 09:00", end="2026-11-21 15:00",
         cause="web checkout redesign breaks the pay button on Safari only: web payment attempts drop, concentrated "
               "in Safari (segment isolation by browser); an Android app release at the same time is a decoy",
         first_cause_event="web checkout redesign (one-page checkout)", evidence=["audience.browser"]),
]
ALL_SCENARIOS = SCENARIOS + EXTRA_SCENARIOS
