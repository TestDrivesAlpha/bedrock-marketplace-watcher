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

## Testing and manual runs

Both Actions workflows offer a **dry_run** option under **Run workflow**. It is
enabled by default for manual runs. A dry run previews currently eligible alerts
without posting to Discord or changing notification history. Scheduled runs send
eligible alerts normally. Existing notification history can mean there is no new
alert to preview.

Run the offline tests from the repository root:

```sh
node --test watcher.test.mjs
python -m unittest -v test_steam_watcher.py
```

The tests use fixtures and mocked webhook requests. They do not post messages.
Each scheduled workflow runs its tests before checking for sales. Keep webhook
URLs in GitHub repository secrets; do not put them in source files or logs.
