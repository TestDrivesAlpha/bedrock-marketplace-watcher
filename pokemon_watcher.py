"""Track original Pokemon base games and their version-specific DLC prices.

Public price listings are comparisons, not a claim about delivered cost. Digital
redemption is not confirmed until the Nintendo Account country is known.
"""

import copy
import hashlib
import json
import os
import re
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from html.parser import HTMLParser
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from urllib.request import Request, urlopen

ITEMS = {
    "pokemon-violet": {"title": "Pokémon Violet", "kind": "base game",
        "cover": "https://cdn.dekudeals.com/images/e14d18139c82fb9849155b66dc06c66d2b59c310/w540.jpg"},
    "pokemon-sword": {"title": "Pokémon Sword", "kind": "base game",
        "cover": "https://cdn.dekudeals.com/images/9813cd3648c7eec8397ba1e348941b2851830038/w540.jpg"},
    "pokmon-sword-expansion-pass": {"title": "Pokémon Sword Expansion Pass", "kind": "DLC",
        "requires": "Pokémon Sword base game required. Does not work with Pokémon Shield.",
        "cover": "https://cdn.dekudeals.com/images/1fe3ae619f46a7d3789f0c441d3f5a798f945a3d/w540.jpg"},
    "pokemon-violet-the-hidden-treasure-of-area-zero": {
        "title": "Pokémon Violet: The Hidden Treasure of Area Zero", "kind": "DLC",
        "requires": "Pokémon Violet base game required. Does not work with Pokémon Scarlet.",
        "cover": "https://cdn.dekudeals.com/images/e4e2a88caae32680ab8e876088f553213caba37e/w540.jpg"},
}
PLAYASIA = {
    "pokemon-violet": "https://www.play-asia.com/en/pokemon-violet-english/13/70f8m1",
    "pokemon-sword": "https://www.play-asia.com/en/pocket-monsters-sword-multi-language/13/70ckpt",
}
PSE_SOURCE = "https://www.psegameshop.com/?s=pokemon+violet&post_type=product"
MERCHANTS = {
    "amazon": ("Amazon US", "amazon.com"), "amazon_de": ("Amazon Germany", "amazon.de"),
    "target": ("Target", "target.com"), "walmart": ("Walmart", "walmart.com"),
    "bestbuy": ("Best Buy", "bestbuy.com"), "gamestop": ("GameStop", "gamestop.com"),
    "woot": ("Woot", "woot.com"), "eshop": ("Nintendo eShop", "nintendo.com"),
    "eshop_de": ("Nintendo eShop Germany", "nintendo.com"),
    "my_nintendo_store_us": ("Nintendo Store", "nintendo.com"),
    "humble_us": ("Humble Store", "humblebundle.com"),
}
USER_AGENT = "Mozilla/5.0 (compatible; Pokemon-Price-Watcher/1.0)"


def normalized(text):
    return re.sub(r"[^a-z0-9]", "", unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().lower())


def https_host(url, suffix):
    try:
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
        return (parts.scheme == "https" and parts.username is None and parts.password is None
                and parts.port in (None, 443) and (host == suffix or host.endswith("." + suffix)))
    except ValueError:
        return False


def direct_link(url, suffix):
    # Deku uses affiliate redirect links. Preserve the verified retailer target,
    # never an arbitrary host encoded in a redirect.
    for _ in range(3):
        if https_host(url, suffix):
            parts = urlsplit(url)
            query = [(k, v) for k, v in parse_qsl(parts.query)
                     if k not in ("tag", "tagid", "aref", "utm_source", "utm_medium", "utm_campaign")]
            return urlunsplit(parts._replace(query=urlencode(query), fragment=""))
        params = dict(parse_qsl(urlsplit(url).query))
        url = params.get("url") or params.get("u") or ""
    return None


class Page(HTMLParser):
    """Collect explicit metadata, price rows, and visible product text."""
    def __init__(self, html):
        super().__init__(convert_charrefs=True)
        self.meta, self.scripts, self.rows, self.text, self.anchors = {}, [], [], [], []
        self._script = None
        self._skip = 0
        self._row = None
        self._anchor = None
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "script":
            self._script = []
        elif tag in ("style", "noscript"):
            self._skip += 1
        elif tag == "meta":
            key = attrs.get("property") or attrs.get("name")
            if key:
                self.meta[key.lower()] = attrs.get("content", "")
        elif tag == "tr":
            self._row = {"attrs": attrs, "text": [], "anchors": []}
        elif tag == "a":
            self._anchor = {"attrs": attrs, "text": []}

    def handle_endtag(self, tag):
        if tag == "script" and self._script is not None:
            self.scripts.append("".join(self._script))
            self._script = None
        elif tag in ("style", "noscript"):
            self._skip = max(0, self._skip - 1)
        elif tag == "a" and self._anchor is not None:
            self._anchor["text"] = " ".join(self._anchor["text"])
            self.anchors.append(self._anchor)
            if self._row is not None:
                self._row["anchors"].append(self._anchor)
            self._anchor = None
        elif tag == "tr" and self._row is not None:
            self._row["text"] = " ".join(self._row["text"])
            self.rows.append(self._row)
            self._row = None

    def handle_data(self, data):
        if self._script is not None:
            self._script.append(data)
        elif not self._skip:
            text = data.strip()
            if text:
                self.text.append(text)
                if self._row is not None:
                    self._row["text"].append(text)
                if self._anchor is not None:
                    self._anchor["text"].append(text)


def money_cents(text):
    value = re.sub(r"[^0-9.,]", "", text)
    if "," in value and "." in value:
        value = value.replace(",", "") if value.rfind(".") > value.rfind(",") else value.replace(".", "").replace(",", ".")
    elif "," in value:
        value = value.replace(",", ".")
    try:
        return int(Decimal(value) * 100)
    except (InvalidOperation, ValueError):
        raise ValueError("Price could not be parsed.") from None


def parse_deku(html, item_id, country):
    page = Page(html)
    expected = ITEMS[item_id]
    title = page.meta.get("og:title", "").split(" | ")[0]
    if normalized(title) != normalized(expected["title"]):
        raise ValueError("Deku returned a different game or edition.")
    locale = next((a["text"].strip().split()[-1].upper() for a in page.anchors
                   if a["attrs"].get("id") == "navbarCountry1"), "")
    if locale != country:
        raise ValueError("Deku returned a different country.")
    currency = {"DE": "EUR", "US": "USD"}[country]
    source = f"https://www.dekudeals.com/items/{item_id}?country={country.lower()}"
    offers = []
    decoder = json.JSONDecoder()
    for script in page.scripts:
        for match in re.finditer(r"outAnalytics\[['\"]([^'\"]+)['\"]\]\s*=\s*", script):
            try:
                data, _ = decoder.raw_decode(script[match.end():])
                item = data["items"][0]
                merchant_key = item["affiliation"]
                if (item["item_id"] != item_id or normalized(item["item_name"]) != normalized(expected["title"])
                        or data["currency"] != currency or merchant_key not in MERCHANTS):
                    continue
                price = data["value"]
                form = item["item_variant"]
                if not isinstance(price, int) or isinstance(price, bool) or price <= 0 or form not in ("physical", "digital"):
                    continue
                if expected["kind"] == "DLC" and form != "digital":
                    continue
                merchant, host = MERCHANTS[merchant_key]
                rows = [row for row in page.rows if any(a["attrs"].get("data-out-analytics-id") == match[1] for a in row["anchors"])]
                main = next((row for row in rows if "item-note" not in row["attrs"].get("class", "")
                             and not re.match(r"^(Used:|From 3rd party)", row["text"])), None)
                if main is None:
                    continue
                url = next((direct_link(a["attrs"].get("href", ""), host) for a in main["anchors"]
                            if a["attrs"].get("href")), None)
                if not url:
                    continue
                status = "unavailable" if re.search(r"unavailable|sold out|out of stock", main["text"], re.I) else "listed"
                base = {"item_id": item_id, "title": expected["title"], "kind": expected["kind"],
                    "region": country, "currency": currency, "format": form, "condition": "new",
                    "price": price, "merchant": merchant, "url": url, "source": source,
                    "image": expected["cover"], "stock": status,
                    "stock_details": "Listed by Deku Deals; retailer stock and delivery unverified.",
                    "delivery": ("US retailer; delivery to Indonesia/Germany not verified." if country == "US"
                                 else "Germany listing; delivery to Indonesia not verified."),
                    "requires": expected.get("requires", "")}
                if merchant_key == "woot":
                    base["delivery"] = "Woot ships this only to the contiguous 48 United States. No direct delivery to Indonesia/Germany."
                offers.append(base)
                if form == "physical":
                    for row in rows:
                        used = re.search(r"\bUsed:\s*([$€][\d.,]+)", row["text"])
                        if used:
                            offers.append({**base, "condition": "used", "price": money_cents(used[1]),
                                "merchant": merchant + " used offers (seller unverified)",
                                "stock_details": "Used offer reported by tracker; seller, condition, and stock require verification."})
            except (KeyError, IndexError, TypeError, ValueError):
                continue
    if not offers:
        raise ValueError("No supported current prices were found; page format may have changed.")
    return offers


def parse_playasia(html, item_id):
    page = Page(html)
    text = " ".join(page.text)
    # Scope to this product, excluding header/cart recommendations and reviews.
    title_pattern = r"Pokemon\s+Violet\s*\(English\)" if item_id == "pokemon-violet" else r"Pocket\s+Monsters\s+Sword\s*\(Multi-Language\)"
    match = re.search(title_pattern, text, re.I)
    if not match:
        raise ValueError("Playasia did not return the expected base game.")
    product = text[match.end():]
    product = re.split(r"Product Languages|Product Features|Item Description|Customer reviews", product, maxsplit=1)[0]
    if not re.search(r"\bJapan\b", product) or not re.search(r"\bEnglish\b", text):
        raise ValueError("Playasia edition or English support was not confirmed.")
    price_match = re.search(r"US\$\s*([\d,]+\.\d{2})", product)
    if not price_match:
        raise ValueError("Playasia current USD price was not found.")
    stock, detail = "unknown", "Retailer stock status could not be confirmed."
    arrival = re.search(r"Expected stock arrival\s*(?:on\s*)?:?\s*([0-9/.-]+)", product, re.I)
    if arrival:
        stock, detail = "backorder", "Expected stock arrival: " + arrival[1] + " (retailer date)."
    elif re.search(r"In our warehouse|In stock|In Stock", product):
        stock, detail = "in_stock", "Retailer reports stock in its warehouse."
    elif re.search(r"out of stock|sold out|discontinued", product, re.I):
        stock, detail = "unavailable", "Retailer reports unavailable."
    return [{"item_id": item_id, "title": ITEMS[item_id]["title"], "kind": "base game",
        "region": "JP import", "currency": "USD", "format": "physical", "condition": "new",
        "price": money_cents(price_match[1]), "merchant": "Playasia", "url": PLAYASIA[item_id],
        "source": PLAYASIA[item_id], "image": ITEMS[item_id]["cover"], "stock": stock,
        "stock_details": detail, "requires": "",
        "delivery": "Japan packaging; English supported. Shipping quote, import fees, and delivery eligibility unverified."}]


class PseCards(HTMLParser):
    def __init__(self, html):
        super().__init__(convert_charrefs=True)
        self.cards, self.current, self.depth, self.card_depth = [], None, 0, 0
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if tag == "div":
            self.depth += 1
            if self.current is None and "type-product" in values.get("class", "").split():
                self.current = []
                self.card_depth = self.depth
        if self.current is not None:
            self.current.append(self.get_starttag_text())

    def handle_data(self, data):
        if self.current is not None:
            self.current.append(data)

    def handle_endtag(self, tag):
        if self.current is not None:
            self.current.append(f"</{tag}>")
        if tag == "div":
            if self.current is not None and self.depth == self.card_depth:
                self.cards.append("".join(self.current))
                self.current = None
            self.depth -= 1


def parse_pse(html):
    offers = []
    for card in PseCards(html).cards:
        page = Page(card)
        anchor = next((a for a in page.anchors if "woocommerce-loop-product__link" in a["attrs"].get("class", "").split()), None)
        if anchor is None:
            continue
        title = anchor["text"]
        # Exact base-game identities: cards, steelcases, DLC, accounts and
        # accessories returned by this broad retail search cannot qualify.
        if normalized(title) not in ("switchpokemonvioletceroenglish", "switchpokemonvioletmdeenglish"):
            continue
        url = direct_link(anchor["attrs"].get("href", ""), "psegameshop.com")
        if not url or urlsplit(url).path not in (
                "/product/switch-pokemon-violet-cero-english/", "/product/switch-pokemon-violet-mde-english/"):
            continue
        sale = re.search(r"<ins\b[^>]*>([\s\S]*?)</ins>", card, re.I)
        price_text_source = " ".join(Page(sale[1]).text if sale else page.text)
        price_match = re.search(r"\bRp\s*([\d\s.,]+)", price_text_source)
        if not price_match:
            continue
        rupiah = int(re.sub(r"\D", "", price_match[1]))
        if rupiah <= 0:
            continue
        in_stock = bool(re.search(r"\binstock\b", card))
        offers.append({"item_id": "pokemon-violet", "title": ITEMS["pokemon-violet"]["title"],
            "kind": "base game", "region": "ID", "currency": "IDR", "format": "physical", "condition": "new",
            "price": rupiah * 100, "merchant": "PSe Gameshop (" + ("Cero/English" if "Cero" in title else "Mde/English") + ")",
            "url": url, "source": PSE_SOURCE, "image": ITEMS["pokemon-violet"]["cover"],
            "stock": "listed_in_stock" if in_stock else "unknown",
            "stock_details": ("Retailer's search listing says in stock; checkout stock unverified." if in_stock
                              else "Retailer stock could not be confirmed from its public search listing."),
            "delivery": "Indonesia retailer listing. Destination availability and delivery fee require a shipping quote.",
            "requires": ""})
    if not offers:
        raise ValueError("No exact Pokemon Violet base-game prices found in PSe search.")
    return offers


def fetch_page(url):
    with urlopen(Request(url, headers={"User-Agent": USER_AGENT}), timeout=25) as response:
        if response.status != 200:
            raise RuntimeError(f"Source returned HTTP {response.status}.")
        return response.read(4_000_000).decode("utf-8", errors="replace")


def gather_offers(countries=("DE", "US"), fetch=fetch_page):
    requests = [("deku", item, country, f"https://www.dekudeals.com/items/{item}?country={country.lower()}")
                for item in ITEMS for country in countries]
    requests += [("playasia", item, "JP import", url) for item, url in PLAYASIA.items()]
    requests.append(("pse", "pokemon-violet", "ID", PSE_SOURCE))
    def read(spec):
        source, item, country, url = spec
        try:
            html = fetch(url)
            parsed = parse_deku(html, item, country) if source == "deku" else (
                parse_playasia(html, item) if source == "playasia" else parse_pse(html))
            return parsed, None
        except (HTTPError, URLError, TimeoutError, OSError, RuntimeError, ValueError) as error:
            # Report the public source, not potentially sensitive request details.
            status = f"HTTP {error.code}" if isinstance(error, HTTPError) else type(error).__name__
            if isinstance(error, HTTPError):
                error.close()
            return [], f"{source}/{item}/{country}: {status}"
    offers, errors = [], []
    with ThreadPoolExecutor(max_workers=4) as pool:
        for found, error in pool.map(read, requests):
            offers.extend(found)
            if error:
                errors.append(error)
    if not offers:
        raise RuntimeError("All Pokemon price sources were unavailable or unparseable.")
    # Compare like with like: each country, currency, format, and condition is a
    # separate group. No USD/EUR or new/used comparisons imply delivered value.
    best = {}
    for offer in offers:
        key = offer_key(offer)
        if key not in best or (offer["stock"] == "unavailable", offer["price"], offer["merchant"]) < (
                best[key]["stock"] == "unavailable", best[key]["price"], best[key]["merchant"]):
            best[key] = offer
    return sorted(best.values(), key=offer_key), errors


def offer_key(offer):
    return "|".join(offer[key] for key in ("item_id", "region", "currency", "format", "condition"))


def save_state(path, state):
    path = Path(path)
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(json.dumps(state, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    temp.replace(path)


def prepare_events(state, offers, observed_at):
    state = copy.deepcopy(state)
    state.setdefault("version", 1)
    state.setdefault("offers", {})
    state.setdefault("pending", [])
    for offer in offers:
        key = offer_key(offer)
        previous = state["offers"].get(key)
        reasons = []
        if previous is None:
            reasons.append("First observed price — discount not established")
        else:
            if offer["price"] < previous["price"]:
                reasons.append("New lowest observed by this watcher" if offer["price"] < previous.get("lowest", previous["price"])
                               else "Price dropped since the previous check")
            if offer["stock"] != previous.get("stock"):
                reasons.append("Availability changed")
        pending = next((event for event in state["pending"] if event["key"] == key), None)
        # Refresh an unsent event to the latest observed price/availability.
        # An old queued price must not be sent after its listing has changed.
        if pending:
            pending["offer"] = offer
            pending["observed_at"] = observed_at
            if pending.get("previous_price") is not None:
                refreshed = []
                if offer["price"] < pending["previous_price"]:
                    refreshed.append("Price dropped since the last pre-alert observation")
                if offer["stock"] != pending.get("previous_stock"):
                    refreshed.append("Availability changed")
                if refreshed:
                    pending["reason"] = "; ".join(refreshed)
                else:
                    state["pending"].remove(pending)
        elif reasons:
            event_id = hashlib.sha256((key + observed_at + str(offer["price"]) + offer["stock"]).encode()).hexdigest()[:20]
            state["pending"].append({"id": event_id, "key": key, "offer": offer,
                "reason": "; ".join(reasons), "previous_price": previous["price"] if previous else None,
                "previous_stock": previous.get("stock") if previous else None,
                "observed_at": observed_at})
        state["offers"][key] = {"price": offer["price"], "stock": offer["stock"],
            "lowest": min(offer["price"], previous.get("lowest", previous["price"])) if previous else offer["price"],
            "observed_at": observed_at}
    return state


def price_text(cents, currency):
    if currency == "IDR":
        return f"IDR {cents // 100:,}"
    return f"{currency} {cents / 100:.2f}"


def event_embed(event, account_country="", coverage=""):
    offer = event["offer"]
    digital = offer["format"] == "digital"
    if digital:
        if not account_country:
            eligibility = "Nintendo Account country unknown. Regional price reference only; redemption is NOT confirmed."
        elif account_country != offer["region"]:
            eligibility = f"Your configured account is {account_country}; this {offer['region']} digital listing is a regional reference. Redemption is NOT confirmed."
        else:
            eligibility = f"Listed for {offer['region']}; confirm account/code region and payment eligibility before purchase."
    else:
        eligibility = "Physical cartridge. Delivery eligibility and total cost require confirmation."
    previous = event.get("previous_price")
    price = price_text(offer["price"], offer["currency"])
    if previous is not None and previous != offer["price"]:
        price += " (previously observed " + price_text(previous, offer["currency"]) + ")"
    description = (
        f"**{event['reason']}**\n**Item price: {price}**\n"
        f"{offer['merchant']} · {offer['region']} · {offer['format']} · {offer['condition']}\n\n"
        f"**Stock:** {offer['stock_details']}\n**Region:** {eligibility}\n"
        f"**Delivery / fees:** {offer['delivery']} Final shipping, tax/import adjustments and payment fees are unverified.\n"
    )
    if offer.get("requires"):
        description += "\n**DLC requirement:** " + offer["requires"] + "\n"
    description += f"\n[Product listing]({offer['url']}) · [Price source]({offer['source']})\nObserved: {event['observed_at']}"
    return {"title": offer["title"] + (" — DLC" if offer["kind"] == "DLC" else ""),
        "url": offer["url"], "description": description, "image": {"url": offer["image"]},
        "color": 0x7B45BB, "footer": {"text": (
            "Lowest listing found within this source/group. Not a delivered-total or all-time-low claim. " + coverage).strip()}}


def make_batches(events, account_country="", coverage=""):
    batches, ids, embeds, size = [], [], [], 0
    for event in events:
        embed = event_embed(event, account_country, coverage)
        # Count every string, including URLs, conservatively below Discord's
        # 6000-character embed-text limit; also respect its 10-embed cap.
        length = len(json.dumps(embed, ensure_ascii=False))
        if embeds and (len(embeds) == 10 or size + length > 5500):
            batches.append((ids, {"username": "Pokémon Price Watcher", "content": "Pokémon price and availability update",
                                 "allowed_mentions": {"parse": []}, "embeds": embeds}))
            ids, embeds, size = [], [], 0
        ids.append(event["id"])
        embeds.append(embed)
        size += length
    if embeds:
        batches.append((ids, {"username": "Pokémon Price Watcher", "content": "Pokémon price and availability update",
                             "allowed_mentions": {"parse": []}, "embeds": embeds}))
    return batches


def send_discord(webhook, payload):
    parts = urlsplit(webhook)
    query = [(k, v) for k, v in parse_qsl(parts.query) if k != "wait"] + [("wait", "true")]
    request = Request(urlunsplit(parts._replace(query=urlencode(query))),
        data=json.dumps(payload).encode("utf-8"), method="POST",
        headers={"Content-Type": "application/json", "User-Agent": USER_AGENT})
    try:
        with urlopen(request, timeout=30) as response:
            ack = json.loads(response.read(1_000_000))
            if response.status != 200 or not all(str(ack.get(k, "")).isdigit() for k in ("id", "channel_id")):
                raise RuntimeError("Discord did not return a message acknowledgement.")
            return {"message_id": str(ack["id"]), "channel_id": str(ack["channel_id"])}
    except HTTPError as error:
        raise RuntimeError(f"Discord webhook failed (HTTP {error.code}).") from None
    except (URLError, TimeoutError, ValueError):
        raise RuntimeError("Discord webhook failed or returned an invalid acknowledgement.") from None


def run(offers, state_path, *, dry_run=False, webhook=None, account_country="", coverage="", post=send_discord, now=None):
    state_path = Path(state_path)
    state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}
    observed_at = (now or datetime.now(timezone.utc)).isoformat(timespec="seconds")
    state = prepare_events(state, offers, observed_at)
    fresh_keys = {offer_key(offer) for offer in offers}
    # A pending source that is unreachable today cannot confirm today's price.
    # Retain it for a fresh observation instead of sending a stale deal.
    batches = make_batches([event for event in state["pending"] if event["key"] in fresh_keys], account_country, coverage)
    if dry_run:
        for _, payload in batches:
            print(json.dumps(payload, indent=2, ensure_ascii=False))
        print(f"Dry run: {sum(len(ids) for ids, _ in batches)} price cards; no messages sent or history written.")
        return []
    if batches and not webhook:
        raise RuntimeError("DISCORD_STEAM_WEBHOOK_URL secret is missing.")
    # Save pending before the first network request. Successful batches are
    # removed individually so failures cannot lose the unsent remainder.
    save_state(state_path, state)
    acknowledgements = []
    for event_ids, payload in batches:
        ack = post(webhook, payload)
        if not isinstance(ack, dict) or not all(str(ack.get(k, "")).isdigit() for k in ("message_id", "channel_id")):
            raise RuntimeError("Discord message acknowledgement was invalid.")
        sent = set(event_ids)
        state["pending"] = [event for event in state["pending"] if event["id"] not in sent]
        save_state(state_path, state)
        acknowledgements.append(ack)
        print(json.dumps(ack))
    if not batches:
        print("No new Pokemon price drops or availability changes.")
    return acknowledgements


def source_health(errors):
    required = [error for error in errors if error.startswith("deku/")]
    optional = [error for error in errors if not error.startswith("deku/")]
    return required, optional


def coverage_text(countries, errors):
    required, optional = source_health(errors)
    covered = "/".join(countries) + " tracker listings"
    if required:
        covered += " (incomplete)"
    if optional:
        return "Coverage: " + covered + "; some direct Indonesia/import sources unavailable and excluded."
    return "Coverage: " + covered + " and available direct retailers; destination checkout unverified."


def main():
    countries = tuple(dict.fromkeys(c.strip().upper() for c in os.environ.get("POKEMON_COUNTRIES", "DE,US").split(",") if c.strip()))
    if not countries or any(country not in ("DE", "US") for country in countries):
        raise ValueError("POKEMON_COUNTRIES supports DE and US only.")
    account_country = os.environ.get("NINTENDO_ACCOUNT_COUNTRY", "").strip().upper()
    offers, errors = gather_offers(countries)
    required, optional = source_health(errors)
    coverage = coverage_text(countries, errors)
    for error in errors:
        print(("Required source unavailable: " if error in required else "Optional source unavailable (excluded): ") + error)
    print(f"Current listings: {len(offers)}; unavailable sources: {len(errors)}.")
    print(coverage)
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        with Path(summary_path).open("a", encoding="utf-8") as summary:
            summary.write("\n### Pokémon price coverage\n\n" + coverage + "\n\n")
            summary.write(f"Current listing groups: {len(offers)}. Nintendo Account country: "
                          + (account_country or "unknown; digital prices are regional references only") + ".\n")
            for label, unavailable in (("Required", required), ("Optional; excluded", optional)):
                for error in unavailable:
                    summary.write(f"- {label}: {error}\n")
    run(offers, Path(os.environ.get("POKEMON_STATE_FILE", ".pokemon-price-state.json")),
        dry_run=os.environ.get("DRY_RUN", "false").lower().strip() in ("true", "1", "yes"),
        webhook=os.environ.get("DISCORD_WEBHOOK_URL"), account_country=account_country, coverage=coverage)
    if required:
        # Valid messages/state above are retained. The workflow's always-run
        # state-save step can commit checkpoints while exposing required gaps.
        raise RuntimeError(f"{len(required)} required Deku source(s) failed; valid observations were processed.")


if __name__ == "__main__":
    main()
