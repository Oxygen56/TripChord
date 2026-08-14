# Formal live-source production trust root

The formal six-layer run has no repository key, test-key fallback, Browser-token
derivation, or in-process key registry.  A clean checkout must be provisioned
before the Browser bridge can start.

## First start

Choose an owner-controlled persistent directory outside disposable build output,
then run:

```bash
export TRIPCHORD_FORMAL_SOURCE_TRUST_ROOT=/absolute/persistent/tripchord-formal-source
uv run python scripts/formal_source_trust.py init
uv run python scripts/formal_source_trust.py verify
```

`init` refuses an existing target.  It creates an owner-only `0700` root and
generation directory, `0600` private key, public anchor, control token, ledger,
and current-generation pointer using exclusive descriptor creation and `fsync`.
The API fails closed if any object is absent, linked, has the wrong owner/mode,
changes while read, or if the private key and public anchor key id differ.

The API process and the gate runner must receive the same absolute
`TRIPCHORD_FORMAL_SOURCE_TRUST_ROOT`.  Ordinary API and Browser principals never
receive the control token or signing key.  Offline validation reads only the
current generation's public anchor.

## Cold restart and active challenges

The ledger atomically stores the signed challenge, pre-event baseline, complete
event chain, count/hash, heartbeat and finalization context.  A cold API restart
restores the one unexpired active flow.  Operators must finalize it or use the
protected `abort`/`expire` control transition; a second challenge cannot replace
it.  A consumed, aborted, or expired challenge cannot record or finalize again.

## Rotation

Verify the root, ensure no challenge is active, stop new formal runs, then run:

```bash
uv run python scripts/formal_source_trust.py verify
uv run python scripts/formal_source_trust.py rotate
uv run python scripts/formal_source_trust.py verify
```

Rotation creates and `fsync`s a complete new generation before atomically
replacing the owner-only current-generation pointer.  A crash before that final
replace leaves the old generation authoritative; after it, only the new public
anchor is authoritative and old-key evidence is deliberately rejected.  Never
copy a development/test key into this root and never repair permissions with a
post-creation `chmod`; reprovision a new protected root instead.
