# Hearth Git-First Sync & Anti-Hot-Patch Invariant

## Hard Constraints

1. **Git-First Source of Truth (`brockventures/hearth-private`):**
   - **NEVER** hot-patch live Hearth runtime files directly on Host 2 (`$HOST_2_DOCKER_ROOT/hearth/deploy/`, including `ical-bridge/server.py`, `config/`, `widgets/`, `custom.css`, or `compose.hearth.yaml`) as a standalone edit.
   - All Hearth code, bridge, widget, and non-secret config changes **MUST** be edited in a tracked `brockventures/hearth-private` git checkout/worktree, verified with scoped tests, committed, and pushed to `brockventures/hearth-private` before deploying to Host 2.

2. **No `.bak` Hot-Patch Drift:**
   - **NEVER** rely on ad-hoc `.bak` file copies on Host 2 instead of git history. Back-to-back live patches without git reconciliation silently overwrite earlier changes.
   - If an emergency live hotfix is ever needed to restore a downed service, commit and reconcile that exact diff into `brockventures/hearth-private` in the same session so future deployments never clobber it.

3. **Deploy Parity Verification:**
   - Because Host 2 Hearth containers bind-mount local deploy directories directly over image paths, always deploy from the committed `brockventures/hearth-private` state, restart only the affected service using its explicit `com.docker.compose.project.config_files` `-f` path, and verify the live endpoint after restart.
