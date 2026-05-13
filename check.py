"""
Marktplaats new-listing alerter.

Loads the same HTML page a browser would and parses the embedded JSON state
Marktplaats injects into every page, so whatever filters work in your
browser URL work here.

Two run modes:
  - default          : real-monitor mode (only alerts on truly new listings)
  - --test           : force-alert on the most recent listing right now,
                       regardless of seen.json. Use this to verify the
                       Telegram pipe end-to-end without waiting.
"""

import json
import os
import re
import sys
from pathlib import Path

import requests

# --- CONFIG -----------------------------------------------------------------

TARGET_URL = (
    "https://www.marktplaats.nl/l/audio-tv-en-foto/fotocamera-s-digitaal/"
    "?offeredSince=Vandaag&postcode=1033SC"
)

SEEN_FILE = Path("seen.json")
MAX_SEEN = 500
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
    """Returns True on success."""
    if not TG_TOKEN or not TG_CHAT:
        print("WARN: Telegram secrets not set; would have sent:")
        print(text)
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
        print(f"Telegram OK ({r.status_code})")
        return True
    except requests.RequestException as e:
        print(f"Telegram exception: {e}")
        return False


# --- LISTING FETCH ----------------------------------------------------------


def fetch_listings_from_page() -> list[dict]:
    r = requests.get(TARGET_URL, headers=HEADERS, timeout=25)
    if not r.ok:
        print(f"HTTP {r.status_code} from page fetch")
        print(f"Response body (first 600 chars):\n{r.text[:600]}")
        r.raise_for_status()

    html = r.text
    print(f"Got {len(html)} bytes of HTML")

    m = re.search(r"window\.__CONFIG__\s*=\s*({.+?})\s*;\s*</script>", html, re.DOTALL)
    if m:
        try:
            cfg = json.loads(m.group(1))
            listings = _find_listings_in(cfg)
            if listings:
                print(f"Extracted {len(listings)} listings from __CONFIG__")
                return listings
        except json.JSONDecodeError as e:
            print(f"__CONFIG__ JSON decode failed: {e}")

    m = re.search(
        r'<script[^>]+id="__NEXT_DATA__"[^>]*>(.+?)</script>', html, re.DOTALL
    )
    if m:
        try:
            cfg = json.loads(m.group(1))
            listings = _find_listings_in(cfg)
            if listings:
                print(f"Extracted {len(listings)} listings from __NEXT_DATA__")
                return listings
        except json.JSONDecodeError as e:
            print(f"__NEXT_DATA__ JSON decode failed: {e}")

    m = re.search(r'"listings"\s*:\s*(\[[^\[\]]*?\{.+?\}\s*\])', html, re.DOTALL)
    if m:
        try:
            listings = json.loads(m.group(1))
            if isinstance(listings, list) and listings:
                print(f"Extracted {len(listings)} listings via regex fallback")
                return listings
        except json.JSONDecodeError:
            pass

    print("Could not locate listings JSON in the page.")
    return []


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


def is_real_new(listing: dict) -> bool:
    pp = listing.get("priorityProduct", "NONE")
    return pp in ("NONE", None, "")


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


# --- MAIN -------------------------------------------------------------------


def run_test_mode(listings: list[dict]) -> int:
    """Force-alert on the first organic listing, regardless of seen-state."""
    print("=== TEST MODE ===")
    print(f"Telegram token set: {bool(TG_TOKEN)}")
    print(f"Telegram chat set:  {bool(TG_CHAT)}")

    target = next((l for l in listings if is_real_new(l)), None)
    if not target:
        print("No organic listings found to test with.")
        return 1

    print(f"Sending test alert for itemId={target.get('itemId')}: "
          f"{target.get('title', '')[:60]}")
    ok = tg_send(format_message(target, prefix="🧪 TEST:"))
    return 0 if ok else 1


def run_normal_mode(listings: list[dict]) -> int:
    first_run = not SEEN_FILE.exists()
    print(f"first_run={first_run}, seen.json exists={SEEN_FILE.exists()}")

    seen = load_seen()
    seen_set = set(seen)
    print(f"Loaded {len(seen)} IDs from seen.json")

    new_listings = []
    for l in listings:
        item_id = str(l.get("itemId") or "")
        if not item_id or item_id in seen_set:
            continue
        if not is_real_new(l):
            seen.append(item_id)
            seen_set.add(item_id)
            continue
        new_listings.append(l)
        seen.append(item_id)
        seen_set.add(item_id)

    if first_run:
        print("First run — seeding seen.json without sending alerts.")
        save_seen(seen)
        return 0

    new_listings.reverse()

    if len(new_listings) > MAX_ALERTS_PER_RUN:
        print(f"Capping {len(new_listings)} new listings to {MAX_ALERTS_PER_RUN}")
        new_listings = new_listings[-MAX_ALERTS_PER_RUN:]

    print(f"Sending {len(new_listings)} alert(s)")
    for l in new_listings:
        tg_send(format_message(l))

    save_seen(seen)
    return 0


def main() -> int:
    test_mode = "--test" in sys.argv

    try:
        listings = fetch_listings_from_page()
    except Exception as e:
        print(f"Fetch failed: {e}")
        return 1

    if not listings:
        print("No listings extracted; aborting.")
        return 1

    print(f"Working with {len(listings)} listings")

    if test_mode:
        return run_test_mode(listings)
    return run_normal_mode(listings)


if __name__ == "__main__":
    sys.exit(main())
