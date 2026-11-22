"""The fake Amazon-like marketplace: components with versions and settings, simulated hour by hour.

Each component is real logic with state. A fault is a *change to a component* (new version, setting,
supplier delay), applied by an action that also writes the change log, exactly like a real deploy would.
Nothing is multiplied onto the output afterwards: the numbers move because behaviour changed.

Components (Amazon-shaped, from public knowledge):
  marketing        campaigns that bring extra sessions with their own buying intent and media cost
  sponsored_ads    advertisers per category with daily budgets; the pacing controller spreads spend over the day
  search           ranker per country: relevance -> how many searches lead to a product page, zero-result share
  recommendations  "For You" feed: model version, inventory filter, discount affinity
  product_page     availability (popularity-weighted in-stock share) -> add to cart
  promotions       discount level on what is bought
  checkout         checkout flow
  payments         payment SDK per platform; DE customers also pay with a local method
  wholesale        suppliers deliver every morning at 06:00; a delayed delivery empties their SKUs
  data_pipeline    ingestion completeness per source (late rows)

Output is described in docs/DATA_OVERVIEW.md: facts, inventory, events, completeness (+ injected.json ground truth).
"""
from __future__ import annotations

import copy
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from .catalog import (AOV, CART_RATE, CATEGORIES, COGS, COUNTRIES, LISTED, LOCAL_METHOD_SHARE, PAY_OK, PLATFORMS,
                      WEEKLY, Catalog, hourly_shape)
from .context import Context
from .ops import Ops
from .users import UserPanel

H = pd.Timedelta(hours=1)
TOP_PRODUCTS = 50
ADVERTISERS = {"electronics": ["Voltix", "Nordtek", "Pixelon"], "fashion": ["Mode Haus", "Urbanfit", "Linea"],
               "home": ["Casa Verde", "Hearthly", "Moebelix"], "grocery": ["FreshCart", "Bio Markt", "Snackbox"]}
ADV_SHARE = [0.5, 0.3, 0.2]
MONEY = ("gmv", "discounts", "cogs", "ad_cost", "ad_revenue", "marketing_cost", "gmv_3p", "marketplace_fees",
         "retail_gross_profit", "fulfilment_cost", "payment_cost")
SEG = pd.MultiIndex.from_product([list(COUNTRIES), list(PLATFORMS), list(CATEGORIES)],
                                 names=["country", "platform", "category"]).to_frame(index=False)
N = len(SEG)
SOURCES = {"web_analytics": ["sessions", "searches", "zero_result_searches", "product_views", "add_to_carts",
                             "feed_clicks", "ivt_sessions"],
           "orders_db": ["checkouts", "payment_attempts", "orders", "gmv", "refunds", "discounts", "cogs",
                         "retail_gross_profit", "fulfilment_cost", "payment_cost",
                         "gmv_3p", "marketplace_fees", "chargebacks"],
           "ads_platform": ["ad_cost", "sponsored_sessions", "ad_impressions", "ad_revenue", "marketing_cost"],
           "inventory": ["listed_skus", "in_stock_skus"]}


def _mask(spec: dict) -> np.ndarray:
    m = np.ones(N, bool)
    for dim in ("country", "platform", "category"):
        v = spec.get(dim, "*")
        if v not in (None, "*"):
            m &= (SEG[dim] == v).to_numpy()
    return m


@dataclass
class State:
    """All component settings. Actions change this; the simulation reads it every hour."""
    # search: ranker version per country and its quality
    ranker: dict = field(default_factory=lambda: {c: "2.2" for c in COUNTRIES})
    ranker_quality: dict = field(default_factory=lambda: {"2.2": (0.60, 0.05)})          # (ctr, zero-result share)
    ranker_locale_bug: dict = field(default_factory=dict)                                # version -> country
    # payments: SDK version per platform; versions that break a payment method in a country
    pay_sdk: dict = field(default_factory=lambda: {"android": "7.3.9", "ios": "5.1.0", "web": "web-2026.11"})
    pay_broken: dict = field(default_factory=dict)            # version -> (country, method)
    # recommendations ("For You")
    recs_model: str = "v11"
    recs_inventory_filter: bool = True
    recs_oos_bias: float = 1.0            # how strongly the model favours attractive-but-unavailable items
    recs_discount_affinity: dict = field(default_factory=dict)   # country -> extra discount share pushed
    # sponsored ads
    pacer: str = "3.0"
    pacer_ignores_schedule: set = field(default_factory=set)     # categories whose campaigns front-load
    sponsored_share: float = 0.20          # share of sessions that come in through sponsored placements
    cpc: float = 0.60                      # EUR per sponsored session
    # marketing campaigns: list of dicts(country, uplift, intent, paid, cps_mult, until)
    campaigns: list = field(default_factory=list)
    # wholesale: delayed supplier deliveries per category: (effective from, until)
    delivery_delay: dict = field(default_factory=dict)
    # data pipeline: completeness overrides per source (ts -> share)
    late_rows: dict = field(default_factory=dict)
    # ---- operations layer (marketplace/ops.py); all empty = no effect on any number
    traffic_mods: list = field(default_factory=list)       # holidays, TV events: dict(country, start, end, mult)
    search_maint: list = field(default_factory=list)       # (start, end, extra zero-result share, ctr multiplier)
    psp_degraded: dict = field(default_factory=dict)       # country -> (share of card payments failing, until)
    checkout_pool: float = 1.0                             # checkout DB capacity relative to normal
    seller_offline: dict = field(default_factory=dict)     # seller -> (from, until): feed failing, offers hidden
    recs_stale: dict = field(default_factory=dict)         # category -> since: For You features no longer refreshed
    bots: list = field(default_factory=list)               # dict(country, platform, categories, sessions_per_hour, ..)
    frozen: dict = field(default_factory=dict)             # source -> (from, until): pipeline re-sends old snapshot
    flash_sales: list = field(default_factory=list)        # planned promos: dict(country, category, start, end, uplift, discount)
    silent_bugs: list = field(default_factory=list)        # unannounced bugs: dict(country, platform, start, end, checkout_mult)
    # ---- context layer (marketplace/context.py)
    price_changes: list = field(default_factory=list)      # pricing team: dict(category, top, pct, start, end)
    catalog_launches: list = field(default_factory=list)   # product team: dict(category, n, start)
    browser_bugs: list = field(default_factory=list)       # UI team: dict(browser, attempt_mult, start, end)
    competitor_promos: list = field(default_factory=list)  # external: dict(competitor, country, category, discount, ...)


class Marketplace:
    def __init__(self, seed: int = 7, start: str = "2026-11-01 00:00", user_sample: float = 0.0, days: int = 21):
        self.seed = seed
        self.start = pd.Timestamp(start)
        self.rng0 = np.random.default_rng(seed)
        self.catalog = Catalog(self.rng0)
        self.state = State()
        self.shape = hourly_shape()
        self.events: list[dict] = []
        self.facts: list[pd.DataFrame] = []
        self.inventory: list[pd.DataFrame] = []
        self.completeness: list[pd.DataFrame] = []
        self.products: list[pd.DataFrame] = []      # top products per category: stock status (drill-down)
        self.feed: list[dict] = []                  # For You exposure per category (drill-down)
        self.ads: list[dict] = []                   # sponsored ads per advertiser (drill-down)
        # wholesale: SKUs supplied by just-in-time suppliers (daily 06:00 delivery, ~1 day of cover)
        self.jit = {cat: self.rng0.random(n) < (0.36 if cat == "electronics" else 0.0) for cat, n in LISTED.items()}
        self.ads_spent_today: dict = {}
        self.out = {cat: self.catalog.stock_days[cat] <= 0 for cat in LISTED}
        for cat in LISTED:
            self.catalog.stock_days[cat][self.jit[cat]] = 1.0
        # user-level panel (own random streams: does not change any aggregate table)
        self.panel = UserPanel(seed, user_sample, days=days, start=start) if user_sample > 0 else None
        self.ops = Ops(seed, self.catalog, self.jit, start)    # ads auctions, sellers, services, support, ...
        self.ctx = Context(seed, self.catalog, start)          # audience split, product sales, market

    # ------------------------------------------------------------------ change log
    def log(self, ts, type_, name, component, owner, spec=None, planned=True, end=None, deploy_lead_min=20):
        spec = spec or {}
        ts = pd.Timestamp(ts)
        self.events.append(dict(type=type_, name=name, component=component, owner=owner,
                                deployed_at=str(ts - pd.Timedelta(minutes=deploy_lead_min))[:16],
                                first_exposure_at=str(ts)[:16],
                                country=spec.get("country", "*"), platform=spec.get("platform", "*"),
                                category=spec.get("category", "*"), planned=planned,
                                ts=str(ts)[:16], end=str(pd.Timestamp(end))[:16] if end is not None else ""))

    # ------------------------------------------------------------------ one hour
    def step(self, ts: pd.Timestamp):
        s = self.state
        rng = np.random.default_rng([self.seed, int((ts - self.start) / H)])
        hour, dow = ts.hour, ts.day_of_week
        self.ops.begin(ts)
        self.ctx.begin(ts)
        offline = self.ops.offline_sellers(s, ts)

        # campaigns expire
        s.campaigns = [c for c in s.campaigns if pd.Timestamp(c["until"]) > ts]
        for cat, (frm, until) in list(s.delivery_delay.items()):
            if pd.Timestamp(until) <= ts:
                del s.delivery_delay[cat]
        delayed = {cat for cat, (frm, _u) in s.delivery_delay.items() if ts >= pd.Timestamp(frm)}

        # ---------------- wholesale + inventory (per SKU)
        inv_rows, avail, avail0, stock_now = [], {}, {}, {}
        for cat, cover in self.catalog.stock_days.items():
            jit = self.jit[cat]
            out = self.out[cat]
            # regular SKUs: now and then one sells out; restocked at the 06:00 delivery (about half per day)
            out |= (~jit) & (rng.random(len(out)) < 0.001)
            if hour == 6:
                back = out & (~jit) & (rng.random(len(out)) < 0.5)
                out &= ~back
            # just-in-time SKUs (wholesale supplier): ~1 day of cover, refilled by the 06:00 delivery
            cover[jit] -= 1 / 24
            if hour == 6 and cat not in delayed:
                cover[jit] = rng.uniform(0.9, 1.4, jit.sum())
            if cat not in delayed:
                cover[jit] = np.maximum(cover[jit], 0.05)            # never dry between deliveries normally
            in_stock = ~out & ~(jit & (cover <= 0))
            avail0[cat] = float((self.catalog.pop[cat] * in_stock).sum())
            if offline:                                              # stale seller feed -> offers hidden
                in_stock = in_stock & ~self.ops.owned_by(cat, offline)
            avail[cat] = float((self.catalog.pop[cat] * in_stock).sum())
            stock_now[cat] = in_stock
            inv_rows.append(dict(ts=ts, category=cat, listed_skus=LISTED[cat], in_stock_skus=int(in_stock.sum())))
            top = np.argsort(-self.catalog.pop[cat])[:TOP_PRODUCTS]
            self.products.append(pd.DataFrame(dict(
                ts=ts, category=cat, sku=[f"{cat[:3].upper()}-{i:05d}" for i in top], popularity_rank=np.arange(1, len(top) + 1),
                view_share=self.catalog.pop[cat][top].round(5), in_stock=in_stock[top].astype(int),
                supplier=np.where(jit[top], "Voltix (JIT)", "regular stock"))))

        # ---------------- traffic: organic + sponsored + campaigns
        base = (SEG.country.map(COUNTRIES) * SEG.platform.map(PLATFORMS) * SEG.category.map(CATEGORIES)).to_numpy()
        season = self.shape[hour] * WEEKLY[dow]
        lam = base * season * rng.lognormal(0, 0.05, N)
        sponsored_lam = lam * s.sponsored_share
        organic_lam = lam - sponsored_lam

        # sponsored ads pacing: the daily budget is what an evenly paced day spends. A controller that
        # ignores the schedule bids at the maximum from 06:00 (higher CPC, more placements won) until it is gone.
        day = ts.normalize()
        cps = np.full(N, s.cpc)
        for cat in CATEGORIES:
            key = (day, cat)
            m = (SEG.category == cat).to_numpy()
            if cat in s.pacer_ignores_schedule:
                budget = float((base[m] * WEEKLY[dow] * s.sponsored_share).sum() * 24 * s.cpc)
                spent = self.ads_spent_today.get(key, 0.0)
                if hour >= 6:
                    cps[m] = s.cpc * 3.0
                    want = sponsored_lam[m] * 2.5
                    cost = float(want.sum() * cps[m][0])
                    left = max(budget - spent, 0.0)
                    sponsored_lam[m] = want * (min(1.0, left / cost) if cost > 0 else 0.0)
            spend = float((sponsored_lam[m] * cps[m]).sum())
            self.ads_spent_today[key] = self.ads_spent_today.get(key, 0.0) + spend
            budget_day = float((base[m] * WEEKLY[dow] * s.sponsored_share).sum() * 24 * s.cpc)
            for adv, sh in zip(ADVERTISERS[cat], ADV_SHARE):
                self.ads.append(dict(ts=ts, advertiser=adv, category=cat, pacer=s.pacer if cat in s.pacer_ignores_schedule else "3.0",
                                     daily_budget=round(budget_day * sh, 2), spend=round(spend * sh, 2),
                                     spent_today=round(self.ads_spent_today[key] * sh, 2),
                                     budget_used=round(self.ads_spent_today[key] / budget_day, 3) if budget_day else 0.0,
                                     cpc=round(float(cps[m][0]), 2)))

        camp_lam = np.zeros(N)
        intent_num = lam.copy()
        camp_cost = np.zeros(N)
        for c in s.campaigns:
            extra = lam * c["uplift"] * _mask(c)
            camp_lam += extra
            intent_num += extra * c["intent"]
            camp_cost += extra * c.get("cps", 0.0)          # paid social / display; 0 for email + push
        intent = intent_num / np.maximum(lam + camp_lam, 1e-9)

        # ---------------- search quality per country
        ctr = np.empty(N)
        zero = np.empty(N)
        for c in COUNTRIES:
            v = s.ranker[c]
            q, z = s.ranker_quality.get(v, (0.60, 0.05))
            if s.ranker_locale_bug.get(v) == c:
                q, z = q * 0.58, 0.21                                       # German queries get wrong results
            m = (SEG.country == c).to_numpy()
            ctr[m], zero[m] = q, z

        # ---------------- recommendations ("For You")
        def exposure(av):
            e = np.zeros(N)
            if not s.recs_inventory_filter:
                # without the filter the model ranks by predicted click-through: unavailable items have
                # high historical CTR (people click, cannot buy) and float to the top
                for cat in CATEGORIES:
                    e[(SEG.category == cat).to_numpy()] = min(0.85, (1 - av[cat]) * s.recs_oos_bias)
            return e
        oos_exposure = exposure(avail)
        for cat in CATEGORIES:
            m = (SEG.category == cat).to_numpy()
            self.feed.append(dict(ts=ts, category=cat, model=s.recs_model, inventory_filter=s.recs_inventory_filter,
                                  out_of_stock_exposure=round(float(oos_exposure[m][0]), 3),
                                  catalog_out_of_stock=round(1 - avail[cat], 3)))

        # ---------------- payments: success probability per segment
        pay = SEG.platform.map(PAY_OK).to_numpy().copy()
        for plat, ver in s.pay_sdk.items():
            broken = s.pay_broken.get(ver)
            if broken:
                c, _method = broken
                m = ((SEG.platform == plat) & (SEG.country == c)).to_numpy()
                local = LOCAL_METHOD_SHARE[c]
                pay[m] = pay[m] * (1 - local) + 0.05 * local          # local method fails almost always

        disc_push = np.array([s.recs_discount_affinity.get(c, 0.0) for c in SEG.country])
        x0 = dict(organic=organic_lam, sponsored=sponsored_lam, camp=camp_lam, intent=intent, camp_cost=camp_cost,
                  cps=cps, ctr=ctr, zero=zero, oos=exposure(avail0), feed_mult=np.ones(N),
                  avail=SEG.category.map(avail0).to_numpy(), disc_push=disc_push, pay=pay, checkout_pool=1.0,
                  checkout_mult=np.ones(N), cart_mult=np.ones(N), attempt_mult=np.ones(N))
        out = self._funnel(rng, x0)

        # ---------------- operations-layer conditions (holidays, maintenance, sellers, features, capacity, PSP).
        # The baseline draw above always uses the main random stream exactly as before; segments whose conditions
        # differ are drawn again from the operations stream, so nothing else moves.
        x1 = dict(x0, oos=oos_exposure, avail=SEG.category.map(avail).to_numpy())
        if s.traffic_mods:                                           # holidays, TV events
            k = self.ops.traffic_mult(s, ts, SEG)
            x1.update(organic=organic_lam * k, sponsored=sponsored_lam * k, camp=camp_lam * k, camp_cost=camp_cost * k)
        for a, b, dz, cm in s.search_maint:                          # planned index rebuild: partial results
            if pd.Timestamp(a) <= ts < pd.Timestamp(b):
                x1.update(zero=np.minimum(zero + dz, 1.0), ctr=ctr * cm)
        if s.recs_stale:                                             # stale features: less relevant recommendations
            x1["feed_mult"] = self.ops.feed_decay(s, ts, SEG)
        pay1 = pay.copy()
        for c, (fail, until) in s.psp_degraded.items():              # payment provider: card authorisations fail
            if ts < pd.Timestamp(until):
                m = (SEG.country == c).to_numpy()
                pay1[m] = pay1[m] * (1 - (0.35 if LOCAL_METHOD_SHARE[c] > 0 else 0.60) * fail)
        x1["pay"], x1["checkout_pool"] = pay1, s.checkout_pool
        for fs in s.flash_sales:                                     # planned flash sale: more traffic, deeper discounts
            if pd.Timestamp(fs["start"]) <= ts < pd.Timestamp(fs["end"]):
                m = _mask(fs)
                k = np.where(m, 1.0 + fs["uplift"], 1.0)
                x1.update(organic=x1["organic"] * k, sponsored=x1["sponsored"] * k, camp=x1["camp"] * k,
                          disc_push=np.where(m, x1["disc_push"] + fs["discount"], x1["disc_push"]))
        for bug in s.silent_bugs:                                    # unannounced bug: no change-log entry at all
            if pd.Timestamp(bug["start"]) <= ts < pd.Timestamp(bug["end"]):
                x1["checkout_mult"] = np.where(_mask(bug), bug["checkout_mult"], x1["checkout_mult"])
        # context layer: pricing updates, competitor promotions, browser-specific UI bugs
        x1["cart_mult"] = x1["cart_mult"] * self.ctx.cart_mult(s, ts, SEG)
        x1["attempt_mult"] = x1["attempt_mult"] * self.ctx.attempt_mult(s, ts, SEG)
        if s.competitor_promos:
            k = self.ctx.traffic_mult(s, ts, SEG)
            x1.update(organic=x1["organic"] * k, sponsored=x1["sponsored"] * k)
        affected = np.zeros(N, bool)
        for key, v in x1.items():
            if isinstance(v, np.ndarray):
                affected |= ~np.isclose(v, x0[key], rtol=0, atol=1e-12)
        if s.checkout_pool < 1.0:
            affected[:] = True
        if affected.any():
            alt = self._funnel(self.ops.rng, x1)
            out = {k: np.where(affected, alt[k], v) for k, v in out.items()}
        lam, camp_lam = x1["organic"] + x1["sponsored"], x1["camp"]

        f = SEG.copy()
        f.insert(0, "ts", ts)
        for col in ("sessions", "searches", "zero_result_searches", "product_views", "add_to_carts", "checkouts",
                    "payment_attempts", "orders", "gmv", "refunds", "discounts", "cogs", "ad_cost", "feed_clicks",
                    "sponsored_sessions"):
            f[col] = out[col]

        # ---------------- user panel: sample sessions of this hour, played out customer by customer
        bots = self.ops.bots(s, ts, SEG)
        if self.panel is not None:
            self.panel.step(self, ts, f, stock_now, oos_exposure, lam, camp_lam, bots=bots)
        # ---------------- operations layer: bots join the traffic, auctions, sellers, services, support, pipeline
        self.ops.hour(self, ts, f, avail, oos_exposure, cps, stock_now, bots)

        # ---------------- data pipeline: late rows
        comp = []
        for src, cols in SOURCES.items():
            share = s.late_rows.get((src, ts), 1.0)
            comp.append(dict(ts=ts, source=src, completeness=share))
            if share < 1.0:
                for col in cols:
                    if col in f:
                        f[col] = (f[col] * share).round(2 if col in MONEY else 0)
        self.facts.append(f)
        self.ctx.hour(self, ts, f, stock_now, lam, camp_lam, bots)
        inv_rows = self.ctx.catalog_overlay(s, ts, inv_rows)
        self.inventory.append(pd.DataFrame(self.ops.freeze_inventory(s, ts, inv_rows)))
        self.completeness.append(pd.DataFrame(comp))

    # ------------------------------------------------------------------ funnel (one draw per hour)
    def _funnel(self, rng, x):
        """Sessions -> search -> product views -> cart -> checkout -> payment -> money for all segments.
        The order of random draws is fixed: same inputs + same rng reproduce the original model exactly."""
        sessions = rng.poisson(x["organic"] + x["sponsored"] + x["camp"])
        sponsored = np.minimum(rng.poisson(x["sponsored"]), sessions)
        # search
        searches = rng.binomial(sessions, 0.55)
        zero_res = rng.binomial(searches, x["zero"])
        search_views = rng.poisson(searches * x["ctr"])
        # recommendations ("For You")
        feed_rate = 0.12                                       # feed product clicks per session
        attractive = (1.0 + 1.0 * x["oos"]) * x["feed_mult"]   # more attractive items -> more clicks
        feed_clicks = rng.poisson(sessions * feed_rate * attractive)
        product_views = search_views + feed_clicks
        # product page -> cart
        cart_p = (SEG.category.map(CART_RATE).to_numpy() * (x["avail"] / 0.955) * x["intent"]
                  * (1 + 1.15 * x["disc_push"]) * x["cart_mult"])
        # feed clicks on unavailable items cannot be added to the cart
        feed_cartable = feed_clicks * (1 - x["oos"])
        cart_p_eff = cart_p * (search_views + feed_cartable) / np.maximum(product_views, 1)
        add_to_carts = rng.binomial(product_views, np.clip(cart_p_eff, 0, 1))
        # checkout + payments
        checkouts = rng.binomial(add_to_carts, 0.50 * x["checkout_mult"])
        if x["checkout_pool"] < 1.0:                           # checkout capacity cut: slow pages lose attempts
            attempts = rng.binomial(checkouts, 0.92 * x["attempt_mult"]
                                    * self.ops.checkout_load(self.state, SEG, checkouts))
        else:
            attempts = rng.binomial(checkouts, 0.92 * x["attempt_mult"])
        orders = rng.binomial(attempts, x["pay"])
        # money
        aov = SEG.category.map(AOV).to_numpy() * rng.lognormal(0, 0.08, N)
        gmv = (orders * aov).round(2)
        disc_share = (0.08 + x["disc_push"]) * rng.lognormal(0, 0.04, N)
        discounts = (gmv * disc_share).round(2)
        cogs = (gmv * SEG.category.map(COGS).to_numpy() * rng.lognormal(0, 0.02, N)).round(2)
        refunds = rng.binomial(orders, 0.04)
        ad_cost = (sponsored * x["cps"] + x["camp_cost"] * rng.lognormal(0, 0.06, N)).round(2)
        return dict(sessions=sessions, searches=searches, zero_result_searches=zero_res, product_views=product_views,
                    add_to_carts=add_to_carts, checkouts=checkouts, payment_attempts=attempts, orders=orders, gmv=gmv,
                    refunds=refunds, discounts=discounts, cogs=cogs, ad_cost=ad_cost, feed_clicks=feed_clicks,
                    sponsored_sessions=sponsored)

    # ------------------------------------------------------------------ outputs
    def tables(self):
        facts = pd.concat(self.facts, ignore_index=True)
        for col in ("sessions", "searches", "zero_result_searches", "product_views", "add_to_carts", "checkouts",
                    "payment_attempts", "orders", "refunds", "feed_clicks", "sponsored_sessions", "ad_impressions",
                    "chargebacks", "ivt_sessions"):
            facts[col] = facts[col].astype(int)
        inv = pd.concat(self.inventory, ignore_index=True)
        comp = pd.concat(self.completeness, ignore_index=True)
        ev = pd.DataFrame(self.events, columns=["type", "name", "component", "owner", "deployed_at",
                                                "first_exposure_at", "country", "platform", "category", "planned",
                                                "ts", "end"])
        return facts, inv, ev, comp

    def ops_tables(self):
        return {**self.ops.tables(), **self.ctx.tables()}

    def user_tables(self):
        return self.panel.tables() if self.panel is not None else None

    def detail_tables(self):
        return (pd.concat(self.products, ignore_index=True), pd.DataFrame(self.feed), pd.DataFrame(self.ads))
