"""Exercise the actual dry gateway image with networking disabled and persistent local state."""

import json
import os
import secrets
import subprocess
import tempfile
import time
from pathlib import Path


def docker(*args):
    return subprocess.check_output(["docker", *args], text=True, stderr=subprocess.DEVNULL).strip()


def main():
    name = "pipmind-gateway-smoke-" + secrets.token_hex(4)
    token = secrets.token_urlsafe(32)
    with tempfile.TemporaryDirectory(prefix="pipmind-gateway-") as temp:
        directory = Path(temp)
        os.chmod(directory, 0o777)  # Disposable synthetic state; image runs as uid 10001.
        (directory / "symbols.local.json").write_text(
            json.dumps(
                [
                    {
                        "instrument": "EUR/USD",
                        "market_id": 42,
                        "unit": "base_units",
                        "quantity_factor": "1",
                        "increment": "1000",
                        "minimum": "1000",
                    }
                ]
            )
        )
        try:
            docker(
                "run",
                "-d",
                "--name",
                name,
                "--network",
                "none",
                "--read-only",
                "--cap-drop",
                "ALL",
                "--security-opt",
                "no-new-privileges",
                "--tmpfs",
                "/tmp",
                "--mount",
                f"type=bind,source={directory},target=/state",
                "--env",
                f"PIPMIND_GATEWAY_TOKEN={token}",
                "--env",
                "PIPMIND_GATEWAY_SYMBOLS=/state/symbols.local.json",
                "--env",
                "PIPMIND_GATEWAY_LEDGER=/state/commands.sqlite3",
                "pipmind-gateway:ci",
            )
            probe = "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8081/healthz',timeout=2).status)"
            for _attempt in range(30):
                try:
                    if docker("exec", name, "python", "-c", probe) == "200":
                        break
                except subprocess.CalledProcessError:
                    time.sleep(0.25)
            else:
                raise RuntimeError("dry_gateway_startup_failed")
            exercise = """
import json,os,urllib.request,urllib.error,pathlib
base='http://127.0.0.1:8081'
assert not json.load(urllib.request.urlopen(base+'/healthz'))['dispatch_enabled']
try:
    urllib.request.urlopen(base+'/state')
    raise AssertionError('missing_authentication')
except urllib.error.HTTPError as error:
    assert error.code==401
request={'schema_version':1,'client_order_id':'synthetic-smoke','action':'open',
         'instrument':'EUR/USD','side':'buy','size':1000,'order_type':'market'}
headers={'Authorization':'Bearer '+os.environ['PIPMIND_GATEWAY_TOKEN'],
         'Content-Type':'application/json'}
def accept(body):
    return json.load(urllib.request.urlopen(urllib.request.Request(base+'/orders',
        data=json.dumps(body).encode(),headers=headers)))
first=accept(request)
second=accept(request)
assert second['replayed'] and first['acknowledgement']==second['acknowledgement']
assert first['acknowledgement']['dispatch_state']=='not_sent'
assert first['acknowledgement']['mode']=='dry'
assert not pathlib.Path('/app/pipmind/smoke-private.pipmind').exists()
assert not pathlib.Path('/app/AGENTS.md').exists()
print('replayed' if first['replayed'] else 'accepted')
"""
            assert docker("exec", name, "python", "-c", exercise) == "accepted"
            docker("restart", name)
            for _attempt in range(30):
                try:
                    if docker("exec", name, "python", "-c", probe) == "200":
                        break
                except subprocess.CalledProcessError:
                    time.sleep(0.25)
            assert docker("exec", name, "python", "-c", exercise) == "replayed"
            print(
                "Dry gateway container: auth, acceptance, durable restart replay; zero dispatches."
            )
        finally:
            docker("rm", "-f", name)


if __name__ == "__main__":
    main()
