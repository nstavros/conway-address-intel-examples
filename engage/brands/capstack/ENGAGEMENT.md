# Capstack — Instagram engagement plan

Verified 2026-08-19. Every follower/following number was read off the live
profile that day. Every handle came from a source that ties it to the person
or brand — none were guessed. Ten look-alike handles turned out to be other
people entirely; they are blocklisted in `config.yaml` so they cannot slip in
later.

## The constraint that shapes everything

@capstacknick has **25 followers and 6 posts**. At that size the growth
mechanism is not peer follow-back. It is:

1. Comment early on a post whose audience already contains aspiring
   developers, while the comment section is short enough to be read.
2. Teach one concrete thing in the comment — a mechanism, a structure, a
   number from your own material.
3. Earn the profile click. The follow comes from *other readers* of that
   comment, far more often than from the account owner.

So the list has two roles, and conflating them is the mistake to avoid:

| Role | What it is | Action |
|---|---|---|
| **Reciprocal peer** (tier A) | Follows people back, replies in comments | Engage **and** follow |
| **Comment venue** (tier B) | Right audience, but a broadcaster | Comment only — do **not** follow |
| **Trade media** (tier C) | Small or brand-run, topical | Occasional, when the post is on-topic |

Your rule "no mass-following large accounts that won't follow back" governs
the *follow* action. It does not restrict commenting — and commenting on a
large, engaged account is the highest-yield thing a 25-follower account can do.

## The signal that sorts them: followers ÷ following

An account following 7,000+ people behaves reciprocally. An account with 26K
followers following 460 is broadcasting. That ratio, not raw size, predicts
whether engagement comes back.

| Handle | Followers | Following | Ratio | Read |
|---|---|---|---|---|
| localrealestatedevelopers | 1,080 | 1,064 | 1.0:1 | perfectly reciprocal |
| bllaboutiquelodging | 3,259 | 2,621 | 1.2:1 | very reciprocal |
| bethazor | 10.3K | 7,571 | 1.4:1 | very reciprocal, posts daily |
| bohonews | 1,373 | 848 | 1.6:1 | reciprocal |
| boutiquehotelgirl | 4,142 | 2,080 | 2:1 | reciprocal |
| **evanholladay** | **17.9K** | **8,002** | **2.2:1** | **best size × reciprocity on the list** |
| hotelbusinessmag | 1,374 | 615 | 2.2:1 | reciprocal, verified |
| marcusndaniels | 23.1K | 7,674 | 3:1 | reciprocal at real scale |
| bestevercre | 12.2K | 3,020 | 4:1 | reciprocal, LP-investor audience |
| sujmehta | 13K | 1,595 | 8:1 | strong hotel-side target |
| kenmcelroyofficial | 91K | 6,817 | 13:1 | big venue that still follows people |
| somerscapital | 8,369 | 484 | 17:1 | moderate |
| thencrea | 8,215 | 452 | 18:1 | DM funnel, not personal replies |
| commercial_in_nashville | 26.4K | 1,176 | 22:1 | venue, CRE-native |
| blakejdailey | 26.6K | 460 | 58:1 | pure broadcaster — comment only |

## Start here — in this order

**1. @evanholladay** (17.9K / 8,002). Attainable-housing developer who posts
capital-stack explainers and geotech studies, and runs a development
mentorship. He is the rare account that is both the right subject matter and
genuinely reciprocal at real scale. Your Boston co-GP structure and the
rolled-fee-as-equity mechanism answer his audience's exact question.

**2. @localrealestatedevelopers** (1,080 / 1,064). Bio: *"Develop in your own
backyard. Real projects | No gatekeeping."* Capstack's own voice guide says
"No gatekeeping, no guru energy" — this is the same thesis with a different
face, and a 1:1 follow ratio. Small reach, highest conversion odds. They run
a Local Developer Meet Up (Aug 27–29, Louisville) worth knowing about.

**3. @bethazor** (10.3K / 7,571). Retail leasing educator, posts multiple
times a day, follows 7,571 people. Volume plus reciprocity means many at-bats.

**4. @sujmehta** (13K / 1,595). Hotel investor posting real renovation and
deal content. Your Witkoff hotel-reno material — the beach-sand concrete, the
10–15% contingency rule — is lived experience his audience cannot get
elsewhere. Then **@bllaboutiquelodging** and **@boutiquehotelgirl**, the
boutique hotel association and its COO, both near 1:1 ratios.

**Best comment venues** (do not follow, just comment early): **@stripmallguy**
(45.1K — his last two acquisitions reportedly came from people DMing the
account, so that comment section is read by dealmakers), **@avivarealestate**
(43.8K, verified, top-ranked CRE influencer), and **@fundedbyvic** (12.9K, RE
debt and capital markets — the closest account to your actual subject matter).

## What isn't there — three findings worth knowing

**1. Hotel brokerage and capital markets barely exist on Instagram.** CBRE
Hotels, JLL Hotels, Berkadia and HREC have no meaningful hospitality IG
presence. That audience is on LinkedIn. If reaching brokers and capital
markets matters, Instagram is the wrong channel — your LinkedIn cadence
(3/week in config) is where that belongs.

**2. Much of institutional CRE is absent from Instagram too.** @adventuresincre
has 53 followers and follows 2. @moseskagan is private. @cre.daily is a private
0/0 placeholder. Break Into CRE has no active IG. These are top-tier CRE
education brands that live on YouTube, X and LinkedIn. The IG real-estate
world is its own smaller ecosystem, weighted toward multifamily, retail and
hospitality education rather than institutional CRE.

**3. Follower count and CRE influence diverge sharply here.** Several of the
most respected voices have tiny Instagram audiences, while some of the largest
accounts have thin CRE substance. Do not rank by follower count.

## The trap list — why 10 handles are blocklisted

Look-alike handles are the real hazard in this niche. Each of these is cited
somewhere as belonging to a person it does not belong to:

| Trap handle | What it actually is |
|---|---|
| `tylercauble` | 17 followers — not the CRE figure. Use `commercial_in_nashville` |
| `coachcaubs` | a different Tyler Cauble — a lacrosse coach |
| `thecauble` | does not resolve at all |
| `melansonrealty` | Mark Melanson, a Calgary realtor — not developer Shane Melanson |
| `hunterlthompson` | a car sales rep — not the capital-raising educator |
| `developlex` | private, 0 followers, unrelated. Use `developlexpod` |
| `multifamilyu` | 2 followers, unrelated person |
| `theboutiquehotelier` | traveler audience — not the trade mag `boutiquehoteliermag` |
| `tiffany.ryland` | a 1,959-follower secondary. Use `tiffannryland` |
| `blake_hagzcre` | dead handle in old show notes. Use `blake_haggett` |

These are in `blocked_accounts`, so the scorer hard-zeroes them on sight.

Handles that do **not** exist as spelled (verified "Profile isn't available"):
`thefortpodcast`, `jakeandgino`, `breakintocre`, `rodkhleif`, `fortcapitallp`,
`chrispowersjr`. The brands are real; those handles are not. **Never add a
handle unless a source ties it to the account** — this failure mode produced
six wrong guesses in one afternoon.

## Deliberately excluded

- **Interior design firms** (@hospitalitydesign 132K, @avroko, @meyerdavis,
  @studiomunge, @watgdesigns, @stonehilltaylor, @wimberlyinteriors). Large and
  active, but the audience is designers and architects. Capital-stack teaching
  does not land there.
- **Over the 150K ceiling**: @sweatystartup (210K), @gabe_einhorn (249K),
  @molzerdevelopment (243K), @rich_somers (502K), @investorgirlbritt (320K),
  @arthurthedeveloper (~222K). Where they have smaller business partners, use
  those instead — @somerscapital and @sujmehta rather than the two hotel megas.
- **Corporate project-marketing feeds**: @mack_development, @peak_developers,
  @redrealestatedevelopers, @rodrockdevelopment. In band, but they market
  projects rather than teach, so their audience is buyers, not developers.
- **@justin.goodin** — ground-up multifamily and syndication, thesis-aligned,
  but a BiggerPockets thread titled "WARNING – Justin Goodin is Operating as
  Goodin Development" exists and was not read. Do diligence before engaging.

## Operating rules

- Cooldown is 7 days per author (`scoring.cooldown_days`) — the engine
  hard-excludes an account touched inside that window, so engagement spreads
  instead of clustering.
- Rate limits in config: 10 IG replies/day. Do not raise them to chase reach.
- No DMs and no follow/unfollow automation exist anywhere in ENGAGE, by
  design. Following is a manual action you take; the engine never does it.
- Re-verify quarterly. Accounts go dormant fast here: @hotelinvestoracademy
  stopped in March 2026, @kenvanliew in April, @rossboggess in November 2025.
