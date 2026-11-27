"""Deploy 3D floorplan per-room & whole-house Cast speaker music badges and tighter 3-floor stack to Home Assistant.

Steps executed in full deploy mode:
1. Fetch live brock-house-cycles-card.js from Host 1 and back it up locally to
   /workspace/scratch/brock-house-cycles-card.room-music.backup.js.
2. Idempotently patch brock-house-cycles-card.js with:
   A) Replacement of dead 3F apolosign_music with active-only per-room Cast speaker
      Now-Playing badges (kind: "music") across all 3 floors:
      - 3F: kitchen_music, rosie_music, isaac_music
      - 2F: living_music (fallback media_player.kitchen_hub), bedroom_music
      - 1F: office_music (fallback media_player.office_hub)
   B) kind === "music" support in _evalRunningItem(ri), runningItems HTML template
      (<img class="run-thumb">), _resolveRunningItemThumb, _applyRunningItemThumb,
      and TV_COVER_ART_UPDATE loop, with suppression when a multi-speaker group is active.
   C) Floating Multi-Speaker Group badge (/* ROOM_MUSIC_GROUP_HTML_BEGIN */ .. /* ROOM_MUSIC_GROUP_HTML_END */)
      inside #stage positioned outside the floorplan envelope via
      /* ROOM_MUSIC_CSS_BEGIN */ .. /* ROOM_MUSIC_CSS_END */
      (position: absolute; top: 10px; right: 14px; z-index: 26;), clickable to open
      HA More Info for the active group entity (media_player.whole_house / media_player.home_group),
      plus /* ROOM_MUSIC_UPDATE_BEGIN */ .. /* ROOM_MUSIC_UPDATE_END */ in _updateState().
   D) Tighter 3-floor exploded vertical stack spacing (-15.5% desktop, -16.5% mobile).
3. Validate patched JS syntax via `node --check`.
4. Deploy updated JS card to Host 1 Volume 2 NVMe, bump the HA Lovelace
   resource cache-buster via WebSocket, and verify HTTP 200 and live content.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import re
import sys
import time
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))
_TOOLS_DIR = Path(__file__).resolve().parent
if str(_TOOLS_DIR) not in sys.path:
    sys.path.insert(1, str(_TOOLS_DIR))

from tools.deploy_oven_meater_popup import (
    _http_get,
    _replace_or_insert_block,
    _ws_lovelace_resources,
    deploy_card_to_host1,
    fetch_live_card_and_backup,
    resolve_ha_credentials,
    update_lovelace_resource_cache_buster,
    validate_js_syntax,
)

BACKUP_CARD_PATH = Path("/workspace/scratch/brock-house-cycles-card.room-music.backup.js")

CSS_BEGIN_MARKER = "/* ROOM_MUSIC_CSS_BEGIN */"
CSS_END_MARKER = "/* ROOM_MUSIC_CSS_END */"
GROUP_HTML_BEGIN_MARKER = "/* ROOM_MUSIC_GROUP_HTML_BEGIN */"
GROUP_HTML_END_MARKER = "/* ROOM_MUSIC_GROUP_HTML_END */"
HELPER_BEGIN_MARKER = "/* ROOM_MUSIC_HELPER_BEGIN */"
HELPER_END_MARKER = "/* ROOM_MUSIC_HELPER_END */"
EVAL_BEGIN_MARKER = "/* ROOM_MUSIC_EVAL_BEGIN */"
EVAL_END_MARKER = "/* ROOM_MUSIC_EVAL_END */"
LISTENERS_BEGIN_MARKER = "/* ROOM_MUSIC_LISTENERS_BEGIN */"
LISTENERS_END_MARKER = "/* ROOM_MUSIC_LISTENERS_END */"
UPDATE_BEGIN_MARKER = "/* ROOM_MUSIC_UPDATE_BEGIN */"
UPDATE_END_MARKER = "/* ROOM_MUSIC_UPDATE_END */"

REQUIRED_MARKERS: tuple[str, ...] = (
    CSS_BEGIN_MARKER,
    CSS_END_MARKER,
    GROUP_HTML_BEGIN_MARKER,
    GROUP_HTML_END_MARKER,
    HELPER_BEGIN_MARKER,
    HELPER_END_MARKER,
    EVAL_BEGIN_MARKER,
    EVAL_END_MARKER,
    LISTENERS_BEGIN_MARKER,
    LISTENERS_END_MARKER,
    UPDATE_BEGIN_MARKER,
    UPDATE_END_MARKER,
)

# Per-floor active-only Cast speaker music items: (floor_id, item_id, js_object_literal, x_str, y_str, primary_entity)
FLOOR_MUSIC_ITEMS: tuple[tuple[str, str, str, str, str, str], ...] = (
    (
        "floor3",
        "kitchen_music",
        '{ id: "kitchen_music", kind: "music", entity: "media_player.kitchen_speaker", label: "Kitchen", icon: "music", accent: "green", x: 34.0, y: 39.0 }',
        "34.0",
        "39.0",
        "media_player.kitchen_speaker",
    ),
    (
        "floor3",
        "rosie_music",
        '{ id: "rosie_music", kind: "music", entity: "media_player.rosie_s_speaker", label: "Rosie\'s", icon: "music", accent: "green", x: 74.0, y: 50.0 }',
        "74.0",
        "50.0",
        "media_player.rosie_s_speaker",
    ),
    (
        "floor3",
        "isaac_music",
        '{ id: "isaac_music", kind: "music", entity: "media_player.isaac_s_speaker", label: "Isaac\'s", icon: "music", accent: "green", x: 68.0, y: 74.0 }',
        "68.0",
        "74.0",
        "media_player.isaac_s_speaker",
    ),
    (
        "floor2",
        "living_music",
        '{ id: "living_music", kind: "music", entity: "media_player.living_room_speaker", fallbackEntities: ["media_player.kitchen_hub"], label: "Living", icon: "music", accent: "green", x: 24.0, y: 40.0 }',
        "24.0",
        "40.0",
        "media_player.living_room_speaker",
    ),
    (
        "floor2",
        "bedroom_music",
        '{ id: "bedroom_music", kind: "music", entity: "media_player.bedroom_speaker", label: "Bedroom", icon: "music", accent: "green", x: 68.0, y: 66.0 }',
        "68.0",
        "66.0",
        "media_player.bedroom_speaker",
    ),
    (
        "floor1",
        "office_music",
        '{ id: "office_music", kind: "music", entity: "media_player.office_speaker", fallbackEntities: ["media_player.office_hub"], label: "Office", icon: "music", accent: "green", x: 26.0, y: 51.0 }',
        "26.0",
        "51.0",
        "media_player.office_speaker",
    ),
)

ROOM_MUSIC_CSS = """        /* ROOM_MUSIC_CSS_BEGIN */
        .stage.exploded .floor-card + .floor-card {
          margin-top: -15.5%;
        }
        @media (max-width: 720px) {
          .stage.exploded .floor-card + .floor-card {
            margin-top: -16.5%;
          }
        }
        .run-badge[data-run-kind="music"] .run-thumb[data-run-thumb] {
          width: 18px;
          height: 18px;
          aspect-ratio: 1 / 1;
          border-radius: 3px;
          object-fit: cover;
        }
        .music-group-badge {
          position: absolute;
          top: 10px;
          right: 14px;
          z-index: 26;
          display: none;
          align-items: center;
          gap: 7px;
          padding: 5px 11px 5px 8px;
          border-radius: 999px;
          background: rgba(10, 26, 20, 0.92);
          border: 1px solid rgba(74, 222, 128, 0.72);
          color: #d8fbe6;
          box-shadow: 0 6px 18px rgba(0, 0, 0, 0.5), 0 0 14px rgba(74, 222, 128, 0.25);
          backdrop-filter: blur(10px);
          -webkit-backdrop-filter: blur(10px);
          cursor: pointer;
          pointer-events: auto;
          font-family: inherit;
          text-align: left;
          max-width: 260px;
          transition: transform 0.16s ease, box-shadow 0.18s ease, border-color 0.18s ease;
        }
        .music-group-badge.active {
          display: inline-flex;
        }
        .music-group-badge:hover {
          transform: translateY(-1px);
          border-color: rgba(74, 222, 128, 0.95);
          box-shadow: 0 8px 22px rgba(0, 0, 0, 0.58), 0 0 18px rgba(74, 222, 128, 0.38);
        }
        .music-group-badge .run-dot {
          width: 7px;
          height: 7px;
          border-radius: 50%;
          background: #4ade80;
          box-shadow: 0 0 8px #4ade80;
          flex-shrink: 0;
        }
        .music-group-badge .run-thumb {
          width: 22px;
          height: 22px;
          border-radius: 4px;
          object-fit: cover;
          flex-shrink: 0;
          border: 1px solid rgba(255, 255, 255, 0.24);
          background: rgba(0, 0, 0, 0.35);
        }
        .music-group-badge .run-icon {
          display: inline-flex;
          align-items: center;
          justify-content: center;
          width: 15px;
          height: 15px;
          color: #4ade80;
          flex-shrink: 0;
        }
        .music-group-badge.has-thumb .run-icon {
          display: none;
        }
        .music-group-badge .mg-text {
          display: flex;
          flex-direction: column;
          min-width: 0;
          line-height: 1.15;
        }
        .music-group-badge .mg-label {
          font-size: 10px;
          font-weight: 700;
          letter-spacing: 0.04em;
          text-transform: uppercase;
          color: #6ee7b7;
          white-space: nowrap;
          overflow: hidden;
          text-overflow: ellipsis;
        }
        .music-group-badge .mg-track {
          font-size: 11px;
          font-weight: 600;
          color: #ecfdf5;
          white-space: nowrap;
          overflow: hidden;
          text-overflow: ellipsis;
        }
        /* ROOM_MUSIC_CSS_END */"""

ROOM_MUSIC_GROUP_HTML = (
    '          ${/* ROOM_MUSIC_GROUP_HTML_BEGIN */ ""}\n'
    '          <button class="music-group-badge" id="musicGroupBadge" data-group-entity="" data-run-entity="" title="Whole House">\n'
    '            <span class="run-dot"></span>\n'
    '            <img class="run-thumb" id="musicGroupThumb" data-group-thumb="1" alt="" decoding="async" draggable="false" style="display:none;" onload="delete this.dataset.failedSrc;this.style.display=\'block\';this.closest(\'.music-group-badge\')?.classList.add(\'has-thumb\');" onerror="this.dataset.failedSrc=this.getAttribute(\'src\')||\'\';this.style.display=\'none\';this.closest(\'.music-group-badge\')?.classList.remove(\'has-thumb\');" />\n'
    '            <span class="run-icon" id="musicGroupIcon">${SVG_ICONS.music || ""}</span>\n'
    '            <span class="mg-text">\n'
    '              <span class="mg-label" id="musicGroupLabel" data-group-label="1">Whole House</span>\n'
    '              <span class="mg-track" id="musicGroupTrack" data-group-track="1"></span>\n'
    "            </span>\n"
    "          </button>\n"
    '          ${/* ROOM_MUSIC_GROUP_HTML_END */ ""}'
)

ROOM_MUSIC_HELPER_JS = """  /* ROOM_MUSIC_HELPER_BEGIN */
  _isMusicEntityActive(st) {
    if (!st) return false;
    if (st.state === "playing") return true;
    if (st.state === "paused" && String(st.attributes?.media_title || st.attributes?.title || "").trim()) {
      return true;
    }
    return false;
  }

  _extractMusicTrackText(st, fallbackLabel = "Music") {
    const attrs = st?.attributes || {};
    const title = String(attrs.media_title || attrs.title || "").trim();
    const artist = String(attrs.media_artist || attrs.artist || "").trim();
    const appName = String(attrs.app_name || "").trim();
    let label = "";
    if (title && artist) {
      label = `${title} · ${artist}`;
    } else if (title) {
      label = title;
    } else if (appName) {
      label = `${fallbackLabel}: ${appName}`;
    } else {
      label = `${fallbackLabel} Playing`;
    }
    if (label.length > 28) {
      label = label.slice(0, 27) + "…";
    }
    return label;
  }

  _extractMusicThumb(st) {
    if (!st || !st.attributes) return "";
    const raw = st.attributes.entity_picture || st.attributes.entity_picture_local || "";
    if (typeof this._sanitizeArtworkUrl === "function") {
      return this._sanitizeArtworkUrl(raw);
    }
    return String(raw || "").trim();
  }

  _evalMusicGroup() {
    const states = this._hass?.states || {};
    const groups = [
      { entity: "media_player.whole_house", label: "Whole House" },
      { entity: "media_player.home_group", label: "Home Group" },
    ];
    for (const g of groups) {
      const st = states[g.entity];
      if (this._isMusicEntityActive(st)) {
        const trackText = this._extractMusicTrackText(st, g.label);
        const thumb = this._extractMusicThumb(st);
        return {
          active: true,
          entity: g.entity,
          label: g.label,
          trackText,
          text: `${g.label} · ${trackText}`,
          thumb,
        };
      }
    }
    return {
      active: false,
      entity: "media_player.whole_house",
      label: "Whole House",
      trackText: "",
      text: "Whole House",
      thumb: "",
    };
  }

  _evalRoomMusicItem(item) {
    const states = this._hass?.states || {};
    const groupEv = this._evalMusicGroup();
    if (groupEv.active) {
      return { active: false, text: item.label, entity: item.entity, thumb: "", suppressedByGroup: true };
    }
    const candidates = [item.entity, ...(item.fallbackEntities || [])];
    for (const cid of candidates) {
      if (!cid) continue;
      const st = states[cid];
      if (this._isMusicEntityActive(st)) {
        const text = this._extractMusicTrackText(st, item.label);
        const thumb = this._extractMusicThumb(st);
        return { active: true, text, entity: cid, thumb };
      }
    }
    return { active: false, text: item.label, entity: item.entity, thumb: "" };
  }
  /* ROOM_MUSIC_HELPER_END */"""

ROOM_MUSIC_EVAL_JS = """    /* ROOM_MUSIC_EVAL_BEGIN */
    if (item.kind === "music") {
      return this._evalRoomMusicItem(item);
    }
    /* ROOM_MUSIC_EVAL_END */"""

ROOM_MUSIC_LISTENERS_JS = """    /* ROOM_MUSIC_LISTENERS_BEGIN */
    {
      const mgBadge =
        (typeof sr?.getElementById === "function" && sr.getElementById("musicGroupBadge")) ||
        (typeof sr?.querySelector === "function" && sr.querySelector("#musicGroupBadge, .music-group-badge"));
      mgBadge?.addEventListener?.("click", (e) => {
        if (e && typeof e.stopPropagation === "function") e.stopPropagation();
        const eid = mgBadge.dataset?.groupEntity || mgBadge.dataset?.runEntity;
        if (eid) this._openMoreInfo(eid);
      });
    }
    /* ROOM_MUSIC_LISTENERS_END */"""

ROOM_MUSIC_UPDATE_JS = """      /* ROOM_MUSIC_UPDATE_BEGIN */
      {
        const hideForClimate = typeof isClimate !== "undefined" ? Boolean(isClimate) : this._activeLens === "climate";
        const groupEv = this._evalMusicGroup();
        const groupEl =
          (typeof sr?.getElementById === "function" && sr.getElementById("musicGroupBadge")) ||
          (typeof sr?.querySelector === "function" && sr.querySelector("#musicGroupBadge, .music-group-badge"));
        if (groupEl) {
          const showGroup = Boolean(groupEv.active && !hideForClimate);
          groupEl.classList?.toggle?.("active", showGroup);
          if (groupEl.dataset) {
            groupEl.dataset.groupEntity = groupEv.entity;
            groupEl.dataset.runEntity = groupEv.entity;
          }
          groupEl.title = groupEv.active ? `${groupEv.label} · ${groupEv.trackText}` : groupEv.label;
          const lblEl =
            groupEl.querySelector?.("#musicGroupLabel, [data-group-label]") ||
            (typeof sr?.getElementById === "function" ? sr.getElementById("musicGroupLabel") : null);
          if (lblEl) lblEl.textContent = groupEv.label;
          const trkEl =
            groupEl.querySelector?.("#musicGroupTrack, [data-group-track]") ||
            (typeof sr?.getElementById === "function" ? sr.getElementById("musicGroupTrack") : null);
          if (trkEl) trkEl.textContent = groupEv.trackText;
          const thumbEl =
            groupEl.querySelector?.("#musicGroupThumb, [data-group-thumb]") ||
            (typeof sr?.getElementById === "function" ? sr.getElementById("musicGroupThumb") : null);
          if (thumbEl) {
            if (showGroup && groupEv.thumb) {
              if (thumbEl.dataset?.failedSrc === groupEv.thumb) {
                if (thumbEl.style) thumbEl.style.display = "none";
                groupEl.classList?.remove?.("has-thumb");
              } else {
                if (typeof thumbEl.getAttribute === "function" && thumbEl.getAttribute("src") !== groupEv.thumb) {
                  if (thumbEl.dataset) delete thumbEl.dataset.failedSrc;
                  thumbEl.setAttribute?.("src", groupEv.thumb);
                }
                thumbEl.src = groupEv.thumb;
                if (thumbEl.style) thumbEl.style.display = "block";
                groupEl.classList?.add?.("has-thumb");
              }
            } else {
              if (thumbEl.dataset) delete thumbEl.dataset.failedSrc;
              thumbEl.removeAttribute?.("src");
              thumbEl.src = "";
              if (thumbEl.style) thumbEl.style.display = "none";
              groupEl.classList?.remove?.("has-thumb");
            }
          }
        }
      }
      /* ROOM_MUSIC_UPDATE_END */"""


def _patch_floor_running_items(source_js: str) -> str:
    """Idempotently remove dead apolosign_music on 3F and upsert per-room Cast speaker music badges on 3F, 2F, 1F."""
    out = source_js

    # 1. Remove dead apolosign_music from anywhere in FLOORS runningItems
    apolosign_re = re.compile(
        r'^[ \t]*\{[^{}\n]*\bid:\s*"apolosign_music"[^{}\n]*\},?[ \t]*\n?',
        re.MULTILINE,
    )
    out = apolosign_re.sub("", out)

    # 2. Upsert each floor's room music items by id
    for floor_id in ("floor3", "floor2", "floor1"):
        floor_items = [item for item in FLOOR_MUSIC_ITEMS if item[0] == floor_id]
        floor_running_re = re.compile(
            rf'(\bid:\s*"{re.escape(floor_id)}"(?:(?!\bid:\s*"floor[123]")[\s\S])*?\brunningItems:\s*\[\n?)([\s\S]*?)(\n[ \t]*\])'
        )
        m_floor = floor_running_re.search(out)
        if not m_floor:
            raise RuntimeError(f"Could not locate runningItems array for {floor_id}")

        body = m_floor.group(2)
        indent_match = re.search(r"^([ \t]+)\{", body, re.MULTILINE)
        indent = indent_match.group(1) if indent_match else "      "

        for _fl, item_id, item_literal, _x, _y, _entity in floor_items:
            existing_re = re.compile(
                rf'^[ \t]*\{{[^{{}}\n]*\bid:\s*"{re.escape(item_id)}"[^{{}}\n]*\}},?[ \t]*$',
                re.MULTILINE,
            )
            replacement_line = f"{indent}{item_literal},"
            if existing_re.search(body):
                body = existing_re.sub(replacement_line, body, count=1)
            else:
                stripped = body.rstrip()
                if stripped:
                    if not stripped.endswith(","):
                        stripped += ","
                    body = f"{stripped}\n{replacement_line}"
                else:
                    body = replacement_line

        out = out[: m_floor.start(2)] + body + out[m_floor.end(2) :]

    return out


def _patch_tv_cover_art_music_kind(source_js: str) -> str:
    """Idempotently extend TV_COVER_ART HTML, thumb helpers, sigParts, and update loop to include kind === 'music'."""
    out = source_js

    # Extend item.kind === "tv" || item.kind === "media_sensor" -> || item.kind === "music"
    out = re.sub(
        r'\b(item|ri)\.kind\s*===\s*"tv"\s*\|\|\s*\1\.kind\s*===\s*"media_sensor"(?!\s*\|\|\s*\1\.kind\s*===\s*"music")',
        r'\1.kind === "tv" || \1.kind === "media_sensor" || \1.kind === "music"',
        out,
    )

    # Extend (item|ri).kind !== "tv" && (item|ri).kind !== "media_sensor" -> && (item|ri).kind !== "music"
    out = re.sub(
        r'\b(item|ri)\.kind\s*!==\s*"tv"\s*&&\s*\1\.kind\s*!==\s*"media_sensor"(?!\s*&&\s*\1\.kind\s*!==\s*"music")',
        r'\1.kind !== "tv" && \1.kind !== "media_sensor" && \1.kind !== "music"',
        out,
    )

    # Ensure _collectTvCoverArtSigParts also tracks multi-speaker group changes
    if "_collectTvCoverArtSigParts" in out and "const mg = this._evalMusicGroup();" not in out:
        out = re.sub(
            r"(_collectTvCoverArtSigParts\s*\(\s*\)\s*\{[\s\S]*?)(\n[ \t]*return\s+parts;\s*\n[ \t]*\})",
            r'\g<1>\n    if (typeof this._evalMusicGroup === "function") {\n      const mg = this._evalMusicGroup();\n      parts.push(mg.active ? "1" : "0", mg.entity, mg.text, mg.thumb);\n    }\g<2>',
            out,
            count=1,
        )

    return out


def _patch_group_html(source_js: str) -> str:
    """Idempotently inject or replace the floating multi-speaker group badge inside #stage."""
    marked_group_re = re.compile(
        r'(?:[ \t]*\$\{/\* ROOM_MUSIC_GROUP_HTML_BEGIN \*/ ""\}[\s\S]*?\$\{/\* ROOM_MUSIC_GROUP_HTML_END \*/ ""\}'
        r"|[ \t]*/\* ROOM_MUSIC_GROUP_HTML_BEGIN \*/[\s\S]*?/\* ROOM_MUSIC_GROUP_HTML_END \*/)"
    )
    if marked_group_re.search(source_js):
        return marked_group_re.sub(lambda _m: ROOM_MUSIC_GROUP_HTML, source_js, count=1)

    return _replace_or_insert_block(
        source_js,
        GROUP_HTML_BEGIN_MARKER,
        GROUP_HTML_END_MARKER,
        ROOM_MUSIC_GROUP_HTML,
        (
            r'<div\s+class="stage\b[^\n>]*\bid="stage">',
            r'<div\s+id="stage"[^\n>]*>',
            r'<div\s+class="stage"[^\n>]*>',
        ),
        insert_before=False,
        label="ROOM_MUSIC_GROUP_HTML",
    )


def verify_patched_card_contents(card_js: str) -> None:
    """Assert room music badges, floating group badge, cover art wiring, and tighter exploded stack CSS are present."""
    missing: list[str] = []

    for marker in REQUIRED_MARKERS:
        if marker not in card_js:
            missing.append(f"marker:{marker}")

    for forbidden in ('"apolosign_music"', '"sensor.apolosign_media"'):
        if forbidden in card_js:
            missing.append(f"forbidden_still_present:{forbidden}")

    for floor_id, item_id, _literal, exp_x, exp_y, primary_entity in FLOOR_MUSIC_ITEMS:
        floor_re = re.compile(
            rf'\bid:\s*"{re.escape(floor_id)}"(?:(?!\bid:\s*"floor[123]")[\s\S])*?\brunningItems:\s*\[([\s\S]*?)\n[ \t]*\]'
        )
        m_floor = floor_re.search(card_js)
        if not m_floor:
            missing.append(f"floor_missing:{floor_id}")
            continue
        floor_body = m_floor.group(1)
        item_pat = re.compile(
            rf'\{{[^{{}}\n]*\bid:\s*"{re.escape(item_id)}"[^{{}}\n]*\bkind:\s*"music"[^{{}}\n]*'
            rf'\bentity:\s*"{re.escape(primary_entity)}"[^{{}}\n]*\bx:\s*{re.escape(exp_x)},\s*y:\s*{re.escape(exp_y)}\b'
        )
        if not item_pat.search(floor_body):
            missing.append(f"room_music_item:{floor_id}:{item_id}")

    for token in (
        '"media_player.kitchen_hub"',
        '"media_player.office_hub"',
        '"media_player.whole_house"',
        '"media_player.home_group"',
        '"Whole House"',
        '"Home Group"',
        'item.kind === "tv" || item.kind === "media_sensor" || item.kind === "music"',
        'ri.kind !== "tv" && ri.kind !== "media_sensor" && ri.kind !== "music"',
        "margin-top: -15.5%;",
        "margin-top: -16.5%;",
        "position: absolute;",
        "top: 10px;",
        "right: 14px;",
        "z-index: 26;",
        'id="musicGroupBadge"',
        'id="musicGroupThumb"',
        'id="musicGroupLabel"',
        'id="musicGroupTrack"',
        "_evalMusicGroup",
        "_evalRoomMusicItem",
    ):
        if token not in card_js:
            missing.append(f"token:{token}")

    if missing:
        raise RuntimeError("Patched floorplan room music verification failed; missing: " + ", ".join(missing))


def patch_card_js(source_js: str) -> str:
    """Idempotently patch brock-house-cycles-card.js with room & whole-house music badges and tighter 3-floor stack."""
    out = _patch_floor_running_items(source_js)
    out = _patch_tv_cover_art_music_kind(out)

    out = _replace_or_insert_block(
        out,
        CSS_BEGIN_MARKER,
        CSS_END_MARKER,
        ROOM_MUSIC_CSS,
        (
            r"^\s*</style>",
            r"</style>",
        ),
        insert_before=True,
        label="ROOM_MUSIC_CSS",
    )

    out = _patch_group_html(out)

    out = _replace_or_insert_block(
        out,
        HELPER_BEGIN_MARKER,
        HELPER_END_MARKER,
        ROOM_MUSIC_HELPER_JS,
        r"^\s*_evalRunningItem\s*\(\s*(?:item|ri)\s*\)\s*\{",
        insert_before=True,
        label="ROOM_MUSIC_HELPER",
    )

    out = _replace_or_insert_block(
        out,
        EVAL_BEGIN_MARKER,
        EVAL_END_MARKER,
        ROOM_MUSIC_EVAL_JS,
        (
            r"_evalRunningItem\s*\(\s*(?:item|ri)\s*\)\s*\{\s*\n\s*const\s+states\s*=\s*this\._hass\?\.states\s*\|\|\s*\{\};",
            r"_evalRunningItem\s*\(\s*(?:item|ri)\s*\)\s*\{",
        ),
        insert_before=False,
        label="ROOM_MUSIC_EVAL",
    )

    out = _replace_or_insert_block(
        out,
        LISTENERS_BEGIN_MARKER,
        LISTENERS_END_MARKER,
        ROOM_MUSIC_LISTENERS_JS,
        (
            r"/\*\s*OVEN_MEATER_LISTENERS_END\s*\*/",
            r'sr\.querySelectorAll\("\.run-badge"\)\.forEach\([\s\S]*?\}\);',
            r"_bindListeners\s*\(\s*sr\s*\)\s*\{",
        ),
        insert_before=False,
        label="ROOM_MUSIC_LISTENERS",
    )

    out = _replace_or_insert_block(
        out,
        UPDATE_BEGIN_MARKER,
        UPDATE_END_MARKER,
        ROOM_MUSIC_UPDATE_JS,
        (
            r"/\*\s*OVEN_MEATER_UPDATE_END\s*\*/",
            r"/\*\s*TV_COVER_ART_UPDATE_END\s*\*/",
            r"_updateState\s*\([^)]*\)\s*\{",
        ),
        insert_before=False,
        label="ROOM_MUSIC_UPDATE",
    )

    verify_patched_card_contents(out)
    validate_js_syntax(out)
    return out


def verify_live_http(ha_url: str, ha_token: str = "") -> tuple[bool, list[str]]:
    """Verify HTTP 200 and room music / whole-house group badge contents on the live JS card on Home Assistant."""
    errors: list[str] = []
    card_rel = "/local/brock-house-3d/brock-house-cycles-card.js"

    if ha_token:
        try:
            resources = asyncio.run(
                asyncio.wait_for(_ws_lovelace_resources(ha_url, ha_token, cache_buster=None), timeout=25)
            )
            matched = next((r for r in resources if card_rel in (r.get("url") or "")), None)
            if matched:
                card_rel = matched["url"]
                print(f"[verify] Lovelace resource URL: {card_rel}", flush=True)
            else:
                errors.append(f"Lovelace resource for {card_rel} not found via WebSocket")
        except Exception as exc:  # noqa: BLE001
            errors.append(f"Lovelace WebSocket query failed: {exc}")

    card_url = f"{ha_url}{card_rel}"
    status, body = _http_get(card_url)
    if status != 200:
        errors.append(f"HTTP {status} for live card: {card_url}")
    else:
        card_text = body.decode("utf-8", errors="replace")
        try:
            verify_patched_card_contents(card_text)
            print(f"OK [200] {card_url} ({len(body):,} bytes)", flush=True)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"Live card content check failed ({card_url}): {exc}")

    return (len(errors) == 0), errors


def run_deploy(*, dry_run: bool = False) -> int:
    """Execute the full fetch -> backup -> patch -> syntax check -> deploy -> WS bust -> HTTP verify pipeline."""
    ha_url, ha_token = resolve_ha_credentials()

    live_js = fetch_live_card_and_backup(BACKUP_CARD_PATH)
    patched_js = patch_card_js(live_js)
    js_hash = hashlib.sha256(patched_js.encode("utf-8")).hexdigest()[:8]
    print(
        f"[2/4] Patched & syntax-checked card JS via node --check "
        f"(sha256={js_hash}, {len(patched_js.encode('utf-8')):,} bytes, Room & Whole-House Music Badges)",
        flush=True,
    )

    if dry_run:
        print("[dry-run] Skipping Host 1 upload, WebSocket cache-buster update, and HTTP check.", flush=True)
        return 0

    deploy_card_to_host1(patched_js)
    cache_buster = f"{js_hash}{int(time.time()) & 0xFFFF:04x}"[:12]
    update_lovelace_resource_cache_buster(ha_url, ha_token, cache_buster=cache_buster)

    ok, errors = verify_live_http(ha_url, ha_token)
    if not ok:
        for err in errors:
            print(f"VERIFY ERROR: {err}", file=sys.stderr, flush=True)
        return 1

    print("SUCCESS: Deployed and verified 3D floorplan room & whole-house music badges on live card (HTTP 200).", flush=True)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Patch and deploy 3D floorplan room & whole-house music badges and tighter 3-floor stack to Home Assistant."
    )
    parser.add_argument(
        "--verify-only",
        action="store_true",
        help="Verify HTTP 200 and room/whole-house music badge contents on the live card on Home Assistant.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Fetch, back up, patch, and syntax-check card JS in memory without modifying Host 1 or HA.",
    )
    args = parser.parse_args(argv)

    if args.verify_only:
        ha_url, ha_token = resolve_ha_credentials()
        ok, errors = verify_live_http(ha_url, ha_token)
        if not ok:
            for err in errors:
                print(f"ERROR: {err}", file=sys.stderr, flush=True)
            return 1
        print("VERIFIED: Live card returned HTTP 200 with floorplan room & whole-house music badges wired.", flush=True)
        return 0

    return run_deploy(dry_run=args.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())
