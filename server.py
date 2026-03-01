#!/usr/bin/env python3
"""Local API server: serves timetable from Pronote + transport data + the dashboard HTML."""
import os
import json
import time
import threading
from datetime import date, datetime, timedelta

import requests
from flask import Flask, jsonify, send_from_directory
from dotenv import load_dotenv
import pronotepy

load_dotenv()

app = Flask(__name__)
app.json.sort_keys = False

PRONOTE_URL  = os.environ["PRONOTE_URL"]
PRONOTE_USER = os.environ["PRONOTE_USER"]
PRONOTE_PASS = os.environ["PRONOTE_PASS"]

ABEL_EMAIL        = os.environ.get("ABEL_EMAIL", "")
ABEL_PASS         = os.environ.get("ABEL_PASS", "")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

DLR_URL  = "https://api.tfl.gov.uk/StopPoint/940GZZDLGRE/Arrivals"
RAIL_URL = (
    "https://transportapi.com/v3/uk/train/station_timetables/GNW.json"
    "?app_id=04668624&app_key=fcf40276b02a30519083fda8e6fe6772"
    "&live=true&train_status=passenger&type=departure"
)
LONDON_BOUND = ["luton", "bedford", "st albans", "harpenden", "welwyn", "stevenage", "farringdon"]

_cache           = {"data": None, "updated_at": None, "error": None}
_transport_cache = {"data": None, "updated_at": None, "error": None}
_dinners_cache   = {"data": None, "updated_at": None, "error": None, "week": None}
_lock            = threading.Lock()

STATIC_DIR = os.path.dirname(os.path.abspath(__file__))

DAYS_FR   = ["Lundi", "Mardi", "Mercredi", "Jeudi", "Vendredi", "Samedi", "Dimanche"]
MONTHS_FR = ["Jan", "Fév", "Mar", "Avr", "Mai", "Jun", "Jul", "Aoû", "Sep", "Oct", "Nov", "Déc"]


def target_dates():
    """Return remaining weekdays this week, or next Mon–Fri on weekends."""
    today   = date.today()
    weekday = today.weekday()
    if weekday >= 5:  # weekend → next Mon–Fri
        start = today + timedelta(days=(7 - weekday))
        return [start + timedelta(days=i) for i in range(5)]
    # weekday: from today through Friday
    return [today + timedelta(days=i) for i in range(5 - weekday)]


def format_date_fr(d):
    return f"{DAYS_FR[d.weekday()]} {d.day} {MONTHS_FR[d.month - 1]}"


def _is_london_bound(t):
    d = (t.get("destination_name") or "").lower()
    return "london" in d or any(x in d for x in LONDON_BOUND)


def _is_night():
    total_mins = datetime.now().hour * 60 + datetime.now().minute
    return total_mins >= 22 * 60 or total_mins < 7 * 60 + 15


def _secs_until_715():
    """Seconds until next 07:15."""
    now    = datetime.now()
    target = now.replace(hour=7, minute=15, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return (target - now).total_seconds()


# ── TRANSPORT ──────────────────────────────────────────────────────────────────

def fetch_trains():
    """Fetch DLR and National Rail, return {"dlr": [...], "rail": [...]}."""
    # DLR
    dlr_trains = []
    try:
        r = requests.get(DLR_URL, timeout=10)
        r.raise_for_status()
        arrivals = [t for t in r.json()
                    if t.get("modeName") == "dlr" and t.get("direction") == "inbound"]
        arrivals.sort(key=lambda t: t["timeToStation"])
        for t in arrivals[:4]:
            dlr_trains.append({
                "destination": t.get("destinationName", "Unknown"),
                "mins":        round(t["timeToStation"] / 60),
            })
    except Exception as exc:
        print(f"[{datetime.now():%H:%M:%S}] DLR error: {exc}", flush=True)

    # National Rail
    rail_trains = []
    try:
        r = requests.get(RAIL_URL, timeout=10)
        r.raise_for_status()
        all_dep = (r.json().get("departures") or {}).get("all") or []
        london_dep = [t for t in all_dep
                      if t.get("best_departure_estimate_mins") is not None
                      and _is_london_bound(t)]
        london_dep.sort(key=lambda t: t["best_departure_estimate_mins"])
        for t in london_dep[:4]:
            rail_trains.append({
                "destination": t.get("destination_name", "Unknown"),
                "mins":        t["best_departure_estimate_mins"],
                "status":      t.get("status", ""),
                "operator":    t.get("operator_name", ""),
            })
    except Exception as exc:
        print(f"[{datetime.now():%H:%M:%S}] Rail error: {exc}", flush=True)

    return {"dlr": dlr_trains, "rail": rail_trains}


def train_refresh_delay():
    total_mins = datetime.now().hour * 60 + datetime.now().minute
    if total_mins >= 22 * 60 or total_mins < 7 * 60 + 15:
        return _secs_until_715()
    if total_mins < 9 * 60:
        return 600    # 10 min peak
    return 3600       # 60 min off-peak


def transport_refresh_loop():
    while True:
        if not _is_night():
            try:
                print(f"[{datetime.now():%H:%M:%S}] Fetching transport...", flush=True)
                data = fetch_trains()
                with _lock:
                    _transport_cache["data"]       = data
                    _transport_cache["updated_at"] = datetime.now().strftime("%H:%M")
                    _transport_cache["error"]      = None
                print(f"[{datetime.now():%H:%M:%S}] Transport cached OK.", flush=True)
            except Exception as exc:
                print(f"[{datetime.now():%H:%M:%S}] Transport ERROR: {exc}", flush=True)
                with _lock:
                    _transport_cache["error"] = str(exc)
        else:
            secs = _secs_until_715()
            print(f"[{datetime.now():%H:%M:%S}] Transport: night mode, sleeping {secs/3600:.1f}h", flush=True)
        time.sleep(train_refresh_delay())


# ── PRONOTE ────────────────────────────────────────────────────────────────────

def fetch_pronote():
    dates  = target_dates()
    today  = date.today()
    hw_end = today + timedelta(days=7)

    client = pronotepy.ParentClient(PRONOTE_URL, PRONOTE_USER, PRONOTE_PASS)

    children = []
    for child in client.children:
        client.set_child(child)

        # Fetch the full week range and group by date
        all_lessons = sorted(client.lessons(dates[0], dates[-1]), key=lambda l: l.start)
        lessons_by_date = {}
        for l in all_lessons:
            lessons_by_date.setdefault(l.start.date(), []).append(l)

        days = []
        for d in dates:
            day_lessons = lessons_by_date.get(d, [])
            days.append({
                "date_label": d.strftime("%-d %B"),
                "date_fr":    format_date_fr(d),
                "weekday":    d.strftime("%A"),
                "is_today":   d == today,
                "lessons": [
                    {
                        "start":      l.start.strftime("%H:%M"),
                        "end":        l.end.strftime("%H:%M"),
                        "start_mins": l.start.hour * 60 + l.start.minute,
                        "end_mins":   l.end.hour * 60 + l.end.minute,
                        "subject":    l.subject.name if l.subject else "?",
                        "cancelled":  bool(l.canceled),
                        "status":     l.status or "",
                        "teacher":    l.teacher_name or "",
                        "room":       l.classroom or "",
                    }
                    for l in day_lessons
                ],
            })

        hw_by_date = {}
        try:
            hw_raw = {}
            for hw in client.homework(today, hw_end):
                if hw.done:
                    continue
                hw_date = hw.date.date() if hasattr(hw.date, "date") else hw.date
                hw_raw.setdefault(hw_date, []).append({
                    "subject":     hw.subject.name if hw.subject else "?",
                    "description": hw.description or "",
                })
            hw_by_date = {format_date_fr(d): items for d, items in sorted(hw_raw.items())}
        except Exception as exc:
            print(f"[{datetime.now():%H:%M:%S}] Homework error ({child.name}): {exc}", flush=True)

        children.append({
            "name":     child.name.split()[-1].capitalize(),
            "days":     days,
            "homework": hw_by_date,
        })

    return {"children": children}


def pronote_refresh_loop():
    while True:
        if not _is_night():
            try:
                print(f"[{datetime.now():%H:%M:%S}] Fetching Pronote...", flush=True)
                data = fetch_pronote()
                with _lock:
                    _cache["data"]       = data
                    _cache["updated_at"] = datetime.now().strftime("%H:%M")
                    _cache["error"]      = None
                print(f"[{datetime.now():%H:%M:%S}] Pronote cached OK.", flush=True)
            except Exception as exc:
                print(f"[{datetime.now():%H:%M:%S}] Pronote ERROR: {exc}", flush=True)
                with _lock:
                    _cache["error"] = str(exc)
            time.sleep(3600)
        else:
            secs = _secs_until_715()
            print(f"[{datetime.now():%H:%M:%S}] Pronote: night mode, sleeping {secs/3600:.1f}h", flush=True)
            time.sleep(secs)


# ── DINNERS ────────────────────────────────────────────────────────────────────

def scrape_abel_cole() -> list:
    """Login to Abel & Cole and extract the next delivery's product list."""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            )
        )
        page = context.new_page()

        print(f"[{datetime.now():%H:%M:%S}] Abel&Cole: going to login page…", flush=True)
        page.goto("https://www.abelandcole.co.uk/login", wait_until="networkidle", timeout=30000)

        page.fill('input[name="email"], input[type="email"]', ABEL_EMAIL)
        page.fill('input[name="password"], input[type="password"]', ABEL_PASS)
        page.click('button[type="submit"]')
        page.wait_for_load_state("networkidle", timeout=30000)
        print(f"[{datetime.now():%H:%M:%S}] Abel&Cole: logged in, URL={page.url}", flush=True)

        # Navigate to upcoming deliveries
        page.goto("https://www.abelandcole.co.uk/my-account/deliveries", wait_until="networkidle", timeout=30000)
        print(f"[{datetime.now():%H:%M:%S}] Abel&Cole: deliveries page URL={page.url}", flush=True)

        # Log a snippet of HTML to help identify correct selectors
        html = page.content()
        print(f"[{datetime.now():%H:%M:%S}] Abel&Cole: page HTML ({len(html)} chars) preview:\n{html[:3000]}", flush=True)

        # Try progressively broader selectors until items are found
        items = []
        candidate_selectors = [
            ".delivery-item__name",
            ".product-name",
            ".item-name",
            ".basket-item__name",
            ".order-item__name",
            ".product-title",
            "[data-product-name]",
            ".delivery-products li",
            ".order-items li",
        ]
        for sel in candidate_selectors:
            els = page.query_selector_all(sel)
            if els:
                items = [el.inner_text().strip() for el in els if el.inner_text().strip()]
                if items:
                    print(f"[{datetime.now():%H:%M:%S}] Abel&Cole: {len(items)} items via '{sel}'", flush=True)
                    break

        browser.close()

    if not items:
        raise RuntimeError("Abel&Cole scrape returned no items — check selectors in server.py logs")
    return items


def generate_meals(vegetables: list) -> list:
    """Call Claude Haiku to propose 4 dinners from the vegetable list."""
    import anthropic

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    veg_list = ", ".join(vegetables)
    prompt = (
        f"Tu es un chef cuisinier. Voici les légumes et produits disponibles cette semaine "
        f"dans ma box Abel & Cole :\n{veg_list}\n\n"
        "Propose 4 dîners variés pour lundi, mardi, mercredi et jeudi. "
        "Chaque légume/produit ne doit être utilisé que dans UN SEUL dîner maximum.\n\n"
        "Réponds UNIQUEMENT avec un JSON valide, sans aucun autre texte, dans ce format exact :\n"
        '[\n'
        '  {"day": "Lundi",    "name": "Nom du plat", "description": "Description courte en français", "uses": ["Légume1"]},\n'
        '  {"day": "Mardi",    "name": "Nom du plat", "description": "Description courte en français", "uses": ["Légume2"]},\n'
        '  {"day": "Mercredi", "name": "Nom du plat", "description": "Description courte en français", "uses": ["Légume3"]},\n'
        '  {"day": "Jeudi",    "name": "Nom du plat", "description": "Description courte en français", "uses": ["Légume4"]}\n'
        ']'
    )

    message = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=1024,
        messages=[{"role": "user", "content": prompt}],
    )
    content = message.content[0].text.strip()

    # Strip markdown code fences if present
    if "```" in content:
        parts = content.split("```")
        content = parts[1] if len(parts) > 1 else parts[0]
        if content.startswith("json"):
            content = content[4:].strip()

    return json.loads(content)


def fetch_dinners() -> dict:
    vegetables = scrape_abel_cole()
    meals = generate_meals(vegetables)
    return {
        "week_of":    date.today().strftime("%Y-%m-%d"),
        "vegetables": vegetables,
        "meals":      meals,
    }


def _current_monday() -> date:
    today = date.today()
    return today - timedelta(days=today.weekday())


def dinners_refresh_loop():
    while True:
        try:
            today          = date.today()
            weekday        = today.weekday()          # 0=Mon … 6=Sun
            current_monday = str(_current_monday())

            with _lock:
                cached_week = _dinners_cache["week"]

            # Refresh on: Saturday (5), Sunday (6), or Monday before noon (0 + hour < 12)
            is_refresh_window = (
                weekday >= 5
                or (weekday == 0 and datetime.now().hour < 12)
            )
            should_refresh = (cached_week != current_monday) and is_refresh_window

            if should_refresh:
                print(f"[{datetime.now():%H:%M:%S}] Fetching dinners…", flush=True)
                data = fetch_dinners()
                with _lock:
                    _dinners_cache["data"]       = data
                    _dinners_cache["updated_at"] = datetime.now().strftime("%H:%M")
                    _dinners_cache["error"]      = None
                    _dinners_cache["week"]       = current_monday
                print(f"[{datetime.now():%H:%M:%S}] Dinners cached OK.", flush=True)
            else:
                print(f"[{datetime.now():%H:%M:%S}] Dinners: no refresh needed (week={cached_week}, window={is_refresh_window})", flush=True)

        except Exception as exc:
            print(f"[{datetime.now():%H:%M:%S}] Dinners ERROR: {exc}", flush=True)
            with _lock:
                _dinners_cache["error"] = str(exc)

        time.sleep(3600)  # check every hour


# ── ROUTES ─────────────────────────────────────────────────────────────────────

@app.route("/api/trains")
def api_trains():
    with _lock:
        if _transport_cache["data"] is None:
            msg = _transport_cache["error"] or "Loading…"
            return jsonify({"error": msg}), 503
        return jsonify({**_transport_cache["data"], "cached_at": _transport_cache["updated_at"]})


@app.route("/api/timetable")
def api_timetable():
    with _lock:
        if _cache["data"] is None:
            msg = _cache["error"] or "Loading…"
            return jsonify({"error": msg}), 503
        return jsonify({**_cache["data"], "cached_at": _cache["updated_at"]})


@app.route("/api/dinners")
def api_dinners():
    with _lock:
        if _dinners_cache["data"] is None:
            msg = _dinners_cache["error"] or "En attente du prochain week-end…"
            return jsonify({"error": msg}), 503
        return jsonify({**_dinners_cache["data"], "cached_at": _dinners_cache["updated_at"]})


@app.route("/")
def index():
    return send_from_directory(STATIC_DIR, "index.html")


if __name__ == "__main__":
    threading.Thread(target=pronote_refresh_loop, daemon=True).start()
    threading.Thread(target=transport_refresh_loop, daemon=True).start()
    threading.Thread(target=dinners_refresh_loop, daemon=True).start()
    app.run(host="0.0.0.0", port=5000, debug=False)
