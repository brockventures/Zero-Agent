#!/usr/bin/env python3
"""Gotchi V4.3 Interactive Hardware Assembly, Pinout & Wiring Portal.

Served at https://zero.brock.ventures/gotchi (and /gotchi/assembly) by zero_health_server.py.
Features:
- Interactive soldering & mechanical assembly checklist with localStorage persistence and live progress bar.
- Visual XIAO ESP32S3 Sense dual-column pinout map (D0-D10, 3V3, 5V, GND + bottom BAT+/BAT- pads).
- Clear schematic diagrams for the 3x 220 kOhm resistor network (D2 VBUS sense + D4 VBAT divider).
- Embedded V4.3 CAD cross-section and assembly renders.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

RENDERS_DIR = Path("/workspace/gotchi/cad/renders")

ALLOWED_ASSETS = {
    "v4_3_build_preview.png": RENDERS_DIR / "v4_3_build_preview.png",
    "v4_3_camera_yz_cross_section.png": RENDERS_DIR / "v4_3_camera_yz_cross_section.png",
    "v4_3_camera_springs_v2_preview.png": RENDERS_DIR / "v4_4_back_and_v3_pcb_springs_preview.png",
    "v4_4_back_and_v3_pcb_springs_preview.png": RENDERS_DIR / "v4_4_back_and_v3_pcb_springs_preview.png",
}


def resolve_asset_bytes(asset_name: str) -> Optional[bytes]:
    """Return PNG bytes for an allowlisted CAD render asset."""
    clean = (asset_name or "").strip().lstrip("/")
    target = ALLOWED_ASSETS.get(clean)
    if target and target.is_file():
        return target.read_bytes()
    return None


def render_gotchi_assembly_html() -> str:
    """Render the self-contained dark-mode Gotchi V4.3 Assembly & Wiring Guide HTML."""
    return """<!DOCTYPE html>
<html lang="en" class="dark">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>GOTCHI V4.3 // Interactive Wiring &amp; Assembly Bench Guide</title>
  <script src="https://cdn.tailwindcss.com"></script>
  <script>
    tailwind.config = {
      darkMode: 'class',
      theme: {
        extend: {
          colors: {
            slate: {
              850: '#111827',
              900: '#0f172a',
              950: '#020617',
            }
          },
          fontFamily: {
            mono: ['ui-monospace', 'SFMono-Regular', 'Menlo', 'Monaco', 'Consolas', 'monospace']
          }
        }
      }
    }
  </script>
  <style>
    .check-row input:checked + div {
      opacity: 0.55;
      text-decoration: line-through;
    }
    .check-row:hover {
      background-color: rgba(30, 41, 59, 0.55);
    }
    ::-webkit-scrollbar { width: 6px; height: 6px; }
    ::-webkit-scrollbar-track { background: #020617; }
    ::-webkit-scrollbar-thumb { background: #334155; border-radius: 3px; }
  </style>
</head>
<body class="bg-slate-950 text-slate-100 font-sans min-h-screen antialiased selection:bg-cyan-500/30 selection:text-cyan-200">

  <!-- Sticky Top Header & Progress Bar -->
  <header class="border-b border-slate-800 bg-slate-950/90 backdrop-blur sticky top-0 z-50 px-4 lg:px-8 py-3.5">
    <div class="max-w-7xl mx-auto flex flex-col sm:flex-row sm:items-center justify-between gap-3">
      <div class="flex items-center gap-3">
        <div class="h-9 w-9 rounded-lg bg-cyan-500/10 border border-cyan-500/30 flex items-center justify-center text-lg font-bold text-cyan-400 font-mono">
          G
        </div>
        <div>
          <div class="flex items-center gap-2 flex-wrap">
            <h1 class="font-bold text-base sm:text-lg tracking-tight text-white">GOTCHI V4.3 // Bench Wiring &amp; Assembly</h1>
            <span class="text-xs px-2 py-0.5 rounded-full font-mono font-semibold bg-cyan-500/10 text-cyan-400 border border-cyan-500/20">54 &times; 44 &times; 18.5 mm</span>
            <span class="text-xs px-2 py-0.5 rounded-full font-mono font-semibold bg-emerald-500/10 text-emerald-400 border border-emerald-500/20">Option 1A Top USB-C</span>
          </div>
          <p class="text-xs text-slate-400 font-mono">XIAO ESP32S3 Sense &middot; Waveshare 1.54&quot; V2 &middot; MAX98357A &middot; 1000mAh 803040 LiPo</p>
        </div>
      </div>

      <!-- Interactive Progress Tracker -->
      <div class="flex items-center gap-3">
        <div class="w-40 sm:w-48">
          <div class="flex justify-between text-[11px] font-mono text-slate-400 mb-1">
            <span>Bench Progress</span>
            <span id="progress-label" class="text-cyan-400 font-bold">0 / 28 (0%)</span>
          </div>
          <div class="w-full h-2 bg-slate-900 rounded-full overflow-hidden border border-slate-800">
            <div id="progress-bar" class="h-full bg-gradient-to-r from-cyan-500 to-emerald-400 transition-all duration-200" style="width: 0%"></div>
          </div>
        </div>
        <button onclick="resetChecklist()" class="text-xs font-mono text-slate-400 hover:text-rose-400 bg-slate-900 hover:bg-slate-800 border border-slate-800 px-2.5 py-1.5 rounded-lg transition cursor-pointer">
          Reset
        </button>
        <a href="https://zero.brock.ventures/" class="text-xs font-mono text-indigo-400 hover:text-indigo-300 bg-indigo-500/10 border border-indigo-500/20 px-2.5 py-1.5 rounded-lg transition">
          &larr; Zero Dash
        </a>
      </div>
    </div>
  </header>

  <main class="max-w-7xl mx-auto px-4 lg:px-8 py-6 space-y-8">

    <!-- Quick Navigation Pills -->
    <nav class="flex flex-wrap gap-2 text-xs font-mono">
      <a href="#pinout-map" class="px-3 py-1.5 rounded-lg bg-slate-900 border border-slate-800 hover:border-cyan-500/50 text-cyan-400 transition">1. Visual XIAO Pinout</a>
      <a href="#resistor-network" class="px-3 py-1.5 rounded-lg bg-slate-900 border border-slate-800 hover:border-amber-500/50 text-amber-400 transition">2. 3x 220 k&Omega; Resistor Schematic</a>
      <a href="#solder-steps" class="px-3 py-1.5 rounded-lg bg-slate-900 border border-slate-800 hover:border-emerald-500/50 text-emerald-400 transition">3. Step-by-Step Soldering Checklist</a>
      <a href="#mechanical-seating" class="px-3 py-1.5 rounded-lg bg-slate-900 border border-slate-800 hover:border-purple-500/50 text-purple-400 transition">4. V4.3 Clamshell &amp; Camera Seating</a>
      <a href="#smoke-test" class="px-3 py-1.5 rounded-lg bg-slate-900 border border-slate-800 hover:border-rose-500/50 text-rose-400 transition">5. Smoke Test &amp; Bring-Up</a>
    </nav>

    <!-- Safety Callout Banner -->
    <div class="bg-amber-500/10 border border-amber-500/30 rounded-xl p-4 flex items-start gap-3">
      <span class="text-amber-400 text-lg leading-none mt-0.5">&#9888;</span>
      <div class="text-xs sm:text-sm text-amber-200/90 space-y-1">
        <p class="font-bold text-amber-300">Bench Safety &amp; Boot-Glitch Invariants (Read Before Soldering):</p>
        <ul class="list-disc list-inside space-y-0.5 text-amber-100/80">
          <li><strong>Unplug USB-C Before Soldering LiPo:</strong> Cut the factory JST plug off the 803040 LiPo <strong>one wire at a time</strong> (never snip both leads simultaneously with metal flush cutters).</li>
          <li><strong>Remove Waveshare Standoffs &amp; PH2.0 Shroud:</strong> Unscrew all 4 brass standoffs from the Waveshare 1.54&quot; PCB and solder 30 AWG wires directly to the 8 top pads so the total stack clears the <strong>18.5 mm</strong> V4.3 case.</li>
          <li><strong>Pop-Free Audio Boot:</strong> Wire <code class="text-amber-300 font-mono">MAX98357A BCLK</code> to <code class="text-amber-300 font-mono">D7 (GPIO44)</code> (silent at reset) and <code class="text-amber-300 font-mono">LRC</code> to <code class="text-amber-300 font-mono">D6 (GPIO43)</code> so UART0 ROM boot text never pops the speaker.</li>
        </ul>
      </div>
    </div>

    <!-- SECTION 1: VISUAL XIAO ESP32S3 PINOUT & COLOR-CODED BUS MAP -->
    <section id="pinout-map" class="grid grid-cols-1 lg:grid-cols-12 gap-6">
      
      <!-- Left: Visual Physical Board Top-Down Diagram -->
      <div class="lg:col-span-5 bg-slate-900 border border-slate-800 rounded-xl p-5 space-y-4">
        <div class="flex items-center justify-between border-b border-slate-800 pb-3">
          <div>
            <h2 class="text-base font-bold text-white">XIAO / Meshnology W11-MB-V0.2 Pin Map</h2>
            <p class="text-xs text-slate-400 font-mono">Bottom silkscreen labels (<code class="text-cyan-300">IO1&ndash;IO9</code>, <code class="text-purple-300">TX0/RX0</code>, <code class="text-rose-300">B+/B-</code>) + Seeed <code class="text-slate-300">D0&ndash;D10</code></p>
          </div>
          <span class="text-[11px] font-mono px-2 py-0.5 rounded bg-slate-800 text-slate-300">W11-MB-V0.2 Verified</span>
        </div>

        <!-- Board Visual Container -->
        <div class="bg-slate-950 border-2 border-slate-800 rounded-2xl p-4 max-w-md mx-auto relative">
          <!-- USB-C Connector Pill at Top -->
          <div class="w-36 h-5 mx-auto -mt-6 mb-3 bg-slate-800 border-2 border-slate-600 rounded-md flex items-center justify-center text-[10px] font-mono font-bold text-slate-200 shadow">
            USB-C (TOP &middot; BOTTOM VIEW)
          </div>

          <!-- Dual-Row Pin Grid (Matches W11-MB-V0.2 Bottom Silkscreen Orientation!) -->
          <div class="grid grid-cols-2 gap-3 font-mono text-xs">
            <!-- Left Column in Bottom View: 5V, GND, 3.3V, [Skip IO13..10], IO9 -->
            <div class="space-y-2">
              <div class="flex items-center gap-2 bg-rose-500/10 border border-rose-500/40 rounded-lg p-2">
                <span class="px-1.5 py-0.5 rounded bg-rose-500 text-white font-bold">5V</span>
                <div class="leading-tight">
                  <div class="font-bold text-rose-300">5V / VBUS (Hole 1)</div>
                  <div class="text-[10px] text-slate-400">Amp VIN + 220k&rarr;IO3</div>
                </div>
              </div>
              <div class="flex items-center gap-2 bg-slate-800/80 border border-slate-600 rounded-lg p-2">
                <span class="px-1.5 py-0.5 rounded bg-slate-700 text-white font-bold">GND</span>
                <div class="leading-tight">
                  <div class="font-bold text-slate-200">Common GND (Hole 2)</div>
                  <div class="text-[10px] text-slate-400">ePaper, Amp, Btns</div>
                </div>
              </div>
              <div class="flex items-center gap-2 bg-orange-500/10 border border-orange-500/40 rounded-lg p-2">
                <span class="px-1.5 py-0.5 rounded bg-orange-400 text-slate-950 font-bold">3.3V</span>
                <div class="leading-tight">
                  <div class="font-bold text-orange-300">3.3V Rail (Hole 3)</div>
                  <div class="text-[10px] text-slate-400">ePaper VCC</div>
                </div>
              </div>
              <div class="bg-rose-950/40 border border-dashed border-rose-500/40 rounded-lg p-2 text-[10px] text-rose-300 leading-snug">
                <div class="font-bold text-rose-400">HOLES 4&ndash;7: IO13, IO12, IO11, IO10</div>
                <div>DO NOT SOLDER &mdash; Used internally by Camera DVP Bus (PCLK/Y7/Y8/XCLK).</div>
              </div>
              <div class="flex items-center gap-2 bg-cyan-500/10 border border-cyan-500/30 rounded-lg p-2">
                <span class="px-1.5 py-0.5 rounded bg-cyan-500 text-slate-950 font-bold">IO9</span>
                <div class="leading-tight">
                  <div class="font-bold text-cyan-300">ePaper DIN (Hole 8)</div>
                  <div class="text-[10px] text-slate-400">D10 &middot; Bottom-Left Hole</div>
                </div>
              </div>
            </div>

            <!-- Right Column in Bottom View: IO1 to IO8 (Holes 1 to 8) -->
            <div class="space-y-1.5">
              <div class="flex items-center justify-between gap-2 bg-cyan-500/10 border border-cyan-500/30 rounded-lg p-1.5">
                <div class="leading-tight">
                  <div class="font-bold text-cyan-300">ePaper RST</div>
                  <div class="text-[10px] text-slate-400">Hole 1 (D0)</div>
                </div>
                <span class="px-1.5 py-0.5 rounded bg-cyan-500 text-slate-950 font-bold">IO1</span>
              </div>
              <div class="flex items-center justify-between gap-2 bg-cyan-500/10 border border-cyan-500/30 rounded-lg p-1.5">
                <div class="leading-tight">
                  <div class="font-bold text-cyan-300">ePaper CS</div>
                  <div class="text-[10px] text-slate-400">Hole 2 (D1)</div>
                </div>
                <span class="px-1.5 py-0.5 rounded bg-cyan-500 text-slate-950 font-bold">IO2</span>
              </div>
              <div class="flex items-center justify-between gap-2 bg-amber-500/10 border border-amber-500/40 rounded-lg p-1.5">
                <div class="leading-tight">
                  <div class="font-bold text-amber-300">BTN_TOP + 5V</div>
                  <div class="text-[10px] text-slate-400">Hole 3 (D2)</div>
                </div>
                <span class="px-1.5 py-0.5 rounded bg-amber-400 text-slate-950 font-bold">IO3</span>
              </div>
              <div class="flex items-center justify-between gap-2 bg-cyan-500/10 border border-cyan-500/30 rounded-lg p-1.5">
                <div class="leading-tight">
                  <div class="font-bold text-cyan-300">ePaper DC</div>
                  <div class="text-[10px] text-slate-400">Hole 4 (D3)</div>
                </div>
                <span class="px-1.5 py-0.5 rounded bg-cyan-500 text-slate-950 font-bold">IO4</span>
              </div>
              <div class="flex items-center justify-between gap-2 bg-amber-500/10 border border-amber-500/40 rounded-lg p-1.5">
                <div class="leading-tight">
                  <div class="font-bold text-amber-300">BTN_BOT + VBAT</div>
                  <div class="text-[10px] text-slate-400">Hole 5 (D4)</div>
                </div>
                <span class="px-1.5 py-0.5 rounded bg-amber-400 text-slate-950 font-bold">IO5</span>
              </div>
              <div class="flex items-center justify-between gap-2 bg-cyan-500/10 border border-cyan-500/30 rounded-lg p-1.5">
                <div class="leading-tight">
                  <div class="font-bold text-cyan-300">ePaper BUSY</div>
                  <div class="text-[10px] text-slate-400">Hole 6 (D5)</div>
                </div>
                <span class="px-1.5 py-0.5 rounded bg-cyan-500 text-slate-950 font-bold">IO6</span>
              </div>
              <div class="flex items-center justify-between gap-2 bg-cyan-500/10 border border-cyan-500/30 rounded-lg p-1.5">
                <div class="leading-tight">
                  <div class="font-bold text-cyan-300">ePaper CLK</div>
                  <div class="text-[10px] text-slate-400">Hole 7 (D8)</div>
                </div>
                <span class="px-1.5 py-0.5 rounded bg-cyan-500 text-slate-950 font-bold">IO7</span>
              </div>
              <div class="flex items-center justify-between gap-2 bg-purple-500/10 border border-purple-500/30 rounded-lg p-1.5">
                <div class="leading-tight">
                  <div class="font-bold text-purple-300">I2S DIN (Amp)</div>
                  <div class="text-[10px] text-slate-400">Hole 8 (D9)</div>
                </div>
                <span class="px-1.5 py-0.5 rounded bg-purple-400 text-slate-950 font-bold">IO8</span>
              </div>
            </div>
          </div>

          <!-- Bottom Center Round & Oval Surface Pads (Routed via 1.55mm V4.4 Under-Board Wire Trench!) -->
          <div class="mt-4 pt-3 border-t border-dashed border-slate-700 space-y-2 font-mono text-xs">
            <div class="text-[10px] text-center text-slate-400">CENTER SURFACE PADS (Exit via V4.4 1.55 mm Under-Board Trench)</div>
            <div class="grid grid-cols-2 gap-2">
              <div class="bg-purple-500/10 border border-purple-500/40 rounded-lg p-2 text-center">
                <div class="font-bold text-purple-300">CENTER PAD: TX0</div>
                <div class="text-[10px] text-slate-400">D6 (GPIO43) &rarr; Amp LRC</div>
              </div>
              <div class="bg-purple-500/10 border border-purple-500/40 rounded-lg p-2 text-center">
                <div class="font-bold text-purple-300">CENTER PAD: RX0</div>
                <div class="text-[10px] text-slate-400">D7 (GPIO44) &rarr; Amp BCLK</div>
              </div>
              <div class="bg-slate-800 border border-slate-600 rounded-lg p-2 text-center">
                <div class="font-bold text-slate-200">BOTTOM OVAL: B-</div>
                <div class="text-[10px] text-slate-400">Direct from LiPo Black (-)</div>
              </div>
              <div class="bg-rose-500/10 border border-rose-500/40 rounded-lg p-2 text-center">
                <div class="font-bold text-rose-400">BOTTOM OVAL: B+</div>
                <div class="text-[10px] text-slate-400">From SPDT Switch Outer Pin</div>
              </div>
            </div>
          </div>
        </div>

        <!-- Legend -->
        <div class="flex flex-wrap gap-3 text-xs font-mono pt-1 justify-center">
          <span class="flex items-center gap-1.5"><span class="h-2.5 w-2.5 rounded-full bg-cyan-400"></span>Waveshare ePaper (8)</span>
          <span class="flex items-center gap-1.5"><span class="h-2.5 w-2.5 rounded-full bg-purple-400"></span>MAX98357A Audio (5)</span>
          <span class="flex items-center gap-1.5"><span class="h-2.5 w-2.5 rounded-full bg-amber-400"></span>Buttons + 220k&Omega; Sense</span>
        </div>
      </div>

      <!-- Right: Complete Master Pin Allocation Table -->
      <div class="lg:col-span-7 bg-slate-900 border border-slate-800 rounded-xl p-5 space-y-4">
        <div class="flex items-center justify-between border-b border-slate-800 pb-3">
          <div>
            <h2 class="text-base font-bold text-white">Master Wiring &amp; Bus Allocation Reference (Meshnology W11-MB-V0.2)</h2>
            <p class="text-xs text-slate-400 font-mono">Exact silkscreen labels on bottom of your board (USB-C at top, looking at bottom silkscreen)</p>
          </div>
          <a href="https://github.com/brockventures/gotchi/blob/main/docs/HARDWARE_ASSEMBLY.md" target="_blank" rel="noopener" class="text-xs font-mono text-cyan-400 hover:underline">GitHub Markdown &rarr;</a>
        </div>

        <div class="overflow-x-auto">
          <table class="w-full text-left text-xs font-mono">
            <thead class="text-slate-400 border-b border-slate-800 text-[11px] uppercase">
              <tr>
                <th class="pb-2 pr-2">W11 Silkscreen</th>
                <th class="pb-2 pr-2">Physical Location (Bottom View)</th>
                <th class="pb-2 pr-2">Sub-System</th>
                <th class="pb-2">Solder Target &amp; Notes</th>
              </tr>
            </thead>
            <tbody class="divide-y divide-slate-800/60">
              <tr>
                <td class="py-2 font-bold text-cyan-400">IO1 <span class="text-slate-500">(D0)</span></td>
                <td class="py-2 text-slate-300">Right Edge &middot; Hole 1 (Top)</td>
                <td class="py-2"><span class="px-1.5 py-0.5 rounded bg-cyan-500/10 text-cyan-300">ePaper RST</span></td>
                <td class="py-2 text-slate-200">Waveshare Pad <strong>RST</strong> <em>(ignore IO48 text below hole)</em></td>
              </tr>
              <tr>
                <td class="py-2 font-bold text-cyan-400">IO2 <span class="text-slate-500">(D1)</span></td>
                <td class="py-2 text-slate-300">Right Edge &middot; Hole 2</td>
                <td class="py-2"><span class="px-1.5 py-0.5 rounded bg-cyan-500/10 text-cyan-300">ePaper CS</span></td>
                <td class="py-2 text-slate-200">Waveshare Pad <strong>CS</strong> <em>(ignore IO47 text)</em></td>
              </tr>
              <tr>
                <td class="py-2 font-bold text-amber-400">IO3 <span class="text-slate-500">(D2)</span></td>
                <td class="py-2 text-slate-300">Right Edge &middot; Hole 3</td>
                <td class="py-2"><span class="px-1.5 py-0.5 rounded bg-amber-500/10 text-amber-300">BTN_TOP + VBUS</span></td>
                <td class="py-2 text-slate-200"><strong>1 Wire Only:</strong> Solder 1 wire to <strong>BTN_TOP Pin 1 (Signal)</strong> <em>(Resistor #1 twists onto BTN_TOP Pin 1 &amp; splices into the 5V wire!)</em></td>
              </tr>
              <tr>
                <td class="py-2 font-bold text-cyan-400">IO4 <span class="text-slate-500">(D3)</span></td>
                <td class="py-2 text-slate-300">Right Edge &middot; Hole 4</td>
                <td class="py-2"><span class="px-1.5 py-0.5 rounded bg-cyan-500/10 text-cyan-300">ePaper DC</span></td>
                <td class="py-2 text-slate-200">Waveshare Pad <strong>DC</strong> <em>(ignore IO45 text)</em></td>
              </tr>
              <tr>
                <td class="py-2 font-bold text-amber-400">IO5 <span class="text-slate-500">(D4)</span></td>
                <td class="py-2 text-slate-300">Right Edge &middot; Hole 5</td>
                <td class="py-2"><span class="px-1.5 py-0.5 rounded bg-amber-500/10 text-amber-300">BTN_BOT + VBAT</span></td>
                <td class="py-2 text-slate-200"><strong>1 Wire Only:</strong> Solder 1 wire to <strong>BTN_BOT Pin 1 (Signal)</strong> <em>(Resistors #2 &amp; #3 twist onto BTN_BOT Pin 1 before soldering!)</em></td>
              </tr>
              <tr>
                <td class="py-2 font-bold text-cyan-400">IO6 <span class="text-slate-500">(D5)</span></td>
                <td class="py-2 text-slate-300">Right Edge &middot; Hole 6</td>
                <td class="py-2"><span class="px-1.5 py-0.5 rounded bg-cyan-500/10 text-cyan-300">ePaper BUSY</span></td>
                <td class="py-2 text-slate-200">Waveshare Pad <strong>BUSY</strong> <em>(ignore IO21 text)</em></td>
              </tr>
              <tr>
                <td class="py-2 font-bold text-cyan-400">IO7 <span class="text-slate-500">(D8)</span></td>
                <td class="py-2 text-slate-300">Right Edge &middot; Hole 7</td>
                <td class="py-2"><span class="px-1.5 py-0.5 rounded bg-cyan-500/10 text-cyan-300">ePaper CLK</span></td>
                <td class="py-2 text-slate-200">Waveshare Pad <strong>CLK</strong> (SPI SCK) <em>(ignore IO18 text)</em></td>
              </tr>
              <tr>
                <td class="py-2 font-bold text-purple-400">IO8 <span class="text-slate-500">(D9)</span></td>
                <td class="py-2 text-slate-300">Right Edge &middot; Hole 8 (Bottom)</td>
                <td class="py-2"><span class="px-1.5 py-0.5 rounded bg-purple-500/10 text-purple-300">I2S DIN</span></td>
                <td class="py-2 text-slate-200">MAX98357A <strong>DIN</strong> (Audio PCM Data Out &mdash; 1 wire only)</td>
              </tr>
              <tr>
                <td class="py-2 font-bold text-rose-400">5V</td>
                <td class="py-2 text-slate-300">Left Edge &middot; Hole 1 (Top)</td>
                <td class="py-2"><span class="px-1.5 py-0.5 rounded bg-rose-500/10 text-rose-300">Amp Power</span></td>
                <td class="py-2 text-slate-200"><strong>1 Wire Only:</strong> Solder 1 wire directly to <strong>MAX98357A Vin</strong> (1 wire in ESP32, 1 wire in Amp!)</td>
              </tr>
              <tr>
                <td class="py-2 font-bold text-slate-200">GND (3 Pads!)</td>
                <td class="py-2 text-slate-300">Hole 2 + Top <code class="text-cyan-300">GND</code> + Center <code class="text-cyan-300">G</code></td>
                <td class="py-2"><span class="px-1.5 py-0.5 rounded bg-slate-800 text-slate-300">3 Separate Pads</span></td>
                <td class="py-2 text-slate-200"><strong>1 Wire Per Pad:</strong> Hole 2 &rarr; <strong>Waveshare GND</strong>; Top <code class="text-cyan-300">GND</code> Pad &rarr; <strong>Amp GND</strong>; Center <code class="text-cyan-300">G</code> Pad &rarr; <strong>Buttons GND</strong></td>
              </tr>
              <tr>
                <td class="py-2 font-bold text-orange-400">3.3V</td>
                <td class="py-2 text-slate-300">Left Edge &middot; Hole 3</td>
                <td class="py-2"><span class="px-1.5 py-0.5 rounded bg-orange-500/10 text-orange-300">Logic Power</span></td>
                <td class="py-2 text-slate-200">Waveshare Pad <strong>VCC</strong></td>
              </tr>
              <tr class="bg-rose-950/30">
                <td class="py-2 font-bold text-rose-400">IO13&ndash;IO10</td>
                <td class="py-2 text-rose-300">Left Edge &middot; Holes 4&ndash;7</td>
                <td class="py-2"><span class="px-1.5 py-0.5 rounded bg-rose-500/20 text-rose-300">DO NOT USE</span></td>
                <td class="py-2 text-rose-200"><strong>Skip Holes 4&ndash;7 on Left Edge!</strong> Connected to Camera DVP parallel bus (PCLK/Y7/Y8/XCLK).</td>
              </tr>
              <tr>
                <td class="py-2 font-bold text-cyan-400">IO9 <span class="text-slate-500">(D10)</span></td>
                <td class="py-2 text-slate-300">Left Edge &middot; Hole 8 (Bottom)</td>
                <td class="py-2"><span class="px-1.5 py-0.5 rounded bg-cyan-500/10 text-cyan-300">ePaper DIN</span></td>
                <td class="py-2 text-slate-200">Waveshare Pad <strong>DIN</strong> (SPI MOSI &mdash; bottom-left corner hole)</td>
              </tr>
              <tr>
                <td class="py-2 font-bold text-purple-400">TX0 <span class="text-slate-500">(D6/43)</span></td>
                <td class="py-2 text-purple-200">Center Round Pad (Row 4 Left)</td>
                <td class="py-2"><span class="px-1.5 py-0.5 rounded bg-purple-500/10 text-purple-300">I2S LRCLK</span></td>
                <td class="py-2 text-slate-200">MAX98357A <strong>LRC</strong> (exits via V4.4 1.55 mm under-board wire channel)</td>
              </tr>
              <tr>
                <td class="py-2 font-bold text-purple-400">RX0 <span class="text-slate-500">(D7/44)</span></td>
                <td class="py-2 text-purple-200">Center Round Pad (Row 4 Right)</td>
                <td class="py-2"><span class="px-1.5 py-0.5 rounded bg-purple-500/10 text-purple-300">I2S BCLK</span></td>
                <td class="py-2 text-slate-200">MAX98357A <strong>BCLK</strong> (Silent at boot &mdash; exits via under-board channel)</td>
              </tr>
              <tr>
                <td class="py-2 font-bold text-rose-400">B- / B+</td>
                <td class="py-2 text-rose-200">Bottom Center Oval Pads</td>
                <td class="py-2"><span class="px-1.5 py-0.5 rounded bg-rose-500/10 text-rose-300">LiPo Power</span></td>
                <td class="py-2 text-slate-200"><strong>B-</strong> (Left Oval) &rarr; LiPo Black (-) &middot; <strong>B+</strong> (Right Oval) &rarr; Switched BAT+</td>
              </tr>
            </tbody>
          </table>
        </div>
      </div>
    </section>

    <!-- SECTION 2: THE 3x 220 kOhm RESISTOR NETWORK EXPLAINED VISUALLY -->
    <section id="resistor-network" class="bg-slate-900 border border-slate-800 rounded-xl p-5 space-y-4">
      <div class="border-b border-slate-800 pb-3 flex flex-col sm:flex-row sm:items-center justify-between gap-2">
        <div>
          <h2 class="text-base font-bold text-white">The 3x 220 k&Omega; Resistor Multiplexing Network (IO3 / D2 &amp; IO5 / D4)</h2>
          <p class="text-xs text-slate-400 font-mono">All 3 resistors are the exact same value: <strong>220 k&Omega; (1/4W 1%)</strong>. Sleeve exposed leads in heat-shrink tubing and tuck into the 9 mm right-side button trench.</p>
        </div>
        <span class="text-xs font-mono px-2.5 py-1 rounded bg-amber-500/10 text-amber-300 border border-amber-500/30">Zero Extra GPIOs Needed</span>
      </div>

      <div class="grid grid-cols-1 md:grid-cols-2 gap-5">
        <!-- Card A: IO3 (D2) Wall-Brick Sense (1x 220k) -->
        <div class="bg-slate-950 border border-slate-800 rounded-xl p-4 space-y-3">
          <div class="flex items-center justify-between">
            <span class="text-sm font-bold text-amber-300 font-mono">2-Leg BTN_TOP + Resistor #1 (Splice Into 5V Wire!)</span>
            <span class="text-xs font-mono px-2 py-0.5 rounded bg-amber-500/10 text-amber-400">Pre-Twist &amp; Solder Once</span>
          </div>
          <pre class="bg-slate-900/90 border border-slate-800 rounded-lg p-3 text-[11px] font-mono text-slate-200 overflow-x-auto leading-relaxed">
Existing [5V &harr; Amp Vin Wire] &mdash;(mid-wire splice)&rarr; [ 220 k&Omega; #1 ]
                                                        &#9474;
W11 [IO3] (Hole 3) &mdash;(1 wire)&rarr; [BTN_TOP Pin 1 (Signal)] &mdash;[Button]&rarr; [BTN_TOP Pin 2 (GND)]</pre>
          <p class="text-xs text-slate-400 leading-relaxed">
            <strong>How to wire <code class="text-slate-200">BTN_TOP</code> (2 legs):</strong> Twist your <code class="text-slate-200">IO3</code> wire and one leg of <code class="text-slate-200">220 k&Omega; #1</code> together around <code class="text-slate-200">BTN_TOP Pin 1</code> and solder once. Splice the other leg of <code class="text-slate-200">220 k&Omega; #1</code> directly into the middle of your existing <code class="text-slate-200">5V</code> amp wire so you never re-heat the ESP32 or Amp pads!
          </p>
        </div>

        <!-- Card B: IO5 (D4) LiPo Fuel Gauge Divider (2x 220k) -->
        <div class="bg-slate-950 border border-slate-800 rounded-xl p-4 space-y-3">
          <div class="flex items-center justify-between">
            <span class="text-sm font-bold text-emerald-300 font-mono">2-Leg BTN_BOT + 3-Leg SPDT Switch + Resistors #2 &amp; #3</span>
            <span class="text-xs font-mono px-2 py-0.5 rounded bg-emerald-500/10 text-emerald-400">Pre-Twist on Bench</span>
          </div>
          <pre class="bg-slate-900/90 border border-slate-800 rounded-lg p-3 text-[11px] font-mono text-slate-200 overflow-x-auto leading-relaxed">
[SPDT Pin 1 (Outer)] &mdash;(1 wire)&rarr; W11 [B+ Oval Pad]   (SPDT Pin 2 Middle = Battery Red +, Pin 3 = Empty)
         &#9474;
   [ 220 k&Omega; #2 ]
         &#9474;
W11 [IO5] (Hole 5) &mdash;(1 wire)&rarr; [BTN_BOT Pin 1 (Signal)] &mdash;[ 220 k&Omega; #3 ]&rarr; [BTN_BOT Pin 2 (GND)]</pre>
          <p class="text-xs text-slate-400 leading-relaxed">
            <strong>How to wire <code class="text-slate-200">BTN_BOT</code> (2 legs):</strong> Wrap <code class="text-slate-200">220 k&Omega; #3</code> across <code class="text-slate-200">BTN_BOT Pin 1</code> and <code class="text-slate-200">Pin 2</code>. Before soldering <code class="text-slate-200">Pin 1</code>, also twist on your <code class="text-slate-200">IO5</code> wire and one leg of <code class="text-slate-200">220 k&Omega; #2</code>, then solder <code class="text-slate-200">Pin 1</code> in a single pass!
          </p>
        </div>
      </div>

      <!-- Card C: Full GND Daisy-Chain Bar -->
      <div class="bg-slate-950 border border-cyan-500/30 rounded-xl p-4 space-y-2">
        <div class="flex items-center justify-between flex-wrap gap-2">
          <span class="text-sm font-bold text-cyan-300 font-mono">Button GND Chain (2-Leg Buttons: Pre-Twist Jumper + GND Feed on BTN_BOT Pin 2)</span>
          <span class="text-xs font-mono px-2 py-0.5 rounded bg-cyan-500/10 text-cyan-300">Solder Each Pin Once</span>
        </div>
        <pre class="bg-slate-900/90 border border-slate-800 rounded-lg p-3 text-xs font-mono text-cyan-200 overflow-x-auto">[Existing GND Wire Splice OR ESP32 Square G Pad] &mdash;&rarr; [BTN_BOT Pin 2 (GND)] &mdash;(15 mm jumper)&rarr; [BTN_TOP Pin 2 (GND)]</pre>
      </div>
    </section>

    <!-- SECTION 3: INTERACTIVE STEP-BY-STEP SOLDERING CHECKLIST -->
    <section id="solder-steps" class="space-y-4">
      <div class="flex items-center justify-between flex-wrap gap-2">
        <div>
          <h2 class="text-lg font-bold text-white">In-Case Order of Operations Checklist (ESP32 First &rarr; Seat in Case &rarr; Display, Amp &amp; Speaker &rarr; Battery &rarr; Buttons Last)</h2>
          <p class="text-xs text-slate-400 font-mono">Pre-wire all leads onto the ESP32 on the bench first, drop the ESP32 into the case, then wire outward in place so the shell closes cleanly at the end.</p>
        </div>
        <span class="text-xs font-mono px-2.5 py-1 rounded bg-emerald-500/10 text-emerald-300 border border-emerald-500/30">Order: ESP32 &rarr; Case &rarr; Display/Amp/Spkr &rarr; Battery &rarr; Buttons</span>
      </div>

      <div class="grid grid-cols-1 md:grid-cols-2 gap-5">

        <!-- Stage 1: Pre-Wire ESP32 on Bench & Seat in Case First, Then Land Display -->
        <div class="bg-slate-900 border border-slate-800 rounded-xl p-5 space-y-3">
          <div class="flex items-center justify-between border-b border-slate-800 pb-2.5">
            <h3 class="text-sm font-bold text-cyan-400 font-mono">STAGE 1 // Wire ESP32 First &rarr; Seat in Case &rarr; Land Display</h3>
            <span class="text-[11px] font-mono text-emerald-400">Step 1 &amp; 2 in Case</span>
          </div>
          <div class="space-y-1.5 text-xs">
            <label class="check-row flex items-start gap-3 p-2 rounded-lg cursor-pointer transition">
              <input type="checkbox" data-step="s1_0" class="mt-0.5 h-4 w-4 rounded border-slate-700 bg-slate-950 text-cyan-500 focus:ring-0">
              <div><strong>1A. Pre-Wire All ESP32 Pads &amp; Holes First (Bench):</strong> Solder all bottom-side wires (<code class="font-mono">RX0, TX0, GND, B-, B+</code>), cover bottom pads with Kapton tape, and solder all 12 perimeter hole wires (<code class="font-mono">5V, GND, 3.3V, IO9</code> on left; <code class="font-mono">IO1&ndash;IO8</code> on right) onto the ESP32 while it is free on the bench.</div>
            </label>
            <label class="check-row flex items-start gap-3 p-2 rounded-lg cursor-pointer transition">
              <input type="checkbox" data-step="s1_1" class="mt-0.5 h-4 w-4 rounded border-slate-700 bg-slate-950 text-cyan-500 focus:ring-0">
              <div><strong>1B. Seat ESP32 in Case First (<code class="font-mono">gotchi_back</code>):</strong> Route the bottom wires down the <strong>1.55 mm under-board floor trench</strong>, seat the XIAO ESP32S3 + Sense into the top guide rails (USB-C in top port), stick the U.FL antenna to the side wall, and fan the wires out to their target bays.</div>
            </label>
            <label class="check-row flex items-start gap-3 p-2 rounded-lg cursor-pointer transition">
              <input type="checkbox" data-step="s1_2" class="mt-0.5 h-4 w-4 rounded border-slate-700 bg-slate-950 text-cyan-500 focus:ring-0">
              <div><code class="text-cyan-300 font-mono">Waveshare VCC</code> &larr; Land pre-wired W11 <code class="text-orange-300 font-mono">3.3V</code> wire <em>(Left Edge Hole 3)</em></div>
            </label>
            <label class="check-row flex items-start gap-3 p-2 rounded-lg cursor-pointer transition">
              <input type="checkbox" data-step="s1_3" class="mt-0.5 h-4 w-4 rounded border-slate-700 bg-slate-950 text-cyan-500 focus:ring-0">
              <div><code class="text-cyan-300 font-mono">Waveshare GND</code> &larr; Land pre-wired W11 <code class="text-slate-200 font-mono">GND</code> wire <em>(Left Edge Hole 2 &mdash; Hop 1 of GND chain)</em></div>
            </label>
            <label class="check-row flex items-start gap-3 p-2 rounded-lg cursor-pointer transition">
              <input type="checkbox" data-step="s1_4" class="mt-0.5 h-4 w-4 rounded border-slate-700 bg-slate-950 text-cyan-500 focus:ring-0">
              <div><code class="text-cyan-300 font-mono">Waveshare DIN (MOSI)</code> &larr; Land pre-wired W11 <code class="text-cyan-300 font-mono">IO9 (D10)</code> wire <em>(Left Edge Hole 8 &mdash; Bottom-Left Corner!)</em></div>
            </label>
            <label class="check-row flex items-start gap-3 p-2 rounded-lg cursor-pointer transition">
              <input type="checkbox" data-step="s1_5" class="mt-0.5 h-4 w-4 rounded border-slate-700 bg-slate-950 text-cyan-500 focus:ring-0">
              <div><code class="text-cyan-300 font-mono">Waveshare CLK (SCK)</code> &larr; Land pre-wired W11 <code class="text-cyan-300 font-mono">IO7 (D8)</code> wire <em>(Right Edge Hole 7)</em></div>
            </label>
            <label class="check-row flex items-start gap-3 p-2 rounded-lg cursor-pointer transition">
              <input type="checkbox" data-step="s1_6" class="mt-0.5 h-4 w-4 rounded border-slate-700 bg-slate-950 text-cyan-500 focus:ring-0">
              <div><code class="text-cyan-300 font-mono">Waveshare CS</code> &larr; Land pre-wired W11 <code class="text-cyan-300 font-mono">IO2 (D1)</code> wire <em>(Right Edge Hole 2)</em></div>
            </label>
            <label class="check-row flex items-start gap-3 p-2 rounded-lg cursor-pointer transition">
              <input type="checkbox" data-step="s1_7" class="mt-0.5 h-4 w-4 rounded border-slate-700 bg-slate-950 text-cyan-500 focus:ring-0">
              <div><code class="text-cyan-300 font-mono">Waveshare DC</code> &larr; Land pre-wired W11 <code class="text-cyan-300 font-mono">IO4 (D3)</code> wire <em>(Right Edge Hole 4)</em></div>
            </label>
            <label class="check-row flex items-start gap-3 p-2 rounded-lg cursor-pointer transition">
              <input type="checkbox" data-step="s1_8" class="mt-0.5 h-4 w-4 rounded border-slate-700 bg-slate-950 text-cyan-500 focus:ring-0">
              <div><code class="text-cyan-300 font-mono">Waveshare RST</code> &larr; Land pre-wired W11 <code class="text-cyan-300 font-mono">IO1 (D0)</code> wire <em>(Right Edge Hole 1 &mdash; Top-Right!)</em></div>
            </label>
            <label class="check-row flex items-start gap-3 p-2 rounded-lg cursor-pointer transition">
              <input type="checkbox" data-step="s1_9" class="mt-0.5 h-4 w-4 rounded border-slate-700 bg-slate-950 text-cyan-500 focus:ring-0">
              <div><code class="text-cyan-300 font-mono">Waveshare BUSY</code> &larr; Land pre-wired W11 <code class="text-cyan-300 font-mono">IO6 (D5)</code> wire <em>(Right Edge Hole 6)</em></div>
            </label>
          </div>
        </div>

        <!-- Stage 2: MAX98357A Audio Amp & Twisted Speaker -->
        <div class="bg-slate-900 border border-slate-800 rounded-xl p-5 space-y-3">
          <div class="flex items-center justify-between border-b border-slate-800 pb-2.5">
            <h3 class="text-sm font-bold text-purple-400 font-mono">STAGE 2 // Wire MAX98357A Amp (+3 dB Bridge) &amp; Speaker in Case</h3>
            <span class="text-[11px] font-mono text-purple-300">+12 dB Gain (GAIN &rarr; GND)</span>
          </div>
          <div class="space-y-1.5 text-xs">
            <label class="check-row flex items-start gap-3 p-2 rounded-lg cursor-pointer transition">
              <input type="checkbox" data-step="s2_1" class="mt-0.5 h-4 w-4 rounded border-slate-700 bg-slate-950 text-purple-500 focus:ring-0">
              <div><strong>Speaker Leads (Seat Speaker in Front Chin Grille First):</strong> Seat the <code class="font-mono">1510</code> speaker face-down in the chin cradle, route the 40&ndash;45 mm twisted pair through the side slot, and solder <strong>Black &rarr; Amp Left Top Hole (<code class="font-mono">-</code>)</strong> and <strong>Red &rarr; Amp Right Top Hole (<code class="font-mono">+</code>)</strong>.</div>
            </label>
            <label class="check-row flex items-start gap-3 p-2 rounded-lg cursor-pointer transition">
              <input type="checkbox" data-step="s2_2" class="mt-0.5 h-4 w-4 rounded border-slate-700 bg-slate-950 text-purple-500 focus:ring-0">
              <div><code class="text-purple-300 font-mono">MAX98357A Vin (Pin 7, Far Right)</code> &larr; Land the pre-wired W11 <code class="text-rose-300 font-mono">5V</code> wire <em>(from Left Edge Hole 1)</em>.</div>
            </label>
            <label class="check-row flex items-start gap-3 p-2 rounded-lg cursor-pointer transition">
              <input type="checkbox" data-step="s2_3" class="mt-0.5 h-4 w-4 rounded border-slate-700 bg-slate-950 text-purple-500 focus:ring-0">
              <div><code class="text-purple-300 font-mono">MAX98357A GND (Pin 6)</code> &larr; Land the pre-wired W11 back-side <code class="text-slate-200 font-mono">GND</code> wire (from round pad next to <code class="font-mono">EN</code>).</div>
            </label>
            <label class="check-row flex items-start gap-3 p-2 rounded-lg cursor-pointer transition">
              <input type="checkbox" data-step="s2_7" class="mt-0.5 h-4 w-4 rounded border-slate-700 bg-slate-950 text-purple-500 focus:ring-0">
              <div><code class="text-emerald-300 font-mono">MAX98357A GAIN (Pin 4) &rarr; Bridge to GND (Pin 6) (+3 dB Boost / +12 dB Total):</code> Bridge a short wire tail or jumper from <code class="font-mono">GAIN (Pin 4)</code> directly to <code class="font-mono">GND (Pin 6)</code> (leaving <code class="font-mono">SD Pin 5</code> in between completely empty/floating). Seat the amp in the lower rear bay!</div>
            </label>
            <label class="check-row flex items-start gap-3 p-2 rounded-lg cursor-pointer transition">
              <input type="checkbox" data-step="s2_4" class="mt-0.5 h-4 w-4 rounded border-slate-700 bg-slate-950 text-purple-500 focus:ring-0">
              <div><code class="text-purple-300 font-mono">MAX98357A BCLK (Pin 2)</code> &larr; Land the pre-wired W11 <code class="text-purple-300 font-mono">RX0 (D7 / GPIO44)</code> wire.</div>
            </label>
            <label class="check-row flex items-start gap-3 p-2 rounded-lg cursor-pointer transition">
              <input type="checkbox" data-step="s2_5" class="mt-0.5 h-4 w-4 rounded border-slate-700 bg-slate-950 text-purple-500 focus:ring-0">
              <div><code class="text-purple-300 font-mono">MAX98357A LRC (Pin 1, Far Left)</code> &larr; Land the pre-wired W11 <code class="text-purple-300 font-mono">TX0 (D6 / GPIO43)</code> wire.</div>
            </label>
            <label class="check-row flex items-start gap-3 p-2 rounded-lg cursor-pointer transition">
              <input type="checkbox" data-step="s2_6" class="mt-0.5 h-4 w-4 rounded border-slate-700 bg-slate-950 text-purple-500 focus:ring-0">
              <div><code class="text-purple-300 font-mono">MAX98357A DIN (Pin 3)</code> &larr; Land the pre-wired W11 <code class="text-purple-300 font-mono">IO8 (D9)</code> wire <em>(from Right Edge Hole 8)</em>.</div>
            </label>
          </div>
        </div>

        <!-- Stage 3: LiPo Battery & 3-Leg SPDT Slide Switch (Before Buttons!) -->
        <div class="bg-slate-900 border border-slate-800 rounded-xl p-5 space-y-3">
          <div class="flex items-center justify-between border-b border-slate-800 pb-2.5">
            <h3 class="text-sm font-bold text-rose-400 font-mono">STAGE 3 // Wire 803040 LiPo Battery &amp; 3-Leg SPDT Switch in Place</h3>
            <span class="text-[11px] font-mono text-rose-300">USB-C Unplugged &mdash; Before Buttons</span>
          </div>
          <div class="space-y-1.5 text-xs">
            <label class="check-row flex items-start gap-3 p-2 rounded-lg cursor-pointer transition">
              <input type="checkbox" data-step="s4_1" class="mt-0.5 h-4 w-4 rounded border-slate-700 bg-slate-950 text-rose-500 focus:ring-0">
              <div><strong>Switch Leg 1 (Outer Leg &mdash; <code class="font-mono">Switched B+</code>):</strong> Twist the pre-routed W11 <code class="text-rose-300 font-mono">B+</code> red wire together with one leg of <strong>220 k&Omega; Resistor #2</strong> (whose other leg goes to <code class="font-mono">BTN_BOT Pin 1</code> in Stage 4) around <strong>Switch Outer Leg 1</strong> and solder in one pass.</div>
            </label>
            <label class="check-row flex items-start gap-3 p-2 rounded-lg cursor-pointer transition">
              <input type="checkbox" data-step="s4_2" class="mt-0.5 h-4 w-4 rounded border-slate-700 bg-slate-950 text-rose-500 focus:ring-0">
              <div><strong>Switch Leg 2 (Middle Leg &mdash; <code class="font-mono">Battery Red +</code>):</strong> Cut the JST plug off the LiPo <strong>one wire at a time</strong> (Red first, then Black). Solder <strong>Battery Red (<code class="font-mono">+</code>)</strong> to the <strong>Middle Leg (Leg 2)</strong> of the 3-leg slide switch.</div>
            </label>
            <label class="check-row flex items-start gap-3 p-2 rounded-lg cursor-pointer transition">
              <input type="checkbox" data-step="s4_3" class="mt-0.5 h-4 w-4 rounded border-slate-700 bg-slate-950 text-rose-500 focus:ring-0">
              <div><strong>Switch Leg 3 (Other Outer Leg &mdash; <code class="font-mono">OFF Position</code>):</strong> Leave <strong>completely empty / unconnected</strong>, and snap the SPDT switch into its upper-left cradle!</div>
            </label>
            <label class="check-row flex items-start gap-3 p-2 rounded-lg cursor-pointer transition">
              <input type="checkbox" data-step="s4_4" class="mt-0.5 h-4 w-4 rounded border-slate-700 bg-slate-950 text-rose-500 focus:ring-0">
              <div><strong>Battery Black (<code class="font-mono">-</code>) &amp; Seat LiPo:</strong> Connect to the pre-routed W11 <code class="text-slate-200 font-mono">B-</code> wire, insulate with Kapton/heat-shrink, and seat the <code class="font-mono">803040</code> LiPo flat in the rear bay over the amp.</div>
            </label>
          </div>
        </div>

        <!-- Stage 4: Buttons & 3x 220k Resistors LAST (Wire in Place & Close Case!) -->
        <div class="bg-slate-900 border border-slate-800 rounded-xl p-5 space-y-3">
          <div class="flex items-center justify-between border-b border-slate-800 pb-2.5">
            <h3 class="text-sm font-bold text-amber-400 font-mono">STAGE 4 // Wire 2-Leg Buttons &amp; 3x 220 k&Omega; Resistors LAST, Then Close Case</h3>
            <span class="text-[11px] font-mono text-amber-300">Final Step Before Closing</span>
          </div>
          <div class="space-y-1.5 text-xs">
            <label class="check-row flex items-start gap-3 p-2 rounded-lg cursor-pointer transition">
              <input type="checkbox" data-step="s3_0" class="mt-0.5 h-4 w-4 rounded border-slate-700 bg-slate-950 text-amber-500 focus:ring-0">
              <div><strong>Seat Button Caps &amp; Switches First:</strong> Insert the 2 printed button caps from inside the right wall of <code class="font-mono">gotchi_back</code> and slide <code class="font-mono">BTN_TOP</code> and <code class="font-mono">BTN_BOT</code> into their 6.80 mm right-side cradles so you can wire them right at the trench edge.</div>
            </label>
            <label class="check-row flex items-start gap-3 p-2 rounded-lg cursor-pointer transition">
              <input type="checkbox" data-step="s3_1" class="mt-0.5 h-4 w-4 rounded border-slate-700 bg-slate-950 text-amber-500 focus:ring-0">
              <div><strong>Step 4A &mdash; <code class="font-mono">BTN_TOP Pin 1 (Signal)</code>:</strong> Twist the pre-routed W11 <code class="text-amber-300 font-mono">IO3</code> wire and one leg of <strong>220 k&Omega; Resistor #1</strong> together around <code class="font-mono">BTN_TOP Pin 1</code> and solder in one pass.</div>
            </label>
            <label class="check-row flex items-start gap-3 p-2 rounded-lg cursor-pointer transition">
              <input type="checkbox" data-step="s3_2" class="mt-0.5 h-4 w-4 rounded border-slate-700 bg-slate-950 text-amber-500 focus:ring-0">
              <div><strong>Step 4B &mdash; Splice Resistor #1 into Existing <code class="font-mono">5V</code> Wire:</strong> Strip a tiny 2&ndash;3 mm window in the insulation of your <code class="font-mono">5V</code> wire (running between ESP32 <code class="font-mono">5V</code> and <code class="font-mono">MAX98357A Vin</code>), wrap the free leg of <strong>Resistor #1</strong> around the bare strands, solder it, and insulate with Kapton tape.</div>
            </label>
            <label class="check-row flex items-start gap-3 p-2 rounded-lg cursor-pointer transition">
              <input type="checkbox" data-step="s3_3" class="mt-0.5 h-4 w-4 rounded border-slate-700 bg-slate-950 text-amber-500 focus:ring-0">
              <div><strong>Step 4C &mdash; <code class="font-mono">BTN_TOP Pin 2 (GND)</code>:</strong> Solder <strong>1 short 15 mm jumper wire</strong> to <code class="font-mono">BTN_TOP Pin 2</code> (leading down to <code class="font-mono">BTN_BOT Pin 2</code>).</div>
            </label>
            <label class="check-row flex items-start gap-3 p-2 rounded-lg cursor-pointer transition">
              <input type="checkbox" data-step="s3_4" class="mt-0.5 h-4 w-4 rounded border-slate-700 bg-slate-950 text-amber-500 focus:ring-0">
              <div><strong>Step 4D &mdash; <code class="font-mono">BTN_BOT Pin 1 (Signal)</code>:</strong> Wrap the pre-routed W11 <code class="text-amber-300 font-mono">IO5</code> wire + the free leg of <strong>220 k&Omega; Resistor #2</strong> (from Switch Leg 1) + one leg of <strong>220 k&Omega; Resistor #3</strong> around <code class="font-mono">BTN_BOT Pin 1</code> and solder all three in one pass!</div>
            </label>
            <label class="check-row flex items-start gap-3 p-2 rounded-lg cursor-pointer transition">
              <input type="checkbox" data-step="s3_5" class="mt-0.5 h-4 w-4 rounded border-slate-700 bg-slate-950 text-amber-500 focus:ring-0">
              <div><strong>Step 4E &mdash; <code class="font-mono">BTN_BOT Pin 2 (GND)</code> &amp; Close Case:</strong> Wrap the other leg of <strong>Resistor #3</strong> + the <strong>15 mm jumper from <code class="font-mono">BTN_TOP Pin 2</code></strong> + <strong>1 <code class="font-mono">GND</code> splice wire</strong> around <code class="font-mono">BTN_BOT Pin 2</code> and solder in one pass. Tuck the resistors into the 9 mm right-side trench and snap the case closed!</div>
            </label>
          </div>
        </div>

      </div>
    </section>

    <!-- SECTION 4: V4.4 MECHANICAL SEATING, WIRE TRENCHES & CAD DIAGRAMS -->
    <section id="mechanical-seating" class="bg-slate-900 border border-slate-800 rounded-xl p-5 space-y-5">
      <div class="border-b border-slate-800 pb-3 flex flex-col sm:flex-row sm:items-center justify-between gap-2">
        <div>
          <h2 class="text-base font-bold text-white">V4.4 Clamshell Seating, V3 PCB-Clip Springs &amp; Under-Board Wire Channel</h2>
          <p class="text-xs text-slate-400 font-mono">Click any diagram to open full-resolution 1920&times;1180 render in a new tab</p>
        </div>
        <span class="text-xs font-mono px-2.5 py-1 rounded bg-purple-500/10 text-purple-300 border border-purple-500/30">V4.4 Back + V3 PCB-Edge Clips</span>
      </div>

      <!-- 3-Column Visual Render Gallery -->
      <div class="grid grid-cols-1 lg:grid-cols-3 gap-4">
        <a href="/gotchi/assets/v4_4_back_and_v3_pcb_springs_preview.png" target="_blank" class="group bg-slate-950 border border-slate-800 hover:border-cyan-500/50 rounded-xl overflow-hidden flex flex-col transition">
          <img src="/gotchi/assets/v4_4_back_and_v3_pcb_springs_preview.png" alt="Gotchi V4.4 Back Shell and V3 PCB-Clip Springs" class="w-full h-56 object-contain bg-slate-950 p-2 group-hover:scale-[1.02] transition">
          <div class="p-3 border-t border-slate-800 text-xs font-mono">
            <div class="font-bold text-cyan-400">1. V4.4 Back Shell + V3 PCB-Clip Springs</div>
            <div class="text-slate-400 text-[11px]">1.55 mm under-board wire channel, 6.80 mm button cradles, flex-snap switch clips &amp; V3 Sense-PCB clips.</div>
          </div>
        </a>

        <a href="/gotchi/assets/v4_3_camera_yz_cross_section.png" target="_blank" class="group bg-slate-950 border border-slate-800 hover:border-cyan-500/50 rounded-xl overflow-hidden flex flex-col transition">
          <img src="/gotchi/assets/v4_3_camera_yz_cross_section.png" alt="Gotchi Camera Y-Z Cross Section" class="w-full h-56 object-contain bg-slate-950 p-2 group-hover:scale-[1.02] transition">
          <div class="p-3 border-t border-slate-800 text-xs font-mono">
            <div class="font-bold text-emerald-400">2. Camera Y-Z &amp; X-Z Cross-Section</div>
            <div class="text-slate-400 text-[11px]">Shows 3.80 mm air gap between camera backing and XIAO Sense header, bridged by V3 PCB-Clip Spring.</div>
          </div>
        </a>

        <a href="/gotchi/assets/v4_3_build_preview.png" target="_blank" class="group bg-slate-950 border border-slate-800 hover:border-cyan-500/50 rounded-xl overflow-hidden flex flex-col transition">
          <img src="/gotchi/assets/v4_3_build_preview.png" alt="Gotchi Open Clamshell CAD Preview" class="w-full h-56 object-contain bg-slate-950 p-2 group-hover:scale-[1.02] transition">
          <div class="p-3 border-t border-slate-800 text-xs font-mono">
            <div class="font-bold text-amber-400">3. Open Clamshell Reference</div>
            <div class="text-slate-400 text-[11px]">9 mm side wire trenches, 4 corner display pins, and top bezel camera cradle alignment.</div>
          </div>
        </a>
      </div>

      <!-- Mechanical Seating Checklist -->
      <div class="grid grid-cols-1 md:grid-cols-2 gap-4 text-xs pt-2">
        <div class="bg-slate-950 border border-slate-800 rounded-xl p-4 space-y-2">
          <div class="font-bold text-cyan-400 font-mono mb-1">FRONT SHELL (`gotchi_front`) SEATING:</div>
          <label class="check-row flex items-start gap-3 p-1.5 rounded cursor-pointer">
            <input type="checkbox" data-step="m_1" class="mt-0.5 h-4 w-4 rounded border-slate-700 bg-slate-900 text-cyan-500">
            <div><strong>Camera + V3 Sense PCB Clip:</strong> Clip the <strong>V3 PCB-Edge-Clip Spring</strong> (<code class="font-mono">#1A</code> or <code class="font-mono">#2A</code>) onto the 17.8 mm left/right edges of the XIAO Sense board between <code class="font-mono">L2</code> and <code class="font-mono">D1</code>, then rest the 8.5&times;8.5 mm camera onto the spring saddle so it centers into the front bezel cradle when closing.</div>
          </label>
          <label class="check-row flex items-start gap-3 p-1.5 rounded cursor-pointer">
            <input type="checkbox" data-step="m_2" class="mt-0.5 h-4 w-4 rounded border-slate-700 bg-slate-900 text-cyan-500">
            <div><strong>Waveshare 1.54&quot; Display:</strong> Drop the PCB onto the 4 tapered corner locator pins (<code class="font-mono">x=&plusmn;14, y=-23 &amp; +20</code>) with the 8 top wires dressed upward.</div>
          </label>
          <label class="check-row flex items-start gap-3 p-1.5 rounded cursor-pointer">
            <input type="checkbox" data-step="m_3" class="mt-0.5 h-4 w-4 rounded border-slate-700 bg-slate-900 text-cyan-500">
            <div><strong>1510 Cavity Speaker:</strong> Seat speaker face-down over the 5-slot chin grille and exit the twisted-pair leads out the side slot.</div>
          </label>
        </div>

        <div class="bg-slate-950 border border-slate-800 rounded-xl p-4 space-y-2">
          <div class="font-bold text-purple-400 font-mono mb-1">BACK SHELL (`gotchi_back` V4.4) SEATING:</div>
          <label class="check-row flex items-start gap-3 p-1.5 rounded cursor-pointer">
            <input type="checkbox" data-step="m_4" class="mt-0.5 h-4 w-4 rounded border-slate-700 bg-slate-900 text-purple-500">
            <div><strong>Buttons, Caps &amp; Switch:</strong> Insert the 2 printed button caps from inside the right wall, slide the two 6&times;6&times;5 mm switches into the widened 6.80 mm cradles, snap the SPDT power switch into the cantilevered flex-arms, and tuck the 3x 220 k&Omega; resistors into the 9 mm side trench.</div>
          </label>
          <label class="check-row flex items-start gap-3 p-1.5 rounded cursor-pointer">
            <input type="checkbox" data-step="m_5" class="mt-0.5 h-4 w-4 rounded border-slate-700 bg-slate-900 text-purple-500">
            <div><strong>MAX98357A + 803040 LiPo:</strong> Seat the amp in the lower rear bay underneath the LiPo battery (below the <code class="font-mono">y=+4.95 mm</code> XIAO stop bumpers). Stick the U.FL antenna to the plastic perimeter wall (&ge;5 mm away from LiPo foil).</div>
          </label>
          <label class="check-row flex items-start gap-3 p-1.5 rounded cursor-pointer">
            <input type="checkbox" data-step="m_6" class="mt-0.5 h-4 w-4 rounded border-slate-700 bg-slate-900 text-purple-500">
            <div><strong>W11-MB-V0.2 + Sense Board:</strong> Route the bottom-soldered <code class="font-mono">TX0, RX0, B-, B+</code> wires down the 1.55 mm center floor trench, seat the PCB edges onto the outer rails with USB-C aligned in the top cutout, and snap the clamshell closed.</div>
          </label>
        </div>
      </div>
    </section>

    <!-- SECTION 5: SMOKE TEST & FIRST POWER CHECKLIST -->
    <section id="smoke-test" class="bg-slate-900 border border-slate-800 rounded-xl p-5 space-y-3">
      <div class="flex items-center justify-between border-b border-slate-800 pb-2.5">
        <h2 class="text-base font-bold text-emerald-400 font-mono">STAGE 5 // First-Power Multimeter &amp; Smoke-Test</h2>
        <span class="text-xs font-mono text-slate-400">Before Closing Clamshell</span>
      </div>
      <div class="grid grid-cols-1 md:grid-cols-2 gap-3 text-xs">
        <label class="check-row flex items-start gap-3 p-2.5 bg-slate-950 border border-slate-800 rounded-lg cursor-pointer">
          <input type="checkbox" data-step="t_1" class="mt-0.5 h-4 w-4 rounded border-slate-700 bg-slate-900 text-emerald-500">
          <div><strong>1. Multimeter Rail Check (Switch OFF):</strong> Verify there is no continuity short between <code class="font-mono">3V3 &harr; GND</code> or <code class="font-mono">5V &harr; GND</code>.</div>
        </label>
        <label class="check-row flex items-start gap-3 p-2.5 bg-slate-950 border border-slate-800 rounded-lg cursor-pointer">
          <input type="checkbox" data-step="t_2" class="mt-0.5 h-4 w-4 rounded border-slate-700 bg-slate-900 text-emerald-500">
          <div><strong>2. USB-C Enumeration &amp; Self-Test:</strong> Plug in USB-C, flip SPDT switch <strong>ON</strong>, and run <code class="font-mono">{"cmd": "self_test"}</code> to verify speaker 880 Hz tone, PDM mic RMS, camera frame capture, and e-paper refresh.</div>
        </label>
      </div>
    </section>

  </main>

  <script>
    const STORAGE_KEY = 'gotchi_v43_bench_checklist_v1';

    function loadChecklist() {
      let saved = {};
      try {
        saved = JSON.parse(localStorage.getItem(STORAGE_KEY) || '{}');
      } catch (e) {}
      const boxes = document.querySelectorAll('input[type="checkbox"][data-step]');
      boxes.forEach(box => {
        const key = box.getAttribute('data-step');
        box.checked = !!saved[key];
        box.addEventListener('change', () => {
          saved[key] = box.checked;
          try {
            localStorage.setItem(STORAGE_KEY, JSON.stringify(saved));
          } catch (e) {}
          updateProgress();
        });
      });
      updateProgress();
    }

    function updateProgress() {
      const boxes = document.querySelectorAll('input[type="checkbox"][data-step]');
      const total = boxes.length;
      let checked = 0;
      boxes.forEach(b => { if (b.checked) checked++; });
      const pct = total > 0 ? Math.round((checked / total) * 100) : 0;
      const lbl = document.getElementById('progress-label');
      const bar = document.getElementById('progress-bar');
      if (lbl) lbl.textContent = `${checked} / ${total} (${pct}%)`;
      if (bar) bar.style.width = `${pct}%`;
    }

    function resetChecklist() {
      try { localStorage.removeItem(STORAGE_KEY); } catch (e) {}
      document.querySelectorAll('input[type="checkbox"][data-step]').forEach(b => {
        b.checked = false;
      });
      updateProgress();
    }

    document.addEventListener('DOMContentLoaded', loadChecklist);
  </script>
</body>
</html>
"""
