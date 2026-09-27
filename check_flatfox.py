#!/usr/bin/env python3
"""
wg-luzern-watcher (Flatfox)
===========================
Sucht auf Flatfox.ch regelmaessig nach WG-Zimmern in der Region Luzern und
benachrichtigt per Telegram, sobald ein passendes NEUES Inserat erscheint.

Kriterien
---------
- WG-Zimmer (Flatfox object_category = "SHARED")
- Miete <= MAX_PRICE CHF/Monat (Default 950, inkl. NK)
- Region: PLZ in der Umgebung von Luzern / HSLU Horw (<= ~40 Min OeV),
  siehe in_region()
- Einzug: unbefristet/flexibel oder ab MIN_MOVING_DATE (Default 2027-01-01)
- Mindestens ~6 Monate: klar befristete Inserate (is_temporary) werden
  MARKIERT, nicht ausgeschlossen (kurze Zwischenmieten erkennt man am Hinweis)
- Keine reinen Frauen-WGs und keine Senioren-/Generationen-WGs (Textfilter)
- Alter der WG (20-30) wird NICHT hart gefiltert -> selbst kurz pruefen

Alles Uebrige (genaue OeV-Zeit, Alter, Geschlecht, Mindestdauer) verifizierst du
selbst ueber den Link -- lieber ein Treffer zu viel als einen verpassen.

Technik
-------
Flatfox hat eine offene JSON-API:
  1) /api/v1/pin/      -> Inserate (pk + Preis + Koordinaten) in einer Bounding-Box
  2) /api/v1/public-listing/?pk=... -> Detaildaten
Kein Browser noetig. Wiederhol-Schutz ueber STATE_FILE (GitHub-Actions-Cache).

Benutzung
---------
    python check_flatfox.py            # Live-Check
    python check_flatfox.py --selftest # Filter gegen eingebautes Fixture testen
    python check_flatfox.py --dry-run  # Live-Check, Nachricht nur ausgeben

Umgebungsvariablen
------------------
    TELEGRAM_TOKEN, TELEGRAM_CHAT_ID   fuer den Versand
    MAX_PRICE          Default 950
    MIN_MOVING_DATE    Default 2027-01-01 (Einzug fruehestens; leere Daten ok)
    ALWAYS_NOTIFY      "1" -> auch ohne neuen Treffer eine (Status-)Nachricht
    STATUS_NOTE        optionale Kopfzeile der Nachricht
    STATE_FILE         Pfad fuer den Wiederhol-Schutz
"""

from __future__ import annotations

import os
import sys
import json
import html as htmllib
from datetime import date

import requests


# --- Konfiguration ----------------------------------------------------------

BASE = "https://flatfox.ch"
PIN_URL = BASE + "/api/v1/pin/"
LISTING_URL = BASE + "/api/v1/public-listing/"

# Bounding-Box, die die Region Luzern grosszuegig abdeckt (Sursee-Zug-Sarnen).
# Die genaue Ortsfilterung macht danach in_region() ueber die PLZ.
BBOX = {"south": 46.80, "north": 47.22, "west": 8.00, "east": 8.62}

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36 wg-luzern-watcher"
)

MAX_PRICE = int(os.environ.get("MAX_PRICE", "950"))
MIN_MOVING_DATE = os.environ.get("MIN_MOVING_DATE", "2027-01-01")

# Region: PLZ <= ~40 Min OeV von der HSLU Horw. Bewusst grosszuegig; die genaue
# Reisezeit pruefst du selbst. Anpassbar.
FAR_EXCLUDE = {
    6067, 6068,  # Melchtal / Stoeckalp
    6072, 6073, 6074, 6078,  # Sachseln, Giswil, Lungern (Brueniglinie, zu weit)
    6083, 6084, 6085, 6086,  # Hasliberg-Richtung
    6375, 6376, 6377, 6382, 6383, 6386, 6387, 6388, 6390,  # Nidwalden-Berg / Engelberg
    6197, 6182, 6110, 6156, 6103, 6113,  # Entlebuch-Taeler (OeV schlecht)
    6234, 6242, 6243, 6244, 6245, 6260, 6262, 6263, 6264, 6265,  # oberes Suretal / Wiggertal
    6280,  # (Hochdorf grenzwertig -> siehe explizite Liste unten)
}
EXTRA_ALLOW = {
    6362, 6363, 6370, 6372, 6373, 6374,        # Nidwalden (Stansstad/Stans/Buochs)
    6402, 6403,                                # Kuessnacht
    6053, 6055, 6056, 6060, 6062, 6063, 6064,  # Alpnach/Sarnen/Kerns (grenzwertig)
    6280, 6281, 6283, 6284, 6285, 6287, 6288, 6289,  # Hochdorf/Seetal
}


def in_region(zipcode) -> bool:
    """True, wenn die PLZ in der Region Luzern/Horw (<= ~40 Min OeV) liegt."""
    try:
        z = int(str(zipcode).strip())
    except (TypeError, ValueError):
        return False
    if z in FAR_EXCLUDE:
        return False
    if z in EXTRA_ALLOW:
        return True
    # Kern Luzern-Agglo (6000-6066), Sursee/Sempach (6200-6233), Zug (6300-6345).
    return (6000 <= z <= 6066) or (6200 <= z <= 6233) or (6300 <= z <= 6345)


# --- Textfilter (best effort) ----------------------------------------------

def _norm(text: str) -> str:
    t = (text or "").lower()
    return (t.replace("ä", "ae").replace("ö", "oe").replace("ü", "ue")
             .replace("ß", "ss"))


WOMEN_ONLY_PHRASES = [
    "nur frauen", "nur an frauen", "nur weibliche", "nur weiblich",
    "reine frauen", "frauen-wg", "frauenwg", "frauen wg", "maedels-wg",
    "nur fuer frauen", "only women", "women only", "female only", "females only",
    "keine maenner", "suchen eine frau", "eine frau gesucht",
    "weibliche mitbewohnerin gesucht", "nur mitbewohnerinnen",
]

SENIOR_PHRASES = [
    "senioren", "senior:innen", "seniorinnen", "generationen-wg", "generationenwg",
    "50+", "55+", "60+", "ab 50 jahren", "ab 55", "ab 60", "rentner",
]


def is_women_only(text: str) -> bool:
    n = _norm(text)
    return any(p in n for p in WOMEN_ONLY_PHRASES)


def is_senior_wg(text: str) -> bool:
    n = _norm(text)
    return any(p in n for p in SENIOR_PHRASES)


# --- Auswertung eines Inserats ---------------------------------------------

def moving_ok(moving_date) -> bool:
    """True, wenn Einzug flexibel/leer ODER >= MIN_MOVING_DATE."""
    if not moving_date:
        return True
    try:
        return str(moving_date)[:10] >= MIN_MOVING_DATE
    except Exception:
        return True


def price_of(listing):
    p = listing.get("price_display")
    try:
        return int(p) if p is not None else None
    except (TypeError, ValueError):
        return None


def evaluate_listing(listing):
    """Gibt (is_match, room_dict, reject_reason) zurueck."""
    if listing.get("object_category") != "SHARED":
        return False, None, "keine WG"
    if listing.get("reserved") is True:
        return False, None, "reserviert"

    price = price_of(listing)
    if price is None or price > MAX_PRICE:
        return False, None, f"Preis {price}"

    zipcode = listing.get("zipcode")
    if not in_region(zipcode):
        return False, None, f"Region {zipcode}"

    if not moving_ok(listing.get("moving_date")):
        return False, None, f"Einzug {listing.get('moving_date')}"

    text = " ".join(str(listing.get(k) or "") for k in
                    ("public_title", "description", "description_title", "pitch_title"))
    if is_women_only(text):
        return False, None, "nur Frauen"
    if is_senior_wg(text):
        return False, None, "Senioren-WG"

    url = listing.get("url") or listing.get("short_url") or ""
    if url.startswith("/"):
        url = BASE + url.replace("/en/", "/de/", 1)

    room = {
        "pk": listing.get("pk"),
        "price": price,
        "zipcode": zipcode,
        "city": listing.get("city") or "",
        "street": listing.get("street") or "",
        "rooms": listing.get("number_of_rooms"),
        "moving_date": listing.get("moving_date") or "n. Vereinbarung",
        "temporary": bool(listing.get("is_temporary")),
        "title": (listing.get("public_title") or "").strip(),
        "url": url,
    }
    return True, room, None


# --- API-Zugriff ------------------------------------------------------------

def _get_json(url, params=None):
    resp = requests.get(url, params=params, headers={"User-Agent": USER_AGENT}, timeout=30)
    resp.raise_for_status()
    return resp.json()


def fetch_matches():
    """Ruft Flatfox ab und gibt (matches, anomalies) zurueck.

    matches: Liste room_dict fuer passende WG-Zimmer.
    anomalies: Liste von Warntexten (Abruf-/Strukturfehler).
    """
    anomalies = []
    try:
        pins = _get_json(PIN_URL, {**BBOX, "object_category": "SHARED", "max_count": 400})
    except Exception as exc:
        return [], [f"Flatfox /pin/ Abruf fehlgeschlagen: {exc}"]

    if not isinstance(pins, list):
        return [], ["Flatfox /pin/ lieferte unerwartetes Format (keine Liste)."]

    # Kandidaten-pks: grober Preis-Vorfilter (unbekannte Preise mitnehmen).
    pks = []
    for p in pins:
        price = p.get("price_display")
        if price is None or (isinstance(price, (int, float)) and price <= MAX_PRICE):
            if p.get("pk"):
                pks.append(p["pk"])

    matches = []
    # Details in Bloecken abrufen.
    for i in range(0, len(pks), 40):
        chunk = pks[i:i + 40]
        params = [("pk", pk) for pk in chunk] + [("limit", 0)]
        try:
            data = _get_json(LISTING_URL, params)
        except Exception as exc:
            anomalies.append(f"Flatfox Detail-Abruf fehlgeschlagen: {exc}")
            continue
        listings = data if isinstance(data, list) else data.get("results", [])
        for listing in listings:
            ok, room, _reason = evaluate_listing(listing)
            if ok:
                matches.append(room)

    # Doppelte pks entfernen, nach Preis sortieren.
    seen = set()
    unique = []
    for r in sorted(matches, key=lambda x: (x["price"] if x["price"] is not None else 9999)):
        if r["pk"] in seen:
            continue
        seen.add(r["pk"])
        unique.append(r)
    return unique, anomalies


# --- Telegram / Nachricht ---------------------------------------------------

SEARCH_LINK = (
    f"{BASE}/de/search/?north={BBOX['north']}&south={BBOX['south']}"
    f"&west={BBOX['west']}&east={BBOX['east']}&object_category=SHARED"
)


def format_room(r) -> str:
    ort = " ".join(x for x in [str(r["zipcode"] or ""), r["city"]] if x).strip()
    kopf = r["street"] or r["title"] or ort or "WG-Zimmer"
    zeile2_bits = [
        f"CHF {r['price']}",
        ort if ort and ort not in kopf else "",
        f"{r['rooms']:.0f} Zi." if isinstance(r["rooms"], (int, float)) else "",
        f"Einzug {r['moving_date']}",
    ]
    if r["temporary"]:
        zeile2_bits.append("⚠️ befristet – Dauer prüfen")
    zeile2 = " · ".join(b for b in zeile2_bits if b)
    return (f"🏠 <b>{htmllib.escape(kopf)}</b>\n"
            f"{htmllib.escape(zeile2)}\n"
            f"🔗 <a href=\"{htmllib.escape(r['url'])}\">Inserat ansehen</a>")


TELEGRAM_LIMIT = 3800  # Sicherheitsgrenze unter Telegrams 4096 Zeichen


def build_messages(new_rooms, all_rooms, anomalies, send_summary):
    """Baut eine LISTE von Nachrichten (jede < TELEGRAM_LIMIT Zeichen)."""
    messages = []

    if new_rooms:
        header = ("neues WG-Zimmer" if len(new_rooms) == 1
                  else f"{len(new_rooms)} neue WG-Zimmer")
        head_line = f"✨ <b>{header}</b> (Region Luzern, ≤ CHF {MAX_PRICE})"
        current = head_line
        for block in (format_room(r) for r in new_rooms):
            if len(current) + len(block) + 2 > TELEGRAM_LIMIT:
                messages.append(current)
                current = block
            else:
                current = current + "\n\n" + block
        messages.append(current)
    elif send_summary:
        parts = [f"🟢 <b>WG-Bot läuft.</b> Aktuell {len(all_rooms)} passende(s) "
                 f"WG-Zimmer auf Flatfox (Region Luzern, ≤ CHF {MAX_PRICE})."]
        parts.extend(format_room(r) for r in all_rooms[:8])
        if len(all_rooms) > 8:
            parts.append(f"… und {len(all_rooms) - 8} weitere – siehe Link unten.")
        messages.append("\n\n".join(parts))

    # Hinweise + Such-Link ans Ende haengen (oder als eigene Nachricht).
    tail_parts = []
    if anomalies:
        tail_parts.append("⚠️ <b>Hinweise:</b>\n" + "\n".join(htmllib.escape(a) for a in anomalies))
    tail_parts.append(f"🔎 <a href=\"{htmllib.escape(SEARCH_LINK)}\">Alle WG-Zimmer auf Flatfox</a>")
    tail = "\n\n".join(tail_parts)

    if messages and len(messages[-1]) + len(tail) + 2 <= TELEGRAM_LIMIT:
        messages[-1] = messages[-1] + "\n\n" + tail
    else:
        messages.append(tail)
    return messages


def send_telegram(text: str) -> None:
    token = os.environ.get("TELEGRAM_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        raise RuntimeError("TELEGRAM_TOKEN / TELEGRAM_CHAT_ID sind nicht gesetzt.")
    resp = requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data={"chat_id": chat_id, "text": text, "parse_mode": "HTML",
              "disable_web_page_preview": "true"},
        timeout=30,
    )
    if not resp.ok:
        raise RuntimeError(f"Telegram-Fehler {resp.status_code}: {resp.text}")


# --- Zustand (Wiederhol-Schutz) --------------------------------------------

def load_seen(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return set(json.load(fh))
    except (FileNotFoundError, ValueError, TypeError):
        return set()


def save_seen(path, pks) -> None:
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(sorted(pks), fh)


# --- Selbsttest -------------------------------------------------------------

SELFTEST_LISTINGS = [
    # Treffer: Luzern, 900, unbefristet, Einzug 2027-03-01
    {"pk": 1, "object_category": "SHARED", "zipcode": "6003", "city": "Luzern",
     "street": "Musterweg 1", "price_display": 900, "number_of_rooms": 1.0,
     "moving_date": "2027-03-01", "is_temporary": False, "reserved": False,
     "public_title": "Schoenes Zimmer in 4er-WG", "description": "Wir sind eine gemischte WG."},
    # Ausschluss: zu teuer (980)
    {"pk": 2, "object_category": "SHARED", "zipcode": "6003", "city": "Luzern",
     "price_display": 980, "moving_date": None, "is_temporary": False, "reserved": False,
     "public_title": "Zimmer", "description": ""},
    # Ausschluss: Region zu weit (8005 Zuerich)
    {"pk": 3, "object_category": "SHARED", "zipcode": "8005", "city": "Zürich",
     "price_display": 800, "moving_date": None, "is_temporary": False, "reserved": False,
     "public_title": "Zimmer", "description": ""},
    # Ausschluss: nur Frauen
    {"pk": 4, "object_category": "SHARED", "zipcode": "6010", "city": "Kriens",
     "price_display": 700, "moving_date": None, "is_temporary": False, "reserved": False,
     "public_title": "Zimmer in Frauen-WG", "description": "Wir suchen nur Frauen."},
    # Treffer + Flag: befristet, aber sonst passend (Zug, 850)
    {"pk": 5, "object_category": "SHARED", "zipcode": "6300", "city": "Zug",
     "street": "Seeweg 3", "price_display": 850, "number_of_rooms": 1.0,
     "moving_date": "2027-04-01", "is_temporary": True, "reserved": False,
     "public_title": "Zimmer am See", "description": "Zwischenmiete moeglich."},
    # Ausschluss: Einzug zu frueh (2026-10)
    {"pk": 6, "object_category": "SHARED", "zipcode": "6004", "city": "Luzern",
     "price_display": 800, "moving_date": "2026-10-01", "is_temporary": False, "reserved": False,
     "public_title": "Zimmer", "description": ""},
    # Ausschluss: Senioren-WG
    {"pk": 7, "object_category": "SHARED", "zipcode": "6005", "city": "Luzern",
     "price_display": 750, "moving_date": None, "is_temporary": False, "reserved": False,
     "public_title": "Zimmer in Generationen-WG", "description": "Fuer Senior:innen 60+."},
    # Ausschluss: reserviert
    {"pk": 8, "object_category": "SHARED", "zipcode": "6003", "city": "Luzern",
     "price_display": 700, "moving_date": None, "is_temporary": False, "reserved": True,
     "public_title": "Zimmer", "description": ""},
    # Treffer: Einzug flexibel (None), Emmenbruecke 6020, 890
    {"pk": 9, "object_category": "SHARED", "zipcode": "6020", "city": "Emmenbrücke",
     "street": "Gerliswilstrasse 5", "price_display": 890, "number_of_rooms": 1.0,
     "moving_date": None, "is_temporary": False, "reserved": False,
     "public_title": "Helles Zimmer", "description": "Gemischte 3er-WG, Studenten willkommen."},
]


def selftest() -> int:
    ok = True
    results = {}
    for l in SELFTEST_LISTINGS:
        match, room, reason = evaluate_listing(l)
        results[l["pk"]] = (match, reason)

    expected = {1: True, 2: False, 3: False, 4: False, 5: True, 6: False, 7: False, 8: False, 9: True}
    print("Selbsttest evaluate_listing:")
    for pk, exp in expected.items():
        got = results[pk][0]
        passed = got == exp
        ok = ok and passed
        detail = "" if got else f"(abgelehnt: {results[pk][1]})"
        print(f"  [{'OK' if passed else 'FAIL'}] pk{pk}: erwartet={exp} bekommen={got} {detail}")

    checks = [
        ("in_region 6048 Horw", in_region("6048") is True),
        ("in_region 6300 Zug", in_region("6300") is True),
        ("in_region 6403 Kuessnacht", in_region("6403") is True),
        ("in_region 8005 Zuerich false", in_region("8005") is False),
        ("in_region 6078 Lungern false", in_region("6078") is False),
        ("in_region 6390 Engelberg false", in_region("6390") is False),
        ("women-only erkannt", is_women_only("Wir suchen nur Frauen") is True),
        ("gemischt nicht women-only", is_women_only("gemischte WG") is False),
        ("senior erkannt", is_senior_wg("WG fuer Senioren 60+") is True),
        ("moving None ok", moving_ok(None) is True),
        ("moving 2027-03 ok", moving_ok("2027-03-01") is True),
        ("moving 2026-10 nicht ok", moving_ok("2026-10-01") is False),
    ]
    for label, passed in checks:
        ok = ok and passed
        print(f"  [{'OK' if passed else 'FAIL'}] {label}")

    match_rooms = [evaluate_listing(l)[1] for l in SELFTEST_LISTINGS if evaluate_listing(l)[0]]
    print("\nBeispiel-Nachricht (neue Treffer):")
    for m in build_messages(match_rooms, match_rooms, [], False):
        print(m)
    return 0 if ok else 1


# --- Einstieg ---------------------------------------------------------------

def main(argv) -> int:
    if "--selftest" in argv:
        return selftest()

    dry_run = "--dry-run" in argv
    always_notify = os.environ.get("ALWAYS_NOTIFY", "").lower() in ("1", "true", "yes")
    state_file = os.environ.get("STATE_FILE")

    rooms, anomalies = fetch_matches()
    current_pks = {r["pk"] for r in rooms}

    print(f"Gefundene passende WG-Zimmer: {len(rooms)}")
    for r in rooms:
        print(f"  - {r['zipcode']} {r['city']} | CHF {r['price']} | Einzug {r['moving_date']}"
              f"{' | befristet' if r['temporary'] else ''} | {r['url']}")
    for a in anomalies:
        print("  ! " + a)

    first_run = False
    if state_file:
        first_run = not os.path.exists(state_file)
        seen = load_seen(state_file)
        # Beim allerersten Lauf NICHT alle bestehenden Inserate als "neu" melden.
        new_rooms = [] if first_run else [r for r in rooms if r["pk"] not in seen]
        save_seen(state_file, current_pks)
    else:
        new_rooms = rooms  # ohne Zustand (lokal) alles als "neu" behandeln

    # Beim Erst-Lauf oder manuellem Start eine Zusammenfassung senden.
    send_summary = always_notify or first_run
    should_notify = bool(new_rooms) or bool(anomalies) or send_summary
    if not should_notify:
        print("Nichts Neues – keine Nachricht gesendet.")
        return 0

    messages = build_messages(new_rooms, rooms, anomalies, send_summary)
    note = os.environ.get("STATUS_NOTE", "").strip()
    if first_run and not note:
        note = "🚀 WG-Bot aktiviert – ab jetzt bekommst du neue passende Inserate automatisch."
    if note:
        messages[0] = htmllib.escape(note) + "\n\n" + messages[0]

    if dry_run or not (os.environ.get("TELEGRAM_TOKEN") and os.environ.get("TELEGRAM_CHAT_ID")):
        print(f"\n--- {len(messages)} Nachricht(en) (nicht gesendet) ---\n"
              + "\n\n===== NÄCHSTE NACHRICHT =====\n\n".join(messages))
        return 0

    for msg in messages:
        send_telegram(msg)
    print(f"{len(messages)} Telegram-Nachricht(en) gesendet.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
