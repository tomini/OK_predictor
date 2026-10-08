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

DAYS_AHEAD    = 14                 # only 7-day is reliable; 14 is best-effort
DATE_FROM     = date(2025, 1, 1)   # older data still shown in history, ignored by model
FIT_WINDOW    = 28                 # days used to score each candidate lag
FIT_DECAY     = 0.9                # per-day weight decay when scoring a lag (newest day = 1.0)
VOTE_POWER    = 4                  # each lag votes with weight fit**VOTE_POWER
LAGS          = list(range(2, 29))  # candidate periods in days. Weekly-only lags {7,14,21,28}
                                    # missed the strict 6-day cycle that began 2026-09-07.
                                    # Walk-forward backtest (2025-03 .. 2026-10, horizons 1/2/3/7):
                                    # best single lag W21 70.1 % -> recency-weighted vote
                                    # W28 d0.9 pw4 73.6 % (last 3 months 70.2 -> 75.3 %).
PROB_MIN, PROB_MAX = 0.25, 0.95    # clamp of displayed top probability
VARIANT_WINDOW = 120               # days back to collect % variants for a type


def today_prague() -> date:
    return datetime.now(PRAGUE_TZ).date()


# ─── Discount type normalization ─────────────────────────────────────────────

def canonical_discount(s):
    """Czech wording of a discount string. The shop once served the Slovak variant
    ("ZĽAVA 35 % NA PRODUKTY FIRST MINUTE", 2026-09-07); fold it into the Czech form so
    it neither becomes its own type nor leaks into the displayed variants."""
    s = re.sub(r'\bZ[ĽL]AVA\b', 'SLEVA', s)
    # Label without "SE ŠTÍTKEM" / "OZNAČENÉ ŠTÍTKEM" -> add it
    s = re.sub(r'(NA PRODUKTY) (?!.*ŠTÍTKEM)(\S.*)$', r'\1 SE ŠTÍTKEM \2', s)
    return s


def discount_type_key(s):
    s = canonical_discount(s)
    m = re.search(r'SLEVA \d+\s*%.+ŠTÍTKEM (.+)', s)
    if m:
        return f'SLEVA % | {m.group(1).strip()}'

    if re.search(r'SLEVA \d+\s*% NA VŠE', s):
        return 'SLEVA % NA VŠE SKLADEM'

    if re.search(r'SLEVA \d+ KČ', s):
        return 'SLEVA KČ'

    return s


def notify_discord(message: str):
    url = os.environ.get("DISCORD_WEBHOOK", "").strip()
    if not url:
        return
    try:
        requests.post(url, json={"content": message}, timeout=10)
    except Exception as e:
        print(f"Discord notify failed: {e}")


def normalize(text):
    if not text:
        return ""
    return canonical_discount(re.sub(r"\s+", " ", text.strip().upper()))


# ─── Data I/O ─────────────────────────────────────────────────────────────────

def load_data():
    if DATA_FILE.exists():
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {
        "last_updated":  None,
        "history":       [],
        "predictions":   [],
        "accuracy":      {"total": 0, "correct": 0, "log": [], "changelog": []},
        "analysis_from": DATE_FROM.isoformat(),
    }


def save_data(data):
    data["last_updated"]  = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    data["analysis_from"] = DATE_FROM.isoformat()
    # Deduplicate accuracy log — keep last entry per date
    seen = {}
    for entry in data["accuracy"]["log"]:
        seen[entry["date"]] = entry
    data["accuracy"]["log"] = list(seen.values())
    data["accuracy"]["total"]   = len(data["accuracy"]["log"])
    data["accuracy"]["correct"] = sum(1 for e in data["accuracy"]["log"] if e["correct"])
    if "changelog" not in data["accuracy"]:
        data["accuracy"]["changelog"] = []
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
        notify_discord(f"⚠️ **OdKarla prediktor** — chyba fetchování endpointu:\n```{e}```")
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
        notify_discord(f"⚠️ **OdKarla prediktor** — HTML struktura banneru se změnila, nelze naparsovat slevu.\nHTML: `{html[:300]}`")
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

def compute_predictions(history_entries, days_ahead=DAYS_AHEAD, as_of=None):
    """Recency-weighted lag vote.

    The schedule is a rotating cycle whose period changes every month or two, so each
    candidate lag P (2..28 days) is scored on how often "type P days ago" matched the type
    on the day over the last FIT_WINDOW days, with newer days counting more (FIT_DECAY).
    The prediction for a day is the type with the highest fit**VOTE_POWER-weighted vote
    across all lags (ties go to the shorter lag). Days beyond the last observed one use
    earlier predictions as lag sources (rollout), so a lag shorter than the horizon still
    votes. Displayed probability = the top type's vote share (backtest: mean 67 % shown vs
    68-76 % hit rate for horizons 1-14, i.e. roughly calibrated), clamped to PROB_MIN..MAX.
    """
    today = as_of or today_prague()   # as_of lets us replay the model at a past date

    by_date = {}   # date -> raw discount string (latest wins)
    for entry in history_entries:
        try:
            d = date.fromisoformat(entry["date"])
        except Exception:
            continue
        discount = entry.get("discount", "")
        if discount and DATE_FROM <= d <= today:
            by_date[d] = canonical_discount(discount)

    tkeys = {d: discount_type_key(disc) for d, disc in by_date.items()}

    # Fit of each lag as of today: decayed share of recent days whose type matched the
    # type P days earlier. Needs >= 4 comparable days, else the lag does not vote.
    lag_fit = {}
    for P in LAGS:
        num = den = 0.0
        n = 0
        for k in range(FIT_WINDOW):
            d = today - timedelta(days=k)
            src = d - timedelta(days=P)
            if d in tkeys and src in tkeys:
                w = FIT_DECAY ** k
                den += w
                n += 1
                if tkeys[d] == tkeys[src]:
                    num += w
        lag_fit[P] = (num / den) if n >= 4 else 0.0

    # Recent % variants per type key, for the sub-line under each candidate.
    variant_weight = defaultdict(lambda: defaultdict(float))
    for d, disc in by_date.items():
        if (today - d).days > VARIANT_WINDOW:
            continue
        variant_weight[tkeys[d]][disc] += 1.0

    series = dict(tkeys)   # observed types; rolled-out predictions are added as we go
    predictions = []
    for i in range(0, days_ahead + 1):
        target = today + timedelta(days=i)

        votes = defaultdict(float)
        for P in LAGS:                       # ascending: shorter lag wins vote ties
            ty = series.get(target - timedelta(days=P))
            if ty is None:
                continue
            votes[ty] += lag_fit[P] ** VOTE_POWER

        total = sum(votes.values())
        if not votes or total <= 0:
            predictions.append({"date": target.isoformat(),
                                 "day_of_week": target.weekday(), "candidates": []})
            continue

        order = sorted(votes, key=lambda t: -votes[t])   # stable: insertion (short lag) first
        if target not in tkeys:
            series[target] = order[0]
        prob = {t: votes[t] / total for t in order}
        prob[order[0]] = min(PROB_MAX, max(PROB_MIN, prob[order[0]]))
        rest_total = sum(votes[t] for t in order[1:])
        for t in order[1:]:
            prob[t] = (1.0 - prob[order[0]]) * (votes[t] / rest_total) if rest_total else 0.0

        candidates = []
        for tkey in order:
            vw = variant_weight.get(tkey, {})
            variants_list = sorted(
                [{"discount": k, "weight": round(v, 1)} for k, v in vw.items()],
                key=lambda x: -x["weight"]
            )[:4]
            top_variant = variants_list[0]["discount"] if variants_list else tkey
            candidates.append({
                "type_key":    tkey,
                "probability": round(prob[tkey], 4),
                "top_variant": top_variant,
                "variants":    variants_list,
            })

        predictions.append({
            "date":        target.isoformat(),
            "day_of_week": target.weekday(),
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
            # Warn if discount doesn't match any known normalization pattern
            if discount_type_key(discount) == discount:
                notify_discord(f"ℹ️ **OdKarla prediktor** — neznámý formát slevy (nesedí žádná normalizace):\n`{discount}`\nZkontroluj `discount_type_key()` v update_data.py.")
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
