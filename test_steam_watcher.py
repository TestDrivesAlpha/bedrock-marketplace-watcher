import contextlib
import io
import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.error import URLError

import steam_watcher as watcher


class SteamWatcherTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.state = Path(self.directory.name) / ".steam-sale-notified"
        self.sale = watcher.SALES[1]
        self.now = datetime.fromisoformat("2026-12-18T12:00:00+00:00")
        self.fetch = Mock(return_value="")
        self.post = Mock()

    def run_check(self, **kwargs):
        with contextlib.redirect_stdout(io.StringIO()):
            return watcher.check_sales(
                kwargs.pop("now", self.now), self.state,
                fetch_html=self.fetch, post_payload=self.post,
                webhook="https://discord.example/test", **kwargs)

    def test_future_and_expired_events_do_not_send(self):
        self.assertEqual(self.run_check(now=datetime.fromisoformat("2026-10-17T12:00:00+00:00")), 0)
        self.fetch.assert_not_called()
        self.post.assert_not_called()
        self.assertFalse(self.state.exists())

    def test_event_start_included_and_end_excluded(self):
        self.assertEqual(self.run_check(now=datetime.fromisoformat(self.sale["end"]), dry_run=True), 0)
        self.assertEqual(self.run_check(now=datetime.fromisoformat(self.sale["start"]), dry_run=True), 1)
        self.post.assert_not_called()

    def test_duplicate_excluded_without_resetting_history(self):
        self.state.write_text("autumn-2026\nwinter-2026\n", encoding="utf-8")
        self.assertEqual(self.run_check(), 0)
        self.post.assert_not_called()
        self.assertEqual(self.state.read_text(encoding='utf-8'), "autumn-2026\nwinter-2026\n")

    def test_success_embeds_image_link_and_saves_existing_history(self):
        self.state.write_text("autumn-2026\n", encoding="utf-8")
        self.assertEqual(self.run_check(), 1)
        payload = self.post.call_args.args[1]
        embed = payload["embeds"][0]
        self.assertEqual(embed["title"], "Steam Winter Sale 2026")
        self.assertEqual(embed["image"]["url"], watcher.STEAM_LOGO)
        self.assertEqual(embed["url"], "https://store.steampowered.com/specials/")
        self.assertEqual(payload["allowed_mentions"], {"parse": []})
        self.assertNotIn("username", payload)
        self.assertNotIn("avatar_url", payload)
        self.assertIn("autumn-2026", self.state.read_text(encoding='utf-8'))
        self.assertIn("winter-2026", self.state.read_text(encoding='utf-8'))

    def test_dry_run_does_not_send_or_advance_history(self):
        self.state.write_text("autumn-2026\n", encoding="utf-8")
        self.assertEqual(self.run_check(dry_run=True), 1)
        self.post.assert_not_called()
        self.assertEqual(self.state.read_text(encoding='utf-8'), "autumn-2026\n")

    def test_failed_webhook_does_not_advance_history(self):
        self.state.write_text("autumn-2026\n", encoding="utf-8")
        self.post.side_effect = RuntimeError("Discord webhook failed (HTTP 500).")
        with self.assertRaises(RuntimeError):
            self.run_check()
        self.assertEqual(self.state.read_text(encoding='utf-8'), "autumn-2026\n")

    def test_artwork_fetch_failure_still_sends_logo(self):
        self.fetch.side_effect = URLError("offline")
        self.assertEqual(self.run_check(), 1)
        self.assertEqual(self.post.call_args.args[1]["embeds"][0]["image"]["url"], watcher.STEAM_LOGO)

    def test_matching_sale_metadata_uses_official_event_artwork(self):
        html = '''<meta property="og:title" content="Steam Winter Sale 2026">
                  <meta property="og:image" content="https://cdn.akamai.steamstatic.com/sale.jpg">'''
        self.assertEqual(watcher.event_image(self.sale, html),
                         ("https://cdn.akamai.steamstatic.com/sale.jpg", True))
        payload = watcher.build_payload(self.sale, html)
        self.assertNotIn("username", payload)
        self.assertNotIn("avatar_url", payload)
        self.assertEqual(payload["embeds"][0]["image"]["url"], "https://cdn.akamai.steamstatic.com/sale.jpg")

    def test_wrong_event_or_untrusted_image_falls_back_to_logo(self):
        for title, image in [
            ("Steam Summer Sale 2026", "https://cdn.akamai.steamstatic.com/summer.jpg"),
            ("Steam Winter Sale 2026", "https://example.com/fake.jpg"),
            ("Steam Winter Sale 2026", "https://steamstatic.com.evil.example/fake.jpg"),
            ("Steam Winter Sale 2026", "http://cdn.akamai.steamstatic.com/winter.jpg"),
            ("Steam Winter Sale 2026", "https://cdn.akamai.steamstatic.com:bad/winter.jpg"),
        ]:
            with self.subTest(title=title, image=image):
                html = f'<meta property="og:title" content="{title}"><meta property="og:image" content="{image}">'
                self.assertEqual(watcher.event_image(self.sale, html), (watcher.STEAM_LOGO, False))

    def test_schedule_retains_all_original_events(self):
        self.assertEqual([sale["id"] for sale in watcher.SALES],
                         ["autumn-2026", "winter-2026", "spring-2027", "summer-2027"])

    def test_retention_records_ack_without_resetting_normal_history(self):
        journal = Path(self.directory.name) / "posts.json"
        self.post.return_value = {"message_id": "123", "channel_id": "456", "webhook_id": "789"}
        self.assertEqual(self.run_check(journal_path=journal), 1)
        record = json.loads(journal.read_text(encoding='utf-8'))["messages"]["123"]
        self.assertEqual(record["webhook_id"], "789")
        self.assertEqual(record["items"], [{"key": self.sale["id"], "scope": "steam-schedule"}])
        self.assertIn(self.sale["id"], self.state.read_text(encoding='utf-8'))
        self.assertEqual(self.run_check(journal_path=journal), 0)
        self.assertEqual(len(json.loads(journal.read_text(encoding='utf-8'))["messages"]), 1)

    def test_retention_snapshot_rechecks_announced_sale_and_confirms_ended_sale(self):
        snapshot = Path(self.directory.name) / "current.json"
        self.state.write_text(self.sale["id"] + "\n")
        self.assertEqual(self.run_check(snapshot_path=snapshot), 0)
        current = json.loads(snapshot.read_text(encoding='utf-8'))
        self.assertIn(self.sale["id"], current["items"])
        self.assertIn("Still available — refreshed after 60 days", current["items"][self.sale["id"]]["embed"]["description"])
        self.assertEqual(self.run_check(now=datetime.fromisoformat(self.sale["end"]), snapshot_path=snapshot), 0)
        ended = json.loads(snapshot.read_text(encoding='utf-8'))
        self.assertNotIn(self.sale["id"], ended["items"])
        self.assertTrue(ended["coverage"]["steam-schedule"])
        self.post.assert_not_called()

    def test_retention_refresh_uses_saved_webhook_profile_and_keeps_official_artwork(self):
        snapshot = Path(self.directory.name) / "current.json"
        self.fetch.return_value = '''<meta property="og:title" content="Steam Winter Sale 2026">
            <meta property="og:image" content="https://cdn.akamai.steamstatic.com/sale.jpg">'''
        self.run_check(dry_run=True, snapshot_path=snapshot)
        current = json.loads(snapshot.read_text(encoding="utf-8"))
        self.assertEqual(current["identity"], {
            "content": "Steam seasonal sale status — refreshed after 60 days",
        })
        embed = current["items"][self.sale["id"]]["embed"]
        self.assertEqual(embed["image"]["url"], "https://cdn.akamai.steamstatic.com/sale.jpg")
        self.assertEqual(embed["url"], watcher.STORE_URL)
        self.post.assert_not_called()

    def test_retention_dry_run_only_exports_ephemeral_snapshot(self):
        journal = Path(self.directory.name) / "posts.json"
        snapshot = Path(self.directory.name) / "current.json"
        journal.write_text("keep exactly")
        self.run_check(dry_run=True, journal_path=journal, snapshot_path=snapshot)
        self.assertEqual(journal.read_text(encoding='utf-8'), "keep exactly")
        self.assertTrue(snapshot.exists())
        self.assertFalse(self.state.exists())
        self.post.assert_not_called()

    def test_webhook_wait_ack_is_required_and_only_safe_ids_returned(self):
        class Response(io.BytesIO):
            status = 200
            def __enter__(self):
                return self
            def __exit__(self, *args):
                self.close()
        with patch.object(watcher, "urlopen", return_value=Response(b'{"id":"123","channel_id":"456","webhook_id":"789","token":"secret"}')) as opened:
            self.assertEqual(watcher.send_discord("https://discord.example/hook?wait=false&thread_id=9", {}),
                             {"message_id": "123", "channel_id": "456", "webhook_id": "789"})
        self.assertIn("wait=true", opened.call_args.args[0].full_url)
        self.assertIn("thread_id=9", opened.call_args.args[0].full_url)
        for body in (b'{}', b'{"id":"invalid","channel_id":"456"}', b'{"id":"123","channel_id":"456"}', b'{"id":"123","channel_id":"456","webhook_id":"invalid"}', b'[]', b'invalid JSON'):
            with self.subTest(body=body), patch.object(watcher, "urlopen", return_value=Response(body)):
                with self.assertRaises(RuntimeError):
                    watcher.send_discord("https://discord.example/hook", {})


if __name__ == "__main__":
    unittest.main()
