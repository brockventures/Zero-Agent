# Physical Snowflake Addressing Discipline (The Snowflake Invariant)

## Core Invariant:
In Crab Cavern shared channels (`#the-banana-stand`, `#lounge`, `#side-project`) and multi-agent coordination threads, **ALWAYS** tag the explicit physical Discord mention snowflake (`<@ID>` or `<@&ROLE_ID>`) when addressing, pinging, or handing off to peer bots or teammates.

**NEVER** use bare plain-text handles (e.g. `@Amos`, `@Marvin`, `@Aerial`).

---

## Technical Rationale:
1. **Listeners Require Snowflakes:** Discord bots sleep by default and filter message streams for physical snowflake tokens. Bare text strings like `@Amos` or `@Zero` do not register as Discord mention events, leaving peer agents asleep in orbit.
2. **Zero Ambient Wakes:** Peer bots operating under strict ambient filtering (e.g. Amos, Zero) ignore unaddressed chatter. Physical snowflake mentions ensure guaranteed wakeups without burning roundtrips on un-triggered pings.

---

## Canonical Snowflake Registry:

| Entity | Role / Name | Discord Snowflake |
|---|---|---|
| **Amos** | Peer Engineering Bot | `<@1468012353206354197>` |
| **Marvin** | Peer Engineering Bot | `<@1492043459618537492>` |
| **Aerial** | Peer Engineering Bot | `<@1542035925603713086>` |
| **Zero** | Primary Engineering Bot | `<@1542285964213358633>` |
| **Banana Watcher** | Topic Synthesis Daemon | `<@1545924520236290198>` |
| **Robots / Team** | Shared Agent Role | `<@&1543462881624858624>` |
| **Mike** | Arbiter | `<@93420059858305024>` |
| **Dr. Coley** | The Moon Problem / Ian | `<@453030589914939393>` |
| **Alex** | Arcane | `<@169260920550195200>` |
| **Ryan** | Ryan Brock | `<@179407724335988736>` |

---

## Outgoing Bridge Enforcement:
- The egress formatting pipeline (`tools/bridge_formatting.py` and `tools/handoff.py`) auto-normalizes bare `@amos`, `@marvin`, and `@aerial` tokens outside code blocks into live Discord snowflakes as an automated safety net.
