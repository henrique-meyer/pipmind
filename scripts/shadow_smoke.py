"""Real local SQL/container flow on an internal Docker network, without cloud calls."""

import json
import os
import secrets
import subprocess
import tempfile
import time
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
SQL_IMAGE = "pipmind-sql:local"
APP_IMAGE = "pipmind-shadow:ci"
SERVICE_DATABASE = "pipmind_shadow_service"


def service_checkpoint_code():
    """Read one exact local fixture checkpoint, returning hashes instead of bodies."""
    return f"""
import hashlib,json,os
from pipmind.contracts import loads
from pipmind.shadow.common import encoded
from pipmind.shadow.sql import connector
from pipmind.shadow.store import SqlMind
assert os.environ.get('PIPMIND_SQL_LOCAL')=='true'
assert os.environ.get('PIPMIND_SQL_DATABASE')=={SERVICE_DATABASE!r}
assert os.environ['PIPMIND_SHADOW_TRADER'].startswith('fixture-service-')
store=SqlMind(connector(),os.environ['PIPMIND_SHADOW_TRADER'])
queries={{
 'pm_minds':('SELECT trader,lineage,binding,revision,paused,created,working,'
             'playbook_version,parent FROM pm_minds WHERE trader=%s ORDER BY trader'),
 'pm_records':("SELECT pk,id,part,CONCAT(CONVERT(varchar(33),ts,126),'Z'),meta,body "
               'FROM pm_records WHERE trader=%s ORDER BY pk'),
 'pm_inputs':'SELECT pk,id,facts,envelope,status FROM pm_inputs WHERE trader=%s ORDER BY pk',
 'pm_outcomes':'SELECT pk,event_id,result FROM pm_outcomes WHERE trader=%s ORDER BY pk',
 'pm_cursors':('SELECT source,seq FROM pm_cursors WHERE trader=%s '
               'ORDER BY source COLLATE Latin1_General_100_BIN2'),
 'pm_terms':('SELECT t.term,t.record_pk FROM pm_terms t '
             'JOIN pm_records r ON t.record_pk=r.pk '
             'WHERE r.trader=%s ORDER BY t.term,t.record_pk'),
}}
result={{'tables':{{}}}}
with store.transaction() as cursor:
 head=store._head(cursor)
 result.update(paused=head['paused'],revision=head['revision'],
               reasoning_hold_present=store._runtime(cursor,'reasoning_hold')is not None)
 for table,query in queries.items():
  cursor.execute(query,(store.trader,));fingerprint=hashlib.sha256();count=0
  while True:
   row=cursor.fetchone()
   if row is None:break
   fingerprint.update(encoded(list(row)).encode());fingerprint.update(b'\\n');count+=1
  result['tables'][table]={{'count':count,'sha256':fingerprint.hexdigest()}}
 cursor.execute('SELECT id,status FROM pm_inputs WHERE trader=%s ORDER BY pk',(store.trader,))
 result['input_statuses']=dict(cursor.fetchall())
 result['pending']=sum(value=='pending'for value in result['input_statuses'].values())
 cursor.execute('SELECT event_id,result FROM pm_outcomes WHERE trader=%s ORDER BY pk',
                (store.trader,))
 result['outcome_statuses']={{}}
 for identifier,body in cursor.fetchall():
  outcome=loads(body)
  result['outcome_statuses'][identifier]={{'status':outcome['status'],
                                         'dispatches':outcome['dispatches']}}
 cursor.execute("SELECT v FROM pf_meta WHERE k='source'")
 row=cursor.fetchone();source=row[0]if row else None
 cursor.execute('SELECT seq FROM pm_cursors WHERE trader=%s AND source=%s',
                (store.trader,source));row=cursor.fetchone()
 result['feed_cursor']=row[0]if row else None
 cursor.execute('SELECT COALESCE(MAX(seq),0) FROM pf_events')
 result['outbox_max']=cursor.fetchone()[0]
 cursor.execute("SELECT COUNT(*) FROM pf_inbox WHERE status='pending'")
 result['inbox_pending']=cursor.fetchone()[0]
print(encoded(result))
"""


def service_checkpoint_ready(value, expected_outcomes, *, frozen=None):
    """No aggregate count can substitute for these exact committed identities."""
    try:
        ready = (
            value["paused"] is False
            and value["reasoning_hold_present"] is False
            and value["pending"] == value["inbox_pending"] == 0
            and value["feed_cursor"] == value["outbox_max"]
            and value["tables"]["pm_inputs"]["count"] == 2
            and value["tables"]["pm_outcomes"]["count"] == 2
            and value["input_statuses"] == dict.fromkeys(expected_outcomes, "complete")
            and set(value["outcome_statuses"]) == set(expected_outcomes)
            and all(
                value["outcome_statuses"][identifier]["status"] == status
                and type(value["outcome_statuses"][identifier]["dispatches"]) is int
                and value["outcome_statuses"][identifier]["dispatches"] == 0
                for identifier, status in expected_outcomes.items()
            )
        )
        return ready and (frozen is None or value == frozen)
    except (KeyError, TypeError):
        return False


def command(*args, check=True, timeout=180):
    result = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    if check and result.returncode:
        raise RuntimeError("shadow_container_command_failed")
    return result.stdout.strip() if result.returncode == 0 else None


def wait(predicate, seconds=100):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            value = predicate()
            if value:
                return value
        except (RuntimeError, ValueError, KeyError):
            pass
        time.sleep(1)
    raise RuntimeError("shadow_container_wait_timeout")


def main():
    suffix = uuid4().hex[:12]
    network, sql, app = ["pipmind-m4-" + key + "-" + suffix for key in ("net", "sql", "app")]
    configured = os.environ.get("PIPMIND_DATA_DIR", "").strip()
    private = Path(configured).expanduser().resolve() if configured else ROOT / ".data"
    private.mkdir(parents=True, exist_ok=True)
    (private / "shadow-ci" / "summary.json").unlink(missing_ok=True)
    with tempfile.TemporaryDirectory(prefix="m4-", dir=private) as temporary:
        work = Path(temporary).resolve()
        assert work.parent == private.resolve()
        password, token = "Pm4!" + secrets.token_hex(24), secrets.token_hex(32)
        sql_env, app_env = work / "sql.env", work / "shadow.env"
        sql_env.write_text(
            "ACCEPT_EULA=Y\nMSSQL_PID=Developer\nMSSQL_SA_PASSWORD=" + password + "\n"
        )
        app_env.write_text(
            "\n".join(
                (
                    "PIPMIND_SQL_LOCAL=true",
                    "PIPMIND_SQL_SERVER=sql",
                    "PIPMIND_SQL_DATABASE=pipmind_shadow",
                    "PIPMIND_SQL_USER=sa",
                    "PIPMIND_SQL_PASSWORD=" + password,
                    "PIPMIND_SHADOW_TOKEN=" + token,
                    "PIPMIND_FEED_TOKEN=" + token,
                    "PIPMIND_FEED_READS=false",
                    "PIPMIND_SHADOW_RUN_FEEDS=true",
                    "PIPMIND_FEED_BACKEND=sql",
                    "PIPMIND_SHADOW_TRADER=fixture-service-" + suffix,
                    "PIPMIND_MODEL_CALLS=false",
                )
            )
            + "\n"
        )
        sql_env.chmod(0o600)
        app_env.chmod(0o600)

        def sqlcmd(query):
            return command(
                "docker",
                "exec",
                sql,
                "/bin/bash",
                "-c",
                'SQLCMDPASSWORD="$MSSQL_SA_PASSWORD" /opt/mssql-tools18/bin/sqlcmd '
                '-S localhost -U sa -C -b -Q "' + query + '"',
                check=False,
            )

        def job(*args):
            return command(
                "docker",
                "run",
                "--rm",
                "--network",
                network,
                "--env-file",
                str(app_env),
                "--read-only",
                "--cap-drop",
                "ALL",
                "--security-opt",
                "no-new-privileges",
                "--tmpfs",
                "/tmp",
                APP_IMAGE,
                *args,
            )

        def request(path, *, auth=True, body=None):
            # The test token is read inside its own container, never passed in argv/logs.
            code = (
                "import os,json,urllib.request,urllib.error; "
                "headers={'Authorization':'Bearer '+os.environ['PIPMIND_SHADOW_TOKEN']} "
                f"if {auth!r} else {{}}; "
                f"data={body!r}; "
                "data=json.dumps(data).encode() if data is not None else None; "
                "headers['Content-Type']='application/json'; "
                f"req=urllib.request.Request('http://127.0.0.1:8084{path}',"
                "headers=headers,data=data); "
                "\ntry:\n r=urllib.request.urlopen(req,timeout=5); "
                "print(json.dumps({'status':r.status,'body':json.loads(r.read())}))"
                "\nexcept urllib.error.HTTPError as e:\n "
                "print(json.dumps({'status':e.code,'body':json.loads(e.read())}))"
            )
            return json.loads(command("docker", "exec", app, "python", "-c", code))

        def service_checkpoint(expected_outcomes, *, frozen=None):
            last = {}

            def ready():
                nonlocal last
                last = json.loads(
                    command("docker", "exec", app, "python", "-c", service_checkpoint_code())
                )
                return (
                    last
                    if service_checkpoint_ready(last, expected_outcomes, frozen=frozen)
                    else None
                )

            try:
                return wait(ready)
            except RuntimeError:
                # Bounded diagnostics contain counts/status/phase codes, never
                # retained payloads, request tokens, environment or SQL bodies.
                diagnostic = {
                    "error": "service_checkpoint_not_ready_or_changed",
                    "paused": last.get("paused"),
                    "revision": last.get("revision"),
                    "pending": last.get("pending"),
                    "inbox_pending": last.get("inbox_pending"),
                    "feed_cursor": last.get("feed_cursor"),
                    "outbox_max": last.get("outbox_max"),
                    "row_counts": {
                        key: row["count"] for key, row in last.get("tables", {}).items()
                    },
                    "frozen_table_matches": {
                        key: row == frozen["tables"].get(key)
                        for key, row in last.get("tables", {}).items()
                    }
                    if frozen is not None
                    else {},
                    "outcome_statuses": [
                        row["status"] for row in last.get("outcome_statuses", {}).values()
                    ],
                }
                try:
                    response = request("/status")
                    state = response["body"]
                    diagnostic.update(
                        http_status=response["status"],
                        worker_phase=state.get("reasoning_worker", {}).get("phase"),
                        worker_error_type=state.get("reasoning_worker", {}).get("error_type"),
                    )
                except (RuntimeError, ValueError, KeyError):
                    diagnostic["status_probe"] = "unavailable"
                print("FAIL: " + json.dumps(diagnostic, sort_keys=True), flush=True)
                raise

        try:
            command("docker", "network", "create", "--internal", network)
            command(
                "docker",
                "run",
                "-d",
                "--name",
                sql,
                "--network",
                network,
                "--network-alias",
                "sql",
                "--env-file",
                str(sql_env),
                "--memory",
                "3g",
                SQL_IMAGE,
            )
            wait(lambda: sqlcmd("SELECT 1;"))
            print("PASS: isolated SQL Server ready", flush=True)
            if sqlcmd("CREATE DATABASE pipmind_shadow;") is None:
                raise RuntimeError("sql_test_database_failed")
            summary = json.loads(job("python", "-m", "pipmind.shadow", "replay"))
            assert summary["dispatches"] == 0 and all(summary["checks"].values())
            inventory = json.loads((ROOT / "evals/shadow/scenario-v1.json").read_text())
            assert set(summary["checks"]) == set(inventory["additional_gates"])
            development_image = (ROOT / "Dockerfile").read_text().splitlines()[0].split()[1]
            checks = subprocess.run(
                [
                    "docker",
                    "run",
                    "--rm",
                    "--network",
                    network,
                    "--env-file",
                    str(app_env),
                    "-e",
                    "PIPMIND_SQL_TESTS=true",
                    "-v",
                    str(ROOT) + ":/workspace",
                    "-w",
                    "/workspace",
                    development_image,
                    ".venv/bin/python",
                    "-m",
                    "pytest",
                    "tests/test_shadow_sql.py",
                    "tests/test_safety_recovery_managed_sql.py",
                    "-q",
                ],
                capture_output=True,
                text=True,
                timeout=300,
            )
            print(checks.stdout[-6000:], flush=True)
            if checks.returncode:
                raise RuntimeError("shadow_sql_integration_tests_failed")
            # File staging is a private disk bind, never RAM-backed /tmp.
            stream = command(
                "docker",
                "run",
                "--rm",
                "--network",
                network,
                "--env-file",
                str(app_env),
                "--user",
                f"{os.getuid()}:{os.getgid()}",
                "--memory",
                "512m",
                "--cpus",
                "0.25",
                "--read-only",
                "--cap-drop",
                "ALL",
                "--security-opt",
                "no-new-privileges",
                "--tmpfs",
                "/tmp:rw,noexec,nosuid,size=1m,mode=1777",
                "-v",
                str(work) + ":/exports",
                "-v",
                str(ROOT / "scripts") + ":/checks:ro",
                APP_IMAGE,
                "python",
                "-c",
                "import runpy; runpy.run_path('/checks/stream_smoke.py',run_name='__main__')",
                timeout=600,
            )
            summary["stream_experience"] = json.loads(stream)
            print("PASS: complete streaming SQL clone within existing demo allocation", flush=True)
            # Unit tests intentionally retain inbox/outbox history. Their pending
            # captures can normalize even with external reads disabled, so the
            # restart fixture gets its own database on the SAME local SQL server.
            if sqlcmd(f"CREATE DATABASE {SERVICE_DATABASE};") is None:
                raise RuntimeError("sql_service_database_failed")
            environment = app_env.read_text()
            previous = "PIPMIND_SQL_DATABASE=pipmind_shadow\n"
            assert environment.count(previous) == 1
            app_env.write_text(
                environment.replace(previous, f"PIPMIND_SQL_DATABASE={SERVICE_DATABASE}\n")
            )
            code = (
                "import os; from pipmind.shadow.sql import connector,migrate,fulltext; "
                "from pipmind.shadow.store import SqlMind; "
                "from pipmind.shadow.common import now,digest; "
                "from pipmind.shadow.fixtures import IDENTITY; "
                "assert os.environ.get('PIPMIND_SQL_LOCAL')=='true'; "
                f"assert os.environ['PIPMIND_SQL_DATABASE']=={SERVICE_DATABASE!r}; "
                "c=connector(); migrate(c); fulltext(c); "
                "s=SqlMind(c,os.environ['PIPMIND_SHADOW_TRADER']); "
                "s.initialize(IDENTITY,{'version':'v1','text':'Synthetic'},now().isoformat(),"
                "binding=digest('synthetic-service')); s.set_paused(False)"
            )
            job("python", "-c", code)
            command(
                "docker",
                "run",
                "-d",
                "--name",
                app,
                "--network",
                network,
                "--env-file",
                str(app_env),
                "--read-only",
                "--cpus",
                "0.25",
                "--memory",
                "512m",
                "--cap-drop",
                "ALL",
                "--security-opt",
                "no-new-privileges",
                "--tmpfs",
                "/tmp",
                APP_IMAGE,
                "python",
                "-m",
                "pipmind.shadow",
                "serve-fixture",
                "--host",
                "0.0.0.0",
            )
            assert wait(lambda: request("/healthz", auth=False))["body"]["trading"] is False
            print("PASS: private shadow service ready", flush=True)
            assert request("/status", auth=False)["status"] == 401
            assert request("/orders", body={})["status"] == 404
            assert request("/docs")["status"] == 404
            assert request("/feeds/status", auth=False)["status"] == 401
            workers = wait(lambda: request("/feeds/status")["body"].get("worker_heartbeats"))
            assert set(workers) == {"macro", "news", "social"}
            wait(
                lambda: all(
                    value
                    for value in request("/feeds/status")["body"]["worker_heartbeats"].values()
                )
            )
            # Exercise the actual SQL outbox -> local consumer -> shadow journal path.
            news_identifier = "smoke-news:" + suffix
            command(
                "docker",
                "exec",
                app,
                "python",
                "-c",
                "from pipmind.feeds.sql_store import SqlFeedStore; "
                "from pipmind.shadow.sql import connector; "
                "from pipmind.feeds.fixtures import pulse; from pipmind.shadow.common import now; "
                "s=SqlFeedStore(connector()); t=now(); p=pulse('news',t); "
                f"p['event_id']={news_identifier!r}; "
                "\nwith s.transaction() as d: s.emit(d,'news',p,t)",
            )
            from datetime import UTC, datetime

            moment = datetime.now(UTC).isoformat().replace("+00:00", "Z")
            identifier = "smoke:" + suffix
            event = {
                "schema_version": 1,
                "kind": "ClockEvent",
                "message_id": identifier,
                "available_at": moment,
                "payload": {
                    "schema_version": 1,
                    "event_id": identifier,
                    "ts": moment,
                    "reason": "daily_brief",
                },
            }
            packet = {"source": "smoke", "events": [{"sequence": 1, "event": event}]}
            assert request("/inputs", body=packet)["status"] == 200
            expected_outcomes = {news_identifier: "analysis_retained", identifier: "observed"}
            service_checkpoint(expected_outcomes)
            memory = []
            memory_probe = (
                "from pathlib import Path; "
                "paths=['/sys/fs/cgroup/memory.current',"
                "'/sys/fs/cgroup/memory/memory.usage_in_bytes']; "
                "print(next(int(Path(p).read_text()) for p in paths if Path(p).exists()))"
            )
            for _ in range(6):
                memory.append(int(command("docker", "exec", app, "python", "-c", memory_probe)))
                assert request("/status")["body"]["pending"] == 0
                time.sleep(5)
            # Leave room for real inference/SQL bursts and diagnostic processes.
            # This is a regression gate, not full-day capacity certification.
            assert max(memory) < 448 * 1024 * 1024, "shadow_memory_headroom_exhausted"
            print(
                f"PASS: warm SDK and isolated workers peak {max(memory) // 1048576} MiB", flush=True
            )
            # Freeze the complete quiet source AFTER warming, rather than comparing
            # restart state against an outcome count sampled before later commits.
            before = service_checkpoint(expected_outcomes)
            command("docker", "restart", "--time", "10", app)
            wait(lambda: request("/healthz", auth=False))
            service_checkpoint(expected_outcomes, frozen=before)
            command("docker", "stop", "--time", "10", sql)
            packet["events"][0]["sequence"] = 2
            assert request("/inputs", body=packet)["status"] == 503
            assert request("/healthz", auth=False)["status"] == 200
            command("docker", "start", sql)
            wait(lambda: sqlcmd("SELECT 1;"))
            # SQL accepting server-level queries does not imply that the
            # application database has completed recovery. Verify the actual
            # authenticated state within the same bounded readiness window.
            service_checkpoint(expected_outcomes, frozen=before)
            wait(
                lambda: all(
                    row["alive"] for row in request("/feeds/status")["body"]["workers"].values()
                )
            )
            config = json.loads(command("docker", "inspect", app))[0]
            assert config["Config"]["User"] == "10001:10001"
            assert config["HostConfig"]["ReadonlyRootfs"]
            assert config["HostConfig"]["Memory"] == 536870912
            summary["container_checks"] = {
                "authenticated_flow": True,
                "service_restart": True,
                "sql_outage_refusal": True,
                "sql_restart": True,
                "nonroot_readonly": True,
                "isolated_network": True,
                "demo_allocation": True,
                "integrated_feed_workers": True,
                "sql_feed_handoff": True,
                "named_events_completed": True,
                "restart_exact_source_checkpoint": True,
                "service_database_isolated": True,
            }
            output = private / "shadow-ci"
            output.mkdir(exist_ok=True)
            (output / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
            print("PASS: SQL shadow replay, private API, process/SQL restarts and zero dispatches")
        finally:
            command("docker", "rm", "-f", app, sql, check=False)
            command("docker", "network", "rm", network, check=False)


if __name__ == "__main__":
    main()
