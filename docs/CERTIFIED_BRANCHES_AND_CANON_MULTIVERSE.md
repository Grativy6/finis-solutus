# Certified Branches and the Canon Multiverse

**Status:** Proposed trust layer; not operational

**Current kernel basis:** Finis Solutus — World, Campaign & DM Kernel v0.16 Draft, especially Part I (Canon and Campaign Scope) and Part XLI (Joint Branches and World Translation)

This document records a future direction without silently adding mechanics to the v0.16 kernel.

## 1. The canon multiverse

Finis Solutus separates two kinds of canon:

- **Universal Canon** contains the deep rules shared by every Finis Solutus World Branch.
- **World-Branch Canon** contains the places, people, institutions, events, discoveries, and other facts materially established inside one persistent campaign world.

Local lore does not become universal merely because one branch generated it. Branches may meet through a Rimae Crossing, but the receiving world keeps its own physics, history, people, ecology, Solums, and unresolved Threads. Arriving characters keep their identity and trace.

The resulting multiverse is canonically plural. It does not require a single master storyline. A joint event can become established in every participating branch through linked receipts while each branch keeps the parts of reality and history that remain its own.

## 2. What certification would mean

Branchline certification is envisioned as a revision-specific attestation that a named branch satisfied declared trace and custody checks at a stated time.

A minimum certification record should bind:

| Field | Purpose |
|---|---|
| Certification ID and status | Stable identity; active, suspended, superseded, withdrawn, or expired |
| World-Branch ID | The exact branch being checked |
| Kernel lock | Version, source hash, and governing commit |
| Transport locks | Exact multiplayer or relay protocols, if any |
| Genesis or parent receipt | Where this branch began or diverged |
| Steward and delegated roles | Who may change rules, resolve the world, or relay state |
| Public-state head | Latest verified public receipt and content hash |
| Private-state boundary | Declared custodian and proof that public/private separation exists, without exposing secrets |
| Migration lineage | Every admitted rules migration and superseded lock |
| Crossing lineage | Linked cross-branch receipts and translations |
| Verification profile | Exact checks performed and their versions |
| Issuer and timestamp | Who made this bounded attestation and when |

Certification should say only what its evidence supports. It would not certify that:

- every fictional statement is objectively true outside its branch;
- every resolution is aesthetically good or free from judgment calls;
- the branch is Universal Canon;
- the operator speaks for Christopher D. Pang, Branchline Systems, or another branch;
- a model is conscious, reliable in unrelated settings, or authorized beyond its declared role;
- future revisions still conform after the certified state head.

Certification is not required to play, fork, adapt, or publish under CC BY 4.0. It is an optional trust signal, not a gate on the licensed material.

## 3. Crossing between certified branches

A Rimae Crossing joins worlds without absorbing one branch into another. A crossing receipt should therefore preserve both sides of the boundary.

At minimum, record:

1. source and destination World-Branch IDs;
2. source and destination rules locks and state heads;
3. participating characters, objects, and custody;
4. the admitted event in each branch;
5. native stats and any translated display notation;
6. retained knowledge, inventory, conditions, obligations, and unresolved audits;
7. differences of reality that remain unmerged;
8. paired resulting receipts and hashes.

The kernel's rule is the design test:

> **Translate differences of notation. Preserve differences of reality.**

If two branches later disagree about the joint event, neither record should be silently overwritten. Suspend only the dependent crossing claims, preserve both receipts, and reopen from the last mutually verified boundary.

## 4. Certification lifecycle

A practical service could use these states:

| State | Meaning |
|---|---|
| `UNVERIFIED` | No certification claim has been made |
| `PENDING` | A named revision is under review |
| `CERTIFIED` | The named revision passed the declared verification profile |
| `SUPERSEDED` | A newer certification record controls |
| `SUSPENDED` | A material audit, custody, or divergence issue remains open |
| `WITHDRAWN` | The issuer has withdrawn the attestation, without erasing its history |
| `EXPIRED` | The declared verification period ended |

Status changes must append records; they must not erase earlier attestations. A new campaign event does not retroactively invalidate an honest older certification, but it may move current play beyond that certification's covered state head.

## 5. Name and representation boundary

Anyone may make and share branches or adaptations under the repository license. The license does not itself confer endorsement or certified status.

An interface should never turn visual similarity into authority. A valid certification display must resolve to the exact certification record, issuer, World-Branch ID, state head, and verification profile. If that record cannot be resolved, display `UNVERIFIED` or `CERTIFICATION UNKNOWN` rather than guessing.

## 6. Promotion boundary

This proposal should become an operational certification specification only after it has:

- a numbered version;
- machine-readable record schemas;
- deterministic verification tests where possible;
- explicit human-review boundaries where judgment remains;
- key and issuer-rotation procedures;
- suspension, correction, appeal, and reopening paths;
- public examples that do not expose private DM state;
- at least one adversarial test of forged, stale, copied, and partially valid certification claims.

Until then, this document records direction, not authority.
