import contextlib
import io
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.error import HTTPError

import pokemon_watcher as watcher


def deku_fixture(item_id="pokemon-violet", country="DE", used=True, *, item_name=None, url=None):
    item_name = item_name or watcher.ITEMS[item_id]["title"]
    merchant = "amazon_de" if country == "DE" else "amazon"
    currency = "EUR" if country == "DE" else "USD"
    url = url or "https://www.amazon.de/Pokemon-Violet/dp/B09TTZZWW6?tag=affiliate"
    analytics = {"currency": currency, "value": 4534, "items": [{
        "item_id": item_id, "item_name": item_name, "affiliation": merchant,
        "item_variant": "digital" if watcher.ITEMS[item_id]["kind"] == "DLC" else "physical"}]}
    return f'''<meta property="og:title" content="{watcher.ITEMS[item_id]['title']} | Deku Deals">
        <a id="navbarCountry1"><span>🌏</span>{country}</a>
        <table><script>outAnalytics['{merchant}:id'] = {json.dumps(analytics)}</script>
        <tr><td><a data-out-analytics-id="{merchant}:id" href="{url}">Physical EUR45.34</a></td></tr>
        {f'<tr><td><a data-out-analytics-id="{merchant}:id" href="{url}">Used: €32,12</a></td></tr>' if used else ''}
        </table>'''


def playasia_fixture(item_id="pokemon-violet", stock="In our warehouse"):
    name = "Pokemon Violet (English)" if item_id == "pokemon-violet" else "Pocket Monsters Sword (Multi-Language)"
    return f'''<h1>{name}</h1><div>Japan</div><div>Get Shipping Quote</div>
        <div>{stock}</div><b>US$37.99</b><div>Our previous price: US$59.99</div>
        <div>Add to Cart</div><h3>Product Languages</h3><p>Japanese, English, French</p>
        <h3>Specs</h3><p>Edition Standard</p>'''


def pse_fixture(title="Switch Pokemon Violet (Cero/English)", path="switch-pokemon-violet-cero-english", sale=True, stock=True):
    price = ('<span class="price"><del><span>Rp720 000</span></del>'
             '<ins><span>Rp699 000</span></ins></span>') if sale else '<span class="price">Rp699 000</span>'
    return f'''<div class="product-small col type-product {'instock' if stock else 'outofstock'} purchasable">
        <div><a class="woocommerce-LoopProduct-link woocommerce-loop-product__link"
        href="https://www.psegameshop.com/product/{path}/">{title}</a>{price}
        <a class="add_to_cart_button" data-product_id="127888">Keranjangin!</a></div></div>'''


def offer(**changes):
    value = {"item_id": "pokemon-violet", "title": "Pokémon Violet", "kind": "base game",
        "region": "DE", "currency": "EUR", "format": "physical", "condition": "new", "price": 4534,
        "merchant": "Amazon Germany", "url": "https://www.amazon.de/dp/B09TTZZWW6",
        "source": "https://www.dekudeals.com/items/pokemon-violet?country=de",
        "image": watcher.ITEMS["pokemon-violet"]["cover"], "stock": "listed",
        "stock_details": "Listed by tracker; retailer stock unverified.",
        "delivery": "Destination delivery and fees unverified.", "requires": ""}
    return {**value, **changes}


class ParserTests(unittest.TestCase):
    def test_deku_separates_new_and_used_and_removes_affiliate_tag(self):
        found = watcher.parse_deku(deku_fixture(), "pokemon-violet", "DE")
        self.assertEqual([(o["condition"], o["price"]) for o in found], [("new", 4534), ("used", 3212)])
        self.assertNotEqual(watcher.offer_key(found[0]), watcher.offer_key(found[1]))
        self.assertNotIn("tag=", found[0]["url"])
        self.assertIn("seller unverified", found[1]["merchant"])

    def test_deku_wrong_locale_or_game_raises(self):
        with self.assertRaises(ValueError):
            watcher.parse_deku(deku_fixture(country="US"), "pokemon-violet", "DE")
        with self.assertRaises(ValueError):
            watcher.parse_deku(deku_fixture().replace("Pokémon Violet |", "Pokémon Scarlet |"), "pokemon-violet", "DE")

    def test_deku_mismatched_dlc_or_account_offer_does_not_qualify(self):
        for wrong in ("Pokémon Violet Account", "Pokémon Violet: The Hidden Treasure of Area Zero", "Pokémon Scarlet"):
            with self.subTest(wrong=wrong), self.assertRaises(ValueError):
                watcher.parse_deku(deku_fixture(item_name=wrong), "pokemon-violet", "DE")

    def test_deku_untrusted_product_host_does_not_qualify(self):
        with self.assertRaises(ValueError):
            watcher.parse_deku(deku_fixture(url="https://amazon.de.evil.example/game"), "pokemon-violet", "DE")

    def test_dlc_keeps_exact_version_and_requirement(self):
        for item_id in ("pokmon-sword-expansion-pass", "pokemon-violet-the-hidden-treasure-of-area-zero"):
            with self.subTest(item_id=item_id):
                found = watcher.parse_deku(deku_fixture(item_id, used=False), item_id, "DE")
                self.assertEqual(len(found), 1)
                self.assertEqual(found[0]["format"], "digital")
                self.assertEqual(found[0]["kind"], "DLC")
                self.assertIn("base game required", found[0]["requires"])
                self.assertIn("Does not work with", found[0]["requires"])

    def test_playasia_in_stock_and_backorder_are_distinct(self):
        violet = watcher.parse_playasia(playasia_fixture(), "pokemon-violet")[0]
        sword = watcher.parse_playasia(playasia_fixture("pokemon-sword", "Expected stock arrival on10/20/26."), "pokemon-sword")[0]
        self.assertEqual(violet["price"], 3799)
        self.assertEqual(violet["stock"], "in_stock")
        self.assertEqual(sword["stock"], "backorder")
        self.assertIn("10/20/26", sword["stock_details"])
        self.assertIn("English", sword["delivery"])

    def test_playasia_wrong_edition_and_block_page_rejected(self):
        with self.assertRaises(ValueError):
            watcher.parse_playasia(playasia_fixture("pokemon-sword"), "pokemon-violet")
        with self.assertRaises(ValueError):
            watcher.parse_playasia("Cloudflare - please wait", "pokemon-violet")

    def test_pse_uses_current_sale_price_not_struck_out_original(self):
        found = watcher.parse_pse(pse_fixture())
        self.assertEqual(found[0]["currency"], "IDR")
        self.assertEqual(found[0]["price"], 69900000)
        self.assertEqual(watcher.price_text(found[0]["price"], "IDR"), "IDR 699,000")
        self.assertEqual(found[0]["stock"], "listed_in_stock")
        self.assertIn("checkout stock unverified", found[0]["stock_details"])

    def test_pse_steelcase_cards_accounts_and_dlc_are_rejected(self):
        for title in ("Switch Pokemon Sword Steelcase", "Pokemon Violet Account", "Pokemon Card Violet", "Pokemon Violet DLC"):
            with self.subTest(title=title), self.assertRaises(ValueError):
                watcher.parse_pse(pse_fixture(title=title))

    def test_gather_partial_failure_keeps_supported_sources(self):
        def fetch(url):
            if "psegameshop" in url:
                return pse_fixture()
            if "play-asia" in url:
                raise HTTPError(url, 403, "Forbidden", {}, None)
            item_id = url.split("/items/")[1].split("?")[0]
            return deku_fixture(item_id, used=False)
        found, errors = watcher.gather_offers(("DE",), fetch)
        self.assertEqual(len(found), 5)
        self.assertEqual(len(errors), 2)
        self.assertTrue(any(o["region"] == "ID" for o in found))
        self.assertTrue(all("HTTP 403" in error for error in errors))

    def test_all_source_failures_raise(self):
        with self.assertRaisesRegex(RuntimeError, "All Pokemon"):
            watcher.gather_offers(fetch=Mock(side_effect=OSError("offline")))


class AlertTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "state.json"
        self.post = Mock(return_value={"message_id": "123", "channel_id": "456"})
        self.now = datetime(2026, 10, 18, tzinfo=timezone.utc)

    def run_prices(self, offers, **kwargs):
        with contextlib.redirect_stdout(io.StringIO()):
            return watcher.run(offers, self.path, post=self.post, webhook="https://discord.example/secret", now=self.now, **kwargs)

    def read_state(self):
        return json.loads(self.path.read_text(encoding="utf-8"))

    def test_initial_baseline_has_image_and_is_not_called_discount(self):
        self.run_prices([offer()])
        payload = self.post.call_args.args[1]
        embed = payload["embeds"][0]
        self.assertIn("First observed price — discount not established", embed["description"])
        self.assertEqual(embed["image"]["url"], offer()["image"])
        self.assertIn("unverified", embed["description"])
        self.assertEqual(payload["allowed_mentions"], {"parse": []})
        self.assertEqual(payload["avatar_url"], watcher.AVATAR_URL)
        self.assertEqual(self.read_state()["pending"], [])

    def test_unchanged_or_increased_price_does_not_repeat(self):
        self.run_prices([offer()])
        self.post.reset_mock()
        self.run_prices([offer()])
        self.run_prices([offer(price=5000)])
        self.post.assert_not_called()

    def test_price_drop_and_availability_change_notify(self):
        self.run_prices([offer()])
        self.post.reset_mock()
        self.run_prices([offer(price=4000)])
        self.assertIn("New lowest observed by this watcher", self.post.call_args.args[1]["embeds"][0]["description"])
        self.run_prices([offer(price=4000, stock="unavailable", stock_details="Unavailable")])
        self.assertIn("Availability changed", self.post.call_args.args[1]["embeds"][0]["description"])

    def test_drop_above_own_record_is_not_called_new_lowest(self):
        self.run_prices([offer(price=4000)])
        self.run_prices([offer(price=5000)])
        self.run_prices([offer(price=4500)])
        description = self.post.call_args.args[1]["embeds"][0]["description"]
        self.assertIn("Price dropped since the previous check", description)
        self.assertNotIn("New lowest", description)

    def test_dry_run_never_sends_or_writes_new_or_existing_state(self):
        self.run_prices([offer()], dry_run=True)
        self.assertFalse(self.path.exists())
        self.run_prices([offer()])
        saved = self.path.read_bytes()
        self.post.reset_mock()
        self.run_prices([offer(price=1000)], dry_run=True)
        self.post.assert_not_called()
        self.assertEqual(self.path.read_bytes(), saved)

    def test_failed_webhook_retains_pending_without_acknowledging(self):
        self.post.side_effect = RuntimeError("HTTP 500")
        with self.assertRaises(RuntimeError):
            self.run_prices([offer()])
        self.assertEqual(len(self.read_state()["pending"]), 1)
        self.post.side_effect = None
        self.run_prices([offer()])
        self.assertEqual(self.read_state()["pending"], [])

    def test_pending_missing_source_is_held_until_fresh_observation(self):
        self.post.side_effect = RuntimeError("HTTP 500")
        with self.assertRaises(RuntimeError):
            self.run_prices([offer()])
        self.post.side_effect = None
        self.post.reset_mock()
        self.run_prices([])
        self.post.assert_not_called()
        self.assertEqual(len(self.read_state()["pending"]), 1)
        self.run_prices([offer(price=4400)])
        self.assertIn("EUR 44.00", self.post.call_args.args[1]["embeds"][0]["description"])

    def test_queued_drop_reverted_before_delivery_is_not_sent(self):
        self.run_prices([offer(price=1000)])
        self.post.side_effect = RuntimeError("HTTP 500")
        with self.assertRaises(RuntimeError):
            self.run_prices([offer(price=800)])
        self.post.side_effect = None
        self.post.reset_mock()
        self.run_prices([offer(price=1200)])
        self.post.assert_not_called()
        self.assertEqual(self.read_state()["pending"], [])

    def test_queued_availability_survives_reverted_price_drop(self):
        self.run_prices([offer(price=1000, stock="backorder")])
        self.post.side_effect = RuntimeError("HTTP 500")
        with self.assertRaises(RuntimeError):
            self.run_prices([offer(price=800, stock="in_stock")])
        self.post.side_effect = None
        self.run_prices([offer(price=1200, stock="in_stock")])
        description = self.post.call_args.args[1]["embeds"][0]["description"]
        self.assertIn("Availability changed", description)
        self.assertNotIn("Price dropped", description)
        self.assertNotIn("New lowest", description)

    def test_partial_batches_checkpoint_only_successful_deliveries(self):
        offers = [offer(region=f"test-{i}") for i in range(20)]
        events = watcher.prepare_events({}, offers, self.now.isoformat())["pending"]
        batches = watcher.make_batches(events)
        first_count = len(batches[0][0])
        self.post.side_effect = [{"message_id": "123", "channel_id": "456"}, RuntimeError("HTTP 500")]
        with self.assertRaises(RuntimeError):
            self.run_prices(offers)
        self.assertEqual(len(self.read_state()["pending"]), 20 - first_count)
        self.post.side_effect = None
        self.post.reset_mock()
        self.run_prices(offers)
        self.assertEqual(sum(len(call.args[1]["embeds"]) for call in self.post.call_args_list), 20 - first_count)
        self.assertEqual(self.read_state()["pending"], [])

    def test_batching_preserves_all_events_and_respects_discord_limits(self):
        events = watcher.prepare_events({}, [offer(region=f"test-{i}") for i in range(35)], self.now.isoformat())["pending"]
        batches = watcher.make_batches(events)
        self.assertEqual(sum(len(ids) for ids, _ in batches), 35)
        self.assertEqual(len(set(event_id for ids, _ in batches for event_id in ids)), 35)
        for _, payload in batches:
            self.assertEqual(payload["avatar_url"], watcher.AVATAR_URL)
            self.assertLessEqual(len(payload["embeds"]), 10)
            self.assertLessEqual(sum(len(json.dumps(embed, ensure_ascii=False)) for embed in payload["embeds"]), 5500)
            self.assertEqual(len({embed["url"] for embed in payload["embeds"]}), len(payload["embeds"]))

    def test_new_and_used_cards_sharing_product_url_are_sent_in_separate_messages(self):
        current = [offer(condition="new", price=4500), offer(condition="used", price=3200)]
        events = watcher.prepare_events({}, current, self.now.isoformat())["pending"]
        batches = watcher.make_batches(events)
        self.assertEqual(len(batches), 2)
        self.assertEqual([event_id for ids, _ in batches for event_id in ids], [event["id"] for event in events])
        embeds = [embed for _, payload in batches for embed in payload["embeds"]]
        self.assertEqual([embed["url"] for embed in embeds], [offer()["url"], offer()["url"]])
        self.assertIn("physical · new", embeds[0]["description"])
        self.assertIn("physical · used", embeds[1]["description"])

    def test_unknown_or_mismatched_account_labels_digital_reference(self):
        event = watcher.prepare_events({}, [offer(format="digital")], self.now.isoformat())["pending"][0]
        for country in ("", "US"):
            with self.subTest(country=country):
                description = watcher.event_embed(event, country)["description"]
                self.assertIn("reference", description)
                self.assertIn("NOT confirmed", description)

    def test_dlc_cards_explicitly_require_correct_base_game(self):
        for item_id in ("pokmon-sword-expansion-pass", "pokemon-violet-the-hidden-treasure-of-area-zero"):
            meta = watcher.ITEMS[item_id]
            current = offer(item_id=item_id, title=meta["title"], kind="DLC", format="digital", requires=meta["requires"])
            event = watcher.prepare_events({}, [current], self.now.isoformat())["pending"][0]
            description = watcher.event_embed(event)["description"]
            self.assertIn("base game required", description)
            self.assertIn("Does not work with", description)

    def test_invalid_acknowledgement_does_not_remove_pending(self):
        self.post.return_value = {"message_id": "123"}
        with self.assertRaisesRegex(RuntimeError, "acknowledgement"):
            self.run_prices([offer()])
        self.assertEqual(len(self.read_state()["pending"]), 1)

    def test_discord_uses_wait_true_and_returns_only_ids(self):
        response = Mock()
        response.status = 200
        response.read.return_value = b'{"id":"123","channel_id":"456","content":"not logged"}'
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        with patch.object(watcher, "urlopen", return_value=response) as fetch:
            result = watcher.send_discord("https://discord.example/hook?wait=false", {"content": "test"})
        self.assertEqual(result, {"message_id": "123", "channel_id": "456"})
        self.assertIn("wait=true", fetch.call_args.args[0].full_url)
        self.assertNotIn("wait=false", fetch.call_args.args[0].full_url)

    def test_requested_overview_sends_fresh_prices_without_creating_history(self):
        self.run_prices([offer(price=4200)], announce_current=True)
        self.assertFalse(self.path.exists())
        payload = self.post.call_args.args[1]
        self.assertEqual(payload["avatar_url"], watcher.AVATAR_URL)
        self.assertIn("requested refresh", payload["content"])
        description = payload["embeds"][0]["description"]
        self.assertIn("Current overview — requested refresh", description)
        self.assertIn("EUR 42.00", description)
        self.assertNotIn("First observed", description)
        self.assertNotIn("lowest observed", description)
        self.assertNotIn("Price dropped", description)

    def test_overview_preserves_existing_history_and_pending_events_exactly(self):
        self.post.side_effect = RuntimeError("HTTP 500")
        with self.assertRaises(RuntimeError):
            self.run_prices([offer(price=4500)])
        original = self.path.read_bytes()
        self.post.side_effect = None
        self.run_prices([offer(price=4200)], announce_current=True)
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(len(self.read_state()["pending"]), 1)

    def test_overview_dry_run_never_posts_reads_or_writes_history(self):
        self.path.write_text("not valid json; overview must not read this", encoding="utf-8")
        original = self.path.read_bytes()
        self.run_prices([offer(format="digital")], announce_current=True, dry_run=True)
        self.post.assert_not_called()
        self.assertEqual(self.path.read_bytes(), original)

    def test_failed_overview_delivery_does_not_change_history(self):
        self.path.write_text('{"pending": ["keep exactly"]}\n', encoding="utf-8")
        original = self.path.read_bytes()
        self.post.side_effect = RuntimeError("HTTP 500")
        with self.assertRaises(RuntimeError):
            self.run_prices([offer()], announce_current=True)
        self.assertEqual(self.path.read_bytes(), original)

    def test_overview_keeps_digital_eligibility_dlc_requirement_and_coverage(self):
        item_id = "pokmon-sword-expansion-pass"
        current = offer(item_id=item_id, title=watcher.ITEMS[item_id]["title"], kind="DLC", format="digital",
                        requires=watcher.ITEMS[item_id]["requires"])
        self.run_prices([current], announce_current=True, coverage="Coverage: DE tracker only.")
        embed = self.post.call_args.args[1]["embeds"][0]
        self.assertIn("NOT confirmed", embed["description"])
        self.assertIn("Pokémon Sword base game required", embed["description"])
        self.assertIn("Coverage: DE tracker only", embed["footer"]["text"])


class HealthTests(unittest.TestCase):
    def test_retention_snapshot_keeps_healthy_item_country_scopes_when_others_fail(self):
        errors = ["deku/pokemon-sword/DE: ValueError", "playasia/pokemon-violet/JP import: HTTP 403",
                  "pse/pokemon-violet/ID: HTTP 403"]
        now = datetime.fromisoformat("2026-10-18T01:00:00+00:00")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "current.json"
            watcher.export_retention_snapshot([offer()], errors, ("DE", "US"), now, path)
            snapshot = json.loads(path.read_text(encoding='utf-8'))
        self.assertTrue(snapshot["coverage"]["deku/pokemon-violet/DE"])
        self.assertTrue(snapshot["coverage"]["deku/pokemon-sword/US"])
        self.assertFalse(snapshot["coverage"]["deku/pokemon-sword/DE"])
        self.assertFalse(snapshot["coverage"]["playasia/pokemon-violet/JP import"])
        self.assertFalse(snapshot["coverage"]["pse/pokemon-violet/ID"])
        embed = snapshot["items"][watcher.offer_key(offer())]["embed"]
        self.assertIn("Current price — refreshed after 60 days", embed["description"])
        self.assertNotIn("lowest observed", embed["description"])

    def test_retention_unavailable_is_inactive_but_unknown_retries(self):
        now = datetime.fromisoformat("2026-10-18T01:00:00+00:00")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "current.json"
            watcher.export_retention_snapshot([offer(stock="unavailable")], [], ("DE", "US"), now, path)
            unavailable = json.loads(path.read_text(encoding='utf-8'))
            self.assertTrue(unavailable["coverage"]["deku/pokemon-violet/DE"])
            self.assertEqual(unavailable["items"], {})
            watcher.export_retention_snapshot([offer(stock="unknown")], [], ("DE", "US"), now, path)
            unknown = json.loads(path.read_text(encoding='utf-8'))
            self.assertFalse(unknown["coverage"]["deku/pokemon-violet/DE"])
            self.assertEqual(unknown["items"], {})

    def test_retention_journals_normal_and_manual_overview_without_corrupting_price_history(self):
        now = datetime.fromisoformat("2026-10-18T01:00:00+00:00")
        with tempfile.TemporaryDirectory() as directory, contextlib.redirect_stdout(io.StringIO()):
            state = Path(directory) / "state.json"
            journal = Path(directory) / "posts.json"
            watcher.run([offer()], state, webhook="https://discord.invalid/hook", now=now, journal_path=journal,
                        post=lambda *args: {"message_id": "123", "channel_id": "456"})
            original = state.read_bytes()
            watcher.run([offer(price=4000)], state, webhook="https://discord.invalid/hook", now=now, journal_path=journal,
                        announce_current=True, post=lambda *args: {"message_id": "124", "channel_id": "456"})
            records = json.loads(journal.read_text(encoding='utf-8'))["messages"]
            self.assertEqual(state.read_bytes(), original)
        self.assertEqual(records["123"]["items"], records["124"]["items"])
        self.assertEqual(records["124"]["purpose"], "overview")

    def test_retention_dry_run_never_reads_or_changes_journal(self):
        with tempfile.TemporaryDirectory() as directory, contextlib.redirect_stdout(io.StringIO()):
            state = Path(directory) / "state.json"
            journal = Path(directory) / "posts.json"
            journal.write_text("keep exactly")
            for overview in (False, True):
                watcher.run([offer()], state, dry_run=True, announce_current=overview, journal_path=journal)
            self.assertEqual(journal.read_text(encoding='utf-8'), "keep exactly")
            self.assertFalse(state.exists())

    def test_main_parses_announce_current_as_explicit_boolean(self):
        for value, expected in (("true", True), ("false", False)):
            with self.subTest(value=value), \
                    patch.dict(watcher.os.environ, {"ANNOUNCE_CURRENT": value, "DRY_RUN": "true", "GITHUB_STEP_SUMMARY": ""}), \
                    patch.object(watcher, "gather_offers", return_value=([offer()], [])), \
                    patch.object(watcher, "run") as run, contextlib.redirect_stdout(io.StringIO()):
                watcher.main()
                self.assertIs(run.call_args.kwargs["announce_current"], expected)

    def test_optional_source_failure_is_visible_and_does_not_fail_run(self):
        errors = ["playasia/pokemon-sword/JP import: HTTP 403", "pse/pokemon-violet/ID: HTTP 403"]
        with patch.dict(watcher.os.environ, {"DRY_RUN": "true", "GITHUB_STEP_SUMMARY": ""}), \
                patch.object(watcher, "gather_offers", return_value=([offer()], errors)), \
                patch.object(watcher, "run") as run, contextlib.redirect_stdout(io.StringIO()) as output:
            watcher.main()
        self.assertIn("Optional source unavailable (excluded)", output.getvalue())
        coverage = run.call_args.kwargs["coverage"]
        self.assertIn("unavailable and excluded", coverage)
        event = watcher.prepare_events({}, [offer()], "2026-10-18T00:00:00Z")["pending"][0]
        self.assertIn(coverage, watcher.event_embed(event, coverage=coverage)["footer"]["text"])

    def test_required_failure_processes_valid_observations_then_fails(self):
        errors = ["deku/pokemon-sword/DE: ValueError"]
        with patch.dict(watcher.os.environ, {"DRY_RUN": "true", "GITHUB_STEP_SUMMARY": ""}), \
                patch.object(watcher, "gather_offers", return_value=([offer()], errors)), \
                patch.object(watcher, "run") as run, contextlib.redirect_stdout(io.StringIO()), \
                self.assertRaisesRegex(RuntimeError, "required Deku"):
            watcher.main()
        run.assert_called_once()
        self.assertIn("incomplete", run.call_args.kwargs["coverage"])

    def test_github_summary_discloses_unknown_region_and_source_gap(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "summary.md"
            with patch.dict(watcher.os.environ, {"DRY_RUN": "true", "NINTENDO_ACCOUNT_COUNTRY": "", "GITHUB_STEP_SUMMARY": str(path)}), \
                    patch.object(watcher, "gather_offers", return_value=([offer()], ["pse/pokemon-violet/ID: HTTP 403"])), \
                    patch.object(watcher, "run"), contextlib.redirect_stdout(io.StringIO()):
                watcher.main()
            summary = path.read_text(encoding="utf-8")
            self.assertIn("regional references only", summary)
            self.assertIn("Optional; excluded: pse/", summary)


if __name__ == "__main__":
    unittest.main()
