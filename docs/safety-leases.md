# SQL lease primitive

`pipmind.safety.lease_sql` is an isolated ownership primitive for a SQL Server /
Azure SQL database incarnation. It is not connected to the shadow runtime,
gateway, order transport or owner controls. It neither authorizes trading nor
provides independent zero-order evidence. Existing contracts and migrations are
unchanged.

An operator explicitly calls `bootstrap(connect)` to create only
`dbo.pm_safety_leases_v1`. Bootstrap DDL has its own transaction-owned SQL
application lock. The constructor performs no SQL. A caller then constructs
`SqlLeaseStore(connect, LeaseBinding(account_id, environment, resource), authority_epoch)`
and calls `initialize()` separately. Initialization inserts an absent binding;
repeated initialization preserves the row exactly, including an active holder.
An existing different authority epoch is refused, never silently replaced.

The connector must return fresh, non-pooled DB-API connections with
`autocommit=False`. The existing configured SQL connector satisfies this
requirement; this module does not load credentials, open a configured connection
implicitly, alter database settings or integrate a migration hook. SQL sessions
are disposed on every outcome; disposal aborts uncommitted work without issuing
a potentially blocking rollback handshake on a broken transport.

## Operations and evidence

- `acquire(owner_id, *, ttl_seconds)` refuses an active holder, including the same
  owner. Successful acquisition increments the durable generation and returns a
  frozen `LeaseGrant` containing the binding, authority epoch, opaque owner ID,
  generation, random 256-bit token and server expiry (UTC). The raw token is omitted
  from its representation and only its SHA-256 digest is stored in SQL. Keep the
  grant private. Acquisition is never an idempotent command replay mechanism.
- `renew(grant, *, ttl_seconds)` requires an exact current, unexpired grant and
  extends expiry using SQL server time. It cannot revive an expired lease.
- `release(grant)` checks current ownership and expiry and clears holder fields;
  the generation remains. The next acquisition creates a new generation/token.
- `compare_and_act(grant, sql_callback)` checks current ownership, runs a trusted
  synchronous SQL-only callback with the same cursor, then checks identity and
  expiry again before commit. A changed lease row is refused. Callback SQL effects
  roll back when the callback fails, ownership no longer matches or expiry has
  elapsed. Its result is returned only after an acknowledged commit.

Every operation serializes on a transaction-owned application lock for the
binding; rows are also read with `UPDLOCK,HOLDLOCK` under serializable isolation.
The binding key hashes a canonical array of exact account/environment/resource
strings; the retained strings and authority are compared exactly in Python.
Different positions or commands are not combined by this primitive.

TTL must be an explicit integer from **1 to 300 seconds**, excluding booleans.
This is a technical API bound, not an owner-approved trading/risk policy or a
recommendation for deployment timing. Expiry is exclusively `SYSUTCDATETIME()`;
equality with expiry is expired. Returned timestamps are observations, not a
detached permit valid until that time. The post-callback check is inside SQL's
transaction; scheduling, transport and commit can still take time afterward.

The callback must consume its SQL results before returning. It must not call a
broker/network service, commit/roll back, change isolation, close the connection,
alter the lease table or invoke nested lease operations. Python cannot sandbox a
trusted callback: transaction control or external effects would violate its
contract and cannot be undone by a later refusal. This is a local SQL
compare-and-act boundary, **not** an atomic SQL-to-broker dispatch protocol. A
future command/outbox integration must preserve uncertain outcomes and prove its
own dispatch/reconciliation semantics.

## Failures and recovery boundary

`LeaseRefused` contains a fixed refusal code (for example `lease_held`,
`lease_stale_owner`, `lease_expired`, `lease_authority_mismatch`). Missing bootstrap
or unexpected SQL/callback failures produce fixed `LeaseUnavailable` codes. No
driver text or callback contents are exposed. There is no fallback in-memory
lease. Generation exhaustion also refuses acquisition.

If the commit call raises, the operation raises
`LeaseCommitUnknown("lease_commit_unknown")`, whether or not SQL actually
committed. It does not return a grant or callback result, infer rollback from a
closed connection, or automatically retry. Reconcile the durable operation and
any downstream obligations before deciding a next action. This module has no
command receipt/idempotency ledger and cannot decide whether replay is safe.

A monotonically increasing SQL generation **can rewind on database restore**.
The stored `authority_epoch` only binds a caller-supplied value; this module does
not verify an external authority, attest its freshness, detect restores or fence
an old process holding a grant that matches a restored row. Restoring this table
and reusing the old epoch cannot establish safe ownership.

Before operating after restore, an external trusted authority must renew the
authority epoch outside the restored database's rollback domain, old owners must
be fenced/quiesced, an explicitly reviewed rebind must occur, and current broker
activity/pending obligations must be reconciled. That authority and workflow are
integration prerequisites, not implemented resources, credentials or a reset API
here. Fresh constructors, bootstrap and initialization cannot bypass ownership.
This primitive alone does not satisfy the roadmap's restore/reconciliation gate
or authorize M6 demo orders.

## Validation

`tests/test_safety_lease.py` uses synthetic DB-API transaction fakes only. The
separately supplied actual-SQL regression fragment exercises concurrent holders,
expiry rollback, stale tokens/generations, exact bindings, renewal/release and
lost commit acknowledgements on the existing disposable CI SQL connector.
Offline success is not evidence that actual SQL or a hosted deployment ran.
