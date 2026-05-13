# Marktplaats new-listing alert

Polls Marktplaats every 5 minutes for new digital camera listings in your
filter (postcode 1033SC, today only) and pushes them to your Telegram
instantly. Filters out bumped / dagtopper ads so you only see genuinely new
posts.

## Setup (15 min, one time)

### 1. Make a Telegram bot

1. Open Telegram, search for **@BotFather**, start a chat
2. Send `/newbot`
3. Pick a name (e.g. "Marktplaats Camera Bot") and a username ending in `bot`
4. BotFather replies with an HTTP API token like
   `7891234567:AAH...`. **Save it.**

### 2. Get your chat ID

1. Search for the bot you just made and send it any message (e.g. "hi")
2. In your browser, visit:
   `https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates`
3. Look for `"chat":{"id":123456789,...}`. That number is your chat ID.

### 3. Create the GitHub repo

1. Make a new **private** repo on GitHub (private is fine for Actions free tier)
2. Push the three files in this folder to it:
   - `check.py`
   - `.github/workflows/check.yml`
   - `README.md` (this file)
3. In the repo, go to **Settings → Secrets and variables → Actions → New repository secret** and add:
   - `TELEGRAM_BOT_TOKEN` = your bot token
   - `TELEGRAM_CHAT_ID` = your chat ID

### 4. Done

The workflow runs every 5 minutes automatically. First run seeds the
seen-IDs file silently (no spam). After that, every new ad pings your phone.

You can also trigger it manually from the **Actions** tab → "Marktplaats
alert" → "Run workflow".

## Changing the search

Edit `SEARCH_PARAMS` at the top of `check.py`. Category IDs:
- `l1CategoryId` = top-level category (322 = Audio, tv en foto)
- `l2CategoryId` = subcategory (484 = Fotocamera's | Digitaal)

To find new IDs: open Marktplaats with your desired filters, then look at
the network request to `/lrp/api/search` in your browser's DevTools — it
will show the exact parameters.

## Changing the frequency

In `.github/workflows/check.yml`, change the cron line. Note: GitHub
Actions does not support intervals shorter than 5 minutes, and scheduled
runs may be delayed during high-load periods. For true sub-5-min
intervals you'd need a different host (Render / Railway / Fly.io / a
home server).

## Files

- `check.py` — the script
- `.github/workflows/check.yml` — schedule + runner
- `seen.json` — auto-created, tracks listings already alerted on
