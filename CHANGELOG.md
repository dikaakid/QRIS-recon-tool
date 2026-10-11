# Changelog

## 0.1.0

Initial release.

- Three-way reconciliation between bank statement, acquirer dashboard, and internal ledger.
- Batch settlement check with midnight-boundary lookback (H-1 / H+1).
- Double-settlement detection with configurable acquirer tag.
- Transaction-level match on a (reference, amount) key.
- Excel exceptions report plus Parquet full-detail output.
- Synthetic sample data generator, end-to-end test suite, GitHub Actions CI.