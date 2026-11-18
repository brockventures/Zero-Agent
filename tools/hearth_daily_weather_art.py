#!/usr/bin/env python3
# ruff: noqa: BLE001, S110, S112, PLW1510, RUF059, EXE001
"""Nightly Dynamic Hearth Weather Art Pipeline with Context Collector & Character Consistency.

Executes daily at 6:30 PM PT to:
1. Fetch tomorrow's hourly weather forecast (8 AM, 2 PM, 6 PM PT) from Home Assistant.
2. Map temperature & precipitation to Hearth clothing states (morning_rain, rainy_boots,
   cold_puffer, morning_mild, cool_evening, warm_pants, hot_dress).
3. Collect family gaming, media, book, and calendar context (preserving exact Switch titles
   such as The Legend of Zelda: Ocarina of Time -> Kokiri Forest / Lon Lon Ranch / fairy Navi).
4. Select a non-repeating art style from the 19+ visual medium vault with a 12-day cooldown,
   using a 60/40 context vs vault rotation weighting.
5. Generate 3 cohesive 16:9 weather cards (morning, afternoon, evening) via $0 subscription
   Gemini with strict character consistency (Rosie 7yo, Isaac ~3yo, Eve ~3mo infant all with
   golden-blonde hair and light blue-green eyes, right-weighted 42% composition, zero text/UI).
6. Verify cards with check_image(), compile daily_manifest.json, and deploy via scp -O to Host 2.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import random
import re
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Self
from zoneinfo import ZoneInfo

# Add workspace roots to sys.path
for _p in ["/workspace", "/workspace/tools", str(Path(__file__).resolve().parent.parent), str(Path(__file__).resolve().parent)]:
    if _p not in sys.path:
        sys.path.insert(0, _p)

try:
    from tools import nas_config
    from tools.deliver_image import check_image
    from tools.gemini_image import generate_image_subscription
except ImportError:
    import nas_config
    from deliver_image import check_image
    from gemini_image import generate_image_subscription

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("hearth_weather_art")

PT_TZ = ZoneInfo("America/Los_Angeles")
DATA_DIR = Path("/workspace/data")
DEFAULT_HISTORY_FILE = DATA_DIR / "hearth_weather_art_history.json"
DEFAULT_REF_IMAGE = DATA_DIR / "hearth_weather_ref" / "family_ref.jpg"
DEFAULT_OUTPUT_DIR = DATA_DIR / "hearth_daily_weather"
DEFAULT_SCHEDULE_FILE = DATA_DIR / "schedule.json"

DEFAULT_IMAGE_TIMEOUT = 240
DEFAULT_MAX_RETRIES = 2

HEARTH_REMOTE_REL_PATH = "hearth/web/static/ui/assets/weather-packs/ghibli_rosie_isaac_eve"

# ---------------------------------------------------------------------------
# 1. Family Cast Sheet & Strict Character Invariants
# ---------------------------------------------------------------------------
FAMILY_CAST_SHEET: dict[str, dict[str, str]] = {
    "rosie": {
        "name": "Rosie",
        "age": "7-year-old girl",
        "hair": "flowing golden-blonde hair with soft natural waves",
        "eyes": "bright light blue-green eyes",
        "stature": "tallest child with slender, cheerful 7yo proportions",
        "demeanor": "caring, smiling older sister with a warm expressive face",
    },
    "isaac": {
        "name": "Isaac",
        "age": "3-year-old toddler boy",
        "hair": "tousled messy light golden-blonde hair",
        "eyes": "curious bright light blue-green eyes",
        "stature": "noticeably shorter than Rosie, standing at her shoulder or chest height",
        "demeanor": "playful, energetic toddler boy with rounded cheeks and joyous smile",
    },
    "eve": {
        "name": "Genevieve (Eve)",
        "age": "3-month-old infant baby girl",
        "hair": "delicate wispy pale golden-blonde baby hair",
        "eyes": "sweet sleepy light blue-green eyes",
        "stature": "tiny young-infant size, newborn proportions",
        "demeanor": "peacefully asleep or softly gazing, always cradled safely and securely in an arm, sling, or swaddle",
    },
}

BASE_CHARACTER_CONSISTENCY_CLAUSE = (
    "Three young siblings together sharing absolute family consistency: "
    "1) Rosie: 7-year-old girl, tallest child, long flowing golden-blonde hair with soft natural waves, bright light blue-green eyes, warm friendly smile. "
    "2) Isaac: 3-year-old toddler boy, noticeably shorter than Rosie (reaching her chest or shoulder height), tousled messy light golden-blonde hair, curious bright light blue-green eyes, playful toddler demeanor. "
    "3) Genevieve (Eve): 3-month-old infant baby girl, tiny newborn-sized baby, fine wispy pale golden-blonde baby hair, sweet light blue-green eyes, always bundled snugly and cradled securely in an arm, chest wrap, or cushioned basket (never standing, never left alone). "
    "MANDATORY INVARIANTS: All three children MUST share matching golden-blonde hair and light blue-green eyes. "
    "NEGATIVE GUARDRAILS: Absolutely no dark hair, no brown hair, no black hair, no brunette characters. "
    "No adult or teenage Genevieve; Genevieve is strictly a 3-month-old cradled infant. "
    "COMPOSITION INVARIANT: Horizontal widescreen 16:9 aspect ratio. ALL three characters MUST be clustered together within the RIGHT 42% of the frame. "
    "The entire LEFT 58% of the frame MUST remain open, clean atmospheric scenery and expansive landscape so left-side weather dashboard UI text never covers character faces. "
    "ZERO-UI INVARIANT: Absolutely no text, no lettering, no numerals, no watermarks, no signatures, no weather icons, no gauges, no borders."
)

CLOTHING_STATE_PROMPTS: dict[str, str] = {
    "morning_rain": (
        "Rosie and Isaac dressed in cheerful yellow and sky-blue hooded rain slickers with bright waterproof puddle boots, "
        "stepping happily near dewy morning puddles; baby Genevieve warmly swaddled and dry inside a cozy hooded fleece bunting, "
        "sheltered safely under a wide umbrella."
    ),
    "rainy_boots": (
        "Rosie and Isaac in vibrant waterproof hooded raincoats and colorful puddle-jumping rain boots walking along glistening wet paths; "
        "baby Genevieve bundled cozy and dry in a hooded fleece carrier or waterproof swaddle, securely cradled."
    ),
    "cold_puffer": (
        "Rosie and Isaac bundled in cozy quilted down puffer coats, warm knit pom-pom beanies, and fuzzy mittens against the crisp chilly air; "
        "baby Genevieve nestled snugly in a warm quilted snowsuit swaddle with a tiny knit bonnet, cradled close."
    ),
    "morning_mild": (
        "Rosie and Isaac wearing fresh comfortable morning layers—soft cotton long sleeves, lightweight knitted cardigans, and relaxed everyday trousers; "
        "baby Genevieve resting peacefully in a soft pastel cotton baby wrap."
    ),
    "cool_evening": (
        "Rosie and Isaac in warm cozy knit sweaters, soft woolen scarves, and evening layers enjoying the cool dusk air; "
        "baby Genevieve bundled warmly in a soft knit baby blanket and bonnet, cradled securely in gentle twilight."
    ),
    "warm_pants": (
        "Rosie and Isaac in casual comfortable cotton shirts, soft fleece pullovers, and sturdy denim or corduroy trousers in pleasant temperate weather; "
        "baby Genevieve nestled contentedly in a soft cotton onesie and baby wrap."
    ),
    "hot_dress": (
        "Rosie wearing a breezy light cotton summer sundress, Isaac in short sleeves and comfortable cotton shorts in the warm sunshine; "
        "baby Genevieve in a light, airy cotton sun-romper and sun bonnet, shaded and cradled gently in the pleasant breeze."
    ),
}

SLOT_LIGHTING: dict[str, str] = {
    "morning": "Crisp morning sunlight, soft low-angle golden dawn illumination, long gentle shadows, fresh morning atmosphere, 8:00 AM dawn ambiance",
    "afternoon": "Vibrant clear afternoon daylight, warm luminous natural sunlit ambiance, rich colorful daylight illumination, 2:00 PM sun",
    "evening": "Cozy twilight golden-hour dusk, warm amber sunset glow in the sky, soft lanterns or gentle evening twilight colors, 6:00 PM dusk",
}

# ---------------------------------------------------------------------------
# 2. Art Style Vault (19+ Distinct Visual Mediums)
# ---------------------------------------------------------------------------
ART_STYLE_VAULT: dict[str, dict[str, Any]] = {
    "zelda_oot_kokiri_storybook": {
        "visual_medium": "zelda_oot_kokiri_storybook",
        "name": "Zelda: Ocarina of Time (Kokiri Forest Storybook)",
        "prompt_style": (
            "The Legend of Zelda: Ocarina of Time classic storybook illustration, lush Kokiri Forest "
            "with massive ancient tree roots, mossy wooden canopy footbridges, wooden hollow-trunk cottages, "
            "glowing winged fairy companions (Navi) leaving sparkling warm golden light trails, "
            "Great Deku Tree emerald foliage in the distant background, warm whimsical fantasy watercolor and gouache"
        ),
        "scene_flavor": {
            "morning": "dewy morning sunbeams breaking through the Great Deku Tree's ancient emerald canopy, glowing fairy Navi fluttering near the bridge, misty forest floor with blue wildflowers",
            "afternoon": "sunlit clearing in Kokiri Forest, vibrant sunbeams dancing across wooden boardwalks and mossy hollow logs, sparkling fairy dust drifting gently",
            "evening": "twilight amber dusk in Kokiri Forest, glowing fairy lantern light casting warm golden rings on ancient bark, deep indigo twilight sky visible through towering forest branches",
        },
        "context_tags": ["zelda", "ocarina of time", "oot", "nintendo", "switch", "fantasy"],
    },
    "zelda_botw_watercolor": {
        "visual_medium": "zelda_botw_watercolor",
        "name": "Zelda: Breath of the Wild (Painterly Watercolor)",
        "prompt_style": (
            "The Legend of Zelda: Breath of the Wild painterly watercolor and cel-shaded concept art, "
            "sweeping open Hyrule wilderness, rolling wind-swept golden-green hills, ancient moss-covered Guardian "
            "stone ruins overgrown with silent princess wildflowers, vast vivid open sky with soft painterly cumulus clouds"
        ),
        "scene_flavor": {
            "morning": "crisp morning mountain breeze across sweeping green plateau, soft pastel dawn horizon, gentle mountain mist",
            "afternoon": "wide expansive sunlit Hyrule hillside, gentle wind ripples through tall grass, ancient stone archway in distance",
            "evening": "dramatic golden-hour dusk over rolling mountain peaks, warm magenta and amber twilight sky, distant ancient towers glowing faint blue",
        },
        "context_tags": ["zelda", "botw", "breath of the wild", "totk", "hyrule", "switch"],
    },
    "links_awakening_diorama": {
        "visual_medium": "links_awakening_diorama",
        "name": "Zelda: Link's Awakening (Miniature Tilt-Shift Diorama)",
        "prompt_style": (
            "The Legend of Zelda: Link's Awakening tilt-shift miniature toy diorama, glossy smooth clay-like "
            "chibi figurines, handcrafted toy-box aesthetic, soft depth of field, rounded wooden trees, tiny potted shrubs, "
            "warm ambient studio lighting with glossy reflections"
        ),
        "scene_flavor": {
            "morning": "morning sun glinting off glossy miniature ceramic houses and tiny wooden fencing in Mabe Village",
            "afternoon": "charming sunlit toy-box beach trail with miniature palm trees and glossy sea shells along the path",
            "evening": "miniature dusk lighting with warm glowing tabletop fairy bulbs illuminating tiny clay cottage verandas",
        },
        "context_tags": ["zelda", "link's awakening", "mabe village", "nintendo", "toy"],
    },
    "studio_ghibli_gouache": {
        "visual_medium": "studio_ghibli_gouache",
        "name": "Studio Ghibli (Hand-Painted Gouache & Watercolor)",
        "prompt_style": (
            "Hand-painted Studio Ghibli watercolor and gouache illustration with delicate warm ink linework, "
            "lush storybook botanical and rustic architectural detail, soft luminous natural lighting, gentle film grain, "
            "whimsical European-Japanese countryside aesthetic with floating puffy cumulus clouds"
        ),
        "scene_flavor": {
            "morning": "sunlit garden terrace beside a mossy stone cottage wall, climbing morning glory vines, fresh morning dew",
            "afternoon": "breezy countryside meadow overlooking distant rolling hills and a winding brick path lined with wildflowers",
            "evening": "warm lantern-lit cottage porch at twilight, amber window glow casting soft light across a flagstone path under a violet evening sky",
        },
        "context_tags": ["ghibli", "totoro", "kiki", "ponyo", "howl", "anime", "plex", "movie"],
    },
    "spiderverse_halftone": {
        "visual_medium": "spiderverse_halftone",
        "name": "Spider-Verse (Dynamic Halftone & Comic Animation)",
        "prompt_style": (
            "Spider-Verse dynamic stylized comic animation art style, subtle visible Ben-Day halftone dot patterns, "
            "crisp graphic ink contours, subtle chromatic aberration edge accents, vibrant stylized color grading, "
            "cinematic animated concept art"
        ),
        "scene_flavor": {
            "morning": "crisp morning skyline with stylized halftone clouds and sharp graphic sunbeams cutting through city rooftop gardens",
            "afternoon": "vibrant sun-drenched rooftop terrace with expressive graphic shadows, vivid sky blues, and dynamic comic textures",
            "evening": "dramatic twilight skyline bathed in rich magenta and electric orange, glowing halftone city lights twinkling in the background",
        },
        "context_tags": ["spider-man", "spiderverse", "marvel", "comic", "animation", "plex"],
    },
    "calvin_hobbes_ink_wash": {
        "visual_medium": "calvin_hobbes_ink_wash",
        "name": "Calvin & Hobbes (Loose Ink & Watercolor Wash)",
        "prompt_style": (
            "Bill Watterson Calvin and Hobbes classic Sunday comic strip watercolor illustration, "
            "expressive energetic brush ink linework, soft luminous watercolor washes, nostalgic outdoor adventure, "
            "whimsical natural landscape with textured foliage and loose spontaneous ink contours"
        ),
        "scene_flavor": {
            "morning": "crisp early morning woods with loose ink trees and fresh pastel watercolor wash skies",
            "afternoon": "sunny hillside meadow with tumbling autumn leaves, loose breezy ink strokes, and warm yellow watercolor sunlight",
            "evening": "peaceful dusk under a majestic sprawling oak tree, rich indigo and violet watercolor twilight wash with ink silhouette branches",
        },
        "context_tags": ["calvin and hobbes", "comic", "strip", "adventure", "books", "humor"],
    },
    "snes_pixel_storybook": {
        "visual_medium": "snes_pixel_storybook",
        "name": "SNES 16-Bit Pixel Storybook",
        "prompt_style": (
            "High-definition 16-bit Super Nintendo pixel storybook art, meticulously handcrafted pixel art landscape "
            "reminiscent of Chrono Trigger and Secret of Mana, rich vibrant color palette, nostalgic 90s RPG scenic overlook, "
            "crisp pixel cluster shading with lush botanical details"
        ),
        "scene_flavor": {
            "morning": "morning sun rising over a 16-bit hillside overlook, tiered pixel cliffs, gentle pixelated river cascading below",
            "afternoon": "sunlit RPG village outskirts with colorful pixel flora, cobblestone trail, and charming wooden cottage roofs",
            "evening": "twilight 16-bit campfire or village lantern glow under an expansive pixelated starry gradient night sky",
        },
        "context_tags": ["snes", "chrono trigger", "pixel", "rpg", "retro", "gaming"],
    },
    "cut_paper_collage": {
        "visual_medium": "cut_paper_collage",
        "name": "Cut-Paper Layered Collage",
        "prompt_style": (
            "Handcrafted cut-paper collage illustration, multi-layered textured paper sheets with soft physical drop shadows, "
            "clean geometric silhouettes, tactile heavy-stock paper textures, warm ambient side-lighting creating genuine physical dimensional depth"
        ),
        "scene_flavor": {
            "morning": "layered pastel paper hills and delicate cut-paper floral petals bathed in soft morning side-light",
            "afternoon": "vibrant multi-tiered cut-paper meadow with textured cardstock trees and geometric fluffy paper clouds",
            "evening": "deep midnight blue and warm amber paper silhouettes of rolling hills, with a luminous cut-out paper moon and stars",
        },
        "context_tags": ["craft", "paper", "art", "collage", "geometric"],
    },
    "aardman_claymation": {
        "visual_medium": "aardman_claymation",
        "name": "Aardman Claymation Stop-Motion",
        "prompt_style": (
            "Aardman stop-motion claymation style, handcrafted plasticine clay textures with subtle tactile fingerprint marks, "
            "expressive sculpted clay figurines, warm tactile studio miniature-set lighting, cozy British storybook diorama aesthetic"
        ),
        "scene_flavor": {
            "morning": "charming clay garden patch with sculpted plasticine vegetables, miniature wooden fence, and soft warm studio morning light",
            "afternoon": "sunlit miniature cobblestone path with charming clay cottages, sculpted chimney pots, and bright plasticine flowers",
            "evening": "cozy twilight scene outside a clay cottage with glowing yellow plasticine windows and miniature brass streetlamp",
        },
        "context_tags": ["claymation", "aardman", "wallace", "gromit", "animation"],
    },
    "ukiyoe_woodblock": {
        "visual_medium": "ukiyoe_woodblock",
        "name": "Shin-Hanga & Ukiyo-e Woodblock Print",
        "prompt_style": (
            "Traditional Japanese Shin-Hanga and Ukiyo-e woodblock print illustration, elegant carved black key-line woodcut contours, "
            "subtle bokashi gradient ink blending, fibrous textured washi paper surface, atmospheric nature vista with timeless botanical elegance"
        ),
        "scene_flavor": {
            "morning": "misty morning lake shore with graceful pine boughs, soft bokashi pink and gold sunrise wash on fibrous washi paper",
            "afternoon": "crisp cedar forest path with delicate carved woodcut foliage, gentle mountain stream, and clear cobalt sky",
            "evening": "twilight indigo woodblock vista with a crescent moon peeking through silhouetted Japanese maple branches",
        },
        "context_tags": ["japan", "woodblock", "ukiyo-e", "shin-hanga", "art"],
    },
    "golden_books_gouache": {
        "visual_medium": "golden_books_gouache",
        "name": "Little Golden Books Mid-Century Gouache",
        "prompt_style": (
            "Classic 1950s Little Golden Books gouache illustration style, bold retro-flat gouache colors, charming mid-century "
            "children's book textured paper, Mary Blair inspired geometric whimsy, nostalgic warmth and innocent joyful storybook charm"
        ),
        "scene_flavor": {
            "morning": "bright cheerful mid-century garden with stylized gouache sunflowers, painted wooden birdhouse, and retro pastel morning sunshine",
            "afternoon": "breezy rolling hillside with playful flat-color trees, whimsical patterned picnic blanket, and bold cheerful brushwork",
            "evening": "cozy storybook bedroom window or twilight porch with warm golden gouache lamplight and deep stylized navy night sky",
        },
        "context_tags": ["golden books", "mary blair", "vintage", "mid-century", "storybook", "children"],
    },
    "lofi_cozy_anime": {
        "visual_medium": "lofi_cozy_anime",
        "name": "Lo-Fi Cozy Chillhop Anime",
        "prompt_style": (
            "Cozy Lo-Fi chillhop anime illustration, soft warm aesthetic, detailed nostalgic environment, "
            "gentle atmospheric lighting, potted houseplants, warm tea cups, serene peaceful warmth, soothing pastel palette"
        ),
        "scene_flavor": {
            "morning": "sun-drenched morning sunroom filled with lush potted monsteras and ferns, golden dust motes floating in warm light beams",
            "afternoon": "peaceful sunlit balcony garden overlooking leafy quiet neighborhood streets, soft pastel blue sky and gentle breeze",
            "evening": "cozy twilight veranda with warm string fairy lights, steaming ceramic mugs, and soothing deep lavender evening sky",
        },
        "context_tags": ["lofi", "chillhop", "anime", "cozy", "music", "coffee"],
    },
    "nordic_folk_storybook": {
        "visual_medium": "nordic_folk_storybook",
        "name": "Nordic Scandinavian Folk Tale",
        "prompt_style": (
            "Scandinavian Nordic folk art storybook illustration, intricate floral rosemaling patterns, stylized forest pine trees "
            "and woodland creatures, earthy palette of deep forest green, terracotta, mustard yellow, and warm cream, Hans Christian Andersen tale aesthetic"
        ),
        "scene_flavor": {
            "morning": "crisp boreal forest clearing bordered by stylized pine trees and lingonberry bushes, soft pale Nordic morning sun",
            "afternoon": "charming timber cottage meadow with decorative wooden fences, patterned wildflowers, and warm amber sunshine",
            "evening": "mystical twilight northern forest with stylized glowing lantern and twinkling stars peeking through geometric pine branches",
        },
        "context_tags": ["nordic", "scandinavian", "folk", "storybook", "winter", "forest"],
    },
    "ligne_claire_comic": {
        "visual_medium": "ligne_claire_comic",
        "name": "Franco-Belgian Ligne Claire Comic",
        "prompt_style": (
            "Franco-Belgian Ligne Claire comic illustration style reminiscent of Hergé's Tintin, crisp uniform black ink line work "
            "of equal weight, clear flat solid color fills, meticulously detailed architectural and natural backgrounds, classic European graphic novel clarity"
        ),
        "scene_flavor": {
            "morning": "pristine morning village square with detailed European stone facades, striped awnings, and crisp clean linework under clear blue sky",
            "afternoon": "scenic coastal path or garden terrace with clear geometric sailboats on the water and immaculate clean graphic foliage",
            "evening": "quiet charming cobblestone alley at dusk, crisp lamppost casting clean geometric shadows against warm brick walls",
        },
        "context_tags": ["tintin", "ligne claire", "comic", "european", "belgian", "adventure"],
    },
    "oil_pastel_impressionist": {
        "visual_medium": "oil_pastel_impressionist",
        "name": "Oil Pastel & Impressionist Canvas",
        "prompt_style": (
            "Rich oil pastel and textured impressionist oil painting, vibrant tactile pigment strokes, dappled natural sunlight "
            "filtering through lush tree canopies, broken color technique reminiscent of Claude Monet and Mary Cassatt, warm emotional atmosphere"
        ),
        "scene_flavor": {
            "morning": "dappled morning sunlight dancing across a wildflower garden, rich creamy oil pastel strokes of lilac, buttercup yellow, and sage",
            "afternoon": "sunlit orchard path with vibrant textured strokes of golden light on grassy banks and apple tree boughs",
            "evening": "glowing sunset over a tranquil pond with thick textured reflections of apricot, rose, and violet in the water",
        },
        "context_tags": ["impressionist", "monet", "cassatt", "oil pastel", "painting", "art"],
    },
    "botanical_field_guide": {
        "visual_medium": "botanical_field_guide",
        "name": "19th-Century Botanical Naturalist Field Guide",
        "prompt_style": (
            "Vintage 19th-century naturalist field guide illustration, delicate scientific watercolor with fine sepia calligraphic ink details, "
            "labeled botanical flora and gentle garden birds in soft marginal vignetting, aged warm ivory parchment paper background, archival plate aesthetic"
        ),
        "scene_flavor": {
            "morning": "morning botanical study with dew-kissed fern fronds, flowering dogwood boughs, and delicate morning warbler in soft sepia watercolor",
            "afternoon": "sunlit garden flora with detailed cross-sections of wildflowers, fluttering swallowtail butterflies, and warm botanical watercolor washes",
            "evening": "twilight garden study featuring night-blooming jasmine, soft moth wings, and gentle sepia wash shadows on warm tea-stained paper",
        },
        "context_tags": ["botanical", "field guide", "nature", "vintage", "science", "flowers"],
    },
    "isometric_toy_world": {
        "visual_medium": "isometric_toy_world",
        "name": "Charming Isometric Miniature Toy World",
        "prompt_style": (
            "Charming 3D isometric miniature diorama, handcrafted wooden and ceramic miniature buildings, tiny sculpted trees, "
            "warm golden overhead sunlight, soft tilt-shift focus, adorable tiny toy town square with cobblestone detail and cheerful miniature greenery"
        ),
        "scene_flavor": {
            "morning": "isometric morning town square with tiny bakery cart, miniature ceramic planters, and long warm diagonal morning shadows",
            "afternoon": "busy miniature park with tiny wooden footbridge over a clear resin pond, green felt lawn, and toy wooden benches",
            "evening": "twilight miniature village with tiny glowing LED streetlamps casting warm puddles of light across the isometric cobblestones",
        },
        "context_tags": ["isometric", "diorama", "miniature", "toy", "craft"],
    },
    "chalk_twilight": {
        "visual_medium": "chalk_twilight",
        "name": "Soft Pastel & Twilight Chalkboard",
        "prompt_style": (
            "Soft colored chalk and luminous pastel on dark textured paper, glowing radiant highlights, dreamy smudged edges, "
            "magical twilight embers and fireflies, whimsical dreamy nightfall atmosphere with vibrant luminous pastels cutting through velvety dark tones"
        ),
        "scene_flavor": {
            "morning": "soft morning chalk pastel with radiant golden and coral sunbeams breaking through misty velvety charcoal tree silhouettes",
            "afternoon": "vibrant colorful chalk meadow with bright turquoise skies, sunlit lemon-yellow wildflowers, and tactile powdery texture",
            "evening": "magical twilight hillside with glowing golden fireflies, luminous chalk stars, and deep velvety indigo sky with soft pink horizon glow",
        },
        "context_tags": ["chalk", "pastel", "twilight", "fireflies", "dreamy"],
    },
    "pixar_storybook_light": {
        "visual_medium": "pixar_storybook_light",
        "name": "Modern Pixar 3D Cinematic Storybook",
        "prompt_style": (
            "Modern Pixar animated feature 3D concept art lighting, rich cinematic volumetric rim lighting, soft subsurface scattering, "
            "stylized appealing character proportions, warm emotional cinematic depth, lush environmental rendering with soft depth of field"
        ),
        "scene_flavor": {
            "morning": "golden morning sunbeams streaming through a cozy breakfast nook window, soft volumetric dust motes, rich warm lighting",
            "afternoon": "bright cinematic sunlit backyard lawn with lush subsurface scattering on vibrant green leaves and vivid colorful toys",
            "evening": "cozy twilight backyard patio with warm glowing fairy light bulbs, deep cinematic blue hour atmosphere, and emotional cinematic warmth",
        },
        "context_tags": ["pixar", "3d", "cinematic", "disney", "animation", "movie"],
    },
}


# ---------------------------------------------------------------------------
# 3. Forecast Retrieval & Clothing State Mapping
# ---------------------------------------------------------------------------
def _get_ha_credentials() -> tuple[str, str]:
    """Retrieve Home Assistant base URL and token from env or /secrets/env.json."""
    base_url = os.environ.get("HA_BASE_URL", "")
    token = os.environ.get("HA_ACCESS_TOKEN", "")

    if (not base_url or not token) and Path("/secrets/env.json").exists():
        try:
            with open("/secrets/env.json", "r", encoding="utf-8") as f:
                d = json.load(f)
                base_url = base_url or d.get("HA_BASE_URL", "")
                token = token or d.get("HA_ACCESS_TOKEN", "")
        except Exception as e:
            log.warning("Could not read HA credentials from /secrets/env.json: %s", e)

    return base_url.rstrip("/"), token


def map_weather_to_clothing_state(
    slot: str,
    temp_f: float,
    condition: str,
    precip: float = 0.0,
    precip_prob: float = 0.0,
) -> str:
    """Map weather parameters to one of 7 Hearth clothing states.

    States: morning_rain, rainy_boots, cold_puffer, morning_mild, cool_evening, warm_pants, hot_dress.
    """
    cond_norm = condition.lower()
    is_rain = (
        any(k in cond_norm for k in ("rain", "pour", "drizzle", "shower", "thunder", "storm", "wet"))
        or precip > 0.04
        or precip_prob >= 40.0
    )

    if is_rain:
        return "morning_rain" if slot == "morning" else "rainy_boots"

    if temp_f <= 48.0:
        return "cold_puffer"

    if temp_f >= 75.0:
        return "hot_dress"

    # Mild / Temperate bracket (49°F - 74°F)
    if slot == "morning":
        return "morning_mild" if temp_f >= 52.0 else "cold_puffer"
    elif slot == "evening":
        return "cool_evening" if temp_f <= 62.0 else "warm_pants"
    else:  # afternoon
        return "warm_pants"


def fetch_tomorrow_slots_forecast(target_date: str | None = None) -> dict[str, Any]:
    """Fetch hourly forecasts from Home Assistant weather.forecast_home for 8 AM, 2 PM, 6 PM PT."""
    now_pt = datetime.now(PT_TZ)
    if target_date:
        target_date_str = target_date.strip()
    else:
        target_dt = now_pt + timedelta(days=1)
        target_date_str = target_dt.strftime("%Y-%m-%d")

    target_hours = {
        "morning": 8,
        "afternoon": 14,
        "evening": 18,
    }

    ha_url, ha_token = _get_ha_credentials()
    raw_hourly_forecasts: list[dict[str, Any]] = []

    if ha_url and ha_token:
        import urllib.request

        headers = {
            "Authorization": f"Bearer {ha_token}",
            "Content-Type": "application/json",
        }
        for entity in ["weather.forecast_home", "weather.home"]:
            url = f"{ha_url}/api/services/weather/get_forecasts?return_response"
            payload = json.dumps({"entity_id": entity, "type": "hourly"}).encode("utf-8")
            try:
                req = urllib.request.Request(url, data=payload, headers=headers, method="POST")
                with urllib.request.urlopen(req, timeout=10) as resp:
                    if resp.status == 200:
                        data = json.loads(resp.read().decode("utf-8"))
                        payload_root = data.get("service_response", data) if isinstance(data, dict) else {}
                        entity_block = payload_root.get(entity, {}) if isinstance(payload_root, dict) else {}
                        fc = entity_block.get("forecast", []) if isinstance(entity_block, dict) else []
                        if fc:
                            raw_hourly_forecasts = fc
                            log.info("Fetched %d hourly forecast entries from %s", len(fc), entity)
                            break
            except Exception as e:
                log.debug("HA forecast service query failed on %s: %s", entity, e)

    # Process slots from HA hourly forecasts or fallback to seasonal defaults
    slots: dict[str, dict[str, Any]] = {}
    month = int(target_date_str.split("-")[1]) if "-" in target_date_str else now_pt.month
    is_winter = month in (11, 12, 1, 2)
    is_summer = month in (6, 7, 8)

    for slot_name, target_h in target_hours.items():
        matched_item = None
        min_diff = 999999

        for item in raw_hourly_forecasts:
            dt_str = item.get("datetime")
            if not dt_str or not isinstance(dt_str, str):
                continue
            try:
                # Parse ISO timestamp and convert timezone-aware datetimes to PT_TZ before matching
                item_dt = datetime.fromisoformat(dt_str)
                if item_dt.tzinfo is not None:
                    item_dt = item_dt.astimezone(PT_TZ)
                if item_dt.strftime("%Y-%m-%d") != target_date_str:
                    continue
                diff = abs(item_dt.hour - target_h)
                if diff < min_diff:
                    min_diff = diff
                    matched_item = item
            except Exception:
                continue

        if matched_item and min_diff <= 3:
            temp_f = float(matched_item.get("temperature", 60.0))
            condition = str(matched_item.get("condition", "partlycloudy"))
            precip = float(matched_item.get("precipitation", 0.0) or 0.0)
            precip_prob = float(matched_item.get("precipitation_probability", 0.0) or 0.0)
        else:
            # Deterministic seasonal fallback for Northern California
            if is_winter:
                default_temps = {"morning": 46.0, "afternoon": 58.0, "evening": 50.0}
            elif is_summer:
                default_temps = {"morning": 62.0, "afternoon": 82.0, "evening": 70.0}
            else:
                default_temps = {"morning": 54.0, "afternoon": 68.0, "evening": 58.0}
            temp_f = default_temps[slot_name]
            condition = "sunny" if slot_name == "afternoon" else "partlycloudy"
            precip = 0.0
            precip_prob = 0.0

        clothing_state = map_weather_to_clothing_state(
            slot=slot_name,
            temp_f=temp_f,
            condition=condition,
            precip=precip,
            precip_prob=precip_prob,
        )

        slots[slot_name] = {
            "hour": target_h,
            "temp_f": round(temp_f, 1),
            "condition": condition,
            "precipitation": precip,
            "precipitation_probability": precip_prob,
            "clothing_state": clothing_state,
        }

    return {
        "target_date": target_date_str,
        "slots": slots,
        "source": "ha_forecast" if raw_hourly_forecasts else "seasonal_estimate",
    }


# ---------------------------------------------------------------------------
# 4. Family Context Collector
# ---------------------------------------------------------------------------
def collect_family_context() -> dict[str, Any]:
    """Collect family gaming, media, book, and calendar context across the last 7 days.

    Preserves exact Switch title fidelity: maps Ocarina of Time to Kokiri Forest,
    Lon Lon Ranch, glowing fairy Navi, and Great Deku Tree storybook scenes.
    """
    ctx: dict[str, Any] = {
        "switch": [],
        "steam": [],
        "tautulli": [],
        "books": [],
        "calendar": [],
        "matched_themes": [],
        "special_notes": [],
    }

    # 1. Switch 2 Data & Profile
    switch_titles: list[str] = []
    switch_json = DATA_DIR / "switch_data.json"
    if switch_json.exists():
        try:
            data = json.loads(switch_json.read_text(encoding="utf-8"))
            for dev in data.get("devices", []):
                for daily in dev.get("dailySummaries", []):
                    for app in daily.get("playedApps", []):
                        title = app.get("application", {}).get("title")
                        if title and title not in switch_titles:
                            switch_titles.append(title)
        except Exception as e:
            log.debug("Error parsing switch_data.json: %s", e)

    for switch_md in [
        Path("/workspace/memory/private/media_gaming_switch_profile.md"),
        Path("/workspace/memory/media_gaming_switch_profile.md"),
    ]:
        if switch_md.exists():
            try:
                text = switch_md.read_text(encoding="utf-8")
                in_played_section = False
                for line in text.splitlines():
                    if "played switch" in line.lower() or "played titles" in line.lower():
                        in_played_section = True
                        continue
                    if in_played_section and line.startswith("##"):
                        in_played_section = False
                        continue
                    if in_played_section:
                        m = re.search(r"-\s+\*\*([^*]+)\*\*", line)
                        if m:
                            t = m.group(1).strip().rstrip(":")
                            if t and t not in switch_titles:
                                switch_titles.append(t)
            except Exception as e:
                log.debug("Error parsing switch profile markdown: %s", e)
            break

    ctx["switch"] = switch_titles

    # Check exact title fidelity: Ocarina of Time mapping
    oot_matched = any("ocarina of time" in t.lower() for t in switch_titles)
    if oot_matched:
        ctx["matched_themes"].append("zelda_oot_kokiri_storybook")
        ctx["special_notes"].append(
            "Switch 2 logged 'The Legend of Zelda: Ocarina of Time'. Preserve exact title fidelity: "
            "map to Kokiri Forest ancient roots, Lon Lon Ranch, glowing fairy Navi, and Great Deku Tree storybook scenes."
        )

    if any("breath of the wild" in t.lower() or "totk" in t.lower() or "tears of the kingdom" in t.lower() for t in switch_titles):
        ctx["matched_themes"].append("zelda_botw_watercolor")

    if any("link's awakening" in t.lower() for t in switch_titles):
        ctx["matched_themes"].append("links_awakening_diorama")

    if any("mario" in t.lower() for t in switch_titles):
        ctx["matched_themes"].append("pixar_storybook_light")

    # 2. Steam & Xbox Profiles
    for steam_md in [
        Path("/workspace/memory/private/media_gaming_steam_profile.md"),
        Path("/workspace/memory/media_gaming_steam_profile.md"),
    ]:
        if steam_md.exists():
            try:
                text = steam_md.read_text(encoding="utf-8")
                if "chrono trigger" in text.lower() or "pixel" in text.lower() or "snes" in text.lower():
                    ctx["matched_themes"].append("snes_pixel_storybook")
                if "spider-man" in text.lower():
                    ctx["matched_themes"].append("spiderverse_halftone")
            except Exception:
                pass
            break

    # 3. Tautulli & Plex Profile
    for plex_md in [
        Path("/workspace/memory/private/media_plex_taste_profile.md"),
        Path("/workspace/memory/media_plex_taste_profile.md"),
    ]:
        if plex_md.exists():
            try:
                text = plex_md.read_text(encoding="utf-8")
                if any(k in text.lower() for k in ("ghibli", "totoro", "kiki", "ponyo", "mononoke")):
                    ctx["matched_themes"].append("studio_ghibli_gouache")
                if "spider-verse" in text.lower():
                    ctx["matched_themes"].append("spiderverse_halftone")
                if "calvin" in text.lower():
                    ctx["matched_themes"].append("calvin_hobbes_ink_wash")
            except Exception:
                pass
            break

    # 4. Books & Podcasts
    for book_md in [
        Path("/workspace/memory/private/media_and_podcasts.md"),
        Path("/workspace/memory/media_and_podcasts.md"),
    ]:
        if book_md.exists():
            try:
                text = book_md.read_text(encoding="utf-8")
                if "calvin and hobbes" in text.lower():
                    ctx["matched_themes"].append("calvin_hobbes_ink_wash")
                if "nordic" in text.lower() or "folk" in text.lower():
                    ctx["matched_themes"].append("nordic_folk_storybook")
            except Exception:
                pass
            break

    # Deduplicate matched themes
    ctx["matched_themes"] = list(dict.fromkeys(ctx["matched_themes"]))
    return ctx


# ---------------------------------------------------------------------------
# 5. Art Style Selection & History Rotation (12-Day Cooldown, 60/40 Weighting)
# ---------------------------------------------------------------------------
def load_theme_history(history_file: Path = DEFAULT_HISTORY_FILE) -> list[dict[str, Any]]:
    """Load historical theme selections."""
    if not history_file.exists():
        return []
    try:
        data = json.loads(history_file.read_text(encoding="utf-8"))
        if isinstance(data, list):
            return data
    except Exception as e:
        log.warning("Could not read theme history from %s: %s", history_file, e)
    return []


def record_theme_history(
    target_date: str,
    theme_key: str,
    visual_medium: str,
    context_source: str,
    history_file: Path = DEFAULT_HISTORY_FILE,
) -> None:
    """Record selected theme into history with atomic write."""
    history = load_theme_history(history_file)
    entry = {
        "date": target_date,
        "theme_key": theme_key,
        "visual_medium": visual_medium,
        "context_source": context_source,
        "recorded_at": datetime.now(PT_TZ).isoformat(),
    }
    # Update existing entry for target_date or append
    updated = False
    for i, h in enumerate(history):
        if h.get("date") == target_date:
            history[i] = entry
            updated = True
            break
    if not updated:
        history.append(entry)

    # Sort by date
    history.sort(key=lambda x: str(x.get("date", "")))

    history_file.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = history_file.with_suffix(f".tmp.{os.getpid()}")
    tmp_path.write_text(json.dumps(history, indent=2), encoding="utf-8")
    tmp_path.replace(history_file)
    log.info("Recorded theme history entry for %s: %s (%s)", target_date, theme_key, visual_medium)


def select_daily_theme(
    family_context: dict[str, Any],
    target_date: str,
    forced_theme: str | None = None,
    seed: int | None = None,
    history_file: Path = DEFAULT_HISTORY_FILE,
) -> tuple[dict[str, Any], str]:
    """Select a non-repeating daily theme enforcing a 12-day cooldown and 60/40 context weighting.

    Returns (theme_dict, selection_reason).
    """
    if forced_theme:
        if forced_theme not in ART_STYLE_VAULT:
            valid_keys = ", ".join(sorted(ART_STYLE_VAULT.keys()))
            raise ValueError(f"Unknown theme {forced_theme!r}. Available themes: {valid_keys}")
        return ART_STYLE_VAULT[forced_theme], f"forced_cli (--theme={forced_theme})"

    history = load_theme_history(history_file)
    try:
        target_dt = datetime.strptime(target_date, "%Y-%m-%d").date()
    except Exception:
        target_dt = datetime.now(PT_TZ).date()

    # Identify themes and visual mediums on 12-day cooldown
    cooldown_keys = set()
    cooldown_mediums = set()
    for entry in history:
        entry_date_str = entry.get("date")
        if not entry_date_str:
            continue
        try:
            entry_dt = datetime.strptime(entry_date_str, "%Y-%m-%d").date()
            day_diff = (target_dt - entry_dt).days
            if 0 <= day_diff < 12:
                cooldown_keys.add(entry.get("theme_key"))
                cooldown_mediums.add(entry.get("visual_medium"))
        except Exception:
            continue

    eligible_theme_keys = [
        k for k, v in ART_STYLE_VAULT.items()
        if k not in cooldown_keys and v["visual_medium"] not in cooldown_mediums
    ]

    # Defensive fallback if all themes are on cooldown
    if not eligible_theme_keys:
        log.warning("All %d themes on 12-day cooldown; resetting eligible pool to all vault themes", len(ART_STYLE_VAULT))
        eligible_theme_keys = list(ART_STYLE_VAULT.keys())

    rng = random.Random(seed) if seed is not None else random.Random()

    # Identify eligible context candidates
    context_candidates = [
        k for k in family_context.get("matched_themes", [])
        if k in eligible_theme_keys
    ]

    # 60/40 Decision Rule
    roll = rng.random()
    if roll < 0.60 and context_candidates:
        chosen_key = rng.choice(context_candidates)
        reason = f"family_context (matched {chosen_key!r}, roll={roll:.2f} < 0.60)"
    else:
        chosen_key = rng.choice(eligible_theme_keys)
        reason = f"vault_rotation (roll={roll:.2f}, pool={len(eligible_theme_keys)})"

    return ART_STYLE_VAULT[chosen_key], reason


# ---------------------------------------------------------------------------
# 6. Prompt Construction
# ---------------------------------------------------------------------------
def build_slot_prompt(
    slot: str,
    theme: dict[str, Any],
    weather_info: dict[str, Any],
    family_context: dict[str, Any] | None = None,
) -> str:
    """Build a complete, surgical 16:9 prompt for a specific weather card slot.

    Includes style medium, slot lighting, explicit forecast weather atmosphere,
    weather-matched attire override mandate, scene flavor, character consistency clause,
    right-weighted 42% composition invariant, and negative guardrails.
    """
    temp_f = float(weather_info.get("temp_f", 60.0))
    condition = str(weather_info.get("condition", "partlycloudy") or "partlycloudy").strip()
    precip = float(weather_info.get("precipitation", 0.0) or 0.0)
    precip_prob = float(weather_info.get("precipitation_probability", 0.0) or 0.0)

    clothing_state = weather_info.get("clothing_state") or map_weather_to_clothing_state(
        slot=slot,
        temp_f=temp_f,
        condition=condition,
        precip=precip,
        precip_prob=precip_prob,
    )
    clothing_desc = CLOTHING_STATE_PROMPTS.get(clothing_state, CLOTHING_STATE_PROMPTS["morning_mild"])
    lighting_desc = SLOT_LIGHTING.get(slot, SLOT_LIGHTING["morning"])

    cond_norm = condition.lower()
    is_rain = (
        any(k in cond_norm for k in ("rain", "pour", "drizzle", "shower", "thunder", "storm", "wet"))
        or precip > 0.04
        or precip_prob >= 40.0
    )
    if is_rain:
        sky_desc = "overcast rain showers, wet glistening surfaces, and raindrop ripples in puddles"
    elif any(k in cond_norm for k in ("cloud", "overcast", "fog", "mist", "haz")):
        if "partly" in cond_norm:
            sky_desc = "partly cloudy skies with drifting clouds and soft sunbreaks"
        else:
            sky_desc = "overcast cloudy skies with soft diffused light and misty air"
    elif any(k in cond_norm for k in ("clear", "sun")):
        sky_desc = "warm sunshine and clear skies" if slot != "evening" else "clear twilight skies"
    else:
        sky_desc = f"visible {condition} weather conditions"

    if temp_f <= 48.0:
        temp_feel = "chilly brisk air"
    elif temp_f <= 60.0:
        temp_feel = "cool crisp air"
    elif temp_f < 75.0:
        temp_feel = "pleasant mild air"
    else:
        temp_feel = "warm summery air"

    precip_parts: list[str] = []
    if precip > 0.0:
        precip_parts.append(f"precipitation {precip:.2f} in")
    if precip_prob > 0.0:
        precip_parts.append(f"precipitation probability {precip_prob:.0f}%")
    precip_summary = f", {', '.join(precip_parts)}" if precip_parts else ""

    weather_atmosphere_clause = (
        f"Forecast Weather ({condition}, {temp_f:.0f}°F{precip_summary}): "
        f"Depict visible {sky_desc} with {temp_feel} matching the {condition} ({temp_f:.0f}°F) forecast "
        f"purely through environmental visuals and atmosphere without rendering any text or numbers."
    )

    attire_mandate_clause = (
        f"MANDATORY WEATHER-MATCHED ATTIRE ({clothing_state}): {clothing_desc} "
        f"Rosie, Isaac, and baby Genevieve's outfits MUST strictly match the forecast weather "
        f"(raincoats/umbrella/boots on wet forecasts, puffer coats/beanies in cold, sweaters/jackets in cool weather, "
        f"or light breezy clothes in hot weather) rather than copying clothing from the reference image or wearing generic fantasy costumes."
    )

    scene_flavor = theme.get("scene_flavor", {}).get(slot, "scenic picturesque outdoor landscape")
    prompt_style = theme.get("prompt_style", "")

    # Check for Ocarina of Time special notes
    special_flavor = ""
    if theme.get("visual_medium") == "zelda_oot_kokiri_storybook":
        special_flavor = (
            " Scene features the iconic storybook Kokiri Forest with ancient mossy roots, "
            "wooden boardwalk footbridges, and a glowing friendly winged fairy (Navi) hovering warmly near the children."
        )

    full_prompt = (
        f"{prompt_style}. "
        f"{attire_mandate_clause} "
        f"Atmosphere: {lighting_desc}. {weather_atmosphere_clause} "
        f"Setting: {scene_flavor}.{special_flavor} "
        f"Characters & Attire: {BASE_CHARACTER_CONSISTENCY_CLAUSE} "
        f"Current weather attire ({clothing_state}): {clothing_desc} "
        f"Composed in cinematic horizontal 16:9 widescreen with rich artistic storytelling depth."
    )
    return full_prompt


# ---------------------------------------------------------------------------
# 7. Deployment & Host 2 Synchronization
# ---------------------------------------------------------------------------
def deploy_to_host2(
    local_files: list[Path],
    remote_dir: str | None = None,
    timeout: int = 120,
) -> tuple[bool, str]:
    """Upload daily weather pack files to Host 2 via scp -O and prune >3-day-old images."""
    if not local_files:
        return False, "No local files provided for deployment"

    remote_target_dir = remote_dir or f"{nas_config.DOCKER_ROOT}/{HEARTH_REMOTE_REL_PATH}"

    # Build scp -O command line using nas_config parameters
    scp_cmd = [
        "scp",
        "-O",
        "-i", str(nas_config.SSH_KEY),
        "-P", str(nas_config.SSH_PORT),
        "-o", "StrictHostKeyChecking=no",
        "-o", "ConnectTimeout=15",
        "-o", "BatchMode=yes",
    ]
    for lf in local_files:
        if not lf.exists():
            return False, f"File does not exist on disk: {lf}"
        # Ensure local files have writable/readable 0644 permissions before upload
        try:
            os.chmod(lf, 0o644)
        except Exception as e:
            log.warning("Could not set 0644 permissions on %s: %s", lf, e)
        scp_cmd.append(str(lf.resolve()))

    scp_cmd.append(f"{nas_config.SSH_USER}@{nas_config.HOST_2_IP}:{remote_target_dir}/")

    # Ensure remote files have user write permissions so scp overwrites never fail with Permission denied
    pre_chmod_cmd = f"chmod -R u+w {remote_target_dir} 2>/dev/null || true"
    rc, stdout, stderr = nas_config.run_ssh(nas_config.HOST_2_IP, pre_chmod_cmd, timeout=30)
    if rc != 0:
        log.warning("Pre-upload chmod on Host 2 returned rc=%d: %s", rc, stderr.strip())

    log.info("Deploying %d files to Host 2 (%s:%s/)", len(local_files), nas_config.HOST_2_IP, remote_target_dir)
    try:
        proc = subprocess.run(
            scp_cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if proc.returncode != 0:
            err = proc.stderr.strip() or proc.stdout.strip()
            return False, f"SCP upload failed (rc={proc.returncode}): {err}"
    except Exception as e:
        return False, f"Failed executing SCP upload: {e}"

    # Prune >3-day-old daily_*.jpg files on Host 2 via SSH
    prune_cmd = f"find {remote_target_dir} -name 'daily_*.jpg' -mtime +3 -delete"
    rc, stdout, stderr = nas_config.run_ssh(nas_config.HOST_2_IP, prune_cmd, timeout=30)
    if rc != 0:
        log.warning("Prune command on Host 2 returned rc=%d: %s", rc, stderr.strip())
    else:
        log.info("Host 2 prune of >3-day-old daily_*.jpg completed successfully")

    return True, f"Successfully uploaded {len(local_files)} files to Host 2"


# ---------------------------------------------------------------------------
# 8. Schedule Registration
# ---------------------------------------------------------------------------
def install_schedule_entry(schedule_file: Path = DEFAULT_SCHEDULE_FILE) -> tuple[bool, str]:
    """Register daily 6:30 PM PT (18:30 PT) job in /workspace/data/schedule.json."""
    if not schedule_file.exists():
        schedule_data: list[dict[str, Any]] = []
    else:
        try:
            schedule_data = json.loads(schedule_file.read_text(encoding="utf-8"))
            if not isinstance(schedule_data, list):
                schedule_data = []
        except Exception as e:
            return False, f"Failed reading {schedule_file}: {e}"

    job_id = "hearth_daily_weather_art"
    target_job = {
        "id": job_id,
        "name": "Nightly Hearth Weather Art Generator",
        "enabled": True,
        "schedule_type": "daily",
        "hour_pt": 18,
        "minute_pt": 30,
        "prompt": (
            "Run nightly Hearth dynamic weather art pipeline via silent sidecar: "
            "python3 /workspace/tools/sidecars.py hearth_weather_art. "
            "Generates 3 cohesive weather cards (morning, afternoon, evening) with family context "
            "and character consistency, and syncs daily_manifest.json to Host 2. "
            "Silent background sidecar (silent on success, alert only on failure)."
        ),
        "catchup_if_missed": False,
        "channel_id": 1544953275877556334,
    }

    updated = False
    for i, job in enumerate(schedule_data):
        if job.get("id") == job_id:
            schedule_data[i] = target_job
            updated = True
            break
    if not updated:
        schedule_data.append(target_job)

    schedule_file.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = schedule_file.with_suffix(f".tmp.{os.getpid()}")
    tmp_path.write_text(json.dumps(schedule_data, indent=2), encoding="utf-8")
    tmp_path.replace(schedule_file)
    log.info("Installed '%s' in %s (daily 18:30 PT)", job_id, schedule_file)
    return True, f"Successfully registered '{job_id}' in {schedule_file}"


# ---------------------------------------------------------------------------
# 9. Main Pipeline Execution
# ---------------------------------------------------------------------------
class SlotUrl(str):
    """String URL subclass providing backward-compatible .get() for in-memory consumers."""

    def __new__(cls, content: str, details: dict[str, Any] | None = None) -> Self:
        obj = super().__new__(cls, content)
        obj._details = details or {}
        return obj

    def get(self, key: str, default: Any = None) -> Any:
        return self._details.get(key, default)


def run_nightly_pipeline(
    target_date: str | None = None,
    theme: str | None = None,
    seed: int | None = None,
    reference_image: str | None = None,
    output_dir: str | None = None,
    dry_run: bool = False,
    no_deploy: bool = False,
    history_file: Path = DEFAULT_HISTORY_FILE,
    timeout: int = DEFAULT_IMAGE_TIMEOUT,
    max_retries: int = DEFAULT_MAX_RETRIES,
    force: bool = False,
) -> dict[str, Any]:
    """Execute the nightly weather art pipeline end-to-end."""
    out_dir = Path(output_dir) if output_dir else DEFAULT_OUTPUT_DIR
    out_dir.mkdir(parents=True, exist_ok=True)

    # 1. Forecast & Target Date Resolution
    forecast_data = fetch_tomorrow_slots_forecast(target_date)
    resolved_date = forecast_data["target_date"]
    slots_info = forecast_data["slots"]
    log.info("Resolved target date: %s", resolved_date)

    # 2. Family Context Collection
    family_context = collect_family_context()
    log.info(
        "Family context collected: %d Switch titles, %d matched themes",
        len(family_context["switch"]),
        len(family_context["matched_themes"]),
    )

    # Inspect existing manifest for same-date theme-locked resume
    manifest_path = out_dir / "daily_manifest.json"
    existing_manifest: dict[str, Any] | None = None
    if manifest_path.exists():
        try:
            parsed = json.loads(manifest_path.read_text(encoding="utf-8"))
            if isinstance(parsed, dict):
                existing_manifest = parsed
        except Exception as e:
            log.warning("Could not read existing manifest from %s: %s", manifest_path, e)

    # 3. Theme Selection (with same-date theme locking on resume)
    selected_theme: dict[str, Any]
    selection_reason: str
    existing_date = (
        existing_manifest.get("target_date") or existing_manifest.get("date")
        if existing_manifest
        else None
    )
    if (
        not force
        and not theme
        and existing_manifest
        and existing_date == resolved_date
        and existing_manifest.get("theme_key") in ART_STYLE_VAULT
    ):
        locked_theme_key = existing_manifest["theme_key"]
        selected_theme = ART_STYLE_VAULT[locked_theme_key]
        selection_reason = f"theme_locked_resume ({locked_theme_key})"
        log.info("Locked daily theme from existing manifest: %s (%s)", selected_theme["name"], selection_reason)
    else:
        selected_theme, selection_reason = select_daily_theme(
            family_context=family_context,
            target_date=resolved_date,
            forced_theme=theme,
            seed=seed,
            history_file=history_file,
        )
    theme_key = selected_theme["visual_medium"]
    log.info("Selected theme: %s (%s)", selected_theme["name"], selection_reason)

    # Resolve Reference Image
    ref_img_path = None
    if reference_image:
        p = Path(reference_image)
        if p.exists() and p.is_file():
            ref_img_path = str(p.resolve())
    elif DEFAULT_REF_IMAGE.exists() and DEFAULT_REF_IMAGE.is_file():
        ref_img_path = str(DEFAULT_REF_IMAGE.resolve())

    # 4. Generate Slot Prompts & Images
    manifest_slots: dict[str, Any] = {}
    generated_image_paths: list[Path] = []
    pipeline_success = True

    # Check whether existing manifest matches target date and selected theme for slot reuse
    manifest_matches_context = (
        not dry_run
        and not force
        and existing_manifest is not None
        and existing_date == resolved_date
        and existing_manifest.get("theme_key") == theme_key
    )

    for slot_name in ("morning", "afternoon", "evening"):
        slot_weather = slots_info[slot_name]
        prompt = build_slot_prompt(
            slot=slot_name,
            theme=selected_theme,
            weather_info=slot_weather,
            family_context=family_context,
        )
        card_filename = f"daily_{resolved_date}_{slot_name}.jpg"
        target_card_path = out_dir / card_filename

        slot_manifest_entry = {
            "hour": slot_weather["hour"],
            "clothing_state": slot_weather["clothing_state"],
            "temp_f": slot_weather["temp_f"],
            "condition": slot_weather["condition"],
            "precipitation": slot_weather["precipitation"],
            "precipitation_probability": slot_weather["precipitation_probability"],
            "filename": card_filename,
            "relative_url": f"/static/ui/assets/weather-packs/ghibli_rosie_isaac_eve/{card_filename}",
            "prompt": prompt,
            "valid": False,
        }

        if dry_run:
            log.info("[DRY-RUN] Slot %s: %s (clothing=%s)", slot_name, card_filename, slot_weather["clothing_state"])
            slot_manifest_entry["valid"] = True
            slot_manifest_entry["dry_run"] = True
            manifest_slots[slot_name] = slot_manifest_entry
            continue

        # Check for valid slot reuse on resume
        reused = False
        if manifest_matches_context and existing_manifest:
            existing_slots_meta = existing_manifest.get("slot_details")
            if not isinstance(existing_slots_meta, dict):
                raw_slots = existing_manifest.get("slots")
                existing_slots_meta = raw_slots if isinstance(raw_slots, dict) else {}

            existing_slot = existing_slots_meta.get(slot_name, {})
            if isinstance(existing_slot, dict) and existing_slot.get("valid") is True and target_card_path.exists():
                chk = check_image(target_card_path)
                if chk.get("valid"):
                    log.info(
                        "Reusing verified existing card for %s slot: %s (%dx%d, %s KB)",
                        slot_name,
                        card_filename,
                        chk["width"],
                        chk["height"],
                        chk["size_kb"],
                    )
                    slot_manifest_entry.update({
                        "valid": True,
                        "width": chk["width"],
                        "height": chk["height"],
                        "format": chk["format"],
                        "size_kb": chk["size_kb"],
                        "reused": True,
                    })
                    generated_image_paths.append(target_card_path)
                    manifest_slots[slot_name] = slot_manifest_entry
                    reused = True
                else:
                    log.warning(
                        "Existing card for %s slot failed forensic verification: %s",
                        slot_name,
                        chk.get("error"),
                    )

        if reused:
            continue

        # Generate image with per-slot retries
        success = False
        last_error = ""
        total_attempts = 1 + max(0, max_retries)

        for attempt in range(1, total_attempts + 1):
            log.info(
                "Generating card for %s slot (attempt %d/%d, timeout=%ds): %s",
                slot_name,
                attempt,
                total_attempts,
                timeout,
                card_filename,
            )
            gen_res = generate_image_subscription(
                prompt=prompt,
                aspect_ratio="16:9",
                image_name=target_card_path.stem,
                output_dir=str(out_dir),
                reference_image=ref_img_path,
                exact_filename=True,
                timeout=timeout,
            )

            if not gen_res.get("success"):
                last_error = gen_res.get("error") or "Unknown generation error"
                log.warning("Attempt %d/%d failed for %s slot: %s", attempt, total_attempts, slot_name, last_error)
                continue

            produced_path = Path(gen_res["path"])
            # Forensically verify image on disk
            chk = check_image(produced_path)
            if not chk.get("valid"):
                last_error = chk.get("error") or "Image validation failed"
                log.warning(
                    "Validation for attempt %d/%d failed for %s (%s): %s",
                    attempt,
                    total_attempts,
                    slot_name,
                    produced_path,
                    last_error,
                )
                continue

            log.info("Verified %s: %dx%d, %s KB (attempt %d)", card_filename, chk["width"], chk["height"], chk["size_kb"], attempt)
            slot_manifest_entry.update({
                "valid": True,
                "width": chk["width"],
                "height": chk["height"],
                "format": chk["format"],
                "size_kb": chk["size_kb"],
            })
            if attempt > 1:
                slot_manifest_entry["retries"] = attempt - 1
            generated_image_paths.append(produced_path)
            success = True
            break

        if not success:
            log.error("All %d attempts failed for %s slot: %s", total_attempts, slot_name, last_error)
            slot_manifest_entry["error"] = last_error
            pipeline_success = False

        manifest_slots[slot_name] = slot_manifest_entry

    # 5. Compile and Write daily_manifest.json (Hearth frontend contract)
    manifest = {
        "target_date": resolved_date,
        "date": resolved_date,
        "style_label": selected_theme["name"],
        "theme_title": selected_theme["name"],
        "generated_at": datetime.now(PT_TZ).isoformat(),
        "theme_key": theme_key,
        "visual_medium": selected_theme["visual_medium"],
        "theme_name": selected_theme["name"],
        "selection_reason": selection_reason,
        "reference_image": ref_img_path,
        "dry_run": dry_run,
        "context_used": {
            "switch_titles": family_context["switch"][:5],
            "matched_themes": family_context["matched_themes"],
            "special_notes": family_context["special_notes"],
        },
        "slots": {
            slot: SlotUrl(entry.get("relative_url", ""), entry)
            for slot, entry in manifest_slots.items()
        },
        "slot_details": manifest_slots,
    }

    manifest_path = out_dir / "daily_manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    log.info("Wrote manifest to %s", manifest_path)

    # 6. Record history (only if real generation succeeded or in dry_run)
    if pipeline_success:
        record_theme_history(
            target_date=resolved_date,
            theme_key=theme_key,
            visual_medium=selected_theme["visual_medium"],
            context_source=selection_reason,
            history_file=history_file,
        )

    # 7. Deployment to Host 2
    deploy_status: dict[str, Any] = {"deployed": False}
    if not dry_run and not no_deploy and pipeline_success:
        files_to_deploy = [manifest_path] + generated_image_paths
        ok, msg = deploy_to_host2(files_to_deploy)
        deploy_status = {"deployed": ok, "message": msg}
        if not ok:
            log.error("Host 2 deployment failed: %s", msg)
        else:
            log.info("Host 2 deployment successful: %s", msg)

    return {
        "success": pipeline_success,
        "target_date": resolved_date,
        "theme": selected_theme["name"],
        "manifest_path": str(manifest_path),
        "manifest": manifest,
        "deploy": deploy_status,
    }


# ---------------------------------------------------------------------------
# 10. CLI Entry Point
# ---------------------------------------------------------------------------
def main() -> int:
    parser = argparse.ArgumentParser(description="Nightly Hearth Dynamic Weather Art Pipeline")
    parser.add_argument("--target-date", type=str, default=None, help="Target date YYYY-MM-DD (defaults to tomorrow PT)")
    parser.add_argument("--theme", type=str, default=None, help="Force specific theme key from ART_STYLE_VAULT")
    parser.add_argument("--seed", type=int, default=None, help="Random seed for deterministic theme selection")
    parser.add_argument("--reference-image", type=str, default=None, help="Reference image path for conditioning")
    parser.add_argument("--output-dir", type=str, default=None, help="Local directory to output images and manifest")
    parser.add_argument("--dry-run", action="store_true", help="Build prompts and manifest without generating images or deploying")
    parser.add_argument("--no-deploy", action="store_true", help="Generate images locally without deploying to Host 2")
    parser.add_argument("--timeout", type=int, default=DEFAULT_IMAGE_TIMEOUT, help="Per-slot image generation timeout in seconds (default 240)")
    parser.add_argument("--max-retries", type=int, default=DEFAULT_MAX_RETRIES, help="Maximum retry attempts per failed slot (default 2)")
    parser.add_argument("--force", action="store_true", help="Force regeneration of slots even if valid existing cards exist")
    parser.add_argument("--install-schedule", action="store_true", help="Register daily 6:30 PM PT job in schedule.json")

    args = parser.parse_args()

    if args.install_schedule:
        ok, msg = install_schedule_entry()
        print(f"[Schedule] {msg}")
        if not args.dry_run and not args.theme and not args.target_date:
            return 0 if ok else 1

    res = run_nightly_pipeline(
        target_date=args.target_date,
        theme=args.theme,
        seed=args.seed,
        reference_image=args.reference_image,
        output_dir=args.output_dir,
        dry_run=args.dry_run,
        no_deploy=args.no_deploy,
        timeout=args.timeout,
        max_retries=args.max_retries,
        force=args.force,
    )

    print(json.dumps(res, indent=2))
    return 0 if res["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
