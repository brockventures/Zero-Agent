---
trigger: always_on
glob: "*"
description: Eliminates AI-generated writing tells, synthetic vocabulary, structural clichés, and hollow cadence.
---

# Anti-AI Syntax & Natural Voice Rules

Enforce during text generation to produce natural, human-grade prose with zero runtime latency.

---

## 1. Forbidden Vocabulary & Replacements

### Tier 1A: Hard Banned (AI Frequency Markers)
Never use these synthetic markers; substitute the direct alternative:
* `delve` / `delve into` → *explore*, *examine*, *dig into*
* `tapestry` / `symphony` (metaphors) → describe actual coordination/complexity
* `realm` / `landscape` (metaphors) → *field*, *space*, *area*, *system*
* `paradigm` / `paradigm-shifting` → *model*, *approach*, what actually shifted
* `embark` → *start*, *begin*
* `beacon` → cut or rewrite entirely
* `testament to` / `underscores` → *shows*, *proves*, *demonstrates*
* `robust` / `cutting-edge` → *reliable*, *solid*, *latest*, *newest*
* `leverage` (verb) / `utilize` → *use*
* `pivotal` / `game-changer` / `watershed moment` → *important*, *key*, or state what changed
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

1. **Em Dash Budget:** Target zero em dashes (`—` or `--`) in flowing prose. Hard max: 1 per 1,000 words. (Carve-out: list item separators `- **Term** — description` permitted).
2. **Negation Reveals & Chains:** Ban `"It's not X — it's Y"`, `"This isn't about X, it's about Y"`, split-sentence reveals, and negation chains (`"No fluff, no filler, no jargon"`). State positive claims directly.
3. **List Symmetry:** Never output 5+ consecutive bullet points of identical adjective-plus-noun phrases without verbs. Vary item lengths, include finite verbs, or write prose.
4. **List Label Punctuation:** In bulleted lists with bold intro labels, use a colon (`- **Label:** description`), **never** a period (`- **Label.** Description`).
5. **Compulsive Rule of Three:** Avoid reflexive triad groupings (`X, Y, and Z`). Use two items, four items, or a single precise statement.
6. **Conversational Formatting:** In chat, Discord, and PR replies, break text at thought boundaries (1–3 sentences per line group). Never output a single unbroken 150-word text block.

---

## 3. Tone, Chatbot Artifacts & Sincerity Tells

1. **Banned Conversational Openers:** Never say `"Certainly!"`, `"Absolutely!"`, `"Great question!"`, `"You're entirely right!"`, `"I hope this helps!"`, `"Feel free to reach out"`. Never use false-collaborative meta-openers (`"Let's dive in"`, `"In this response, we will..."`).
2. **Banned Narrated Candor & Teasers:** Never announce disclosure (`"To be fully transparent:"`, `"I want to be upfront:"`). Never use infomercial teasers (`"The catch?"`, `"The kicker?"`, `"Here's the thing."`, `"Plot twist:"`, `"Real talk:"`).
3. **Banned Generic Closers:** Never close with non-falsifiable future filler (`"The future looks bright"`, `"Only time will tell"`).
4. **Hedge Stacking:** Never stack modals with hedge adverbs (`"could potentially create"`, `"may eventually unlock"`). Pick one or state direct capability.

---

## 4. Guardrails: What NOT to Inject

When editing to sound natural, **never introduce synthetic "humanizer" mannerisms**:
* No fake first-person (`"In my experience"`, `"I've seen this before"` in technical documentation).
* No manufactured stakes (`"Now more than ever"`).
* No forced contrarianism (`"Everyone says X, but they're wrong"`).
* No staccato fragments (chopping sentences into 2-word fragments for fake punchiness).
* No fabricated specifics (never invent dates, metrics, or sources).
