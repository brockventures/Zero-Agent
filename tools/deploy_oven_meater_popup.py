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
    proteinCategories: [
      {
        id: "poultry",
        label: "Poultry",
        icon: "poultry",
        cuts: [
          { label: "Turkey Breast (165°F)", cut: "Turkey Breast", doneness: "USDA Safe", targetF: 165, restMin: 20, presetOption: "Turkey Breast (165°F)" },
          { label: "Turkey Thigh (175°F)", cut: "Turkey Thigh", doneness: "Dark Meat", targetF: 175, restMin: 20, presetOption: "Turkey Thigh (175°F)" },
          { label: "Whole Chicken (165°F)", cut: "Whole Chicken", doneness: "USDA Safe", targetF: 165, restMin: 15, presetOption: "Chicken (165°F)" },
          { label: "Chicken Breast (165°F)", cut: "Chicken Breast", doneness: "White Meat", targetF: 165, restMin: 5, presetOption: "Chicken (165°F)" },
          { label: "Duck Breast Med-Rare (135°F)", cut: "Duck Breast", doneness: "Med-Rare", targetF: 135, restMin: 10, presetOption: "Duck Breast Med-Rare (135°F)" },
        ],
      },
      {
        id: "beef",
        label: "Beef",
        icon: "beef",
        cuts: [
          { label: "Prime Rib Med-Rare (130°F)", cut: "Prime Rib", doneness: "Med-Rare", targetF: 130, restMin: 25, presetOption: "Prime Rib Med-Rare (130°F)" },
          { label: "Steak Rare (125°F)", cut: "Steak Rare", doneness: "Rare", targetF: 125, restMin: 8, presetOption: "Steak Rare (125°F)" },
          { label: "Steak Med-Rare (135°F)", cut: "Steak Med-Rare", doneness: "Med-Rare", targetF: 135, restMin: 8, presetOption: "Steak Med-Rare (135°F)" },
          { label: "Steak Medium (145°F)", cut: "Steak Medium", doneness: "Medium", targetF: 145, restMin: 8, presetOption: "Steak Medium (145°F)" },
          { label: "Brisket (203°F)", cut: "Brisket", doneness: "BBQ Tender", targetF: 203, restMin: 30, presetOption: "Brisket (203°F)" },
        ],
      },
      {
        id: "pork",
        label: "Pork",
        icon: "pork",
        cuts: [
          { label: "Pork Chop (145°F)", cut: "Pork Chop", doneness: "Medium", targetF: 145, restMin: 5, presetOption: "Pork Chop (145°F)" },
          { label: "Pork Tenderloin (145°F)", cut: "Pork Tenderloin", doneness: "Medium", targetF: 145, restMin: 5, presetOption: "Pork Tenderloin (145°F)" },
          { label: "Pork Shoulder / Butt (203°F)", cut: "Pork Shoulder / Butt", doneness: "Pulled BBQ", targetF: 203, restMin: 30, presetOption: "Pork Shoulder (203°F)" },
          { label: "Baby Back Ribs (198°F)", cut: "Baby Back Ribs", doneness: "BBQ Tender", targetF: 198, restMin: 15, presetOption: "Baby Back Ribs (198°F)" },
        ],
      },
      {
        id: "lamb_fish",
        label: "Lamb & Fish",
        icon: "lamb_fish",
        cuts: [
          { label: "Rack of Lamb Med-Rare (135°F)", cut: "Rack of Lamb", doneness: "Med-Rare", targetF: 135, restMin: 10, presetOption: "Rack of Lamb Med-Rare (135°F)" },
          { label: "Salmon (125°F)", cut: "Salmon", doneness: "Medium", targetF: 125, restMin: 3, presetOption: "Salmon (125°F)" },
          { label: "White Fish (140°F)", cut: "White Fish", doneness: "Flaky", targetF: 140, restMin: 3, presetOption: "White Fish (140°F)" },
        ],
      },
      {
        id: "custom",
        label: "Custom",
        icon: "custom",
        note: "Use the -5° / -1° / +1° / +5° stepper above to dial in any custom target temperature (100°F–225°F).",
        cuts: [],
      },
    ],
  },
};
/* OVEN_MEATER_CONFIG_END */"""

OVEN_MEATER_ICONS_JS = """  /* OVEN_MEATER_ICONS_BEGIN */
  oven: `<svg viewBox="0 0 24 24"><path fill="currentColor" d="M18,2H6A2,2 0 0,0 4,4V20A2,2 0 0,0 6,22H18A2,2 0 0,0 20,20V4A2,2 0 0,0 18,2M7.5,4.5A1,1 0 0,1 8.5,5.5A1,1 0 0,1 7.5,6.5A1,1 0 0,1 6.5,5.5A1,1 0 0,1 7.5,4.5M12,4.5A1,1 0 0,1 13,5.5A1,1 0 0,1 12,6.5A1,1 0 0,1 11,5.5A1,1 0 0,1 12,4.5M16.5,4.5A1,1 0 0,1 17.5,5.5A1,1 0 0,1 16.5,6.5A1,1 0 0,1 15.5,5.5A1,1 0 0,1 16.5,4.5M18,19H6V9H18V19M12,11C10.5,12.5 9.8,13.9 9.8,15.1A2.2,2.2 0 0,0 12,17.3A2.2,2.2 0 0,0 14.2,15.1C14.2,13.9 13.5,12.5 12,11Z"/></svg>`,
  meater: `<svg viewBox="0 0 24 24"><path fill="currentColor" d="M20.2,3.8A2.8,2.8 0 0,0 16.2,3.8L13.4,6.6L17.4,10.6L20.2,7.8A2.8,2.8 0 0,0 20.2,3.8M12.3,7.7L4.4,15.6L2.5,20.5L3.5,21.5L8.4,19.6L16.3,11.7L12.3,7.7M8.5,2.5A7.5,7.5 0 0,0 3.2,4.7L4.6,6.1A5.5,5.5 0 0,1 8.5,4.5V2.5M8.5,6.5A3.5,3.5 0 0,0 6,7.5L7.4,8.9A1.5,1.5 0 0,1 8.5,8.5V6.5Z"/></svg>`,
  poultry: `<svg viewBox="0 0 24 24"><path fill="currentColor" d="M18.06,5.94C15.9,3.78 12.55,3.5 10.07,5.28C7.94,6.8 7.12,9.45 7.77,11.88L4.9,14.75C4.12,14.55 3.26,14.76 2.66,15.36C1.78,16.24 1.78,17.66 2.66,18.54C3.09,18.97 3.67,19.18 4.25,19.18C4.82,19.18 5.4,18.97 5.84,18.54C5.41,18.97 5.2,19.55 5.2,20.13C5.2,20.7 5.41,21.28 5.84,21.72C6.72,22.6 8.14,22.6 9.02,21.72C9.62,21.12 9.83,20.26 9.63,19.48L12.5,16.61C14.93,17.26 17.58,16.44 19.1,14.31C20.88,11.83 20.6,8.48 18.06,5.94Z"/></svg>`,
  beef: `<svg viewBox="0 0 24 24"><path fill="currentColor" d="M18.5,4C15.8,2.5 11.4,3.1 8.4,5.4C5.1,7.9 3,11.7 3,15.2C3,18.4 5.6,21 9.2,21C12.3,21 14.7,19.3 16.9,17.4C19.3,15.3 21,12.8 21,9.5C21,7.1 20.1,4.9 18.5,4M15.5,11.8C14.1,13 12.1,13.2 10.9,12C9.7,10.8 9.9,8.8 11.3,7.6C12.7,6.4 14.7,6.2 15.9,7.4C17.1,8.6 16.9,10.6 15.5,11.8M7.8,17.2C6.7,17.2 5.8,16.3 5.8,15.2C5.8,14.1 6.7,13.2 7.8,13.2C8.9,13.2 9.8,14.1 9.8,15.2C9.8,16.3 8.9,17.2 7.8,17.2Z"/></svg>`,
  pork: `<svg viewBox="0 0 24 24"><path fill="currentColor" d="M19.8,10.5C19.5,8.2 17.9,6.4 15.7,5.7L17,3.5L14.3,4.3C13.4,4.1 12.4,4 11.5,4C7.5,4 4.2,6.6 3.5,10.1C2.6,10.3 2,11.1 2,12C2,13 2.7,13.8 3.6,14C4.1,15.3 5,16.4 6.2,17.2L5.5,20H8.2L8.7,18.3C9.6,18.6 10.5,18.7 11.5,18.7C12.5,18.7 13.4,18.5 14.3,18.3L14.8,20H17.5L16.8,17.1C18,16.2 19,15 19.5,13.5H21.5V10.5H19.8M15.5,10.5A1.2,1.2 0 0,1 14.3,9.3A1.2,1.2 0 0,1 15.5,8.1A1.2,1.2 0 0,1 16.7,9.3A1.2,1.2 0 0,1 15.5,10.5Z"/></svg>`,
  lamb_fish: `<svg viewBox="0 0 24 24"><path fill="currentColor" d="M12,6C16.4,6 20.1,8.4 22,12C20.1,15.6 16.4,18 12,18C9.3,18 6.9,17 5,15.3L2,18V6L5,8.7C6.9,7 9.3,6 12,6M16.5,10.2A1.8,1.8 0 0,0 14.7,12A1.8,1.8 0 0,0 16.5,13.8A1.8,1.8 0 0,0 18.3,12A1.8,1.8 0 0,0 16.5,10.2Z"/></svg>`,
  custom: `<svg viewBox="0 0 24 24"><path fill="currentColor" d="M15,13V5A3,3 0 0,0 9,5V13A5,5 0 1,0 15,13M12,4A1,1 0 0,1 13,5V8H11V5A1,1 0 0,1 12,4Z"/></svg>`,
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
          background: rgba(4, 8, 15, 0.44);
          display: none;
          align-items: center;
          justify-content: center;
          padding: 16px;
        }
        .cook-backdrop.open {
          display: flex;
        }
        .cook-tether-svg {
          position: absolute;
          inset: 0;
          width: 100%;
          height: 100%;
          pointer-events: none;
          z-index: 1;
          overflow: visible;
        }
        .cook-tether-halo {
          transform-box: fill-box;
          transform-origin: center;
          animation: cookHaloPulse 2.2s ease-in-out infinite;
        }
        @keyframes cookHaloPulse {
          0%, 100% { transform: scale(0.92); opacity: 0.72; }
          50% { transform: scale(1.18); opacity: 1; }
        }
        .cook-modal {
          position: relative;
          z-index: 2;
          width: min(92vw, 780px);
          max-height: 92%;
          overflow-y: auto;
          background: rgba(12, 20, 34, 0.96);
          backdrop-filter: blur(12px);
          -webkit-backdrop-filter: blur(12px);
          border: 1.5px solid rgba(255, 152, 78, 0.42);
          border-radius: 24px;
          box-shadow: 0 28px 72px rgba(0, 0, 0, 0.78), 0 0 36px rgba(255, 138, 60, 0.14);
          padding: 22px 26px 24px;
          color: #eef5fc;
          display: flex;
          flex-direction: column;
          gap: 16px;
        }
        .cm-head {
          display: flex;
          align-items: center;
          justify-content: space-between;
          gap: 14px;
        }
        .cm-title-wrap {
          display: flex;
          align-items: center;
          gap: 12px;
          min-width: 0;
        }
        .cm-head-icon {
          width: 42px;
          height: 42px;
          border-radius: 12px;
          background: rgba(255, 146, 72, 0.18);
          border: 1px solid rgba(255, 146, 72, 0.48);
          color: #ffb578;
          display: inline-flex;
          align-items: center;
          justify-content: center;
          flex-shrink: 0;
        }
        .cm-head-icon svg { width: 24px; height: 24px; }
        .cm-title { font-size: 22px; font-weight: 800; color: #f8fafc; letter-spacing: -0.01em; }
        .cm-sub { font-size: 14px; color: #a3bad4; margin-top: 2px; }
        .cook-modal .rd-close {
          width: 38px;
          height: 38px;
          font-size: 22px;
          border-radius: 11px;
        }
        .cm-section-card {
          background: rgba(255, 255, 255, 0.045);
          border: 1px solid rgba(255, 255, 255, 0.1);
          border-radius: 18px;
          padding: 16px 18px;
          display: flex;
          flex-direction: column;
          gap: 14px;
        }
        .cm-sec-head {
          display: flex;
          align-items: center;
          justify-content: space-between;
          gap: 10px;
        }
        .cm-sec-label {
          display: inline-flex;
          align-items: center;
          gap: 8px;
          font-size: 13.5px;
          font-weight: 700;
          letter-spacing: 0.04em;
          text-transform: uppercase;
          color: #b4cae3;
        }
        .cm-sec-label svg { width: 17px; height: 17px; color: #ffb272; }
        .cm-status-pill {
          font-size: 13px;
          font-weight: 700;
          padding: 5px 12px;
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
          gap: 20px;
        }
        .cm-gauge-wrap {
          position: relative;
          width: 172px;
          height: 156px;
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
          padding-top: 8px;
          pointer-events: none;
        }
        .cm-gauge-temp {
          font-size: 39px;
          font-weight: 800;
          letter-spacing: -0.03em;
          color: #ffffff;
          line-height: 1.05;
        }
        .cm-gauge-target {
          font-size: 14.5px;
          font-weight: 700;
          color: #a7c1df;
          margin-top: 4px;
        }
        .cm-stats-grid {
          flex: 1 1 0;
          display: grid;
          grid-template-columns: repeat(2, 1fr);
          gap: 10px;
        }
        .cm-stat-box {
          background: rgba(255, 255, 255, 0.045);
          border: 1px solid rgba(255, 255, 255, 0.08);
          border-radius: 13px;
          padding: 10px 13px;
        }
        .cm-stat-k {
          font-size: 12px;
          font-weight: 700;
          text-transform: uppercase;
          letter-spacing: 0.04em;
          color: #8da8c7;
        }
        .cm-stat-v {
          font-size: 18.5px;
          font-weight: 800;
          color: #f1f5f9;
          margin-top: 4px;
          white-space: nowrap;
        }
        .cm-stat-box.cm-stat-eta {
          grid-column: 1 / -1;
          background: linear-gradient(135deg, rgba(56, 189, 248, 0.12), rgba(255, 146, 72, 0.08));
          border-color: rgba(56, 189, 248, 0.28);
          padding: 12px 15px;
        }
        .cm-stat-eta-head {
          display: flex;
          align-items: center;
          justify-content: space-between;
          gap: 8px;
          flex-wrap: wrap;
        }
        .cm-stat-pull-badge {
          font-size: 11.5px;
          font-weight: 700;
          padding: 2px 9px;
          border-radius: 999px;
          background: rgba(253, 224, 71, 0.14);
          border: 1px solid rgba(253, 224, 71, 0.42);
          color: #fde68a;
          white-space: nowrap;
        }
        .cm-stat-eta-main {
          display: flex;
          align-items: baseline;
          justify-content: space-between;
          gap: 10px;
          margin-top: 5px;
          flex-wrap: wrap;
        }
        .cm-stat-eta-val {
          font-size: 27px;
          font-weight: 800;
          letter-spacing: -0.02em;
          line-height: 1.08;
          color: #f8fafc;
          white-space: nowrap;
        }
        .cm-stat-rate-sub {
          font-size: 11.5px;
          font-weight: 600;
          color: #8da8c7;
          white-space: nowrap;
        }
        .cm-chart-card {
          background: rgba(7, 13, 24, 0.58);
          border: 1px solid rgba(255, 255, 255, 0.09);
          border-radius: 14px;
          padding: 12px 14px 10px;
          display: flex;
          flex-direction: column;
          gap: 8px;
        }
        .cm-chart-head {
          display: flex;
          align-items: center;
          justify-content: space-between;
          gap: 10px;
          flex-wrap: wrap;
        }
        .cm-chart-title {
          font-size: 13px;
          font-weight: 700;
          text-transform: uppercase;
          letter-spacing: 0.04em;
          color: #b4cae3;
        }
        .cm-chart-badges {
          display: flex;
          align-items: center;
          gap: 8px;
          flex-wrap: wrap;
        }
        .cm-chart-pill {
          font-size: 12px;
          font-weight: 700;
          padding: 3px 10px;
          border-radius: 999px;
          background: rgba(255, 146, 72, 0.16);
          border: 1px solid rgba(255, 146, 72, 0.45);
          color: #ffd9b8;
          white-space: nowrap;
        }
        .cm-chart-pill.eta {
          background: rgba(56, 189, 248, 0.16);
          border-color: rgba(56, 189, 248, 0.48);
          color: #bae6fd;
        }
        .cm-chart-svg-wrap {
          width: 100%;
          height: 210px;
          position: relative;
        }
        .cm-chart-svg {
          width: 100%;
          height: 210px;
          display: block;
        }
        .cm-stepper-card {
          display: flex;
          align-items: center;
          justify-content: space-between;
          gap: 12px;
          background: rgba(255, 255, 255, 0.045);
          border: 1px solid rgba(255, 255, 255, 0.09);
          border-radius: 14px;
          padding: 11px 14px;
        }
        .cm-step-group {
          display: flex;
          align-items: center;
          gap: 8px;
        }
        .cm-step-btn {
          min-width: 52px;
          height: 42px;
          padding: 0 11px;
          border-radius: 11px;
          border: 1px solid rgba(255, 255, 255, 0.22);
          background: rgba(255, 255, 255, 0.09);
          color: #ffffff;
          font-size: 15px;
          font-weight: 800;
          cursor: pointer;
        }
        .cm-step-btn:hover {
          background: rgba(255, 146, 72, 0.28);
          border-color: rgba(255, 146, 72, 0.72);
        }
        .cm-step-readout {
          text-align: center;
          min-width: 96px;
        }
        .cm-step-readout-val {
          font-size: 24px;
          font-weight: 800;
          color: #ffffff;
        }
        .cm-step-readout-lbl {
          font-size: 11.5px;
          font-weight: 700;
          color: #96b0ce;
          text-transform: uppercase;
          letter-spacing: 0.04em;
        }
        .cm-presets {
          display: flex;
          flex-wrap: wrap;
          gap: 7px;
        }
        .cm-preset-btn {
          border: 1px solid rgba(255, 255, 255, 0.16);
          background: rgba(255, 255, 255, 0.06);
          color: #d0e2f4;
          font-size: 13px;
          font-weight: 700;
          padding: 7px 13px;
          border-radius: 999px;
          cursor: pointer;
          transition: background 0.14s ease, border-color 0.14s ease;
        }
        .cm-preset-btn:hover {
          background: rgba(255, 255, 255, 0.14);
        }
        .cm-preset-btn.active {
          border-color: rgba(255, 146, 72, 0.88);
          background: rgba(255, 146, 72, 0.26);
          color: #ffffff;
        }
        .cm-protein-picker {
          display: flex;
          flex-direction: column;
          gap: 10px;
        }
        .cm-protein-tabs {
          display: grid;
          grid-template-columns: repeat(5, minmax(0, 1fr));
          gap: 8px;
        }
        .cm-protein-tab {
          display: flex;
          flex-direction: column;
          align-items: center;
          justify-content: center;
          gap: 5px;
          padding: 9px 6px;
          border-radius: 13px;
          border: 1px solid rgba(255, 255, 255, 0.14);
          background: rgba(255, 255, 255, 0.05);
          color: #c8dced;
          font-size: 12.5px;
          font-weight: 700;
          cursor: pointer;
          transition: background 0.15s ease, border-color 0.15s ease, color 0.15s ease, box-shadow 0.15s ease;
        }
        .cm-protein-tab:hover {
          background: rgba(255, 255, 255, 0.11);
          border-color: rgba(255, 184, 118, 0.45);
          color: #ffffff;
        }
        .cm-protein-tab.active {
          border-color: rgba(255, 146, 72, 0.92);
          background: rgba(255, 146, 72, 0.24);
          color: #fff3e6;
          box-shadow: 0 0 14px rgba(255, 146, 72, 0.24);
        }
        .cm-protein-icon {
          width: 22px;
          height: 22px;
          display: inline-flex;
          align-items: center;
          justify-content: center;
          color: #ffb578;
        }
        .cm-protein-tab.active .cm-protein-icon {
          color: #ffd8b3;
        }
        .cm-protein-icon svg {
          width: 20px;
          height: 20px;
        }
        .cm-protein-lbl {
          white-space: nowrap;
          overflow: hidden;
          text-overflow: ellipsis;
          max-width: 100%;
        }
        .cm-cut-grid {
          display: grid;
          grid-template-columns: repeat(auto-fill, minmax(195px, 1fr));
          gap: 8px;
        }
        .cm-cut-card {
          display: flex;
          flex-direction: column;
          gap: 6px;
          text-align: left;
          padding: 10px 12px;
          border-radius: 12px;
          border: 1px solid rgba(255, 255, 255, 0.12);
          background: rgba(255, 255, 255, 0.045);
          color: #e2ecf7;
          cursor: pointer;
          transition: background 0.14s ease, border-color 0.14s ease, box-shadow 0.14s ease;
        }
        .cm-cut-card:hover {
          background: rgba(255, 255, 255, 0.1);
          border-color: rgba(255, 184, 118, 0.42);
        }
        .cm-cut-card.active {
          border-color: rgba(255, 146, 72, 0.9);
          background: rgba(255, 146, 72, 0.22);
          color: #ffffff;
          box-shadow: 0 0 12px rgba(255, 146, 72, 0.2);
        }
        .cm-cut-top {
          display: flex;
          align-items: center;
          justify-content: space-between;
          gap: 6px;
        }
        .cm-cut-name {
          font-size: 13.5px;
          font-weight: 800;
          color: #f8fafc;
        }
        .cm-cut-doneness {
          font-size: 11px;
          font-weight: 700;
          padding: 2px 7px;
          border-radius: 999px;
          background: rgba(255, 146, 72, 0.18);
          border: 1px solid rgba(255, 146, 72, 0.42);
          color: #ffd9b8;
          white-space: nowrap;
        }
        .cm-cut-meta {
          display: flex;
          align-items: center;
          justify-content: space-between;
          gap: 8px;
          font-size: 12.5px;
          font-weight: 700;
        }
        .cm-cut-temp {
          color: #ffb578;
          font-size: 14px;
          font-weight: 800;
        }
        .cm-cut-rest {
          color: #96b0ce;
          font-size: 12px;
        }
        .cm-custom-note {
          grid-column: 1 / -1;
          display: flex;
          align-items: center;
          justify-content: space-between;
          gap: 12px;
          padding: 12px 14px;
          border-radius: 13px;
          border: 1px dashed rgba(255, 184, 118, 0.42);
          background: rgba(255, 146, 72, 0.08);
          color: #dbe7f3;
        }
        .cm-custom-note-left {
          display: flex;
          align-items: center;
          gap: 11px;
          min-width: 0;
        }
        .cm-custom-note-icon {
          width: 32px;
          height: 32px;
          border-radius: 9px;
          background: rgba(255, 146, 72, 0.2);
          color: #ffb578;
          display: inline-flex;
          align-items: center;
          justify-content: center;
          flex-shrink: 0;
        }
        .cm-custom-note-icon svg {
          width: 18px;
          height: 18px;
        }
        .cm-custom-note-title {
          font-size: 13.5px;
          font-weight: 800;
          color: #f8fafc;
        }
        .cm-custom-note-sub {
          font-size: 12.5px;
          color: #a7c1df;
          margin-top: 2px;
        }
        .cm-oven-grid {
          display: grid;
          grid-template-columns: repeat(2, 1fr);
          gap: 12px;
        }
        .cm-oven-card {
          background: rgba(255, 255, 255, 0.04);
          border: 1px solid rgba(255, 255, 255, 0.1);
          border-radius: 14px;
          padding: 13px 15px;
          display: flex;
          flex-direction: column;
          gap: 6px;
        }
        .cm-oven-card.active {
          border-color: rgba(255, 148, 72, 0.75);
          background: rgba(255, 136, 56, 0.13);
        }
        .cm-oven-top {
          display: flex;
          align-items: center;
          justify-content: space-between;
          gap: 8px;
        }
        .cm-oven-name {
          font-size: 14.5px;
          font-weight: 700;
          color: #e2ecf7;
        }
        .cm-oven-temp {
          font-size: 28px;
          font-weight: 800;
          color: #ffffff;
          line-height: 1.1;
        }
        .cm-oven-meta {
          font-size: 13.5px;
          color: #a3bad4;
        }
        .cm-foot {
          display: flex;
          justify-content: space-between;
          align-items: center;
          padding-top: 4px;
        }
        .cook-modal .cm-foot .rd-mini-btn {
          font-size: 13.5px;
          font-weight: 700;
          padding: 8px 16px;
          border-radius: 11px;
        }
        @media (max-width: 640px) {
          .cook-backdrop {
            padding: 8px;
          }
          .cook-modal {
            width: 100%;
            max-height: 95%;
            border-radius: 16px;
            padding: 14px 14px 16px;
            gap: 11px;
          }
          .cm-head {
            gap: 10px;
          }
          .cm-title-wrap {
            gap: 9px;
          }
          .cm-head-icon {
            width: 34px;
            height: 34px;
            border-radius: 10px;
          }
          .cm-head-icon svg {
            width: 19px;
            height: 19px;
          }
          .cm-title {
            font-size: 17px;
          }
          .cm-sub {
            font-size: 12px;
          }
          .cook-modal .rd-close {
            width: 32px;
            height: 32px;
            font-size: 19px;
            border-radius: 9px;
          }
          .cm-section-card {
            border-radius: 14px;
            padding: 12px;
            gap: 10px;
          }
          .cm-sec-label {
            font-size: 12px;
            gap: 6px;
          }
          .cm-sec-label svg {
            width: 15px;
            height: 15px;
          }
          .cm-status-pill {
            font-size: 11.5px;
            padding: 4px 9px;
          }
          .cm-gauge-row {
            flex-direction: column;
            align-items: stretch;
            gap: 12px;
          }
          .cm-gauge-wrap {
            width: 136px;
            height: 124px;
            margin: 0 auto;
          }
          .cm-gauge-temp {
            font-size: 30px;
          }
          .cm-gauge-target {
            font-size: 12.5px;
            margin-top: 2px;
          }
          .cm-stats-grid {
            width: 100%;
            gap: 8px;
          }
          .cm-stat-box {
            padding: 8px 10px;
            border-radius: 11px;
          }
          .cm-stat-k {
            font-size: 11px;
          }
          .cm-stat-v {
            font-size: 16px;
          }
          .cm-stat-box.cm-stat-eta {
            padding: 10px 12px;
          }
          .cm-stat-eta-val {
            font-size: 22px;
          }
          .cm-stat-rate-sub {
            font-size: 11px;
          }
          .cm-stat-pull-badge {
            font-size: 10.5px;
            padding: 2px 7px;
          }
          .cm-chart-card {
            padding: 10px 10px 8px;
            border-radius: 12px;
            gap: 6px;
          }
          .cm-chart-title {
            font-size: 11.5px;
          }
          .cm-chart-pill {
            font-size: 11px;
            padding: 2px 8px;
          }
          .cm-chart-svg-wrap,
          .cm-chart-svg {
            height: 150px;
          }
          .cm-stepper-card {
            padding: 9px 10px;
            gap: 8px;
            border-radius: 12px;
          }
          .cm-step-group {
            gap: 5px;
          }
          .cm-step-btn {
            min-width: 40px;
            height: 36px;
            padding: 0 8px;
            font-size: 13px;
            border-radius: 9px;
          }
          .cm-step-readout {
            min-width: 68px;
          }
          .cm-step-readout-val {
            font-size: 19px;
          }
          .cm-step-readout-lbl {
            font-size: 10px;
          }
          .cm-protein-tabs {
            gap: 5px;
          }
          .cm-protein-tab {
            padding: 7px 4px;
            gap: 3px;
            font-size: 11px;
            border-radius: 10px;
          }
          .cm-protein-icon {
            width: 18px;
            height: 18px;
          }
          .cm-protein-icon svg {
            width: 16px;
            height: 16px;
          }
          .cm-cut-grid {
            grid-template-columns: repeat(2, minmax(0, 1fr));
            gap: 7px;
          }
          .cm-cut-card {
            padding: 8px 9px;
            gap: 4px;
            border-radius: 10px;
          }
          .cm-cut-name {
            font-size: 12.5px;
          }
          .cm-cut-doneness {
            font-size: 10px;
            padding: 1px 6px;
          }
          .cm-cut-meta {
            font-size: 11.5px;
          }
          .cm-cut-temp {
            font-size: 13px;
          }
          .cm-cut-rest {
            font-size: 11px;
          }
          .cm-custom-note {
            padding: 10px;
            gap: 8px;
            flex-wrap: wrap;
          }
          .cm-oven-grid {
            gap: 8px;
          }
          .cm-oven-card {
            padding: 10px 11px;
            border-radius: 12px;
            gap: 4px;
          }
          .cm-oven-name {
            font-size: 13px;
          }
          .cm-oven-temp {
            font-size: 22px;
          }
          .cm-oven-meta {
            font-size: 12px;
          }
          .cook-modal .cm-foot .rd-mini-btn {
            font-size: 12.5px;
            padding: 7px 12px;
            border-radius: 9px;
          }
        }
        @media (max-width: 400px) {
          .cook-backdrop {
            padding: 4px;
          }
          .cook-modal {
            padding: 11px 10px 13px;
            border-radius: 14px;
          }
          .cm-gauge-wrap {
            width: 120px;
            height: 110px;
          }
          .cm-gauge-temp {
            font-size: 26px;
          }
          .cm-chart-svg-wrap,
          .cm-chart-svg {
            height: 132px;
          }
          .cm-step-btn {
            min-width: 34px;
            height: 34px;
            padding: 0 6px;
            font-size: 12px;
          }
          .cm-step-readout {
            min-width: 58px;
          }
          .cm-step-readout-val {
            font-size: 17px;
          }
          .cm-protein-tabs {
            grid-template-columns: repeat(3, minmax(0, 1fr));
          }
          .cm-cut-grid,
          .cm-oven-grid {
            grid-template-columns: 1fr;
          }
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
    const liveTargetF =
      targetNum !== null
        ? Math.round(targetNum)
        : attrTarget !== undefined && attrTarget !== null && !Number.isNaN(Number(attrTarget))
          ? Math.round(Number(attrTarget))
          : null;
    let targetF = liveTargetF;
    if (this._optimisticTargetF !== undefined && this._optimisticTargetF !== null) {
      const ageMs = Date.now() - (this._optimisticTargetAt || 0);
      if (liveTargetF === this._optimisticTargetF || ageMs > 6000) {
        this._optimisticTargetF = null;
        this._optimisticTargetAt = 0;
      } else {
        targetF = this._optimisticTargetF;
      }
    }

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
    this._cookProteinCat = this._detectProteinCategory(this._evalMeaterStatus());
    this._renderCookModal();
    this._fetchCookHistory();
  }

  _closeCookModal() {
    this._cookModalOpen = false;
    this._cookProteinCat = null;
    this._cookSelectedCut = null;
    const backdrop = this.shadowRoot?.getElementById("cookBackdrop");
    if (backdrop) {
      backdrop.classList.remove("open");
      backdrop.setAttribute("aria-hidden", "true");
    }
    const tetherSvg = this.shadowRoot?.getElementById("cookTetherSvg");
    if (tetherSvg) {
      tetherSvg.innerHTML = "";
    }
  }

  _formatClockTime(tsMs) {
    if (!Number.isFinite(tsMs) || tsMs <= 0) return "--";
    const d = new Date(tsMs);
    let h = d.getHours();
    const m = d.getMinutes();
    const ampm = h >= 12 ? "PM" : "AM";
    h = h % 12 || 12;
    const mm = m < 10 ? `0${m}` : `${m}`;
    return `${h}:${mm} ${ampm}`;
  }

  _fetchCookHistory(force = false) {
    const nowMs = Date.now();
    const ttlMs = 30 * 1000;
    if (
      !force &&
      Array.isArray(this._cookHistoryPoints) &&
      this._cookHistoryFetchedAt &&
      nowMs - this._cookHistoryFetchedAt < ttlMs
    ) {
      return Promise.resolve(this._cookHistoryPoints);
    }
    if (this._cookHistoryInFlight) {
      return this._cookHistoryInFlight;
    }
    const entityId = OVEN_MEATER_CONFIG.meater.internalEntity;
    if (!this._hass || typeof this._hass.callApi !== "function") {
      const liveOnly = this._readNumericState(entityId);
      this._cookHistoryPoints =
        liveOnly !== null && liveOnly > 0 ? [{ ts: nowMs, temp: liveOnly }] : this._cookHistoryPoints || [];
      this._cookHistoryFetchedAt = nowMs;
      return Promise.resolve(this._cookHistoryPoints);
    }

    this._cookHistoryLoading = true;
    const startIso = new Date(nowMs - 4 * 3600 * 1000).toISOString();
    const apiPath = `history/period/${encodeURIComponent(startIso)}?filter_entity_id=${encodeURIComponent(entityId)}&minimal_response&no_attributes&significant_changes_only=0`;

    this._cookHistoryInFlight = Promise.resolve(this._hass.callApi("GET", apiPath))
      .then((raw) => {
        const rows = Array.isArray(raw) ? (Array.isArray(raw[0]) ? raw[0] : raw) : [];
        let pts = [];
        for (const item of rows) {
          if (!item || typeof item !== "object") continue;
          const sVal = item.s !== undefined ? item.s : item.state;
          if (sVal === undefined || sVal === null || sVal === "" || sVal === "unavailable" || sVal === "unknown") {
            continue;
          }
          const temp = Number(sVal);
          if (Number.isNaN(temp) || temp <= 0 || temp > 600) continue;

          let ts = NaN;
          if (item.lu !== undefined && item.lu !== null) {
            const luNum = Number(item.lu);
            if (!Number.isNaN(luNum)) {
              ts = luNum < 1e11 ? Math.round(luNum * 1000) : Math.round(luNum);
            } else {
              ts = Date.parse(String(item.lu));
            }
          } else if (item.last_updated || item.last_changed) {
            ts = Date.parse(String(item.last_updated || item.last_changed));
          }
          if (!Number.isFinite(ts) || ts <= 0) continue;
          pts.push({ ts, temp });
        }

        pts.sort((a, b) => a.ts - b.ts);

        const liveTemp = this._readNumericState(entityId);
        const curNow = Date.now();
        if (liveTemp !== null && liveTemp > 0) {
          if (
            pts.length === 0 ||
            Math.abs(curNow - pts[pts.length - 1].ts) > 1000 ||
            pts[pts.length - 1].temp !== liveTemp
          ) {
            pts.push({ ts: curNow, temp: liveTemp });
          }
        }

        if (pts.length > 160) {
          const maxPts = 160;
          const sampled = [pts[0]];
          const step = (pts.length - 1) / (maxPts - 1);
          for (let i = 1; i < maxPts - 1; i++) {
            sampled.push(pts[Math.round(i * step)]);
          }
          sampled.push(pts[pts.length - 1]);
          pts = sampled;
        }

        this._cookHistoryPoints = pts;
        this._cookHistoryFetchedAt = curNow;
        this._cookHistoryLoading = false;
        this._cookHistoryInFlight = null;
        if (this._cookModalOpen) {
          this._renderCookModal();
        }
        return pts;
      })
      .catch(() => {
        this._cookHistoryLoading = false;
        this._cookHistoryInFlight = null;
        if (!Array.isArray(this._cookHistoryPoints)) {
          this._cookHistoryPoints = [];
        }
        if (this._cookModalOpen) {
          this._renderCookModal();
        }
        return this._cookHistoryPoints;
      });

    return this._cookHistoryInFlight;
  }

  _computeCookProjection(meater, historyPoints) {
    const nowMs = Date.now();
    const rawPts = Array.isArray(historyPoints) ? historyPoints : [];
    const pts = [];
    for (const p of rawPts) {
      if (p && Number.isFinite(p.ts) && Number.isFinite(p.temp) && p.temp > 0) {
        pts.push({ ts: p.ts, temp: p.temp });
      }
    }
    pts.sort((a, b) => a.ts - b.ts);

    const liveTemp = meater.internalF !== null && Number.isFinite(meater.internalF) ? meater.internalF : null;
    if (liveTemp !== null && liveTemp > 0) {
      if (pts.length === 0) {
        pts.push({ ts: nowMs, temp: liveTemp });
      } else {
        const last = pts[pts.length - 1];
        if (nowMs - last.ts > 2000 || Math.abs(last.temp - liveTemp) > 0.01) {
          pts.push({ ts: Math.max(nowMs, last.ts + 1000), temp: liveTemp });
        }
      }
    }

    const nowPt = pts.length ? pts[pts.length - 1] : null;
    const currentTemp = nowPt ? nowPt.temp : liveTemp;
    const nowTs = nowPt ? nowPt.ts : nowMs;
    const targetF = meater.targetF !== null ? meater.targetF : 165;
    const pullOffset = OVEN_MEATER_CONFIG.meater.carryoverPullOffsetF || 5;
    const pullF = targetF - pullOffset;

    let regRate = null;
    if (pts.length >= 2) {
      const windowStart = nowTs - 30 * 60 * 1000;
      let winPts = pts.filter((p) => p.ts >= windowStart);
      if (winPts.length < 2) {
        winPts = pts.slice(-Math.min(pts.length, 10));
      }
      const spanMin = (winPts[winPts.length - 1].ts - winPts[0].ts) / 60000;
      if (winPts.length >= 2 && spanMin >= 0.5) {
        const t0 = winPts[0].ts;
        let sumT = 0;
        let sumY = 0;
        let sumTY = 0;
        let sumTT = 0;
        const n = winPts.length;
        for (const p of winPts) {
          const t = (p.ts - t0) / 60000;
          const y = p.temp;
          sumT += t;
          sumY += y;
          sumTY += t * y;
          sumTT += t * t;
        }
        const denom = n * sumTT - sumT * sumT;
        if (Math.abs(denom) > 1e-6) {
          regRate = (n * sumTY - sumT * sumY) / denom;
        }
      }
    }

    const etaFallbackRate =
      currentTemp !== null &&
      meater.etaMin !== null &&
      Number.isFinite(meater.etaMin) &&
      meater.etaMin > 0 &&
      targetF > currentTemp
        ? (targetF - currentTemp) / meater.etaMin
        : null;

    let effectiveRate = null;
    if (meater.rateFPerMin !== null && Number.isFinite(meater.rateFPerMin) && meater.rateFPerMin > 0.02) {
      effectiveRate = meater.rateFPerMin;
    } else if (etaFallbackRate !== null && etaFallbackRate > 0.02) {
      effectiveRate = etaFallbackRate;
    } else if (regRate !== null && regRate > 0.02) {
      effectiveRate = regRate;
    }

    const displayRate =
      meater.rateFPerMin !== null && Number.isFinite(meater.rateFPerMin)
        ? meater.rateFPerMin
        : effectiveRate !== null
          ? effectiveRate
          : regRate;

    let hasProjection = false;
    let projMins = 0;
    let endTemp = currentTemp;
    let endTs = nowTs;
    let pullTs = null;
    let pullClock = null;
    let targetClock = null;
    let etaClockLabel = "";

    if (currentTemp !== null && effectiveRate !== null && effectiveRate > 0.02 && currentTemp < targetF) {
      hasProjection = true;
      const rawMinsToTarget = (targetF - currentTemp) / effectiveRate;
      projMins = Math.min(180, Math.max(1, rawMinsToTarget));
      endTemp = rawMinsToTarget <= 180 ? targetF : currentTemp + effectiveRate * 180;
      endTs = nowTs + projMins * 60 * 1000;
      targetClock = this._formatClockTime(endTs);

      if (currentTemp < pullF) {
        const rawMinsToPull = (pullF - currentTemp) / effectiveRate;
        if (rawMinsToPull <= 180) {
          pullTs = nowTs + rawMinsToPull * 60 * 1000;
          pullClock = this._formatClockTime(pullTs);
        }
      }
      etaClockLabel = pullClock ? `Pull ~${pullClock}` : `ETA ~${targetClock}`;
    }

    return {
      pts,
      nowTs,
      currentTemp,
      targetF,
      pullF,
      effectiveRate,
      displayRate,
      hasProjection,
      projMins,
      endTemp,
      endTs,
      pullTs,
      pullClock,
      targetClock,
      etaClockLabel,
    };
  }

  _stepMeaterTargetTemp(delta) {
    if (!this._hass || Number.isNaN(delta)) return;
    const m = this._evalMeaterStatus();
    const base = m.targetF !== null ? m.targetF : 165;
    const next = Math.max(m.minTarget, Math.min(m.maxTarget, Math.round(base + delta)));
    this._optimisticTargetF = next;
    this._optimisticTargetAt = Date.now();
    this._hass.callService("number", "set_value", {
      entity_id: OVEN_MEATER_CONFIG.meater.targetEntity,
      value: next,
    });
    if (this._cookModalOpen) {
      this._renderCookModal();
    }
  }

  _selectMeaterPreset(option) {
    if (!this._hass || !option) return;
    this._hass.callService("select", "select_option", {
      entity_id: OVEN_MEATER_CONFIG.meater.presetEntity,
      option,
    });
  }

  _detectProteinCategory(meater) {
    const cats = OVEN_MEATER_CONFIG.meater.proteinCategories || [];
    const preset = String(meater?.currentPreset || "").trim();
    const presetLower = preset.toLowerCase();
    if (preset && presetLower !== "custom") {
      for (const cat of cats) {
        for (const cut of cat.cuts || []) {
          if (
            cut.presetOption === preset ||
            cut.label === preset ||
            presetLower.includes(String(cut.cut || "").toLowerCase())
          ) {
            return cat.id;
          }
        }
      }
      if (/(turkey|chicken|duck|poultry)/.test(presetLower)) return "poultry";
      if (/(prime rib|steak|brisket|beef|roast)/.test(presetLower)) return "beef";
      if (/(pork|chop|tenderloin|shoulder|butt|rib)/.test(presetLower)) return "pork";
      if (/(lamb|salmon|fish|seafood)/.test(presetLower)) return "lamb_fish";
    }
    const targetF = meater?.targetF;
    if (targetF !== null && Number.isFinite(targetF)) {
      for (const cat of cats) {
        if ((cat.cuts || []).some((c) => c.targetF === targetF)) {
          return cat.id;
        }
      }
    }
    return "custom";
  }

  _selectCookProteinCat(catId) {
    if (!catId) return;
    const cats = OVEN_MEATER_CONFIG.meater.proteinCategories || [];
    if (!cats.some((c) => c.id === catId)) return;
    this._cookProteinCat = catId;
    if (this._cookModalOpen) {
      this._renderCookModal();
    }
  }

  _selectMeaterCut(catId, cutIdx) {
    if (!this._hass) return;
    const cats = OVEN_MEATER_CONFIG.meater.proteinCategories || [];
    const cat = cats.find((c) => c.id === catId);
    const idx = Number(cutIdx);
    const cut = cat && Array.isArray(cat.cuts) ? cat.cuts[idx] : null;
    if (!cut) return;
    this._cookProteinCat = cat.id;
    this._cookSelectedCut = `${cat.id}:${idx}`;
    const m = this._evalMeaterStatus();
    const opts = Array.isArray(m.presetOptions) ? m.presetOptions : [];
    let presetToSelect = null;
    if (cut.presetOption && opts.includes(cut.presetOption)) {
      presetToSelect = cut.presetOption;
    } else if (cut.label && opts.includes(cut.label)) {
      presetToSelect = cut.label;
    } else if (opts.includes("Custom")) {
      presetToSelect = "Custom";
    }
    if (presetToSelect) {
      this._hass.callService("select", "select_option", {
        entity_id: OVEN_MEATER_CONFIG.meater.presetEntity,
        option: presetToSelect,
      });
    }
    if (Number.isFinite(cut.targetF)) {
      const next = Math.max(m.minTarget, Math.min(m.maxTarget, Math.round(cut.targetF)));
      this._optimisticTargetF = next;
      this._optimisticTargetAt = Date.now();
      this._hass.callService("number", "set_value", {
        entity_id: OVEN_MEATER_CONFIG.meater.targetEntity,
        value: next,
      });
    }
    if (this._cookModalOpen) {
      this._renderCookModal();
    }
  }

  _buildCookGaugeSvg(meater) {
    const cx = 86;
    const cy = 84;
    const r = 64;
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
    const px1 = cx + (r - 8) * Math.cos(pullRad);
    const py1 = cy + (r - 8) * Math.sin(pullRad);
    const px2 = cx + (r + 8) * Math.cos(pullRad);
    const py2 = cy + (r + 8) * Math.sin(pullRad);

    let stroke = "#ff9248";
    if (meater.pillClass === "pull") stroke = "#ff5252";
    else if (meater.pillClass === "done") stroke = "#4ade80";
    else if (meater.pillClass === "almost") stroke = "#ffcc48";
    else if (meater.pillClass === "standby") stroke = "#56b4ff";

    const trackD = arcPath(startDeg, startDeg + sweepDeg);
    const progD = pct > 0.01 ? arcPath(startDeg, progDeg) : "";

    return `
      <svg class="cm-gauge-svg" viewBox="0 0 172 156" aria-hidden="true">
        <path d="${trackD}" fill="none" stroke="rgba(255,255,255,0.12)" stroke-width="12" stroke-linecap="round" />
        ${progD ? `<path d="${progD}" fill="none" stroke="${stroke}" stroke-width="12" stroke-linecap="round" />` : ""}
        <line x1="${px1.toFixed(2)}" y1="${py1.toFixed(2)}" x2="${px2.toFixed(2)}" y2="${py2.toFixed(2)}" stroke="#fde047" stroke-width="3" stroke-linecap="round" />
      </svg>
    `;
  }

  _buildCookChartSvg(meater, historyPoints) {
    const W = 680;
    const H = 210;
    const padL = 46;
    const padR = 24;
    const padT = 18;
    const padB = 28;
    const plotW = W - padL - padR;
    const plotH = H - padT - padB;

    const proj = this._computeCookProjection(meater, historyPoints);
    const pts = proj.pts;

    if (!pts.length && proj.currentTemp === null) {
      const msg = this._cookHistoryLoading
        ? "Loading temperature history..."
        : "Waiting for MEATER+ temperature telemetry...";
      return `
        <svg class="cm-chart-svg" viewBox="0 0 680 210" role="img" aria-label="Temperature History and Projected Trend">
          <rect x="${padL}" y="${padT}" width="${plotW}" height="${plotH}" rx="8" fill="rgba(15, 23, 42, 0.45)" stroke="rgba(148, 163, 184, 0.14)" />
          <text x="${(padL + plotW / 2).toFixed(1)}" y="${(padT + plotH / 2 + 4).toFixed(1)}" text-anchor="middle" fill="#8ea7c4" font-size="13" font-weight="600">${this._escapeHtml(msg)}</text>
        </svg>
      `;
    }

    const allTemps = pts.map((p) => p.temp);
    allTemps.push(proj.targetF, proj.pullF);
    if (proj.hasProjection && Number.isFinite(proj.endTemp)) {
      allTemps.push(proj.endTemp);
    }
    const rawMin = Math.min(...allTemps);
    const rawMax = Math.max(...allTemps);
    const minTemp = Math.max(30, Math.floor((rawMin - 8) / 10) * 10);
    const maxTemp = Math.min(550, Math.ceil((rawMax + 10) / 10) * 10);
    const tempSpan = Math.max(20, maxTemp - minTemp);

    const yForTemp = (t) => {
      const clamped = Math.max(minTemp, Math.min(maxTemp, t));
      return padT + plotH - ((clamped - minTemp) / tempSpan) * plotH;
    };

    const nowTs = proj.nowTs;
    const earliestTs = pts.length >= 2 ? pts[0].ts : nowTs - 20 * 60 * 1000;
    const projSpanMs = proj.hasProjection ? proj.projMins * 60 * 1000 : 20 * 60 * 1000;
    const histSpanMs = Math.max(20 * 60 * 1000, nowTs - earliestTs, Math.round(projSpanMs * 0.35));
    const futureSpanMs = proj.hasProjection
      ? Math.max(Math.round(projSpanMs * 1.12), Math.round(histSpanMs * 0.28))
      : Math.max(15 * 60 * 1000, Math.round(histSpanMs * 0.22));
    const startTs = nowTs - histSpanMs;
    const endTs = nowTs + futureSpanMs;
    const totalSpanMs = Math.max(60 * 1000, endTs - startTs);

    const xForTs = (ts) => {
      const clamped = Math.max(startTs, Math.min(endTs, ts));
      return padL + ((clamped - startTs) / totalSpanMs) * plotW;
    };

    const gridLines = [];
    const ySteps = 4;
    for (let i = 0; i <= ySteps; i++) {
      const val = Math.round(minTemp + (tempSpan * i) / ySteps);
      const y = yForTemp(val);
      gridLines.push(
        `<line x1="${padL}" y1="${y.toFixed(1)}" x2="${padL + plotW}" y2="${y.toFixed(1)}" stroke="rgba(148, 163, 184, 0.16)" stroke-width="1" />`,
        `<text x="${padL - 8}" y="${(y + 4).toFixed(1)}" text-anchor="end" fill="#94aecb" font-size="12" font-weight="600">${val}°F</text>`
      );
    }

    const nowX = xForTs(nowTs);
    const futureW = Math.max(0, padL + plotW - nowX);

    const tickCandidates = [
      { ts: startTs, label: this._formatClockTime(startTs) },
      { ts: startTs + histSpanMs * 0.5, label: this._formatClockTime(startTs + histSpanMs * 0.5) },
      { ts: nowTs, label: "Now", isNow: true },
    ];
    if (proj.hasProjection) {
      tickCandidates.push({ ts: proj.endTs, label: this._formatClockTime(proj.endTs) });
    } else {
      tickCandidates.push({ ts: endTs, label: this._formatClockTime(endTs) });
    }

    const xTicks = [];
    const placedXs = [nowX];
    for (const tc of tickCandidates) {
      const tx = xForTs(tc.ts);
      if (!tc.isNow && placedXs.some((px) => Math.abs(px - tx) < 56)) {
        continue;
      }
      if (!tc.isNow) placedXs.push(tx);
      xTicks.push(
        `<line x1="${tx.toFixed(1)}" y1="${padT + plotH}" x2="${tx.toFixed(1)}" y2="${padT + plotH + 5}" stroke="rgba(148, 163, 184, 0.4)" stroke-width="1" />`,
        `<text x="${tx.toFixed(1)}" y="${padT + plotH + 19}" text-anchor="middle" fill="${tc.isNow ? "#f8fafc" : "#94aecb"}" font-size="11.5" font-weight="${tc.isNow ? "700" : "600"}">${this._escapeHtml(tc.label)}</text>`
      );
    }

    const targetY = yForTemp(proj.targetF);
    const pullY = yForTemp(proj.pullF);

    const drawPts =
      pts.length === 1
        ? [{ ts: Math.max(startTs, pts[0].ts - Math.round(histSpanMs * 0.12)), temp: pts[0].temp }, pts[0]]
        : pts;
    const coords = drawPts.map((p) => [xForTs(p.ts), yForTemp(p.temp)]);
    const histLineD = coords
      .map(([x, y], idx) => `${idx === 0 ? "M" : "L"} ${x.toFixed(1)} ${y.toFixed(1)}`)
      .join(" ");
    const firstCoord = coords[0];
    const lastCoord = coords[coords.length - 1];
    const histAreaD = `${histLineD} L ${lastCoord[0].toFixed(1)} ${(padT + plotH).toFixed(1)} L ${firstCoord[0].toFixed(1)} ${(padT + plotH).toFixed(1)} Z`;

    const nowY = lastCoord[1];

    let trendSvg = "";
    if (proj.hasProjection) {
      const projX = xForTs(proj.endTs);
      const projY = yForTemp(proj.endTemp);
      let pullDotSvg = "";
      if (proj.pullTs && proj.pullTs < proj.endTs) {
        const pullX = xForTs(proj.pullTs);
        const pullDotY = yForTemp(proj.pullF);
        pullDotSvg = `<circle class="cm-chart-pull-dot" cx="${pullX.toFixed(1)}" cy="${pullDotY.toFixed(1)}" r="3.8" fill="#fde047" stroke="#0c1422" stroke-width="1.5" />`;
      }
      const lblAnchor = projX > padL + plotW - 95 ? "end" : "start";
      const lblX = lblAnchor === "end" ? projX - 8 : projX + 8;
      const lblY = Math.max(padT + 13, Math.min(padT + plotH - 8, projY - 8));
      trendSvg = `
        <line class="cm-chart-trend-line" x1="${nowX.toFixed(1)}" y1="${nowY.toFixed(1)}" x2="${projX.toFixed(1)}" y2="${projY.toFixed(1)}" stroke="#38bdf8" stroke-width="2.5" stroke-dasharray="7 5" stroke-linecap="round" />
        ${pullDotSvg}
        <circle class="cm-chart-trend-end" cx="${projX.toFixed(1)}" cy="${projY.toFixed(1)}" r="4.5" fill="#38bdf8" stroke="#ffffff" stroke-width="1.8" />
        <text class="cm-chart-eta-label" x="${lblX.toFixed(1)}" y="${lblY.toFixed(1)}" text-anchor="${lblAnchor}" fill="#bae6fd" font-size="12" font-weight="700">${this._escapeHtml(proj.etaClockLabel)}</text>
      `;
    }

    return `
      <svg class="cm-chart-svg" viewBox="0 0 680 210" role="img" aria-label="Temperature History and Projected Trend">
        <defs>
          <linearGradient id="cmHistFillGrad" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stop-color="#ff9248" stop-opacity="0.36" />
            <stop offset="100%" stop-color="#ff9248" stop-opacity="0.02" />
          </linearGradient>
        </defs>
        <rect class="cm-chart-future-zone" x="${nowX.toFixed(1)}" y="${padT}" width="${futureW.toFixed(1)}" height="${plotH}" fill="rgba(56, 189, 248, 0.065)" />
        ${gridLines.join("")}
        <line class="cm-chart-target-line" x1="${padL}" y1="${targetY.toFixed(1)}" x2="${padL + plotW}" y2="${targetY.toFixed(1)}" stroke="#4ade80" stroke-width="1.5" stroke-dasharray="6 4" stroke-opacity="0.88" />
        <text x="${padL + 6}" y="${Math.max(padT + 11, targetY - 4).toFixed(1)}" fill="#4ade80" font-size="11.5" font-weight="700">Target ${proj.targetF}°F</text>
        <line class="cm-chart-pull-line" x1="${padL}" y1="${pullY.toFixed(1)}" x2="${padL + plotW}" y2="${pullY.toFixed(1)}" stroke="#fde047" stroke-width="1.5" stroke-dasharray="4 4" stroke-opacity="0.88" />
        <text x="${padL + 108}" y="${Math.min(padT + plotH - 4, pullY + 13).toFixed(1)}" fill="#fde047" font-size="11.5" font-weight="700">Pull ${proj.pullF}°F</text>
        <line class="cm-chart-now-line" x1="${nowX.toFixed(1)}" y1="${padT}" x2="${nowX.toFixed(1)}" y2="${padT + plotH}" stroke="rgba(226, 232, 240, 0.45)" stroke-width="1.5" stroke-dasharray="4 4" />
        <path class="cm-chart-history-area" d="${histAreaD}" fill="url(#cmHistFillGrad)" />
        <path class="cm-chart-history-line" d="${histLineD}" fill="none" stroke="#ff9248" stroke-width="3" stroke-linecap="round" stroke-linejoin="round" />
        ${trendSvg}
        <circle cx="${nowX.toFixed(1)}" cy="${nowY.toFixed(1)}" r="8" fill="rgba(255, 146, 72, 0.28)" />
        <circle class="cm-chart-now-dot" cx="${nowX.toFixed(1)}" cy="${nowY.toFixed(1)}" r="4.5" fill="#ff9248" stroke="#ffffff" stroke-width="2" />
        ${xTicks.join("")}
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
    const rateSubStr =
      meater.rateFPerMin !== null
        ? `${meater.rateFPerMin >= 0 ? "+" : ""}${meater.rateFPerMin.toFixed(2)}°F/min`
        : "";
    const batStr = meater.batteryPct !== null ? `${Math.round(meater.batteryPct)}%` : "--";
    const pullTarget = meater.targetF !== null ? `${meater.targetF - OVEN_MEATER_CONFIG.meater.carryoverPullOffsetF}°F` : "--";

    const proj = this._computeCookProjection(meater, this._cookHistoryPoints);
    const climbRatePillTxt =
      proj.displayRate !== null && Number.isFinite(proj.displayRate)
        ? `${proj.displayRate >= 0 ? "+" : ""}${proj.displayRate.toFixed(2)}°F/min`
        : "Rate --°F/min";
    const pullClockPillTxt = proj.pullClock
      ? `Est. Pull ${proj.pullClock}`
      : proj.targetClock
        ? `Est. Done ${proj.targetClock}`
        : meater.internalF !== null && meater.targetF !== null && meater.internalF >= proj.pullF
          ? "Pull Reached"
          : "Est. Pull --";

    const cats = OVEN_MEATER_CONFIG.meater.proteinCategories || [];
    if (!this._cookProteinCat || !cats.some((c) => c.id === this._cookProteinCat)) {
      this._cookProteinCat = this._detectProteinCategory(meater);
    }
    const activeCat = cats.find((c) => c.id === this._cookProteinCat) || cats[0];

    const proteinTabsHtml = cats
      .map((cat) => {
        const isActive = cat.id === activeCat.id;
        return `
          <button class="cm-protein-tab ${isActive ? "active" : ""}" data-cm-action="protein-cat" data-cm-cat="${this._escapeHtml(cat.id)}" title="${this._escapeHtml(cat.label)}">
            <span class="cm-protein-icon">${SVG_ICONS[cat.icon] || ""}</span>
            <span class="cm-protein-lbl">${this._escapeHtml(cat.label)}</span>
          </button>
        `;
      })
      .join("");

    let activeCutIdx = -1;
    if (activeCat && Array.isArray(activeCat.cuts) && activeCat.cuts.length > 0) {
      if (this._cookSelectedCut && this._cookSelectedCut.startsWith(`${activeCat.id}:`)) {
        const parsedIdx = Number(this._cookSelectedCut.split(":")[1]);
        if (
          Number.isInteger(parsedIdx) &&
          activeCat.cuts[parsedIdx] &&
          (meater.targetF === null || activeCat.cuts[parsedIdx].targetF === meater.targetF)
        ) {
          activeCutIdx = parsedIdx;
        }
      }
      if (activeCutIdx === -1 && meater.currentPreset && meater.currentPreset !== "Custom") {
        activeCutIdx = activeCat.cuts.findIndex(
          (c) => c.presetOption === meater.currentPreset || c.label === meater.currentPreset
        );
      }
      if (activeCutIdx === -1 && meater.targetF !== null) {
        activeCutIdx = activeCat.cuts.findIndex((c) => c.targetF === meater.targetF);
      }
    }

    const cutGridHtml =
      activeCat && activeCat.id === "custom"
        ? `
          <div class="cm-custom-note">
            <div class="cm-custom-note-left">
              <span class="cm-custom-note-icon">${SVG_ICONS.custom || ""}</span>
              <div>
                <div class="cm-custom-note-title">Manual Target Temperature (${meater.targetF !== null ? `${meater.targetF}°F` : "--°F"})</div>
                <div class="cm-custom-note-sub">${this._escapeHtml(activeCat.note)}</div>
              </div>
            </div>
            <button class="cm-preset-btn ${meater.currentPreset === "Custom" ? "active" : ""}" data-cm-action="preset" data-cm-preset="Custom">Custom</button>
          </div>
        `
        : (activeCat?.cuts || [])
            .map((cut, idx) => {
              const isCutActive = idx === activeCutIdx;
              return `
                <button class="cm-cut-card ${isCutActive ? "active" : ""}" data-cm-action="protein-cut" data-cm-cat="${this._escapeHtml(activeCat.id)}" data-cm-cut-idx="${idx}" data-cm-preset="${this._escapeHtml(cut.presetOption || cut.label)}">
                  <div class="cm-cut-top">
                    <span class="cm-cut-name">${this._escapeHtml(cut.cut)}</span>
                    <span class="cm-cut-doneness">${this._escapeHtml(cut.doneness)}</span>
                  </div>
                  <div class="cm-cut-meta">
                    <span class="cm-cut-temp">${cut.targetF}°F</span>
                    <span class="cm-cut-rest">${cut.restMin}m rest</span>
                  </div>
                </button>
              `;
            })
            .join("");

    const proteinPickerHtml = `
      <div class="cm-protein-picker">
        <div class="cm-protein-tabs">${proteinTabsHtml}</div>
        <div class="cm-cut-grid">${cutGridHtml}</div>
      </div>
    `;

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
            <div class="cm-stat-box cm-stat-eta">
              <div class="cm-stat-eta-head">
                <div class="cm-stat-k">Estimated Time</div>
                <span class="cm-stat-pull-badge">Pull ${pullTarget}</span>
              </div>
              <div class="cm-stat-eta-main">
                <span class="cm-stat-eta-val">${etaStr}</span>
                ${rateSubStr ? `<span class="cm-stat-rate-sub">${rateSubStr}</span>` : ""}
              </div>
            </div>
            <div class="cm-stat-box">
              <div class="cm-stat-k">Ambient</div>
              <div class="cm-stat-v">${ambStr}</div>
            </div>
            <div class="cm-stat-box">
              <div class="cm-stat-k">Battery</div>
              <div class="cm-stat-v">${batStr}</div>
            </div>
          </div>
        </div>
        <div class="cm-chart-card">
          <div class="cm-chart-head">
            <span class="cm-chart-title">Temperature History &amp; Projected Trend</span>
            <div class="cm-chart-badges">
              <span class="cm-chart-pill rate">${this._escapeHtml(climbRatePillTxt)}</span>
              <span class="cm-chart-pill eta">${this._escapeHtml(pullClockPillTxt)}</span>
            </div>
          </div>
          <div class="cm-chart-svg-wrap">
            ${this._buildCookChartSvg(meater, this._cookHistoryPoints)}
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
        ${proteinPickerHtml}
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

    this._positionCookModalAndCone();
    if (typeof requestAnimationFrame === "function") {
      requestAnimationFrame(() => {
        if (this._cookModalOpen) this._positionCookModalAndCone();
      });
    }
  }

  _positionCookModalAndCone() {
    const sr = this.shadowRoot;
    if (!sr || !this._cookModalOpen) return;
    const backdrop = sr.getElementById("cookBackdrop");
    const modal = sr.getElementById("cookModal");
    const tetherSvg = sr.getElementById("cookTetherSvg");
    if (!backdrop || !modal) return;

    const bRect =
      typeof backdrop.getBoundingClientRect === "function"
        ? backdrop.getBoundingClientRect()
        : null;
    const bw = Number(bRect?.width) > 0 ? Number(bRect.width) : 1280;
    const bh = Number(bRect?.height) > 0 ? Number(bRect.height) : 800;
    const bLeft = Number(bRect?.left) || 0;
    const bTop = Number(bRect?.top) || 0;

    const hotspotEl =
      typeof sr.querySelector === "function"
        ? sr.querySelector(".cook-hotspot") ||
          sr.querySelector("[data-oven-meater-badge='lg_oven']") ||
          sr.querySelector('[data-oven-meater-badge="lg_oven"]')
        : null;
    const hRect =
      hotspotEl && typeof hotspotEl.getBoundingClientRect === "function"
        ? hotspotEl.getBoundingClientRect()
        : null;

    let ax = (OVEN_MEATER_CONFIG.hotspot.x / 100) * bw;
    let ay = (OVEN_MEATER_CONFIG.hotspot.y / 100) * bh;
    if (hRect && (Number(hRect.width) > 0 || Number(hRect.height) > 0)) {
      ax = Number(hRect.left) + Number(hRect.width) / 2 - bLeft;
      ay = Number(hRect.top) + Number(hRect.height) / 2 - bTop;
    }
    ax = Math.max(14, Math.min(bw - 14, ax));
    ay = Math.max(14, Math.min(bh - 14, ay));

    const margin = 14;
    const gap = 34;
    const roomLeft = ax - margin;
    const roomRight = bw - ax - margin;
    const roomAbove = ay - margin;
    const roomBelow = bh - ay - margin;

    const candidates = [
      { side: "left", room: roomLeft },
      { side: "right", room: roomRight },
      { side: "below", room: roomBelow },
      { side: "above", room: roomAbove },
    ];
    candidates.sort((a, b) => b.room - a.room);
    const best = candidates[0];

    const mStyle = modal.style || (modal.style = {});
    const isNarrow = bw < 680 || bh < 420 || best.room < 260;
    if (isNarrow) {
      mStyle.position = "relative";
      mStyle.left = "";
      mStyle.top = "";
      mStyle.maxWidth = "";
    } else if (best.side === "left" || best.side === "right") {
      const availW = best.room - gap;
      mStyle.maxWidth = availW >= 460 && availW < 780 ? `${Math.floor(availW)}px` : "";
    } else {
      mStyle.maxWidth = "";
    }

    const mRectInit =
      typeof modal.getBoundingClientRect === "function"
        ? modal.getBoundingClientRect()
        : null;
    const capW = mStyle.maxWidth ? parseFloat(mStyle.maxWidth) : 780;
    const mw =
      mRectInit && Number(mRectInit.width) > 0
        ? Math.min(Number(mRectInit.width), capW)
        : Math.min(bw - margin * 2, capW);
    const mh =
      mRectInit && Number(mRectInit.height) > 0
        ? Number(mRectInit.height)
        : Math.min(bh - margin * 2, 600);

    let placedLeft = Math.round((bw - mw) / 2);
    let placedTop = Math.round((bh - mh) / 2);

    if (!isNarrow) {
      let targetLeft = placedLeft;
      let targetTop = placedTop;
      if (best.side === "left") {
        targetLeft = ax - gap - mw;
        targetTop = ay - mh / 2;
      } else if (best.side === "right") {
        targetLeft = ax + gap;
        targetTop = ay - mh / 2;
      } else if (best.side === "above") {
        targetLeft = ax - mw / 2;
        targetTop = ay - gap - mh;
      } else {
        targetLeft = ax - mw / 2;
        targetTop = ay + gap;
      }
      const maxLeft = Math.max(margin, bw - margin - mw);
      const maxTop = Math.max(margin, bh - margin - mh);
      placedLeft = Math.round(Math.max(margin, Math.min(maxLeft, targetLeft)));
      placedTop = Math.round(Math.max(margin, Math.min(maxTop, targetTop)));
      mStyle.position = "absolute";
      mStyle.left = `${placedLeft}px`;
      mStyle.top = `${placedTop}px`;
    }

    const mRectFinal =
      typeof modal.getBoundingClientRect === "function"
        ? modal.getBoundingClientRect()
        : null;
    let ml = placedLeft;
    let mt = placedTop;
    let mr = placedLeft + mw;
    let mb = placedTop + mh;
    if (mRectFinal && Number(mRectFinal.width) > 0 && Number(mRectFinal.height) > 0) {
      const fLeft = Number(mRectFinal.left) - bLeft;
      const fTop = Number(mRectFinal.top) - bTop;
      const initLeft = mRectInit ? Number(mRectInit.left) - bLeft : fLeft;
      const initTop = mRectInit ? Number(mRectInit.top) - bTop : fTop;
      const layoutMoved =
        isNarrow ||
        Math.abs(fLeft - initLeft) > 0.5 ||
        Math.abs(fTop - initTop) > 0.5 ||
        (Math.abs(fLeft - placedLeft) < 2 && Math.abs(fTop - placedTop) < 2);
      if (layoutMoved) {
        ml = fLeft;
        mt = fTop;
        mr = ml + Math.min(Number(mRectFinal.width), capW);
        mb = mt + Number(mRectFinal.height);
      }
    }

    if (!tetherSvg) return;

    const mcx = (ml + mr) / 2;
    const mcy = (mt + mb) / 2;
    const inset = Math.min(24, Math.max(8, Math.floor(Math.min(mr - ml, mb - mt) * 0.18)));

    const horizSep = ax >= mr ? ax - mr : ax <= ml ? ml - ax : 0;
    const vertSep = ay >= mb ? ay - mb : ay <= mt ? mt - ay : 0;
    const useHorizontalEdge =
      horizSep > 0 || vertSep > 0
        ? horizSep >= vertSep
        : Math.abs(ax - mcx) / Math.max(1, mr - ml) >= Math.abs(ay - mcy) / Math.max(1, mb - mt);

    let c1x = 0;
    let c1y = 0;
    let c2x = 0;
    let c2y = 0;
    if (useHorizontalEdge) {
      const edgeX = ax >= mcx ? mr : ml;
      c1x = edgeX;
      c1y = mt + inset;
      c2x = edgeX;
      c2y = mb - inset;
    } else {
      const edgeY = ay >= mcy ? mb : mt;
      c1x = ml + inset;
      c1y = edgeY;
      c2x = mr - inset;
      c2y = edgeY;
    }

    const midX = (c1x + c2x) / 2;
    const midY = (c1y + c2y) / 2;
    const r1aX = ax + (c1x - ax) * 0.36;
    const r1aY = ay + (c1y - ay) * 0.36;
    const r1bX = ax + (c2x - ax) * 0.36;
    const r1bY = ay + (c2y - ay) * 0.36;
    const r2aX = ax + (c1x - ax) * 0.68;
    const r2aY = ay + (c1y - ay) * 0.68;
    const r2bX = ax + (c2x - ax) * 0.68;
    const r2bY = ay + (c2y - ay) * 0.68;

    tetherSvg.setAttribute("viewBox", `0 0 ${Math.round(bw)} ${Math.round(bh)}`);
    tetherSvg.innerHTML = `
      <defs>
        <linearGradient id="cookTetherGrad" gradientUnits="userSpaceOnUse" x1="${ax.toFixed(1)}" y1="${ay.toFixed(1)}" x2="${midX.toFixed(1)}" y2="${midY.toFixed(1)}">
          <stop offset="0%" stop-color="rgba(255, 148, 72, 0.34)" />
          <stop offset="100%" stop-color="rgba(255, 148, 72, 0.08)" />
        </linearGradient>
      </defs>
      <polygon class="cook-tether-cone" points="${ax.toFixed(1)},${ay.toFixed(1)} ${c1x.toFixed(1)},${c1y.toFixed(1)} ${c2x.toFixed(1)},${c2y.toFixed(1)}" fill="url(#cookTetherGrad)" />
      <line class="cook-tether-rung" x1="${r1aX.toFixed(1)}" y1="${r1aY.toFixed(1)}" x2="${r1bX.toFixed(1)}" y2="${r1bY.toFixed(1)}" stroke="rgba(255, 184, 118, 0.24)" stroke-width="1" stroke-dasharray="4 4" />
      <line class="cook-tether-rung" x1="${r2aX.toFixed(1)}" y1="${r2aY.toFixed(1)}" x2="${r2bX.toFixed(1)}" y2="${r2bY.toFixed(1)}" stroke="rgba(255, 184, 118, 0.24)" stroke-width="1" stroke-dasharray="5 4" />
      <line class="cook-tether-ray" x1="${ax.toFixed(1)}" y1="${ay.toFixed(1)}" x2="${c1x.toFixed(1)}" y2="${c1y.toFixed(1)}" stroke="rgba(255, 178, 106, 0.58)" stroke-width="1.5" stroke-linecap="round" />
      <line class="cook-tether-ray" x1="${ax.toFixed(1)}" y1="${ay.toFixed(1)}" x2="${c2x.toFixed(1)}" y2="${c2y.toFixed(1)}" stroke="rgba(255, 178, 106, 0.58)" stroke-width="1.5" stroke-linecap="round" />
      <circle class="cook-tether-halo" cx="${ax.toFixed(1)}" cy="${ay.toFixed(1)}" r="15" fill="rgba(255, 148, 72, 0.16)" stroke="rgba(255, 184, 118, 0.62)" stroke-width="1.5" />
      <circle class="cook-tether-dot" cx="${ax.toFixed(1)}" cy="${ay.toFixed(1)}" r="5.5" fill="#ff9448" stroke="#fff3e6" stroke-width="1.8" />
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
          <svg class="cook-tether-svg" id="cookTetherSvg" aria-hidden="true"></svg>
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
      if (e.target.id === "cookBackdrop" || e.target.id === "cookTetherSvg") this._closeCookModal();
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
      } else if (act === "protein-cat") {
        this._selectCookProteinCat(btn.dataset.cmCat);
      } else if (act === "protein-cut") {
        this._selectMeaterCut(btn.dataset.cmCat, Number(btn.dataset.cmCutIdx));
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

    if (!this._boundCookResize && typeof window !== "undefined") {
      this._boundCookResize = () => {
        if (this._cookModalOpen) {
          this._positionCookModalAndCone();
        }
      };
      window.addEventListener("resize", this._boundCookResize);
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
          ovenEl.classList.toggle("active", ovenEv.active);
          ovenEl.title = ovenEv.title;
          const ovenTxt = ovenEl.querySelector('[data-om-text="lg_oven"]');
          if (ovenTxt) ovenTxt.textContent = ovenEv.badgeText;
        }
        const meaterEv = this._evalMeaterStatus();
        const meaterEl = card?.querySelector('[data-oven-meater-badge="meater"]');
        if (meaterEl) {
          meaterEl.classList.remove("meater-standby", "meater-cooking", "meater-almost", "meater-pull", "meater-done");
          meaterEl.classList.add(meaterEv.badgeClass);
          meaterEl.classList.toggle("active", meaterEv.active);
          meaterEl.title = `MEATER+ · ${meaterEv.cookStatus}`;
          const meaterTxt = meaterEl.querySelector('[data-om-text="meater"]');
          if (meaterTxt) meaterTxt.textContent = meaterEv.badgeText;
        }
      }
      /* OVEN_MEATER_UPDATE_END */"""

OVEN_MEATER_MODAL_REFRESH_JS = """    /* OVEN_MEATER_MODAL_REFRESH_BEGIN */
    if (this._cookModalOpen) {
      this._fetchCookHistory(false);
      this._renderCookModal();
    }
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
        "poultry:",
        "beef:",
        "pork:",
        "lamb_fish:",
        "custom:",
        "proteinCategories:",
        ".cook-hotspot",
        ".cook-modal",
        ".cm-protein-tabs",
        ".cm-cut-grid",
        ".cm-stat-eta",
        "@media (max-width: 640px)",
        "_optimisticTargetF",
        "min(92vw, 780px)",
        "rgba(4, 8, 15, 0.44)",
        "cookBackdrop",
        "cookTetherSvg",
        "cook-tether-svg",
        "cookModal",
        "_evalOvenStatus",
        "_evalMeaterStatus",
        "_collectOvenMeaterSigParts",
        "_openCookModal",
        "_closeCookModal",
        "_fetchCookHistory",
        "_computeCookProjection",
        "_buildCookGaugeSvg",
        "_buildCookChartSvg",
        "_stepMeaterTargetTemp",
        "_selectMeaterPreset",
        "_detectProteinCategory",
        "_selectCookProteinCat",
        "_selectMeaterCut",
        "_renderCookModal",
        "_positionCookModalAndCone",
        'data-cm-action="protein-cat"',
        'data-cm-action="protein-cut"',
        "Temperature History &amp; Projected Trend",
        "cm-chart-svg",
        'viewBox="0 0 172 156"',
        'viewBox="0 0 680 210"',
        "history/period/",
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

    # Clean single marker form inside template literal:
    modal_html_block = (
        '        ${/* OVEN_MEATER_MODAL_HTML_BEGIN */ ""}\n'
        '        <div class="cook-backdrop" id="cookBackdrop" aria-hidden="true">\n'
        '          <svg class="cook-tether-svg" id="cookTetherSvg" aria-hidden="true"></svg>\n'
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

    # Strip any existing OVEN_MEATER_MODAL_REFRESH block (including any stray block
    # previously injected inside _pingDevice() / _ringTag()) before anchoring cleanly
    # at the end of _updateState().
    modal_refresh_re = re.compile(
        r"[ \t]*/\*\s*OVEN_MEATER_MODAL_REFRESH_BEGIN\s*\*/[\s\S]*?/\*\s*OVEN_MEATER_MODAL_REFRESH_END\s*\*/(?:\r?\n)?",
    )
    out = modal_refresh_re.sub("", out)

    out = _replace_or_insert_block(
        out,
        "/* OVEN_MEATER_MODAL_REFRESH_BEGIN */",
        "/* OVEN_MEATER_MODAL_REFRESH_END */",
        OVEN_MEATER_MODAL_REFRESH_JS,
        (
            r"if\s*\(this\._thermoModalOpen\)\s*this\._renderThermostatModal\(\);\s*\n\s*if\s*\(this\._presenceModalOpen\)\s*this\._renderPresenceModal\(\);",
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
