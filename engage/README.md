# ENGAGE

Social engagement and growth engine for three brands. One engine, three
configs, hard per-brand isolation. CLI-first; nothing ever posts without
explicit approval, and v1 ships with **no live posting sink at all** —
publishing is dry-run by design until a platform sink is deliberately added.

See [DESIGN.md](DESIGN.md) for the architecture rationale.

## Setup

```bash
cd engage
pip install pyyaml          # only dependency
python3 -m unittest tests.test_isolation tests.test_gates tests.test_scoring tests.test_funnel
```

Run everything from this directory (or `pip install -e .` for the `engage`
entry point). Brand configs are discovered under `./brands` (override with
`--brands-dir` or `ENGAGE_BRANDS_DIR`).

## The daily loop

```bash
# 1. Ingest — from a Chatplace MCP export of our own posts, or a manual JSON
#    file of third-party posts worth considering
engage ingest --brand capstack --source manual   --file listening.json
engage ingest --brand capstack --source chatplace --file our_media.json

# 2. Score and rank opportunities (auditable components stored per post)
engage score --brand capstack
engage opportunities --brand capstack --limit 10

# 3. Register source material (deal updates, scripts, notes) — figures in
#    drafts MUST come from here or the gates block them
engage material --brand capstack --file deal_update.txt --id cove1 --kind deal_update

# 4. Draft. Either generate replies (needs an LLM backend command)...
engage draft-reply --brand capstack --post-id x-101 --backend-cmd "your-llm-cli"
#    ...or submit externally written text through the gates:
engage submit --brand capstack --file draft.txt --platform instagram \
    --material-id cove1 --backend-cmd "your-llm-cli"

# 5. Approve — the ONLY write path. Approval binds to the text's SHA-256;
#    any edit afterwards revokes it.
engage queue list --brand capstack
engage queue show <draft-id> --brand capstack
engage queue approve <draft-id> --brand capstack

# 6. Publish (dry-run default — prints exactly what would post, where)
engage publish --brand capstack

# 7. Read the two-minute digest across all brands
engage digest
```

`--backend-cmd` is any shell command that reads a prompt on stdin and writes a
completion on stdout. Without one, the LLM safety gate **fails closed**: the
draft lands as BLOCKED, never PENDING.

## Brand layout

```
brands/<name>/
  config.yaml     # platforms, watch lists, scoring weights, blocks, funnel, cadence
  prompts/*.md    # voice/reply/original templates — each carries [[CANARY:<name>]]
  data/           # per-brand SQLite + queue (gitignored)
```

Current brands: `empires` and `capstack` (active, calibrated on real posts
pulled 2026-08-19), `stowecap` (stub, hard blocks live, awaiting calibration).

## Guarantees enforced in code (and tested)

- **Approval-only write path.** `publish` refuses anything not APPROVED; the
  approval hash must match the current text; dry-run is the default and the
  only mode until a sink exists. No DM/follow/unfollow code exists anywhere.
- **Fail-closed gates.** Regex layer (per-brand blocked language), provenance
  layer (numbers/era-years must literally appear in linked source material),
  LLM judge layer (anything but explicit PASS blocks — including judge
  errors or absence). Blocked drafts cannot be approved; there is no override.
- **Hard brand isolation.** One BrandContext per pipeline run, one SQLite DB
  per brand, canary tokens verified on every prompt assembly, and a
  contamination test suite: engine purity (no brand language in the engine
  package), cross-signature scans, foreign-canary rejection, path-escape
  refusal in the config loader.
- **Funnel enforcement** (brands with a `funnel:` config): every draft
  classified TOP/MIDDLE/BOTTOM, 60/30/10 default ratio, and a week that is
  all bottom-funnel is refused outright.
- **Rate limits** per platform from config, enforced at publish time.

## Ingest sources

Ingest is a protocol (`engage/ingest/base.py`); drafting never touches the
network. Included sources:

- `manual` — JSON list of posts (`author`, `text`, optional `id/platform/url/
  created_at/engagement/own`). Always works; APIs are assumed flaky.
- `chatplace` — parses exports of `automations_triggers_list_instagram_media`
  from the Chatplace MCP connector (our own posts: triage/measure/repurposing,
  never opportunities).

Add a platform API or MCP adapter by implementing `poll() -> list[Post]` —
nothing downstream changes.

## Fixtures

`fixtures/` contains real captions pulled from the connected Instagram
accounts on 2026-08-19 (voice calibration + offline drafting tests) and a
small synthetic listening sample for the demo above.
