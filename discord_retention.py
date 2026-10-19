"""Keep Discord alert channels readable without changing sale or price history.

Only acknowledged webhook posts entered in a separate delivery journal are eligible.
Fresh, complete source coverage is required before retiring any aged message.
"""
from __future__ import annotations

import argparse
import copy
import json
import os
import re
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit, urlunsplit
from urllib.request import Request, urlopen

VERSION = 1
DEFAULT_RETENTION_DAYS = 60
MAX_EMBED_CHARS = 5500
SNOWFLAKE = re.compile(r"^[1-9][0-9]*$")


class RetentionError(RuntimeError):
    """A safe, token-free explanation suitable for workflow logs."""


def _time(value=None):
    if value is None:
        return datetime.now(timezone.utc)
    if isinstance(value, datetime):
        result = value
    elif isinstance(value, str):
        try:
            result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            raise RetentionError("Invalid timestamp.") from None
    else:
        raise RetentionError("Invalid timestamp.")
    if result.tzinfo is None:
        raise RetentionError("Timestamps must include a timezone.")
    return result.astimezone(timezone.utc)


def _iso(value=None):
    return _time(value).isoformat().replace("+00:00", "Z")


def _id(value):
    result = str(value) if value is not None else ""
    if not SNOWFLAKE.fullmatch(result):
        raise RetentionError("Discord returned an invalid message or channel ID.")
    return result


def _items(items):
    if not isinstance(items, list) or not items:
        raise RetentionError("A registered alert must identify its source items.")
    result = []
    seen = set()
    for item in items:
        if not isinstance(item, dict):
            raise RetentionError("Invalid source item.")
        key, scope = item.get("key"), item.get("scope")
        if not isinstance(key, str) or not key or not isinstance(scope, str) or not scope:
            raise RetentionError("Invalid source item key or scope.")
        if key not in seen:
            result.append({"key": key, "scope": scope})
            seen.add(key)
    return result


def _atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def load_journal(path):
    path = Path(path)
    if not path.exists():
        return {"version": VERSION, "messages": {}}
    try:
        journal = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise RetentionError("Delivery journal could not be read; no cleanup was performed.") from None
    if not isinstance(journal, dict) or journal.get("version") != VERSION or not isinstance(journal.get("messages"), dict):
        raise RetentionError("Unsupported delivery journal; no cleanup was performed.")
    if "legacy_webhook_id" in journal:
        _id(journal["legacy_webhook_id"])
    for message_id, message in journal["messages"].items():
        if not isinstance(message, dict) or _id(message_id) != _id(message.get("message_id")):
            raise RetentionError("Invalid delivery journal message.")
        _id(message.get("channel_id"))
        if "webhook_id" in message:
            _id(message["webhook_id"])
        _time(message.get("created_at"))
        if message.get("purpose") not in ("alert", "overview"):
            raise RetentionError("Non-alert entries cannot be managed by retention.")
        _items(message.get("items"))
    return journal


def record_message(journal_path, acknowledgement, items, now=None, purpose="alert"):
    """Persist a successful wait=true ACK separately from price/dedup history."""
    if purpose not in ("alert", "overview"):
        raise RetentionError("Artwork and header messages must not be registered for cleanup.")
    if not isinstance(acknowledgement, dict):
        raise RetentionError("Discord acknowledgement is missing.")
    message_id = _id(acknowledgement.get("message_id", acknowledgement.get("id")))
    channel_id = _id(acknowledgement.get("channel_id"))
    webhook_id = _id(acknowledgement["webhook_id"]) if "webhook_id" in acknowledgement else None
    journal = load_journal(journal_path)
    if message_id in journal["messages"]:
        old = journal["messages"][message_id]
        if (old["channel_id"] != channel_id or old["items"] != _items(items)
                or (webhook_id and old.get("webhook_id") not in (None, webhook_id))):
            raise RetentionError("Acknowledgement conflicts with an existing delivery record.")
        if webhook_id and "webhook_id" not in old:
            old["webhook_id"] = webhook_id
            _atomic_json(journal_path, journal)
        return message_id
    journal["messages"][message_id] = {
        "message_id": message_id, "channel_id": channel_id,
        "created_at": _iso(now), "purpose": purpose,
        "items": _items(items), "replacement_ids": [],
    }
    if webhook_id:
        journal["messages"][message_id]["webhook_id"] = webhook_id
    _atomic_json(journal_path, journal)
    return message_id


def write_snapshot(path, snapshot):
    """Write an ephemeral source observation, never a price-history checkpoint."""
    _validate_snapshot(snapshot)
    _atomic_json(path, snapshot)


def _validate_snapshot(snapshot):
    if (not isinstance(snapshot, dict) or snapshot.get("version") != VERSION
            or not isinstance(snapshot.get("destination"), str)
            or not isinstance(snapshot.get("coverage"), dict)
            or not isinstance(snapshot.get("items"), dict)
            or not isinstance(snapshot.get("identity"), dict)):
        raise RetentionError("Current source snapshot is invalid.")
    _time(snapshot.get("observed_at"))
    for key, item in snapshot["items"].items():
        if (not isinstance(key, str) or not key or not isinstance(item, dict)
                or not isinstance(item.get("scope"), str) or not item["scope"]
                or not isinstance(item.get("embed"), dict)):
            raise RetentionError("Current source snapshot contains an invalid item.")


def _snapshot(path, destination, now):
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise RetentionError("A fresh source snapshot is unavailable; cleanup will retry.") from None
    _validate_snapshot(value)
    if value["destination"] != destination:
        raise RetentionError("Source snapshot destination does not match this watcher.")
    age = now - _time(value["observed_at"])
    try:
        maximum = timedelta(hours=float(os.environ.get("RETENTION_SNAPSHOT_MAX_HOURS", "6")))
    except (ValueError, OverflowError):
        raise RetentionError("Invalid snapshot freshness configuration.") from None
    if maximum <= timedelta(0) or age > maximum or age < -timedelta(minutes=5):
        raise RetentionError("Source snapshot is stale; cleanup will retry after a fresh check.")
    return value


def _webhook(value):
    try:
        parts = urlsplit(value or "")
        match = re.fullmatch(r"/api(?:/v[0-9]+)?/webhooks/([1-9][0-9]*)/[A-Za-z0-9._-]+/?", parts.path)
        if (parts.scheme != "https" or parts.hostname not in
                {"discord.com", "discordapp.com", "canary.discord.com", "ptb.discord.com"}
                or parts.port not in (None, 443) or parts.username or parts.password or not match):
            raise ValueError()
    except ValueError:
        raise RetentionError("A valid Discord webhook is required for cleanup.") from None
    return urlunsplit((parts.scheme, parts.netloc, parts.path.rstrip("/"), "", "")), match.group(1)


def _request(method, url, payload=None):
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    headers = {"User-Agent": "GameDealWatcher-Retention/1.0"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    request = Request(url, data=data, headers=headers, method=method)
    try:
        with urlopen(request, timeout=30) as response:
            status = response.status
            raw = response.read(2_000_001)
    except HTTPError as error:
        return error.code, None
    except (URLError, OSError, TimeoutError):
        raise RetentionError("Discord request failed; cleanup will retry.") from None
    if len(raw) > 2_000_000:
        raise RetentionError("Discord response exceeded the allowed size.")
    if not raw:
        return status, None
    try:
        return status, json.loads(raw)
    except (ValueError, UnicodeError):
        raise RetentionError("Discord returned an invalid response.") from None


def _embed_chars(embed):
    return (len(str(embed.get("title", ""))) + len(str(embed.get("description", "")))
            + len(str(embed.get("footer", {}).get("text", "")))
            + len(str(embed.get("author", {}).get("name", "")))
            + sum(len(str(field.get("name", ""))) + len(str(field.get("value", "")))
                  for field in embed.get("fields", [])))


def _refresh_embed(item, purpose):
    embed = copy.deepcopy(item["embed"])
    label = ("Current price — refreshed after 60 days" if purpose == "overview"
             else "Still available — refreshed after 60 days")
    description = embed.get("description", "")
    if not isinstance(description, str):
        raise RetentionError("Refresh embed description is invalid.")
    embed["description"] = (description if "refreshed after 60 days" in description.split("\n", 1)[0]
                            else label + ("\n\n" + description if description else ""))
    if (len(embed["description"]) > 4096 or len(str(embed.get("title", ""))) > 256
            or len(embed.get("fields", [])) > 25 or _embed_chars(embed) > MAX_EMBED_CHARS):
        raise RetentionError("A refreshed offer exceeds Discord's message limits.")
    for field in embed.get("fields", []):
        if len(str(field.get("name", ""))) > 256 or len(str(field.get("value", ""))) > 1024:
            raise RetentionError("A refreshed offer exceeds Discord's field limits.")
    return embed


def _batches(items, purpose):
    batch, count, urls = [], 0, set()
    for key, item in items:
        embed = _refresh_embed(item, purpose)
        size, url = _embed_chars(embed), embed.get("url")
        if batch and (len(batch) >= 10 or count + size > MAX_EMBED_CHARS or (url and url in urls)):
            yield batch
            batch, count, urls = [], 0, set()
        batch.append((key, item["scope"], embed))
        count += size
        if url:
            urls.add(url)
    if batch:
        yield batch


def _covered_elsewhere(journal, old, key, scope, visible):
    old_time = _time(old["created_at"])
    replacements = set(old.get("replacement_ids", []))
    return any(
        candidate["message_id"] != old["message_id"]
        and candidate["channel_id"] == old["channel_id"]
        and (_time(candidate["created_at"]) > old_time or candidate["message_id"] in replacements)
        and any(item["key"] == key and item["scope"] == scope for item in candidate["items"])
        and visible(candidate)
        for candidate in journal["messages"].values()
    )


def _ack(value, expected_channel, expected_message=None, webhook_id=None, require_origin=False):
    if not isinstance(value, dict):
        raise RetentionError("Discord acknowledgement is missing.")
    message_id, channel_id = _id(value.get("id", value.get("message_id"))), _id(value.get("channel_id"))
    if channel_id != expected_channel or (expected_message and message_id != expected_message):
        raise RetentionError("Discord message identity or channel does not match the delivery journal.")
    if require_origin and value.get("webhook_id") is None:
        raise RetentionError("Discord message origin is missing; the recorded alert was left intact.")
    origin = _id(value["webhook_id"]) if value.get("webhook_id") is not None else None
    if webhook_id and origin is not None and origin != webhook_id:
        raise RetentionError("Discord message belongs to a different webhook.")
    result = {"message_id": message_id, "channel_id": channel_id}
    if origin:
        result["webhook_id"] = origin
    return result


def run_retention(destination, journal_path, snapshot_path, webhook_url=None, dry_run=False, now=None, request=None, legacy_webhook_url=None):
    """Refresh/delete aged acknowledged alerts; return safe counters and diagnostics.

    Source errors leave the original post and journal intact. Replacement ACKs are
    committed before DELETE so partial failures can retry without duplicate posts.
    """
    summary = {"enabled": False, "dry_run": bool(dry_run), "eligible": 0,
               "refreshed": 0, "deleted": 0, "retired": 0, "pinned": 0,
               "deferred": 0, "errors": []}
    enabled = os.environ.get("RETENTION_ENABLED", "false").lower() == "true"
    if not enabled and not dry_run:
        return summary
    summary["enabled"] = enabled
    current = _time(now)
    try:
        days = float(os.environ.get("RETENTION_DAYS", str(DEFAULT_RETENTION_DAYS)))
        if not days > 0:
            raise ValueError()
        cutoff = current - timedelta(days=days)
        journal = load_journal(journal_path)
        aged = [message for message in journal["messages"].values() if _time(message["created_at"]) <= cutoff]
        summary["eligible"] = len(aged)
        if not aged:
            return summary
        snapshot = _snapshot(snapshot_path, destination, current)
        if dry_run:
            for old in aged:
                if any(snapshot["coverage"].get(item["scope"]) is not True for item in old["items"]):
                    summary["deferred"] += 1
            return summary
        base, webhook_id = _webhook(webhook_url or os.environ.get("DISCORD_WEBHOOK_URL"))
        send = request or _request
        status, identity = send("GET", base)
        if (status != 200 or not isinstance(identity, dict) or _id(identity.get("id")) != webhook_id):
            raise RetentionError("Discord webhook identity could not be verified.")
        channel_id = _id(identity.get("channel_id"))
        routes = {webhook_id: base}
        verified = {webhook_id}
        legacy_value = legacy_webhook_url or os.environ.get("DISCORD_LEGACY_WEBHOOK_URL")
        legacy_id = journal.get("legacy_webhook_id")
        original_value = os.environ.get("DISCORD_ORIGINAL_WEBHOOK_ID", "").strip()
        original_id = _id(original_value) if original_value else None
        if legacy_value:
            legacy_base, supplied_legacy_id = _webhook(legacy_value)
            if supplied_legacy_id == webhook_id and supplied_legacy_id != original_id:
                raise RetentionError("Replacement and legacy webhooks must have different identities.")
            if legacy_id and legacy_id != supplied_legacy_id:
                raise RetentionError("Legacy webhook differs from the recorded migration origin; cleanup was deferred.")
            if original_id and supplied_legacy_id != original_id:
                raise RetentionError("Legacy webhook differs from the configured original identity; cleanup was deferred.")
            legacy_id = supplied_legacy_id
            routes[legacy_id] = legacy_base
        strict_origin = destination in ("steam", "minecraft")
    except (RetentionError, ValueError, OverflowError) as error:
        summary["errors"].append(str(error) if isinstance(error, RetentionError) else "Invalid retention configuration.")
        return summary

    def origin_for(record):
        origin = record.get("webhook_id") or legacy_id or original_id
        if not origin:
            if strict_origin:
                raise RetentionError("An older alert has no recorded origin; its legacy webhook credential is required.")
            origin = webhook_id  # Existing single-webhook Eneba/Pokemon journals remain compatible.
        if origin not in routes:
            raise RetentionError("The credential for a recorded alert's webhook is unavailable; that alert was left intact.")
        if record["channel_id"] != channel_id:
            raise RetentionError("Webhook channel differs from a recorded alert; that alert was left intact.")
        if origin not in verified:
            status, found = send("GET", routes[origin])
            if (status != 200 or not isinstance(found, dict)
                    or _id(found.get("id")) != origin or _id(found.get("channel_id")) != channel_id):
                raise RetentionError("Legacy webhook identity or channel could not be verified; old alerts were left intact.")
            verified.add(origin)
            if journal.get("legacy_webhook_id") is None:
                journal["legacy_webhook_id"] = origin
                _atomic_json(journal_path, journal)
        return routes[origin], origin

    for old in sorted(aged, key=lambda value: (_time(value["created_at"]), int(value["message_id"]))):
        try:
            message_id = old["message_id"]
            old_base, old_origin = origin_for(old)
            status, message = send("GET", old_base + "/messages/" + message_id)
            missing = status == 404
            if not missing:
                if status != 200:
                    raise RetentionError("An old Discord alert could not be checked; cleanup will retry.")
                _ack(message, channel_id, message_id, old_origin, require_origin=strict_origin or "webhook_id" in old)
                if message.get("pinned") is not False:
                    # Missing pin metadata is treated as unknown, never assumed unpinned.
                    summary["pinned"] += 1
                    continue
            if any(snapshot["coverage"].get(item["scope"]) is not True for item in old["items"]):
                summary["deferred"] += 1
                continue
            active = []
            def visible(record):
                candidate_id = record["message_id"]
                candidate_base, candidate_origin = origin_for(record)
                status, candidate = send("GET", candidate_base + "/messages/" + candidate_id)
                if status == 404:
                    return False
                if status != 200:
                    raise RetentionError("A newer alert could not be checked; the original was kept for retry.")
                _ack(candidate, channel_id, candidate_id, candidate_origin, require_origin=strict_origin or "webhook_id" in record)
                return True
            for item in old["items"]:
                observed = snapshot["items"].get(item["key"])
                if observed is not None:
                    if observed["scope"] != item["scope"]:
                        raise RetentionError("An offer changed source scope; cleanup will retry after verification.")
                    if not _covered_elsewhere(journal, old, item["key"], item["scope"], visible):
                        active.append((item["key"], observed))
            for batch in _batches(active, old["purpose"]):
                identity = snapshot["identity"]
                payload = {"allowed_mentions": {"parse": []}, "embeds": [entry[2] for entry in batch]}
                for field in ("username", "avatar_url"):
                    if identity.get(field):
                        payload[field] = identity[field]
                content = "Current offers checked again after 60 days; these are refreshed posts, not new price-drop claims."
                if identity.get("content"):
                    content += "\n\n" + str(identity["content"])
                if len(content) > 2000:
                    raise RetentionError("Refresh content exceeds Discord's message limit.")
                payload["content"] = content
                status, acknowledgement = send("POST", base + "?wait=true", payload)
                if status != 200:
                    raise RetentionError("A refresh was not acknowledged; the original alert was kept.")
                ack = _ack(acknowledgement, channel_id, webhook_id=webhook_id, require_origin=strict_origin)
                ack["webhook_id"] = webhook_id
                if ack["message_id"] in journal["messages"]:
                    raise RetentionError("Discord returned an already recorded refresh ID.")
                journal["messages"][ack["message_id"]] = {
                    **ack, "created_at": _iso(current), "purpose": old["purpose"],
                    "items": [{"key": entry[0], "scope": entry[1]} for entry in batch],
                    "replacement_ids": [],
                }
                old.setdefault("replacement_ids", []).append(ack["message_id"])
                _atomic_json(journal_path, journal)
                summary["refreshed"] += 1
            if not missing:
                status, _ = send("DELETE", old_base + "/messages/" + message_id)
                if status not in (204, 404):
                    raise RetentionError("The replacement is saved; deleting the old alert will retry without reposting.")
                if status == 204:
                    summary["deleted"] += 1
            del journal["messages"][message_id]
            _atomic_json(journal_path, journal)
            summary["retired"] += 1
        except RetentionError as error:
            summary["errors"].append(str(error))
    return summary


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--destination", required=True, choices=("steam", "eneba", "pokemon", "minecraft"))
    parser.add_argument("--journal", required=True)
    parser.add_argument("--snapshot", required=True)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    summary = run_retention(args.destination, args.journal, args.snapshot, dry_run=args.dry_run)
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    return 1 if summary["errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
