"""User-level report on the panel: funnel, cart abandonment, most clicked / favourited products, For You,
dwell time, exit pages, and a consistency check of the panel against facts.csv.

python -m marketplace.user_report                          whole period
python -m marketplace.user_report --from "2026-11-17 14:00" --to "2026-11-18 02:00" --country DE --platform android
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]


def load(d: Path):
    s = pd.read_csv(d / "sessions.csv", parse_dates=["start_ts", "end_ts"], dtype={"campaign": str, "order_id": str,
                    "payment_method": str, "payment_result": str}, keep_default_na=False)
    c = pd.read_csv(d / "clickstream.csv", parse_dates=["ts"], keep_default_na=False, low_memory=False)
    u = pd.read_csv(d / "users.csv")
    o = pd.read_csv(d / "orders.csv", parse_dates=["ts"])
    return s, c, u, o


def pct(a, b):
    return f"{a / b:6.1%}" if b else "   n/a"


def report(d: Path, frm=None, to=None, **seg):
    s, c, u, o = load(d)
    if frm:
        s = s[s.start_ts >= pd.Timestamp(frm)]
    if to:
        s = s[s.start_ts < pd.Timestamp(to)]
    for k, v in seg.items():
        if v:
            s = s[s[k] == v]
    c = c[c.session_id.isin(s.session_id)]
    n = len(s)
    print(f"\n=== user panel: {n:,} sessions, {s.user_id.nunique():,} customers"
          f"  ({s.start_ts.min():%d %b %H:%M} .. {s.start_ts.max():%d %b %H:%M})")
    if not n:
        return

    print("\n-- funnel (share of sessions)")
    steps = [("session", n), ("viewed a product", (s.product_views > 0).sum()), ("added to cart", (s.cart_adds > 0).sum()),
             ("started checkout", s.checkout_started.sum()), ("attempted payment", s.payment_attempted.sum()),
             ("ordered", s.ordered.sum())]
    prev = n
    for name, v in steps:
        print(f"  {name:<20} {v:>8,}  {pct(v, n)} of sessions  {pct(v, prev)} of previous step")
        prev = v
    carts = (s.cart_adds > 0).sum()
    print(f"\n  cart abandonment: {s.cart_abandoned.sum():,} of {carts:,} carts not bought ({pct(s.cart_abandoned.sum(), carts)}),"
          f" EUR {s.loc[s.cart_abandoned, 'cart_value'].sum():,.0f} left in baskets")
    print(f"  bounce rate {pct(s.bounced.sum(), n)} | pages/session {s.page_views.mean():.2f} | "
          f"median session {s.duration_s.median():.0f}s (mean {s.duration_s.mean():.0f}s) | "
          f"new customers {pct(s.is_new_user.sum(), n)}")
    pr = s.payment_result.fillna("")
    fails = c[c.event == "payment_failed"].error_code.value_counts()
    print(f"  payment success {pct((pr == 'success').sum(), (pr != '').sum())}; failures by error: "
          + ", ".join(f"{k} {v}" for k, v in fails.head(5).items()))

    print("\n-- where customers leave (exit page)        avg seconds on page")
    exits = s.exit_page.value_counts()
    dwell = c[c.event == "page_view"].groupby("page").dwell_s.agg(lambda x: pd.to_numeric(x).mean())
    for page, v in exits.items():
        print(f"  {page:<20} {v:>7,} exits {pct(v, n)}        {dwell.get(page, float('nan')):6.1f}s")

    pv = c[(c.event == "page_view") & (c.page == "product")]
    print("\n-- most viewed products              views  in stock   carts  favourites")
    carts_by = c[c.event == "add_to_cart"].sku.value_counts()
    fav_by = c[c.event == "wishlist_add"].sku.value_counts()
    top = pv.groupby("sku").agg(views=("sku", "size"), in_stock=("in_stock", lambda x: pd.to_numeric(x).mean()))
    for sku, r in top.sort_values("views", ascending=False).head(10).iterrows():
        print(f"  {sku:<30} {r.views:>7}   {r.in_stock:6.0%}  {carts_by.get(sku, 0):>6}  {fav_by.get(sku, 0):>10}")
    print("-- most favourited (wishlist): " + ", ".join(f"{k} ({v})" for k, v in fav_by.head(8).items()))

    print("\n-- For You feed")
    fy = c[(c.event == "page_view") & (c.page == "for_you")]
    fc = c[c.event == "feed_click"]
    imp = pd.to_numeric(fy.results, errors="coerce").sum()
    fsk = set(zip(fc.session_id, fc.sku))
    fy_carts = sum((a, b) in fsk for a, b in zip(c[c.event == 'add_to_cart'].session_id, c[c.event == 'add_to_cart'].sku))
    print(f"  feed visits {len(fy):,} | impressions {imp:,.0f} | clicks {len(fc):,} (CTR {pct(len(fc), imp)}) | "
          f"clicked item out of stock {pct((pd.to_numeric(fc.in_stock) == 0).sum(), len(fc))} | "
          f"carts from feed clicks {pct(fy_carts, len(fc))}")
    print("  by model: " + ", ".join(f"{m}: {g.shape[0]} clicks, {pct((pd.to_numeric(g.in_stock) == 0).sum(), len(g))} OOS"
                                     for m, g in fc.groupby("version")))

    print("\n-- search")
    q = c[c.event == "search"]
    res = pd.to_numeric(q.results)
    print(f"  queries {len(q):,} | zero-result {pct((res == 0).sum(), len(q))} | by ranker: "
          + ", ".join(f"{v}: {pct((pd.to_numeric(g.results) == 0).sum(), len(g))} zero" for v, g in q.groupby("version")))
    print("  top queries: " + ", ".join(f"{k} ({v})" for k, v in q["query"].value_counts().head(8).items()))

    print("\n-- by segment                 sessions  conv.   cart-aband.  bounce  median s")
    for dim in ("platform", "country", "traffic_source"):
        for k, g in s.groupby(dim):
            cg = (g.cart_adds > 0).sum()
            print(f"  {dim[:8]:<8} {str(k):<18} {len(g):>7,}  {pct(g.ordered.sum(), len(g))}  {pct(g.cart_abandoned.sum(), cg)}"
                  f"     {pct(g.bounced.sum(), len(g))}  {g.duration_s.median():6.0f}")
    seg_u = s.merge(u[["user_id", "persona"]], on="user_id")
    print("  persona: " + ", ".join(f"{k} {pct(g.ordered.sum(), len(g))} conv." for k, g in seg_u.groupby("persona")))


def consistency(d: Path):
    """Panel / sample vs facts.csv: the two views of the marketplace must agree."""
    s, c, _u, o = load(d)
    f = pd.read_csv(d / "facts.csv")
    meta = json.loads((d / "users_meta.json").read_text()) if (d / "users_meta.json").exists() else {}
    r = meta.get("sample", 0.01)
    checks = [("sessions", len(s), f.sessions.sum()),
              ("product_views", s.product_views.sum(), f.product_views.sum()),
              ("feed_clicks", s.feed_clicks.sum(), f.feed_clicks.sum()),
              ("add_to_carts", s.cart_adds.sum(), f.add_to_carts.sum()),
              ("payment_attempts", s.payment_attempted.sum(), f.payment_attempts.sum()),
              ("orders", s.ordered.sum(), f.orders.sum()),
              ("gmv (orders.csv)", o.gmv.sum(), f.gmv.sum())]
    print(f"\n-- consistency: panel / {r:.0%} vs facts.csv")
    for name, panel, fact in checks:
        print(f"  {name:<18} panel {panel / r:>14,.0f}   facts {fact:>14,.0f}   {panel / r / fact - 1:+.1%}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default=str(ROOT / "data" / "eye"))
    ap.add_argument("--from", dest="frm")
    ap.add_argument("--to")
    ap.add_argument("--country")
    ap.add_argument("--platform")
    ap.add_argument("--category")
    a = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")
    d = Path(a.data)
    report(d, a.frm, a.to, country=a.country, platform=a.platform, category=a.category)
    if not (a.frm or a.to or a.country or a.platform or a.category):
        consistency(d)


if __name__ == "__main__":
    main()
