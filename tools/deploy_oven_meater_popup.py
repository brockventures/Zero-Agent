#!/usr/bin/env python3
"""Deploy 3D floorplan LG Oven & MEATER+ badges and Cook Monitor popup to Home Assistant.

Steps executed in full deploy mode:
1. Fetch live brock-house-cycles-card.js from Host 1 and back it up locally to
   /workspace/scratch/brock-house-cycles-card.oven-meater.backup.js.
2. Idempotently patch brock-house-cycles-card.js with marker-delimited
   (/* OVEN_MEATER_..._BEGIN */ / /* OVEN_MEATER_..._END */) config, SVG icons,
   Floor 3 kitchen oven hotspot + active-only LG Oven and MEATER+ badges,
   interactive Cook Monitor & LG Dual Oven popup modal (radial gauge, ETA,
   target temp steppers, meat presets), sigParts detection, and live updates.
3. Validate patched JS syntax via `node --check`.
4. Deploy updated JS card to Host 1 Volume 2 NVMe, bump the HA Lovelace
   resource cache-buster via WebSocket, and verify HTTP 200 and live content.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import io
import json
import os
import posixpath
import re
import shlex
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))
_TOOLS_DIR = Path(__file__).resolve().parent
if str(_TOOLS_DIR) not in sys.path:
    sys.path.insert(1, str(_TOOLS_DIR))

from tools import nas_config

BACKUP_CARD_PATH = Path("/workspace/scratch/brock-house-cycles-card.oven-meater.backup.js")

ALL_OVEN_MEATER_ENTITIES: tuple[str, ...] = (
    "sensor.oven_upper_current_status",
    "sensor.oven_lower_current_status",
    "number.oven_upper_temperature",
    "number.oven_lower_temperature",
    "select.oven_upper_cook_mode",
    "select.oven_lower_cook_mode",
    "select.oven_upper_operation",
    "select.oven_lower_operation",
    "sensor.meater_meater_internal_temperature",
    "sensor.meater_meater_ambient_temperature",
    "sensor.meater_meater_battery",
    "sensor.meater_meater_eta",
    "sensor.meater_meater_cook_status",
    "binary_sensor.meater_meater_connected",
    "number.meater_meater_target_temperature",
    "select.meater_meater_cook_preset",
)

REQUIRED_MARKERS: tuple[str, ...] = (
    "/* OVEN_MEATER_CONFIG_BEGIN */",
    "/* OVEN_MEATER_CONFIG_END */",
    "/* OVEN_MEATER_ICONS_BEGIN */",
    "/* OVEN_MEATER_ICONS_END */",
    "/* OVEN_MEATER_CSS_BEGIN */",
    "/* OVEN_MEATER_CSS_END */",
    "/* OVEN_MEATER_HELPER_BEGIN */",
    "/* OVEN_MEATER_HELPER_END */",
    "/* OVEN_MEATER_HTML_BEGIN */",
    "/* OVEN_MEATER_HTML_END */",
    "/* OVEN_MEATER_MODAL_HTML_BEGIN */",
    "/* OVEN_MEATER_MODAL_HTML_END */",
    "/* OVEN_MEATER_LISTENERS_BEGIN */",
    "/* OVEN_MEATER_LISTENERS_END */",
    "/* OVEN_MEATER_SIG_BEGIN */",
    "/* OVEN_MEATER_SIG_END */",
    "/* OVEN_MEATER_UPDATE_BEGIN */",
    "/* OVEN_MEATER_UPDATE_END */",
    "/* OVEN_MEATER_MODAL_REFRESH_BEGIN */",
    "/* OVEN_MEATER_MODAL_REFRESH_END */",
)

OVEN_MEATER_CONFIG_JS = """/* OVEN_MEATER_CONFIG_BEGIN */
const OVEN_MEATER_CONFIG = {
  floorId: "floor3",
  hotspot: { x: 57.5, y: 45.0 },
  ovenBadgePos: { x: 57.0, y: 42.0 },
  meaterBadgePos: { x: 58.5, y: 46.5 },
  activeOvenStates: ["preheating", "cooking_in_progress", "cooling", "done"],
  upper: {
    label: "Upper Oven",
    statusEntity: "sensor.oven_upper_current_status",
    tempEntity: "number.oven_upper_temperature",
    modeEntity: "select.oven_upper_cook_mode",
    opEntity: "select.oven_upper_operation",
  },
  lower: {
    label: "Lower Oven",
    statusEntity: "sensor.oven_lower_current_status",
    tempEntity: "number.oven_lower_temperature",
    modeEntity: "select.oven_lower_cook_mode",
    opEntity: "select.oven_lower_operation",
  },
  meater: {
    internalEntity: "sensor.meater_meater_internal_temperature",
    ambientEntity: "sensor.meater_meater_ambient_temperature",
    batteryEntity: "sensor.meater_meater_battery",
    etaEntity: "sensor.meater_meater_eta",
    statusEntity: "sensor.meater_meater_cook_status",
    connectedEntity: "binary_sensor.meater_meater_connected",
    targetEntity: "number.meater_meater_target_temperature",
    presetEntity: "select.meater_meater_cook_preset",
    minTargetF: 100,
    maxTargetF: 225,
    carryoverPullOffsetF: 5,
    fallbackPresets: [
      "Custom",
      "Turkey Breast (165°F)",
      "Turkey Thigh (175°F)",
      "Prime Rib Med-Rare (130°F)",
      "Steak Med-Rare (135°F)",
      "Pork Chop (145°F)",
      "Chicken (165°F)",
      "Pork Shoulder (203°F)",
      "Brisket (203°F)",
    ],
  },
};
/* OVEN_MEATER_CONFIG_END */"""

OVEN_MEATER_ICONS_JS = """  /* OVEN_MEATER_ICONS_BEGIN */
  oven: `<svg viewBox="0 0 24 24"><path fill="currentColor" d="M18,2H6A2,2 0 0,0 4,4V20A2,2 0 0,0 6,22H18A2,2 0 0,0 20,20V4A2,2 0 0,0 18,2M7.5,4.5A1,1 0 0,1 8.5,5.5A1,1 0 0,1 7.5,6.5A1,1 0 0,1 6.5,5.5A1,1 0 0,1 7.5,4.5M12,4.5A1,1 0 0,1 13,5.5A1,1 0 0,1 12,6.5A1,1 0 0,1 11,5.5A1,1 0 0,1 12,4.5M16.5,4.5A1,1 0 0,1 17.5,5.5A1,1 0 0,1 16.5,6.5A1,1 0 0,1 15.5,5.5A1,1 0 0,1 16.5,4.5M18,19H6V9H18V19M12,11C10.5,12.5 9.8,13.9 9.8,15.1A2.2,2.2 0 0,0 12,17.3A2.2,2.2 0 0,0 14.2,15.1C14.2,13.9 13.5,12.5 12,11Z"/></svg>`,
  meater: `<svg viewBox="0 0 24 24"><path fill="currentColor" d="M20.2,3.8A2.8,2.8 0 0,0 16.2,3.8L13.4,6.6L17.4,10.6L20.2,7.8A2.8,2.8 0 0,0 20.2,3.8M12.3,7.7L4.4,15.6L2.5,20.5L3.5,21.5L8.4,19.6L16.3,11.7L12.3,7.7M8.5,2.5A7.5,7.5 0 0,0 3.2,4.7L4.6,6.1A5.5,5.5 0 0,1 8.5,4.5V2.5M8.5,6.5A3.5,3.5 0 0,0 6,7.5L7.4,8.9A1.5,1.5 0 0,1 8.5,8.5V6.5Z"/></svg>`,
  /* OVEN_MEATER_ICONS_END */"""

OVEN_MEATER_CSS = """        /* OVEN_MEATER_CSS_BEGIN */
        .cook-hotspot {
          position: absolute;
          transform: translate(-50%, -50%);
          width: 44px;
          height: 44px;
          border-radius: 50%;
          border: 1.5px dashed rgba(255, 168, 92, 0.0);
          background: rgba(255, 138, 60, 0.0);
          pointer-events: auto;
          cursor: pointer;
          z-index: 17;
          padding: 0;
          transition: background 0.18s ease, border-color 0.18s ease, transform 0.16s ease, box-shadow 0.18s ease;
        }
        .cook-hotspot:hover {
          background: rgba(255, 145, 72, 0.18);
          border-color: rgba(255, 184, 112, 0.65);
          box-shadow: 0 0 16px rgba(255, 132, 56, 0.35);
          transform: translate(-50%, -50%) scale(1.06);
        }
        .run-badge.oven-badge,
        .run-badge.meater-badge {
          z-index: 18;
        }
        .run-badge.oven-active {
          border-color: rgba(255, 148, 72, 0.9);
          background: rgba(54, 24, 10, 0.95);
          color: #ffe2c6;
          box-shadow: 0 0 14px rgba(255, 126, 48, 0.34), 0 4px 14px rgba(0, 0, 0, 0.48);
        }
        .run-badge.oven-active .run-dot { background: #ff9448; }
        .run-badge.oven-cooling {
          border-color: rgba(255, 196, 86, 0.78);
          background: rgba(46, 33, 10, 0.94);
          color: #ffe7ad;
        }
        .run-badge.oven-cooling .run-dot { background: #ffc456; }
        .run-badge.oven-done {
          border-color: rgba(76, 217, 138, 0.85);
          background: rgba(12, 36, 24, 0.95);
          color: #c2f9da;
        }
        .run-badge.oven-done .run-dot { background: #4cd98a; }
        .run-badge.meater-standby {
          border-color: rgba(86, 180, 255, 0.78);
          background: rgba(12, 28, 48, 0.95);
          color: #d2ebff;
        }
        .run-badge.meater-standby .run-dot { background: #56b4ff; }
        .run-badge.meater-cooking {
          border-color: rgba(255, 146, 72, 0.88);
          background: rgba(52, 22, 10, 0.95);
          color: #ffe2cc;
          box-shadow: 0 0 14px rgba(255, 128, 56, 0.32), 0 4px 14px rgba(0, 0, 0, 0.48);
        }
        .run-badge.meater-cooking .run-dot { background: #ff9248; }
        .run-badge.meater-almost {
          border-color: rgba(255, 204, 72, 0.92);
          background: rgba(54, 38, 8, 0.96);
          color: #fff0b8;
          box-shadow: 0 0 14px rgba(255, 196, 60, 0.34), 0 4px 14px rgba(0, 0, 0, 0.48);
        }
        .run-badge.meater-almost .run-dot { background: #ffcc48; }
        .run-badge.meater-pull {
          border-color: rgba(255, 86, 86, 0.96);
          background: rgba(68, 14, 18, 0.96);
          color: #ffd0d0;
          box-shadow: 0 0 16px rgba(255, 72, 72, 0.46), 0 4px 14px rgba(0, 0, 0, 0.48);
        }
        .run-badge.meater-pull .run-dot { background: #ff5252; }
        .run-badge.meater-done {
          border-color: rgba(74, 222, 128, 0.92);
          background: rgba(12, 42, 24, 0.96);
          color: #c4fbd6;
          box-shadow: 0 0 14px rgba(74, 222, 128, 0.34), 0 4px 14px rgba(0, 0, 0, 0.48);
        }
        .run-badge.meater-done .run-dot { background: #4ade80; }

        .cook-backdrop {
          position: absolute;
          inset: 0;
          z-index: 52;
          background: rgba(5, 9, 16, 0.66);
          display: none;
          align-items: center;
          justify-content: center;
          padding: 16px;
        }
        .cook-backdrop.open {
          display: flex;
        }
        .cook-modal {
          width: min(94vw, 440px);
          max-height: 90%;
          overflow-y: auto;
          background: rgba(14, 22, 36, 0.98);
          border: 1px solid rgba(255, 152, 78, 0.36);
          border-radius: 20px;
          box-shadow: 0 24px 64px rgba(0, 0, 0, 0.72);
          padding: 16px 18px 18px;
          color: #eef5fc;
          display: flex;
          flex-direction: column;
          gap: 12px;
        }
        .cm-head {
          display: flex;
          align-items: center;
          justify-content: space-between;
          gap: 10px;
        }
        .cm-title-wrap {
          display: flex;
          align-items: center;
          gap: 9px;
          min-width: 0;
        }
        .cm-head-icon {
          width: 32px;
          height: 32px;
          border-radius: 10px;
          background: rgba(255, 146, 72, 0.16);
          border: 1px solid rgba(255, 146, 72, 0.42);
          color: #ffb578;
          display: inline-flex;
          align-items: center;
          justify-content: center;
          flex-shrink: 0;
        }
        .cm-head-icon svg { width: 18px; height: 18px; }
        .cm-title { font-size: 15.5px; font-weight: 700; color: #f8fafc; }
        .cm-sub { font-size: 11.5px; color: #9bb0c9; margin-top: 1px; }
        .cm-section-card {
          background: rgba(255, 255, 255, 0.04);
          border: 1px solid rgba(255, 255, 255, 0.09);
          border-radius: 15px;
          padding: 12px 13px;
          display: flex;
          flex-direction: column;
          gap: 10px;
        }
        .cm-sec-head {
          display: flex;
          align-items: center;
          justify-content: space-between;
          gap: 8px;
        }
        .cm-sec-label {
          display: inline-flex;
          align-items: center;
          gap: 6px;
          font-size: 11.5px;
          font-weight: 700;
          letter-spacing: 0.04em;
          text-transform: uppercase;
          color: #a9bfd8;
        }
        .cm-sec-label svg { width: 14px; height: 14px; color: #ffb272; }
        .cm-status-pill {
          font-size: 11px;
          font-weight: 700;
          padding: 3px 9px;
          border-radius: 999px;
          border: 1px solid rgba(148, 163, 184, 0.35);
          background: rgba(148, 163, 184, 0.14);
          color: #dbe7f3;
          white-space: nowrap;
        }
        .cm-status-pill.cooking {
          border-color: rgba(255, 146, 72, 0.72);
          background: rgba(255, 146, 72, 0.2);
          color: #ffd7b3;
        }
        .cm-status-pill.almost {
          border-color: rgba(255, 204, 72, 0.78);
          background: rgba(255, 204, 72, 0.22);
          color: #ffefc0;
        }
        .cm-status-pill.pull {
          border-color: rgba(255, 86, 86, 0.88);
          background: rgba(255, 86, 86, 0.26);
          color: #ffd2d2;
        }
        .cm-status-pill.done {
          border-color: rgba(74, 222, 128, 0.8);
          background: rgba(74, 222, 128, 0.22);
          color: #c8fce0;
        }
        .cm-status-pill.standby {
          border-color: rgba(86, 180, 255, 0.65);
          background: rgba(86, 180, 255, 0.18);
          color: #cbe8ff;
        }
        .cm-gauge-row {
          display: flex;
          align-items: center;
          gap: 14px;
        }
        .cm-gauge-wrap {
          position: relative;
          width: 128px;
          height: 116px;
          flex-shrink: 0;
        }
        .cm-gauge-svg {
          width: 100%;
          height: 100%;
          display: block;
        }
        .cm-gauge-center {
          position: absolute;
          inset: 0;
          display: flex;
          flex-direction: column;
          align-items: center;
          justify-content: center;
          padding-top: 6px;
          pointer-events: none;
        }
        .cm-gauge-temp {
          font-size: 24px;
          font-weight: 800;
          letter-spacing: -0.03em;
          color: #ffffff;
          line-height: 1.05;
        }
        .cm-gauge-target {
          font-size: 11px;
          font-weight: 600;
          color: #9cb6d4;
          margin-top: 2px;
        }
        .cm-stats-grid {
          flex: 1 1 0;
          display: grid;
          grid-template-columns: repeat(2, 1fr);
          gap: 7px;
        }
        .cm-stat-box {
          background: rgba(255, 255, 255, 0.04);
          border: 1px solid rgba(255, 255, 255, 0.07);
          border-radius: 10px;
          padding: 6px 8px;
        }
        .cm-stat-k {
          font-size: 10px;
          font-weight: 700;
          text-transform: uppercase;
          letter-spacing: 0.04em;
          color: #849ebb;
        }
        .cm-stat-v {
          font-size: 12.5px;
          font-weight: 700;
          color: #f1f5f9;
          margin-top: 2px;
          white-space: nowrap;
        }
        .cm-stepper-card {
          display: flex;
          align-items: center;
          justify-content: space-between;
          gap: 8px;
          background: rgba(255, 255, 255, 0.04);
          border: 1px solid rgba(255, 255, 255, 0.08);
          border-radius: 12px;
          padding: 8px 10px;
        }
        .cm-step-group {
          display: flex;
          align-items: center;
          gap: 5px;
        }
        .cm-step-btn {
          min-width: 34px;
          height: 30px;
          padding: 0 7px;
          border-radius: 9px;
          border: 1px solid rgba(255, 255, 255, 0.2);
          background: rgba(255, 255, 255, 0.08);
          color: #ffffff;
          font-size: 12px;
          font-weight: 700;
          cursor: pointer;
        }
        .cm-step-btn:hover {
          background: rgba(255, 146, 72, 0.28);
          border-color: rgba(255, 146, 72, 0.72);
        }
        .cm-step-readout {
          text-align: center;
          min-width: 68px;
        }
        .cm-step-readout-val {
          font-size: 16px;
          font-weight: 800;
          color: #ffffff;
        }
        .cm-step-readout-lbl {
          font-size: 9.5px;
          color: #8ea7c4;
          text-transform: uppercase;
          letter-spacing: 0.04em;
        }
        .cm-presets {
          display: flex;
          flex-wrap: wrap;
          gap: 5px;
        }
        .cm-preset-btn {
          border: 1px solid rgba(255, 255, 255, 0.14);
          background: rgba(255, 255, 255, 0.05);
          color: #c8daee;
          font-size: 10.5px;
          font-weight: 700;
          padding: 5px 8.5px;
          border-radius: 999px;
          cursor: pointer;
          transition: background 0.14s ease, border-color 0.14s ease;
        }
        .cm-preset-btn:hover {
          background: rgba(255, 255, 255, 0.13);
        }
        .cm-preset-btn.active {
          border-color: rgba(255, 146, 72, 0.85);
          background: rgba(255, 146, 72, 0.24);
          color: #ffffff;
        }
        .cm-oven-grid {
          display: grid;
          grid-template-columns: repeat(2, 1fr);
          gap: 8px;
        }
        .cm-oven-card {
          background: rgba(255, 255, 255, 0.035);
          border: 1px solid rgba(255, 255, 255, 0.09);
          border-radius: 12px;
          padding: 9px 10px;
          display: flex;
          flex-direction: column;
          gap: 4px;
        }
        .cm-oven-card.active {
          border-color: rgba(255, 148, 72, 0.72);
          background: rgba(255, 136, 56, 0.12);
        }
        .cm-oven-top {
          display: flex;
          align-items: center;
          justify-content: space-between;
          gap: 6px;
        }
        .cm-oven-name {
          font-size: 11.5px;
          font-weight: 700;
          color: #e2ecf7;
        }
        .cm-oven-temp {
          font-size: 20px;
          font-weight: 800;
          color: #ffffff;
          line-height: 1.1;
        }
        .cm-oven-meta {
          font-size: 11px;
          color: #9bb2cb;
        }
        .cm-foot {
          display: flex;
          justify-content: space-between;
          align-items: center;
          padding-top: 2px;
        }
        /* OVEN_MEATER_CSS_END */"""

OVEN_MEATER_HELPER_JS = """  /* OVEN_MEATER_HELPER_BEGIN */
  _escapeHtml(raw) {
    return String(raw ?? "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  _formatCookMode(rawMode) {
    const m = String(rawMode || "").trim().toLowerCase();
    if (!m || m === "unknown" || m === "unavailable" || m === "none") return "Idle";
    const labels = {
      bake: "Bake",
      convection_bake: "Convection Bake",
      convection_roast: "Convection Roast",
      broil: "Broil",
      warm: "Keep Warm",
      proof: "Proof",
      air_fry: "Air Fry",
    };
    return labels[m] || m.replace(/_/g, " ").replace(/\\b\\w/g, (c) => c.toUpperCase());
  }

  _formatOvenStatus(rawStatus) {
    const s = String(rawStatus || "").trim().toLowerCase();
    const labels = {
      initial: "Standby",
      preheating: "Preheating",
      cooking_in_progress: "Cooking",
      cooling: "Cooling",
      done: "Done",
    };
    if (!s || s === "unknown" || s === "unavailable") return "Unavailable";
    return labels[s] || s.replace(/_/g, " ").replace(/\\b\\w/g, (c) => c.toUpperCase());
  }

  _evalCavity(cfg) {
    const states = this._hass?.states || {};
    const st = states[cfg.statusEntity];
    const rawStatus = String(st?.state || "").trim().toLowerCase();
    const active = OVEN_MEATER_CONFIG.activeOvenStates.includes(rawStatus);
    const setTemp = this._readNumericState(cfg.tempEntity);
    const modeRaw = states[cfg.modeEntity]?.state || "";
    const opRaw = states[cfg.opEntity]?.state || "";
    return {
      label: cfg.label,
      statusEntity: cfg.statusEntity,
      rawStatus,
      statusText: this._formatOvenStatus(rawStatus),
      active,
      setTemp: setTemp !== null && setTemp > 0 ? Math.round(setTemp) : null,
      modeText: this._formatCookMode(modeRaw),
      opText: opRaw && !["unknown", "unavailable"].includes(String(opRaw).toLowerCase()) ? String(opRaw) : "",
    };
  }

  _evalOvenStatus() {
    const upper = this._evalCavity(OVEN_MEATER_CONFIG.upper);
    const lower = this._evalCavity(OVEN_MEATER_CONFIG.lower);
    const activeCavities = [upper, lower].filter((c) => c.active);
    const anyActive = activeCavities.length > 0;

    let badgeClass = "oven-active";
    let badgeText = "Oven";
    if (anyActive) {
      const primary = activeCavities[0];
      if (activeCavities.every((c) => c.rawStatus === "done")) {
        badgeClass = "oven-done";
        badgeText = activeCavities.length > 1 ? "Ovens Done" : "Oven Done";
      } else if (activeCavities.every((c) => c.rawStatus === "cooling")) {
        badgeClass = "oven-cooling";
        badgeText = "Oven Cooling";
      } else if (activeCavities.length > 1) {
        const temps = activeCavities.map((c) => (c.setTemp !== null ? `${c.setTemp}°F` : c.statusText));
        badgeText = `2 Ovens · ${temps.join("/")}`;
      } else if (primary.rawStatus === "preheating") {
        badgeText = primary.setTemp !== null ? `Preheat ${primary.setTemp}°F` : "Preheating";
      } else {
        badgeText = primary.setTemp !== null ? `Oven ${primary.setTemp}°F` : `Oven ${primary.statusText}`;
      }
    }

    return {
      active: anyActive,
      badgeClass,
      badgeText,
      title: anyActive
        ? activeCavities.map((c) => `${c.label}: ${c.statusText}${c.setTemp ? ` (${c.setTemp}°F ${c.modeText})` : ""}`).join(" · ")
        : "LG Dual Oven · Standby",
      upper,
      lower,
    };
  }

  _evalMeaterStatus() {
    const states = this._hass?.states || {};
    const cfg = OVEN_MEATER_CONFIG.meater;
    const connState = String(states[cfg.connectedEntity]?.state || "").trim().toLowerCase();
    const connected = connState === "on";
    const statusSt = states[cfg.statusEntity];
    const cookStatus = String(statusSt?.state || "").trim();
    const cookStatusLower = cookStatus.toLowerCase();
    const isKnownStatus = Boolean(cookStatus) && !["unknown", "unavailable", "none"].includes(cookStatusLower);
    const statusActive = isKnownStatus && cookStatusLower !== "standby";
    const active = connected || statusActive;

    const internalF = this._readNumericState(cfg.internalEntity);
    const ambientF = this._readNumericState(cfg.ambientEntity);
    const batteryPct = this._readNumericState(cfg.batteryEntity);
    const etaSt = states[cfg.etaEntity];
    const etaMin = this._readNumericState(cfg.etaEntity);
    const rateRaw = etaSt?.attributes?.rate_f_per_min;
    const rateFPerMin = rateRaw !== undefined && rateRaw !== null && !Number.isNaN(Number(rateRaw)) ? Number(rateRaw) : null;

    const targetNum = this._readNumericState(cfg.targetEntity);
    const attrTarget = statusSt?.attributes?.target_temp_f;
    const targetF =
      targetNum !== null
        ? Math.round(targetNum)
        : attrTarget !== undefined && attrTarget !== null && !Number.isNaN(Number(attrTarget))
          ? Math.round(Number(attrTarget))
          : null;

    const targetSt = states[cfg.targetEntity];
    const minTarget = Number(targetSt?.attributes?.min ?? cfg.minTargetF) || cfg.minTargetF;
    const maxTarget = Number(targetSt?.attributes?.max ?? cfg.maxTargetF) || cfg.maxTargetF;

    const presetSt = states[cfg.presetEntity];
    const presetRaw = String(presetSt?.state || statusSt?.attributes?.cook_preset || "").trim();
    const currentPreset =
      presetRaw && !["unknown", "unavailable", "none"].includes(presetRaw.toLowerCase()) ? presetRaw : "Custom";
    const presetOptions =
      Array.isArray(presetSt?.attributes?.options) && presetSt.attributes.options.length
        ? presetSt.attributes.options.map((o) => String(o))
        : cfg.fallbackPresets;

    const rssiRaw = statusSt?.attributes?.rssi;
    const rssi = rssiRaw !== undefined && rssiRaw !== null && !Number.isNaN(Number(rssiRaw)) ? Math.round(Number(rssiRaw)) : null;

    let badgeClass = "meater-standby";
    let pillClass = "standby";
    if (cookStatusLower === "pull now") {
      badgeClass = "meater-pull";
      pillClass = "pull";
    } else if (cookStatusLower === "target reached") {
      badgeClass = "meater-done";
      pillClass = "done";
    } else if (cookStatusLower === "almost ready") {
      badgeClass = "meater-almost";
      pillClass = "almost";
    } else if (cookStatusLower === "cooking") {
      badgeClass = "meater-cooking";
      pillClass = "cooking";
    }

    const intRound = internalF !== null ? Math.round(internalF) : null;
    let badgeText = "MEATER+";
    if (cookStatusLower === "pull now") {
      badgeText = intRound !== null ? `${intRound}°F · Pull!` : "Pull Now!";
    } else if (cookStatusLower === "target reached") {
      badgeText = intRound !== null ? `${intRound}°F · Done` : "Target Done";
    } else if (cookStatusLower === "almost ready") {
      badgeText = intRound !== null ? `${intRound}°F · Soon` : "Almost Ready";
    } else if (cookStatusLower === "cooking") {
      badgeText = intRound !== null && targetF !== null ? `${intRound}°/${targetF}°F` : intRound !== null ? `${intRound}°F` : "Cooking";
    } else if (intRound !== null) {
      badgeText = `MEATER ${intRound}°F`;
    }

    return {
      active,
      connected,
      cookStatus: isKnownStatus ? cookStatus : connected ? "Connected" : "Disconnected",
      pillClass: active ? pillClass : "",
      badgeClass,
      badgeText,
      internalF,
      ambientF,
      batteryPct,
      etaMin,
      rateFPerMin,
      targetF,
      minTarget,
      maxTarget,
      currentPreset,
      presetOptions,
      rssi,
    };
  }

  _formatCookEta(etaMin) {
    if (etaMin === null || Number.isNaN(etaMin) || etaMin < 0) return "--";
    const mins = Math.round(etaMin);
    if (mins === 0) return "Now";
    if (mins < 60) return `${mins}m`;
    const h = Math.floor(mins / 60);
    const m = mins % 60;
    return m === 0 ? `${h}h` : `${h}h ${m}m`;
  }

  _collectOvenMeaterSigParts() {
    const states = this._hass?.states || {};
    const cfg = OVEN_MEATER_CONFIG;
    const ids = [
      cfg.upper.statusEntity,
      cfg.upper.tempEntity,
      cfg.upper.modeEntity,
      cfg.upper.opEntity,
      cfg.lower.statusEntity,
      cfg.lower.tempEntity,
      cfg.lower.modeEntity,
      cfg.lower.opEntity,
      cfg.meater.internalEntity,
      cfg.meater.ambientEntity,
      cfg.meater.batteryEntity,
      cfg.meater.etaEntity,
      cfg.meater.statusEntity,
      cfg.meater.connectedEntity,
      cfg.meater.targetEntity,
      cfg.meater.presetEntity,
    ];
    const parts = [this._cookModalOpen ? "cook-open" : ""];
    for (const eid of ids) {
      const st = states[eid];
      if (!st) {
        parts.push("");
        continue;
      }
      const a = st.attributes || {};
      parts.push(
        st.state || "",
        a.target_temp_f ?? "",
        a.cook_preset ?? "",
        a.rssi ?? "",
        a.rate_f_per_min ?? "",
        Array.isArray(a.options) ? a.options.join(",") : ""
      );
    }
    return parts;
  }

  _openCookModal() {
    this._cookModalOpen = true;
    this._renderCookModal();
  }

  _closeCookModal() {
    this._cookModalOpen = false;
    const backdrop = this.shadowRoot?.getElementById("cookBackdrop");
    if (backdrop) {
      backdrop.classList.remove("open");
      backdrop.setAttribute("aria-hidden", "true");
    }
  }

  _stepMeaterTargetTemp(delta) {
    if (!this._hass || Number.isNaN(delta)) return;
    const m = this._evalMeaterStatus();
    const base = m.targetF !== null ? m.targetF : 165;
    const next = Math.max(m.minTarget, Math.min(m.maxTarget, Math.round(base + delta)));
    this._hass.callService("number", "set_value", {
      entity_id: OVEN_MEATER_CONFIG.meater.targetEntity,
      value: next,
    });
  }

  _selectMeaterPreset(option) {
    if (!this._hass || !option) return;
    this._hass.callService("select", "select_option", {
      entity_id: OVEN_MEATER_CONFIG.meater.presetEntity,
      option,
    });
  }

  _buildCookGaugeSvg(meater) {
    const cx = 64;
    const cy = 62;
    const r = 46;
    const startDeg = 135;
    const sweepDeg = 270;
    const polar = (deg) => {
      const rad = (deg * Math.PI) / 180;
      return [cx + r * Math.cos(rad), cy + r * Math.sin(rad)];
    };
    const arcPath = (degA, degB) => {
      const [x1, y1] = polar(degA);
      const [x2, y2] = polar(degB);
      const large = Math.abs(degB - degA) > 180 ? 1 : 0;
      return `M ${x1.toFixed(2)} ${y1.toFixed(2)} A ${r} ${r} 0 ${large} 1 ${x2.toFixed(2)} ${y2.toFixed(2)}`;
    };

    const floorTemp = 32;
    const target = meater.targetF !== null ? meater.targetF : 165;
    const span = Math.max(20, target - floorTemp);
    const pct =
      meater.internalF !== null
        ? Math.max(0, Math.min(1, (meater.internalF - floorTemp) / span))
        : 0;
    const progDeg = startDeg + sweepDeg * pct;

    const pullTemp = target - OVEN_MEATER_CONFIG.meater.carryoverPullOffsetF;
    const pullPct = Math.max(0.05, Math.min(0.98, (pullTemp - floorTemp) / span));
    const pullDeg = startDeg + sweepDeg * pullPct;
    const pullRad = (pullDeg * Math.PI) / 180;
    const px1 = cx + (r - 6) * Math.cos(pullRad);
    const py1 = cy + (r - 6) * Math.sin(pullRad);
    const px2 = cx + (r + 6) * Math.cos(pullRad);
    const py2 = cy + (r + 6) * Math.sin(pullRad);

    let stroke = "#ff9248";
    if (meater.pillClass === "pull") stroke = "#ff5252";
    else if (meater.pillClass === "done") stroke = "#4ade80";
    else if (meater.pillClass === "almost") stroke = "#ffcc48";
    else if (meater.pillClass === "standby") stroke = "#56b4ff";

    const trackD = arcPath(startDeg, startDeg + sweepDeg);
    const progD = pct > 0.01 ? arcPath(startDeg, progDeg) : "";

    return `
      <svg class="cm-gauge-svg" viewBox="0 0 128 116" aria-hidden="true">
        <path d="${trackD}" fill="none" stroke="rgba(255,255,255,0.12)" stroke-width="9" stroke-linecap="round" />
        ${progD ? `<path d="${progD}" fill="none" stroke="${stroke}" stroke-width="9" stroke-linecap="round" />` : ""}
        <line x1="${px1.toFixed(2)}" y1="${py1.toFixed(2)}" x2="${px2.toFixed(2)}" y2="${py2.toFixed(2)}" stroke="#fde047" stroke-width="2.5" stroke-linecap="round" />
      </svg>
    `;
  }

  _renderCookModal() {
    const sr = this.shadowRoot;
    if (!sr) return;
    const backdrop = sr.getElementById("cookBackdrop");
    const modal = sr.getElementById("cookModal");
    if (!backdrop || !modal) return;
    if (!this._cookModalOpen) {
      backdrop.classList.remove("open");
      backdrop.setAttribute("aria-hidden", "true");
      return;
    }
    backdrop.classList.add("open");
    backdrop.setAttribute("aria-hidden", "false");

    const oven = this._evalOvenStatus();
    const meater = this._evalMeaterStatus();

    const intStr = meater.internalF !== null ? `${meater.internalF.toFixed(1)}°` : "--°";
    const tgtStr = meater.targetF !== null ? `/ ${meater.targetF}°F` : "/ --°F";
    const ambStr = meater.ambientF !== null ? `${Math.round(meater.ambientF)}°F` : "--";
    const etaStr = this._formatCookEta(meater.etaMin);
    const rateStr =
      meater.rateFPerMin !== null
        ? ` (${meater.rateFPerMin >= 0 ? "+" : ""}${meater.rateFPerMin.toFixed(2)}°/m)`
        : "";
    const batStr = meater.batteryPct !== null ? `${Math.round(meater.batteryPct)}%` : "--";
    const rssiStr = meater.rssi !== null ? `${meater.rssi} dBm` : "--";
    const pullTarget = meater.targetF !== null ? `${meater.targetF - OVEN_MEATER_CONFIG.meater.carryoverPullOffsetF}°F` : "--";

    const presetHtml = meater.presetOptions
      .map((opt) => {
        const isActive = opt === meater.currentPreset;
        return `<button class="cm-preset-btn ${isActive ? "active" : ""}" data-cm-action="preset" data-cm-preset="${this._escapeHtml(opt)}">${this._escapeHtml(opt)}</button>`;
      })
      .join("");

    const renderCavityCard = (cav) => {
      const tempTxt = cav.setTemp !== null ? `${cav.setTemp}°F` : "--";
      const badgeCls =
        cav.rawStatus === "done"
          ? "done"
          : cav.rawStatus === "cooling"
            ? "almost"
            : cav.active
              ? "cooking"
              : "";
      return `
        <div class="cm-oven-card ${cav.active ? "active" : ""}">
          <div class="cm-oven-top">
            <span class="cm-oven-name">${this._escapeHtml(cav.label)}</span>
            <span class="cm-status-pill ${badgeCls}">${this._escapeHtml(cav.statusText)}</span>
          </div>
          <div class="cm-oven-temp">${tempTxt}</div>
          <div class="cm-oven-meta">${this._escapeHtml(cav.modeText)}${cav.opText ? ` · ${this._escapeHtml(cav.opText)}` : ""}</div>
        </div>
      `;
    };

    modal.innerHTML = `
      <div class="cm-head">
        <div class="cm-title-wrap">
          <span class="cm-head-icon">${SVG_ICONS.oven || ""}</span>
          <div>
            <div class="cm-title">Kitchen Cook Monitor</div>
            <div class="cm-sub">LG Dual Oven &amp; MEATER+ Wireless Probe</div>
          </div>
        </div>
        <button class="rd-close" data-cm-action="close" title="Close">×</button>
      </div>

      <div class="cm-section-card">
        <div class="cm-sec-head">
          <span class="cm-sec-label">${SVG_ICONS.meater || ""} MEATER+ Probe</span>
          <span class="cm-status-pill ${meater.pillClass}">${this._escapeHtml(meater.cookStatus)}</span>
        </div>
        <div class="cm-gauge-row">
          <div class="cm-gauge-wrap">
            ${this._buildCookGaugeSvg(meater)}
            <div class="cm-gauge-center">
              <div class="cm-gauge-temp">${intStr}</div>
              <div class="cm-gauge-target">${tgtStr}</div>
            </div>
          </div>
          <div class="cm-stats-grid">
            <div class="cm-stat-box">
              <div class="cm-stat-k">Ambient</div>
              <div class="cm-stat-v">${ambStr}</div>
            </div>
            <div class="cm-stat-box">
              <div class="cm-stat-k">ETA · Pull ${pullTarget}</div>
              <div class="cm-stat-v">${etaStr}${rateStr}</div>
            </div>
            <div class="cm-stat-box">
              <div class="cm-stat-k">Battery</div>
              <div class="cm-stat-v">${batStr}</div>
            </div>
            <div class="cm-stat-box">
              <div class="cm-stat-k">BLE Signal</div>
              <div class="cm-stat-v">${rssiStr}</div>
            </div>
          </div>
        </div>
        <div class="cm-stepper-card">
          <div class="cm-step-group">
            <button class="cm-step-btn" data-cm-action="step-target" data-cm-delta="-5">-5°</button>
            <button class="cm-step-btn" data-cm-action="step-target" data-cm-delta="-1">-1°</button>
          </div>
          <div class="cm-step-readout">
            <div class="cm-step-readout-val">${meater.targetF !== null ? `${meater.targetF}°F` : "--°F"}</div>
            <div class="cm-step-readout-lbl">Target Temp</div>
          </div>
          <div class="cm-step-group">
            <button class="cm-step-btn" data-cm-action="step-target" data-cm-delta="1">+1°</button>
            <button class="cm-step-btn" data-cm-action="step-target" data-cm-delta="5">+5°</button>
          </div>
        </div>
        <div class="cm-presets">${presetHtml}</div>
      </div>

      <div class="cm-section-card">
        <div class="cm-sec-head">
          <span class="cm-sec-label">${SVG_ICONS.oven || ""} LG Dual Oven</span>
          <span class="cm-status-pill ${oven.active ? "cooking" : ""}">${oven.active ? "Active" : "Standby"}</span>
        </div>
        <div class="cm-oven-grid">
          ${renderCavityCard(oven.upper)}
          ${renderCavityCard(oven.lower)}
        </div>
      </div>

      <div class="cm-foot">
        <button class="rd-mini-btn" data-cm-action="more-info" data-entity="${OVEN_MEATER_CONFIG.meater.statusEntity}">HA Details</button>
        <button class="rd-mini-btn" data-cm-action="close">Done</button>
      </div>
    `;
  }
  /* OVEN_MEATER_HELPER_END */"""

OVEN_MEATER_HTML = """                /* OVEN_MEATER_HTML_BEGIN */
                ${
                  f.id === OVEN_MEATER_CONFIG.floorId
                    ? `
                  <button class="cook-hotspot" data-cook-open="1" style="left:${OVEN_MEATER_CONFIG.hotspot.x}%;top:${OVEN_MEATER_CONFIG.hotspot.y}%;" title="Kitchen Oven & MEATER+ Cook Monitor" aria-label="Open Kitchen Cook Monitor"></button>
                  <button class="run-badge oven-badge" data-oven-meater-badge="lg_oven" data-cook-open="1" style="left:${OVEN_MEATER_CONFIG.ovenBadgePos.x}%;top:${OVEN_MEATER_CONFIG.ovenBadgePos.y}%;" title="LG Oven">
                    <span class="run-dot"></span>
                    <span class="run-icon">${SVG_ICONS.oven || ""}</span>
                    <span data-om-text="lg_oven">Oven</span>
                  </button>
                  <button class="run-badge meater-badge" data-oven-meater-badge="meater" data-cook-open="1" style="left:${OVEN_MEATER_CONFIG.meaterBadgePos.x}%;top:${OVEN_MEATER_CONFIG.meaterBadgePos.y}%;" title="MEATER+ Probe">
                    <span class="run-dot"></span>
                    <span class="run-icon">${SVG_ICONS.meater || ""}</span>
                    <span data-om-text="meater">MEATER+</span>
                  </button>
                `
                    : ""
                }
                /* OVEN_MEATER_HTML_END */"""

OVEN_MEATER_MODAL_HTML = """        /* OVEN_MEATER_MODAL_HTML_BEGIN */
        <div class="cook-backdrop" id="cookBackdrop" aria-hidden="true">
          <div class="cook-modal" id="cookModal"></div>
        </div>
        /* OVEN_MEATER_MODAL_HTML_END */"""

OVEN_MEATER_LISTENERS_JS = """    /* OVEN_MEATER_LISTENERS_BEGIN */
    sr.querySelectorAll("[data-cook-open]").forEach((btn) => {
      btn.addEventListener("click", (e) => {
        e.stopPropagation();
        this._openCookModal();
      });
    });

    sr.getElementById("cookBackdrop")?.addEventListener("click", (e) => {
      if (e.target.id === "cookBackdrop") this._closeCookModal();
    });

    sr.getElementById("cookModal")?.addEventListener("click", (e) => {
      const btn = e.target.closest("[data-cm-action]");
      if (!btn) return;
      e.stopPropagation();
      const act = btn.dataset.cmAction;
      if (act === "close") {
        this._closeCookModal();
      } else if (act === "step-target") {
        this._stepMeaterTargetTemp(Number(btn.dataset.cmDelta));
      } else if (act === "preset") {
        this._selectMeaterPreset(btn.dataset.cmPreset);
      } else if (act === "more-info") {
        const eid = btn.dataset.entity || OVEN_MEATER_CONFIG.meater.statusEntity;
        this._closeCookModal();
        this._openMoreInfo(eid);
      }
    });

    if (!this._boundCookEsc && typeof document !== "undefined") {
      this._boundCookEsc = (e) => {
        if (e.key === "Escape" && this._cookModalOpen) {
          this._closeCookModal();
        }
      };
      document.addEventListener("keydown", this._boundCookEsc);
    }
    /* OVEN_MEATER_LISTENERS_END */"""

OVEN_MEATER_SIG_JS = """    /* OVEN_MEATER_SIG_BEGIN */
    sigParts.push(...this._collectOvenMeaterSigParts());
    /* OVEN_MEATER_SIG_END */"""

OVEN_MEATER_UPDATE_JS = """      /* OVEN_MEATER_UPDATE_BEGIN */
      if (f.id === OVEN_MEATER_CONFIG.floorId) {
        const ovenEv = this._evalOvenStatus();
        const ovenEl = card?.querySelector('[data-oven-meater-badge="lg_oven"]');
        if (ovenEl) {
          ovenEl.classList.remove("oven-active", "oven-cooling", "oven-done");
          ovenEl.classList.add(ovenEv.badgeClass);
          ovenEl.classList.toggle("active", ovenEv.active && !isClimate);
          ovenEl.title = ovenEv.title;
          const ovenTxt = ovenEl.querySelector('[data-om-text="lg_oven"]');
          if (ovenTxt) ovenTxt.textContent = ovenEv.badgeText;
        }
        const meaterEv = this._evalMeaterStatus();
        const meaterEl = card?.querySelector('[data-oven-meater-badge="meater"]');
        if (meaterEl) {
          meaterEl.classList.remove("meater-standby", "meater-cooking", "meater-almost", "meater-pull", "meater-done");
          meaterEl.classList.add(meaterEv.badgeClass);
          meaterEl.classList.toggle("active", meaterEv.active && !isClimate);
          meaterEl.title = `MEATER+ · ${meaterEv.cookStatus}`;
          const meaterTxt = meaterEl.querySelector('[data-om-text="meater"]');
          if (meaterTxt) meaterTxt.textContent = meaterEv.badgeText;
        }
      }
      /* OVEN_MEATER_UPDATE_END */"""

OVEN_MEATER_MODAL_REFRESH_JS = """    /* OVEN_MEATER_MODAL_REFRESH_BEGIN */
    if (this._cookModalOpen) this._renderCookModal();
    /* OVEN_MEATER_MODAL_REFRESH_END */"""


def get_remote_paths() -> dict[str, str]:
    """Resolve Host 1 WWW paths dynamically from nas_config without hardcoded volumes or IPs."""
    www_dir = posixpath.join(nas_config.HA_CONFIG_ROOT, "www", "brock-house-3d")
    return {
        "www_dir": www_dir,
        "card_path": posixpath.join(www_dir, "brock-house-cycles-card.js"),
    }


def resolve_ha_credentials() -> tuple[str, str]:
    """Resolve Home Assistant base URL and access token from env or /secrets."""
    ha_url = (os.environ.get("HA_BASE_URL") or "").strip()
    ha_token = (os.environ.get("HA_ACCESS_TOKEN") or "").strip()

    for secret_path, url_key, tok_key in (
        (Path("/secrets/ha.json"), "url", "token"),
        (Path("/secrets/env.json"), "HA_BASE_URL", "HA_ACCESS_TOKEN"),
    ):
        if ha_url and ha_token:
            break
        if secret_path.is_file():
            try:
                data = json.loads(secret_path.read_text(encoding="utf-8"))
                if not ha_url and data.get(url_key):
                    ha_url = str(data[url_key]).strip()
                if not ha_token and data.get(tok_key):
                    ha_token = str(data[tok_key]).strip()
            except Exception as exc:  # noqa: BLE001
                print(f"[warn] Failed reading {secret_path}: {exc}", file=sys.stderr, flush=True)

    if not ha_url and nas_config.HOST_1_IP and nas_config.HOST_1_IP != "127.0.0.1":
        ha_url = f"http://{nas_config.HOST_1_IP}:8123"
    if not ha_url:
        raise RuntimeError("Could not resolve HA_BASE_URL from env, /secrets/ha.json, or /secrets/env.json")
    return ha_url.rstrip("/"), ha_token


def _replace_or_insert_block(
    src: str,
    begin_marker: str,
    end_marker: str,
    new_block: str,
    anchor_patterns: str | tuple[str, ...],
    *,
    insert_before: bool = False,
    label: str,
) -> str:
    """Idempotently replace a marked block or insert it relative to the first matching anchor regex."""
    marked_re = re.compile(
        r"[ \t]*" + re.escape(begin_marker) + r"[\s\S]*?" + re.escape(end_marker),
    )
    if marked_re.search(src):
        return marked_re.sub(lambda _m: new_block, src, count=1)

    patterns = (anchor_patterns,) if isinstance(anchor_patterns, str) else anchor_patterns
    for pat in patterns:
        m = re.search(pat, src, re.MULTILINE)
        if m:
            if insert_before:
                return src[: m.start()] + new_block + "\n" + src[m.start() :]
            return src[: m.end()] + "\n" + new_block + src[m.end() :]
    raise RuntimeError(f"Could not locate anchor for '{label}'")


def validate_js_syntax(card_js: str) -> None:
    """Validate patched JavaScript syntax using `node --check`."""
    with tempfile.NamedTemporaryFile(
        mode="w",
        suffix=".js",
        encoding="utf-8",
        delete=False,
    ) as tmp:
        tmp.write(card_js)
        tmp_path = Path(tmp.name)
    try:
        proc = subprocess.run(
            ["node", "--check", str(tmp_path)],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
        if proc.returncode != 0:
            raise RuntimeError(f"node --check failed (rc={proc.returncode}):\n{proc.stderr.strip()}")
    finally:
        tmp_path.unlink(missing_ok=True)


def verify_patched_card_contents(card_js: str) -> None:
    """Assert all LG Oven & MEATER+ markers, entities, modal elements, and handlers are present."""
    missing: list[str] = []
    for marker in REQUIRED_MARKERS:
        if marker not in card_js:
            missing.append(f"marker:{marker}")
    for eid in ALL_OVEN_MEATER_ENTITIES:
        if eid not in card_js:
            missing.append(f"entity:{eid}")
    for token in (
        "OVEN_MEATER_CONFIG",
        "oven:",
        "meater:",
        ".cook-hotspot",
        ".cook-modal",
        "cookBackdrop",
        "cookModal",
        "_evalOvenStatus",
        "_evalMeaterStatus",
        "_collectOvenMeaterSigParts",
        "_openCookModal",
        "_closeCookModal",
        "_stepMeaterTargetTemp",
        "_selectMeaterPreset",
        "_renderCookModal",
        "data-oven-meater-badge",
        "select_option",
        "set_value",
    ):
        if token not in card_js:
            missing.append(f"token:{token}")
    if missing:
        raise RuntimeError("Patched oven/MEATER+ verification failed; missing: " + ", ".join(missing))


def patch_card_js(source_js: str) -> str:
    """Idempotently patch brock-house-cycles-card.js with LG Oven & MEATER+ badges and Cook Monitor modal."""
    out = source_js

    out = _replace_or_insert_block(
        out,
        "/* OVEN_MEATER_CONFIG_BEGIN */",
        "/* OVEN_MEATER_CONFIG_END */",
        OVEN_MEATER_CONFIG_JS,
        r"^\s*const\s+(?:LENSES|FLOORS)\s*=\s*\[",
        insert_before=True,
        label="OVEN_MEATER_CONFIG",
    )

    out = _replace_or_insert_block(
        out,
        "/* OVEN_MEATER_ICONS_BEGIN */",
        "/* OVEN_MEATER_ICONS_END */",
        OVEN_MEATER_ICONS_JS,
        (
            r"/\*\s*GAMING_BADGES_ICONS_END\s*\*/",
            r"const\s+SVG_ICONS\s*=\s*\{",
        ),
        insert_before=False,
        label="OVEN_MEATER_ICONS",
    )

    out = _replace_or_insert_block(
        out,
        "/* OVEN_MEATER_CSS_BEGIN */",
        "/* OVEN_MEATER_CSS_END */",
        OVEN_MEATER_CSS,
        (
            r"/\*\s*TV_COVER_ART_CSS_END\s*\*/",
            r"/\*\s*GAMING_BADGES_CSS_END\s*\*/",
            r"^\s*\.run-badge\.danger\s+\.run-dot\s*\{\s*background:\s*#ff5252;\s*\}",
        ),
        insert_before=False,
        label="OVEN_MEATER_CSS",
    )

    out = _replace_or_insert_block(
        out,
        "/* OVEN_MEATER_HELPER_BEGIN */",
        "/* OVEN_MEATER_HELPER_END */",
        OVEN_MEATER_HELPER_JS,
        r"^\s*_evalRunningItem\(item\)\s*\{",
        insert_before=True,
        label="OVEN_MEATER_HELPER",
    )

    # Note: HTML block sits inside running-layer; wrap inside ${...} in template literal.
    # For clean JS template evaluation, OVEN_MEATER_HTML_BEGIN/END wraps a ${ ... } expression.
    wrapped_html = "${" + OVEN_MEATER_HTML.strip().removeprefix("/* OVEN_MEATER_HTML_BEGIN */").removesuffix("/* OVEN_MEATER_HTML_END */").strip() + "}"
    # Better: keep the markers outside the template expression or inside the JS comment inside ${...}
    html_block = (
        "                ${/* OVEN_MEATER_HTML_BEGIN */\n"
        '                  f.id === OVEN_MEATER_CONFIG.floorId\n'
        "                    ? `\n"
        '                  <button class="cook-hotspot" data-cook-open="1" style="left:${OVEN_MEATER_CONFIG.hotspot.x}%;top:${OVEN_MEATER_CONFIG.hotspot.y}%;" title="Kitchen Oven & MEATER+ Cook Monitor" aria-label="Open Kitchen Cook Monitor"></button>\n'
        '                  <button class="run-badge oven-badge" data-oven-meater-badge="lg_oven" data-cook-open="1" style="left:${OVEN_MEATER_CONFIG.ovenBadgePos.x}%;top:${OVEN_MEATER_CONFIG.ovenBadgePos.y}%;" title="LG Oven">\n'
        '                    <span class="run-dot"></span>\n'
        '                    <span class="run-icon">${SVG_ICONS.oven || ""}</span>\n'
        '                    <span data-om-text="lg_oven">Oven</span>\n'
        "                  </button>\n"
        '                  <button class="run-badge meater-badge" data-oven-meater-badge="meater" data-cook-open="1" style="left:${OVEN_MEATER_CONFIG.meaterBadgePos.x}%;top:${OVEN_MEATER_CONFIG.meaterBadgePos.y}%;" title="MEATER+ Probe">\n'
        '                    <span class="run-dot"></span>\n'
        '                    <span class="run-icon">${SVG_ICONS.meater || ""}</span>\n'
        '                    <span data-om-text="meater">MEATER+</span>\n'
        "                  </button>\n"
        "                `\n"
        '                    : ""\n'
        "                /* OVEN_MEATER_HTML_END */}"
    )
    _ = wrapped_html  # unused; html_block is cleaner
    marked_html_re = re.compile(
        r"[ \t]*\$\{/\* OVEN_MEATER_HTML_BEGIN \*/[\s\S]*?/\* OVEN_MEATER_HTML_END \*/\}",
    )
    if marked_html_re.search(out):
        out = marked_html_re.sub(lambda _m: html_block, out, count=1)
    else:
        out = _replace_or_insert_block(
            out,
            "/* OVEN_MEATER_HTML_BEGIN */",
            "/* OVEN_MEATER_HTML_END */",
            html_block,
            (
                r"<!--\s*GAMING_BADGES_HTML_END\s*-->",
                r"/\*\s*TV_COVER_ART_HTML_END\s*\*/[\s\S]*?\.join\(\"\"\)\}",
                r'<div\s+class="running-layer"\s+data-running-layer="\$\{f\.id\}">[\s\S]*?\.join\(""\)\}',
            ),
            insert_before=False,
            label="OVEN_MEATER_HTML",
        )

    modal_html_block = (
        '        <!-- OVEN_MEATER_MODAL_HTML_BEGIN -->\n'
        '        <!-- /* OVEN_MEATER_MODAL_HTML_BEGIN */ -->\n'
        '        <div class="cook-backdrop" id="cookBackdrop" aria-hidden="true">\n'
        '          <div class="cook-modal" id="cookModal"></div>\n'
        "        </div>\n"
        '        <!-- /* OVEN_MEATER_MODAL_HTML_END */ -->'
    )
    # Clean single marker form inside template literal:
    modal_html_block = (
        '        ${/* OVEN_MEATER_MODAL_HTML_BEGIN */ ""}\n'
        '        <div class="cook-backdrop" id="cookBackdrop" aria-hidden="true">\n'
        '          <div class="cook-modal" id="cookModal"></div>\n'
        "        </div>\n"
        '        ${/* OVEN_MEATER_MODAL_HTML_END */ ""}'
    )
    marked_modal_re = re.compile(
        r'[ \t]*\$\{/\* OVEN_MEATER_MODAL_HTML_BEGIN \*/ ""\}[\s\S]*?\$\{/\* OVEN_MEATER_MODAL_HTML_END \*/ ""\}',
    )
    if marked_modal_re.search(out):
        out = marked_modal_re.sub(lambda _m: modal_html_block, out, count=1)
    else:
        out = _replace_or_insert_block(
            out,
            "/* OVEN_MEATER_MODAL_HTML_BEGIN */",
            "/* OVEN_MEATER_MODAL_HTML_END */",
            modal_html_block,
            (
                r'<div\s+class="presence-backdrop"[\s\S]*?<div\s+class="presence-modal"[^>]*></div>\s*</div>',
                r'<div\s+class="thermo-backdrop"[\s\S]*?<div\s+class="thermo-modal"[^>]*></div>\s*</div>',
                r'<div\s+class="running-layer"[\s\S]*?</div>\s*</div>',
            ),
            insert_before=False,
            label="OVEN_MEATER_MODAL_HTML",
        )

    out = _replace_or_insert_block(
        out,
        "/* OVEN_MEATER_LISTENERS_BEGIN */",
        "/* OVEN_MEATER_LISTENERS_END */",
        OVEN_MEATER_LISTENERS_JS,
        (
            r'sr\.getElementById\("presenceModal"\)\?\.addEventListener\("click"[\s\S]*?\}\);',
            r'sr\.getElementById\("thermoModal"\)\?\.addEventListener\("click"[\s\S]*?\}\);',
            r'sr\.querySelectorAll\("\.run-badge"\)\.forEach\([\s\S]*?\}\);',
        ),
        insert_before=False,
        label="OVEN_MEATER_LISTENERS",
    )

    out = _replace_or_insert_block(
        out,
        "/* OVEN_MEATER_SIG_BEGIN */",
        "/* OVEN_MEATER_SIG_END */",
        OVEN_MEATER_SIG_JS,
        r'^\s*const\s+sig\s*=\s*sigParts\.join\("\|"\);',
        insert_before=True,
        label="OVEN_MEATER_SIG",
    )

    out = _replace_or_insert_block(
        out,
        "/* OVEN_MEATER_UPDATE_BEGIN */",
        "/* OVEN_MEATER_UPDATE_END */",
        OVEN_MEATER_UPDATE_JS,
        (
            r"/\*\s*TV_COVER_ART_UPDATE_END\s*\*/",
            r"/\*\s*GAMING_BADGES_UPDATE_END\s*\*/",
            (
                r"for\s*\(const\s+ri\s+of\s+\(f\.runningItems\s*\|\|\s*\[\]\)\)\s*\{[\s\S]*?"
                r"if\s*\(txtEl\)\s*txtEl\.textContent\s*=\s*ev\.text;\s*\n\s*\}\s*\n\s*\}"
            ),
        ),
        insert_before=False,
        label="OVEN_MEATER_UPDATE",
    )

    out = _replace_or_insert_block(
        out,
        "/* OVEN_MEATER_MODAL_REFRESH_BEGIN */",
        "/* OVEN_MEATER_MODAL_REFRESH_END */",
        OVEN_MEATER_MODAL_REFRESH_JS,
        (
            r"if\s*\(this\._presenceModalOpen\)\s*this\._renderPresenceModal\(\);",
            r"if\s*\(this\._thermoModalOpen\)\s*this\._renderThermostatModal\(\);",
            r"return\s+true;\s*\n\s*\}",
        ),
        insert_before=False,
        label="OVEN_MEATER_MODAL_REFRESH",
    )

    verify_patched_card_contents(out)
    validate_js_syntax(out)
    return out


def fetch_live_card_and_backup(backup_path: Path = BACKUP_CARD_PATH) -> str:
    """Fetch live brock-house-cycles-card.js from Host 1 via SSH and back it up locally."""
    paths = get_remote_paths()
    card_path = paths["card_path"]
    print(f"[1/4] Fetching live card from Host 1: {card_path}", flush=True)
    rc, out, err = nas_config.run_ssh(
        nas_config.HOST_1_IP,
        f"base64 -w 0 {shlex.quote(card_path)}",
        timeout=30,
    )
    if rc != 0:
        raise RuntimeError(f"Failed fetching {card_path} from Host 1 (rc={rc}): {err}")

    raw_bytes = base64.b64decode(out.strip())
    card_text = raw_bytes.decode("utf-8")
    backup_path.parent.mkdir(parents=True, exist_ok=True)
    already_patched = "OVEN_MEATER_HELPER_BEGIN" in card_text
    if not already_patched or not backup_path.is_file():
        backup_path.write_bytes(raw_bytes)
        print(f"[1/4] Backed up live card ({len(raw_bytes):,} bytes) -> {backup_path}", flush=True)
    else:
        print(
            f"[1/4] Fetched live card ({len(raw_bytes):,} bytes); preserved pre-patch backup at {backup_path}",
            flush=True,
        )
    return card_text


def deploy_card_to_host1(patched_js: str) -> None:
    """Upload updated brock-house-cycles-card.js to Host 1 Volume 2 NVMe www dir."""
    paths = get_remote_paths()
    buf = io.BytesIO()
    now_ts = int(time.time())
    with tarfile.open(fileobj=buf, mode="w") as tf:
        js_bytes = patched_js.encode("utf-8")
        js_info = tarfile.TarInfo(name="brock-house-cycles-card.js")
        js_info.size = len(js_bytes)
        js_info.mode = 0o644
        js_info.mtime = now_ts
        tf.addfile(js_info, io.BytesIO(js_bytes))

    b64_payload = base64.b64encode(buf.getvalue()).decode("ascii")
    remote_cmd = (
        f"mkdir -p {shlex.quote(paths['www_dir'])} && "
        f"base64 -d | tar -xf - -C {shlex.quote(paths['www_dir'])}"
    )
    rc, _out, err = nas_config.run_ssh(
        nas_config.HOST_1_IP,
        remote_cmd,
        timeout=30,
        stdin_data=b64_payload,
    )
    if rc != 0:
        raise RuntimeError(f"Failed deploying card JS to Host 1 (rc={rc}): {err}")
    print(
        f"[3/4] Deployed brock-house-cycles-card.js ({len(patched_js.encode('utf-8')):,} bytes) -> {paths['card_path']}",
        flush=True,
    )


def _with_cache_buster(resource_url: str, cache_buster: str) -> str:
    """Replace or append ?v=<cache_buster> on a Lovelace resource URL."""
    parsed = urllib.parse.urlsplit(resource_url)
    query_pairs = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
    new_pairs = [(k, v) for (k, v) in query_pairs if k != "v"]
    new_pairs.append(("v", cache_buster))
    new_query = urllib.parse.urlencode(new_pairs)
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path, new_query, parsed.fragment))


async def _ws_lovelace_resources(ha_url: str, ha_token: str, cache_buster: str | None = None) -> list[dict[str, Any]]:
    """Query Lovelace resources via HA WebSocket and optionally update brock-house-cycles-card.js cache-buster."""
    import aiohttp

    ws_url = ha_url.replace("http://", "ws://").replace("https://", "wss://") + "/api/websocket"
    client_timeout = aiohttp.ClientTimeout(total=20)
    async with (
        aiohttp.ClientSession(timeout=client_timeout) as session,
        session.ws_connect(ws_url, timeout=15) as ws,
    ):
        hello = await asyncio.wait_for(ws.receive_json(), timeout=10)
        if hello.get("type") != "auth_required":
            raise RuntimeError(f"Unexpected HA WS handshake: {hello}")
        await ws.send_json({"type": "auth", "access_token": ha_token})
        auth_resp = await asyncio.wait_for(ws.receive_json(), timeout=10)
        if auth_resp.get("type") != "auth_ok":
            raise RuntimeError(f"HA WS auth failed: {auth_resp}")

        await ws.send_json({"id": 1, "type": "lovelace/resources"})
        res = await asyncio.wait_for(ws.receive_json(), timeout=10)
        if not res.get("success"):
            raise RuntimeError(f"HA WS lovelace/resources failed: {res}")
        resources: list[dict[str, Any]] = res.get("result") or []

        if cache_buster is not None:
            target = next(
                (r for r in resources if "/local/brock-house-3d/brock-house-cycles-card.js" in (r.get("url") or "")),
                None,
            )
            if not target:
                raise RuntimeError("Could not find brock-house-cycles-card.js in HA lovelace/resources")
            new_url = _with_cache_buster(target["url"], cache_buster)
            await ws.send_json(
                {
                    "id": 2,
                    "type": "lovelace/resources/update",
                    "resource_id": target["id"],
                    "url": new_url,
                    "res_type": target.get("type", "module"),
                }
            )
            upd = await asyncio.wait_for(ws.receive_json(), timeout=10)
            if not upd.get("success"):
                raise RuntimeError(f"HA WS lovelace/resources/update failed: {upd}")
            return [upd["result"]]

        return resources


def update_lovelace_resource_cache_buster(ha_url: str, ha_token: str, cache_buster: str) -> str:
    """Update HA Lovelace resource URL for brock-house-cycles-card.js with ?v=<cache_buster>."""
    if not ha_token:
        raise RuntimeError("HA access token required to update Lovelace resource via WebSocket")
    updated = asyncio.run(asyncio.wait_for(_ws_lovelace_resources(ha_url, ha_token, cache_buster=cache_buster), timeout=25))
    new_url = updated[0]["url"]
    print(f"[4/4] Updated HA Lovelace resource via WebSocket -> {new_url}", flush=True)
    return new_url


def _http_get(url: str, timeout: int = 15) -> tuple[int, bytes]:
    req = urllib.request.Request(url, method="GET", headers={"Cache-Control": "no-cache"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return int(resp.status), resp.read()
    except urllib.error.HTTPError as exc:
        return int(exc.code), b""


def verify_live_http(ha_url: str, ha_token: str = "") -> tuple[bool, list[str]]:
    """Verify HTTP 200 and LG Oven / MEATER+ popup contents on the live JS card on Home Assistant."""
    errors: list[str] = []
    card_rel = "/local/brock-house-3d/brock-house-cycles-card.js"

    if ha_token:
        try:
            resources = asyncio.run(asyncio.wait_for(_ws_lovelace_resources(ha_url, ha_token, cache_buster=None), timeout=25))
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
        f"(sha256={js_hash}, {len(patched_js.encode('utf-8')):,} bytes, LG Oven & MEATER+ Cook Monitor)",
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

    print("SUCCESS: Deployed and verified 3D floorplan LG Oven & MEATER+ Cook Monitor on live card (HTTP 200).", flush=True)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Wire and deploy 3D floorplan LG Oven & MEATER+ badges and Cook Monitor popup to Home Assistant."
    )
    parser.add_argument(
        "--verify-only",
        action="store_true",
        help="Verify HTTP 200 and LG Oven / MEATER+ popup contents on the live card on Home Assistant.",
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
        print("VERIFIED: Live card returned HTTP 200 with LG Oven & MEATER+ Cook Monitor wired.", flush=True)
        return 0

    return run_deploy(dry_run=args.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())
