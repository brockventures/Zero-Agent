import asyncio
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

if "/workspace" not in sys.path:
    sys.path.insert(0, "/workspace")


@pytest.mark.asyncio
async def test_slash_model_defers_immediately_and_uses_followup():
    """Verify /model calls interaction.response.defer() before slow CLI/worker operations
    and replies via interaction.followup.send() to prevent 10062 Unknown Interaction errors.
    """
    import tools.bridge as bridge

    interaction = MagicMock()
    interaction.response = MagicMock()
    interaction.response.defer = AsyncMock()
    interaction.response.send_message = AsyncMock()
    interaction.followup = MagicMock()
    interaction.followup.send = AsyncMock()

    fake_available = {
        "gemini-4-argon-low": "Gemini 4 Argon (Low)",
        "gemini-4-argon-high": "Gemini 4 Argon (High)",
    }

    with (
        patch("tools.bridge_models.list_available_models", return_value=fake_available),
        patch("tools.bridge._set_active_model") as mock_set,
        patch("tools.bridge_daemons.daemon_manager.recycle_all", new_callable=AsyncMock) as mock_recycle,
    ):
        await bridge.slash_model.callback(interaction, model_name="argon-low")

    interaction.response.defer.assert_awaited_once()
    interaction.response.send_message.assert_not_awaited()
    mock_set.assert_called_once_with("gemini-4-argon-low")
    mock_recycle.assert_awaited_once()
    interaction.followup.send.assert_awaited_once()
    sent_text = interaction.followup.send.call_args[0][0]
    assert "gemini-4-argon-low" in sent_text


@pytest.mark.asyncio
async def test_slash_model_empty_arg_lists_models_via_followup():
    """Verify /model with no arguments also defers and sends model list via followup."""
    import tools.bridge as bridge

    interaction = MagicMock()
    interaction.response = MagicMock()
    interaction.response.defer = AsyncMock()
    interaction.followup = MagicMock()
    interaction.followup.send = AsyncMock()

    fake_available = {
        "gemini-4-argon-low": "Gemini 4 Argon (Low)",
        "claude-sonnet-5-5-medium": "Claude Sonnet 5.5 (Medium)",
    }

    with (
        patch("tools.bridge_models.list_available_models", return_value=fake_available),
        patch("tools.bridge._get_active_model", return_value="gemini-4-argon-low"),
    ):
        await bridge.slash_model.callback(interaction, model_name="")

    interaction.response.defer.assert_awaited_once()
    interaction.followup.send.assert_awaited_once()
    sent_text = interaction.followup.send.call_args[0][0]
    assert "Current Active Model:" in sent_text
    assert "gemini-4-argon-low" in sent_text


@pytest.mark.asyncio
async def test_slash_model_autocomplete_is_non_blocking():
    """Verify autocomplete reads cached models synchronously and never blocks on _fetch_models."""
    import tools.bridge as bridge

    interaction = MagicMock()
    fake_cache = {
        "fetched_at": 9999999999.0,
        "models": {
            "gemini-4-argon-low": "Gemini 4 Argon (Low)",
            "gemini-4-argon-high": "Gemini 4 Argon (High)",
        },
    }

    with (
        patch("tools.bridge_models._read_cache", return_value=fake_cache),
        patch("tools.bridge_models._fetch_models") as mock_fetch,
    ):
        choices = await bridge.slash_model_autocomplete(interaction, "argon")

    mock_fetch.assert_not_called()
    values = [c.value for c in choices]
    assert "argon" in values
    assert "argon-low" in values
    assert "gemini-4-argon-low" in values
