# Advent-calendar watcher (Blush Bar)

Checks [blush-bar.com](https://www.blush-bar.com) every 30 seconds and alerts your phone the moment
a "Calendario de adviento" appears in the catalog or becomes buyable. Runs for free on GitHub
Actions, so your computer can be off. Each run watches for 5 hours and then starts its successor.

To watch another store on the same platform (VTEX), add one line to `STORES` in `watch.py`.

## Setup (about 5 minutes, once)

1. **Phone alerts.** Install the **ntfy** app (Android / iPhone). Tap **+**, subscribe to a topic
   name only you would guess, for example `trendy-adviento-k7x92q`. Anyone who knows the name can
   read the alerts, so keep it random.
2. **Repository.** Create a new **public** repository on GitHub (public = unlimited free minutes;
   nothing private is in the code) and upload everything in this folder, including `.github`.
3. **Secret.** In the repository: *Settings → Secrets and variables → Actions → New repository secret*.
   Name `NTFY_TOPIC`, value = your topic name.
4. **Test.** *Actions → Trendy watch → Run workflow*, tick **Send a test alert**. Your phone should buzz.

Done. It now runs by itself.

## Optional channels (add as extra secrets)

| Channel  | Secrets | How to get them |
|----------|---------|-----------------|
| Email    | `NTFY_EMAIL` | Just your address. ntfy sends the email (limited to a few per day). |
| Telegram | `TELEGRAM_TOKEN`, `TELEGRAM_CHAT` | Create a bot with @BotFather; get your chat id from @userinfobot. |
| WhatsApp | `WHATSAPP_PHONE`, `WHATSAPP_APIKEY` | [CallMeBot](https://www.callmebot.com/blog/free-api-whatsapp-messages/): message their number once and it replies with your key. |

## What you will receive

- **Urgent** (bypasses silent mode in ntfy): a calendar product has stock - tap to open it.
- **Normal**: a calendar product was listed without stock yet, or the home page links a new calendar page.
- An email from GitHub if the watcher itself stops working.

## Run it on your own computer instead

```bash
python watch.py --test
```

```bash
python watch.py
```

Set `NTFY_TOPIC` (and friends) as environment variables first. `KEYWORDS` changes what is matched
(default `adviento,advent`).
