import textwrap

import pytest

from tradebot.brokers import BrokerScheduleError, load_brokers
from tradebot.execution.charges import round_trip_charges


def test_the_three_shipped_schedules_load():
    brokers = load_brokers("brokers.yaml")
    assert set(brokers) == {"groww", "zerodha", "legacy"}


def test_groww_round_trip_matches_the_pricing_page():
    """Worked by hand from groww.in/pricing, verified 2026-09-22, on a 12,500 position:
    brokerage min(12.50, 20) x 2 = 25.00; STT 25,000 x 0.1% = 25.00; txn 25,000 x 0.00297% =
    0.7425; SEBI 25,000 x 0.0001% = 0.025; stamp 12,500 x 0.015% = 1.875;
    GST 18% x (25 + 0.7425 + 0.025) = 4.63815; DP 23.60. Total 80.88065."""
    cfg = load_brokers("brokers.yaml")["groww"].charges
    assert round_trip_charges(12_500.0, 12_500.0, cfg, product="CNC") == pytest.approx(80.88)


def test_zerodha_round_trip_matches_the_pricing_page():
    """Same position at zerodha.com/charges, verified 2026-09-22: delivery brokerage is zero, so
    GST falls to 18% x (0.7425 + 0.025) = 0.13815, and the DP fee is 15.34. Total 43.12065."""
    cfg = load_brokers("brokers.yaml")["zerodha"].charges
    assert round_trip_charges(12_500.0, 12_500.0, cfg, product="CNC") == pytest.approx(43.12)


def test_legacy_is_the_schedule_the_committed_notes_were_computed_from():
    """Groww's brokerage with Zerodha's DP fee: 80.88065 - 23.60 + 15.34 = 72.62065. This
    corresponds to no real broker and exists only to reproduce figures already published."""
    cfg = load_brokers("brokers.yaml")["legacy"].charges
    assert round_trip_charges(12_500.0, 12_500.0, cfg, product="CNC") == pytest.approx(72.62)


def test_legacy_matches_the_dataclass_defaults_exactly():
    """If these ever diverge, every note in docs/superpowers/notes/ silently stops reproducing."""
    from tradebot.config import ChargesConfig
    legacy = load_brokers("brokers.yaml")["legacy"].charges
    default = ChargesConfig()
    for field in ("brokerage_pct", "brokerage_max", "brokerage_min", "stt_sell_pct",
                  "exchange_txn_pct", "sebi_pct", "stamp_buy_pct", "delivery_stt_pct",
                  "delivery_stamp_buy_pct", "dp_charge", "gst_pct"):
        assert getattr(legacy, field) == getattr(default, field), field


def test_intraday_charges_are_identical_across_the_three_schedules_except_brokerage():
    """Only brokerage and the DP fee differ by broker, and DP is delivery-only. An intraday round
    trip at groww and at legacy must therefore be the same number."""
    brokers = load_brokers("brokers.yaml")
    groww = round_trip_charges(12_500.0, 12_500.0, brokers["groww"].charges)
    legacy = round_trip_charges(12_500.0, 12_500.0, brokers["legacy"].charges)
    assert groww == pytest.approx(legacy)


def test_provenance_is_mandatory(tmp_path):
    """A rate whose source nobody recorded is the defect this whole file exists to fix."""
    p = tmp_path / "b.yaml"
    p.write_text(textwrap.dedent("""
        acme:
          brokerage_pct: 0.1
    """))
    with pytest.raises(BrokerScheduleError) as e:
        load_brokers(p)
    assert "verified_on" in str(e.value) and "acme" in str(e.value)


def test_an_unknown_rate_key_is_an_error(tmp_path):
    """A typo must not silently leave a rate at its default."""
    p = tmp_path / "b.yaml"
    p.write_text(textwrap.dedent("""
        acme:
          verified_on: 2026-09-22
          source: https://example.com
          brokerage_percent: 0.1
    """))
    with pytest.raises(BrokerScheduleError) as e:
        load_brokers(p)
    assert "brokerage_percent" in str(e.value)


def test_a_missing_file_names_the_path(tmp_path):
    with pytest.raises(BrokerScheduleError) as e:
        load_brokers(tmp_path / "nope.yaml")
    assert "nope.yaml" in str(e.value)


def test_verified_on_is_exposed_so_a_stale_rate_can_be_found():
    from datetime import date
    assert load_brokers("brokers.yaml")["groww"].verified_on == date(2026, 9, 22)
    assert "groww.in" in load_brokers("brokers.yaml")["groww"].source


def test_a_non_numeric_rate_value_names_the_schedule_and_key(tmp_path):
    """A bare ValueError from float() would name neither the file, the schedule, nor the key --
    every other error path in this loader does all three, so this one must too."""
    p = tmp_path / "b.yaml"
    p.write_text(textwrap.dedent("""
        acme:
          verified_on: 2026-09-22
          source: https://example.com
          brokerage_pct: twenty
          brokerage_max: 20.0
          brokerage_min: 5.0
          stt_sell_pct: 0.025
          exchange_txn_pct: 0.00297
          sebi_pct: 0.0001
          stamp_buy_pct: 0.003
          delivery_stt_pct: 0.1
          delivery_stamp_buy_pct: 0.015
          dp_charge: 15.34
          gst_pct: 18.0
    """))
    with pytest.raises(BrokerScheduleError) as e:
        load_brokers(p)
    assert "acme" in str(e.value) and "brokerage_pct" in str(e.value)


def test_a_schedule_missing_one_rate_key_names_it(tmp_path):
    """A schedule with provenance but a missing rate would silently inherit ChargesConfig's
    default for that rate -- the Groww-brokerage/Zerodha-DP chimera this file exists to
    eliminate -- while still claiming verified_on/source cover the whole schedule."""
    p = tmp_path / "b.yaml"
    p.write_text(textwrap.dedent("""
        acme:
          verified_on: 2026-09-22
          source: https://example.com
          brokerage_pct: 0.1
          brokerage_max: 20.0
          brokerage_min: 5.0
          stt_sell_pct: 0.025
          exchange_txn_pct: 0.00297
          sebi_pct: 0.0001
          stamp_buy_pct: 0.003
          delivery_stt_pct: 0.1
          delivery_stamp_buy_pct: 0.015
          gst_pct: 18.0
    """))
    with pytest.raises(BrokerScheduleError) as e:
        load_brokers(p)
    assert "acme" in str(e.value) and "dp_charge" in str(e.value)


def test_a_schedule_missing_several_rate_keys_names_all_of_them(tmp_path):
    """The missing-key error must list every absent rate, not just the first, so a partial
    schedule can be completed in one pass instead of failing once per key."""
    p = tmp_path / "b.yaml"
    p.write_text(textwrap.dedent("""
        acme:
          verified_on: 2026-09-22
          source: https://example.com
          brokerage_pct: 0.1
          brokerage_max: 20.0
          brokerage_min: 5.0
    """))
    with pytest.raises(BrokerScheduleError) as e:
        load_brokers(p)
    msg = str(e.value)
    for missing in ("stt_sell_pct", "exchange_txn_pct", "sebi_pct", "stamp_buy_pct",
                    "delivery_stt_pct", "delivery_stamp_buy_pct", "dp_charge", "gst_pct"):
        assert missing in msg, missing
