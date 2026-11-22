"""User-level layer: a panel of simulated customers whose sessions, clicks and orders add up to facts.csv.

Each hour, after the marketplace has simulated a segment (country x platform x category), a sample of that
segment's sessions (default 1 %) is played out one customer at a time: landing page, searches, For You feed,
product pages, favourites, cart, checkout, payment, order, and the page where the customer left.

How it stays consistent with the aggregates: every step uses the segment's *realised* rates for that hour
(search share, product views per session, cart rate per available product, checkout / payment / order ratios).
So the panel scaled by 1/sample matches facts.csv in expectation, and every injected fault shows up in the user
data as well. The details come from the component state: which SKUs are out of stock, what the For You feed
exposes, which payment method fails on which SDK, which ranker / model version served the request.

The panel has its own random streams: switching it on does not change a single number in the other tables.

Output (written next to the data contract by run.py):
  users.csv        one row per customer in the panel (profile, persona, how they were acquired)
  sessions.csv     one row per session: source, landing / exit page, funnel flags, duration, cart, payment, order
  clickstream.csv  one row per page view or action, with dwell time, SKU, query, feed position, versions
  orders.csv       orders as the orders database sees them (late ingestion drops rows, like facts.csv)
  order_items.csv  products per order
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from .catalog import AOV, CATEGORIES, COUNTRIES, LISTED, LOCAL_METHOD_SHARE, PAY_OK, PLATFORMS

H = pd.Timedelta(hours=1)
EPOCH = pd.Timestamp("1970-01-01")

PERSONAS = ["loyal_buyer", "deal_hunter", "casual", "browser"]
PERSONA_P = [0.15, 0.25, 0.40, 0.20]
PERSONA_ACTIVITY = {"loyal_buyer": 1.6, "deal_hunter": 1.2, "casual": 0.8, "browser": 1.3}
PERSONA_PROPENSITY = {"loyal_buyer": 1.8, "deal_hunter": 1.1, "casual": 0.9, "browser": 0.35}
AGE_BANDS = ["18-24", "25-34", "35-44", "45-54", "55-64", "65+"]
AGE_P = [0.14, 0.27, 0.23, 0.17, 0.12, 0.07]

# organic traffic sources (sponsored ads and campaigns come from the marketplace itself)
ORGANIC = {"app": (["app_open", "search_engine", "email_newsletter", "referral"], [0.70, 0.08, 0.12, 0.10]),
           "web": (["direct", "search_engine", "email_newsletter", "referral"], [0.35, 0.40, 0.10, 0.15])}
NEW_USER_P = {"sponsored_ad": 0.08, "paid_social": 0.30, "email_push": 0.02, "search_engine": 0.10,
              "referral": 0.06, "direct": 0.03, "app_open": 0.01, "email_newsletter": 0.0}

# median seconds on a page (lognormal around it); mobile sessions are a bit quicker
DWELL = {"home": 14, "for_you": 28, "search_results": 11, "product": 38, "cart": 22, "checkout": 50, "payment": 35,
         "order_confirmation": 12}
FEED_VISIT_P = 0.55                 # share of sessions that open the For You feed
SLOW_CHECKOUT_MS = 2500             # checkout p99 above this: customers see a hanging checkout
PAY_METHODS = {"DE": (["local_bank_transfer", "card", "wallet"], [LOCAL_METHOD_SHARE["DE"], 0.35, 0.20])}
PAY_DEFAULT = (["card", "wallet"], [0.60, 0.40])
CARD_ERRORS = (["card_declined", "3ds_timeout", "insufficient_funds", "network_error"], [0.40, 0.25, 0.20, 0.15])

QUERIES = {
    "electronics": {
        "de": ["kopfhörer", "ladekabel usb c", "fernseher 55 zoll", "bluetooth lautsprecher", "powerbank", "laptop",
               "smartwatch", "gaming maus"],
        "fr": ["écouteurs", "chargeur usb c", "télévision 55 pouces", "enceinte bluetooth", "batterie externe",
               "ordinateur portable", "montre connectée", "souris gamer"],
        "nl": ["koptelefoon", "oplader usb c", "televisie 55 inch", "bluetooth speaker", "powerbank", "laptop",
               "smartwatch", "gaming muis"],
        "pl": ["słuchawki", "ładowarka usb c", "telewizor 55 cali", "głośnik bluetooth", "powerbank", "laptop",
               "smartwatch", "myszka gamingowa"]},
    "fashion": {
        "de": ["winterjacke", "sneaker herren", "jeans damen", "pullover", "laufschuhe", "handschuhe", "kleid", "mütze"],
        "fr": ["doudoune", "baskets homme", "jean femme", "pull", "chaussures de course", "gants", "robe", "bonnet"],
        "nl": ["winterjas", "sneakers heren", "jeans dames", "trui", "hardloopschoenen", "handschoenen", "jurk", "muts"],
        "pl": ["kurtka zimowa", "buty sportowe męskie", "jeansy damskie", "sweter", "buty do biegania", "rękawiczki",
               "sukienka", "czapka"]},
    "home": {
        "de": ["bettwäsche", "kaffeemaschine", "lichterkette", "staubsauger", "handtücher", "pfanne", "kissen",
               "adventskalender"],
        "fr": ["linge de lit", "machine à café", "guirlande lumineuse", "aspirateur", "serviettes", "poêle", "coussin",
               "calendrier de l'avent"],
        "nl": ["dekbedovertrek", "koffiezetapparaat", "lichtsnoer", "stofzuiger", "handdoeken", "koekenpan", "kussen",
               "adventskalender"],
        "pl": ["pościel", "ekspres do kawy", "lampki choinkowe", "odkurzacz", "ręczniki", "patelnia", "poduszka",
               "kalendarz adwentowy"]},
    "grocery": {
        "de": ["kaffeebohnen", "haferflocken", "olivenöl", "schokolade", "tee", "nudeln", "mineralwasser", "nüsse"],
        "fr": ["café en grains", "flocons d'avoine", "huile d'olive", "chocolat", "thé", "pâtes", "eau minérale", "noix"],
        "nl": ["koffiebonen", "havermout", "olijfolie", "chocolade", "thee", "pasta", "mineraalwater", "noten"],
        "pl": ["kawa ziarnista", "płatki owsiane", "oliwa z oliwek", "czekolada", "herbata", "makaron",
               "woda mineralna", "orzechy"]},
}
QUERY_P = (1.0 / np.arange(1, 9)) / (1.0 / np.arange(1, 9)).sum()

CLICK_COLS = ["session_id", "user_id", "ts", "seq", "event", "page", "sku", "seller", "category", "via", "query", "results",
              "position", "in_stock", "dwell_s", "version", "payment_method", "error_code", "is_exit"]
SESSION_COLS = ["session_id", "user_id", "is_new_user", "start_ts", "end_ts", "duration_s", "country", "platform",
                "category", "traffic_source", "campaign", "landing_page", "exit_page", "page_views", "bounced",
                "searches", "zero_result_searches", "product_views", "oos_product_views", "feed_impressions",
                "feed_clicks", "wishlist_adds", "cart_adds", "cart_removes", "cart_value", "checkout_started",
                "payment_attempted", "payment_method", "payment_result", "ordered", "order_id", "cart_abandoned",
                "ivt_flag"]
ORDER_COLS = ["order_id", "session_id", "user_id", "ts", "country", "platform", "category", "payment_method",
              "n_items", "gmv", "discount", "cogs", "refunded"]
ITEM_COLS = ["order_id", "sku", "category", "list_price", "line_amount"]


def sku_name(cat: str, i: int) -> str:
    return f"{cat[:3].upper()}-{i:05d}"          # same ids as products.csv


def _pick(rng, items, p):
    return items[int(np.searchsorted(np.cumsum(p), rng.random() * sum(p)))]


class UserPanel:
    def __init__(self, seed: int, sample: float = 0.01, days: int = 21, start="2026-11-01 00:00"):
        self.seed, self.sample = seed, sample
        rng = np.random.default_rng([seed, 4241])
        self.price = {cat: np.round(AOV[cat] * rng.lognormal(-0.125, 0.5, n), 2) for cat, n in LISTED.items()}
        self.cum_pop = None
        # ---- customer pool per country (existing customers; new ones are created by sessions)
        self.users: list[dict] = []
        self.prop: list[float] = []                 # buying propensity per user (mean 1 over sessions)
        self.pool: dict = {}
        self.new_users = {c: [] for c in COUNTRIES}
        t_lo, t_hi = pd.Timestamp("2018-01-01").value, pd.Timestamp(start).value
        acts, props = [], []
        for c, avg in COUNTRIES.items():
            n = max(50, int(avg * 24 * days * sample / 3.0))      # ~3 sessions per customer over the period
            persona = rng.choice(PERSONAS, n, p=PERSONA_P)
            home = rng.choice(list(PLATFORMS), n, p=list(PLATFORMS.values()))
            pref = rng.choice(list(CATEGORIES), n, p=list(CATEGORIES.values()))
            act = rng.lognormal(0, 1.0, n) * np.array([PERSONA_ACTIVITY[x] for x in persona])
            signup = pd.to_datetime(t_lo + (t_hi - t_lo) * rng.beta(2.0, 1.2, n)).floor("D")
            member = rng.random(n) < np.where(persona == "loyal_buyer", 0.70, 0.25)
            age = rng.choice(AGE_BANDS, n, p=AGE_P)
            idx = np.arange(len(self.users), len(self.users) + n)
            for k in range(n):
                self.users.append(dict(user_id=f"U{idx[k]:07d}", country=c, home_platform=home[k],
                                       signup_date=str(signup[k].date()), age_band=age[k],
                                       membership="plus" if member[k] else "none", persona=persona[k],
                                       preferred_category=pref[k], acquired_via="before_period"))
                props.append(PERSONA_PROPENSITY[persona[k]])
            acts.append(act)
            self.pool[c] = dict(idx=idx, cum={(p, cat): np.cumsum(act * np.where(home == p, 1.0, 0.15)
                                                                  * np.where(pref == cat, 2.5, 1.0))
                                              for p in PLATFORMS for cat in CATEGORIES})
        act_all, props = np.concatenate(acts), np.array(props)
        self.norm = float((act_all * props).sum() / act_all.sum())   # so the average session has propensity 1
        self.prop = list(props / self.norm)
        self.n_sessions = 0
        self.n_orders = 0
        self.clicks: list[tuple] = []
        self.sessions: list[tuple] = []
        self.orders: list[tuple] = []
        self.items: list[tuple] = []

    # ------------------------------------------------------------------ customers
    def _user(self, rng, c, p, cat, source, start_s):
        new_list = self.new_users[c]
        if rng.random() < NEW_USER_P.get(source, 0.03):
            uidx = len(self.users)
            persona = "casual" if rng.random() < 0.7 else "browser"
            self.users.append(dict(user_id=f"U{uidx:07d}", country=c, home_platform=p,
                                   signup_date=str((EPOCH + pd.Timedelta(seconds=start_s)).date()),
                                   age_band=_pick(rng, AGE_BANDS, AGE_P), membership="none", persona=persona,
                                   preferred_category=cat, acquired_via=source))
            self.prop.append(PERSONA_PROPENSITY[persona] / self.norm)
            new_list.append(uidx)
            return uidx, True
        if new_list and rng.random() < 0.06:              # customers acquired earlier in the period come back
            return new_list[int(rng.integers(len(new_list)))], False
        pool = self.pool[c]
        cum = pool["cum"][(p, cat)]
        return int(pool["idx"][int(np.searchsorted(cum, rng.random() * cum[-1]))]), False

    # ------------------------------------------------------------------ one hour
    def step(self, mp, ts, f: pd.DataFrame, stock: dict, oos: np.ndarray, lam: np.ndarray, camp_lam: np.ndarray,
             bots: dict | None = None):
        s = mp.state
        self.sku_seller = mp.ops.sku_seller
        if self.cum_pop is None:
            self.cum_pop = {cat: np.cumsum(mp.catalog.pop[cat]) for cat in LISTED}
        rng = np.random.default_rng([self.seed, 4242, int((ts - mp.start) / H)])
        cum_in = {cat: np.cumsum(mp.catalog.pop[cat] * stock[cat]) for cat in LISTED}
        cum_out = {cat: np.cumsum(mp.catalog.pop[cat] * ~stock[cat]) for cat in LISTED}
        hour = dict(ts=ts, ts_s=(ts - EPOCH).total_seconds(), stock=stock, cum_in=cum_in, cum_out=cum_out,
                    orders_share=s.late_rows.get(("orders_db", ts), 1.0),
                    web_share=s.late_rows.get(("web_analytics", ts), 1.0))
        n_seg = rng.binomial(f.sessions.to_numpy(), self.sample)
        rows = f.to_dict("records")
        for j in np.nonzero(n_seg)[0]:
            cx = self._segment(mp, ts, rows[j], cum_in, oos[j], lam[j], camp_lam[j])
            for _ in range(int(n_seg[j])):
                self._session(rng, hour, rows[j], cx)
        if bots is not None:                                # bot sessions (invalid traffic) in the same sample
            n_bot = rng.binomial(bots["sessions"], self.sample)
            for j in np.nonzero(n_bot)[0]:
                for _ in range(int(n_bot[j])):
                    self._bot_session(rng, hour, rows[j])

    def _segment(self, mp, ts, r, cum_in, oos_j, lam_j, camp_j):
        """Realised rates of this segment-hour -> per-session probabilities."""
        s = mp.state
        c, p, cat = r["country"], r["platform"], r["category"]
        S = max(r["sessions"], 1)
        kind = "web" if p == "web" else "app"
        p_sp = r["sponsored_sessions"] / S
        p_camp = float(camp_j / (lam_j + camp_j)) if camp_j > 0 else 0.0
        camps = [x for x in s.campaigns if x.get("country", "*") in ("*", c)]
        camp_w = np.array([x["uplift"] for x in camps]) if camps else np.array([])
        intent = float((camp_w * np.array([x["intent"] for x in camps])).sum() / camp_w.sum()) if camps else 1.0
        paid_share = float((camp_w * np.array([x.get("cps", 0) > 0 for x in camps])).sum() / camp_w.sum()) if camps else 0
        p_org = max(0.0, 1 - p_sp - p_camp)
        org_names, org_p = ORGANIC[kind]
        se_share = org_p[org_names.index("search_engine")]
        m_nonfeed = (r["product_views"] - r["feed_clicks"]) / S
        landing_views = p_sp * 0.5 + p_camp * paid_share * 0.5 + p_org * se_share * 0.35
        k_land = min(1.0, 0.6 * m_nonfeed / landing_views) if landing_views > 0 else 1.0
        p_search = r["searches"] / S
        z = r["zero_result_searches"] / r["searches"] if r["searches"] else 0.05
        # some sessions land on search results / the feed from outside: they search / see the feed in any case
        forced_search = p_org * se_share * 0.15
        searching = forced_search + (1 - forced_search) * p_search
        forced_feed = 0.5 * (p_org * org_p[org_names.index("email_newsletter")] + p_camp * (1 - paid_share))
        feed_visit = forced_feed + (1 - forced_feed) * FEED_VISIT_P
        all_zero = z * (1 + z) / 2                       # every query of the session came back empty
        search_clicks = max(0.0, m_nonfeed - landing_views * k_land) / max(searching, 1e-9) / max(1 - all_zero, 0.2)
        avail = float(cum_in[cat][-1])
        cartable = (r["product_views"] - r["feed_clicks"]) * avail + r["feed_clicks"] * (1 - oos_j)
        p_cart = min(1.0, r["add_to_carts"] / cartable) if cartable > 0 else 0.0
        methods, mp_ = PAY_METHODS.get(c, PAY_DEFAULT)
        sdk = s.pay_sdk[p]
        broken = s.pay_broken.get(sdk)
        base_ok = [0.05 if (broken and broken[0] == c and m.startswith("local")) else PAY_OK[p] for m in methods]
        psp = s.psp_degraded.get(c)
        psp_fail = psp[0] if psp and ts < pd.Timestamp(psp[1]) else 0.0
        base_ok = [b * (1 - psp_fail) if m == "card" else b for m, b in zip(methods, base_ok)]
        chk = mp.ops.checkout_state
        expected_ok = float(np.dot(mp_, base_ok))
        real_ok = r["orders"] / r["payment_attempts"] if r["payment_attempts"] else expected_ok
        ok = [min(1.0, b * real_ok / expected_ok) for b in base_ok]
        gmv_per_order = r["gmv"] / r["orders"] if r["orders"] else AOV[cat]
        return dict(
            kind=kind, p_sp=p_sp, p_camp=p_camp, camps=camps, camp_w=camp_w, org=(org_names, org_p),
            k_land=k_land, p_search=p_search, z=z, search_clicks=search_clicks,
            feed_clicks=r["feed_clicks"] / S / feed_visit, oos=float(oos_j),
            p_cart=p_cart, mean_w=(1 - p_camp) + p_camp * intent,
            r_co=r["checkouts"] / r["add_to_carts"] if r["add_to_carts"] else 0.5,
            r_pa=r["payment_attempts"] / r["checkouts"] if r["checkouts"] else 0.92,
            methods=methods, method_p=mp_, ok=ok, broken_local=bool(broken and broken[0] == c), psp_fail=psp_fail,
            checkout_p99=chk[p]["p99"] if chk else SLOW_CHECKOUT_MS / 4,
            aov=gmv_per_order, disc=r["discounts"] / r["gmv"] if r["gmv"] else 0.08,
            cogs=r["cogs"] / r["gmv"] if r["gmv"] else 0.65, refund=r["refunds"] / r["orders"] if r["orders"] else 0.04,
            ranker=s.ranker[c], locale_bug=s.ranker_locale_bug.get(s.ranker[c]) == c, recs=s.recs_model, sdk=sdk,
            lang=c.lower())

    # ------------------------------------------------------------------ one customer session
    def _session(self, rng, hour, r, cx):
        c, p, cat = r["country"], r["platform"], r["category"]
        start = hour["ts_s"] + float(rng.uniform(0, 3600))
        # where the visit came from
        u, camp_name, w = rng.random(), "", 1.0
        if u < cx["p_sp"]:
            source = "sponsored_ad"
        elif u < cx["p_sp"] + cx["p_camp"] and cx["camps"]:
            camp = cx["camps"][int(np.searchsorted(np.cumsum(cx["camp_w"]), rng.random() * cx["camp_w"].sum()))]
            source = "paid_social" if camp.get("cps", 0) > 0 else "email_push"
            camp_name, w = camp.get("name", "campaign"), camp["intent"]
        else:
            source = _pick(rng, *cx["org"])
        uidx, is_new = self._user(rng, c, p, cat, source, start)
        uid = self.users[uidx]["user_id"]
        persona = self.users[uidx]["persona"]
        prop = self.prop[uidx] * w / cx["mean_w"]
        engage = rng.lognormal(-0.18, 0.6)
        self.n_sessions += 1
        sid = f"S{self.n_sessions:08d}"
        plat_f = 1.0 if p == "web" else 0.85
        stock = hour["stock"][cat]

        ev: list[list] = []
        t = [start]
        st = dict(page_views=0, searches=0, zero=0, views=0, oos_views=0, impressions=0, feed_clicks=0, wish=0,
                  adds=0, removes=0)

        def page(name, mod=1.0, **kw):
            dw = max(1, int(round(DWELL[name] * rng.lognormal(0, 0.6) * mod * plat_f)))
            ev.append([t[0], "page_view", name, kw.get("sku", ""), kw.get("via", ""), kw.get("query", ""),
                       kw.get("results", ""), kw.get("position", ""), kw.get("in_stock", ""), dw,
                       kw.get("version", ""), "", ""])
            t0 = t[0]
            t[0] += dw
            st["page_views"] += 1
            return t0, dw

        def action(at, event, pg, **kw):
            ev.append([at, event, pg, kw.get("sku", ""), kw.get("via", ""), kw.get("query", ""), kw.get("results", ""),
                       kw.get("position", ""), kw.get("in_stock", ""), "", kw.get("version", ""),
                       kw.get("payment_method", ""), kw.get("error_code", "")])

        cart: list[int] = []

        def product(i, via, **kw):
            ins = bool(stock[i])
            t0, dw = page("product", 1.0 if ins else 0.3, sku=sku_name(cat, i), via=via, in_stock=int(ins), **kw)
            st["views"] += 1
            st["oos_views"] += 0 if ins else 1
            pw = (0.03 if ins else 0.08) * (1.5 if persona == "deal_hunter" else 1.0) * (1.4 if cat == "fashion" else 1.0)
            if rng.random() < pw:
                action(t0 + dw * 0.5, "wishlist_add", "product", sku=sku_name(cat, i), in_stock=int(ins))
                st["wish"] += 1
            if ins and rng.random() < min(0.95, cx["p_cart"] * prop):
                action(t0 + dw * 0.8, "add_to_cart", "product", sku=sku_name(cat, i), in_stock=1)
                st["adds"] += 1
                cart.append(i)

        def pick(cum):
            return int(np.searchsorted(cum, rng.random() * cum[-1]))

        # landing page
        land = "home"
        if source in ("sponsored_ad", "paid_social") and rng.random() < 0.5 * cx["k_land"]:
            land = "product"
        elif source == "search_engine":
            v = rng.random()
            land = "product" if v < 0.35 * cx["k_land"] else ("search_results" if v < 0.35 * cx["k_land"] + 0.15 else "home")
        elif source in ("email_push", "email_newsletter") and rng.random() < 0.5:
            land = "for_you"
        if land == "product":
            product(pick(self.cum_pop[cat]), "external_" + ("ad" if source != "search_engine" else "search"))
        elif land == "home":
            page("home")

        # browsing blocks: searches and the For You feed, in random order
        blocks, nq = [], 0
        if land == "search_results" or rng.random() < cx["p_search"]:
            nq = 1 + int(rng.poisson(0.4))
            blocks.append("search")
        if land == "for_you" or rng.random() < FEED_VISIT_P:
            blocks.append("feed")
        if len(blocks) == 2 and land != "search_results" and (land == "for_you" or rng.random() < 0.5):
            blocks.reverse()

        for b in blocks:
            if b == "search":
                n_click = int(rng.poisson(cx["search_clicks"] * engage))
                qs, q = [], 0
                while q < nq:
                    query = QUERIES[cat][cx["lang"]][int(np.searchsorted(np.cumsum(QUERY_P), rng.random()))]
                    zero = rng.random() < cx["z"]
                    qs.append((query, zero))
                    if zero and rng.random() < 0.5:
                        nq += 1                              # reformulate after an empty result page
                    q += 1
                    if nq > 4:
                        break
                good = [k for k, (_, zr) in enumerate(qs) if not zr]
                per_q = np.bincount(rng.integers(0, len(good), n_click), minlength=len(good)) if good else []
                gi = 0
                for query, zero in qs:
                    st["searches"] += 1
                    st["zero"] += int(zero)
                    n_res = 0 if zero else int(rng.integers(40, 2400))
                    action(t[0], "search", "search_results", query=query, results=n_res, version=cx["ranker"])
                    page("search_results", 0.5 if zero else 1.0, query=query, version=cx["ranker"], results=n_res)
                    if not zero:
                        for _ in range(per_q[gi]):
                            pos = int(rng.geometric(0.12 if cx["locale_bug"] else 0.30))
                            product(pick(self.cum_pop[cat]), "search", query=query, position=pos)
                        gi += 1
            else:
                fc = int(rng.poisson(cx["feed_clicks"] * engage))
                shown = 8 + int(rng.poisson(4 + 3 * fc))
                st["impressions"] += shown
                t0, dw = page("for_you", results=shown, version=cx["recs"])
                for _ in range(fc):
                    oos_click = rng.random() < cx["oos"] and hour["cum_out"][cat][-1] > 0
                    i = pick(hour["cum_out"][cat] if oos_click else hour["cum_in"][cat])
                    pos = int(rng.geometric(0.25))
                    action(t[0], "feed_click", "for_you", sku=sku_name(cat, i), position=pos, in_stock=int(stock[i]),
                           version=cx["recs"])
                    st["feed_clicks"] += 1
                    product(i, "for_you", position=pos, version=cx["recs"])

        # cart -> checkout -> payment -> order
        checkout = bool(cart) and rng.random() < min(1.0, cx["r_co"] * len(cart))
        method, result, order_id = "", "", ""
        if cart and not checkout and rng.random() < 0.45:
            t0, dw = page("cart")                                   # looked at the basket, then left
            if rng.random() < 0.25:
                gone = cart.pop(int(rng.integers(len(cart))))
                action(t0 + dw * 0.6, "remove_from_cart", "cart", sku=sku_name(cat, gone))
                st["removes"] += 1
        cart_value = float(self.price[cat][cart].sum()) if cart else 0.0
        attempted = False
        if checkout:
            t0, dw = page("cart")
            if len(cart) > 1 and rng.random() < 0.10:
                gone = cart.pop(int(rng.integers(len(cart))))
                action(t0 + dw * 0.6, "remove_from_cart", "cart", sku=sku_name(cat, gone))
                st["removes"] += 1
            cart_value = float(self.price[cat][cart].sum())
            action(t[0], "checkout_start", "checkout")
            slow = cx["checkout_p99"] > SLOW_CHECKOUT_MS
            t0, dw = page("checkout", max(1.0, cx["checkout_p99"] / 900))
            attempted = rng.random() < cx["r_pa"]
            if slow and not attempted:
                action(t0 + dw * 0.9, "checkout_error", "checkout", error_code="CHECKOUT_TIMEOUT")
            if attempted:
                k = int(np.searchsorted(np.cumsum(cx["method_p"]), rng.random() * sum(cx["method_p"])))
                method = cx["methods"][k]
                ok = rng.random() < cx["ok"][k]
                t0, dw = page("payment", 1.0 if ok else 1.6)
                action(t0 + dw * 0.5, "payment_attempt", "payment", payment_method=method, version=cx["sdk"])
                if ok:
                    result = "success"
                    self.n_orders += 1
                    order_id = f"O{self.n_orders:07d}"
                    action(t0 + dw * 0.9, "order_placed", "payment", payment_method=method)
                    page("order_confirmation")
                    self._order(rng, hour, r, cx, order_id, sid, uid, t0 + dw * 0.9, method, cart)
                else:
                    if method.startswith("local") and cx["broken_local"]:
                        err = "LOCAL_METHOD_SDK_ERROR"
                    elif method == "card" and cx["psp_fail"] and rng.random() < 0.9:
                        err = "AUTH_GATEWAY_TIMEOUT"
                    elif method == "wallet":
                        err = "wallet_cancelled"
                    else:
                        err = _pick(rng, *CARD_ERRORS)
                    result = "failed"
                    action(t0 + dw * 0.9, "payment_failed", "payment", payment_method=method, error_code=err,
                           version=cx["sdk"])

        # web analytics ingestion lag drops whole sessions from the clickstream (orders_db is separate)
        if hour["web_share"] < 1.0 and rng.random() > hour["web_share"]:
            return
        last_page = max(k for k, e in enumerate(ev) if e[1] == "page_view")
        sellers = self.sku_seller[cat]
        for k, e in enumerate(ev):
            self.clicks.append((sid, uid, e[0], k + 1, e[1], e[2], e[3], sellers[int(e[3][4:])] if e[3] else "", cat,
                                e[4], e[5], e[6], e[7], e[8], e[9], e[10], e[11], e[12], k == last_page))
        end = t[0]
        self.sessions.append((sid, uid, is_new, start, end, int(round(end - start)), c, p, cat, source, camp_name,
                              ev[0][2] if ev[0][1] == "page_view" else land, ev[last_page][2], st["page_views"],
                              st["page_views"] == 1, st["searches"], st["zero"], st["views"], st["oos_views"],
                              st["impressions"], st["feed_clicks"], st["wish"], st["adds"], st["removes"],
                              round(cart_value, 2), checkout, attempted, method, result, bool(order_id), order_id,
                              bool(cart) and not order_id, rng.random() < 0.002))

    def _bot_session(self, rng, hour, r):
        """Click bot: comes in through a sponsored ad, opens a few product pages in seconds, never buys."""
        c, p, cat = r["country"], r["platform"], r["category"]
        start = hour["ts_s"] + float(rng.uniform(0, 3600))
        uidx = len(self.users)
        self.users.append(dict(user_id=f"U{uidx:07d}", country=c, home_platform=p,
                               signup_date=str((EPOCH + pd.Timedelta(seconds=start)).date()), age_band="",
                               membership="none", persona="unknown", preferred_category=cat, acquired_via="sponsored_ad"))
        self.prop.append(0.0)
        self.n_sessions += 1
        sid, uid = f"S{self.n_sessions:08d}", f"U{uidx:07d}"
        t, n = start, 1 + int(rng.poisson(1.5))
        stock = hour["stock"][cat]
        sellers = self.sku_seller[cat]
        oos_views = 0
        for k in range(n):
            i = int(np.searchsorted(self.cum_pop[cat], rng.random() * self.cum_pop[cat][-1]))
            oos_views += int(not stock[i])
            dw = int(rng.integers(1, 4))
            self.clicks.append((sid, uid, t, k + 1, "page_view", "product", sku_name(cat, i), sellers[i], cat,
                                "external_ad" if k == 0 else "product", "", "", "", int(stock[i]), dw, "", "", "",
                                k == n - 1))
            t += dw
        self.sessions.append((sid, uid, True, start, t, int(round(t - start)), c, p, cat, "sponsored_ad", "", "product",
                              "product", n, n == 1, 0, 0, n, oos_views, 0, 0, 0, 0, 0, 0.0, False,
                              False, "", "", False, "", False, rng.random() < 0.55))

    def _order(self, rng, hour, r, cx, order_id, sid, uid, at, method, cart):
        if rng.random() > hour["orders_share"]:
            return                                          # row not ingested yet (orders_db lag)
        cat = r["category"]
        gmv = round(cx["aov"] * rng.lognormal(-0.03, 0.25), 2)
        prices = self.price[cat][cart]
        disc = round(gmv * cx["disc"] * rng.lognormal(0, 0.1), 2)
        self.orders.append((order_id, sid, uid, at, r["country"], r["platform"], cat, method, len(cart), gmv, disc,
                            round(gmv * cx["cogs"], 2), rng.random() < cx["refund"]))
        for i, pr in zip(cart, prices):
            self.items.append((order_id, sku_name(cat, i), cat, float(pr), round(gmv * pr / prices.sum(), 2)))

    # ------------------------------------------------------------------ outputs
    def tables(self):
        def ts(col):
            return (EPOCH + pd.to_timedelta(col, unit="s")).dt.floor("s")

        clicks = pd.DataFrame(self.clicks, columns=CLICK_COLS)
        clicks["ts"] = ts(clicks["ts"])
        sessions = pd.DataFrame(self.sessions, columns=SESSION_COLS)
        sessions["start_ts"], sessions["end_ts"] = ts(sessions["start_ts"]), ts(sessions["end_ts"])
        orders = pd.DataFrame(self.orders, columns=ORDER_COLS)
        orders["ts"] = ts(orders["ts"])
        items = pd.DataFrame(self.items, columns=ITEM_COLS)
        seen = set(sessions.user_id)
        users = pd.DataFrame([u for u in self.users if u["user_id"] in seen])
        return users, sessions, clicks, orders, items
