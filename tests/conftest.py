"""Shared fixtures for QRIS recon tests."""
import pandas as pd
import pytest


@pytest.fixture
def statement_row():
    def _make(no_ref="BA-2401010001-ACQ", code="903", amount=100_000.0,
              desc="QRIS SETTLEMENT", narrative="", dt="01/01/2024 10:00"):
        return {
            "NO REFERENCE": no_ref,
            "TRANSACTION CODE": code,
            "AMOUNT LCY": amount,
            "TRX DESC": desc,
            "NARRATIVE": narrative,
            "DATE TIME": dt,
        }
    return _make


@pytest.fixture
def dashboard_row():
    def _make(no_ref="BA-2401010001-ACQ", batch_ref="BA-2401010001-ACQ",
              amount=100_000.0, mdr=1_000.0, status="SUCCEED"):
        return {
            "NO REFERENCE": no_ref,
            "BATCH REFERENCE": batch_ref,
            "AMOUNT": amount,
            "MDR AMOUNT": mdr,
            "STATUS TRANSACTION": status,
        }
    return _make
