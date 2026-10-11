"""Tests for statement row classification."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import pandas as pd
import qris_recon as qr


def _df(rows):
    cols = ["NO REFERENCE", "TRANSACTION CODE", "AMOUNT LCY",
            "TRX DESC", "NARRATIVE", "DATE TIME"]
    return pd.DataFrame(rows, columns=cols)


def test_qris_batch_classified_correctly():
    df = _df([
        ("BA-2401010001-ACQ", "903", 100_000.0, "QRIS SETTLEMENT", "", "01/01/2024 10:00"),
        ("BQ-2401010002-ACQ", "904", 1_000.0, "QRIS FEE", "", "01/01/2024 10:00"),
    ])
    out = qr.classify_statement_rows(df)
    assert len(out["qris_batch"]) == 2
    assert len(out["qris_individual"]) == 0


def test_qris_individual_classified_correctly():
    df = _df([
        ("QR1234567", "903", 50_000.0, "ATM TRANSFER", "", "01/01/2024 11:00"),
    ])
    out = qr.classify_statement_rows(df)
    assert len(out["qris_batch"]) == 0
    assert len(out["qris_individual"]) == 1


def test_non_recon_keyword_matches():
    df = _df([
        ("SOMEREF", "000", 0.0, "INTERNAL TRANSFER", "", "01/01/2024 12:00"),
    ])
    out = qr.classify_statement_rows(df)
    assert len(out["non_recon"]) == 1


def test_unclassified_when_nothing_matches():
    df = _df([
        ("UNKNOWN", "999", 100.0, "SOMETHING ELSE", "", "01/01/2024 13:00"),
    ])
    out = qr.classify_statement_rows(df)
    assert len(out["unclassified"]) == 1
