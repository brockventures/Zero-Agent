#!/usr/bin/env python3
"""Deterministic Seerr -> Discord #media-requests webhook card auto-sync.

Scans recent Seerr webhook cards in Discord #media-requests whose Request Status
is still active ('Processing', 'Pending Approval', or 'Partially Available'),
queries live request/media/season status from Seerr, and edits the webhook
cards in-place via Discord's Edit Webhook Message API.
"""

from __future__ import annotations

import argparse
import copy
import json
import logging
import os
import re
import sys
import time
import unicodedata
import urllib.parse
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import requests

logger = logging.getLogger(__name__)

for _path in [str(Path(__file__).resolve().parent.parent), str(Path(__file__).resolve().parent)]:
    if _path not in sys.path:
        sys.path.insert(0, _path)

try:
    from tools.deliver_image import get_discord_token
except ImportError:
    from deliver_image import get_discord_token

DEFAULT_CHANNEL_ID = 1453427860793463000
DEFAULT_WEBHOOK_ID = "1453553876241350687"
DISCORD_API_BASE = "https://discord.com/api/v10"
PT = ZoneInfo("America/Los_Angeles")

SYNCABLE_STATUSES = frozenset({
    "processing",
    "pending approval",
    "partially available",
})

READONLY_EMBED_KEYS = frozenset({
    "type",
    "proxy_url",
    "width",
    "height",
    "placeholder",
    "placeholder_version",
    "flags",
    "content_type",
})

COLOR_AVAILABLE = 3066993
COLOR_PARTIAL = 15965202
COLOR_PROCESSING = 10181046
COLOR_FAILED = 15158332

_TITLE_PREFIX_RE = re.compile(
    r"^(movie|series|tv)\s+request(?:\s+[^:]+)?:\s*(.+)$",
    re.IGNORECASE,
)
_TRAILING_YEAR_RE = re.compile(r"^(.*?)\s*\((\d{4})\)\s*$")


def get_seerr_config(env_path: str | Path = "/secrets/env.json") -> tuple[str, str]:
    """Resolve SEERR_BASE_URL and SEERR_API_KEY from environment or secrets file."""
    base_url = (os.environ.get("SEERR_BASE_URL") or "").strip()
    api_key = (os.environ.get("SEERR_API_KEY") or "").strip()

    secrets_file = Path(env_path)
    if (not base_url or not api_key) and secrets_file.exists():
        try:
            data = json.loads(secrets_file.read_text(encoding="utf-8"))
            if not base_url and data.get("SEERR_BASE_URL"):
                base_url = str(data["SEERR_BASE_URL"]).strip()
            if not api_key and data.get("SEERR_API_KEY"):
                api_key = str(data["SEERR_API_KEY"]).strip()
        except (OSError, json.JSONDecodeError, TypeError) as exc:
            logger.warning("Failed reading %s: %s", secrets_file, exc)

    if not base_url or not api_key:
        raise RuntimeError(
            "Seerr configuration missing: set SEERR_BASE_URL and SEERR_API_KEY "
            "in environment or /secrets/env.json"
        )

    return base_url.rstrip("/"), api_key


def _normalize_title(text: str) -> str:
    ascii_text = (
        unicodedata.normalize("NFKD", text or "")
        .encode("ascii", "ignore")
        .decode("ascii")
    )
    return re.sub(r"[^a-z0-9]+", " ", ascii_text.lower()).strip()


def _normalize_poster_path(poster: str | None) -> str:
    if not poster:
        return ""
    clean = str(poster).strip()
    if not clean:
        return ""
    if "://" in clean:
        clean = urllib.parse.urlparse(clean).path
    seg = clean.rstrip("/").rsplit("/", 1)[-1]
    return f"/{seg}" if seg else ""


def _parse_iso_ts(ts_str: str | None) -> float | None:
    if not ts_str or not isinstance(ts_str, str):
        return None
    try:
        return datetime.fromisoformat(ts_str).timestamp()
    except ValueError:
        return None


def _strip_readonly_keys(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {
            k: _strip_readonly_keys(v)
            for k, v in obj.items()
            if k not in READONLY_EMBED_KEYS
        }
    if isinstance(obj, list):
        return [_strip_readonly_keys(item) for item in obj]
    return obj


def parse_seerr_embed(embed: dict[str, Any], message_ts: str = "") -> dict[str, Any] | None:
    """Parse a Seerr Discord webhook embed into structured card metadata."""
    if not isinstance(embed, dict):
        return None

    raw_title = (embed.get("title") or "").strip()
    if not raw_title:
        return None

    prefix_match = _TITLE_PREFIX_RE.match(raw_title)
    if not prefix_match:
        return None

    kind = prefix_match.group(1).lower()
    media_type = "movie" if kind == "movie" else "tv"
    title_body = prefix_match.group(2).strip()

    year: int | None = None
    year_match = _TRAILING_YEAR_RE.match(title_body)
    if year_match:
        title = year_match.group(1).strip()
        year = int(year_match.group(2))
    else:
        title = title_body

    fields_map: dict[str, str] = {}
    for field in embed.get("fields") or []:
        if isinstance(field, dict) and field.get("name"):
            key = str(field["name"]).strip().lower()
            fields_map[key] = str(field.get("value") or "").strip()

    requested_by = fields_map.get("requested by", "")
    current_status = fields_map.get("request status", "")

    seasons_raw = fields_map.get("requested seasons", "")
    requested_seasons = {int(num) for num in re.findall(r"\d+", seasons_raw)}

    thumb = embed.get("thumbnail") or {}
    thumb_url = thumb.get("url") if isinstance(thumb, dict) else ""
    poster_path = _normalize_poster_path(thumb_url)

    return {
        "media_type": media_type,
        "title": title,
        "year": year,
        "requested_by": requested_by,
        "current_status": current_status,
        "requested_seasons": requested_seasons,
        "poster_path": poster_path,
        "message_ts": message_ts,
    }


def resolve_seerr_request_status(
    req: dict[str, Any],
    detail: dict[str, Any] | None = None,
    requested_seasons: set[int] | None = None,
) -> tuple[str, int, str]:
    """Resolve live Seerr request/media/season status to (status_label, color, footer_suffix)."""
    req_status = req.get("status")
    if req_status == 3:
        return ("Declined", COLOR_FAILED, "Declined")
    if req_status == 4:
        return ("Failed", COLOR_FAILED, "Failed")

    media = req.get("media") or {}
    detail_media_info = (detail or {}).get("mediaInfo") or {}
    media_status = media.get("status")
    if media_status is None:
        media_status = detail_media_info.get("status")

    if requested_seasons:
        if req_status == 5:
            return ("Available", COLOR_AVAILABLE, "Available on Plex")

        season_status_map: dict[int, int] = {}
        for entry in req.get("seasons") or []:
            if isinstance(entry, dict) and entry.get("seasonNumber") is not None and entry.get("status") is not None:
                season_status_map[int(entry["seasonNumber"])] = int(entry["status"])

        for entry in detail_media_info.get("seasons") or []:
            if isinstance(entry, dict) and entry.get("seasonNumber") is not None and entry.get("status") is not None:
                sn = int(entry["seasonNumber"])
                st = int(entry["status"])
                if season_status_map.get(sn) != 5:
                    season_status_map[sn] = st

        matched_statuses = [season_status_map[sn] for sn in requested_seasons if sn in season_status_map]
        if matched_statuses:
            if len(matched_statuses) == len(requested_seasons) and all(st == 5 for st in matched_statuses):
                return ("Available", COLOR_AVAILABLE, "Available on Plex")
            if any(st in (4, 5) for st in matched_statuses):
                return ("Partially Available", COLOR_PARTIAL, "Partially Available on Plex")
            if req_status == 1:
                return ("Pending Approval", COLOR_PROCESSING, "Pending Approval")
            return ("Processing", COLOR_PROCESSING, "Processing")

    if req_status == 5 or media_status == 5:
        return ("Available", COLOR_AVAILABLE, "Available on Plex")
    if media_status == 4:
        return ("Partially Available", COLOR_PARTIAL, "Partially Available on Plex")
    if req_status == 1:
        return ("Pending Approval", COLOR_PROCESSING, "Pending Approval")
    return ("Processing", COLOR_PROCESSING, "Processing")


def _extract_candidate(cand: Any) -> dict[str, Any]:
    """Normalize a candidate entry (raw request, (req, detail) tuple, or {request, detail})."""
    if isinstance(cand, tuple) and len(cand) == 2:
        req, detail = cand
    elif isinstance(cand, dict) and isinstance(cand.get("request"), dict):
        req = cand["request"]
        detail = cand.get("detail")
    elif isinstance(cand, dict):
        req = cand
        detail = cand.get("detail") or cand.get("_detail")
    else:
        return {}

    media = req.get("media") or {}
    detail = detail or {}
    media_type = str(media.get("mediaType") or req.get("type") or cand.get("media_type") if isinstance(cand, dict) else media.get("mediaType") or req.get("type") or "").strip().lower()
    if media_type == "series":
        media_type = "tv"

    poster_raw = detail.get("posterPath") or media.get("posterPath") or (cand.get("posterPath") if isinstance(cand, dict) else "")
    poster_path = _normalize_poster_path(poster_raw)

    title = (
        detail.get("title")
        or detail.get("name")
        or media.get("title")
        or media.get("name")
        or (cand.get("title") if isinstance(cand, dict) else "")
        or ""
    )
    date_str = (
        detail.get("releaseDate")
        or detail.get("firstAirDate")
        or media.get("releaseDate")
        or media.get("firstAirDate")
        or ""
    )
    year: int | None = None
    if isinstance(date_str, str) and len(date_str) >= 4 and date_str[:4].isdigit():
        year = int(date_str[:4])

    req_by = req.get("requestedBy") or {}
    requesters: set[str] = set()
    if isinstance(req_by, dict):
        for key in ("displayName", "plexUsername", "username", "jellyfinUsername"):
            val = req_by.get(key)
            if val:
                requesters.add(str(val).strip().lower())

    seasons = {
        int(s["seasonNumber"])
        for s in (req.get("seasons") or [])
        if isinstance(s, dict) and s.get("seasonNumber") is not None
    }

    return {
        "raw": cand,
        "media_type": media_type,
        "poster_path": poster_path.lower(),
        "norm_title": _normalize_title(str(title)),
        "year": year,
        "requesters": requesters,
        "created_ts": _parse_iso_ts(req.get("createdAt")),
        "seasons": seasons,
    }


def _rank_candidate(
    extracted: dict[str, Any],
    card_seasons: set[int],
    card_ts: float | None,
) -> tuple[int, int, float]:
    """Rank tie-breakers: exact season match > season overlap > timestamp proximity."""
    cand_seasons = extracted["seasons"]
    if card_seasons and cand_seasons:
        exact = 1 if cand_seasons == card_seasons else 0
        overlap = len(card_seasons & cand_seasons)
    else:
        exact = 0
        overlap = 0

    cand_ts = extracted["created_ts"]
    if card_ts is not None and cand_ts is not None:
        proximity = -abs(cand_ts - card_ts)
    else:
        proximity = float("-inf")

    return (exact, overlap, proximity)


def match_card_to_request(card: dict[str, Any], candidates: list[Any]) -> Any | None:
    """Match a parsed Discord Seerr card to its corresponding Seerr request candidate."""
    if not card or not candidates:
        return None

    card_type = str(card.get("media_type") or "").strip().lower()
    card_poster = str(card.get("poster_path") or "").strip().lower()
    card_norm_title = _normalize_title(str(card.get("title") or ""))
    card_year = card.get("year")
    card_requester = str(card.get("requested_by") or "").strip().lower()
    card_seasons = set(card.get("requested_seasons") or set())
    card_ts = _parse_iso_ts(card.get("message_ts"))

    extracted_list = [_extract_candidate(c) for c in candidates]
    extracted_list = [e for e in extracted_list if e and e["media_type"] == card_type]
    if not extracted_list:
        return None

    def _requester_matches(ex: dict[str, Any]) -> bool:
        if not card_requester or not ex["requesters"]:
            return True
        return card_requester in ex["requesters"]

    # Pass 1: exact poster_path + media_type (+ requester)
    if card_poster:
        poster_matches = [
            ex for ex in extracted_list
            if ex["poster_path"] and ex["poster_path"] == card_poster and _requester_matches(ex)
        ]
        if poster_matches:
            best = max(poster_matches, key=lambda ex: _rank_candidate(ex, card_seasons, card_ts))
            return best["raw"]

    # Pass 2: normalized title + media_type + year + requester + timestamp proximity
    if card_norm_title:
        title_matches = []
        for ex in extracted_list:
            if ex["norm_title"] != card_norm_title:
                continue
            if card_year is not None and ex["year"] is not None and ex["year"] != card_year:
                continue
            if not _requester_matches(ex):
                continue
            title_matches.append(ex)

        if title_matches:
            best = max(title_matches, key=lambda ex: _rank_candidate(ex, card_seasons, card_ts))
            return best["raw"]

    return None


def build_updated_embed(
    orig_embed: dict[str, Any],
    new_status: str,
    new_color: int,
    footer_suffix: str,
    now_pt: datetime | None = None,
) -> dict[str, Any]:
    """Return a clean deep-copied Discord embed updated with live Seerr status and PT footer."""
    embed = copy.deepcopy(orig_embed)

    fields = embed.get("fields")
    if not isinstance(fields, list):
        fields = []
        embed["fields"] = fields

    updated_field = False
    for field in fields:
        if isinstance(field, dict) and str(field.get("name") or "").strip().lower() == "request status":
            field["value"] = new_status
            updated_field = True
            break

    if not updated_field:
        fields.append({"name": "Request Status", "value": new_status, "inline": True})

    embed["color"] = new_color

    if now_pt is None:
        dt_pt = datetime.now(PT)
    elif now_pt.tzinfo is None:
        dt_pt = now_pt.replace(tzinfo=PT)
    else:
        dt_pt = now_pt.astimezone(PT)

    pt_str = dt_pt.strftime("%Y-%m-%d %I:%M %p PT")
    embed["footer"] = {"text": f"Status synced via Seerr • {footer_suffix} ({pt_str})"}

    return _strip_readonly_keys(embed)


def _fetch_discord_messages(
    http_client: Any,
    discord_token: str,
    channel_id: int | str,
    limit: int,
) -> list[dict[str, Any]]:
    headers = {"Authorization": f"Bot {discord_token}"}
    messages: list[dict[str, Any]] = []
    remaining = max(1, int(limit))
    before: str | None = None

    while remaining > 0:
        batch_size = min(remaining, 100)
        params: dict[str, Any] = {"limit": batch_size}
        if before:
            params["before"] = before

        resp = http_client.get(
            f"{DISCORD_API_BASE}/channels/{channel_id}/messages",
            headers=headers,
            params=params,
            timeout=15,
        )
        resp.raise_for_status()
        batch = resp.json()
        if not isinstance(batch, list) or not batch:
            break

        messages.extend(batch)
        remaining -= len(batch)
        if len(batch) < batch_size:
            break
        before = batch[-1].get("id")
        if not before:
            break

    return messages


def _fetch_webhook_tokens(
    http_client: Any,
    discord_token: str,
    channel_id: int | str,
) -> dict[str, str]:
    resp = http_client.get(
        f"{DISCORD_API_BASE}/channels/{channel_id}/webhooks",
        headers={"Authorization": f"Bot {discord_token}"},
        timeout=15,
    )
    resp.raise_for_status()
    webhooks = resp.json()
    tokens: dict[str, str] = {}
    if isinstance(webhooks, list):
        for wh in webhooks:
            if isinstance(wh, dict) and wh.get("id") and wh.get("token"):
                tokens[str(wh["id"])] = str(wh["token"])
    return tokens


def _fetch_seerr_requests(
    http_client: Any,
    seerr_base_url: str,
    seerr_api_key: str,
) -> list[dict[str, Any]]:
    headers = {"X-Api-Key": seerr_api_key}
    all_requests: list[dict[str, Any]] = []

    for page_idx in range(2):
        skip = page_idx * 100
        resp = http_client.get(
            f"{seerr_base_url}/api/v1/request",
            headers=headers,
            params={"take": 100, "skip": skip, "sort": "added"},
            timeout=15,
        )
        resp.raise_for_status()
        payload = resp.json()
        results = payload.get("results") if isinstance(payload, dict) else payload
        if not isinstance(results, list) or not results:
            break

        all_requests.extend(results)
        page_info = payload.get("pageInfo") if isinstance(payload, dict) else None
        total_pages = page_info.get("pages") if isinstance(page_info, dict) else None
        if len(results) < 100 or (total_pages is not None and page_idx + 1 >= int(total_pages)):
            break

    return all_requests


def _fetch_seerr_detail_memoized(
    http_client: Any,
    seerr_base_url: str,
    seerr_api_key: str,
    media_type: str,
    tmdb_id: int | str,
    cache: dict[tuple[str, str], dict[str, Any] | None],
) -> dict[str, Any] | None:
    key = (media_type, str(tmdb_id))
    if key in cache:
        return cache[key]

    try:
        resp = http_client.get(
            f"{seerr_base_url}/api/v1/{media_type}/{tmdb_id}",
            headers={"X-Api-Key": seerr_api_key},
            timeout=15,
        )
        resp.raise_for_status()
        detail = resp.json()
        cache[key] = detail if isinstance(detail, dict) else None
    except (requests.RequestException, RuntimeError, ValueError, TypeError) as exc:
        logger.warning("Failed fetching Seerr detail %s/%s: %s", media_type, tmdb_id, exc)
        cache[key] = None

    return cache[key]


def sync_seerr_cards(
    channel_id: int | str = DEFAULT_CHANNEL_ID,
    limit: int = 100,
    dry_run: bool = False,
    http_client: Any = None,
) -> dict[str, Any]:
    """Scan Discord webhook cards in #media-requests and sync live statuses from Seerr."""
    own_session = http_client is None
    if own_session:
        http_client = requests.Session()
        if os.environ.get("SEERR_VERIFY_SSL", "").strip().lower() in ("0", "false", "no"):
            http_client.verify = False

    try:
        discord_token = get_discord_token()
        messages = _fetch_discord_messages(http_client, discord_token, channel_id, limit)

        candidate_items: list[dict[str, Any]] = []
        for msg in messages:
            if not isinstance(msg, dict) or not msg.get("webhook_id"):
                continue
            embeds = msg.get("embeds") or []
            if not isinstance(embeds, list):
                continue

            for idx, embed in enumerate(embeds):
                parsed = parse_seerr_embed(embed, message_ts=str(msg.get("timestamp") or ""))
                if not parsed:
                    continue
                if parsed["current_status"].strip().lower() not in SYNCABLE_STATUSES:
                    continue
                candidate_items.append({
                    "message": msg,
                    "embed_index": idx,
                    "embed": embed,
                    "card": parsed,
                })

        summary: dict[str, Any] = {
            "scanned": len(messages),
            "candidates": len(candidate_items),
            "updated": 0,
            "skipped": 0,
            "errors": 0,
            "changes": [],
        }

        if not candidate_items:
            return summary

        seerr_base_url, seerr_api_key = get_seerr_config()
        requests_list = _fetch_seerr_requests(http_client, seerr_base_url, seerr_api_key)

        needed_types = {item["card"]["media_type"] for item in candidate_items}
        detail_cache: dict[tuple[str, str], dict[str, Any] | None] = {}
        enriched_candidates: list[dict[str, Any]] = []

        for req in requests_list:
            if not isinstance(req, dict):
                continue
            media = req.get("media") or {}
            media_type = str(media.get("mediaType") or req.get("type") or "").strip().lower()
            if media_type == "series":
                media_type = "tv"
            if media_type not in needed_types:
                continue

            tmdb_id = media.get("tmdbId")
            detail: dict[str, Any] | None = None
            if tmdb_id is not None:
                detail = _fetch_seerr_detail_memoized(
                    http_client,
                    seerr_base_url,
                    seerr_api_key,
                    media_type,
                    tmdb_id,
                    detail_cache,
                )

            enriched_candidates.append({"request": req, "detail": detail})

        webhook_tokens: dict[str, str] = {}
        if not dry_run:
            webhook_tokens = _fetch_webhook_tokens(http_client, discord_token, channel_id)

        for item in candidate_items:
            card = item["card"]
            msg = item["message"]
            matched = match_card_to_request(card, enriched_candidates)
            if not matched:
                summary["skipped"] += 1
                continue

            req = matched["request"]
            detail = matched["detail"]
            new_status, new_color, footer_suffix = resolve_seerr_request_status(
                req,
                detail=detail,
                requested_seasons=card["requested_seasons"],
            )

            if new_status.strip().lower() == card["current_status"].strip().lower():
                summary["skipped"] += 1
                continue

            updated_embed = build_updated_embed(
                item["embed"],
                new_status=new_status,
                new_color=new_color,
                footer_suffix=footer_suffix,
            )

            if not dry_run:
                wh_id = str(msg.get("webhook_id") or "")
                wh_token = webhook_tokens.get(wh_id)
                if not wh_token:
                    summary["errors"] += 1
                    continue

                payload_embeds = [
                    updated_embed if i == item["embed_index"] else _strip_readonly_keys(copy.deepcopy(e))
                    for i, e in enumerate(msg.get("embeds") or [])
                ]

                try:
                    patch_resp = http_client.patch(
                        f"{DISCORD_API_BASE}/webhooks/{wh_id}/{wh_token}/messages/{msg['id']}",
                        headers={"Content-Type": "application/json"},
                        json={"embeds": payload_embeds},
                        timeout=15,
                    )
                    patch_resp.raise_for_status()
                    time.sleep(0.4)
                except (requests.RequestException, RuntimeError, ValueError) as exc:
                    logger.warning("Failed patching Discord message %s: %s", msg.get("id"), exc)
                    summary["errors"] += 1
                    continue

            summary["updated"] += 1
            summary["changes"].append({
                "message_id": str(msg.get("id") or ""),
                "request_id": req.get("id"),
                "title": card["title"],
                "media_type": card["media_type"],
                "old_status": card["current_status"],
                "new_status": new_status,
                "dry_run": dry_run,
            })

        return summary
    finally:
        if own_session and hasattr(http_client, "close"):
            http_client.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Sync Seerr webhook embed cards in #media-requests with live Seerr status."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Inspect and compute updates without editing Discord webhook messages.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=100,
        help="Maximum number of recent Discord messages to scan (default: 100).",
    )
    parser.add_argument(
        "--channel-id",
        type=int,
        default=DEFAULT_CHANNEL_ID,
        help=f"Target Discord channel ID (default: {DEFAULT_CHANNEL_ID}).",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output summary as JSON.",
    )
    args = parser.parse_args(argv)

    result = sync_seerr_cards(
        channel_id=args.channel_id,
        limit=args.limit,
        dry_run=args.dry_run,
    )

    if args.json:
        print(json.dumps(result, indent=2))
    else:
        mode = "DRY-RUN" if args.dry_run else "LIVE"
        print(
            f"[seerr_card_sync:{mode}] scanned={result['scanned']} "
            f"candidates={result['candidates']} updated={result['updated']} "
            f"skipped={result['skipped']} errors={result['errors']}"
        )
        for ch in result["changes"]:
            print(
                f"  - {ch['title']} ({ch['media_type']}): "
                f"{ch['old_status']} -> {ch['new_status']} [msg {ch['message_id']}]"
            )

    return 0 if result["errors"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
