import copy
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import discord_retention as retention


class FakeDiscord:
    def __init__(self):
        self.calls = []
        self.messages = {"101": {"id": "101", "channel_id": "900", "webhook_id": "700", "pinned": False}}
        self.next_id = 201
        self.post_failure_at = None
        self.delete_failures = 0
        self.posts = 0
        self.identity = {"id": "700", "channel_id": "900"}

    def __call__(self, method, url, payload=None):
        self.calls.append((method, url, copy.deepcopy(payload)))
        if method == "GET" and "/messages/" not in url:
            return 200, self.identity
        message_id = url.rsplit("/", 1)[-1]
        if method == "GET":
            return (200, copy.deepcopy(self.messages[message_id])) if message_id in self.messages else (404, None)
        if method == "POST":
            self.posts += 1
            if self.posts == self.post_failure_at:
                return 500, None
            ack = {"id": str(self.next_id), "channel_id": "900", "webhook_id": "700", "pinned": False}
            self.next_id += 1
            self.messages[ack["id"]] = ack
            return 200, ack
        if method == "DELETE":
            if self.delete_failures:
                self.delete_failures -= 1
                return 500, None
            self.messages.pop(message_id, None)
            return 204, None
        raise AssertionError(method)


class DiscordRetentionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.journal = Path(self.temp.name) / ".discord-posts-eneba.json"
        self.snapshot_path = Path(self.temp.name) / ".discord-snapshot-eneba.json"
        self.now = datetime(2027, 1, 1, 12, tzinfo=timezone.utc)
        self.old = self.now - timedelta(days=60)
        self.fake = FakeDiscord()
        self.snapshot = {
            "version": 1, "destination": "eneba", "observed_at": retention._iso(self.now),
            "identity": {"username": "Eneba Sale Watcher", "avatar_url": "https://example.com/avatar.png", "content": "Verify activation before purchase."},
            "coverage": {"offers": True},
            "items": {"coin": {"scope": "offers", "embed": {"title": "Minecoins", "url": "https://example.com/coin", "description": "EUR 3.10"}}},
        }
        self.env = patch.dict(os.environ, {"RETENTION_ENABLED": "true", "RETENTION_DAYS": "60", "RETENTION_SNAPSHOT_MAX_HOURS": "6"})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.register()
        self.save_snapshot()

    def register(self, message_id="101", items=None, when=None, purpose="alert"):
        retention.record_message(self.journal, {"message_id": message_id, "channel_id": "900"},
                                 items or [{"key": "coin", "scope": "offers"}], now=when or self.old, purpose=purpose)

    def save_snapshot(self):
        retention.write_snapshot(self.snapshot_path, self.snapshot)

    def run_check(self, **kwargs):
        return retention.run_retention("eneba", self.journal, self.snapshot_path,
                                       webhook_url="https://discord.com/api/webhooks/700/SECRET_TOKEN",
                                       now=self.now, request=self.fake, **kwargs)

    def read(self):
        return json.loads(self.journal.read_text(encoding="utf-8"))

    def mutations(self):
        return [call for call in self.fake.calls if call[0] in ("POST", "DELETE")]

    def test_sixty_days_boundary_included_but_one_second_younger_excluded(self):
        self.assertEqual(self.run_check()["retired"], 1)
        self.assertEqual([call[0] for call in self.mutations()], ["POST", "DELETE"])
        self.assertEqual(self.run_check()["eligible"], 0)
        self.journal.unlink()
        self.register(when=self.old + timedelta(seconds=1))
        self.fake.calls.clear()
        self.assertEqual(self.run_check()["eligible"], 0)
        self.assertEqual(self.fake.calls, [])

    def test_active_offer_refresh_is_acknowledged_before_old_deletion(self):
        result = self.run_check()
        self.assertEqual(result["errors"], [])
        self.assertEqual(result["refreshed"], 1)
        self.assertEqual(set(self.read()["messages"]), {"201"})
        post, delete = self.mutations()
        self.assertEqual(post[0], "POST")
        self.assertEqual(delete[0], "DELETE")
        self.assertIn("Still available — refreshed after 60 days", post[2]["embeds"][0]["description"])
        self.assertEqual(post[2]["allowed_mentions"], {"parse": []})
        self.assertIn("?wait=true", post[1])
        self.assertEqual(self.read()["messages"]["201"]["created_at"], "2027-01-01T12:00:00Z")

    def test_ended_offer_deleted_without_replacement(self):
        self.snapshot["items"] = {}
        self.save_snapshot()
        result = self.run_check()
        self.assertEqual([call[0] for call in self.mutations()], ["DELETE"])
        self.assertEqual(result["deleted"], 1)
        self.assertEqual(self.read()["messages"], {})

    def test_pinned_post_is_kept_even_if_source_says_ended(self):
        self.fake.messages["101"]["pinned"] = True
        self.snapshot["items"] = {}
        self.save_snapshot()
        before = self.journal.read_bytes()
        self.assertEqual(self.run_check()["pinned"], 1)
        self.assertEqual(self.mutations(), [])
        self.assertEqual(self.journal.read_bytes(), before)

    def test_unknown_pin_status_is_protected(self):
        del self.fake.messages["101"]["pinned"]
        self.assertEqual(self.run_check()["pinned"], 1)
        self.assertEqual(self.mutations(), [])

    def test_failed_or_missing_scope_never_retires_old_message(self):
        for coverage in ({}, {"offers": False}, {"offers": 1}):
            with self.subTest(coverage=coverage):
                self.snapshot["coverage"] = coverage
                self.save_snapshot()
                before = self.journal.read_bytes()
                self.assertEqual(self.run_check()["deferred"], 1)
                self.assertEqual(self.mutations(), [])
                self.assertEqual(self.journal.read_bytes(), before)

    def test_missing_or_stale_snapshot_defers_all_cleanup(self):
        self.snapshot_path.unlink()
        self.assertTrue(self.run_check()["errors"])
        self.assertEqual(self.fake.calls, [])
        self.snapshot["observed_at"] = retention._iso(self.now - timedelta(hours=6, seconds=1))
        self.save_snapshot()
        self.assertTrue(self.run_check()["errors"])
        self.assertEqual(self.fake.calls, [])

    def test_future_or_wrong_destination_snapshot_rejected(self):
        self.snapshot["observed_at"] = retention._iso(self.now + timedelta(minutes=6))
        self.save_snapshot()
        self.assertTrue(self.run_check()["errors"])
        self.snapshot["observed_at"] = retention._iso(self.now)
        self.snapshot["destination"] = "pokemon"
        self.save_snapshot()
        self.assertTrue(self.run_check()["errors"])
        self.assertEqual(self.fake.calls, [])

    def test_default_off_does_not_read_snapshot_or_contact_discord(self):
        os.environ.pop("RETENTION_ENABLED")
        self.snapshot_path.unlink()
        before = self.journal.read_bytes()
        result = self.run_check()
        self.assertFalse(result["enabled"])
        self.assertEqual(result["errors"], [])
        self.assertEqual(self.fake.calls, [])
        self.assertEqual(self.journal.read_bytes(), before)

    def test_dry_run_never_contacts_discord_or_writes_journal(self):
        os.environ.pop("RETENTION_ENABLED")
        before = self.journal.read_bytes()
        result = self.run_check(dry_run=True)
        self.assertEqual(result["eligible"], 1)
        self.assertTrue(result["dry_run"])
        self.assertEqual(self.fake.calls, [])
        self.assertEqual(self.journal.read_bytes(), before)

    def test_wrong_webhook_channel_fails_before_any_post_or_delete(self):
        self.fake.identity["channel_id"] = "901"
        result = self.run_check()
        self.assertTrue(result["errors"])
        self.assertEqual(self.mutations(), [])
        self.assertEqual(len(self.fake.calls), 1)

    def test_wrong_old_message_channel_fails_before_mutations(self):
        self.fake.messages["101"]["channel_id"] = "901"
        self.assertTrue(self.run_check()["errors"])
        self.assertEqual(self.mutations(), [])

    def test_failed_delete_remembers_replacement_then_retries_without_reposting(self):
        self.fake.delete_failures = 1
        first = self.run_check()
        self.assertEqual(first["refreshed"], 1)
        self.assertTrue(first["errors"])
        self.assertEqual(set(self.read()["messages"]), {"101", "201"})
        self.assertEqual(self.read()["messages"]["101"]["replacement_ids"], ["201"])
        second = self.run_check()
        self.assertEqual(second["refreshed"], 0)
        self.assertEqual(second["deleted"], 1)
        self.assertEqual(self.fake.posts, 1)
        self.assertEqual(set(self.read()["messages"]), {"201"})

    def test_partial_batches_save_ack_and_only_retry_unsent_items(self):
        self.register_more_items(12)
        self.fake.post_failure_at = 2
        first = self.run_check()
        self.assertEqual(first["refreshed"], 1)
        self.assertEqual(first["deleted"], 0)
        self.assertTrue(first["errors"])
        self.assertEqual(len(self.read()["messages"]["201"]["items"]), 10)
        second = self.run_check()
        self.assertEqual(second["refreshed"], 1)
        self.assertEqual(second["deleted"], 1)
        payloads = [call[2] for call in self.fake.calls if call[0] == "POST"]
        self.assertEqual([len(payload["embeds"]) for payload in payloads], [10, 2, 2])
        self.assertEqual(len(self.read()["messages"]), 2)

    def register_more_items(self, number, same_url=False):
        self.journal.unlink()
        items = []
        self.snapshot["items"] = {}
        for index in range(number):
            key = f"offer-{index}"
            items.append({"key": key, "scope": "offers"})
            self.snapshot["items"][key] = {"scope": "offers", "embed": {"title": key, "url": "https://example.com/shared" if same_url else f"https://example.com/{index}"}}
        self.register(items=items)
        self.save_snapshot()

    def test_duplicate_embed_urls_are_sent_in_separate_messages(self):
        self.register_more_items(3, same_url=True)
        result = self.run_check()
        self.assertEqual(result["refreshed"], 3)
        self.assertEqual([len(call[2]["embeds"]) for call in self.fake.calls if call[0] == "POST"], [1, 1, 1])

    def test_embed_character_budget_splits_messages(self):
        self.register_more_items(3)
        for item in self.snapshot["items"].values():
            item["embed"]["description"] = "x" * 2700
        self.save_snapshot()
        self.assertEqual(self.run_check()["refreshed"], 2)
        for call in self.fake.calls:
            if call[0] == "POST":
                self.assertLessEqual(sum(retention._embed_chars(embed) for embed in call[2]["embeds"]), 5500)

    def test_newer_existing_post_supersedes_old_without_duplicate_refresh(self):
        self.register(message_id="102", when=self.now - timedelta(days=1))
        self.fake.messages["102"] = {"id": "102", "channel_id": "900", "webhook_id": "700", "pinned": False}
        result = self.run_check()
        self.assertEqual(result["refreshed"], 0)
        self.assertEqual(result["deleted"], 1)
        self.assertEqual(set(self.read()["messages"]), {"102"})

    def test_missing_newer_post_does_not_hide_active_offer(self):
        self.register(message_id="102", when=self.now - timedelta(days=1))
        result = self.run_check()
        self.assertEqual(result["refreshed"], 1)
        self.assertEqual(result["deleted"], 1)
        self.assertIn("201", self.read()["messages"])

    def test_newer_other_storefront_post_does_not_supersede_this_scope(self):
        self.register(message_id="102", when=self.now - timedelta(days=1),
                      items=[{"key": "coin", "scope": "other-storefront"}])
        result = self.run_check()
        self.assertEqual(result["refreshed"], 1)
        self.assertEqual(result["deleted"], 1)

    def test_unreachable_newer_post_keeps_original_for_retry(self):
        self.register(message_id="102", when=self.now - timedelta(days=1))
        def unreachable(method, url, payload=None):
            if method == "GET" and url.endswith("/messages/102"):
                return 500, None
            return self.fake(method, url, payload)
        result = retention.run_retention("eneba", self.journal, self.snapshot_path,
            webhook_url="https://discord.com/api/webhooks/700/SECRET_TOKEN", now=self.now, request=unreachable)
        self.assertTrue(result["errors"])
        self.assertEqual(self.mutations(), [])
        self.assertIn("101", self.read()["messages"])

    def test_missing_old_message_refreshes_active_once_then_retires(self):
        self.fake.messages.clear()
        first = self.run_check()
        self.assertEqual(first["refreshed"], 1)
        self.assertEqual(first["retired"], 1)
        self.assertEqual(first["deleted"], 0)
        self.assertEqual([call[0] for call in self.mutations()], ["POST"])
        self.assertEqual(self.run_check()["eligible"], 0)
        self.assertEqual(self.fake.posts, 1)

    def test_missing_old_message_and_ended_offer_only_retires_record(self):
        self.fake.messages.clear()
        self.snapshot["items"] = {}
        self.save_snapshot()
        self.assertEqual(self.run_check()["retired"], 1)
        self.assertEqual(self.mutations(), [])

    def test_failed_source_does_not_treat_missing_old_as_ended(self):
        self.fake.messages.clear()
        self.snapshot["coverage"] = {}
        self.save_snapshot()
        self.assertEqual(self.run_check()["deferred"], 1)
        self.assertIn("101", self.read()["messages"])
        self.assertEqual(self.mutations(), [])

    def test_post_failure_keeps_original_and_no_fake_ack_record(self):
        self.fake.post_failure_at = 1
        before = self.journal.read_bytes()
        result = self.run_check()
        self.assertTrue(result["errors"])
        self.assertEqual(result["deleted"], 0)
        self.assertEqual(self.journal.read_bytes(), before)

    def test_invalid_post_ack_keeps_original(self):
        original = self.fake
        def wrong_ack(method, url, payload=None):
            status, value = original(method, url, payload)
            if method == "POST":
                value["channel_id"] = "901"
            return status, value
        self.fake = wrong_ack
        result = self.run_check()
        self.assertTrue(result["errors"])
        self.assertIn("101", self.read()["messages"])
        self.assertFalse(any(call[0] == "DELETE" for call in original.calls))

    def test_wrong_webhook_identity_and_untrusted_urls_are_rejected(self):
        self.fake.identity["id"] = "701"
        self.assertTrue(self.run_check()["errors"])
        self.assertEqual(self.mutations(), [])
        with self.assertRaises(retention.RetentionError):
            retention._webhook("https://discord.com.evil.example/api/webhooks/700/SECRET_TOKEN")

    def test_unknown_scope_change_is_retryable_without_deletion(self):
        self.snapshot["items"]["coin"]["scope"] = "different"
        self.save_snapshot()
        self.assertTrue(self.run_check()["errors"])
        self.assertEqual(self.mutations(), [])

    def test_overview_refresh_label_and_identity_preserved(self):
        self.journal.unlink()
        self.register(purpose="overview")
        self.run_check()
        payload = next(call[2] for call in self.fake.calls if call[0] == "POST")
        self.assertEqual(payload["username"], "Eneba Sale Watcher")
        self.assertEqual(payload["avatar_url"], "https://example.com/avatar.png")
        self.assertIn("Current price — refreshed after 60 days", payload["embeds"][0]["description"])
        self.assertEqual(self.read()["messages"]["201"]["purpose"], "overview")

    def test_header_registration_rejected_and_duplicate_ack_idempotent(self):
        before = self.journal.read_bytes()
        with self.assertRaises(retention.RetentionError):
            self.register(message_id="300", purpose="header")
        self.register(when=self.now)
        self.assertEqual(self.journal.read_bytes(), before)

    def test_corrupted_journal_not_overwritten(self):
        self.journal.write_text("{broken", encoding="utf-8")
        result = self.run_check()
        self.assertTrue(result["errors"])
        self.assertEqual(self.journal.read_text(), "{broken")
        self.assertEqual(self.fake.calls, [])

    def test_oversized_refresh_keeps_old_message(self):
        self.snapshot["items"]["coin"]["embed"]["description"] = "x" * 4096
        self.save_snapshot()
        self.assertTrue(self.run_check()["errors"])
        self.assertEqual(self.mutations(), [])

    def test_price_history_files_are_never_read_or_changed(self):
        price_history = Path(self.temp.name) / ".eneba-price-state.json"
        price_history.write_bytes(b"{historical-price-records-not-valid-json}")
        self.run_check()
        self.assertEqual(price_history.read_bytes(), b"{historical-price-records-not-valid-json}")

    def test_safe_error_output_does_not_include_url_token_or_response_body(self):
        def failure(method, url, payload=None):
            raise retention.RetentionError("Discord request failed; cleanup will retry.")
        result = retention.run_retention("eneba", self.journal, self.snapshot_path,
            webhook_url="https://discord.com/api/webhooks/700/SECRET_TOKEN", now=self.now, request=failure)
        output = json.dumps(result)
        self.assertNotIn("SECRET_TOKEN", output)
        self.assertNotIn("https://", output)
        self.assertTrue(result["errors"])


class OriginDiscord:
    """Discord's webhook-message endpoints cannot see a different origin's posts."""
    def __init__(self):
        self.calls = []
        self.identities = {"700": {"id": "700", "channel_id": "900"}, "701": {"id": "701", "channel_id": "900"}}
        self.messages = {"700": {"101": {"id": "101", "channel_id": "900", "webhook_id": "700", "pinned": False}}, "701": {}}
        self.identity_status = {}
        self.delete_failures = 0
        self.next_id = 201
        self.posts = 0
        self.post_changes = {}

    def __call__(self, method, url, payload=None):
        self.calls.append((method, url, copy.deepcopy(payload)))
        origin = url.split('/webhooks/', 1)[1].split('/', 1)[0]
        if method == 'GET' and '/messages/' not in url:
            return self.identity_status.get(origin, 200), copy.deepcopy(self.identities[origin])
        message_id = url.rsplit('/', 1)[-1]
        if method == 'GET':
            value = self.messages[origin].get(message_id)
            return (200, copy.deepcopy(value)) if value else (404, None)
        if method == 'POST':
            self.posts += 1
            ack = {'id': str(self.next_id), 'channel_id': '900', 'webhook_id': origin, 'pinned': False}
            self.next_id += 1
            self.messages[origin][ack['id']] = copy.deepcopy(ack)
            ack.update(self.post_changes)
            return 200, ack
        if method == 'DELETE':
            if self.delete_failures:
                self.delete_failures -= 1
                return 500, None
            return (204, self.messages[origin].pop(message_id)) if message_id in self.messages[origin] else (404, None)
        raise AssertionError(method)


class WebhookMigrationTests(unittest.TestCase):
    CURRENT = 'https://discord.com/api/webhooks/701/CURRENT_SECRET'
    LEGACY = 'https://discord.com/api/webhooks/700/LEGACY_SECRET'

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.journal = Path(self.temp.name) / 'journal.json'
        self.snapshot = Path(self.temp.name) / 'snapshot.json'
        self.now = datetime(2027, 1, 1, 12, tzinfo=timezone.utc)
        self.old = self.now - timedelta(days=65)
        self.fake = OriginDiscord()
        self.env = patch.dict(os.environ, {'RETENTION_ENABLED': 'true', 'RETENTION_DAYS': '60', 'DISCORD_LEGACY_WEBHOOK_URL': '', 'DISCORD_ORIGINAL_WEBHOOK_ID': ''})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.register('101', when=self.old)
        self.source = {'version': 1, 'destination': 'steam', 'observed_at': retention._iso(self.now),
            'identity': {'content': 'Current sale status'}, 'coverage': {'offers': True},
            'items': {'sale': {'scope': 'offers', 'embed': {'title': 'Sale', 'url': 'https://store.steampowered.com/specials/', 'description': 'Current sale'}}}}
        retention.write_snapshot(self.snapshot, self.source)

    def register(self, message_id, origin=None, when=None):
        ack = {'id': message_id, 'channel_id': '900'}
        if origin is not None:
            ack['webhook_id'] = origin
        retention.record_message(self.journal, ack, [{'key': 'sale', 'scope': 'offers'}], now=when or self.now)

    def read(self):
        return json.loads(self.journal.read_text(encoding='utf-8'))

    def run_check(self, legacy=LEGACY, **kwargs):
        return retention.run_retention('steam', self.journal, self.snapshot, webhook_url=self.CURRENT,
            legacy_webhook_url=legacy, now=self.now, request=self.fake, **kwargs)

    def mutations(self):
        return [call for call in self.fake.calls if call[0] in ('POST', 'DELETE')]

    def assert_no_message_requests(self):
        self.assertFalse(any('/messages/' in call[1] for call in self.fake.calls))
        self.assertEqual(self.mutations(), [])

    def test_legacy_old_message_refreshes_through_new_origin_and_deletes_through_legacy(self):
        result = self.run_check()
        self.assertEqual(result['errors'], [])
        self.assertEqual([call[0] for call in self.mutations()], ['POST', 'DELETE'])
        self.assertTrue(self.mutations()[0][1].startswith(self.CURRENT))
        self.assertTrue(self.mutations()[1][1].startswith(self.LEGACY))
        self.assertEqual(self.read()['messages']['201']['webhook_id'], '701')
        self.assertEqual(self.read()['legacy_webhook_id'], '700')
        self.assertEqual(self.read()['version'], 1)
        self.assertNotIn('avatar_url', self.mutations()[0][2])
        self.assertNotIn('username', self.mutations()[0][2])

    def test_failed_legacy_delete_retries_without_reposting_new_origin(self):
        self.fake.delete_failures = 1
        first = self.run_check()
        self.assertEqual(first['refreshed'], 1)
        self.assertTrue(first['errors'])
        self.assertEqual(self.read()['messages']['101']['replacement_ids'], ['201'])
        second = self.run_check()
        self.assertEqual(second['refreshed'], 0)
        self.assertEqual(second['deleted'], 1)
        self.assertEqual(self.fake.posts, 1)
        self.assertTrue(any('/701/' in url and url.endswith('/messages/201') for method, url, _ in self.fake.calls if method == 'GET'))

    def test_pinned_legacy_message_is_preserved(self):
        self.fake.messages['700']['101']['pinned'] = True
        self.assertEqual(self.run_check()['pinned'], 1)
        self.assertEqual(self.mutations(), [])
        self.assertIn('101', self.read()['messages'])

    def test_missing_legacy_for_untagged_steam_alert_never_tries_current_message_endpoint(self):
        before = self.journal.read_bytes()
        self.assertTrue(self.run_check(legacy=None)['errors'])
        self.assert_no_message_requests()
        self.assertEqual(self.journal.read_bytes(), before)

    def test_configured_original_identity_preserves_cleanup_before_secret_replacement(self):
        with patch.dict(os.environ, {'DISCORD_ORIGINAL_WEBHOOK_ID': '700'}):
            result = retention.run_retention('steam', self.journal, self.snapshot,
                webhook_url=self.LEGACY, now=self.now, request=self.fake)
        self.assertEqual(result['errors'], [])
        self.assertEqual(result['refreshed'], 1)
        self.assertEqual(result['deleted'], 1)
        self.assertTrue(all(url.startswith(self.LEGACY) for _, url, _ in self.fake.calls))

    def test_original_identity_without_legacy_secret_cannot_use_replacement_for_old_message(self):
        with patch.dict(os.environ, {'DISCORD_ORIGINAL_WEBHOOK_ID': '700'}):
            self.assertTrue(self.run_check(legacy=None)['errors'])
        self.assert_no_message_requests()

    def test_original_credential_backup_is_harmless_before_primary_secret_changes(self):
        self.fake.messages['700']['101']['pinned'] = True
        with patch.dict(os.environ, {'DISCORD_ORIGINAL_WEBHOOK_ID': '700'}):
            result = retention.run_retention('steam', self.journal, self.snapshot,
                webhook_url=self.LEGACY, legacy_webhook_url=self.LEGACY,
                now=self.now, request=self.fake)
        self.assertEqual(result['errors'], [])
        self.assertEqual(result['pinned'], 1)
        self.assertEqual(self.mutations(), [])

    def test_malformed_original_identity_or_conflicting_legacy_fails_closed(self):
        for original in ('malformed', '702'):
            with self.subTest(original=original), patch.dict(os.environ, {'DISCORD_ORIGINAL_WEBHOOK_ID': original}):
                self.assertTrue(self.run_check()['errors'])
                self.assert_no_message_requests()

    def test_missing_legacy_for_explicit_origin_fails_closed(self):
        self.register('101', origin='700', when=self.old)
        self.assertTrue(self.run_check(legacy=None)['errors'])
        self.assert_no_message_requests()

    def test_durable_migration_marker_requires_legacy_if_secret_later_missing(self):
        self.fake.messages['700']['101']['pinned'] = True
        self.assertEqual(self.run_check()['pinned'], 1)
        self.assertEqual(self.read()['legacy_webhook_id'], '700')
        self.fake.calls.clear()
        self.assertTrue(self.run_check(legacy=None)['errors'])
        self.assert_no_message_requests()

    def test_legacy_channel_or_identity_mismatch_blocks_old_message_requests(self):
        for field, value in [('id', '799'), ('channel_id', '999')]:
            with self.subTest(field=field):
                self.fake = OriginDiscord()
                self.fake.identities['700'][field] = value
                self.assertTrue(self.run_check()['errors'])
                self.assert_no_message_requests()

    def test_unavailable_legacy_identity_never_treats_old_message_as_missing(self):
        for status in (401, 403, 404, 429, 500):
            with self.subTest(status=status):
                self.fake = OriginDiscord()
                self.fake.identity_status['700'] = status
                self.assertTrue(self.run_check()['errors'])
                self.assert_no_message_requests()
                self.assertIn('101', self.read()['messages'])

    def test_unknown_third_origin_cannot_fall_back_to_any_supplied_url(self):
        self.register('101', origin='799', when=self.old)
        self.assertTrue(self.run_check()['errors'])
        self.assert_no_message_requests()

    def test_old_message_origin_and_channel_must_match_its_resolved_credential(self):
        for field, value in [('webhook_id', '701'), ('webhook_id', None), ('channel_id', '999')]:
            with self.subTest(field=field, value=value):
                self.fake = OriginDiscord()
                self.fake.messages['700']['101'][field] = value
                self.assertTrue(self.run_check()['errors'])
                self.assertEqual(self.mutations(), [])
                self.assertIn('101', self.read()['messages'])

    def test_new_post_ack_must_identify_current_origin_before_old_deletion(self):
        for value in ('700', None, 'invalid'):
            with self.subTest(value=value):
                self.fake = OriginDiscord()
                self.fake.post_changes['webhook_id'] = value
                self.assertTrue(self.run_check()['errors'])
                self.assertFalse(any(call[0] == 'DELETE' for call in self.fake.calls))
                self.assertEqual(set(self.read()['messages']), {'101'})

    def test_current_candidate_covers_legacy_old_message_via_its_own_origin(self):
        self.register('102', origin='701')
        self.fake.messages['701']['102'] = {'id': '102', 'channel_id': '900', 'webhook_id': '701', 'pinned': False}
        result = self.run_check()
        self.assertEqual(result['errors'], [])
        self.assertEqual(result['refreshed'], 0)
        self.assertEqual(result['deleted'], 1)
        self.assertTrue(any(url.startswith(self.CURRENT) and url.endswith('/messages/102') for method, url, _ in self.fake.calls if method == 'GET'))

    def test_legacy_candidate_covers_current_old_message_via_legacy_origin(self):
        self.register('101', origin='701', when=self.old)
        self.fake.messages['701']['101'] = self.fake.messages['700'].pop('101')
        self.fake.messages['701']['101']['webhook_id'] = '701'
        self.register('102', origin='700')
        self.fake.messages['700']['102'] = {'id': '102', 'channel_id': '900', 'webhook_id': '700', 'pinned': False}
        result = self.run_check()
        self.assertEqual(result['errors'], [])
        self.assertEqual(result['refreshed'], 0)
        self.assertTrue(self.mutations()[0][1].startswith(self.CURRENT))
        self.assertTrue(any(url.startswith(self.LEGACY) and url.endswith('/messages/102') for method, url, _ in self.fake.calls if method == 'GET'))

    def test_404_through_correct_legacy_origin_refreshes_active_offer_once(self):
        self.fake.messages['700'].clear()
        first = self.run_check()
        self.assertEqual(first['refreshed'], 1)
        self.assertEqual(first['retired'], 1)
        self.assertEqual(first['deleted'], 0)
        self.assertEqual(self.run_check()['eligible'], 0)
        self.assertEqual(self.fake.posts, 1)

    def test_404_through_correct_legacy_origin_can_retire_ended_offer(self):
        self.fake.messages['700'].clear()
        self.source['items'] = {}
        retention.write_snapshot(self.snapshot, self.source)
        self.assertEqual(self.run_check()['retired'], 1)
        self.assertEqual(self.mutations(), [])

    def test_dry_run_does_not_write_migration_marker_or_contact_discord(self):
        before = self.journal.read_bytes()
        self.assertEqual(self.run_check(dry_run=True)['eligible'], 1)
        self.assertEqual(self.fake.calls, [])
        self.assertEqual(self.journal.read_bytes(), before)

    def test_new_origin_registration_is_numeric_and_cannot_conflict(self):
        self.register('102', origin='701')
        self.assertEqual(self.read()['messages']['102']['webhook_id'], '701')
        before = self.journal.read_bytes()
        for origin in ('700', 'not-an-id'):
            with self.assertRaises(retention.RetentionError):
                self.register('102', origin=origin)
        self.assertEqual(self.journal.read_bytes(), before)

    def test_corrupt_or_conflicting_legacy_marker_is_not_overwritten(self):
        for marker in ('invalid', '799'):
            with self.subTest(marker=marker):
                value = self.read()
                value['legacy_webhook_id'] = marker
                self.journal.write_text(json.dumps(value), encoding='utf-8')
                before = self.journal.read_bytes()
                self.fake.calls.clear()
                self.assertTrue(self.run_check()['errors'])
                self.assertEqual(self.journal.read_bytes(), before)
                self.assertEqual(self.mutations(), [])

    def test_same_current_and_legacy_id_is_rejected_without_message_requests(self):
        self.assertTrue(self.run_check(legacy=self.CURRENT)['errors'])
        self.assert_no_message_requests()

    def test_tagged_current_post_can_be_checked_without_legacy_credentials(self):
        self.register('101', origin='701', when=self.old)
        self.fake.messages['701']['101'] = self.fake.messages['700'].pop('101')
        self.fake.messages['701']['101']['webhook_id'] = '701'
        result = self.run_check(legacy=None)
        self.assertEqual(result['errors'], [])
        self.assertEqual(result['deleted'], 1)
        self.assertEqual(self.read()['messages']['201']['webhook_id'], '701')

    def test_summary_and_journal_never_contain_webhook_urls_or_tokens(self):
        self.fake.identity_status['700'] = 403
        result = self.run_check()
        output = json.dumps(result) + self.journal.read_text(encoding='utf-8')
        for private in (self.CURRENT, self.LEGACY, 'CURRENT_SECRET', 'LEGACY_SECRET'):
            self.assertNotIn(private, output)
        self.assertTrue(result['errors'])


if __name__ == "__main__":
    unittest.main()
