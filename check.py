"""
Marktplaats new-listing alerter.

Polls the Marktplaats internal JSON API for a fixed search (digital cameras,
postcode 1033SC, offeredSince=Vandaag), compares against a local seen-IDs
file, and pushes Telegram notifications for genuinely new listings.

Filters out bumped / "dagtopper" / priority ads so we only alert on real
new posts, which is exactly what the user wanted (Marktplaats's own sort
mixes those in and is the reason new ads don't appear at the top).
"""

import json
import os
import sys
from pathlib import Path

import requests

# --- CONFIG -----------------------------------------------------------------

# Tweak these freely. Built from the user's link:
# https://www.marktplaats.nl/l/audio-tv-en-foto/fotocamera-s-digitaal/#offeredSince:Vandaag|postcode:1033SC
SEARCH_PARAMS = {
    "l1CategoryId": 322,     # Audio, tv en foto
    "l2CategoryId": 484,     # Fotocamera's | Digitaal
    "postcode": "1033SC",
    "offeredSince": "Vandaag",
    "limit": 30,
    "offset": 0,
    "sortBy": "SORT_INDEX",
    "sortOrder": "DECREASING",
}

API_URL = "https://www.marktplaats.nl/lrp/api/search"
SEEN_FILE = Path("seen.json")
MAX_SEEN = 500  # keep the file small; older IDs age out

# Cap how many notifications we send in a single run. On the very first run
# the seen-list is empty, so without this we'd spam every "today" listing.
MAX_ALERTS_PER_RUN = 15

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Accept-Language": "nl-NL,nl;q=0.9,en;q=0.8",
}

# --- TELEGRAM ---------------------------------------------------------------

TG_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TG_CHAT = os.environ.get("TELEGRAM_CHAT_ID")


def tg_send(text: str) -> None:
    """Send a Telegram message. Silently logs failures and continues."""
    if not TG_TOKEN or not TG_CHAT:
        print("WARN: Telegram secrets not set; would have sent:\n", text)
        return
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
    except requests.RequestException as e:
        print(f"Telegram exception: {e}")


# --- LISTING FETCH ----------------------------------------------------------


def fetch_listings() -> list[dict]:
    """Query the Marktplaats internal JSON API."""
    r = requests.get(API_URL, params=SEARCH_PARAMS, headers=HEADERS, timeout=20)
    r.raise_for_status()
    data = r.json()
    return data.get("listings", [])


def is_real_new(listing: dict) -> bool:
    """
    Skip bumped / paid-priority ads so we only alert on genuinely new posts.

    Marktplaats marks bumped/priority ads via `priorityProduct` (e.g. "DAGTOPPER",
    "TOPADVERTENTIE", "BASIC") and/or a non-empty `verticals`/`extendedAttributes`
    indicating promotion. The safest signal is `priorityProduct`: only "NONE" (or
    missing) is a normal organic ad.
    """
    pp = listing.get("priorityProduct", "NONE")
    return pp in ("NONE", None, "")


def listing_url(listing: dict) -> str:
    """Build a full URL from the API's relative vipUrl."""
    vip = listing.get("vipUrl") or ""
    if vip.startswith("http"):
        return vip
    return f"https://www.marktplaats.nl{vip}"


def price_text(listing: dict) -> str:
    pi = listing.get("priceInfo", {}) or {}
    cents = pi.get("priceCents")
    ptype = pi.get("priceType", "")
    if ptype == "FIXED" and cents is not None:
        return f"€{cents/100:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    # Common non-fixed types: BIDDING, SEE_DESCRIPTION, RESERVED, FREE, ON_DEMAND, etc.
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


def format_message(listing: dict) -> str:
    title = escape_html(listing.get("title", "(geen titel)"))
    price = escape_html(price_text(listing))
    loc = escape_html(
        (listing.get("location", {}) or {}).get("cityName", "")
        or listing.get("sellerInformation", {}).get("sellerCity", "")
        or "—"
    )
    seller = escape_html(
        (listing.get("sellerInformation", {}) or {}).get("sellerName", "—")
    )
    url = listing_url(listing)
    return (
        f"📸 <b>{title}</b>\n"
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
    # Trim to the most recent MAX_SEEN entries.
    SEEN_FILE.write_text(json.dumps(ids[-MAX_SEEN:], indent=2))


# --- MAIN -------------------------------------------------------------------


def main() -> int:
    try:
        listings = fetch_listings()
    except Exception as e:
        print(f"Fetch failed: {e}")
        return 1

    print(f"Fetched {len(listings)} listings from API")

    seen = load_seen()
    seen_set = set(seen)

    new_listings = []
    for l in listings:
        item_id = str(l.get("itemId") or "")
        if not item_id:
            continue
        if item_id in seen_set:
            continue
        if not is_real_new(l):
            # Still mark as seen so we don't reconsider next run.
            seen.append(item_id)
            seen_set.add(item_id)
            continue
        new_listings.append(l)
        seen.append(item_id)
        seen_set.add(item_id)

    # First-run protection: if seen.json was empty, don't spam every "today" ad.
    first_run = len(seen) == len([l for l in listings if str(l.get("itemId") or "")])
    if first_run and not SEEN_FILE.exists():
        print("First run detected — seeding seen.json without sending alerts.")
        save_seen(seen)
        return 0

    # Oldest first, so notifications arrive in chronological order.
    new_listings.reverse()

    if len(new_listings) > MAX_ALERTS_PER_RUN:
        print(f"Capping {len(new_listings)} new listings down to {MAX_ALERTS_PER_RUN}")
        new_listings = new_listings[-MAX_ALERTS_PER_RUN:]

    print(f"Sending {len(new_listings)} alert(s)")
    for l in new_listings:
        tg_send(format_message(l))

    save_seen(seen)
    return 0


if __name__ == "__main__":
    sys.exit(main())
