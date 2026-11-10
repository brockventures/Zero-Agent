import asyncio
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

# Ensure the active worktree's tools package takes precedence when PYTHONPATH=/workspace is passed
_REPO_ROOT = str(Path(__file__).resolve().parent.parent)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)
elif sys.path[0] != _REPO_ROOT:
    sys.path.remove(_REPO_ROOT)
    sys.path.insert(0, _REPO_ROOT)

from tools.bridge_scheduler import (
    DEFAULT_PEER_SHARED_RULES,
    BridgeScheduler,
    ensure_peer_shared_customizations_registered,
)


def test_ensure_peer_shared_customizations_no_opt_zero(tmp_path: Path):
    gemini_cfg = tmp_path / "gemini_config"
    opt_zero = tmp_path / "nonexistent_opt_zero"
    ws_agents = tmp_path / "workspace_agents"

    res = ensure_peer_shared_customizations_registered(
        gemini_config_dir=gemini_cfg,
        opt_zero_agents_dir=opt_zero,
        workspace_agents_dir=ws_agents,
    )
    assert res == {
        "registered": False,
        "skills_json": False,
        "rules_json": False,
        "synced_rules": [],
    }
    assert not gemini_cfg.exists()


def test_ensure_peer_shared_customizations_creates_and_preserves_configs(tmp_path: Path):
    gemini_cfg = tmp_path / "gemini_config"
    opt_zero = tmp_path / "opt_zero" / ".agents"
    ws_agents = tmp_path / "workspace" / ".agents"

    (opt_zero / "skills").mkdir(parents=True)
    (opt_zero / "rules").mkdir(parents=True)
    (ws_agents / "rules").mkdir(parents=True)

    # Pre-seed skills.json with an existing entry and inherits
    gemini_cfg.mkdir(parents=True)
    (gemini_cfg / "skills.json").write_text(
        json.dumps(
            {
                "inherits": ["default"],
                "entries": [{"path": "/custom/skills"}],
            }
        ),
        encoding="utf-8",
    )

    # Create 2 default shared rules and 1 extra symlinked rule in opt_zero
    (opt_zero / "rules" / "discord_formatting.md").write_text("# Discord Formatting\n", encoding="utf-8")
    (opt_zero / "rules" / "anti-ai-syntax.md").write_text("# Anti AI Syntax\n", encoding="utf-8")
    (opt_zero / "rules" / "custom_shared.md").write_text("# Custom Shared\n", encoding="utf-8")

    # Symlink custom_shared.md and discord_formatting.md in workspace/.agents/rules
    (ws_agents / "rules" / "custom_shared.md").symlink_to(opt_zero / "rules" / "custom_shared.md")
    (ws_agents / "rules" / "discord_formatting.md").symlink_to(opt_zero / "rules" / "discord_formatting.md")
    # Non-symlink local rule in workspace should not be copied
    (ws_agents / "rules" / "local_only.md").write_text("# Local Only\n", encoding="utf-8")

    res = ensure_peer_shared_customizations_registered(
        gemini_config_dir=gemini_cfg,
        opt_zero_agents_dir=opt_zero,
        workspace_agents_dir=ws_agents,
    )

    assert res["registered"] is True
    assert res["skills_json"] is True
    assert res["rules_json"] is True
    assert set(res["synced_rules"]) == {
        "custom_shared.md",
        "discord_formatting.md",
        "anti-ai-syntax.md",
    }

    skills_data = json.loads((gemini_cfg / "skills.json").read_text(encoding="utf-8"))
    assert skills_data["inherits"] == ["default"]
    assert {"path": "/custom/skills"} in skills_data["entries"]
    assert {"path": str(opt_zero / "skills")} in skills_data["entries"]
    assert len(skills_data["entries"]) == 2

    rules_data = json.loads((gemini_cfg / "rules.json").read_text(encoding="utf-8"))
    assert rules_data["entries"] == [{"path": str(opt_zero / "rules")}]

    # Verify synced files in gemini_cfg / "rules" are regular files, not symlinks
    for name in ("custom_shared.md", "discord_formatting.md", "anti-ai-syntax.md"):
        p = gemini_cfg / "rules" / name
        assert p.exists()
        assert not p.is_symlink()
    assert not (gemini_cfg / "rules" / "local_only.md").exists()

    # Second run is idempotent: no duplicate entries and 0 newly synced rules
    res2 = ensure_peer_shared_customizations_registered(
        gemini_config_dir=gemini_cfg,
        opt_zero_agents_dir=opt_zero,
        workspace_agents_dir=ws_agents,
    )
    assert res2["registered"] is True
    assert res2["synced_rules"] == []

    skills_data2 = json.loads((gemini_cfg / "skills.json").read_text(encoding="utf-8"))
    assert len(skills_data2["entries"]) == 2

    # Modifying upstream rule triggers re-sync
    (opt_zero / "rules" / "discord_formatting.md").write_text("# Updated Content\n", encoding="utf-8")
    res3 = ensure_peer_shared_customizations_registered(
        gemini_config_dir=gemini_cfg,
        opt_zero_agents_dir=opt_zero,
        workspace_agents_dir=ws_agents,
    )
    assert res3["synced_rules"] == ["discord_formatting.md"]
    assert (gemini_cfg / "rules" / "discord_formatting.md").read_text(encoding="utf-8") == "# Updated Content\n"


def test_all_seven_default_shared_rules_synced(tmp_path: Path):
    gemini_cfg = tmp_path / "gemini_config"
    opt_zero = tmp_path / "opt_zero" / ".agents"
    ws_agents = tmp_path / "workspace" / ".agents"

    (opt_zero / "rules").mkdir(parents=True)
    for rname in DEFAULT_PEER_SHARED_RULES:
        (opt_zero / "rules" / rname).write_text(f"Rule: {rname}\n", encoding="utf-8")

    res = ensure_peer_shared_customizations_registered(
        gemini_config_dir=gemini_cfg,
        opt_zero_agents_dir=opt_zero,
        workspace_agents_dir=ws_agents,
    )
    assert set(res["synced_rules"]) == set(DEFAULT_PEER_SHARED_RULES)
    for rname in DEFAULT_PEER_SHARED_RULES:
        assert (gemini_cfg / "rules" / rname).read_text(encoding="utf-8") == f"Rule: {rname}\n"


def test_scheduler_hooks_invoke_registration():
    sched = BridgeScheduler(dispatch_fn=MagicMock())
    with patch("tools.bridge_scheduler.ensure_peer_shared_customizations_registered") as mock_reg, \
         patch("tools.bridge_state.update_gateway_heartbeat"), \
         patch("tools.process_probe.reap_stale_agy_processes"):
        sched._sync_housekeeping()
        assert mock_reg.call_count == 1

    with patch("tools.bridge_scheduler.ensure_peer_shared_customizations_registered") as mock_reg2, \
         patch.object(sched, "_evaluate_and_dispatch_jobs"), \
         patch.object(sched, "_loop"), \
         patch.object(sched, "_heartbeat_loop"):
        async def _run():
            await sched.start()
            await sched.stop()
        asyncio.run(_run())
        assert mock_reg2.call_count == 1
