# magicpin "Vera" AI Challenge — System Design & Strategy Document

**Purpose:** Reference blueprint for building a #1-ranked submission to magicpin's AI Challenge (Product/Tech/AI Analyst role, Gurugram, PPO 15 LPA).
**Audience:** You, while coding — and yourself in the verification interview, when you need to explain every design decision.

---

## 0. Non-negotiable facts about this challenge

- **No resume, no interview to get in.** Selection is 100% output-based — the bot's behavior is the entire filter.
- **Solo applications only.** Do not submit as a pair/team.
- **Post-selection verification.** Before the final offer, magicpin verifies you actually did the work yourself. Anything you can't explain line-by-line is a liability, no matter where it came from (this doc included — treat it as a blueprint, not a copy-paste source).
- **The local `judge_simulator.py` is a development anchor, not the exam.** It only scores the 30 canonical test pairs you can see. The real judge harness uses the same scoring code but injects fresh, unseen scenarios (new digest items, shifted performance numbers, new triggers, surprise customer contexts, hostile/auto-reply/intent-transition replays). A bot that overfits to the 30 visible pairs will score badly on the real run.
- **This is a live, ongoing challenge with real prior entrants.** At least half a dozen public submissions already exist for this exact dataset and rubric. "A hybrid LLM + deterministic bot with output validation" is the **baseline** other candidates are already doing — see §4 for what's already common, and §7 for what actually differentiates a #1 entry.

---

## 1. What magicpin is actually scoring (rubric reverse-engineered)

Every composed message is scored 0–10 on 5 dimensions (max 50):

| Dimension | What earns points | What loses points |
|---|---|---|
| **Decision quality** | Picking the *right* trigger for this moment — combining trigger urgency + merchant state + category fit *before* writing anything. This is the highest-leverage dimension; it's a selection problem, not a writing problem. | Sending a message just because a trigger exists; ignoring merchant signals; treating all triggers as equally worth acting on |
| **Specificity** | Real numbers, dates, %, prices, source citations that trace to the given context | Generic framings ("increase your sales", "10% off") |
| **Category fit** | Voice matches vertical — dentists: clinical/peer; salons: warm/practical; restaurants: operator-to-operator; gyms: coaching; pharmacies: precise/trustworthy | Promotional tone in a clinical category; wrong vocabulary register |
| **Merchant fit** | Uses this merchant's real name/owner/offers/history/language, not a template with blanks filled in | Fabricated data; ignoring language preference (`hi`/`en`/`hi-en mix`) |
| **Engagement compulsion** | One clear low-friction ask; a real reason to reply now (curiosity, loss aversion, social proof, reciprocity, "asking the merchant" a question) | Multiple CTAs; buried ask; no reason to act *now* |

**Penalties:** fabricating data not in context (−2), exposing internal jargon like `ctr`/`payload`/field names to the merchant (−1), identical repeated message (−2), malformed/empty response (−2), 3 consecutive `/v1/healthz` failures = disqualified for that slot.

**Documented gaps in production Vera** (these are explicitly named as opportunities in the brief — the judge will be primed to notice if you close them):
1. Auto-reply pollution wastes 2–3 turns before detection.
2. Intent-handoff failures — merchant says "let's do it," bot asks another qualifying question instead of acting.
3. Generic discount copy instead of service+price framing.
4. Low engagement frequency — only functional/reminder-style nudges, not curiosity- or knowledge-driven ones.
5. **Social proof** and **"asking the merchant a question"** levers are the two weakest compulsion families in production — explicitly called out as the biggest unlock.

---

## 2. The 4-context data model

```python
CategoryContext:
    slug                      # "dentists" | "salons" | "restaurants" | "gyms" | "pharmacies"
    display_name
    voice: { tone, register, code_mix, vocab_allowed[], vocab_taboo[], salutation_examples[], tone_examples[] }
    offer_catalog: [{ id, title, value, audience, type }]
    peer_stats: { scope, avg_rating, avg_reviews, avg_ctr, ... }
    digest: [{ id, kind, title, source, trial_n?, patient_segment?, summary }]
    patient_content_library: [{ id, title, channel, body }]
    seasonal_beats: [{ month_range, note }]
    trend_signals: [{ query, delta_yoy, segment_age }]

MerchantContext:
    merchant_id, category_slug
    identity: { name, owner_first_name, city, locality, place_id, verified, languages[], established_year }
    subscription: { status, plan, days_remaining, renewed_at }
    performance: { window_days, views, calls, directions, ctr, leads, delta_7d: {...} }
    offers: [{ id, title, status, started/ended }]
    conversation_history: [{ ts, from, body, engagement }]
    customer_aggregate: { total_unique_ytd, lapsed_180d_plus, retention_6mo_pct, high_risk_adult_count }
    signals: [ "stale_posts:22d", "ctr_below_peer_median", "dormant_with_vera", ... ]
    review_themes: [{ theme, sentiment, occurrences_30d, common_quote }]

TriggerContext:
    id, scope ("merchant"|"customer"), kind, source ("external"|"internal")
    payload: {...}             # WARNING: ~75% of generated triggers are placeholder-only — design for this (see §5.2)
    urgency (1-5), suppression_key, expires_at

CustomerContext (optional):
    customer_id, merchant_id
    identity: { name, phone_redacted, language_pref }
    relationship: { first_visit, last_visit, visits_total, services_received[] }
    state: "new"|"active"|"lapsed_soft"|"lapsed_hard"|"churned"
    preferences: { preferred_slots, channel }
    consent: { opted_in_at, scope[] }
```

⚠️ **Field names in the real dataset drift slightly from the brief's prose examples** (`owner_first_name`, `vocab_taboo` as a plural list, etc.). Always parse the actual JSON in `dataset/`, never hand-copy the brief's illustrative snippets.

---

## 3. Architecture

```
                        ┌───────────────────────────────────────────────┐
                        │           YOUR BOT — public HTTPS URL          │
  magicpin Judge   ───► │  FastAPI app                                   │
  (context push,        │   ├── POST /v1/context   idempotent store      │
   tick, reply,         │   ├── POST /v1/tick      decide what to send   │
   healthz, metadata) ◄─┤   ├── POST /v1/reply     multi-turn reply      │
                        │   ├── GET  /v1/healthz                         │
                        │   └── GET  /v1/metadata                        │
                        │                                                 │
                        │  ┌───────────────────────────────────────────┐ │
                        │  │ L1  Context Store (Redis / in-memory)      │ │
                        │  │ L2  Trigger Priority Engine                │ │
                        │  │ L3  Fact-Sheet Builder + Curator            │ │
                        │  │ L4  Composer (LLM prose + deterministic)   │ │
                        │  │ L5  Output Validator                       │ │
                        │  │ L6  Conversation State Machine             │ │
                        │  └───────────────────────────────────────────┘ │
                        └───────────────────────────────────────────────┘
```

### Tech stack

| Component | Choice | Why |
|---|---|---|
| Web framework | **FastAPI** | Matches the reference skeleton in the testing brief; async, handles 10 req/sec from the judge |
| Hosting | Render / Railway / Fly.io, or Vercel (serverless) | Needs a public HTTPS URL; free tiers are enough |
| State store | **Redis** (Upstash REST API if serverless) | Serverless functions have no instance affinity between requests — a plain Python dict silently loses context between ticks. A long-running container (Render/Railway) can get away with in-memory, but Redis is safer either way |
| Composer LLM | A frontier model at **temperature = 0** | Brief explicitly requires deterministic output for the same input; pick a model strong enough for nuanced category-voice writing (not a high-volume/cheap tier optimized for repetitive tasks) |
| Fallback | Pure deterministic Python templates, one per trigger `kind` | Bot must never crash or go silent if the LLM call fails, times out, or the key is missing |
| Validation | Token/regex matcher against the fact sheet | Every number/date/% in the output must trace to a supplied fact |
| Logging | JSONL per conversation + per context push | For your own debugging and for the README's evidence section |

---

## 4. What existing public submissions already do (the baseline you must beat)

Several public repos already implement this exact challenge. The common pattern among the stronger ones:

```
compose()
  → build_factsheet()        # pull only verified facts from the 4 contexts, two tiers:
                              #   hard facts (numbers/dates — cite directly)
                              #   attributed facts (review theme, digest item — cite with source phrase)
  → curate_hard_facts()       # narrow ~20 available facts to the 4-6 this trigger kind needs
  → LLM prose call            # LLM only WRITES; it does not decide what's true or what to send
  → validate_output()         # machine check: every number/date/name must trace to the fact sheet;
                               # jargon blocklist; phantom-offer check; length check
  → fail? retry once (lower temperature, stricter reminder)
  → fail again? → deterministic per-trigger-kind template (never calls out, always succeeds)
```

And in `/v1/tick`, trigger selection as an **explicit, separate scoring layer** (not left to the LLM):

```python
def priority_score(trigger, merchant, category):
    score = trigger.urgency * 10
    score += 15 if trigger.kind in HIGH_STAKES_KINDS else 0        # renewal_due, regulation_change, supply_alert...
    score += 20 if trigger.kind matches an active merchant.signal else 0   # dormant signal -> winback trigger
    score += 5  if trigger.kind fits the category (compliance triggers score higher for dentists/pharmacies)
    score -= 10 if trigger.payload is placeholder-only
    return score
```
— with a **floor**: if the best available score is still weak, return `actions: []` rather than force a message. And a rule that a merchant with an already-open, unanswered conversation only gets interrupted by a new trigger if it clears a much higher bar.

Multi-turn handling (`/v1/reply`) as a **priority-ordered checklist**, not a single LLM call:
1. Auto-reply detection (canned phrase or verbatim repeat) → escalate politely → wait → end
2. Hostility (checked *before* a soft opt-out) → apologize + end, don't argue
3. Explicit opt-out → end + suppress permanently
4. Intent transition ("let's do it") → switch straight to a binary confirm/cancel on the already-stated plan, zero further qualifying questions
5. Curveball/off-topic → polite decline + redirect to the one open ask
6. Generic yes/no → advance or end
7. Fallback → acknowledge + restate the ask, with anti-repetition (never send the same body twice in one conversation)

**This is table stakes now.** Building exactly this and stopping there gets you into the pack, not to #1.

---

## 5. Differentiation strategy — how to actually rank #1

### 5.1 Decision quality is under-invested by almost everyone
Most submissions treat trigger selection as a simple weighted sum. Go further:
- Track **per-merchant message fatigue** — not just "is there an open unanswered conversation" but a rolling count of sends in the last N hours, with diminishing willingness to fire again.
- Make the **rationale a first-class, structured object** (not just a sentence) that names: candidate triggers considered, their scores, why the winner won, which compulsion lever was chosen and why it fits *this* merchant's signals. The brief says the judge reads and scores rationale quality — a templated one-liner rationale is a missed opportunity.

### 5.2 Placeholder-trigger intelligence (documented, real gap)
~75% of generated trigger payloads are placeholder-only (`{"placeholder": true, "metric_or_topic": "..."}`). A generic fallback message here is a specificity-score leak multiplied across most of the dataset. Instead:
- When a trigger payload is thin, fall back to pulling a genuinely specific, verifiable fact from *whatever else is available* — a real `delta_7d` movement, an active offer title, a review theme, a customer_aggregate number — rather than a content-free "check out our offers!" line.
- This is the single highest-leverage engineering investment in the whole project, because it affects most of the dataset, not just the 25 fully-populated seed triggers.

### 5.3 Cross-conversation, per-merchant dedupe ledger
Most implementations only dedupe within a single conversation thread. Build a ledger keyed by `merchant_id` (not `conversation_id`) so a merchant never receives two near-identical messages across two different triggers in the same tick window — a real risk given how sparse many trigger payloads are.

### 5.4 Weaponize the two documented weak compulsion levers
The brief explicitly states that **social proof** ("3 dentists in your locality did X this month") and **asking the merchant a direct question** ("what's your most-asked treatment this week?") are production Vera's biggest engagement misses. Deliberately engineer these into your composer's lever selection — using `category.peer_stats` for social proof and a dedicated "curious-ask" trigger family for the second. This is a stated, verified gap the judge is primed to reward closing.

### 5.5 Design explicitly for adaptive context injection
The judge pushes new digest items, shifted performance numbers, new triggers, and surprise customer contexts *after* your submission, mid-test. Test this yourself before submitting:
- Push a new higher-`version` context for a merchant you've already messaged, then verify your next composed message reflects the new numbers and doesn't contradict what you said earlier.
- Verify your bot never re-derives or fabricates a "new" fact that wasn't in the injected payload.

### 5.6 README as an interview artifact, not a formality
Write it as if you're explaining the design to the verification interviewer: what you built, what you deliberately didn't do and why, what additional context would have helped most (the brief explicitly asks this). This is the same document that will anchor your live explanation later — write it to be defensible, not just complete.

### 5.7 Understand every line yourself
Whatever tool you use to help write the code (agentic IDE, LLM pair-programmer), you personally must be able to explain and modify every file. This isn't optional politeness — it's the literal pass/fail criterion of the post-selection verification step.

---

## 6. Endpoint contract (implement exactly this)

| Endpoint | Method | Behavior |
|---|---|---|
| `/v1/context` | POST | Idempotent by `(scope, context_id, version)`. Higher version replaces atomically; same/lower version is a no-op (`409` with `current_version`). Persist until test end. |
| `/v1/tick` | POST | Given `now` + `available_triggers`, return 0–20 `actions[]`. Must respond within 30s even with nothing to send (`{"actions": []}`). |
| `/v1/reply` | POST | Given the simulated merchant/customer's message, return `{"action": "send"\|"wait"\|"end", ...}` within 30s. |
| `/v1/healthz` | GET | `{"status": "ok", "uptime_seconds": ..., "contexts_loaded": {...}}`. 3 consecutive non-200s disqualifies the run. |
| `/v1/metadata` | GET | Team name, model, approach, version — shown on the leaderboard. |

**Hard operational rules:**
- If a composition would take longer than the 30s budget, return `{"actions": []}` immediately rather than trying to background-process — late responses are dropped, not queued.
- Only one action per `(merchant_id, conversation_id)` pair per tick; use a follow-up tick for more.
- Wipe all state on receiving an optional `POST /v1/teardown`.

---

## 7. Build order (10-day plan)

| Day | Task |
|---|---|
| 1 | Run `generate_dataset.py`; manually trace one merchant → one trigger → one composed message by hand before writing any code |
| 2 | FastAPI skeleton + Redis-backed storage layer + `/v1/healthz`, `/v1/metadata`, `/v1/context` |
| 3–4 | Fact-sheet builder + curator + full deterministic composer (all trigger kinds) — bot must work end-to-end with zero LLM calls at this point |
| 5 | Wire in the LLM prose layer + output validator + fallback path |
| 6 | `/v1/tick` priority-scoring engine + restraint floor + per-merchant fatigue tracking |
| 7 | `/v1/reply` state machine — auto-reply / hostile / intent-transition / curveball / dedupe |
| 8 | Run `judge_simulator.py` repeatedly; specifically stress-test placeholder-only triggers and adaptive re-injection |
| 9 | Deploy to a public URL; load-test latency against the 30s/10-req-per-sec budget; write the README |
| 10 | Final self-review: for every file, can you explain *why* each decision was made without looking at this document? |

---

## 8. Pre-submission checklist

- [ ] All 5 endpoints live and returning the exact schemas above
- [ ] `/v1/context` idempotent, tested with repeated and out-of-order versions
- [ ] `/v1/tick` and `/v1/reply` both return within 30s under worst-case LLM latency, always
- [ ] Bot survives a full 60-minute simulated window without state loss (test a cold-start / restart mid-window if deploying serverless)
- [ ] Zero fabricated facts across all 100 generated triggers, not just the 30 canonical pairs — spot-check by scanning outputs for numbers/names not present in the source context
- [ ] Auto-reply / hostile / intent-transition scenarios pass in `judge_simulator.py`
- [ ] `README.md` explains approach, tradeoffs, and what additional context would have helped — written as if for the verification interview
- [ ] You can explain every file yourself, unaided

---

*This document was assembled from the official challenge brief, testing brief, and public reference submissions for the same dataset. It is a strategy and architecture reference — not a substitute for writing and understanding your own code.*
