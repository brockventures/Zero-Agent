"""Unit tests for Plex Audiobook Multi-Dimensional Collections & Custom Posters."""

from __future__ import annotations

import io
import sys
from pathlib import Path

_REPO_ROOT = str(Path(__file__).resolve().parent.parent)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from PIL import Image

from tools.plex_audiobook_enricher import (
    COLLECTION_BADGE_LABELS,
    compute_book_collections,
    render_collection_poster,
)


def test_series_suppression_and_standalone():
    """Verify 1-book series stubs are suppressed from series collections and categorized as Standalone Essentials."""
    series_counts = {"Children of Time": 1, "The Expanse": 9}
    narrator_counts = {"Mel Hudson": 1, "Jefferson Mays": 9}

    # 1-book series stub -> series suppressed, becomes Standalone Essentials
    meta_stub = {
        "series": "Children of Time",
        "rating": 4.29,
        "genres": ["Sci-Fi", "Space Exploration"],
        "narrator": "Mel Hudson",
    }
    cols_stub = compute_book_collections("Children of Time", meta_stub, series_counts, narrator_counts)
    assert "Children of Time" not in cols_stub
    assert "Standalone Essentials" in cols_stub

    # Multi-book series (>= 2) -> series collection kept, not standalone
    meta_multi = {
        "series": "The Expanse",
        "rating": 4.38,
        "genres": ["Sci-Fi", "Space Opera"],
        "narrator": "Jefferson Mays",
    }
    cols_multi = compute_book_collections("Leviathan Wakes", meta_multi, series_counts, narrator_counts)
    assert "The Expanse" in cols_multi
    assert "Standalone Essentials" not in cols_multi
    assert "Narrated by Jefferson Mays" in cols_multi


def test_acclaimed_essentials_threshold():
    """Verify 4.40+ threshold triggers '★ 4.4+ Acclaimed Essentials'."""
    series_counts = {}
    narrator_counts = {"Ray Porter": 5}

    meta_high = {
        "series": None,
        "rating": 4.51,
        "genres": ["Sci-Fi"],
        "narrator": "Ray Porter",
    }
    cols_high = compute_book_collections("Project Hail Mary", meta_high, series_counts, narrator_counts)
    assert "★ 4.4+ Acclaimed Essentials" in cols_high

    meta_low = {
        "series": None,
        "rating": 4.39,
        "genres": ["Sci-Fi"],
        "narrator": "Ray Porter",
    }
    cols_low = compute_book_collections("Artemis", meta_low, series_counts, narrator_counts)
    assert "★ 4.4+ Acclaimed Essentials" not in cols_low


def test_history_survival_and_memoirs():
    """Verify History, Survival & True Crime vs Memoirs, Comedy & Kitchens routing."""
    series_counts = {}
    narrator_counts = {}

    meta_hist = {
        "series": None,
        "rating": 4.62,
        "genres": ["History", "Survival"],
        "narrator": "Simon Prebble",
    }
    cols_hist = compute_book_collections("Endurance", meta_hist, series_counts, narrator_counts)
    assert "History, Survival & True Crime" in cols_hist
    assert "Standalone Essentials" not in cols_hist

    meta_memoir = {
        "series": None,
        "rating": 4.25,
        "genres": ["Memoir", "Food & Culinary"],
        "narrator": "Anthony Bourdain",
    }
    cols_memoir = compute_book_collections("Kitchen Confidential", meta_memoir, series_counts, narrator_counts)
    assert "Memoirs, Comedy & Kitchens" in cols_memoir
    assert "Standalone Essentials" not in cols_memoir

    meta_title_match = {
        "series": None,
        "rating": 4.30,
        "genres": ["Non-Fiction"],
        "narrator": "Norm Macdonald",
    }
    cols_title = compute_book_collections("Based on a True Story", meta_title_match, series_counts, narrator_counts)
    assert "Memoirs, Comedy & Kitchens" in cols_title


def test_full_cast_and_narrator_spotlight():
    """Verify Full Cast Productions and Narrator Spotlight assignments."""
    series_counts = {}
    narrator_counts = {"Ray Porter": 9, "Tim Gerard Reynolds": 6, "Single Narrator": 2}

    # Full Cast string in narrator
    meta_fc = {
        "series": None,
        "rating": 4.02,
        "genres": ["Horror"],
        "narrator": "Full Cast",
    }
    cols_fc = compute_book_collections("World War Z", meta_fc, series_counts, narrator_counts)
    assert "Full Cast Productions" in cols_fc

    # Multiple narrators with comma
    meta_comma = {
        "series": None,
        "rating": 4.28,
        "genres": ["Sci-Fi", "Short Stories"],
        "narrator": "Abby Craden, Todd McLaren, Carrington MacDuffie",
    }
    cols_comma = compute_book_collections("Stories of Your Life and Others", meta_comma, series_counts, narrator_counts)
    assert "Full Cast Productions" in cols_comma

    # Narrator with >= 3 books
    meta_porter = {
        "series": None,
        "rating": 4.51,
        "genres": ["Sci-Fi"],
        "narrator": "Ray Porter",
    }
    cols_porter = compute_book_collections("Project Hail Mary", meta_porter, series_counts, narrator_counts)
    assert "Narrated by Ray Porter" in cols_porter

    # Narrator with < 3 books
    meta_rare = {
        "series": None,
        "rating": 4.10,
        "genres": ["Fiction"],
        "narrator": "Single Narrator",
    }
    cols_rare = compute_book_collections("Some Book", meta_rare, series_counts, narrator_counts)
    assert "Narrated by Single Narrator" not in cols_rare


def test_render_collection_poster_dimensions_and_counts():
    """Verify render_collection_poster creates valid 1000x1000 JPEG output across various cover counts."""
    # 0 covers (fallback canvas)
    p0 = render_collection_poster("★ 4.4+ Acclaimed Essentials", [])
    assert isinstance(p0, bytes)
    img0 = Image.open(io.BytesIO(p0))
    assert img0.size == (1000, 1000)
    assert img0.format == "JPEG"

    # 1 cover
    c1 = [Image.new("RGB", (600, 600), (200, 50, 50))]
    p1 = render_collection_poster("Standalone Essentials", c1)
    img1 = Image.open(io.BytesIO(p1))
    assert img1.size == (1000, 1000)

    # 2 covers
    c2 = [
        Image.new("RGB", (600, 600), (200, 50, 50)),
        Image.new("RGB", (600, 600), (50, 200, 50)),
    ]
    p2 = render_collection_poster("Two Book Series", c2)
    img2 = Image.open(io.BytesIO(p2))
    assert img2.size == (1000, 1000)

    # 3 covers (hero layout - never repeats in 2x2 grid)
    c3 = [
        Image.new("RGB", (600, 600), (200, 50, 50)),
        Image.new("RGB", (600, 600), (50, 200, 50)),
        Image.new("RGB", (600, 600), (50, 50, 200)),
    ]
    p3 = render_collection_poster("Gentleman Bastard", c3)
    img3 = Image.open(io.BytesIO(p3))
    assert img3.size == (1000, 1000)

    # 4 covers (2x2 grid)
    c4 = [
        Image.new("RGB", (600, 600), (200, 50, 50)),
        Image.new("RGB", (600, 600), (50, 200, 50)),
        Image.new("RGB", (600, 600), (50, 50, 200)),
        Image.new("RGB", (600, 600), (200, 200, 50)),
    ]
    p4 = render_collection_poster("The Expanse", c4)
    img4 = Image.open(io.BytesIO(p4))
    assert img4.size == (1000, 1000)


def test_collection_badge_labels_completeness():
    """Verify defined collections in COLLECTION_BADGE_LABELS render badges correctly."""
    for col_name in [
        "★ 4.4+ Acclaimed Essentials",
        "Standalone Essentials",
        "History, Survival & True Crime",
        "Memoirs, Comedy & Kitchens",
        "Full Cast Productions",
        "Narrated by Ray Porter",
    ]:
        assert col_name in COLLECTION_BADGE_LABELS
        label, color = COLLECTION_BADGE_LABELS[col_name]
        assert isinstance(label, str) and len(label) > 0
        assert len(color) == 3
