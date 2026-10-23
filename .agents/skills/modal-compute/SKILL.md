---
name: modal-compute
description: >-
  Use this skill whenever running or orchestrating heavy machine learning model training, multi-fold cross-validation sweeps, hyperparameter optimization (Nelder-Mead, Powell, Optuna), large cohort backtests, or compute-intensive workloads. Offloads heavy CPU/RAM tasks from Host 2 NAS to Modal serverless cloud compute (8–16 vCPUs, 32–64 GB RAM), protecting the NAS hardware from memory exhaustion and watchdog panics.
---

# ☁️ Modal Serverless Cloud Compute Skill

The **Modal-Compute** skill governs offloading heavy computation from Host 2 (`<HOST_2_IP>`) to Modal serverless cloud containers.

---

## 🎯 When to Activate This Skill

Activate this skill whenever:
- Training, tuning, or fitting machine learning models in `big-projections` (`HistGradientBoosting`, `RandomForest`, `Ridge`, `CatBoost`, `LightGBM`).
- Running multi-fold cross-validation suites (`KFold(n_splits=5)`) across multiple statistical targets (`PA`, `HR`, `ISO`, `BABIP`, `K%`, `BB%`, `SB`, `ERA`, `WHIP`).
- Executing parameter optimization sweeps (>15 parameter combinations, Nelder-Mead, Powell, or grid search) across historical player cohorts (2021–2026).
- Generating large transition datasets or multi-year feature matrices that would consume $>1\text{ GB}$ of RAM.
- Any computational batch expected to take $>2\text{ minutes}$ or saturate $>2$ CPU cores.

---

## ⚡ Primary Entry Point: `/baseball/big-projections/modal_harness.py`

Do **NOT** write one-off 200-line Modal boilerplate scripts from scratch or run iterative LLM-in-the-loop scratch sweeps on Host 2. Use the unified **`modal_harness.py`** runner backed by local snapshot caching (`/tmp/ivy_modal_snapshot.pkl`) and the persistent **`ivy-baseball-cache`** Modal Volume:

### 1. Automatic Hybrid Local / Cloud Parameter Grid Search (`--mode grid`)
Invoke directly via `python3` so the harness automatically selects the fastest backend:
```bash
python3 /baseball/big-projections/modal_harness.py \
  --mode grid \
  --grid '{"sp_bb_scale": [0.95, 0.97, 0.99], "rp_bb_scale": [0.98, 1.00, 1.02], "sp_k_scale": [0.99, 1.00, 1.01]}'
```
- **Local Fast-Path (`<= 64` combinations, or `--local`):** Evaluates `evaluate_grid_chunk.local()` directly in-process against `/tmp/ivy_modal_snapshot.pkl` (clamped to `OMP_NUM_THREADS=2`), completing 9–27 combo sweeps in **1.2s–3.5s** with zero cloud cold-start or WAN upload overhead.
- **Cloud Parallel Fan-Out (`> 64` combinations, or `--cloud`):** Runs a **0.1s local pre-flight smoke test** on combo `#0` to catch `KeyError`/schema bugs immediately, ensures `/cache/snapshot_bundle.pkl` is synced to the Modal Volume, and fans out the remaining chunks across parallel Modal cloud containers (`8 vCPUs, 32 GB RAM` per worker) via `evaluate_grid_chunk.map()`.

### 2. Auto-Sync Local PostgreSQL & Cohort Cache (`--mode sync`)
```bash
python3 /baseball/big-projections/modal_harness.py --mode sync
```
- Computes an MD5 fingerprint of `player_milb_mles_v2`, `/workspace/scratch/full_regression_results.pkl`, and `/baseball/big-projections/data/cohort_cache/cache_*.pkl`.
- Tracks local digest (`digest`) and remote Modal Volume digest (`cloud_digest`) separately in `/tmp/ivy_modal_manifest.json` (`0.01s` skip when unchanged; add `--local` to rebuild local `/tmp/ivy_modal_snapshot.pkl` without waiting on WAN upload).

### 3. Multi-Target `KFold` Cross-Validation Sweep (`--mode cv`)
```bash
modal run /baseball/big-projections/modal_harness.py \
  --mode cv \
  --targets "iso,babip,kpct,bbpct,sb,era,whip"
```
- Fans out 5-fold `HistGradientBoostingRegressor` and `HuberRegressor` fits across all targets concurrently using `fit_cv_target.map()`.

### 4. Out-of-Sample Benchmark (`--mode benchmark`)
```bash
python3 /baseball/big-projections/modal_harness.py --mode benchmark
```
- Runs the 19-category out-of-sample freshman scorecard (`7/7` Hitters + `12/12` Pitchers) locally in `~0.4s` and prints the JSON summary.

---

## 🛑 Host 2 Hardware Protection Invariants

Host 2 is a 4-core / 8-thread NAS host with shared storage:

1. **Local Heavy Compute Ban:**
   - Training suites, large grid sweeps, and unbounded multi-threaded fits are strictly forbidden from running directly on Host 2 CPU.
   - `batch_guard.py` enforces a strict cap of **3 writes per scratch script** per turn—never use LLM edit-run-inspect loops as an optimizer.
2. **Local Worker & Thread Clamping:**
   - Lightweight single-pass local scripts on Host 2 (`<10s`) MUST enforce `OMP_NUM_THREADS=2`, `OPENBLAS_NUM_THREADS=2`, and `MKL_NUM_THREADS=2`.
3. **Unified Python Environment:**
   - Both `/usr/bin/python3` and `/usr/local/bin/modal` (`/opt/zero/workspace/.venv/modal/bin/python3`) carry `modal`, `psycopg2-binary`, `numpy`, `pandas`, `scipy`, and `scikit-learn`.
