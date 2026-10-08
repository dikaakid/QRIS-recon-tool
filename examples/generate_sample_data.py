"""Generate synthetic source data for the QRIS recon tool.

All names, amounts, and identifiers are fictional. Nothing here references any
real bank, acquirer, or company.
"""
from __future__ import annotations

import argparse
import random
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd


BATCH_PREFIXES = ("BA-", "BQ-")
INDIVIDUAL_PREFIX = "QR"
ACQUIRER_TAG_DEFAULT = "ACQ"


def _rng(seed: int) -> random.Random:
    return random.Random(seed)


def _fmt_token(d: datetime) -> str:
    return d.strftime("%d%b%Y")


def generate_statement(d: datetime, rng: random.Random, acquirer_tag: str) -> pd.DataFrame:
    """Generate one statement file's worth of rows."""
    rows = []
    date_token = d.strftime("%y%m%d")

    # 30 batches
    for i in range(30):
        batch_no = f"{date_token}{i:04d}"
        no_ref = f"BA-{batch_no}-{acquirer_tag}"
        gross = round(rng.uniform(50_000, 500_000), 2)
        fee = round(gross * rng.uniform(0.005, 0.015), 2)
        ts = d + timedelta(hours=rng.randint(0, 23), minutes=rng.randint(0, 59))
        rows.append({
            "NO REFERENCE": no_ref, "TRANSACTION CODE": "903",
            "AMOUNT LCY": gross, "TRX DESC": "QRIS SETTLEMENT",
            "NARRATIVE": "", "DATE TIME": ts.strftime("%d/%m/%Y %H:%M"),
        })
        rows.append({
            "NO REFERENCE": no_ref, "TRANSACTION CODE": "904",
            "AMOUNT LCY": -fee, "TRX DESC": "QRIS FEE",
            "NARRATIVE": "", "DATE TIME": ts.strftime("%d/%m/%Y %H:%M"),
        })

    # 10 individual
    for i in range(10):
        no_ref = f"QR{rng.randint(1000000, 9999999)}"
        gross = round(rng.uniform(10_000, 100_000), 2)
        fee = round(gross * rng.uniform(0.005, 0.015), 2)
        ts = d + timedelta(hours=rng.randint(0, 23), minutes=rng.randint(0, 59))
        rows.append({
            "NO REFERENCE": no_ref, "TRANSACTION CODE": "903",
            "AMOUNT LCY": gross, "TRX DESC": "INDIVIDUAL TRANSFER",
            "NARRATIVE": "", "DATE TIME": ts.strftime("%d/%m/%Y %H:%M"),
        })
        rows.append({
            "NO REFERENCE": no_ref, "TRANSACTION CODE": "904",
            "AMOUNT LCY": -fee, "TRX DESC": "INDIVIDUAL FEE",
            "NARRATIVE": "", "DATE TIME": ts.strftime("%d/%m/%Y %H:%M"),
        })

    # 5 non-recon
    for i in range(5):
        ts = d + timedelta(hours=rng.randint(0, 23))
        rows.append({
            "NO REFERENCE": f"IR{rng.randint(1000, 9999)}",
            "TRANSACTION CODE": "000",
            "AMOUNT LCY": round(rng.uniform(1_000_000, 5_000_000), 2),
            "TRX DESC": "INTERNAL TRANSFER",
            "NARRATIVE": "", "DATE TIME": ts.strftime("%d/%m/%Y %H:%M"),
        })

    return pd.DataFrame(rows)


def generate_dashboard(d: datetime, rng: random.Random, acquirer_tag: str) -> pd.DataFrame:
    """Generate dashboard rows matching statement batches."""
    rows = []
    date_token = d.strftime("%y%m%d")
    for i in range(30):
        batch_no = f"{date_token}{i:04d}"
        batch_ref = f"BA-{batch_no}-{acquirer_tag}"
        n_trx = rng.randint(1, 3)
        for j in range(n_trx):
            no_ref = f"{batch_ref}-{j:03d}"
            amount = round(rng.uniform(20_000, 150_000), 2)
            mdr = round(amount * 0.007, 2)
            rows.append({
                "NO REFERENCE": no_ref,
                "BATCH REFERENCE": batch_ref,
                "AMOUNT": amount,
                "MDR AMOUNT": mdr,
                "STATUS TRANSACTION": "SUCCEED",
            })
    return pd.DataFrame(rows)


def generate_ledger(d: datetime, rng: random.Random) -> pd.DataFrame:
    """Generate ledger rows (a subset of dashboard refs)."""
    rows = []
    date_token = d.strftime("%y%m%d")
    for i in range(30):
        batch_no = f"{date_token}{i:04d}"
        batch_ref = f"BA-{batch_no}-ACQ"
        n_trx = rng.randint(0, 2)  # fewer than dashboard to simulate recon differences
        for j in range(n_trx):
            no_ref = f"{batch_ref}-{j:03d}"
            amount = round(rng.uniform(20_000, 150_000), 2)
            rows.append({
                "Transaction SN": no_ref,
                "transaction_amount": amount,
                "transaction_datetime": (d + timedelta(hours=rng.randint(0, 23))).strftime("%d %b %Y, %H:%M:%S"),
                "settlement_amount": amount,
                "admin_fee": 0.0,
                "base_admin_fee": 0.0,
                "deduction_cost": 0.0,
            })
    return pd.DataFrame(rows)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", default="examples/dummy_data")
    parser.add_argument("--date", default="2026-09-17")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--acquirer-tag", default=ACQUIRER_TAG_DEFAULT)
    args = parser.parse_args()

    out = Path(args.output_dir)
    (out / "mutasi").mkdir(parents=True, exist_ok=True)
    (out / "dash").mkdir(parents=True, exist_ok=True)
    (out / "gds").mkdir(parents=True, exist_ok=True)

    rng = _rng(args.seed)
    d = datetime.strptime(args.date, "%Y-%m-%d")

    # Statement
    stmt = generate_statement(d, rng, args.acquirer_tag)
    stmt.to_csv(out / "mutasi" / f"BankStatement {_fmt_token(d)}.csv", sep="|", index=False)

    # Dashboard (split into 2 parts to exercise multi-file loading)
    dash = generate_dashboard(d, rng, args.acquirer_tag)
    half = len(dash) // 2
    dash.iloc[:half].to_csv(out / "dash" / f"TransactionDashboard {_fmt_token(d)}_1.csv", sep="|", index=False)
    dash.iloc[half:].to_csv(out / "dash" / f"TransactionDashboard {_fmt_token(d)}_2.csv", sep="|", index=False)

    # Ledger
    led = generate_ledger(d, rng)
    led.to_csv(out / "gds" / f"GDS_{args.acquirer_tag}_2026-09-17.csv", index=False)

    print(f"Sample data written to {out}")


if __name__ == "__main__":
    main()
