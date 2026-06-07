#!/usr/bin/env python3
"""
One-time conversion of historical CSV/XLSX to data/history.json.
Run once before deploying.
Accepts 2-column (Discount;Date) or 3-column (Discount;Date;Code) CSV.
Filters to DATE_FROM onwards to avoid old policy data skewing predictions.
"""
import json
import re
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from collections import defaultdict

import pandas as pd

ROOT = Path(__file__).parent.parent
DATA_FILE = ROOT / "data" / "history.json"

CSV_FILES = [
    ROOT / "OdKarla_2025-Q2_26.csv",
]

# Only use data from this date onwards for predictions
DATE_FROM = date(2025, 1, 1)

DAYS_AHEAD = 90
RECENT_WEIGHT = 3.0   # last 90 days
MID_WEIGHT = 2.0      # 90-180 days
OLD_WEIGHT = 1.0      # older


# ─── Discount type normalization ─────────────────────────────────────────────

def discount_type_key(s):
    """
    Map exact discount string → canonical type key for grouping.
    Groups "OBŘÍ SLEVA 62% MEGAVÝPRODEJ" and "OBŘÍ SLEVA 73% MEGAVÝPRODEJ"
    into one bucket so probability isn't fragmented by changing percentages.
    """
    m = re.search(r'SLEVA \d+\s*%.+ŠTÍTKEM (.+)', s)
    if m:
        return f'SLEVA % | {m.group(1).strip()}'

    if re.search(r'SLEVA \d+\s*% NA VŠE', s):
        return 'SLEVA % NA VŠE SKLADEM'

    if re.search(r'SLEVA \d+ KČ', s):
        return 'SLEVA KČ'

    return s


# ─── Data loading ─────────────────────────────────────────────────────────────

def load_csv():
    all_rows = []
    for path in CSV_FILES:
        if not path.exists():
            continue
        try:
            if path.suffix == ".csv":
                raw = pd.read_csv(path, sep=";", header=None, encoding="utf-8-sig")
            else:
                raw = pd.read_excel(path, header=None)
            # Normalize to 3 columns regardless of source
            if raw.shape[1] >= 3:
                raw.columns = list(raw.columns[:3]) + list(raw.columns[3:])
                df = raw.iloc[:, :3].copy()
                df.columns = ["Akce", "Datum", "Kod"]
            else:
                df = raw.iloc[:, :2].copy()
                df.columns = ["Akce", "Datum"]
                df["Kod"] = ""
            all_rows.append(df)
            print(f"Loaded: {path.name} ({len(df)} rows)")
        except Exception as e:
            print(f"Skip {path.name}: {e}")

    if not all_rows:
        print("No source files found.")
        sys.exit(1)

    df = pd.concat(all_rows, ignore_index=True)
    df = df.dropna(subset=["Datum", "Akce"], how="all")
    df["Datum"] = df["Datum"].astype(str).str.strip()
    df["Akce"] = df["Akce"].astype(str).str.strip().str.upper()
    df["Kod"] = df["Kod"].fillna("").astype(str).str.strip().str.upper()
    df["Kod"] = df["Kod"].replace("NAN", "").replace("NONE", "")
    df = df[~df["Datum"].str.contains("nenalezeno", case=False, na=False)]
    df = df[df["Datum"].str.lower() != "nan"]
    df = df[df["Datum"] != ""]
    df = df[df["Akce"] != ""]
    df = df[df["Akce"].str.lower() != "nan"]
    df["Datum"] = pd.to_datetime(df["Datum"], format="%d.%m.%Y", errors="coerce")
    df = df.dropna(subset=["Datum"])
    df = df.drop_duplicates(subset=["Datum", "Akce"])
    df = df.sort_values("Datum")
    return df


# ─── Prediction computation ───────────────────────────────────────────────────

def compute_predictions(history_entries, days_ahead=DAYS_AHEAD):
    today = date.today()
    cutoff_90  = today - timedelta(days=90)
    cutoff_180 = today - timedelta(days=180)

    # Count by type_key, tracking exact variants underneath
    dow_type_weight  = defaultdict(lambda: defaultdict(float))        # dow → type_key → weight
    dow_type_variant = defaultdict(lambda: defaultdict(lambda: defaultdict(float)))  # dow → type_key → variant → weight
    dow_totals       = defaultdict(float)

    for entry in history_entries:
        try:
            d = date.fromisoformat(entry["date"])
        except Exception:
            continue
        discount = entry.get("discount", "")
        if not discount:
            continue

        # Only use entries within our analysis window
        if d < DATE_FROM:
            continue

        dow = d.weekday()
        weight = RECENT_WEIGHT if d >= cutoff_90 else MID_WEIGHT if d >= cutoff_180 else OLD_WEIGHT
        tkey = discount_type_key(discount)

        dow_type_weight[dow][tkey]           += weight
        dow_type_variant[dow][tkey][discount] += weight
        dow_totals[dow]                       += weight

    predictions = []
    for i in range(0, days_ahead + 1):
        target = today + timedelta(days=i)
        dow    = target.weekday()
        total  = dow_totals[dow]

        if total == 0:
            predictions.append({"date": target.isoformat(), "day_of_week": dow, "candidates": []})
            continue

        candidates = []
        for tkey, tw in sorted(dow_type_weight[dow].items(), key=lambda x: -x[1]):
            prob = tw / total
            if prob < 0.03:
                continue

            variants_raw = dow_type_variant[dow][tkey]
            top_variant  = max(variants_raw, key=variants_raw.get)
            variants_list = sorted(
                [{"discount": k, "weight": round(v, 1)} for k, v in variants_raw.items()],
                key=lambda x: -x["weight"]
            )[:4]

            candidates.append({
                "type_key":    tkey,
                "probability": round(prob, 4),
                "top_variant": top_variant,
                "variants":    variants_list,
            })

        predictions.append({
            "date":        target.isoformat(),
            "day_of_week": dow,
            "candidates":  candidates[:6],
        })

    return predictions


# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    df = load_csv()

    # Filter to DATE_FROM+ for history display (still store older but mark)
    df_all    = df.copy()
    df_recent = df[df["Datum"].dt.date >= DATE_FROM]

    print(f"Total rows: {len(df_all)} | From {DATE_FROM}: {len(df_recent)}")

    history = []
    for _, row in df_all.iterrows():
        kod = row["Kod"] if row["Kod"] not in ("", "NAN", "NONE") else None
        history.append({
            "date":     row["Datum"].strftime("%Y-%m-%d"),
            "discount": row["Akce"],
            "code":     kod,
            "source":   "csv_import",
        })

    # Sort newest first for UI
    history.sort(key=lambda x: x["date"], reverse=True)

    # Predictions use only DATE_FROM+ data
    predictions = compute_predictions(history)

    # Preserve existing accuracy log if history.json already exists
    existing_accuracy = {"total": 0, "correct": 0, "log": []}
    if DATA_FILE.exists():
        try:
            with open(DATA_FILE, "r", encoding="utf-8") as f:
                existing_accuracy = json.load(f).get("accuracy", existing_accuracy)
        except Exception:
            pass

    data = {
        "last_updated": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "history":      history,
        "predictions":  predictions,
        "accuracy":     existing_accuracy,
        "analysis_from": DATE_FROM.isoformat(),
    }

    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    print(f"Wrote {DATA_FILE}")
    print(f"History: {len(history)} entries | Predictions: {len(predictions)} days ahead")

    # Show sample predictions
    print("\nSample predictions:")
    for p in predictions[:3]:
        from datetime import date as ddate
        dow_names = ['Po','Út','St','Čt','Pá','So','Ne']
        print(f"  {p['date']} ({dow_names[p['day_of_week']]}):")
        for c in p['candidates'][:2]:
            print(f"    {c['probability']*100:.0f}% — {c['type_key']}")
            for v in c['variants'][:2]:
                print(f"      -> {v['discount']} (w={v['weight']})")


if __name__ == "__main__":
    main()
