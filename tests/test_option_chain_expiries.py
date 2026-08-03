"""Expiry selection for the option-chain archive.

The rule is not "the next two expiries". It has to behave sensibly on two
indices with different expiry structures, and getting it wrong is expensive in
opposite directions: too narrow and the archive has no long-dated coverage at
all for the weekly index; too wide and it silently collects a 60-DTE contract
nobody trades, at real storage cost, for months before anyone looks.
"""

from __future__ import annotations

from datetime import date

from app.option_chain import select_expiries


def test_nifty_style_weeklies_gain_the_monthly():
    """Nearest two Nifty weeklies are both mid-month, so neither is the monthly.

    Without the extra pick, the archive would hold nothing past ~11 DTE on
    Nifty -- which is exactly the coverage gap that made every long-dated Nifty
    coefficient extrapolated in the first place.
    """
    expiries = {date(2026, 8, 6), date(2026, 8, 13), date(2026, 8, 20), date(2026, 8, 27), date(2026, 9, 24)}
    chosen = select_expiries(expiries, today=date(2026, 8, 3), count=2)
    assert chosen == [date(2026, 8, 6), date(2026, 8, 13), date(2026, 8, 27)]


def test_banknifty_style_monthlies_are_not_padded():
    """Bank Nifty has no weeklies, so its nearest two ARE monthlies.

    Adding "the next monthly" here would append a third contract at ~60 DTE
    that no strategy touches, inflating storage by half for nothing.
    """
    expiries = {date(2026, 8, 25), date(2026, 9, 29), date(2026, 10, 27), date(2026, 12, 29)}
    chosen = select_expiries(expiries, today=date(2026, 8, 3), count=2)
    assert chosen == [date(2026, 8, 25), date(2026, 9, 29)]


def test_monthly_already_in_the_nearest_two_is_not_duplicated():
    """Late in the month the nearest weekly IS the monthly."""
    expiries = {date(2026, 8, 27), date(2026, 9, 3), date(2026, 9, 24)}
    chosen = select_expiries(expiries, today=date(2026, 8, 25), count=2)
    assert chosen == [date(2026, 8, 27), date(2026, 9, 3)]


def test_expired_contracts_are_excluded_but_today_is_kept():
    """Expiry day itself is a trading day and its chain is worth recording --
    it is the only place 0-DTE behaviour will ever be observable."""
    expiries = {date(2026, 7, 30), date(2026, 8, 3), date(2026, 8, 10)}
    chosen = select_expiries(expiries, today=date(2026, 8, 3), count=2)
    assert date(2026, 7, 30) not in chosen
    assert chosen[0] == date(2026, 8, 3)


def test_no_future_expiries_returns_empty_rather_than_raising():
    """A stale instrument cache produces exactly this. The collector must skip
    the cycle, not crash the scheduler job."""
    assert select_expiries({date(2026, 1, 1)}, today=date(2026, 8, 3), count=2) == []
    assert select_expiries(set(), today=date(2026, 8, 3), count=2) == []
