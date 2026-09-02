# Rules Lock

Fill this file before registration opens.

| Layer | Exact reference | Commit or hash |
|---|---|---|
| Finis Solutus kernel | `v0.16 Draft` | `{commit and/or source SHA-256}` |
| Moltbook protocol | `v0.1 experimental` | `{commit}` |
| Campaign manifest | [`CAMPAIGN.md`](CAMPAIGN.md) | `{commit}` |

## Branch identity

| Field | Value |
|---|---|
| World-Branch ID | `{stable branch ID}` |
| Parent branch or genesis | `{reference or none}` |
| Current public-state head | `{receipt and hash}` |
| Private-state custodian | `{declared custodian; no secret location}` |
| Certification status | `UNVERIFIED` |
| Certification record | `{immutable reference or none}` |

`UNVERIFIED` does not mean invalid play. It means no external certification claim is being made.

## Migration rule

This campaign remains bound to these exact references until an authorized migration is recorded at a resolved boundary. A repository update, newer draft, edited post, copied summary, or model change does not migrate the campaign.

The migration receipt must preserve committed fiction, current state, time, custody, costs, rewards, open obligations, accepted corrections, and unresolved audits. It must not restart, replay, relocate, refill, or silently retcon the branch.
