---
trigger: model_decision
description: >-
  Apply whenever installing, upgrading, auditing, or troubleshooting Python packages, pip dependencies, Dockerfiles, container entrypoints, or persistent host-mounted virtualenvs (.venv) across Zero, Ivy, or Vulcan.
---

# Python Dependency & Persistent Virtualenv Hygiene Invariant

All Resident Agents (Zero, Ivy, and Vulcan) share a thin base Docker image (`discord-agy-agent-antigravity-agent:latest`) paired with persistent host-mounted Python virtualenvs (`/workspace/.venv/*`). Follow this pattern whenever adding or modifying Python dependencies or agent-hosted services.

## Hard Constraints

1. **Ban Ephemeral Ad-Hoc `pip install` Into Container System Site-Packages:**
   - **NEVER** run `pip3 install --break-system-packages <pkg>` inside a running container as a permanent install. Any container rebuild or recreate wipes the writable container layer and silently breaks dependent tools or daemons on the next restart.
   - If an urgent hotfix requires an immediate live install, you **MUST** also persist the package into the appropriate host-mounted `.venv` (or the shared `Dockerfile` if Tier B applies) in the same workflow.

2. **Two-Tier Durable Placement Decision Tree:**
   - **Tier A — Persistent Host-Mounted Virtualenv (Default for Role-Specific & Heavy Packages):**
     - Place heavy, optional, or agent-role-specific libraries into a persistent virtualenv under `/workspace/.venv/<name>` and expose them to Python at container boot via a `.pth` file in `/usr/local/lib/python3.11/dist-packages/`:
       - **Zero Extras** (`/workspace/.venv/extras` → `zero_extras.pth`): browser automation, plotting, local embeddings, and vector search (`playwright`, `matplotlib`, `fastembed`, `sqlite-vec`).
       - **Zero Modal / CAD** (`/workspace/.venv/modal` → `zero_cad.pth`): cloud compute and 3D mesh/CAD libraries (`modal`, `build123d`, `trimesh`, `manifold3d`).
       - **Ivy Baseball Analytics** (`/workspace/.venv/baseball` → `ivy_baseball.pth`): data-science and ML stack (`numpy`, `pandas`, `scipy`, `scikit-learn`, `matplotlib`, `fastembed`, `sqlite-vec`).
     - Install packages using the target venv's own pip binary (`/workspace/.venv/<name>/bin/pip install <pkg>`) so the container image stays lean and installs survive container recreates immediately without an image rebuild.
   - **Tier B — Shared Base `Dockerfile` (Thin Core & Built-In Agent Daemons Only):**
     - Reserve `$NAS_DOCKER_ROOT/discord-agy-agent/Dockerfile` strictly for lightweight shared bridge/CLI dependencies, OS audio/system libraries (`ffmpeg`, `libopus0`, `libsodium23`), and core runtime packages required by always-on services hosted inside the main agent container (e.g., `discord.py`, `google-genai`, `fastapi`, `uvicorn`).

3. **Keep Agent-Coupled Services Inside Their Agent Container:**
   - Do **NOT** split tightly coupled agent front ends or helper daemons into separate standalone virtualenv containers unless they require independent network boundaries, privilege isolation, or separate scaling lifecycles.
   - Isolate heavy *package libraries* in persistent host-mounted `.venv` directories; keep tightly coupled *services* inside their parent agent's container and start them via that container's `entrypoint.sh`.

4. **Shared-Entrypoint Role Isolation (`AGENT_ROLE` Guard):**
   - Because Vulcan mounts Zero's base image and `entrypoint.sh` with `AGENT_ROLE=vulcan`, any Zero-only background supervisor or daemon started in `entrypoint.sh` **MUST** be guarded with `[ "${AGENT_ROLE:-zero}" != "vulcan" ]`.

5. **Monthly Maintenance Registration:**
   - Whenever creating a new persistent virtualenv under `/workspace/.venv/`, register it in `VENV_TARGETS` inside `/workspace/tools/venv_update_check.py` (with representative `smoke_imports`) so automated monthly update checks and smoke tests cover it.
