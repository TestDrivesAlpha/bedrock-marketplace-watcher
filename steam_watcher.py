"""Announce active major Steam seasonal sales with an image and store link."""

import json
import os
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from urllib.request import Request, urlopen

STORE_URL = "https://store.steampowered.com/specials/"
HOME_URL = "https://store.steampowered.com/?l=english"
STEAM_LOGO = "https://store.fastly.steamstatic.com/public/shared/images/responsive/share_steam_logo.png"
SCHEDULE_SOURCE = "https://partner.steamgames.com/doc/marketing/upcoming_events"
AVATAR_URL = "https://raw.githubusercontent.com/TestDrivesAlpha/bedrock-marketplace-watcher/main/assets/steam-catgirl-avatar.png"

# Existing event IDs and dates are retained. Valve's official schedule was
# checked on 2026-10-17; the original 10 AM Pacific start/end times are retained.
SALES = [
    {"id": "autumn-2026", "name": "Steam Autumn Sale 2026",
     "start": "2026-10-01T17:00:00+00:00", "end": "2026-10-08T17:00:00+00:00"},
    {"id": "winter-2026", "name": "Steam Winter Sale 2026",
     "start": "2026-12-17T18:00:00+00:00", "end": "2027-01-04T18:00:00+00:00"},
    {"id": "spring-2027", "name": "Steam Spring Sale 2027",
     "start": "2027-03-18T17:00:00+00:00", "end": "2027-03-25T17:00:00+00:00"},
    {"id": "summer-2027", "name": "Steam Summer Sale 2027",
     "start": "2027-06-24T17:00:00+00:00", "end": "2027-07-08T17:00:00+00:00"},
]


class SocialMetadata(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.values = {}

    def handle_starttag(self, tag, attrs):
        if tag.lower() != "meta":
            return
        values = dict(attrs)
        key = values.get("property") or values.get("name")
        if key and values.get("content"):
            self.values.setdefault(key.lower(), values["content"])


def official_image(url):
    """Only embed HTTPS images hosted on Steam's own image CDNs."""
    parsed = urlsplit(url)
    host = (parsed.hostname or "").lower()
    return (
        parsed.scheme == "https"
        and parsed.username is None and parsed.password is None
        and parsed.port in (None, 443)
        and any(host == suffix or host.endswith("." + suffix)
                for suffix in ("steamstatic.com", "steamcdn-a.akamaihd.net"))
    )


def event_image(sale, html):
    """Use current event artwork only when its title identifies this sale."""
    metadata = SocialMetadata()
    metadata.feed(html)
    titles = [metadata.values.get(key, "").casefold()
              for key in ("og:title", "twitter:title")]
    # A homepage may advertise a different fest or have a generic store image.
    # Never label that artwork as this seasonal sale's banner.
    event_name = sale["name"].removeprefix("Steam ").casefold()
    if not any(event_name in title for title in titles):
        return STEAM_LOGO, False
    for key in ("og:image", "twitter:image"):
        image_url = metadata.values.get(key, "")
        try:
            if official_image(image_url):
                return image_url, True
        except ValueError:
            pass
    return STEAM_LOGO, False


def fetch_homepage():
    request = Request(HOME_URL, headers={"User-Agent": "Steam-Sale-Watcher/2.0"})
    with urlopen(request, timeout=20) as response:
        return response.read(2_000_000).decode("utf-8", errors="replace")


def build_payload(sale, html=""):
    start = int(datetime.fromisoformat(sale["start"]).timestamp())
    end = int(datetime.fromisoformat(sale["end"]).timestamp())
    image_url, event_artwork = event_image(sale, html)
    return {
        "content": "A major Steam seasonal sale is live!",
        "avatar_url": AVATAR_URL,
        "allowed_mentions": {"parse": []},
        "embeds": [{
            "title": sale["name"],
            "url": STORE_URL,
            "description": (
                "Browse the seasonal discounts on Steam. "
                "Individual game prices and availability vary by region.\n\n"
                f"**Started:** <t:{start}:F>\n"
                f"**Ends:** <t:{end}:F> (<t:{end}:R>)\n\n"
                f"[Browse Steam discounts]({STORE_URL})"
            ),
            "color": 0x1B9BDF,
            "image": {"url": image_url},
            "footer": {"text": (
                "Official Steam sale artwork" if event_artwork
                else "Steam logo - event artwork is not available"
            )},
        }],
    }


def send_discord(webhook, payload):
    parts = urlsplit(webhook)
    query = [(key, value) for key, value in parse_qsl(parts.query) if key != "wait"]
    query.append(("wait", "true"))
    url = urlunsplit(parts._replace(query=urlencode(query)))
    request = Request(url, data=json.dumps(payload).encode("utf-8"), method="POST",
                      headers={"Content-Type": "application/json",
                               "User-Agent": "Steam-Sale-Watcher/2.0"})
    try:
        with urlopen(request, timeout=30) as response:
            if not 200 <= response.status < 300:
                raise RuntimeError(f"Discord webhook failed (HTTP {response.status}).")
    except HTTPError as error:
        raise RuntimeError(f"Discord webhook failed (HTTP {error.code}).") from None
    except (URLError, TimeoutError):
        # Do not include an exception containing the secret webhook URL in logs.
        raise RuntimeError("Discord webhook failed (network error).") from None


def check_sales(now, state_file, *, dry_run=False, webhook=None,
                fetch_html=fetch_homepage, post_payload=send_discord, sales=SALES):
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("Current time must include a timezone.")
    state_file = Path(state_file)
    notified = set(state_file.read_text(encoding="utf-8").split()) if state_file.exists() else set()
    count = 0
    for sale in sales:
        start = datetime.fromisoformat(sale["start"])
        end = datetime.fromisoformat(sale["end"])
        if not start <= now < end or sale["id"] in notified:
            continue
        try:
            html = fetch_html()
        except (HTTPError, URLError, TimeoutError, OSError):
            print("Steam artwork unavailable; using the official Steam logo.")
            html = ""
        payload = build_payload(sale, html)
        if dry_run:
            print(json.dumps(payload, indent=2))
            count += 1
            continue
        if not webhook:
            raise RuntimeError("DISCORD_STEAM_WEBHOOK_URL secret is missing.")
        post_payload(webhook, payload)
        # Persist after confirmed success, never for a preview or failed send.
        notified.add(sale["id"])
        temporary = state_file.with_name(state_file.name + ".tmp")
        temporary.write_text("\n".join(sorted(notified)) + "\n", encoding="utf-8")
        temporary.replace(state_file)
        count += 1
        print(f"Discord notified: {sale['name']}")
    if not count:
        print("No new major Steam sale to announce.")
    return count


def main():
    dry_run = os.environ.get("DRY_RUN", "false").strip().lower() in ("1", "true", "yes")
    check_sales(datetime.now(timezone.utc), Path(".steam-sale-notified"),
                dry_run=dry_run, webhook=os.environ.get("DISCORD_WEBHOOK_URL"))


if __name__ == "__main__":
    main()
