# Changelog

## 0.3.0

### Fixed
* **Shortfall figure.** It summed signed gross differences, so an over-settled batch cancelled an
  under-settled one and fee differences were ignored. It is now computed on a net basis and
  shortfall and over-settlement are reported separately.
* **Crash on a missing ledger amount.** The match key cast amounts with `astype("int64")`; it now
  rounds to 2 decimals, never crashes, and reports rows without a usable amount.
* **Wrong leg in the next-day lookback.** The individual rail always searched for the fee leg
  (904); it now searches for whichever leg is missing and stores it in the right column.
* **Boundary artifacts ignored the amount.** A batch found in a neighbouring day's statement is
  only a boundary artifact if the amount agrees; otherwise it is a `MISMATCH`.
* **Two definitions of "match".** `Check_Settlement` now uses the same predicate as the batch
  status (gross, fee and net must all agree), so the two views cannot contradict each other.
* **Repeated ledger keys multiplied rows.** The Dash/ledger merge is strictly one-to-one;
  repeats are reported as unmatched ledger rows.
* **Missing output folder** is created automatically.

### Added
* `Unrecon_Side` column (`DASH_ONLY` / `GDS_ONLY`).
* Command line options (`--no-lookback`), per-run cache for neighbouring-day files.
* Synthetic sample data generator, end-to-end test suite, mutation-check log, GitHub Actions CI.
* README rewritten around the sample data, with the exact behaviour of every status.

## 0.2.0
* Initial public release.