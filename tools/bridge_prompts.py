"""
bridge_prompts.py — Crab Cavern & Bridge System Prompt Constants and Escalation Directives.

Centralizes Crab Cavern air-gap security rules and cross-channel Decision Card escalation
guidance so `bridge_pipeline.py` and external prompt templates enforce consistent 1-click
Decision Card staging to `#zero-chat` (`1542081375287640084`) whenever a Crab Cavern turn
requires a homelab/NAS action (never asking Ryan to re-prompt in `#zero-chat`).
"""

from __future__ import annotations

from pathlib import Path

ZERO_CHAT_CHANNEL_ID = 1542081375287640084
ZERO_CHAT_CHANNEL_ID_STR = "1542081375287640084"
CRAB_CAVERN_PROMPT_PATH = Path("/workspace/config/prompts/crab_cavern_prompt.md")

CRAB_CAVERN_HOMELAB_ESCALATION_DIRECTIVE = (
    "• Homelab/NAS Cross-Channel Escalation: Whenever a Crab Cavern request requires a homelab/NAS action "
    "(air-gapped from shared channels), NEVER ask Ryan to re-prompt in #zero-chat; immediately stage a 1-click "
    "Decision Card to #zero-chat (`1542081375287640084`) via "
    "`python3 /workspace/tools/forge.py propose --channel 1542081375287640084 ... --post`."
)

CRAB_CAVERN_AIRGAP_RULE_TEXT = (
    "3. HOMELAB / NAS AIR-GAP & 1-CLICK `#zero-chat` DECISION CARD ESCALATION (NEVER ASK RYAN TO RE-PROMPT):\n"
    "   - NEVER directly execute private homelab NAS (Host 1, Host 2), SSH, or Home Assistant commands inline inside shared Crab Cavern turns.\n"
    "   - However, whenever a request in Crab Cavern requires a homelab/NAS action, you MUST NEVER tell Ryan to re-prompt, switch channels, or repeat his request in `#zero-chat`.\n"
    "   - Instead, immediately stage a 1-click Decision Card to `#zero-chat` (`1542081375287640084`) on the spot via "
    "`python3 /workspace/tools/forge.py propose --channel 1542081375287640084 --category \"homelab_action\" --title \"<Title>\" --summary \"<Summary>\" --handler \"<handler>\" --payload '<json>' --post` "
    "(or `stage-decision --channel-id 1542081375287640084 ... --post`), and let Ryan know in Crab Cavern that the 1-click approval card is waiting for him in <#1542081375287640084>."
)


def get_crab_cavern_escalation_directive() -> str:
    """Return the Crab Cavern to #zero-chat 1-click Decision Card escalation prompt bullet."""
    return CRAB_CAVERN_HOMELAB_ESCALATION_DIRECTIVE


def load_crab_cavern_system_prompt(prompt_path: Path | str | None = None) -> str:
    """Load the Crab Cavern system prompt template with the #zero-chat Decision Card escalation rule."""
    target = Path(prompt_path) if prompt_path else CRAB_CAVERN_PROMPT_PATH
    if not target.exists():
        repo_fallback = Path(__file__).resolve().parent.parent / "config" / "prompts" / "crab_cavern_prompt.md"
        if repo_fallback.exists():
            target = repo_fallback
    if target.exists():
        try:
            text = target.read_text(encoding="utf-8")
            if "forge.py propose --channel 1542081375287640084" not in text:
                text = f"{text}\n\n{CRAB_CAVERN_HOMELAB_ESCALATION_DIRECTIVE}"
            return text
        except Exception:
            pass
    return f"[CRAB CAVERN MULTI-AGENT COLLABORATION ENVIRONMENT]\n{CRAB_CAVERN_AIRGAP_RULE_TEXT}\n"
