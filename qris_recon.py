"""
qris_recon.py
=========================
Three-way reconciliation: bank statement <-> transaction
dashboard <-> internal ledger (GDS).

This script is the result of an extended debugging and design process. Every non-obvious
rule below was learned by testing against real-world data, not assumed up front.
Read the docstring of the relevant function before changing any of this logic - several
rules exist specifically because a naive version was tried first and produced wrong
numbers.

DATA SOURCES (3 sources, 3 different file-naming patterns, downloaded manually by the
Reconciliation Staff into 3 separate folders each day):
  - Statement (bank export)     : BankStatement DDMonYYYY.csv          (e.g. 17Sep2026)
  - Dashboard (acquirer detail) : TransactionDashboard DDMonYYYY[_N].csv (split into
                                    multiple numbered parts when daily volume is high)
  - GDS (internal ledger)       : GDS_<ACQUIRER>_YYYY-MM-DD.csv        (e.g. 2026-09-17)
  Separators in the file name (space vs underscore) and the vendor/QRIS word order in
  the GDS file name are NOT consistent from day to day - the discovery regexes below accept
  both variants on purpose.

MUTASI ROW CLASSIFICATION (4 categories - the check order matters):
  1. QRIS_BATCH      : reference prefix BA-/BQ-, transaction code 903/904.
                        This is the acquirer's normal QRIS settlement, batched every
                        5 minutes. 903 = gross amount in, 904 = the acquirer's fee out.
  2. QRIS_INDIVIDUAL  : reference prefix QR<digits>, transaction code 903/904.
                        This is the individual (non-API) settlement rail (one ref = one
                        transaction, not a 5-minute batch). These transactions are NOT
                        API-based QRIS and are therefore NEVER present in the dashboard
                        or the ledger - there is no independent source to cross-check
                        them against, only an internal check within the statement.
  3. NON_RECON        : a configured keyword appears in the description or narrative
                        (manual internal fund transfers made by the internal finance
                        team, regardless of which internal account they move to), OR the
                        description starts with a configured prefix (internal admin fee),
                        OR starts with a configured prefix (monthly revenue-sharing
                        settlement, unrelated to daily transaction volume).
  4. UNCLASSIFIED     : everything else. These rows MUST be reviewed manually and must
                        never be silently dropped - an unrecognised pattern is exactly
                        the kind of thing that hides a real problem.

BATCH SETTLEMENT CHECK (QRIS_BATCH only):
  A completeness check runs first: if a batch reference exists in Mutasi but is entirely
  absent from Dash, that almost always means one of the numbered Dash file parts for that
  day has not been downloaded yet, not that the amounts genuinely disagree. Such batches
  are marked INCOMPLETE_DATA, never MISMATCH. Only batches where BOTH sides have data but
  the amounts disagree beyond a rounding tolerance are MISMATCH, and only those
  contribute to the "settlement shortfall" total that gets escalated.

MIDNIGHT BOUNDARY LOOKBACK (automatic, in both directions):
  The acquirer settlement cycle does not align with calendar midnight. Two distinct
  boundary effects were found by testing, not assumed:
    - A batch labelled as belonging to today can actually be posted into TOMORROW's
      statement file (this affects batches very late in the day).
    - A batch labelled as belonging to today can also be posted into YESTERDAY's Mutasi
      file (this affects batches very early in the day, e.g. 00:05-00:55 - confirmed on
      real data where every "missing" batch for one day turned out to be sitting in
      the previous day's file).
  Because of this, any batch that first looks like MISSING_IN_MUTASI is checked against
  BOTH the next day's (H+1) and the previous day's (H-1) statement file before it is
  treated as a genuine gap. Only a batch confirmed absent from both is escalated.

GDS DATA IS A LIVE EXPORT, NOT A FINAL SNAPSHOT:
  Discovered the hard way: downloading the same day GDS file twice, a day apart,
  produced completely different totals (hundreds of thousands of rows on T+1 vs a
  noticeably smaller count on T+3 for the same date). GDS reflects the internal
  data warehouse in real time and only stabilises around T+2. Running this script on data younger than T+2
  will show large, confusing discrepancies that have nothing to do with a real
  reconciliation problem - they simply reflect transactions whose GDS record was not
  finished yet at download time. The script prints an explicit warning whenever it is run
  for a date that has not yet reached T+2.

EXCEL OUTPUT SIZE (why the raw sheets and the full line-level detail are NOT in the
xlsx file):
  Writing hundreds of thousands of rows per sheet through openpyxl is very slow (minutes per sheet at this scale); with several large sheets stacked, the workbook became slow to write and very large. The full row-per-row detail is
  now written to a Parquet file instead (a fraction of a second, far smaller on disk,
  fully re-queryable with pandas/polars/DuckDB), and the Excel file only receives the
  "exceptions" - rows that are NOT already a clean settled match. Raw source data is not
  duplicated into the workbook at all, since the original CSVs already exist on disk.
"""

import io
import os
import re
import sys
import glob
import warnings
from pathlib import Path
from datetime import datetime, timedelta

import pandas as pd

# Optional: load environment variables from a local `.env` file (see .env.example).
# If python-dotenv is not installed, the script continues with shell environment
# variables only - it does not crash.
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# ------------------------------------------------------------------------------------
# CONFIGURATION
# ------------------------------------------------------------------------------------
# Path folder sumber data diambil dari environment variable, dengan fallback ke folder
# default di dalam repo (relatif). Ini supaya path pribadi/mesin lokal tidak ikut
# ter-commit ke Git. Set environment variable lewat file .env (lihat .env.example),
# atau override langsung di shell sebelum menjalankan script.
DIR_MUTASI = Path(os.getenv("RECON_MUTASI_DIR", "data/mutasi"))
DIR_DASH   = Path(os.getenv("RECON_DASH_DIR",   "data/dash"))
DIR_GDS    = Path(os.getenv("RECON_GDS_DIR",    "data/gds"))
DIR_OUTPUT = Path(os.getenv("RECON_OUTPUT_DIR", "output"))

# --- Output file name prefixes (cosmetic; safe to change) ---
OUTPUT_PREFIX  = os.getenv("RECON_OUTPUT_PREFIX",  "Recon")
PARQUET_PREFIX = os.getenv("RECON_PARQUET_PREFIX", "LinePerLine_Full")

# Acquirer tag used in midnight-boundary batch references (e.g. "...0000-<TAG>").
# Read from environment variable so the acquirer name does not have to be hardcoded
# in the source. If unset, midnight-boundary detection for double-settlement is
# disabled (the script prints a warning, but otherwise continues normally).
ACQUIRER_TAG = os.getenv("RECON_ACQUIRER_TAG", "").strip()
MIDNIGHT_BATCH_SUFFIX = f"0000-{ACQUIRER_TAG}" if ACQUIRER_TAG else None

TOLERANCE_RP = 1.0                  # rounding tolerance (rupiah) before flagging MISMATCH
CUTOFF_WINDOW_MINUTES = 5           # window around 00:00:00 used to classify Unrecon rows
ROW_WARN_THRESHOLD = 900_000        # 90% of Excel's hard row limit per sheet
ROW_HARD_LIMIT = 1_048_576          # Excel's hard row limit per sheet

QRIS_CODES = {"903", "904"}
NON_RECON_KEYWORDS = tuple(
    k.strip() for k in os.getenv("RECON_NON_RECON_KEYWORDS", "INTERNAL TRANSFER").split(",") if k.strip()
)
NON_RECON_DESC_PREFIXES = tuple(
    p.strip() for p in os.getenv("RECON_NON_RECON_PREFIXES", "AC-,Sharing MDR").split(",") if p.strip()
)

# The separator between the report name and the date is not consistent from the
# vendor/ACQUIRER side - sometimes underscore, sometimes a plain space. [ _] accepts both so
# file discovery does not silently break the next time the naming convention drifts.
FNAME_STATEMENT = re.compile(r"BankStatement[ _](\d{2}[A-Za-z]{3}\d{4})\.csv$", re.IGNORECASE)
FNAME_DASHBOARD = re.compile(r"TransactionDashboard[ _](\d{2}[A-Za-z]{3}\d{4})(?:_(\d+))?\.csv$", re.IGNORECASE)
# The vendor/QRIS word order in the GDS file name has also been seen both ways
# Multiple word orderings have been observed in real data - the regex accepts
# any combination of tokens so file discovery does not silently break.
FNAME_GDS = re.compile(r"GDS_(?:[A-Za-z]+_)*(\d{4}-\d{2}-\d{2})\.csv$", re.IGNORECASE)

GDS_LEADING_DATETIME = re.compile(r"^(\d{1,2} \w+ \d{4}, \d{2}:\d{2}:\d{2}),")


# ------------------------------------------------------------------------------------
# Helpers: date tokens - 3 sources, 3 different date formats. DO NOT unify them.
# ------------------------------------------------------------------------------------
def mutasi_dash_token(d):
    return d.strftime("%d%b%Y")            # "17Sep2026"


def gds_token(d):
    return d.strftime("%Y-%m-%d")          # "2026-09-17"


# ------------------------------------------------------------------------------------
# STEP 0: File discovery
# ------------------------------------------------------------------------------------
def discover_statement_file(source_dir, d):
    token = mutasi_dash_token(d)
    matches = [
        f for f in glob.glob(os.path.join(source_dir, "*.csv"))
        if (m := FNAME_STATEMENT.search(os.path.basename(f))) and m.group(1).lower() == token.lower()
    ]
    if len(matches) == 0:
        return None
    if len(matches) > 1:
        raise RuntimeError(f"Found {len(matches)} Mutasi files for {token}, expected exactly 1: {matches}")
    return matches[0]


def discover_dashboard_files(source_dir, d):
    token = mutasi_dash_token(d)
    found = []
    for f in glob.glob(os.path.join(source_dir, "*.csv")):
        m = FNAME_DASHBOARD.search(os.path.basename(f))
        if m and m.group(1).lower() == token.lower():
            part_no = int(m.group(2)) if m.group(2) else 1
            found.append((part_no, f))
    found.sort(key=lambda x: x[0])
    return [f for _, f in found]


def discover_gds_file(source_dir, d):
    token = gds_token(d)
    matches = [
        f for f in glob.glob(os.path.join(source_dir, "*.csv"))
        if (m := FNAME_GDS.search(os.path.basename(f))) and m.group(1) == token
    ]
    if len(matches) == 0:
        return None
    if len(matches) > 1:
        raise RuntimeError(f"Found {len(matches)} GDS files for {token}, expected exactly 1: {matches}")
    return matches[0]


# ------------------------------------------------------------------------------------
# STEP 1: Loaders
# ------------------------------------------------------------------------------------
def load_statement_raw(path):
    df = pd.read_csv(path, sep="|", dtype=str)
    df.columns = [c.strip() for c in df.columns]
    df["AMOUNT LCY"] = pd.to_numeric(df["AMOUNT LCY"], errors="raise")
    df["DATE TIME"] = pd.to_datetime(df["DATE TIME"], format="%d/%m/%Y %H:%M")
    return df


def load_dashboard_raw(paths):
    """Returns (clean_df, exact_duplicate_rows_removed).
    Guard: NO REFERENCE must be unique per row (it is the transaction-level key). A row
    is only treated as a safe "exact duplicate" (file part downloaded twice) if EVERY
    column matches an earlier row with the same NO REFERENCE. If the amount or batch
    reference differs, this is NOT a harmless export artifact - it is a potential
    genuine double-settlement case, and must be kept (never silently dropped) so
    detect_double_settlement() can evaluate it. Blindly dropping by NO REFERENCE alone
    was found to silently discard a real anomaly and throw off a batch's total by
    exactly the dropped row's amount."""
    frames = []
    for p in paths:
        d = pd.read_csv(p, sep="|", dtype=str)
        d.columns = [c.strip() for c in d.columns]
        d["__source_file"] = os.path.basename(p)
        frames.append(d)
    df = pd.concat(frames, ignore_index=True)

    df["AMOUNT"] = pd.to_numeric(df["AMOUNT"], errors="raise")
    df["MDR AMOUNT"] = pd.to_numeric(df["MDR AMOUNT"], errors="raise")

    compare_cols = [c for c in df.columns if c != "__source_file"]
    is_full_duplicate = df.duplicated(subset=compare_cols, keep="first")

    df["Duplicated?_DASH"] = is_full_duplicate
    exact_dupes = df[is_full_duplicate].copy()
    if not exact_dupes.empty:
        print(f"    [WARNING] {len(exact_dupes)} exact-duplicate rows found (identical in every "
              f"column, likely a file part was downloaded twice). De-duplicated (kept first "
              f"occurrence) - see the Exact_Duplicate_Dash sheet for details.")
    df = df[~is_full_duplicate].copy()

    # After removing TRUE exact duplicates, NO REFERENCE should now be unique. If it is
    # not, these are rows that share a reference but disagree on amount/batch/etc - a
    # genuine anomaly, not an export artifact. They are kept in the data (never dropped)
    # so detect_double_settlement() can flag them properly.
    remaining_dupes = df[df.duplicated(subset=["NO REFERENCE"], keep=False)]
    if not remaining_dupes.empty:
        print(f"    [WARNING] {len(remaining_dupes)} rows share a NO REFERENCE with another row "
              f"but are NOT identical (amount/batch reference differs) - this is a potential "
              f"genuine double-settlement case, NOT a safe duplicate. Kept in the data for "
              f"Step 4 to evaluate - see Double_Settlement_Flag.")

    return df, exact_dupes


def load_gds_raw(path):
    """CONDITIONAL FIX: some GDS exports have a quoting bug (created_datetime is not
    quoted even though it contains a comma), but NOT every file has this bug - some
    daily production exports turned out to already be quoted correctly by the source.
    This function detects the quoting style from the first data row before deciding
    whether to apply the fix, so a correctly-quoted file is never corrupted and a
    badly-quoted one is still repaired."""
    with open(path, "r", encoding="utf-8") as f:
        lines = f.read().splitlines()
    header, rows = lines[0], [r for r in lines[1:] if r.strip()]

    already_quoted = bool(rows) and rows[0].lstrip().startswith('"')

    if already_quoted:
        print("    [INFO] GDS file is already correctly quoted (created_datetime is quoted) - "
              "parsing directly, no quoting fix applied.")
        fixed_rows = rows
        bad_count = 0
    else:
        bad_count = 0
        fixed_rows = []
        for r in rows:
            m = GDS_LEADING_DATETIME.match(r)
            if m:
                fixed_rows.append(GDS_LEADING_DATETIME.sub(r'"\1",', r, count=1))
            else:
                bad_count += 1
                fixed_rows.append(r)  # leave as-is; let pandas raise a clear parsing error
        if bad_count:
            print(f"    [WARNING] {bad_count} GDS rows did not match the expected "
                  f"unquoted-leading-datetime pattern - left unchanged, check manually if "
                  f"parsing fails or columns look shifted.")

    text = header + "\n" + "\n".join(fixed_rows)
    # dtype=str enforced here, matching the pattern in load_statement_raw and load_dashboard_raw
    # atas. Tanpa ini, pandas C-parser menebak tipe tiap kolom per-chunk secara independen -
    # kolom seperti merchant_id bisa berakhir sebagai campuran int/str asli dalam satu kolom
    # (bukan sekadar salah label dtype), yang lolos tanpa error sampai baris ini tapi
    # menggagalkan to_parquet() di akhir run dengan ArrowTypeError ("Expected bytes, got a
    # 'int' object"). Kolom yang memang butuh numerik (transaction_amount, admin_fee, dst)
    # tetap dikonversi eksplisit lewat pd.to_numeric() tepat di bawah ini - jadi aman.
    df = pd.read_csv(io.StringIO(text), dtype=str)

    for col in ("transaction_amount", "admin_fee", "base_admin_fee", "deduction_cost", "settlement_amount"):
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    # The month abbreviation in this column is "Sept" (4 letters), not the standard
    # 3-letter "Sep" that Python's %b expects. Parsing with a strict %b format used to
    # fail SILENTLY on every single row (errors="coerce" turned everything into NaT
    # without raising anything) - this was only discovered after that empty column had
    # already been relied on elsewhere. Cleaning the string first and then parsing with
    # an explicit format is also far faster than letting pandas guess row by row
    # (~1.8 seconds vs ~40 seconds for several hundred thousand rows).
    cleaned = df["transaction_datetime"].str.replace("Sept", "Sep", regex=False)
    df["transaction_datetime"] = pd.to_datetime(cleaned, format="%d %b %Y, %H:%M:%S", errors="coerce")
    n_failed = df["transaction_datetime"].isna().sum()
    if n_failed:
        print(f"    [WARNING] {n_failed} rows failed to parse transaction_datetime even after "
              f"the fix - there may be another month-name variant that is not handled yet. "
              f"Check manually.")
    return df


# ------------------------------------------------------------------------------------
# STEP 2: Classify Mutasi rows
# ------------------------------------------------------------------------------------
def classify_statement_rows(df):
    ref = df["NO REFERENCE"].astype(str)
    desc = df["TRX DESC"].astype(str)
    narrative = df["NARRATIVE"].astype(str)
    code = df["TRANSACTION CODE"]

    is_batch_ref = ref.str.match(r"^(BA|BQ)-", na=False)
    is_individual_ref = ref.str.match(r"^QR\d+$", na=False)
    is_qris_code = code.isin(QRIS_CODES)

    kw_pattern = "|".join(NON_RECON_KEYWORDS)
    is_non_recon = (
        desc.str.contains(kw_pattern, case=False, na=False)
        | narrative.str.contains(kw_pattern, case=False, na=False)
        | desc.str.startswith(NON_RECON_DESC_PREFIXES)
    )

    qris_batch = df[is_batch_ref & is_qris_code].copy()
    qris_individual = df[is_individual_ref & is_qris_code].copy()
    non_recon = df[~is_batch_ref & ~is_individual_ref & is_non_recon].copy()

    handled_idx = pd.concat([qris_batch, qris_individual, non_recon]).index
    unclassified = df[~df.index.isin(handled_idx)].copy()

    if not unclassified.empty:
        print(f"    [WARNING] {len(unclassified)} Mutasi rows did not match any known pattern. "
              f"Review the Unclassified_Mutasi sheet manually - DO NOT assume these are safe.")

    return {
        "qris_batch": qris_batch,
        "qris_individual": qris_individual,
        "non_recon": non_recon,
        "unclassified": unclassified,
    }


def pair_qris_individual(qi_df, current_date, lookback_next_day=True):
    """QRIS_INDIVIDUAL (individual rail): normally 903+904 are posted in the same minute, one
    pair per reference. When a pair is incomplete, it is flagged PENDING_FEE_CROSSDAY -
    the missing fee is NEVER assumed to be zero. The `note` column always states "no
    independent cross-check (not an API QRIS transaction)" because this category is
    simply never recorded in Dash or GDS by design."""
    if qi_df.empty:
        return pd.DataFrame(columns=["NO REFERENCE", "gross", "fee", "status", "note"])

    rows = []
    for no_ref, g in qi_df.groupby("NO REFERENCE"):
        gross_row = g[g["TRANSACTION CODE"] == "903"]
        fee_row = g[g["TRANSACTION CODE"] == "904"]
        gross = gross_row["AMOUNT LCY"].sum() if not gross_row.empty else None
        fee = fee_row["AMOUNT LCY"].sum() if not fee_row.empty else None

        if gross is not None and fee is not None:
            status, note = "PAIRED", "no independent cross-check (not an API QRIS transaction)"
        else:
            status, note = "PENDING_FEE_CROSSDAY", "the 903/904 pair is incomplete in today's file"
            if lookback_next_day:
                resolved_fee, resolved_note = _lookback_qi_next_day(no_ref, current_date)
                if resolved_fee is not None:
                    fee = resolved_fee
                    status, note = "RESOLVED_NEXTDAY", resolved_note
                else:
                    note = resolved_note

        rows.append({"NO REFERENCE": no_ref, "gross": gross, "fee": fee, "status": status, "note": note})

    return pd.DataFrame(rows)


def _lookback_qi_next_day(no_ref, current_date):
    """Checks the next day's (H+1) Mutasi file for the missing 904 (or 903) row for this
    reference. If the H+1 file does not exist yet, this is not treated as an error - it
    simply means the check cannot be completed until the script is run again tomorrow."""
    next_day = current_date + timedelta(days=1)
    next_path = discover_statement_file(DIR_MUTASI, next_day)
    if next_path is None:
        return None, "next-day (H+1) Mutasi file not available yet - re-check on the next run"

    next_df = load_statement_raw(next_path)
    match = next_df[(next_df["NO REFERENCE"] == no_ref) & (next_df["TRANSACTION CODE"] == "904")]
    if match.empty:
        return None, "checked the H+1 Mutasi file, the pair is STILL missing - manual review needed"
    return match["AMOUNT LCY"].sum(), "resolved: the matching pair was found in the H+1 Mutasi file"


# ------------------------------------------------------------------------------------
# STEP 3: Batch settlement check (QRIS_BATCH only)
# ------------------------------------------------------------------------------------
def pivot_statement_batch(qris_batch):
    wide = qris_batch.pivot_table(
        index="NO REFERENCE", columns="TRANSACTION CODE", values="AMOUNT LCY", aggfunc="sum"
    ).reset_index()
    wide.rename(columns={"903": "statement_gross", "904": "statement_fee"}, inplace=True)
    for col in ("statement_gross", "statement_fee"):
        if col not in wide.columns:
            wide[col] = 0.0
    wide[["statement_gross", "statement_fee"]] = wide[["statement_gross", "statement_fee"]].fillna(0.0)

    # Representative posting timestamp per batch, needed for the Check Settlement sheet's
    # "DATE TIME" column and for detecting a cross-day settlement (see build_check_settlement_sheet).
    dt_map = qris_batch.groupby("NO REFERENCE")["DATE TIME"].first()
    wide = wide.merge(dt_map, on="NO REFERENCE", how="left")

    return wide.rename(columns={"NO REFERENCE": "STATEMENT_BATCH_REF", "DATE TIME": "statement_datetime"})


def agg_dashboard_by_batch(dashboard_df):
    """Only SUCCEED transactions count toward what should have settled to the bank.
    PENDING (or any other non-SUCCEED status) transactions have not actually moved
    money yet - including them was found to overstate a batch's expected total by
    exactly the pending transaction's net amount, producing a false MISMATCH against
    Mutasi (which correctly does not include money that hasn't settled)."""
    succeed_only = dashboard_df[dashboard_df["STATUS TRANSACTION"] == "SUCCEED"]
    non_succeed = dashboard_df[dashboard_df["STATUS TRANSACTION"] != "SUCCEED"]
    if not non_succeed.empty:
        print(f"    [INFO] {len(non_succeed)} Dash rows are not STATUS TRANSACTION=SUCCEED "
              f"(e.g. PENDING) - excluded from the batch settlement total, see Non_Succeed_Trx.")
    agg = succeed_only.groupby("BATCH REFERENCE").agg(
        dash_gross=("AMOUNT", "sum"), dash_mdr=("MDR AMOUNT", "sum"), trx_count=("AMOUNT", "size")
    ).reset_index()
    return agg.rename(columns={"BATCH REFERENCE": "DASHBOARD_BATCH_REF"}), non_succeed


def completeness_check(statement_wide, dashboard_agg):
    mutasi_batches = set(statement_wide["STATEMENT_BATCH_REF"])
    dash_batches = set(dashboard_agg["DASHBOARD_BATCH_REF"])
    missing_in_dash = mutasi_batches - dash_batches
    if missing_in_dash:
        print(f"    [COMPLETENESS] {len(missing_in_dash)} batches exist in Mutasi but not in Dash "
              f"(likely a Dash file part has not been fully downloaded) - marked "
              f"INCOMPLETE_DATA, not MISMATCH.")
    return missing_in_dash


def reconcile_batches(statement_wide, dashboard_agg, incomplete_batches):
    merged = statement_wide.merge(dashboard_agg, left_on="STATEMENT_BATCH_REF", right_on="DASHBOARD_BATCH_REF", how="outer")
    merged["diff_gross"] = merged["statement_gross"] - merged["dash_gross"]
    merged["diff_fee_vs_mdr"] = merged["statement_fee"].abs() - merged["dash_mdr"]

    def status(row):
        ref = row["STATEMENT_BATCH_REF"] if pd.notna(row["STATEMENT_BATCH_REF"]) else row["DASHBOARD_BATCH_REF"]
        if ref in incomplete_batches:
            return "INCOMPLETE_DATA"
        if pd.isna(row["dash_gross"]):
            return "MISSING_IN_DASH"
        if pd.isna(row["statement_gross"]):
            return "MISSING_IN_MUTASI"
        if abs(row["diff_gross"]) <= TOLERANCE_RP and abs(row["diff_fee_vs_mdr"]) <= TOLERANCE_RP:
            return "MATCH"
        return "MISMATCH"

    merged["status"] = merged.apply(status, axis=1)
    return merged.sort_values("STATEMENT_BATCH_REF")


def resolve_missing_in_mutasi(batch_recon, current_date, lookback_next_day=True):
    """MISSING_IN_MUTASI means a batch is recorded in Dash but has no trace at all in the
    bank statement (Mutasi) - different from MISMATCH (present on both sides but the
    amounts disagree) or INCOMPLETE_DATA (simply not fully downloaded yet). There are two
    possible causes with very different consequences:
      1. Midnight boundary: the batch "belongs" to this calendar day but its settlement
         was actually posted into a neighbouring day's Mutasi file - not missing money,
         just a date-boundary quirk.
      2. The acquirer genuinely has not transferred the funds yet - this is serious and must be
         escalated to Ops.
    Both directions are checked automatically (H+1 and H-1 - see the module docstring for
    why both directions matter). If a batch is still not found in either file, it is
    NEVER left silently ambiguous - the `note` column explicitly calls for escalation.
    """
    batch_recon = batch_recon.copy()
    if "note" not in batch_recon.columns:
        batch_recon["note"] = ""

    missing_idx = batch_recon.index[batch_recon["status"] == "MISSING_IN_MUTASI"]
    if len(missing_idx) == 0:
        return batch_recon

    if not lookback_next_day:
        batch_recon.loc[missing_idx, "note"] = "lookback_next_day is disabled - H+1/H-1 not checked"
        return batch_recon

    next_day = current_date + timedelta(days=1)
    prev_day = current_date - timedelta(days=1)

    for source_label, target_day in (("H+1", next_day), ("H-1", prev_day)):
        target_statement_path = discover_statement_file(DIR_MUTASI, target_day)
        if target_statement_path is None:
            continue
        target_statement_df = load_statement_raw(target_statement_path)
        target_refs = set(target_statement_df["NO REFERENCE"])

        still_open = batch_recon.index[batch_recon["status"] == "MISSING_IN_MUTASI"]
        for idx in still_open:
            ref = batch_recon.at[idx, "DASHBOARD_BATCH_REF"]
            if ref in target_refs:
                batch_recon.at[idx, "status"] = "CROSSDAY_BOUNDARY_ARTIFACT"
                batch_recon.at[idx, "note"] = (f"found in the {source_label} Mutasi file "
                                                f"({target_day.strftime('%d %b %Y')}) - not missing "
                                                f"money, just a midnight boundary artifact")

    still_missing = batch_recon[batch_recon["status"] == "MISSING_IN_MUTASI"]
    checked_h1 = discover_statement_file(DIR_MUTASI, next_day) is not None
    checked_hm1 = discover_statement_file(DIR_MUTASI, prev_day) is not None
    for idx in still_missing.index:
        if checked_h1 and checked_hm1:
            batch_recon.at[idx, "note"] = ("Checked BOTH the H+1 and H-1 Mutasi files, the batch is "
                                            "STILL not found - MUST be escalated to Ops, funds "
                                            "may not have settled")
        else:
            missing_sides = [lbl for lbl, ok in (("H+1", checked_h1), ("H-1", checked_hm1)) if not ok]
            batch_recon.at[idx, "note"] = (f"could not be fully checked yet - the {', '.join(missing_sides)} "
                                            f"Mutasi file is not available. This status is PROVISIONAL, not final.")
    if not still_missing.empty:
        print(f"    [MISSING_IN_MUTASI] {len(still_missing)} batches are STILL not found in Mutasi "
              f"after checking both H+1 and H-1 - MUST be escalated to Ops, not a boundary artifact.")
    return batch_recon


def _embedded_batch_date(batch_ref):
    """Extracts the calendar date encoded inside a batch reference itself
    (e.g. 'BA-YYMMDD####-<ACQUIRER_TAG>' -> 19 Sept 2026), without needing to open any
    other day's file. Used to catch a third boundary case found in real data: a Mutasi
    row whose batch reference belongs to TOMORROW's Dash file, sitting inside TODAY's
    Mutasi export. This is different from the H+1/H-1 cases already handled, because
    here it is Dash (not Mutasi) that is simply looking at the wrong day."""
    m = re.search(r"-(\d{2})(\d{2})(\d{2})\d{4}-", str(batch_ref))
    if not m:
        return None
    yy, mm, dd = m.groups()
    try:
        return datetime(2000 + int(yy), int(mm), int(dd))
    except ValueError:
        return None


def build_check_settlement_sheet(current_date, dashboard_agg, statement_wide, dashboard_exact_dupes, lookback_next_day=True):
    """Reproduces the exact Check_Settlement layout the Reconciliation team already uses in Excel:
    Dash is the anchor (every batch Dash recorded must be accounted for), Mutasi is
    looked up against it. Column names, order, and the plain 'Match'/'Unmatch' wording
    are kept identical to the reference format.

    The one enhancement requested: when a batch's actual settlement date (column G,
    DATE TIME) differs from the Dash batch date (column A, DATE_DASH) - i.e. a midnight
    boundary case - the Match? column states which day it actually settled on instead of
    just saying "Match", so the discrepancy is self-explanatory without opening the
    script or asking Reconciliation for context.
    """
    date_dash_str = current_date.strftime("%d-%m-%Y")

    dupe_batches = set(dashboard_exact_dupes["BATCH REFERENCE"]) if not dashboard_exact_dupes.empty else set()

    merged = dashboard_agg.merge(statement_wide, left_on="DASHBOARD_BATCH_REF", right_on="STATEMENT_BATCH_REF",
                             how="outer", indicator=True)

    rows = []
    for _, row in merged.iterrows():
        batch_ref = row["DASHBOARD_BATCH_REF"] if pd.notna(row["DASHBOARD_BATCH_REF"]) else row["STATEMENT_BATCH_REF"]
        amount_dash = row["dash_gross"]
        mdr_dash = row["dash_mdr"]
        settled_amount = (amount_dash - mdr_dash) if pd.notna(amount_dash) else None

        mutasi_dt = row.get("statement_datetime")
        amount_lcy = None
        if pd.notna(row.get("statement_gross")):
            amount_lcy = row["statement_gross"] + row["statement_fee"]  # fee is negative -> net

        out = {
            "DATE_DASH": date_dash_str,
            "BATCH REFERENCE_DASH": batch_ref,
            "Duplicated?_DASH": True if batch_ref in dupe_batches else None,
            "AMOUNT_DASH": amount_dash,
            "MDR AMOUNT_DASH": mdr_dash,
            "Settled Amount": settled_amount,
            "DATE TIME": mutasi_dt,
            "NO REFERENCE": row["STATEMENT_BATCH_REF"] if pd.notna(row.get("STATEMENT_BATCH_REF")) else None,
            "AMOUNT LCY": amount_lcy,
            "Duplicated": None,
            "_merge": row["_merge"],
        }

        if row["_merge"] == "both":
            if pd.isna(settled_amount) or pd.isna(amount_lcy) or abs(settled_amount - amount_lcy) > TOLERANCE_RP:
                out["Match?"] = "Unmatch"
            else:
                mutasi_date_str = pd.to_datetime(mutasi_dt).strftime("%d-%m-%Y") if pd.notna(mutasi_dt) else None
                if mutasi_date_str and mutasi_date_str != date_dash_str:
                    direction = "next day" if pd.to_datetime(mutasi_dt).date() > current_date.date() else "previous day"
                    out["Match?"] = f"Match (settled {direction}, {mutasi_date_str})"
                else:
                    out["Match?"] = "Match"
        elif row["_merge"] == "left_only":
            # In Dash, not (yet) in today's Mutasi -- check the neighbouring day's Mutasi
            # file before concluding this is a genuine gap (same H+1/H-1 logic as the
            # main pipeline, applied here so this sheet is self-consistent on its own).
            out["Match?"] = "Unmatch"
            if lookback_next_day:
                for direction, offset in (("next day", 1), ("previous day", -1)):
                    target_day = current_date + timedelta(days=offset)
                    target_path = discover_statement_file(DIR_MUTASI, target_day)
                    if target_path is None:
                        continue
                    target_df = load_statement_raw(target_path)
                    match = target_df[(target_df["NO REFERENCE"] == batch_ref) &
                                       (target_df["TRANSACTION CODE"].isin(["903", "904"]))]
                    if not match.empty:
                        gross = match.loc[match["TRANSACTION CODE"] == "903", "AMOUNT LCY"].sum()
                        fee = match.loc[match["TRANSACTION CODE"] == "904", "AMOUNT LCY"].sum()
                        out["DATE TIME"] = match["DATE TIME"].iloc[0]
                        out["NO REFERENCE"] = batch_ref
                        out["AMOUNT LCY"] = gross + fee
                        out["Match?"] = f"Match (settled {direction}, {target_day.strftime('%d-%m-%Y')})"
                        break
        else:  # right_only: in Mutasi, not in Dash
            embedded_date = _embedded_batch_date(batch_ref)
            if embedded_date and embedded_date.date() != current_date.date():
                # This Mutasi row's batch reference actually belongs to a DIFFERENT
                # day's Dash file (e.g. the 19th's first batches sitting in the 18th's
                # Mutasi export) - it is not that today's Dash data is incomplete, it is
                # that this row simply isn't today's business at all.
                out["Match?"] = (f"Unmatch (Dash data incomplete) "
                                  f"(Transaction Date {embedded_date.strftime('%Y-%m-%d')})")
            else:
                out["Match?"] = "Unmatch (Dash data incomplete)"

        rows.append(out)

    result = pd.DataFrame(rows, columns=[
        "DATE_DASH", "BATCH REFERENCE_DASH", "Duplicated?_DASH", "AMOUNT_DASH", "MDR AMOUNT_DASH",
        "Settled Amount", "DATE TIME", "NO REFERENCE", "AMOUNT LCY", "Duplicated", "_merge", "Match?",
    ])

    # amount_mismatch? -- the absolute rupiah gap this row still represents, 0 when
    # everything is accounted for (a clean match OR an explained boundary/date case).
    # For a genuine "both" mismatch this is |Settled Amount - AMOUNT LCY|; for a Mutasi
    # entry with no Dash counterpart it is the full unaccounted AMOUNT LCY; for a Dash
    # batch with no Mutasi counterpart it is the full expected Settled Amount.
    def _mismatch_amount(row):
        if str(row["Match?"]).startswith("Match"):
            return 0
        if row["_merge"] == "both":
            return abs(row["Settled Amount"] - row["AMOUNT LCY"])
        if row["_merge"] == "right_only":
            return row["AMOUNT LCY"]
        return row["Settled Amount"]

    result["amount_mismatch?"] = result.apply(_mismatch_amount, axis=1)
    return result.sort_values("BATCH REFERENCE_DASH")


def settled_batch_set(batch_recon):
    """The SINGLE source of truth for "which batches are settled". Reused everywhere
    this question matters: the settlement-shortfall total, the "ks" remark on the Dash
    Raw copy, and Settlement_Status in Line Per Line - so those three things can never
    contradict each other. CROSSDAY_BOUNDARY_ARTIFACT counts as settled (it is only a
    date-boundary quirk, not a funds gap)."""
    ok = batch_recon[batch_recon["status"].isin(["MATCH", "INCOMPLETE_DATA", "CROSSDAY_BOUNDARY_ARTIFACT"])]
    refs = pd.concat([ok["STATEMENT_BATCH_REF"], ok["DASHBOARD_BATCH_REF"]]).dropna().unique()
    return set(refs)


# ------------------------------------------------------------------------------------
# STEP 4: Double Settlement detection (Dash-level, transaction-level)
# ------------------------------------------------------------------------------------
def detect_double_settlement(dashboard_df, current_date, lookback_next_day=True):
    """One NO REFERENCE appearing under more than one distinct BATCH REFERENCE is real
    money, not an export artifact, and must be treated with care. Lookback H+1: if the
    second batch reference is a midnight-boundary batch (ends with the configured
    pattern) and that same batch also legitimately appears in the next day's Dash file,
    it is downgraded to informational (not an actual double charge). Otherwise it stays
    flagged for manual review."""
    grp = dashboard_df.groupby("NO REFERENCE")["BATCH REFERENCE"].nunique()
    suspects = grp[grp > 1].index

    if MIDNIGHT_BATCH_SUFFIX is None and len(suspects) > 0:
        print("    [WARN] RECON_ACQUIRER_TAG is not set - midnight-boundary detection "
              "for double settlement is DISABLED. Set the env var (see .env.example) "
              "to enable it.")
    if len(suspects) == 0:
        return pd.DataFrame(columns=["NO REFERENCE", "batch_refs", "status", "note"])

    next_day = current_date + timedelta(days=1)
    next_dashboard_paths = discover_dashboard_files(DIR_DASH, next_day)
    next_dash_batches = set()
    if next_dashboard_paths:
        next_df, _ = load_dashboard_raw(next_dashboard_paths)
        next_dash_batches = set(next_df["BATCH REFERENCE"].unique())

    rows = []
    for no_ref in suspects:
        batches = sorted(dashboard_df.loc[dashboard_df["NO REFERENCE"] == no_ref, "BATCH REFERENCE"].unique())
        boundary_batches = [b for b in batches if MIDNIGHT_BATCH_SUFFIX and str(b).endswith(MIDNIGHT_BATCH_SUFFIX)]

        if lookback_next_day and boundary_batches and any(b in next_dash_batches for b in boundary_batches):
            status = "CROSSDAY_BOUNDARY_ARTIFACT"
            note = "the midnight batch also legitimately appears in the H+1 Dash file - not a double charge, informational only"
        else:
            status = "NEEDS_MANUAL_REVIEW"
            note = ("no evidence yet that this is a boundary artifact - MUST be verified against "
                    "Mutasi to confirm whether funds genuinely landed twice before escalating to Ops")
        rows.append({"NO REFERENCE": no_ref, "batch_refs": ", ".join(batches), "status": status, "note": note})

    result = pd.DataFrame(rows)
    flagged = result[result["status"] == "NEEDS_MANUAL_REVIEW"]
    if not flagged.empty:
        print(f"    [DOUBLE SETTLEMENT] {len(flagged)} NO REFERENCE values appear to have settled "
              f"in more than one batch WITHOUT a boundary explanation - manual review required "
              f"before assuming this is safe.")
    return result


# ------------------------------------------------------------------------------------
# STEP 5: Line Per Line reconciliation (transaction-level 3-way match) + Unrecon
# ------------------------------------------------------------------------------------
def build_line_per_line(gds_df, dashboard_df, settled_batches):
    """Vectorized on purpose - DO NOT use .apply(axis=1) here. For ~several hundred thousand rows x ~50
    columns after the merge, row-by-row .apply is far slower than the vectorized merge.

    Matches on (reference, amount) rather than reference alone: real-world data
    showed the acquirer reusing the same NO REFERENCE for two completely unrelated transactions
    from two different merchants, a few hours apart. Matching on reference alone would
    have silently merged the wrong pair together. Amount is added to the key purely to
    disambiguate this rare collision - it is not expected to change the outcome for the
    overwhelming majority of rows, where the reference is already unique."""
    dashboard_df = dashboard_df.copy()
    gds_df = gds_df.copy()
    dashboard_df["_match_key"] = dashboard_df["NO REFERENCE"].astype(str) + "|" + dashboard_df["AMOUNT"].astype("int64").astype(str)
    gds_df["_match_key"] = gds_df["Transaction SN"].astype(str) + "|" + gds_df["transaction_amount"].astype("int64").astype(str)

    dash_dupe_keys = dashboard_df["_match_key"].duplicated(keep=False)
    if dash_dupe_keys.any():
        print(f"    [WARNING] {dash_dupe_keys.sum()} Dash rows still collide on (reference, amount) "
              f"even after adding amount to the key - a true ambiguous case, only the first "
              f"occurrence will be matched to GDS. Check Double_Settlement_Flag for these references.")
    dashboard_df = dashboard_df.drop_duplicates(subset=["_match_key"], keep="first")

    merged = dashboard_df.merge(
        gds_df, on="_match_key", how="outer",
        suffixes=("_DASH", "_GDS"), indicator=True,
    )
    merged["Match?"] = merged["_merge"].map({"both": "Recon", "left_only": "Unrecon", "right_only": "Unrecon"})

    is_in_batch = merged["BATCH REFERENCE"].notna()
    is_settled = merged["BATCH REFERENCE"].isin(settled_batches)
    merged["Settlement_Status"] = pd.Series(
        pd.NA, index=merged.index, dtype=object
    )
    merged.loc[~is_in_batch, "Settlement_Status"] = "N/A"
    merged.loc[is_in_batch & is_settled, "Settlement_Status"] = "Settled"
    merged.loc[is_in_batch & ~is_settled, "Settlement_Status"] = "Unsettled"
    return merged.drop(columns=["_match_key"])


def build_unrecon(line_per_line):
    """ket (remark) = "cut off" ONLY when the GDS transaction_datetime falls within a
    +-N minute window of 00:00:00 (default 5 minutes, confirmed against real data).
    Outside that window the remark is "needs review", because every other reason a
    transaction ends up unmatched is a manual judgement call, not something this script
    can classify automatically."""
    unrecon = line_per_line[line_per_line["_merge"] == "right_only"].copy()
    if unrecon.empty:
        unrecon["ket"] = pd.Series(dtype=str)
        return unrecon

    ts = pd.to_datetime(unrecon["transaction_datetime"])
    minutes_from_midnight = ts.dt.hour * 60 + ts.dt.minute
    is_near_midnight = (minutes_from_midnight <= CUTOFF_WINDOW_MINUTES) | \
                        (minutes_from_midnight >= 24 * 60 - CUTOFF_WINDOW_MINUTES)
    unrecon["ket"] = is_near_midnight.map({True: "cut off", False: "needs review"})
    return unrecon


def build_non_recon_sheet(classified):
    """Combines all three 'not a normal QRIS batch settlement' categories into one sheet,
    tagged by a Transaction_Type column so each row's origin is still traceable. Mutasi's
    raw export has exactly 10 columns, so this new column naturally lands in column K -
    matching the Reconciliation team's requested layout without needing to force a specific position."""
    parts = []
    for label, df in (
        ("Non_Recon", classified["non_recon"]),
        ("QRIS_Individual", classified["qris_individual"]),
        ("Unclassified", classified["unclassified"]),
    ):
        if df.empty:
            continue
        tagged = df.copy()
        tagged["Transaction_Type"] = label
        parts.append(tagged)
    if not parts:
        return pd.DataFrame()
    return pd.concat(parts, ignore_index=True)


# ------------------------------------------------------------------------------------
# STEP 6: Row-count guard (Excel/openpyxl, hard limit 1,048,576 rows per sheet)
# ------------------------------------------------------------------------------------
def check_row_count_guard(n_rows, sheet_name):
    if n_rows > ROW_HARD_LIMIT:
        raise RuntimeError(
            f"[STOP] Sheet '{sheet_name}' has {n_rows:,} rows, which EXCEEDS Excel's hard "
            f"limit ({ROW_HARD_LIMIT:,}). Stopping BEFORE writing, so the output file is "
            f"never left half-written/corrupted. This is the point to revisit the output "
            f"architecture (the exceptions-only + Parquet approach used here will need a "
            f"further redesign, e.g. splitting exceptions across multiple sheets)."
        )
    if n_rows >= ROW_WARN_THRESHOLD:
        warnings.warn(
            f"[WARNING] Sheet '{sheet_name}' has {n_rows:,} rows, approaching Excel's hard "
            f"limit ({ROW_HARD_LIMIT:,}). Transaction volume may be growing - it may be "
            f"time to revisit the output architecture before actually hitting the limit.",
            stacklevel=2,
        )


# ------------------------------------------------------------------------------------
# MAIN
# ------------------------------------------------------------------------------------
def run(date_str, lookback_next_day=True):
    """date_str in ISO format: 'YYYY-MM-DD', e.g. '2026-09-17'.

    IMPORTANT (found through extended investigation - see the module docstring): GDS is a
    LIVE EXPORT reflecting the internal data warehouse - ITS CONTENTS CAN CHANGE on
    re-download, and are only considered FINAL/stable at T+2 (2 days after the
    transaction date). If run() is called for a date whose GDS data is not yet T+2, the
    reconciliation result from this run is NOT final - do not use it as the basis for an
    escalation to Ops before re-running it after T+2."""
    d = datetime.strptime(date_str, "%Y-%m-%d")

    print("=" * 78)
    print(f" QRIS RECONCILIATION - {d.strftime('%A, %d %B %Y')}")
    print("=" * 78)

    days_elapsed = (datetime.now() - d).days
    if days_elapsed < 2:
        warnings.warn(
            f"[DATA NOT FINAL] {date_str} is only T+{days_elapsed} from today. GDS is a "
            f"live export and only stabilises at T+2 - this run's results are "
            f"PROVISIONAL, especially for anything comparing GDS to Mutasi. DO NOT use "
            f"this as the basis for a final escalation to Ops. Re-run after "
            f"{(d + timedelta(days=2)).strftime('%Y-%m-%d')} for final numbers.",
            stacklevel=2,
        )
        print(f"[DATA NOT FINAL] See warning above - today's results are provisional (T+{days_elapsed}).")
    else:
        print(f"[DATA STATUS] T+{days_elapsed} - GDS data should be final.")

    print("\n--- STEP 0: Locating source files ---")
    statement_path = discover_statement_file(DIR_MUTASI, d)
    dashboard_paths = discover_dashboard_files(DIR_DASH, d)
    gds_path = discover_gds_file(DIR_GDS, d)
    if not statement_path or not dashboard_paths or not gds_path:
        raise FileNotFoundError(
            f"Incomplete source files for {date_str} -- Mutasi:{statement_path}, "
            f"Dash:{dashboard_paths}, GDS:{gds_path}"
        )
    print(f"  Mutasi : {statement_path}")
    print(f"  Dash   : {dashboard_paths}")
    print(f"  Ledger   : {gds_path}")

    print("\n--- STEP 1: Loading and cleaning source files ---")
    statement_df = load_statement_raw(statement_path)
    print(f"  [{datetime.now().strftime('%H:%M:%S')}] Mutasi loaded: {len(statement_df):,} rows")
    dashboard_df, dashboard_exact_dupes = load_dashboard_raw(dashboard_paths)
    print(f"  [{datetime.now().strftime('%H:%M:%S')}] Dash loaded: {len(dashboard_df):,} rows "
          f"(from {len(dashboard_paths)} file part(s))")
    gds_df = load_gds_raw(gds_path)
    print(f"  [{datetime.now().strftime('%H:%M:%S')}] GDS loaded: {len(gds_df):,} rows")

    print("\n--- STEP 2: Classifying Mutasi rows ---")
    classified = classify_statement_rows(statement_df)
    print(f"  QRIS Batch (BA-/BQ-)       : {len(classified['qris_batch']):,} rows")
    print(f"  QRIS Individual (individual rail): {len(classified['qris_individual']):,} rows")
    print(f"  Non-Recon (internal moves) : {len(classified['non_recon']):,} rows")
    print(f"  Unclassified (needs review): {len(classified['unclassified']):,} rows")
    qi_pairing = pair_qris_individual(classified["qris_individual"], d, lookback_next_day)
    if not qi_pairing.empty:
        print(f"  QRIS Individual pairing   : {qi_pairing['status'].value_counts().to_dict()}")
    print(f"  [{datetime.now().strftime('%H:%M:%S')}] Step 2 complete")

    print("\n--- STEP 3: Batch settlement check (Mutasi vs Dash) ---")
    statement_wide = pivot_statement_batch(classified["qris_batch"])
    dashboard_agg, dashboard_non_succeed = agg_dashboard_by_batch(dashboard_df)
    incomplete = completeness_check(statement_wide, dashboard_agg)
    batch_recon = reconcile_batches(statement_wide, dashboard_agg, incomplete)
    batch_recon = resolve_missing_in_mutasi(batch_recon, d, lookback_next_day)
    settled_batches = settled_batch_set(batch_recon)
    check_settlement_sheet = build_check_settlement_sheet(
        d, dashboard_agg, statement_wide, dashboard_exact_dupes, lookback_next_day
    )
    print(f"  [{datetime.now().strftime('%H:%M:%S')}] Step 3 complete (including H+1/H-1 lookback)")

    kurang_settle = batch_recon.loc[batch_recon["status"] == "MISMATCH", "diff_gross"].sum()
    print(f"\n  Batch status breakdown:")
    for status_name, count in batch_recon["status"].value_counts().items():
        print(f"    {status_name:<28} {count:>6,}")
    print(f"  Settlement shortfall (genuine MISMATCH only, not INCOMPLETE_DATA): "
          f"Rp{kurang_settle:,.0f}")
    print(f"  Check_Settlement sheet Match? breakdown:")
    for match_label, count in check_settlement_sheet["Match?"].value_counts().items():
        print(f"    {match_label:<40} {count:>6,}")

    print("\n--- STEP 4: Double settlement check ---")
    double_settlement = detect_double_settlement(dashboard_df, d, lookback_next_day)
    print(f"  {len(double_settlement)} NO REFERENCE value(s) flagged for review")
    print(f"  [{datetime.now().strftime('%H:%M:%S')}] Step 4 complete (including H+1 lookback)")

    print("\n--- STEP 5: Transaction-level 3-way match (Dash <-> GDS) ---")
    # Matching key is (reference, amount), not reference alone - see build_line_per_line
    # for why (a real reference collision between two different merchants was found).
    line_per_line = build_line_per_line(gds_df, dashboard_df, settled_batches)
    print(f"  Merged rows: {len(line_per_line):,}")
    unrecon = build_unrecon(line_per_line)
    if not unrecon.empty:
        print(f"  Unrecon breakdown: {unrecon['ket'].value_counts().to_dict()}")
    print(f"  [{datetime.now().strftime('%H:%M:%S')}] Step 5 complete")

    print("\n--- STEP 6: Row-count guard ---")
    is_exception_preview = (line_per_line["Match?"] == "Unrecon") | (line_per_line["Settlement_Status"] != "Settled")
    n_exceptions = int(is_exception_preview.sum())
    print(f"  Exception rows that will be written to Excel: {n_exceptions:,} "
          f"(out of {len(line_per_line):,} total)")
    check_row_count_guard(n_exceptions, "Line Per Line (Exceptions)")

    print("\n--- STEP 7: Writing output ---")
    out_name = f"{OUTPUT_PREFIX}_{d.strftime('%d-%m-%y')}.xlsx"
    out_path = os.path.join(DIR_OUTPUT, out_name)

    # The full row-level detail is saved as Parquet (fast, small, fully re-queryable via
    # pandas/polars/DuckDB at any time) rather than written in full into Excel. Excel
    # only receives the rows that are NOT already a clean settled match.
    parquet_path = os.path.join(DIR_OUTPUT, f"LinePerLine_Full_{d.strftime('%d-%m-%y')}.parquet")
    line_per_line.to_parquet(parquet_path, index=False)
    print(f"  Full Line Per Line ({len(line_per_line):,} rows) saved to Parquet: {parquet_path}")

    is_exception = (line_per_line["Match?"] == "Unrecon") | (line_per_line["Settlement_Status"] != "Settled")
    line_per_line_exceptions = line_per_line[is_exception].copy()
    print(f"  Line Per Line exceptions for Excel: {len(line_per_line_exceptions):,} rows "
          f"(the remaining Recon+Settled rows only need the Parquet file).")

    print(f"  [{datetime.now().strftime('%H:%M:%S')}] Writing Excel workbook: {out_path}")
    print(f"  (Raw source sheets are no longer duplicated into Excel - the original CSVs "
          f"already exist on disk. Line Per Line is exceptions-only.)")
    non_recon_combined = build_non_recon_sheet(classified)
    with pd.ExcelWriter(out_path, engine="openpyxl") as writer:
        line_per_line_exceptions.to_excel(writer, sheet_name="Line Per Line (Exceptions)", index=False)
        unrecon.to_excel(writer, sheet_name="Unrecon", index=False)
        check_settlement_sheet.to_excel(writer, sheet_name="Check_Settlement", index=False)
        non_recon_combined.to_excel(writer, sheet_name="Non Recon Trx", index=False)
        double_settlement.to_excel(writer, sheet_name="Double_Settlement_Flag", index=False)
        dashboard_exact_dupes.to_excel(writer, sheet_name="Exact_Duplicate_Dash", index=False)
        dashboard_non_succeed.to_excel(writer, sheet_name="Non_Succeed_Trx", index=False)

    print(f"\n[{datetime.now().strftime('%H:%M:%S')}] DONE. Output: {out_path}")
    print("=" * 78)
    return {
        "batch_recon": batch_recon,
        "line_per_line": line_per_line,
        "unrecon": unrecon,
        "double_settlement": double_settlement,
        "qi_pairing": qi_pairing,
    }


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: python qris_recon.py <YYYY-MM-DD>   example: 2026-09-17")
        sys.exit(1)
    run(sys.argv[1])