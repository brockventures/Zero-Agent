"""Unit tests for tools/deploy_floorplan_room_music.py."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest import mock

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from tools import deploy_floorplan_room_music as mod

CARD_FIXTURE_UNPATCHED = """/* Brock House 4K Photorealistic Cycles Floorplan Card */
const SVG_ICONS = {
  music: "<svg></svg>",
  tv: "<svg></svg>",
  coffee: "<svg></svg>",
};

const FLOORS = [
  {
    id: "floor3",
    runningItems: [
      { id: "coffee", kind: "switch", entity: "switch.coffee_machine", label: "Coffee On", icon: "coffee", accent: "amber", x: 44.5, y: 31.5 },
      { id: "apolosign_music", kind: "media_sensor", entity: "sensor.apolosign_media", label: "Music", icon: "music", accent: "green", x: 29.5, y: 43.0 },
    ],
  },
  {
    id: "floor2",
    runningItems: [
      { id: "living_tv", kind: "tv", entity: "media_player.plex_plex_for_android_tv_google_tv_streamer", fallbackEntities: ["media_player.living_room_tv"], label: "TV", icon: "tv", accent: "blue", x: 26.5, y: 29.5 },
    ],
  },
  {
    id: "floor1",
    runningItems: [
      { id: "office_tv", kind: "tv", entity: "media_player.office_chromecast", fallbackEntities: ["media_player.office_chromecast_2"], label: "Office TV", icon: "tv", accent: "blue", x: 42.0, y: 44.5 },
    ],
  },
];

class BrockHouseCyclesCard {
  constructor() {
    this._hass = null;
    this._activeFloor = "all";
    this._activeLens = "lights";
    this.shadowRoot = null;
    this._moreInfoEntities = [];
  }

  _openMoreInfo(entityId) {
    this._moreInfoEntities.push(entityId);
  }

  _sanitizeArtworkUrl(rawUrl) {
    const url = String(rawUrl || "").trim();
    if (!url) return "";
    const lower = url.toLowerCase();
    if (["none", "null", "undefined", "unknown", "unavailable"].includes(lower)) return "";
    if (
      url.startsWith("/") ||
      lower.startsWith("http://") ||
      lower.startsWith("https://") ||
      lower.startsWith("data:image/")
    ) {
      return url;
    }
    return "";
  }

  _extractMediaEntityArtwork(st, kind) {
    if (!st || !st.attributes) return "";
    const attrs = st.attributes;
    if (kind === "media_sensor") {
      return this._sanitizeArtworkUrl(
        attrs.entity_picture || attrs.entity_picture_local || attrs.artwork_url || attrs.cover_url || ""
      );
    }
    return this._sanitizeArtworkUrl(attrs.entity_picture || attrs.entity_picture_local || "");
  }

  _resolveRunningItemThumb(item, ev) {
    if (!item || !ev || !ev.active) return "";
    if (item.kind !== "tv" && item.kind !== "media_sensor") return "";
    const states = this._hass?.states || {};
    const activeEntity = ev.entity || item.entity;
    const activeThumb = this._extractMediaEntityArtwork(states[activeEntity], "tv");
    if (activeThumb) return activeThumb;
    return "";
  }

  _collectTvCoverArtSigParts() {
    const states = this._hass?.states || {};
    const parts = [];
    for (const f of FLOORS) {
      for (const ri of f.runningItems || []) {
        if (ri.kind !== "tv" && ri.kind !== "media_sensor") continue;
        const candidates = [ri.entity, ...(ri.fallbackEntities || [])];
        for (const cid of candidates) {
          if (!cid) continue;
          const st = states[cid];
          if (!st) {
            parts.push("");
            continue;
          }
          parts.push(st.state || "", this._extractMediaEntityArtwork(st, ri.kind));
        }
      }
    }
    return parts;
  }

  _applyRunningItemThumb(el, ri, ev) {
    if (!el || !ri || (ri.kind !== "tv" && ri.kind !== "media_sensor")) return;
    const thumbEl = el.querySelector(`[data-run-thumb="${ri.id}"]`);
    if (!thumbEl) return;
    const thumb = this._resolveRunningItemThumb(ri, ev);
    if (thumb) {
      if (thumbEl.dataset?.failedSrc === thumb) {
        thumbEl.style.display = "none";
        el.classList.remove("has-thumb");
      } else {
        if (thumbEl.getAttribute("src") !== thumb) {
          if (thumbEl.dataset) delete thumbEl.dataset.failedSrc;
          thumbEl.setAttribute("src", thumb);
        }
        thumbEl.style.display = "block";
        el.classList.add("has-thumb");
      }
    } else {
      if (thumbEl.dataset) delete thumbEl.dataset.failedSrc;
      thumbEl.removeAttribute("src");
      thumbEl.style.display = "none";
      el.classList.remove("has-thumb");
    }
  }

  _evalRunningItem(item) {
    const states = this._hass?.states || {};
    if (item.kind === "media_sensor") {
      const st = states[item.entity];
      if (st && st.state === "playing") {
        return { active: true, text: "Music Playing", entity: item.entity };
      }
      return { active: false, text: item.label, entity: item.entity };
    }
    if (item.kind === "switch") {
      const st = states[item.entity];
      if (st && st.state === "on") return { active: true, text: item.label, entity: item.entity };
      return { active: false, text: item.label, entity: item.entity };
    }
    if (item.kind === "tv") {
      const candidates = [item.entity, ...(item.fallbackEntities || [])];
      for (const cid of candidates) {
        const st = states[cid];
        if (st && (st.state === "playing" || st.state === "paused")) {
          return { active: true, text: `${item.label} · Playing`, entity: cid };
        }
      }
      return { active: false, text: item.label, entity: item.entity };
    }
    return { active: false, text: item.label, entity: item.entity };
  }

  _renderSkeleton() {
    return `
      <style>
        .stage.exploded .floor-card + .floor-card {
          margin-top: -11%;
        }
        @media (max-width: 720px) {
          .stage.exploded .floor-card + .floor-card {
            margin-top: -12%;
          }
        }
      </style>
      <div class="shell">
        <div class="stage ${this._activeFloor === "all" ? "exploded" : ""}" id="stage">
          ${FLOORS.map(
            (f) => `
            <div class="floor-card active" data-floor-card="${f.id}">
              <div class="running-layer" data-running-layer="${f.id}">
                ${/* TV_COVER_ART_HTML_BEGIN */
                  (f.runningItems || [])
                    .map(
                      (item) => `
                  <button class="run-badge ${item.accent || ""}" data-run-id="${item.id}" data-run-kind="${item.kind || ""}" data-run-entity="${item.entity}" style="left:${item.x}%;top:${item.y}%;" title="${item.label}">
                    <span class="run-dot"></span>
                    ${
                      item.kind === "tv" || item.kind === "media_sensor"
                        ? `<img class="run-thumb" data-run-thumb="${item.id}" alt="" decoding="async" draggable="false" style="display:none;" />`
                        : ""
                    }
                    <span class="run-icon" data-run-icon="${item.id}">${SVG_ICONS[item.icon] || ""}</span>
                    <span data-run-text="${item.id}">${item.label}</span>
                  </button>
                `
                    )
                  /* TV_COVER_ART_HTML_END */
                  .join("")}
              </div>
            </div>
          `
          ).join("")}
        </div>
      </div>
    `;
  }

  _bindListeners(sr) {
    sr.querySelectorAll(".run-badge").forEach((btn) => {
      btn.addEventListener("click", (e) => {
        e.stopPropagation();
        const eid = btn.dataset.runEntity;
        if (eid) this._openMoreInfo(eid);
      });
    });
    /* OVEN_MEATER_LISTENERS_BEGIN */
    /* OVEN_MEATER_LISTENERS_END */
  }

  _updateState() {
    const sr = this.shadowRoot;
    const isClimate = this._activeLens === "climate";
    for (const f of FLOORS) {
      const card = sr.querySelector(`[data-floor-card="${f.id}"]`);
      for (const ri of (f.runningItems || [])) {
        const ev = this._evalRunningItem(ri);
        const el = card?.querySelector(`.run-badge[data-run-id="${ri.id}"]`);
        if (el) {
          el.classList.toggle("active", ev.active && !isClimate);
          el.dataset.runEntity = ev.entity;
          const txtEl = el.querySelector(`[data-run-text="${ri.id}"]`);
          if (txtEl) txtEl.textContent = ev.text;
        }
      }
      /* TV_COVER_ART_UPDATE_BEGIN */
      for (const ri of (f.runningItems || [])) {
        if (ri.kind !== "tv" && ri.kind !== "media_sensor") continue;
        const el = card?.querySelector(`.run-badge[data-run-id="${ri.id}"]`);
        if (!el) continue;
        const ev = this._evalRunningItem(ri);
        this._applyRunningItemThumb(el, ri, isClimate ? { ...ev, active: false } : ev);
      }
      /* TV_COVER_ART_UPDATE_END */
    }
  }
}
"""


def test_patch_card_js_removes_apolosign_adds_room_speakers_injects_markers_and_is_idempotent() -> None:
    patched_once = mod.patch_card_js(CARD_FIXTURE_UNPATCHED)
    patched_twice = mod.patch_card_js(patched_once)
    assert patched_once == patched_twice

    for marker in mod.REQUIRED_MARKERS:
        assert marker in patched_once
        assert patched_once.count(marker) == 1

    # Verify dead apolosign_music is completely removed
    assert "apolosign_music" not in patched_once
    assert "sensor.apolosign_media" not in patched_once

    # Verify all 6 room speakers are present with exact coordinates and fallback entities
    assert (
        '{ id: "kitchen_music", kind: "music", entity: "media_player.kitchen_speaker", label: "Kitchen", icon: "music", accent: "green", x: 34.0, y: 39.0 }'
        in patched_once
    )
    assert (
        '{ id: "rosie_music", kind: "music", entity: "media_player.rosie_s_speaker", label: "Rosie\'s", icon: "music", accent: "green", x: 74.0, y: 50.0 }'
        in patched_once
    )
    assert (
        '{ id: "isaac_music", kind: "music", entity: "media_player.isaac_s_speaker", label: "Isaac\'s", icon: "music", accent: "green", x: 68.0, y: 74.0 }'
        in patched_once
    )
    assert (
        '{ id: "living_music", kind: "music", entity: "media_player.living_room_speaker", fallbackEntities: ["media_player.kitchen_hub"], label: "Living", icon: "music", accent: "green", x: 24.0, y: 40.0 }'
        in patched_once
    )
    assert (
        '{ id: "bedroom_music", kind: "music", entity: "media_player.bedroom_speaker", label: "Bedroom", icon: "music", accent: "green", x: 68.0, y: 66.0 }'
        in patched_once
    )
    assert (
        '{ id: "office_music", kind: "music", entity: "media_player.office_speaker", fallbackEntities: ["media_player.office_hub"], label: "Office", icon: "music", accent: "green", x: 26.0, y: 51.0 }'
        in patched_once
    )

    # Verify kind === "music" thumbnail wiring and exploded stack tightening CSS
    assert 'item.kind === "tv" || item.kind === "media_sensor" || item.kind === "music"' in patched_once
    assert 'ri.kind !== "tv" && ri.kind !== "media_sensor" && ri.kind !== "music"' in patched_once
    assert "margin-top: -15.5%;" in patched_once
    assert "margin-top: -16.5%;" in patched_once
    assert "position: absolute;" in patched_once
    assert "top: 10px;" in patched_once
    assert "right: 14px;" in patched_once
    assert "z-index: 26;" in patched_once


def test_patch_card_js_on_live_or_cached_scratch_card_if_present() -> None:
    candidates = [
        Path("/workspace/scratch/live_brock_house_cycles_card.js"),
        Path("/workspace/scratch/brock-house-cycles-card.climate-co2-cross-lens.backup.js"),
        Path("/workspace/scratch/brock-house-cycles-card.backup.js"),
    ]
    target_path = next((p for p in candidates if p.is_file()), None)
    if target_path is None:
        pytest.skip("No cached brock-house-cycles-card.js found in /workspace/scratch")
    raw_js = target_path.read_text(encoding="utf-8")
    patched_once = mod.patch_card_js(raw_js)
    patched_twice = mod.patch_card_js(patched_once)
    assert patched_once == patched_twice
    mod.verify_patched_card_contents(patched_once)


def test_verify_patched_card_contents_passes_and_raises_on_unpatched() -> None:
    patched = mod.patch_card_js(CARD_FIXTURE_UNPATCHED)
    mod.verify_patched_card_contents(patched)

    with pytest.raises(RuntimeError, match="Patched floorplan room music verification failed"):
        mod.verify_patched_card_contents(CARD_FIXTURE_UNPATCHED)


def test_room_speaker_and_whole_house_group_suppression_behavioral_node() -> None:
    patched = mod.patch_card_js(CARD_FIXTURE_UNPATCHED)
    harness = (
        patched
        + """
function makeClassList(initial = []) {
  const set = new Set(initial);
  return {
    add: (...cls) => cls.forEach((c) => set.add(c)),
    remove: (...cls) => cls.forEach((c) => set.delete(c)),
    toggle: (c, force) => {
      const shouldAdd = force === undefined ? !set.has(c) : Boolean(force);
      if (shouldAdd) set.add(c);
      else set.delete(c);
      return shouldAdd;
    },
    contains: (c) => set.has(c),
    toArray: () => Array.from(set),
  };
}

function makeThumbEl() {
  const attrs = {};
  return {
    dataset: {},
    style: { display: "none" },
    src: "",
    getAttribute: (k) => attrs[k] || "",
    setAttribute: (k, v) => {
      attrs[k] = String(v);
      if (k === "src") thumbObj.src = String(v);
    },
    removeAttribute: (k) => {
      delete attrs[k];
      if (k === "src") thumbObj.src = "";
    },
  };
  var thumbObj;
}

function createBadgeMock(runId, entity) {
  const attrs = {};
  const thumbEl = {
    dataset: {},
    style: { display: "none" },
    src: "",
    getAttribute: (k) => attrs[k] || "",
    setAttribute: (k, v) => {
      attrs[k] = String(v);
      if (k === "src") thumbEl.src = String(v);
    },
    removeAttribute: (k) => {
      delete attrs[k];
      if (k === "src") thumbEl.src = "";
    },
  };
  const textEl = { textContent: "" };
  const listeners = {};
  return {
    dataset: { runId, runEntity: entity },
    classList: makeClassList(["run-badge"]),
    style: {},
    title: "",
    thumbEl,
    textEl,
    listeners,
    addEventListener: (evt, fn) => {
      listeners[evt] = fn;
    },
    querySelector: (sel) => {
      if (sel === `[data-run-thumb="${runId}"]`) return thumbEl;
      if (sel === `[data-run-text="${runId}"]`) return textEl;
      return null;
    },
  };
}

// Build floor cards and badges for all FLOORS
const badgesById = {};
const floorCards = {};
for (const f of FLOORS) {
  const floorBadges = {};
  for (const ri of f.runningItems || []) {
    const b = createBadgeMock(ri.id, ri.entity);
    badgesById[ri.id] = b;
    floorBadges[ri.id] = b;
  }
  floorCards[f.id] = {
    querySelector: (sel) => {
      const m = sel.match(/\\[data-run-id="([^"]+)"\\]/);
      if (m && floorBadges[m[1]]) return floorBadges[m[1]];
      return null;
    },
  };
}

// Build floating musicGroupBadge mock outside floor cards
const groupThumbAttrs = {};
const musicGroupThumb = {
  dataset: {},
  style: { display: "none" },
  src: "",
  getAttribute: (k) => groupThumbAttrs[k] || "",
  setAttribute: (k, v) => {
    groupThumbAttrs[k] = String(v);
    if (k === "src") musicGroupThumb.src = String(v);
  },
  removeAttribute: (k) => {
    delete groupThumbAttrs[k];
    if (k === "src") musicGroupThumb.src = "";
  },
};
const musicGroupLabel = { textContent: "Whole House" };
const musicGroupTrack = { textContent: "" };
const groupListeners = {};
const musicGroupBadge = {
  dataset: { groupEntity: "", runEntity: "" },
  classList: makeClassList(["music-group-badge"]),
  title: "",
  addEventListener: (evt, fn) => {
    groupListeners[evt] = fn;
  },
  querySelector: (sel) => {
    if (sel.includes("musicGroupLabel") || sel.includes("data-group-label")) return musicGroupLabel;
    if (sel.includes("musicGroupTrack") || sel.includes("data-group-track")) return musicGroupTrack;
    if (sel.includes("musicGroupThumb") || sel.includes("data-group-thumb")) return musicGroupThumb;
    return null;
  },
};

const mockShadowRoot = {
  getElementById: (id) => {
    if (id === "musicGroupBadge") return musicGroupBadge;
    if (id === "musicGroupLabel") return musicGroupLabel;
    if (id === "musicGroupTrack") return musicGroupTrack;
    if (id === "musicGroupThumb") return musicGroupThumb;
    return null;
  },
  querySelector: (sel) => {
    const m = sel.match(/\\[data-floor-card="([^"]+)"\\]/);
    if (m && floorCards[m[1]]) return floorCards[m[1]];
    if (sel.includes("musicGroupBadge") || sel.includes("music-group-badge")) return musicGroupBadge;
    return null;
  },
  querySelectorAll: (sel) => {
    if (sel === ".run-badge") return Object.values(badgesById);
    return [];
  },
};

const card = new BrockHouseCyclesCard();
card.shadowRoot = mockShadowRoot;
card._bindListeners(mockShadowRoot);

// Scenario 1: Only media_player.kitchen_speaker is playing
card._hass = {
  states: {
    "media_player.kitchen_speaker": {
      state: "playing",
      attributes: {
        media_title: "So What",
        media_artist: "Miles Davis",
        entity_picture: "/api/media_player_proxy/media_player.kitchen_speaker?token=abc",
      },
    },
    "media_player.rosie_s_speaker": { state: "idle", attributes: {} },
    "media_player.isaac_s_speaker": { state: "off", attributes: {} },
    "media_player.living_room_speaker": { state: "idle", attributes: {} },
    "media_player.bedroom_speaker": { state: "idle", attributes: {} },
    "media_player.office_speaker": { state: "idle", attributes: {} },
    "media_player.whole_house": { state: "off", attributes: {} },
    "media_player.home_group": { state: "idle", attributes: {} },
  },
};

card._updateState();

const kitchenBadge = badgesById["kitchen_music"];
const allMusicIds = ["kitchen_music", "rosie_music", "isaac_music", "living_music", "bedroom_music", "office_music"];
const activeRoomBadgesScenario1 = allMusicIds.filter((id) => badgesById[id].classList.contains("active"));

const scenario1 = {
  activeRoomBadges: activeRoomBadgesScenario1,
  kitchenText: kitchenBadge.textEl.textContent,
  kitchenThumbSrc: kitchenBadge.thumbEl.getAttribute("src"),
  kitchenHasThumb: kitchenBadge.classList.contains("has-thumb"),
  groupActive: musicGroupBadge.classList.contains("active"),
};

// Scenario 2: media_player.whole_house starts playing while kitchen_speaker & living_room_speaker also report playing
card._hass = {
  states: {
    ...card._hass.states,
    "media_player.living_room_speaker": {
      state: "playing",
      attributes: {
        media_title: "So What",
        media_artist: "Miles Davis",
        entity_picture: "/api/media_player_proxy/media_player.living_room_speaker?token=def",
      },
    },
    "media_player.whole_house": {
      state: "playing",
      attributes: {
        media_title: "So What",
        media_artist: "Miles Davis",
        entity_picture: "/api/media_player_proxy/media_player.whole_house?token=xyz",
      },
    },
  },
};

card._updateState();

// Click the floating Whole House group badge -> should open HA More Info for media_player.whole_house
if (groupListeners.click) {
  groupListeners.click({ stopPropagation: () => {} });
}

const activeRoomBadgesScenario2 = allMusicIds.filter((id) => badgesById[id].classList.contains("active"));
const scenario2 = {
  activeRoomBadges: activeRoomBadgesScenario2,
  kitchenHasThumb: kitchenBadge.classList.contains("has-thumb"),
  groupActive: musicGroupBadge.classList.contains("active"),
  groupEntity: musicGroupBadge.dataset.groupEntity,
  groupLabel: musicGroupLabel.textContent,
  groupTrack: musicGroupTrack.textContent,
  groupThumbSrc: musicGroupThumb.getAttribute("src"),
  groupHasThumb: musicGroupBadge.classList.contains("has-thumb"),
  moreInfoOpened: card._moreInfoEntities,
};

// Scenario 3: Whole House stops; 2F fallback media_player.kitchen_hub plays via app_name ("Spotify")
// and 1F fallback media_player.office_hub is paused with media_title + entity_picture_local (long title > 28 chars)
card._hass = {
  states: {
    "media_player.kitchen_speaker": { state: "paused", attributes: {} }, // paused without media_title -> inactive
    "media_player.rosie_s_speaker": { state: "idle", attributes: {} },
    "media_player.isaac_s_speaker": { state: "off", attributes: {} },
    "media_player.living_room_speaker": { state: "unavailable", attributes: {} },
    "media_player.kitchen_hub": {
      state: "playing",
      attributes: {
        app_name: "Spotify",
        entity_picture: "/api/media_player_proxy/media_player.kitchen_hub?token=hub1",
      },
    },
    "media_player.bedroom_speaker": { state: "idle", attributes: {} },
    "media_player.office_speaker": { state: "idle", attributes: {} },
    "media_player.office_hub": {
      state: "paused",
      attributes: {
        media_title: "Super Long Track Title That Exceeds Twenty Eight Characters",
        media_artist: "Artist Name",
        entity_picture_local: "/local/office_art.png",
      },
    },
    "media_player.whole_house": { state: "idle", attributes: {} },
    "media_player.home_group": { state: "off", attributes: {} },
  },
};

card._updateState();

const livingBadge = badgesById["living_music"];
const officeBadge = badgesById["office_music"];
const activeRoomBadgesScenario3 = allMusicIds.filter((id) => badgesById[id].classList.contains("active"));
const scenario3 = {
  activeRoomBadges: activeRoomBadgesScenario3,
  livingText: livingBadge.textEl.textContent,
  livingEntity: livingBadge.dataset.runEntity,
  livingThumbSrc: livingBadge.thumbEl.getAttribute("src"),
  officeText: officeBadge.textEl.textContent,
  officeTextLen: officeBadge.textEl.textContent.length,
  officeEntity: officeBadge.dataset.runEntity,
  officeThumbSrc: officeBadge.thumbEl.getAttribute("src"),
  groupActive: musicGroupBadge.classList.contains("active"),
};

// Scenario 4: media_player.home_group is paused with media_title -> activates Home Group badge & suppresses room speakers
card._hass = {
  states: {
    ...card._hass.states,
    "media_player.home_group": {
      state: "paused",
      attributes: {
        media_title: " Clair de Lune ",
        media_artist: "Debussy",
        entity_picture_local: "/api/media_player_proxy/media_player.home_group?token=hg",
      },
    },
  },
};

card._updateState();

const activeRoomBadgesScenario4 = allMusicIds.filter((id) => badgesById[id].classList.contains("active"));
const scenario4 = {
  activeRoomBadges: activeRoomBadgesScenario4,
  groupActive: musicGroupBadge.classList.contains("active"),
  groupEntity: musicGroupBadge.dataset.groupEntity,
  groupLabel: musicGroupLabel.textContent,
  groupTrack: musicGroupTrack.textContent,
  groupThumbSrc: musicGroupThumb.getAttribute("src"),
};

console.log(JSON.stringify({ scenario1, scenario2, scenario3, scenario4 }));
"""
    )

    with tempfile.NamedTemporaryFile(mode="w", suffix=".js", encoding="utf-8", delete=False) as tmp:
        tmp.write(harness)
        tmp_path = Path(tmp.name)
    try:
        proc = subprocess.run(
            ["node", str(tmp_path)],
            capture_output=True,
            text=True,
            timeout=15,
            check=True,
        )
        res = json.loads(proc.stdout.strip())
    finally:
        tmp_path.unlink(missing_ok=True)

    # Assertions for Scenario 1: single room speaker playing
    s1 = res["scenario1"]
    assert s1["activeRoomBadges"] == ["kitchen_music"]
    assert s1["kitchenText"] == "So What · Miles Davis"
    assert s1["kitchenThumbSrc"] == "/api/media_player_proxy/media_player.kitchen_speaker?token=abc"
    assert s1["kitchenHasThumb"] is True
    assert s1["groupActive"] is False

    # Assertions for Scenario 2: Whole House group playing suppresses individual room badges
    s2 = res["scenario2"]
    assert s2["activeRoomBadges"] == []
    assert s2["kitchenHasThumb"] is False
    assert s2["groupActive"] is True
    assert s2["groupEntity"] == "media_player.whole_house"
    assert s2["groupLabel"] == "Whole House"
    assert s2["groupTrack"] == "So What · Miles Davis"
    assert s2["groupThumbSrc"] == "/api/media_player_proxy/media_player.whole_house?token=xyz"
    assert s2["groupHasThumb"] is True
    assert s2["moreInfoOpened"] == ["media_player.whole_house"]

    # Assertions for Scenario 3: fallback entities (kitchen_hub & office_hub), app_name formatting, 28-char truncation
    s3 = res["scenario3"]
    assert s3["activeRoomBadges"] == ["living_music", "office_music"]
    assert s3["livingText"] == "Living: Spotify"
    assert s3["livingEntity"] == "media_player.kitchen_hub"
    assert s3["livingThumbSrc"] == "/api/media_player_proxy/media_player.kitchen_hub?token=hub1"
    assert s3["officeEntity"] == "media_player.office_hub"
    assert s3["officeTextLen"] == 28
    assert s3["officeText"].endswith("…")
    assert s3["officeThumbSrc"] == "/local/office_art.png"
    assert s3["groupActive"] is False

    # Assertions for Scenario 4: Home Group paused with media_title activates floating badge and suppresses rooms
    s4 = res["scenario4"]
    assert s4["activeRoomBadges"] == []
    assert s4["groupActive"] is True
    assert s4["groupEntity"] == "media_player.home_group"
    assert s4["groupLabel"] == "Home Group"
    assert s4["groupTrack"] == "Clair de Lune · Debussy"
    assert s4["groupThumbSrc"] == "/api/media_player_proxy/media_player.home_group?token=hg"


def test_dry_run_and_verify_only_cli() -> None:
    patched = mod.patch_card_js(CARD_FIXTURE_UNPATCHED)

    with (
        mock.patch.object(mod, "resolve_ha_credentials", return_value=("http://198.51.100.10:8123", "tok")),
        mock.patch.object(mod, "fetch_live_card_and_backup", return_value=CARD_FIXTURE_UNPATCHED) as mock_fetch,
        mock.patch.object(mod, "deploy_card_to_host1") as mock_deploy,
    ):
        rc = mod.main(["--dry-run"])
    assert rc == 0
    mock_fetch.assert_called_once_with(mod.BACKUP_CARD_PATH)
    mock_deploy.assert_not_called()

    async def fake_ws(*_args: object, **_kwargs: object) -> list[dict[str, str]]:
        return [{"id": "1", "url": "/local/brock-house-3d/brock-house-cycles-card.js?v=123456"}]

    with (
        mock.patch.object(mod, "resolve_ha_credentials", return_value=("http://198.51.100.10:8123", "tok")),
        mock.patch.object(mod, "_ws_lovelace_resources", side_effect=fake_ws),
        mock.patch.object(mod, "_http_get", return_value=(200, patched.encode("utf-8"))),
    ):
        rc_verify = mod.main(["--verify-only"])
    assert rc_verify == 0
