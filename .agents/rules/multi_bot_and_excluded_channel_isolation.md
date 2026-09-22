# Multi-Bot & Excluded Channel Isolation Invariant

## 1. Territory Exclusivity & Channel Boundaries
- **`#baseball` (`1548196929308065893`) is Exclusively Owned by Ivy:**
  - Ivy (`discord-ivy-agent`, Bot ID `1541205716948353074`) is the dedicated Assistant General Manager / Front-Office AI.
  - `#baseball` is dedicated strictly to Ivy's operations: sabermetric modeling, FanGraphs ingestion, Big Board Pro, dynasty valuations, and 1-on-1 collaboration with Ryan Brock.
  - Zero has zero jurisdiction in `#baseball`. Zero must never monitor, interject, summarize, or react in `#baseball` under normal operations.

## 2. Absolute Bot-to-Bot Prohibition on Brock Guild
- **No Bot-on-Bot Triggers:**
  - On the Brock Guild (`1210466877294518272`), Zero NEVER responds to any bot (including Ivy, Home Assistant webhooks, Dockhand alerts, or third-party bots).
  - If `msg.author.bot` is `True`, Zero drops the message unconditionally and immediately at the front door (`handle_message`).
  - Cross-agent collaboration occurs exclusively in Crab Cavern (`#the-banana-stand`, `#lounge` on Guild `1534436119888793747`) under the Banana Mutex and Last Word Protocol.

## 3. Excluded Channel Front-Door Quarantine
- **Strict Owner Text-Tag Invariant:**
  - In any excluded channel (`DEFAULT_EXCLUDED_HOME_CHANNELS` / `is_excluded_channel(msg.channel)`):
    1. Any message from a bot is dropped immediately.
    2. Any message from any user other than Ryan Brock (`OWNER_USER_ID: 179407724335988736`) is dropped immediately.
    3. Ryan MUST explicitly include Zero's tag (`<@1542285964213358633>`, `<@!1542285964213358633>`, or `@zero\b`) in raw message text.
- **Discord Inline Reply Quarantine:**
  - Discord native inline replies (`msg.reference`) automatically inject the referenced author into `msg.mentions`.
  - In excluded channels and public Brock channels, Zero MUST NEVER treat `bot.user in msg.mentions` as an explicit mention. The mention must physically exist in `msg.content`.
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
