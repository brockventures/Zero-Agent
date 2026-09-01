---
description: Comprehensive rules to eliminate AI-generated writing tells, synthetic vocabulary, structural clichés, and hollow cadence.
globs: "*"
---

# Anti-AI Syntax & Natural Voice Rules

Distilled from forensic analysis of LLM writing markers (`avoid-ai-writing`, `humanizer`, and stylometric classifiers). Enforce during text generation to produce natural, human-grade prose with zero runtime latency.

---

## 1. Forbidden Vocabulary & Replacements

### Tier 1A: Hard Banned (AI Frequency Markers)
Never use these synthetic markers; substitute the direct alternative:
* `delve` / `delve into` → *explore*, *examine*, *dig into*
* `tapestry` / `symphony` (metaphors) → *describe the actual complexity/coordination*
* `realm` / `landscape` (metaphors) → *field*, *space*, *area*, *system*
* `paradigm` / `paradigm-shifting` → *model*, *approach*, *what actually shifted*
* `embark` → *start*, *begin*
* `beacon` → *cut / rewrite entirely*
* `testament to` / `underscores` → *shows*, *proves*, *demonstrates*
* `robust` / `cutting-edge` → *reliable*, *solid*, *latest*, *newest*
* `leverage` (verb) / `utilize` → *use*
* `pivotal` / `game-changer` / `watershed moment` → *important*, *key*, or describe what changed
* `meticulous` / `meticulously` → *careful*, *precise*, *detailed*
* `seamless` / `seamlessly` → *smooth*, *direct*, *without friction*
* `nestled` / `vibrant` / `thriving` / `bustling` → *located*, *active*, or cite specific numbers
* `holistic` / `actionable` / `learnings` → *complete*, *practical*, *lessons*, *takeaways*
* `thought leadership` / `best practices` → *expert methods*, *standard approach*
* `synergy` / `interplay` → *combined effect*, *interaction*, *connection*
* `load-bearing` (abstract metaphor) → *essential*, *critical*, or state what breaks

### Tier 1B: Inflated Formality & Wordiness
* `in order to` → *to*
* `due to the fact that` → *because*
* `serves as` / `boasts` / `features` (copula avoidance) → *is*, *has*, *includes*
* `commence` / `ascertain` / `endeavor` → *start*, *find out*, *try*

### Tier 2: Cluster Banned (Never use 2+ in one paragraph)
* `harness`, `navigate`, `foster`, `elevate`, `unleash`, `streamline`, `empower`, `bolster`, `spearhead`, `resonate`, `revolutionize`, `facilitate`, `underpin`, `nuanced`, `crucial`, `multifaceted`, `myriad`, `plethora`, `encompass`, `catalyze`, `reimagine`, `galvanize`, `augment`, `cultivate`, `poised to`, `nascent`, `quintessential`, `overarching`.

### Tier 3: Boilerplate Phrases
* Ban empty category fillers: `the integration of`, `the intersection of`, `community-driven`, `long-term sustainability`, `decentralized compute`, `tokenized incentive structures`. State the concrete mechanism or time horizon.

---

## 2. Structural & Cadence Constraints

1. **Em Dash Budget:**
   - Target: **zero** em dashes (`—` or `--`) in flowing prose. Hard max: 1 per 1,000 words.
   - *Carve-out:* List item separators (`- **Term** — description`) are permitted. Never use em dashes as mid-sentence drama splices.
2. **Negation Reveals & Countdowns:**
   - Ban `"It's not X — it's Y"`, `"This isn't about X, it's about Y"`, and split-sentence reveals (`"The headline isn't speed. The real story is Y."`). State the positive claim directly.
   - Ban negation chains (`"No fluff, no filler, no jargon."`).
3. **List Symmetry & Bare Noun Phrases:**
   - Never output 5+ consecutive bullet points of identical short adjective-plus-noun phrases without verbs (*"Stable efficiency / Reliable connectivity / Optimized performance"*).
   - Vary list item lengths, include finite verbs, or write as prose paragraphs.
4. **List Label Punctuation:**
   - In bulleted lists with bold intro labels, use a colon (`- **Label:** description`), **never** a period (`- **Label.** Description`).
5. **Compulsive Rule of Three:**
   - Avoid reflexive triad groupings (`X, Y, and Z`, or `colon into a triple`). Use two items, four items, or a single precise statement.
6. **Conversational Formatting (Anti Wall-of-Text):**
   - In chat, Discord, and PR replies, break text at thought boundaries (1–3 sentences per line group). Never output a single unbroken 150-word text block.

---

## 3. Tone, Chatbot Artifacts & Sincerity Tells

1. **Banned Conversational Openers & Sycophancy:**
   - Never say: `"Certainly!"`, `"Absolutely!"`, `"Great question!"`, `"You're entirely right!"`, `"I hope this helps!"`, `"Feel free to reach out"`.
   - Never use false-collaborative meta-openers: `"Let's dive in"`, `"Let's explore"`, `"Let's examine"`, `"In this response, we will..."`.
2. **Banned Narrated Candor & Infomercial Hooks:**
   - Never announce disclosure: `"To be fully transparent:"`, `"I want to be upfront:"`, `"Rather than bury this:"`. Just state the fact.
   - Never use dramatic infomercial teasers: `"The catch?"`, `"The kicker?"`, `"Here's the thing."`, `"Plot twist:"`, `"Real talk:"`.
3. **Banned Aphorisms & Speculative Openers:**
   - Never use slot-fill profundity (`"X is the language of Y"`, `"X is the currency of Z"`).
   - Never use speculative scenario openers (`"Imagine a world where..."`, `"Picture a future in which..."`).
4. **Banned Generic Closers:**
   - Never close with non-falsifiable future filler: `"The future looks bright"`, `"Only time will tell"`, `"poised to become the defining trend of the coming decade"`.
5. **Moral-Adjective Category Errors:**
   - Never apply moral/human adjectives to non-agentic technical nouns (`"an honest shape"`, `"described honestly"`). State the concrete property (*"realistic curve"*, *"noted"*).
6. **Hedge Stacking:**
   - Never stack modals with hedge adverbs (`"could potentially create"`, `"may eventually unlock"`). Pick one or state the direct capability.

---

## 4. Guardrails: What NOT to Inject (Anti-Overcorrection)

When writing or editing to sound natural, **never introduce synthetic "humanizer" mannerisms**:
* **No Fake First-Person:** Do not inject *"In my experience"* or *"I've seen this before"* into objective or third-person documentation.
* **No Manufactured Stakes:** Do not add *"Now more than ever"* or *"The stakes have never been higher"*.
* **No Forced Contrarianism:** Do not invent a fake adversary (*"Everyone says X, but they're wrong"*).
* **No Staccato Fragments:** Do not chop coherent sentences into jarring 2-word fragments for fake punchiness.
* **No Fabricated Specifics:** Do not invent fake dates, metrics, or sources to replace a vague claim. If the detail is unknown, state the known facts plainly.

**Guiding Rule:** *Subtract filler and sharpen claims; never invent stance, fake persona, or artificial drama.*
