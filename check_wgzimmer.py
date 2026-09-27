#!/usr/bin/env python3
"""
wgzimmer.ch-Checker (via Playwright)
====================================
wgzimmer.ch schuetzt die Suche mit reCAPTCHA v3 (unsichtbar) + CSRF, daher
braucht es einen ECHTEN Browser. Dieses Skript steuert Chromium headless,
fuehrt die Suche (Region Luzern, Miete <= MAX_PRICE) aus und meldet neue
passende Inserate per Telegram – zusammen mit dem Flatfox-Bot, ohne Duplikate
(eigener Zustand, eigene pk-Praefixe "wg:").

Hinweis: Ob reCAPTCHA v3 von GitHub-Actions-IPs akzeptiert wird, ist nicht
garantiert. Findet das Skript keine Eintraege, meldet es das als Hinweis,
statt still zu scheitern.

Filter (wie Flatfox): Region Luzern, Miete <= 950, Einzug ab MIN_MOVING_DATE,
mind. ~6 Monate (befristete mit Enddatum < 6 Mte werden ausgeschlossen, sonst
markiert), keine Frauen-/Senioren-WGs; Alter/Geschlecht als Auszug mitgeschickt.
"""

from __future__ import annotations

import os
import re
import sys
import html as htmllib
from datetime import date, datetime

from playwright.sync_api import sync_playwright

from check_flatfox import (send_telegram, load_seen, save_seen,
                           is_women_only, is_senior_wg, extract_highlights)

SEARCH_URL = "https://www.wgzimmer.ch/wgzimmer/search/mate.html"
BASE = "https://www.wgzimmer.ch"
MAX_PRICE = int(os.environ.get("MAX_PRICE", "950"))
MIN_MOVING_DATE = os.environ.get("MIN_MOVING_DATE", "2027-01-01")
# wgzimmer-Regionen rund um Horw (kommagetrennt erweiterbar).
REGIONS = [r.strip() for r in os.environ.get("WG_REGIONS", "luzern,zug,nidwalden,obwalden").split(",") if r.strip()]
MIN_MONTHS = 6

# Klar zu weit entfernte Orte im Kanton Luzern (OeV > ~40 Min ab Horw).
FAR_PLACES = {"willisau", "entlebuch", "escholzmatt", "wolhusen", "schuepfheim",
              "dagmersellen", "reiden", "zell", "grosswangen", "hasle", "flühli",
              "fluehli", "romoos", "menznau", "wikon", "altbüron", "ebersecken"}

ENTRY_JS = """(els) => els.map(li => {
  const a = li.querySelector('a');
  const t = (s) => {
    const e = li.querySelector(s);
    if (!e) return '';
    const c = e.cloneNode(true);
    c.querySelectorAll('script,style').forEach(x => x.remove());
    return c.textContent.trim();
  };
  return {
    id: (li.id || '').replace('row-', ''),
    href: a ? a.getAttribute('href') : '',
    fromdate: t('.from-date'),
    cost: t('.cost'),
    state: t('.state')
  };
})"""


def _clean(s):
    return re.sub(r"\s+", " ", s or "").strip()


def dismiss_consent(page):
    """Consent-Dialog wegklicken – bevorzugt ablehnen, sonst zustimmen (Fallback)."""
    page.wait_for_timeout(1500)
    try:
        page.get_by_text("Optionen verwalten", exact=False).first.click(timeout=4000)
        page.get_by_text("Auswahl bestätigen", exact=False).first.click(timeout=4000)
        return
    except Exception:
        pass
    for label in ("Auswahl bestätigen", "Nur notwendige", "Ablehnen", "Einwilligen", "Alle akzeptieren"):
        try:
            page.get_by_text(label, exact=False).first.click(timeout=2000)
            return
        except Exception:
            continue


def scrape_region(region):
    """Gibt Liste roher Eintrags-dicts fuer eine Region zurueck."""
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        ctx = browser.new_context(
            locale="de-CH",
            user_agent=("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"),
        )
        page = ctx.new_page()
        page.goto(SEARCH_URL, wait_until="domcontentloaded", timeout=60000)
        dismiss_consent(page)
        try:
            page.select_option("select[name='wgState']", region, timeout=8000)
            page.select_option("select[name='priceMax']", str(MAX_PRICE), timeout=8000)
        except Exception as exc:
            browser.close()
            raise RuntimeError(f"Formular konnte nicht ausgefuellt werden: {exc}")
        page.get_by_role("button", name="Suchen").first.click()
        try:
            page.wait_for_selector("li.search-result-entry", timeout=25000)
        except Exception:
            pass  # evtl. keine Treffer ODER reCAPTCHA blockiert
        entries = page.eval_on_selector_all("li.search-result-entry", ENTRY_JS)
        browser.close()
        return entries


# --- Parsing / Filter -------------------------------------------------------

def _iso(d, m, y):
    try:
        return date(int(y), int(m), int(d))
    except ValueError:
        return None


def parse_href(href):
    """Aus /wglink/.../1-11-2026-31-03-2027-885-luzern.html -> (from, to|None, price|None)."""
    seg = href.rsplit("/", 1)[-1].replace(".html", "")
    m = re.match(r"(\d{1,2})-(\d{1,2})-(\d{4})(?:-(\d{1,2})-(\d{1,2})-(\d{4}))?-(\d+)-", seg)
    if not m:
        return None, None, None
    g = m.groups()
    d_from = _iso(g[0], g[1], g[2])
    d_to = _iso(g[3], g[4], g[5]) if g[3] else None
    price = int(g[6]) if g[6] else None
    return d_from, d_to, price


def evaluate_entry(e):
    """(is_match, room|None, reason)."""
    d_from, d_to, price_href = parse_href(e.get("href", ""))
    cost = _clean(e.get("cost", ""))
    price = int(cost) if cost.isdigit() else price_href
    if price is None or price > MAX_PRICE:
        return False, None, f"Preis {price}"

    if d_from is None:
        return False, None, "kein Einzugsdatum"
    if d_from.isoformat() < MIN_MOVING_DATE:
        return False, None, f"Einzug {d_from.isoformat()} vor {MIN_MOVING_DATE}"

    state = _clean(e.get("state", ""))
    if is_women_only(state):
        return False, None, "nur Frauen"
    if is_senior_wg(state):
        return False, None, "Senioren-WG"

    # Ort (2. Wort nach "Luzern"/Region) grob gegen zu weite Orte pruefen.
    tokens = state.split()
    place = tokens[1].lower().strip(",.") if len(tokens) > 1 else ""
    if place in FAR_PLACES:
        return False, None, f"Ort {place} zu weit"

    # Mindestdauer ~6 Monate.
    temporary = False
    fd = _clean(e.get("fromdate", ""))
    unbefristet = "unbefristet" in fd.lower()
    if d_to is not None:
        months = (d_to - d_from).days / 30.44
        if months < MIN_MONTHS - 0.5:
            return False, None, f"nur {months:.1f} Monate"
        temporary = True
    elif not unbefristet and "bis" in fd.lower():
        temporary = True  # befristet ohne parsebares Enddatum -> markieren

    url = BASE + e["href"] if e.get("href", "").startswith("/") else e.get("href", "")
    # Ort/Beschreibung aufteilen: "Luzern <Ort> <Beschreibung...>"
    ort = " ".join(tokens[:2]) if len(tokens) > 1 else state[:40]
    room = {
        "pk": "wg:" + e.get("id", url),
        "price": price,
        "place": ort,
        "moving_human": d_from.isoformat() + (f" bis {d_to.isoformat()}" if d_to else ""),
        "temporary": temporary,
        "unbefristet": unbefristet,
        "highlight": extract_highlights(state, max_len=200),
        "url": url,
    }
    return True, room, None


def format_room(r):
    bits = [f"CHF {r['price']}", r["place"], f"Einzug {r['moving_human']}"]
    if r["unbefristet"]:
        bits.append("unbefristet ✅")
    if r["temporary"]:
        bits.append("⚠️ befristet – Dauer prüfen")
    lines = [f"🏠 <b>{htmllib.escape(r['place'])}</b>",
             htmllib.escape(" · ".join(b for b in bits if b))]
    if r.get("highlight"):
        lines.append(f"💬 <i>{htmllib.escape(r['highlight'])}</i>")
    lines.append(f"🔗 <a href=\"{htmllib.escape(r['url'])}\">Inserat ansehen (wgzimmer)</a>")
    return "\n".join(lines)


TELEGRAM_LIMIT = 3800


def build_messages(new_rooms, all_count, anomalies, send_summary):
    messages = []
    if new_rooms:
        head = ("neues WG-Zimmer (wgzimmer)" if len(new_rooms) == 1
                else f"{len(new_rooms)} neue WG-Zimmer (wgzimmer)")
        current = f"✨ <b>{head}</b> (Region Luzern, ≤ CHF {MAX_PRICE})"
        for block in (format_room(r) for r in new_rooms):
            if len(current) + len(block) + 2 > TELEGRAM_LIMIT:
                messages.append(current)
                current = block
            else:
                current += "\n\n" + block
        messages.append(current)
    elif send_summary:
        messages.append(f"🟢 <b>wgzimmer-Check läuft.</b> Aktuell {all_count} passende(s) "
                        f"WG-Zimmer auf wgzimmer.ch (Region Luzern, ≤ CHF {MAX_PRICE}).")

    tail = []
    if anomalies:
        tail.append("⚠️ <b>Hinweise:</b>\n" + "\n".join(htmllib.escape(a) for a in anomalies))
    tail.append("🔎 <a href=\"https://www.wgzimmer.ch/wgzimmer/search/mate.html\">wgzimmer.ch Suche</a>")
    tail_s = "\n\n".join(tail)
    if messages and len(messages[-1]) + len(tail_s) + 2 <= TELEGRAM_LIMIT:
        messages[-1] += "\n\n" + tail_s
    else:
        messages.append(tail_s)
    return messages


def main(argv):
    dry_run = "--dry-run" in argv
    always_notify = os.environ.get("ALWAYS_NOTIFY", "").lower() in ("1", "true", "yes")
    state_file = os.environ.get("STATE_FILE")

    rooms, anomalies = [], []
    seen_ids = set()
    for region in REGIONS:
        try:
            entries = scrape_region(region)
        except Exception as exc:
            anomalies.append(f"Region {region}: {exc}")
            continue
        print(f"Region {region}: {len(entries)} rohe Eintraege gelesen")
        if not entries:
            anomalies.append(f"Region {region}: keine Eintraege gelesen "
                             f"(evtl. keine Treffer oder reCAPTCHA blockiert).")
            continue
        for e in entries:
            ok, room, _reason = evaluate_entry(e)
            if ok and room["pk"] not in seen_ids:
                seen_ids.add(room["pk"])
                rooms.append(room)

    rooms.sort(key=lambda r: r["price"])
    print(f"wgzimmer passende Zimmer: {len(rooms)} (Regionen: {', '.join(REGIONS)})")
    for r in rooms:
        print(f"  - {r['place']} | CHF {r['price']} | {r['moving_human']} | {r['url']}")
    for a in anomalies:
        print("  ! " + a)

    first_run = False
    if state_file:
        first_run = not os.path.exists(state_file)
        seen = load_seen(state_file)
        new_rooms = [] if first_run else [r for r in rooms if r["pk"] not in seen]
        save_seen(state_file, {r["pk"] for r in rooms})
    else:
        new_rooms = rooms

    send_summary = always_notify or first_run
    if not (new_rooms or anomalies or send_summary):
        print("Nichts Neues – keine Nachricht gesendet.")
        return 0

    messages = build_messages(new_rooms, len(rooms), anomalies, send_summary)
    note = os.environ.get("STATUS_NOTE", "").strip()
    if first_run and not note:
        note = "🚀 wgzimmer-Check aktiviert – ab jetzt bekommst du neue passende Inserate."
    if note:
        messages[0] = htmllib.escape(note) + "\n\n" + messages[0]

    if dry_run or not (os.environ.get("TELEGRAM_TOKEN") and os.environ.get("TELEGRAM_CHAT_ID")):
        print("\n--- Nachricht(en) (nicht gesendet) ---\n" + "\n\n=====\n\n".join(messages))
        return 0
    for m in messages:
        send_telegram(m)
    print(f"{len(messages)} Telegram-Nachricht(en) gesendet.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
