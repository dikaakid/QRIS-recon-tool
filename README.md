# QRIS Recon Tool

Three-way reconciliation between:

- **Bank statement** - acquirer "Mutasi Rekening" style export
- **Transaction dashboard** - acquirer "Trx Merchant" style export
- **Internal ledger (GDS)** - the acquirer internal transaction ledger

This tool was built through an extended debugging and design process against real
production data. Every non-obvious rule below was *learned by testing*, not assumed up
front. Several rules exist specifically because a naive version was tried first and
produced wrong numbers.

> Read this document before changing any logic in the script. The reasoning behind
> each rule is documented here on purpose - the script itself only contains a short
> docstring pointing back to this README.

---

## Table of Contents

1. [Data Sources](#1-data-sources)
2. [Statement Row Classification](#2-statement-row-classification)
3. [Batch Settlement Check](#3-batch-settlement-check)
4. [Midnight Boundary Lookback](#4-midnight-boundary-lookback)
5. [Ledger Is a Live Export (T+2 Rule)](#5-ledger-is-a-live-export-t2-rule)
6. [Excel Output Size Strategy](#6-excel-output-size-strategy)
7. [Reconciliation Workflow](#7-reconciliation-workflow)
8. [Non-Obvious Design Decisions](#8-non-obvious-design-decisions)
9. [Installation](#9-installation)
10. [Configuration](#10-configuration)
11. [Usage](#11-usage)
12. [Output Files](#12-output-files)
13. [Troubleshooting](#13-troubleshooting)

---

## 1. Data Sources

Three sources, three different file-naming patterns, downloaded manually into
three separate folders each day:

| Source       | Contents                 | File name pattern                                  |
|--------------|--------------------------|----------------------------------------------------|
| Statement    | Bank statement           | `BankStatement DDMonYYYY.csv`                      |
| Dashboard    | Acquirer transaction log | `TransactionDashboard DDMonYYYY[_N].csv`           |
| Ledger (GDS) | Internal transaction log | `GDS_<ACQUIRER>_YYYY-MM-DD.csv`                    |

### Notes on file names

- **Separators are inconsistent.** Some days use a space, some use an underscore
  between the report name and the date. The discovery regexes accept both.
- **Dashboard can be split into multiple parts.** When daily volume is high, the
  export comes as `..._1.csv`, `..._2.csv`, etc. The script loads and concatenates
  all parts.
- **Ledger file name word order is inconsistent.** Multiple orderings of the
  intermediate tokens have been observed. The regex accepts any.

---

## 2. Statement Row Classification

Every statement row is assigned to exactly one of four categories. **The check
order matters** - an earlier category wins.

### 2.1 `QRIS_BATCH`

- Reference prefix: `BA-` or `BQ-`
- Transaction code: `903` or `904`
- This is the acquirer normal QRIS settlement, batched every 5 minutes.
- `903` = gross amount in, `904` = acquirer fee taken out.
- **Cross-checked against the dashboard and ledger.**

### 2.2 `QRIS_INDIVIDUAL`

- Reference prefix: `QR<digits>` (e.g. `QR1234567`)
- Transaction code: `903` or `904`
- This is the individual (non-API) settlement rail - one reference = one
  transaction, not a 5-minute batch.
- These transactions are **NOT** API-based QRIS and are therefore **never
  present in the dashboard or ledger**.
- There is no independent source to cross-check them against; only an internal
  consistency check within the statement itself (pairing the 903 and 904 legs).

### 2.3 `NON_RECON`

Any of the following:

- A configured keyword appears in the description **or** narrative
  (manual internal fund transfers by the internal finance team, regardless of
  which internal account they move to).
- The description starts with a configured prefix (an internal admin fee line).
- The description starts with a configured prefix (a monthly revenue-sharing
  settlement, unrelated to daily transaction volume).

The keywords and prefixes are read from environment variables
(`RECON_NON_RECON_KEYWORDS` and `RECON_NON_RECON_PREFIXES`). See
[Configuration](#10-configuration).

### 2.4 `UNCLASSIFIED`

Everything else.

> **These rows MUST be reviewed manually and must never be silently dropped.**
> An unrecognised pattern is exactly the kind of thing that hides a real problem.

---

## 3. Batch Settlement Check

Applies to `QRIS_BATCH` rows only.

### Completeness check runs first

If a batch reference exists in the **statement** but is entirely absent from the
**dashboard**, that almost always means one of the numbered dashboard file parts
for that day has not been downloaded yet - not that the amounts genuinely
disagree.

Such batches are marked **`INCOMPLETE_DATA`**, never `MISMATCH`.

### Only then does the mismatch check run

- Batches where **both** sides have data but the amounts disagree beyond a
  rounding tolerance (`TOLERANCE_RP`, default 1) are `MISMATCH`.
- **Only `MISMATCH` batches contribute to the "settlement shortfall" total that
  gets escalated.**

### Only SUCCEED transactions count

In dashboard aggregation, only `STATUS TRANSACTION == "SUCCEED"` rows count
toward what should have settled to the bank. `PENDING` (or any other non-SUCCEED
status) transactions have not actually moved money yet - including them was
found to overstate a batch expected total by exactly the pending transaction net
amount, producing a **false MISMATCH** against the statement (which correctly
does not include money that has not settled).

---

## 4. Midnight Boundary Lookback

The acquirer settlement cycle does not align perfectly with calendar midnight.
**Two distinct boundary effects** were found by testing, not assumed:

1. **Forward (+1 day).** A batch labelled as belonging to *today* can actually be
   posted into **tomorrow** statement file. This affects batches very late in the
   day.

2. **Backward (-1 day).** A batch labelled as belonging to *today* can also be
   posted into **yesterday** statement file. This affects batches very early in
   the day (e.g. 00:05-00:55).

   > Confirmed on real data: for one day, **every "missing" batch** turned out to
   > be sitting in the previous day file.

### The rule

Because of this, any batch that first looks like `MISSING_IN_STATEMENT` is
checked against **BOTH** the next day (H+1) **and** the previous day (H-1)
statement file before it is treated as a genuine gap.

- If found -> `CROSSDAY_BOUNDARY_ARTIFACT` (not missing money, only a date quirk).
- If the H+1 or H-1 file does not exist yet -> the status is marked `PROVISIONAL`.
- **Only a batch confirmed absent from both is escalated to Ops.**

### Batch-reference-embedded date

There is a third, rarer boundary case: a statement row whose batch reference
belongs to **tomorrow** dashboard file, sitting inside **today** statement
export. The batch reference encodes its own calendar date
(e.g. `BA-YYMMDD####-<ACQUIRER_TAG>`), which is extracted via
`_embedded_batch_date()` without opening any other day file.

---

## 5. Ledger Is a Live Export (T+2 Rule)

**This was discovered the hard way.**

Downloading the same day ledger file twice, one day apart, produced completely
different totals:

| Download time | Rows for the same calendar date |
|---------------|----------------------------------|
| Day T+1       | several hundred thousand         |
| Day T+3       | a noticeably smaller count       |

The ledger reflects the internal data warehouse **in real time**. It only
stabilises around **T+2** (two days after the transaction date).

### Consequences

- Running this script on data younger than T+2 will show **large, confusing
  discrepancies** that have nothing to do with a real reconciliation problem.
- They simply reflect transactions whose ledger record was not finished yet at
  download time.
- The script prints an explicit **`[DATA NOT FINAL]`** warning whenever it is
  run for a date that has not yet reached T+2.

> **Do not use a pre-T+2 run as the basis for a final escalation to Ops.**

---

## 6. Excel Output Size Strategy

### The problem

Writing several hundred thousand rows per sheet through `openpyxl` was measured
at **many minutes per sheet**. With multiple large sheets stacked (which the very
first version of this script did), the workbook became slow to write and very large.

### The solution

- The **full row-per-row detail** is written to a **Parquet** file instead.
  - A fraction of a second to write, far smaller on disk, fully re-queryable
    with `pandas` / `polars` / `DuckDB`.
- The **Excel** file only receives the **exceptions** - rows that are *not*
  already a clean settled match.
- Raw source data is **not duplicated into the workbook at all**, since the
  original CSVs already exist on disk.

---

## 7. Reconciliation Workflow

The script `run()` function executes these steps in order:

| Step | Name                                    | What it does                                                                 |
|------|-----------------------------------------|------------------------------------------------------------------------------|
| 0    | Locate source files                     | Discover the statement, dashboard, and ledger files for the given date       |
| 1    | Load & clean source files               | Parse, type-cast, de-duplicate, fix known quoting bugs                       |
| 2    | Classify statement rows                 | Assign each row to `QRIS_BATCH` / `QRIS_INDIVIDUAL` / `NON_RECON` / `UNCLASSIFIED` |
| 3    | Batch settlement check                  | Pivot by batch, aggregate dashboard (SUCCEED only), reconcile, H+1/H-1 lookback |
| 4    | Double settlement detection             | Flag `NO REFERENCE` values appearing under >1 `BATCH REFERENCE`              |
| 5    | Transaction-level 3-way match           | Vectorized outer merge on (reference, amount) key                            |
| 6    | Row-count guard                         | Refuse to write if any sheet would exceed Excel hard limit                   |
| 7    | Write output                            | Parquet (full) + Excel (exceptions only)                                     |

---

## 8. Non-Obvious Design Decisions

Each of these exists for a specific, discovered reason.

| Decision                                                | Why                                                                                                     |
|---------------------------------------------------------|---------------------------------------------------------------------------------------------------------|
| Regex accepts both space **and** underscore             | File names are not consistent day to day                                                                |
| Dashboard duplicates compared on **all columns**, not just `NO REFERENCE` | Blindly dropping by `NO REFERENCE` alone silently discarded a real anomaly and threw off a batch total |
| Only `SUCCEED` counts toward batch total                | `PENDING` overstates the expected total -> false MISMATCH                                               |
| `INCOMPLETE_DATA` != `MISMATCH`                         | A missing dashboard file part is not lost money                                                         |
| Two-directional lookback (**H+1 AND H-1**)              | Batches at 00:05-00:55 and late-day batches both exist in real data                                     |
| Match key = `(reference, amount)`, not `reference`      | The acquirer reuses the same `NO REFERENCE` for two unrelated transactions from two different merchants |
| Transaction-level merge is vectorized (no row-wise `.apply`) | Row-wise logic over hundreds of thousands of rows is far slower; row-wise code is limited to the batch-level tables (hundreds of rows per day) |
| Parquet for full detail, Excel for exceptions only | Writing every row to Excel through `openpyxl` is slow and produces very large files; Parquet is compact and re-queryable |
| Ledger treated as live, only stable at T+2              | Totals change drastically if downloaded too early                                                       |
| `"Sept"` -> `"Sep"` pre-cleaning before `pd.to_datetime` | Python `%b` does not recognise `"Sept"`; silently NaT otherwise                                         |
| `dtype=str` enforced in ledger loader                   | Prevents mixed-type columns -> `ArrowTypeError` on `to_parquet()`                                       |
| `settled_batch_set()` is the single source of truth     | Keeps shortfall total, dashboard remark, and Line-Per-Line status from contradicting each other         |

### The `ket` column

`ket` is short for the Indonesian *keterangan* (remark / note). The column name
is **kept as-is on purpose** - downstream spreadsheets that reference this column keep working; renaming it would break them.

---

## 9. Installation

    git clone <repo-url>
    cd QRIS-recon-tool
    python -m venv .venv
    .venv\Scripts\activate          # Windows
    # source .venv/bin/activate     # macOS / Linux
    pip install -r requirements.txt

**Requirements:** Python 3.10+ (uses the walrus operator `:=`).

---

## 10. Configuration

Folder paths are read from environment variables. Copy `.env.example` to `.env`
and adjust, or set the variables directly in your shell.

| Variable                      | Default             | Purpose                                       |
|-------------------------------|---------------------|-----------------------------------------------|
| `RECON_MUTASI_DIR`            | `./data/mutasi`     | Bank statement folder                         |
| `RECON_DASH_DIR`              | `./data/dash`       | Transaction dashboard folder                  |
| `RECON_GDS_DIR`               | `./data/gds`        | Internal ledger folder                        |
| `RECON_OUTPUT_DIR`            | `./output`          | Output folder                                 |
| `RECON_OUTPUT_PREFIX`         | `Recon`             | Prefix for the Excel output filename          |
| `RECON_PARQUET_PREFIX`        | `LinePerLine_Full`  | Prefix for the Parquet output filename        |
| `RECON_ACQUIRER_TAG`          | *(empty)*           | Acquirer tag in midnight batch refs (optional)|
| `RECON_NON_RECON_KEYWORDS`    | `INTERNAL TRANSFER` | Comma-separated keywords for NON_RECON        |
| `RECON_NON_RECON_PREFIXES`    | `AC-,Sharing MDR`   | Comma-separated description prefixes          |

Other tunables are defined as constants at the top of the script:

| Constant                 | Default   | Purpose                                        |
|--------------------------|-----------|------------------------------------------------|
| `TOLERANCE_RP`           | `1.0`     | Rounding tolerance before `MISMATCH`           |
| `CUTOFF_WINDOW_MINUTES`  | `5`       | Window around 00:00:00 for `cut off` remark    |
| `ROW_WARN_THRESHOLD`     | 900,000   | Excel row warning threshold                    |
| `ROW_HARD_LIMIT`         | 1,048,576 | Excel hard row limit                           |

---

## 11. Usage

    python qris_recon.py 2026-09-17

The date must be in `YYYY-MM-DD` format.

### Example output (abridged)

    ==============================================================================
     QRIS RECONCILIATION - Thursday, 17 September 2026
    ==============================================================================
    [DATA STATUS] T+3 - ledger data should be final.

    --- STEP 0: Locating source files ---
      Statement : ./data/mutasi/BankStatement 17Sep2026.csv
      Dashboard : ['./data/dash/TransactionDashboard 17Sep2026_1.csv', ...]
      Ledger    : ./data/gds/GDS_<ACQUIRER>_2026-09-17.csv
    ...

---

## 12. Output Files

Written to `RECON_OUTPUT_DIR`:

| File                                       | Contents                                                       |
|--------------------------------------------|----------------------------------------------------------------|
| `<RECON_OUTPUT_PREFIX>_DD-MM-YY.xlsx`      | Exceptions-only workbook (see sheets below)                    |
| `<RECON_PARQUET_PREFIX>_DD-MM-YY.parquet`  | Full row-level 3-way match detail (query with pandas/DuckDB)   |

### Excel sheets

| Sheet                        | Purpose                                                                |
|------------------------------|------------------------------------------------------------------------|
| `Line Per Line (Exceptions)` | Rows that are not a clean settled match                                |
| `Unrecon`                    | Ledger (GDS) rows with no dashboard counterpart (with `ket` remark)          |
| `Check_Settlement`           | Dashboard-anchored batch view, one row per dashboard batch     |
| `Non Recon Trx`              | Combined `NON_RECON` + `QRIS_INDIVIDUAL` + `UNCLASSIFIED` rows         |
| `Double_Settlement_Flag`     | `NO REFERENCE` values appearing under >1 `BATCH REFERENCE`             |
| `Exact_Duplicate_Dash`       | Full-row duplicates in the dashboard (file part likely downloaded twice) |
| `Non_Succeed_Trx`            | Dashboard rows with status other than `SUCCEED`                        |

---

## 13. Troubleshooting

### `[DATA NOT FINAL]` warning appears

You are running the script for a date that has not reached T+2. This is expected
for recent days. Re-run after T+2 for final numbers. **Do not escalate based on
a provisional run.**

### Large ledger <-> statement discrepancies

Most likely the same T+2 issue. Re-download the ledger file after the date has
reached T+2 and re-run.

### `[WARNING] N statement rows did not match any known pattern`

Check the `Non Recon Trx` sheet with `Transaction_Type == "Unclassified"`.
Investigate each row manually. **Do not assume they are safe.**

### `[STOP] Sheet ... exceeds Excel hard limit`

The exception set itself has grown too large for one sheet. The script
intentionally refuses to write a partial file. Redesign the output architecture
at this point (e.g. split exceptions across multiple sheets).

### `ArrowTypeError` on `to_parquet()`

Should not happen - the ledger loader enforces `dtype=str`. If it recurs, check
whether a new column was added to the ledger export that is being cast to int
somewhere.

---

## License

This project is licensed under the **MIT License** - see the [LICENSE](LICENSE)
file for details.
