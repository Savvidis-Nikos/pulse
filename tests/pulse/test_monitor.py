"""Monitor severity logic, onset, incident lifecycle (pure), plus a few checks on the real simulator data."""
import os

import pandas as pd
import pytest

os.environ["PULSE_LLM"] = "mock"

from pulse import contracts as C  # noqa: E402
from pulse.contracts import KpiStatus  # noqa: E402
from pulse.incidents import IncidentBook  # noqa: E402
from pulse.monitor import bad_direction_z, classify, find_onset, load_kpis  # noqa: E402


# ---------------------------------------------------------------- severity / polarity

def test_only_unwanted_direction_alerts():
    assert classify(-5.0, -10.0, "up_is_good") == ("alert", "down")
    assert classify(+5.0, +10.0, "up_is_good") == ("ok", "up")          # good news never alerts
    assert classify(+5.0, +10.0, "down_is_good") == ("alert", "up")
    assert classify(-5.0, -10.0, "down_is_good") == ("ok", "down")


def test_mild_is_watch_and_tiny_is_ok():
    assert classify(-3.0, -6.0, "up_is_good")[0] == "watch"
    assert classify(-0.5, -1.0, "up_is_good") == ("ok", "flat")
    # significant but economically tiny move stays at watch
    assert classify(-6.0, -1.0, "up_is_good", min_dev_pct=3.0)[0] == "watch"


def test_neutral_never_alerts_and_missing_is_ok():
    assert classify(-8.0, -20.0, "neutral")[0] == "watch"
    assert classify(None, None, "up_is_good") == ("ok", "flat")
    assert classify(float("nan"), 5.0, "up_is_good") == ("ok", "flat")
    assert bad_direction_z(-4.0, "up_is_good") == 4.0
    assert bad_direction_z(-4.0, "down_is_good") == -4.0


def test_onset_finds_start_of_current_run():
    idx = pd.date_range("2026-11-17 08:00", periods=10, freq="h")
    s = pd.Series([0, 0, 3, 0, 0, 3.1, 4, 1.5, 5, 6], index=idx)  # one-hour gap at 15:00 tolerated
    assert find_onset(s, idx[-1], 2.5) == pd.Timestamp("2026-11-17 13:00")
    assert find_onset(s.iloc[:5], idx[4], 2.5) is None


def test_kpis_yaml_names_exist():
    from argus.semantic.registry import Registry
    kp = load_kpis(reg=Registry.load())
    assert 6 <= len(kp) <= 10
    assert {"gmv", "orders", "conversion_rate", "payment_success_rate"} <= {k.name for k in kp}


# ---------------------------------------------------------------- incident lifecycle

@pytest.fixture
def state_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(C, "STATE_DIR", tmp_path)
    return tmp_path


def _st(sev, dev=-8.0, z=-5.0, direction="down", name="payment_success_rate"):
    return KpiStatus(name=name, label="Payment success", severity=sev, deviation_pct=dev, z=z, direction=direction)


def test_incident_open_persist_resolve_no_duplicates(state_dir):
    t = pd.Timestamp("2026-11-17 15:00")
    b = IncidentBook(resolve_after=3)
    ev = b.update([_st("alert")], t, {"payment_success_rate": "2026-11-17 14:00"})
    assert [e.kind for e in ev] == ["opened"]
    inc_id = ev[0].incident.id
    assert inc_id == "INC-payment_success_rate-2026111714"
    b.save()
    for h in range(1, 4):                                    # reload each tick: memory survives processes
        b = IncidentBook.load(resolve_after=3)
        ev = b.update([_st("alert", dev=-9.0)], t + pd.Timedelta(hours=h))
        assert [e.kind for e in ev] == ["persisting"]
        b.save()
    assert len(b.incidents) == 1 and b.incidents[0].ticks_seen == 4 and b.incidents[0].state == "persisting"
    kinds = [b.update([_st("ok", dev=-1, z=-0.5, direction="flat")], t + pd.Timedelta(hours=4 + h))[0].kind
             for h in range(3)]
    assert kinds == ["recovering", "recovering", "resolved"]
    assert b.incidents[0].state == "resolved" and b.incidents[0].resolved_at
    # recurrence later = a new incident, not a reopened one
    ev = b.update([_st("alert")], t + pd.Timedelta(hours=20), {"payment_success_rate": "2026-11-18 11:00"})
    assert ev[0].kind == "opened" and ev[0].incident.id != inc_id and len(b.active()) == 1


def test_escalation_watch_flicker_and_data_hold(state_dir):
    t = pd.Timestamp("2026-11-17 15:00")
    b = IncidentBook(resolve_after=3)
    b.update([_st("alert", dev=-6.0)], t)
    assert b.update([_st("watch", dev=-3.0, z=-3.0)], t + pd.Timedelta(hours=1))[0].kind == "recovering"
    assert b.update([_st("alert", dev=-7.0)], t + pd.Timedelta(hours=2))[0].kind == "persisting"   # streak reset
    assert b.update([_st("alert", dev=-12.0)], t + pd.Timedelta(hours=3))[0].kind == "escalated"
    assert b.update([_st("alert", dev=-12.5)], t + pd.Timedelta(hours=4))[0].kind == "persisting"  # once only
    st = _st("data_issue", dev=None, z=None, direction="flat")
    assert b.update([st], t + pd.Timedelta(hours=5))[0].kind == "held"
    assert st.incident_id == b.incidents[0].id and len(b.incidents) == 1
    # opposite direction is a separate incident; the first one counts the rebound toward its recovery
    ev = b.update([_st("alert", dev=+9, z=+5, direction="up")], t + pd.Timedelta(hours=6))
    assert [e.kind for e in ev] == ["recovering", "opened"] and len(b.incidents) == 2


# ---------------------------------------------------------------- real data (simulator ground truth)

@pytest.fixture(scope="module")
def w():
    from pulse.world import world
    return world()


def _by(w, ts):
    from pulse.monitor import evaluate_ex
    return {e.name: e for e in evaluate_ex(w, load_kpis(reg=w.reg), ts)}


def test_fault_alerts_on_the_right_kpi(w):
    ev = _by(w, "2026-11-17 16:00")                       # payment_failure (DE android) since 14:00
    p = ev["payment_success_rate"]
    assert p.status.severity == "alert" and p.status.direction == "down"
    assert p.onset == "2026-11-17 14:00"
    assert len(p.status.series) == 48 and p.status.series[-1].value == p.status.value
    assert [k for k, e in ev.items() if e.status.severity == "alert"] == ["payment_success_rate"]


def test_innocent_campaign_is_downgraded(w):
    ev = _by(w, "2026-11-18 19:00")                       # planned FR Black Week teaser
    c = ev["cart_rate"]                                    # z ~ -5: would alert without the planned event
    assert c.raw_severity == "alert" and c.status.severity == "watch"
    assert c.expected_by == "FR Black Week teaser campaign"
    assert ev["conversion_rate"].expected_by == "FR Black Week teaser campaign"
    assert ev["conversion_rate"].status.severity == "watch"
    assert not [k for k, e in ev.items() if e.status.severity == "alert"]


def test_late_data_is_a_data_issue_not_an_alert(w):
    ev = _by(w, "2026-11-21 23:00")                       # orders_db 38 % complete
    for k in ("gmv", "orders", "conversion_rate"):
        assert ev[k].status.severity == "data_issue", k
        assert ev[k].status.completeness is not None and ev[k].status.completeness < 0.9


def test_event_still_in_the_window_explains_it(w):
    ev = _by(w, "2026-11-14 23:00")["sessions"]           # match ended 23:00, the 3 h window still holds it
    assert ev.raw_severity == "alert" and ev.status.severity == "watch"
    assert ev.expected_by and ev.expected_by.startswith("Football")


def test_announced_maintenance_upstream_of_the_kpi_is_expected(w):
    ev = _by(w, "2026-11-08 04:00")["gmv"]                # night search rebuild moves search_ctr -> GMV
    assert ev.raw_severity == "alert" and ev.status.severity == "watch"
    assert "upstream of gmv" in ev.status.note


def test_segment_event_does_not_mask_a_fault_elsewhere(w):
    ev = _by(w, "2026-11-19 10:00")["conversion_rate"]    # FR campaign running, DE search ranker broken
    assert ev.status.severity == "alert" and ev.expected_by is None
    assert "DE is also" in ev.status.note


def test_slow_drift_escalates_to_alert(w):
    ev = _by(w, "2026-11-20 19:00")["in_stock_rate"]      # electronics stockout: z ~3.9 for hours (< alert_z 4)
    assert ev.status.severity == "alert" and "sustained" in ev.status.note
