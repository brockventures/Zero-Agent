# Servarr (Sonarr & Radarr) PostgreSQL Migration Plan

**Author:** Ivy / Ryan Brock  
**Date:** 2026-08-28  
**Target Host:** Host 1 (`127.0.0.1` / `Host 1`)  
**Status:** In Progress (Phase 1: Radarr)

---

## 1. Problem Statement & Motivation
Sonarr (`sonarr.db`: ~328 MB) and Radarr (`radarr.db`: ~57 MB) currently run on SQLite databases on Host 1 (`.82`). SQLite uses file-level single-writer locking. When high-concurrency background services run simultaneously:
- **Kometa (Plex-Meta-Manager):** Heavy metadata sweeps and collection queries
- **Maintainerr:** Rapid episode/movie PUT sweeps to unmonitor content
- **Posterizarr & Seerr:** Polling and updating queues and art assets
- **Sonarr's RefreshSeries:** Internal background refreshes

Concurrent writes frequently fail with `database is locked` errors, triggering HTTP 500s, Seerr queue failures, and false-positive watchdog restarts.

**The Solution:** Migrate Sonarr and Radarr to PostgreSQL. PostgreSQL provides Multi-Version Concurrency Control (MVCC) with row-level locking, enabling true concurrent reads and writes with zero lock contention.

---

## 2. Infrastructure Architecture
- **Host:** Host 1 (`127.0.0.1`)
- **Docker Compose:** Integrated into `/docker/appdata/docker-compose.yml`
- **Network:** `trash-guides_default` (shared bridge network; containers communicate via DNS `postgres-arr:5432`)
- **Image:** `postgres:16-alpine` (strictly pinned to prevent breaking major-engine auto-upgrades)
- **Persistent Data:** `/docker/appdata/postgres-arr/data`
- **Backups:** `/docker/appdata/postgres-arr/backups`
- **Databases & Credentials:**
  - `radarr-main` and `radarr-log` (user: `radarr`)
  - `sonarr-main` and `sonarr-log` (user: `sonarr`)

---

## 3. Implementation Phases

### Phase 1: Database Provisioning & Radarr Migration
1. **Container Setup:** Add `postgres-arr` to `docker-compose.yml`, start container, and verify health.
2. **Database Provisioning:** Create users and databases (`radarr-main`, `radarr-log`).
3. **Cold Snapshot:** Stop Radarr, create timestamped copy of `radarr.db`, `radarr.db-wal`, and `config.xml`.
4. **Schema Bootstrap:** Add Postgres credentials to Radarr's `config.xml`. Start Radarr to let it execute built-in migrations against Postgres, then stop it.
5. **Data Transfer (`pgloader`):** Run `ghcr.io/roxedus/pgloader` with data-only mode and quoted identifiers to copy rows from `radarr.db` into `radarr-main`.
6. **Sequence Alignment:** Reset PostgreSQL sequence generators (`setval`) to match `max(id) + 1` for all auto-increment tables.
7. **Verification:** Start Radarr on Postgres. Verify library count, monitored status, root folders, indexers, download clients, and API responsiveness.

### Phase 2: Automated Backup & Version Management
1. **Automated `pg_dump` Script:** Install `/docker/appdata/postgres-arr/backup-postgres.sh`.
2. **Daily Schedule:** Run daily at 02:30 AM PT via DSM / sidecar scheduler. Retain 7 daily and 4 weekly compressed dumps.
3. **Major Version Governance:** Pin major version tag (`postgres:16-alpine`). Ivy manages major upgrades via orchestrated dump-and-restore with interactive user confirmation.

### Phase 3: Sonarr Migration
1. Repeat migration process for Sonarr (`sonarr-main`, `sonarr-log`, `sonarr.db` 328 MB).
2. Validate episode history, queue processing, and Seerr connectivity.

---

## 4. Rollback Plan
If any issue occurs during migration:
1. Stop the target container (`docker stop radarr`).
2. Restore original `config.xml` from backup (removes `<Postgres...>` tags).
3. Start the container (`docker start radarr`). Radarr will immediately resume on its untouched SQLite database. Rollback time: < 30 seconds. Zero data loss.
