# Empires & Egos — Instagram engagement plan

Verified 2026-08-19, the same night as the first 4 comments this session
(see git history). Every follower/following number was read off the live
profile that night. Every handle came from either an account we directly
engaged with, or E&E's own "Following" list, or an account co-tagged with
one of those — never guessed.

## The niche, and why it's narrow

@empires.and.egos is AI-generated/illustrated ancient-history content:
Rome, Greece, Persia, "why empires fall." The real peer group is NOT
generic history education (Kings & Generals, Fall of Civilizations, etc. —
those are the YouTube competitive-watch list already in config.yaml, kept
separate) but other **AI-history creator accounts on Instagram**, who
share an audience, an aesthetic, and often the same AI tools.

## The signal that sorts them: followers ÷ following

Same method as Capstack's list. An account following hundreds or thousands
of people back is reciprocal; an account following 0–20 is a pure
broadcaster.

| Handle | Followers | Following | Ratio | Read |
|---|---|---|---|---|
| historictalesthroughtime | 320 | 1,136 | 3.6:1 | most reciprocal in the niche |
| ki_zeitreise | 76.2K | 599 | 128:1 | still follows real people; cross-collaborates |
| historicalobserver | 48.4K | 459 | 105:1 | reciprocal-leaning broadcaster |
| historyaiillustration | 255K | 0 | pure broadcast | largest same-niche account |
| imperiumarchive_ | 87.1K | 0 | pure broadcast | Rome/Byzantium/Templar |
| aiwh_atever | 126K | 221 | 570:1 | broadcaster, some toxic threads |
| historicalfrontiers | 42.8K | 12 | 3,567:1 | pure broadcast |
| timemap.app | 4,680 | 1 | pure broadcast | reel-app account, engaged well |

## Start here

**@historictalesthroughtime** (320 followers, follows 1,136). The clearest
reciprocity signal in the niche, and their posts often sit at zero comments
for a day — first-comment advantage is real. Already engaged once (the
Alexander post) with a strong result.

**@ki_zeitreise** (76.2K, follows 599). German-language AI-history creator
("Zeitreisen ✖ Geschichte" — time travel × history) who was co-tagged with
timemap.app on a Roman-fire video, meaning AI-history creators already
cross-engage each other's audiences. Worth commenting even across the
language gap — visual history content travels; put the sharpest one-line
fact in English and let it stand on its own.

**@timemap.app** and **@imperiumarchive_** are the best pure comment
venues: both landed a top-of-thread comment tonight within minutes of
posting, on accounts with real reach (4.7K and 87K respectively).

## Two accounts need care, not avoidance

**@aiwh_atever** (126K) runs a genuinely strong AI-history-art account, but
some of its posts — the Persepolis one specifically — sit under an active
political argument in the comments (a top reply about "Islamic colonialism"
has 1,800+ likes). Engaging under that kind of post exposes E&E's name to
present-day political controversy, which brand config already blocks for
monetization safety. Check the top few comments before engaging any post
from this account; skip anything with an active political thread.

**@historicalobserver** (48.4K) is mostly clean archaeology/history content
but occasionally runs present-day-geopolitics posts (a Greece/Turkey
"future enemy" post was live on their grid tonight). Same rule: check the
post before commenting, skip anything political.

## Deliberately excluded

- **@acropolis.panorama.athens** — looks like a history account (bio:
  "Iconic views... your front-row seat to Ancient History") but it's
  actually an Airbnb rental listing for a rooftop in Athens; the bio ends
  "This view is one booking away" and links to airbnb.com. Its 2,127
  followers are travel bookers, not history fans — wrong audience for
  follower growth, even though a single on-topic comment there was
  harmless. Do not add to the watch list.
- **@georgiekirani** — Greek-Australian travel/lifestyle influencer.
  Modern travel content, not ancient history. Wrong niche entirely.
- **@raydalio** — finance. E&E follows this account already but it shares
  no audience overlap with ancient history.
- **@history** (HISTORY channel/network, verified) — real and on-topic,
  but a huge official media brand with essentially no chance of personal
  reply. Low value as an engagement target; fine to leave followed.

## What the search tools taught us

Instagram's hashtag pages (#ancientrome, etc.) and keyword search surface
**top** posts, not recent ones — everything found that way was 6+ months
old and useless for first-comment advantage. The only reliable way to find
fresh posts is going directly to an active account's profile grid, which
sorts newest-first. E&E's own "Following" list was the best source of
niche-verified accounts, since Instagram's generic "Suggested for you"
panel returned unrelated accounts.

## Operating rules

- Cooldown is 7 days per author (`scoring.cooldown_days`) — already touched
  tonight: acropolis.panorama.athens, historictalesthroughtime,
  timemap.app, imperiumarchive_.
- Rate limits in config: 20 IG replies/day, but pace to account age and
  size — @empires.and.egos is 19 followers and 2 weeks old; comment
  velocity is what triggers spam detection on new accounts, not raw volume.
- `blocks.claims_require_source: true` — every historical claim in a
  comment must trace to E&E's own published captions (registered as
  material `own-captions` in the store). Never assert a fact the gate
  can't verify against that source.
- No DMs, no follow/unfollow automation. Following is a manual action;
  the engine never does it.
- Re-verify quarterly, and re-check @aiwh_atever / @historicalobserver's
  recent posts for political content before every engagement, not just once.
