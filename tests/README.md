# Plugin tests

End-to-end tests for the multi-host fork, run against a real CTFd checkout and two fake Docker hosts
(no real Docker needed). 195 checks: users mode, teams mode, port clashes, sweep, locks, dead instances,
per-host time settings, DB upgrade, popup images.

## Run

```bash
# 1. CTFd 3.7.7 + this plugin
git clone -b 3.7.7 https://github.com/CTFd/CTFd.git && cd CTFd
python3.11 -m venv ../venv && ../venv/bin/pip install -r requirements.txt -r development.txt
cp -r ../CTFd-Docker-Challenges/docker_challenges CTFd/plugins/
cp ../CTFd-Docker-Challenges/tests/*.py .

# 2. TLS certs for fake host B in ../certs (ca.pem, server-cert.pem, server-key.pem, cert.pem, key.pem)
#    e.g. run setup-docker-tls.sh's openssl steps with CN=127.0.0.1, or any self-made CA

# 3. two fake Docker hosts (separate terminals)
../venv/bin/python fake_docker.py A 2401
../venv/bin/python fake_docker.py B 2402 "$(realpath ../certs)"

# 4. tests
PYTHONPATH=. ../venv/bin/python test_multihost.py     # ends with: RESULT: 195 passed, 0 failed
```

Restart the fake hosts between runs if a run was interrupted (leftover containers break the counts).
