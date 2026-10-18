"""Owner-only SQL bootstrap from ignored deployment state; never used by CI.

Run from the repository root with the locked shadow dependencies and an existing
Azure CLI owner login. All parameters and outputs containing real values stay
in ignored files. No SQL password or Graph directory role is needed.
"""

import argparse
import ipaddress
import json
from pathlib import Path
from uuid import UUID


def private_file(value):
    path = Path(value).resolve()
    root = Path.cwd().resolve()
    if not any(path.is_relative_to(root / name) for name in (".azure", ".data")):
        raise ValueError("bootstrap inputs must be private local files")
    return json.loads(path.read_text(encoding="utf-8-sig"))


def bootstrap(config):
    import certifi
    import pytds
    from azure.identity import AzureCliCredential

    from pipmind.feeds.sql_store import SqlFeedStore
    from pipmind.feeds.store import encoded
    from pipmind.shadow.common import digest, now
    from pipmind.shadow.sql import fulltext, migrate
    from pipmind.shadow.store import SqlMind
    from pipmind.shadow.tls import configure_tls

    configure_tls()
    credential = AzureCliCredential()

    def connect():
        return pytds.connect(
            dsn=config["sqlHostname"],
            database=config["databaseName"],
            cafile=certifi.where(),
            validate_host=True,
            enc_login_only=False,
            access_token_callable=lambda: (
                credential.get_token("https://database.windows.net/.default").token
            ),
            login_timeout=30,
            timeout=60,
            use_mars=False,
            autocommit=False,
        )

    rules = private_file(config["databaseFirewallFile"])
    if len(rules) > 256:
        raise ValueError("too many database firewall rules")
    with connect() as db:
        db.autocommit = True
        cur = db.cursor()
        for index, rule in enumerate(rules):
            start = str(ipaddress.IPv4Address(rule["startIpAddress"]))
            end = str(ipaddress.IPv4Address(rule["endIpAddress"]))
            cur.execute(
                "DECLARE @rule nvarchar(128)=%s, @start varchar(50)=%s, @end varchar(50)=%s; "
                "EXEC sp_set_database_firewall_rule @name=@rule,"
                "@start_ip_address=@start,@end_ip_address=@end",
                (f"shadow-db-{index}", start, end),
            )
    migrate(connect)
    fulltext(connect)
    recipes = private_file(config["feedConfigFile"])
    SqlFeedStore(connect, profile=digest(encoded(recipes)))
    store = SqlMind(connect, config["trader"])
    with connect() as db:
        cur = db.cursor()
        cur.execute("SELECT 1 FROM pm_minds WHERE trader=%s", (config["trader"],))
        initialized = cur.fetchone() is not None
    if not initialized:
        store.initialize(
            "You are PipMind. Observe macroeconomic information in shadow mode. "
            "Preserve uncertainty and source timestamps. No order submission is possible.",
            {
                "version": "shadow-observation-v1",
                "text": "Observe and record. No strategy or risk "
                "limits have been approved. Do not propose or execute orders. Never invent "
                "positions, fills or market facts. Keep identity and playbook unchanged.",
            },
            now().isoformat(),
            binding=digest(config["bindingRef"]),
        )
    # UUID.bytes_le matches SQL Server uniqueidentifier storage (not UUID.bytes).
    sid = UUID(config["identityPrincipalId"]).bytes_le.hex()
    with connect() as db:
        cur = db.cursor()
        cur.execute("SELECT sid,type FROM sys.database_principals WHERE name='pipmind-shadow'")
        existing = cur.fetchone()
        if existing and (bytes(existing[0]).hex() != sid or existing[1] != "E"):
            raise ValueError("existing principal binding differs")
        if not existing:
            cur.execute(f"CREATE USER [pipmind-shadow] WITH SID=0x{sid}, TYPE=E")
        for table in (
            "pm_minds",
            "pm_records",
            "pm_terms",
            "pm_inputs",
            "pm_cursors",
            "pm_outcomes",
            "pm_runtime",
            "pf_meta",
            "pf_state",
            "pf_inbox",
            "pf_events",
        ):
            cur.execute(f"GRANT SELECT, INSERT ON dbo.{table} TO [pipmind-shadow]")
        for table in (
            "pm_minds",
            "pm_records",
            "pm_inputs",
            "pm_cursors",
            "pm_runtime",
            "pf_state",
            "pf_inbox",
        ):
            cur.execute(f"GRANT UPDATE ON dbo.{table} TO [pipmind-shadow]")
        cur.execute("GRANT SELECT ON dbo.pm_schema TO [pipmind-shadow]")
        cur.execute("GRANT SELECT,INSERT,DELETE ON dbo.pf_alerts TO [pipmind-shadow]")
        db.commit()
    print("PASS: SQL schema, full-text, initialized paused mind and scoped identity grants")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    args = parser.parse_args()
    bootstrap(private_file(args.config))
