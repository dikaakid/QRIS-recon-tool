"""Tests for file discovery and regex patterns."""
from datetime import datetime
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import qris_recon as qr


def test_statement_regex_accepts_space_and_underscore():
    assert qr.FNAME_STATEMENT.search("BankStatement 17Sep2026.csv")
    assert qr.FNAME_STATEMENT.search("BankStatement_17Sep2026.csv")


def test_statement_regex_rejects_wrong_format():
    assert not qr.FNAME_STATEMENT.search("BankStatement-17Sep2026.csv")
    assert not qr.FNAME_STATEMENT.search("SomeOtherFile 17Sep2026.csv")


def test_dashboard_regex_accepts_numbered_parts():
    m1 = qr.FNAME_DASHBOARD.search("TransactionDashboard 17Sep2026.csv")
    m2 = qr.FNAME_DASHBOARD.search("TransactionDashboard 17Sep2026_1.csv")
    m3 = qr.FNAME_DASHBOARD.search("TransactionDashboard 17Sep2026_42.csv")
    assert m1 and m1.group(2) is None
    assert m2 and m2.group(2) == "1"
    assert m3 and m3.group(2) == "42"


def test_ledger_regex_is_word_order_agnostic():
    # Any of these orderings should match
    assert qr.FNAME_GDS.search("GDS_ACQUIRER_QRIS_2024-01-01.csv")
    assert qr.FNAME_GDS.search("GDS_QRIS_ACQUIRER_2024-01-01.csv")
    assert qr.FNAME_GDS.search("GDS_ACQUIRER_2024-01-01.csv")
    # Even with no intermediate token at all
    assert qr.FNAME_GDS.search("GDS_2024-01-01.csv")


def test_ledger_regex_rejects_wrong_format():
    # Date must be in YYYY-MM-DD (with dashes)
    assert not qr.FNAME_GDS.search("GDS_ACQUIRER_20240101.csv")
    assert not qr.FNAME_GDS.search("GDS_ACQUIRER_2024_01_01.csv")
    # No date at all
    assert not qr.FNAME_GDS.search("GDS_ACQUIRER.csv")


def test_mutasi_dash_token_format():
    d = datetime(2026, 9, 17)
    assert qr.mutasi_dash_token(d) == "17Sep2026"


def test_gds_token_format():
    d = datetime(2026, 9, 17)
    assert qr.gds_token(d) == "2026-09-17"


def test_embedded_batch_date_parses_yymmdd():
    ref = "BA-2609170001-ACQUIRER"
    dt = qr._embedded_batch_date(ref)
    assert dt is not None
    assert dt.year == 2026
    assert dt.month == 9
    assert dt.day == 17


def test_embedded_batch_date_returns_none_for_invalid():
    assert qr._embedded_batch_date("no-date-here") is None
