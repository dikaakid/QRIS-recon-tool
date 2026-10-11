"""Synthetic sample-data generator for qris_recon.py.

Writes three folders (mutasi/, dash/, gds/) of fake-but-realistic export files for one
business day plus the neighbouring days the lookback logic needs. Every reconciliation
scenario the tool handles is injected on purpose, so the output of a run can be checked
by eye and by the test-suite:

    python tools/make_sample_data.py --out sample_data
    set RECON_MUTASI_DIR=sample_data/mutasi  (and DASH / GDS)  ->  python qris_recon.py 2026-09-17

Nothing here is real data. Amounts, references and merchants are random (fixed seed).
"""
import argparse
import csv
import itertools
import random
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

TAG = "SAMPLEACQ"            # fake acquirer tag, ends every batch reference
MDR_RATE = 0.007
MERCHANTS = ["M-ALPHA", "M-BRAVO", "M-CHARLIE"]
NORMAL_BATCH_TIMES = ["0105", "0630", "0915", "1030", "1145", "1645", "1800", "1915", "2030", "2145"]

MUTASI_COLS = ["ACCOUNT NO", "DATE TIME", "NO REFERENCE", "TRX DESC", "NARRATIVE",
               "TRANSACTION CODE", "AMOUNT LCY", "CURRENCY", "BALANCE", "BRANCH"]
DASH_COLS = ["NO REFERENCE", "BATCH REFERENCE", "MERCHANT NAME", "AMOUNT", "MDR AMOUNT",
             "STATUS TRANSACTION", "TRANSACTION TIME"]
GDS_COLS = ["transaction_datetime", "Transaction SN", "merchant_id", "transaction_amount",
            "admin_fee", "settlement_amount", "status"]


def _batch_ref(day, hhmm):
    return f"BA-{day:%y%m%d}{hhmm}-{TAG}"


def _tok(d):                       # 17Sep2026 - used in Mutasi / Dash file names
    return d.strftime("%d%b%Y")


def _gds_dt(ts):                   # '17 Sept 2026, 10:15:00' - the export writes "Sept" for September
    text = f"{ts.day} {ts.strftime('%b')} {ts.year}, {ts:%H:%M:%S}"
    return text.replace(" Sep ", " Sept ")


def build(out_dir, day=date(2026, 9, 17), seed=7, non_recon_keyword="INTERNAL TRANSFER", non_recon_prefix="AC-"):
    """Create the sample files under out_dir and return a dict describing what was injected.

    non_recon_keyword / non_recon_prefix must match the NON_RECON_* settings of qris_recon.py so the
    "internal transfer" and "admin fee" rows are classified as NON_RECON rather than UNCLASSIFIED."""
    rnd = random.Random(seed)
    out = Path(out_dir)
    for sub in ("mutasi", "dash", "gds"):
        (out / sub).mkdir(parents=True, exist_ok=True)

    d0 = datetime(day.year, day.month, day.day)
    prev_day, next_day = d0 - timedelta(days=1), d0 + timedelta(days=1)
    seq = itertools.count(1)
    trx = []

    def add(batch, ts, amount=None, ref=None, merchant=None, status="SUCCEED", in_gds=True):
        amount = amount if amount is not None else rnd.randrange(10, 500) * 500
        rec = {"ref": ref or f"RRN{next(seq):08d}", "batch": batch, "ts": ts, "amount": amount,
               "mdr": round(amount * MDR_RATE), "merchant": merchant or rnd.choice(MERCHANTS),
               "status": status, "in_gds": in_gds}
        trx.append(rec)
        return rec

    def at(hhmm, jitter=0):
        minutes = max(int(hhmm[:2]) * 60 + int(hhmm[2:]) - rnd.randint(0, jitter), 0)
        return d0 + timedelta(minutes=minutes)

    def fill(hhmm, n=25):
        ref = _batch_ref(d0, hhmm)
        for _ in range(n):
            add(ref, at(hhmm, 4))
        return ref

    refs = {"normal": [fill(t) for t in NORMAL_BATCH_TIMES]}

    # --- scenarios that live in the Dash/GDS files -------------------------------------
    refs["short"] = fill("1100")                       # bank paid LESS than the dashboard says
    refs["over"] = fill("1200")                        # bank paid MORE
    refs["offset"] = fill("1245")                      # gross +500 AND fee +500: net cash is right, parts are wrong
    refs["gap"] = fill("1400", 12)                     # in Dash, in no Mutasi file at all
    refs["pending"] = fill("1500", 10)                 # one PENDING trx must not count
    add(refs["pending"], at("1500", 4), amount=77_000, status="PENDING")
    refs["early"] = fill("0005", 12)                   # 00:05 batch, posted in the PREVIOUS day's Mutasi
    refs["early_off"] = fill("0020", 8)                # same, but the bank paid 1,000 LESS
    refs["late"] = fill("2355", 12)                    # 23:55 batch, posted in the NEXT day's Mutasi
    refs["incomplete"] = _batch_ref(d0, "1330")        # in Mutasi only (a Dash part is "missing")
    refs["tomorrow_early"] = _batch_ref(next_day, "0005")   # tomorrow's batch inside today's Mutasi
    refs["midnight_next"] = _batch_ref(next_day, "0000")

    add(refs["normal"][1], at("0630", 4), amount=40_000, ref="DUPREF1")      # real double settlement
    add(refs["normal"][4], at("1145", 4), amount=40_000, ref="DUPREF1", in_gds=False)
    add(refs["normal"][2], at("0915", 4), amount=10_000, ref="REUSE1", merchant="M-ALPHA")   # reference reuse,
    add(refs["normal"][5], at("1645", 4), amount=25_000, ref="REUSE1", merchant="M-BRAVO")   # different amount
    add(refs["normal"][3], at("1030", 4), amount=12_000, ref="RRNDASHONLY", in_gds=False)    # Dash-only trx
    add(refs["late"], at("2355"), amount=33_000, ref="MIDREF1")                             # midnight boundary:
    add(refs["midnight_next"], at("2355"), amount=33_000, ref="MIDREF1", in_gds=False)       # listed under 2 batches

    # --- Mutasi ------------------------------------------------------------------------
    mutasi = {prev_day: [], d0: [], next_day: []}

    def mrow(when, ref, code, amount, desc="QRIS SETTLEMENT", narrative=""):
        mutasi[when.replace(hour=0, minute=0)].append({
            "ACCOUNT NO": "0000000001", "DATE TIME": when.strftime("%d/%m/%Y %H:%M"),
            "NO REFERENCE": ref, "TRX DESC": desc, "NARRATIVE": narrative,
            "TRANSACTION CODE": code, "AMOUNT LCY": amount, "CURRENCY": "IDR",
            "BALANCE": 0, "BRANCH": "001"})

    def settle(batch, when, gross_delta=0, gross=None, fee=None, fee_delta=0):
        ok = [t for t in trx if t["batch"] == batch and t["status"] == "SUCCEED"]
        g = (gross if gross is not None else sum(t["amount"] for t in ok)) + gross_delta
        f = (fee if fee is not None else sum(t["mdr"] for t in ok)) + fee_delta
        mrow(when, batch, "903", g)
        mrow(when, batch, "904", -f)

    def posted(hhmm, plus=1):
        return d0 + timedelta(hours=int(hhmm[:2]), minutes=int(hhmm[2:]) + plus)

    for ref in refs["normal"] + [refs["pending"]]:
        settle(ref, posted(ref[9:13]))
    settle(refs["short"], posted("1100"), gross_delta=-50_000)
    settle(refs["over"], posted("1200"), gross_delta=+20_000)
    settle(refs["offset"], posted("1245"), gross_delta=+500, fee_delta=+500)
    settle(refs["early"], prev_day.replace(hour=23, minute=59))
    settle(refs["early_off"], prev_day.replace(hour=23, minute=58), gross_delta=-1_000)
    settle(refs["late"], next_day.replace(hour=0, minute=1))
    settle(refs["midnight_next"], next_day.replace(hour=0, minute=2))
    settle(refs["incomplete"], posted("1330"), gross=200_000, fee=1_400)
    settle(refs["tomorrow_early"], d0.replace(hour=23, minute=59), gross=150_000, fee=1_050)

    # ATM-style individual rail: 903 = gross, 904 = fee, one reference per transaction
    for ref, g, f in (("QR1000001", 100_000, 700), ("QR1000002", 50_000, 350)):
        mrow(posted("1010"), ref, "903", g)
        mrow(posted("1010"), ref, "904", -f)
    mrow(posted("1020"), "QR9000003", "904", -1_400)                       # 903 leg arrives tomorrow
    mrow(next_day.replace(hour=0, minute=3), "QR9000003", "903", 200_000)
    mrow(posted("1030"), "QR9000004", "903", 80_000)                       # 904 leg arrives tomorrow
    mrow(next_day.replace(hour=0, minute=3), "QR9000004", "904", -560)
    mrow(posted("1040"), "QR9000005", "903", 60_000)                       # never completed
    mrow(posted("1500"), "TRF0001", "100", -5_000_000, "TRANSFER TO OPS", f"{non_recon_keyword} 0001")
    mrow(posted("1510"), "ADM0001", "200", -2_500, f"{non_recon_prefix}ADMIN FEE", "")
    mrow(posted("1520"), "ZZ0001", "555", -1_000, "UNKNOWN CHARGE", "")

    # --- GDS-only anomalies ------------------------------------------------------------
    gds_extra = [
        {"sn": "GDSONLY01", "ts": d0.replace(hour=23, minute=58), "amount": "15000"},   # near midnight -> "cut off"
        {"sn": "GDSONLY02", "ts": d0.replace(hour=12, minute=30), "amount": "20000"},   # -> "needs review"
        {"sn": "GDSNOAMT01", "ts": d0.replace(hour=9), "amount": ""},                   # missing amount
    ]
    first = next(t for t in trx if t["batch"] == refs["normal"][0])
    gds_extra.append({"sn": first["ref"], "ts": d0.replace(hour=14), "amount": str(first["amount"])})  # duplicate key

    # --- write files -------------------------------------------------------------------
    for when, rows in mutasi.items():
        path = out / "mutasi" / f"BankStatement {_tok(when)}.csv"
        with open(path, "w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=MUTASI_COLS, delimiter="|", lineterminator="\n")
            w.writeheader()
            w.writerows(rows)

    def dash_row(t):
        return {"NO REFERENCE": t["ref"], "BATCH REFERENCE": t["batch"], "MERCHANT NAME": t["merchant"],
                "AMOUNT": t["amount"], "MDR AMOUNT": t["mdr"], "STATUS TRANSACTION": t["status"],
                "TRANSACTION TIME": t["ts"].strftime("%Y-%m-%d %H:%M:%S")}

    rows = [dash_row(t) for t in trx if t["batch"] != refs["incomplete"]]
    half = len(rows) // 2
    parts = [rows[:half], rows[half:] + [rows[half - 1]]]       # last row of part 1 downloaded twice
    for n, part in enumerate(parts, start=1):
        path = out / "dash" / f"TransactionDashboard {_tok(d0)}_{n}.csv"
        with open(path, "w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=DASH_COLS, delimiter="|", lineterminator="\n")
            w.writeheader()
            w.writerows(part)

    nxt_rows = [dash_row(t) for t in trx if t["batch"] == refs["midnight_next"]]
    for i in range(3):
        nxt_rows.append({"NO REFERENCE": f"RRN9{i:07d}", "BATCH REFERENCE": refs["midnight_next"],
                         "MERCHANT NAME": "M-ALPHA", "AMOUNT": 20_000, "MDR AMOUNT": 140,
                         "STATUS TRANSACTION": "SUCCEED", "TRANSACTION TIME": f"{next_day:%Y-%m-%d} 00:01:00"})
    with open(out / "dash" / f"TransactionDashboard {_tok(next_day)}.csv", "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=DASH_COLS, delimiter="|", lineterminator="\n")
        w.writeheader()
        w.writerows(nxt_rows)

    gds_rows = [[_gds_dt(t["ts"]), t["ref"], f"MID{MERCHANTS.index(t['merchant']) + 1:03d}", t["amount"],
                 t["mdr"], t["amount"] - t["mdr"], t["status"]] for t in trx if t["in_gds"]]
    gds_rows += [[_gds_dt(g["ts"]), g["sn"], "MID001", g["amount"], "", "", "SUCCEED"] for g in gds_extra]
    with open(out / "gds" / f"GDS_QRIS_{day:%Y-%m-%d}.csv", "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh, lineterminator="\n")
        w.writerow(GDS_COLS)
        w.writerows(gds_rows)

    return {
        "day": day, "tag": TAG, "refs": refs,
        "short_amount": 50_000 + 1_000,      # short batch + the boundary batch paid 1,000 less
        "over_amount": 20_000,
        "n_gap_trx": sum(1 for t in trx if t["batch"] == refs["gap"]),
        "n_dash_rows": len(trx) - sum(1 for t in trx if t["batch"] == refs["incomplete"]),
        "n_gds_rows": len(gds_rows),
        # GDS_ONLY count = unique keys contributed by gds_extra that are not
        # already accounted for by a Dash row. One gds_extra entry may share a
        # key with a trx-derived GDS row (intentional, to exercise GDS dedup),
        # in which case it is not GDS_ONLY after dedup.
        "n_gds_only": len(
            {f"{g['sn']}|{g['amount']}" for g in gds_extra}
            - {f"{t['ref']}|{t['amount']}" for t in trx if t["batch"] != refs["incomplete"]}
        ),
    }


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Generate synthetic QRIS reconciliation sample files.")
    ap.add_argument("--out", default="sample_data", help="output folder (default: sample_data)")
    ap.add_argument("--date", default="2026-09-17", help="business date YYYY-MM-DD")
    ap.add_argument("--non-recon-keyword", help="override; default is read from qris_recon.NON_RECON_KEYWORDS")
    ap.add_argument("--non-recon-prefix", help="override; default is read from qris_recon.NON_RECON_DESC_PREFIXES")
    args = ap.parse_args()

    keyword, prefix = args.non_recon_keyword, args.non_recon_prefix
    if keyword is None or prefix is None:                 # follow the tool's own configuration
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
        import qris_recon
        keyword = keyword or qris_recon.NON_RECON_KEYWORDS[0]
        prefix = prefix or qris_recon.NON_RECON_DESC_PREFIXES[0]

    meta = build(args.out, date.fromisoformat(args.date), non_recon_keyword=keyword, non_recon_prefix=prefix)
    print(f"Sample files written to {args.out}/  (acquirer tag: {meta['tag']})")