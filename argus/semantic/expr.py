"""Symbolic layer (SymPy): parse metric expressions, expand them to base metrics, classify formulas,
compare definitions symbolically and evaluate them numerically on aligned frames."""
from __future__ import annotations

import re

import numpy as np
import sympy as sp

from argus.semantic.registry import MetricSpec, Registry

FUNCS = {"sqrt": sp.sqrt, "log": sp.log, "exp": sp.exp, "Abs": sp.Abs}
TOKEN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def sym(name: str) -> sp.Symbol:
    return sp.Symbol(name, positive=True)


def parse(expr: str, known: set[str] | None = None) -> sp.Expr:
    """Parse an arithmetic expression over metric names. Unknown names raise ValueError when `known` is given."""
    names = set(TOKEN.findall(expr)) - set(FUNCS)
    if known is not None:
        unknown = sorted(n for n in names if n not in known)
        if unknown:
            raise ValueError(f"unknown metric(s) in expression: {unknown}")
    local = {n: sym(n) for n in names} | FUNCS
    try:
        return sp.sympify(expr, locals=local, rational=True)
    except (sp.SympifyError, SyntaxError, TypeError) as exc:
        raise ValueError(f"cannot parse expression {expr!r}: {exc}") from None


def definition(spec: MetricSpec, known: set[str] | None = None) -> sp.Expr:
    """One-level definition of a metric in terms of other metrics (base metrics define themselves)."""
    if spec.type == "ratio":
        return sym(spec.numerator) / sym(spec.denominator)
    if spec.type == "derived":
        return parse(spec.expr, known)
    return sym(spec.name)


def expand(name: str, reg: Registry) -> sp.Expr:
    """Fully expand a metric down to base metrics (simple and sql types). Cached on the registry."""
    cache: dict = reg.__dict__.setdefault("_expand_cache", {})
    if name in cache:
        return cache[name]
    spec = reg[name]
    if spec.type in ("simple", "sql"):
        out = sym(name)
    else:
        e = definition(spec, set(reg.names()))
        out = e.xreplace({s: expand(str(s), reg) for s in e.free_symbols})
    cache[name] = out
    return out


def fraction_parts(e: sp.Expr) -> tuple[sp.Expr, sp.Expr]:
    num, den = sp.fraction(sp.together(e))
    return sp.expand(num), sp.expand(den)


def is_linear(e: sp.Expr) -> bool:
    if not e.free_symbols:
        return True
    try:
        poly = sp.Poly(e, *sorted(e.free_symbols, key=str))
    except sp.PolynomialError:
        return False
    return poly.total_degree() <= 1


def classify(e: sp.Expr) -> str:
    """sum (linear combination), ratio (one fraction), product (monomial), other."""
    if is_linear(e):
        return "sum"
    num, den = fraction_parts(e)
    if den != 1 and is_linear(num) and is_linear(den):
        return "ratio"
    if isinstance(e, sp.Mul) or isinstance(e, sp.Pow):
        return "product"
    return "other"


def equivalent(a: sp.Expr, b: sp.Expr) -> bool:
    """Symbolic identity check (same function of the same base metrics)."""
    try:
        if sp.simplify(a - b) == 0:
            return True
        if b != 0 and sp.simplify(a / b) == 1:
            return True
    except Exception:
        return False
    return False


def sign_of(e: sp.Expr, s: sp.Symbol) -> int:
    """Sign of d e / d s assuming positive inputs (+1, -1 or 0 when mixed/unknown)."""
    d = sp.diff(e, s)
    if d.is_positive:
        return 1
    if d.is_negative:
        return -1
    num, den = fraction_parts(d)
    if num.is_positive and den.is_positive:
        return 1
    if num.is_negative and den.is_positive:
        return -1
    return 0


def linear_terms(e: sp.Expr) -> dict[str, float]:
    """Coefficients of a linear expression {metric: coef} (constant under key '1')."""
    out: dict[str, float] = {}
    for term in sp.Add.make_args(sp.expand(e)):
        coef, rest = term.as_coeff_Mul()
        key = "1" if rest == 1 else str(rest)
        out[key] = out.get(key, 0.0) + float(coef)
    return out


def evaluate(e: sp.Expr, columns: dict[str, np.ndarray]) -> np.ndarray:
    """Evaluate on aligned numpy arrays; division by zero -> NaN."""
    symbols = sorted(e.free_symbols, key=str)
    if not symbols:
        n = len(next(iter(columns.values()))) if columns else 1
        return np.full(n, float(e))
    f = sp.lambdify(symbols, e, modules="numpy")
    with np.errstate(divide="ignore", invalid="ignore"):
        out = np.array(f(*[np.asarray(columns[str(s)], dtype=float) for s in symbols]), dtype=float, copy=True)
    out[~np.isfinite(out)] = np.nan
    return out


def pretty(e: sp.Expr) -> str:
    return str(e).replace("**", "^")
