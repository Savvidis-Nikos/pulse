"""Static setup of the fake Amazon-like marketplace: markets, devices, categories and the product catalog.

Everything here is public-knowledge shaped (how a large marketplace works), not taken from any real company.
"""
from __future__ import annotations

import numpy as np

COUNTRIES = {"DE": 9000, "FR": 6000, "NL": 2500, "PL": 3000}          # avg sessions / hour
PLATFORMS = {"android": 0.45, "ios": 0.30, "web": 0.25}               # share of sessions
CATEGORIES = {"electronics": 0.30, "fashion": 0.30, "home": 0.20, "grocery": 0.20}

# product detail page -> add to cart, for an in-stock product
CART_RATE = {"electronics": 0.08, "fashion": 0.12, "home": 0.10, "grocery": 0.20}
AOV = {"electronics": 180.0, "fashion": 60.0, "home": 85.0, "grocery": 40.0}
COGS = {"electronics": 0.70, "fashion": 0.55, "home": 0.60, "grocery": 0.72}   # cost of goods, share of list price
LISTED = {"electronics": 4000, "fashion": 9000, "home": 5000, "grocery": 3000}

# payments: card / wallet work everywhere; DE customers also use a local method (bank transfer style)
PAY_OK = {"android": 0.930, "ios": 0.950, "web": 0.955}
LOCAL_METHOD_SHARE = {"DE": 0.45, "FR": 0.0, "NL": 0.0, "PL": 0.0}

WEEKLY = np.array([1.00, 0.98, 0.99, 1.01, 1.05, 1.10, 1.12])  # Mon..Sun


def hourly_shape() -> np.ndarray:
    """Shopping traffic over the day: lunch bump, big evening peak, small commute bump."""
    h = np.arange(24)
    s = (0.12 + 0.35 * np.exp(-((h - 12.5) / 2.8) ** 2) + 0.75 * np.exp(-((h - 20.5) / 2.6) ** 2)
         + 0.10 * np.exp(-((h - 8) / 1.5) ** 2))
    return s / s.mean()


class Catalog:
    """SKUs per category with a long-tail popularity (Zipf). Popular products get most of the views,
    so when they go out of stock, sales suffer far more than the share of out-of-stock SKUs suggests."""

    def __init__(self, rng: np.random.Generator):
        self.pop: dict[str, np.ndarray] = {}
        self.stock_days: dict[str, np.ndarray] = {}      # days of cover per SKU
        for cat, n in LISTED.items():
            ranks = np.arange(1, n + 1)
            p = 1.0 / ranks ** 0.9
            self.pop[cat] = p / p.sum()
            # most SKUs comfortably stocked; ~4.5% out at any time (normal churn)
            cover = rng.gamma(4.0, 3.0, n)
            cover[rng.random(n) < 0.045] = 0.0
            self.stock_days[cat] = cover
