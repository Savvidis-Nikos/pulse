"""Build a realistic fake Pulse state dir (pulse/fixtures/state_demo) from the contract models.

    .venv/Scripts/python.exe -m pulse.dashboard.make_fixtures              # (re)write the fixture dir
    .venv/Scripts/python.exe -m pulse.dashboard.make_fixtures --simulate D # replay a live investigation into D

`--simulate` writes everything except the final interpretation, then grows
interpretations/<id>.partial.json one trace step every ~2 s and finally writes the final file, so the
dashboard's "investigating..." animation can be demoed without any agent running.
"""
from __future__ import annotations

import argparse
import json
import math
import random
import shutil
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from pulse.contracts import (Brief, BriefItem, Edge, Finding, GraphChange, GraphSnapshot, Incident,
                             Interpretation, KpiStatus, Node, SeriesPoint, TickState, TraceStep)

HERE = Path(__file__).resolve().parent
OUT = HERE.parent / "fixtures" / "state_demo"
NOW = datetime(2026, 11, 22, 11, 0, tzinfo=timezone.utc)
ORDERS_ID = "inc-0412-orders"
CONV_ID = "inc-0409-conversion"
GMV_ID = "inc-0398-gmv"
# production convention: snapshot_id = "<kpi>-<center>-<YYYYmmddHH>", interpretation_id = incident id
ORDERS_SNAP = "orders-orders-2026112211"
CONV_SNAP = "conversion_rate-conversion_rate-2026112202"
GMV_SNAP = "gmv-gmv-2026112115"


def iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


# ------------------------------------------------------------------ KPI series

def _series(base: float, amp: float, noise: float, seed: int, shock=None, missing_last: int = 0,
            band: float = 0.06) -> list[SeriesPoint]:
    """48 hourly points: daily seasonality around `base`, optional shock(i) multiplier on the last hours."""
    rnd = random.Random(seed)
    pts = []
    for i in range(48):
        ts = NOW - timedelta(hours=47 - i)
        season = 1 + amp * math.sin((ts.hour - 8) / 24 * 2 * math.pi)
        exp = base * season
        val = exp * (1 + rnd.gauss(0, noise))
        if shock:
            val *= shock(i)
        val_out = None if i >= 48 - missing_last else round(val, 6)
        pts.append(SeriesPoint(ts=iso(ts), value=val_out, expected=round(exp, 6),
                               lo=round(exp * (1 - band), 6), hi=round(exp * (1 + band), 6)))
    return pts


def _kpi(name, label, unit, polarity, series: list[SeriesPoint], severity, completeness=1.0, incident=None,
         z=None) -> KpiStatus:
    last = next((p for p in reversed(series) if p.value is not None), series[-1])
    value, expected = last.value, series[-1].expected
    dev = None if value is None or not expected else round((value - expected) / abs(expected) * 100, 2)
    direction = "flat" if dev is None or abs(dev) < 1 else ("up" if dev > 0 else "down")
    if z is None and dev is not None:
        z = round(dev / 3.2, 2)
    return KpiStatus(name=name, label=label, unit=unit, polarity=polarity, value=value, expected=expected,
                     deviation_pct=dev, z=z, direction=direction, severity=severity, completeness=completeness,
                     incident_id=incident, series=series)


def build_state() -> TickState:
    release = lambda i: 0.86 if i >= 46 else (0.93 if i == 45 else 1.0)          # 09:00 release, orders drop
    campaign = lambda i: 1.10 if i >= 36 else 1.0                                  # FR campaign since yesterday
    conv = lambda i: (0.94 if i >= 36 else 1.0) * (0.86 if i >= 46 else 1.0)
    kpis = [
        _kpi("gmv", "Revenue / hour", "EUR", "up_is_good",
             _series(48500, 0.35, 0.02, 1, shock=lambda i: 0.97 if i >= 46 else 1.0, band=0.07), "ok"),
        _kpi("orders", "Successful orders / hour", "count", "up_is_good",
             _series(945, 0.35, 0.015, 2, shock=release), "alert", incident=ORDERS_ID, z=-4.1),
        _kpi("conversion_rate", "User conversion", "ratio", "up_is_good",
             _series(0.0312, 0.05, 0.012, 3, shock=lambda i: 0.94 if i >= 36 else 1.0, band=0.04), "watch",
             incident=CONV_ID, z=-2.2),
        _kpi("contribution_margin", "Contribution margin / hour", "EUR", "up_is_good",
             _series(9100, 0.35, 0.03, 4, missing_last=3, band=0.09), "data_issue", completeness=0.55),
        _kpi("sessions", "Traffic (sessions / hour)", "count", "up_is_good",
             _series(30300, 0.4, 0.015, 5, shock=lambda i: 1.04 if i >= 36 else 1.0, band=0.07), "ok"),
        _kpi("aov", "Average basket", "EUR", "up_is_good", _series(51.3, 0.02, 0.01, 6, band=0.04), "ok"),
        _kpi("in_stock_rate", "Product availability", "ratio", "up_is_good",
             _series(0.947, 0.0, 0.003, 7, band=0.015), "ok"),
        _kpi("service_error_rate", "Site error rate", "ratio", "down_is_good",
             _series(0.0041, 0.1, 0.04, 8, band=0.2), "ok"),
    ]
    _ = campaign, conv
    log = [
        "tick 412: 8 KPIs checked, 1 alert (orders), 1 watch (conversion), 1 data issue (contribution margin)",
        "orders -14% vs expected (z=-4.1): opened incident inc-0412-orders, asked Graph Interpreter to investigate",
        "conversion_rate still -6%: incident inc-0409-conversion persisting (innocent: FR campaign), no re-investigation",
        "contribution_margin: cogs feed 55% complete - flagged as data issue instead of alerting",
        "Graph Handler added auth_timeout_rate from raw auth logs (identity check error 0.3%)",
        "CEO briefing rewritten (1 new item)",
    ]
    return TickState(ts=iso(NOW), tick=412, kpis=kpis, spend_usd=1.84, llm_mode="replay", log=log)


# ------------------------------------------------------------------ snapshots

def N(id, label, depth, status="ok", value=None, expected=None, unit="ratio", polarity="up_is_good", domain="funnel",
      kind="metric", definition=None, formula_status=None, roles=(), added_by="seed", completeness=1.0, desc="",
      tier=None) -> Node:
    dev = None
    if value is not None and expected:
        dev = round((value - expected) / abs(expected) * 100, 1)
    z = None if dev is None else round(dev / 3.5, 1)
    return Node(id=id, label=label, kind=kind, domain=domain, tier=tier, unit=unit, polarity=polarity,
                description=desc, definition=definition, value=value, expected=expected, deviation_pct=dev, z=z,
                completeness=completeness, status=status, formula_status=formula_status, added_by=added_by,
                depth=depth, roles=list(roles))


def E(s, t, kind="formula", status="validated", op=None, sign=None, lag_h=None, strength=None, contribution_pct=None,
      label="", added_by="seed") -> Edge:
    return Edge(source=s, target=t, kind=kind, status=status, op=op, sign=sign, lag_h=lag_h, strength=strength,
                contribution_pct=contribution_pct, label=label, added_by=added_by)


PSR_SEG = "payment_success_rate|DE·android"


def _enrich(nodes: list[Node], own: dict[str, tuple]) -> list[Node]:
    """own-vs-inherited change: {node: (own_deviation_pct, inherited_pct, onset hours before NOW)}."""
    for n in nodes:
        if n.id in own:
            o, inh, h = own[n.id]
            n.own_deviation_pct, n.inherited_pct = o, inh
            n.onset_ts = iso(NOW - timedelta(hours=h)) if h is not None else None
    return nodes
RELEASE = "comp:android_app_3.42"


def snapshot_orders() -> GraphSnapshot:
    nodes = [
        N("orders", "Successful orders", 0, "anomalous", 812, 945, "count", kind="kpi", tier="outcome",
          definition="sessions × conversion_rate", formula_status="validated", roles=["kpi"],
          desc="Successfully paid orders"),
        N("sessions", "Sessions", 1, "ok", 31480, 30300, "count", tier="outcome", desc="Visits to the marketplace"),
        N("conversion_rate", "Conversion rate", 1, "anomalous", 0.0258, 0.0312, tier="outcome",
          definition="orders / sessions", formula_status="validated", roles=["on_path"], desc="Orders per session"),
        N("payment_attempts", "Payment attempts", 1, "ok", 1003, 1012, "count",
          definition="checkouts × payment_attempt_rate", formula_status="validated",
          desc="Payment authorisations attempted"),
        N("payment_success_rate", "Payment success", 1, "anomalous", 0.810, 0.934, domain="payments",
          definition="orders / payment_attempts", formula_status="validated", roles=["on_path"],
          desc="Successful payments per attempt"),
        N("gmv", "Revenue (GMV)", 1, "anomalous", 41650, 48500, "EUR", domain="economics", tier="north_star",
          definition="orders × aov", formula_status="validated", roles=["symptom"],
          desc="Gross merchandise value of paid orders"),
        N("aov", "Average order value", 1, "ok", 51.3, 51.3, "EUR", domain="economics", definition="gmv / orders",
          formula_status="validated", roles=["ruled_out"], desc="Average order value"),
        # depth 2
        N("product_view_rate", "Product view rate", 2, "ok", 2.41, 2.44, definition="product_views / sessions",
          formula_status="validated", roles=["ruled_out"], desc="Product page views per session"),
        N("cart_rate", "Cart rate", 2, "ok", 0.081, 0.082, definition="add_to_carts / product_views",
          formula_status="validated", roles=["ruled_out"], desc="Add-to-carts per product view"),
        N("checkout_rate", "Checkout rate", 2, "ok", 0.402, 0.405, definition="checkouts / add_to_carts",
          formula_status="validated", roles=["ruled_out"], desc="Checkouts started per add-to-cart"),
        N("payment_attempt_rate", "Payment attempt rate", 2, "ok", 0.871, 0.874,
          definition="payment_attempts / checkouts", formula_status="validated", roles=["ruled_out"],
          desc="Payment attempts per checkout"),
        N(PSR_SEG, "Payment success · DE / Android", 2, "anomalous", 0.581, 0.931, domain="payments", kind="segment",
          definition="payment_success_rate where country=DE and platform=android", formula_status="validated",
          roles=["on_path", "first_cause"], desc="Payment success for Android app users in Germany"),
        N("auth_failure_rate", "Auth failure rate", 2, "anomalous", 0.178, 0.061, domain="payments",
          polarity="down_is_good", definition="auth_failures / auth_attempts", formula_status="validated",
          roles=["symptom"], desc="Share of payment authorisations that fail"),
        N("service_error_rate", "Service error rate", 2, "ok", 0.0042, 0.0041, domain="platform",
          polarity="down_is_good", definition="service_errors / service_requests", formula_status="validated",
          roles=["ruled_out"], desc="Share of service requests that fail"),
        N("checkouts", "Checkouts", 2, "ok", 1151, 1158, "count", desc="Checkout flows started"),
        N("sessions|FR", "Sessions · France", 2, "anomalous", 6930, 6300, "count", kind="segment",
          roles=["ruled_out"], desc="Sessions from France (marketing campaign running)"),
        N("marketing_cost", "Marketing spend", 2, "anomalous", 2480, 1650, "EUR", domain="marketing",
          polarity="neutral", desc="Paid media spend that bought traffic"),
        N("discount_share", "Discount share", 2, "ok", 0.061, 0.06, domain="economics", polarity="down_is_good",
          definition="discounts / gmv", formula_status="validated", desc="Discounts as a share of GMV"),
        N("contribution_margin", "Contribution margin", 2, "unknown", None, 9100, "EUR", domain="economics",
          definition="gmv - discounts - cogs - ad_cost", formula_status="incomplete", completeness=0.55,
          desc="GMV minus discounts, cost of goods and advertising cost"),
        # depth 3
        N("crash_rate", "App crash rate", 3, "ok", 0.0081, 0.0079, domain="platform", polarity="down_is_good",
          definition="app_crashes / app_sessions", formula_status="validated", roles=["ruled_out"],
          desc="Crashes per app session"),
        N("in_stock_rate", "In-stock rate", 3, "missing_data", None, 0.947, domain="supply", completeness=0.0,
          definition="in_stock_skus / listed_skus", formula_status="validated",
          desc="Share of listed products that are in stock (inventory snapshot did not arrive)"),
        N("cogs", "Cost of goods", 3, "missing_data", 14100, 25600, "EUR", domain="economics",
          polarity="down_is_good", completeness=0.55, desc="Cost of goods sold (feed 55% complete)"),
        N(RELEASE, "Android app 3.42 release", 3, "anomalous", 1, 0, "event", domain="platform", kind="component",
          polarity="neutral", roles=["unusual"], desc="Android app 3.42 rolled out to DE at 09:00 UTC"),
        N("auth_timeout_rate", "Auth timeout rate", 3, "anomalous", 0.312, 0.004, domain="payments",
          polarity="down_is_good", definition="auth_timeouts / auth_attempts", formula_status="unvalidated",
          added_by="graph_handler", roles=["on_path"],
          desc="Share of authorisations that time out - extracted by the Graph Handler from raw auth logs"),
        N("search_ctr", "Search CTR", 3, "stale", 0.97, 0.98, definition="product_views / searches",
          formula_status="validated", completeness=0.8, desc="Product views per search (last update 6 h ago)"),
        N("add_to_carts", "Add to carts", 3, "ok", 2870, 2890, "count", desc="Products added to the cart"),
    ]
    edges = [
        E("sessions", "orders", op="×", contribution_pct=-28.0, label="orders = sessions × conversion"),
        E("conversion_rate", "orders", op="×", contribution_pct=128.0, label="orders = sessions × conversion"),
        E("payment_attempts", "orders", op="×", contribution_pct=7.0),
        E("payment_success_rate", "orders", op="×", contribution_pct=93.0),
        E("orders", "gmv", op="×"), E("aov", "gmv", op="×"),
        E("orders", "aov", op="/"),
        E("product_view_rate", "conversion_rate", op="×", contribution_pct=6.0),
        E("cart_rate", "conversion_rate", op="×", contribution_pct=5.0),
        E("checkout_rate", "conversion_rate", op="×", contribution_pct=3.0),
        E("payment_attempt_rate", "conversion_rate", op="×", contribution_pct=2.0),
        E("payment_success_rate", "conversion_rate", op="×", contribution_pct=84.0),
        E("checkouts", "payment_attempts", op="×"), E("payment_attempt_rate", "payment_attempts", op="×"),
        E(PSR_SEG, "payment_success_rate", kind="breakdown", op="country × platform", contribution_pct=91.0,
          label="DE/android explains 91% of the drop"),
        E("auth_failure_rate", "payment_success_rate", kind="cause", status="validated", sign=-1, lag_h=0,
          strength=0.97, label="failed authorisations are failed payments"),
        E("service_error_rate", "payment_success_rate", kind="cause", status="asserted", sign=-1, lag_h=0,
          strength=0.4, label="backend errors break payment calls"),
        E("sessions|FR", "sessions", kind="breakdown", op="country", contribution_pct=54.0),
        E("marketing_cost", "sessions", kind="cause", status="learned", sign=1, lag_h=1, strength=0.81,
          label="paid media buys traffic", added_by="graph_handler"),
        E("discount_share", "aov", kind="cause", status="asserted", sign=-1, label="discounts lower basket value"),
        E("gmv", "contribution_margin", op="+"),
        E("cogs", "contribution_margin", op="-"),
        E("crash_rate", "payment_attempt_rate", kind="cause", status="asserted", sign=-1, lag_h=0, strength=0.6,
          label="crashes abort checkout"),
        E("in_stock_rate", "cart_rate", kind="cause", status="asserted", sign=1, label="out of stock cannot be carted"),
        E("search_ctr", "product_view_rate", kind="cause", status="ambiguous", sign=1, strength=0.35,
          label="relevance drives views (sign unstable across countries)", added_by="graph_handler"),
        E("add_to_carts", "cart_rate", op="/"),
        E("auth_timeout_rate", "auth_failure_rate", kind="formula", status="learned", op="+", strength=0.98,
          label="timeouts are counted as auth failures", added_by="graph_handler"),
        E(RELEASE, PSR_SEG, kind="proposed", status="proposed", sign=-1, lag_h=0, strength=0.94,
          label="new: release 3.42 reached DE/android at 09:00, drop starts the same hour", added_by="interpreter"),
        E(RELEASE, "crash_rate", kind="component", status="asserted", label="app release can change crash rate"),
    ]
    nodes = _enrich(nodes, {
        "orders": (-0.4, 97, 2), "conversion_rate": (-0.6, 96, 2), "payment_success_rate": (-1.1, 92, 2),
        "gmv": (0.0, 100, 2), PSR_SEG: (-37.0, 3, 2), "auth_failure_rate": (12.0, 94, 2),
        "auth_timeout_rate": (7700.0, 0, 2), RELEASE: (100.0, 0, 2), "sessions|FR": (10.0, 0, 12),
        "marketing_cost": (50.3, 0, 12), "sessions": (0.2, 95, 12)})
    return GraphSnapshot(snapshot_id=ORDERS_SNAP, kpi="orders", center="orders", ts=iso(NOW), window_h=3, depth=3,
                         nodes=nodes, edges=edges,
                         notes=["inventory snapshot (in_stock_rate) did not arrive this window",
                                "cogs feed 55% complete: contribution_margin formula incomplete",
                                "search_ctr last updated 6 h ago (stale)"])


def trace_orders() -> list[TraceStep]:
    return [
        TraceStep(i=1, action="look", node="orders",
                  finding="Orders are 14% below the seasonal baseline (812 vs 945 per hour), z = -4.1.",
                  numbers={"value": 812, "expected": 945, "deviation_pct": -14.1, "z": -4.1},
                  highlight_nodes=["orders"]),
        TraceStep(i=2, action="expand", node="orders",
                  finding="orders = sessions × conversion: traffic is up 3.9%, so the whole drop sits in conversion.",
                  numbers={"sessions_dev_pct": 3.9, "conversion_dev_pct": -17.3, "conversion_share_pct": 128},
                  highlight_nodes=["orders", "sessions", "conversion_rate"],
                  highlight_edges=[("sessions", "orders"), ("conversion_rate", "orders")]),
        TraceStep(i=3, action="detail", node="conversion_rate",
                  finding="Walking the funnel: views, cart, checkout and attempt rates are flat; payment success "
                          "fell from 93.4% to 81.0%.",
                  numbers={"product_view_rate": -1.2, "cart_rate": -1.2, "checkout_rate": -0.7,
                           "payment_attempt_rate": -0.3, "payment_success_rate": -13.3},
                  highlight_nodes=["conversion_rate", "product_view_rate", "cart_rate", "checkout_rate",
                                   "payment_attempt_rate", "payment_success_rate"],
                  highlight_edges=[("payment_success_rate", "conversion_rate")]),
        TraceStep(i=4, action="rule_out", node="crash_rate",
                  finding="App crashes (0.81%) and backend errors (0.42%) are normal, so the app and the platform "
                          "are not breaking checkout.",
                  numbers={"crash_rate": 0.0081, "crash_expected": 0.0079, "service_error_rate": 0.0042},
                  highlight_nodes=["crash_rate", "service_error_rate", "payment_attempt_rate"],
                  highlight_edges=[("crash_rate", "payment_attempt_rate"),
                                   ("service_error_rate", "payment_success_rate")]),
        TraceStep(i=5, action="breakdown", node="payment_success_rate", args={"by": ["country", "platform"]},
                  finding="By country × platform: DE / Android collapsed to 58% (expected 93%); every other "
                          "segment is within ±1.5%. DE / Android explains 91% of the drop.",
                  numbers={"DE_android": 0.581, "DE_android_expected": 0.931, "explained_pct": 91,
                           "other_segments_max_dev_pct": 1.5},
                  highlight_nodes=["payment_success_rate", PSR_SEG],
                  highlight_edges=[(PSR_SEG, "payment_success_rate")]),
        TraceStep(i=6, action="detail", node="auth_failure_rate",
                  finding="Auth failures tripled (17.8% vs 6.1%), almost all timeouts (31% of DE / Android "
                          "authorisations) - a metric the Graph Handler just added from raw auth logs.",
                  numbers={"auth_failure_rate": 0.178, "expected": 0.061, "auth_timeout_rate": 0.312},
                  highlight_nodes=["auth_failure_rate", "auth_timeout_rate", "payment_success_rate"],
                  highlight_edges=[("auth_failure_rate", "payment_success_rate"),
                                   ("auth_timeout_rate", "auth_failure_rate")]),
        TraceStep(i=7, action="propose_edge", node=RELEASE,
                  finding="Android app 3.42 reached German users at 09:00 and the drop starts the same hour; "
                          "proposed a new edge release → DE / Android payment success.",
                  numbers={"lag_h": 0, "correlation": 0.94, "rollout_share_DE": 0.71},
                  highlight_nodes=[RELEASE, PSR_SEG], highlight_edges=[(RELEASE, PSR_SEG)]),
        TraceStep(i=8, action="conclude", node=PSR_SEG,
                  finding="First cause: payment success for Android users in Germany since the 3.42 release. "
                          "Revenue and auth failures are symptoms. In-stock data is missing but cart rate is "
                          "flat, so it cannot explain the drop.",
                  numbers={"orders_lost_per_h": 133, "revenue_lost_per_h_eur": 6850},
                  highlight_nodes=["orders", "payment_success_rate", PSR_SEG],
                  highlight_edges=[("payment_success_rate", "orders"), (PSR_SEG, "payment_success_rate")]),
    ]


def interp_orders(steps: list[TraceStep] | None = None) -> Interpretation:
    return Interpretation(
        interpretation_id=ORDERS_ID, incident_id=ORDERS_ID, kpi="orders", snapshot_id=ORDERS_SNAP, ts=iso(NOW),
        headline="Orders down 14%: card payments failing on Android in Germany since app 3.42",
        summary="Successful orders are 14% below normal (812 vs 945 per hour). Traffic is fine; the loss sits "
                "entirely in payments, where Android users in Germany now succeed only 58% of the time instead "
                "of 93%. The drop started at 09:00, the hour app release 3.42 reached Germany, and shows up as "
                "authorisation timeouts. It costs about 133 orders and EUR 6.9k revenue per hour.",
        path=["orders", "payment_success_rate", PSR_SEG],
        first_cause=Finding(node=PSR_SEG, why="Payment success for DE / Android dropped from 93% to 58% at 09:00, "
                                              "explaining 91% of the order drop; all other segments are normal.",
                            numbers={"value": 0.581, "expected": 0.931, "explained_pct": 91}),
        confidence="supported",
        symptoms=[Finding(node="gmv", why="Revenue follows orders: -14% with an unchanged basket.",
                          numbers={"deviation_pct": -14.1, "aov_dev_pct": 0.0}),
                  Finding(node="auth_failure_rate", why="Failed authorisations are the payment failures, seen "
                                                        "from the provider side.",
                          numbers={"value": 0.178, "expected": 0.061}),
                  Finding(node="conversion_rate", why="Conversion falls because payments fail, not because "
                                                      "shoppers changed behaviour.",
                          numbers={"deviation_pct": -17.3})],
        unusual=[Finding(node="auth_timeout_rate", why="Timeouts jumped from 0.4% to 31% on DE / Android.",
                         numbers={"value": 0.312, "expected": 0.004}),
                 Finding(node=RELEASE, why="Release 3.42 rolled out to 71% of German Android users at 09:00.",
                         numbers={"rollout_share_DE": 0.71})],
        missing_data=[Finding(node="in_stock_rate", why="Inventory snapshot did not arrive; cart rate is flat so "
                                                        "availability cannot explain the drop.",
                              numbers={"completeness": 0.0}),
                      Finding(node="cogs", why="Cost feed 55% complete: margin impact cannot be computed yet.",
                              numbers={"completeness": 0.55})],
        ruled_out=[Finding(node="sessions", why="Traffic is up 3.9% (FR campaign).", numbers={"deviation_pct": 3.9}),
                   Finding(node="checkout_rate", why="Checkout starts per cart are flat.",
                           numbers={"deviation_pct": -0.7}),
                   Finding(node="crash_rate", why="Crash rate is normal at 0.81%.", numbers={"value": 0.0081}),
                   Finding(node="service_error_rate", why="Backend error rate is normal at 0.42%.",
                           numbers={"value": 0.0042}),
                   Finding(node="aov", why="Basket value unchanged.", numbers={"deviation_pct": 0.0})],
        new_connections=[E(RELEASE, PSR_SEG, kind="proposed", status="proposed", sign=-1, lag_h=0, strength=0.94,
                           label="release 3.42 → DE/android payment success", added_by="interpreter")],
        uncertainty="We see the timing match and the timeouts, but not the app code: the release is the most likely "
                    "trigger, a provider change for Android in DE at the same hour would look the same.",
        watch_next=["DE / Android payment success after a 3.42 rollback or hotfix",
                    "auth_timeout_rate by payment provider", "in_stock_rate once the inventory snapshot arrives"],
        trace=steps if steps is not None else trace_orders(),
        model="claude-sonnet (replay)", llm_mode="replay", cost_usd=0.41,
    )


def snapshot_conversion() -> GraphSnapshot:
    nodes = [
        N("conversion_rate", "Conversion rate", 0, "anomalous", 0.0293, 0.0312, kind="kpi", tier="outcome",
          definition="orders / sessions", formula_status="validated", roles=["kpi"], desc="Orders per session"),
        N("orders", "Successful orders", 1, "ok", 948, 945, "count", roles=["ruled_out"],
          desc="Successfully paid orders"),
        N("sessions", "Sessions", 1, "anomalous", 32350, 30300, "count", roles=["on_path"],
          desc="Visits to the marketplace"),
        N("sessions|FR", "Sessions · France", 2, "anomalous", 8510, 6300, "count", kind="segment",
          roles=["on_path", "first_cause"], desc="Sessions from France"),
        N("marketing_cost", "Marketing spend", 2, "anomalous", 2480, 1650, "EUR", domain="marketing",
          polarity="neutral", roles=["on_path"], desc="Paid media spend that bought traffic"),
        N("new_visitor_share", "New-visitor share", 2, "anomalous", 0.41, 0.29, polarity="neutral",
          added_by="graph_handler", definition="new_visitor_sessions / sessions", formula_status="validated",
          roles=["unusual"], desc="Share of sessions from first-time visitors (added by the Graph Handler)"),
        N("product_view_rate", "Product view rate", 1, "ok", 2.38, 2.44, definition="product_views / sessions",
          formula_status="validated", desc="Product page views per session"),
        N("cart_rate", "Cart rate", 1, "ok", 0.081, 0.082, formula_status="validated", roles=["ruled_out"]),
        N("payment_success_rate", "Payment success", 1, "ok", 0.93, 0.934, domain="payments",
          formula_status="validated", roles=["ruled_out"]),
        N("conversion_rate|excl_FR", "Conversion excl. France", 1, "ok", 0.0311, 0.0312, kind="segment",
          roles=["ruled_out"], desc="Conversion without French traffic"),
    ]
    edges = [
        E("orders", "conversion_rate", op="/"), E("sessions", "conversion_rate", op="/", contribution_pct=96.0),
        E("sessions|FR", "sessions", kind="breakdown", op="country", contribution_pct=104.0),
        E("marketing_cost", "sessions|FR", kind="cause", status="learned", sign=1, lag_h=1, strength=0.81,
          label="FR campaign buys traffic", added_by="graph_handler"),
        E("new_visitor_share", "sessions", kind="cause", status="ambiguous", strength=0.55,
          label="new visitors convert less", added_by="graph_handler"),
        E("product_view_rate", "conversion_rate", op="×"), E("cart_rate", "conversion_rate", op="×"),
        E("payment_success_rate", "conversion_rate", op="×"),
        E("conversion_rate|excl_FR", "conversion_rate", kind="breakdown", op="country"),
    ]
    nodes = _enrich(nodes, {
        "conversion_rate": (-0.3, 95, 11), "sessions": (0.4, 94, 11), "sessions|FR": (35.1, 2, 11),
        "marketing_cost": (50.3, 0, 12), "new_visitor_share": (12.0, 70, 11)})
    return GraphSnapshot(snapshot_id=CONV_SNAP, kpi="conversion_rate", center="conversion_rate",
                         ts=iso(NOW - timedelta(hours=9)), window_h=3, depth=2, nodes=nodes, edges=edges,
                         notes=["FR spring campaign live since 2026-11-21 23:00"])


def interp_conversion() -> Interpretation:
    steps = [
        TraceStep(i=1, action="look", node="conversion_rate", finding="Conversion is 6% below baseline (2.93% vs "
                  "3.12%), but orders are on plan.", numbers={"deviation_pct": -6.1, "orders_dev_pct": 0.3},
                  highlight_nodes=["conversion_rate", "orders"], highlight_edges=[("orders", "conversion_rate")]),
        TraceStep(i=2, action="expand", node="conversion_rate", finding="The denominator moved: sessions +6.8% "
                  "while orders are flat.", numbers={"sessions_dev_pct": 6.8},
                  highlight_nodes=["conversion_rate", "sessions"], highlight_edges=[("sessions", "conversion_rate")]),
        TraceStep(i=3, action="breakdown", node="sessions", args={"by": ["country"]},
                  finding="France brings all the extra traffic (+35%); conversion outside France is normal.",
                  numbers={"FR_dev_pct": 35.1, "conv_excl_FR_dev_pct": -0.3},
                  highlight_nodes=["sessions", "sessions|FR", "conversion_rate|excl_FR"],
                  highlight_edges=[("sessions|FR", "sessions")]),
        TraceStep(i=4, action="detail", node="marketing_cost", finding="Paid media spend in FR is up 50% since "
                  "23:00 - the spring campaign - bringing many first-time visitors.",
                  numbers={"marketing_cost_dev_pct": 50.3, "new_visitor_share": 0.41},
                  highlight_nodes=["marketing_cost", "new_visitor_share", "sessions|FR"],
                  highlight_edges=[("marketing_cost", "sessions|FR")]),
        TraceStep(i=5, action="conclude", node="sessions|FR", finding="Innocent: more, lower-intent visitors from "
                  "the FR campaign dilute conversion; no shopper is lost.", numbers={"orders_dev_pct": 0.3},
                  highlight_nodes=["conversion_rate", "sessions", "sessions|FR"],
                  highlight_edges=[("sessions", "conversion_rate"), ("sessions|FR", "sessions")]),
    ]
    return Interpretation(
        interpretation_id=CONV_ID, incident_id=CONV_ID, kpi="conversion_rate", snapshot_id=CONV_SNAP,
        ts=iso(NOW - timedelta(hours=9)),
        headline="Conversion dip is innocent: FR campaign brings extra first-time visitors",
        summary="Conversion is 6% below normal only because traffic grew: the French spring campaign added 35% more "
                "visitors, many of them first-time. Orders are on plan and conversion outside France is normal, "
                "so no action is needed.",
        path=["conversion_rate", "sessions", "sessions|FR"],
        first_cause=Finding(node="sessions|FR", why="French sessions +35% from the campaign, converting below "
                                                     "average.", numbers={"deviation_pct": 35.1}),
        confidence="hypothesis",
        symptoms=[Finding(node="sessions", why="Total traffic +6.8%.", numbers={"deviation_pct": 6.8})],
        unusual=[Finding(node="new_visitor_share", why="New visitors 41% of sessions vs 29%.",
                         numbers={"value": 0.41, "expected": 0.29})],
        ruled_out=[Finding(node="orders", why="Orders on plan.", numbers={"deviation_pct": 0.3}),
                   Finding(node="payment_success_rate", why="Payments normal.", numbers={"value": 0.93}),
                   Finding(node="conversion_rate|excl_FR", why="Conversion outside France normal.",
                           numbers={"deviation_pct": -0.3})],
        uncertainty="We have no campaign calendar in the data; the campaign is inferred from FR paid spend.",
        watch_next=["orders per FR session once the campaign ends"],
        trace=steps, model="claude-sonnet (replay)", llm_mode="replay", cost_usd=0.22,
    )


def snapshot_gmv() -> GraphSnapshot:
    nodes = [
        N("gmv", "Revenue (GMV)", 0, "ok", 48900, 48500, "EUR", kind="kpi", domain="economics",
          definition="orders × aov", formula_status="validated", roles=["kpi"]),
        N("orders", "Successful orders", 1, "ok", 951, 945, "count"),
        N("aov", "Average order value", 1, "ok", 51.4, 51.3, "EUR", domain="economics"),
        N("searches", "Searches", 2, "ok", 21800, 21500, "count"),
        N("search_ctr", "Search CTR", 2, "ok", 0.98, 0.98, formula_status="validated"),
    ]
    edges = [E("orders", "gmv", op="×"), E("aov", "gmv", op="×"),
             E("searches", "orders", kind="cause", status="asserted"),
             E("search_ctr", "orders", kind="cause", status="learned")]
    return GraphSnapshot(snapshot_id=GMV_SNAP, kpi="gmv", center="gmv", ts=iso(NOW - timedelta(hours=20)),
                         nodes=nodes, edges=edges, depth=2)


# ------------------------------------------------------------------ incidents, brief, changes

def build_incidents() -> list[Incident]:
    return [
        Incident(id=ORDERS_ID, kpi="orders", state="open", severity="alert", opened_at=iso(NOW - timedelta(hours=1)),
                 last_seen=iso(NOW), ticks_seen=2,
                 headline="Orders down 14%: card payments failing on Android in Germany since app 3.42",
                 snapshot_id=ORDERS_SNAP, interpretation_id=ORDERS_ID,
                 history=["10:00 orders -9% (watch)", "11:00 orders -14% (alert), investigation started",
                          "11:02 first cause found: payment success DE / Android"]),
        Incident(id=CONV_ID, kpi="conversion_rate", state="persisting", severity="watch",
                 opened_at=iso(NOW - timedelta(hours=11)), last_seen=iso(NOW), ticks_seen=11,
                 headline="Conversion -6%: innocent, FR campaign brings extra first-time visitors",
                 snapshot_id=CONV_SNAP, interpretation_id=CONV_ID,
                 history=["00:00 conversion -5% (watch)", "02:00 explained as FR campaign (hypothesis)",
                          "11:00 still -6%, unchanged explanation"]),
        Incident(id=GMV_ID, kpi="gmv", state="resolved", severity="alert", opened_at=iso(NOW - timedelta(hours=22)),
                 last_seen=iso(NOW - timedelta(hours=20)), resolved_at=iso(NOW - timedelta(hours=19)), ticks_seen=3,
                 headline="Revenue dip during a 2-hour search outage - recovered", snapshot_id=GMV_SNAP,
                 history=["13:00 revenue -11%", "15:00 back within band, resolved"]),
    ]


def build_brief() -> Brief:
    items = [
        BriefItem(kpi="orders", label="Successful orders / hour", severity="alert", incident_id=ORDERS_ID,
                  sentence="Down 14%: Android users in Germany cannot pay since app 3.42 - about EUR 6.9k revenue "
                           "lost per hour. Roll back or hotfix 3.42."),
        BriefItem(kpi="conversion_rate", label="User conversion", severity="watch", incident_id=CONV_ID,
                  sentence="Down 6%, but innocent: the French campaign brings many first-time visitors; orders are "
                           "on plan."),
        BriefItem(kpi="contribution_margin", label="Contribution margin / hour", severity="data_issue",
                  sentence="Not reliable right now: the cost-of-goods feed is only 55% complete."),
        BriefItem(kpi="gmv", label="Revenue / hour", severity="ok",
                  sentence="Slightly below normal (-3%) because of the payment issue, still within the band."),
        BriefItem(kpi="sessions", label="Traffic (sessions / hour)", severity="ok",
                  sentence="Up 4% thanks to the French campaign."),
        BriefItem(kpi="aov", label="Average basket", severity="ok", sentence="Stable at about EUR 51."),
        BriefItem(kpi="in_stock_rate", label="Product availability", severity="ok", sentence="Normal at 95%."),
        BriefItem(kpi="service_error_rate", label="Site error rate", severity="ok",
                  sentence="Normal - the platform is healthy."),
    ]
    return Brief(ts=iso(NOW), headline="One real problem: Android payments in Germany are failing since this "
                                       "morning's app release.",
                 items=items, model="claude-opus (replay)", llm_mode="replay",
                 text="Good morning. One issue needs action: since app release 3.42 went out at 09:00, Android "
                      "users in Germany fail to pay 4 times in 10, which costs about 133 orders and EUR 6.9k revenue "
                      "per hour. Everything else is either healthy or explained: conversion looks lower only because "
                      "the French campaign brings extra visitors, and margin is temporarily unmeasurable because the "
                      "cost feed is incomplete.")


def build_changes() -> list[GraphChange]:
    t = lambda h, m=0: iso(NOW - timedelta(hours=h, minutes=-m))
    return [
        GraphChange(ts=t(30), action="add_edge", edge=E("marketing_cost", "sessions", kind="cause", status="learned",
                    sign=1, lag_h=1, strength=0.81, label="paid media buys traffic", added_by="graph_handler"),
                    reason="Paid spend leads sessions by one hour in every country.",
                    evidence={"corr": 0.81, "lag_h": 1, "countries_consistent": "6/6", "n_hours": 336}),
        GraphChange(ts=t(26), action="reject_edge", edge=E("feed_click_rate", "cart_rate", kind="cause",
                    status="rejected", added_by="graph_handler"),
                    reason="No relation in 14 days of data - proposed edge rejected.",
                    evidence={"corr": 0.04, "p_value": 0.71, "n_hours": 336}),
        GraphChange(ts=t(20), action="mark_ambiguous", edge=E("search_ctr", "product_view_rate", kind="cause",
                    status="ambiguous", sign=1, strength=0.35, added_by="graph_handler"),
                    reason="Positive in DE/FR, negative in ES: sign unstable across countries.",
                    evidence={"corr": 0.35, "sign_flips": 2, "countries": 6}),
        GraphChange(ts=t(9), action="add_node", node=N("new_visitor_share", "New-visitor share", 2, "ok", None, None,
                    polarity="neutral", added_by="graph_handler", definition="new_visitor_sessions / sessions",
                    formula_status="validated"),
                    reason="Explains traffic-mix effects on conversion; built from the audience table.",
                    evidence={"rows": 9120, "identity_error_pct": 0.0, "coverage": 1.0}),
        GraphChange(ts=t(2), action="update_formula", node=N("contribution_margin", "Contribution margin", 2,
                    "unknown", None, None, "EUR", domain="economics", definition="gmv - discounts - cogs - ad_cost",
                    formula_status="incomplete"),
                    reason="cogs feed missing for 2 of 6 categories - formula marked incomplete.",
                    evidence={"cogs_completeness": 0.55, "categories_missing": 2}),
        GraphChange(ts=t(0, 1), action="add_node", node=N("auth_timeout_rate", "Auth timeout rate", 3, "anomalous",
                    0.312, 0.004, domain="payments", polarity="down_is_good", definition="auth_timeouts / auth_attempts",
                    formula_status="unvalidated", added_by="graph_handler"),
                    reason="Parsed timeout codes from messy raw auth logs to split auth failures.",
                    evidence={"rows_parsed": 48210, "parse_rate": 0.97, "identity_error_pct": 0.3}),
        GraphChange(ts=t(0, 1), action="add_edge", edge=E("auth_timeout_rate", "auth_failure_rate", status="learned",
                    op="+", strength=0.98, added_by="graph_handler"),
                    reason="Timeouts are a component of auth failures (sum identity holds).",
                    evidence={"corr": 0.98, "lag_h": 0, "identity_error_pct": 0.3}),
        GraphChange(ts=t(0, 2), action="add_edge", edge=E(RELEASE, PSR_SEG, kind="proposed", status="proposed",
                    sign=-1, lag_h=0, strength=0.94, added_by="interpreter"),
                    reason="Proposed by the interpreter; timing matches, awaiting a second window to validate.",
                    evidence={"corr": 0.94, "lag_h": 0, "windows_seen": 1, "windows_needed": 2}),
    ]


# ------------------------------------------------------------------ writers

def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def _dump(model, path: Path) -> None:
    _write(path, model.model_dump_json(indent=2))


def write_all(out: Path, final_interp: bool = True) -> None:
    for sub in ("snapshots", "interpretations"):
        if (out / sub).exists():
            shutil.rmtree(out / sub)
    out.mkdir(parents=True, exist_ok=True)
    _dump(build_state(), out / "state.json")
    _write(out / "incidents.json", json.dumps([i.model_dump(mode="json") for i in build_incidents()], indent=2))
    for snap in (snapshot_orders(), snapshot_conversion(), snapshot_gmv()):
        _dump(snap, out / "snapshots" / f"{snap.snapshot_id}.json")
    # partial first (older mtime), then the final: the dashboard shows the final one
    _dump(interp_orders(trace_orders()[:4]), out / "interpretations" / f"{ORDERS_ID}.partial.json")
    if final_interp:
        time.sleep(0.05)
        _dump(interp_orders(), out / "interpretations" / f"{ORDERS_ID}.json")
    _dump(interp_conversion(), out / "interpretations" / f"{CONV_ID}.json")
    _dump(build_brief(), out / "brief.json")
    _write(out / "graph_changes.jsonl", "".join(c.model_dump_json() + "\n" for c in build_changes()))
    _write(out / "spend.json", json.dumps({"usd": 1.84}))


def simulate(out: Path, delay: float = 2.0) -> None:
    if out.exists():
        shutil.rmtree(out)
    write_all(out, final_interp=False)
    steps = trace_orders()
    part = out / "interpretations" / f"{ORDERS_ID}.partial.json"
    for k in range(1, len(steps) + 1):
        _dump(interp_orders(steps[:k]), part)
        print(f"partial: {k}/{len(steps)} steps", flush=True)
        time.sleep(delay)
    _dump(interp_orders(), out / "interpretations" / f"{ORDERS_ID}.json")
    part.unlink(missing_ok=True)
    print("final interpretation written", flush=True)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(OUT))
    ap.add_argument("--simulate", metavar="DIR", help="replay a live investigation into DIR")
    ap.add_argument("--delay", type=float, default=2.0)
    a = ap.parse_args()
    if a.simulate:
        simulate(Path(a.simulate), a.delay)
    else:
        write_all(Path(a.out))
        print(f"fixtures written to {a.out}")
