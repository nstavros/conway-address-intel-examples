# ENGAGE — Design Proposal

One engine, three brands (Stowecap, Empires and Egos, Capstack Nick). Narrow,
opinionated, config-driven. This document is the pre-implementation proposal
covering the three things requested up front: module structure, the scoring
function, and how brand isolation is enforced in code. Implementation starts
only after this is signed off and voice calibration material is supplied.

---

## 1. Module structure

Python package, CLI-first. The pipeline is `ingest → scoring → drafting →
safety → approval → publish → measure`, and each stage is a module with a
narrow interface so it can be tested alone against fixtures.

```
engage/
├── engage/                        # the engine (brand-agnostic — contains ZERO brand text)
│   ├── cli.py                     # every content command requires --brand
│   ├── config/
│   │   ├── schema.py              # pydantic models for brand YAML; strict, unknown keys rejected
│   │   └── loader.py              # loads brands/<name>/ into a BrandContext
│   ├── core/
│   │   ├── brand.py               # BrandContext — THE isolation boundary (see §3)
│   │   ├── models.py              # Post, Opportunity, Draft, QueueItem, SourceMaterial, Metric
│   │   └── store.py               # SQLite wrapper, one DB file per brand, bound at construction
│   ├── ingest/
│   │   ├── base.py                # IngestSource protocol: poll() -> list[Post]
│   │   ├── manual.py              # paste / file-drop (always works; the fallback)
│   │   ├── mcp.py                 # MCP-connector-backed source
│   │   └── platform/              # per-platform API adapters (optional, degrade gracefully)
│   ├── scoring/
│   │   └── scorer.py              # pure function over (Post, BrandContext, Store) — see §2
│   ├── drafting/
│   │   ├── replies.py             # 2 variants, distinct angles, "one concrete addition" check
│   │   ├── originals.py           # weekly queue from source material
│   │   ├── repurpose.py           # fan-out + reuse ledger (no recycling inside window)
│   │   └── llm.py                 # single LLM call chokepoint; assembles prompts from BrandContext only
│   ├── safety/
│   │   ├── gates.py               # regex gates + LLM gate; FAIL CLOSED (error/timeout/uncertain ⇒ BLOCKED)
│   │   ├── rules/                 # brand-agnostic gate *machinery*; the rules themselves live in brand YAML
│   │   └── contamination.py       # canary-token + phrase-signature cross-brand checks
│   ├── funnel/
│   │   └── classifier.py          # TOP/MIDDLE/BOTTOM classification + ratio enforcement (Capstack)
│   ├── approval/
│   │   └── queue.py               # the ONLY write path; approval binds to a content hash
│   ├── publish/
│   │   └── publisher.py           # dry-run by default; refuses anything not APPROVED; rate limits
│   ├── triage/
│   │   └── comments.py            # human-needed vs draftable; Empires theme mining
│   ├── measure/
│   │   └── collector.py           # performance pulls, Capstack signup/sale attribution
│   └── report/
│       ├── digest.py              # daily 2-minute digest, segmented by brand
│       └── weekly.py              # per-brand weekly report + one specific recommendation
├── brands/                        # ALL brand-specific text/config lives here, nowhere else
│   ├── stowecap/
│   │   ├── config.yaml            # accounts, keywords, voice rules, blocks, cadence, weights
│   │   ├── prompts/               # voice.md, reply.md, original.md … each carries a canary token
│   │   └── data/                  # engage.db + approval queue (gitignored)
│   ├── empires/                   # same layout
│   └── capstack/                  # same layout + funnel ratios + product catalog in config.yaml
├── fixtures/                      # captured posts/comments/scripts for offline drafting tests
└── tests/
    ├── test_isolation.py          # cross-contamination suite (see §3)
    ├── test_gates.py              # every hard block, incl. fail-closed on LLM error
    ├── test_scoring.py
    └── test_funnel.py
```

Key structural decisions:

- **Drafting runs offline against fixtures.** `drafting/` takes `Post` /
  `SourceMaterial` objects, never a network handle. Ingest is the only module
  that talks to the outside world for reads; publish is the only one for writes.
- **Ingest is swappable.** Everything downstream consumes the same `Post`
  model whether it came from an API, an MCP connector, or pasted text.
  `manual.py` is a first-class source, not a stopgap — APIs are assumed flaky.
- **The engine contains zero brand language.** No voice text, no example
  posts, no brand-specific rules in `engage/`. If a brand string appears in
  the engine package, that is a bug the contamination tests catch.
- **Funnel logic is a Capstack-only module**, switched on by config presence,
  not by `if brand == "capstack"` scattered through the code.

### Data model (local store, SQLite per brand)

| Table | What it tracks |
|---|---|
| `posts` | everything ingested, with platform, author, engagement snapshots |
| `opportunities` | scored posts + score components (so scoring is auditable) |
| `drafts` | every draft, its gate results, funnel class, content hash |
| `approvals` | who/when/what-hash was approved |
| `published` | what actually went out, where, when |
| `touches` | which accounts we engaged and when (drives cooldowns) |
| `material_uses` | source material → where each derivative was used (reuse ledger) |
| `metrics` | performance pulls per published post; Capstack signup attribution |

`metrics` feeds back into scoring (§2, the `history` term) — what performed
raises similar future opportunities.

---

## 2. The scoring function

Scoring is a pure, auditable function: every opportunity stores its component
scores, so "why did this rank #1" is always answerable from the digest.

```
score(post) = w_rel·relevance + w_auth·author_value + w_rec·recency
            + w_mom·momentum + w_hist·history
            × question_multiplier (Capstack only)
            → hard-zeroed by exclusions
```

All components normalized to [0, 1]. Weights come from brand YAML.

- **relevance** — keyword/hashtag/topic overlap between the post and the
  brand's configured audience terms, plus a topic match against the brand's
  "can speak to this" list. For Capstack, a post detected as a *question*
  (interrogative + topic in the teachable-topics list mapped to the product
  catalog) gets a `question_multiplier` (default 1.5) — answered questions
  are the top of the funnel, so they should dominate the ranking.
- **author_value** — tiered: accounts on the brand's watch list carry a
  configured tier (A = 1.0, B = 0.6, C = 0.3); unknown authors get a value
  from audience-fit signals (bio keyword match, follower band). Stowecap
  weights this heaviest because the goal is *who* engages, not how many.
- **recency** — exponential decay `exp(−age_hours / half_life)`, half-life
  per platform (X/TikTok short, LinkedIn long) set in brand YAML.
- **momentum** — is the post still climbing: engagement velocity between the
  last two ingest snapshots, normalized against the author's typical
  engagement (or platform baseline for unknowns), capped at 1.0. When only
  one snapshot exists (manual paste), momentum is neutral (0.5) rather than
  penalizing manual ingest.
- **history** — the feedback loop: bonus when the post's topic/author
  resembles past engagements that performed above the brand's median
  (from `metrics`), small penalty when they resemble past duds.

**Exclusions (hard zero, not weighted):** already engaged with this post;
author on blocklist; author touched within the cooldown window (default 7
days, per-brand) — prevents clustering all our engagement on one account.

**Default weights per brand** (in YAML, these are just starting points):

| Component | Stowecap | Empires | Capstack |
|---|---|---|---|
| relevance | 0.30 | 0.25 | 0.40 |
| author_value | 0.40 | 0.05 | 0.15 |
| recency | 0.10 | 0.25 | 0.15 |
| momentum | 0.10 | 0.35 | 0.15 |
| history | 0.10 | 0.10 | 0.15 |

Rationale: Stowecap optimizes for who's in the room (author-heavy), Empires
rides trends (momentum/recency-heavy), Capstack hunts answerable questions
(relevance-heavy with the question multiplier on top).

---

## 3. Brand isolation — enforced in code

Isolation is structural, not prompt-discipline. Four mechanisms:

**1. `BrandContext` is the only door.** Every module that touches content
takes a `BrandContext`, constructed exclusively by `config.loader` from one
`brands/<name>/` directory. It carries the config, the prompt files, and the
store handle. There is no global config, no shared "default voice", and no
API to hold two contexts in one pipeline run. The loader refuses (path-checks)
any prompt or data file that resolves outside the brand's directory.

**2. Physically separate state.** One SQLite file per brand, one approval
queue per brand, one prompts directory per brand. The store is bound to its
DB file at construction — there is no cross-brand query surface to misuse.
CLI: every content command requires `--brand`; there is no `--all-brands`
for drafting or approval.

**3. Canary tokens.** Each brand's prompt files embed a unique canary string
(e.g. `⟦SC-9f2a⟧` in every Stowecap prompt). The single LLM chokepoint
(`drafting/llm.py`) asserts, on every assembled prompt, that exactly one
brand's canaries are present and zero foreign ones. A foreign canary raises —
it doesn't warn — because it means prompt assembly crossed brands.

**4. Contamination test suite (CI-run).**
- *Canary test:* run every pipeline stage per brand against fixtures; assert
  no foreign canary ever reaches an LLM call or a draft.
- *Signature-phrase test:* each brand config lists signature phrases and
  banned-for-other-brands markers (e.g. Capstack's first-person teaching
  moves, Stowecap's numbers-forward constructions). Drafts generated for
  brand A are scanned for brand B/C signatures; matches fail the build.
- *Voice classifier check:* an LLM judge classifies fixture-generated drafts
  by brand blind; misclassification above a threshold fails.
- *Engine purity test:* grep-level check that `engage/` contains no brand
  names or brand phrases outside test fixtures.

### Safety gates (fail closed)

Every draft passes `safety/gates.py` before it may enter the approval queue:

1. **Regex layer** (deterministic, per-brand rules from YAML): Stowecap —
   return/IRR/multiple patterns, projection language, fundraising terms
   (506(b)/(c), "raising", "allocation"), LP/lender name list, unclosed-deal
   identifiers. Capstack — advice patterns ("you should buy/invest"),
   guarantee patterns, and **numeric provenance**: any number in a deal
   citation must literally appear in the linked source material, else block.
   Empires — every factual assertion must trace to the provided script/notes.
2. **LLM layer**: judge the draft against the brand's blocked-topics list
   with the specific failure modes named. Output is strict (`PASS`/`BLOCK` +
   reason).
3. **Fail closed**: LLM error, timeout, or anything other than an explicit
   `PASS` ⇒ draft status `BLOCKED`, visible in the digest with the reason.
   A blocked draft cannot be approved; there is no override flag in v1.

### Approval is the only write path

- Drafts land in the per-brand approval queue with status `PENDING`.
- Approval records the SHA-256 of the exact draft text. Publisher verifies
  hash-at-publish == hash-at-approval; any edit after approval invalidates it
  (back to `PENDING`).
- `publisher.py` is the only module with write credentials, refuses anything
  not `APPROVED`+gate-passed, enforces per-platform rate limits from config,
  and defaults to `--dry-run` (prints exactly what would post, where, when).
- No DM endpoints, no follow/unfollow calls, exist anywhere in the codebase.

---

## 4. Brand config schema (sketch)

```yaml
# brands/capstack/config.yaml
brand: capstack
platforms: [x, linkedin, instagram, tiktok]
watch:
  accounts: [{handle: "...", tier: A}, ...]
  keywords: [...]
  hashtags: [...]
voice:
  prompt_dir: prompts/          # must resolve inside this brand dir
  signature_phrases: [...]      # used by contamination tests
scoring:
  weights: {relevance: 0.40, author_value: 0.15, recency: 0.15, momentum: 0.15, history: 0.15}
  question_multiplier: 1.5
  cooldown_days: 7
  half_life_hours: {x: 6, linkedin: 48, instagram: 24, tiktok: 8}
blocks:
  regex: [...]                  # compiled at load; invalid pattern = config error
  topics: [...]                 # fed to LLM gate
funnel:                         # presence of this key switches funnel logic on
  ratio: {top: 0.6, middle: 0.3, bottom: 0.1}
  products: [{id: underwriting-course, topics: [...]}, ...]
cadence: {x: 7/week, linkedin: 3/week, ...}
rate_limits: {x: {replies_per_day: 10}, ...}
```

---

## 5. Build order

1. Core models, config loader, BrandContext, store, three brand config stubs.
2. Safety gates + contamination test suite (before any drafting exists —
   the guardrails are testable against hand-written fixture drafts).
3. Manual ingest + scoring + fixtures.
4. Drafting (replies → originals → repurposing), offline against fixtures.
5. Approval queue + dry-run publisher + funnel enforcement.
6. Digest, triage, measure, weekly report.
7. API/MCP ingest adapters last — everything works via manual paste first.

**Blocked on input:** 5–10 real posts per brand (the calibration set) to
write the voice prompt files and signature-phrase lists against.
