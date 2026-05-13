"""
Marktplaats new-listing alerter.

Instead of guessing the internal API's exact parameters, this version
loads the same HTML page a browser would (the category page with your
filters) and parses the embedded JSON state Marktplaats injects into
every page. This means whatever filters work in your browser URL will
work here, because we ARE the browser URL.
"""

import json
import os
import re
import sys
from pathlib import Path

import requests

# --- CONFIG -----------------------------------------------------------------

# Your real Marktplaats URL, with the hash-fragment filters converted to
# regular query parameters (Marktplaats's HTML page understands both).
# Original link: https://www.marktplaats.nl/l/audio-tv-en-foto/fotocamera-s-digitaal/#offeredSince:Vandaag|postcode:1033SC
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


def fetch_listings_from_page() -> list[dict]:
    """
    Fetch the category page HTML and extract the embedded listings JSON.

    Marktplaats injects a __CONFIG__ / __NEXT_DATA__-style blob into every
    listing page that contains the same data the JSON API returns. By
    parsing that out, we don't need to know any API param names.
    """
    r = requests.get(TARGET_URL, headers=HEADERS, timeout=25)
    if not r.ok:
        print(f"HTTP {r.status_code} from page fetch")
        print(f"Response body (first 600 chars):\n{r.text[:600]}")
        r.raise_for_status()

    html = r.text
    print(f"Got {len(html)} bytes of HTML")

    # Try several known embeddings, in order of likelihood.
    # 1) The custom Marktplaats embed: window.__CONFIG__ = {...};
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

    # 2) Next.js style: <script id="__NEXT_DATA__" type="application/json">{...}</script>
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

    # 3) Last resort — any inline JSON containing a "listings" array.
    m = re.search(r'"listings"\s*:\s*(\[[^\[\]]*?\{.+?\}\s*\])', html, re.DOTALL)
    if m:
        try:
            listings = json.loads(m.group(1))
            if isinstance(listings, list) and listings:
                print(f"Extracted {len(listings)} listings via regex fallback")
                return listings
        except json.JSONDecodeError:
            pass

    print("Could not locate listings JSON in the page. Saving first 2000 "
          "chars of HTML for inspection:")
    print(html[:2000])
    return []


def _find_listings_in(obj):
    """Walk a nested dict/list and return the first 'listings' array found."""
    if isinstance(obj, dict):
        if isinstance(obj.get("listings"), list) and obj["listings"]:
            # Make sure it looks like ad data, not unrelated lists.
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
    item_id = listing.get("itemId", "")
    return f"https://www.marktplaats.nl/v/{item_id}" if item_id else "https://www.marktplaats.nl/"


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
        listings = fetch_listings_from_page()
    except Exception as e:
        print(f"Fetch failed: {e}")
        return 1

    if not listings:
        print("No listings extracted; aborting this run (will retry on next schedule).")
        return 1

    print(f"Working with {len(listings)} listings")

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
