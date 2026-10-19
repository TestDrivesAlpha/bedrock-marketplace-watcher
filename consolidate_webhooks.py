"""Copy only fixed, verified historical alerts to the current webhook identity.

Read-only unless --apply is supplied. Never deletes a webhook or changes sale
history. Verified original messages are backed up in both modes. A durable
replacement checkpoint precedes every source-message deletion, allowing retries.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import re
from pathlib import Path

from discord_retention import (RetentionError, _ack, _atomic_json, _embed_chars,
                               _id, _request, _time, _webhook, load_journal)


DESTINATIONS = {
    "minecraft": {
        "old": "1557461917742669976", "current": "1561627676526247938",
        "channel": "1557423972633808996", "journal": ".discord-posts-minecraft.json",
        "messages": {"1560340113769103411", "1561039288831836292"},
    },
    "steam": {
        "old": "1557498149772333176", "current": "1561635443630411837",
        "channel": "1557498100417953834", "journal": ".discord-posts-steam.json",
        "messages": {"1557503332027998319"},
    },
}
WEBHOOK_LINK = re.compile(r"https?://[^\s\"<>]*discord(?:app)?\.com/[^\s\"<>]*webhooks/", re.I)
EMBED_KEYS = {"title", "description", "url", "timestamp", "color", "fields", "footer", "image", "thumbnail", "author"}
EMBED_CHILDREN = {
    "footer": {"text", "icon_url"}, "image": {"url"}, "thumbnail": {"url"},
    "author": {"name", "url", "icon_url"},
}


def _call(request, method, url, payload=None):
    try:
        result = request(method, url, payload)
    except Exception:
        raise RetentionError("Discord request failed; no unverified source was removed.") from None
    if not isinstance(result, tuple) or len(result) != 2 or not isinstance(result[0], int):
        raise RetentionError("Discord returned an invalid HTTP result.")
    return result


def _no_secrets(value, tokens):
    encoded = json.dumps(value, ensure_ascii=False)
    if any(token and token in encoded for token in tokens) or WEBHOOK_LINK.search(encoded):
        raise RetentionError("A message contains credential-like data; it was not archived or copied.")
    def inspect(item):
        if isinstance(item, dict):
            for key, child in item.items():
                if key.lower() in {"token", "access_token", "refresh_token", "authorization", "webhook_url"} and child:
                    raise RetentionError("A message contains credential-like data; it was not archived or copied.")
                inspect(child)
        elif isinstance(item, list):
            for child in item:
                inspect(child)
    inspect(value)


def _embeds(message):
    original = message.get("embeds", [])
    if not isinstance(original, list) or len(original) > 10:
        raise RetentionError("Original embeds cannot be copied safely.")
    cleaned = []
    for source in original:
        if not isinstance(source, dict):
            raise RetentionError("Original embeds cannot be copied safely.")
        embed = {key: copy.deepcopy(value) for key, value in source.items() if key in EMBED_KEYS}
        for key, allowed in EMBED_CHILDREN.items():
            if key in embed:
                if not isinstance(embed[key], dict):
                    raise RetentionError("Original embed structure is unsupported.")
                embed[key] = {name: value for name, value in embed[key].items() if name in allowed}
        if (len(str(embed.get("title", ""))) > 256 or len(str(embed.get("description", ""))) > 4096
                or not isinstance(embed.get("fields", []), list) or len(embed.get("fields", [])) > 25):
            raise RetentionError("Original embed exceeds Discord limits.")
        if "fields" in embed:
            if any(not isinstance(field, dict) or len(str(field.get("name", ""))) > 256
                   or len(str(field.get("value", ""))) > 1024 for field in embed["fields"]):
                raise RetentionError("Original embed fields exceed Discord limits.")
            embed["fields"] = [{key: value for key, value in field.items() if key in {"name", "value", "inline"}}
                               for field in embed["fields"]]
        if len(str(embed.get("footer", {}).get("text", ""))) > 2048 or len(str(embed.get("author", {}).get("name", ""))) > 256:
            raise RetentionError("Original embed exceeds Discord limits.")
        cleaned.append(embed)
    if sum(_embed_chars(embed) for embed in cleaned) > 6000:
        raise RetentionError("Original message exceeds Discord's total embed limit.")
    return cleaned


def _message(value, config, message_id, origin, tokens, *, require_unpinned):
    ack = _ack(value, config["channel"], message_id, origin, require_origin=True)
    if not isinstance(value.get("author"), dict) or value["author"].get("id") != origin:
        raise RetentionError("Message author does not match its verified webhook.")
    if require_unpinned and value.get("pinned") is not False:
        raise RetentionError("A source is pinned or its pin status is unknown; no messages were changed.")
    if value.get("attachments") != [] or any(value.get(key) for key in ("components", "poll", "sticker_items")):
        raise RetentionError("A message has unsupported attachments or interactive content; no messages were changed.")
    if not isinstance(value.get("content"), str):
        raise RetentionError("Message content is unreadable.")
    _no_secrets(value, tokens)
    return ack


def _archive_payload(message, record):
    posted = _time(record["created_at"]).isoformat().replace("+00:00", "Z")
    prefix = (f"**Archived alert — originally posted {posted}**\n"
              "Historical copy, not a new sale; prices/details from original alert.\n\n")
    content = prefix + message["content"]
    if len(content) > 2000:
        raise RetentionError("The historical label would exceed Discord's content limit.")
    return {"content": content, "embeds": _embeds(message), "allowed_mentions": {"parse": []}}


def _digest_payload(value):
    material = {"content": value["content"], "embeds": _embeds(value)}
    return hashlib.sha256(json.dumps(material, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _old_records(journal, config):
    if journal.get("legacy_webhook_id", config["old"]) != config["old"]:
        raise RetentionError("Journal legacy identity does not match the fixed destination.")
    old = []
    for message_id, record in journal["messages"].items():
        if record["channel_id"] != config["channel"]:
            raise RetentionError("Journal contains a different channel; nothing was changed.")
        origin = record.get("webhook_id", config["old"])
        if origin not in {config["old"], config["current"]}:
            raise RetentionError("Journal contains an unknown webhook origin; nothing was changed.")
        if origin == config["old"]:
            if message_id not in config["messages"]:
                raise RetentionError("Journal contains an unexpected old or untagged message; nothing was changed.")
            old.append((message_id, record))
    return sorted(old)


def consolidate(destination, journal_path, backup_dir, *, current_url=None, legacy_url=None,
                dry_run=True, request=None, receipt=None):
    if destination not in DESTINATIONS:
        raise RetentionError("Only the fixed Minecraft and Steam destinations are supported.")
    config = DESTINATIONS[destination]
    receipt = receipt if receipt is not None else {}
    receipt.update(destination=destination, dry_run=dry_run, planned=0, backed_up=0,
                   post_attempts=0, posted=0, reused=0, deleted=0, already_absent=0,
                   remaining_old_origin=0, source_ids=[], replacement_ids=[])
    current, current_id = _webhook(current_url or os.environ.get("DISCORD_WEBHOOK_URL"))
    legacy, legacy_id = _webhook(legacy_url or os.environ.get("DISCORD_LEGACY_WEBHOOK_URL"))
    if current_id != config["current"] or legacy_id != config["old"]:
        raise RetentionError("Webhook IDs do not match the fixed destination; nothing was changed.")
    tokens = [current.rsplit("/", 1)[-1], legacy.rsplit("/", 1)[-1]]
    request = request or _request
    for base, expected in ((legacy, config["old"]), (current, config["current"])):
        status, identity = _call(request, "GET", base)
        # Identity bodies may contain tokens: never persist or return them.
        if status != 200 or not isinstance(identity, dict) or identity.get("id") != expected or identity.get("channel_id") != config["channel"]:
            raise RetentionError("Webhook identity or destination could not be verified; nothing was changed.")
    journal = load_journal(journal_path)
    old_records = _old_records(journal, config)
    receipt.update(planned=len(old_records), remaining_old_origin=len(old_records), source_ids=[key for key, _ in old_records])
    plan = []
    # Complete every source/replacement check before the first POST or DELETE.
    for source_id, record in old_records:
        replacements = record.get("replacement_ids", [])
        if not isinstance(replacements, list) or len(replacements) > 1:
            raise RetentionError("Ambiguous replacement checkpoints; nothing was changed.")
        replacement_id = replacements[0] if replacements else None
        replacement = None
        if replacement_id:
            _id(replacement_id)
            candidate = journal["messages"].get(replacement_id)
            if (not candidate or candidate.get("webhook_id") != config["current"]
                    or candidate.get("archived_from") != source_id
                    or not re.fullmatch(r"[0-9a-f]{64}", candidate.get("archive_sha256", ""))
                    or any(candidate.get(key) != record.get(key) for key in ("created_at", "items", "purpose"))):
                raise RetentionError("Historical replacement checkpoint is invalid; nothing was changed.")
            status, replacement = _call(request, "GET", current + "/messages/" + replacement_id)
            if status != 200:
                raise RetentionError("Historical replacement could not be verified; nothing was changed.")
            _message(replacement, config, replacement_id, config["current"], tokens, require_unpinned=False)
            if _digest_payload(replacement) != candidate["archive_sha256"]:
                raise RetentionError("Historical replacement differs from its checkpoint; nothing was changed.")
        status, original = _call(request, "GET", legacy + "/messages/" + source_id)
        if status == 200:
            _message(original, config, source_id, config["old"], tokens, require_unpinned=True)
            if not isinstance(original.get("timestamp"), str) or _time(original["timestamp"]) != _time(record["created_at"]):
                raise RetentionError("Original timestamp differs from its journal; nothing was changed.")
            payload = _archive_payload(original, record)
            if replacement_id and _digest_payload(payload) != journal["messages"][replacement_id]["archive_sha256"]:
                raise RetentionError("Original and historical replacement do not match; nothing was changed.")
        elif status == 404 and replacement_id:
            # A prior DELETE may have succeeded before its final journal save.
            # The durable, verified replacement is required to recover safely.
            original, payload = None, None
        else:
            raise RetentionError("Original alert could not be verified; nothing was changed.")
        plan.append((source_id, record, original, payload, replacement_id))
    for source_id, _, original, _, replacement_id in plan:
        if original is not None:
            _atomic_json(Path(backup_dir) / f"{destination}-{source_id}.json", original)
            receipt["backed_up"] += 1
        else:
            _atomic_json(Path(backup_dir) / f"{destination}-{source_id}-already-absent.json",
                         {"source_id": source_id, "replacement_id": replacement_id, "replacement_verified": True, "source_already_absent": True})
    if dry_run:
        return receipt
    for source_id, record, original, payload, replacement_id in plan:
        if replacement_id:
            receipt["reused"] += 1
        else:
            receipt["post_attempts"] += 1
            status, acknowledgment = _call(request, "POST", current + "?wait=true", payload)
            if status != 200:
                raise RetentionError("Historical copy was not acknowledged; its original remains intact.")
            ack = _ack(acknowledgment, config["channel"], webhook_id=config["current"], require_origin=True)
            replacement_id = ack["message_id"]
            _message(acknowledgment, config, replacement_id, config["current"], tokens, require_unpinned=True)
            digest = _digest_payload(payload)
            if replacement_id in journal["messages"] or _digest_payload(acknowledgment) != digest:
                raise RetentionError("Historical copy acknowledgment does not match; its original remains intact.")
            copied = copy.deepcopy(record)
            copied.update(message_id=replacement_id, webhook_id=config["current"], replacement_ids=[],
                          archived_from=source_id, archive_sha256=digest)
            journal["messages"][replacement_id] = copied
            journal["messages"][source_id]["replacement_ids"] = [replacement_id]
            _atomic_json(journal_path, journal)
            receipt["posted"] += 1
        receipt["replacement_ids"].append(replacement_id)
        if original is not None:
            status, latest = _call(request, "GET", legacy + "/messages/" + source_id)
            if status == 200:
                _message(latest, config, source_id, config["old"], tokens, require_unpinned=True)
                if _digest_payload(_archive_payload(latest, record)) != journal["messages"][replacement_id]["archive_sha256"]:
                    raise RetentionError("Original alert changed after copying; its source was left intact.")
            elif status != 404:
                raise RetentionError("Original alert could not be rechecked; its verified replacement is checkpointed.")
            status, _ = _call(request, "DELETE", legacy + "/messages/" + source_id)
            if status not in (204, 404):
                raise RetentionError("Original removal failed; its verified replacement is checkpointed for retry.")
            receipt["deleted"] += 1
        else:
            receipt["already_absent"] += 1
        del journal["messages"][source_id]
        _atomic_json(journal_path, journal)
        receipt["remaining_old_origin"] -= 1
    return receipt


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--destination", choices=tuple(DESTINATIONS), required=True)
    parser.add_argument("--journal")
    parser.add_argument("--backup-dir", default="webhook-consolidation-backup")
    parser.add_argument("--apply", action="store_true", help="Copy and remove only verified allowlisted historical messages")
    args = parser.parse_args(argv)
    receipt = {}
    try:
        consolidate(args.destination, args.journal or DESTINATIONS[args.destination]["journal"], args.backup_dir,
                    dry_run=not args.apply, receipt=receipt)
    except RetentionError as error:
        print(json.dumps(dict(receipt, success=False, error=str(error)), sort_keys=True))
        return 1
    except Exception:
        print(json.dumps(dict(receipt, success=False, error="Consolidation stopped; inspect the saved checkpoints before retrying."), sort_keys=True))
        return 1
    print(json.dumps(dict(receipt, success=True), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
