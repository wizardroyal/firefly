from datetime import datetime, timedelta, timezone
from decimal import Decimal as D

import pytest

from entropymm.config import RiskCfg
from entropymm.ledger import Ledger, LedgerError
from entropymm.risk import Mode, RiskManager

T = datetime(2026, 11, 4, 12, tzinfo=timezone.utc)


def rm(tmp_path, **kw):
    return RiskManager(RiskCfg(**kw), str(tmp_path), "t")


def test_breach_needs_two_readings(tmp_path):
    r = rm(tmp_path)
    r.update(0.0, T)
    v = r.update(-2.5, T)
    assert v.mode == Mode.FLATTEN_ONLY and "unconfirmed" in v.reason
    assert r.update(0.0, T).mode == Mode.NORMAL          # one bad snapshot: no halt
    r.update(-2.5, T)
    assert r.update(-2.5, T).mode == Mode.HALT


def test_halt_survives_restart_and_budget_not_reset(tmp_path):
    r = rm(tmp_path)
    r.update(0.0, T)
    r.update(-0.9, T)
    r.update(-0.9, T)                                     # daily 0.75 breached twice
    assert rm(tmp_path).update(-0.9, T).mode == Mode.HALT
    assert rm(tmp_path).update(0.0, T + timedelta(days=1)).mode == Mode.NORMAL   # daily halt ends next day
    r2 = rm(tmp_path)
    assert r2.update(-1.7, T + timedelta(days=1)).mode == Mode.FLATTEN_ONLY       # week loss carried: 1.7 >= 1.6


def test_corrupt_state_halts_forever(tmp_path):
    (tmp_path / "risk_t.json").write_text("{not json")
    v = rm(tmp_path).update(0.0, T)
    assert v.mode == Mode.HALT and "unreadable" in v.reason
    assert any(p.name.startswith("risk_t.json.corrupt-") for p in tmp_path.iterdir())


def test_set_basis_carries_loss(tmp_path):
    r = rm(tmp_path)
    r.set_basis("ledger", 0.0)
    r.update(0.0, T)
    r.update(-1.0, T)
    r.set_basis("other", 50.0)
    assert r.update(50.0, T).week_loss == pytest.approx(1.0)


def test_market_cap_two_readings(tmp_path):
    r = rm(tmp_path)
    assert r.update_market("io:SNDK", 0.0, 1.0, T).mode == Mode.NORMAL
    assert r.update_market("io:SNDK", -1.1, 1.0, T).mode == Mode.FLATTEN_ONLY
    assert r.update_market("io:SNDK", -1.1, 1.0, T).mode == Mode.HALT
    assert rm(tmp_path).update_market("io:SNDK", 0.0, 1.0, T).mode == Mode.HALT


def test_ledger_fills_fees_funding_dedupe(tmp_path):
    lg = Ledger(str(tmp_path), "t")
    lg.init_market("io:SNDK", D(0), D("800"), start_ms=1000)
    assert lg.apply_fill("io:SNDK", True, D("800"), D("0.015"), D("0.00036"), "1", 2000)
    assert not lg.apply_fill("io:SNDK", True, D("800"), D("0.015"), D("0.00036"), "1", 2000)   # dupe
    assert not lg.apply_fill("io:SNDK", True, D("800"), D("0.015"), D("0"), "0", 500)          # before start
    assert lg.apply_fill("io:SNDK", False, D("801"), D("0.015"), D("0.00036"), "2", 3000)
    assert lg.apply_funding("io:SNDK", -0.01, 3600_000)
    assert not lg.apply_funding("io:SNDK", -0.01, 3600_000)
    pnl = lg.pnl({"io:SNDK": (D(0), D("801"))})
    assert pnl == pytest.approx(0.015 - 0.00072 - 0.01)
    t = lg.totals("io:SNDK")
    assert t["fees"] == pytest.approx(0.00072) and t["funding"] == pytest.approx(-0.01) and t["fills"] == 2
    lg.save()
    lg2 = Ledger(str(tmp_path), "t")
    assert not lg2.apply_fill("io:SNDK", True, D("800"), D("0.015"), D("0"), "1", 2000)          # seen persisted
    assert lg2.pnl({"io:SNDK": (D(0), D("801"))}) == pytest.approx(pnl)


def test_ledger_values_open_position_and_needs_mark(tmp_path):
    lg = Ledger(str(tmp_path), "t")
    lg.init_market("io:SNDK", D("0.03"), D("800"), start_ms=0)       # starts at zero pnl
    assert lg.pnl({"io:SNDK": (D("0.03"), D("800"))}) == pytest.approx(0)
    assert lg.pnl({"io:SNDK": (D("0.03"), D("0"))}) is None


def test_ledger_corrupt_refuses(tmp_path):
    (tmp_path / "ledger_t.json").write_text("[]")
    with pytest.raises(LedgerError):
        Ledger(str(tmp_path), "t")
