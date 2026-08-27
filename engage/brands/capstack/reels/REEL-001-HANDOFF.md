# HANDOFF — Capstack Reel 001, "Thirty Seconds in a Lobby"

For a **local** Claude Code session with access to Nick's files. Everything
needed to assemble and ship one reel is in this document; nothing else needs to
be read first.

Script is **locked** — Nick approved it, including his two edits ("I develop"
not "I buy"; "worn through" not "worn through there"). Do not rewrite it.
Shooting script reference: https://claude.ai/code/artifact/93156575-4271-4a72-aedb-58885fdd2baa

## What this is

Reel 001 of a series that launches the Capstack Nick account. Nick records
himself once at his desk; five hotel images or clips carry the middle. Structure
is **face → footage → face**: he's on camera for the open and the close, and his
voice runs over the hotel footage in between.

Why this format and not the deal-report format that was drafted first: the
account has ~19 followers. A weekly capital-markets brief is a reason to *keep*
following, not a reason to *start*. This is the top-of-funnel version — hotels as
something everyone has experienced, explained by someone who develops them.

## Inputs Nick supplies

| # | Asset | Used at |
|---|---|---|
| — | **His recording** — one continuous take of the full script, raw, untrimmed | audio bed throughout; picture at open + close |
| 1 | Luggage / bell cart, ideally showing wear | after the hook |
| 2 | Lobby ceiling or chandelier | tell 2 |
| 3 | Elevator bank, floor visible — **the hero shot** | tell 3 |
| 4 | Lobby, wide | tell 4 |
| 5 | Front desk / reception, no identifiable faces | tell 5 |

Stills are fine and expected. They don't need to be the same hotel, and they
don't need to be a property Nick owns.

## The locked script

**[ON CAMERA]**
> I develop hotels for a living. Give me thirty seconds in a lobby and I'll tell
> you if the owner's in trouble.

**[ASSET 1 — luggage carts]**
> The luggage carts. Scuffed, wobbling, parked where everyone can see them?
> Nobody's funding replacements.

**[ASSET 2 — lighting]**
> The lighting. A burnt-out bulb in a chandelier isn't laziness — it means
> nobody's on a relamping schedule. That's a budget decision.

**[ASSET 3 — elevator carpet]**
> The carpet at the elevator bank. Highest-traffic ten square feet in the
> building. Worn through means the renovation is overdue and they can't pay
> for it.

**[ASSET 4 — lobby wide]**
> The smell. A working scent system costs real money every month. If it's
> running, someone is still spending to hold brand standards.

**[ASSET 5 — front desk]**
> The front desk at three in the afternoon. Fully staffed means groups are
> coming. Empty means they're living on walk-ins — and that's thin.

**[ON CAMERA]**
> None of this is online. I'm working out whether the owner still has money —
> and you can see that in a lobby long before it ever shows up in the
> financials.
>
> Next hotel you check into, look down at the elevator carpet. Tell me what you
> find.

## On-screen text

Burned in, one per segment. Most viewers watch muted.

| Segment | Text |
|---|---|
| Hook | I develop hotels. Here's what I see. |
| Asset 1 | 1 · The luggage carts |
| Asset 2 | 2 · The lighting |
| Asset 3 | 3 · The elevator carpet |
| Asset 4 | 4 · The smell |
| Asset 5 | 5 · The front desk |
| Close | Look at the carpet. Tell me. |

## Assembly

**Cut to his voice, not to a stopwatch.** The timings below are the target
shape, not the spec. Scrub or transcribe the actual recording, find where each
line starts, and set the cut points there. A cut that lands mid-sentence is the
single most likely way this ships badly.

Target shape (~42s total):

```
0:00–0:04   ON CAMERA    hook
0:04–0:10   asset 1      luggage carts
0:10–0:16   asset 2      lighting
0:16–0:23   asset 3      elevator carpet   <- hold a beat longer, strongest tell
0:23–0:29   asset 4      lobby wide
0:29–0:35   asset 5      front desk
0:35–0:42   ON CAMERA    close + homework
```

Rules:

- **His recorded audio runs continuously underneath.** Never cut the audio; only
  the picture changes. He records the whole script including the middle even
  though that footage isn't used.
- **Stills get slow motion** — a gentle push in or drift (ffmpeg `zoompan`, or
  Ken Burns in any editor). Nothing static; a still frame reads as a dead video
  and kills retention.
- **Optional, effective:** small round face-cam inset in a bottom corner during
  the footage section, so viewers stay connected to him while looking at the
  hotel.
- **Output:** 1080 × 1920, H.264, `yuv420p`, `+faststart`, AAC audio.
- **Strip metadata** on the final file (`-map_metadata -1`). Camera and editor
  fingerprints travel in exports; a previous asset this session shipped with
  Google/YouTube transcode atoms baked in.
- **No watermark, no end card.** An end card is what got removed from the last
  video before it could be posted.

ffmpeg is the expected path (a static build was used earlier this session and
works fine). CapCut is an acceptable alternative if Nick prefers to drive it
himself — the spec above is editor-agnostic.

## Caption

Cleared the brand's regex, meta-text and provenance gates; classifies **TOP** —
the first top-of-funnel post on the account.

```
Five things in a hotel lobby that tell you how the owner is really doing. None of them are online.

The elevator carpet is the one I check first — highest traffic in the building, and the first thing that goes when there's no money for a renovation.

Next hotel you check into, look down at the elevator bank. Tell me what you find.
```

**No call to action. No "comment GAP."** The homework line *is* the call to
action, and it is the engagement engine for the whole post — it gives people
something they'll actually do and come back to report. Do not soften it to
"let me know your thoughts."

Re-run the gates if the caption changes at all:

```bash
cd engage && python3 -c "
from engage.config import load_brand
from engage.safety.gates import run_gates
from engage.funnel.classifier import classify
ctx = load_brand('brands','capstack')
t = open('<caption file>').read()
r = run_gates(t, ctx, source_texts=(t,), llm=lambda p:'PASS')
print('gates:', r.passed, '| funnel:', classify(t, ctx))
"
```

Note the `llm=lambda p:'PASS'` stub only exercises the deterministic layers. With
a real `--backend-cmd` wired the LLM judge runs too; without one the judge fails
closed by design.

## Three hooks to test

Nick records all three back to back while the camera is already running. Same
body, three openers, three reels from one sitting.

- **A · authority** — "I develop hotels for a living. Give me thirty seconds in a
  lobby and I'll tell you if the owner's in trouble." *Run this first.*
- **B · secret** — "There are five things in every hotel lobby that tell you the
  owner is running out of money."
- **C · confrontation** — "You've walked past this a hundred times and never
  noticed the hotel was in trouble."

Whichever holds attention longest becomes the template for the series.

## Constraints that carry across the account

- Approval before posting. Nothing publishes from the engine; posting is a human
  action.
- Capstack has `numeric_provenance: true` — any figure in a caption must appear
  in registered source material or the gate blocks it. This reel uses no figures,
  so nothing needed registering. Episodes 2 and 5 below will.
- Bottom-funnel asks stay at roughly one post in six. The account's existing six
  posts ran 83% middle-funnel against a 30% target, with zero top-of-funnel —
  that imbalance is what this series exists to correct.
- **Hold the Gemini avatar.** This format runs on "this guy actually does this,"
  and a synthetic presenter punctures that on an account with no track record.
  It earns its place later on data episodes where he narrates filings rather
  than vouching for himself.

## Next episodes, same production

Same setup, same three opening words, batched in one sitting.

02 · Hotels don't make their money on rooms. Here's where it actually comes from. *(needs sourced figures)*
03 · Every hotel you've stayed in was laid out to walk you past the bar.
04 · The most expensive thing I've ever found behind a wall. *(his beach-sand concrete story — already the best post on the account)*
05 · The brand on the building almost never owns the building. *(needs sourced figures)*
