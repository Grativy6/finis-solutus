# Finis Solutus Ledger Helpers

These small Python tools make repeatable arithmetic and bookkeeping easier. They do not resolve actions, choose rewards, establish evidence, close obligations, migrate campaigns, create canon, authenticate a custodian, or certify a branch.

The active kernel, campaign rules lock, accepted DM resolution, and committed campaign receipts remain controlling. Inside one helper ledger, `events.jsonl` is the internal replay source for `state.json`; neither file becomes campaign authority merely because a command accepted it. A successful command means only that the supplied input passed the checks implemented by that tool version and reported scope.

## Requirements and tools

- Python 3.10 or newer.
- Python standard library only; no installation step or network access is required.
- Run commands from the repository root.

| Tool | Purpose |
|---|---|
| `fs_math.py` | Stateless v0.16 calculations for derived resources, EXP carryover, Fatigue and overexertion, time, and load limits |
| `fs_ledger.py` | Apply already-resolved transitions to one helper ledger, replay its event chain, render a player footer, and rebuild its projection |
| `fs_repo_check.py` | Inspect the Git index of a proposed public checkout for common private-state boundary violations |

The ledger schema uses integers for Level, EXP, stats, whole Gold, revisions, and elapsed seconds. Current HP, RP, Fatigue, and overexertion are exact decimal strings. The stateless calculator accepts exact decimal strings wherever the calculation permits them. Binary JSON floats, duplicate keys, non-finite values, unknown operations, and unsupported fields are refused by the ledger parser.

## Quick arithmetic

Each command emits one JSON object:

```bash
python scripts/fs_math.py maxima \
  --strength 12 --agility 15 --stamina 15 --wisdom 24

python scripts/fs_math.py exp \
  --level 1 --current-exp 0 --award 1000

python scripts/fs_math.py fatigue-cost \
  --current 104 --maximum 105 --cost 6 --remainder 3 --hp 135

python scripts/fs_math.py fatigue-recover \
  --current 105 --maximum 105 --amount 8 --remainder 2

python scripts/fs_math.py time-advance \
  --current-seconds 32400 --elapsed-seconds 5400

python scripts/fs_math.py time-format --total-seconds 37800

python scripts/fs_math.py load-limit \
  --strength 30 --body-factor 1 --capability-factor 0.8
```

The calculator tallies caller-supplied values. It never decides whether an EXP award, Fatigue cost, recovery amount, elapsed interval, or load factor is justified.

## Ledger schemas

| Schema | Role |
|---|---|
| `fs-ledger-genesis/1` | Caller-supplied initial state |
| `fs-ledger-manifest/1` | Ledger identity and preserved genesis record |
| `fs-ledger-proposal/1` | One caller-supplied, already-resolved transition request |
| `fs-ledger-event/1` | One persisted, hash-chained internal event |
| `fs-ledger-reducer/1` | Versioned reducer identity stored inside each event so replay binds to declared transition semantics |
| `fs-ledger-state/1` | Rebuildable current-state projection |

Schema acceptance establishes structural compatibility only. In particular, the `kernel`, `refs`, reasons, summaries, correction labels, and migration labels are caller-supplied records; the helper does not prove that they are true, sufficient, authorized, or consistent with the full kernel. Calling a proposal a migration does not itself migrate a campaign or update its rules lock.

The on-disk helper ledger contains:

- `manifest.json` — the genesis record and ledger identity;
- `events.jsonl` — the logically append-only, hash-chained internal replay log;
- `state.json` — a rebuildable projection of that log;
- `LOCK` — a persistent lock target used to serialize writes.

Hashes detect an inconsistent or accidentally altered chain. They are not signatures, authentication, certification, or proof against a coordinated rewrite. Preserve accepted head hashes outside the ledger when rollback detection matters.

## Basic ledger workflow

Create a public player helper ledger from the synthetic example:

```bash
python scripts/fs_ledger.py init /path/to/example-ledger \
  scripts/examples/public_genesis.json
```

Inspect its identity, preview one already-resolved proposal, and apply that exact file:

```bash
python scripts/fs_ledger.py head /path/to/example-ledger --json

python scripts/fs_ledger.py apply /path/to/example-ledger \
  scripts/examples/resolved_block.json --dry-run --json

python scripts/fs_ledger.py apply /path/to/example-ledger \
  scripts/examples/resolved_block.json --json
```

Then render or inspect the projection and verify internal replay:

```bash
python scripts/fs_ledger.py status /path/to/example-ledger
python scripts/fs_ledger.py show /path/to/example-ledger --json
python scripts/fs_ledger.py verify /path/to/example-ledger --json
python scripts/fs_ledger.py rebuild /path/to/example-ledger --json
```

`verify` checks the manifest, internal hashes, exact reducer replay, revision continuity, and agreement between the event chain and `state.json`. It does not verify narrative truth, DM standing, kernel compliance beyond the implemented invariants, public-receipt adoption, external head continuity, or certification.

Every proposal names its intended campaign, branch, and subject; supplies the expected current revision and head; and carries stable receipt and request IDs. Reusing either ID for different content, applying from a stale head, overdrawing Gold, omitting required Earned Point assignments, or breaking another implemented invariant is refused before a new event is committed. Retrying the identical accepted request is idempotent.

The JSON event's `receipt_id` may correspond to a campaign receipt, but applying it does not create, adopt, or commit the public Markdown receipt. Preserve the declared DM's resolution, player-facing footer, smallest true deltas, open items, tool version and validation scope, and resulting helper head in the campaign's normal receipt process.

## Minimal model loop

A host can give a model a deterministic reducer without handing the tool narrative authority:

1. Read the current identity with `python scripts/fs_ledger.py head LEDGER --json` and supply the relevant controlling kernel, accepted campaign state, and unresolved items to the declared DM/model.
2. Let the DM/model resolve the scene under those authorities and emit one `fs-ledger-proposal/1` document. The proposal records the resolved transition; it does not authorize itself.
3. Run `python scripts/fs_ledger.py apply LEDGER PROPOSAL --dry-run --json`.
4. If refused, preserve the error and ask for a corrected proposal against the same unchanged head. Do not silently edit amounts or invent missing justification.
5. If the preview is accepted, apply the same unchanged proposal file with `python scripts/fs_ledger.py apply LEDGER PROPOSAL --json`.
6. Run `status LEDGER` for the footer and, when needed, `verify LEDGER --json` for scoped internal replay verification. Preserve the actual campaign receipt separately.

A model or wrapper must branch on the returned result field as well as the process exit code. In particular, `committed_projection_stale` and `already_applied_projection_stale` exit successfully because the event is already committed while `state.json` is stale. They are recovery states, not permission to submit a new transition.

## Projection recovery, backups, and rollback limits

`events.jsonl` is written before `state.json`. A process or storage failure can therefore commit the event while leaving the projection stale. If `apply --json` returns `committed_projection_stale` or `already_applied_projection_stale`:

1. Treat the named request as already committed.
2. Do not change its IDs, create a replacement proposal, or apply its effects again.
3. Retry the identical request or run `python scripts/fs_ledger.py rebuild LEDGER --json`.
4. Finish with `python scripts/fs_ledger.py verify LEDGER --json` and compare the head with the externally preserved expected head.

`rebuild` replaces only `state.json` after replaying a valid manifest and event chain. It never deletes, rewrites, or repairs an invalid event. If the manifest or event chain is corrupt, stop dependent play and recover from a protected backup or another independently anchored copy.

The helper has no rollback or backup command and no consistent-rollback detector. Before consequential writes, keep protected copies appropriate to the ledger's privacy and record accepted head hashes in an independent location such as the public campaign receipt or a protected operator record. The hash chain cannot detect restoration of a fully self-consistent older manifest, event log, and projection unless an external head anchor exposes the rollback.

## Cross-ledger operations are not atomic

One `apply` call locks and changes one ledger. The helper does not atomically coordinate a player ledger, one or more private NPC ledgers, multiple characters, or multiple World Branches.

For a resolved event spanning ledgers, preserve every starting head, dry-run every proposal first, and keep protected backups. Record the relationship among the per-ledger receipt and request IDs in the external campaign receipt; do not add an undeclared transaction field to the proposal schema. Apply in a declared order and verify every resulting ledger before publishing the joint campaign receipt. If some ledgers commit and a later one fails, stop at that partial boundary. Do not delete committed events or fabricate inverse operations; reconcile the accepted campaign state through explicit correction receipts or restore a protected set only under the campaign's actual authority and custody rules.

## Private NPC ledgers

Exact NPC mechanics must remain outside player-facing state. A private NPC helper ledger:

- must live outside every Git worktree, not merely in an ignored directory;
- contains no NPC Level, EXP, threshold, player points, or Earned Points;
- refuses the player-facing `status` rendering;
- suppresses exact values in ordinary command output.

Exact inspection is deliberately explicit:

```bash
python scripts/fs_ledger.py head /private/path/npc-ledger \
  --reveal-private --json

python scripts/fs_ledger.py show /private/path/npc-ledger \
  --reveal-private --json
```

The private `head` reveal exposes private ledger identity and its internal head; keep that anchor in a protected operator record. The `show` reveal writes the complete exact private state to standard output. Terminals, wrappers, logs, or redirected files may retain either result. Use them only through an authorized private route. Never paste their output into a public receipt. Public receipts may carry an opaque private companion reference, never the private values, internal head, identity, or filesystem path.

Do not store credentials, private human information, or raw secret prompts in either kind of ledger.

## Public repository boundary check

After staging the intended public changes, inspect the Git index:

```bash
python scripts/fs_repo_check.py .
```

Use `--json` for the deterministic `{checked_paths, findings, ok}` result. Exit `0` means no implemented finding was present, exit `1` means at least one boundary finding, and exit `2` means the repository could not be inspected.

The checker inspects staged/tracked Git paths and blobs, not untracked files or arbitrary working-tree content. It checks common private-ledger paths and names, every tracked symlink under `campaigns/`, and private Finis Solutus ledger manifests, states, and JSONL events at arbitrary tracked paths. It does not scan prose, judge whether public disclosure was authorized, or prove that no private material exists. It deliberately reports path and finding codes without echoing suspected private JSON values.

## Exit statuses

`fs_ledger.py` uses:

| Exit | Meaning |
|---:|---|
| `0` | Command completed; inspect the JSON result for states such as `committed_projection_stale` |
| `2` | Schema, syntax, input, or generic filesystem/I/O error |
| `3` | Stale or mismatched expected head/revision/identity |
| `4` | Implemented state invariant refused the transition |
| `5` | Manifest, event chain, or projection corruption/inconsistency |
| `6` | Another writer holds the ledger lock |
| `7` | Private-state boundary refused the operation |

`fs_math.py` returns `0` on success and argparse's `2` for invalid usage or values. `fs_repo_check.py` uses its separate `0` clean, `1` findings, `2` inspection-failure contract described above.

## Tests

```bash
python -m unittest discover -s scripts/tests -v
```

Tests use temporary directories and synthetic records. They do not require campaign data or private DM state.
