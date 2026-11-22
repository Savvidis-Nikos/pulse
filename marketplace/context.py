"""Context layer: the data an investigation reaches for after the business metrics moved.

  audience.csv         hour x country x platform x category x browser x user_type (new / returning):
                       sessions, product_views, add_to_carts, checkouts, payment_attempts, orders, gmv.
                       An exact split of facts.csv (every cell sums back to the facts row), so the engine can
                       read the same metrics by browser or customer type ("segment isolation").
  product_sales.csv    hour x category x top-50 SKU: price, price change, views, add-to-carts, orders
                       (the product-level counterfactual: did only the repriced SKUs lose sales?)
  market.csv           hour x country x category: our price index vs competitors (1.00 = parity), market
                       demand index (100 = normal), active competitor promotion
  external_events.csv  competitor promotions picked up by price monitoring (with decoys that have no effect)

Internal changes that act here: pricing updates (pricing team), catalog launches (product team) and browser-
specific UI bugs (UI team); external: competitor promotions. Own random streams, so nothing else moves.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from .catalog import AOV, CATEGORIES, COUNTRIES, LISTED

H = pd.Timedelta(hours=1)
TOP = 50
BROWSERS = {"Chrome": 0.55, "Safari": 0.25, "Edge": 0.12, "Firefox": 0.08}       # web only; apps are "app"
NEW_SHARE = 0.30                     # share of sessions from new customers (campaign traffic: 0.65, bots: 1.0)
VIEW_MULT = {"new": 0.90, "returning": 1.05}
CART_MULT = {"new": 0.65, "returning": 1.15}
ELASTICITY = 4.0


def price_factor(pct: float) -> float:
    return (1 + pct) ** (-ELASTICITY)


class Context:
    def __init__(self, seed: int, catalog, start):
        self.seed = seed
        self.start = pd.Timestamp(start)
        rng = np.random.default_rng([seed, 9200])
        self.pop = catalog.pop
        self.price = {cat: (AOV[cat] * rng.lognormal(0, 0.45, n)).round(2) for cat, n in LISTED.items()}
        self.rows = {k: [] for k in ("audience", "product_sales", "market")}
        self.external: list[dict] = []
        # slow random walk of our price position and market demand per country x category
        self.idx = {(c, k): 1.0 + rng.normal(0, 0.01) for c in COUNTRIES for k in CATEGORIES}
        self.dem = {(c, k): 100.0 for c in COUNTRIES for k in CATEGORIES}

    def begin(self, ts):
        self.rng = np.random.default_rng([self.seed, 9201, int((ts - self.start) / H)])

    # ------------------------------------------------------------------ effects used by world.step (x1 inputs)
    def repriced(self, s, ts, cat):
        """Indices of repriced SKUs and the price change, for active pricing updates in `cat`."""
        for pc in s.price_changes:
            if pc["category"] == cat and pd.Timestamp(pc["start"]) <= ts < pd.Timestamp(pc["end"]):
                return np.arange(0, pc["top"], 2), pc["pct"]
        return None, 0.0

    def cart_mult(self, s, ts, seg) -> np.ndarray:
        m = np.ones(len(seg))
        for cat in CATEGORIES:
            idx, pct = self.repriced(s, ts, cat)
            if idx is not None:          # demand on repriced SKUs falls with the price elasticity
                w = float(self.pop[cat][idx].sum())
                m[(seg.category == cat).to_numpy()] *= 1 - w * (1 - price_factor(pct))
        for p in self.promos(s, ts):     # customers compare and buy at the competitor
            mask = ((seg.country == p["country"]) & (seg.category == p["category"])).to_numpy()
            m[mask] *= 1 - p["cart_loss"]
        return m

    def traffic_mult(self, s, ts, seg) -> np.ndarray:
        m = np.ones(len(seg))
        for p in self.promos(s, ts):
            mask = ((seg.country == p["country"]) & (seg.category == p["category"])).to_numpy()
            m[mask] *= 1 - p["traffic_loss"]
        return m

    def attempt_mult(self, s, ts, seg) -> np.ndarray:
        """Browser-specific checkout bug: the web segment loses attempts in proportion to that browser's share."""
        m = np.ones(len(seg))
        for b in self.browser_bugs(s, ts):
            mask = (seg.platform == "web").to_numpy()
            m[mask] *= 1 - BROWSERS[b["browser"]] * (1 - b["attempt_mult"])
        return m

    @staticmethod
    def promos(s, ts):
        return [p for p in s.competitor_promos if pd.Timestamp(p["start"]) <= ts < pd.Timestamp(p["end"])]

    @staticmethod
    def browser_bugs(s, ts):
        return [b for b in s.browser_bugs if pd.Timestamp(b["start"]) <= ts < pd.Timestamp(b["end"])]

    def catalog_overlay(self, s, ts, inv_rows):
        """Catalog launches: new SKUs are listed (and in stock) from the launch on; no demand yet."""
        out = []
        for r in inv_rows:
            add = sum(c["n"] for c in s.catalog_launches if c["category"] == r["category"] and ts >= pd.Timestamp(c["start"]))
            out.append(dict(r, listed_skus=r["listed_skus"] + add, in_stock_skus=r["in_stock_skus"] + add) if add else r)
        return out

    # ------------------------------------------------------------------ tables, after facts are final
    def hour(self, mp, ts, f: pd.DataFrame, stock_now, lam, camp_lam, bots):
        s, rng = mp.state, self.rng
        self._audience(s, ts, f, lam, camp_lam, bots, rng)
        self._products(s, ts, f, stock_now, rng)
        self._market(s, ts, rng)

    def _audience(self, s, ts, f, lam, camp_lam, bots, rng):
        bugs = self.browser_bugs(s, ts)
        bot_s = bots["sessions"] if bots else np.zeros(len(f), int)
        bot_v = bots["views"] if bots else np.zeros(len(f), int)
        rows = []
        for i, r in enumerate(f.itertuples(index=False)):
            web = r.platform == "web"
            cells = [(b, u) for b in BROWSERS for u in ("new", "returning")] if web else \
                [("app", u) for u in ("new", "returning")]
            camp = camp_lam[i] / max(lam[i] + camp_lam[i], 1e-9)
            p_new = NEW_SHARE * (1 - camp) + 0.65 * camp
            w = np.array([(BROWSERS[b] if web else 1.0) * (p_new if u == "new" else 1 - p_new) for b, u in cells])
            bot_cell = cells.index(("Chrome", "new") if web else ("app", "new"))
            sess = _split(rng, max(int(r.sessions) - int(bot_s[i]), 0), w)
            sess[bot_cell] += int(bot_s[i])
            human_s = sess.astype(float).copy()
            human_s[bot_cell] -= bot_s[i]
            views = _split(rng, max(int(r.product_views) - int(bot_v[i]), 0),
                           human_s * np.array([VIEW_MULT[u] for _, u in cells]))
            human_v = views.astype(float)
            views[bot_cell] += int(bot_v[i])
            # nested thinning: carts come out of views, checkouts out of carts, ... (binomial-level noise, exact sums)
            carts = _thin(rng, human_v.astype(int), int(r.add_to_carts), np.array([CART_MULT[u] for _, u in cells]))
            chk = _thin(rng, carts, int(r.checkouts))
            am = np.ones(len(cells))
            for b in bugs:
                am[[k for k, (br, _) in enumerate(cells) if br == b["browser"]]] *= b["attempt_mult"]
            att = _thin(rng, chk, int(r.payment_attempts), am)
            orders = _thin(rng, att, int(r.orders))
            gmv = _money(float(r.gmv), orders if orders.sum() else sess)
            for k, (b, u) in enumerate(cells):
                rows.append((ts, r.country, r.platform, r.category, b, u, sess[k], views[k], carts[k], chk[k], att[k],
                             orders[k], gmv[k]))
        self.rows["audience"].append(pd.DataFrame(rows, columns=[
            "ts", "country", "platform", "category", "browser", "user_type", "sessions", "product_views",
            "add_to_carts", "checkouts", "payment_attempts", "orders", "gmv"]))

    def _products(self, s, ts, f, stock_now, rng):
        out = []
        for cat in CATEGORIES:
            g = f[f.category == cat]
            views, carts, orders = int(g.product_views.sum()), int(g.add_to_carts.sum()), int(g.orders.sum())
            pop, ins = self.pop[cat], stock_now[cat].astype(float)
            el = np.ones(len(pop))
            pct = np.zeros(len(pop))
            idx, ch = self.repriced(s, ts, cat)
            if idx is not None:
                el[idx], pct[idx] = price_factor(ch), ch
            denom = float((pop * ins * el).sum()) or 1.0
            top = np.arange(TOP)
            v = rng.poisson(views * pop[top])
            exp_c = carts * pop[top] * ins[top] * el[top] / denom
            c = np.minimum(rng.poisson(exp_c), v)
            o = rng.binomial(c, min(1.0, orders / carts) if carts else 0.0)
            out.append(pd.DataFrame(dict(ts=ts, category=cat, sku=[f"{cat[:3].upper()}-{i:05d}" for i in top],
                                         price=(self.price[cat][top] * (1 + pct[top])).round(2),
                                         price_change=pct[top].round(3), in_stock=ins[top].astype(int),
                                         views=v, add_to_carts=c, orders=o)))
        self.rows["product_sales"].append(pd.concat(out, ignore_index=True))

    def _market(self, s, ts, rng):
        active = {(p["country"], p["category"]): p for p in self.promos(s, ts)}
        rows = []
        for (c, k) in self.idx:
            self.idx[(c, k)] += rng.normal(0, 0.002) - 0.02 * (self.idx[(c, k)] - 1.0)
            self.dem[(c, k)] += rng.normal(0, 1.0) - 0.05 * (self.dem[(c, k)] - 100.0)
            p = active.get((c, k))
            idx = self.idx[(c, k)] / (1 - p["discount"]) if p else self.idx[(c, k)]
            rows.append((ts, c, k, round(idx, 4), round(self.dem[(c, k)], 1), p["competitor"] if p else ""))
        self.rows["market"].append(pd.DataFrame(rows, columns=["ts", "country", "category", "price_index",
                                                               "demand_index", "competitor_promo"]))

    def tables(self) -> dict[str, pd.DataFrame]:
        out = {k: pd.concat(v, ignore_index=True) for k, v in self.rows.items() if v}
        out["external_events"] = pd.DataFrame(self.external, columns=[
            "ts", "name", "kind", "competitor", "country", "category", "discount", "start", "end", "source"])
        return out


def _split(rng, n: int, w) -> np.ndarray:
    w = np.clip(np.asarray(w, float), 0, None)
    if n <= 0 or w.sum() <= 0:
        return np.zeros(len(w), int)
    return rng.multinomial(n, w / w.sum())


def _thin(rng, parent: np.ndarray, n: int, mult=None) -> np.ndarray:
    """Pick n of the parent units across cells (each unit at most once), cell propensity `mult`."""
    parent = np.asarray(parent, int)
    mult = np.ones(len(parent)) if mult is None else np.asarray(mult, float)
    n = min(int(n), int(parent.sum()))
    if n <= 0:
        return np.zeros(len(parent), int)
    w = parent * mult
    p = np.clip(n * mult / max(w.sum(), 1e-9), 0, 1)
    k = rng.binomial(parent, p)
    d = n - int(k.sum())
    if d > 0:
        k += rng.multivariate_hypergeometric(parent - k, d)
    elif d < 0:
        k -= rng.multivariate_hypergeometric(k, -d)
    return k


def _money(total: float, orders: np.ndarray) -> np.ndarray:
    """Split a money total by weights (orders; sessions when late rows left no orders) so cells sum exactly."""
    if not orders.sum():
        out = np.zeros(len(orders))
        out[0] = total
        return out
    out = np.round(total * orders / orders.sum(), 2)
    out[int(np.argmax(orders))] += round(total - out.sum(), 2)     # cells sum exactly to the facts row
    return out
