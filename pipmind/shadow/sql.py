"""SQL Server/Azure SQL persistence. SQLite is not a hosted shadow store.

All writers use a transaction-owned application lock and a revision check. This
protects shadow state; it is not the M5 broker ownership/fencing protocol.
"""

import os
import re
import ssl
from contextlib import contextmanager

from pipmind.shadow.common import ShadowError

DDL = (
    """IF OBJECT_ID('pm_minds') IS NULL CREATE TABLE pm_minds (
      trader varchar(128) COLLATE Latin1_General_100_BIN2 NOT NULL PRIMARY KEY,
      lineage varchar(128) NOT NULL,
      binding char(64) NOT NULL, revision bigint NOT NULL, paused bit NOT NULL,
      created varchar(32) NOT NULL, working nvarchar(max) NOT NULL,
      playbook_version nvarchar(128) NOT NULL, parent nvarchar(max) NULL)""",
    """IF OBJECT_ID('pm_records') IS NULL CREATE TABLE pm_records (
      pk bigint IDENTITY NOT NULL CONSTRAINT PK_pm_records PRIMARY KEY,
      trader varchar(128) COLLATE Latin1_General_100_BIN2 NOT NULL REFERENCES pm_minds(trader),
      id nvarchar(256) COLLATE Latin1_General_100_BIN2 NOT NULL, part varchar(32) NOT NULL,
      ts datetime2(6) NOT NULL, meta nvarchar(max) NOT NULL, body nvarchar(max) NOT NULL,
      CONSTRAINT UQ_pm_records UNIQUE(trader,id))""",
    """IF OBJECT_ID('pm_terms') IS NULL CREATE TABLE pm_terms (
      term varchar(40) NOT NULL, record_pk bigint NOT NULL REFERENCES pm_records(pk),
      CONSTRAINT PK_pm_terms PRIMARY KEY(term,record_pk))""",
    """IF OBJECT_ID('pm_inputs') IS NULL CREATE TABLE pm_inputs (
      pk bigint IDENTITY PRIMARY KEY,
      trader varchar(128) COLLATE Latin1_General_100_BIN2 NOT NULL REFERENCES pm_minds(trader),
      id nvarchar(256) COLLATE Latin1_General_100_BIN2 NOT NULL,
      facts char(64) NOT NULL, envelope nvarchar(max) NOT NULL,
      status varchar(16) NOT NULL DEFAULT 'pending',
      CONSTRAINT UQ_pm_inputs UNIQUE(trader,id))""",
    """IF OBJECT_ID('pm_cursors') IS NULL CREATE TABLE pm_cursors (
      trader varchar(128) COLLATE Latin1_General_100_BIN2 NOT NULL REFERENCES pm_minds(trader),
      source varchar(128) COLLATE Latin1_General_100_BIN2 NOT NULL,
      seq bigint NOT NULL, CONSTRAINT PK_pm_cursors PRIMARY KEY(trader,source))""",
    """IF OBJECT_ID('pm_outcomes') IS NULL CREATE TABLE pm_outcomes (
      pk bigint IDENTITY PRIMARY KEY,
      trader varchar(128) COLLATE Latin1_General_100_BIN2 NOT NULL REFERENCES pm_minds(trader),
      event_id nvarchar(256) COLLATE Latin1_General_100_BIN2 NOT NULL,
      result nvarchar(max) NOT NULL,
      CONSTRAINT UQ_pm_outcomes UNIQUE(trader,event_id))""",
    """IF OBJECT_ID('pm_runtime') IS NULL CREATE TABLE pm_runtime (
      trader varchar(128) COLLATE Latin1_General_100_BIN2 NOT NULL REFERENCES pm_minds(trader),
      k varchar(128) COLLATE Latin1_General_100_BIN2 NOT NULL,
      v nvarchar(max) NOT NULL, CONSTRAINT PK_pm_runtime PRIMARY KEY(trader,k))""",
    """IF OBJECT_ID('pm_schema') IS NULL BEGIN
      CREATE TABLE pm_schema(version int NOT NULL); INSERT pm_schema VALUES(1); END""",
)


def connector(env=None):
    env = os.environ if env is None else env
    import pytds

    host, database = env.get("PIPMIND_SQL_SERVER", ""), env.get("PIPMIND_SQL_DATABASE", "")
    if not host or not database or not re.fullmatch(r"[a-zA-Z0-9_.-]{1,253}", host):
        raise ShadowError("sql_configuration_required")
    options = {
        "dsn": host,
        "database": database,
        "port": int(env.get("PIPMIND_SQL_PORT", "1433")),
        "login_timeout": 10,
        "timeout": 30,
        "autocommit": False,
        "use_mars": False,
    }
    if env.get("PIPMIND_SQL_LOCAL") == "true":
        # Unencrypted transport is restricted to an explicitly opted-in local container.
        if host not in {"localhost", "127.0.0.1", "sql"}:
            raise ShadowError("local_sql_host_required")
        options.update(user=env.get("PIPMIND_SQL_USER"), password=env.get("PIPMIND_SQL_PASSWORD"))
        if not options["user"] or not options["password"]:
            raise ShadowError("local_sql_credentials_required")
    else:
        from azure.identity import ManagedIdentityCredential

        from pipmind.shadow.tls import configure_tls

        configure_tls()

        ca = env.get("PIPMIND_SQL_CA_FILE") or ssl.get_default_verify_paths().cafile
        if not ca:
            raise ShadowError("sql_trust_store_required")
        identity = env.get("PIPMIND_MANAGED_IDENTITY_CLIENT_ID")
        credential = ManagedIdentityCredential(**({"client_id": identity} if identity else {}))
        options.update(
            cafile=ca,
            validate_host=True,
            enc_login_only=False,
            access_token_callable=lambda: (
                credential.get_token("https://database.windows.net/.default").token
            ),
        )

    def connect():
        try:
            return pytds.connect(**options)
        except Exception:
            raise ShadowError("sql_unavailable") from None

    return connect


@contextmanager
def transaction(connect, resource="pipmind-shadow"):
    db = None
    try:
        db = connect()
        cur = db.cursor()
        cur.execute(
            "SET NOCOUNT ON; SET XACT_ABORT ON; SET TRANSACTION ISOLATION LEVEL SERIALIZABLE"
        )
        cur.execute(
            """DECLARE @r int; EXEC @r=sp_getapplock @Resource=%s,
                    @LockMode='Exclusive', @LockOwner='Transaction', @LockTimeout=5000;
                    SELECT @r""",
            (resource,),
        )
        while cur.description is None:
            if not cur.nextset():
                raise ShadowError("shadow_lock_result_missing")
        if cur.fetchone()[0] < 0:
            raise ShadowError("shadow_store_busy")
        yield cur
        db.commit()
    except ShadowError:
        if db:
            db.rollback()
        raise
    except Exception:
        if db:
            db.rollback()
        raise ShadowError("shadow_store_transaction_failed") from None
    finally:
        if db:
            db.close()


@contextmanager
def stream_transaction(connect, resource, *, snapshot=True):
    """Use an already-enabled transaction snapshot; never alter database options.

    Without that capability, the existing coordinated source lock is required.
    A failed snapshot never falls back mid-checkpoint to a different version.
    """
    db = None
    try:
        enabled = False
        if snapshot:
            db = connect()
            cur = db.cursor()
            cur.execute(
                "SELECT snapshot_isolation_state FROM sys.databases WHERE database_id=DB_ID()"
            )
            enabled = cur.fetchone()[0] == 1
        if not enabled:
            if db:
                db.close()
                db = None
            with transaction(connect, resource) as cur:
                yield cur, "locked"
            return
        from pytds.extensions import ISOLATION_LEVEL_SNAPSHOT

        # TDS automatically begins transactions. Use the driver's public setter
        # to end the metadata transaction and begin the next one in SNAPSHOT;
        # a SET statement inside an already-open transaction is too late.
        db.isolation_level = ISOLATION_LEVEL_SNAPSHOT
        cur = db.cursor()
        cur.execute("SET NOCOUNT ON; SET XACT_ABORT ON")
        yield cur, "snapshot"
        db.commit()
    except ShadowError:
        if db:
            db.rollback()
        raise
    except Exception:
        if db:
            db.rollback()
        raise ShadowError("shadow_snapshot_transaction_failed") from None
    finally:
        if db:
            db.close()


def migrate(connect):
    with transaction(connect, "pipmind-shadow-schema") as cur:
        for statement in DDL:
            cur.execute(statement)
        cur.execute("SELECT version FROM pm_schema")
        if [row[0] for row in cur.fetchall()] != [1]:
            raise ShadowError("incompatible_shadow_schema")


def fulltext(connect):
    """DDL runs outside a user transaction; Azure SQL has this capability."""
    db = connect()
    try:
        db.autocommit = True
        cur = db.cursor()
        cur.execute("SELECT FULLTEXTSERVICEPROPERTY('IsFullTextInstalled')")
        if cur.fetchone()[0] != 1:
            raise ShadowError("sql_fulltext_required")
        cur.execute(
            "IF NOT EXISTS(SELECT 1 FROM sys.fulltext_catalogs WHERE name='pm_memory') "
            "CREATE FULLTEXT CATALOG pm_memory"
        )
        cur.execute(
            "IF NOT EXISTS(SELECT 1 FROM sys.fulltext_indexes "
            "WHERE object_id=OBJECT_ID('pm_records')) "
            "CREATE FULLTEXT INDEX ON pm_records(body LANGUAGE 1033) "
            "KEY INDEX PK_pm_records ON pm_memory WITH CHANGE_TRACKING AUTO"
        )
    except ShadowError:
        raise
    except Exception:
        raise ShadowError("sql_fulltext_setup_failed") from None
    finally:
        db.close()
