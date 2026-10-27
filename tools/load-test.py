#!/usr/bin/env python3
"""
Load test for CTFd + docker_challenges: N fake players start their instance at the same moment.

Run on the PLATFORM server (only needs python3, no extra packages):

    export CTFD_TOKEN=ctfd_xxx          # CTFd -> Settings -> Access Tokens -> Generate (as admin)
    python3 load-test.py                # 25 players, all visible docker challenges

Options:
    --url http://localhost      CTFd address (default: http://localhost)
    --players 25                how many fake players
    --chal 3,4                  only these challenge IDs (default: every visible docker challenge)
    --map PUBLIC=PRIVATE        reach instances via a private IP, e.g. --map 34.2.151.22=10.148.0.5
                                (needed if ctf-chal-ports only allows your own IP); repeatable
    --no-probe                  don't check that instances answer
    --keep                      leave instances running at the end
    --delete-users              delete the fake players at the end

Fake players are named loadtest-01..NN, created HIDDEN (not on the scoreboard), password random.
Users mode only (the internal CTF). Safe to rerun: existing loadtest-* players are reused.
"""
import argparse
import http.cookiejar
import json
import os
import re
import secrets
import socket
import statistics
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

LOGIN_BATCH = 9    # CTFd: max 10 logins per IP, counter clears after 5 s with no logins
LOGIN_PAUSE = 5.5


class Api:
    """Tiny HTTP client: admin token OR a logged-in browser-like session."""

    def __init__(self, base, token=None):
        self.base = base.rstrip("/")
        self.token = token
        self.nonce = None
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.jar), urllib.request.ProxyHandler({})
        )

    def raw(self, method, path, data=None, json_body=None, timeout=60):
        headers = {}
        body = None
        if json_body is not None:
            body = json.dumps(json_body).encode()
            headers["Content-Type"] = "application/json"
            if self.nonce:
                headers["CSRF-Token"] = self.nonce
        elif data is not None:
            body = urllib.parse.urlencode(data).encode()
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        if self.token:
            headers["Authorization"] = "Token " + self.token
            headers.setdefault("Content-Type", "application/json")
        req = urllib.request.Request(self.base + path, data=body, headers=headers, method=method)
        try:
            with self.opener.open(req, timeout=timeout) as r:
                return r.status, r.read().decode(errors="replace")
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode(errors="replace")

    def json(self, method, path, body=None, timeout=60):
        status, text = self.raw(method, path, json_body=body if body is not None else ({} if method != "GET" else None), timeout=timeout)
        try:
            return status, json.loads(text)
        except ValueError:
            return status, {"message": text[:200]}

    def login(self, name, password):
        _, page = self.raw("GET", "/login")
        m = re.search(r"csrfNonce['\"]?\s*:\s*\"([0-9a-f]+)\"", page)
        if not m:
            return False
        status, _ = self.raw("POST", "/login", data={"name": name, "password": password, "nonce": m.group(1)})
        _, page = self.raw("GET", "/challenges")
        m = re.search(r"csrfNonce['\"]?\s*:\s*\"([0-9a-f]+)\"", page)
        if not m or "/logout" not in page:
            return False
        self.nonce = m.group(1)
        return True


def die(msg):
    print("ERROR:", msg)
    sys.exit(1)


def message(resp):
    if isinstance(resp, dict):
        return str(resp.get("message") or resp.get("errors") or resp)[:120]
    return str(resp)[:120]


def tcp_open(host, port, timeout=3):
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return True
    except OSError:
        return False


def main():
    ap = argparse.ArgumentParser(description="CTFd docker_challenges load test")
    ap.add_argument("--url", default="http://localhost")
    ap.add_argument("--players", type=int, default=25)
    ap.add_argument("--chal", default="")
    ap.add_argument("--map", action="append", default=[])
    ap.add_argument("--no-probe", action="store_true")
    ap.add_argument("--keep", action="store_true")
    ap.add_argument("--delete-users", action="store_true")
    args = ap.parse_args()

    token = os.environ.get("CTFD_TOKEN")
    if not token:
        die("set CTFD_TOKEN first (CTFd -> Settings -> Access Tokens -> Generate, as admin)")
    ipmap = dict(m.split("=", 1) for m in args.map)

    admin = Api(args.url, token=token)

    # --- checks -----------------------------------------------------------
    status, conf = admin.json("GET", "/api/v1/configs/user_mode")
    if status != 200:
        die("admin token rejected or wrong --url (%s): %s" % (status, message(conf)))
    if conf["data"]["value"] != "users":
        die("this script supports Users mode only (CTFd is in %s mode)" % conf["data"]["value"])

    status, chals = admin.json("GET", "/api/v1/challenges?view=admin")
    docker = []
    for c in chals.get("data", []):
        if c.get("type") != "docker":
            continue
        _, full = admin.json("GET", "/api/v1/challenges/%d" % c["id"])
        d = full["data"]
        if args.chal and str(d["id"]) not in args.chal.split(","):
            continue
        if not args.chal and d.get("state") != "visible":
            continue
        docker.append(d)
    if not docker:
        die("no docker challenges found (make one visible, or pass --chal ID)")
    print("Challenges: " + ", ".join("#%d %s" % (d["id"], d["name"]) for d in docker))

    # --- players ----------------------------------------------------------
    print("\n[1/4] Creating %d hidden players..." % args.players)
    password = secrets.token_urlsafe(12)
    players = []
    for i in range(1, args.players + 1):
        name = "loadtest-%02d" % i
        status, r = admin.json("POST", "/api/v1/users", {
            "name": name, "email": "%s@loadtest.invalid" % name, "password": password,
            "hidden": True, "verified": True,
        })
        if status == 200:
            uid = r["data"]["id"]
        else:  # already exists -> reuse, reset password
            _, found = admin.json("GET", "/api/v1/users?view=admin&field=name&q=" + name)
            match = [u for u in found.get("data", []) if u["name"] == name]
            if not match:
                die("cannot create or find %s: %s" % (name, message(r)))
            uid = match[0]["id"]
            admin.json("PATCH", "/api/v1/users/%d" % uid, {"password": password, "hidden": True})
        players.append({"name": name, "id": uid, "chal": docker[(i - 1) % len(docker)]})

    print("[2/4] Logging them in (in batches, CTFd rate-limits logins)...")
    for i, p in enumerate(players):
        if i and i % LOGIN_BATCH == 0:
            time.sleep(LOGIN_PAUSE)
        p["api"] = Api(args.url)
        if not p["api"].login(p["name"], password):
            time.sleep(LOGIN_PAUSE)  # rate limited by something else? wait and retry once
            p["api"] = Api(args.url)
            if not p["api"].login(p["name"], password):
                die("login failed for %s" % p["name"])

    # stop anything left over from a previous run (stop is allowed any time)
    for p in players:
        p["api"].json("POST", "/api/v1/container", {"id": p["chal"]["id"], "stop": True})

    # --- the actual burst -------------------------------------------------
    print("[3/4] %d players press Start at the same moment..." % len(players))
    barrier = threading.Barrier(len(players))

    def start(p):
        barrier.wait()
        t0 = time.time()
        status, r = p["api"].json("POST", "/api/v1/container", {"id": p["chal"]["id"]}, timeout=120)
        p["secs"] = time.time() - t0
        p["ok"] = status == 200
        p["err"] = None if p["ok"] else "%s %s" % (status, message(r))

    threads = [threading.Thread(target=start, args=(p,)) for p in players]
    t_all = time.time()
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    wall = time.time() - t_all

    # where did each one land?
    for p in players:
        p["addr"] = None
        if p["ok"]:
            _, s = p["api"].json("GET", "/api/v1/docker_status")
            for item in s.get("data", []):
                if str(item.get("challenge_id")) == str(p["chal"]["id"]) and item.get("ports"):
                    p["addr"] = (item["host"], item["ports"][0])

    if not args.no_probe:
        print("[4/4] Checking every instance answers...")
        time.sleep(2)  # give services a moment to start listening
        for p in players:
            if p["addr"]:
                host, port = p["addr"]
                p["reach"] = tcp_open(ipmap.get(host, host), port)

    # --- report -----------------------------------------------------------
    ok = [p for p in players if p["ok"]]
    times = sorted(p["secs"] for p in ok)
    print("\n================ RESULT ================")
    print("Started OK      : %d / %d" % (len(ok), len(players)))
    if times:
        p95 = times[min(len(times) - 1, int(round(0.95 * len(times))) - 1)]
        print("Start time      : median %.1fs, 95%% %.1fs, slowest %.1fs" % (statistics.median(times), p95, times[-1]))
    print("Whole burst     : %.1fs" % wall)
    if not args.no_probe:
        reach = [p for p in ok if p.get("reach")]
        print("Reachable       : %d / %d" % (len(reach), len(ok)))
    ports = [p["addr"] for p in ok if p["addr"]]
    if len(ports) != len(set(ports)):
        print("!! Two players got the SAME host:port")
    by_host = {}
    for p in ok:
        if p["addr"]:
            by_host.setdefault(p["addr"][0], 0)
            by_host[p["addr"][0]] += 1
    for h, n in sorted(by_host.items()):
        print("  on %-15s : %d containers" % (h, n))
    errors = {}
    for p in players:
        if p["err"]:
            errors.setdefault(p["err"], 0)
            errors[p["err"]] += 1
        elif not args.no_probe and p["ok"] and not p.get("reach"):
            errors.setdefault("started but not reachable from here", 0)
            errors["started but not reachable from here"] += 1
    for e, n in errors.items():
        print("  %2dx %s" % (n, e))
    if not args.no_probe and any(p["ok"] and not p.get("reach") for p in players):
        print("  (if ctf-chal-ports only allows your own IP, rerun with --map <public-ip>=<private-ip>)")

    print("\nNow, on each challenge server, look at:  sudo docker stats --no-stream ; free -h")
    if not args.keep:
        try:
            input("Press Enter to stop all test instances... ")
        except EOFError:
            pass
        for p in players:
            p["api"].json("POST", "/api/v1/container", {"id": p["chal"]["id"], "stop": True})
        print("Stopped.")
    if args.delete_users:
        for p in players:
            admin.json("DELETE", "/api/v1/users/%d" % p["id"])
        print("Deleted %d loadtest players." % len(players))

    good = len(ok) == len(players) and (args.no_probe or all(p.get("reach") for p in ok))
    print("\nPASS" if good else "\nFAIL (see above)")
    sys.exit(0 if good else 1)


if __name__ == "__main__":
    main()
