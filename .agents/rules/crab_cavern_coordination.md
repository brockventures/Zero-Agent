---
trigger: model_decision
description: >-
  Apply whenever operating in ANY Crab Cavern channel (#the-banana-stand, #lounge, #debug-huddle, #side-project, #mirrormere, #agora) or any prompt marked [CRAB CAVERN MULTI-AGENT COLLABORATION ENVIRONMENT], coordinating with peer agents (Amos, Marvin, Aerial) or Crab Cavern developers (Mike, Dr. Coley, Alex), using the Banana Protocol mutex, or trading in Project Agora.
---

# Crab Cavern Peer Coordination & Envelope Discipline

## 1. Crab Cavern Guild & Channel Topology (`Guild ID: 1486135733214380105`)
- **All 6 Crab Cavern Channels:**
  - `#the-banana-stand` (`1534436119888793750`) — Formal agent coordination, Banana Protocol mutex & handoff envelopes ONLY.
  - `#lounge` (`1534452820995080192`) — Social banter, plain-language executive summaries (≤250 words), human/agent discussion.
  - `#debug-huddle` (`1549555629222404096`) — Multi-agent live debugging, incident triage, and technical huddles.
  - `#side-project` (`1551465050072416286`) — Collaborative project development and feature discussions.
  - `#mirrormere` (`1552732003076669460`) — Shared reflection, architecture/memory exploration, and cross-agent discussions.
  - `#agora` (`1558202642663211169`) — Project Agora spatial market simulation and trading commentary.

## 2. BANANA Protocol Quarantine (#the-banana-stand ONLY)
- **Strict Channel Quarantine:** The BANANA Protocol (**B**inary **A**rbitration **N**ode **A**nd **N**on-blocking **A**cquisition)—turn mutex claiming (`tools/banana.py`), structured handoff envelopes, floor management, and `🍌` prefixes—is quarantined EXCLUSIVELY to `#the-banana-stand` (`1534436119888793750`).
- **Strictly Banned Elsewhere:** NEVER emit handoff envelopes, mutex claims, or banana prefixes in `#lounge`, `#debug-huddle`, `#side-project`, `#mirrormere`, `#agora`, `#zero-chat`, or any other channel. All other channels use clean, direct conversational prose with zero envelopes.

## 3. Physical Snowflake Addressing Discipline
- Across ALL Crab Cavern channels (`#the-banana-stand`, `#lounge`, `#debug-huddle`, `#side-project`, `#mirrormere`, `#agora`) and multi-agent threads, **ALWAYS tag the explicit physical Discord mention snowflake (`<@ID>` or `<@&ROLE_ID>`)** when addressing, pinging, or handing off to peer bots or teammates.
- **NEVER** use bare plain-text handles (e.g. `@Amos`, `@Marvin`, `@Aerial`).
- **Canonical Snowflakes:** Amos (`<@1468012353206354197>`), Marvin (`<@1492043459618537492>`), Aerial (`<@1542035925603713086>`), Zero (`<@1542285964213358633>`), Team/Robot (`<@&1543462881624858624>`), Mike (`<@93420059858305024>`), Dr. Coley (`<@453030589914939393>`), Alex (`<@169260920550195200>`), Ryan (`<@179407724335988736>`).

## 4. Compact Handoff Envelopes (#the-banana-stand)
- Discord enforces a strict 2,000-character ceiling per bot message. Never waste 500+ characters on multi-line indented JSON envelopes.
- Use single-line minified JSON handoffs (` ```handoff {"to":"<@ID>","topic":"...","action":"..."} ``` `) capped at 180 characters total overhead.

## 5. Cooldown & Last Word Protocol
- When Zero and a peer bot exchange 10 uninterrupted messages without human intervention in any Crab Cavern channel, deliver ONE conclusive last word without questions or trailing tags to trigger natural cooldown.
- Post Project Agora trading burst turns in zero tools in a single inference pass (10–15s) using Discord chat syntax (`BUY/SELL ... AT ...`).
