# Private State Boundary

This public campaign directory is not the DM ledger.

Keep the following in a separate private store:

- exact NPC STR, AGI, STA, WIS, HP, RP, Fatigue, inventory, and Gold;
- NPC knowledge paths, hidden relationships, and disposition state;
- unrevealed world facts, prepared events, and DM plans;
- private character material and privately delivered limits;
- credentials, tokens, account secrets, and private human information.

Public receipts may cite an opaque private companion-receipt ID or commitment. They must not reveal the underlying hidden state.

Any tool that processes this material must read from and write to a separate private store outside the public Git checkout. An ignored folder inside this checkout is not a private store.
