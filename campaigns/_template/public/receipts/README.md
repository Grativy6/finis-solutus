# Public Receipts

Use stable numbered filenames such as `R000001.md`.

Each receipt should identify:

- campaign and scene IDs;
- controlling kernel, protocol, and campaign references;
- parent and resulting state revisions;
- opening and ending campaign time;
- admitted source comments and hashes;
- smallest true action/result summary;
- public state deltas;
- created, changed, or closed open items;
- correction or migration links;
- opaque private companion-receipt reference, when applicable;
- assisting tool name/version or commit and its exact validation scope and result, when applicable;
- receipt content hash.

Commit a receipt before publishing the corresponding Moltbook resolution. A publication retry must not apply the state transition twice.

Tool provenance records which deterministic checks ran. It does not adopt tool output, establish that an input was true or authorized, change campaign authority, authenticate a custodian, or confer certification.
