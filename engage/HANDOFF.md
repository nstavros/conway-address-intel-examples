# HANDOFF — continue ENGAGE locally

Written 2026-08-19 by the cloud session that built v1. This project now lives
on Nick's desktop; the cloud session is done. Everything below is what a fresh
local session needs to continue without re-deriving anything.

## State of the work

- Branch: `claude/engage-social-agent-cygefq` (PR #1). All work is pushed.
- ENGAGE v1 is complete and verified: engine + brand configs + fail-closed
  gates + approval-hash publishing + funnel enforcement + digest. 37/37 tests
  pass. Full pipeline smoke-tested end-to-end (see README for the loop).
- `DESIGN.md` is the architecture contract. `README.md` is setup + usage.
- Priority brands: **empires** and **capstack** (active, voice-calibrated).
  **stowecap** is a deliberate stub — hard blocks live and tested, voice
  awaiting calibration posts, no channel connected.

## Calibration provenance (do not re-invent)

- `fixtures/empires_instagram_media.json` (14 posts) and
  `fixtures/capstack_instagram_media.json` (6 posts) are REAL captions pulled
  2026-08-19 from @empires.and.egos and @capstacknick via the Chatplace MCP
  connector (`automations_triggers_list_instagram_media`, bot IDs
  `01a017c7-672b-7208-9a0d-15d0d6dca62e` / `01a017de-3b30-70a7-af10-3d35683e7772`).
  The voice prompt files in `brands/*/prompts/` were distilled from them.
- Empires strategy inputs came from the artifact "Empires & Egos — Content
  Roadmap": https://claude.ai/code/artifact/3ce52cf6-e185-4891-a304-612f8f37353e
  (90-day plan, Evolution-of-X Shorts finding, lane tables, decision gates,
  monetization-safe controversy line). Its watch list and cadence are already
  in `brands/empires/config.yaml`.

## Hard constraints already enforced in code — keep them

1. Approval queue is the ONLY write path; approval binds to text SHA-256;
   v1 has NO live posting sink (dry-run only) and must stay that way until
   Nick explicitly asks for a sink.
2. Gates fail closed (regex + numeric/era provenance + LLM judge; judge
   missing or erroring = BLOCKED; no override flag).
3. Brand isolation is structural: engine package contains zero brand
   language (test-enforced), canaries checked at the single LLM chokepoint,
   one SQLite DB per brand.
4. No DMs, no follow/unfollow automation, per-platform rate limits in config.

## Immediate next steps (in order)

1. `cd engage && pip install pyyaml && python3 -m unittest tests.test_isolation
   tests.test_gates tests.test_scoring tests.test_funnel` — confirm green locally.
2. Wire a local LLM backend: any CLI that reads prompt on stdin, writes
   completion on stdout, passed as `--backend-cmd`. This unblocks
   `draft-reply` generation AND the LLM gate layer (until then every draft
   without a backend lands BLOCKED, by design).
3. Nick will connect a materials folder (Dropbox/Drive locally is fine now
   that the session is local) — ingest via `engage material`. That pool is
   what the provenance gates check figures against.
4. Build the remaining modules from DESIGN.md §5 build order: repurposing
   fan-out (step 4 in DESIGN), comment triage, measure/weekly report.
5. Stowecap stays parked until Nick supplies calibration posts and a channel.

## Open items Nick mentioned

- He can connect social accounts: Chatplace already covers both Instagrams;
  TikTok not connected (Higgsfield `tiktok_connect` exists); X/LinkedIn/YouTube
  have no connector — `manual` ingest covers them meanwhile.
- Weekly performance report + Capstack signup attribution are designed
  (DESIGN.md measure module) but not yet implemented.
