# wg-luzern-watcher

Telegram-Bot, der auf **Flatfox.ch** regelmäßig nach **WG-Zimmern in der Region
Luzern** sucht und dich benachrichtigt, sobald ein passendes **neues** Inserat
erscheint. Läuft komplett auf GitHub-Servern – kein eigener Computer/Akku nötig.

## Kriterien

- WG-Zimmer (Flatfox-Kategorie `SHARED`)
- Miete **≤ 950 CHF/Monat** (inkl. NK) – anpassbar über `MAX_PRICE`
- Region **Luzern / HSLU Horw** (≤ ~40 Min ÖV), PLZ-Liste in
  [`check_flatfox.py`](check_flatfox.py) (`in_region`) – anpassbar
- Einzug flexibel oder ab `MIN_MOVING_DATE` (Default `2027-01-01`)
- Klar **befristete** Inserate werden **markiert** (⚠️), nicht ausgeschlossen –
  Mindestdauer (≥ 6 Monate) selbst prüfen
- Keine reinen **Frauen-WGs** und keine **Senioren-/Generationen-WGs** (Textfilter)

**Best effort:** Alter der WG (20–30), Geschlecht, genaue ÖV-Zeit und
Mindestdauer lassen sich nicht 100 % automatisch prüfen. Der Bot filtert grob
und schickt lieber einen Treffer zu viel – **den finalen Check machst du über
den Link** (30 Sekunden pro Inserat).

## Wie es funktioniert

- [`check_flatfox.py`](check_flatfox.py) nutzt die offene Flatfox-API
  (`/api/v1/pin/` für die Region + `/api/v1/public-listing/` für Details) –
  kein Browser nötig.
- Der Workflow [`check-flatfox.yml`](.github/workflows/check-flatfox.yml) läuft
  **alle 15 Minuten**.
- **Wiederhol-Schutz:** Jedes Inserat wird nur **einmal** gemeldet
  (Zustand über den GitHub-Actions-Cache, `STATE_FILE`).
- **Erst-Lauf:** meldet nicht alle bestehenden Inserate einzeln, sondern eine
  Zusammenfassung; danach nur echte Neuzugänge.
- **Status 2×/Woche** (Mo & Do): Lebenszeichen auch ohne neue Treffer.

## Einrichtung

1. Telegram-Bot bei [@BotFather](https://t.me/BotFather) erstellen → Token.
2. GitHub-Secrets setzen: `TELEGRAM_TOKEN`, `TELEGRAM_CHAT_ID`.
3. Manuell testen: **Actions → „WG-Zimmer Check (Flatfox)" → Run workflow**.

## Lokal ausführen

```bash
python3 -m venv venv
venv/bin/pip install -r requirements.txt

venv/bin/python check_flatfox.py --selftest   # Filter-Test
venv/bin/python check_flatfox.py --dry-run     # Live-Check ohne Versand
```

## wgzimmer.ch (via Playwright)

[`check_wgzimmer.py`](check_wgzimmer.py) prüft zusätzlich **wgzimmer.ch**
(Regionen Luzern, Nidwalden, Obwalden, Zug). wgzimmer schützt die Suche mit
reCAPTCHA v3 + CSRF, daher steuert das Skript einen echten Chromium-Browser
(Playwright) – das funktioniert auch aus GitHub Actions. Workflow:
[`check-wgzimmer.yml`](.github/workflows/check-wgzimmer.yml), alle 30 Min.
Eigener Zustand (`seen_wg.json`, pk-Präfix `wg:`), gleiche Telegram-Secrets.

## Später: Homegate & ImmoScout24

Diese wehren automatische Abfragen mit DataDome-CAPTCHA ab. Geplanter Schritt:
deren **eigene E-Mail-Alerts** an ein Postfach leiten und per IMAP nach Telegram
weiterreichen.

## Konfiguration (Umgebungsvariablen)

| Variable          | Default      | Bedeutung                                   |
| ----------------- | ------------ | ------------------------------------------- |
| `MAX_PRICE`       | `950`        | Maximalmiete CHF/Monat                      |
| `MIN_MOVING_DATE` | `2027-01-01` | frühester Einzug (leere/flexible Daten ok)  |
| `ALWAYS_NOTIFY`   | –            | `1` → auch ohne neuen Treffer senden        |
| `STATUS_NOTE`     | –            | optionale Kopfzeile                         |
| `STATE_FILE`      | –            | Pfad für den Wiederhol-Schutz               |
| `TELEGRAM_TOKEN` / `TELEGRAM_CHAT_ID` | – | Telegram-Zugang               |
