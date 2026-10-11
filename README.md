# QRIS Recon Tool

![CI](https://github.com/dikaakid/QRIS-recon-tool/actions/workflows/ci.yml/badge.svg)

Daily three-way reconciliation of QRIS acquiring settlements. It cross-checks a **bank
statement**, the **acquirer's transaction dashboard** and an **internal ledger export**, and
reports missing settlements, batch mismatches, double settlements and unmatched transactions
before they turn into escalations.

> The repository ships with **synthetic sample data** that exercises every scenario below.
> No real transaction data is included.

## Background

This started as a daily reconciliation chore: comparing a bank statement, a dashboard
export and an internal ledger by hand. The first automated version was a 900-line script
that took over half an hour to run and produced a 450 MB Excel file. This is the rewrite
that the operations team actually uses.

The three sources never share one clean key and never close at the same instant:

* the bank settles **batches** (one gross line + one fee line per batch), the dashboard lists
  **transactions** inside those batches;
* batches near midnight are posted into the *previous* or *next* day's statement;
* exports are downloaded in parts, sometimes twice, sometimes incompletely;
* the internal ledger is a live export that only stabilises around T+2.

A naive join produces either false alarms (boundary batches, partial downloads) or silent
errors (offsetting differences, repeated references). This tool is built around not doing that.

## Quick start

```bash
pip install -r requirements.txt
python tools/make_sample_data.py --out sample_data
```

PowerShell:

```powershell
$env:RECON_MUTASI_DIR = "sample_data/mutasi"; $env:RECON_DASH_DIR = "sample_data/dash"
$env:RECON_GDS_DIR    = "sample_data/gds";    $env:RECON_ACQUIRER_TAG = "SAMPLEACQ"
python qris_recon.py 2026-09-17
```

bash:

```bash
RECON_MUTASI_DIR=sample_data/mutasi RECON_DASH_DIR=sample_data/dash \
RECON_GDS_DIR=sample_data/gds RECON_ACQUIRER_TAG=SAMPLEACQ python qris_recon.py 2026-09-17
```

Output goes to `output/` (override with `RECON_OUTPUT_DIR`): an Excel workbook with the
exceptions plus a Parquet file with the full transaction-level detail. A pre-generated workbook
from the sample data is in [`examples/sample_output/`](examples/sample_output/).

`--no-lookback` skips the neighbouring-day (H-1 / H+1) lookups.

### Sample run (abridged)

```text
 QRIS RECONCILIATION - Thursday, 17 September 2026
--- STEP 2: Classifying statement rows ---
  QRIS Batch (BA-/BQ-)       : 32 rows
  QRIS Individual (individual rail): 7 rows
  Non-Recon (internal moves) : 2 rows
  Unclassified (needs review): 1 rows
  QRIS Individual pairing    : {'PAIRED': 2, 'RESOLVED_NEXTDAY': 2, 'PENDING_FEE_CROSSDAY': 1}
--- STEP 3: Batch settlement check (statement vs dashboard) ---
  [COMPLETENESS] 2 batches exist in the statement but not in the dashboard ... marked INCOMPLETE_DATA, not MISMATCH.
  [MISSING_IN_MUTASI] 1 batches are STILL not found in the statement after checking both H+1 and H-1 - MUST be escalated
    MATCH                            11
    MISMATCH                          4
    CROSSDAY_BOUNDARY_ARTIFACT        3
    INCOMPLETE_DATA                   2
    MISSING_IN_MUTASI                 1
  Settlement shortfall (genuine MISMATCH only, not INCOMPLETE_DATA): Rp51,000  |  over-settled: Rp20,000
--- STEP 4: Double settlement check ---
  [DOUBLE SETTLEMENT] 2 NO REFERENCE values appear to have settled in more than one batch WITHOUT a boundary explanation
--- STEP 5: Transaction-level 3-way match (Dash <-> GDS) ---
  Merged rows: 389
  Unrecon breakdown: {'needs review': 3, 'cut off': 1}
```

## Input files

One set per business day, downloaded into three folders. File-name matching tolerates a space
or an underscore between name and date.

| Source | File name | Format | Columns the tool needs |
|---|---|---|---|
| Bank statement | `BankStatement 17Sep2026.csv` | `\|`-separated | `NO REFERENCE`, `TRX DESC`, `NARRATIVE`, `TRANSACTION CODE`, `AMOUNT LCY`, `DATE TIME` (`dd/mm/yyyy HH:MM`) |
| Dashboard ("Dash") | `TransactionDashboard 17Sep2026[_N].csv` (may be split in parts) | `\|`-separated | `NO REFERENCE`, `BATCH REFERENCE`, `AMOUNT`, `MDR AMOUNT`, `STATUS TRANSACTION` |
| Internal ledger ("GDS") | `GDS_..._2026-09-17.csv` | comma-separated | `transaction_datetime`, `Transaction SN`, `transaction_amount` |

The ledger export sometimes leaves its leading datetime unquoted although it contains a comma;
the loader detects the quoting style from the first data row and repairs it only when needed.

## What it checks

**1. Bank-statement classification** - every row lands in exactly one bucket; nothing is dropped.

| Bucket | Rule |
|---|---|
| `QRIS_BATCH` | reference `BA-`/`BQ-`, code 903 (gross) / 904 (fee) - cross-checked against Dash |
| `QRIS_INDIVIDUAL` | reference `QR<digits>`, code 903/904 - no independent source, only paired internally |
| `NON_RECON` | description/narrative keywords or description prefixes (see *Configuration*) |
| `UNCLASSIFIED` | everything else - must be reviewed by a person |

**2. Batch settlement (bank vs dashboard).** Gross, fee and net must each agree within
`TOLERANCE_RP` (default Rp 1).

| Status | Meaning |
|---|---|
| `MATCH` | gross, fee and net agree |
| `MISMATCH` | amounts differ. Feeds the escalated figure |
| `INCOMPLETE_DATA` | batch in the bank statement but not in Dash - almost always a missing download part. Never counted as a mismatch |
| `MISSING_IN_MUTASI` | batch in Dash, absent from the statement even after checking H-1 and H+1. **Escalate.** Marked *provisional* if a neighbouring file is not available yet |
| `CROSSDAY_BOUNDARY_ARTIFACT` | found in a neighbouring day's statement **with the same amount** |

The headline figure reports **shortfall** (bank paid less than the dashboard says) and
**over-settlement** separately, on a net basis, so one cannot hide the other.

**3. QRIS individual rail.** 903 + 904 normally post together. If one leg is missing the tool
looks for *that* leg in the next day's statement; otherwise the pair stays
`PENDING_FEE_CROSSDAY`. A missing fee is never assumed to be zero.

**4. Double settlement.** One `NO REFERENCE` under more than one batch is real money until
proven otherwise. It is downgraded to `CROSSDAY_BOUNDARY_ARTIFACT` only if a midnight batch
(configured via `RECON_ACQUIRER_TAG`) also appears in the next day's Dash file; otherwise
`NEEDS_MANUAL_REVIEW`.

**5. Transaction-level match (Dash vs ledger).** Key = reference **and** amount, because
references are occasionally reused by unrelated transactions. The merge is strictly
one-to-one: a repeated key is reported as unmatched instead of multiplying rows. Each row gets a
`Match?` (`Recon`/`Unrecon`), an `Unrecon_Side` (`DASH_ONLY` / `GDS_ONLY`) and a
`Settlement_Status` (`Settled` / `Unsettled` / `N/A`) inherited from its batch.
The `Unrecon` sheet lists ledger rows with no dashboard counterpart; its `ket` column says
`cut off` when the row falls within +-5 minutes of 00:00, otherwise `needs review`.
Dashboard-only rows appear in `Line Per Line (Exceptions)`.

### Output workbook

`Line Per Line (Exceptions)` - `Unrecon` - `Check_Settlement` (dashboard-anchored batch view)
- `Non Recon Trx` - `Double_Settlement_Flag` - `Exact_Duplicate_Dash` - `Non_Succeed_Trx`.
Rows that reconcile *and* are settled are not written to Excel; they are in the Parquet file.

## Design decisions

* **Never drop silently.** Unclassified rows stay visible; exact duplicate Dash rows are listed
  on their own sheet; an unparseable amount in the bank statement or Dash export stops the run
  instead of being coerced.
* **Components and net must both agree.** Gross +500 with fee +500 leaves the net cash correct
  but is still wrong; a net-only check would call it a match.
* **A reference found in another day is not proof.** It counts as a boundary artifact only if
  the amount agrees too.
* **Sign-agnostic fee line.** The bank's fee amount is compared by absolute value.
* **Read neighbouring files once.** Lookbacks reuse a per-run cache instead of re-parsing.
* **Parquet for detail, Excel for exceptions.** Writing every row through `openpyxl` is slow and
  produces very large workbooks.
* **Provisional results are labelled.** The ledger export is live; runs on data younger than T+2
  warn that the numbers are not final.

The fixes above are covered by regression tests. [`docs/mutation-checks.md`](docs/mutation-checks.md)
lists, for each fix, how it was reverted and which test then fails.

## Configuration

Environment variables (or a `.env` file, see `.env.example`):

| Variable | Default | Purpose |
|---|---|---|
| `RECON_MUTASI_DIR` / `RECON_DASH_DIR` / `RECON_GDS_DIR` | `data/mutasi` / `data/dash` / `data/gds` | input folders |
| `RECON_OUTPUT_DIR` | `output` | where the workbook and Parquet file are written |
| `RECON_OUTPUT_PREFIX` / `RECON_PARQUET_PREFIX` | `Recon` / `LinePerLine_Full` | output file name prefixes |
| `RECON_NON_RECON_KEYWORDS` | `INTERNAL TRANSFER` | comma-separated keywords; a statement row whose description or narrative contains one is `NON_RECON` |
| `RECON_NON_RECON_PREFIXES` | `AC-,Sharing MDR` | comma-separated description prefixes that mark a row as `NON_RECON` (admin-fee lines, revenue-share settlements) |
| `RECON_ACQUIRER_TAG` | empty | tag at the end of midnight-boundary batch references (`BA-YYMMDD0000-<TAG>`). If unset, midnight-boundary detection for double settlement is disabled and a warning is printed |

Constants at the top of `qris_recon.py`: `TOLERANCE_RP`, `CUTOFF_WINDOW_MINUTES`, `QRIS_CODES`.

## Tests

```bash
pip install -r requirements-dev.txt
pytest
```

`tools/make_sample_data.py` builds a synthetic day (plus the neighbouring days the lookbacks
need) with every scenario injected on purpose: short and over-settled batches, offsetting
gross/fee errors, boundary batches (right and wrong amount), a batch absent everywhere, a missing
download part, a duplicated download part, a pending transaction, reused and repeated
references, ledger rows with a missing amount, and individual-rail pairs missing either leg.
The tests run the whole pipeline on it and assert each outcome. CI runs them on Python 3.10-3.13.

## Limitations

* One business day per run; whole files are read into memory.
* One bank account and one acquirer tag per run.
* No time-zone conversion: `transaction_datetime` is assumed to be in the same local time as
  the bank statement.
* Pairing is by (reference, amount) and first occurrence; repeated identical keys are flagged,
  not resolved.
* The +-5 minute cut-off window and the tolerance are constants, not per-merchant settings.

## Project layout

```text
qris_recon.py            the reconciliation pipeline (python qris_recon.py YYYY-MM-DD)
tools/make_sample_data.py  synthetic data generator
tests/test_recon.py      end-to-end tests on the synthetic data
sample_data/             generated sample inputs (synthetic)
examples/sample_output/  workbook produced from the sample data
docs/mutation-checks.md  evidence that the tests catch each fix being reverted
```

## License

MIT - see `LICENSE`.