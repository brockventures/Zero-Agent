#!/usr/bin/env python3
"""
Zero Phone Speech Dispatcher (Pixel 8 Pro Hardware Speaker)
Synthesizes speech using neural TTS (edge-tts) and broadcasts over ADB.
Default profile: Steffan (Ultron) — en-US-SteffanNeural (-5Hz pitch, -5% rate).
"""

import sys
import os
import time
import argparse
import asyncio
import subprocess
from pathlib import Path

DEFAULT_VOICE = "en-US-SteffanNeural"
DEFAULT_PITCH = "-5Hz"
DEFAULT_RATE = "-5%"
DEFAULT_VOLUME = 21

ADB_PATH = "/workspace/tools/bin/adb"
import nas_config
ADB_HOST = nas_config.HOST_2_IP
ADB_PORT = "5037"
ADB_DEVICE = "36261FDJG0003L"

ADB_CMD = [ADB_PATH, "-H", ADB_HOST, "-P", ADB_PORT, "-s", ADB_DEVICE]
PREVIEW_COMP = "com.google.android.apps.nbu.files/com.google.android.apps.nbu.files.gateway.preview.PreviewActivity"
REMOTE_PATH = "/sdcard/Notifications/zero_announcement.mp3"
LOCAL_CACHE_DIR = Path("/workspace/scratch/audio")


async def synthesize_track(text: str, voice: str, pitch: str, rate: str, out_path: Path):
    import edge_tts
    out_path.parent.mkdir(parents=True, exist_ok=True)
    comm = edge_tts.Communicate(text, voice, pitch=pitch, rate=rate)
    await comm.save(str(out_path))


def play_on_device(local_file: Path, volume: int = DEFAULT_VOLUME, sleep_after: float = 0.0):
    # 1. Push file
    subprocess.run(ADB_CMD + ["push", str(local_file), REMOTE_PATH], check=True, capture_output=True)

    # 2. Set volume
    subprocess.run(ADB_CMD + ["shell", "cmd", "media_session", "volume", "--stream", "3", "--set", str(volume)], capture_output=True)

    # 3. Wake screen & start player
    subprocess.run(ADB_CMD + ["shell", "input", "keyevent", "KEYCODE_WAKEUP"], capture_output=True)
    subprocess.run(ADB_CMD + ["shell", "am", "start", "-n", PREVIEW_COMP, "-a", "android.intent.action.VIEW", "-d", f"file://{REMOTE_PATH}", "-t", "audio/mp3"], capture_output=True)

    # 4. Wait for audio duration
    if sleep_after > 0:
        time.sleep(sleep_after)
        # Clear screen back to home
        subprocess.run(ADB_CMD + ["shell", "input", "keyevent", "3"], capture_output=True)


def main():
    parser = argparse.ArgumentParser(description="Zero Physical Hardware Speech Dispatcher")
    parser.add_argument("text", help="Text to speak")
    parser.add_argument("--voice", default=DEFAULT_VOICE, help=f"Voice profile (default: {DEFAULT_VOICE})")
    parser.add_argument("--pitch", default=DEFAULT_PITCH, help=f"Pitch adjustment (default: {DEFAULT_PITCH})")
    parser.add_argument("--rate", default=DEFAULT_RATE, help=f"Rate adjustment (default: {DEFAULT_RATE})")
    parser.add_argument("--volume", type=int, default=DEFAULT_VOLUME, help=f"Media volume (default: {DEFAULT_VOLUME})")
    parser.add_argument("--duration", type=float, default=0.0, help="Estimated speech duration to wait before returning to home")
    parser.add_argument("--no-home", action="store_true", help="Do not send keyevent 3 (Home) after playback")

    args = parser.parse_args()

    # Estimate duration if not provided: ~13 characters per second for slow/Ultron speech
    est_duration = args.duration
    if est_duration <= 0.0:
        est_duration = max(3.5, len(args.text) / 12.0 + 1.5)

    local_file = LOCAL_CACHE_DIR / "speech.mp3"
    asyncio.run(synthesize_track(args.text, args.voice, args.pitch, args.rate, local_file))

    wait_time = 0.0 if args.no_home else est_duration
    play_on_device(local_file, volume=args.volume, sleep_after=wait_time)
    print(f"Broadcasted [{args.voice}]: \"{args.text}\"")


if __name__ == "__main__":
    main()
