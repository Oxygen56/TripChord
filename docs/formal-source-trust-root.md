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
`TRIPCHORD_FORMAL_SOURCE_TRUST_ROOT`; put that explicit value in the service
manager configuration before starting the controlled API.  There is no
repository-local or implicit default.  Ordinary API and Browser principals
never receive the control token or signing key.  Offline validation selects the
exact retained public generation named by each signed proof.

## Cold restart and active challenges

The ledger atomically stores the signed challenge, pre-event baseline, complete
event chain, count/hash, heartbeat and finalization context.  TripChord uses the
explicit **non-continuation** restart model: if `pid`/`started_at` changes while a
challenge is active, startup atomically marks that attempt `aborted` with
`runtime_restart_requires_new_attempt` and removes its active state.  The old
signed proof remains auditable, but it cannot record or finalize in the new
process.  The runner must prepare a new job attempt and receive a new job and
challenge identity.  A consumed, aborted, or expired challenge cannot record or
finalize again.

## Rotation

Verify the root, ensure no challenge is active, stop new formal runs, then run:

```bash
uv run python scripts/formal_source_trust.py verify
uv run python scripts/formal_source_trust.py rotate
uv run python scripts/formal_source_trust.py verify
```

Rotation creates and `fsync`s a complete new generation before atomically
replacing the owner-only current-generation pointer.  A crash before that final
replace leaves the old signing generation current.  After replacement, only the
new private key can sign new evidence; retained old public anchors continue to
verify already committed evidence by exact `anchor_version` and `key_id`.
Unknown, cross-generation, rollback and old-private-key signing attempts remain
fail-closed.  Restart the controlled API after rotation so no resident process
can keep the retired private key.  Never copy a development/test key into this
root and never repair secret-file permissions after creation; reprovision a new
protected root instead.
