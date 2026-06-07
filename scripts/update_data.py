#!/usr/bin/env python3
"""
Hourly runner (GitHub Actions): fetch live endpoint, update history.json, recompute predictions.
Manual entries via MANUAL_DATE / MANUAL_DISCOUNT / MANUAL_CODE env vars (workflow_dispatch).
"""
import json
import os
import re
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from collections import defaultdict

import requests
from bs4 import BeautifulSoup
from zoneinfo import ZoneInfo

ROOT      = Path(__file__).parent.parent
DATA_FILE = ROOT / "data" / "history.json"
ENDPOINT  = "https://www.odkarla.cz/HeaderPromo/jsHeaderPromoSecret"
PRAGUE_TZ = ZoneInfo("Europe/Prague")

DAYS_AHEAD    = 30
DATE_FROM     = date(2025, 1, 1)   # only this window used for prediction weights
RECENT_WEIGHT = 3.0
MID_WEIGHT    = 2.0
OLD_WEIGHT    = 1.0


def today_prague() -> date:
    return datetime.now(PRAGUE_TZ).date()


# ─── Discount type normalization ─────────────────────────────────────────────

def discount_type_key(s):
    m = re.search(r'SLEVA \d+\s*%.+ŠTÍTKEM (.+)', s)
    if m:
        return f'SLEVA % | {m.group(1).strip()}'

    if re.search(r'SLEVA \d+\s*% NA VŠE', s):
        return 'SLEVA % NA VŠE SKLADEM'

    if re.search(r'SLEVA \d+ KČ', s):
        return 'SLEVA KČ'

    return s


def normalize(text):
    if not text:
        return ""
    return re.sub(r"\s+", " ", text.strip().upper())


# ─── Data I/O ─────────────────────────────────────────────────────────────────

def load_data():
    if DATA_FILE.exists():
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {
        "last_updated":  None,
        "history":       [],
        "predictions":   [],
        "accuracy":      {"total": 0, "correct": 0, "log": []},
        "analysis_from": DATE_FROM.isoformat(),
    }


def save_data(data):
    data["last_updated"]  = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    data["analysis_from"] = DATE_FROM.isoformat()
    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


# ─── Endpoint fetch ───────────────────────────────────────────────────────────

def fetch_current_code():
    """Returns (discount_str, code_str) or (None, None)."""
    try:
        r = requests.get(ENDPOINT, timeout=15)
        r.raise_for_status()
        payload = r.json()
    except Exception as e:
        print(f"Fetch error: {e}")
        return None, None

    d = payload.get("data", {})
    if not d.get("isSecretCode"):
        print("No secret code active.")
        return None, None

    html = d.get("html", "")
    soup = BeautifulSoup(html, "html.parser")
    heading = soup.find(class_="lp-special-action-text-heading")
    if not heading:
        print(f"Cannot parse heading. HTML: {html[:200]}")
        return None, None

    strongs  = heading.find_all("strong")
    discount = None
    code     = None

    for s in strongs:
        parent_classes = s.parent.get("class", []) if s.parent else []
        if "enter-code" in parent_classes:
            code = s.get_text(strip=True)
        else:
            discount = normalize(s.get_text(strip=True))

    if not discount:
        full = heading.get_text(" ", strip=True)
        full = re.sub(r"Zadejte kód:\s*\S+", "", full, flags=re.IGNORECASE).strip()
        discount = normalize(full)

    # Strip endpoint-specific prefix not present in historical FB data
    if discount:
        discount = re.sub(r'^TAJNÝ KÓD JEN PRO VÁS\.?\s*', '', discount).strip()

    return discount, code


# ─── Prediction computation ───────────────────────────────────────────────────

def compute_predictions(history_entries, days_ahead=DAYS_AHEAD):
    today      = today_prague()
    cutoff_90  = today - timedelta(days=90)
    cutoff_180 = today - timedelta(days=180)

    dow_type_weight  = defaultdict(lambda: defaultdict(float))
    dow_type_variant = defaultdict(lambda: defaultdict(lambda: defaultdict(float)))
    dow_totals       = defaultdict(float)

    for entry in history_entries:
        try:
            d = date.fromisoformat(entry["date"])
        except Exception:
            continue
        if d < DATE_FROM:
            continue
        discount = entry.get("discount", "")
        if not discount:
            continue

        dow    = d.weekday()
        weight = RECENT_WEIGHT if d >= cutoff_90 else MID_WEIGHT if d >= cutoff_180 else OLD_WEIGHT
        tkey   = discount_type_key(discount)

        dow_type_weight[dow][tkey]            += weight
        dow_type_variant[dow][tkey][discount] += weight
        dow_totals[dow]                        += weight

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


# ─── Accuracy tracking ────────────────────────────────────────────────────────

def check_accuracy(data, date_str, actual_discount):
    pred = next((p for p in data.get("predictions", []) if p["date"] == date_str), None)
    if not pred or not pred.get("candidates"):
        return

    top_type     = pred["candidates"][0]["type_key"]
    actual_type  = discount_type_key(normalize(actual_discount))
    correct      = top_type == actual_type

    data["accuracy"]["total"] = data["accuracy"].get("total", 0) + 1
    if correct:
        data["accuracy"]["correct"] = data["accuracy"].get("correct", 0) + 1

    data["accuracy"]["log"].append({
        "date":          date_str,
        "predicted_type": top_type,
        "predicted":     pred["candidates"][0].get("top_variant", ""),
        "actual":        normalize(actual_discount),
        "correct":       correct,
    })
    print(f"Accuracy {date_str}: type_match={correct} | predicted={top_type} | actual={actual_type}")


# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    data      = load_data()
    today_str = today_prague().isoformat()

    # --- Manual entry via workflow_dispatch ---
    manual_date     = os.environ.get("MANUAL_DATE", "").strip()
    manual_discount = os.environ.get("MANUAL_DISCOUNT", "").strip()
    manual_code     = os.environ.get("MANUAL_CODE", "").strip()

    if manual_date and manual_discount:
        existing = next((e for e in data["history"] if e["date"] == manual_date), None)
        norm_discount = normalize(manual_discount)
        if existing:
            print(f"Manual: updating {manual_date}")
            existing["discount"] = norm_discount
            if manual_code:
                existing["code"] = manual_code.upper()
            existing["source"] = "manual"
        else:
            print(f"Manual: adding {manual_date}")
            check_accuracy(data, manual_date, norm_discount)
            data["history"].append({
                "date":     manual_date,
                "discount": norm_discount,
                "code":     manual_code.upper() if manual_code else None,
                "source":   "manual",
            })
        data["history"].sort(key=lambda x: x["date"], reverse=True)
        data["predictions"] = compute_predictions(data["history"])
        save_data(data)
        print(f"Saved. Total: {len(data['history'])}")
        return

    # --- Automatic endpoint fetch ---
    existing_today = next((e for e in data["history"] if e["date"] == today_str), None)
    discount, code = fetch_current_code()

    if discount:
        if not existing_today:
            print(f"New entry: {discount} | code={code}")
            check_accuracy(data, today_str, discount)
            data["history"].append({
                "date":     today_str,
                "discount": discount,
                "code":     code,
                "source":   "endpoint",
            })
            data["history"].sort(key=lambda x: x["date"], reverse=True)
        else:
            if code and not existing_today.get("code"):
                existing_today["code"] = code
                print(f"Updated code: {code}")
            else:
                print(f"Already recorded: {existing_today['discount']}")
    else:
        print("No active secret code.")

    data["predictions"] = compute_predictions(data["history"])
    save_data(data)
    print(f"Done. History: {len(data['history'])}, Predictions: {len(data['predictions'])}")


if __name__ == "__main__":
    main()
