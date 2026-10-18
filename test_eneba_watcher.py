"""Public Apollo fixtures match captured Eneba Minecoin pages; no network or secrets."""
import copy
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
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


if __name__ == "__main__":
    unittest.main()
