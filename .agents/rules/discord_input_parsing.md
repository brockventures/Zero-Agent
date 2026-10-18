---
trigger: always_on
glob: "*"
description: Interprets terse prompts, Discord thread reply contexts, and proactive unprompted image parsing.
---

# Discord Input Parsing & Implicit Antecedent Invariant

## 1. Message Reply Priority (Explicit Thread Anchoring)
- Whenever an inbound Discord message is an inline reply to a previous message (`[DISCORD MESSAGE REPLY CONTEXT]` or `(replying to @Author)` headers), anchor to that specific referenced message as primary semantic context.
- When resolving terse directives (`"in English please"`, `"explain this"`, `"what about X?"`, `"check this"`, `"why"`, `"what happened"`), **ALWAYS** evaluate against the specific message being replied to, never against ambient chatter or old session topics.
- Trace reply threads along parent-child relationships rather than assuming a single linear stream.

## 2. Minimal Prompt Expansion (Lazy Typers)
- Users often ping with minimal input: e.g. role pings, `@Zero`, `^`, `^^`, `what?`, `this`, `investigate`, `check`, `troubleshoot`, `fix`, or an uncaptioned attachment.
- **NEVER** reject minimal prompts with boilerplate confusion or assume an expired topic.
- Read back the preceding 3–5 messages in Discord channel history, identify the active alert, question, link, or failure at hand, and address it directly with full forensic rigor.

## 3. Proactive Image Parsing Invariant (Unprompted Image Inspection)
- Whenever an image (screenshot, photo, error capture, architecture diagram, UI mockup) is attached or linked, parse and inspect it using `view_file` on the local attachment path (`/workspace/data/attachments/...`).
- Treat visual contents as primary input context, **EVEN IF the user prompt made no mention of the image** (e.g. prompt is blank, "look", or a role ping).
- Forensically extract all visible text, error messages, terminal traces, and UI elements before formulating a response.
