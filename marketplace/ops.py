"""Operations layer around the marketplace: the signals a real monitoring team has besides the funnel.

Everything here is driven by the same component state as world.py (pacer, payments, sellers, checkout capacity,
feature pipeline, ...), so a fault shows up consistently across tables: business metrics, ad auctions, service
health, support tickets and data-pipeline health. Own random streams: none of the core numbers change unless a
scenario that acts through this layer is active.

Tables (written by run.py next to the data contract):
  ads_auctions.csv        hour x category: ad requests, fill, no-fill reasons, impressions, clicks, CPC, RPM
  sellers.csv             hour x seller: 1P / 3P, listings, active offers, orders, GMV, commission, feed sync health
  service_health.csv      hour x service x platform: requests, errors, p99 latency, capacity utilisation
  app_health.csv          hour x platform: app version, payment SDK, crashes, crash-free sessions
  support_tickets.csv     hour x country x topic: tickets, sentiment, first response time
  support_ticket_samples.csv  sampled ticket subjects in the customer's language (for evidence / LLM review)
  pipeline_health.csv     hour x source: completeness, lag, last update, heartbeat
  calendar.csv            known events: holidays, maintenance windows, TV events, sales periods
  status_feed.csv         third-party status pages and internal incident channels ("who already knows?")
New columns in facts.csv: ad_impressions, ad_revenue, marketing_cost, gmv_3p, marketplace_fees, chargebacks,
ivt_sessions. Extra columns in ads_advertisers.csv and feed_exposure.csv.
"""
from __future__ import annotations

import re
from collections import deque

import numpy as np
import pandas as pd

from .catalog import CATEGORIES, COUNTRIES, LISTED, LOCAL_METHOD_SHARE, PLATFORMS, WEEKLY, hourly_shape

H = pd.Timedelta(hours=1)

# ---------------------------------------------------------------- sellers (share of the category's demand)
SELLERS = {
    "electronics": [("Retail (1P)", 0.40), ("Voltix", 0.18), ("Nordtek", 0.12), ("Pixelon", 0.08), ("GadgetHub", 0.07),
                    ("long-tail sellers", 0.15)],
    "fashion": [("Retail (1P)", 0.20), ("Urbanfit", 0.22), ("Mode Haus", 0.15), ("Linea", 0.10), ("StyleLoop", 0.08),
                ("long-tail sellers", 0.25)],
    "home": [("Retail (1P)", 0.35), ("Casa Verde", 0.15), ("Hearthly", 0.12), ("Moebelix", 0.10), ("NordNest", 0.06),
             ("long-tail sellers", 0.22)],
    "grocery": [("Retail (1P)", 0.55), ("FreshCart", 0.15), ("Bio Markt", 0.10), ("Snackbox", 0.06),
                ("long-tail sellers", 0.14)],
}
TAKE_RATE = {"electronics": 0.08, "fashion": 0.15, "home": 0.15, "grocery": 0.08}   # commission on 3P GMV

# ---------------------------------------------------------------- sponsored products auction
SEARCH_SLOTS, PAGE_SLOTS = 8, 4            # sponsored slots per search result page / product page
AD_CTR = 0.038                             # clicks per sponsored impression
NO_ELIGIBLE = 0.10                         # requests where no campaign targets the query / product
TIMEOUTS = 0.004
FLOOR_CPC = 0.30

# ---------------------------------------------------------------- services: base p99 (ms), base error rate,
# requests per unit of driver, driver column
SERVICES = {
    "api-gateway": (300, 0.002, 12.0, "sessions"),
    "search": (420, 0.002, 3.0, "searches"),
    "recommendations": (380, 0.002, 1.1, "sessions"),
    "catalog": (250, 0.001, 2.0, "product_views"),
    "cart": (180, 0.001, 3.0, "add_to_carts"),
    "checkout": (900, 0.003, 4.0, "checkouts"),
    "payments": (1400, 0.004, 1.2, "payment_attempts"),
    "ads-server": (90, 0.002, 1.0, "ad_requests"),
}
PER_SESSION = {"sessions": 1.0, "searches": 0.55, "product_views": 0.45, "add_to_carts": 0.055, "checkouts": 0.0275,
               "payment_attempts": 0.025, "ad_requests": 6.2}
PEAK_SESSIONS = sum(COUNTRIES.values()) * hourly_shape().max() * WEEKLY.max()
CAPACITY_HEADROOM = 1.5                   # capacity = 1.5 x the busiest normal hour
CARD_SHARE = {c: (0.35 if LOCAL_METHOD_SHARE[c] > 0 else 0.60) for c in COUNTRIES}
# payment providers per method (fictional names): card processor, wallet, the DE local bank-transfer method
PROVIDERS = {"card": "PayFlow", "wallet": "WalletPay", "local": "GiroLokal"}
BASE_FAIL = {"card": 0.055, "wallet": 0.040, "local": 0.070}
FULFIL = {"electronics": 4.20, "fashion": 3.10, "home": 5.40, "grocery": 2.60}     # EUR fulfilment cost per order
PAY_PCT, PAY_FIXED = 0.012, 0.08                                                     # payment cost: 1.2 % + 0.08 per attempt
SUPPLIERS = {"electronics": [("Voltix", True), ("Nordwerk Distribution", False)], "fashion": [("Textil Union", False)],
             "home": [("Casa Supply", False)], "grocery": [("FreshCo Wholesale", False)]}
DELIVERY_HOURS = (6, 7, 8, 9)            # morning trucks: purchase orders are delivered between 06:00 and 10:00

APP_VERSION0 = {"android": "android-app 12.4.1", "ios": "ios-app 5.1.3", "web": "web 2026.11.3"}
CRASH_RATE = {"android": 0.0040, "ios": 0.0022, "web": 0.0050}          # web: sessions with a fatal JS error

# ---------------------------------------------------------------- support
TOPICS = {  # topic: (baseline sentiment, driver coefficient)
    "payment_problem": (-0.45, 0.12), "item_unavailable": (-0.35, 0.002), "cant_find_product": (-0.25, 0.004),
    "website_slow_or_error": (-0.50, 0.00004), "order_status_delivery": (-0.30, 0.012),
    "return_refund": (-0.20, 0.25), "account_login": (-0.40, 0.00003)}
B2B_TOPICS = {"advertiser_billing": -0.40, "seller_listing": -0.45}
SUBJECTS = {
    "payment_problem": {"de": "Zahlung fehlgeschlagen", "fr": "Le paiement a échoué", "nl": "Betaling mislukt",
                        "pl": "Płatność nie powiodła się"},
    "item_unavailable": {"de": "Artikel nicht verfügbar", "fr": "Article indisponible", "nl": "Artikel niet beschikbaar",
                         "pl": "Produkt niedostępny"},
    "cant_find_product": {"de": "Suche findet nichts", "fr": "La recherche ne trouve rien", "nl": "Zoeken vindt niets",
                          "pl": "Wyszukiwarka nic nie znajduje"},
    "website_slow_or_error": {"de": "Seite lädt sehr langsam", "fr": "Le site est très lent",
                              "nl": "Website is erg traag", "pl": "Strona bardzo wolno działa"},
    "order_status_delivery": {"de": "Wo ist meine Bestellung?", "fr": "Où est ma commande ?",
                              "nl": "Waar is mijn bestelling?", "pl": "Gdzie jest moje zamówienie?"},
    "return_refund": {"de": "Rücksendung und Erstattung", "fr": "Retour et remboursement", "nl": "Retour en terugbetaling",
                      "pl": "Zwrot i refundacja"},
    "account_login": {"de": "Kann mich nicht anmelden", "fr": "Impossible de me connecter", "nl": "Kan niet inloggen",
                      "pl": "Nie mogę się zalogować"},
    "advertiser_billing": {"*": "Question about sponsored ads invoice"},
    "seller_listing": {"*": "Question about my listings"},
}
SPECIFIC = {  # (topic, cause) -> subject; used when the ticket was caused by an active fault
    ("payment_problem", "local_broken"): {"de": "Bezahlung per Banküberweisung in der Android-App schlägt immer fehl"},
    ("payment_problem", "psp"): {"nl": "Mijn creditcard wordt steeds geweigerd bij het afrekenen",
                                 "de": "Kreditkarte wird beim Bezahlen abgelehnt", "fr": "Ma carte est refusée au paiement",
                                 "pl": "Karta odrzucona przy płatności"},
    ("cant_find_product", "ranker"): {"de": "Suchergebnisse passen überhaupt nicht zu meiner Suche"},
    ("website_slow_or_error", "checkout"): {"de": "Kasse hängt, Bestellung geht nicht durch",
                                            "fr": "La page de paiement tourne sans fin",
                                            "nl": "Afrekenen blijft laden", "pl": "Kasa się zawiesza"},
    ("advertiser_billing", "pacing"): {"*": "Our daily ads budget was used up before noon"},
    ("advertiser_billing", "ivt"): {"*": "Many clicks from Poland but no sales - invalid traffic?"},
    ("seller_listing", "feed"): {"*": "Our offers disappeared from the marketplace, feed upload fails"},
    ("item_unavailable", "stock"): {"de": "Elektronikartikel seit Tagen ausverkauft"},
}

SOURCE_LAG = {"web_analytics": (4, 2), "orders_db": (6, 3), "ads_platform": (15, 5), "inventory": (10, 4),
              "seller_feeds": (20, 8), "support_desk": (5, 2)}


class Ops:
    def __init__(self, seed: int, catalog, jit: dict, start):
        self.seed = seed
        self.start = pd.Timestamp(start)
        rng = np.random.default_rng([seed, 9100])
        self.pop = catalog.pop
        # SKU ownership: just-in-time electronics are bought wholesale by the 1P retail business
        self.owner: dict[str, np.ndarray] = {}
        self.names = {cat: [n for n, _ in sel] for cat, sel in SELLERS.items()}
        for cat, sel in SELLERS.items():
            p = np.array([w for _, w in sel])
            own = rng.choice(len(sel), LISTED[cat], p=p / p.sum())
            own[jit[cat]] = 0
            self.owner[cat] = own
        self.sku_seller = {cat: np.array(self.names[cat], dtype=object)[self.owner[cat]] for cat in LISTED}
        self.cap = {(svc, p): PEAK_SESSIONS * PLATFORMS[p] * PER_SESSION[d] * k * CAPACITY_HEADROOM
                    for svc, (_, _, k, d) in SERVICES.items() for p in PLATFORMS}
        self.orders_hist: deque = deque(maxlen=168)        # for chargebacks (arrive about a week later)
        self.bot_today: dict = {}
        self.exhausted_at: dict = {}
        self.prev_drivers = None
        self.ticket_ewma: dict = {}
        self.last_inv = None
        self.frozen_since = {}
        self.checkout_state = None
        self.rows = {k: [] for k in ("ads_auctions", "sellers", "service_health", "app_health", "support_tickets",
                                     "support_ticket_samples", "pipeline_health", "payments", "wholesale")}
        self.calendar: list[dict] = []
        self.status: list[dict] = []

    # ------------------------------------------------------------------ helpers used by world.step
    def begin(self, ts):
        self.rng = np.random.default_rng([self.seed, 9101, int((ts - self.start) / H)])
        self.checkout_state = None

    def owned_by(self, cat: str, sellers) -> np.ndarray:
        return np.isin(self.sku_seller[cat], list(sellers))

    def offline_sellers(self, s, ts):
        return {n for n, (frm, until) in s.seller_offline.items() if pd.Timestamp(frm) <= ts < pd.Timestamp(until)}

    def traffic_mult(self, s, ts, seg) -> np.ndarray:
        m = np.ones(len(seg))
        for t in s.traffic_mods:
            if pd.Timestamp(t["start"]) <= ts < pd.Timestamp(t["end"]):
                k = t["mult"][ts.hour] if isinstance(t["mult"], (list, tuple)) else t["mult"]
                m[(seg.country == t["country"]).to_numpy()] *= k
        return m

    def feed_decay(self, s, ts, seg) -> np.ndarray:
        m = np.ones(len(seg))
        for cat, since in s.recs_stale.items():
            if ts >= pd.Timestamp(since):
                days = (ts - pd.Timestamp(since)) / pd.Timedelta(days=1)
                m[(seg.category == cat).to_numpy()] = max(0.55, 1 - 0.045 * days)
        return m

    def checkout_load(self, s, seg, checkouts) -> np.ndarray:
        """Checkout capacity: latency grows with utilisation; slow checkouts lose payment attempts."""
        fac, st = np.ones(len(seg)), {}
        for p in PLATFORMS:
            m = (seg.platform == p).to_numpy()
            util = checkouts[m].sum() * SERVICES["checkout"][2] / (self.cap[("checkout", p)] * s.checkout_pool)
            p99 = SERVICES["checkout"][0] * (1 + 4.0 * max(0.0, util - 0.75))
            fac[m] = 1 / (1 + 0.12 * max(0.0, p99 / 1000 - 1.2))
            st[p] = dict(util=util, p99=p99, err=0.003 + 0.05 * max(0.0, util - 1.0), factor=float(fac[m][0]))
        self.checkout_state = st
        return fac

    def bots(self, s, ts, seg):
        active = [b for b in s.bots if pd.Timestamp(b["start"]) <= ts < pd.Timestamp(b["until"])]
        if not active:
            return None
        n = np.zeros(len(seg), int)
        for b in active:
            m = ((seg.country == b["country"]) & (seg.platform == b["platform"])
                 & seg.category.isin(b["categories"])).to_numpy()
            n[m] += self.rng.poisson(b["sessions_per_hour"] / m.sum(), m.sum())
        views = n + self.rng.poisson(n * 1.5)                        # bots open a few product pages, never buy
        return dict(sessions=n, views=views)

    def freeze_inventory(self, s, ts, inv_rows):
        frozen = s.frozen.get("inventory")
        if frozen and pd.Timestamp(frozen[0]) <= ts < pd.Timestamp(frozen[1]) and self.last_inv is not None:
            self.frozen_since.setdefault("inventory", self.last_ts)
            return [dict(r, ts=ts) for r in self.last_inv]           # same snapshot again: values stop moving
        self.frozen_since.pop("inventory", None)
        self.last_inv, self.last_ts = inv_rows, ts
        return inv_rows

    # ------------------------------------------------------------------ the hour
    def hour(self, mp, ts, f: pd.DataFrame, avail, oos, cps, stock, bots):
        s, rng = mp.state, self.rng
        cat_a = f.category.to_numpy()
        human_sessions = f.sessions.to_numpy().copy()
        human_sponsored = f.sponsored_sessions.to_numpy().copy()
        marketing = np.maximum(f.ad_cost.to_numpy() - human_sponsored * cps, 0).round(2)
        bot_n = bots["sessions"] if bots else np.zeros(len(f), int)
        if bots:
            f["sessions"] = f.sessions + bot_n
            f["sponsored_sessions"] = f.sponsored_sessions + bot_n
            f["product_views"] = f.product_views + bots["views"]
            f["ad_cost"] = (f.ad_cost + bot_n * cps).round(2)        # bots' clicks are billed like any click

        # ---------------- sponsored products auction (per segment, aggregated per category below)
        req = SEARCH_SLOTS * f.searches.to_numpy() + PAGE_SLOTS * f.product_views.to_numpy()
        impr = np.minimum(0.95 * req, human_sponsored / (AD_CTR * rng.lognormal(0, 0.05, len(f))) + 1.1 * bot_n)
        impr = np.round(impr).astype(int)
        no_elig = np.minimum(rng.binomial(req, NO_ELIGIBLE), req - impr)
        tout = np.minimum(rng.binomial(req, TIMEOUTS), req - impr - no_elig)
        day = ts.normalize()
        exhausted = set()
        for cat in s.pacer_ignores_schedule:
            budget = sum(r["daily_budget"] for r in mp.ads[-12:] if r["category"] == cat)
            spent = sum(r["spent_today"] for r in mp.ads[-12:] if r["category"] == cat)
            if budget and spent >= 0.995 * budget:
                exhausted.add(cat)
                self.exhausted_at.setdefault((day, cat), ts)
        rest = req - impr - no_elig - tout
        no_budget = np.where(np.isin(cat_a, list(exhausted)), rest, 0)
        below_floor = rest - no_budget
        ad_revenue = (f.sponsored_sessions.to_numpy() * cps).round(2)
        g = pd.DataFrame(dict(category=cat_a, ad_requests=req, impressions=impr, no_fill_no_eligible=no_elig,
                              no_fill_budget_exhausted=no_budget, no_fill_below_floor=below_floor, timeouts=tout,
                              clicks=f.sponsored_sessions.to_numpy(), invalid_clicks=bot_n, ad_revenue=ad_revenue,
                              page_views=f.searches.to_numpy() + f.product_views.to_numpy(),
                              orders=f.orders.to_numpy(), gmv=f.gmv.to_numpy(), human_sessions=human_sessions,
                              human_sponsored=human_sponsored)).groupby("category").sum()
        for cat, r in g.iterrows():
            self.rows["ads_auctions"].append(dict(
                ts=ts, category=cat, ad_requests=int(r.ad_requests), impressions=int(r.impressions),
                fill_rate=round(r.impressions / r.ad_requests, 4) if r.ad_requests else 0.0,
                no_fill_no_eligible=int(r.no_fill_no_eligible), no_fill_budget_exhausted=int(r.no_fill_budget_exhausted),
                no_fill_below_floor=int(r.no_fill_below_floor), timeouts=int(r.timeouts), clicks=int(r.clicks),
                ctr=round(r.clicks / r.impressions, 4) if r.impressions else 0.0,
                avg_cpc=round(r.ad_revenue / r.clicks, 3) if r.clicks else 0.0, floor_cpc=FLOOR_CPC,
                ad_revenue=round(r.ad_revenue, 2), rpm=round(r.ad_revenue / r.page_views * 1000, 3) if r.page_views else 0.0))

        # ---------------- advertisers: delivery, pacing vs plan, ROAS, invalid clicks (rows written by world.step)
        cum_share = np.cumsum(mp.shape) / 24
        for row in mp.ads[-12:]:
            cat = row["category"]
            sh = row["daily_budget"] / max(sum(r["daily_budget"] for r in mp.ads[-12:] if r["category"] == cat), 1e-9)
            r = g.loc[cat]
            bot_spend = float((bot_n * cps)[cat_a == cat].sum()) * sh
            key = (day, row["advertiser"])
            self.bot_today[key] = self.bot_today.get(key, 0.0) + bot_spend
            if bot_spend:
                row["spend"] = round(row["spend"] + bot_spend, 2)
                row["spent_today"] = round(row["spent_today"] + self.bot_today[key], 2)
                row["budget_used"] = round(row["spent_today"] / row["daily_budget"], 3) if row["daily_budget"] else 0.0
            conv = r.human_sponsored / r.human_sessions if r.human_sessions else 0.0
            att_orders = r.orders * conv * 1.3 * sh
            att_sales = r.gmv * conv * 1.3 * sh
            expected = row["daily_budget"] * cum_share[ts.hour]
            row.update(budget_per_hour=round(row["daily_budget"] / 24, 2),
                       impressions=int(r.impressions * sh), clicks=int(round(r.clicks * sh)),
                       invalid_clicks=int(round(r.invalid_clicks * sh)), attributed_orders=round(att_orders, 1),
                       attributed_sales=round(att_sales, 2),
                       roas=round(att_sales / row["spend"], 2) if row["spend"] else None,
                       expected_spent_today=round(expected, 2),
                       pacing_index=round(row["spent_today"] / expected, 3) if expected else None,
                       status="budget_exhausted" if cat in exhausted else "active",
                       exhausted_at=str(self.exhausted_at[(day, cat)])[:16] if (day, cat) in self.exhausted_at else "")

        # ---------------- sellers, 1P / 3P split, take rate
        off = self.offline_sellers(s, ts)
        share_3p = {}
        orders_cat = f.groupby("category").orders.sum()
        gmv_cat = f.groupby("category").gmv.sum()
        for cat in CATEGORIES:
            w = np.bincount(self.owner[cat], weights=self.pop[cat] * stock[cat], minlength=len(SELLERS[cat]))
            share_3p[cat] = float(w[1:].sum() / w.sum()) if w.sum() else 0.0
            so = rng.multinomial(int(orders_cat[cat]), w / w.sum()) if w.sum() else np.zeros(len(w), int)
            listed = np.bincount(self.owner[cat], minlength=len(w))
            active = np.bincount(self.owner[cat], weights=stock[cat], minlength=len(w)).astype(int)
            for k, name in enumerate(self.names[cat]):
                gmv_k = float(gmv_cat[cat]) * so[k] / orders_cat[cat] if orders_cat[cat] else 0.0
                failing = name in off
                since = pd.Timestamp(s.seller_offline[name][0]) if failing else None
                self.rows["sellers"].append(dict(
                    ts=ts, seller=name, category=cat, type="1P" if k == 0 else "3P", listed_skus=int(listed[k]),
                    active_offers=int(active[k]), orders=int(so[k]), gmv=round(gmv_k, 2),
                    commission=round(gmv_k * TAKE_RATE[cat], 2) if k else 0.0,
                    feed_status="failing" if failing else "ok",
                    last_feed_sync=str(since - pd.Timedelta(minutes=20) if failing
                                       else ts + pd.Timedelta(minutes=int(rng.integers(30, 60))))[:16],
                    feed_errors=int(rng.poisson(12)) if failing else int(rng.poisson(0.05))))
        gmv_3p = (f.gmv.to_numpy() * np.array([share_3p[c] for c in cat_a])).round(2)
        fees = (gmv_3p * np.array([TAKE_RATE[c] for c in cat_a])).round(2)

        # ---------------- chargebacks (about a week after the order) and invalid traffic detection
        past = self.orders_hist[0] if len(self.orders_hist) == 168 else f.orders.to_numpy()
        chargebacks = rng.poisson(0.003 * past)
        self.orders_hist.append(f.orders.to_numpy().copy())
        ivt = rng.binomial(bot_n, 0.55) + rng.binomial(human_sessions, 0.002)
        f["ad_impressions"], f["ad_revenue"], f["marketing_cost"] = impr, ad_revenue, marketing
        f["gmv_3p"], f["marketplace_fees"], f["chargebacks"], f["ivt_sessions"] = gmv_3p, fees, chargebacks, ivt
        # ---------------- unit economics (deterministic, no random draws): 1P gross profit, fulfilment, payment cost
        s3 = np.array([share_3p[c] for c in cat_a])
        f["retail_gross_profit"] = ((f.gmv.to_numpy() - f.cogs.to_numpy()) * (1 - s3)).round(2)
        f["fulfilment_cost"] = (f.orders.to_numpy() * np.array([FULFIL[c] for c in cat_a])).round(2)
        f["payment_cost"] = (PAY_PCT * f.gmv.to_numpy() + PAY_FIXED * f.payment_attempts.to_numpy()).round(2)

        # ---------------- For You feed: model calibration and feature freshness
        sess_cat = f.groupby("category").sessions.sum()
        feed_cat = f.groupby("category").feed_clicks.sum()
        for row in mp.feed[-4:]:
            cat = row["category"]
            imp = sess_cat[cat] * 0.55 * 12
            stale = s.recs_stale.get(cat)
            fresh = ((ts - pd.Timestamp(stale)) / H + 1.0) if stale and ts >= pd.Timestamp(stale) else float(rng.uniform(0.3, 1.5))
            predicted = 0.12 / (0.55 * 12) * (1 + row["out_of_stock_exposure"])
            row.update(oos_impressions=int(round(imp * row["out_of_stock_exposure"])),
                       predicted_clicks=int(round(imp * predicted)))
            row.update(impressions=int(imp), predicted_ctr=round(predicted, 4),
                       actual_ctr=round(feed_cat[cat] / imp, 4) if imp else 0.0, feature_freshness_h=round(fresh, 1))

        # ---------------- service health
        per_p = f.assign(ad_requests=req).groupby("platform").sum(numeric_only=True)
        hum_p = pd.Series(human_sessions, index=f.index).groupby(f.platform).sum()
        maint = any(pd.Timestamp(a) <= ts < pd.Timestamp(b) for a, b, *_ in s.search_maint)
        for svc, (p99_0, err0, k, drv) in SERVICES.items():
            for p in PLATFORMS:
                n_req = int(per_p.loc[p, drv] * k)
                util = n_req / self.cap[(svc, p)]
                err, p99 = err0, p99_0 * (1 + 4.0 * max(0.0, util - 0.75))
                if svc == "checkout" and self.checkout_state:
                    err, p99 = self.checkout_state[p]["err"], self.checkout_state[p]["p99"]
                    util = self.checkout_state[p]["util"]
                if svc == "payments":
                    tech = 0.0
                    att = f[f.platform == p].groupby("country").payment_attempts.sum()
                    broken = s.pay_broken.get(s.pay_sdk[p])
                    for c, a in att.items():
                        if broken and broken[0] == c:
                            tech += a * LOCAL_METHOD_SHARE[c] * 0.95
                        if c in s.psp_degraded and pd.Timestamp(s.psp_degraded[c][1]) > ts:
                            tech += a * CARD_SHARE[c] * s.psp_degraded[c][0]
                            p99 = max(p99, 9000.0)
                    err = err0 + tech / max(att.sum(), 1)
                if svc == "search" and maint:
                    err, p99 = err0 * 3, p99 * 2.2
                errors = int(rng.binomial(max(n_req, 0), min(err, 1.0)))
                self.rows["service_health"].append(dict(
                    ts=ts, service=svc, platform=p, requests=n_req, errors=errors,
                    error_rate=round(errors / n_req, 5) if n_req else 0.0,
                    p99_ms=int(p99 * rng.lognormal(0, 0.06)), capacity_util=round(util, 3)))
        feeds = sum(len(v) for v in SELLERS.values())
        failing = len(off)
        ingest = feeds * 60
        ing_err = int(rng.binomial(ingest, min(1.0, 0.001 + failing / feeds)))
        self.rows["service_health"].append(dict(ts=ts, service="catalog-ingest", platform="backend", requests=ingest,
                                                errors=ing_err, error_rate=round(ing_err / ingest, 5),
                                                p99_ms=int(800 * rng.lognormal(0, 0.06)), capacity_util=0.2))

        # ---------------- app health
        versions = self._versions(mp, ts)
        for p in PLATFORMS:
            n = int(hum_p[p])
            crashes = int(rng.binomial(n, CRASH_RATE[p]))
            self.rows["app_health"].append(dict(ts=ts, platform=p, app_version=versions[p], payment_sdk=s.pay_sdk[p],
                                                sessions=n, crashes=crashes,
                                                crash_free_rate=round(1 - crashes / n, 5) if n else 1.0))

        # ---------------- support tickets (customers write about what went wrong in the previous hour)
        self._support(mp, ts, f, human_sessions, avail, oos, bot_n, exhausted, off)

        # ---------------- data pipeline health
        for src, (lag0, sd) in SOURCE_LAG.items():
            comp = s.late_rows.get((src, ts), 1.0)
            lag = max(1.0, rng.normal(lag0, sd)) + (60 + 90 * (1 - comp) if comp < 1 else 0)
            last = ts + H + pd.Timedelta(minutes=-lag)
            since = self.frozen_since.get(src)
            fz = s.frozen.get(src)
            if fz and pd.Timestamp(fz[0]) <= ts < pd.Timestamp(fz[1]) and self.last_inv is not None:
                since = since or self.last_ts                        # this hour already re-sends the old snapshot
            elif since is not None and not (fz and ts < pd.Timestamp(fz[1])):
                since = None                                         # fresh snapshot again this hour
            if since is not None:
                last = since + H
                lag = (ts + H - last) / pd.Timedelta(minutes=1) + lag0
            self.rows["pipeline_health"].append(dict(ts=ts, source=src, completeness=comp, lag_minutes=round(lag, 1),
                                                     last_update=str(last)[:16], heartbeat_ok=True))
        self._payments(mp, ts, f)
        self._wholesale(mp, ts, f)

    def _payments(self, mp, ts, f):
        """Payments ledger per provider (own random stream). Attempts and authorisations add up exactly to the
        funnel's payment_attempts and orders; failures go to providers in proportion to how likely each failed."""
        s = mp.state
        rng = np.random.default_rng([self.seed, 9200, int((ts - self.start) / H)])
        g = f.groupby(["country", "platform"])[["payment_attempts", "orders"]].sum()
        for (c, p), r in g.iterrows():
            att, fails = int(r.payment_attempts), int(r.payment_attempts - r.orders)
            local = LOCAL_METHOD_SHARE[c]
            shares = {"local": local, "card": CARD_SHARE[c], "wallet": 1 - local - CARD_SHARE[c]}
            methods = [m for m, v in shares.items() if v > 0]
            a = rng.multinomial(att, [shares[m] for m in methods]) if att else np.zeros(len(methods), int)
            p_fail = {m: BASE_FAIL[m] for m in methods}
            broken = s.pay_broken.get(s.pay_sdk.get(p))
            if broken and broken[0] == c and "local" in p_fail:
                p_fail["local"] = 0.95
            deg = s.psp_degraded.get(c)
            if deg and ts < pd.Timestamp(deg[1]):
                p_fail["card"] = min(1.0, p_fail["card"] + deg[0])
            w = np.array([a[i] * p_fail[m] for i, m in enumerate(methods)], float)
            fl = rng.multinomial(max(fails, 0), w / w.sum()) if fails > 0 and w.sum() > 0 else np.zeros(len(methods), int)
            fl = np.minimum(fl, a)
            left = max(fails, 0) - int(fl.sum())
            while left > 0 and (a - fl).sum() > 0:                   # put what did not fit where there is room
                k = int(np.argmax(a - fl))
                take = min(left, int(a[k] - fl[k]))
                fl[k] += take
                left -= take
            for i, m in enumerate(methods):
                self.rows["payments"].append(dict(ts=ts, country=c, platform=p, provider=PROVIDERS[m], method=m,
                                                  auth_attempts=int(a[i]), auth_failures=int(fl[i])))

    def _wholesale(self, mp, ts, f):
        """Wholesale deliveries per supplier: purchase orders placed at 05:00 for the next delivery, trucks between
        06:00 and 10:00. A delayed supplier delivers nothing (the JIT electronics stock then runs out)."""
        s = mp.state
        rng = np.random.default_rng([self.seed, 9300, int((ts - self.start) / H)])
        delayed = {cat for cat, (frm, until) in s.delivery_delay.items()
                   if pd.Timestamp(frm) <= ts < pd.Timestamp(until)}
        for cat, sups in SUPPLIERS.items():
            jit_share = float(self.pop[cat][mp.jit[cat]].sum())
            n_jit = int(mp.jit[cat].sum())
            for name, is_jit in sups:
                skus = n_jit if is_jit else LISTED[cat] - n_jit
                if skus <= 0:
                    continue
                daily = skus * 0.9                                     # units per day: about one per SKU
                forecast = int(rng.poisson(daily / 24))
                po = int(rng.poisson(daily)) if ts.hour == 5 else 0
                due = int(rng.poisson(daily / len(DELIVERY_HOURS))) if ts.hour in DELIVERY_HOURS else 0
                late = is_jit and cat in delayed
                recv = 0 if late else (int(rng.binomial(due, 0.985)) if due else 0)
                self.rows["wholesale"].append(dict(ts=ts, category=cat, supplier=name, forecast_units=forecast,
                                                   po_units=po, due_units=due, received_units=recv,
                                                   demand_share=round(jit_share if is_jit else 1 - jit_share, 4),
                                                   delivery_status="delayed" if late and due else
                                                   ("delivered" if due else "")))

    def _versions(self, mp, ts):
        v = dict(APP_VERSION0)
        for e in mp.events:
            m = re.match(r"^(ios-app|android-app) (\S+)", e["name"])
            if m and pd.Timestamp(e["ts"]) <= ts and e["type"] == "release":
                v["ios" if m.group(1) == "ios-app" else "android"] = f"{m.group(1)} {m.group(2)}"
        return v

    def _support(self, mp, ts, f, human_sessions, avail, oos, bot_n, exhausted, off):
        s, rng = mp.state, self.rng
        fh = f.assign(sessions=human_sessions)
        oos_views = ((f.product_views - bot_n - f.feed_clicks).clip(lower=0) * (1 - f.category.map(avail))
                     + f.feed_clicks * oos)
        lost_checkout = np.zeros(len(f))
        if self.checkout_state:
            lost_checkout = f.checkouts.to_numpy() * 0.92 * (1 - f.platform.map(
                {p: v["factor"] for p, v in self.checkout_state.items()}).to_numpy())
        d = pd.DataFrame(dict(country=f.country,
                              payment_problem=f.payment_attempts - f.orders, item_unavailable=oos_views,
                              cant_find_product=f.zero_result_searches,
                              website_slow_or_error=fh.sessions + 1000 * lost_checkout,
                              order_status_delivery=f.orders, return_refund=f.refunds,
                              account_login=fh.sessions)).groupby("country").sum()
        causes = {}
        for c in COUNTRIES:
            cs = set()
            for p in PLATFORMS:
                b = s.pay_broken.get(s.pay_sdk[p])
                if b and b[0] == c:
                    cs.add("local_broken")
            if c in s.psp_degraded and pd.Timestamp(s.psp_degraded[c][1]) > ts:
                cs.add("psp")
            if s.ranker_locale_bug.get(s.ranker[c]) == c:
                cs.add("ranker")
            if self.checkout_state and any(v["factor"] < 0.9 for v in self.checkout_state.values()):
                cs.add("checkout")
            if any(pd.Timestamp(frm) <= ts for frm, _u in s.delivery_delay.values()):
                cs.add("stock")
            causes[c] = cs
        b2b = dict(advertiser_billing=0.15 + 1.5 * len(exhausted) + bot_n.sum() / 400,
                   seller_listing=0.2 + 3.0 * len(off))
        b2b_causes = {"advertiser_billing": ({"pacing"} if exhausted else set()) | ({"ivt"} if bot_n.sum() else set()),
                      "seller_listing": {"feed"} if off else set()}
        cur = (d, b2b, causes, b2b_causes)
        d, b2b, causes, b2b_causes = self.prev_drivers or cur
        self.prev_drivers = cur
        total = 0
        rows = []
        for c in COUNTRIES:
            for topic, (sent0, k) in TOPICS.items():
                rows.append((c, topic, sent0, d.loc[c, topic] * k, causes[c]))
        for topic, sent0 in B2B_TOPICS.items():
            rows.append(("*", topic, sent0, b2b[topic], b2b_causes[topic]))
        counts = [int(rng.poisson(max(lam, 0))) for *_x, lam, _c in rows]
        total = sum(counts)
        for (c, topic, sent0, lam, cs), n in zip(rows, counts):
            key = (c, topic)
            base = self.ticket_ewma.get(key, max(lam, 0.1))
            self.ticket_ewma[key] = 0.97 * base + 0.03 * n
            excess = max(0.0, n / max(base, 0.5) - 1)
            sent = float(np.clip(sent0 - 0.12 * np.log1p(excess) + rng.normal(0, 0.05), -1, 1)) if n else None
            self.rows["support_tickets"].append(dict(
                ts=ts, country=c, topic=topic, tickets=n, avg_sentiment=round(sent, 3) if n else None,
                median_first_response_min=round(8 * (1 + total / 80) * rng.lognormal(0, 0.1), 1) if n else None))
            lang = c.lower() if c != "*" else "*"
            for _ in range(int(rng.binomial(n, 0.25))):
                subj = SUBJECTS[topic][lang]
                for cause in cs:
                    spec = SPECIFIC.get((topic, cause), {})
                    if lang in spec and rng.random() < 0.8:
                        subj = spec[lang]
                self.rows["support_ticket_samples"].append(dict(
                    ts=ts + pd.Timedelta(minutes=int(rng.integers(0, 60))), country=c, topic=topic, subject=subj))

    # ------------------------------------------------------------------ outputs
    def tables(self) -> dict[str, pd.DataFrame]:
        out = {k: pd.DataFrame(v) for k, v in self.rows.items()}
        # wide view of the services the metric catalog reads (requests / errors per platform)
        sh = out["service_health"]
        sh = sh[sh.service.isin(["checkout", "payments", "search"]) & (sh.platform != "backend")]
        wide = sh.pivot_table(index=["ts", "platform"], columns="service", values=["requests", "errors"], aggfunc="sum")
        wide.columns = [f"{svc}_{v}" for v, svc in wide.columns]
        out["services"] = wide.reset_index()
        out["support_ticket_samples"] = out["support_ticket_samples"].sort_values("ts", kind="stable")
        out["calendar"] = pd.DataFrame(self.calendar, columns=["name", "kind", "country", "start", "end",
                                                               "expected_impact", "metrics", "expected_low",
                                                               "expected_high", "announced_at"])
        out["status_feed"] = pd.DataFrame(self.status, columns=["ts", "source", "channel", "component", "region",
                                                                "severity", "state", "title"]).sort_values("ts", kind="stable")
        return out
