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
    private = ROOT / ".data"
    private.mkdir(exist_ok=True)
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
            code = (
                "import os; from pipmind.shadow.sql import connector; "
                "from pipmind.shadow.store import SqlMind; "
                "from pipmind.shadow.common import now,digest; "
                "from pipmind.shadow.fixtures import IDENTITY; "
                "s=SqlMind(connector(),os.environ['PIPMIND_SHADOW_TRADER']); "
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
            command(
                "docker",
                "exec",
                app,
                "python",
                "-c",
                "from pipmind.feeds.sql_store import SqlFeedStore; "
                "from pipmind.shadow.sql import connector; "
                "from pipmind.feeds.fixtures import pulse; from pipmind.shadow.common import now; "
                "s=SqlFeedStore(connector()); t=now(); "
                "\nwith s.transaction() as d: s.emit(d,'news',pulse('news',t),t)",
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
            wait(lambda: request("/status")["body"]["pending"] == 0)
            before = request("/status")["body"]["outcomes"]
            assert before >= 2
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
            command("docker", "restart", "--time", "10", app)
            wait(lambda: request("/healthz", auth=False))
            wait(lambda: request("/status")["body"].get("outcomes") == before)
            command("docker", "stop", "--time", "10", sql)
            packet["events"][0]["sequence"] = 2
            assert request("/inputs", body=packet)["status"] == 503
            assert request("/healthz", auth=False)["status"] == 200
            command("docker", "start", sql)
            wait(lambda: sqlcmd("SELECT 1;"))
            # SQL accepting server-level queries does not imply that the
            # application database has completed recovery. Verify the actual
            # authenticated state within the same bounded readiness window.
            wait(lambda: request("/status")["body"].get("outcomes") == before)
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
