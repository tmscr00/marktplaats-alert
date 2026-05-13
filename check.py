"""
Marktplaats new-listing alerter.

Loads the listing page HTML, parses embedded JSON state, filters out
paid/promoted ads, dedupes against seen.json, pings Telegram on new items.

Modes:
  default            : check the first 3 pages once, then exit
  --test             : send a test Telegram for the first organic listing
  --loop N           : run repeatedly for N minutes, checking every 60s.
                       (Used by the GitHub Actions workflow to keep checks
                       coming even when the scheduler is slow.)
"""

import json
import os
import re
import sys
import time
from pathlib import Path

import requests

# --- CONFIG -----------------------------------------------------------------

# Page 1, 2, 3 — Marktplaats's filter sort isn't strictly newest-first, so we
# scan multiple pages and let seen.json handle dedup.
TARGET_URLS = [
    "https://www.marktplaats.nl/l/audio-tv-en-foto/fotocamera-s-digitaal/"
    "?offeredSince=Vandaag&postcode=1033SC",
    "https://www.marktplaats.nl/l/audio-tv-en-foto/fotocamera-s-digitaal/p/2/"
    "?offeredSince=Vandaag&postcode=1033SC",
    "https://www.marktplaats.nl/l/audio-tv-en-foto/fotocamera-s-digitaal/p/3/"
    "?offeredSince=Vandaag&postcode=1033SC",
]

SEEN_FILE = Path("seen.json")
MAX_SEEN = 1000
MAX_ALERTS_PER_RUN = 15

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,*/*;q=0.8"
    ),
    "Accept-Language": "nl-NL,nl;q=0.9,en-US;q=0.8,en;q=0.7",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
}

# --- TELEGRAM ---------------------------------------------------------------

TG_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TG_CHAT = os.environ.get("TELEGRAM_CHAT_ID")


def tg_send(text: str) -> bool:
    if not TG_TOKEN or not TG_CHAT:
        print("WARN: Telegram secrets not set")
        return False
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            json={
                "chat_id": TG_CHAT,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": False,
            },
            timeout=15,
        )
        if not r.ok:
            print(f"Telegram error {r.status_code}: {r.text}")
            return False
        return True
    except requests.RequestException as e:
        print(f"Telegram exception: {e}")
        return False


# --- LISTING FETCH ----------------------------------------------------------


def fetch_page(url: str) -> list[dict]:
    try:
        r = requests.get(url, headers=HEADERS, timeout=25)
        r.raise_for_status()
    except requests.RequestException as e:
        print(f"  fetch failed for {url}: {e}")
        return []

    html = r.text

    m = re.search(r"window\.__CONFIG__\s*=\s*({.+?})\s*;\s*</script>", html, re.DOTALL)
    if m:
        try:
            cfg = json.loads(m.group(1))
            listings = _find_listings_in(cfg)
            if listings:
                return listings
        except json.JSONDecodeError:
            pass

    m = re.search(r'<script[^>]+id="__NEXT_DATA__"[^>]*>(.+?)</script>', html, re.DOTALL)
    if m:
        try:
            cfg = json.loads(m.group(1))
            listings = _find_listings_in(cfg)
            if listings:
                return listings
        except json.JSONDecodeError:
            pass

    return []


def fetch_all_listings() -> list[dict]:
    """Fetch all configured pages and merge, deduped by itemId."""
    seen_ids = set()
    out = []
    for url in TARGET_URLS:
        page_listings = fetch_page(url)
        kept = 0
        for l in page_listings:
            iid = str(l.get("itemId") or "")
            if iid and iid not in seen_ids:
                seen_ids.add(iid)
                out.append(l)
                kept += 1
        print(f"  page {url.split('/p/')[1].split('/')[0] if '/p/' in url else '1'}: "
              f"{len(page_listings)} listings, {kept} new this fetch")
    return out


def _find_listings_in(obj):
    if isinstance(obj, dict):
        if isinstance(obj.get("listings"), list) and obj["listings"]:
            first = obj["listings"][0]
            if isinstance(first, dict) and ("itemId" in first or "title" in first):
                return obj["listings"]
        for v in obj.values():
            found = _find_listings_in(v)
            if found:
                return found
    elif isinstance(obj, list):
        for v in obj:
            found = _find_listings_in(v)
            if found:
                return found
    return None


# --- FILTER -----------------------------------------------------------------


# Map Dutch short month names to numbers.
DUTCH_MONTHS = {
    "jan": 1, "feb": 2, "mrt": 3, "apr": 4, "mei": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "okt": 10, "nov": 11, "dec": 12,
}


def is_real_new(listing: dict) -> bool:
    """First-pass filter: reject Admarkt + Dagtopper + paid traits."""
    item_id = str(listing.get("itemId") or "")
    if not item_id.startswith("m"):
        return False

    pp = listing.get("priorityProduct", "NONE")
    if pp not in ("NONE", None, ""):
        return False

    traits = listing.get("traits") or []
    bad_traits = {
        "ADMARKT_CONSOLE", "ADMARKT", "DAG_TOPPER", "DAG_TOPPER_7DAYS",
        "TOPADVERTENTIE", "PROFILE",
    }
    if any(t in bad_traits for t in traits):
        return False

    return True


def verify_posted_today(listing: dict) -> bool:
    """
    Second-pass filter: fetch the listing's own page and read the
    'Sinds DD mmm '26' line. The page-list 'date' field shows when an
    ad was last bumped, not posted, so we can't trust it. Returns True
    only if the listing's actual creation date is today.

    One HTTP request per genuinely-new listing (after seen.json dedup),
    so cost is small.
    """
    vip = listing.get("vipUrl") or ""
    if not vip:
        return False
    url = vip if vip.startswith("http") else f"https://www.marktplaats.nl{vip}"

    try:
        r = requests.get(url, headers=HEADERS, timeout=20)
        r.raise_for_status()
    except requests.RequestException as e:
        print(f"     verify-date fetch failed for {listing.get('itemId')}: {e}")
        return False

    # Look for "Sinds 12 mei '26" or "Sinds 12 mei 2026"
    m = re.search(r"Sinds\s+(\d{1,2})\s+([a-z]{3,4})\s+'?(\d{2,4})", r.text, re.IGNORECASE)
    if not m:
        print(f"     verify-date: could not find 'Sinds' line for {listing.get('itemId')}")
        return False

    day = int(m.group(1))
    month_name = m.group(2).lower()[:3]
    year_raw = m.group(3)
    year = int(year_raw) if len(year_raw) == 4 else 2000 + int(year_raw)
    month = DUTCH_MONTHS.get(month_name)
    if month is None:
        print(f"     verify-date: unknown month '{month_name}' for {listing.get('itemId')}")
        return False

    # Compare with today (in NL local time = UTC+1 or +2; use UTC since the
    # difference doesn't matter on the day boundary unless we run at midnight,
    # and we just check the date once anyway).
    from datetime import datetime, timezone, timedelta
    nl_now = datetime.now(timezone.utc) + timedelta(hours=2)  # CEST in May
    today = (nl_now.year, nl_now.month, nl_now.day)
    listing_date = (year, month, day)
    is_today = listing_date == today
    if not is_today:
        print(f"     verify-date: {listing.get('itemId')} posted "
              f"{listing_date}, not today {today} — skipping")
    return is_today


# --- FORMAT -----------------------------------------------------------------


def listing_url(listing: dict) -> str:
    vip = listing.get("vipUrl") or ""
    if vip.startswith("http"):
        return vip
    if vip:
        return f"https://www.marktplaats.nl{vip}"
    return "https://www.marktplaats.nl/"


def price_text(listing: dict) -> str:
    pi = listing.get("priceInfo", {}) or {}
    cents = pi.get("priceCents")
    ptype = pi.get("priceType", "")
    if ptype == "FIXED" and cents is not None:
        return f"€{cents/100:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    mapping = {
        "BIDDING": "Bieden",
        "SEE_DESCRIPTION": "Zie omschrijving",
        "RESERVED": "Gereserveerd",
        "FREE": "Gratis",
        "EXCHANGE": "Ruilen",
        "ON_DEMAND": "Op aanvraag",
        "NOTK": "n.o.t.k.",
        "MIN_BID": f"Vanaf €{cents/100:.2f}" if cents else "Bieden vanaf",
    }
    return mapping.get(ptype, ptype or "—")


def escape_html(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def format_message(listing: dict, prefix: str = "📸") -> str:
    title = escape_html(listing.get("title", "(geen titel)"))
    price = escape_html(price_text(listing))
    loc = escape_html(
        (listing.get("location", {}) or {}).get("cityName", "")
        or (listing.get("sellerInformation", {}) or {}).get("sellerCity", "")
        or "—"
    )
    seller = escape_html(
        (listing.get("sellerInformation", {}) or {}).get("sellerName", "—")
    )
    url = listing_url(listing)
    return (
        f"{prefix} <b>{title}</b>\n"
        f"💶 {price}\n"
        f"📍 {loc}  ·  👤 {seller}\n"
        f"<a href=\"{url}\">Bekijk advertentie</a>"
    )


# --- SEEN-IDS PERSISTENCE ---------------------------------------------------


def load_seen() -> list[str]:
    if not SEEN_FILE.exists():
        return []
    try:
        return json.loads(SEEN_FILE.read_text())
    except json.JSONDecodeError:
        return []


def save_seen(ids: list[str]) -> None:
    SEEN_FILE.write_text(json.dumps(ids[-MAX_SEEN:], indent=2))


# --- RUN MODES --------------------------------------------------------------


def one_check() -> int:
    """One full check: fetch all pages, alert on new, save seen.json. Returns count alerted."""
    print(f"[{time.strftime('%H:%M:%S')}] fetching...")
    listings = fetch_all_listings()
    print(f"  total unique listings across pages: {len(listings)}")

    if not listings:
        print("  no listings returned — skipping this check")
        return 0

    first_run = not SEEN_FILE.exists()
    seen = load_seen()
    seen_set = set(seen)

    new_listings = []
    seen_already = 0
    filtered_out = 0
    for l in listings:
        item_id = str(l.get("itemId") or "")
        if not item_id:
            continue
        if item_id in seen_set:
            seen_already += 1
            continue
        if not is_real_new(l):
            filtered_out += 1
            seen.append(item_id)
            seen_set.add(item_id)
            continue
        new_listings.append(l)
        seen.append(item_id)
        seen_set.add(item_id)

    print(f"  seen-already: {seen_already}, "
          f"filtered-as-paid: {filtered_out}, "
          f"candidates-for-verify: {len(new_listings)}")

    if first_run:
        print("  first run — seeding seen.json without sending alerts.")
        save_seen(seen)
        return 0

    new_listings.reverse()
    if len(new_listings) > MAX_ALERTS_PER_RUN:
        new_listings = new_listings[-MAX_ALERTS_PER_RUN:]

    # Second-pass: verify each candidate was actually posted TODAY by
    # looking at its own listing page (the page-list date is when it was
    # last bumped, not when posted, so we can't trust it).
    verified = []
    for l in new_listings:
        if verify_posted_today(l):
            verified.append(l)

    if verified:
        print(f"  📨 sending {len(verified)} alert(s) (of {len(new_listings)} candidates)")
        for l in verified:
            ok = tg_send(format_message(l))
            print(f"     - {l.get('itemId')} {'✅' if ok else '❌'} {l.get('title','')[:50]}")
    else:
        if new_listings:
            print(f"  {len(new_listings)} new-to-us listings but none verified as posted today")
        else:
            print("  nothing new")

    save_seen(seen)
    return len(verified)


def run_test_mode() -> int:
    print("=== TEST MODE ===")
    listings = fetch_all_listings()
    if not listings:
        print("no listings.")
        return 1
    print(f"\nFilter walk-through ({len(listings)} listings):")
    kept = []
    for i, l in enumerate(listings):
        ok = is_real_new(l)
        status = "✅ KEEP" if ok else "❌ skip"
        date = (l.get("date") or "")[:20]
        print(f"  {i+1:2d}. {status}  id={l.get('itemId')}  "
              f"pp={l.get('priorityProduct')}  date={date}  "
              f"traits={(l.get('traits') or [])[:3]}")
        if ok:
            kept.append(l)

    print(f"\n{len(kept)} listings passed the filter.")
    if kept:
        print("\nFull data for each KEPT listing (so we can spot any that shouldn't pass):")
        for l in kept[:5]:
            print(f"\n  itemId  : {l.get('itemId')}")
            print(f"  title   : {l.get('title','')[:70]}")
            print(f"  date    : {l.get('date')}")
            print(f"  pp      : {l.get('priorityProduct')}")
            print(f"  traits  : {l.get('traits')}")
            print(f"  seller  : {(l.get('sellerInformation') or {}).get('sellerName')}")
            print(f"  vipUrl  : {l.get('vipUrl','')[:90]}")

    if not kept:
        print("\nNo organic listings to test with right now.")
        return 1
    target = kept[0]
    print(f"\nSending test alert for itemId={target.get('itemId')}")
    ok = tg_send(format_message(target, prefix="🧪 TEST:"))
    return 0 if ok else 1


def run_loop_mode(minutes: int) -> int:
    """Check every 60 seconds for `minutes` minutes."""
    print(f"=== LOOP MODE: {minutes} minutes, checking every 60s ===")
    end = time.time() + minutes * 60
    checks = 0
    total_alerts = 0
    while time.time() < end:
        checks += 1
        print(f"\n--- check #{checks} ---")
        try:
            total_alerts += one_check()
        except Exception as e:
            print(f"  check failed: {e}")
        # Don't sleep past the end time
        remaining = end - time.time()
        if remaining <= 0:
            break
        time.sleep(min(60, remaining))
    print(f"\n=== loop done: {checks} checks, {total_alerts} alerts sent ===")
    return 0


# --- MAIN -------------------------------------------------------------------


def main() -> int:
    if "--test" in sys.argv:
        return run_test_mode()

    if "--loop" in sys.argv:
        i = sys.argv.index("--loop")
        try:
            minutes = int(sys.argv[i + 1])
        except (IndexError, ValueError):
            minutes = 4
        return run_loop_mode(minutes)

    one_check()
    return 0


if __name__ == "__main__":
    sys.exit(main())
