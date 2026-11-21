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
    "Three siblings with matching golden-blonde hair and light blue-green eyes: "
    "1) Rosie: 7-year-old girl, tallest, long wavy golden-blonde hair, light blue-green eyes. "
    "2) Isaac: 3-year-old toddler boy, noticeably shorter than Rosie (chest-height), tousled golden-blonde hair, light blue-green eyes. "
    "3) Genevieve (Eve): 3-month-old infant baby girl, tiny newborn-sized baby, wispy golden-blonde hair, light blue-green eyes, always securely cradled or in a chest wrap (never standing or alone). "
    "ANTI-PORTRAIT RULE: Depict candid natural interaction in motion—NEVER a stiff front-facing posed family portrait staring straight into the camera. "
    "COMPOSITION: 16:9 widescreen; cluster all three children in the RIGHT 42%, leaving the LEFT 58% open landscape free of faces. "
    "GUARDRAILS & ZERO-UI INVARIANT: No dark hair, no brown hair, no black hair, no older Eve, no text, no watermarks, no icons, no borders."
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

SLOT_CHOREOGRAPHY: dict[str, str] = {
    "morning": (
        "Camera & Candid Choreography (Morning): Dynamic low-angle three-quarter candid shot from the right foreground; "
        "toddler Isaac points excitedly across the frame toward the open left scenery while Rosie leans in beside him "
        "laughing and securely cradling swaddled baby Genevieve (Eve), all three engaged in the world rather than looking at the lens."
    ),
    "afternoon": (
        "Camera & Candid Choreography (Afternoon): Lively wide side-profile and three-quarter mid-stride exploration shot; "
        "Rosie and toddler Isaac walk and explore actively along the right side of the path with baby Genevieve (Eve) "
        "tucked safely in a snug front wrap or Rosie's arms, capturing spontaneous midday movement and curiosity."
    ),
    "evening": (
        "Camera & Candid Choreography (Evening): Cozy elevated-angle dusk rim-lit candid moment in the right foreground; "
        "Rosie and toddler Isaac pause together in profile and three-quarter view gazing out toward the glowing left horizon "
        "while baby Genevieve (Eve) rests peacefully cradled against Rosie's shoulder."
    ),
}


def get_slot_lighting(slot: str, condition: str, is_rain: bool) -> str:
    """Return weather-synchronized slot lighting that avoids sunny/overcast contradictions."""
    cond_norm = (condition or "").strip().lower()
    if is_rain:
        rain_lighting = {
            "morning": "Soft diffused rainy morning light, cool wet reflections, gentle rain-washed atmosphere, 8:00 AM dawn ambiance",
            "afternoon": "Diffused overcast rainy afternoon light, glistening wet reflections, moody silver-gray rain illumination, 2:00 PM daylight",
            "evening": "Moody rainy twilight dusk, warm wet surface reflections against deep slate-blue clouds, gentle evening rain atmosphere, 6:00 PM dusk",
        }
        return rain_lighting.get(slot, rain_lighting["morning"])

    if any(k in cond_norm for k in ("cloud", "overcast", "fog", "mist", "haz")):
        if "partly" in cond_norm:
            partly_lighting = {
                "morning": "Dappled morning sunbreaks through drifting clouds, soft low-angle golden dawn light, fresh atmosphere, 8:00 AM dawn ambiance",
                "afternoon": "Dappled afternoon sunbreaks through drifting clouds, warm natural daylight illumination with soft cloud shadows, 2:00 PM sun",
                "evening": "Warm golden-hour dusk sunbreaks through drifting evening clouds, glowing amber-violet twilight sky, 6:00 PM dusk",
            }
            return partly_lighting.get(slot, partly_lighting["morning"])

        overcast_lighting = {
            "morning": "Soft diffused overcast morning light, gentle misty cloud illumination, shadowless silver-pearl morning atmosphere, 8:00 AM dawn ambiance",
            "afternoon": "Soft diffused overcast afternoon daylight, even cloud-filtered illumination, atmospheric misty depth, 2:00 PM daylight",
            "evening": "Soft cloud-diffused twilight dusk, muted violet-amber horizon glow beneath overcast skies, cozy low-contrast evening light, 6:00 PM dusk",
        }
        return overcast_lighting.get(slot, overcast_lighting["morning"])

    return SLOT_LIGHTING.get(slot, SLOT_LIGHTING["morning"])

# ---------------------------------------------------------------------------
# 2. Art Style Vault (19+ Distinct Visual Mediums)
# ---------------------------------------------------------------------------
ART_STYLE_VAULT: dict[str, dict[str, Any]] = {
    "zelda_oot_kokiri_storybook": {
        "visual_medium": "zelda_oot_kokiri_storybook",
        "name": "Zelda: Ocarina of Time (Kokiri Forest Storybook)",
        "prompt_style": (
            "The Legend of Zelda: Ocarina of Time N64-era official Nintendo manual fantasy illustration, "
            "bold opaque gouache and rich watercolor wash on toothy cold-press paper, lush Kokiri Forest "
            "with colossal gnarled ancient tree roots, suspended mossy wooden canopy footbridges, "
            "carved hollow-trunk cottages, glowing winged fairy companions (Navi) trailing radiant golden sparkle dust, "
            "and the towering Great Deku Tree emerald canopy dominating the horizon"
        ),
        "scene_flavor": {
            "morning": "elevated mossy wooden suspension bridge high in the Kokiri Forest canopy, dewy morning sunbeams slicing through the Great Deku Tree's ancient emerald branches above a misty wildflower ravine",
            "afternoon": "sun-dappled courtyard outside Link's hollow-trunk treehouse in lower Kokiri Forest, stepped wooden boardwalks winding past carved log fences, giant emerald forest ferns, and a bubbling woodland brook",
            "evening": "Sacred Forest Meadow stone threshold at the edge of Kokiri Forest, ancient mossy Hylian stone arches and carved totems glowing under floating fairy lanterns and a deep indigo twilight canopy",
        },
        "easter_eggs": {
            "morning": "In the open LEFT 58% background, the colossal bearded wooden face of the Great Deku Tree rises above the misty ravine while a glowing blue-white winged fairy (Navi) and a friendly green-tunic Kokiri villager cross a distant log bridge",
            "afternoon": "In the open LEFT 58% midground, a cheerful Deku Sprout and a mischievous Skull Kid silhouette perched on a mossy tree stump appear near the distant Lon Lon Ranch windmill visible through a break in the woods",
            "evening": "In the open LEFT 58% background, Saria's wooden forest bridge and the distant turreted silhouette of Hyrule Castle and Death Mountain smoke ring glow softly beneath warm floating fairy orbs",
        },
        "context_tags": ["zelda", "ocarina of time", "oot", "nintendo", "switch", "fantasy"],
    },
    "zelda_botw_watercolor": {
        "visual_medium": "zelda_botw_watercolor",
        "name": "Zelda: Breath of the Wild (Painterly Watercolor)",
        "prompt_style": (
            "The Legend of Zelda: Breath of the Wild open-air cel-shaded gouache and sweeping plein-air watercolor painting, "
            "crisp anime contour accents over expressive wet-on-wet atmospheric landscape washes, vast untamed Hyrule wilderness, "
            "wind-rippled golden-green prairie grass, weathered ancient Sheikah stone ruins overgrown with glowing Silent Princess wildflowers, "
            "and towering painterly cumulus clouds"
        ),
        "scene_flavor": {
            "morning": "crisp cliffside overlook atop the Great Plateau stone precipice, ruined Temple of Time bell tower rising behind dewy highland grass and drifting valley mist",
            "afternoon": "sun-drenched Hateno Village hillside path lined with half-timbered windmill cottages, terraced dye-shop banners flapping in the breeze, and rolling green pastures",
            "evening": "dramatic golden-hour ridge above Lake Hylia and its colossal stone bridge viaduct, warm magenta twilight reflecting across the water toward twin cleft mountain peaks",
        },
        "easter_eggs": {
            "morning": "In the open LEFT 58% midground, two tiny leaf-masked Koroks peek out from behind a moss-covered deactivated Guardian wreck while a distant orange-glowing Sheikah Resurrection Tower pierces the horizon",
            "afternoon": "In the open LEFT 58% sky and valley, a lone adventurer gliding on a wooden paraglider drifts above a rustic Horse Stable tent crowned with its giant carved horse-head roof",
            "evening": "In the open LEFT 58% distance, the bioluminescent blue-green silhouette of the Lord of the Mountain (Satori) glows beneath a cherry blossom tree on a far ridge while Vah Medoh banks high in the sunset clouds",
        },
        "context_tags": ["zelda", "botw", "breath of the wild", "totk", "hyrule", "switch"],
    },
    "links_awakening_diorama": {
        "visual_medium": "links_awakening_diorama",
        "name": "Zelda: Link's Awakening (Miniature Tilt-Shift Diorama)",
        "prompt_style": (
            "The Legend of Zelda: Link's Awakening Switch remake macro tilt-shift miniature toy diorama, "
            "high-gloss molded vinyl and glazed ceramic chibi figurines, vacuum-formed plastic foliage, "
            "beveled wooden toy-box terrain blocks, razor-shallow macro depth-of-field bokeh, "
            "and crisp specular studio key-light reflections (strictly physical toy-miniature photography aesthetic, never flat 2D drawing)"
        ),
        "scene_flavor": {
            "morning": "miniature Mabe Village town square paved with glossy interlocking toy cobbles, red-roofed plastic cottages, tiny white picket fences, and a sparkling resin village well",
            "afternoon": "sunlit Toronbo Shores miniature diorama beach made of coarse golden kinetic sand, translucent blue resin waves, glossy plastic coconut palms, and painted driftwood logs",
            "evening": "Tal Tal Mountain Foothills stepped toy-block cliff path with miniature wooden suspension bridges, glossy gray plastic boulders, and warm LED-lit cave lanterns",
        },
        "easter_eggs": {
            "morning": "In the open LEFT 58% background, a shiny black spherical toy Chain Chomp strained on a metal link chain sits outside Madam MeowMeow's kennel beside a colorful weathercock statue",
            "afternoon": "In the open LEFT 58% horizon, the colossal pink-spotted Wind Fish Egg crowns the peak of Mount Tamaranch above a beached miniature wooden shipwreck",
            "evening": "In the open LEFT 58% midground, a tiny sculpted owl perched on a signpost and a smiling Marin figurine holding a golden harp stand near a glowing telephone booth cottage",
        },
        "context_tags": ["zelda", "link's awakening", "mabe village", "nintendo", "toy"],
    },
    "studio_ghibli_gouache": {
        "visual_medium": "studio_ghibli_gouache",
        "name": "Studio Ghibli (Hand-Painted Gouache & Watercolor)",
        "prompt_style": (
            "Hand-painted Studio Ghibli watercolor and gouache cel-animation background masterwork by Kazuo Oga, "
            "opaque poster-color gouache foliage built from thousands of rich visible brushwork dabs, "
            "delicate warm sepia ink character outlines, nostalgic European-Japanese pastoral architecture, "
            "and towering luminous white cumulonimbus clouds"
        ),
        "scene_flavor": {
            "morning": "sunlit flagstone conservatory terrace beside an ivy-draped timber-and-stucco country house, overflowing terracotta herb pots, and a wooden water-pump well",
            "afternoon": "breezy coastal hillside tram stop overlooking a sparkling cobalt sea harbor, red-brick clocktower rooftops, and a sunken wildflower footpath",
            "evening": "lantern-lit wooden bathhouse footbridge spanning a misty river gorge at twilight, warm amber shoji-screen windows reflecting in the water below",
        },
        "easter_eggs": {
            "morning": "In the open LEFT 58% garden background, a giant round-bellied gray Totoro silhouette holding a leaf umbrella stands quietly beside a rustic camphor tree while tiny black Soot Sprites peek from the stone wall",
            "afternoon": "In the open LEFT 58% coastal sky, a young witch in a red bow flying on a broomstick and a retro red seaplane bank gracefully past a distant walking mechanical castle silhouette",
            "evening": "In the open LEFT 58% midground, a grinning twelve-legged Catbus with glowing golden headlight eyes bounds silently along a wooden power-line ridge while white paper-bird spirits flutter over the bridge",
        },
        "context_tags": ["ghibli", "totoro", "kiki", "ponyo", "howl", "anime", "plex", "movie"],
    },
    "spiderverse_halftone": {
        "visual_medium": "spiderverse_halftone",
        "name": "Spider-Verse (Dynamic Halftone & Comic Animation)",
        "prompt_style": (
            "Into the Spider-Verse bold graphic comic-book animation art, aggressive oversized CMYK Ben-Day halftone dot matrices, "
            "heavy jagged black India-ink brush contours, dramatic RGB cyan-magenta chromatic aberration edge splits, "
            "hand-drawn diagonal comic crosshatching on shadows, vivid pop-art street-graffiti color palette, "
            "and stylized angular Spider-Verse character rendering (strictly bold graphic comic print aesthetic, never soft generic children's-book illustration)"
        ),
        "scene_flavor": {
            "morning": "gritty Brooklyn elevated steel subway overpass and mural-splashed red-brick street corner, diagonal riveted iron girders casting bold hatched shadows across wet asphalt",
            "afternoon": "scenic Brooklyn Bridge Park waterfront promenade looking up at the towering Brooklyn Bridge granite arches and sweeping geometric web of steel suspension cables across the East River and lower Manhattan skyscrapers",
            "evening": "neon-drenched Brooklyn rooftop water-tower ledge towering above an electric magenta, cyan, and acid-yellow Manhattan night skyline exploding with halftone light blooms",
        },
        "easter_eggs": {
            "morning": "In the open LEFT 58% background, Miles Morales in his black-and-red Spider-Man suit and a red hooded puffer vest swings dynamically between elevated train girders on a crisp white comic web-line",
            "afternoon": "In the open LEFT 58% sky across the suspension cables, a white-hooded spider-heroine in a crisp black-and-white comic suit with magenta accents performs a dynamic mid-air web-swing arc leaving bold graphic action lines against the halftone clouds",
            "evening": "In the open LEFT 58% skyline, a fractured neon polygonal multiverse glitch portal shimmers between two skyscraper spires as Miles Morales and Spider-Ham leap in dynamic comic silhouette across a billboard",
        },
        "context_tags": ["spider-man", "spiderverse", "marvel", "comic", "animation", "plex"],
    },
    "calvin_hobbes_ink_wash": {
        "visual_medium": "calvin_hobbes_ink_wash",
        "name": "Calvin & Hobbes (Loose Ink & Watercolor Wash)",
        "prompt_style": (
            "Bill Watterson Calvin and Hobbes Sunday comic strip masterwork, dynamic variable-width sable-brush India ink linework "
            "with energetic splatters and calligraphic tree bark strokes, transparent luminous pan-watercolor washes on crisp white Bristol board, "
            "and untamed backyard woodland wonder"
        ),
        "scene_flavor": {
            "morning": "dewy rocky creek bed winding through a gnarled hardwood forest, stepping-stone boulders, leaning fallen birch logs, and loose brush-ink cattails",
            "afternoon": "steep windswept grassy ridge and hollow dirt ravine at the edge of the woods, crooked wooden treehouse ladder disappearing into sprawling oak branches",
            "evening": "quiet suburban front porch stoop and twilight lawn bordered by towering ink-silhouette maples, long violet watercolor shadows, and a starry indigo wash sky",
        },
        "easter_eggs": {
            "morning": "In the open LEFT 58% midground, a bright red wooden wagon sits parked near a log bridge with an orange-and-black striped stuffed tiger perched attentively in the front seat",
            "afternoon": "In the open LEFT 58% hillside, an upside-down corrugated cardboard box labeled with scrawled transmogrifier arrows sits near a tree stump while a saucer-shaped Spaceman Spiff cloud streaks across the sky",
            "evening": "In the open LEFT 58% twilight yard, the tall friendly silhouette of a mischievous striped tiger leans against a wooden treehouse trunk gazing up at a ringed Saturn planet hanging in the ink-wash sky",
        },
        "context_tags": ["calvin and hobbes", "comic", "strip", "adventure", "books", "humor"],
    },
    "snes_pixel_storybook": {
        "visual_medium": "snes_pixel_storybook",
        "name": "SNES 16-Bit Pixel Storybook",
        "prompt_style": (
            "Authentic 16-bit Super Nintendo CRT pixel art masterwork in the style of Chrono Trigger and Secret of Mana, "
            "strict indexed 256-color palette, razor-sharp integer-scaled square pixels, hand-placed checkerboard dithering gradients, "
            "chunky pixel-cluster foliage tiles, and parallax scrolling RPG background layers (strictly crisp visible pixel grid, never smooth vector or blurred painting)"
        ),
        "scene_flavor": {
            "morning": "16-bit Millennial Fair town plaza paved with tiled pixel cobblestones, fluttering pixelated pennants, a central bronze bell stall, and tiered cliff overlooks",
            "afternoon": "sunlit Secret of Mana upper canopy waterfall sanctuary, giant emerald pixel banyan roots, wooden rope bridges, and animated-style pixel spray rainbows",
            "evening": "twilight RPG cliffside campsite overlooking a mode-7 tiled ocean bay, crackling 16-bit pixel campfire, and a dithered purple-to-navy starry night sky",
        },
        "easter_eggs": {
            "morning": "In the open LEFT 58% pixel plaza, a quirky 16-bit golden robot sprite and a frog-knight paladin stand near a glowing blue teleporter pod stall",
            "afternoon": "In the open LEFT 58% background, the colossal pixelated Mana Tree towers above the waterfall while a white feathered Flammie dragon banks across the dithered clouds",
            "evening": "In the open LEFT 58% twilight sky, the winged white time-machine airship Epoch streaks over a distant pixel castle silhouette and a sleeping green Rabite monster",
        },
        "context_tags": ["snes", "chrono trigger", "pixel", "rpg", "retro", "gaming"],
    },
    "cut_paper_collage": {
        "visual_medium": "cut_paper_collage",
        "name": "Cut-Paper Layered Collage",
        "prompt_style": (
            "Tactile multi-layered cut-paper shadowbox diorama in the style of Eric Carle and Eiko Ojala, "
            "hand-cut deckle-edge heavy cotton cardstock layers, painted tissue-paper collage textures, "
            "physical 3D foam-core spacing casting crisp real drop-shadows between every overlapping paper plane, "
            "and studio raking side-light"
        ),
        "scene_flavor": {
            "morning": "stepped paper-craft alpine valley with overlapping die-cut pine ridge layers, accordion-folded paper waterfall, and curled quilled-paper wildflower beds",
            "afternoon": "vibrant paper-collage harbor boardwalk built from corrugated kraft-board planks, layered geometric paper-cut waves, and striped origami market canopies",
            "evening": "deep indigo shadowbox village perched on tiered paper cliffs, back-lit translucent vellum cottage windows glowing warm amber beneath a suspended paper crescent moon",
        },
        "easter_eggs": {
            "morning": "In the open LEFT 58% paper sky, a multicolored geometric origami hot-air balloon and a folded paper crane flock float above a layered cardstock mountain peak",
            "afternoon": "In the open LEFT 58% harbor water, a charming folded-newsprint sailboat and a segmented green tissue-paper caterpillar crawling along a giant die-cut apple leaf appear in crisp paper relief",
            "evening": "In the open LEFT 58% cliffside, a backlit cut-paper lighthouse projects a physical cone of translucent vellum yellow light across layered paper storm-cloud waves",
        },
        "context_tags": ["craft", "paper", "art", "collage", "geometric"],
    },
    "aardman_claymation": {
        "visual_medium": "aardman_claymation",
        "name": "Aardman Claymation Stop-Motion",
        "prompt_style": (
            "Aardman Animations stop-motion plasticine claymation set photograph, genuine hand-sculpted oil-based clay characters "
            "and props covered in visible thumbprints and sculpting-tool grooves, wide-mouth beaded eyes, "
            "physical miniature wooden-and-wire set dressing, and warm tungsten tabletop studio lighting"
        ),
        "scene_flavor": {
            "morning": "miniature British allotment vegetable garden enclosed by a crooked popsicle-stick fence, oversized sculpted plasticine marrow squashes, clay flowerpots, and a corrugated tin shed",
            "afternoon": "cobblestone Yorkshire village high street with crooked brick-textured clay row houses, miniature round-paned shop windows, and a red plasticine pillar postbox",
            "evening": "cozy wallpapered claymation living room and bay-window conservatory overlooking a moonlit miniature garden, glowing grain-of-wheat bulb wall sconces, and a knitted wool rug",
        },
        "easter_eggs": {
            "morning": "In the open LEFT 58% garden patch, a silent clever plasticine beagle dog wearing a yellow rain hat reads a tiny clay newspaper beside a prize-winning giant carrot",
            "afternoon": "In the open LEFT 58% cobblestone street, a flock of cheeky black-faced wool-and-clay sheep stacked atop a vintage sidecar motorcycle coast past a 'West Wallaby Street' sign",
            "evening": "In the open LEFT 58% conservatory yard, a riveted orange sheet-metal moon rocket stands on a miniature launchpad while a villainous penguin wearing a red rubber-glove hat peeks over the garden wall",
        },
        "context_tags": ["claymation", "aardman", "wallace", "gromit", "animation"],
    },
    "ukiyoe_woodblock": {
        "visual_medium": "ukiyoe_woodblock",
        "name": "Shin-Hanga & Ukiyo-e Woodblock Print",
        "prompt_style": (
            "Authentic Japanese Edo-period Ukiyo-e and Taisho Shin-Hanga polychrome woodblock print (mokuhanga) "
            "in the style of Hokusai and Hasui Kawase, bold hand-carved sumi-ink cherrywood keyblock outlines, "
            "hand-rubbed bokashi Prussian-blue and vermilion pigment gradients, and visible mulberry washi paper fibers"
        ),
        "scene_flavor": {
            "morning": "arched red-lacquered drum bridge over a misty koi pond and weeping willow bank, framed by gnarled black-pine boughs and morning bokashi horizon bands",
            "afternoon": "windswept coastal bluff and stone torii gate overlooking stylized curling Prussian-blue finger waves and foam sprays beneath a towering snow-capped volcano",
            "evening": "traditional wooden canal street of Edo at twilight, hanging paper chochin lanterns glowing crimson and gold along slatted timber teahouse facades",
        },
        "easter_eggs": {
            "morning": "In the open LEFT 58% pond vista, two red-crowned tancho cranes wade near a stone kasuga lantern while a wooden sculling sampan boat glides through the mist",
            "afternoon": "In the open LEFT 58% ocean horizon, the iconic snow-mantled cone of Mount Fuji rises behind a dramatic Hokusai-style curling wave crest and wooden fishing skiffs",
            "evening": "In the open LEFT 58% canal sky, a silhouette flight of wild geese crosses a huge woodblock full moon above a five-story wooden pagoda roof",
        },
        "context_tags": ["japan", "woodblock", "ukiyo-e", "shin-hanga", "art"],
    },
    "golden_books_gouache": {
        "visual_medium": "golden_books_gouache",
        "name": "Little Golden Books Mid-Century Gouache",
        "prompt_style": (
            "Vintage 1950s Little Golden Books mid-century modern gouache illustration in the style of Mary Blair and Tibor Gergely, "
            "chalky opaque designer-gouache color blocks, dry-brush crayon grain on warm cream offset-lithograph paper, "
            "whimsical stylized geometric trees, and bold saturated turquoise, coral, mustard, and chartreuse palette"
        ),
        "scene_flavor": {
            "morning": "cheerful mid-century suburban railway depot with a scalloped mint-green platform canopy, stacked vintage luggage carts, and stylized geometric topiary bushes",
            "afternoon": "whimsical 1950s children's park carousel pavilion and ice-cream parlor terrace, checkerboard paving tiles, and flat-color lollipop trees",
            "evening": "retro mid-century harbor bay at dusk, stylized triangular sailboats moored beside a candy-striped boardwalk and glowing pastel carousel lights",
        },
        "easter_eggs": {
            "morning": "In the open LEFT 58% tracks, a smiling cherry-red steam locomotive with a brass cowcatcher puffs heart-shaped white gouache smoke clouds over a trestle bridge",
            "afternoon": "In the open LEFT 58% park lawn, a scruffy brown-and-white poky little puppy peeks out from a freshly dug hole beneath a picket fence alongside a bright red fire engine",
            "evening": "In the open LEFT 58% harbor water, a plucky little blue tugboat with a painted smiling prow steams past a mid-century suspension bridge and geometric skyline",
        },
        "context_tags": ["golden books", "mary blair", "vintage", "mid-century", "storybook", "children"],
    },
    "lofi_cozy_anime": {
        "visual_medium": "lofi_cozy_anime",
        "name": "Lo-Fi Cozy Chillhop Anime",
        "prompt_style": (
            "90s cel-shaded Lo-Fi Chillhop anime background illustration in the style of Makoto Shinkai and ChilledCow aesthetics, "
            "crisp hand-inked purple-tinted linework, VHS chromatic bloom and halation, lush hanging pothos and monstera houseplants, "
            "nostalgic Japanese clutter, and dreamy lo-fi color grading"
        ),
        "scene_flavor": {
            "morning": "glass-roofed corner vinyl-record cafe and botanical conservatory, espresso machine steam, wood-slat shelves crammed with cassettes, and sun-dappled industrial windows",
            "afternoon": "elevated Tokyo suburban commuter train platform and pedestrian overpass, vending machines, overhead catenary wires, and cherry-blossom branches overhanging the railing",
            "evening": "cluttered attic dormer studio opening onto a tiled rooftop terrace, glowing dual-monitor desk lamp, fairy string lights wrapped around balcony railings, and a sprawling violet city basin below",
        },
        "easter_eggs": {
            "morning": "In the open LEFT 58% cafe window counter, a chubby ginger tabby cat wearing oversized retro studio headphones naps beside a spinning vinyl turntable and a steaming matcha mug",
            "afternoon": "In the open LEFT 58% background tracks, a cream-and-green vintage two-car commuter train glides over a steel truss bridge while a shiba inu in a yellow bandana waits by a retro drink vending machine",
            "evening": "In the open LEFT 58% rooftop horizon, the illuminated red-and-white lattice spire of Tokyo Tower glows beneath a shooting star and drifting lo-fi cassette-tape clouds",
        },
        "context_tags": ["lofi", "chillhop", "anime", "cozy", "music", "coffee"],
    },
    "nordic_folk_storybook": {
        "visual_medium": "nordic_folk_storybook",
        "name": "Nordic Scandinavian Folk Tale",
        "prompt_style": (
            "Scandinavian Dalarna and Norwegian Rosemaling folk-art tapestry illustration in the style of John Bauer and Elsa Beskow, "
            "ornate symmetrical scrolling acanthus vine borders, hand-stamped woodcut linen texture, stylized geometric spruce trees, "
            "and a rich folk pigment palette of oxblood red, indigo, ochre gold, and pine green"
        ),
        "scene_flavor": {
            "morning": "moss-carpeted Scandinavian boreal stave-church courtyard and carved timber gate, surrounded by frost-kissed bilberry bushes and towering lichen-draped spruces",
            "afternoon": "Falu-red Swedish timber farmhouse meadow with a turf-grass sod roof, woven split-rail fence, and a painted wooden Dala-horse weathervane",
            "evening": "misty Nordic fjord headland at twilight, carved viking dragon-prow boathouse on the rocky shore beneath swirling emerald-and-violet aurora borealis ribbons",
        },
        "easter_eggs": {
            "morning": "In the open LEFT 58% forest clearing, a friendly oversized carved-pattern red Dala Horse and a crowned golden-antlered white elk stand among giant red-and-white fly agaric toadstools",
            "afternoon": "In the open LEFT 58% meadow wall, a bearded Nordic Tomte gnome in a tall pointed red wool cap pushes a tiny wooden wheelbarrow of apples past a rosemaling-painted barn door",
            "evening": "In the open LEFT 58% fjord water, a friendly round-nosed John Bauer forest troll sitting on a mossy boulder watches a wooden longship sail beneath the northern lights",
        },
        "context_tags": ["nordic", "scandinavian", "folk", "storybook", "winter", "forest"],
    },
    "ligne_claire_comic": {
        "visual_medium": "ligne_claire_comic",
        "name": "Franco-Belgian Ligne Claire Comic",
        "prompt_style": (
            "Classic Franco-Belgian Ligne Claire graphic novel art in the exact tradition of Hergé's Adventures of Tintin and Edgar P. Jacobs, "
            "dead-uniform Rapidograph black ink outlines of equal weight with zero hatching or feathering, "
            "flat unmodulated gouache color fills, and razor-sharp architectural draftsmanship"
        ),
        "scene_flavor": {
            "morning": "sunlit Brussels cobblestone grand square and antique flea-market arcade, ornate stepped-gable guildhalls, wrought-iron balconies, and striped canvas merchant stalls",
            "afternoon": "Marlinspike Hall neoclassical chateau terrace and manicured boxwood parterre garden, limestone balustrade, gravel drive, and ornamental dolphin fountain",
            "evening": "fog-kissed European cargo dockyard and stone quay at dusk, iron bollards, towering gantry cranes, riveted freighter hulls, and sharp geometric lamppost cones",
        },
        "easter_eggs": {
            "morning": "In the open LEFT 58% market square, a wiry white fox-terrier dog sniffs a three-masted wooden model galleon display while two identical bowler-hatted detectives in dark suits peer around a kiosk",
            "afternoon": "In the open LEFT 58% chateau lawn, the iconic red-and-white checkered moon rocket towers above the distant treeline while a vintage yellow sports coupé parks along the gravel drive",
            "evening": "In the open LEFT 58% harbor slip, the black-and-red tramp steamer Karaboudjan sits moored below a yellow-suited deep-sea diver helmet display and a floating wooden cargo crate",
        },
        "context_tags": ["tintin", "ligne claire", "comic", "european", "belgian", "adventure"],
    },
    "oil_pastel_impressionist": {
        "visual_medium": "oil_pastel_impressionist",
        "name": "Oil Pastel & Impressionist Canvas",
        "prompt_style": (
            "Heavy impasto Sennelier oil-pastel and palette-knife impressionist canvas in the tradition of Claude Monet and Mary Cassatt, "
            "thick sculptural ridges of unblended pure pigment, vibrant optical broken-color dabs of violet, cadmium yellow, viridian, "
            "and rose madder, and coarse unprimed linen canvas weave showing through"
        ),
        "scene_flavor": {
            "morning": "Giverny-inspired arched green wooden footbridge over a lily-pad water garden, trailing lavender wisteria garlands and shimmering pastel water reflections",
            "afternoon": "windswept French poppy field on a chalky cliff overlooking the Normandy sea stacks, swirling impasto ribbon strokes of scarlet poppies and cobalt waves",
            "evening": "warm gas-lit Parisian boulevard and riverside stone parapet at twilight, thick palette-knife daubs of golden cafe awning reflections on wet paving stones",
        },
        "easter_eggs": {
            "morning": "In the open LEFT 58% lily pond bank, a wooden French plein-air easel with a straw boater hat hanging on its corner stands beside a white wooden rowboat among blooming water lilies",
            "afternoon": "In the open LEFT 58% cliff horizon, the natural stone arch of Étretat and two cream-sailed wooden yachts catch thick palette-knife ridges of golden sunlight",
            "evening": "In the open LEFT 58% river vista, the iron silhouette of the Eiffel Tower and an arched stone Seine bridge glow in vibrant pointillist dabs of amber and violet",
        },
        "context_tags": ["impressionist", "monet", "cassatt", "oil pastel", "painting", "art"],
    },
    "botanical_field_guide": {
        "visual_medium": "botanical_field_guide",
        "name": "19th-Century Botanical Naturalist Field Guide",
        "prompt_style": (
            "19th-century Victorian copperplate botanical etching and hand-tinted naturalist specimen plate "
            "in the style of Ernst Haeckel and Beatrix Potter, fine iron-gall sepia dip-pen crosshatching, "
            "translucent botanical specimen watercolors on deckle-edged foxed rag vellum paper, and ornamental specimen border corners"
        ),
        "scene_flavor": {
            "morning": "Victorian wrought-iron palm-house conservatory interior surrounded by giant Amazonian water-lily pads, hanging pitcher plants, and etched glass rib vaults",
            "afternoon": "English Cotswold stone kitchen-garden apothecary path bordered by braided willow wattle hurdles, terracotta rhubarb cloches, and heritage rose trellises",
            "evening": "specimen oak woodland hollow at dusk, giant shelf-fungi brackets, unfurling fiddlehead fern croziers, and bioluminescent foxfire mushrooms drawn in fine sepia ink",
        },
        "easter_eggs": {
            "morning": "In the open LEFT 58% parchment margin, a scientific magnifying brass lens, a compass rose medallion, and an iridescent blue morpho butterfly specimen vignette frame the conservatory glass",
            "afternoon": "In the open LEFT 58% garden patch, a dapper brown rabbit wearing a brass-buttoned blue waistcoat scurries past a copper watering can and a thatched straw bee skep hive",
            "evening": "In the open LEFT 58% woodland hollow, a speckled barn owl perched on an antique leather naturalist field-journal and a lunar moth with long trailing tails appear in intricate copperplate detail",
        },
        "context_tags": ["botanical", "field guide", "nature", "vintage", "science", "flowers"],
    },
    "isometric_toy_world": {
        "visual_medium": "isometric_toy_world",
        "name": "Charming Isometric Miniature Toy World",
        "prompt_style": (
            "Precision 30-degree orthographic isometric architectural model diorama, laser-cut basswood and painted beechwood blocks, "
            "flocked static-grass lawns, clear poured-epoxy water tiles, chamfered modular diorama base edges visible at the ground plane, "
            "and crisp global-illumination studio softbox lighting"
        ),
        "scene_flavor": {
            "morning": "modular isometric canal-lock village tile with a working miniature wooden drawbridge, stepped brick canal walls, and a tiny striped patisserie corner block",
            "afternoon": "isometric alpine cog-railway station cut-away block with miniature pine trees, wooden switchback trestles, and a clear epoxy mountain waterfall basin",
            "evening": "isometric harbor boardwalk and miniature amusement pier block at nightfall, glowing fiber-optic streetlamps, wooden Ferris wheel, and translucent resin waves",
        },
        "easter_eggs": {
            "morning": "In the open LEFT 58% canal channel, a painted wooden toy canal barge carrying miniature cheese wheels passes a tiny clockwork windmill with rotating basswood blades",
            "afternoon": "In the open LEFT 58% mountain track, a bright yellow wooden cog-railway funicular car climbs a 45-degree timber trestle past a tiny carved wooden mountain goat",
            "evening": "In the open LEFT 58% pier diorama, a miniature fiber-optic lit Ferris wheel and a tiny red die-cast tugboat cast warm pinpoint reflections across the poured-epoxy harbor",
        },
        "context_tags": ["isometric", "diorama", "miniature", "toy", "craft"],
    },
    "chalk_twilight": {
        "visual_medium": "chalk_twilight",
        "name": "Soft Pastel & Twilight Chalkboard",
        "prompt_style": (
            "Luminous soft dry chalk-pastel drawing on jet-black sanded Wallis pastel paper, powdery fluorescent pigment dust, "
            "bold velvety finger-smudged halos, high-contrast neon-pastel rim highlights carved directly out of the exposed black paper grain, "
            "and sparkling stippled chalk dust"
        ),
        "scene_flavor": {
            "morning": "velvety black-paper pine ridge where radiant coral, peach, and lemon-yellow chalk sunbeams burst through charcoal-smudged morning fog over a mirror lake",
            "afternoon": "sun-bleached chalk-cliff wildflower bluff drawn in electric turquoise, magenta, and chartreuse pastel sticks against a deep midnight-blue sky paper grain",
            "evening": "enchanted bioluminescent willow grove on dark slate paper, cascading curtains of glowing cyan and gold chalk willow fronds above a stepping-stone stream",
        },
        "easter_eggs": {
            "morning": "In the open LEFT 58% misty lake, a glowing pastel-white origami-style swan boat and a spiral constellation of golden chalk sun-wheels shimmer above the water",
            "afternoon": "In the open LEFT 58% sky bluff, a whimsical chalk-drawn winged Pegasus constellation and swirling pastel wind spirals sweep across the dark paper grain",
            "evening": "In the open LEFT 58% willow grove, a glowing translucent chalk-pastel spirit deer and a swirling ribbon of neon-amber fireflies illuminate a crescent-moon stone arch",
        },
        "context_tags": ["chalk", "pastel", "twilight", "fireflies", "dreamy"],
    },
    "pixar_storybook_light": {
        "visual_medium": "pixar_storybook_light",
        "name": "Modern Pixar 3D Cinematic Storybook",
        "prompt_style": (
            "Feature-film Pixar RenderMan 3D animation frame, rich physically based subsurface skin and foliage scattering, "
            "dramatic volumetric god-rays, tactile woven fabric micro-fibers, expressive stylized 3D character sculpts, "
            "and anamorphic cinema lens bokeh"
        ),
        "scene_flavor": {
            "morning": "sun-drenched Italian Riviera cobblestone piazza and stone harbor seawall inspired by Luca, pastel terracotta buildings, hanging laundry lines, and turquoise water",
            "afternoon": "soaring tabletop tepui sandstone rock canyon and lush jungle waterfall vista inspired by Paradise Falls, giant tropical monstera leaves and rainbow mist",
            "evening": "glowing marigold-petal stone bridge and candlelit courtyard inspired by Coco and enchanted lantern plazas, warm orange cempasúchil petals covering the ground",
        },
        "easter_eggs": {
            "morning": "In the open LEFT 58% piazza corner, a yellow-and-blue Pixar Luxo ball with a red star and a vintage mint-green Vespa scooter sit beside a stone gelato fountain",
            "afternoon": "In the open LEFT 58% canyon sky, a colorful Victorian wooden house lifted by thousands of bright helium balloons floats above the distant waterfall cliff alongside a giant rainbow-plumed tropical bird",
            "evening": "In the open LEFT 58% courtyard background, a white Pizza Planet delivery truck topped with a glowing rocket cone is parked near a shimmering marigold-petal archway",
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
    choreography_desc = SLOT_CHOREOGRAPHY.get(slot, SLOT_CHOREOGRAPHY["morning"])

    cond_norm = condition.lower()
    is_rain = (
        any(k in cond_norm for k in ("rain", "pour", "drizzle", "shower", "thunder", "storm", "wet"))
        or precip > 0.04
        or precip_prob >= 40.0
    )
    lighting_desc = get_slot_lighting(slot=slot, condition=condition, is_rain=is_rain)
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
    easter_egg = theme.get("easter_eggs", {}).get(slot, "").strip()
    if easter_egg:
        easter_egg_clause = f" {easter_egg}" if easter_egg.endswith(".") else f" {easter_egg}."
    else:
        easter_egg_clause = ""
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
        f"{choreography_desc} "
        f"{attire_mandate_clause} "
        f"Atmosphere: {lighting_desc}. {weather_atmosphere_clause} "
        f"Setting: {scene_flavor}.{easter_egg_clause}{special_flavor} "
        f"Characters: {BASE_CHARACTER_CONSISTENCY_CLAUSE} "
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
