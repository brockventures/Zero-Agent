#!/usr/bin/env python3
"""
Plex Audiobook Enricher & Cover Badge Engine (`/workspace/tools/plex_audiobook_enricher.py`).

Provides automated weekly & on-demand metadata enrichment for Plex Audiobooks (Section 4):
1. File Permission & Tag Normalization:
   - Enforces world-readable permissions (`chmod -R a+rX`) on the NAS audiobooks root so unpacked
     `.m4b`/`.mp3` files with `0600` permissions are never skipped by the Plex Media Scanner.
   - Normalizes embedded ID3/MP4 `Album` and `Album Artist` tags on multi-track folders so books
     never splinter into hundreds of individual chapter albums or merge across series.
2. Dynamic Multi-API Metadata & Rating Resolution:
   - Combines a curated fast-lookup catalog with live queries to Apple Books / iTunes Audiobook API,
     OpenLibrary Search API, and Google Books API for any new audiobooks added in the future.
   - Extracts community 5-star ratings, narrator credits, series names/numbers, release years,
     clean plot synopses (stripping publisher praise banners), and 1000x1000 square cover art.
3. Kometa-Style Cover Badges & Series Collections:
   - Renders `★ Rating` (top-left gold pill) and `BOOK #` (top-right cyan pill) onto 1000x1000
     square cover art and uploads directly to Plex.
   - Locks chronological `titleSort` (`<Series> 01 - <Title>`) and configures Plex Album Collections.
"""

from __future__ import annotations

import argparse
import html
import io
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont

for _p in ("/workspace", "/workspace/tools"):
    if _p not in sys.path:
        sys.path.insert(0, _p)

try:
    from tools import nas_config
except ImportError:
    import nas_config  # type: ignore

DATA_DIR = Path(os.environ.get("DATA_DIR", "/workspace/data"))
STATE_FILE = DATA_DIR / "audiobook_enricher_state.json"
COVER_CACHE_DIR = DATA_DIR / "audiobook_covers_clean"

# Baseline curated catalog for instant, deterministic enrichment of core library titles
BOOK_CATALOG: dict[str, dict[str, Any]] = {
    "The Blade Itself": {
        "author": "Joe Abercrombie", "series": "The First Law", "pos": 1, "year": 2006,
        "rating": 4.18, "narrator": "Steven Pacey", "genres": ["Fantasy", "Grimdark"],
        "summary": "Inquisitor Glokta, a crippled and increasingly bitter relic of the last war, former fencing champion Jezal dan Luthar, and Logen Ninefingers, an infamous barbarian whose luck is running out, are drawn together as the First of the Magi, Bayaz, returns to the Union.",
    },
    "Before They Are Hanged": {
        "author": "Joe Abercrombie", "series": "The First Law", "pos": 2, "year": 2007,
        "rating": 4.41, "narrator": "Steven Pacey", "genres": ["Fantasy", "Grimdark"],
        "summary": "Superior Glokta must defend the doomed city of Dagoska from the Gurkish army, Crown Prince Ladisla leads a disastrous campaign in the North, and Bayaz takes Logen, Jezal, and Ferro on a perilous quest to the edge of the world.",
    },
    "Last Argument of Kings": {
        "author": "Joe Abercrombie", "series": "The First Law", "pos": 3, "year": 2008,
        "rating": 4.50, "narrator": "Steven Pacey", "genres": ["Fantasy", "Grimdark"],
        "summary": "The epic conclusion to The First Law trilogy. War engulfs Adua as King Bethod's Northmen and the Gurkish Empire converge on the capital, and Bayaz unveils the true, ruthless cost of power.",
    },
    "Red Rising": {
        "author": "Pierce Brown", "series": "Red Rising Saga", "pos": 1, "year": 2014,
        "rating": 4.27, "narrator": "Tim Gerard Reynolds", "genres": ["Sci-Fi", "Dystopian"],
        "summary": "Darrow is a Red, a member of the lowest caste in the color-coded society of the future. Driven by a longing for justice after a devastating tragedy, he infiltrates the ruthless Gold ruling class at their brutal Institute.",
    },
    "Golden Son": {
        "author": "Pierce Brown", "series": "Red Rising Saga", "pos": 2, "year": 2015,
        "rating": 4.54, "narrator": "Tim Gerard Reynolds", "genres": ["Sci-Fi", "Space Opera"],
        "summary": "As a Lancer for House Augustus, Darrow rises through the ranks of the Golds until betrayal sparks an all-out civil war across the solar system.",
    },
    "Morning Star": {
        "author": "Pierce Brown", "series": "Red Rising Saga", "pos": 3, "year": 2016,
        "rating": 4.52, "narrator": "Tim Gerard Reynolds", "genres": ["Sci-Fi", "Space Opera"],
        "summary": "Betrayed and imprisoned in darkness for a year, Darrow is rescued by the Sons of Ares to ignite a solar-system-wide uprising against the Sovereign and the Gold Society.",
    },
    "Iron Gold": {
        "author": "Pierce Brown", "series": "Red Rising Saga", "pos": 4, "year": 2018,
        "rating": 4.25, "narrator": "Tim Gerard Reynolds, Julian Elfer, John Curless, Aedin Moloney", "genres": ["Sci-Fi", "Space Opera"],
        "summary": "Ten years after the Rising overthrew the Society, Darrow defies the fragile Solar Republic Senate on a rogue mission to end the war once and for all.",
    },
    "Dark Age": {
        "author": "Pierce Brown", "series": "Red Rising Saga", "pos": 5, "year": 2019,
        "rating": 4.53, "narrator": "Tim Gerard Reynolds, John Curless, Moira Quirk, James Langton", "genres": ["Sci-Fi", "Space Opera"],
        "summary": "Outlawed by the Republic he founded, Darrow wages a desperate, brutal war on Mercury against the Ash Lord's fleets while political conspiracies tear the Republic apart from within.",
    },
    "Light Bringer": {
        "author": "Pierce Brown", "series": "Red Rising Saga", "pos": 6, "year": 2023,
        "rating": 4.66, "narrator": "Tim Gerard Reynolds", "genres": ["Sci-Fi", "Space Opera"],
        "summary": "Stranded far from home after the fall of Mercury, Darrow and Cassius embark on a perilous odyssey to the Rim Worlds to forge a new alliance against Lysander au Lune and the Minotaur.",
    },
    "Dungeon Crawler Carl": {
        "author": "Matt Dinniman", "series": "Dungeon Crawler Carl", "pos": 1, "year": 2020,
        "rating": 4.49, "narrator": "Jeff Hays", "genres": ["Sci-Fi", "LitRPG", "Comedy"],
        "summary": "Earth's surface is obliterated in seconds and transformed into an eighteen-level subterranean dungeon for an intergalactic game show. Coast Guard vet Carl and his ex-girlfriend's prize-winning Persian cat, Princess Donut, must fight to survive.",
    },
    "Carl's Doomsday Scenario": {
        "author": "Matt Dinniman", "series": "Dungeon Crawler Carl", "pos": 2, "year": 2021,
        "rating": 4.58, "narrator": "Jeff Hays", "genres": ["Sci-Fi", "LitRPG", "Comedy"],
        "summary": "Descending to the Overcity on the third floor of the World Dungeon, Carl and Princess Donut finally choose their races and classes while navigating deadly questlines and galactic ratings.",
    },
    "The Dungeon Anarchist's Cookbook": {
        "author": "Matt Dinniman", "series": "Dungeon Crawler Carl", "pos": 3, "year": 2021,
        "rating": 4.60, "narrator": "Jeff Hays", "genres": ["Sci-Fi", "LitRPG", "Comedy"],
        "summary": "Welcome to the Iron Tangle, a labyrinthine fourth-floor subway network of runaway trains and nightmare monsters. Carl inherits a secret, subversive artifact passed down by past crawlers: The Dungeon Anarchist's Cookbook.",
    },
    "The Gate of the Feral Gods": {
        "author": "Matt Dinniman", "series": "Dungeon Crawler Carl", "pos": 4, "year": 2021,
        "rating": 4.62, "narrator": "Jeff Hays", "genres": ["Sci-Fi", "LitRPG", "Comedy"],
        "summary": "Trapped in a sealed bubble ecosystem on the fifth floor, Carl, Donut, and Mongo must conquer four interconnected quadrants before the entire bubble collapses.",
    },
    "The Butcher's Masquerade": {
        "author": "Matt Dinniman", "series": "Dungeon Crawler Carl", "pos": 5, "year": 2022,
        "rating": 4.73, "narrator": "Jeff Hays", "genres": ["Sci-Fi", "LitRPG", "Comedy"],
        "summary": "On the Hunting Grounds of the sixth floor, rich alien elites pay to hunt crawlers for sport, culminating in an explosive showdown at the Butcher's Masquerade.",
    },
    "The Eye of the Bedlam Bride": {
        "author": "Matt Dinniman", "series": "Dungeon Crawler Carl", "pos": 6, "year": 2023,
        "rating": 4.65, "narrator": "Jeff Hays", "genres": ["Sci-Fi", "LitRPG", "Comedy"],
        "summary": "After skipping the seventh floor entirely, Carl and Donut face the deck-building madness of the eighth floor, capturing regional ghosts and folklore monsters across recreated remnants of Earth.",
    },
    "This Inevitable Ruin": {
        "author": "Matt Dinniman", "series": "Dungeon Crawler Carl", "pos": 7, "year": 2024,
        "rating": 4.78, "narrator": "Jeff Hays", "genres": ["Sci-Fi", "LitRPG", "Comedy"],
        "summary": "Faction Wars has arrived on the ninth floor. For the first time in dungeon history, an army of crawlers and former crawlers leads its own faction into total war against the ruling galactic houses.",
    },
    "Leviathan Wakes": {
        "author": "James S.A. Corey", "series": "The Expanse", "pos": 1, "year": 2011,
        "rating": 4.30, "narrator": "Jefferson Mays", "genres": ["Sci-Fi", "Space Opera"],
        "summary": "Ice miner Jim Holden and washed-up Ceres detective Joe Miller cross paths while investigating a missing young woman, uncovering an alien biological threat that pushes Earth, Mars, and the Belt to the brink of war.",
    },
    "Caliban's War": {
        "author": "James S.A. Corey", "series": "The Expanse", "pos": 2, "year": 2012,
        "rating": 4.33, "narrator": "Jefferson Mays", "genres": ["Sci-Fi", "Space Opera"],
        "summary": "When a monstrous protomolecule hybrid slaughters Marines on Ganymede, Bobbie Draper, Chrisjen Avasarala, Prax Meng, and the crew of the Rocinante race to stop a covert biological weapons program.",
    },
    "Abaddon's Gate": {
        "author": "James S.A. Corey", "series": "The Expanse", "pos": 3, "year": 2013,
        "rating": 4.27, "narrator": "Jefferson Mays", "genres": ["Sci-Fi", "Space Opera"],
        "summary": "The alien Ring Gate has risen beyond Uranus, opening a doorway to a hub of a thousand star systems. When fleets from Earth, Mars, and the OPA enter the Slow Zone, paranoia and ancient defenses turn it into a trap.",
    },
    "Cibola Burn": {
        "author": "James S.A. Corey", "series": "The Expanse", "pos": 4, "year": 2014,
        "rating": 4.18, "narrator": "Jefferson Mays", "genres": ["Sci-Fi", "Space Opera"],
        "summary": "On the frontier world of Ilus beyond the Ring Gates, Belter refugees and a corporate Earth charter clash violently while Holden is sent to mediate—just as two billion-year-old planetary machinery wakes up.",
    },
    "Nemesis Games": {
        "author": "James S.A. Corey", "series": "The Expanse", "pos": 5, "year": 2015,
        "rating": 4.44, "narrator": "Jefferson Mays", "genres": ["Sci-Fi", "Space Opera"],
        "summary": "As the crew of the Rocinante scatters across the solar system to handle personal business, a radical Belter faction launches a catastrophic, coordinated strike that changes human history forever.",
    },
    "Babylon's Ashes": {
        "author": "James S.A. Corey", "series": "The Expanse", "pos": 6, "year": 2016,
        "rating": 4.24, "narrator": "Jefferson Mays", "genres": ["Sci-Fi", "Space Opera"],
        "summary": "With Earth reeling from asteroid bombardment, the Free Navy controls the space lanes. Holden, Avasarala, and an uneasy coalition of Earth, Mars, and moderate Belters fight to reclaim the solar system.",
    },
    "Persepolis Rising": {
        "author": "James S.A. Corey", "series": "The Expanse", "pos": 7, "year": 2017,
        "rating": 4.39, "narrator": "Jefferson Mays", "genres": ["Sci-Fi", "Space Opera"],
        "summary": "Thirty years after the Free Navy war, the breakaway Martian fleet on Laconia returns through the Ring Gate with alien warship technology to conquer all of humanity.",
    },
    "Tiamat's Wrath": {
        "author": "James S.A. Corey", "series": "The Expanse", "pos": 8, "year": 2019,
        "rating": 4.57, "narrator": "Jefferson Mays", "genres": ["Sci-Fi", "Space Opera"],
        "summary": "Under the totalitarian rule of the Laconian Empire, Naomi leads the underground resistance while Bobbie and Amos strike at the heart of Laconia—and the extradimensional entities that killed the Ring Builders awaken.",
    },
    "Leviathan Falls": {
        "author": "James S.A. Corey", "series": "The Expanse", "pos": 9, "year": 2021,
        "rating": 4.58, "narrator": "Jefferson Mays", "genres": ["Sci-Fi", "Space Opera"],
        "summary": "The grand finale of The Expanse. As the dark gods beyond the rings begin extinguishing human consciousness across the galaxy, Holden and the Rocinante crew make one final stand at the Ring Station.",
    },
    "The Lies of Locke Lamora": {
        "author": "Scott Lynch", "series": "Gentleman Bastard", "pos": 1, "year": 2006,
        "rating": 4.29, "narrator": "Michael Page", "genres": ["Fantasy", "Heist"],
        "summary": "In the island city of Camorr, orphan and con artist Locke Lamora leads the Gentlemen Bastards in fleecing the nobility—until a shadowy killer known as the Gray King threatens to destroy the underworld.",
    },
    "Red Seas Under Red Skies": {
        "author": "Scott Lynch", "series": "Gentleman Bastard", "pos": 2, "year": 2007,
        "rating": 4.23, "narrator": "Michael Page", "genres": ["Fantasy", "Heist"],
        "summary": "Fleeing Camorr for the exotic port of Tal Verrar, Locke Lamora and Jean Tannen plot to rob the Sinspire, the most heavily guarded gambling house in the world, while forced onto the high seas as pirates.",
    },
    "The Republic of Thieves": {
        "author": "Scott Lynch", "series": "Gentleman Bastard", "pos": 3, "year": 2013,
        "rating": 4.22, "narrator": "Michael Page", "genres": ["Fantasy", "Heist"],
        "summary": "With Locke dying from a mysterious poison, the Bondsmagi of Karthain offer to save him on one condition: he and Jean must rig their five-year political election—against Locke's lost love, Sabetha.",
    },
    "The Last Wish": {
        "author": "Andrzej Sapkowski", "series": "The Witcher", "pos": 1, "year": 1993,
        "rating": 4.12, "narrator": "Peter Kenny", "genres": ["Fantasy", "Witcher"],
        "summary": "Geralt of Rivia is a Witcher, a mutated monster hunter for hire who stalks the Continent's beasts—and discovers that humans are often far more monstrous than the creatures he slays.",
    },
    "Sword of Destiny": {
        "author": "Andrzej Sapkowski", "series": "The Witcher", "pos": 2, "year": 1992,
        "rating": 4.30, "narrator": "Peter Kenny", "genres": ["Fantasy", "Witcher"],
        "summary": "Across six interconnected tales of fate and love, Geralt navigates his tumultuous bond with the sorceress Yennefer of Vengerberg and meets his Child of Surprise, Ciri.",
    },
    "Blood of Elves": {
        "author": "Andrzej Sapkowski", "series": "The Witcher", "pos": 3, "year": 1994,
        "rating": 4.12, "narrator": "Peter Kenny", "genres": ["Fantasy", "Witcher"],
        "summary": "After the slaughter of Cintra, Geralt spirits Princess Cirilla to the mountain fortress of Kaer Morhen to train as a Witcher while kings, mages, and Nilfgaardian agents hunt for her Elder Blood.",
    },
    "Time of Contempt": {
        "author": "Andrzej Sapkowski", "series": "The Witcher", "pos": 4, "year": 1995,
        "rating": 4.18, "narrator": "Peter Kenny", "genres": ["Fantasy", "Witcher"],
        "summary": "A coup at the conclave of mages on Thanedd Island shatters the Brotherhood of Sorcerers, scattering Geralt, Yennefer, and Ciri across a war-torn Continent.",
    },
    "Baptism of Fire": {
        "author": "Andrzej Sapkowski", "series": "The Witcher", "pos": 5, "year": 1996,
        "rating": 4.29, "narrator": "Peter Kenny", "genres": ["Fantasy", "Witcher"],
        "summary": "Healing in Brokilon Forest after Thanedd, Geralt sets out southward to rescue Ciri from Nilfgaard accompanied by Dandelion, archer Milva, enigmatic barber-surgeon Regis, and haunted knight Cahir.",
    },
    "The Tower of the Swallow": {
        "author": "Andrzej Sapkowski", "series": "The Witcher", "pos": 6, "year": 1997,
        "rating": 4.25, "narrator": "Peter Kenny", "genres": ["Fantasy", "Witcher"],
        "summary": "Brutally wounded by the bounty hunter Leo Bonhart, Ciri recounts her harrowing flight toward the mythical Tower of the Swallow as Geralt's fellowship battles across the Yaruga.",
    },
    "The Lady of the Lake": {
        "author": "Andrzej Sapkowski", "series": "The Witcher", "pos": 7, "year": 1999,
        "rating": 4.19, "narrator": "Peter Kenny", "genres": ["Fantasy", "Witcher"],
        "summary": "Jumping between worlds and times to escape the Wild Hunt, Ciri fights to return to her own world as Geralt's company storms Vilgefortz's stronghold in the epic finale of The Witcher saga.",
    },
    "Season of Storms": {
        "author": "Andrzej Sapkowski", "series": "The Witcher", "pos": 8, "year": 2013,
        "rating": 3.96, "narrator": "Peter Kenny", "genres": ["Fantasy", "Witcher"],
        "summary": "Set between the stories of The Last Wish, Geralt of Rivia's iconic silver and steel Witcher swords are stolen in the corrupt kingdom of Kerack, drawing him into a conspiracy of rogue mages.",
    },
    "We Are Legion (We Are Bob)": {
        "author": "Dennis E. Taylor", "series": "Bobiverse", "pos": 1, "year": 2016,
        "rating": 4.28, "narrator": "Ray Porter", "genres": ["Sci-Fi", "Space Exploration"],
        "summary": "After selling his software company, Bob Johansson is killed crossing the street—and wakes up a century later as a sentient AI controlling a self-replicating Von Neumann interstellar probe.",
    },
    "For We Are Many": {
        "author": "Dennis E. Taylor", "series": "Bobiverse", "pos": 2, "year": 2017,
        "rating": 4.32, "narrator": "Ray Porter", "genres": ["Sci-Fi", "Space Exploration"],
        "summary": "As generations of cloned Bobs spread across the galaxy terraforming worlds and rescuing Earth's survivors, they discover a terrifying hive-mind species known as the Others.",
    },
    "All These Worlds": {
        "author": "Dennis E. Taylor", "series": "Bobiverse", "pos": 3, "year": 2017,
        "rating": 4.33, "narrator": "Ray Porter", "genres": ["Sci-Fi", "Space Exploration"],
        "summary": "The Bobs rally every available probe and superweapon for an existential war against the Others as the alien armada advances toward the Sol system.",
    },
    "Heaven's River": {
        "author": "Dennis E. Taylor", "series": "Bobiverse", "pos": 4, "year": 2020,
        "rating": 4.30, "narrator": "Ray Porter", "genres": ["Sci-Fi", "Space Exploration"],
        "summary": "Searching for the missing Bender a century later, Bob-1 discovers a colossal billion-mile-long Topopolis megastructure inhabited by an alien species—while civil conflict brews inside the Bobiverse.",
    },
    "Not Till We Are Lost": {
        "author": "Dennis E. Taylor", "series": "Bobiverse", "pos": 5, "year": 2024,
        "rating": 4.25, "narrator": "Ray Porter", "genres": ["Sci-Fi", "Space Exploration"],
        "summary": "In the aftermath of the Starfleet crisis, Icarus and Daedalus uncover a shocking existential threat at the center of the Milky Way galaxy that forces humanity and the Bobs to rethink everything.",
    },
    "The Power of the Dog": {
        "author": "Don Winslow", "series": "Power of the Dog", "pos": 1, "year": 2005,
        "rating": 4.32, "narrator": "Ray Porter", "genres": ["Crime", "Thriller"],
        "summary": "DEA agent Art Keller wages a thirty-year blood feud against the Barrera brothers, rulers of the Mexican drug federation, in a sweeping, brutal epic of the War on Drugs.",
    },
    "The Cartel": {
        "author": "Don Winslow", "series": "Power of the Dog", "pos": 2, "year": 2015,
        "rating": 4.38, "narrator": "Ray Porter", "genres": ["Crime", "Thriller"],
        "summary": "When cartel kingpin Adán Barrera escapes from a Mexican maximum-security prison, Art Keller is pulled back into a decade-long narco war that consumes Juárez and Sinaloa.",
    },
    "The Border": {
        "author": "Don Winslow", "series": "Power of the Dog", "pos": 3, "year": 2019,
        "rating": 4.38, "narrator": "Ray Porter", "genres": ["Crime", "Thriller"],
        "summary": "Appointed head of the DEA, Art Keller brings the war home to Washington and Wall Street, exposing the American financial and political corruption fueling the heroin epidemic.",
    },
    "Project Hail Mary": {
        "author": "Andy Weir", "series": None, "pos": None, "year": 2021,
        "rating": 4.52, "narrator": "Ray Porter", "genres": ["Sci-Fi", "Hard Sci-Fi"],
        "summary": "Ryland Grace wakes up from a coma as the sole survivor on a desperate last-chance interstellar mission to save Earth's dimming sun—and meets an unexpected alien ally among the stars.",
    },
    "11/22/63": {
        "author": "Stephen King", "series": None, "pos": None, "year": 2011,
        "rating": 4.34, "narrator": "Craig Wasson", "genres": ["Sci-Fi", "Time Travel", "Historical Fiction"],
        "summary": "High school English teacher Jake Epping steps through a portal in a Maine diner pantry into 1958 on a five-year mission to stop Lee Harvey Oswald from assassinating JFK—only to find the past pushes back.",
    },
    "Lonesome Dove": {
        "author": "Larry McMurtry", "series": None, "pos": None, "year": 1985,
        "rating": 4.53, "narrator": "Lee Horsley", "genres": ["Western", "Historical Fiction"],
        "summary": "Former Texas Rangers Augustus McCrae and Woodrow F. Call lead an epic, perilous cattle drive from the Rio Grande to the untamed grasslands of Montana in the Pulitzer Prize-winning Western masterpiece.",
    },
    "World War Z": {
        "author": "Max Brooks", "series": None, "pos": None, "year": 2006,
        "rating": 4.02, "narrator": "Full Cast (Alan Alda, Mark Hamill, Nathan Fillion, Simon Pegg)", "genres": ["Horror", "Post-Apocalyptic"],
        "summary": "An oral history of the global Zombie War told through gripping firsthand interviews with survivors, soldiers, politicians, and scientists across the world.",
    },
    "The First Fifteen Lives of Harry August": {
        "author": "Claire North", "series": None, "pos": None, "year": 2014,
        "rating": 4.04, "narrator": "Peter Kenny", "genres": ["Sci-Fi", "Time Loop"],
        "summary": "Every time Harry August dies, he is reborn in the exact same time and place with all the memories of his previous lives intact—until a child brings a message from a future that is ending.",
    },
    "Midnight in Chernobyl": {
        "author": "Adam Higginbotham", "series": None, "pos": None, "year": 2019,
        "rating": 4.49, "narrator": "Jacques Roy", "genres": ["History", "Non-Fiction"],
        "summary": "The definitive, minute-by-minute account of the 1986 Chernobyl nuclear disaster, drawing on declassified Soviet archives and survivor interviews.",
    },
    "Greenlights": {
        "author": "Matthew McConaughey", "series": None, "pos": None, "year": 2020,
        "rating": 4.30, "narrator": "Matthew McConaughey", "genres": ["Memoir", "Biography"],
        "summary": "From thirty-five years of diaries and road journals, Matthew McConaughey shares an unconventional memoir of outlaw wisdom, stories, and catching more greenlights in life.",
    },
    "Kitchen Confidential": {
        "author": "Anthony Bourdain", "series": None, "pos": None, "year": 2000,
        "rating": 4.18, "narrator": "Anthony Bourdain", "genres": ["Memoir", "Food & Culinary"],
        "summary": "Anthony Bourdain's raw, hilarious, behind-the-kitchen-doors exposé of New York's culinary underbelly, read by Bourdain himself.",
    },
    "Endurance": {
        "author": "Alfred Lansing", "series": None, "pos": None, "year": 1959,
        "rating": 4.62, "narrator": "Simon Prebble", "genres": ["History", "Survival"],
        "summary": "The harrowing true story of Ernest Shackleton and his twenty-seven crewmen fighting to survive for over a year after their ship, the Endurance, was crushed by Antarctic pack ice in 1915.",
    },
    "Based on a True Story": {
        "author": "Norm Macdonald", "series": None, "pos": None, "year": 2016,
        "rating": 4.30, "narrator": "Norm Macdonald", "genres": ["Comedy", "Memoir"],
        "summary": "Norm Macdonald's wildly inventive, deadpan comic 'not-a-memoir' recounting his rise from rural Quebec to Saturday Night Live's Weekend Update desk and a high-stakes road trip to Las Vegas.",
    },
    "Best Served Cold": {
        "author": "Joe Abercrombie", "series": "The First Law", "pos": 4, "year": 2009,
        "rating": 4.24, "narrator": "Steven Pacey", "genres": ["Fantasy", "Grimdark", "Revenge"],
        "summary": "Mercenary general Monza Murcatto—the Snake of Talins—is betrayed and thrown down a mountain by her employer, Grand Duke Orso. Barely surviving, she recruits a band of Styria's most infamous outcasts to kill the seven men who betrayed her.",
    },
    "The Heroes": {
        "author": "Joe Abercrombie", "series": "The First Law", "pos": 5, "year": 2011,
        "rating": 4.32, "narrator": "Steven Pacey", "genres": ["Fantasy", "Grimdark", "Military"],
        "summary": "Over three blood-soaked days in the North, the Union army and Black Dow's Northmen clash for possession of a meaningless hilltop stone circle known as the Heroes.",
    },
    "Red Country": {
        "author": "Joe Abercrombie", "series": "The First Law", "pos": 6, "year": 2012,
        "rating": 4.25, "narrator": "Steven Pacey", "genres": ["Fantasy", "Grimdark", "Western"],
        "summary": "When Shy South's younger brother and sister are stolen from their burned frontier farmstead, she and her cowardly stepfather Lamb—an old man with a very bloody past—ride into the lawless Far Country to get them back.",
    },
    "The Blacktongue Thief": {
        "author": "Christopher Buehlman", "series": None, "pos": None, "year": 2021,
        "rating": 4.24, "narrator": "Christopher Buehlman", "genres": ["Fantasy", "Heist", "Dark Comedy"],
        "summary": "Kinch Na Shannack owes the Takers Guild a small fortune for his education as a thief. When his attempt to rob a veteran knight goes sideways, he is drafted onto a perilous quest across goblin-scarred kingdoms.",
    },
    "The Traitor Baru Cormorant": {
        "author": "Seth Dickinson", "series": "The Masquerade", "pos": 1, "year": 2015,
        "rating": 4.14, "narrator": "Christine Marshall", "genres": ["Fantasy", "Political Thriller"],
        "summary": "When the Empire of Masks conquers her island home, prodigy Baru Cormorant joins the Imperial civil service as an accountant—infiltrating the empire's financial machinery to destroy it from within.",
    },
    "Children of Time": {
        "author": "Adrian Tchaikovsky", "series": "Children of Time", "pos": 1, "year": 2015,
        "rating": 4.31, "narrator": "Mel Hudson", "genres": ["Sci-Fi", "Hard Sci-Fi"],
        "summary": "The last remnants of humanity flee a dying Earth toward a terraformed world, unaware that an evolutionary nanovirus intended for primates has instead uplifted the planet's jumping spiders into an industrial civilization.",
    },
    "Hyperion": {
        "author": "Dan Simmons", "series": "Hyperion Cantos", "pos": 1, "year": 1989,
        "rating": 4.25, "narrator": "Full Cast (Victor Bevine, Marc Vietor, Allyson Johnson, Jay Snyder)", "genres": ["Sci-Fi", "Space Opera"],
        "summary": "On the eve of interstellar Armageddon, seven pilgrims set forth on a final voyage to the world of Hyperion to confront the Time Tombs and the terrifying, blade-covered creature known as the Shrike.",
    },
    "There Is No Antimemetics Division": {
        "author": "qntm", "series": None, "pos": None, "year": 2020,
        "rating": 4.38, "narrator": "qntm", "genres": ["Sci-Fi", "Cosmic Horror"],
        "summary": "An antimeme is an idea with self-censoring properties—an idea that, by its intrinsic nature, discourages or prevents people from spreading or remembering it. How do you fight an enemy you can never remember exists?",
    },
    "The Tiger": {
        "author": "John Vaillant", "series": None, "pos": None, "year": 2010,
        "rating": 4.18, "narrator": "Robertson Dean", "genres": ["Non-Fiction", "Nature", "Survival"],
        "summary": "In December 1997, a wounded man-eating Amur tiger stalks the remote villages of Primorye in Russia's Far East, hunting down poachers with uncanny, vengeful precision while Inspection Tiger tracks it through the bitter Siberian winter.",
    },
    "Say Nothing": {
        "author": "Patrick Radden Keefe", "series": None, "pos": None, "year": 2018,
        "rating": 4.46, "narrator": "Matthew Blaney", "genres": ["History", "True Crime", "Non-Fiction"],
        "summary": "Starting with the 1972 abduction of widowed mother-of-ten Jean McConville from her Belfast flat, Patrick Radden Keefe chronicles the Troubles in Northern Ireland through the eyes of Dolours Price, Brendan Hughes, and Gerry Adams.",
    },
    "The Wager": {
        "author": "David Grann", "series": None, "pos": None, "year": 2023,
        "rating": 4.24, "narrator": "Dion Graham", "genres": ["History", "Maritime", "Survival"],
        "summary": "In 1741, HMS Wager wrecks on a desolate island off Patagonia during a secret mission around Cape Horn. Months later, two rival boats of ragged survivors wash ashore telling completely contradictory stories of mutiny and murder.",
    },
    "Heat": {
        "author": "Bill Buford", "series": None, "pos": None, "year": 2006,
        "rating": 4.13, "narrator": "Mel Foster", "genres": ["Memoir", "Food & Culinary"],
        "summary": "Leaving his desk at The New Yorker, Bill Buford signs on as a kitchen slave at Mario Batali's three-star restaurant Babbo before traveling to Italy to apprentice under pasta makers in Emilia-Romagna and Tuscan butcher Dario Cecchini.",
    },
    "Lincoln in the Bardo": {
        "author": "George Saunders", "series": None, "pos": None, "year": 2017,
        "rating": 3.92, "narrator": "Full Cast (Nick Offerman, David Sedaris, Bill Hader, Megan Mullally)", "genres": ["Historical Fiction", "Literary"],
        "summary": "Over a single February night in 1862, Abraham Lincoln visits the Georgetown crypt of his eleven-year-old son Willie while a chorus of bickering ghosts in the cemetery's transitional 'bardo' battles for the boy's soul.",
    },
    "Sixteen Ways to Defend a Walled City": {
        "author": "K.J. Parker", "series": "The Siege", "pos": 1, "year": 2019,
        "rating": 4.21, "narrator": "Rob Shapiro", "genres": ["Fantasy", "Military", "Engineering"],
        "summary": "When a massive hostile army besieges the imperial capital and the senior leadership is wiped out, disgraced military engineer Orhan—a con artist who prefers building bridges to fighting—must improvise sixteen ways to hold the walls.",
    },
    "The Quantum Thief": {
        "author": "Hannu Rajaniemi", "series": "Jean le Flambeur", "pos": 1, "year": 2010,
        "rating": 3.85, "narrator": "Scott Brick", "genres": ["Sci-Fi", "Post-Human Heist"],
        "summary": "Master thief Jean le Flambeur is broken out of a quantum dilemma prison by the warrior Mieli to pull off one final heist inside the Oubliette, a moving Martian city where time is currency and memories are locked behind cryptographic privacy shields.",
    },
    "Stories of Your Life and Others": {
        "author": "Ted Chiang", "series": None, "pos": None, "year": 2002,
        "rating": 4.28, "narrator": "Abby Craden, Todd McLaren, Carrington MacDuffie", "genres": ["Sci-Fi", "Short Stories"],
        "summary": "Ted Chiang's landmark first collection of eight speculative stories, including 'Tower of Babylon,' 'Understand,' and the Nebula Award-winning novella 'Story of Your Life' (adapted into the film Arrival).",
    },
    "True Grit": {
        "author": "Charles Portis", "series": None, "pos": None, "year": 1968,
        "rating": 4.16, "narrator": "Donna Tartt", "genres": ["Western", "Classic Fiction"],
        "summary": "Fourteen-year-old Mattie Ross leaves her Arkansas home in the winter of 1870 to avenge her father's murder, hiring one-eyed U.S. Marshal Rooster Cogburn—the meanest lawman in the Indian Territory—to track killer Tom Chaney.",
    },
    "The Dog of the South": {
        "author": "Charles Portis", "series": None, "pos": None, "year": 1979,
        "rating": 3.96, "narrator": "Geoffrey Lewis", "genres": ["Comic Fiction", "Road Novel"],
        "summary": "When Ray Midge's wife Norma runs off to Belize with her ex-husband—taking Ray's Ford Torino and credit cards—Ray sets out from Little Rock in a malfunctioning Buick Special to track them down across Mexico and Central America.",
    },
}

SERIES_DESCRIPTIONS: dict[str, str] = {
    "Red Rising Saga": "Pierce Brown's epic solar-system-spanning sci-fi saga of Darrow of Lykos and the Rising against the Gold Society, narrated by Tim Gerard Reynolds.",
    "Dungeon Crawler Carl": "Matt Dinniman's hit apocalyptic LitRPG series following Coast Guard vet Carl and Princess Donut through the eighteen-level World Dungeon, narrated by Jeff Hays.",
    "The Expanse": "James S.A. Corey's Hugo Award-winning nine-book space opera following Jim Holden and the crew of the Rocinante, narrated by Jefferson Mays.",
    "The First Law": "Joe Abercrombie's landmark grimdark fantasy saga featuring Logen Ninefingers, Inquisitor Glokta, and Bayaz, narrated by Steven Pacey.",
    "The Witcher": "Andrzej Sapkowski's complete Witcher saga following Geralt of Rivia, Yennefer of Vengerberg, and Cirilla of Cintra, narrated by Peter Kenny.",
    "Gentleman Bastard": "Scott Lynch's swashbuckling fantasy heist series following master thief Locke Lamora and Jean Tannen, narrated by Michael Page.",
    "Bobiverse": "Dennis E. Taylor's hard sci-fi exploration series following self-replicating Von Neumann AI probes across the galaxy, narrated by Ray Porter.",
    "Power of the Dog": "Don Winslow's monumental three-volume crime epic chronicling DEA agent Art Keller and the thirty-year Cartel wars, narrated by Ray Porter.",
    "★ 4.4+ Acclaimed Essentials": "Highest-rated audiobooks in the library (4.40+ Goodreads community rating across sci-fi, fantasy, fiction, and history).",
    "Standalone Essentials": "Complete single-volume novels, classics, and speculative fiction masterpieces—no multi-book commitment required.",
    "History, Survival & True Crime": "Gripping narrative nonfiction, polar expeditions, maritime disasters, and investigative history.",
    "Memoirs, Comedy & Kitchens": "Author-narrated memoirs, culinary exposés, and comedy classics.",
    "Full Cast Productions": "Immersive multi-voice ensemble audiobook productions featuring full actor casts.",
    "Narrated by Ray Porter": "Audiobooks performed by Audie Award-winning narrator Ray Porter (Project Hail Mary, Bobiverse, The Power of the Dog trilogy).",
    "Narrated by Peter Kenny": "Audiobooks performed by acclaimed British voice actor Peter Kenny (The Witcher saga, The First Fifteen Lives of Harry August).",
    "Narrated by Jefferson Mays": "Complete Expanse space opera performed by Audie Award winner Jefferson Mays.",
    "Narrated by Jeff Hays": "Soundbooth Theater founder Jeff Hays's tour-de-force vocal performance of Dungeon Crawler Carl.",
    "Narrated by Steven Pacey": "Joe Abercrombie's Circle of the World novels brought to life by master narrator Steven Pacey.",
    "Narrated by Tim Gerard Reynolds": "Pierce Brown's Red Rising Saga performed by Tim Gerard Reynolds.",
}

COLLECTION_BADGE_LABELS: dict[str, tuple[str, tuple[int, int, int]]] = {
    "Red Rising Saga": ("SERIES · 6 BOOKS", (239, 68, 68)),
    "Dungeon Crawler Carl": ("SERIES · 7 BOOKS", (245, 197, 24)),
    "The Expanse": ("SERIES · 9 BOOKS", (56, 189, 248)),
    "The First Law": ("SERIES · 6 BOOKS", (217, 119, 6)),
    "The Witcher": ("SERIES · 8 BOOKS", (168, 85, 247)),
    "Gentleman Bastard": ("SERIES · 3 BOOKS", (234, 179, 8)),
    "Bobiverse": ("SERIES · 5 BOOKS", (34, 197, 94)),
    "Power of the Dog": ("SERIES · 3 BOOKS", (220, 38, 38)),
    "★ 4.4+ Acclaimed Essentials": ("TOP RATED · 4.4+", (245, 197, 24)),
    "Standalone Essentials": ("STANDALONE PICKS", (56, 189, 248)),
    "History, Survival & True Crime": ("NON-FICTION", (16, 185, 129)),
    "Memoirs, Comedy & Kitchens": ("MEMOIR & COMEDY", (249, 115, 22)),
    "Full Cast Productions": ("FULL CAST AUDIO", (168, 85, 247)),
    "Narrated by Ray Porter": ("NARRATOR SPOTLIGHT", (56, 189, 248)),
    "Narrated by Peter Kenny": ("NARRATOR SPOTLIGHT", (56, 189, 248)),
    "Narrated by Jefferson Mays": ("NARRATOR SPOTLIGHT", (56, 189, 248)),
    "Narrated by Jeff Hays": ("NARRATOR SPOTLIGHT", (56, 189, 248)),
    "Narrated by Steven Pacey": ("NARRATOR SPOTLIGHT", (56, 189, 248)),
    "Narrated by Tim Gerard Reynolds": ("NARRATOR SPOTLIGHT", (56, 189, 248)),
}

AUTHOR_BIOS: dict[str, str] = {
    "Joe Abercrombie": "British fantasy author best known as 'Lord Grimdark' for The First Law trilogy and its standalone follow-ups in the Circle of the World.",
    "Pierce Brown": "Number-one New York Times bestselling author of the Red Rising Saga.",
    "Matt Dinniman": "Bestselling author of the Dungeon Crawler Carl series, Kaiju: Battlefield Surgeon, and Dominion of Blades.",
    "James S.A. Corey": "Pen name of collaborators Daniel Abraham and Ty Franck, authors of the Hugo Award-winning Expanse series and The Captive's War.",
    "Scott Lynch": "American fantasy writer and creator of the Gentleman Bastard sequence beginning with The Lies of Locke Lamora.",
    "Andrzej Sapkowski": "Celebrated Polish fantasy author and creator of Geralt of Rivia and The Witcher saga.",
    "Dennis E. Taylor": "Former computer programmer and bestselling sci-fi author of the Bobiverse series, Outland, and Roadkill.",
    "Don Winslow": "New York Times bestselling author of twenty-two crime and thriller novels, including The Power of the Dog trilogy and the Danny Ryan trilogy.",
    "Andy Weir": "Bestselling hard science fiction author of The Martian, Artemis, and Project Hail Mary.",
    "Stephen King": "One of the most acclaimed and prolific authors of modern horror, suspense, and speculative fiction, including 11/22/63, The Stand, and It.",
    "Larry McMurtry": "Pulitzer Prize and Academy Award-winning novelist and screenwriter, author of Lonesome Dove and The Last Picture Show.",
    "Max Brooks": "Author of World War Z, The Zombie Survival Guide, and Devolution.",
    "Claire North": "Pseudonym of acclaimed British novelist Catherine Webb, winner of the World Fantasy Award.",
    "Adam Higginbotham": "Journalist and historian, winner of the Andrew Carnegie Medal for Excellence in Nonfiction for Midnight in Chernobyl.",
    "Matthew McConaughey": "Academy Award-winning actor and bestselling author of Greenlights.",
    "Anthony Bourdain": "Chef, author of Kitchen Confidential, and Emmy-winning host of No Reservations and Parts Unknown.",
    "Alfred Lansing": "American journalist and historian whose 1959 classic Endurance remains the definitive account of Shackleton's Imperial Trans-Antarctic Expedition.",
    "Norm Macdonald": "Legendary Canadian stand-up comedian, writer, and Saturday Night Live Weekend Update anchor.",
}


def _load_state() -> dict[str, Any]:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    return {"enriched_albums": {}, "dynamic_catalog": {}}


def _save_state(state: dict[str, Any]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2))


def _resolve_plex_token(state: dict[str, Any]) -> str:
    tok = os.environ.get("PLEX_TOKEN", "")
    if tok:
        return tok
    if os.path.exists("/secrets/env.json"):
        try:
            env_data = json.loads(Path("/secrets/env.json").read_text())
            tok = env_data.get("PLEX_TOKEN", "")
            if tok:
                return tok
        except Exception:
            pass
    if state.get("plex_token"):
        return str(state["plex_token"])
    # Dynamically query Plex Preferences.xml on Host 1 via SSH
    rc, out, _ = nas_config.run_ssh(
        nas_config.HOST_1_IP,
        f"sudo grep -o 'PlexOnlineToken=\"[^\"]*\"' '{DOCKER_ROOT.rsplit('/', 1)[0]}/Plex/Library/Application Support/Plex Media Server/Preferences.xml' 2>/dev/null || "
        f"sudo find {DOCKER_ROOT.rsplit('/', 1)[0]} -maxdepth 4 -name 'Preferences.xml' -exec grep -o 'PlexOnlineToken=\"[^\"]*\"' {{}} + 2>/dev/null | head -n 1",
        timeout=20,
    )
    m = re.search(r'PlexOnlineToken="([^"]+)"', out or "")
    if m:
        state["plex_token"] = m.group(1)
        _save_state(state)
        return m.group(1)
    raise RuntimeError("Unable to resolve PLEX_TOKEN from environment, /secrets/env.json, or Host 1 Preferences.xml")


def plex_req(
    path: str,
    token: str,
    method: str = "GET",
    params: dict[str, Any] | None = None,
    data: bytes | None = None,
    headers: dict[str, str] | None = None,
    timeout: int = 30,
) -> dict[str, Any]:
    base_url = f"http://{nas_config.HOST_1_IP}:32400"
    q: dict[str, Any] = {"X-Plex-Token": token}
    if params:
        q.update(params)
    url = f"{base_url}{path}?{urllib.parse.urlencode(q, doseq=True)}"
    hdrs = {"Accept": "application/json"}
    if headers:
        hdrs.update(headers)
    req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
        if not raw:
            return {}
        try:
            return json.loads(raw.decode("utf-8", errors="ignore"))
        except Exception:
            return {"raw": raw}


def clean_publisher_praise(raw_description: str) -> str:
    """Strip publisher marketing banners, newspaper review lists, and pull quotes from book descriptions."""
    if not raw_description:
        return ""
    raw_html = html.unescape(str(raw_description))
    blocks = re.split(r"(?:<br\s*/?>|</p>|\n)+", raw_html, flags=re.I)
    kept_blocks: list[str] = []
    praise_pat = re.compile(
        r"(ONE OF THE MOST ACCLAIMED|NAMED A BEST BOOK|NEW YORK TIMES BESTSELLER|"
        r"NATIONAL BESTSELLER|INTERNATIONAL BESTSELLER|WINNER OF THE|FINALIST FOR|"
        r"PRAISE FOR|ACCLAIM FOR|WASHINGTON POST|FINANCIAL TIMES|THE GUARDIAN|"
        r"KIRKUS REVIEWS|PUBLISHERS WEEKLY|BOOKLIST|DALLAS MORNING NEWS|SUNDAY TIMES)",
        re.I,
    )
    html_tag_re = re.compile(r"</?[a-zA-Z][a-zA-Z0-9]*(?:\s+[^<>]*)?>")
    for blk in blocks:
        plain = html_tag_re.sub(" ", blk).strip()
        plain = re.sub(r"\s+", " ", plain)
        if not plain:
            continue
        alpha = [c for c in plain if c.isalpha()]
        upper_ratio = (sum(1 for c in alpha if c.isupper()) / len(alpha)) if len(alpha) >= 12 else 0.0
        if upper_ratio > 0.55:
            continue
        if plain.count("•") >= 2 or praise_pat.search(plain):
            continue
        if plain.startswith(('"', "“", "'", "‘")) and ("—" in plain or " - " in plain or len(plain) < 180):
            continue
        kept_blocks.append(plain)
    cleaned = " ".join(kept_blocks).strip()
    cleaned = re.sub(
        r"^(?:From the (?:New York Times )?(?:bestselling|acclaimed) author[^,]*,\s*(?:here is\s*)?|With over [0-9]+ million copies sold,\s*)",
        "",
        cleaned,
        flags=re.I,
    ).strip()
    if not cleaned:
        cleaned = html_tag_re.sub(" ", raw_html).strip()
        cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned


def resolve_dynamic_book_metadata(title: str, author: str, state: dict[str, Any]) -> dict[str, Any]:
    """Dynamically resolve rating, narrator, series, year, genres, and synopsis for new audiobooks."""
    dyn_cache = state.setdefault("dynamic_catalog", {})
    cache_key = f"{author}::{title}"
    if cache_key in dyn_cache:
        return dyn_cache[cache_key]

    meta: dict[str, Any] = {
        "author": author,
        "series": None,
        "pos": None,
        "year": 2024,
        "rating": 4.25,
        "narrator": author,
        "genres": ["Audiobook"],
        "summary": "",
    }

    # 1. Query Apple Books / iTunes Audiobook API for narrator, description, year, genre
    try:
        q = f"{title} {author}"
        url = f"https://itunes.apple.com/search?{urllib.parse.urlencode({'term': q, 'media': 'audiobook', 'limit': 3})}"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=10) as r:
            items = json.loads(r.read().decode()).get("results", [])
            if items:
                it = items[0]
                if it.get("releaseDate"):
                    meta["year"] = int(str(it["releaseDate"])[:4])
                if it.get("primaryGenreName"):
                    meta["genres"] = [it["primaryGenreName"]]
                desc = clean_publisher_praise(it.get("description", ""))
                if desc:
                    meta["summary"] = desc
                # Extract 'Narrated by X' or 'Read by X' if present in description/collectionName
                m_nar = re.search(r"(?:Narrated|Read) by ([A-Z][A-Za-z .'-]+(?:,\s*[A-Z][A-Za-z .'-]+)*)", it.get("description", ""))
                if m_nar:
                    meta["narrator"] = m_nar.group(1).strip()
    except Exception:
        pass

    # 2. Query OpenLibrary Search API for community star rating & series info
    try:
        q = f"{title} {author}"
        url = f"https://openlibrary.org/search.json?{urllib.parse.urlencode({'q': q, 'limit': 3})}"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=10) as r:
            docs = json.loads(r.read().decode()).get("docs", [])
            for d in docs:
                if d.get("ratings_average"):
                    meta["rating"] = round(float(d["ratings_average"]), 2)
                if d.get("first_publish_year"):
                    meta["year"] = int(d["first_publish_year"])
                break
    except Exception:
        pass

    # 3. Query Google Books API as fallback for rating and description
    if not meta["summary"]:
        try:
            q = f"intitle:{title} inauthor:{author}"
            url = f"https://www.googleapis.com/books/v1/volumes?{urllib.parse.urlencode({'q': q, 'maxResults': 3})}"
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=10) as r:
                items = json.loads(r.read().decode()).get("items", [])
                for it in items:
                    vi = it.get("volumeInfo", {})
                    if vi.get("averageRating") and meta["rating"] == 4.25:
                        meta["rating"] = round(float(vi["averageRating"]), 2)
                    if vi.get("description") and not meta["summary"]:
                        meta["summary"] = clean_publisher_praise(vi["description"])
                    if vi.get("categories") and meta["genres"] == ["Audiobook"]:
                        meta["genres"] = vi["categories"][:2]
        except Exception:
            pass

    if not meta["summary"]:
        meta["summary"] = f"Unabridged audiobook edition of {title} by {author}."

    dyn_cache[cache_key] = meta
    _save_state(state)
    return meta


def match_book_title(raw_title: str, raw_artist: str = "") -> str | None:
    t = (raw_title or "").lower()
    a = (raw_artist or "").lower()
    if "abercrombie" in a:
        if "blade" in t:
            return "The Blade Itself"
        if "hanged" in t:
            return "Before They Are Hanged"
        if "served" in t:
            return "Best Served Cold"
        if "hero" in t:
            return "The Heroes"
        if "country" in t:
            return "Red Country"
        if "argument" in t or "kings" in t:
            return "Last Argument of Kings"
    if "harry august" in t or "primeiras quinze vidas" in t or "claire north" in a:
        return "The First Fifteen Lives of Harry August"
    if "11" in t and "63" in t:
        return "11/22/63"
    for canon_title in BOOK_CATALOG:
        ct_low = canon_title.lower()
        if ct_low in t:
            return canon_title
        if ct_low.startswith("the ") and ct_low[4:] in t:
            return canon_title
    if "we are legion" in t:
        return "We Are Legion (We Are Bob)"
    if "doomsday scenario" in t:
        return "Carl's Doomsday Scenario"
    if "anarchist" in t:
        return "The Dungeon Anarchist's Cookbook"
    if "feral gods" in t:
        return "The Gate of the Feral Gods"
    if "masquerade" in t:
        return "The Butcher's Masquerade"
    if "bedlam bride" in t:
        return "The Eye of the Bedlam Bride"
    if "inevitable ruin" in t:
        return "This Inevitable Ruin"
    return None


def fetch_clean_cover(canon_title: str, meta: dict[str, Any], rk: str, token: str) -> Image.Image:
    COVER_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    safe_name = re.sub(r"[^a-zA-Z0-9_-]", "_", canon_title)
    clean_path = COVER_CACHE_DIR / f"{safe_name}.jpg"
    if clean_path.exists() and clean_path.stat().st_size > 15000:
        return Image.open(clean_path).convert("RGB")

    # 1. Apple Books / iTunes 1000x1000 square audiobook cover
    try:
        q = f"{canon_title} {meta['author']}"
        url = f"https://itunes.apple.com/search?{urllib.parse.urlencode({'term': q, 'media': 'audiobook', 'limit': 5})}"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=10) as r:
            res = json.loads(r.read().decode())
            for item in res.get("results", []):
                art_url = item.get("artworkUrl100", "")
                if art_url:
                    hi_url = re.sub(r"\d+x\d+bb", "1000x1000bb", art_url)
                    with urllib.request.urlopen(urllib.request.Request(hi_url, headers={"User-Agent": "Mozilla/5.0"}), timeout=12) as ir:
                        raw = ir.read()
                        if len(raw) > 15000:
                            img = Image.open(io.BytesIO(raw)).convert("RGB")
                            img.save(clean_path, "JPEG", quality=95)
                            return img
    except Exception:
        pass

    # 2. Existing Plex thumb
    try:
        alb_meta = plex_req(f"/library/metadata/{rk}", token).get("MediaContainer", {}).get("Metadata", [{}])[0]
        thumb_path = alb_meta.get("thumb")
        if thumb_path:
            raw = plex_req(thumb_path, token).get("raw")
            if raw and len(raw) > 5000:
                img = Image.open(io.BytesIO(raw)).convert("RGB")
                img.save(clean_path, "JPEG", quality=95)
                return img
    except Exception:
        pass

    # 3. OpenLibrary cover fallback
    try:
        q = f"{canon_title} {meta['author']}"
        url = f"https://openlibrary.org/search.json?{urllib.parse.urlencode({'q': q, 'limit': 3})}"
        with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"}), timeout=10) as r:
            docs = json.loads(r.read().decode()).get("docs", [])
            for d in docs:
                cid = d.get("cover_i")
                if cid:
                    c_url = f"https://covers.openlibrary.org/b/id/{cid}-L.jpg"
                    with urllib.request.urlopen(c_url, timeout=10) as ir:
                        raw = ir.read()
                        if len(raw) > 10000:
                            img = Image.open(io.BytesIO(raw)).convert("RGB")
                            img.save(clean_path, "JPEG", quality=95)
                            return img
    except Exception:
        pass

    return Image.new("RGB", (1000, 1000), (24, 28, 36))


def render_badged_cover(base_img: Image.Image, rating: float, series_name: str | None = None, series_pos: int | None = None) -> bytes:
    """Render Kometa-style rounded badges onto a 1000x1000 square audiobook cover."""
    img = base_img.resize((1000, 1000), Image.Resampling.LANCZOS).convert("RGBA")
    overlay = Image.new("RGBA", (1000, 1000), (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)

    font_bold_path = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
    if not Path(font_bold_path).exists():
        font_bold = ImageFont.load_default()
        font_sub = ImageFont.load_default()
    else:
        font_bold = ImageFont.truetype(font_bold_path, 40)
        font_sub = ImageFont.truetype(font_bold_path, 34)

    rating_text = f"★ {rating:.2f}"
    bbox = draw.textbbox((0, 0), rating_text, font=font_bold)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    pad_x, pad_y = 26, 16
    x1, y1 = 28, 28
    x2, y2 = x1 + tw + pad_x * 2, y1 + th + pad_y * 2

    draw.rounded_rectangle([x1 + 3, y1 + 4, x2 + 3, y2 + 4], radius=24, fill=(0, 0, 0, 150))
    draw.rounded_rectangle([x1, y1, x2, y2], radius=24, fill=(15, 18, 24, 225), outline=(245, 197, 24, 235), width=3)
    draw.text((x1 + pad_x - bbox[0], y1 + pad_y - bbox[1]), rating_text, font=font_bold, fill=(255, 210, 48, 255))

    if series_pos is not None:
        pos_str = f"BOOK {series_pos}"
        bbox2 = draw.textbbox((0, 0), pos_str, font=font_sub)
        tw2, th2 = bbox2[2] - bbox2[0], bbox2[3] - bbox2[1]
        px, py = 24, 15
        bx2 = 1000 - 28
        bx1 = bx2 - (tw2 + px * 2)
        by1 = 28
        by2 = by1 + th2 + py * 2

        draw.rounded_rectangle([bx1 + 3, by1 + 4, bx2 + 3, by2 + 4], radius=22, fill=(0, 0, 0, 150))
        draw.rounded_rectangle([bx1, by1, bx2, by2], radius=22, fill=(18, 25, 38, 225), outline=(96, 165, 250, 230), width=3)
        draw.text((bx1 + px - bbox2[0], by1 + py - bbox2[1]), pos_str, font=font_sub, fill=(240, 248, 255, 255))

    combined = Image.alpha_composite(img, overlay).convert("RGB")
    buf = io.BytesIO()
    combined.save(buf, "JPEG", quality=93)
    return buf.getvalue()


def _crop_and_resize(img: Image.Image, target_w: int, target_h: int) -> Image.Image:
    """Center-crop and resize an image to fill target dimensions without distortion."""
    src_w, src_h = img.size
    scale = max(target_w / src_w, target_h / src_h)
    new_w = max(target_w, int(src_w * scale))
    new_h = max(target_h, int(src_h * scale))
    resized = img.resize((new_w, new_h), Image.Resampling.LANCZOS)
    left = (new_w - target_w) // 2
    top = (new_h - target_h) // 2
    return resized.crop((left, top, left + target_w, top + target_h))


def render_collection_poster(
    collection_name: str,
    covers: list[Any] | None = None,
) -> bytes:
    """Render a custom 1000x1000 collection poster with Kometa-style badge and cover layout.

    For 3-book series, renders 1 hero cover on the left and 2 stacked covers on the right
    so covers are never repeated in a 2x2 grid.
    """
    canvas = Image.new("RGB", (1000, 1000), (18, 22, 28))

    pil_covers: list[Image.Image] = []
    if covers:
        for c in covers:
            if isinstance(c, Image.Image):
                pil_covers.append(c.convert("RGB"))
            elif isinstance(c, (str, Path)):
                try:
                    pil_covers.append(Image.open(c).convert("RGB"))
                except Exception:
                    pass
            elif isinstance(c, bytes):
                try:
                    pil_covers.append(Image.open(io.BytesIO(c)).convert("RGB"))
                except Exception:
                    pass

    num_covers = len(pil_covers)
    if num_covers == 1:
        canvas.paste(_crop_and_resize(pil_covers[0], 1000, 1000), (0, 0))
    elif num_covers == 2:
        canvas.paste(_crop_and_resize(pil_covers[0], 500, 1000), (0, 0))
        canvas.paste(_crop_and_resize(pil_covers[1], 500, 1000), (500, 0))
    elif num_covers == 3:
        # 3-book hero layout: left half hero, right half stacked 2 books (never repeats in 2x2 grid)
        canvas.paste(_crop_and_resize(pil_covers[0], 500, 1000), (0, 0))
        canvas.paste(_crop_and_resize(pil_covers[1], 500, 500), (500, 0))
        canvas.paste(_crop_and_resize(pil_covers[2], 500, 500), (500, 500))
    elif num_covers >= 4:
        # 2x2 grid with 4 distinct covers
        canvas.paste(_crop_and_resize(pil_covers[0], 500, 500), (0, 0))
        canvas.paste(_crop_and_resize(pil_covers[1], 500, 500), (500, 0))
        canvas.paste(_crop_and_resize(pil_covers[2], 500, 500), (0, 500))
        canvas.paste(_crop_and_resize(pil_covers[3], 500, 500), (500, 500))

    overlay = Image.new("RGBA", (1000, 1000), (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)

    # Dividers between tiled covers
    if num_covers in (2, 3, 4):
        draw.line([(500, 0), (500, 1000)], fill=(15, 18, 24, 255), width=4)
    if num_covers == 3:
        draw.line([(500, 500), (1000, 500)], fill=(15, 18, 24, 255), width=4)
    elif num_covers >= 4:
        draw.line([(0, 500), (1000, 500)], fill=(15, 18, 24, 255), width=4)

    # Subtle top vignette for badge contrast
    for y in range(0, 120):
        alpha = int(140 * ((120 - y) / 120.0) ** 1.5)
        draw.line([(0, y), (1000, y)], fill=(10, 12, 16, alpha))

    # Bottom dark gradient overlay for collection title
    for y in range(800, 1000):
        alpha = int(230 * ((y - 800) / 200.0) ** 1.3)
        draw.line([(0, y), (1000, y)], fill=(10, 12, 16, alpha))

    font_bold_path = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
    if not Path(font_bold_path).exists():
        font_badge = ImageFont.load_default()
        font_title = ImageFont.load_default()
    else:
        font_badge = ImageFont.truetype(font_bold_path, 32)
        font_title = ImageFont.truetype(font_bold_path, 38)

    # Badge Pill (Top-Left)
    if collection_name in COLLECTION_BADGE_LABELS:
        badge_text, badge_color = COLLECTION_BADGE_LABELS[collection_name]
    elif collection_name.startswith("Narrated by "):
        badge_text = "NARRATOR SPOTLIGHT"
        badge_color = (56, 189, 248)
    else:
        count = len(pil_covers)
        badge_text = f"SERIES · {count} BOOKS" if count > 0 else "COLLECTION"
        badge_color = (56, 189, 248)

    bbox = draw.textbbox((0, 0), badge_text, font=font_badge)
    btw, bth = bbox[2] - bbox[0], bbox[3] - bbox[1]
    px, py = 24, 14
    bx1, by1 = 28, 28
    bx2, by2 = bx1 + btw + px * 2, by1 + bth + py * 2

    # Drop shadow & pill
    draw.rounded_rectangle([bx1 + 3, by1 + 4, bx2 + 3, by2 + 4], radius=22, fill=(0, 0, 0, 160))
    draw.rounded_rectangle([bx1, by1, bx2, by2], radius=22, fill=(15, 18, 24, 230), outline=(*badge_color, 235), width=3)
    draw.text((bx1 + px - bbox[0], by1 + py - bbox[1]), badge_text, font=font_badge, fill=(*badge_color, 255))

    # Bottom Collection Title
    t_bbox = draw.textbbox((0, 0), collection_name, font=font_title)
    tw_t, th_t = t_bbox[2] - t_bbox[0], t_bbox[3] - t_bbox[1]
    font_draw = font_title
    if tw_t > 920:
        font_draw = font_badge
        t_bbox = draw.textbbox((0, 0), collection_name, font=font_draw)
        tw_t, th_t = t_bbox[2] - t_bbox[0], t_bbox[3] - t_bbox[1]

    tx = 36
    ty = 1000 - 36 - th_t
    draw.text((tx + 2, ty + 2), collection_name, font=font_draw, fill=(0, 0, 0, 220))
    draw.text((tx, ty), collection_name, font=font_draw, fill=(255, 255, 255, 255))

    combined = Image.alpha_composite(canvas.convert("RGBA"), overlay).convert("RGB")
    buf = io.BytesIO()
    combined.save(buf, "JPEG", quality=93)
    return buf.getvalue()


def compute_book_collections(
    canon_title: str,
    meta: dict[str, Any],
    series_counts: dict[str, int],
    narrator_counts: dict[str, int],
) -> list[str]:
    """Compute multi-dimensional collection tags for an audiobook.

    Assigns:
    - Series name ONLY if series_counts.get(series, 0) >= 2.
    - '★ 4.4+ Acclaimed Essentials' if rating >= 4.40.
    - 'History, Survival & True Crime' if genres match history, non-fiction, survival, true crime, maritime, or nature.
    - 'Memoirs, Comedy & Kitchens' if genres match memoir, biography, food, culinary, or title in ('Based on a True Story', 'Kitchen Confidential', 'Heat', 'Greenlights').
    - 'Standalone Essentials' if (not series or series_counts.get(series, 0) < 2) and not in History/Memoir.
    - 'Full Cast Productions' if 'Full Cast' or ',' in narrator.
    - 'Narrated by <Narrator>' for narrators with 3+ books in the library.
    """
    collections: list[str] = []

    # 1. Series name ONLY if series_counts.get(series, 0) >= 2
    series = meta.get("series")
    if series and series_counts.get(series, 0) >= 2:
        collections.append(series)

    # 2. ★ 4.4+ Acclaimed Essentials
    try:
        rating = float(meta.get("rating", 0))
    except (ValueError, TypeError):
        rating = 0.0

    if rating >= 4.40:
        collections.append("★ 4.4+ Acclaimed Essentials")

    # 3. History, Survival & True Crime
    genres = [str(g).lower() for g in meta.get("genres", [])]
    history_keywords = {"history", "non-fiction", "survival", "true crime", "maritime", "nature"}
    is_history = any(
        g in history_keywords
        or (g != "historical fiction" and any(k in g for k in ("non-fiction", "survival", "true crime", "maritime", "nature")))
        or (g == "history" or (g.startswith("history") and not g.startswith("historical")))
        for g in genres
    )
    if is_history:
        collections.append("History, Survival & True Crime")

    # 4. Memoirs, Comedy & Kitchens
    memoir_keywords = ("memoir", "biography", "food", "culinary")
    is_memoir = (
        any(any(k in g for k in memoir_keywords) for g in genres)
        or canon_title in ("Based on a True Story", "Kitchen Confidential", "Heat", "Greenlights")
    )
    if is_memoir:
        collections.append("Memoirs, Comedy & Kitchens")

    # 5. Standalone Essentials (if (not series or series_counts < 2) and not in History/Memoir)
    if (not series or series_counts.get(series, 0) < 2) and not is_history and not is_memoir:
        collections.append("Standalone Essentials")

    # 6. Full Cast Productions if 'Full Cast' or ',' in narrator
    narrator = str(meta.get("narrator", meta.get("author", "")) or "")
    if "Full Cast" in narrator or "," in narrator:
        collections.append("Full Cast Productions")

    # 7. Narrated by <Narrator> for narrators with 3+ books in the library
    if narrator:
        if "," not in narrator and narrator_counts.get(narrator, 0) >= 3:
            collections.append(f"Narrated by {narrator}")
        else:
            for n in [x.strip() for x in narrator.split(",")]:
                if n and narrator_counts.get(n, 0) >= 3:
                    tag = f"Narrated by {n}"
                    if tag not in collections:
                        collections.append(tag)

    # Deduplicate preserving insertion order
    seen: set[str] = set()
    deduped: list[str] = []
    for c in collections:
        if c not in seen:
            seen.add(c)
            deduped.append(c)

    return deduped


def run_plex_audiobook_enricher(force: bool = False, quiet: bool = False) -> tuple[bool, str]:
    """Execute the end-to-end Plex Audiobook Enricher pipeline."""
    state = _load_state()
    token = _resolve_plex_token(state)

    # 1. Ensure all audiobook files on Host 1 have world-readable permissions (fixes 0600 files skipped by Plex)
    nas_config.run_ssh(
        nas_config.HOST_1_IP,
        f"sudo chmod -R a+rX {DOCKER_ROOT.rsplit('/', 1)[0]}/data/media/audiobooks 2>/dev/null || true",
        timeout=20,
    )

    # Trigger quick library refresh
    plex_req("/library/sections/4/refresh", token)
    time.sleep(4)

    albums = plex_req("/library/sections/4/all", token, params={"type": 9}).get("MediaContainer", {}).get("Metadata", [])

    # 2. Consolidate any split sub-albums belonging to the same disk folder
    folder_groups: dict[str, list[str]] = {}
    rk_to_folder: dict[str, tuple[str, str]] = {}
    for a in albums:
        rk = str(a["ratingKey"])
        children = plex_req(f"/library/metadata/{rk}/children", token).get("MediaContainer", {}).get("Metadata", [])
        if children:
            pts = Path(children[0].get("Media", [{}])[0].get("Part", [{}])[0].get("file", "")).parts
            if len(pts) >= 6:
                fl = f"{pts[4]}/{pts[5]}"
                folder_groups.setdefault(fl, []).append(rk)
                rk_to_folder[rk] = (pts[4], pts[5])

    merged_splits = 0
    for fl, rks in sorted(folder_groups.items()):
        if len(rks) > 1:
            primary = rks[0]
            others = rks[1:]
            for i in range(0, len(others), 40):
                chunk = others[i : i + 40]
                plex_req(f"/library/metadata/{primary}/merge", token, method="PUT", params={"ids": ",".join(chunk)})
                merged_splits += len(chunk)

    if merged_splits > 0:
        time.sleep(2)
        albums = plex_req("/library/sections/4/all", token, params={"type": 9}).get("MediaContainer", {}).get("Metadata", [])

    # 3. Pass 1: Resolve metadata for all albums to compute series_counts and narrator_counts
    resolved_albums: list[dict[str, Any]] = []
    series_counts: dict[str, int] = {}
    narrator_counts: dict[str, int] = {}

    for alb in albums:
        rk = str(alb["ratingKey"])
        raw_title = alb.get("title", "")
        raw_artist = alb.get("parentTitle", "")

        folder_author, folder_book = rk_to_folder.get(rk, ("", ""))
        if not folder_book:
            children = plex_req(f"/library/metadata/{rk}/children", token).get("MediaContainer", {}).get("Metadata", [])
            if children:
                pts = Path(children[0].get("Media", [{}])[0].get("Part", [{}])[0].get("file", "")).parts
                if len(pts) >= 6:
                    folder_author, folder_book = pts[4], pts[5]

        clean_book = folder_book.replace("11_22_63", "11/22/63") if folder_book else ""
        canon = clean_book if clean_book in BOOK_CATALOG else match_book_title(clean_book or raw_title, folder_author or raw_artist)

        if canon and canon in BOOK_CATALOG:
            meta = BOOK_CATALOG[canon]
        else:
            canon = clean_book or raw_title
            meta = resolve_dynamic_book_metadata(canon, folder_author or raw_artist, state)

        resolved_albums.append({
            "alb": alb,
            "rk": rk,
            "canon": canon,
            "meta": meta,
        })

        ser = meta.get("series")
        if ser:
            series_counts[ser] = series_counts.get(ser, 0) + 1

        narr = meta.get("narrator", meta.get("author", ""))
        if narr:
            narrator_counts[narr] = narrator_counts.get(narr, 0) + 1
            if "," in narr:
                for n in [x.strip() for x in narr.split(",")]:
                    if n:
                        narrator_counts[n] = narrator_counts.get(n, 0) + 1

    # Pass 2: Enrich, badge albums & assign multi-dimensional collections
    enriched_map = state.setdefault("enriched_albums", {})
    newly_enriched: list[str] = []
    total_verified = 0

    for item in resolved_albums:
        alb = item["alb"]
        rk = item["rk"]
        canon = item["canon"]
        meta = item["meta"]

        book_cols = compute_book_collections(canon, meta, series_counts, narrator_counts)
        cols_sig = ",".join(sorted(book_cols))

        sig = f"{canon}::{meta.get('rating')}::{meta.get('series')}::{meta.get('pos')}::{cols_sig}"
        if not force and enriched_map.get(rk) == sig and alb.get("userRating") and alb.get("title") == canon:
            total_verified += 1
            continue

        gr_rating = float(meta["rating"])
        plex_rating_10 = round(gr_rating * 2.0, 1)
        series = meta.get("series")
        pos = meta.get("pos")
        year = meta.get("year", 2024)
        narrator = meta.get("narrator", meta["author"])

        if series and pos is not None:
            sort_title = f"{series} {int(pos):02d} - {canon}"
            header_line = f"★ {gr_rating:.2f} Goodreads  ·  {series}, Book {pos}  ·  Narrated by {narrator}"
        else:
            sort_title = canon
            header_line = f"★ {gr_rating:.2f} Goodreads  ·  Narrated by {narrator}"

        full_summary = f"{header_line}\n\n{meta['summary']}"

        try:
            plex_req(f"/library/metadata/{rk}", token, method="PUT", params={"title": canon})
        except Exception:
            pass

        update_params: dict[str, Any] = {
            "type": 9,
            "id": rk,
            "titleSort.value": sort_title,
            "titleSort.locked": 1,
            "summary.value": full_summary,
            "summary.locked": 1,
            "userRating.value": plex_rating_10,
            "userRating.locked": 1,
            "rating.value": plex_rating_10,
            "rating.locked": 1,
            "originallyAvailableAt.value": f"{year}-01-01",
            "originallyAvailableAt.locked": 1,
            "studio.value": f"Narrated by {narrator}",
            "studio.locked": 1,
        }
        for g_idx, g_name in enumerate(meta.get("genres", [])):
            update_params[f"genre[{g_idx}].tag.tag"] = g_name
        update_params["genre.locked"] = 1

        # Clear stale collections on this album if any
        existing_cols = [c.get("tag") for c in alb.get("Collection", []) if c.get("tag")]
        stale_cols = [c for c in existing_cols if c not in book_cols]
        for sc in stale_cols:
            plex_req(
                "/library/sections/4/all",
                token,
                method="PUT",
                params={"type": 9, "id": rk, "collection[].tag.tag-": sc},
            )

        for c_idx, c_name in enumerate(book_cols):
            update_params[f"collection[{c_idx}].tag.tag"] = c_name
        if book_cols:
            update_params["collection.locked"] = 1
        elif existing_cols:
            update_params["collection.locked"] = 0

        plex_req("/library/sections/4/all", token, method="PUT", params=update_params)

        clean_img = fetch_clean_cover(canon, meta, rk, token)
        badged_bytes = render_badged_cover(clean_img, gr_rating, series, pos)
        plex_req(f"/library/metadata/{rk}/posters", token, method="POST", data=badged_bytes, headers={"Content-Type": "image/jpeg"})

        enriched_map[rk] = sig
        newly_enriched.append(f"{meta['author']} — {canon} (★ {gr_rating:.2f})")
        total_verified += 1

    # 4. Configure Collections, Purge 1-Book Stubs & Upload Custom Posters
    if newly_enriched or force:
        cols = plex_req("/library/sections/4/collections", token).get("MediaContainer", {}).get("Metadata", [])
        for col in cols:
            crk = str(col["ratingKey"])
            ctitle = col.get("title", "")
            child_count = int(col.get("childCount", 0))

            if child_count < 2:
                # Delete 1-book series stub collection
                plex_req(f"/library/metadata/{crk}", token, method="DELETE")
                continue

            desc = SERIES_DESCRIPTIONS.get(ctitle, f"Complete {ctitle} audiobook series in chronological reading order.")
            plex_req(
                "/library/sections/4/all",
                token,
                method="PUT",
                params={"type": 18, "id": crk, "summary.value": desc, "summary.locked": 1},
            )
            plex_req(f"/library/metadata/{crk}/prefs", token, method="PUT", params={"collectionSort": 1, "collectionMode": 0})

            # Fetch covers for books in this collection
            items = plex_req(f"/library/metadata/{crk}/children", token).get("MediaContainer", {}).get("Metadata", [])
            col_covers: list[Image.Image] = []
            for item in items:
                it_title = item.get("title", "")
                safe_name = re.sub(r'[\\/*?:"<>|]', "", it_title).strip()
                clean_path = COVER_CACHE_DIR / f"{safe_name}.jpg"
                c_img = None
                if clean_path.exists():
                    try:
                        c_img = Image.open(clean_path).convert("RGB")
                    except Exception:
                        pass
                if c_img is None:
                    thumb = item.get("thumb")
                    if thumb:
                        try:
                            raw = plex_req(thumb, token).get("raw")
                            if raw and len(raw) > 3000:
                                c_img = Image.open(io.BytesIO(raw)).convert("RGB")
                        except Exception:
                            pass
                if c_img is not None:
                    col_covers.append(c_img)

            poster_bytes = render_collection_poster(ctitle, col_covers)
            plex_req(
                f"/library/metadata/{crk}/posters",
                token,
                method="POST",
                data=poster_bytes,
                headers={"Content-Type": "image/jpeg"},
            )

    _save_state(state)

    if quiet and not newly_enriched and merged_splits == 0:
        return True, ""

    summary = (
        f"Plex Audiobooks verified: {total_verified}/{len(albums)} books badged & locked "
        f"({len(newly_enriched)} newly enriched, {merged_splits} split sub-albums merged)."
    )
    if newly_enriched:
        summary += "\nNewly Enriched: " + ", ".join(newly_enriched)
    return True, summary
    return True, summary


def main() -> None:
    parser = argparse.ArgumentParser(description="Plex Audiobook Enricher & Cover Badge Engine")
    parser.add_argument("--force", "-f", action="store_true", help="Re-enrich and re-badge all audiobooks")
    parser.add_argument("--quiet", "-q", action="store_true", help="Silent output when 0 new books enriched")
    args = parser.parse_args()

    ok, msg = run_plex_audiobook_enricher(force=args.force, quiet=args.quiet)
    if msg:
        print(msg)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
