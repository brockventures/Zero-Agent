import asyncio
from pathlib import Path
import sys
from unittest.mock import AsyncMock, MagicMock

_REPO_ROOT = str(Path(__file__).resolve().parent.parent)
sys.path = [p for p in sys.path if p not in ("/workspace", "/workspace/tools")]
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import discord
import tools.bridge
import tools.forge
from tools.bridge_telemetry import (
    record_token_usage,
    get_token_summary,
    resolve_channel_info,
)


class MockThread:
    def __init__(self, t_id=999111, t_name="task-test-thread", p_id=1542081375287640084, p_name="zero-chat"):
        self.id = t_id
        self.name = t_name
        self.parent_id = p_id
        self.parent = type("MockParent", (), {"id": p_id, "name": p_name})()


def test_resolve_channel_info_root():
    info = resolve_channel_info(1542081375287640084)
    assert info["is_thread"] == 0
    assert info["parent_channel_name"] == "zero-chat"
    assert info["thread_id"] is None


def test_resolve_channel_info_thread():
    mock_thread = MockThread()
    info = resolve_channel_info(1542081375287640084, thread=mock_thread, escalated_to_thread=True)
    assert info["is_thread"] == 1
    assert info["thread_id"] == 999111
    assert info["channel_name"] == "task-test-thread"
    assert info["parent_channel_name"] == "zero-chat"


def test_record_and_summary(tmp_path, monkeypatch):
    test_db = tmp_path / "test_telemetry.db"
    monkeypatch.setattr("tools.bridge_telemetry.DB_PATH", test_db)

    usage = {
        "input_tokens": 100,
        "output_tokens": 20,
        "cache_read_tokens": 50,
        "thinking_tokens": 10,
        "total_tokens": 180,
    }

    assert record_token_usage(1542081375287640084, usage, conv_id="test-conv")
    summary = get_token_summary(hours=1.0)
    assert len(summary) == 1
    assert summary[0]["parent_channel_name"] == "zero-chat"
    assert summary[0]["total_input"] == 100
    assert summary[0]["grand_total"] == 180


def test_on_button_interaction_immediate_executing_state(tmp_path, monkeypatch):
    test_db = tmp_path / "test_forge_decisions.db"
    tools.forge.init_forge_db(test_db)
    monkeypatch.setattr(tools.forge, "FORGE_DB_PATH", test_db)

    decision_id = "dec-test-exec-1"
    with tools.forge.get_db_connection(test_db) as conn:
        conn.execute(
            """
            INSERT INTO forge_decisions (id, category, title, summary, handler_type, payload_json, channel_id, is_private, status, expires_at, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (decision_id, "SYSTEM", "Immediate Executing Test", "Testing in-progress card state", "shell", '{"command": "true"}', 12345, 0, "pending", 9999999999, 1000.0),
        )

    btn_approve = discord.ui.Button(label="Approve", custom_id=f"forge_approve:{decision_id}")
    btn_dismiss = discord.ui.Button(label="Dismiss", custom_id=f"forge_dismiss:{decision_id}")
    view = discord.ui.View()
    view.add_item(btn_approve)
    view.add_item(btn_dismiss)

    mock_message = AsyncMock()
    mock_message.components = [discord.components._component_factory(c) for c in view.to_components()]
    mock_message.embeds = [discord.Embed(title="Initial")]
    mock_message.content = ""

    interaction = MagicMock(spec=discord.Interaction)
    interaction.type = discord.InteractionType.component
    interaction.data = {"custom_id": f"forge_approve:{decision_id}"}
    interaction.user = MagicMock()
    interaction.user.id = tools.forge.OWNER_USER_ID
    interaction.response = AsyncMock()
    interaction.response.is_done = MagicMock(return_value=False)
    interaction.message = mock_message

    pre_finish_captured = {}
    real_execute = tools.forge.execute_decision

    def spied_execute_decision(did, uid):
        pre_finish_captured["called"] = mock_message.edit.called
        pre_finish_captured["call_count"] = mock_message.edit.call_count
        if mock_message.edit.call_args:
            pre_finish_captured["kwargs"] = dict(mock_message.edit.call_args.kwargs)
        return real_execute(did, uid, db_path=test_db)

    monkeypatch.setattr(tools.forge, "execute_decision", spied_execute_decision)

    asyncio.run(tools.bridge.on_button_interaction(interaction))

    # 1. Verify before execute_decision finishes, message was edited with executing state and disabled view
    assert pre_finish_captured["called"] is True
    assert pre_finish_captured["call_count"] == 1
    kwargs = pre_finish_captured["kwargs"]
    assert "view" in kwargs
    disabled_view = kwargs["view"]
    assert len(disabled_view.children) == 2
    assert all(child.disabled for child in disabled_view.children)

    assert "embed" in kwargs
    exec_embed = kwargs["embed"]
    assert exec_embed.title.startswith("⏳")
    assert exec_embed.color.value == 0x3B82F6
    assert "Executing..." in exec_embed.footer.text

    # 2. Verify after execute_decision finishes, final update occurred
    assert mock_message.edit.call_count == 2
    final_kwargs = mock_message.edit.call_args.kwargs
    final_embed = final_kwargs["embed"]
    assert final_embed.title.startswith("✅")
    assert final_embed.color.value == 0x10B981


def test_on_button_interaction_already_executing_does_not_overwrite(tmp_path, monkeypatch):
    test_db = tmp_path / "test_forge_decisions.db"
    tools.forge.init_forge_db(test_db)
    monkeypatch.setattr(tools.forge, "FORGE_DB_PATH", test_db)

    decision_id = "dec-test-exec-2"
    with tools.forge.get_db_connection(test_db) as conn:
        conn.execute(
            """
            INSERT INTO forge_decisions (id, category, title, summary, handler_type, payload_json, channel_id, is_private, status, expires_at, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (decision_id, "SYSTEM", "Duplicate Click Test", "Testing already executing lock", "shell", '{"command": "true"}', 12345, 0, "pending", 9999999999, 1000.0),
        )

    btn = discord.ui.Button(label="Approve", custom_id=f"forge_approve:{decision_id}")
    view = discord.ui.View()
    view.add_item(btn)

    mock_message = AsyncMock()
    mock_message.components = [discord.components._component_factory(c) for c in view.to_components()]
    mock_message.embeds = [discord.Embed(title="Initial")]
    mock_message.content = ""

    interaction = MagicMock(spec=discord.Interaction)
    interaction.type = discord.InteractionType.component
    interaction.data = {"custom_id": f"forge_approve:{decision_id}"}
    interaction.user = MagicMock()
    interaction.user.id = tools.forge.OWNER_USER_ID
    interaction.response = AsyncMock()
    interaction.response.is_done = MagicMock(return_value=False)
    interaction.message = mock_message

    # Mock execute_decision to return already executing error (as forge.py does when cur.rowcount == 0)
    monkeypatch.setattr(
        tools.forge,
        "execute_decision",
        lambda did, uid: {"ok": False, "error": f"Decision {did} is already executing."},
    )

    asyncio.run(tools.bridge.on_button_interaction(interaction))

    # The message should have been edited once to in-progress state, and NOT overwritten with a failed card
    assert mock_message.edit.call_count == 1
    kwargs = mock_message.edit.call_args.kwargs
    assert kwargs["embed"].title.startswith("⏳")
    assert kwargs["embed"].color.value == 0x3B82F6


def test_on_button_interaction_dismiss_immediate_disable(tmp_path, monkeypatch):
    test_db = tmp_path / "test_forge_decisions.db"
    tools.forge.init_forge_db(test_db)
    monkeypatch.setattr(tools.forge, "FORGE_DB_PATH", test_db)

    decision_id = "dec-test-dismiss-1"
    with tools.forge.get_db_connection(test_db) as conn:
        conn.execute(
            """
            INSERT INTO forge_decisions (id, category, title, summary, handler_type, payload_json, channel_id, is_private, status, expires_at, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (decision_id, "SYSTEM", "Dismiss Test", "Testing dismiss button", "shell", '{"command": "true"}', 12345, 0, "pending", 9999999999, 1000.0),
        )

    btn = discord.ui.Button(label="Dismiss", custom_id=f"forge_dismiss:{decision_id}")
    view = discord.ui.View()
    view.add_item(btn)

    mock_message = AsyncMock()
    mock_message.components = [discord.components._component_factory(c) for c in view.to_components()]
    mock_message.embeds = [discord.Embed(title="Initial")]
    mock_message.content = ""

    interaction = MagicMock(spec=discord.Interaction)
    interaction.type = discord.InteractionType.component
    interaction.data = {"custom_id": f"forge_dismiss:{decision_id}"}
    interaction.user = MagicMock()
    interaction.user.id = tools.forge.OWNER_USER_ID
    interaction.response = AsyncMock()
    interaction.response.is_done = MagicMock(return_value=False)
    interaction.message = mock_message

    pre_dismiss_captured = {}
    real_dismiss = tools.forge.dismiss_decision

    def spied_dismiss_decision(did, uid):
        pre_dismiss_captured["called"] = mock_message.edit.called
        pre_dismiss_captured["call_count"] = mock_message.edit.call_count
        if mock_message.edit.call_args:
            pre_dismiss_captured["kwargs"] = dict(mock_message.edit.call_args.kwargs)
        return real_dismiss(did, uid, db_path=test_db)

    monkeypatch.setattr(tools.forge, "dismiss_decision", spied_dismiss_decision)

    asyncio.run(tools.bridge.on_button_interaction(interaction))

    # Before dismiss finishes, buttons were immediately disabled
    assert pre_dismiss_captured["called"] is True
    assert pre_dismiss_captured["call_count"] == 1
    disabled_view = pre_dismiss_captured["kwargs"]["view"]
    assert all(child.disabled for child in disabled_view.children)

    # After dismiss finishes, final message update occurred
    assert mock_message.edit.call_count == 2
    final_embed = mock_message.edit.call_args.kwargs["embed"]
    assert final_embed.title.startswith("🛑")
