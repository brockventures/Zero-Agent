from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tools.bridge_handlers import handle_on_ready
from tools.bridge_scheduler import BridgeScheduler


@pytest.mark.asyncio
async def test_handle_on_ready_starts_workers_and_scheduler_first():
    bot = MagicMock()
    bot.user = MagicMock(id=123456)
    bot.get_channel.return_value = None
    bot.fetch_channel = AsyncMock(return_value=None)

    turn_queue = MagicMock()
    ext_turn_queue = MagicMock()
    start_workers_fn = MagicMock()
    start_scheduler_fn = AsyncMock()

    with (
        patch("tools.bridge_handlers.sync_credentials"),
        patch("tools.bridge_handlers.update_beacon"),
        patch("tools.bridge_handlers.has_notified_ready", True),
        patch(
            "tools.bridge_daemons.warmup_persistent_daemons",
            side_effect=RuntimeError("simulated warmup failure"),
        ),
    ):
        await handle_on_ready(
            bot=bot,
            turn_queue=turn_queue,
            ext_turn_queue=ext_turn_queue,
            start_workers_fn=start_workers_fn,
            start_scheduler_fn=start_scheduler_fn,
        )

    start_workers_fn.assert_called_once()
    start_scheduler_fn.assert_awaited_once()


def test_vulcan_runtime_hygiene_skips_agy_and_forge_workers(tmp_path: Path):
    sched = BridgeScheduler.__new__(BridgeScheduler)
    fake_proc = tmp_path / "proc"
    fake_proc.mkdir()

    # PID 200: an agy worker whose prompt mentions zero_mail_listener.py
    p200 = fake_proc / "200"
    p200.mkdir()
    (p200 / "cmdline").write_bytes(
        b"/usr/local/bin/agy\x00--output-format=stream-json\x00-p=edit zero_mail_listener.py\x00"
    )

    # PID 300: an actual duplicate listener daemon
    p300 = fake_proc / "300"
    p300.mkdir()
    (p300 / "cmdline").write_bytes(
        b"/usr/bin/python3\x00/workspace/tools/zero_mail_listener.py\x00run\x00"
    )

    killed = []

    def fake_Path(arg):
        if arg == "/proc":
            return fake_proc
        return Path(arg)

    with (
        patch("tools.bridge_scheduler.Path", side_effect=fake_Path),
        patch("tools.bridge_scheduler.os.kill", side_effect=lambda pid, sig: killed.append(pid)),
    ):
        res = sched._ensure_vulcan_runtime_hygiene(
            workspace_tools_dir=tmp_path / "ws_tools",
            app_dir=tmp_path / "app",
        )

    assert 200 not in killed
    assert 300 in killed
    assert res["killed_pids"] == [300]
