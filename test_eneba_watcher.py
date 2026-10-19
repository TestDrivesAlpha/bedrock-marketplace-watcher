"""Public Apollo fixtures match captured Eneba Minecoin pages; no network or secrets."""
import copy
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError

import eneba_watcher as watcher

NOW = datetime(2026, 10, 18, 1, 10, tzinfo=timezone.utc)
SIMPLE = "xbox-minecraft-minecoins-pack-1720-coins-key-global"
COMPATIBLE = "xbox-minecraft-minecoins-pack-1720-coins-xbox-one-xbox-live-key-global"
IMAGE = "https://imgproxy.eneba.games/public-minecoin-cover"
CONTEXT = {"context": {"country": "ID", "language": "en_ID", "region": "indonesia"}}


def key(name, args=None):
    return name + "(" + json.dumps(args or CONTEXT, separators=(",", ":")) + ")"


def html(state):
    return '<html><script id="__APOLLO_STATE__" type="application/json">' + json.dumps(state) + "</script></html>"


def fixture(slug=SIMPLE, cents=557, compatible=False, stock=True):
    name = "Minecraft: Minecoins Pack: 1720 Coins " + ("(Xbox One) Xbox Live " if compatible else "") + "Key GLOBAL"
    product = {
        "__typename": "Product", "slug": slug, "name": name,
        "type": {"value": "GAME_POINTS"}, "drm": {"slug": "xbox", "name": "Xbox Live"},
        "regions": [{"code": "global", "name": "Global"}],
        'cover({"width":300})': {"src": IMAGE, "src2x": IMAGE + "-2x"},
        "cheapestAuction": {"__ref": "Auction::public-auction"},
        "cashback": {"valuePercent": 6},
    }
    auction = {
        "__typename": "Auction", "isInStock": stock, "isAddableToCart": stock,
        'price({"currency":"EUR"})': {"amount": cents, "currency": "EUR"},
        'cashback({"currency":"EUR"})': {"price": {"amount": 33, "currency": "EUR"}},
        "merchant": {"displayname": "Ultimate Choice"},
    }
    state = {"ROOT_QUERY": {key("search"): {"results": {"totalCount": 1, "edges": [{"node": {"__ref": "Product::" + slug}}]}}},
             "Product::" + slug: product, "Auction::public-auction": auction}
    detail = copy.deepcopy(product)
    detail["countriesAlpha2"] = ["DE", "GB", "US"] + (["ID"] if compatible else [])
    detail["blockedCountries"] = []
    detail["notes"] = ["A Microsoft/Xbox account is required for redemption."]
    details = {"ROOT_QUERY": {key("footerPayments"): [], key("product"): {"__ref": "Product::" + slug}}, "Product::" + slug: detail}
    return state, details


def offer(slug=COMPATIBLE, cents=581, compatible=True):
    search, details = fixture(slug, cents, compatible)
    offers, _, _ = watcher.offers_from_html(html(search))
    return watcher.enrich_offer(offers[0], html(details))


def snapshot(offers=None, campaigns=None, errors=None):
    return {"campaigns": campaigns or [], "offers": offers or [], "errors": errors or []}


def homepage():
    return {"ROOT_QUERY": {key("Slider_sliders"): {"edges": [
        {"node": {"text": "GAMETOBERFEST SALE | OCT 26", "urlPath": "/sales/gametoberfest-sale", 'resizedImageUrl({"width":1500})': IMAGE}},
        {"node": {"text": "GTA VI SHORT PUSH", "urlPath": "/sales/gta-6-preorder-gift-cards-deals"}},
        {"node": {"text": "NORDVPN", "urlPath": "/promo/nordvpn"}},
    ]}}}


class EnebaWatcherTests(unittest.TestCase):
    def test_real_apollo_money_is_cents_and_cashback_is_separate(self):
        search, details = fixture()
        offers, total, count = watcher.offers_from_html(html(search))
        current = watcher.enrich_offer(offers[0], html(details))
        self.assertEqual((total, count), (1, 1))
        self.assertEqual(current["price_minor"], 557)
        self.assertEqual(current["cashback_minor"], 33)
        self.assertEqual(watcher.money(current["price_minor"]), "EUR 5.57")
        embed = watcher.offer_embed(current)
        cashback = next(field["value"] for field in embed["fields"] if "Cashback" in field["name"])
        self.assertIn("not** an upfront price reduction", cashback)
        self.assertIn("EUR 0.33", cashback)

    def test_same_global_label_has_different_actual_country_restrictions(self):
        excluded = offer(SIMPLE, 557, False)
        compatible = offer(COMPATIBLE, 581, True)
        self.assertFalse(watcher.eligible(excluded, "ID"))
        self.assertTrue(watcher.eligible(excluded, "DE"))
        self.assertTrue(watcher.eligible(compatible, "ID"))
        blocked = dict(compatible, blocked_countries=["ID"])
        self.assertFalse(watcher.eligible(blocked, "ID"))
        self.assertIn("Germany alternative only", watcher.offer_embed(excluded)["fields"][0]["value"])

    def test_wrong_currency_country_or_missing_layout_cannot_become_empty_success(self):
        search, _ = fixture()
        wrong_currency = copy.deepcopy(search)
        wrong_currency["Auction::public-auction"]['price({"currency":"EUR"})']["currency"] = "IDR"
        with self.assertRaises(watcher.SourceError):
            watcher.offers_from_html(html(wrong_currency))
        missing_country = copy.deepcopy(search)
        missing_country["ROOT_QUERY"] = {key("search", {"context": {"language": "en_ID"}}): next(iter(search["ROOT_QUERY"].values()))}
        with self.assertRaises(watcher.SourceError):
            watcher.offers_from_html(html(missing_country))
        for body in ["<html>Verify you are human</html>", "<html>Access denied</html>", html({"ROOT_QUERY": {key("footerPayments"): []}})]:
            with self.assertRaises(watcher.SourceError):
                watcher.offers_from_html(body)

    def test_out_of_stock_is_excluded_and_changed_product_schema_is_failure(self):
        search, _ = fixture(stock=False)
        self.assertEqual(watcher.offers_from_html(html(search))[0], [])
        search, _ = fixture()
        search["Product::" + SIMPLE]["type"] = None
        with self.assertRaises(watcher.SourceError):
            watcher.offers_from_html(html(search))

    def test_missing_country_details_do_not_assume_global_eligibility(self):
        search, details = fixture()
        listing = watcher.offers_from_html(html(search))[0][0]
        del details["Product::" + SIMPLE]["countriesAlpha2"]
        with self.assertRaises(watcher.SourceError):
            watcher.enrich_offer(listing, html(details))

    def test_server_country_mismatch_is_a_labelled_reference_with_independent_indonesia_activation(self):
        search, details = fixture(COMPATIBLE, 581, True)
        result = next(iter(search["ROOT_QUERY"].values()))
        search["ROOT_QUERY"] = {key("search", {"context": {"country": "US", "language": "en_ID", "region": "global"}}): result,
                                key("footerPayments"): []}
        detail_product = details["Product::" + COMPATIBLE]
        details["ROOT_QUERY"] = {key("product", {"context": {"country": "US", "language": "en_ID"}}): {"__ref": "Product::" + COMPATIBLE}}
        current = watcher.enrich_offer(watcher.offers_from_html(html(search))[0][0], html(details))
        self.assertEqual(current["source_country"], "US")
        self.assertEqual(current["activation_source_country"], "US")
        self.assertTrue(watcher.eligible(current, "ID"))
        description = watcher.offer_embed(current)["description"]
        self.assertIn("US storefront context", description)
        self.assertIn("not a verified Indonesian checkout quote", description)
        home = homepage()
        slider = next(iter(home["ROOT_QUERY"].values()))
        home["ROOT_QUERY"] = {key("Slider_sliders", {"context": {"country": "US", "language": "en_ID"}}): slider}
        campaign = watcher.campaigns_from_html(html(home), now=NOW)[0]
        self.assertIn("US storefront context", watcher.campaign_embed(campaign)["description"])
        self.assertNotIn("Indonesia homepage", watcher.campaign_embed(campaign)["description"])
        self.assertIn("Applicability to this storefront is unverified", watcher.campaign_embed(campaign)["fields"][1]["value"])
        self.assertEqual(detail_product["countriesAlpha2"][-1], "ID")

    def test_missing_or_ambiguous_relevant_storefront_countries_fail_even_if_other_queries_have_indonesia(self):
        search, _ = fixture()
        result = next(iter(search["ROOT_QUERY"].values()))
        for root in [
            {key("footerPayments"): [], key("search", {"context": {"language": "en_ID"}}): result},
            {key("search"): result, key("search", {"context": {"country": "US"}}): result},
        ]:
            changed = copy.deepcopy(search)
            changed["ROOT_QUERY"] = root
            with self.assertRaises(watcher.SourceError):
                watcher.offers_from_html(html(changed))

    def test_featured_broad_campaign_has_cover_and_dated_coupon_terms_not_minecoin_promise(self):
        campaigns = watcher.campaigns_from_html(html(homepage()), now=NOW)
        self.assertEqual(len(campaigns), 1)
        self.assertEqual(campaigns[0]["terms"]["code"], "FEST")
        embed = watcher.campaign_embed(campaigns[0])
        self.assertEqual(embed["image"]["url"], IMAGE)
        terms = " ".join(field["value"] for field in embed["fields"])
        self.assertIn("captured 2026-10-17", terms)
        self.assertIn("one game under EUR 80", terms)
        self.assertIn("Unverified", terms)
        self.assertEqual(watcher.campaigns_from_html(html(homepage()), now=datetime(2026, 10, 19, 2, tzinfo=timezone.utc)), [])

    def test_missing_campaign_cover_uses_labeled_brand_banner_in_alert_overview_and_refresh(self):
        home = homepage()
        del next(iter(home["ROOT_QUERY"].values()))["edges"][0]['node']['resizedImageUrl({"width":1500})']
        campaign = watcher.campaigns_from_html(html(home), now=NOW)[0]
        original = copy.deepcopy(campaign)
        data = snapshot(campaigns=[campaign])
        alert = watcher.plan_events(data, watcher.empty_state())[0]["embed"]
        overview = watcher.run_overview(data, dry_run=True)["messages"][0]["embeds"][0]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "current.json"
            watcher.export_retention_snapshot(data, NOW, path)
            refreshed = json.loads(path.read_text(encoding="utf8"))["items"]["campaign:" + campaign["key"]]["embed"]
        for embed in (alert, overview, refreshed):
            self.assertEqual(embed["image"]["url"], watcher.BANNER_URL)
            self.assertEqual(embed["thumbnail"]["url"], watcher.AVATAR_URL)
            self.assertIn("channel artwork", embed["footer"]["text"])
            self.assertIn("campaign image unavailable", embed["footer"]["text"])
            self.assertEqual(embed["title"], campaign["title"])
            self.assertEqual(embed["url"], campaign["url"])
            self.assertIn("ID storefront context", embed["description"])
            self.assertEqual(embed["fields"], alert["fields"])
        self.assertEqual(campaign, original)
        genuine = watcher.campaigns_from_html(html(homepage()), now=NOW)[0]
        self.assertEqual(watcher.campaign_embed(genuine)["image"]["url"], IMAGE)
        self.assertEqual(watcher.campaign_embed(genuine)["thumbnail"]["url"], IMAGE)
        self.assertNotIn("footer", watcher.campaign_embed(genuine))

    def test_real_offer_and_campaign_artwork_is_visible_at_top_in_alert_overview_and_refresh(self):
        current = offer()
        campaign = watcher.campaigns_from_html(html(homepage()), now=NOW)[0]
        data = snapshot(offers=[current], campaigns=[campaign])
        normal = [event["embed"] for event in watcher.plan_events(data, watcher.empty_state())]
        overview = [embed for message in watcher.run_overview(data, dry_run=True)["messages"] for embed in message["embeds"]]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "current.json"
            watcher.export_retention_snapshot(data, NOW, path)
            refreshed = [item["embed"] for item in json.loads(path.read_text(encoding="utf8"))["items"].values()]
        for embeds in (normal, overview, refreshed):
            self.assertEqual(len(embeds), 2)
            artwork = {embed["title"]: embed for embed in embeds}
            for title, image in ((campaign["title"], campaign["image"]), (current["title"], current["image"])):
                self.assertEqual(artwork[title]["thumbnail"]["url"], image)
                self.assertEqual(artwork[title]["image"]["url"], image)

    def test_source_status_has_labeled_brand_thumbnail_in_alert_overview_and_refresh(self):
        error = {"source": "Eneba Minecoin search", "error": "public page HTTP 403; no offers inferred"}
        data = snapshot(errors=[error])
        alert = watcher.plan_events(data, watcher.empty_state())[0]["embed"]
        overview = watcher.run_overview(data, dry_run=True)["messages"][0]["embeds"][0]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "current.json"
            watcher.export_retention_snapshot(data, NOW, path)
            refreshed = json.loads(path.read_text(encoding="utf8"))["items"]["health:" + error["source"]]["embed"]
        for embed in (alert, overview, refreshed):
            self.assertEqual(embed["thumbnail"]["url"], watcher.AVATAR_URL)
            self.assertEqual(embed["footer"]["text"], "Eneba watcher artwork")
            self.assertNotIn("image", embed)
            self.assertIn(error["source"], embed["description"])
            self.assertIn(error["error"], embed["description"])
        self.assertIn("monitoring failure", alert["fields"][0]["value"])
        self.assertIn("not reused as current offers", overview["fields"][0]["value"])

    def test_empty_requested_overview_has_brand_thumbnail_without_inventing_an_offer(self):
        result = watcher.run_overview(snapshot(), dry_run=True)
        self.assertEqual(len(result["messages"][0]["embeds"]), 1)
        embed = result["messages"][0]["embeds"][0]
        self.assertEqual(embed["thumbnail"]["url"], watcher.AVATAR_URL)
        self.assertEqual(embed["footer"]["text"], "Eneba watcher artwork")
        self.assertNotIn("image", embed)
        self.assertNotIn("url", embed)
        self.assertIn("no matching available", embed["description"])

    def test_artwork_labels_count_toward_discord_batch_text_limit(self):
        embed = watcher.status_artwork({"title": "Status", "description": "x" * 2980})
        expected = len(embed["title"]) + len(embed["description"]) + len(embed["footer"]["text"])
        self.assertEqual(watcher.embed_size(embed), expected)
        self.assertGreater(2 * expected, 6000)
        self.assertEqual(len(list(watcher.batches([{"embed": embed}, {"embed": embed}]))), 2)

    def test_baseline_is_identifiable_and_has_no_invented_sale(self):
        current = offer()
        events = watcher.plan_events(snapshot([current]), watcher.empty_state())
        self.assertEqual(len(events), 1)
        embed = events[0]["embed"]
        self.assertEqual(embed["image"]["url"], IMAGE + "-2x")
        self.assertEqual(embed["url"], current["url"])
        self.assertIn("baseline; no price discount is claimed", embed["description"])
        self.assertIn("Unverified", " ".join(field["value"] for field in embed["fields"]))
        self.assertIn("per 1,000 Minecoins", embed["fields"][1]["value"])

    def test_indonesia_primary_precedes_separate_cheaper_germany_alternative(self):
        state = watcher.empty_state()
        events = watcher.plan_events(snapshot([offer(SIMPLE, 557, False), offer(COMPATIBLE, 581, True)]), state)
        self.assertEqual(len(events), 2)
        self.assertEqual(events[0]["offer"]["slug"], COMPATIBLE)
        self.assertEqual(events[1]["offer"]["slug"], SIMPLE)
        self.assertIn("Indonesia not supported", events[1]["embed"]["fields"][0]["value"])

    def test_drop_uses_same_sku_observed_price_and_does_not_subtract_cashback(self):
        current = offer(cents=500)
        state = watcher.empty_state()
        state["notified"]["baseline:" + COMPATIBLE + ":ID"] = watcher.stamp(NOW)
        state["prices"][watcher.price_key(current)] = {"price_minor": 600, "currency": "EUR", "source_country": "ID", "at": watcher.stamp(NOW)}
        events = watcher.plan_events(snapshot([current]), state)
        self.assertEqual(len(events), 1)
        self.assertIn("16.7%", events[0]["embed"]["description"])
        same_price = dict(current, price_minor=600, cashback_minor=120, cashback_percent=20)
        self.assertEqual(watcher.plan_events(snapshot([same_price]), state), [])
        tiny_drop = dict(current, price_minor=599)
        self.assertEqual(watcher.plan_events(snapshot([tiny_drop]), state), [])

    def test_different_storefront_prices_are_not_called_a_price_drop(self):
        current = offer(cents=500)
        state = watcher.empty_state()
        state["prices"][watcher.price_key(dict(current, source_country="US"))] = {"price_minor": 600, "currency": "EUR", "source_country": "US", "at": watcher.stamp(NOW)}
        state["notified"]["baseline:" + COMPATIBLE + ":US"] = watcher.stamp(NOW)
        events = watcher.plan_events(snapshot([current]), state)
        self.assertEqual(len(events), 1)
        self.assertIn("baseline; no price discount is claimed", events[0]["embed"]["description"])
        state["notified"]["baseline:" + COMPATIBLE + ":ID"] = watcher.stamp(NOW)
        self.assertEqual(watcher.plan_events(snapshot([current]), state), [])

    def test_alternating_runner_storefronts_keep_each_countrys_price_history(self):
        saved = []
        state = watcher.empty_state()
        for current in [offer(cents=600), dict(offer(cents=900), source_country="US")]:
            watcher.run(snapshot([current]), state, webhook="https://discord.invalid/secret", now=NOW,
                        post=lambda *args: {"message_id": "1", "channel_id": "2"},
                        save=lambda value: saved.append(copy.deepcopy(value)))
            state = saved[-1]
        lower = offer(cents=500)
        self.assertEqual(len(state["prices"]), 2)
        events = watcher.plan_events(snapshot([lower]), state)
        self.assertEqual(len(events), 1)
        self.assertIn("EUR 6.00", events[0]["embed"]["description"])
        self.assertNotIn("EUR 9.00", events[0]["embed"]["description"])

    def test_dry_run_has_no_post_no_save_and_no_state_mutation(self):
        state = watcher.empty_state()
        before = copy.deepcopy(state)
        def forbidden(*args):
            self.fail("dry-run side effect")
        result = watcher.run(snapshot([offer()]), state, dry_run=True, save=forbidden, post=forbidden, now=NOW)
        self.assertTrue(result["dry_run"])
        self.assertEqual(result["indonesia_eligible_offers"], 1)
        self.assertEqual(state, before)
        self.assertEqual(result["messages"][0]["allowed_mentions"], {"parse": []})
        self.assertEqual(result["messages"][0]["avatar_url"], watcher.AVATAR_URL)

    def test_requested_overview_reuses_current_images_regions_and_fees_without_acknowledging_normal_events(self):
        data = snapshot([offer(COMPATIBLE, 581, True), offer(SIMPLE, 557, False)],
                        watcher.campaigns_from_html(html(homepage()), now=NOW))
        state = watcher.empty_state()
        state["prices"][watcher.price_key(data["offers"][0])] = {"price_minor": 900, "currency": "EUR", "source_country": "ID", "at": watcher.stamp(NOW)}
        state["notified"]["baseline:" + COMPATIBLE + ":ID"] = watcher.stamp(NOW)
        pending = watcher.plan_events(data, state)
        original = copy.deepcopy(state)
        messages, checkpoints = [], []
        def post(url, message):
            messages.append(message)
            return {"message_id": "123", "channel_id": "456", "token": "must-not-leak"}
        result = watcher.run_overview(data, webhook="https://discord.invalid/secret", post=post, checkpoint=checkpoints.append)
        self.assertTrue(result["announce_current"])
        self.assertEqual(result["sent_overview_cards"], 3)
        self.assertEqual(state, original)
        self.assertEqual(watcher.plan_events(data, state), pending)
        self.assertEqual(checkpoints, result["acknowledgments"])
        for acknowledgment in checkpoints:
            self.assertEqual(acknowledgment, {"message_id": "123", "channel_id": "456"})
        embeds = [embed for message in messages for embed in message["embeds"]]
        self.assertTrue(all("Current overview — requested refresh" in embed["description"] for embed in embeds))
        self.assertTrue(all(embed.get("image") for embed in embeds))
        self.assertFalse(any("First observed" in embed["description"] or "Observed listing-price drop" in embed["description"] for embed in embeds))
        for message in messages:
            self.assertEqual(message["avatar_url"], watcher.AVATAR_URL)
            self.assertEqual(message["allowed_mentions"], {"parse": []})
        offers = [embed for embed in embeds if "Minecoins Pack" in embed["title"]]
        self.assertIn("Indonesia activation listed", offers[0]["fields"][0]["value"])
        self.assertIn("Germany alternative only", offers[1]["fields"][0]["value"])
        self.assertIn("checkout", offers[0]["fields"][2]["value"])

    def test_overview_partial_failure_checkpoints_only_safe_delivery_ids_not_normal_history(self):
        data = snapshot([offer(slug=f"sku-{index}", cents=500 + index) for index in range(14)])
        for index, current in enumerate(data["offers"]):
            current["coins"] = 1000 + index
        normal_state = watcher.empty_state()
        pending = watcher.plan_events(data, normal_state)
        calls, acknowledged = [], []
        def post(url, message):
            calls.append(message)
            if len(calls) == 2:
                raise watcher.SourceError("simulated refresh delivery failure")
            return {"message_id": "123", "channel_id": "456"}
        with self.assertRaises(watcher.SourceError):
            watcher.run_overview(data, webhook="https://discord.invalid/secret", post=post, checkpoint=acknowledged.append)
        self.assertEqual(acknowledged, [{"message_id": "123", "channel_id": "456"}])
        self.assertEqual(normal_state, watcher.empty_state())
        self.assertEqual(watcher.plan_events(data, normal_state), pending)

    def test_announce_current_dry_run_does_not_read_or_change_even_invalid_normal_history(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture_file = Path(directory) / "fixture.json"
            state_file = Path(directory) / "state.json"
            fixture_file.write_text(json.dumps(snapshot([offer()])), encoding="utf8")
            original = "normal-history-must-not-be-read-or-reset"
            state_file.write_text(original, encoding="utf8")
            env = dict(os.environ, DRY_RUN="true", ANNOUNCE_CURRENT="true", ENEBA_STATE_FILE=str(state_file))
            env.pop("DISCORD_WEBHOOK_URL", None)
            result = subprocess.run([sys.executable, str(Path(watcher.__file__).resolve()), "--fixture", str(fixture_file)], env=env, capture_output=True, text=True, check=True)
            preview = json.loads(result.stdout)
            self.assertTrue(preview["dry_run"])
            self.assertTrue(preview["announce_current"])
            self.assertIn("Current overview — requested refresh", preview["messages"][0]["content"])
            self.assertEqual(state_file.read_text(encoding="utf8"), original)

    def test_requested_overview_retains_source_failure_status_without_using_stale_history(self):
        data = snapshot(errors=[{"source": "Minecoin search", "error": "public page HTTP 403; no offers inferred"}])
        output = io.StringIO()
        with patch.dict(os.environ, {"ANNOUNCE_CURRENT": "true", "DRY_RUN": "true"}, clear=True), \
             patch.object(watcher, "collect", return_value=data), \
             patch.object(watcher, "read_state", side_effect=AssertionError("overview read normal history")), \
             patch.object(watcher, "save_state", side_effect=AssertionError("overview changed normal history")), \
             redirect_stdout(output):
            self.assertEqual(watcher.main([]), 1)
        result = json.loads(output.getvalue())
        self.assertTrue(result["announce_current"])
        self.assertEqual(result["source_errors"], data["errors"])
        embed = result["messages"][0]["embeds"][0]
        self.assertIn("source unavailable", embed["title"])
        self.assertIn("not reused as current", embed["fields"][0]["value"])

    def test_announce_current_defaults_false(self):
        output = io.StringIO()
        with patch.dict(os.environ, {"DRY_RUN": "true"}, clear=True), \
             patch.object(watcher, "collect", return_value=snapshot([offer()])), \
             patch.object(watcher, "read_state", return_value=watcher.empty_state()) as read_history, \
             patch.object(watcher, "run_overview", side_effect=AssertionError("unrequested overview")), \
             redirect_stdout(output):
            self.assertEqual(watcher.main([]), 0)
        read_history.assert_called_once()
        result = json.loads(output.getvalue())
        self.assertNotIn("announce_current", result)

    def test_partial_discord_failure_saves_successful_batches_and_resumes_remaining(self):
        data = snapshot([offer(slug=f"sku-{index}", cents=500 + index) for index in range(14)])
        # Give each SKU a distinct denomination, so all are relevant comparison groups.
        for index, current in enumerate(data["offers"]):
            current["coins"] = 1000 + index
        state = watcher.empty_state()
        saved, calls = [], []
        def post(url, message):
            calls.append(message)
            if len(calls) == 2:
                raise watcher.SourceError("simulated delivery failure")
            return {"message_id": "123", "channel_id": "456"}
        with self.assertRaises(watcher.SourceError):
            watcher.run(data, state, webhook="https://discord.invalid/secret", save=lambda value: saved.append(copy.deepcopy(value)), post=post, now=NOW)
        self.assertTrue(saved)
        delivered = len(calls[0]["embeds"])
        self.assertEqual(len(saved[-1]["notified"]), delivered)
        remaining = watcher.plan_events(data, saved[-1])
        self.assertEqual(len(remaining), 14 - delivered)
        self.assertEqual(state, watcher.empty_state())
        for message in calls:
            self.assertLessEqual(len(message["embeds"]), 10)
            self.assertLessEqual(sum(watcher.embed_size(embed) for embed in message["embeds"]), 6000)

    def test_failed_public_source_is_explicit_and_previous_prices_are_preserved(self):
        search, details = fixture(COMPATIBLE, 581, True)
        def fetcher(url):
            if "/store/" in url:
                return html(search)
            if COMPATIBLE in url:
                return html(details)
            raise watcher.SourceError("public page HTTP 403; no offers inferred")
        data = watcher.collect(fetcher=fetcher, now=NOW)
        self.assertEqual(len(data["offers"]), 1)
        self.assertEqual(len(data["errors"]), 1)
        self.assertIn("403", data["errors"][0]["error"])
        state = watcher.empty_state()
        state["prices"]["old-sku"] = {"price_minor": 999, "currency": "EUR", "at": watcher.stamp(NOW)}
        saved = []
        watcher.run(data, state, webhook="https://discord.invalid/secret", post=lambda *args: {"message_id": "1", "channel_id": "2"}, save=lambda value: saved.append(copy.deepcopy(value)), now=NOW)
        self.assertEqual(saved[-1]["prices"]["old-sku"]["price_minor"], 999)
        self.assertFalse(any("failure" in event for event in watcher.plan_events(data, saved[-1])))

    def test_discord_rate_limit_is_honored_and_only_safe_acknowledgment_ids_are_returned(self):
        class Response(io.BytesIO):
            def __enter__(self):
                return self
            def __exit__(self, *args):
                self.close()
        requests, waits = [], []
        def opener(request, **kwargs):
            requests.append(request)
            if len(requests) == 1:
                raise HTTPError("https://discord.invalid", 429, "rate limited", {}, io.BytesIO(b'{"retry_after":0.5}'))
            return Response(b'{"id":"123","channel_id":"456","token":"must-not-be-returned"}')
        result = watcher.post_discord("https://discord.invalid/webhook?thread_id=789", {"embeds": []}, opener=opener, sleeper=waits.append)
        self.assertEqual(result, {"message_id": "123", "channel_id": "456"})
        self.assertEqual(waits, [0.5])
        self.assertIn("wait=true", requests[-1].full_url)
        self.assertIn("thread_id=789", requests[-1].full_url)

    def test_corrupt_history_is_not_reset_and_atomic_save_round_trips(self):
        with tempfile.TemporaryDirectory() as directory:
            file = Path(directory) / "state.json"
            self.assertEqual(watcher.read_state(file), watcher.empty_state())
            watcher.save_state(watcher.empty_state(), file)
            self.assertEqual(watcher.read_state(file), watcher.empty_state())
            file.write_text("{broken", encoding="utf8")
            with self.assertRaises(ValueError):
                watcher.read_state(file)

    def test_offline_cli_needs_no_webhook_and_cannot_rewrite_saved_history(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture_file = Path(directory) / "fixture.json"
            state_file = Path(directory) / "state.json"
            fixture_file.write_text(json.dumps(snapshot([offer()])), encoding="utf8")
            original = json.dumps(watcher.empty_state())
            state_file.write_text(original, encoding="utf8")
            env = dict(os.environ, DRY_RUN="true", ENEBA_STATE_FILE=str(state_file))
            env.pop("DISCORD_WEBHOOK_URL", None)
            result = subprocess.run([sys.executable, str(Path(watcher.__file__).resolve()), "--fixture", str(fixture_file)], env=env, capture_output=True, text=True, check=True)
            self.assertTrue(json.loads(result.stdout)["dry_run"])
            self.assertEqual(state_file.read_text(encoding="utf8"), original)

    def test_retention_normal_and_overview_register_same_comparable_key_without_price_reset(self):
        current = offer()
        data = snapshot([current])
        state = watcher.empty_state()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "posts.json"
            watcher.run(data, state, webhook="https://discord.invalid/hook", now=NOW,
                        post=lambda *args: {"message_id": "123", "channel_id": "456"}, journal_path=path)
            watcher.run_overview(data, webhook="https://discord.invalid/hook", now=NOW,
                                 post=lambda *args: {"message_id": "124", "channel_id": "456"}, journal_path=path)
            records = json.loads(path.read_text(encoding='utf-8'))["messages"]
        self.assertEqual(records["123"]["items"], records["124"]["items"])
        self.assertEqual(records["123"]["items"], [{"key": "offer:" + watcher.price_key(current), "scope": "eneba-minecoins:ID:EUR"}])
        self.assertEqual(records["124"]["purpose"], "overview")
        self.assertEqual(state, watcher.empty_state())

    def test_retention_complete_collection_and_partial_failures_have_independent_coverage(self):
        search, details = fixture(COMPATIBLE, 581, True)
        def fetcher(url):
            return html(search if "/store/" in url else details if COMPATIBLE in url else homepage())
        current = watcher.collect(fetcher=fetcher, now=NOW)
        self.assertTrue(current["retention_coverage"]["eneba-campaigns:ID"])
        self.assertTrue(current["retention_coverage"]["eneba-minecoins:ID:EUR"])
        def failed_product(url):
            if COMPATIBLE in url:
                raise watcher.SourceError("blocked product activation page")
            return fetcher(url)
        partial = watcher.collect(fetcher=failed_product, now=NOW)
        self.assertTrue(partial["retention_coverage"]["eneba-campaigns:ID"])
        self.assertFalse(partial["retention_coverage"]["eneba-minecoins:ID:EUR"])
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "current.json"
            watcher.export_retention_snapshot(current, NOW, path)
            exported = json.loads(path.read_text(encoding='utf-8'))
        price = exported["items"]["offer:" + watcher.price_key(current["offers"][0])]["embed"]
        self.assertIn("Current price — refreshed after 60 days", price["description"])
        self.assertNotIn("First observed", price["description"])
        self.assertNotIn("Observed listing-price drop", price["description"])

    def test_retention_dry_run_does_not_read_or_write_journal(self):
        data = snapshot([offer()])
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "posts.json"
            path.write_text("keep byte for byte")
            with patch.dict(os.environ, {"DISCORD_RETENTION_FILE": str(path)}):
                watcher.run(data, watcher.empty_state(), dry_run=True)
                watcher.run_overview(data, dry_run=True)
            self.assertEqual(path.read_text(encoding='utf-8'), "keep byte for byte")


if __name__ == "__main__":
    unittest.main()
