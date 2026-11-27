"""Unit tests for tools/deploy_floorplan_hearth_swipe.py."""

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

from tools import deploy_floorplan_hearth_swipe as mod

CARD_FIXTURE_WITH_OVEN_MEATER = """/* Brock House 4K Photorealistic Cycles Floorplan Card */
class BrockHouseCyclesCard {
  constructor() {
    this._cookModalOpen = false;
    this._thermoModalOpen = false;
    this._presenceModalOpen = false;
    this.shadowRoot = null;
  }

  connectedCallback() {
    this._syncKioskMode();
  }

  _syncKioskMode() {}

  _bindListeners(sr) {
    /* OVEN_MEATER_LISTENERS_BEGIN */
    sr.getElementById("cookBackdrop")?.addEventListener("click", () => {});
    /* OVEN_MEATER_LISTENERS_END */
  }
}
"""

CARD_FIXTURE_WITHOUT_OVEN_MEATER = """/* Brock House 4K Photorealistic Cycles Floorplan Card (Fallback Anchor) */
class BrockHouseCyclesCard {
  constructor() {
    this._cookModalOpen = false;
    this._thermoModalOpen = false;
    this._presenceModalOpen = false;
    this.shadowRoot = null;
  }

  connectedCallback() {
    this._syncKioskMode();
  }

  _syncKioskMode() {}
}
"""


def test_patch_card_js_injects_after_oven_meater_listeners_and_is_idempotent() -> None:
    patched_once = mod.patch_card_js(CARD_FIXTURE_WITH_OVEN_MEATER)
    patched_twice = mod.patch_card_js(patched_once)
    assert patched_once == patched_twice

    for marker in mod.REQUIRED_MARKERS:
        assert marker in patched_once
        assert patched_once.count(marker) == 1

    oven_end_idx = patched_once.index("/* OVEN_MEATER_LISTENERS_END */")
    swipe_begin_idx = patched_once.index(mod.BEGIN_MARKER)
    assert swipe_begin_idx > oven_end_idx


def test_patch_card_js_fallback_anchor_in_connected_callback() -> None:
    assert "OVEN_MEATER_LISTENERS_END" not in CARD_FIXTURE_WITHOUT_OVEN_MEATER
    patched_once = mod.patch_card_js(CARD_FIXTURE_WITHOUT_OVEN_MEATER)
    patched_twice = mod.patch_card_js(patched_once)
    assert patched_once == patched_twice

    for marker in mod.REQUIRED_MARKERS:
        assert marker in patched_once
        assert patched_once.count(marker) == 1

    cc_idx = patched_once.index("connectedCallback() {")
    swipe_begin_idx = patched_once.index(mod.BEGIN_MARKER)
    sync_idx = patched_once.index("this._syncKioskMode();")
    assert cc_idx < swipe_begin_idx < sync_idx


def test_verify_patched_card_contents_passes_and_raises_on_unpatched() -> None:
    patched = mod.patch_card_js(CARD_FIXTURE_WITH_OVEN_MEATER)
    mod.verify_patched_card_contents(patched)

    with pytest.raises(RuntimeError, match="Patched Hearth swipe bridge verification failed"):
        mod.verify_patched_card_contents(CARD_FIXTURE_WITH_OVEN_MEATER)


def test_hearth_swipe_bridge_runtime_touch_pointer_and_modal_guards() -> None:
    patched = mod.patch_card_js(CARD_FIXTURE_WITH_OVEN_MEATER)
    harness = (
        patched
        + """
const postedMessages = [];
globalThis.window = {
  parent: {
    postMessage: (msg, targetOrigin) => {
      postedMessages.push({ msg, targetOrigin });
    },
  },
};

const listeners = {};
let activeOpenOverlay = null;
const mockShadowRoot = {
  getElementById: () => null,
  querySelector: (sel) => {
    if (
      activeOpenOverlay &&
      sel.includes(activeOpenOverlay)
    ) {
      return { className: activeOpenOverlay };
    }
    return null;
  },
  addEventListener: (type, fn, opts) => {
    (listeners[type] = listeners[type] || []).push({ fn, opts });
  },
  dispatch: (type, evt) => {
    for (const entry of listeners[type] || []) {
      entry.fn(evt);
    }
  },
};

const card = new BrockHouseCyclesCard();
card.shadowRoot = mockShadowRoot;
card._bindListeners(mockShadowRoot);
// Calling _bindListeners a second time must not duplicate listeners due to _hearthSwipeBound guard
card._bindListeners(mockShadowRoot);

const listenerCounts = Object.fromEntries(
  Object.entries(listeners).map(([k, arr]) => [k, arr.length])
);
const allPassive = Object.values(listeners)
  .flat()
  .every((entry) => entry.opts && entry.opts.passive === true);

// 1. Valid horizontal left swipe -> direction: "next"
mockShadowRoot.dispatch("touchstart", {
  touches: [{ clientX: 500, clientY: 300 }],
  composedPath: () => [],
});
mockShadowRoot.dispatch("touchmove", {
  touches: [{ clientX: 380, clientY: 310 }],
});
mockShadowRoot.dispatch("touchend", {
  changedTouches: [{ clientX: 380, clientY: 310 }],
});

// 2. Valid horizontal right swipe -> direction: "prev"
mockShadowRoot.dispatch("touchstart", {
  touches: [{ clientX: 200, clientY: 300 }],
  composedPath: () => [],
});
mockShadowRoot.dispatch("touchmove", {
  touches: [{ clientX: 340, clientY: 295 }],
});
mockShadowRoot.dispatch("touchend", {
  changedTouches: [{ clientX: 340, clientY: 295 }],
});

// 3. Short tap (|dx| < 60) -> ignored
mockShadowRoot.dispatch("touchstart", {
  touches: [{ clientX: 200, clientY: 300 }],
  composedPath: () => [],
});
mockShadowRoot.dispatch("touchmove", {
  touches: [{ clientX: 235, clientY: 302 }],
});
mockShadowRoot.dispatch("touchend", {
  changedTouches: [{ clientX: 235, clientY: 302 }],
});

// 4. Vertical scroll (|dy| dominates) -> ignored
mockShadowRoot.dispatch("touchstart", {
  touches: [{ clientX: 200, clientY: 300 }],
  composedPath: () => [],
});
mockShadowRoot.dispatch("touchmove", {
  touches: [{ clientX: 280, clientY: 450 }],
});
mockShadowRoot.dispatch("touchend", {
  changedTouches: [{ clientX: 280, clientY: 450 }],
});

// 5. Multi-touch gesture -> cancelled & ignored
mockShadowRoot.dispatch("touchstart", {
  touches: [
    { clientX: 500, clientY: 300 },
    { clientX: 520, clientY: 310 },
  ],
  composedPath: () => [],
});
mockShadowRoot.dispatch("touchend", {
  changedTouches: [{ clientX: 300, clientY: 300 }],
});

// 6. Open modal overlay (.cook-modal-overlay.open) -> blocked
activeOpenOverlay = ".cook-modal-overlay.open";
mockShadowRoot.dispatch("touchstart", {
  touches: [{ clientX: 500, clientY: 300 }],
  composedPath: () => [],
});
mockShadowRoot.dispatch("touchmove", {
  touches: [{ clientX: 320, clientY: 300 }],
});
mockShadowRoot.dispatch("touchend", {
  changedTouches: [{ clientX: 320, clientY: 300 }],
});
activeOpenOverlay = null;

// 7. Gesture starting inside blocked element ([data-prevent-screen-swipe] / .cook-modal-card) -> blocked
mockShadowRoot.dispatch("touchstart", {
  touches: [{ clientX: 500, clientY: 300 }],
  composedPath: () => [
    {
      matches: (sel) => sel.includes("[data-prevent-screen-swipe]"),
    },
  ],
});
mockShadowRoot.dispatch("touchmove", {
  touches: [{ clientX: 320, clientY: 300 }],
});
mockShadowRoot.dispatch("touchend", {
  changedTouches: [{ clientX: 320, clientY: 300 }],
});

// 8. Desktop mouse pointer drag -> posts message; touch pointerType -> ignored (no double-fire)
mockShadowRoot.dispatch("pointerdown", {
  pointerType: "mouse",
  button: 0,
  pointerId: 1,
  clientX: 600,
  clientY: 400,
  composedPath: () => [],
});
mockShadowRoot.dispatch("pointerup", {
  pointerType: "mouse",
  pointerId: 1,
  clientX: 460,
  clientY: 405,
});

mockShadowRoot.dispatch("pointerdown", {
  pointerType: "touch",
  button: 0,
  pointerId: 2,
  clientX: 600,
  clientY: 400,
  composedPath: () => [],
});
mockShadowRoot.dispatch("pointerup", {
  pointerType: "touch",
  pointerId: 2,
  clientX: 460,
  clientY: 405,
});

console.log(
  JSON.stringify({
    boundFlag: card._hearthSwipeBound,
    listenerCounts,
    allPassive,
    postedMessages,
  })
);
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

    assert res["boundFlag"] is True
    assert res["allPassive"] is True
    assert res["listenerCounts"] == {
        "touchstart": 1,
        "touchmove": 1,
        "touchend": 1,
        "touchcancel": 1,
        "pointerdown": 1,
        "pointerup": 1,
        "pointercancel": 1,
    }
    assert res["postedMessages"] == [
        {"msg": {"type": "hearth-swipe", "direction": "next"}, "targetOrigin": "*"},
        {"msg": {"type": "hearth-swipe", "direction": "prev"}, "targetOrigin": "*"},
        {"msg": {"type": "hearth-swipe", "direction": "next"}, "targetOrigin": "*"},
    ]


def test_dry_run_and_verify_only_cli() -> None:
    patched = mod.patch_card_js(CARD_FIXTURE_WITH_OVEN_MEATER)

    with (
        mock.patch.object(mod, "resolve_ha_credentials", return_value=("http://198.51.100.10:8123", "tok")),
        mock.patch.object(mod, "fetch_live_card_and_backup", return_value=CARD_FIXTURE_WITH_OVEN_MEATER) as mock_fetch,
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
