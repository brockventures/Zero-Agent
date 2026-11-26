"""Unit tests for Hearth Daily Weather Art Pipeline.

Covers:
1. Character consistency clause & slot prompt invariants (golden-blonde hair, light blue-green eyes,
   7yo Rosie / 3yo Isaac / 3mo baby Eve, right 42% / left 58% composition, zero UI).
2. Clothing state mapping across rain, cold, mild, warm, and hot weather brackets.
3. Family context collection with mocked Switch, Tautulli, Steam, and memory fixtures
   (RFC 5737 198.51.100.x documentation IPs only) including exact Ocarina of Time fidelity.
4. Art style vault selection with 12-day cooldown and 60/40 context vs vault weighting.
5. Forecast retrieval and seasonal fallback.
6. Daily manifest schema validation, dry-run mode, and mocked image generation / forensic check / scp deploy.
7. Schedule registration and idempotency.
8. CLI entry point options.
"""

from __future__ import annotations

import json
import stat
import sys
from datetime import datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from unittest.mock import MagicMock, patch

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path[:] = [p for p in sys.path if p not in ("/workspace", "/workspace/tools")]
if str(REPO_ROOT) in sys.path:
    sys.path.remove(str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT))

# Evict any modules previously loaded from /workspace
for mod_name in list(sys.modules.keys()):
    if mod_name in ("tools", "sidecars", "scheduler_tool") or mod_name.startswith("tools."):
        mod = sys.modules[mod_name]
        mod_file = str(getattr(mod, "__file__", "") or "")
        if "/workspace/" in mod_file:
            del sys.modules[mod_name]

import tools  # noqa: I001
tools.__path__ = [str(REPO_ROOT / "tools")]
if str(REPO_ROOT / "tools") in sys.path:
    sys.path.remove(str(REPO_ROOT / "tools"))
sys.path.insert(0, str(REPO_ROOT / "tools"))

try:
    from tools import hearth_daily_weather_art as hdwa
except ImportError:
    import hearth_daily_weather_art as hdwa

import pytest


# ===========================================================================
# 1. Character Consistency & Prompt Invariant Tests
# ===========================================================================
class TestCharacterConsistencyAndPrompts:
    """Verifies strict character consistency, composition, and negative guardrails."""

    def test_base_character_consistency_clause_content(self) -> None:
        """Verify the base clause contains all mandatory character invariants."""
        clause = hdwa.BASE_CHARACTER_CONSISTENCY_CLAUSE

        # Rosie checks
        assert "Rosie" in clause
        assert "7-year-old girl" in clause
        assert "golden-blonde hair" in clause
        assert "light blue-green eyes" in clause

        # Isaac checks
        assert "Isaac" in clause
        assert "3-year-old toddler boy" in clause
        assert "noticeably shorter than Rosie" in clause
        assert "golden-blonde hair" in clause
        assert "light blue-green eyes" in clause

        # Genevieve (Eve) checks
        assert "Genevieve (Eve)" in clause
        assert "3-month-old infant baby girl" in clause
        assert "tiny newborn-sized baby" in clause
        assert "golden-blonde" in clause
        assert "light blue-green eyes" in clause
        assert "cradled" in clause

        # Composition invariants (right 42% / left 58%)
        assert "RIGHT 42%" in clause
        assert "LEFT 58%" in clause
        assert "16:9" in clause

        # Negative guardrails
        assert "no dark hair" in clause.lower()
        assert "no brown hair" in clause.lower()
        assert "no black hair" in clause.lower()
        assert "zero-ui invariant" in clause.lower()
        assert "no text" in clause.lower()
        assert "no watermarks" in clause.lower()
        assert "candid natural interaction" in clause.lower()
        assert "never a stiff front-facing posed family portrait" in clause.lower()
        assert len(clause) < 950

    def test_build_slot_prompt_choreography_easter_eggs_and_no_duplicate_attire(self) -> None:
        """Verify distinct per-slot choreography, Easter eggs, weather-synced lighting, and single attire block."""
        theme = hdwa.ART_STYLE_VAULT["spiderverse_halftone"]
        clear_weather = {"temp_f": 64.0, "condition": "sunny", "clothing_state": "warm_pants"}

        prompts = {
            slot: hdwa.build_slot_prompt(slot=slot, theme=theme, weather_info=clear_weather)
            for slot in ("morning", "afternoon", "evening")
        }

        # (a) Distinct choreography, sub-location, and Easter egg across morning/afternoon/evening
        for slot in ("morning", "afternoon", "evening"):
            p = prompts[slot]
            assert hdwa.SLOT_CHOREOGRAPHY[slot] in p
            assert theme["scene_flavor"][slot] in p
            assert theme["easter_eggs"][slot] in p
            # High-signal ordering: style -> choreography -> attire -> atmosphere -> setting -> characters
            assert p.index(theme["prompt_style"]) < p.index(hdwa.SLOT_CHOREOGRAPHY[slot])
            assert p.index(hdwa.SLOT_CHOREOGRAPHY[slot]) < p.index("MANDATORY WEATHER-MATCHED ATTIRE")
            assert p.index("MANDATORY WEATHER-MATCHED ATTIRE") < p.index("Atmosphere:")
            assert p.index("Atmosphere:") < p.index("Setting:")
            assert p.index("Setting:") < p.index("Characters:")

        assert len({hdwa.SLOT_CHOREOGRAPHY[s] for s in ("morning", "afternoon", "evening")}) == 3
        assert "Miles Morales" in prompts["morning"]
        assert "white-hooded spider-heroine" in prompts["afternoon"]
        assert "Spider-Ham" in prompts["evening"]

        # (b) Clothing description appears only ONCE (no duplicate 'Current weather attire' block)
        clothing_desc = hdwa.CLOTHING_STATE_PROMPTS["warm_pants"]
        for slot, p in prompts.items():
            assert p.count(clothing_desc) == 1, f"Duplicate clothing_desc in {slot}"
            assert "Current weather attire" not in p

        # (c) Cloudy/rainy afternoon prompts do NOT emit 'Vibrant clear afternoon daylight'
        cloudy_afternoon = hdwa.build_slot_prompt(
            slot="afternoon",
            theme=theme,
            weather_info={"temp_f": 56.0, "condition": "cloudy", "precipitation": 0.0, "precipitation_probability": 10.0},
        )
        assert "Vibrant clear afternoon daylight" not in cloudy_afternoon
        assert "2:00 PM" in cloudy_afternoon
        assert "overcast" in cloudy_afternoon.lower()

        rainy_afternoon = hdwa.build_slot_prompt(
            slot="afternoon",
            theme=theme,
            weather_info={"temp_f": 54.0, "condition": "rainy", "precipitation": 0.2, "precipitation_probability": 90.0},
        )
        assert "Vibrant clear afternoon daylight" not in rainy_afternoon
        assert "2:00 PM" in rainy_afternoon
        assert "rain" in rainy_afternoon.lower()

    def test_build_slot_prompt_structure_and_lighting(self) -> None:
        """Verify build_slot_prompt generates cohesive prompts with correct slot lighting."""
        theme = hdwa.ART_STYLE_VAULT["studio_ghibli_gouache"]
        weather = {"clothing_state": "morning_mild", "temp_f": 58.0}

        for slot, expected_lighting in [
            ("morning", "8:00 AM dawn ambiance"),
            ("afternoon", "2:00 PM sun"),
            ("evening", "6:00 PM dusk"),
        ]:
            prompt = hdwa.build_slot_prompt(slot=slot, theme=theme, weather_info=weather)
            assert expected_lighting in prompt
            assert "Studio Ghibli watercolor and gouache" in prompt
            assert hdwa.BASE_CHARACTER_CONSISTENCY_CLAUSE in prompt
            assert "golden-blonde hair" in prompt
            assert "light blue-green eyes" in prompt
            assert "RIGHT 42%" in prompt
            assert "LEFT 58%" in prompt
            assert "16:9 widescreen" in prompt

    def test_build_slot_prompt_ocarina_of_time_fidelity(self) -> None:
        """Verify exact Switch title fidelity for Ocarina of Time in prompt flavor."""
        oot_theme = hdwa.ART_STYLE_VAULT["zelda_oot_kokiri_storybook"]
        weather = {"clothing_state": "morning_mild", "temp_f": 62.0}

        prompt = hdwa.build_slot_prompt(slot="morning", theme=oot_theme, weather_info=weather)

        assert "Kokiri Forest" in prompt
        assert "Great Deku Tree" in prompt
        assert "glowing friendly winged fairy (Navi)" in prompt
        assert "wooden boardwalk footbridges" in prompt

        # Non-OOT theme should NOT have the OOT special flavor clause
        botw_theme = hdwa.ART_STYLE_VAULT["zelda_botw_watercolor"]
        botw_prompt = hdwa.build_slot_prompt(slot="morning", theme=botw_theme, weather_info=weather)
        assert "glowing friendly winged fairy (Navi)" not in botw_prompt

    def test_build_slot_prompt_weather_atmosphere_and_attire_override(self) -> None:
        """Verify build_slot_prompt embeds actual forecast condition/temp and attire override mandate."""
        oot_theme = hdwa.ART_STYLE_VAULT["zelda_oot_kokiri_storybook"]
        rainy_weather = {
            "temp_f": 53.0,
            "condition": "rainy",
            "precipitation": 0.18,
            "precipitation_probability": 85.0,
            "clothing_state": "morning_rain",
        }
        rain_prompt = hdwa.build_slot_prompt(slot="morning", theme=oot_theme, weather_info=rainy_weather)

        assert "MANDATORY WEATHER-MATCHED ATTIRE (morning_rain)" in rain_prompt
        assert rain_prompt.index("MANDATORY WEATHER-MATCHED ATTIRE") < rain_prompt.index("Setting:")
        assert "Forecast Weather (rainy, 53°F, precipitation 0.18 in, precipitation probability 85%)" in rain_prompt
        assert "overcast rain showers" in rain_prompt
        assert "cool crisp air" in rain_prompt
        assert "raincoats/umbrella/boots on wet forecasts" in rain_prompt
        assert "rather than copying clothing from the reference image or wearing generic fantasy costumes" in rain_prompt
        assert "hooded rain slickers" in rain_prompt

        botw_theme = hdwa.ART_STYLE_VAULT["zelda_botw_watercolor"]
        cold_weather = {
            "temp_f": 42.0,
            "condition": "clear",
            "precipitation": 0.0,
            "precipitation_probability": 0.0,
        }
        cold_prompt = hdwa.build_slot_prompt(slot="evening", theme=botw_theme, weather_info=cold_weather)
        assert "MANDATORY WEATHER-MATCHED ATTIRE (cold_puffer)" in cold_prompt
        assert "Forecast Weather (clear, 42°F)" in cold_prompt
        assert "chilly brisk air" in cold_prompt
        assert "clear twilight skies" in cold_prompt
        assert "quilted down puffer coats" in cold_prompt

    def test_all_vault_styles_have_scenes_and_mediums(self) -> None:
        """Ensure all 19+ vault styles have required metadata, distinct scene flavors, and left-58% Easter eggs."""
        assert len(hdwa.ART_STYLE_VAULT) >= 19
        for key, style in hdwa.ART_STYLE_VAULT.items():
            assert "visual_medium" in style, f"Missing visual_medium in {key}"
            assert "name" in style, f"Missing name in {key}"
            assert "prompt_style" in style, f"Missing prompt_style in {key}"
            assert "scene_flavor" in style, f"Missing scene_flavor in {key}"
            assert "easter_eggs" in style, f"Missing easter_eggs in {key}"
            assert "context_tags" in style, f"Missing context_tags in {key}"
            assert "subtle" not in style["prompt_style"].lower(), f"Hedged 'subtle' phrasing found in {key}"
            scenes = set()
            for slot in ("morning", "afternoon", "evening"):
                assert slot in style["scene_flavor"], f"Missing {slot} in scene_flavor for {key}"
                assert style["scene_flavor"][slot].strip(), f"Empty {slot} scene_flavor for {key}"
                scenes.add(style["scene_flavor"][slot].strip())
                assert slot in style["easter_eggs"], f"Missing {slot} in easter_eggs for {key}"
                egg = style["easter_eggs"][slot].strip()
                assert egg, f"Empty {slot} easter_egg for {key}"
                assert "LEFT 58%" in egg, f"Easter egg for {key}:{slot} must target LEFT 58%"
            assert len(scenes) == 3, f"Expected 3 distinct sub-locations in {key}"

        spider_style = hdwa.ART_STYLE_VAULT["spiderverse_halftone"]["prompt_style"]
        assert "subtle" not in spider_style.lower()
        assert "Ben-Day halftone" in spider_style
        assert "chromatic aberration" in spider_style


# ===========================================================================
# 2. Clothing State Mapping Tests
# ===========================================================================
class TestClothingStateMapping:
    """Verifies mapping of temperature, condition, and precipitation to clothing states."""

    def test_rain_conditions_mapping(self) -> None:
        """Verify rainy conditions map to morning_rain (morning) or rainy_boots (other)."""
        # Explicit condition keyword
        assert hdwa.map_weather_to_clothing_state("morning", 60.0, "Light rain") == "morning_rain"
        assert hdwa.map_weather_to_clothing_state("afternoon", 60.0, "Rain shower") == "rainy_boots"
        assert hdwa.map_weather_to_clothing_state("evening", 55.0, "Thunderstorm") == "rainy_boots"

        # Precipitation amount threshold (> 0.04)
        assert hdwa.map_weather_to_clothing_state("morning", 65.0, "cloudy", precip=0.08) == "morning_rain"
        assert hdwa.map_weather_to_clothing_state("afternoon", 65.0, "cloudy", precip=0.05) == "rainy_boots"

        # Precipitation probability threshold (>= 40.0)
        assert hdwa.map_weather_to_clothing_state("morning", 68.0, "partlycloudy", precip_prob=45.0) == "morning_rain"
        assert hdwa.map_weather_to_clothing_state("afternoon", 68.0, "partlycloudy", precip_prob=50.0) == "rainy_boots"

    def test_cold_bracket_mapping(self) -> None:
        """Verify cold temperatures (<= 48F) map to cold_puffer for all slots."""
        for slot in ("morning", "afternoon", "evening"):
            assert hdwa.map_weather_to_clothing_state(slot, 42.0, "clear") == "cold_puffer"
            assert hdwa.map_weather_to_clothing_state(slot, 48.0, "sunny") == "cold_puffer"

    def test_hot_bracket_mapping(self) -> None:
        """Verify hot temperatures (>= 75F) map to hot_dress for all slots."""
        for slot in ("morning", "afternoon", "evening"):
            assert hdwa.map_weather_to_clothing_state(slot, 75.0, "sunny") == "hot_dress"
            assert hdwa.map_weather_to_clothing_state(slot, 88.0, "partlycloudy") == "hot_dress"

    def test_temperate_bracket_mapping(self) -> None:
        """Verify mild / temperate temperatures (49F - 74F) respect slot nuances."""
        # Morning: < 52F is cold_puffer, >= 52F is morning_mild
        assert hdwa.map_weather_to_clothing_state("morning", 50.0, "sunny") == "cold_puffer"
        assert hdwa.map_weather_to_clothing_state("morning", 54.0, "sunny") == "morning_mild"

        # Afternoon: defaults to warm_pants
        assert hdwa.map_weather_to_clothing_state("afternoon", 65.0, "sunny") == "warm_pants"

        # Evening: <= 62F is cool_evening, > 62F is warm_pants
        assert hdwa.map_weather_to_clothing_state("evening", 58.0, "clear") == "cool_evening"
        assert hdwa.map_weather_to_clothing_state("evening", 62.0, "clear") == "cool_evening"
        assert hdwa.map_weather_to_clothing_state("evening", 68.0, "clear") == "warm_pants"

    def test_all_seven_clothing_state_prompts_configured(self) -> None:
        """Ensure all 7 clothing states exist and describe all three siblings."""
        expected_states = {
            "morning_rain",
            "rainy_boots",
            "cold_puffer",
            "morning_mild",
            "cool_evening",
            "warm_pants",
            "hot_dress",
        }
        assert set(hdwa.CLOTHING_STATE_PROMPTS.keys()) == expected_states
        for state, desc in hdwa.CLOTHING_STATE_PROMPTS.items():
            assert "Rosie" in desc, f"{state} prompt missing Rosie"
            assert "Isaac" in desc, f"{state} prompt missing Isaac"
            assert "Genevieve" in desc, f"{state} prompt missing Genevieve"


# ===========================================================================
# 3. Family Context Collector Tests (RFC 5737 Compliant)
# ===========================================================================
class TestFamilyContextCollector:
    """Verifies context collection from Switch, Tautulli, Steam, and memory profiles."""

    def test_collect_family_context_empty_fallback(self) -> None:
        """Verify collect_family_context gracefully handles non-existent files."""
        with patch.object(hdwa, "DATA_DIR", Path("/tmp/non_existent_data_dir")):
            ctx = hdwa.collect_family_context()
            assert isinstance(ctx, dict)
            assert "switch" in ctx
            assert "matched_themes" in ctx
            assert "special_notes" in ctx
            assert isinstance(ctx["matched_themes"], list)

    def test_collect_switch_json_ocarina_of_time(self) -> None:
        """Verify exact Switch title match for Ocarina of Time triggers special theme and notes."""
        with TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            switch_json = tmp_path / "switch_data.json"
            switch_payload = {
                "devices": [
                    {
                        "dailySummaries": [
                            {
                                "playedApps": [
                                    {"application": {"title": "The Legend of Zelda: Ocarina of Time"}},
                                    {"application": {"title": "Super Mario Odyssey"}},
                                ]
                            }
                        ]
                    }
                ]
            }
            switch_json.write_text(json.dumps(switch_payload), encoding="utf-8")

            with patch.object(hdwa, "DATA_DIR", tmp_path):
                ctx = hdwa.collect_family_context()

            assert "The Legend of Zelda: Ocarina of Time" in ctx["switch"]
            assert "Super Mario Odyssey" in ctx["switch"]
            assert "zelda_oot_kokiri_storybook" in ctx["matched_themes"]
            assert "pixar_storybook_light" in ctx["matched_themes"]
            assert any("Ocarina of Time" in note for note in ctx["special_notes"])
            assert any("Kokiri Forest" in note for note in ctx["special_notes"])

    def test_collect_switch_markdown_profiles(self) -> None:
        """Verify markdown profile parser extracts titles and matches themes."""
        raw_markdown = (
            "## Currently Played Switch Titles\n"
            "- **The Legend of Zelda: Tears of the Kingdom**: Exploring Hyrule depths\n"
            "- **The Legend of Zelda: Link's Awakening**: Charming Koholint adventure\n"
            "## Completed Titles\n"
        )

        with (
            patch("pathlib.Path.exists", autospec=True) as mock_exists,
            patch("pathlib.Path.read_text", autospec=True) as mock_read_text,
        ):
            def fake_exists(p: Path) -> bool:
                return str(p).endswith("media_gaming_switch_profile.md")

            def fake_read_text(p: Path, encoding: str = "utf-8") -> str:
                if str(p).endswith("media_gaming_switch_profile.md"):
                    return raw_markdown
                return ""

            mock_exists.side_effect = fake_exists
            mock_read_text.side_effect = fake_read_text

            ctx = hdwa.collect_family_context()

        assert "zelda_botw_watercolor" in ctx["matched_themes"]
        assert "links_awakening_diorama" in ctx["matched_themes"]

    def test_collect_steam_plex_and_book_profiles(self) -> None:
        """Verify Steam, Plex, and book profiles extract expected matched themes."""
        sample_profiles = {
            "media_gaming_steam_profile.md": "Playing Chrono Trigger and retro SNES pixel classics.",
            "media_plex_taste_profile.md": "Family movie night: watched My Neighbor Totoro and Studio Ghibli favorites.",
            "media_and_podcasts.md": "Reading Calvin and Hobbes treasury and Nordic folk tales.",
        }

        with TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)

            with (
                patch.object(hdwa, "DATA_DIR", tmp_path),
                patch("pathlib.Path.exists", autospec=True) as mock_exists,
                patch("pathlib.Path.read_text", autospec=True) as mock_read_text,
            ):
                def fake_exists(p: Path) -> bool:
                    filename = p.name
                    return filename in sample_profiles

                def fake_read_text(p: Path, encoding: str = "utf-8") -> str:
                    filename = p.name
                    return sample_profiles.get(filename, "")

                mock_exists.side_effect = fake_exists
                mock_read_text.side_effect = fake_read_text

                ctx = hdwa.collect_family_context()

            assert "snes_pixel_storybook" in ctx["matched_themes"]
            assert "studio_ghibli_gouache" in ctx["matched_themes"]
            assert "calvin_hobbes_ink_wash" in ctx["matched_themes"]
            assert "nordic_folk_storybook" in ctx["matched_themes"]

            # Deduplication check
            assert len(ctx["matched_themes"]) == len(set(ctx["matched_themes"]))


# ===========================================================================
# 4. Art Style Selection & 12-Day Cooldown Tests
# ===========================================================================
class TestThemeSelectionAndCooldown:
    """Verifies 12-day cooldown enforcement, forced CLI theme, and 60/40 weighting."""

    def test_forced_theme_selection(self) -> None:
        """Verify forced theme parameter bypasses cooldown and returns immediately."""
        theme, reason = hdwa.select_daily_theme(
            family_context={"matched_themes": []},
            target_date="2026-11-20",
            forced_theme="studio_ghibli_gouache",
        )
        assert theme["visual_medium"] == "studio_ghibli_gouache"
        assert "forced_cli" in reason

    def test_forced_theme_invalid_raises_value_error(self) -> None:
        """Verify invalid forced theme raises ValueError."""
        with pytest.raises(ValueError, match="Unknown theme 'non_existent_medium'"):
            hdwa.select_daily_theme(
                family_context={"matched_themes": []},
                target_date="2026-11-20",
                forced_theme="non_existent_medium",
            )

    def test_12_day_cooldown_excludes_recent_themes(self) -> None:
        """Verify themes selected within the last 12 days are excluded from selection."""
        with TemporaryDirectory() as tmp_dir:
            history_file = Path(tmp_dir) / "theme_history.json"
            target_date = "2026-11-20"
            target_dt = datetime.strptime(target_date, "%Y-%m-%d").date()

            # Create history entries:
            # - Entry 5 days ago: studio_ghibli_gouache (should be cooled down)
            # - Entry 10 days ago: zelda_botw_watercolor (should be cooled down)
            # - Entry 15 days ago: snes_pixel_storybook (eligible)
            history = [
                {
                    "date": (target_dt - timedelta(days=5)).isoformat(),
                    "theme_key": "studio_ghibli_gouache",
                    "visual_medium": "studio_ghibli_gouache",
                },
                {
                    "date": (target_dt - timedelta(days=10)).isoformat(),
                    "theme_key": "zelda_botw_watercolor",
                    "visual_medium": "zelda_botw_watercolor",
                },
                {
                    "date": (target_dt - timedelta(days=15)).isoformat(),
                    "theme_key": "snes_pixel_storybook",
                    "visual_medium": "snes_pixel_storybook",
                },
            ]
            history_file.write_text(json.dumps(history), encoding="utf-8")

            # Run 50 selections with context matching the cooled-down ghibli theme
            # Ghibli must never be chosen because it is on 12-day cooldown
            for seed in range(50):
                theme, _ = hdwa.select_daily_theme(
                    family_context={"matched_themes": ["studio_ghibli_gouache"]},
                    target_date=target_date,
                    seed=seed,
                    history_file=history_file,
                )
                assert theme["visual_medium"] != "studio_ghibli_gouache"
                assert theme["visual_medium"] != "zelda_botw_watercolor"

    def test_60_40_context_selection_weighting(self) -> None:
        """Verify context candidates are preferred when roll < 0.60 and available."""
        with TemporaryDirectory() as tmp_dir:
            history_file = Path(tmp_dir) / "theme_history.json"
            history_file.write_text(json.dumps([]), encoding="utf-8")

            # Mock random.Random to return roll < 0.60
            with patch("random.Random") as mock_rng_cls:
                mock_rng = MagicMock()
                mock_rng.random.return_value = 0.45  # < 0.60
                mock_rng.choice.side_effect = lambda seq: seq[0]
                mock_rng_cls.return_value = mock_rng

                theme, reason = hdwa.select_daily_theme(
                    family_context={"matched_themes": ["calvin_hobbes_ink_wash"]},
                    target_date="2026-11-20",
                    history_file=history_file,
                )
                assert theme["visual_medium"] == "calvin_hobbes_ink_wash"
                assert "family_context" in reason

            # Mock random.Random to return roll >= 0.60
            with patch("random.Random") as mock_rng_cls:
                mock_rng = MagicMock()
                mock_rng.random.return_value = 0.75  # >= 0.60
                mock_rng.choice.side_effect = lambda seq: seq[0]
                mock_rng_cls.return_value = mock_rng

                theme, reason = hdwa.select_daily_theme(
                    family_context={"matched_themes": ["calvin_hobbes_ink_wash"]},
                    target_date="2026-11-20",
                    history_file=history_file,
                )
                assert "vault_rotation" in reason

    def test_all_themes_on_cooldown_fallback(self) -> None:
        """Verify that if all vault themes are on cooldown, the pool resets safely."""
        with TemporaryDirectory() as tmp_dir:
            history_file = Path(tmp_dir) / "theme_history.json"
            target_date = "2026-11-20"
            target_dt = datetime.strptime(target_date, "%Y-%m-%d").date()

            # Put every single vault theme in history within the last 5 days
            history = []
            for i, (key, style) in enumerate(hdwa.ART_STYLE_VAULT.items()):
                history.append({
                    "date": (target_dt - timedelta(days=(i % 10) + 1)).isoformat(),
                    "theme_key": key,
                    "visual_medium": style["visual_medium"],
                })
            history_file.write_text(json.dumps(history), encoding="utf-8")

            theme, reason = hdwa.select_daily_theme(
                family_context={"matched_themes": []},
                target_date=target_date,
                seed=42,
                history_file=history_file,
            )
            assert theme["visual_medium"] in hdwa.ART_STYLE_VAULT
            assert "vault_rotation" in reason

    def test_record_theme_history_atomic(self) -> None:
        """Verify record_theme_history writes and updates entries cleanly."""
        with TemporaryDirectory() as tmp_dir:
            hist_file = Path(tmp_dir) / "history.json"
            hdwa.record_theme_history(
                target_date="2026-11-20",
                theme_key="studio_ghibli_gouache",
                visual_medium="studio_ghibli_gouache",
                context_source="unit_test",
                history_file=hist_file,
            )

            records = hdwa.load_theme_history(hist_file)
            assert len(records) == 1
            assert records[0]["date"] == "2026-11-20"
            assert records[0]["theme_key"] == "studio_ghibli_gouache"

            # Update entry for the same date
            hdwa.record_theme_history(
                target_date="2026-11-20",
                theme_key="snes_pixel_storybook",
                visual_medium="snes_pixel_storybook",
                context_source="unit_test_update",
                history_file=hist_file,
            )
            records2 = hdwa.load_theme_history(hist_file)
            assert len(records2) == 1
            assert records2[0]["theme_key"] == "snes_pixel_storybook"


# ===========================================================================
# 5. Forecast Retrieval & Seasonal Fallback Tests
# ===========================================================================
class TestForecastRetrieval:
    """Verifies Home Assistant forecast retrieval and seasonal fallback."""

    def test_fetch_tomorrow_slots_forecast_seasonal_fallback(self) -> None:
        """Verify seasonal fallback populates morning, afternoon, and evening slots."""
        with patch.object(hdwa, "_get_ha_credentials", return_value=("", "")):
            data = hdwa.fetch_tomorrow_slots_forecast(target_date="2026-11-20")

            assert data["target_date"] == "2026-11-20"
            assert data["source"] == "seasonal_estimate"
            slots = data["slots"]
            assert set(slots.keys()) == {"morning", "afternoon", "evening"}

            # November is winter fallback in Northern California:
            # morning: 46F -> cold_puffer
            # afternoon: 58F -> warm_pants
            # evening: 50F -> cool_evening
            assert slots["morning"]["temp_f"] == 46.0
            assert slots["morning"]["clothing_state"] == "cold_puffer"

            assert slots["afternoon"]["temp_f"] == 58.0
            assert slots["afternoon"]["clothing_state"] == "warm_pants"

            assert slots["evening"]["temp_f"] == 50.0
            assert slots["evening"]["clothing_state"] == "cool_evening"

    def test_fetch_tomorrow_slots_forecast_ha_mocked(self) -> None:
        """Verify hourly forecast parser extracts correct slot temperatures from Home Assistant."""
        target_date = "2026-11-20"
        mock_ha_payload = {
            "weather.forecast_home": {
                "forecast": [
                    {
                        "datetime": f"{target_date}T08:00:00-08:00",
                        "temperature": 55.0,
                        "condition": "sunny",
                        "precipitation": 0.0,
                        "precipitation_probability": 0.0,
                    },
                    {
                        "datetime": f"{target_date}T14:00:00-08:00",
                        "temperature": 78.0,
                        "condition": "sunny",
                        "precipitation": 0.0,
                        "precipitation_probability": 0.0,
                    },
                    {
                        "datetime": f"{target_date}T18:00:00-08:00",
                        "temperature": 59.0,
                        "condition": "rainy",
                        "precipitation": 0.12,
                        "precipitation_probability": 85.0,
                    },
                ]
            }
        }

        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.read.return_value = json.dumps(mock_ha_payload).encode("utf-8")
        mock_resp.__enter__.return_value = mock_resp

        # Use RFC 5737 documentation IP for mocked HA endpoint
        with (
            patch.object(hdwa, "_get_ha_credentials", return_value=("http://198.51.100.10:8123", "mock_token")),
            patch("urllib.request.urlopen", return_value=mock_resp),
        ):
            fc = hdwa.fetch_tomorrow_slots_forecast(target_date=target_date)

        assert fc["source"] == "ha_forecast"
        slots = fc["slots"]
        assert slots["morning"]["temp_f"] == 55.0
        assert slots["morning"]["clothing_state"] == "morning_mild"

        assert slots["afternoon"]["temp_f"] == 78.0
        assert slots["afternoon"]["clothing_state"] == "hot_dress"

        assert slots["evening"]["temp_f"] == 59.0
        assert slots["evening"]["clothing_state"] == "rainy_boots"

    def test_fetch_tomorrow_slots_forecast_service_response_and_utc_conversion(self) -> None:
        """Verify HA service_response unwrapping and UTC-to-PT datetime conversion (including UTC date rollover)."""
        target_date = "2026-11-20"
        mock_ha_service_payload = {
            "service_response": {
                "weather.forecast_home": {
                    "forecast": [
                        {
                            # 2026-11-20 16:00 UTC -> 2026-11-20 08:00 PST (morning)
                            "datetime": "2026-11-20T16:00:00+00:00",
                            "temperature": 45.0,
                            "condition": "cloudy",
                            "precipitation": 0.0,
                            "precipitation_probability": 15.0,
                        },
                        {
                            # 2026-11-20 22:00 UTC -> 2026-11-20 14:00 PST (afternoon)
                            "datetime": "2026-11-20T22:00:00Z",
                            "temperature": 61.0,
                            "condition": "partlycloudy",
                            "precipitation": 0.0,
                            "precipitation_probability": 10.0,
                        },
                        {
                            # 2026-11-21 02:00 UTC -> 2026-11-20 18:00 PST (evening)
                            "datetime": "2026-11-21T02:00:00+00:00",
                            "temperature": 51.0,
                            "condition": "pouring",
                            "precipitation": 0.25,
                            "precipitation_probability": 95.0,
                        },
                    ]
                }
            }
        }

        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.read.return_value = json.dumps(mock_ha_service_payload).encode("utf-8")
        mock_resp.__enter__.return_value = mock_resp

        with (
            patch.object(hdwa, "_get_ha_credentials", return_value=("http://198.51.100.10:8123", "mock_token")),
            patch("urllib.request.urlopen", return_value=mock_resp),
        ):
            fc = hdwa.fetch_tomorrow_slots_forecast(target_date=target_date)

        assert fc["source"] == "ha_forecast"
        slots = fc["slots"]
        assert slots["morning"]["temp_f"] == 45.0
        assert slots["morning"]["condition"] == "cloudy"
        assert slots["morning"]["clothing_state"] == "cold_puffer"

        assert slots["afternoon"]["temp_f"] == 61.0
        assert slots["afternoon"]["condition"] == "partlycloudy"
        assert slots["afternoon"]["clothing_state"] == "warm_pants"

        assert slots["evening"]["temp_f"] == 51.0
        assert slots["evening"]["condition"] == "pouring"
        assert slots["evening"]["precipitation"] == 0.25
        assert slots["evening"]["precipitation_probability"] == 95.0
        assert slots["evening"]["clothing_state"] == "rainy_boots"


# ===========================================================================
# 6. Schedule Registration Tests
# ===========================================================================
class TestScheduleInstallation:
    """Verifies daily 6:30 PM PT schedule registration and idempotency."""

    def test_install_schedule_creates_new_file(self) -> None:
        """Verify schedule installation creates schedule.json if absent."""
        with TemporaryDirectory() as tmp_dir:
            sched_file = Path(tmp_dir) / "schedule.json"
            ok, msg = hdwa.install_schedule_entry(sched_file)

            assert ok is True
            assert "Successfully registered" in msg
            assert sched_file.exists()

            data = json.loads(sched_file.read_text(encoding="utf-8"))
            assert len(data) == 1
            entry = data[0]
            assert entry["id"] == "hearth_daily_weather_art"
            assert entry["hour_pt"] == 18
            assert entry["minute_pt"] == 30
            assert entry["enabled"] is True
            assert "/workspace/tools/sidecars.py hearth_weather_art" in entry["prompt"]
            assert "silent" in entry["prompt"].lower()
            assert entry.get("channel_id") == 1544953275877556334

    def test_install_schedule_idempotent_and_preserves_other_jobs(self) -> None:
        """Verify running install_schedule multiple times updates in-place and preserves jobs."""
        with TemporaryDirectory() as tmp_dir:
            sched_file = Path(tmp_dir) / "schedule.json"
            existing_jobs = [
                {"id": "existing_cron_job", "name": "Existing Cron", "enabled": True},
            ]
            sched_file.write_text(json.dumps(existing_jobs), encoding="utf-8")

            # First run
            ok1, _ = hdwa.install_schedule_entry(sched_file)
            assert ok1 is True
            data1 = json.loads(sched_file.read_text(encoding="utf-8"))
            assert len(data1) == 2
            assert any(j["id"] == "existing_cron_job" for j in data1)
            assert any(j["id"] == "hearth_daily_weather_art" for j in data1)

            # Second run (idempotency check)
            ok2, _ = hdwa.install_schedule_entry(sched_file)
            assert ok2 is True
            data2 = json.loads(sched_file.read_text(encoding="utf-8"))
            assert len(data2) == 2


# ===========================================================================
# 7. Host 2 Deployment Tests
# ===========================================================================
class TestDeploymentToHost2:
    """Verifies deploy_to_host2 scp parameters, validation, and remote pruning."""

    def test_deploy_to_host2_missing_file_fails(self) -> None:
        """Verify deployment fails immediately if a local file is missing."""
        missing = Path("/tmp/non_existent_file_12345.jpg")
        ok, msg = hdwa.deploy_to_host2([missing])
        assert ok is False
        assert "File does not exist" in msg

    def test_deploy_to_host2_success(self) -> None:
        """Verify scp -O, pre-upload chmod, and remote prune execution with mocked subprocess and ssh."""
        with TemporaryDirectory() as tmp_dir:
            test_file = Path(tmp_dir) / "daily_test.jpg"
            test_file.write_text("dummy_image_data", encoding="utf-8")
            test_file.chmod(0o444)  # Start with read-only permissions

            mock_proc = MagicMock()
            mock_proc.returncode = 0
            mock_proc.stdout = ""
            mock_proc.stderr = ""

            with (
                patch("subprocess.run", return_value=mock_proc) as mock_subproc,
                patch.object(hdwa.nas_config, "run_ssh", return_value=(0, "ok", "")) as mock_ssh,
            ):
                ok, msg = hdwa.deploy_to_host2([test_file])

            assert ok is True
            assert "Successfully uploaded" in msg

            # Verify local file permissions updated to 0644
            mode = stat.S_IMODE(test_file.stat().st_mode)
            assert mode == 0o644

            # Verify scp command structure
            mock_subproc.assert_called_once()
            cmd_args = mock_subproc.call_args[0][0]
            assert cmd_args[0] == "scp"
            assert "-O" in cmd_args
            assert str(test_file.resolve()) in cmd_args

            # Verify pre-upload chmod and remote prune called via SSH
            assert mock_ssh.call_count == 2
            pre_cmd = mock_ssh.call_args_list[0][0][1]
            assert "chmod -R u+w" in pre_cmd
            prune_cmd = mock_ssh.call_args_list[1][0][1]
            assert "daily_*.jpg" in prune_cmd
            assert "-mtime +3 -delete" in prune_cmd

    def test_deploy_to_host2_chmod_read_only_local_files(self) -> None:
        """Verify deploy_to_host2 sets 0644 writable permissions on all input files."""
        with TemporaryDirectory() as tmp_dir:
            f1 = Path(tmp_dir) / "manifest.json"
            f2 = Path(tmp_dir) / "daily_morning.jpg"
            f1.write_text("{}", encoding="utf-8")
            f2.write_text("fake_jpg", encoding="utf-8")
            f1.chmod(0o400)
            f2.chmod(0o555)

            mock_proc = MagicMock()
            mock_proc.returncode = 0

            with (
                patch("subprocess.run", return_value=mock_proc),
                patch.object(hdwa.nas_config, "run_ssh", return_value=(0, "", "")),
            ):
                ok, _ = hdwa.deploy_to_host2([f1, f2])

            assert ok is True
            assert stat.S_IMODE(f1.stat().st_mode) == 0o644
            assert stat.S_IMODE(f2.stat().st_mode) == 0o644

    def test_deploy_to_host2_pre_upload_chmod_failure_resilient(self) -> None:
        """Verify deploy_to_host2 continues upload even if pre-upload remote chmod logs a non-zero exit."""
        with TemporaryDirectory() as tmp_dir:
            f = Path(tmp_dir) / "test.jpg"
            f.write_text("data", encoding="utf-8")

            mock_proc = MagicMock()
            mock_proc.returncode = 0

            # First run_ssh (pre_chmod) returns rc=1, second (prune) returns rc=0
            ssh_returns = [(1, "", "chmod: directory does not exist"), (0, "", "")]

            with (
                patch("subprocess.run", return_value=mock_proc) as mock_subproc,
                patch.object(hdwa.nas_config, "run_ssh", side_effect=ssh_returns) as mock_ssh,
            ):
                ok, msg = hdwa.deploy_to_host2([f])

            assert ok is True
            assert "Successfully uploaded" in msg
            assert mock_subproc.call_count == 1
            assert mock_ssh.call_count == 2

    def test_deploy_to_host2_uses_host2_docker_root(self) -> None:
        """Verify deploy_to_host2 targets nas_config.HOST_2_DOCKER_ROOT rather than DOCKER_ROOT."""
        with TemporaryDirectory() as tmp_dir:
            f = Path(tmp_dir) / "daily_morning.jpg"
            f.write_text("data", encoding="utf-8")

            mock_proc = MagicMock()
            mock_proc.returncode = 0

            with (
                patch.object(hdwa.nas_config, "DOCKER_ROOT", "/srv/test-h1-docker"),
                patch.object(hdwa.nas_config, "HOST_2_DOCKER_ROOT", "/srv/test-h2-docker"),
                patch("subprocess.run", return_value=mock_proc) as mock_subproc,
                patch.object(hdwa.nas_config, "run_ssh", return_value=(0, "", "")) as mock_ssh,
            ):
                ok, _ = hdwa.deploy_to_host2([f])

            assert ok is True
            scp_dest = mock_subproc.call_args[0][0][-1]
            assert f"/srv/test-h2-docker/{hdwa.HEARTH_REMOTE_REL_PATH}/" in scp_dest
            assert "/srv/test-h1-docker/" not in scp_dest
            pre_cmd = mock_ssh.call_args_list[0][0][1]
            prune_cmd = mock_ssh.call_args_list[1][0][1]
            assert f"/srv/test-h2-docker/{hdwa.HEARTH_REMOTE_REL_PATH}" in pre_cmd
            assert f"/srv/test-h2-docker/{hdwa.HEARTH_REMOTE_REL_PATH}" in prune_cmd


# ===========================================================================
# 8. End-to-End Pipeline Execution Tests
# ===========================================================================
class TestRunNightlyPipeline:
    """Verifies run_nightly_pipeline in dry-run, success, and error paths."""

    def test_run_nightly_pipeline_dry_run_manifest_schema(self) -> None:
        """Verify --dry-run mode creates valid daily_manifest.json with all schema fields."""
        with TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            out_dir = tmp_path / "output"
            hist_file = tmp_path / "history.json"

            result = hdwa.run_nightly_pipeline(
                target_date="2026-11-20",
                theme="studio_ghibli_gouache",
                output_dir=str(out_dir),
                dry_run=True,
                history_file=hist_file,
            )

            assert result["success"] is True
            assert result["target_date"] == "2026-11-20"
            manifest_path = Path(result["manifest_path"])
            assert manifest_path.exists()

            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

            # Verify top-level manifest schema
            assert manifest["target_date"] == "2026-11-20"
            assert manifest["date"] == "2026-11-20"
            assert manifest["style_label"] == hdwa.ART_STYLE_VAULT["studio_ghibli_gouache"]["name"]
            assert manifest["theme_title"] == hdwa.ART_STYLE_VAULT["studio_ghibli_gouache"]["name"]
            assert "generated_at" in manifest
            assert manifest["theme_key"] == "studio_ghibli_gouache"
            assert manifest["visual_medium"] == "studio_ghibli_gouache"
            assert manifest["dry_run"] is True
            assert "context_used" in manifest
            assert "slots" in manifest
            assert "slot_details" in manifest

            # Verify slot structures (Hearth web UI contract: string URLs in slots)
            slots = manifest["slots"]
            assert set(slots.keys()) == {"morning", "afternoon", "evening"}
            for slot_name in ("morning", "afternoon", "evening"):
                slot_url = slots[slot_name]
                assert isinstance(slot_url, str)
                assert f"daily_2026-11-20_{slot_name}.jpg" in slot_url
                assert slot_url.startswith("/static/ui/assets/weather-packs/")

            # Verify slot_details contains full metadata
            slot_details = manifest["slot_details"]
            assert set(slot_details.keys()) == {"morning", "afternoon", "evening"}
            for slot_name in ("morning", "afternoon", "evening"):
                slot_info = slot_details[slot_name]
                assert "hour" in slot_info
                assert "clothing_state" in slot_info
                assert "temp_f" in slot_info
                assert "condition" in slot_info
                assert "filename" in slot_info
                assert slot_info["filename"] == f"daily_2026-11-20_{slot_name}.jpg"
                assert "relative_url" in slot_info
                assert "prompt" in slot_info
                assert slot_info["valid"] is True
                assert slot_info["dry_run"] is True

                # Check character consistency invariants in prompt
                prompt = slot_info["prompt"]
                assert "golden-blonde hair" in prompt
                assert "light blue-green eyes" in prompt
                assert "Rosie: 7-year-old girl" in prompt
                assert "Isaac: 3-year-old toddler boy" in prompt
                assert "Genevieve (Eve): 3-month-old infant baby girl" in prompt
                assert "RIGHT 42%" in prompt
                assert "LEFT 58%" in prompt

    def test_run_nightly_pipeline_mocked_generation_and_deploy(self) -> None:
        """Verify full pipeline execution with mocked Gemini subscription and verification."""
        with TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            out_dir = tmp_path / "output"
            hist_file = tmp_path / "history.json"

            def fake_generate_image(prompt: str, **kwargs: Any) -> dict[str, Any]:
                image_name = kwargs.get("image_name", "test")
                target_p = out_dir / f"{image_name}.jpg"
                target_p.write_bytes(b"\xff\xd8\xff\xe0" + b"\x00" * 100)  # Dummy JPEG header
                return {"success": True, "path": str(target_p)}

            fake_check_image = MagicMock(return_value={
                "valid": True,
                "width": 1920,
                "height": 1080,
                "format": "JPEG",
                "size_kb": 256.0,
            })

            with (
                patch.object(hdwa, "generate_image_subscription", side_effect=fake_generate_image) as mock_gen,
                patch.object(hdwa, "check_image", fake_check_image),
                patch.object(hdwa, "deploy_to_host2", return_value=(True, "Synced 4 files")) as mock_deploy,
            ):
                result = hdwa.run_nightly_pipeline(
                    target_date="2026-11-20",
                    theme="zelda_oot_kokiri_storybook",
                    output_dir=str(out_dir),
                    dry_run=False,
                    no_deploy=False,
                    history_file=hist_file,
                )

            assert result["success"] is True
            assert mock_gen.call_count == 3
            assert fake_check_image.call_count == 3
            mock_deploy.assert_called_once()

            manifest = result["manifest"]
            assert manifest["target_date"] == "2026-11-20"
            assert isinstance(manifest["slots"]["morning"], str)
            assert manifest["slot_details"]["morning"]["valid"] is True
            assert manifest["slot_details"]["morning"]["width"] == 1920
            assert manifest["slot_details"]["morning"]["height"] == 1080
            assert manifest["slot_details"]["morning"]["format"] == "JPEG"

            # History must be recorded after successful run
            history = hdwa.load_theme_history(hist_file)
            assert len(history) == 1
            assert history[0]["visual_medium"] == "zelda_oot_kokiri_storybook"

    def test_run_nightly_pipeline_generation_failure_handling(self) -> None:
        """Verify pipeline handles image generation failure gracefully."""
        with TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            out_dir = tmp_path / "output"
            hist_file = tmp_path / "history.json"

            with (
                patch.object(hdwa, "generate_image_subscription", return_value={"success": False, "error": "API quota exceeded"}),
                patch.object(hdwa, "deploy_to_host2") as mock_deploy,
            ):
                result = hdwa.run_nightly_pipeline(
                    target_date="2026-11-20",
                    theme="studio_ghibli_gouache",
                    output_dir=str(out_dir),
                    dry_run=False,
                    history_file=hist_file,
                )

            assert result["success"] is False
            manifest = result["manifest"]
            assert manifest["slot_details"]["morning"]["valid"] is False
            assert "API quota exceeded" in manifest["slot_details"]["morning"]["error"]

            # Deployment must NOT be attempted if generation failed
            mock_deploy.assert_not_called()

            # Failed run should NOT record history
            assert len(hdwa.load_theme_history(hist_file)) == 0

    def test_run_nightly_pipeline_default_timeout_and_retries_constants(self) -> None:
        """Verify default timeout and retry constants."""
        assert hdwa.DEFAULT_IMAGE_TIMEOUT == 240
        assert hdwa.DEFAULT_MAX_RETRIES == 2

    def test_run_nightly_pipeline_retry_after_timeout_recovery(self) -> None:
        """Verify recovery after initial timeout via per-slot retry."""
        with TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            out_dir = tmp_path / "output"
            hist_file = tmp_path / "history.json"

            attempts: dict[str, int] = {"morning": 0, "afternoon": 0, "evening": 0}

            def fake_generate_image(prompt: str, **kwargs: Any) -> dict[str, Any]:
                image_name = kwargs.get("image_name", "")
                slot = "morning" if "morning" in image_name else ("afternoon" if "afternoon" in image_name else "evening")
                attempts[slot] += 1
                if slot == "morning" and attempts["morning"] == 1:
                    return {"success": False, "error": "Command timed out after 240s"}
                target_p = out_dir / f"{image_name}.jpg"
                target_p.write_bytes(b"\xff\xd8\xff\xe0" + b"\x00" * 100)
                return {"success": True, "path": str(target_p)}

            fake_check_image = MagicMock(return_value={
                "valid": True,
                "width": 1920,
                "height": 1080,
                "format": "JPEG",
                "size_kb": 256.0,
            })

            with (
                patch.object(hdwa, "generate_image_subscription", side_effect=fake_generate_image) as mock_gen,
                patch.object(hdwa, "check_image", fake_check_image),
                patch.object(hdwa, "deploy_to_host2", return_value=(True, "Synced")) as mock_deploy,
            ):
                result = hdwa.run_nightly_pipeline(
                    target_date="2026-11-20",
                    theme="studio_ghibli_gouache",
                    output_dir=str(out_dir),
                    dry_run=False,
                    history_file=hist_file,
                    timeout=240,
                    max_retries=2,
                )

            assert result["success"] is True
            assert mock_gen.call_count == 4
            assert attempts["morning"] == 2
            assert attempts["afternoon"] == 1
            assert attempts["evening"] == 1

            manifest = result["manifest"]
            assert manifest["slot_details"]["morning"]["valid"] is True
            assert manifest["slot_details"]["morning"]["retries"] == 1
            assert manifest["slot_details"]["afternoon"]["valid"] is True
            assert "retries" not in manifest["slot_details"]["afternoon"]

            mock_deploy.assert_called_once()
            assert len(hdwa.load_theme_history(hist_file)) == 1

    def test_run_nightly_pipeline_retry_exhaustion(self) -> None:
        """Verify pipeline failure when max_retries are exhausted."""
        with TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            out_dir = tmp_path / "output"
            hist_file = tmp_path / "history.json"

            with (
                patch.object(hdwa, "generate_image_subscription", return_value={"success": False, "error": "Persistent timeout"}) as mock_gen,
                patch.object(hdwa, "deploy_to_host2") as mock_deploy,
            ):
                result = hdwa.run_nightly_pipeline(
                    target_date="2026-11-20",
                    theme="studio_ghibli_gouache",
                    output_dir=str(out_dir),
                    dry_run=False,
                    history_file=hist_file,
                    max_retries=2,
                )

            assert result["success"] is False
            assert mock_gen.call_count == 9
            manifest = result["manifest"]
            assert manifest["slot_details"]["morning"]["valid"] is False
            assert "Persistent timeout" in manifest["slot_details"]["morning"]["error"]
            mock_deploy.assert_not_called()
            assert len(hdwa.load_theme_history(hist_file)) == 0

    def test_run_nightly_pipeline_same_date_theme_locking(self) -> None:
        """Verify resume on same date locks selected theme from existing manifest."""
        with TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            out_dir = tmp_path / "output"
            out_dir.mkdir(parents=True, exist_ok=True)
            hist_file = tmp_path / "history.json"

            existing_manifest = {
                "date": "2026-11-20",
                "theme_key": "snes_pixel_storybook",
                "slots": {},
            }
            (out_dir / "daily_manifest.json").write_text(json.dumps(existing_manifest), encoding="utf-8")

            result = hdwa.run_nightly_pipeline(
                target_date="2026-11-20",
                output_dir=str(out_dir),
                dry_run=True,
                history_file=hist_file,
            )

            assert result["success"] is True
            assert result["theme"] == hdwa.ART_STYLE_VAULT["snes_pixel_storybook"]["name"]
            assert result["manifest"]["theme_key"] == "snes_pixel_storybook"
            assert "theme_locked_resume" in result["manifest"]["selection_reason"]

    def test_run_nightly_pipeline_slot_reuse_on_resume(self) -> None:
        """Verify already-valid slots on disk are reused without re-invoking generation."""
        with TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            out_dir = tmp_path / "output"
            out_dir.mkdir(parents=True, exist_ok=True)
            hist_file = tmp_path / "history.json"

            morning_card = out_dir / "daily_2026-11-20_morning.jpg"
            morning_card.write_bytes(b"\xff\xd8\xff\xe0" + b"\x00" * 100)

            existing_manifest = {
                "date": "2026-11-20",
                "theme_key": "snes_pixel_storybook",
                "slots": {
                    "morning": {
                        "valid": True,
                        "filename": "daily_2026-11-20_morning.jpg",
                    },
                    "afternoon": {
                        "valid": False,
                        "error": "Prior failure",
                    },
                    "evening": {
                        "valid": False,
                        "error": "Prior failure",
                    },
                },
            }
            (out_dir / "daily_manifest.json").write_text(json.dumps(existing_manifest), encoding="utf-8")

            def fake_generate_image(prompt: str, **kwargs: Any) -> dict[str, Any]:
                image_name = kwargs.get("image_name", "")
                target_p = out_dir / f"{image_name}.jpg"
                target_p.write_bytes(b"\xff\xd8\xff\xe0" + b"\x00" * 100)
                return {"success": True, "path": str(target_p)}

            fake_check_image = MagicMock(return_value={
                "valid": True,
                "width": 1920,
                "height": 1080,
                "format": "JPEG",
                "size_kb": 256.0,
            })

            with (
                patch.object(hdwa, "generate_image_subscription", side_effect=fake_generate_image) as mock_gen,
                patch.object(hdwa, "check_image", fake_check_image),
                patch.object(hdwa, "deploy_to_host2", return_value=(True, "Synced")) as mock_deploy,
            ):
                result = hdwa.run_nightly_pipeline(
                    target_date="2026-11-20",
                    output_dir=str(out_dir),
                    dry_run=False,
                    history_file=hist_file,
                )

            assert result["success"] is True
            assert mock_gen.call_count == 2
            manifest = result["manifest"]
            assert manifest["slot_details"]["morning"]["valid"] is True
            assert manifest["slot_details"]["morning"]["reused"] is True
            assert manifest["slot_details"]["afternoon"]["valid"] is True
            assert manifest["slot_details"]["evening"]["valid"] is True
            assert isinstance(manifest["slots"]["morning"], str)
            mock_deploy.assert_called_once()
            deployed_files = mock_deploy.call_args[0][0]
            assert len(deployed_files) == 4

    def test_run_nightly_pipeline_invalidation_on_theme_mismatch(self) -> None:
        """Verify existing cards are NOT reused when theme changes/mismatches."""
        with TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            out_dir = tmp_path / "output"
            out_dir.mkdir(parents=True, exist_ok=True)
            hist_file = tmp_path / "history.json"

            morning_card = out_dir / "daily_2026-11-20_morning.jpg"
            morning_card.write_bytes(b"\xff\xd8\xff\xe0" + b"\x00" * 100)

            existing_manifest = {
                "date": "2026-11-20",
                "theme_key": "snes_pixel_storybook",
                "slots": {
                    "morning": {"valid": True, "filename": "daily_2026-11-20_morning.jpg"},
                },
            }
            (out_dir / "daily_manifest.json").write_text(json.dumps(existing_manifest), encoding="utf-8")

            def fake_generate_image(prompt: str, **kwargs: Any) -> dict[str, Any]:
                image_name = kwargs.get("image_name", "")
                target_p = out_dir / f"{image_name}.jpg"
                target_p.write_bytes(b"\xff\xd8\xff\xe0" + b"\x00" * 100)
                return {"success": True, "path": str(target_p)}

            fake_check_image = MagicMock(return_value={
                "valid": True,
                "width": 1920,
                "height": 1080,
                "format": "JPEG",
                "size_kb": 256.0,
            })

            with (
                patch.object(hdwa, "generate_image_subscription", side_effect=fake_generate_image) as mock_gen,
                patch.object(hdwa, "check_image", fake_check_image),
                patch.object(hdwa, "deploy_to_host2", return_value=(True, "Synced")),
            ):
                result = hdwa.run_nightly_pipeline(
                    target_date="2026-11-20",
                    theme="studio_ghibli_gouache",
                    output_dir=str(out_dir),
                    dry_run=False,
                    history_file=hist_file,
                )

            assert result["success"] is True
            assert result["manifest"]["theme_key"] == "studio_ghibli_gouache"
            assert mock_gen.call_count == 3
            assert result["manifest"]["slot_details"]["morning"].get("reused") is not True

    def test_run_nightly_pipeline_invalidation_on_force(self) -> None:
        """Verify force=True regenerates all slots and ignores existing valid cards."""
        with TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            out_dir = tmp_path / "output"
            out_dir.mkdir(parents=True, exist_ok=True)
            hist_file = tmp_path / "history.json"

            morning_card = out_dir / "daily_2026-11-20_morning.jpg"
            morning_card.write_bytes(b"\xff\xd8\xff\xe0" + b"\x00" * 100)

            existing_manifest = {
                "date": "2026-11-20",
                "theme_key": "studio_ghibli_gouache",
                "slots": {
                    "morning": {"valid": True, "filename": "daily_2026-11-20_morning.jpg"},
                },
            }
            (out_dir / "daily_manifest.json").write_text(json.dumps(existing_manifest), encoding="utf-8")

            def fake_generate_image(prompt: str, **kwargs: Any) -> dict[str, Any]:
                image_name = kwargs.get("image_name", "")
                target_p = out_dir / f"{image_name}.jpg"
                target_p.write_bytes(b"\xff\xd8\xff\xe0" + b"\x00" * 100)
                return {"success": True, "path": str(target_p)}

            fake_check_image = MagicMock(return_value={
                "valid": True,
                "width": 1920,
                "height": 1080,
                "format": "JPEG",
                "size_kb": 256.0,
            })

            with (
                patch.object(hdwa, "generate_image_subscription", side_effect=fake_generate_image) as mock_gen,
                patch.object(hdwa, "check_image", fake_check_image),
                patch.object(hdwa, "deploy_to_host2", return_value=(True, "Synced")),
            ):
                result = hdwa.run_nightly_pipeline(
                    target_date="2026-11-20",
                    theme="studio_ghibli_gouache",
                    output_dir=str(out_dir),
                    dry_run=False,
                    force=True,
                    history_file=hist_file,
                )

            assert result["success"] is True
            assert mock_gen.call_count == 3
            assert result["manifest"]["slot_details"]["morning"].get("reused") is not True

    def test_hearth_manifest_frontend_contract(self) -> None:
        """Verify daily_manifest.json strictly matches the Hearth frontend app.js contract."""
        with TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            out_dir = tmp_path / "output"
            hist_file = tmp_path / "history.json"

            result = hdwa.run_nightly_pipeline(
                target_date="2026-11-20",
                theme="studio_ghibli_gouache",
                output_dir=str(out_dir),
                dry_run=True,
                history_file=hist_file,
            )

            assert result["success"] is True
            manifest = result["manifest"]

            # 1. Hearth contract: target_date is a string and matches date
            assert isinstance(manifest["target_date"], str)
            assert manifest["target_date"] == "2026-11-20"
            assert isinstance(manifest["date"], str)
            assert manifest["date"] == manifest["target_date"]

            # 2. Hearth contract: style_label and theme_title are strings matching theme name
            theme_name = hdwa.ART_STYLE_VAULT["studio_ghibli_gouache"]["name"]
            assert isinstance(manifest["style_label"], str)
            assert manifest["style_label"] == theme_name
            assert isinstance(manifest["theme_title"], str)
            assert manifest["theme_title"] == theme_name

            # 3. Hearth contract: slots[slot] MUST be a string URL/path
            assert isinstance(manifest["slots"], dict)
            for slot_name in ("morning", "afternoon", "evening"):
                slot_url = manifest["slots"][slot_name]
                assert isinstance(slot_url, str)
                assert slot_url.startswith("/static/ui/assets/weather-packs/")
                assert f"daily_2026-11-20_{slot_name}.jpg" in slot_url

            # 4. Hearth contract: slot_details holds rich metadata for resume & forensics
            assert isinstance(manifest["slot_details"], dict)
            for slot_name in ("morning", "afternoon", "evening"):
                details = manifest["slot_details"][slot_name]
                assert isinstance(details, dict)
                assert details["valid"] is True
                assert details["clothing_state"] in hdwa.CLOTHING_STATE_PROMPTS
                assert isinstance(details["temp_f"], float)
                assert isinstance(details["filename"], str)
                assert isinstance(details["relative_url"], str)

            # 5. Forensic disk check: verify serialized JSON format conforms to JS app.js expectations
            manifest_path = Path(result["manifest_path"])
            on_disk = json.loads(manifest_path.read_text(encoding="utf-8"))
            assert isinstance(on_disk["target_date"], str)
            assert on_disk["style_label"] or on_disk["theme_title"]
            assert isinstance(on_disk["slots"]["morning"], str)
            assert isinstance(on_disk["slots"]["afternoon"], str)
            assert isinstance(on_disk["slots"]["evening"], str)

    def test_run_nightly_pipeline_resume_with_new_manifest_contract(self) -> None:
        """Verify resume on same date with new contract reuses valid cards via slot_details."""
        with TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            out_dir = tmp_path / "output"
            out_dir.mkdir(parents=True, exist_ok=True)
            hist_file = tmp_path / "history.json"

            morning_card = out_dir / "daily_2026-11-20_morning.jpg"
            morning_card.write_bytes(b"\xff\xd8\xff\xe0" + b"\x00" * 100)

            # Existing manifest using the new Hearth contract (target_date, string slots, slot_details)
            existing_manifest = {
                "target_date": "2026-11-20",
                "date": "2026-11-20",
                "theme_key": "snes_pixel_storybook",
                "style_label": "Super Nintendo 16-Bit Pixel Storybook",
                "theme_title": "Super Nintendo 16-Bit Pixel Storybook",
                "slots": {
                    "morning": "/static/ui/assets/weather-packs/ghibli_rosie_isaac_eve/daily_2026-11-20_morning.jpg",
                    "afternoon": "/static/ui/assets/weather-packs/ghibli_rosie_isaac_eve/daily_2026-11-20_afternoon.jpg",
                    "evening": "/static/ui/assets/weather-packs/ghibli_rosie_isaac_eve/daily_2026-11-20_evening.jpg",
                },
                "slot_details": {
                    "morning": {
                        "valid": True,
                        "filename": "daily_2026-11-20_morning.jpg",
                        "relative_url": "/static/ui/assets/weather-packs/ghibli_rosie_isaac_eve/daily_2026-11-20_morning.jpg",
                    },
                    "afternoon": {
                        "valid": False,
                        "error": "Previous failure",
                    },
                    "evening": {
                        "valid": False,
                        "error": "Previous failure",
                    },
                },
            }
            (out_dir / "daily_manifest.json").write_text(json.dumps(existing_manifest), encoding="utf-8")

            def fake_generate_image(prompt: str, **kwargs: Any) -> dict[str, Any]:
                image_name = kwargs.get("image_name", "")
                target_p = out_dir / f"{image_name}.jpg"
                target_p.write_bytes(b"\xff\xd8\xff\xe0" + b"\x00" * 100)
                return {"success": True, "path": str(target_p)}

            fake_check_image = MagicMock(return_value={
                "valid": True,
                "width": 1920,
                "height": 1080,
                "format": "JPEG",
                "size_kb": 256.0,
            })

            with (
                patch.object(hdwa, "generate_image_subscription", side_effect=fake_generate_image) as mock_gen,
                patch.object(hdwa, "check_image", fake_check_image),
                patch.object(hdwa, "deploy_to_host2", return_value=(True, "Synced")),
            ):
                result = hdwa.run_nightly_pipeline(
                    target_date="2026-11-20",
                    output_dir=str(out_dir),
                    dry_run=False,
                    history_file=hist_file,
                )

            assert result["success"] is True
            # Morning was reused, afternoon & evening generated
            assert mock_gen.call_count == 2
            manifest = result["manifest"]
            assert manifest["slot_details"]["morning"]["valid"] is True
            assert manifest["slot_details"]["morning"]["reused"] is True
            assert manifest["slot_details"]["afternoon"]["valid"] is True
            assert manifest["slot_details"]["evening"]["valid"] is True
            assert isinstance(manifest["slots"]["morning"], str)

    def test_run_nightly_pipeline_resume_with_legacy_manifest_contract(self) -> None:
        """Verify resume with legacy manifest (date only, dict slots, no slot_details) falls back seamlessly."""
        with TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            out_dir = tmp_path / "output"
            out_dir.mkdir(parents=True, exist_ok=True)
            hist_file = tmp_path / "history.json"

            morning_card = out_dir / "daily_2026-11-20_morning.jpg"
            morning_card.write_bytes(b"\xff\xd8\xff\xe0" + b"\x00" * 100)

            # Legacy manifest format: date only, nested dict slots, no slot_details
            legacy_manifest = {
                "date": "2026-11-20",
                "theme_key": "snes_pixel_storybook",
                "slots": {
                    "morning": {
                        "valid": True,
                        "filename": "daily_2026-11-20_morning.jpg",
                    },
                    "afternoon": {
                        "valid": False,
                    },
                    "evening": {
                        "valid": False,
                    },
                },
            }
            (out_dir / "daily_manifest.json").write_text(json.dumps(legacy_manifest), encoding="utf-8")

            def fake_generate_image(prompt: str, **kwargs: Any) -> dict[str, Any]:
                image_name = kwargs.get("image_name", "")
                target_p = out_dir / f"{image_name}.jpg"
                target_p.write_bytes(b"\xff\xd8\xff\xe0" + b"\x00" * 100)
                return {"success": True, "path": str(target_p)}

            fake_check_image = MagicMock(return_value={
                "valid": True,
                "width": 1920,
                "height": 1080,
                "format": "JPEG",
                "size_kb": 256.0,
            })

            with (
                patch.object(hdwa, "generate_image_subscription", side_effect=fake_generate_image) as mock_gen,
                patch.object(hdwa, "check_image", fake_check_image),
                patch.object(hdwa, "deploy_to_host2", return_value=(True, "Synced")),
            ):
                result = hdwa.run_nightly_pipeline(
                    target_date="2026-11-20",
                    output_dir=str(out_dir),
                    dry_run=False,
                    history_file=hist_file,
                )

            assert result["success"] is True
            assert mock_gen.call_count == 2
            manifest = result["manifest"]
            assert manifest["target_date"] == "2026-11-20"
            assert manifest["slot_details"]["morning"]["valid"] is True
            assert manifest["slot_details"]["morning"]["reused"] is True
            assert isinstance(manifest["slots"]["morning"], str)

    def test_run_nightly_pipeline_resume_string_slots_missing_details(self) -> None:
        """Verify pipeline handles existing manifest with string slots but no slot_details without crashing."""
        with TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            out_dir = tmp_path / "output"
            out_dir.mkdir(parents=True, exist_ok=True)
            hist_file = tmp_path / "history.json"

            # Manifest with string slots and no slot_details
            sparse_manifest = {
                "target_date": "2026-11-20",
                "theme_key": "studio_ghibli_gouache",
                "slots": {
                    "morning": "/static/ui/assets/weather-packs/ghibli_rosie_isaac_eve/daily_2026-11-20_morning.jpg",
                    "afternoon": "/static/ui/assets/weather-packs/ghibli_rosie_isaac_eve/daily_2026-11-20_afternoon.jpg",
                    "evening": "/static/ui/assets/weather-packs/ghibli_rosie_isaac_eve/daily_2026-11-20_evening.jpg",
                },
            }
            (out_dir / "daily_manifest.json").write_text(json.dumps(sparse_manifest), encoding="utf-8")

            def fake_generate_image(prompt: str, **kwargs: Any) -> dict[str, Any]:
                image_name = kwargs.get("image_name", "")
                target_p = out_dir / f"{image_name}.jpg"
                target_p.write_bytes(b"\xff\xd8\xff\xe0" + b"\x00" * 100)
                return {"success": True, "path": str(target_p)}

            fake_check_image = MagicMock(return_value={
                "valid": True,
                "width": 1920,
                "height": 1080,
                "format": "JPEG",
                "size_kb": 256.0,
            })

            with (
                patch.object(hdwa, "generate_image_subscription", side_effect=fake_generate_image) as mock_gen,
                patch.object(hdwa, "check_image", fake_check_image),
                patch.object(hdwa, "deploy_to_host2", return_value=(True, "Synced")),
            ):
                result = hdwa.run_nightly_pipeline(
                    target_date="2026-11-20",
                    output_dir=str(out_dir),
                    dry_run=False,
                    history_file=hist_file,
                )

            assert result["success"] is True
            # All 3 generated fresh since string slots couldn't provide valid=True
            assert mock_gen.call_count == 3
            manifest = result["manifest"]
            assert manifest["slot_details"]["morning"]["valid"] is True
            assert isinstance(manifest["slots"]["morning"], str)

    def test_run_nightly_pipeline_resume_target_date_only_theme_locking(self) -> None:
        """Verify resume with manifest containing only target_date (no date) correctly locks theme."""
        with TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            out_dir = tmp_path / "output"
            out_dir.mkdir(parents=True, exist_ok=True)
            hist_file = tmp_path / "history.json"

            existing_manifest = {
                "target_date": "2026-11-20",
                "theme_key": "snes_pixel_storybook",
                "slots": {},
            }
            (out_dir / "daily_manifest.json").write_text(json.dumps(existing_manifest), encoding="utf-8")

            result = hdwa.run_nightly_pipeline(
                target_date="2026-11-20",
                output_dir=str(out_dir),
                dry_run=True,
                history_file=hist_file,
            )

            assert result["success"] is True
            assert result["theme"] == hdwa.ART_STYLE_VAULT["snes_pixel_storybook"]["name"]
            assert result["manifest"]["theme_key"] == "snes_pixel_storybook"
            assert "theme_locked_resume" in result["manifest"]["selection_reason"]


# ===========================================================================
# 9. Main CLI Entry Point Tests
# ===========================================================================
class TestMainCLI:
    """Verifies argument parsing and execution via main()."""

    def test_main_dry_run_flag(self) -> None:
        """Verify main() runs with --dry-run and exits 0."""
        with TemporaryDirectory() as tmp_dir:
            out_dir = Path(tmp_dir) / "cli_out"
            test_argv = [
                "hearth_daily_weather_art.py",
                "--dry-run",
                "--target-date", "2026-11-20",
                "--output-dir", str(out_dir),
            ]
            with (
                patch("sys.argv", test_argv),
                patch.object(hdwa, "DEFAULT_HISTORY_FILE", Path(tmp_dir) / "hist.json"),
            ):
                ret = hdwa.main()
            assert ret == 0
            assert (out_dir / "daily_manifest.json").exists()

    def test_main_install_schedule_flag(self) -> None:
        """Verify main() runs with --install-schedule and exits 0."""
        with TemporaryDirectory() as tmp_dir:
            sched_file = Path(tmp_dir) / "schedule.json"
            test_argv = [
                "hearth_daily_weather_art.py",
                "--install-schedule",
            ]
            real_install = hdwa.install_schedule_entry
            with (
                patch("sys.argv", test_argv),
                patch.object(hdwa, "install_schedule_entry", side_effect=lambda *a, **kw: real_install(sched_file)),
            ):
                ret = hdwa.main()
            assert ret == 0
            assert sched_file.exists()

    def test_main_timeout_retries_and_force_flags(self) -> None:
        """Verify main() parses and passes --timeout, --max-retries, and --force flags."""
        test_argv = [
            "hearth_daily_weather_art.py",
            "--dry-run",
            "--target-date", "2026-11-20",
            "--timeout", "300",
            "--max-retries", "4",
            "--force",
        ]
        with (
            patch("sys.argv", test_argv),
            patch.object(hdwa, "run_nightly_pipeline", return_value={"success": True}) as mock_pipeline,
        ):
            ret = hdwa.main()

        assert ret == 0
        mock_pipeline.assert_called_once()
        _, kwargs = mock_pipeline.call_args
        assert kwargs["timeout"] == 300
        assert kwargs["max_retries"] == 4
        assert kwargs["force"] is True


# ===========================================================================
# 10. Sidecar Wrapper & Scheduler Integration Tests
# ===========================================================================
class TestSidecarWrapperAndScheduler:
    """Verifies run_hearth_weather_art sidecar semantics, failure alerting, and alias mapping."""

    def test_sidecar_aliases_mapping(self) -> None:
        """Verify scheduler_tool.SIDECAR_ALIASES maps hearth_daily_weather_art."""
        from tools.scheduler_tool import SIDECAR_ALIASES

        assert "hearth_daily_weather_art" in SIDECAR_ALIASES
        aliases = SIDECAR_ALIASES["hearth_daily_weather_art"]
        assert "hearth_daily_weather_art" in aliases
        assert "hearth_weather_art" in aliases

    def test_run_hearth_weather_art_success_and_deploy(self) -> None:
        """Verify run_hearth_weather_art returns (True, summary, res) on success+deploy."""
        from tools import sidecars

        fake_res = {
            "success": True,
            "target_date": "2026-11-20",
            "theme": "Studio Ghibli Gouache",
            "manifest": {
                "theme_key": "studio_ghibli_gouache",
                "slots": {
                    "morning": {"valid": True, "reused": True},
                    "afternoon": {"valid": True, "reused": False},
                    "evening": {"valid": True, "reused": False},
                },
            },
            "deploy": {"deployed": True, "message": "Synced 4 files to Host 2"},
        }

        with patch("tools.hearth_daily_weather_art.run_nightly_pipeline", return_value=fake_res) as mock_pipeline:
            ok, summary, res = sidecars.run_hearth_weather_art(
                target_date="2026-11-20",
                theme="studio_ghibli_gouache",
                force=True,
            )

        assert ok is True
        assert "2026-11-20" in summary
        assert "Studio Ghibli Gouache" in summary
        assert "morning (cached)" in summary
        assert res == fake_res
        mock_pipeline.assert_called_once_with(
            target_date="2026-11-20",
            theme="studio_ghibli_gouache",
            force=True,
        )

    def test_run_hearth_weather_art_pipeline_failure(self) -> None:
        """Verify run_hearth_weather_art returns (False, alert, res) on slot failure."""
        from tools import sidecars

        fake_res = {
            "success": False,
            "target_date": "2026-11-20",
            "theme": "Studio Ghibli Gouache",
            "manifest": {
                "theme_key": "studio_ghibli_gouache",
                "slots": {
                    "morning": {"valid": True},
                    "afternoon": {"valid": False},
                    "evening": {"valid": False},
                },
            },
            "deploy": {"deployed": False, "message": "Pipeline incomplete"},
        }

        with patch("tools.hearth_daily_weather_art.run_nightly_pipeline", return_value=fake_res):
            ok, alert, res = sidecars.run_hearth_weather_art(target_date="2026-11-20")

        assert ok is False
        assert "⚠️" in alert
        assert "afternoon" in alert or "evening" in alert
        assert res == fake_res

    def test_run_hearth_weather_art_deploy_failure(self) -> None:
        """Verify run_hearth_weather_art returns (False, alert, res) when deployment fails."""
        from tools import sidecars

        fake_res = {
            "success": True,
            "target_date": "2026-11-20",
            "theme": "Studio Ghibli Gouache",
            "manifest": {
                "slots": {
                    "morning": {"valid": True},
                    "afternoon": {"valid": True},
                    "evening": {"valid": True},
                },
            },
            "deploy": {"deployed": False, "message": "Connection timed out to Host 2"},
        }

        with patch("tools.hearth_daily_weather_art.run_nightly_pipeline", return_value=fake_res):
            ok, alert, res = sidecars.run_hearth_weather_art(target_date="2026-11-20")

        assert ok is False
        assert "⚠️" in alert
        assert "Deployment to Host 2 failed" in alert
        assert "Connection timed out" in alert
        assert res == fake_res

    def test_run_hearth_weather_art_exception_handling(self) -> None:
        """Verify run_hearth_weather_art catches unhandled exceptions gracefully."""
        from tools import sidecars

        with patch("tools.hearth_daily_weather_art.run_nightly_pipeline", side_effect=RuntimeError("Disk full")):
            ok, alert, res = sidecars.run_hearth_weather_art(target_date="2026-11-20")

        assert ok is False
        assert "🚨" in alert
        assert "Disk full" in alert
        assert res["success"] is False

    def test_run_sidecar_job_hearth_weather_art_status_classification(self) -> None:
        """Verify run_sidecar_job sets status='ok' on success and 'error' on failure."""
        from tools import sidecars

        # Success case
        with patch.object(sidecars, "log_execution") as mock_log:
            ok, _, _ = sidecars.run_sidecar_job(
                "hearth_daily_weather_art",
                "Nightly Hearth Weather Art Generator",
                lambda: (True, "All good", {"summary": "clean"}),
            )
            assert ok is True
            assert mock_log.call_args.kwargs["status"] == "ok"

        # Failure case
        with patch.object(sidecars, "log_execution") as mock_log:
            ok, _, _ = sidecars.run_sidecar_job(
                "hearth_daily_weather_art",
                "Nightly Hearth Weather Art Generator",
                lambda: (False, "Generation failed", {"summary": "failed"}),
            )
            assert ok is False
            assert mock_log.call_args.kwargs["status"] == "error"


# ===========================================================================
# 11. Holiday & Birthday Theming + allow_on_holiday Tests
# ===========================================================================
class TestHolidayAndBirthdayTheming:
    """Verifies US holiday and family birthday detection, prompt injection, and manifest flags."""

    def test_resolve_holiday_exact_rules(self) -> None:
        """Verify resolve_holiday detects all Hearth US holidays and computes dynamic holidays correctly."""
        # 1. New Year's Day (01-01)
        res = hdwa.resolve_holiday("2026-01-01")
        assert res is not None
        assert res["key"] == "new_years"
        assert res["name"] == "New Year's Day"
        assert "shimmering golden ribbons" in res["theming"].lower()
        assert "dawn light" in res["morning"].lower()
        assert "afternoon" in res["afternoon"].lower()
        assert "twilight" in res["evening"].lower()

        # 2. Valentine's Day (02-14)
        res = hdwa.resolve_holiday("2026-02-14")
        assert res is not None
        assert res["key"] == "valentines"
        assert res["name"] == "Valentine's Day"
        assert "heart" in res["theming"].lower()

        # 3. Easter (Gregorian Computus)
        # 2024: March 31, 2025: April 20, 2026: April 5, 2027: March 28
        easter_dates = [
            ("2024-03-31", True),
            ("2024-04-01", False),
            ("2025-04-20", True),
            ("2025-04-13", False),
            ("2026-04-05", True),
            ("2026-04-12", False),
            ("2027-03-28", True),
        ]
        for dt_str, is_easter in easter_dates:
            h = hdwa.resolve_holiday(dt_str)
            if is_easter:
                assert h is not None, f"Expected Easter on {dt_str}"
                assert h["key"] == "easter"
                assert h["name"] == "Easter"
                assert "egg hunt" in h["theming"].lower()
            else:
                assert h is None or h["key"] != "easter", f"Unexpected Easter on {dt_str}"

        # 4. Memorial Day (last Monday of May, day >= 25)
        # 2024-05-27 (Mon), 2025-05-26 (Mon), 2026-05-25 (Mon)
        memorial_dates = [
            ("2024-05-27", True),
            ("2024-05-20", False),  # 3rd Monday (day 20 < 25)
            ("2025-05-26", True),
            ("2026-05-25", True),
            ("2026-05-18", False),  # 3rd Monday
            ("2026-05-26", False),  # Tuesday after Memorial Day
        ]
        for dt_str, is_mem in memorial_dates:
            h = hdwa.resolve_holiday(dt_str)
            if is_mem:
                assert h is not None, f"Expected Memorial Day on {dt_str}"
                assert h["key"] == "memorial_day"
                assert h["name"] == "Memorial Day"
                assert "bunting" in h["theming"].lower()
            else:
                assert h is None or h["key"] != "memorial_day"

        # 5. Fourth of July (07-04)
        res = hdwa.resolve_holiday("2026-07-04")
        assert res is not None
        assert res["key"] == "july_4th"
        assert res["name"] == "Fourth of July"
        assert "patriotic" in res["theming"].lower()

        # 6. Labor Day (first Monday of Sep, day <= 7)
        # 2024-09-02 (Mon), 2025-09-01 (Mon), 2026-09-07 (Mon)
        labor_dates = [
            ("2024-09-02", True),
            ("2025-09-01", True),
            ("2026-09-07", True),
            ("2026-09-14", False),  # 2nd Monday (day 14 > 7)
            ("2026-09-01", False),  # Tuesday
        ]
        for dt_str, is_labor in labor_dates:
            h = hdwa.resolve_holiday(dt_str)
            if is_labor:
                assert h is not None, f"Expected Labor Day on {dt_str}"
                assert h["key"] == "labor_day"
                assert h["name"] == "Labor Day"
                assert "late-summer" in h["theming"].lower()
            else:
                assert h is None or h["key"] != "labor_day"

        # 7. Halloween (10-31)
        res = hdwa.resolve_holiday("2026-10-31")
        assert res is not None
        assert res["key"] == "halloween"
        assert res["name"] == "Halloween"
        assert "jack-o'-lanterns" in res["theming"].lower()

        # 8. Thanksgiving (4th Thursday of Nov, 22 <= day <= 28)
        # 2024-11-28 (Thu), 2025-11-27 (Thu), 2026-11-26 (Thu)
        thanksgiving_dates = [
            ("2024-11-28", True),
            ("2025-11-27", True),
            ("2026-11-26", True),
            ("2026-11-19", False),  # 3rd Thursday (day 19 < 22)
            ("2026-11-27", False),  # Black Friday
        ]
        for dt_str, is_tday in thanksgiving_dates:
            h = hdwa.resolve_holiday(dt_str)
            if is_tday:
                assert h is not None, f"Expected Thanksgiving on {dt_str}"
                assert h["key"] == "thanksgiving"
                assert h["name"] == "Thanksgiving"
                assert "autumn harvest foliage" in h["theming"].lower()
            else:
                assert h is None or h["key"] != "thanksgiving"

        # 9. Christmas (12-24 and 12-25)
        for dt_str in ("2026-12-24", "2026-12-25"):
            res = hdwa.resolve_holiday(dt_str)
            assert res is not None, f"Expected Christmas on {dt_str}"
            assert res["key"] == "christmas"
            assert "christmas" in res["name"].lower()
            assert "evergreen pine" in res["theming"].lower()

        # Non-holidays and invalid inputs
        assert hdwa.resolve_holiday("2026-11-20") is None
        assert hdwa.resolve_holiday("2026-06-15") is None
        assert hdwa.resolve_holiday("") is None
        assert hdwa.resolve_holiday("invalid-date") is None

    def test_resolve_birthday_exact_rules(self) -> None:
        """Verify resolve_birthday identifies family birthdays for Rosie, Isaac, and Genevieve."""
        # 1. Rosie (11-07)
        res = hdwa.resolve_birthday("2026-11-07")
        assert res is not None
        assert res["key"] == "rosie"
        assert res["child"] == "Rosie"
        assert "Rosie" in res["name"]
        assert "balloons" in res["theming"].lower()
        assert "streamers" in res["theming"].lower()
        assert "birthday cake with glowing candles" in res["theming"].lower()
        assert "zero-text guardrails" in res["theming"].lower()
        assert "morning" in res["morning"].lower()
        assert "afternoon" in res["afternoon"].lower()
        assert "evening" in res["evening"].lower()

        # 2. Isaac (12-26)
        res = hdwa.resolve_birthday("2026-12-26")
        assert res is not None
        assert res["key"] == "isaac"
        assert res["child"] == "Isaac"
        assert "Isaac" in res["name"]
        assert "toddler" in res["theming"].lower()
        assert "birthday cake with glowing candles" in res["theming"].lower()

        # 3. Genevieve / Eve (09-03)
        res = hdwa.resolve_birthday("2026-09-03")
        assert res is not None
        assert res["key"] == "eve"
        assert "Genevieve" in res["name"]
        assert "Genevieve" in res["child"]
        assert "milestone" in res["theming"].lower()
        assert "birthday cake with glowing candles" in res["theming"].lower()

        # Non-birthdays and invalid inputs
        assert hdwa.resolve_birthday("2026-11-08") is None
        assert hdwa.resolve_birthday("2026-12-25") is None
        assert hdwa.resolve_birthday("2026-09-04") is None
        assert hdwa.resolve_birthday("") is None
        assert hdwa.resolve_birthday("invalid-date") is None

    def test_build_slot_prompt_holiday_theming_injection(self) -> None:
        """Verify build_slot_prompt injects rich SPECIAL OCCASION THEMING for holidays."""
        theme = hdwa.ART_STYLE_VAULT["studio_ghibli_gouache"]
        weather = {"clothing_state": "morning_mild", "temp_f": 58.0, "condition": "partlycloudy"}

        # Thanksgiving 2026-11-26 across all three slots
        for slot in ("morning", "afternoon", "evening"):
            prompt = hdwa.build_slot_prompt(
                slot=slot,
                theme=theme,
                weather_info=weather,
                target_date="2026-11-26",
            )
            # 1. High-signal ordering invariant preserved
            assert prompt.index(theme["prompt_style"]) < prompt.index(hdwa.SLOT_CHOREOGRAPHY[slot])
            assert prompt.index(hdwa.SLOT_CHOREOGRAPHY[slot]) < prompt.index("MANDATORY WEATHER-MATCHED ATTIRE")
            assert prompt.index("MANDATORY WEATHER-MATCHED ATTIRE") < prompt.index("Atmosphere:")
            assert prompt.index("Atmosphere:") < prompt.index("Setting:")
            assert prompt.index("Setting:") < prompt.index("Characters:")

            # 2. Holiday occasion clause presence and contents
            assert "SPECIAL OCCASION THEMING:" in prompt
            assert "Holiday Celebration (Thanksgiving):" in prompt
            assert "autumn harvest foliage" in prompt
            assert "zero-text invariant" in prompt.lower()

            # 3. Slot-specific visual touch in the prompt
            if slot == "morning":
                assert "morning" in prompt.lower()
            elif slot == "afternoon":
                assert "afternoon" in prompt.lower()
            elif slot == "evening":
                assert "evening" in prompt.lower()

            # 4. Mandatory character consistency and attire preserved
            assert hdwa.BASE_CHARACTER_CONSISTENCY_CLAUSE in prompt
            assert "RIGHT 42%" in prompt
            assert "LEFT 58%" in prompt
            assert "MANDATORY WEATHER-MATCHED ATTIRE (morning_mild)" in prompt

    def test_build_slot_prompt_birthday_theming_injection(self) -> None:
        """Verify build_slot_prompt injects rich celebratory birthday theming with zero-text guardrails."""
        theme = hdwa.ART_STYLE_VAULT["zelda_botw_watercolor"]
        weather = {"clothing_state": "cold_puffer", "temp_f": 40.0, "condition": "clear"}

        # Rosie's birthday: 2026-11-07
        prompt = hdwa.build_slot_prompt(
            slot="afternoon",
            theme=theme,
            weather_info=weather,
            target_date="2026-11-07",
        )
        assert "SPECIAL OCCASION THEMING:" in prompt
        assert "Family Birthday (Rosie's Birthday):" in prompt
        assert "Rosie" in prompt
        assert "balloons" in prompt.lower()
        assert "streamers" in prompt.lower()
        assert "birthday cake with glowing candles" in prompt.lower()
        assert "no letters, words, numbers, or signs on cake" in prompt.lower()

        # Mandatory character consistency & weather attire intact
        assert "MANDATORY WEATHER-MATCHED ATTIRE (cold_puffer)" in prompt
        assert hdwa.BASE_CHARACTER_CONSISTENCY_CLAUSE in prompt

    def test_build_slot_prompt_backwards_compatibility(self) -> None:
        """Verify omitting target_date or passing non-occasion dates does not inject occasion clause."""
        theme = hdwa.ART_STYLE_VAULT["studio_ghibli_gouache"]
        weather = {"clothing_state": "morning_mild", "temp_f": 58.0, "condition": "sunny"}

        # Default target_date=None (omitted)
        p1 = hdwa.build_slot_prompt(slot="morning", theme=theme, weather_info=weather)
        assert "SPECIAL OCCASION THEMING" not in p1

        # Explicit target_date=None
        p2 = hdwa.build_slot_prompt(slot="morning", theme=theme, weather_info=weather, target_date=None)
        assert p1 == p2
        assert "SPECIAL OCCASION THEMING" not in p2

        # Regular non-holiday, non-birthday date
        p3 = hdwa.build_slot_prompt(slot="morning", theme=theme, weather_info=weather, target_date="2026-11-20")
        assert p1 == p3
        assert "SPECIAL OCCASION THEMING" not in p3

    def test_pipeline_manifest_allow_on_holiday_flag(self) -> None:
        """Verify run_nightly_pipeline sets allow_on_holiday=True on holidays and False on regular days."""
        with TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            out_dir = tmp_path / "output"
            hist_file = tmp_path / "history.json"

            # 1. Holiday run: Thanksgiving 2026-11-26
            res_holiday = hdwa.run_nightly_pipeline(
                target_date="2026-11-26",
                theme="studio_ghibli_gouache",
                output_dir=str(out_dir),
                dry_run=True,
                history_file=hist_file,
            )
            assert res_holiday["success"] is True
            manifest_holiday = res_holiday["manifest"]
            assert manifest_holiday["allow_on_holiday"] is True
            assert manifest_holiday["holiday"] == "Thanksgiving"
            assert manifest_holiday["holiday_key"] == "thanksgiving"
            assert res_holiday["allow_on_holiday"] is True
            assert res_holiday["holiday"] == "Thanksgiving"

            # Check prompts in manifest slot_details
            for slot in ("morning", "afternoon", "evening"):
                prompt = manifest_holiday["slot_details"][slot]["prompt"]
                assert "SPECIAL OCCASION THEMING" in prompt
                assert "Thanksgiving" in prompt

            # Verify on-disk daily_manifest.json
            manifest_file = out_dir / "daily_manifest.json"
            on_disk_holiday = json.loads(manifest_file.read_text(encoding="utf-8"))
            assert on_disk_holiday["allow_on_holiday"] is True
            assert on_disk_holiday["holiday"] == "Thanksgiving"

            # 2. Regular day run: 2026-11-20
            out_dir_regular = tmp_path / "output_reg"
            res_regular = hdwa.run_nightly_pipeline(
                target_date="2026-11-20",
                theme="studio_ghibli_gouache",
                output_dir=str(out_dir_regular),
                dry_run=True,
                history_file=hist_file,
            )
            assert res_regular["success"] is True
            manifest_reg = res_regular["manifest"]
            assert manifest_reg["allow_on_holiday"] is False
            assert "holiday" not in manifest_reg
            assert res_regular["allow_on_holiday"] is False
            assert res_regular["holiday"] is None

            # 3. Family birthday run: Rosie 2026-11-07
            out_dir_bday = tmp_path / "output_bday"
            res_bday = hdwa.run_nightly_pipeline(
                target_date="2026-11-07",
                theme="studio_ghibli_gouache",
                output_dir=str(out_dir_bday),
                dry_run=True,
                history_file=hist_file,
            )
            assert res_bday["success"] is True
            manifest_bday = res_bday["manifest"]
            assert manifest_bday["allow_on_holiday"] is False
            assert "holiday" not in manifest_bday
            assert manifest_bday["birthday"] == "Rosie's Birthday"
            assert manifest_bday["birthday_child"] == "Rosie"
            assert res_bday["birthday"] == "Rosie's Birthday"

            for slot in ("morning", "afternoon", "evening"):
                prompt = manifest_bday["slot_details"][slot]["prompt"]
                assert "SPECIAL OCCASION THEMING" in prompt
                assert "Rosie" in prompt
