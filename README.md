# OdKarla – Prediktor slev

Statická webová aplikace (GitHub Pages) předpovídající tajné slevové akce e-shopu [OdKarla.cz](https://www.odkarla.cz) na základě historických dat. Data se automaticky doplňují přes GitHub Actions.

---

## Jak to funguje

### Datový model

Veškerá data jsou uložena v jednom souboru `data/history.json`, který GitHub Actions commituje přímo do repozitáře. GitHub Pages ho pak servírují jako statický soubor — žádný backend, žádná databáze.

Struktura souboru:
```
{
  "last_updated": "2026-06-07T10:00:00Z",
  "history": [{ "date", "discount", "code", "source" }, ...],
  "predictions": [{ "date", "day_of_week", "candidates": [{ "type_key", "probability", "top_variant", "variants" }] }, ...],
  "accuracy": { "total", "correct", "log": [...] }
}
```

---

### Automatické plnění dat

GitHub Actions spouští `scripts/update_data.py` každou hodinu (cron `0 * * * *`).

Skript dotazuje endpoint:
```
https://www.odkarla.cz/HeaderPromo/jsHeaderPromoSecret
```

Odpověď obsahuje pole `data.isSecretCode`:
- **`true`** — právě běží denní tajná akce. Skript naparsuje HTML (`lp-special-action-text-heading`) a extrahuje popis slevy a kód.
- **`false`** — aktivní je jen permanentní kód (např. `SLE25NVK`). Skript nic nezapisuje.

Pokud byl pro dnešní datum záznam již uložen, skript ho nepřepíše (pouze doplní kód, pokud chybí). Při nové položce se automaticky vyhodnotí přesnost dřívější predikce.

Po každém běhu se přepočítají predikce a pokud se `history.json` změnil, Actions ho commituje (`github-actions[bot]`) a pushne.

---

### Ruční zadání (korekce / doplnění)

Přes záložku **Actions → Update discount data → Run workflow** na GitHubu. Vstupy:

| Pole | Formát | Popis |
|---|---|---|
| `manual_date` | `YYYY-MM-DD` | Datum akce |
| `manual_discount` | text | Popis slevy (např. `SLEVA 30 % NA VŠE SKLADEM`) |
| `manual_code` | text | Slevový kód (např. `KOD30VSE`) |

Při zadání `manual_date` + `manual_discount` skript zkontroluje, zda záznam pro dané datum existuje:
- **Existuje** → přepíše popis a kód.
- **Neexistuje** → přidá nový záznam a vyhodnotí přesnost predikce pro dané datum.

Hodí se na doplnění zmeškané akce nebo opravu špatně naparsovaného textu.

---

### Predikce

Predikce jsou generovány pro dnešní den i 30 dní dopředu. Základ je **vážená frekvence podle dne v týdnu**:

- Posledních 90 dní → váha 3×
- 90–180 dní → váha 2×
- Starší → váha 1×

Analýza začíná od **1. 1. 2025** (starší data ze jiné obchodní politiky nejsou do výpočtu zahrnuta, ale zobrazují se v historii).

#### Normalizace typů slev

Různé procentní výše stejné akce (např. `OBŘÍ SLEVA 62 %...` a `OBŘÍ SLEVA 73 %...`) se seskupují pod jeden klíč, aby se pravděpodobnost neroztříštila:

| Vzor v textu | Typ klíč |
|---|---|
| `SLEVA N %...ŠTÍTKEM X` | `SLEVA % \| X` |
| `SLEVA N % NA VŠE` | `SLEVA % NA VŠE SKLADEM` |
| `SLEVA N KČ` | `SLEVA KČ` |
| ostatní | původní text |

Predikční karta zobrazuje klíč (`SLEVA % | MEGAVÝPRODEJ`) s podřádkem procentních variant (`62 % · 73 %`).

---

### Sledování přesnosti

Každý nově přidaný záznam (automaticky i ručně) se porovná s predikcí pro dané datum. Srovnává se na úrovni **typu klíče** (ne přesného textu). Výsledky jsou viditelné v záložce **Přesnost**.

Na záložce **Dnes** se přímo pod aktuálním kódem zobrazuje, zda predikce pro dnešek vyšla.

---

### Sběr historických dat (UserScript)

Historická data byla sesbírána ze skupiny OdKarla na Facebooku pomocí UserScriptu (`FB OdKarla Extractor (Průběžný)-2.0.txt`, spouštěn přes Tampermonkey/Greasemonkey).

Skript prochází příspěvky skupiny a pro každý extrahuje:
- Popis slevy (nadpis z textu příspěvku)
- Datum (řádek obsahující „platí")
- Slevový kód (regex `zadejte v košíku kód\s+([A-Za-z0-9]+)`)

Výstup je tab-separovaný text (`Sleva\tDatum\tKód`) určený ke zkopírování do CSV.

---

### Jednorázový import CSV

Soubor `scripts/convert_csv.py` převede CSV do `data/history.json` včetně přepočtu predikcí. Spouští se lokálně před prvním nasazením nebo po větší aktualizaci historických dat.

```bash
pip install -r scripts/requirements.txt
python scripts/convert_csv.py
```

CSV formát: `Sleva;Datum;Kód` (oddělovač `;`, kódování UTF-8 s BOM, datum `DD.MM.YYYY`).
Chronologické pořadí řádků nehraje roli — skript řadí automaticky.

---

## Workflow – běžné situace

### Normální provoz (nic nedělat)

Actions běží každou hodinu automaticky. Nový záznam se uloží do hodiny po tom, co OdKarla kód zveřejní (obvykle těsně po půlnoci CEST). Datum se přiřazuje vždy podle pražského času.

---

### Dnešní kód nebyl zachycen (okno zmeškat / výpadek)

Kód přestal být aktivní dřív než cron dobíhal, nebo Actions selhaly.

1. GitHub → **Actions → Update discount data → Run workflow**
2. Vyplň:
   - `manual_date`: dnešní datum (`YYYY-MM-DD`)
   - `manual_discount`: popis slevy přesně jak byl na webu (velkými písmeny)
   - `manual_code`: slevový kód
3. Spusť. Záznam se přidá a vyhodnotí přesnost predikce.

> Kde vzít popis: FB skupina OdKarla, email nebo přímo web v den akce.

---

### Oprava existujícího záznamu (špatný popis nebo kód)

Workflow_dispatch s **stejným datem** jako opravovaný záznam — skript existující záznam přepíše.

1. Actions → Run workflow
2. Vyplň `manual_date` + správný `manual_discount` + správný `manual_code`
3. Spusť.

> Pokud opravuješ jen kód a popis je správný, musíš vyplnit i popis — jinak ho skript přepíše prázdným.

---

### Doplnění více záznamů najednou (backfill, rok 2024 apod.)

Workflow_dispatch je pro hromadné doplňování nepraktický (jeden záznam za spuštění). Místo toho:

1. Otevři `OdKarla_2025-Q2_26.csv` v editoru nebo Excelu.
2. Přidej řádky ve formátu `Sleva;DD.MM.YYYY;KOD` — chronologické pořadí nehraje roli, skript řadí sám.
3. Ulož jako UTF-8 s BOM (v Excelu: Uložit jako → CSV UTF-8 s kusovníkem).
4. Lokálně spusť:
   ```bash
   python scripts/convert_csv.py
   ```
5. Commitni a pushni `data/history.json` (a případně upravené CSV):
   ```bash
   git add data/history.json OdKarla_2025-Q2_26.csv
   git commit -m "data: backfill ..."
   git push
   ```

> Accuracy log se při reimportu **zachová** — existující vyhodnocení predikcí se nepřepíší.

---

### Sběr nových historických dat přes UserScript

Pokud chceš doplnit starší záznamy ze FB skupiny, které v CSV chybí:

1. Nainstaluj UserScript `FB OdKarla Extractor (Průběžný)-2.0.txt` do Tampermonkey.
2. Otevři FB skupinu OdKarla, procházej příspěvky — skript průběžně sbírá.
3. Na konci klikni na tlačítko v UI — zkopíruje tab-separovaný výstup (`Sleva\tDatum\tKód`).
4. Vlož do CSV (přidej řádky do `OdKarla_2025-Q2_26.csv`), pak viz backfill postup výše.

---

### Predikce jsou zkreslené / chci přepočítat

Stačí znovu lokálně spustit `python scripts/convert_csv.py` a pushnout `data/history.json`. Actions přepočítají predikce také automaticky při každém hodinovém běhu.

---

### Actions přestaly fungovat / repozitář byl přenesen

Zkontroluj:
- **Settings → Actions → General** → `Read and write permissions` musí být povoleno
- **Settings → Pages** → source nastaveno na `main` / `/ (root)`
- Workflow `update.yml` existuje v `.github/workflows/`

---

## Nasazení

1. Forkni / pushni repozitář na GitHub (privátní nebo veřejný).
2. **Settings → Pages** — nastav zdroj na `main` / `/ (root)`.
3. **Settings → Actions → General** — povol `Read and write permissions`.
4. Spusť `convert_csv.py` lokálně, výsledný `data/history.json` commituj a pushni.
5. Actions se spustí automaticky každou hodinu.

---

## Struktura repozitáře

```
├── index.html                  # SPA frontend
├── data/
│   └── history.json            # živá databáze (generováno)
├── scripts/
│   ├── convert_csv.py          # jednorázový import CSV
│   ├── update_data.py          # hodinový runner (Actions)
│   └── requirements.txt
├── .github/workflows/
│   └── update.yml              # GitHub Actions definice
└── FB OdKarla Extractor (Průběžný)-2.0.txt   # UserScript pro sběr dat
```
