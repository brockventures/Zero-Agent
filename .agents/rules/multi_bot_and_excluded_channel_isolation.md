# Multi-Bot & Excluded Channel Isolation Invariant

## 1. Territory Exclusivity & Channel Boundaries
- **`#baseball` (`1548196929308065893`) is Exclusively Owned by Ivy:**
  - Ivy (`discord-ivy-agent`, Bot ID `1541205716948353074`) is the dedicated Assistant General Manager / Front-Office AI.
  - `#baseball` is dedicated strictly to Ivy's operations: sabermetric modeling, FanGraphs ingestion, Big Board Pro, dynasty valuations, and 1-on-1 collaboration with Ryan Brock.
  - Zero has zero jurisdiction in `#baseball`. Zero must never monitor, interject, summarize, or react in `#baseball` under normal operations.

## 2. Bot-to-Bot Addressing Discipline on Brock Guild
- **Brock Public Channels (`#server-updates`, `#seerr-*`):**
  - Zero NEVER responds to any bot on Brock Guild public channels.
  - If `msg.author.bot` is `True` outside excluded channels, Zero drops the message unconditionally and immediately at the front door (`handle_message`).
- **Dedicated Excluded Channel Quarantine (`#baseball`):**
  - Ivy (`1541205716948353074`) is permitted to communicate directly with Zero strictly via:
    1. Direct snowflake mentions: `<@1542285964213358633>` or `<@!1542285964213358633>` (or `@Zero`).
    2. Native Discord inline replies (`msg.reference` resolving to a message authored by Zero).
  - Passive chatter, vocatives ("Zero."), and unaddressed chatter from Ivy are dropped at the front door.
  - Cross-bot interaction is strictly governed by the **Last Word Protocol** (4-message threshold between Zero and Ivy without human intervention, triggering a concluding turn and a 3-minute cooldown) and a **4-second cascade cooldown**.
  - Any human message from Ryan Brock in `#baseball` immediately unpauses Ivy and resets the streak.

## 3. Excluded Channel Front-Door Quarantine
- **Strict Addressing Invariant:**
  - In any excluded channel (`DEFAULT_EXCLUDED_HOME_CHANNELS` / `is_excluded_channel(msg.channel)`):
    1. Any message from any bot OTHER than Ivy is dropped immediately.
    2. Any message from any human OTHER than Ryan Brock (`OWNER_USER_ID: 179407724335988736`) is dropped immediately.
    3. Zero responds ONLY if explicitly tagged (`<@1542285964213358633>`, `<@!1542285964213358633>`, or `@zero\b`) OR if the message is a direct inline reply to Zero (`is_reply_to_zero`).
- **Lexical / Vocative Immunity:**
  - Excluded channels are completely immune to vocative parsing (`zero:`), sentence-starter matches (`Zero <verb>`), and compound terms (`zero-leakage`, `zero-shot`, `zero-sum`).

## 4. Zero Diagnostic Beacons in Excluded Channels
- Zero MUST NEVER emit diagnostic error beacons (`⚠️ **Turn Failed**`, `⚠️ **Turn Incomplete**`, `⚠️ *Front-office analysis...*`) into excluded channels like `#baseball`.
- If an explicitly invoked turn fails, times out, or produces silence in an excluded channel, `TurnCoordinator` and `deliver_turn_output` MUST suppress the output to `[NO_REPLY]`. Zero never litters another bot's room.

## 5. Blast Radius & Channel Scope Isolation
- Modifications to excluded channel handling must never alter:
  - **Home Turf Operations:** 1-on-1 ops pairing with Ryan across `#zero-chat`, `#zero-ops`, `#shopping`, `#homelab`, `#home-assistant`, `#steam-deck`, `#finances`, `#projects`, `#brock-house`, `#vault`.
  - **Crab Cavern Protocol:** Multi-agent collaboration in `#the-banana-stand` and `#lounge` (Banana mutex, 4-step Last Word Protocol, Tier-2 classifier).
  - **Public Brock Channels:** Explicit owner-only pings in `#server-updates` and `#seerr-*`.
