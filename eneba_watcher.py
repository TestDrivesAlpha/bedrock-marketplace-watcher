"""Public Eneba campaign and Minecoin-code price watcher (Python standard library)."""
import argparse
import copy
import hashlib
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from decimal import Decimal
from html.parser import HTMLParser
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit
from urllib.request import Request, urlopen

from discord_retention import record_message, write_snapshot

UA = "EnebaDealsWatcher/1.0 (+https://github.com/TestDrivesAlpha/bedrock-marketplace-watcher)"
BASE = "https://www.eneba.com"
STATE_FILE = ".eneba-price-state.json"
AVATAR_URL = "https://raw.githubusercontent.com/TestDrivesAlpha/bedrock-marketplace-watcher/main/assets/eneba-avatar.png"
FEST_TERMS = {
    "captured": "2026-10-17", "code": "FEST", "percent": 13,
    "end": "2026-10-18T23:59:00+02:00",
    "text": "Valid until 2026-10-18 23:59 EET. Once per user, for one game under EUR 80. Cannot combine with other promotions, discount codes or cashback. Cannot pay with Eneba Wallet. Sale duration may change without notice.",
}


class SourceError(RuntimeError):
    pass


def utc_now():
    return datetime.now(timezone.utc)


def stamp(value):
    return value.astimezone(timezone.utc).isoformat()


def safe_url(value, host=None):
    if not isinstance(value, str):
        return None
    parsed = urlsplit(value)
    if parsed.scheme != "https" or not parsed.netloc or (host and parsed.hostname != host):
        return None
    return value


def canonical(url):
    parsed = urlsplit(urljoin(BASE, url))
    return urlunsplit(("https", "www.eneba.com", parsed.path.rstrip("/"), "", ""))


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()[:24]


def fetch_html(url):
    request = Request(url, headers={"User-Agent": UA, "Accept": "text/html", "Accept-Language": "en-US,en;q=0.9"})
    try:
        with urlopen(request, timeout=30) as response:
            body = response.read(5_000_001)
            if len(body) > 5_000_000:
                raise SourceError("public page exceeded the expected size")
            return body.decode(response.headers.get_content_charset() or "utf-8")
    except HTTPError as error:
        # Do not evade protection or treat denied access as an empty shop.
        raise SourceError(f"public page HTTP {error.code}; no offers inferred") from None
    except (URLError, TimeoutError) as error:
        raise SourceError(f"public page unavailable ({type(error).__name__}); no offers inferred") from None


class ApolloParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=False)
        self.collecting = False
        self.parts = []

    def handle_starttag(self, tag, attrs):
        if tag == "script" and dict(attrs).get("id") == "__APOLLO_STATE__":
            self.collecting = True

    def handle_endtag(self, tag):
        if tag == "script":
            self.collecting = False

    def handle_data(self, data):
        if self.collecting:
            self.parts.append(data)


def apollo_state(html, country="ID"):
    parser = ApolloParser()
    parser.feed(html)
    try:
        state = json.loads("".join(parser.parts))
    except (ValueError, TypeError):
        raise SourceError("public page has no readable Apollo data (blocked page or changed layout)") from None
    root = state.get("ROOT_QUERY") if isinstance(state, dict) else None
    if not isinstance(root, dict):
        raise SourceError("public Apollo data has no ROOT_QUERY")
    return state


def source_country(state, operation):
    """Read the actual country for this operation; locale does not override server geolocation."""
    countries = set()
    for key in state["ROOT_QUERY"]:
        if not key.startswith(operation + "("):
            continue
        try:
            args = json.loads(key[key.index("(") + 1:-1])
            context = args.get("context") or {}
            country = context.get("country")
        except (ValueError, TypeError, AttributeError):
            raise SourceError(f"{operation} storefront context is unreadable") from None
        # Product pages can also cache a language-only query, alongside the country-specific query.
        if country is None:
            continue
        if not isinstance(country, str) or not re.fullmatch(r"[A-Z]{2}", country):
            raise SourceError(f"{operation} storefront country is invalid")
        countries.add(country)
    if len(countries) != 1:
        raise SourceError(f"{operation} storefront country is missing or ambiguous")
    return countries.pop()


def field(obj, name):
    if not isinstance(obj, dict):
        return None
    return next((value for key, value in obj.items() if key == name or key.startswith(name + "(")), None)


def resolve(state, value):
    return state.get(value["__ref"], {}) if isinstance(value, dict) and "__ref" in value else (value or {})


def cover(product):
    image = field(product, "cover") or {}
    return safe_url(image.get("src2x")) or safe_url(image.get("src"))


def campaigns_from_html(html, country="ID", now=None):
    state = apollo_state(html, country)
    storefront = source_country(state, "Slider_sliders")
    slider = field(state["ROOT_QUERY"], "Slider_sliders")
    if not isinstance(slider, dict) or not isinstance(slider.get("edges"), list):
        raise SourceError("homepage campaign slider is missing; layout changed")
    campaigns = {}
    for edge in slider["edges"]:
        node = resolve(state, edge.get("node"))
        path = node.get("urlPath", "")
        if not path.startswith("/sales/") or "preorder" in path or not re.search(r"sale|fest|black-friday|cyber-monday|flash", path, re.I):
            continue
        url = canonical(f"/en-id{path}")
        title = node.get("text")
        if not title:
            raise SourceError("featured campaign has no title")
        item = {"title": title, "url": url, "image": safe_url(field(node, "resizedImageUrl")), "terms": None, "source_country": storefront}
        if path == "/sales/gametoberfest-sale":
            if (now or utc_now()) >= datetime.fromisoformat(FEST_TERMS["end"]):
                continue
            item["terms"] = copy.deepcopy(FEST_TERMS)
        item["key"] = digest(url + "|" + title)
        campaigns[item["key"]] = item
    return list(campaigns.values())


def minecoin_product(product):
    return bool(isinstance(product, dict) and isinstance(product.get("type"), dict) and product["type"].get("value") == "GAME_POINTS" and
                re.search(r"\bMinecoins\s+Pack\s*:\s*\d+\s+Coins\b", product.get("name", ""), re.I) and
                isinstance(product.get("drm"), dict) and product["drm"].get("slug") in {"xbox", "windows-store"})


def offers_from_html(html, country="ID", currency="EUR"):
    state = apollo_state(html, country)
    storefront = source_country(state, "search")
    search = field(state["ROOT_QUERY"], "search")
    results = search.get("results") if isinstance(search, dict) else None
    if not isinstance(results, dict) or not isinstance(results.get("edges"), list) or not isinstance(results.get("totalCount"), int):
        raise SourceError("Minecoin search results are missing; layout changed")
    offers = []
    recognized = 0
    for edge in results["edges"]:
        product = resolve(state, edge.get("node"))
        if not minecoin_product(product):
            continue
        recognized += 1
        regions = [region.get("code") for region in product.get("regions", [])]
        if not any(region in {"global", "europe"} for region in regions):
            continue
        auction = resolve(state, product.get("cheapestAuction"))
        if auction.get("isInStock") is not True or auction.get("isAddableToCart") is not True:
            continue
        money = field(auction, "price") or {}
        amount = money.get("amount")
        if money.get("currency") != currency or isinstance(amount, bool) or not isinstance(amount, int) or amount <= 0:
            raise SourceError("Minecoin listing price/currency is not a valid EUR minor-unit amount")
        match = re.search(r"Minecoins\s+Pack\s*:\s*(\d+)\s+Coins", product["name"], re.I)
        slug = product.get("slug")
        if not slug or not cover(product):
            raise SourceError("Minecoin listing is missing its product ID or cover")
        cashback = field(auction, "cashback") or {}
        cashback_money = cashback.get("price") or {}
        offers.append({
            "slug": slug, "title": product["name"], "coins": int(match.group(1)), "region": ", ".join(regions),
            "url": f"{BASE}/en-id/{slug}?currency={currency}", "image": cover(product),
            "price_minor": amount, "currency": currency,
            "seller": (auction.get("merchant") or {}).get("displayname", "Not provided"),
            "cashback_minor": cashback_money.get("amount") if cashback_money.get("currency") == currency else None,
            "cashback_percent": (product.get("cashback") or {}).get("valuePercent"),
            "countries": None, "blocked_countries": [], "notes": [],
            "source_country": storefront,
        })
    if results["totalCount"] > 0 and not results["edges"]:
        raise SourceError("nonempty Minecoin search reports no readable product entries")
    if results["totalCount"] > 0 and not recognized:
        raise SourceError("search has no recognizable Minecoin prepaid products; layout or category changed")
    return offers, results["totalCount"], len(results["edges"])


def enrich_offer(offer, html, country="ID"):
    state = apollo_state(html, country)
    activation_source = source_country(state, "product")
    product = state.get("Product::" + offer["slug"])
    if not isinstance(product, dict) or not minecoin_product(product):
        raise SourceError("product details do not match the requested Minecoin prepaid code")
    countries = product.get("countriesAlpha2")
    if not isinstance(countries, list) or not countries or not all(isinstance(code, str) and len(code) == 2 for code in countries):
        raise SourceError("product activation country list is missing or ambiguous")
    result = copy.deepcopy(offer)
    result["countries"] = countries
    result["blocked_countries"] = product.get("blockedCountries") or []
    result["notes"] = product.get("notes") or []
    result["activation_source_country"] = activation_source
    return result


def eligible(offer, country):
    blocked = [item.get("code") if isinstance(item, dict) else item for item in offer.get("blocked_countries", [])]
    return bool(offer.get("countries") and country in offer["countries"] and country not in blocked)


def collect(country="ID", alternative="DE", currency="EUR", fetcher=fetch_html, now=None):
    if country != "ID" or alternative != "DE" or currency != "EUR":
        raise SourceError("this watcher currently supports Indonesia (ID), Germany alternatives (DE), and EUR only")
    result = {"campaigns": [], "offers": [], "errors": [], "retention_coverage": {}}
    try:
        html = fetcher(BASE + "/en-id/?currency=EUR")
        result["campaigns"] = campaigns_from_html(html, country, now)
        storefront = source_country(apollo_state(html), "Slider_sliders")
        result["retention_coverage"]["eneba-campaigns:" + storefront] = True
    except SourceError as error:
        result["errors"].append({"source": "Eneba homepage campaigns", "error": str(error)})
    candidates = {}
    storefronts = set()
    search_complete = False
    try:
        for page in range(1, 6):
            url = BASE + "/en-id/store/minecraft-minecoins?currency=EUR" + (f"&page={page}" if page > 1 else "")
            html = fetcher(url)
            offers, total, count = offers_from_html(html, country, currency)
            storefronts.add(source_country(apollo_state(html), "search"))
            candidates.update((offer["slug"], offer) for offer in offers)
            if page * 20 >= total:
                search_complete = True
                break
            if count == 0 or page == 5:
                raise SourceError("Minecoin search pagination could not be completed")
    except SourceError as error:
        result["errors"].append({"source": "Eneba Minecoin search", "error": str(error)})
    # Check each genuine Global/Europe SKU: similarly named Global codes have different country lists.
    for offer in candidates.values():
        try:
            result["offers"].append(enrich_offer(offer, fetcher(offer["url"]), country))
        except SourceError as error:
            result["errors"].append({"source": offer["slug"], "error": str(error)})
    # A missing SKU is conclusively inactive only after the entire comparable
    # storefront search and every activation check succeeded. Otherwise retry.
    complete = search_complete and len(storefronts) == 1 and not any(
        error["source"] != "Eneba homepage campaigns" for error in result["errors"])
    for storefront in storefronts:
        result["retention_coverage"][f"eneba-minecoins:{storefront}:{currency}"] = complete
    return result


def money(minor):
    return f"EUR {Decimal(minor) / 100:.2f}"


def campaign_embed(campaign, overview=False):
    terms = campaign.get("terms")
    description = f"Featured broad sale on Eneba's public homepage, with **{campaign['source_country']} storefront context**. This is a regional reference; campaign eligibility for Indonesia is unverified. Product eligibility and the checkout price depend on the offer."
    if overview:
        description = "**Current overview — requested refresh**\n" + description
    fields = [{"name": "Source", "value": f"[Open campaign and current terms]({campaign['url']})"}]
    if terms:
        fields.extend([
            {"name": "Coupon snapshot", "value": f"**{terms['percent']}% code: {terms['code']}** — terms captured {terms['captured']} from the Indonesia page. Applicability to this storefront is unverified."},
            {"name": "Coupon conditions", "value": terms["text"]},
            {"name": "Minecoin applicability", "value": "**Unverified.** The stated coupon is for one game; do not assume it applies to Minecoin codes."},
        ])
    else:
        fields.append({"name": "Coupon and dates", "value": "Not verified from public homepage data. Open the campaign for current terms."})
    embed = {"title": campaign["title"][:256], "url": campaign["url"], "description": description, "fields": fields, "color": 0x7139CB}
    if campaign.get("image"):
        embed["image"] = {"url": campaign["image"]}
    return embed


def offer_embed(offer, previous=None, country="ID", alternative="DE", overview=False):
    primary = eligible(offer, country)
    heading = "Indonesia activation listed" if primary else "Germany alternative only — Indonesia not supported"
    if overview:
        event = f"**Current overview — requested refresh**\nCurrent available listing: **{money(offer['price_minor'])}**. This refresh does not claim a new sale or a price drop."
    elif previous and offer["price_minor"] < previous["price_minor"]:
        drop = (Decimal(previous["price_minor"] - offer["price_minor"]) / previous["price_minor"] * 100)
        event = f"Observed listing-price drop: **{drop:.1f}%**, {money(previous['price_minor'])} → **{money(offer['price_minor'])}**."
    else:
        event = f"First observed available listing: **{money(offer['price_minor'])}**. This establishes a baseline; no price discount is claimed."
    event += f"\nListing reference: **{offer['source_country']} storefront context**, displayed in EUR. This is not a verified Indonesian checkout quote."
    fields = [
        {"name": "Activation", "value": f"**{heading}**\nEneba allowed-country list: ID {'yes' if primary else 'no'}; DE {'yes' if eligible(offer, alternative) else 'no'}. 'Global' alone does not guarantee activation."},
        {"name": "Displayed price before checkout fees", "value": f"**{money(offer['price_minor'])}** for {offer['coins']:,} Minecoins\n{money(Decimal(offer['price_minor']) * 1000 / offer['coins'])} per 1,000 Minecoins. Seller: {offer['seller'][:100]}."},
        {"name": "Final payable cost", "value": "**Unverified.** Payment/service fees and taxes may change the checkout total. This is a public 'From' listing price in EUR; no currency conversion or fee estimate was applied."},
        {"name": "Redemption", "value": "Digital Minecoin prepaid code, redeemed to your own Microsoft/Xbox account. Your account and physical location must satisfy the product's published restrictions. No region-change or VPN workaround is assumed."},
    ]
    cashback = []
    if isinstance(offer.get("cashback_minor"), int):
        cashback.append(money(offer["cashback_minor"]))
    if isinstance(offer.get("cashback_percent"), (int, float)):
        cashback.append(f"{offer['cashback_percent']}%")
    if cashback:
        fields.append({"name": "Cashback — separate benefit", "value": f"Source reports {' / '.join(cashback)} cashback. It is **not** an upfront price reduction and has not been deducted from the displayed price. Check eligibility and wallet terms."})
    embed = {"title": offer["title"][:256], "url": offer["url"], "description": event, "fields": fields, "color": 0x3B9956 if primary else 0xE1A634}
    embed["image"] = {"url": offer["image"]}
    return embed


def empty_state():
    return {"version": 1, "prices": {}, "notified": {}, "campaigns": {}, "failures": {}}


def read_state(file):
    try:
        state = json.loads(Path(file).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return empty_state()
    if not isinstance(state, dict) or not all(isinstance(state.get(key), dict) for key in ("prices", "notified", "campaigns", "failures")):
        raise SourceError("Eneba history is invalid; refusing to reset notification history")
    return state


def save_state(state, file):
    target = Path(file)
    temporary = target.with_name(target.name + ".tmp")
    temporary.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
    temporary.replace(target)


def price_key(offer):
    return "|".join((offer["slug"], offer["currency"], offer["source_country"]))


def selected_offers(offers, country="ID", alternative="DE"):
    # Keep regions separate and compare available listings with equal Minecoin quantities.
    groups = {}
    for offer in offers:
        scope = "ID" if eligible(offer, country) else "DE" if eligible(offer, alternative) else None
        if scope:
            key = (scope, offer["coins"])
            if key not in groups or offer["price_minor"] < groups[key]["price_minor"]:
                groups[key] = offer
    return [offer for _, offer in sorted(groups.items(), key=lambda item: (0 if item[0][0] == "ID" else 1, item[0][1]))]


def plan_events(snapshot, state, min_drop_percent=1, country="ID", alternative="DE"):
    events = []
    for campaign in snapshot["campaigns"]:
        if campaign["key"] not in state["campaigns"]:
            events.append({"key": "campaign:" + campaign["key"], "campaign": campaign, "embed": campaign_embed(campaign)})
    for offer in selected_offers(snapshot["offers"], country, alternative):
        previous = state["prices"].get(price_key(offer))
        baseline = "baseline:" + offer["slug"] + ":" + offer["source_country"]
        if baseline not in state["notified"]:
            event_key = baseline
        elif previous and previous.get("currency") == offer["currency"] and previous.get("source_country") == offer["source_country"] and offer["price_minor"] < previous["price_minor"] and (previous["price_minor"] - offer["price_minor"]) * 100 / previous["price_minor"] >= min_drop_percent:
            event_key = f"drop:{offer['slug']}:{offer['source_country']}:{previous['price_minor']}:{offer['price_minor']}:{previous.get('at', '')}"
        else:
            continue
        if event_key not in state["notified"]:
            events.append({"key": event_key, "offer": offer, "embed": offer_embed(offer, previous if event_key.startswith("drop:") else None, country, alternative)})
    for error in snapshot["errors"]:
        if state["failures"].get(error["source"]) != error["error"]:
            events.append({"key": "failure:" + digest(error["source"] + error["error"]), "failure": error,
                           "embed": {"title": "Eneba source check failed", "description": f"{error['source']}: {error['error']}", "color": 0xBF4545,
                                     "fields": [{"name": "Meaning", "value": "This is a monitoring failure, not evidence that sales or Minecoin offers disappeared. Previous successful prices are preserved."}]}})
    return events


def embed_size(embed):
    return len(embed.get("title", "")) + len(embed.get("description", "")) + sum(len(field["name"]) + len(field["value"]) for field in embed.get("fields", []))


def batches(events):
    batch, size = [], 0
    for event in events:
        length = embed_size(event["embed"])
        if length > 6000:
            raise SourceError("Eneba notification exceeds Discord's embed limit")
        if batch and (len(batch) == 10 or size + length > 6000):
            yield batch
            batch, size = [], 0
        batch.append(event)
        size += length
    if batch:
        yield batch


def payload(batch, overview=False):
    content = "Current overview — requested refresh. Current campaign and Minecoin listings; activation regions and checkout fees remain separate." if overview else "Eneba campaign / Minecoin code update. Activation region and checkout fees are shown separately."
    return {"username": "Eneba Sale Watcher", "avatar_url": AVATAR_URL, "content": content,
            "embeds": [event["embed"] for event in batch], "allowed_mentions": {"parse": []}}


def post_discord(webhook, message, opener=urlopen, sleeper=time.sleep):
    parsed = urlsplit(webhook)
    query = dict(parse_qsl(parsed.query))
    query["wait"] = "true"
    url = urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(query), parsed.fragment))
    for attempt in range(5):
        request = Request(url, data=json.dumps(message).encode(), headers={"Content-Type": "application/json", "User-Agent": UA}, method="POST")
        try:
            with opener(request, timeout=30) as response:
                acknowledgment = json.load(response)
            if not isinstance(acknowledgment, dict) or not all(
                    str(acknowledgment.get(key, "")).isdigit() for key in ("id", "channel_id")):
                raise SourceError("Discord did not return a message acknowledgment; batch remains pending")
            return {"message_id": str(acknowledgment["id"]), "channel_id": str(acknowledgment["channel_id"])}
        except HTTPError as error:
            if error.code == 429 and attempt < 4:
                try:
                    delay = float(json.load(error).get("retry_after", 1))
                except (ValueError, TypeError):
                    delay = 1
                finally:
                    error.close()
                if delay > 60:
                    raise SourceError("Discord rate limit exceeds 60 seconds; batch remains pending") from None
                sleeper(max(0.25, delay))
                continue
            error.close()
            if error.code >= 500 and attempt < 4:
                sleeper(attempt + 1)
                continue
            raise SourceError(f"Discord HTTP {error.code}; batch remains pending") from None
        except (URLError, TimeoutError, ValueError):
            raise SourceError("Discord delivery was not acknowledged; batch remains pending") from None


def retention_item(event):
    if "offer" in event:
        offer = event["offer"]
        return {"key": "offer:" + price_key(offer),
                "scope": f"eneba-minecoins:{offer['source_country']}:{offer['currency']}"}
    if "campaign" in event:
        campaign = event["campaign"]
        return {"key": "campaign:" + campaign["key"],
                "scope": "eneba-campaigns:" + campaign["source_country"]}
    return {"key": "health:" + event.get("failure", {}).get("source", "empty-overview"), "scope": "eneba-health"}


def export_retention_snapshot(snapshot, now, path):
    if not path:
        return
    coverage = dict(snapshot.get("retention_coverage", {}))
    coverage["eneba-health"] = True
    # Offline/legacy fixtures have no explicit successful collection metadata;
    # they can still refresh present items, but cannot prove missing ones ended.
    events = [{"campaign": campaign, "embed": campaign_embed(campaign)} for campaign in snapshot["campaigns"]]
    events += [{"offer": offer, "embed": offer_embed(offer, overview=True)}
               for offer in selected_offers(snapshot["offers"])]
    for error in snapshot["errors"]:
        events.append({"failure": error, "embed": {"title": "Eneba source check failed",
            "description": f"{error['source']}: {error['error']}", "color": 0xBF4545}})
    items = {}
    for event in events:
        reference = retention_item(event)
        embed = event["embed"]
        if "offer" in event:
            embed["description"] = embed["description"].replace(
                "Current overview — requested refresh", "Current price — refreshed after 60 days")
        else:
            label = "Source status — refreshed after 60 days" if "failure" in event else "Still available — refreshed after 60 days"
            embed["description"] = f"**{label}**\n" + embed["description"]
        item = {"scope": reference["scope"], "embed": embed}
        end = event.get("campaign", {}).get("terms", {}).get("end") if event.get("campaign", {}).get("terms") else None
        if end:
            item["ends_at"] = end
        items[reference["key"]] = item
    write_snapshot(path, {"version": 1, "destination": "eneba", "observed_at": stamp(now),
        "identity": {"username": "Eneba Sale Watcher", "avatar_url": AVATAR_URL,
                     "content": "Eneba current offers — refreshed after 60 days"},
        "coverage": coverage, "items": items})


def run(snapshot, state, dry_run=False, webhook=None, save=None, post=post_discord, now=None, min_drop_percent=1,
        journal_path=None):
    now = now or utc_now()
    events = plan_events(snapshot, state, min_drop_percent)
    grouped = list(batches(events))
    if dry_run:
        return {"dry_run": True, "offers_checked": len(snapshot["offers"]), "indonesia_eligible_offers": sum(eligible(offer, "ID") for offer in snapshot["offers"]),
                "messages": [payload(batch) for batch in grouped], "source_errors": snapshot["errors"]}
    if grouped and not webhook:
        raise SourceError("DISCORD_WEBHOOK_URL is missing; use --dry-run to preview")
    updated = copy.deepcopy(state)
    journal_path = journal_path or os.getenv("DISCORD_RETENTION_FILE")
    acknowledgments = []
    for batch in grouped:
        acknowledgment = post(webhook, payload(batch))
        if journal_path:
            record_message(journal_path, acknowledgment, [retention_item(event) for event in batch], now=now)
        acknowledgments.append(acknowledgment)
        for event in batch:
            updated["notified"][event["key"]] = stamp(now)
            if "campaign" in event:
                updated["campaigns"][event["campaign"]["key"]] = stamp(now)
            if "offer" in event:
                offer = event["offer"]
                updated["prices"][price_key(offer)] = {"price_minor": offer["price_minor"], "currency": offer["currency"], "source_country": offer["source_country"], "at": stamp(now)}
            if "failure" in event:
                failure = event["failure"]
                updated["failures"][failure["source"]] = failure["error"]
        if save:
            save(updated)
    # Only complete successful collection can reset failure status. Failed products keep their last price.
    for offer in snapshot["offers"]:
        updated["prices"][price_key(offer)] = {"price_minor": offer["price_minor"], "currency": offer["currency"], "source_country": offer["source_country"], "at": stamp(now)}
    updated["failures"] = {error["source"]: error["error"] for error in snapshot["errors"]}
    if save:
        save(updated)
    return {"sent_events": len(events), "offers_checked": len(snapshot["offers"]), "acknowledgments": acknowledgments, "source_errors": snapshot["errors"]}


def run_overview(snapshot, dry_run=False, webhook=None, post=post_discord, checkpoint=None, now=None,
                 journal_path=None):
    """One explicitly requested refresh; never read, save, or acknowledge regular sale history."""
    events = [{"campaign": campaign, "embed": campaign_embed(campaign, overview=True)} for campaign in snapshot["campaigns"]]
    events.extend({"offer": offer, "embed": offer_embed(offer, overview=True)} for offer in selected_offers(snapshot["offers"]))
    for error in snapshot["errors"]:
        events.append({"failure": error, "embed": {
            "title": "Current overview — source unavailable", "color": 0xBF4545,
            "description": f"{error['source']}: {error['error']}",
            "fields": [{"name": "Current data unavailable", "value": "This refresh cannot confirm offers from the failed source. Historical prices were not reused as current offers, and regular notification history was not changed."}],
        }})
    if not events:
        events.append({"embed": {"title": "Current overview — requested refresh", "description": "The successful public source checks returned no matching available Minecoin listings or featured broad campaigns. Regular notification history was not changed.", "color": 0x7139CB}})
    grouped = list(batches(events))
    result = {"announce_current": True, "offers_checked": len(snapshot["offers"]),
              "indonesia_eligible_offers": sum(eligible(offer, "ID") for offer in snapshot["offers"]),
              "source_errors": snapshot["errors"]}
    if dry_run:
        return dict(result, dry_run=True, messages=[payload(batch, overview=True) for batch in grouped])
    if not webhook:
        raise SourceError("DISCORD_WEBHOOK_URL is missing; use --dry-run to preview")
    acknowledgments = []
    journal_path = journal_path or os.getenv("DISCORD_RETENTION_FILE")
    for batch in grouped:
        acknowledgment = post(webhook, payload(batch, overview=True))
        if journal_path:
            record_message(journal_path, acknowledgment, [retention_item(event) for event in batch],
                           now=now or utc_now(), purpose="overview")
        # A refresh acknowledgment belongs only to this manual request, never a normal sale event.
        safe_ack = {"message_id": acknowledgment["message_id"], "channel_id": acknowledgment["channel_id"]}
        acknowledgments.append(safe_ack)
        if checkpoint:
            checkpoint(safe_ack)
    return dict(result, sent_overview_cards=len(events), acknowledgments=acknowledgments)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--fixture", help="offline snapshot JSON; requires dry run")
    args = parser.parse_args(argv)
    dry_run = args.dry_run or os.getenv("DRY_RUN", "").lower() in {"1", "true"}
    if args.fixture and not dry_run:
        parser.error("--fixture requires --dry-run")
    now = utc_now()
    snapshot = json.loads(Path(args.fixture).read_text(encoding="utf-8")) if args.fixture else collect(
        os.getenv("ENEBA_COUNTRY", "ID"), os.getenv("ENEBA_ALT_COUNTRY", "DE"), os.getenv("ENEBA_CURRENCY", "EUR"), now=now)
    export_retention_snapshot(snapshot, now, os.getenv("DISCORD_RETENTION_SNAPSHOT"))
    announce_current = os.getenv("ANNOUNCE_CURRENT", "false").lower() in {"1", "true"}
    if announce_current:
        result = run_overview(snapshot, dry_run=dry_run, webhook=os.getenv("DISCORD_WEBHOOK_URL"),
                              checkpoint=lambda ack: print(json.dumps({"overview_acknowledgment": ack})), now=now)
    else:
        file = os.getenv("ENEBA_STATE_FILE", STATE_FILE)
        minimum = float(os.getenv("ENEBA_MIN_DROP_PERCENT", "1"))
        if not 0 <= minimum <= 100:
            raise SourceError("ENEBA_MIN_DROP_PERCENT must be between 0 and 100")
        result = run(snapshot, read_state(file), dry_run=dry_run, webhook=os.getenv("DISCORD_WEBHOOK_URL"), save=lambda state: save_state(state, file), now=now, min_drop_percent=minimum)
    print(json.dumps(result, indent=2))
    return 1 if snapshot["errors"] else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (SourceError, OSError, ValueError) as error:
        # Never log request objects, webhook URLs or response bodies containing credentials.
        print(f"Eneba watcher failed: {error}", file=sys.stderr)
        sys.exit(1)
