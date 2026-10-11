"""End-to-end tests for qris_recon.py on synthetic data.

The sample files come from tools/make_sample_data.py, which injects every reconciliation
scenario on purpose (short/over-settled batches, boundary batches, double settlement, ...).
Each test asserts that the tool classifies one of those scenarios correctly.
"""
import sys
from datetime import date, datetime
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

import make_sample_data  # noqa: E402
import qris_recon  # noqa: E402

DAY = date(2026, 9, 17)


@pytest.fixture(scope="module")
def sample(tmp_path_factory):
    """Build the sample files once, point the module at them, and run the whole pipeline."""
    root = tmp_path_factory.mktemp("recon")
    meta = make_sample_data.build(
        root / "data", DAY,
        non_recon_keyword=qris_recon.NON_RECON_KEYWORDS[0],
        non_recon_prefix=qris_recon.NON_RECON_DESC_PREFIXES[0],
    )
    mp = pytest.MonkeyPatch()
    mp.setattr(qris_recon, "DIR_MUTASI", root / "data" / "mutasi")
    mp.setattr(qris_recon, "DIR_DASH", root / "data" / "dash")
    mp.setattr(qris_recon, "DIR_GDS", root / "data" / "gds")
    mp.setattr(qris_recon, "DIR_OUTPUT", root / "out")
    mp.setattr(qris_recon, "MIDNIGHT_BATCH_SUFFIX", f"0000-{meta['tag']}")
    result = qris_recon.run(DAY.isoformat())
    yield {"meta": meta, "result": result, "root": root}
    mp.undo()


def _status(sample, ref):
    br = sample["result"]["batch_recon"]
    row = br[(br["STATEMENT_BATCH_REF"] == ref) | (br["DASHBOARD_BATCH_REF"] == ref)]
    assert len(row) == 1, f"{ref}: expected exactly one batch row, got {len(row)}"
    return row.iloc[0]["status"]


# --- batch settlement check -------------------------------------------------------------
def test_clean_batches_match(sample):
    refs = sample["meta"]["refs"]
    for ref in refs["normal"] + [refs["pending"]]:
        assert _status(sample, ref) == "MATCH", ref


def test_pending_transaction_is_not_counted_in_batch_total(sample):
    non_succeed = sample["result"]["line_per_line"]
    assert (non_succeed["STATUS TRANSACTION"] == "PENDING").sum() == 1
    assert _status(sample, sample["meta"]["refs"]["pending"]) == "MATCH"


def test_short_and_over_settled_batches_are_mismatch(sample):
    refs = sample["meta"]["refs"]
    assert _status(sample, refs["short"]) == "MISMATCH"
    assert _status(sample, refs["over"]) == "MISMATCH"


def test_shortfall_and_excess_are_reported_separately(sample):
    """A signed sum would net -50,000 against +20,000 and understate the shortfall."""
    shortfall, excess = qris_recon.summarize_shortfall(sample["result"]["batch_recon"])
    assert shortfall == sample["meta"]["short_amount"]
    assert excess == sample["meta"]["over_amount"]


def test_offsetting_gross_and_fee_errors_are_not_hidden(sample):
    """Gross +500 and fee +500 leave the NET cash unchanged. A net-only check calls that a match;
    both the batch status and the Check_Settlement sheet must still flag it."""
    ref = sample["meta"]["refs"]["offset"]
    assert _status(sample, ref) == "MISMATCH"
    sheet = sample["result"]["check_settlement"].set_index("BATCH REFERENCE_DASH")["Match?"]
    assert sheet[ref].startswith("Unmatch")


def test_missing_dash_part_is_incomplete_not_mismatch(sample):
    refs = sample["meta"]["refs"]
    assert _status(sample, refs["incomplete"]) == "INCOMPLETE_DATA"
    assert _status(sample, refs["tomorrow_early"]) == "INCOMPLETE_DATA"


def test_boundary_batches_are_found_in_neighbouring_days(sample):
    refs = sample["meta"]["refs"]
    for key in ("early", "late", "midnight_next"):
        assert _status(sample, refs[key]) == "CROSSDAY_BOUNDARY_ARTIFACT", key


def test_boundary_batch_with_the_wrong_amount_is_a_mismatch(sample):
    """Finding the reference in a neighbouring day proves nothing if the amount is wrong."""
    ref = sample["meta"]["refs"]["early_off"]
    assert _status(sample, ref) == "MISMATCH"
    sheet = sample["result"]["check_settlement"].set_index("BATCH REFERENCE_DASH")["Match?"]
    assert sheet[ref].startswith("Unmatch")


def test_batch_absent_everywhere_is_escalated(sample):
    refs = sample["meta"]["refs"]
    br = sample["result"]["batch_recon"]
    assert _status(sample, refs["gap"]) == "MISSING_IN_MUTASI"
    note = br.loc[br["DASHBOARD_BATCH_REF"] == refs["gap"], "note"].iloc[0]
    assert "escalated" in note


def test_check_settlement_sheet_agrees_with_batch_status(sample):
    """The Excel view and the batch status must never contradict each other."""
    sheet = sample["result"]["check_settlement"].set_index("BATCH REFERENCE_DASH")["Match?"]
    refs = sample["meta"]["refs"]
    for ref in refs["normal"] + [refs["pending"], refs["early"], refs["late"]]:
        assert sheet[ref].startswith("Match"), (ref, sheet[ref])
    for key in ("short", "over", "gap"):
        assert sheet[refs[key]].startswith("Unmatch"), (key, sheet[refs[key]])


# --- QRIS individual rail ---------------------------------------------------------------
def test_individual_rail_resolves_whichever_leg_is_missing(sample):
    pairing = sample["result"]["qi_pairing"].set_index("NO REFERENCE")["status"]
    assert pairing["QR1000001"] == "PAIRED"
    assert pairing["QR9000003"] == "RESOLVED_NEXTDAY"      # 903 leg posted next day
    assert pairing["QR9000004"] == "RESOLVED_NEXTDAY"      # 904 leg posted next day
    assert pairing["QR9000005"] == "PENDING_FEE_CROSSDAY"  # never completed
    # the resolved leg must land in the right column, not just flip the status
    full = sample["result"]["qi_pairing"].set_index("NO REFERENCE")
    assert (full.at["QR9000003", "gross"], full.at["QR9000003", "fee"]) == (200_000, -1_400)
    assert (full.at["QR9000004", "gross"], full.at["QR9000004", "fee"]) == (80_000, -560)


# --- double settlement ------------------------------------------------------------------
def test_double_settlement_flags(sample):
    flags = sample["result"]["double_settlement"].set_index("NO REFERENCE")["status"]
    assert flags["DUPREF1"] == "NEEDS_MANUAL_REVIEW"
    assert flags["MIDREF1"] == "CROSSDAY_BOUNDARY_ARTIFACT"


# --- transaction-level match (Dash <-> GDS) -----------------------------------------------
def test_unrecon_sides_and_remarks(sample):
    lpl = sample["result"]["line_per_line"]
    assert (lpl["Unrecon_Side"] == "GDS_ONLY").sum() == sample["meta"]["n_gds_only"]
    assert (lpl["Unrecon_Side"] == "DASH_ONLY").sum() >= 1       # RRNDASHONLY (+ dropped duplicates)
    ket = sample["result"]["unrecon"]["ket"].value_counts().to_dict()
    assert ket == {"cut off": 1, "needs review": sample["meta"]["n_gds_only"] - 1}


def test_reused_reference_is_matched_on_amount(sample):
    lpl = sample["result"]["line_per_line"]
    reuse = lpl[lpl["NO REFERENCE"] == "REUSE1"]
    assert len(reuse) == 2
    assert set(reuse["Match?"]) == {"Recon"}


def test_duplicate_gds_key_does_not_multiply_rows(sample):
    lpl = sample["result"]["line_per_line"]
    meta = sample["meta"]
    assert len(lpl) <= meta["n_dash_rows"] + meta["n_gds_rows"]
    assert lpl["Match?"].value_counts().get("Recon", 0) > 0


def test_missing_gds_amount_does_not_crash_and_stays_unreconciled(sample):
    lpl = sample["result"]["line_per_line"]
    row = lpl[lpl["Transaction SN"] == "GDSNOAMT01"]
    assert len(row) == 1 and row.iloc[0]["Match?"] == "Unrecon"


def test_unsettled_status_follows_batch_status(sample):
    lpl = sample["result"]["line_per_line"]
    gap_rows = lpl[lpl["BATCH REFERENCE"] == sample["meta"]["refs"]["gap"]]
    assert len(gap_rows) == sample["meta"]["n_gap_trx"]
    assert set(gap_rows["Settlement_Status"]) == {"Unsettled"}


# --- loaders and output -----------------------------------------------------------------------
def test_dash_exact_duplicate_part_is_reported_not_silently_dropped(sample):
    sheets = pd.read_excel(next((sample["root"] / "out").glob("*.xlsx")), sheet_name=None)
    assert len(sheets["Exact_Duplicate_Dash"]) == 1


def test_non_recon_and_unclassified_rows_are_kept_visible(sample):
    sheets = pd.read_excel(next((sample["root"] / "out").glob("*.xlsx")), sheet_name=None)
    types = set(sheets["Non Recon Trx"]["Transaction_Type"])
    assert {"Non_Recon", "QRIS_Individual", "Unclassified"} <= types


def test_outputs_written(sample):
    out = sample["root"] / "out"
    assert list(out.glob("*.xlsx")) and list(out.glob("*.parquet"))
    full = pd.read_parquet(next(out.glob("*.parquet")))
    assert len(full) == len(sample["result"]["line_per_line"])


@pytest.mark.parametrize("quoted", [True, False])
def test_gds_loader_handles_both_quoting_styles(tmp_path, quoted):
    header = "transaction_datetime,Transaction SN,transaction_amount"
    row = '"17 Sept 2026, 10:15:00",SN1,15000' if quoted else "17 Sept 2026, 10:15:00,SN1,15000"
    path = tmp_path / "GDS_QRIS_2026-09-17.csv"
    path.write_text(header + "\n" + row + "\n", encoding="utf-8")
    df = qris_recon.load_gds_raw(str(path))
    assert df.loc[0, "transaction_datetime"] == datetime(2026, 9, 17, 10, 15, 0)
    assert df.loc[0, "transaction_amount"] == 15000


def test_gds_file_name_pattern_accepts_any_word_order():
    for name in ("GDS_QRIS_2026-09-17.csv", "GDS_A_B_2026-09-17.csv", "GDS_2026-09-17.csv"):
        assert qris_recon.FNAME_GDS.search(name), name