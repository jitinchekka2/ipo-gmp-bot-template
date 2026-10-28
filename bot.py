#!/usr/bin/env python3
"""IPO GMP alert bot (Mainboard IPOs only).

Reads Mainboard GMP tables from investorgain.com (live) and ipowatch.in
(a snapshot saved by a home-network runner, because ipowatch blocks cloud
IPs), merges them by company name and sends Telegram alerts when an
open/upcoming IPO's estimated gain crosses your threshold in either source.
Designed to run on a GitHub Actions schedule; alert history lives in state.json.

    python3 bot.py                    check / digest (MODE), sends alerts
    python3 bot.py snapshot ipowatch  save one source's table to snapshots/
"""
import difflib
import html
import json
import os
import re
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

SOURCE_URLS = {
    "ipowatch": "https://ipowatch.in/",
    "investorgain": "https://www.investorgain.com/report/ipo-gmp-live/331/",
}
PRIORITY = ["investorgain", "ipowatch"]  # whose name/status/dates win when merging
STATE_FILE = Path(os.environ.get("STATE_FILE", "state.json"))
SNAPSHOT_DIR = Path(os.environ.get("SNAPSHOT_DIR", "snapshots"))
IST = timezone(timedelta(hours=5, minutes=30))


def env_float(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        print(f"[warn] {name}={raw!r} is not a number, using {default}")
        return default


def env_list(name: str, default: str) -> list[str]:
    raw = os.environ.get(name)
    return [s.strip().lower() for s in (default if raw is None else raw).split(",") if s.strip()]


THRESHOLDS = {"mainboard": env_float("MAINBOARD_THRESHOLD", 15.0)}
REALERT_DELTA = env_float("REALERT_DELTA", 5.0)  # % points move that triggers an update
MODE = (os.environ.get("MODE") or "check").strip().lower()  # "check" or "digest"
DRY_RUN = os.environ.get("DRY_RUN") == "1"
TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
# Fetched on every run; a failure is reported but doesn't stop the other sources.
LIVE_SOURCES = env_list("LIVE_SOURCES", "investorgain")
# Opt-in: read these from snapshots/<source>.json instead of the network (see optional/).
SNAPSHOT_SOURCES = env_list("SNAPSHOT_SOURCES", "")
SNAPSHOT_TTL = timedelta(hours=env_float("SNAPSHOT_TTL_HOURS", 12))

# Only Mainboard tables are read; SME IPOs are ignored.
IPOWATCH_SECTION = "current mainboard ipo gmp"
ACTIVE = {"open", "upcoming"}
MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}


# ---------------------------------------------------------------- fetching

def fetch(url: str) -> str:
    resp = requests.get(url, timeout=30, headers={
        "User-Agent": ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"),
        "Accept-Language": "en-IN,en;q=0.9",
    })
    resp.raise_for_status()
    return resp.text


def read_page(source: str) -> str:
    saved = os.environ.get(f"HTML_FILE_{source.upper()}")
    return Path(saved).read_text() if saved else fetch(SOURCE_URLS[source])


# ---------------------------------------------------------------- parsing

def signed_number(text: str):
    """First number in text, keeping a leading minus (for GMP like '₹-5')."""
    m = re.search(r"-?\d[\d,]*(?:\.\d+)?", text.replace("₹", ""))
    return float(m.group().replace(",", "")) if m else None


def upper_price(text: str):
    """Upper end of a price band like '₹1,665' or '₹100-105'."""
    nums = re.findall(r"\d[\d,]*(?:\.\d+)?", text)
    return max(float(n.replace(",", "")) for n in nums) if nums else None


def gain_pct(gain_text: str, gmp, price):
    m = re.search(r"\(\s*(-?\d+(?:\.\d+)?)\s*%", gain_text)
    if m:
        return float(m.group(1))
    if gmp is not None and price:
        return round(gmp / price * 100, 2)
    return None


def find_section_table(soup, phrase: str):
    for h in soup.find_all(["h1", "h2", "h3", "h4", "h5", "h6"]):
        if phrase in h.get_text(" ", strip=True).lower():
            return h.find_next("table")
    return None


def col_index(headers, key: str, default: int) -> int:
    for i, h in enumerate(headers):
        if key in h:
            return i
    return default


def parse_ipowatch(page: str) -> list[dict]:
    soup = BeautifulSoup(page, "html.parser")
    ipos = []
    table = find_section_table(soup, IPOWATCH_SECTION)
    if table is None:
        print("[warn] ipowatch Mainboard GMP table not found")
        return ipos
    headers = [th.get_text(" ", strip=True).lower() for th in table.find_all("th")]
    ci = {
        "company": col_index(headers, "company", 0),
        "gmp": col_index(headers, "gmp", 1),
        "price": col_index(headers, "price", 3),
        "gain": col_index(headers, "gain", 4),
        "date": col_index(headers, "date", 5),
    }
    for tr in table.find_all("tr"):
        cells = tr.find_all("td")
        if len(cells) < 5:
            continue

        def text(col):
            i = ci[col]
            return cells[i].get_text(" ", strip=True) if i < len(cells) else ""

        company_cell = cells[ci["company"]]
        link = company_cell.find("a")
        name = (link.get_text(" ", strip=True) if link
                else company_cell.get_text("\n", strip=True).split("\n")[0])
        if not name:
            continue
        rest = company_cell.get_text(" ", strip=True).replace(name, "", 1).lower()
        status = next((s for s in ("open", "upcoming", "closed", "listed") if s in rest),
                      rest.strip() or "unknown")

        gmp = signed_number(text("gmp"))
        price = upper_price(text("price"))
        ipos.append({
            "source": "ipowatch",
            "segment": "mainboard",
            "name": name,
            "url": urljoin(SOURCE_URLS["ipowatch"], link.get("href", "")) if link else "",
            "status": status,
            "gmp": gmp,
            "price": price,
            "pct": gain_pct(text("gain"), gmp, price),
            "dates": text("date"),
        })
    return ipos


INVESTORGAIN_STATUS = {"U": "upcoming", "O": "open", "C": "closed"}


def parse_investorgain(page: str) -> list[dict]:
    soup = BeautifulSoup(page, "html.parser")
    table = soup.find("table")
    if table is None:
        print("[warn] investorgain GMP table not found")
        return []
    headers = [th.get_text(" ", strip=True).lower() for th in table.find_all("th")]
    ci = {
        "name": col_index(headers, "name", 0),
        "gmp": col_index(headers, "gmp", 1),
        "price": col_index(headers, "price", 4),
        "open": col_index(headers, "open", 7),
        "close": col_index(headers, "close", 8),
    }
    ipos = []
    for tr in table.find_all("tr"):
        cells = tr.find_all("td")
        if len(cells) <= max(ci.values()):
            continue

        def text(col):
            return cells[ci[col]].get_text(" ", strip=True)

        name_cell = cells[ci["name"]]
        link = name_cell.find("a")
        name = link.get_text(" ", strip=True) if link else ""
        if not name or re.search(r"\bSME\b", name_cell.get_text(" ", strip=True)):
            continue
        badges = [b.get_text(strip=True) for b in name_cell.select("span.badge")]
        status = next((INVESTORGAIN_STATUS[b] for b in badges if b in INVESTORGAIN_STATUS),
                      "listed")

        gmp_text = text("gmp")
        no_gmp = not gmp_text or re.search(r"₹\s*--", gmp_text)
        gmp = None if no_gmp else signed_number(gmp_text)
        price = upper_price(text("price"))

        # Open/Close cells look like '27-Oct GMP: 44'; rebuild the '27-29 Oct' style.
        opens = re.search(r"(\d{1,2})\s*-\s*[A-Za-z]{3}", text("open"))
        closes = re.search(r"(\d{1,2})\s*-\s*([A-Za-z]{3})", text("close"))
        dates = f"{opens.group(1)}-{closes.group(1)} {closes.group(2)}" if opens and closes else ""

        ipos.append({
            "source": "investorgain",
            "segment": "mainboard",
            "name": name,
            "url": urljoin(SOURCE_URLS["investorgain"], link.get("href", "")),
            "status": status,
            "gmp": gmp,
            "price": price,
            "pct": None if no_gmp else gain_pct(gmp_text, gmp, price),
            "dates": dates,
        })
    return ipos


PARSERS = {"ipowatch": parse_ipowatch, "investorgain": parse_investorgain}


def close_date(dates: str, today: date):
    """'27-29 Oct' -> 29 Oct; '29-2 Nov' -> 2 Nov (month belongs to the end date)."""
    m = re.search(r"(\d{1,2})\s*[-–]\s*(\d{1,2})\s*([A-Za-z]{3})", dates)
    if not m:
        return None
    month = MONTHS.get(m.group(3).lower())
    if not month:
        return None
    year = today.year + (1 if month < today.month - 6 else 0)  # Dec -> Jan wrap
    try:
        return date(year, month, int(m.group(2)))
    except ValueError:
        return None


# ---------------------------------------------------------------- snapshots

SNAPSHOT_FIELDS = ("name", "url", "status", "gmp", "price", "pct", "dates")


def write_snapshot(source: str, rows: list[dict], now: datetime) -> bool:
    """Save a source's rows. Unchanged data is rewritten at most hourly, to keep commits quiet."""
    path = SNAPSHOT_DIR / f"{source}.json"
    slim = [{k: r[k] for k in SNAPSHOT_FIELDS} for r in rows]
    try:
        old = json.loads(path.read_text())
        age = now - datetime.fromisoformat(old["fetched_at"])
        if old["ipos"] == slim and age < timedelta(minutes=55):
            return False
    except (FileNotFoundError, json.JSONDecodeError, KeyError, ValueError):
        pass
    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(
        {"source": source, "fetched_at": now.isoformat(timespec="seconds"), "ipos": slim},
        indent=2, ensure_ascii=False) + "\n")
    return True


def load_snapshot(source: str, now: datetime) -> list[dict]:
    path = SNAPSHOT_DIR / f"{source}.json"
    try:
        data = json.loads(path.read_text())
        age = now - datetime.fromisoformat(data["fetched_at"])
    except (FileNotFoundError, json.JSONDecodeError, KeyError, ValueError):
        print(f"[info] no usable {source} snapshot; continuing without it")
        return []
    if age > SNAPSHOT_TTL:
        print(f"[info] {source} snapshot is {age.total_seconds() / 3600:.1f}h old; ignoring it")
        return []
    return [{"source": source, "segment": "mainboard", **row} for row in data["ipos"]]


# ---------------------------------------------------------------- merging

def norm_name(name: str) -> str:
    name = re.sub(r"\b(ipo|limited|ltd|pvt)\b", " ", name.lower())
    return re.sub(r"[^a-z0-9]+", "", name)


def merge(by_source: dict[str, list[dict]]) -> list[dict]:
    """One entry per company, with each source's figures under 'sources'."""
    merged: dict[str, dict] = {}
    for source in PRIORITY:
        for row in by_source.get(source, []):
            n = norm_name(row["name"])
            if not n:
                continue
            if n not in merged:
                close = difflib.get_close_matches(n, list(merged), n=1, cutoff=0.9)
                n = close[0] if close else n
            ipo = merged.setdefault(n, {
                "segment": row["segment"],
                "name": row["name"],
                "key": f"{row['segment']}:{n}",
                "status": row["status"],
                "dates": "",
                "sources": {},
            })
            ipo["sources"][source] = row
            ipo["dates"] = ipo["dates"] or row["dates"]
    for ipo in merged.values():
        pcts = [r["pct"] for r in ipo["sources"].values() if r["pct"] is not None]
        ipo["pct"] = max(pcts) if pcts else None
    return list(merged.values())


# ---------------------------------------------------------------- telegram

def send(text: str) -> bool:
    if DRY_RUN:
        print("---- [dry run] ----\n" + text + "\n")
        return True
    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{TOKEN}/sendMessage",
            data={"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML",
                  "disable_web_page_preview": "true"},
            timeout=20,
        )
        if resp.ok and resp.json().get("ok"):
            return True
        print(f"[error] Telegram {resp.status_code}: {resp.text[:200]}")
    except requests.RequestException as exc:
        print(f"[error] Telegram request failed: {exc}")
    return False


def money(x) -> str:
    return f"{x:,.0f}" if x is not None and x == int(x) else f"{x:,.2f}"


def source_line(row: dict) -> str:
    label = f"<i>{row['source']}</i>"
    if row["pct"] is None:
        return f"{label}: no GMP yet"
    if row["gmp"] is not None and row["price"]:
        return (f"{label}: GMP ₹{money(row['gmp'])} on ₹{money(row['price'])} → "
                f"est. ₹{money(row['price'] + row['gmp'])} ({row['pct']:+.2f}%)")
    return f"{label}: est. gain {row['pct']:+.2f}%"


def fmt(ipo: dict, headline: str) -> str:
    rows = [ipo["sources"][s] for s in PRIORITY if s in ipo["sources"]]
    lines = [f"{headline} <b>{html.escape(ipo['name'])}</b> — {ipo['status'].upper()}"]
    lines += [source_line(r) for r in rows]
    if ipo["dates"]:
        lines.append(f"Dates: {html.escape(ipo['dates'])}")
    links = [f'<a href="{html.escape(r["url"], quote=True)}">{r["source"]}</a>'
             for r in rows if r["url"]]
    if links:
        lines.append(" · ".join(links))
    return "\n".join(lines)


# ---------------------------------------------------------------- state

def load_state() -> dict:
    try:
        state = json.loads(STATE_FILE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        state = {}
    state.setdefault("ipos", {})
    state.setdefault("meta", {})
    # Older state was keyed by the GMP page URL; re-key it by company name.
    for key in [k for k in state["ipos"] if k.startswith("http")]:
        entry = state["ipos"].pop(key)
        state["ipos"].setdefault(f"mainboard:{norm_name(entry.get('name', ''))}", entry)
    return state


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2, sort_keys=True, ensure_ascii=False) + "\n")


# ---------------------------------------------------------------- main logic

def check(ipos: list[dict], state: dict, today: date, prune: bool) -> None:
    seen = set()
    for ipo in ipos:
        if ipo["pct"] is None:
            continue
        key = ipo["key"]
        seen.add(key)
        s = state["ipos"].setdefault(key, {"name": ipo["name"], "alerted": False})
        if ipo["status"] not in ACTIVE:
            continue

        pct, th = ipo["pct"], THRESHOLDS[ipo["segment"]]
        if pct >= th:
            if not s["alerted"]:
                if send(fmt(ipo, "🟢 GMP alert:")):
                    s.update(alerted=True, last_alert_pct=pct)
            elif abs(pct - s.get("last_alert_pct", pct)) >= REALERT_DELTA:
                prev = s["last_alert_pct"]
                arrow = "📈" if pct > prev else "📉"
                if send(fmt(ipo, f"{arrow} GMP update (was {prev:+.2f}%):")):
                    s["last_alert_pct"] = pct
            if (ipo["status"] == "open" and close_date(ipo["dates"], today) == today
                    and not s.get("closing_reminded")):
                if send(fmt(ipo, "⏰ Closes today:")):
                    s["closing_reminded"] = True
        elif s["alerted"]:
            if send(fmt(ipo, f"🔻 Dropped below {th:g}%:")):
                s.update(alerted=False, last_alert_pct=pct)

    # Forget IPOs that have left the tables, but only when every live source answered.
    if prune:
        state["ipos"] = {k: v for k, v in state["ipos"].items() if k in seen}


def digest(ipos: list[dict]) -> None:
    good = sorted(
        (i for i in ipos if i["status"] in ACTIVE and i["pct"] is not None
         and i["pct"] >= THRESHOLDS[i["segment"]]),
        key=lambda i: -i["pct"],
    )
    if not good:
        send("☀️ <b>Morning digest</b>\nNo open or upcoming Mainboard IPOs above "
             f"{THRESHOLDS['mainboard']:g}% right now.")
        return
    lines = ["☀️ <b>Morning digest</b> — Mainboard IPOs above threshold:"]
    for i in good:
        per_source = " / ".join(f"{s} {i['sources'][s]['pct']:+.2f}%" for s in PRIORITY
                                if s in i["sources"] and i["sources"][s]["pct"] is not None)
        lines.append(f"• <b>{html.escape(i['name'])}</b> [{i['status']}] "
                     f"{per_source} · {html.escape(i['dates'])}")
    send("\n".join(lines))


def snapshot_main(source: str) -> int:
    if source not in PARSERS:
        print(f"[error] unknown source {source!r}; choose from {', '.join(PARSERS)}")
        return 1
    rows = PARSERS[source](read_page(source))
    if not rows:
        print(f"[error] {source}: parsed 0 rows; keeping the previous snapshot")
        return 1
    changed = write_snapshot(source, rows, datetime.now(timezone.utc))
    print(f"{source}: {len(rows)} IPOs; snapshot {'written' if changed else 'unchanged'}")
    return 0


def collect(now: datetime):
    """Rows per source, plus which live sources failed (errors) or came back empty (empty)."""
    by_source: dict[str, list[dict]] = {}
    errors: list[str] = []
    empty: list[str] = []
    for source in LIVE_SOURCES:
        if source not in PARSERS:
            print(f"[warn] unknown live source {source!r}, skipping")
            continue
        try:
            rows = PARSERS[source](read_page(source))
        except (requests.RequestException, OSError) as exc:
            print(f"[error] {source}: {exc}")
            errors.append(source)
            continue
        if not rows:
            empty.append(source)
        by_source[source] = rows
    for source in SNAPSHOT_SOURCES:
        if source in PARSERS and source not in LIVE_SOURCES:
            by_source[source] = load_snapshot(source, now)
    return by_source, errors, empty


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] == "snapshot":
        return snapshot_main(sys.argv[2] if len(sys.argv) > 2 else "ipowatch")

    if not DRY_RUN and not (TOKEN and CHAT_ID):
        print("[error] Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID (or DRY_RUN=1).")
        return 1

    now = datetime.now(timezone.utc)
    today = now.astimezone(IST).date()
    state = load_state()
    by_source, errors, empty = collect(now)
    ipos = merge(by_source)
    used = ", ".join(f"{s} ({len(r)})" for s, r in by_source.items() if r) or "none"
    print(f"Parsed {len(ipos)} IPOs from {used}; thresholds {THRESHOLDS}; mode={MODE}")

    # A source that answers but yields no rows probably changed layout. Warn once a day each.
    for source in empty:
        key = f"parse_warning_{source}"
        if state["meta"].get(key) != today.isoformat():
            if send(f"⚠️ IPO GMP bot found 0 rows on {source}. "
                    "The page layout may have changed; the parser needs a look."):
                state["meta"][key] = today.isoformat()

    if not ipos:
        save_state(state)
        return 1 if errors and not empty else 0  # fetch errors fail the job so GitHub emails you

    check(ipos, state, today, prune=not errors and not empty)
    if MODE == "digest":
        digest(ipos)
    save_state(state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
