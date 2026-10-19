"""Offline fixed-destination archival migration tests; fake credentials only."""
import copy
import importlib.util
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from discord_retention import RetentionError

SCRIPT = Path(__file__).parent / "consolidate_webhooks.py"
SPEC = importlib.util.spec_from_file_location("consolidate_webhooks", SCRIPT)
watcher = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(watcher)
CREATED = "2026-10-07T21:22:11.368000Z"
IMAGE = "https://example.org/public-sale-artwork.png"


class Discord:
    def __init__(self, config):
        self.config = config
        self.legacy = "https://discord.com/api/webhooks/" + config["old"] + "/fake-old-secret"
        self.current = "https://discord.com/api/webhooks/" + config["current"] + "/fake-current-secret"
        self.identities = {
            self.legacy: {"id": config["old"], "channel_id": config["channel"], "token": "fake-old-secret"},
            self.current: {"id": config["current"], "channel_id": config["channel"], "token": "fake-current-secret"},
        }
        self.messages = {}
        self.calls = []
        self.posted = []
        self.delete_status = 204
        self.bad_ack = None
        for source_id in config["messages"]:
            self.messages[(self.legacy, source_id)] = {
                "id": source_id, "channel_id": config["channel"], "webhook_id": config["old"],
                "author": {"id": config["old"], "username": "Watcher", "bot": True},
                "pinned": False, "timestamp": CREATED, "attachments": [],
                "content": "Original alert: 33% reported; purchase price unverified.",
                "embeds": [{"type": "rich", "title": "Original game", "url": "https://example.org/product",
                            "description": "310 Minecoins; original regional listing.",
                            "image": {"url": IMAGE, "proxy_url": "https://example.org/proxy", "height": 200, "width": 200},
                            "footer": {"text": "Original alert footer", "icon_url": IMAGE, "proxy_icon_url": "https://example.org/proxy"},
                            "provider": {"name": "read only"}, "video": {"url": "https://example.org/read-only"}}],
            }

    def __call__(self, method, url, payload=None):
        self.calls.append((method, url, copy.deepcopy(payload)))
        if method == "GET" and url in self.identities:
            return 200, copy.deepcopy(self.identities[url])
        if method == "POST":
            if url != self.current + "?wait=true":
                raise AssertionError("wrong post destination")
            message_id = str(1700000000000000000 + len(self.posted))
            message = dict(copy.deepcopy(payload), id=message_id, channel_id=self.config["channel"],
                           webhook_id=self.config["current"], author={"id": self.config["current"]},
                           pinned=False, attachments=[], timestamp="2026-10-19T00:00:00Z")
            self.posted.append(message_id)
            self.messages[(self.current, message_id)] = copy.deepcopy(message)
            if self.bad_ack:
                message.update(self.bad_ack)
            return 200, message
        for base in (self.legacy, self.current):
            if url.startswith(base + "/messages/"):
                message_id = url.rsplit("/", 1)[-1]
                if method == "GET":
                    value = self.messages.get((base, message_id))
                    return (200, copy.deepcopy(value)) if value else (404, None)
                if method == "DELETE":
                    if base != self.legacy:
                        raise AssertionError("wrong delete origin")
                    if self.delete_status == 204:
                        self.messages.pop((base, message_id), None)
                    return self.delete_status, None
        raise AssertionError("unexpected request")

    def writes(self):
        return [call for call in self.calls if call[0] != "GET"]


class ConsolidationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.journal = self.root / "journal.json"
        self.backup = self.root / "backup"
        self.prepare("steam")

    def prepare(self, destination):
        self.destination = destination
        self.config = watcher.DESTINATIONS[destination]
        self.discord = Discord(self.config)
        self.original = {"version": 1, "messages": {source_id: {
            "message_id": source_id, "channel_id": self.config["channel"], "created_at": CREATED,
            "items": [{"key": "original-item", "scope": "original-scope"}],
            "purpose": "alert", "replacement_ids": [],
        } for source_id in self.config["messages"]}}
        self.journal.write_text(json.dumps(self.original), encoding="utf8")
        self.before = self.journal.read_bytes()

    def run_copy(self, **kwargs):
        return watcher.consolidate(self.destination, self.journal, self.backup,
                                   current_url=self.discord.current, legacy_url=self.discord.legacy,
                                   request=self.discord, **kwargs)

    def test_default_preview_only_reads_discord_and_does_not_write_journal(self):
        with patch.object(watcher, "_atomic_json", wraps=watcher._atomic_json) as save:
            receipt = self.run_copy()
        self.assertEqual(self.discord.writes(), [])
        self.assertEqual(self.journal.read_bytes(), self.before)
        self.assertTrue(all(Path(call.args[0]).parent == self.backup for call in save.call_args_list))
        self.assertEqual(receipt["backed_up"], 1)
        self.assertTrue(receipt["dry_run"])
        backup = json.loads(next(self.backup.glob("*.json")).read_text(encoding="utf8"))
        self.assertEqual(backup, next(iter(self.discord.messages.values())))
        encoded = json.dumps(backup)
        self.assertNotIn("fake-old-secret", encoded)
        self.assertNotIn("fake-current-secret", encoded)
        self.assertNotIn('"token"', encoded)
        self.assertIn(IMAGE, encoded)

    def test_apply_preserves_original_dates_keys_purpose_and_content_with_archive_label(self):
        untouched = self.root / "watcher-state.json"
        untouched.write_text("original sale/price dedup history", encoding="utf8")
        receipt = self.run_copy(dry_run=False)
        migrated = json.loads(self.journal.read_text(encoding="utf8"))["messages"]
        self.assertEqual(len(migrated), 1)
        replacement_id, replacement = next(iter(migrated.items()))
        old_id, old = next(iter(self.original["messages"].items()))
        for field in ("created_at", "items", "purpose", "channel_id"):
            self.assertEqual(replacement[field], old[field])
        self.assertEqual(replacement["webhook_id"], self.config["current"])
        self.assertEqual(replacement["archived_from"], old_id)
        self.assertEqual(replacement["replacement_ids"], [])
        self.assertEqual(replacement["message_id"], replacement_id)
        posted = self.discord.messages[(self.discord.current, replacement_id)]
        self.assertIn("Archived alert — originally posted 2026-10-07T21:22:11.368000Z", posted["content"])
        self.assertIn("Historical copy, not a new sale; prices/details from original alert", posted["content"])
        self.assertTrue(posted["content"].endswith("Original alert: 33% reported; purchase price unverified."))
        self.assertEqual(posted["allowed_mentions"], {"parse": []})
        self.assertNotIn("avatar_url", posted)
        self.assertNotIn("username", posted)
        embed = posted["embeds"][0]
        self.assertEqual(embed["image"], {"url": IMAGE})
        self.assertEqual(embed["description"], "310 Minecoins; original regional listing.")
        self.assertEqual(embed["footer"], {"text": "Original alert footer", "icon_url": IMAGE})
        self.assertNotIn("type", embed)
        self.assertNotIn("provider", embed)
        self.assertNotIn("video", embed)
        self.assertEqual((receipt["posted"], receipt["deleted"], receipt["remaining_old_origin"]), (1, 1, 0))
        self.assertEqual(untouched.read_text(encoding="utf8"), "original sale/price dedup history")

    def test_preflight_validates_all_minecraft_sources_before_any_mutation(self):
        self.prepare("minecraft")
        last_id = sorted(self.config["messages"])[-1]
        self.discord.messages[(self.discord.legacy, last_id)]["pinned"] = True
        with self.assertRaises(RetentionError):
            self.run_copy(dry_run=False)
        self.assertEqual(self.discord.writes(), [])
        self.assertEqual(self.journal.read_bytes(), self.before)
        self.assertFalse(self.backup.exists())

    def test_wrong_identity_id_or_channel_and_missing_credentials_never_mutate(self):
        for base, field, value in ((self.discord.current, "id", self.config["old"]),
                                   (self.discord.legacy, "channel_id", "999999999999999999")):
            with self.subTest(field=field):
                original = self.discord.identities[base][field]
                self.discord.identities[base][field] = value
                with self.assertRaises(RetentionError):
                    self.run_copy(dry_run=False)
                self.discord.identities[base][field] = original
        with patch.dict(os.environ, {}, clear=True):
            for current, legacy in ((None, self.discord.legacy), (self.discord.current, None)):
                with self.subTest(missing=current is None):
                    with self.assertRaises(RetentionError):
                        watcher.consolidate(self.destination, self.journal, self.backup,
                                            current_url=current, legacy_url=legacy, request=self.discord, dry_run=False)
        self.assertEqual(self.discord.writes(), [])
        self.assertEqual(self.journal.read_bytes(), self.before)

    def test_wrong_url_id_message_channel_author_origin_timestamp_or_attachments_never_mutate(self):
        with self.assertRaises(RetentionError):
            watcher.consolidate(self.destination, self.journal, self.backup,
                                current_url=self.discord.legacy, legacy_url=self.discord.current,
                                request=self.discord, dry_run=False)
        source_id = next(iter(self.config["messages"]))
        message = self.discord.messages[(self.discord.legacy, source_id)]
        for field, value in (("id", "999999999999999999"), ("channel_id", "999999999999999999"),
                             ("webhook_id", self.config["current"]), ("author", {"id": self.config["current"]}),
                             ("timestamp", None), ("timestamp", "2026-10-08T00:00:00Z"),
                             ("pinned", True), ("pinned", None), ("attachments", [{"id": "123"}]),
                             ("components", [{"type": 1}])):
            with self.subTest(field=field, value=value):
                original = message.get(field)
                message[field] = value
                with self.assertRaises(RetentionError):
                    self.run_copy(dry_run=False)
                if original is None:
                    message.pop(field, None)
                else:
                    message[field] = original
        self.assertEqual(self.discord.writes(), [])
        self.assertEqual(self.journal.read_bytes(), self.before)

    def test_bad_post_ack_never_deletes_original_or_checkpoints_unverified_copy(self):
        for bad_ack in ({"channel_id": "999999999999999999"}, {"webhook_id": self.config["old"]},
                        {"webhook_id": None}, {"content": "different content"}, {"author": {"id": self.config["old"]}}):
            with self.subTest(bad_ack=bad_ack):
                self.discord.bad_ack = bad_ack
                with self.assertRaises(RetentionError):
                    self.run_copy(dry_run=False)
        self.assertFalse(any(method == "DELETE" for method, _, _ in self.discord.calls))
        self.assertEqual(self.journal.read_bytes(), self.before)

    def test_failed_delete_reuses_durable_verified_replacement_without_duplicate_post(self):
        self.discord.delete_status = 500
        with self.assertRaises(RetentionError):
            self.run_copy(dry_run=False)
        checkpoint = json.loads(self.journal.read_text(encoding="utf8"))["messages"]
        old_id = next(iter(self.config["messages"]))
        replacement_id = checkpoint[old_id]["replacement_ids"][0]
        self.assertEqual(checkpoint[replacement_id]["webhook_id"], self.config["current"])
        self.assertEqual(len(self.discord.posted), 1)
        self.discord.delete_status = 204
        receipt = self.run_copy(dry_run=False)
        self.assertEqual(len(self.discord.posted), 1)
        self.assertEqual((receipt["reused"], receipt["posted"], receipt["deleted"]), (1, 0, 1))
        again = self.run_copy(dry_run=False)
        self.assertEqual(again["planned"], 0)
        self.assertEqual(len(self.discord.posted), 1)

    def test_deleted_source_with_saved_replacement_recovers_without_posting(self):
        self.discord.delete_status = 500
        with self.assertRaises(RetentionError):
            self.run_copy(dry_run=False)
        old_id = next(iter(self.config["messages"]))
        self.discord.messages.pop((self.discord.legacy, old_id))
        receipt = self.run_copy(dry_run=False)
        self.assertEqual((receipt["already_absent"], receipt["reused"], receipt["remaining_old_origin"]), (1, 1, 0))
        self.assertEqual(len(self.discord.posted), 1)

    def test_missing_or_changed_replacement_blocks_retry_and_no_duplicate_post(self):
        self.discord.delete_status = 500
        with self.assertRaises(RetentionError):
            self.run_copy(dry_run=False)
        replacement_id = self.discord.posted[0]
        self.discord.messages[(self.discord.current, replacement_id)]["content"] = "unexpected edit"
        calls_before = len(self.discord.writes())
        with self.assertRaises(RetentionError):
            self.run_copy(dry_run=False)
        self.assertEqual(len(self.discord.writes()), calls_before)
        self.assertEqual(len(self.discord.posted), 1)

    def test_unexpected_untagged_old_origin_or_unknown_origin_records_are_rejected(self):
        for origin in (None, self.config["old"], "999999999999999999"):
            with self.subTest(origin=origin):
                changed = copy.deepcopy(self.original)
                record = copy.deepcopy(next(iter(changed["messages"].values())))
                record["message_id"] = "999999999999999999"
                if origin:
                    record["webhook_id"] = origin
                changed["messages"][record["message_id"]] = record
                self.journal.write_text(json.dumps(changed), encoding="utf8")
                with self.assertRaises(RetentionError):
                    self.run_copy(dry_run=False)
        self.assertEqual(self.discord.writes(), [])

    def test_source_secret_or_webhook_url_cannot_enter_backup_or_post(self):
        message = next(iter(self.discord.messages.values()))
        for content in (self.discord.legacy, "credentials: fake-current-secret"):
            with self.subTest(content=content):
                message["content"] = content
                with self.assertRaises(RetentionError):
                    self.run_copy(dry_run=False)
        self.assertEqual(self.discord.writes(), [])
        self.assertFalse(self.backup.exists())

    def test_journal_checkpoint_is_durable_before_delete(self):
        original_request = self.discord
        def request(method, url, payload=None):
            if method == "DELETE":
                durable = json.loads(self.journal.read_text(encoding="utf8"))["messages"]
                source_id = url.rsplit("/", 1)[-1]
                replacement_id = durable[source_id]["replacement_ids"][0]
                self.assertEqual(durable[replacement_id]["archived_from"], source_id)
                self.assertEqual(durable[replacement_id]["created_at"], CREATED)
                self.assertEqual(durable[replacement_id]["webhook_id"], self.config["current"])
            return original_request(method, url, payload)
        watcher.consolidate(self.destination, self.journal, self.backup, current_url=self.discord.current,
                            legacy_url=self.discord.legacy, request=request, dry_run=False)

    def test_source_pinned_after_copy_is_preserved_with_replacement_checkpoint(self):
        original_request = self.discord
        source_id = next(iter(self.config["messages"]))
        def request(method, url, payload=None):
            result = original_request(method, url, payload)
            if method == "POST":
                self.discord.messages[(self.discord.legacy, source_id)]["pinned"] = True
            return result
        with self.assertRaises(RetentionError):
            watcher.consolidate(self.destination, self.journal, self.backup, current_url=self.discord.current,
                                legacy_url=self.discord.legacy, request=request, dry_run=False)
        self.assertFalse(any(method == "DELETE" for method, _, _ in self.discord.calls))
        checkpoint = json.loads(self.journal.read_text(encoding="utf8"))["messages"]
        self.assertEqual(checkpoint[source_id]["replacement_ids"], self.discord.posted)

    def test_cli_never_prints_network_exception_secrets(self):
        output = io.StringIO()
        with patch.dict(os.environ, {"DISCORD_WEBHOOK_URL": self.discord.current,
                                    "DISCORD_LEGACY_WEBHOOK_URL": self.discord.legacy}), \
             patch.object(watcher, "_request", side_effect=RuntimeError(self.discord.current)), redirect_stdout(output):
            code = watcher.main(["--destination", self.destination, "--journal", str(self.journal), "--backup-dir", str(self.backup)])
        self.assertEqual(code, 1)
        self.assertNotIn("fake-current-secret", output.getvalue())
        self.assertNotIn("fake-old-secret", output.getvalue())
        self.assertFalse(json.loads(output.getvalue())["success"])


if __name__ == "__main__":
    unittest.main()
