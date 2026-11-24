"""Advent-calendar watcher for online stores (currently Girly, a WooCommerce store).

Polls each store's public catalog and alerts the moment a product matching
KEYWORDS shows up or becomes buyable. Standard library only.

  python watch.py          one check (or a loop, see LOOP_SECONDS)
  python watch.py --test   send a test alert to every configured channel

Channels are switched on by environment variables (set any, all, or none):
  NTFY_TOPIC                       push to the ntfy phone app (no account needed)
  NTFY_EMAIL                       also have ntfy email this address
  TELEGRAM_TOKEN, TELEGRAM_CHAT    Telegram bot
  WHATSAPP_PHONE, WHATSAPP_APIKEY  WhatsApp through CallMeBot
"""
import html
import json
import os
import re
import sys
import time
import unicodedata
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

# Stores to watch. "kind" picks the adapter: "woo" (WordPress + WooCommerce) or "vtex".
# "prefix" namespaces a store's entries in state.json.
# Watched earlier with the vtex adapter, until their calendars launched:
#   {"name": "Trendy", "kind": "vtex", "url": "https://www.maquillajetrendyshop.com", "prefix": "trendy:"}
#   {"name": "Blush Bar", "kind": "vtex", "url": "https://www.blush-bar.com", "prefix": "blushbar:"}
STORES = [
    {"name": "Girly", "kind": "woo", "url": "https://girlymaquillaje.com", "prefix": "girly:"},
]
KEYWORDS = [k.strip() for k in os.environ.get("KEYWORDS", "adviento,advent").split(",") if k.strip()]
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state.json")
LOOP_SECONDS = int(os.environ.get("LOOP_SECONDS", "0"))   # keep re-checking for this long
INTERVAL = int(os.environ.get("INTERVAL", "60"))          # seconds between checks in a loop
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
PAGE = 50  # VTEX maximum per request


def http(url, data=None, headers=None, timeout=30, want_headers=False):
    req = urllib.request.Request(url, data=data, headers={"User-Agent": UA, **(headers or {})})
    for attempt in range(3):  # the stores answer 503 now and then; a short retry rides it out
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                body = r.read().decode("utf-8", "replace")
                return (body, r.headers) if want_headers else body
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


# ---------- what a store shows right now ----------

def search(base, query=""):
    """All products for a catalog query (empty query = the whole store)."""
    def page(start):
        # the timestamp param dodges the CDN cache so we never read a stale page
        return f"{base}/api/catalog_system/pub/products/search?{query}&_from={start}&_to={start + PAGE - 1}&_t={int(time.time())}"

    body, headers = http(page(0), want_headers=True)
    products = json.loads(body)
    total = int((headers.get("resources") or "0/0").split("/")[-1])  # header looks like "0-49/956"
    with ThreadPoolExecutor(8) as pool:  # remaining pages in parallel, so a check takes seconds
        for chunk in pool.map(lambda start: json.loads(http(page(start))), range(PAGE, total, PAGE)):
            products += chunk
    return products


def calendar_categories(base):
    """Paths like '3/142' of store categories named after the calendar."""
    found = []

    def walk(nodes, path):
        for c in nodes:
            here = path + [str(c["id"])]
            if matches(c.get("name", "")):
                found.append("/".join(here))
            walk(c.get("children") or [], here)

    walk(json.loads(http(f"{base}/api/catalog_system/pub/category/tree/4?_t={int(time.time())}")), [])
    return found


def vtex_products(base):
    """Calendar products of a VTEX store as {id: {name, url, status, price}}."""
    everything = search(base)
    if len(everything) < 20:
        raise RuntimeError(f"catalog returned only {len(everything)} products - the store API may have changed")
    hits = {p["productId"]: p for p in everything if matches(p.get("productName", "") + " " + p.get("linkText", ""))}
    # Anything filed under a "Calendario de adviento" category counts, whatever it is called.
    for path in calendar_categories(base):
        for p in search(base, f"fq=C:/{path}/"):
            hits.setdefault(p["productId"], p)
    found = {}
    for p in hits.values():
        offers = [s.get("commertialOffer", {}) for i in p.get("items", []) for s in i.get("sellers", [])]
        qty = sum(o.get("AvailableQuantity") or 0 for o in offers)
        found[p["productId"]] = {
            "name": p["productName"],
            "url": f"{base}/{p['linkText']}/p",
            "status": "available" if qty > 0 else "listed",
            "price": next((o.get("Price") for o in offers if o.get("Price")), None),
        }
    return found


def woo_products(base):
    """Calendar products of a WooCommerce store, read from its public Store API."""
    api = f"{base}/wp-json/wc/store/v1/products"
    stamp = int(time.time())  # dodges page caches so we never read stale stock

    # Sanity check: if the feed stops answering with products, fail loudly instead of seeing "nothing".
    if not json.loads(http(f"{api}?per_page=1&_t={stamp}")):
        raise RuntimeError("product feed returned no products - the store API may have changed")

    hits = {}
    for word in KEYWORDS:  # the store's own search looks at names and descriptions
        q = urllib.parse.urlencode({"search": word, "per_page": 100, "_t": stamp})
        for p in json.loads(http(f"{api}?{q}")):
            if matches(p.get("name", "") + " " + p.get("slug", "")):
                hits[p["id"]] = p
    # Anything filed under a calendar category counts, whatever it is called.
    for c in json.loads(http(f"{api}/categories?per_page=100&_t={stamp}")):
        if matches(c.get("name", "") + " " + c.get("slug", "")):
            for p in json.loads(http(f"{api}?category={c['id']}&per_page=100&_t={stamp}")):
                hits.setdefault(p["id"], p)

    found = {}
    for p in hits.values():
        prices = p.get("prices") or {}
        try:
            price = int(prices["price"]) / 10 ** int(prices.get("currency_minor_unit") or 0)
        except (KeyError, TypeError, ValueError):
            price = None
        found[p["id"]] = {
            "name": html.unescape(p["name"]),
            "url": p.get("permalink") or f"{base}/?p={p['id']}",
            "status": "available" if p.get("is_in_stock") and p.get("is_purchasable") else "listed",
            "price": price,
        }
    return found


ADAPTERS = {"vtex": vtex_products, "woo": woo_products}


def snapshot(store):
    """{key: {name, url, status, price}} for everything that looks like the calendar."""
    base, prefix = store["url"], store["prefix"]
    found = {f"{prefix}product:{pid}": item for pid, item in ADAPTERS[store["kind"]](base).items()}
    # Second signal: the home page usually links a landing page before the product goes live.
    home = http(f"{base}/?_t={int(time.time())}")
    for slug in set(re.findall(r"[a-z0-9-]+", home)):
        if matches(slug) and "-" in slug and len(slug) < 80:
            found.setdefault(f"{prefix}page:{slug}", {"name": slug, "url": f"{base}/{slug}", "status": "page", "price": None})
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


def check_store(store, state):
    """Compare one store with what we remember, alert on changes, update `state` in place."""
    name, prefix = store["name"], store["prefix"]
    known = state.setdefault("_stores", [])
    first_time = name not in known
    new = snapshot(store)
    ok = True
    for key, item in new.items():
        before = state.get(key, {}).get("status")
        if item["status"] == before:
            continue
        price = f" - ${item['price']:,.0f}" if item.get("price") else ""
        if item["status"] == "available":
            ok &= notify(f"{name}: calendario de adviento DISPONIBLE", f"{item['name']}{price}. Corre a comprarlo!", item["url"])
        elif item["status"] == "listed" and before is None and not first_time:
            ok &= notify(f"{name}: el calendario ya aparece en el catalogo", f"{item['name']}{price} (aun sin stock)", item["url"])
        elif item["status"] == "page" and not first_time:
            ok &= notify(f"{name}: nueva pagina de calendario de adviento", item["name"], item["url"], urgent=False)
    if not ok:
        # leave the state untouched so the alert is retried on the next check
        raise RuntimeError("an alert could not be delivered")
    for key in [k for k in state if k.startswith(prefix + "product:") and k not in new]:
        state.pop(key)  # a product that vanished can alert again when it returns
    state.update(new)
    if first_time:
        known.append(name)
    live = sum(1 for v in new.values() if v["status"] == "available")
    return f"{name}: {len(new)} match(es), {live} buyable"


def check():
    old = json.load(open(STATE_FILE, encoding="utf-8")) if os.path.exists(STATE_FILE) else {}
    state = json.loads(json.dumps(old))
    summary, errors = [], []

    def run(store):  # one store failing must not blind the others
        try:
            return check_store(store, state)
        except Exception as e:
            errors.append(f"{store['name']}: {e}")

    with ThreadPoolExecutor(len(STORES)) as pool:  # stores are checked side by side
        summary = [line for line in pool.map(run, STORES) if line]
    if state != old:
        json.dump(state, open(STATE_FILE, "w", encoding="utf-8"), indent=1, ensure_ascii=False, sort_keys=True)
    print(time.strftime("%H:%M:%S"), "ok -" if not errors else "PARTIAL -", "; ".join(summary), flush=True)
    if errors:
        raise RuntimeError("; ".join(errors))


if __name__ == "__main__":
    if "--test" in sys.argv:
        sys.exit(0 if notify("Watcher: prueba", "Si lees esto, las alertas funcionan.", STORES[0]["url"]) else 1)
    deadline = time.time() + LOOP_SECONDS
    last_ok = False
    bad_streak = 0
    while True:
        started = time.time()
        try:
            check()
            last_ok = True
            bad_streak = 0
        except Exception as e:
            last_ok = False
            bad_streak += 1
            print("check failed:", e, flush=True)
            if bad_streak == 10:  # blind for a while: say so instead of failing silently
                notify("Watcher: no puedo leer una tienda", f"10 intentos fallidos seguidos: {e}", STORES[0]["url"], urgent=False)
        sys.stdout.flush()
        if time.time() + INTERVAL > deadline:
            break
        time.sleep(max(1, INTERVAL - (time.time() - started)))  # INTERVAL is start-to-start
    # Fail (so GitHub emails you) only if the watcher ended blind, not on a blip that recovered.
    sys.exit(0 if last_ok else 1)
