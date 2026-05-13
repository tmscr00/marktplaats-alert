"""
Marktplaats new-listing alerter.

Calls the Marktplaats internal JSON API and pushes new (non-bumped) listings
to Telegram. This version is verbose on errors so we can see exactly what
Marktplaats complains about if the request is rejected.
"""

import json
import os
import sys
from pathlib import Path

import requests

# --- CONFIG -----------------------------------------------------------------

SEARCH_PARAMS = {
    "l1CategoryId": 322,   # Audio, tv en foto
    "l2CategoryId": 484,   # Fotocamera's | Digitaal
    "limit": "30",
    "offset": "0",
}

API_URL = "https://www.marktplaats.nl/lrp/api/search"
SEEN_FILE = Path("seen.json")
MAX_SEEN = 500
MAX_ALERTS_PER_RUN = 15

# Full browser-like header set. Marktplaats appears to validate these
# (sec-fetch-* in particular) on the /lrp/api/search endpoint.
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "nl-NL,nl;q=0.9,en-US;q=0.8,en;q=0.7",
    "Referer": "https://www.marktplaats.nl/l/audio-tv-en-foto/fotocamera-s-digitaal/",
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-origin",
    "sec-ch-ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
}

# --- TELEGRAM ---------------------------------------------------------------

TG_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TG_CHAT = os.environ.get("TELEGRAM_CHAT_ID")


def tg_send(text: str) -> None:
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


def fetch_listings_via_api() -> list[dict]:
    """Try the internal JSON endpoint."""
    s = requests.Session()
    s.headers.update(HEADERS)

    # Warm the session by hitting the category page first to pick up cookies.
    # Some endpoints reject "cold" requests with no consent cookies set.
    try:
        s.get(
            "https://www.marktplaats.nl/l/audio-tv-en-foto/fotocamera-s-digitaal/",
            timeout=20,
            allow_redirects=True,
        )
    except requests.RequestException as e:
        print(f"Warm-up request failed (non-fatal): {e}")

    r = s.get(API_URL, params=SEARCH_PARAMS, timeout=20)
    if not r.ok:
        # Show what the server actually said before we raise.
        print(f"HTTP {r.status_code} from API")
        print(f"Final URL: {r.url}")
        print(f"Response headers: {dict(r.headers)}")
        body = r.text[:1500]
        print(f"Response body (first 1500 chars):\n{body}")
        r.raise_for_status()
    data = r.json()
    return data.get("listings", [])


def is_real_new(listing: dict) -> bool:
    pp = listing.get("priorityProduct", "NONE")
    return pp in ("NONE", None, "")


def listing_url(listing: dict) -> str:
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
        or (listing.get("sellerInformation", {}) or {}).get("sellerCity", "")
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
    SEEN_FILE.write_text(json.dumps(ids[-MAX_SEEN:], indent=2))


# --- MAIN -------------------------------------------------------------------


def main() -> int:
    try:
        listings = fetch_listings_via_api()
    except Exception as e:
        print(f"Fetch failed: {e}")
        return 1

    print(f"Fetched {len(listings)} listings from API")

    first_run = not SEEN_FILE.exists()
    seen = load_seen()
    seen_set = set(seen)

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


if __name__ == "__main__":
    sys.exit(main())
