---
name: plex-audiobook-enricher
description: Use this skill whenever enriching, badging, unmerging, or auditing Plex Audiobooks (Section 4) metadata, Goodreads star ratings, narrator credits, chronological series collections, or Kometa-style 1000x1000 square cover badges (`★ Rating` + `BOOK #`).
---

# Plex Audiobook Enricher & Cover Badge Engine (`plex-audiobook-enricher`)

## Overview
Provides end-to-end automated metadata enrichment, split-album consolidation, and Kometa-style cover badging for **Plex Audiobooks** (Section 4 on Host 1) via `/workspace/tools/plex_audiobook_enricher.py`.

## Core Pipeline Capabilities

1. **Permission & Scanner Hygiene (`chmod -R a+rX`):**
   - Automatically normalizes file permissions across `$NAS_DOCKER_ROOT/../../data/media/audiobooks` so `.m4b` and `.mp3` files unpacked with `0600` permissions by SABnzbd or Shelfmark are never skipped by the Plex Media Scanner.

2. **Split-Album Consolidation:**
   - Groups Plex album entries (`type=9`) by their physical disk folder (`<Author>/<Book>`) and merges multi-chapter split sub-albums into a single canonical album per book.

3. **Curated + Dynamic Multi-API Metadata Resolution:**
   - Uses a fast local catalog for core library series plus live fallback queries to **Apple Books / iTunes Audiobook Search API**, **OpenLibrary Search API**, and **Google Books API** for any new audiobooks added in the future.
   - Strips publisher praise banners (`ONE OF THE MOST ACCLAIMED...`, `NEW YORK TIMES BESTSELLER...`) and reviewer pull-quotes from fetched descriptions.

4. **Kometa-Style 1000x1000 Square Cover Badges:**
   - Fetches clean `1000x1000` square audiobook cover art (caching originals in `/workspace/data/audiobook_covers_clean/`) and renders:
     - **Top-Left:** Gold Goodreads star rating pill (`★ 4.62`).
     - **Top-Right:** Series book number pill (`BOOK 4`) for series titles.
   - Uploads badged posters to Plex and locks `titleSort` (`<Series> 04 - <Title>`), `summary`, `userRating`, `rating`, `studio` (`Narrated by <Narrator>`), `genre`, and `collection` fields.

## Usage

### Incremental Run (Default — Only Processes New or Unbadged Books)
```bash
python3 /workspace/tools/plex_audiobook_enricher.py
```

### Sidecar Execution (Scheduled Weekly Sundays @ 3:30 AM PT)
```bash
python3 /workspace/tools/sidecars.py plex_audiobook_enricher --quiet
```

### Full Forced Re-Badge Pass
```bash
python3 /workspace/tools/plex_audiobook_enricher.py --force
```
