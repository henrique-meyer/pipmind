"""Exercise isolated workers in the real non-root feed container, without network access."""

import os
import secrets
import tempfile
import time
from pathlib import Path

from gateway_smoke import docker


def main():
    name = "pipmind-feeds-smoke-" + secrets.token_hex(4)
    token = secrets.token_urlsafe(32)
    with tempfile.TemporaryDirectory(prefix="pipmind-feeds-") as temp:
        directory = Path(temp)
        os.chmod(directory, 0o777)
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
                "PIPMIND_FEED_MODE=fixture",
                "--env",
                "PIPMIND_FEED_STORE=/state/events.sqlite3",
                "--env",
                f"PIPMIND_FEED_TOKEN={token}",
                "pipmind-feeds:ci",
            )
            probe = "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8082/healthz',timeout=2).status)"
            for _attempt in range(30):
                try:
                    if docker("exec", name, "python", "-c", probe) == "200":
                        break
                except Exception:
                    time.sleep(0.25)
            else:
                raise RuntimeError("feed_container_startup_failed")
            exercise = """
import json,os,signal,time,urllib.request,urllib.error
base='http://127.0.0.1:8082'
try:
    urllib.request.urlopen(base+'/status')
    raise AssertionError('missing_authentication')
except urllib.error.HTTPError as error:
    assert error.code==401
headers={'Authorization':'Bearer '+os.environ['PIPMIND_FEED_TOKEN']}
def state():
    request=urllib.request.Request(base+'/status',headers=headers)
    return json.load(urllib.request.urlopen(request,timeout=5))
def wait(condition):
    deadline=time.monotonic()+15
    while time.monotonic()<deadline:
        value=state()
        if condition(value):return value
        time.sleep(0.1)
    raise AssertionError('feed_isolation_timeout')
wait(lambda s:len(s['events'])==3)
for victim in ['macro','news','social']:
    before=state()
    pid=before['workers'][victim]['pid']
    assert pid>1
    os.kill(pid,signal.SIGKILL)
    wait(lambda s:all(s['events'].get(k,0)>v for k,v in before['events'].items() if k!=victim))
    wait(lambda s:s['workers'][victim]['pid']!=pid and
         s['events'].get(victim,0)>before['events'][victim])
result=json.load(urllib.request.urlopen(urllib.request.Request(base+'/events',headers=headers)))
kinds={'macro':'ReleaseEvent','news':'NewsEvent','social':'PostEvent'}
assert all(row['event']['kind']==kinds[row['producer']] for row in result['events'])
print('auth and three isolated kill/restart flows passed; no trading')
"""
            print(docker("exec", name, "python", "-c", exercise))
        finally:
            docker("rm", "-f", name)


if __name__ == "__main__":
    main()
