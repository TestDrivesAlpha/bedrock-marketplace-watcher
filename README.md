# Discord sale alerts

## Minecraft Bedrock Marketplace

The Marketplace workflow checks every 15 minutes. Alerts identify each pack with
its cover image, creator, official product link, available Minecoin prices, and
promotion dates. Discord displays dates in each reader's local time zone.

Promotion percentages and payable prices are separate source fields. When a
lower price cannot be confirmed, the alert explicitly labels the reported
promotion as unverified. Future and expired promotions are excluded. Discount
history describes observations made by this bot, not the complete price history
of a product.

The reader is [PlayFab Catalog Service Bedrock](https://github.com/Daniel-Ric/PlayFab-Catalog-Service-Bedrock).
Official Minecraft product metadata supplies additional pack details when available.
The existing Discord webhook is stored in the `DISCORD_WEBHOOK_URL` repository secret.

## Steam

The Steam workflow checks hourly for the configured major seasonal sales. Each
alert includes the sale name, dates, a direct Steam discounts link, and official
sale artwork when available. If matching event artwork is unavailable, it uses
the official Steam logo and labels that fallback. This watcher announces seasonal
events; it does not track individual games or invent game discount percentages.

The configured dates were checked against [Valve's official events schedule](https://partner.steamgames.com/doc/marketing/upcoming_events)
on October 17, 2026. The current schedule covers Autumn 2026 through Summer 2027
and will need extending for later events. The existing Steam webhook is stored in
the `DISCORD_STEAM_WEBHOOK_URL` repository secret. Notification history is retained
in `.steam-sale-notified`.

## Eneba campaigns and Minecoin codes

`deal-watchers.yml` checks hourly for broad sale campaigns featured on Eneba's
public homepage and for observed listing-price drops on genuine Minecoin
prepaid codes. New offers establish an explicitly labelled baseline; a first
observation is not claimed to be a discount or an all-time low.

The public site can choose a storefront country from the runner's network location,
even when the URL uses the Indonesian English locale. Alerts show the actual country
from each source query as a regional reference. A foreign storefront price is not
presented as a confirmed Indonesian checkout quote, and its campaign is not assumed
to apply in Indonesia. Missing or ambiguous source-country data fails explicitly.

Each product's published activation-country list is checked. Indonesia-listed
codes appear first; Germany-only alternatives are clearly identified. A product
called "Global" is not assumed to support Indonesia. Prices are pinned to EUR,
before checkout fees; cashback is displayed separately and is not deducted from
the price. The default notification threshold is a 1% drop for the same product,
currency and actual storefront country. A change of storefront establishes a
separate baseline rather than being called a price drop.

The current FEST coupon terms are a dated snapshot captured on October 17, 2026,
with the site's stated expiry. Its applicability to Minecoin codes is unverified.
Future campaign alerts link to current terms when a coupon or its dates cannot be
verified. The campaign list covers featured homepage sales rather than every offer
on Eneba.

## Pokemon games and DLC

The same workflow tracks these exact products:

- Pokemon Sword, base game
- Pokemon Violet, base game
- Pokemon Sword Expansion Pass, requiring Sword
- Pokemon Violet: The Hidden Treasure of Area Zero, requiring Violet

Deku Deals Germany and US listings are compared separately by game, currency,
format and condition. A physical item price is not a delivered-total comparison,
and a tracker listing is not a guarantee of retailer stock or shipping eligibility.
Optional direct Playasia and Indonesian PSe Gameshop sources are checked when
public access works; blocked sources are not represented as current offers.

The Nintendo Account country is currently unknown. Digital offers are explicitly
labelled regional price references with redemption unconfirmed. Neither an email
address nor the buyer's physical location establishes the account's store country.
No account credentials or email addresses are stored by these watchers.

Alerts include available cover images, retailer and source links, observed price,
region, format, stock details and DLC requirements. After the initial baseline,
price drops and availability changes produce updates. Failed-source prices are
not reused as fresh observations.

Both new watchers post to the existing game-deal destination through the
`DISCORD_STEAM_WEBHOOK_URL` repository secret, using distinct bot names. No new
webhook credential is required. State is kept in `.eneba-price-state.json` and
`.pokemon-price-state.json`; successful delivery batches are checkpointed so a
later failure leaves the remaining alerts pending.

## Testing and manual runs

All three sale-watching workflows offer a **dry_run** option under **Run workflow**. It is
enabled by default for manual runs. A dry run previews currently eligible alerts
without posting to Discord or changing notification history. Scheduled runs send
eligible alerts normally. Existing notification history can mean there is no new
alert to preview.

Run the offline tests from the repository root:

```sh
node --test watcher.test.mjs
python -m unittest -v test_steam_watcher.py
python -m unittest -v test_eneba_watcher.py test_pokemon_watcher.py
```

The tests use fixtures and mocked webhook requests. They do not post messages.
Each scheduled workflow runs its tests before checking for sales. Keep webhook
URLs in GitHub repository secrets; do not put them in source files or logs.
