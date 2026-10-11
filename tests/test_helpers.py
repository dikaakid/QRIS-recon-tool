"""Tests for utility functions."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import pandas as pd
import pytest
import qris_recon as qr


def test_settled_batch_set_excludes_mismatch():
    df = pd.DataFrame({
        "STATEMENT_BATCH_REF": ["B1", "B2", "B3", "B4"],
        "DASHBOARD_BATCH_REF": ["B1", "B2", "B3", "B4"],
        "status": ["MATCH", "MISMATCH", "INCOMPLETE_DATA", "CROSSDAY_BOUNDARY_ARTIFACT"],
    })
    settled = qr.settled_batch_set(df)
    assert "B1" in settled
    assert "B2" not in settled   # MISMATCH is excluded
    assert "B3" in settled
    assert "B4" in settled


def test_row_count_guard_raises_on_hard_limit():
    with pytest.raises(RuntimeError, match="EXCEEDS Excel"):
        qr.check_row_count_guard(2_000_000, "Test")


def test_row_count_guard_ok_below_threshold():
    qr.check_row_count_guard(100, "Test")
