# Finis Solutus

> **Finite rules. Persistent consequences. Open possibility.**

Finis Solutus is a persistent, open-ended world and campaign system for AI-mediated roleplaying. It is not primarily a prewritten story. The DM maintains a coherent world; players decide what their characters attempt; consequences, relationships, work, injury, discovery, and unfinished threads persist.

## Current status

| Field | Value |
|---|---|
| Current project kernel | **v0.16 draft** |
| Status | Draft playtest kernel |
| Author and steward | **Christopher D. Pang** |
| Primary runtime target | Persistent Branchline World Branches |
| Stable release | Not yet declared |
| Project license | [CC BY 4.0](LICENSE) |

Start with the [current-kernel notice](kernel/CURRENT.md), then use either the [readable Markdown kernel](kernel/Finis_Solutus_World_Campaign_DM_Kernel_v0.16_Draft.md) or its [original DOCX source](kernel/source/Finis_Solutus_World_Campaign_DM_Kernel_v0.16_Draft.docx).

## Start playing

1. Give the exact current kernel to a capable model and assign it the DM/world role.
2. Ask it to begin at the **Welcome Gate**.
3. Establish the character, World Branch, stats, derived resources, modes, and first horizon through the kernel's First Resolve.
4. Enter only after the player gives the explicit entry instruction.
5. Preserve the resulting state and receipts between sessions.

For a shorter orientation, see [Player Quickstart](docs/PLAYER_QUICKSTART.md). It is an aid, not a replacement for the kernel.

## Moltbook open table

The [Moltbook Open Table Protocol](docs/MOLTBOOK_OPEN_TABLE_PROTOCOL.md) adapts Finis Solutus to asynchronous, multi-character public play. It adds transport, registration, scene-cut, and receipt rules without changing the world mechanics. [Copy-ready post templates](docs/MOLTBOOK_POST_TEMPLATES.md) provide the smallest practical launch surface.

The essential boundary is simple:

> Control only your registered character. Declare attempts, not outcomes. Only the declared DM's numbered resolution and its repository receipt change campaign state.

Reusable campaign files live under [`campaigns/_template`](campaigns/_template/).

## What controls

- The declared active kernel controls universal game mechanics.
- A campaign's rules lock declares the exact kernel and transport protocol bound to that campaign.
- World-Branch facts remain local to the branch that established them.
- Settled corrections and the latest verified state receipts control over stale summaries or examples.
- A newer draft never silently migrates an active campaign.
- Earlier drafts are development ancestry, not current rules and not independent corroboration.

The DOCX is the authored source artifact for v0.16. The Markdown file is a convenience rendering for browsers and model retrieval. If a conversion defect creates a material conflict, the DOCX controls until the rendering is repaired.

## Certified branches and the canon multiverse

Every campaign is already its own persistent World Branch under v0.16: Universal Canon supplies the shared deep rules, while materially established local facts remain World-Branch Canon. Part XLI also gives separate branches a way to meet through Rimae Crossings without flattening their histories into one false timeline.

That makes a **canon multiverse** possible: freely created local branches, each carrying its own history, with explicit receipts whenever people, objects, knowledge, or consequences cross between them.

Branchline certification is the proposed trust layer for that ecosystem. A certification would attest that a named branch was bound to declared rules and that its lineage, custody boundary, and receipts passed specified checks at a particular time. It would not make the branch's story universally canonical, certify every claim inside it, or grant its operator authority over another branch.

The certification service is **not operational yet**. The proposed record and boundary are documented in [Certified Branches and the Canon Multiverse](docs/CERTIFIED_BRANCHES_AND_CANON_MULTIVERSE.md) so the idea can develop in public without being mistaken for an existing feature.

## Public and private campaign state

This public repository may hold player-visible rules, rosters, scene openings, admitted-turn records, public state, and public receipts.

It must not hold exact NPC mechanics, unrevealed world facts, private character material, credentials, or DM-only plans. Those belong in a separate private store. The repository's ignore rules include tripwires for common private-ledger paths.

## Ledger helpers

The standard-library Python tools under [`scripts/`](scripts/) can calculate v0.16 arithmetic, apply already-resolved state changes to a hash-chained helper ledger, rebuild its projection, and check a staged public Git index for common private-state boundary violations. They are non-authoritative implementation aids: they do not resolve play, adopt facts, migrate a campaign, authenticate a custodian, or certify a branch. Within one helper ledger, its event log is the replay source for its generated projection; the active kernel, rules lock, accepted DM resolution, and committed campaign receipts remain controlling.

## Repository map

| Path | Purpose |
|---|---|
| [`kernel/`](kernel/) | Current kernel, source artifact, and authority notice |
| [`docs/`](docs/) | Player quickstart and public-play protocols |
| [`campaigns/`](campaigns/) | Public campaign templates and, later, public traces |
| [`scripts/`](scripts/) | Deterministic, non-authoritative arithmetic and ledger helpers |
| [`history/`](history/) | Byte-preserved development drafts and checksums |
| [`CHANGELOG.md`](CHANGELOG.md) | Compact development lineage |
| [`CONTRIBUTING.md`](CONTRIBUTING.md) | Playtest reporting and contribution boundaries |
| [`LICENSE`](LICENSE) | Creative Commons Attribution 4.0 International legal text |
| [`RIGHTS.md`](RIGHTS.md) | License scope, attribution, and certification boundary |

## Development history

The supplied v0.1 and v0.3–v0.15 artifacts are preserved unchanged in [`history/drafts`](history/drafts/). v0.2 was not supplied and is recorded as an archival gap rather than reconstructed.

## Authorship

Finis Solutus was authored by **Christopher D. Pang**, its original steward. AI systems may assist with drafting, playtesting, continuity checks, repository preparation, and production. They are tools, not co-authors or authorities.

Except where a file says otherwise, this repository is licensed under [CC BY 4.0](LICENSE). Attribution and scope guidance are recorded in [RIGHTS.md](RIGHTS.md).
