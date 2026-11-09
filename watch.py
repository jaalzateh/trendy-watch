"""Trendy advent-calendar watcher.

Polls the public catalog of maquillajetrendyshop.com (a VTEX store) and alerts
the moment a product matching KEYWORDS shows up or becomes buyable.
Standard library only - no pip install needed.

  python watch.py          one check (or a loop, see LOOP_SECONDS)
  python watch.py --test   send a test alert to every configured channel

Channels are switched on by environment variables (set any, all, or none):
  NTFY_TOPIC                       push to the ntfy phone app (no account needed)
  NTFY_EMAIL                       also have ntfy email this address
  TELEGRAM_TOKEN, TELEGRAM_CHAT    Telegram bot
  WHATSAPP_PHONE, WHATSAPP_APIKEY  WhatsApp through CallMeBot
"""
import json
import os
import re
import sys
import time
import unicodedata
import urllib.parse
import urllib.request

STORE = "https://www.maquillajetrendyshop.com"
KEYWORDS = [k.strip() for k in os.environ.get("KEYWORDS", "adviento,advent").split(",") if k.strip()]
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state.json")
LOOP_SECONDS = int(os.environ.get("LOOP_SECONDS", "0"))   # keep re-checking for this long
INTERVAL = int(os.environ.get("INTERVAL", "60"))          # seconds between checks in a loop
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/130 Safari/537.36"
PAGE = 50  # VTEX maximum per request


def http(url, data=None, headers=None, timeout=30):
    req = urllib.request.Request(url, data=data, headers={"User-Agent": UA, **(headers or {})})
    for attempt in range(3):  # the store answers 503 now and then; a short retry rides it out
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read().decode("utf-8", "replace")
        except Exception:
            if attempt == 2:
                raise
            time.sleep(3 * (attempt + 1))


def norm(text):
    text = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode()
    return text.lower()


def matches(text):
    t = norm(text)
    return any(norm(k) in t for k in KEYWORDS)


# ---------- what the store shows right now ----------

def catalog():
    """Every product in the store, straight from the public catalog API."""
    products, start = [], 0
    while True:
        # the timestamp param dodges the CDN cache so we never read a stale page
        url = f"{STORE}/api/catalog_system/pub/products/search?_from={start}&_to={start + PAGE - 1}&O=OrderByReleaseDateDESC&_t={int(time.time())}"
        page = json.loads(http(url))
        products += page
        if len(page) < PAGE:
            break
        start += PAGE
    if len(products) < 20:
        raise RuntimeError(f"catalog returned only {len(products)} products - the store API may have changed")
    return products


def snapshot():
    """{key: {name, url, status}} for everything that looks like the calendar."""
    found = {}
    for p in catalog():
        if not matches(p.get("productName", "") + " " + p.get("linkText", "")):
            continue
        offers = [s.get("commertialOffer", {}) for i in p.get("items", []) for s in i.get("sellers", [])]
        qty = sum(o.get("AvailableQuantity") or 0 for o in offers)
        price = next((o.get("Price") for o in offers if o.get("Price")), None)
        found["product:" + str(p["productId"])] = {
            "name": p["productName"],
            "url": f"{STORE}/{p['linkText']}/p",
            "status": "available" if qty > 0 else "listed",
            "price": price,
        }
    # Second signal: the home page usually links a landing page before the product goes live.
    home = http(f"{STORE}/?_t={int(time.time())}")
    for slug in set(re.findall(r"[a-z0-9-]+", home)):
        if matches(slug) and "-" in slug and len(slug) < 80:
            found.setdefault("page:" + slug, {"name": slug, "url": f"{STORE}/{slug}", "status": "page", "price": None})
    return found


# ---------- alerts ----------

def notify(title, body, url, urgent=True):
    print(f"ALERT: {title} | {body} | {url}")
    sent = []
    topic = os.environ.get("NTFY_TOPIC")
    if topic:
        msg = {"topic": topic, "title": title, "message": body, "click": url,
               "priority": 5 if urgent else 3, "tags": ["rotating_light" if urgent else "eyes"],
               "actions": [{"action": "view", "label": "Open store", "url": url}]}
        if os.environ.get("NTFY_EMAIL"):
            msg["email"] = os.environ["NTFY_EMAIL"]
        sent.append(("ntfy", lambda: http("https://ntfy.sh", json.dumps(msg).encode(), {"Content-Type": "application/json"})))
    text = f"{title}\n{body}\n{url}"
    if os.environ.get("TELEGRAM_TOKEN") and os.environ.get("TELEGRAM_CHAT"):
        data = urllib.parse.urlencode({"chat_id": os.environ["TELEGRAM_CHAT"], "text": text}).encode()
        sent.append(("telegram", lambda: http(f"https://api.telegram.org/bot{os.environ['TELEGRAM_TOKEN']}/sendMessage", data)))
    if os.environ.get("WHATSAPP_PHONE") and os.environ.get("WHATSAPP_APIKEY"):
        q = urllib.parse.urlencode({"phone": os.environ["WHATSAPP_PHONE"], "text": text, "apikey": os.environ["WHATSAPP_APIKEY"]})
        sent.append(("whatsapp", lambda: http("https://api.callmebot.com/whatsapp.php?" + q)))
    ok = True
    for name, send in sent:
        try:
            send()
            print(f"  sent via {name}")
        except Exception as e:  # one broken channel must not block the others
            ok = False
            print(f"  {name} FAILED: {e}")
    if not sent:
        print("  (no channel configured - printed only)")
    return ok


def check():
    first_run = not os.path.exists(STATE_FILE)
    old = {} if first_run else json.load(open(STATE_FILE, encoding="utf-8"))
    new = snapshot()
    ok = True
    for key, item in new.items():
        before = old.get(key, {}).get("status")
        if item["status"] == before:
            continue
        price = f" - ${item['price']:,.0f}" if item.get("price") else ""
        if item["status"] == "available":
            ok &= notify("Trendy: calendario de adviento DISPONIBLE", f"{item['name']}{price}. Corre a comprarlo!", item["url"])
        elif item["status"] == "listed" and before is None:
            ok &= notify("Trendy: el calendario ya aparece en el catalogo", f"{item['name']}{price} (aun sin stock)", item["url"], urgent=False)
        elif item["status"] == "page" and not first_run:
            ok &= notify("Trendy: nueva pagina de calendario de adviento", item["name"], item["url"], urgent=False)
    if not ok:
        # leave the state untouched so the alert is retried on the next check
        raise RuntimeError("an alert could not be delivered")
    merged = {**old, **new}
    for key in old:  # a product that vanished or sold out can alert again when it returns
        if key.startswith("product:") and key not in new:
            merged.pop(key)
    if merged != old:
        json.dump(merged, open(STATE_FILE, "w", encoding="utf-8"), indent=1, ensure_ascii=False, sort_keys=True)
    live = [v["name"] for v in new.values() if v["status"] == "available"]
    print(time.strftime("%H:%M:%S"), f"ok - {len(new)} match(es), {len(live)} buyable")


if __name__ == "__main__":
    if "--test" in sys.argv:
        sys.exit(0 if notify("Trendy watcher: prueba", "Si lees esto, las alertas funcionan.", STORE) else 1)
    deadline = time.time() + LOOP_SECONDS
    last_ok = False
    while True:
        try:
            check()
            last_ok = True
        except Exception as e:
            last_ok = False
            print("check failed:", e)
        if time.time() + INTERVAL > deadline:
            break
        time.sleep(INTERVAL)
    # Fail (so GitHub emails you) only if the watcher ended blind, not on a blip that recovered.
    sys.exit(0 if last_ok else 1)
