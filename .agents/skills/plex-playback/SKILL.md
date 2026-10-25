---
name: plex-playback
description: >-
  Resolves any movie or TV show on Host 1 Plex Media Server and launches it directly inside the native
  Plex for Android TV app (`com.plexapp.android`) on the Living Room Google TV Streamer via Home Assistant.
---

# 🎬 Plex Living Room TV Playback Skill (`plex-playback`)

Use this skill whenever Ryan asks to play, put on, or start a movie or TV show on Plex on the Living Room TV (e.g. *"Play Emperor's New Groove on Plex on the living room TV"*, *"Put on Bluey on Plex"*).

> [!IMPORTANT]
> **Tier 1 One-Shot Execution (Never Send to `#forge`):**
> Playing media on the TV is a single-command home automation action (`<15s`), **NEVER** a multi-file build or detached background job. Always execute `python3 /workspace/tools/plex_play.py "<title>"` in a single `run_command` call.

---

## 🛠️ One-Command CLI Usage (`/workspace/tools/plex_play.py`)

```bash
# 1. Play a movie directly in the native Plex app on the Living Room Google TV Streamer
python3 /workspace/tools/plex_play.py "The Emperor's New Groove"

# 2. Play the next unwatched episode (or S01E01) of a TV show
python3 /workspace/tools/plex_play.py "Succession" --type show

# 3. Play a specific season and episode of a TV show
python3 /workspace/tools/plex_play.py "The Bear" --type show --season 2 --episode 6

# 4. Dry-run lookup (resolve ratingKey and Android TV plex:// deep link without waking the TV)
python3 /workspace/tools/plex_play.py "The Emperor's New Groove" --resolve-only
```
