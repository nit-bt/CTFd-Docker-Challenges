"""End-to-end test of the multi-host docker_challenges fork against 2 fake Docker hosts.

hostA: http  127.0.0.1:2401
hostB: https 127.0.0.1:2402 (client cert required)
"""
import io
import os
import time

import requests

from CTFd.models import Challenges, Flags, db
from tests.helpers import create_ctfd, destroy_ctfd, gen_team, gen_user, login_as_user

os.environ["NO_PROXY"] = "127.0.0.1,localhost"
CERTS = os.path.join(os.path.dirname(__file__), "..", "certs")
OK, FAIL = 0, 0


def check(cond, msg):
    global OK, FAIL
    if cond:
        OK += 1
        print("  PASS", msg)
    else:
        FAIL += 1
        print("  FAIL", msg)


def fake(port, tls=False):
    if tls:
        return requests.get(
            "https://127.0.0.1:%d/_debug" % port,
            cert=(f"{CERTS}/cert.pem", f"{CERTS}/key.pem"),
            verify=f"{CERTS}/ca.pem",
        ).json()
    return requests.get("http://127.0.0.1:%d/_debug" % port).json()


def nonce(client):
    with client.session_transaction() as s:
        return s["nonce"]


def add_host(admin, data, files=None, host_id=None):
    form = dict(data)
    form["nonce"] = nonce(admin)
    form["action"] = "save"
    for k, path in (files or {}).items():
        form[k] = (open(path, "rb"), os.path.basename(path))
    url = "/admin/docker_config" + ("?id=%d" % host_id if host_id else "")
    return admin.post(url, data=form, content_type="multipart/form-data")


def age_trackers(app, seconds):
    from CTFd.plugins.docker_challenges import DockerChallengeTracker

    with app.app_context():
        for t in DockerChallengeTracker.query.all():
            t.timestamp = int(t.timestamp) - seconds
            t.revert_time = int(t.revert_time) - seconds
            if t.expires is not None:
                t.expires = int(t.expires) - seconds
        db.session.commit()


def setup_hosts_and_chals(app):
    admin = login_as_user(app, name="admin")
    r = admin.get("/admin/docker_config")
    check(r.status_code == 200, "docker config page loads (no hosts)")

    r = add_host(admin, {"name": "web-srv", "hostname": "127.0.0.1:2401", "public_host": "chal1.example.com",
                         "tls_enabled": "False", "port_min": "30000", "port_max": "30010",
                         "mem_limit_mb": "256", "cpu_limit": "0.5", "pids_limit": "128"})
    check(r.status_code == 302, "add host A (http)")
    r = add_host(admin, {"name": "crypto-srv", "hostname": "127.0.0.1:2402", "public_host": "chal2.example.com",
                         "tls_enabled": "True", "port_min": "30500", "port_max": "30510",
                         "mem_limit_mb": "128", "cpu_limit": "0.25", "pids_limit": "64"},
                 files={"ca_cert": f"{CERTS}/ca.pem", "client_cert": f"{CERTS}/cert.pem",
                        "client_key": f"{CERTS}/key.pem"})
    check(r.status_code == 302, "add host B (TLS + client certs)")

    r = admin.get("/admin/docker_config?id=1")
    body = r.get_data(as_text=True)
    check(body.count(">UP<") == 2, "both hosts show UP (http + TLS ping work)")
    check("ctf/web1" in body and "other/junk" in body, "host A lists its images")

    # allow only ctf/* repos
    for hid in (1, 2):
        r = admin.post("/admin/docker_config?id=%d" % hid,
                       data={"nonce": nonce(admin), "action": "save",
                             "name": ["web-srv", "crypto-srv"][hid - 1],
                             "hostname": ["127.0.0.1:2401", "127.0.0.1:2402"][hid - 1],
                             "public_host": ["chal1.example.com", "chal2.example.com"][hid - 1],
                             "tls_enabled": ["False", "True"][hid - 1],
                             "port_min": ["30000", "30500"][hid - 1], "port_max": ["30010", "30510"][hid - 1],
                             "mem_limit_mb": "256", "cpu_limit": "0.5", "pids_limit": "128",
                             "repositories": ["ctf/web1", "ctf/crypto1"]})
        check(r.status_code == 302, "save allowed repos on host %d" % hid)

    r = admin.get("/api/v1/docker_hosts")
    check(r.status_code == 200 and len(r.get_json()["data"]) == 2, "API lists 2 hosts")
    r = admin.get("/api/v1/docker?host_id=1")
    names = [d["name"] for d in r.get_json()["data"]]
    check(sorted(names) == ["ctf/crypto1:latest", "ctf/web1:latest"], "host A images filtered to allowed repos: %s" % names)
    r = admin.get("/api/v1/docker")
    check(r.status_code == 400, "image list without host_id -> 400")

    r = admin.get("/admin/challenges/new")
    check(r.status_code == 200, "challenge create page loads")
    r = admin.get("/plugins/docker_challenges/assets/create.html")
    check(r.status_code == 200 and "dockerhost_select" in r.get_data(as_text=True), "create form has host dropdown")

    ids = []
    for name, cat, host, img, flag in [("Web 1", "web", 1, "ctf/web1:latest", "flag{web}"),
                                       ("Crypto 1", "crypto", 2, "ctf/crypto1:latest", "flag{crypto}")]:
        r = admin.post("/api/v1/challenges", json={
            "name": name, "category": cat, "description": "d", "value": 100, "state": "visible",
            "type": "docker", "docker_host_id": host, "docker_image": img,
            "connection_info": "http://host:port"})
        check(r.status_code == 200, "create docker challenge %s -> %s" % (name, r.status_code))
        cid = r.get_json()["data"]["id"]
        ids.append(cid)
        admin.post("/api/v1/flags", json={"challenge": cid, "content": flag, "type": "static"})

    r = admin.get("/api/v1/challenges/%d" % ids[0])
    d = r.get_json()["data"]
    check(d.get("docker_host_id") == 1 and d.get("docker_image") == "ctf/web1:latest", "read() returns host + image")
    r = admin.get("/admin/challenges/%d" % ids[0])
    check(r.status_code == 200, "admin challenge edit page loads")

    # update: move challenge 1 to host 2 and back
    r = admin.patch("/api/v1/challenges/%d" % ids[0], json={"docker_host_id": 2})
    check(r.status_code == 200, "update challenge host")
    r = admin.patch("/api/v1/challenges/%d" % ids[0], json={"docker_host_id": 1})
    return admin, ids


def run_users_mode():
    print("\n== USERS MODE ==")
    app = create_ctfd(enable_plugins=True)
    with app.app_context():
        admin, (web, crypto) = setup_hosts_and_chals(app)
        gen_user(app.db, name="alice", email="alice@x.com")
        gen_user(app.db, name="bob", email="bob@x.com")
        alice = login_as_user(app, name="alice")
        bob = login_as_user(app, name="bob")

        r = alice.get("/api/v1/challenges/%d" % web)
        check(r.status_code == 200 and "Start Docker Instance" in r.get_json()["data"]["view"], "player sees start button")

        r = alice.get("/api/v1/container?id=%d" % web)
        check(r.status_code in (404, 405), "GET start is refused -> %s" % r.status_code)
        r = alice.post("/api/v1/container", data={"id": web})
        check(r.status_code == 403, "POST without CSRF token refused (403) -> %s" % r.status_code)
        r = alice.post("/api/v1/container", json={"id": web}, headers={"CSRF-Token": "wrong"})
        check(r.status_code == 403, "POST with WRONG CSRF token refused -> %s" % r.status_code)
        r = admin.get("/api/v1/nuke?all=true")
        check(r.status_code in (404, 405), "GET nuke is refused -> %s" % r.status_code)
        r = admin.post("/api/v1/nuke", data={"all": "true"})
        check(r.status_code == 403, "nuke without CSRF token refused (403)")
        r = alice.post("/api/v1/nuke", json={"all": True})
        check(r.status_code in (302, 403), "player cannot nuke -> %s" % r.status_code)

        before = len(fake(2401)["containers"])
        t0 = time.time()
        r = alice.post("/api/v1/container", json={"id": web})
        check(r.status_code == 200, "alice starts Web 1 -> %s %s" % (r.status_code, r.get_data(as_text=True)[:100]))
        a = fake(2401)
        check(len(a["containers"]) == before + 1, "container created on host A")
        c = a["containers"][-1]
        port = int(list(c["HostConfig"]["PortBindings"].values())[0][0]["HostPort"])
        check(30000 <= port <= 30010, "port %d within host A range 30000-30010" % port)
        check(c["HostConfig"]["Memory"] == 256 * 1024 * 1024 and c["HostConfig"]["NanoCpus"] == 500000000
              and c["HostConfig"]["PidsLimit"] == 128
              and c["HostConfig"].get("SecurityOpt") == ["no-new-privileges"], "limits + no-new-privileges applied")
        check(len(fake(2402, tls=True)["containers"]) == 0, "nothing on host B")

        r = alice.get("/api/v1/docker_status")
        s = r.get_json()["data"]
        check(len(s) == 1 and s[0]["host"] == "chal1.example.com" and s[0]["ports"] == [str(port)],
              "status shows PUBLIC host chal1.example.com:%d" % port)

        r = alice.post("/api/v1/container", json={"id": crypto})
        check(r.status_code == 403 and "only run one" in r.get_data(as_text=True), "one container per user enforced")
        r = alice.post("/api/v1/container", json={"id": web})
        check(r.status_code == 403 and "Revert unlocks 5 min" in r.get_data(as_text=True), "revert blocked before 5 min")
        n = len(fake(2401)["containers"])
        r = alice.post("/api/v1/container", json={"id": web, "stop": True})
        check(r.status_code == 200 and len(fake(2401)["containers"]) == n - 1, "STOP works immediately (no 5 min wait)")
        r = alice.post("/api/v1/container", json={"id": web})
        check(r.status_code == 403 and "wait" in r.get_data(as_text=True), "30s cooldown after stop -> %s" % r.get_data(as_text=True)[:80])
        r = alice.post("/api/v1/container", json={"id": web, "stop": True})
        check(r.status_code == 403, "stop with nothing running -> 403")
        from CTFd.cache import cache
        from CTFd.models import Users
        cache.delete("docker_cooldown_user_%s_%s" % (Users.query.filter_by(name="alice").first().id, web))
        r = alice.post("/api/v1/container", json={"id": web})
        check(r.status_code == 200, "can start again after cooldown")

        r = bob.post("/api/v1/container", json={"id": web})
        check(r.status_code == 200, "bob can start his own Web 1")
        a = fake(2401)
        ports = sorted(int(list(c["HostConfig"]["PortBindings"].values())[0][0]["HostPort"]) for c in a["containers"])
        check(len(set(ports)) == len(ports), "alice + bob got different ports %s" % ports)

        age_trackers(app, 400)
        r = alice.post("/api/v1/container", json={"id": web})  # revert
        check(r.status_code == 200, "revert after 5 min")
        check(len(fake(2401)["containers"]) == 2, "revert replaced (still 2 containers on A)")
        age_trackers(app, 400)
        r = alice.post("/api/v1/container", json={"id": web, "stop": True})
        check(r.status_code == 200, "alice stops Web 1")
        check(len(fake(2401)["containers"]) == 1, "alice's container removed from host A")

        r = alice.post("/api/v1/container", json={"id": crypto})
        check(r.status_code == 200, "alice starts Crypto 1 -> %s %s" % (r.status_code, r.get_data(as_text=True)[:150]))
        b = fake(2402, tls=True)
        check(len(b["containers"]) == 1, "container created on host B over TLS")
        port = int(list(b["containers"][0]["HostConfig"]["PortBindings"].values())[0][0]["HostPort"])
        check(30500 <= port <= 30510, "port %d within host B range" % port)
        s = alice.get("/api/v1/docker_status").get_json()["data"]
        check(s[0]["host"] == "chal2.example.com", "status shows chal2.example.com for crypto")

        r = alice.post("/api/v1/challenges/attempt", json={"challenge_id": crypto, "submission": "flag{crypto}"})
        check(r.get_json()["data"]["status"] == "correct", "solve crypto")
        check(len(fake(2402, tls=True)["containers"]) == 0, "container killed on host B after solve")
        check(alice.get("/api/v1/docker_status").get_json()["data"] == [], "tracker cleared after solve")

        r = admin.get("/admin/docker_status")
        body = r.get_data(as_text=True)
        check(r.status_code == 200 and "bob" in body and "web-srv" in body, "admin status page shows bob on web-srv")

        r = admin.post("/api/v1/nuke", json={"all": True})
        check(r.status_code == 200 and len(fake(2401)["containers"]) == 0, "nuke all clears host A")

        # host down -> fast clean error, not a hang
        admin.post("/admin/docker_config?id=1",
                   data={"nonce": nonce(admin), "action": "save", "name": "web-srv", "hostname": "127.0.0.1:2499",
                         "public_host": "chal1.example.com", "tls_enabled": "False", "port_min": "30000",
                         "port_max": "30010", "mem_limit_mb": "256", "cpu_limit": "0.5", "pids_limit": "128"})
        t0 = time.time()
        r = bob.post("/api/v1/container", json={"id": web})
        check(r.status_code == 500 and "Could not start" in r.get_data(as_text=True) and time.time() - t0 < 12,
              "dead host -> clean error in %.1fs" % (time.time() - t0))
        r = admin.get("/admin/docker_config")
        check(">DOWN<" in r.get_data(as_text=True), "config page shows host DOWN")

        # cannot delete host in use
        r = admin.post("/admin/docker_config?id=1", data={"nonce": nonce(admin), "action": "delete"})
        check("used by" in r.get_data(as_text=True), "delete blocked while challenges use host")

        # delete challenge kills its containers
        admin.post("/admin/docker_config?id=1",
                   data={"nonce": nonce(admin), "action": "save", "name": "web-srv", "hostname": "127.0.0.1:2401",
                         "public_host": "chal1.example.com", "tls_enabled": "False", "port_min": "30000",
                         "port_max": "30010", "mem_limit_mb": "256", "cpu_limit": "0.5", "pids_limit": "128",
                         "repositories": ["ctf/web1", "ctf/crypto1"]})
        r = bob.post("/api/v1/container", json={"id": web})
        check(r.status_code == 200 and len(fake(2401)["containers"]) == 1, "bob starts again after host is back")
        r = admin.delete("/api/v1/challenges/%d" % web, json={})
        check(r.status_code == 200 and len(fake(2401)["containers"]) == 0, "deleting challenge kills its containers")
    destroy_ctfd(app)


def run_teams_mode():
    print("\n== TEAMS MODE ==")
    app = create_ctfd(enable_plugins=True, user_mode="teams")
    with app.app_context():
        admin, (web, crypto) = setup_hosts_and_chals(app)
        team = gen_team(app.db, name="team1", email="t1@x.com", member_count=2)
        u1, u2 = team.members[0].name, team.members[1].name
        c1 = login_as_user(app, name=u1)
        c2 = login_as_user(app, name=u2)
        r = c1.post("/api/v1/container", json={"id": web})
        check(r.status_code == 200, "team member 1 starts Web 1 -> %s %s" % (r.status_code, r.get_data(as_text=True)[:150]))
        s = c2.get("/api/v1/docker_status").get_json()["data"]
        check(len(s) == 1 and s[0]["host"] == "chal1.example.com", "teammate sees the team's container")
        r = c2.post("/api/v1/container", json={"id": crypto})
        check(r.status_code == 403 and "only run one" in r.get_data(as_text=True),
              "one container per TEAM enforced (no crash) -> %s" % r.status_code)
        r = admin.get("/admin/docker_status")
        check(r.status_code == 200 and "team1" in r.get_data(as_text=True), "admin status shows team name")
        r = c2.post("/api/v1/challenges/attempt", json={"challenge_id": web, "submission": "flag{web}"})
        check(r.get_json()["data"]["status"] == "correct", "teammate solves Web 1")
        check(len(fake(2401)["containers"]) == 0, "team container killed on solve")
    destroy_ctfd(app)


def run_robustness():
    print("\n== ROBUSTNESS (port clash retry, reservations, global sweep, lock) ==")
    import threading
    app = create_ctfd(enable_plugins=True)
    with app.app_context():
        admin, (web, crypto) = setup_hosts_and_chals(app)
        from CTFd.cache import cache
        from CTFd.models import Users
        from CTFd.plugins import docker_challenges as dc
        for n in ("carol", "dave", "erin"):
            gen_user(app.db, name=n, email=n + "@x.com")
        carol, dave, erin = (login_as_user(app, name=n) for n in ("carol", "dave", "erin"))
        uid = lambda n: Users.query.filter_by(name=n).first().id
        A = "http://127.0.0.1:2401"
        def set_range(lo, hi):
            h = dc.get_host(1); h.port_min, h.port_max = lo, hi; db.session.commit()
        set_range(30000, 30500)   # realistic-ish range for these tests

        # 1. Docker says "port already allocated" twice -> plugin retries with NEW ports, 3rd works
        requests.get(A + "/_fail", params={"kind": "port", "n": 2})
        r = carol.post("/api/v1/container", json={"id": web})
        starts = requests.get(A + "/_starts").json()
        check(r.status_code == 200, "port clash x2 -> still started on 3rd try (%s)" % r.status_code)
        check(len(starts) == 3 and len(set(starts)) == 3, "3 attempts, 3 different ports %s" % starts)
        check(len(fake(2401)["containers"]) == 1, "failed attempts cleaned up, 1 container left")
        carol.post("/api/v1/container", json={"id": web, "stop": True})

        # 2. clash on every attempt -> clean error after 3 tries, nothing left behind
        cache.delete("docker_cooldown_user_%s_%s" % (uid("carol"), web))
        requests.get(A + "/_fail", params={"kind": "port", "n": 3})
        r = carol.post("/api/v1/container", json={"id": web})
        check(r.status_code == 500 and "port is already allocated" in r.get_data(as_text=True),
              "port clash x3 -> clean error (%s)" % r.status_code)
        check(len(requests.get(A + "/_starts").json()) == 3, "gave up after exactly 3 attempts")
        check(len(fake(2401)["containers"]) == 0, "no container left after giving up")

        # 3. a NON-port error is not retried
        requests.get(A + "/_fail", params={"kind": "other", "n": 1})
        r = carol.post("/api/v1/container", json={"id": web})
        check(r.status_code == 500 and len(requests.get(A + "/_starts").json()) == 1,
              "non-port error -> no retry (1 attempt)")

        # 4. port reservations: parallel picks never collide, range exhaustion is reported
        set_range(30900, 30904)   # only 5 ports
        picks, errs = [], []
        def pick():
            with app.app_context():
                try:
                    picks.extend(dc.reserve_ports(dc.get_host(1), 1, set()))
                except Exception as e:
                    errs.append(repr(e))
        ths = [threading.Thread(target=pick) for _ in range(5)]
        [t.start() for t in ths]; [t.join() for t in ths]
        check(sorted(picks) == list(range(30900, 30905)) and not errs,
              "5 parallel picks -> 5 different ports %s %s" % (sorted(picks), errs))
        try:
            dc.reserve_ports(dc.get_host(1), 1, set()); check(False, "6th pick should fail")
        except dc.DockerError as e:
            check("No free ports" in str(e), "6th pick (range full of reservations) -> 'No free ports'")
        set_range(30000, 30500)

        # 5. GLOBAL sweep: dave's 3h-old container is removed when ERIN opens a challenge
        cache.delete("docker_sweep_lock")
        r = dave.post("/api/v1/container", json={"id": web})
        check(r.status_code == 200 and len(fake(2401)["containers"]) == 1, "dave starts Web")
        age_trackers(app, 3 * 3600)
        cache.delete("docker_sweep_lock")
        erin.get("/api/v1/docker_status")
        check(len(fake(2401)["containers"]) == 0, "erin's page view swept dave's 3h-old container off the host")
        check(dave.get("/api/v1/docker_status").get_json()["data"] == [], "dave's tracker row is gone")

        # 6. sweep is rate-limited: a 2nd stale one inside the same minute waits...
        r = dave.post("/api/v1/container", json={"id": web})
        age_trackers(app, 3 * 3600)
        erin.get("/api/v1/docker_status")
        check(len(fake(2401)["containers"]) == 1, "sweep runs at most once a minute (stale one still there)")
        # ...but the admin Docker Status page always sweeps
        admin.get("/admin/docker_status")
        check(len(fake(2401)["containers"]) == 0, "admin Docker Status page forces a sweep")

        # 7. a fresh container is NOT swept
        cache.delete("docker_cooldown_user_%s_%s" % (uid("dave"), web))
        dave.post("/api/v1/container", json={"id": web})
        cache.delete("docker_sweep_lock")
        admin.get("/admin/docker_status")
        check(len(fake(2401)["containers"]) == 1, "fresh container survives the sweep")
        admin.post("/api/v1/nuke", json={"all": True})

        # 8. per-player lock: while a request is running, a 2nd one gets 429
        lock = "docker_owner_lock_user_%s" % uid("erin")
        cache.add(lock, 1, timeout=60)
        r = erin.post("/api/v1/container", json={"id": web})
        check(r.status_code == 429 and "still running" in r.get_data(as_text=True), "double click -> 429 while 1st runs")
        cache.delete(lock)
        r = erin.post("/api/v1/container", json={"id": web})
        check(r.status_code == 200, "after the 1st finishes, start works")
        check(cache.get(lock) is None, "lock released after request (also on success)")
        r = erin.post("/api/v1/container", json={"id": crypto})
        check(r.status_code == 403 and cache.get(lock) is None, "lock released after an error response too")
        admin.post("/api/v1/nuke", json={"all": True})
    destroy_ctfd(app)


def run_dead_instances():
    print("\n== DEAD INSTANCES (crashed / OOM-killed / removed by hand) ==")
    app = create_ctfd(enable_plugins=True)
    with app.app_context():
        admin, (web, crypto) = setup_hosts_and_chals(app)
        from CTFd.plugins import docker_challenges as dc
        h = dc.get_host(1); h.port_min, h.port_max = 30000, 30500; db.session.commit()
        gen_user(app.db, name="frank", email="frank@x.com")
        frank = login_as_user(app, name="frank")
        A = "http://127.0.0.1:2401"

        # 1. container gets OOM-killed -> page shows Start again, no Stop needed, no cooldown
        r = frank.post("/api/v1/container", json={"id": web})
        check(r.status_code == 200, "frank starts Web")
        cid = frank.get("/api/v1/docker_status").get_json()["data"][0]["instance_id"]
        requests.get(A + "/_kill", params={"id": cid})
        st = frank.get("/api/v1/docker_status").get_json()["data"]
        check(st == [], "after OOM kill, opening the challenge shows no instance (Start button)")
        check(cid not in [c["Id"] for c in fake(2401)["containers"]], "the dead container is removed from the host")
        r = frank.post("/api/v1/container", json={"id": web})
        check(r.status_code == 200, "frank can Start again immediately (no 5-min revert wait, no cooldown) -> %s" % r.status_code)

        # 2. dead instance doesn't block starting a DIFFERENT challenge
        cid = frank.get("/api/v1/docker_status").get_json()["data"][0]["instance_id"]
        requests.get(A + "/_kill", params={"id": cid})
        r = frank.post("/api/v1/container", json={"id": crypto})   # no page view in between
        check(r.status_code == 200, "dead Web instance doesn't block starting Crypto -> %s %s" % (r.status_code, r.get_data(as_text=True)[:80]))
        st = frank.get("/api/v1/docker_status").get_json()["data"]
        check(len(st) == 1 and st[0]["challenge_id"] == crypto, "only the live Crypto instance is listed")

        # 3. container removed by hand on the server (404) -> also cleaned up
        cid = st[0]["instance_id"]
        requests.delete("https://127.0.0.1:2402/containers/%s?force=true" % cid,
                        cert=(f"{CERTS}/cert.pem", f"{CERTS}/key.pem"), verify=f"{CERTS}/ca.pem")
        check(frank.get("/api/v1/docker_status").get_json()["data"] == [], "container deleted by hand -> instance forgotten")

        # 4. host unreachable -> we can't tell, so the instance is KEPT (not wrongly dropped)
        r = frank.post("/api/v1/container", json={"id": web})
        h = dc.get_host(1); real = h.hostname; h.hostname = "127.0.0.1:2499"; db.session.commit()
        st = frank.get("/api/v1/docker_status").get_json()["data"]
        check(len(st) == 1, "host unreachable -> instance kept (no false 'dead')")
        h = dc.get_host(1); h.hostname = real; db.session.commit()

        # 4b. Docker answers 500 once while listing containers -> retried, start still works
        frank.post("/api/v1/container", json={"id": web, "stop": True})
        from CTFd.cache import cache
        from CTFd.models import Users
        cache.delete("docker_cooldown_user_%s_%s" % (Users.query.filter_by(name="frank").first().id, web))
        requests.get(A + "/_break", params={"n": 1})
        r = frank.post("/api/v1/container", json={"id": web})
        check(r.status_code == 200, "Docker error once -> retried, start OK (%s)" % r.status_code)
        # 4c. keeps failing -> clean message, not a crash
        frank.post("/api/v1/container", json={"id": web, "stop": True})
        cache.delete("docker_cooldown_user_%s_%s" % (Users.query.filter_by(name="frank").first().id, web))
        requests.get(A + "/_break", params={"n": 5})
        r = frank.post("/api/v1/container", json={"id": web})
        body = r.get_data(as_text=True)
        check(r.status_code == 500 and "Could not start container" in body and "returned an error" in body,
              "Docker keeps failing -> clean error message, not a crash (%s)" % body[:90].replace("\n", " "))
        requests.get(A + "/_break", params={"n": 0})
        r = frank.post("/api/v1/container", json={"id": web})
        st = frank.get("/api/v1/docker_status").get_json()["data"]

        # 5. a healthy running container is never touched
        st2 = frank.get("/api/v1/docker_status").get_json()["data"]
        check(len(st2) == 1 and st2[0]["instance_id"] == st[0]["instance_id"], "healthy instance untouched")
        admin.post("/api/v1/nuke", json={"all": True})
    destroy_ctfd(app)


def run_time_settings():
    print("\n== TIME SETTINGS PER HOST (lifetime / revert / cooldown) + DB upgrade ==")
    app = create_ctfd(enable_plugins=True)
    with app.app_context():
        admin, (web, crypto) = setup_hosts_and_chals(app)
        from CTFd.cache import cache
        from CTFd.models import Users
        from CTFd.plugins import docker_challenges as dc
        from sqlalchemy import inspect, text
        gen_user(app.db, name="gina", email="gina@x.com")
        gina = login_as_user(app, name="gina")
        gid = Users.query.filter_by(name="gina").first().id

        def save_host1(**times):
            data = {"nonce": nonce(admin), "action": "save", "name": "web-srv", "hostname": "127.0.0.1:2401",
                    "public_host": "chal1.example.com", "tls_enabled": "False", "port_min": "30000",
                    "port_max": "30500", "mem_limit_mb": "256", "cpu_limit": "0.5", "pids_limit": "256",
                    "repositories": ["ctf/web1", "ctf/crypto1"]}
            data.update({k: str(v) for k, v in times.items()})
            return admin.post("/admin/docker_config?id=1", data=data)

        def host1():
            db.session.expire_all(); return dc.get_host(1)

        def tracker():
            db.session.expire_all()
            return dc.DockerChallengeTracker.query.filter_by(user_id=str(gid)).first()

        # 1. defaults when the form doesn't send the new fields (old form / API)
        h = host1()
        check((h.lifetime_min, h.revert_min, h.cooldown_sec) == (120, 5, 30),
              "hosts saved without the new fields get defaults 120 min / 5 min / 30 s (%s)" % ((h.lifetime_min, h.revert_min, h.cooldown_sec),))
        body = admin.get("/admin/docker_config?id=1").get_data(as_text=True)
        check('name="lifetime_min"' in body and 'name="revert_min"' in body and 'name="cooldown_sec"' in body,
              "Docker Config page shows the 3 new boxes")

        # 2. set by hand: 10 min lifetime, 1 min revert, no cooldown
        r = save_host1(lifetime_min=10, revert_min=1, cooldown_sec=0)
        h = host1()
        check(r.status_code == 302 and (h.lifetime_min, h.revert_min, h.cooldown_sec) == (10, 1, 0), "save 10 min / 1 min / 0 s")
        # blank box keeps the current value (not 'no limit')
        save_host1(lifetime_min="", revert_min=1, cooldown_sec=0)
        check(host1().lifetime_min == 10, "blank lifetime box keeps 10 (doesn't silently become 'no limit')")

        # 3. validation
        r = save_host1(lifetime_min=5, revert_min=5, cooldown_sec=0)
        check("must be shorter" in r.get_data(as_text=True) and host1().lifetime_min == 10, "revert >= lifetime rejected")
        r = save_host1(lifetime_min=-1, revert_min=1, cooldown_sec=0)
        check("be negative" in r.get_data(as_text=True) and host1().lifetime_min == 10, "negative time rejected")
        r = save_host1(lifetime_min="abc", revert_min=1, cooldown_sec=0)
        check("whole numbers" in r.get_data(as_text=True), "non-number rejected")

        # 4. a new instance uses the host's settings
        t0 = int(time.time())
        r = gina.post("/api/v1/container", json={"id": web})
        st = gina.get("/api/v1/docker_status").get_json()["data"][0]
        tr = tracker()
        check(r.status_code == 200 and abs(tr.expires - (t0 + 600)) <= 3, "instance expires 10 min after start (%s)" % (tr.expires - t0))
        check(abs(st["revert_time"] - (t0 + 60)) <= 3, "revert unlocks after 1 min")
        check("expires" not in st, "players do NOT get the expiry time from the API")
        page = gina.get("/plugins/docker_challenges/assets/view.js").get_data(as_text=True)
        check("expir" not in page.lower(), "player page has no expiry countdown")
        body = admin.get("/admin/docker_status").get_data(as_text=True)
        check("Expires" in body, "admin Docker Status still shows Expires")
        r = gina.post("/api/v1/container", json={"id": web})
        check(r.status_code == 403 and "Revert unlocks 1 min" in r.get_data(as_text=True), "revert blocked in the 1st minute, message says 1 min")
        age_trackers(app, 61)
        r = gina.post("/api/v1/container", json={"id": web})
        check(r.status_code == 200, "revert allowed after 1 min")
        # cooldown 0: stop then start straight away
        gina.post("/api/v1/container", json={"id": web, "stop": True})
        r = gina.post("/api/v1/container", json={"id": web})
        check(r.status_code == 200, "cooldown 0 -> Start right after Stop works")

        # 5. changing the host doesn't touch running instances
        before = tracker().expires
        save_host1(lifetime_min=60, revert_min=1, cooldown_sec=0)
        after = tracker().expires
        check(before == after, "running instance keeps its old expiry after the host setting changes")

        # 6. expiry is enforced by the sweep
        age_trackers(app, 601)
        admin.get("/admin/docker_status")
        check(gina.get("/api/v1/docker_status").get_json()["data"] == [] and len(fake(2401)["containers"]) == 0,
              "expired instance removed by the sweep")

        # 7. lifetime 0 = no limit: never swept, even after 10 h
        save_host1(lifetime_min=0, revert_min=1, cooldown_sec=0)
        cache.delete("docker_cooldown_user_%s_%s" % (gid, web))
        r = gina.post("/api/v1/container", json={"id": web})
        check(r.status_code == 200 and tracker().expires is None, "lifetime 0 -> no expiry")
        age_trackers(app, 10 * 3600)
        admin.get("/admin/docker_status")
        check(len(gina.get("/api/v1/docker_status").get_json()["data"]) == 1, "no-limit instance survives 10 h")
        body = admin.get("/admin/docker_status").get_data(as_text=True)
        check(">never<" in body, "Docker Status shows Expires = never")

        # 8. cooldown setting is used
        save_host1(lifetime_min=120, revert_min=1, cooldown_sec=45)
        gina.post("/api/v1/container", json={"id": web, "stop": True})
        r = gina.post("/api/v1/container", json={"id": web})
        check(r.status_code == 403 and ("wait 45" in r.get_data(as_text=True) or "wait 44" in r.get_data(as_text=True)),
              "cooldown 45 s after Stop")
        admin.post("/api/v1/nuke", json={"all": True})

        # 9. UPGRADE: a database from before this version (no new columns) is fixed on startup
        with db.engine.begin() as conn:
            conn.execute(text("DROP INDEX IF EXISTS ix_docker_host_tracker_expires"))
            conn.execute(text("ALTER TABLE docker_host_tracker DROP COLUMN expires"))
            for c in ("lifetime_min", "revert_min", "cooldown_sec"):
                conn.execute(text("ALTER TABLE docker_hosts DROP COLUMN %s" % c))
            conn.execute(text("INSERT INTO docker_host_tracker (user_id, docker_image, timestamp, revert_time, instance_id, ports, host, host_id, challenge_id, challenge) "
                              "VALUES ('999', 'ctf/web1:latest', 1000000, 1000300, 'legacy', '30001', 'chal1', 1, 1, 'old')"))
        cols = {c["name"] for c in inspect(db.engine).get_columns("docker_hosts")}
        check("lifetime_min" not in cols, "(setup) old-style tables without the new columns")
        dc.migrate_columns()
        db.session.expire_all()
        insp = inspect(db.engine)
        hcols = {c["name"] for c in insp.get_columns("docker_hosts")}
        tcols = {c["name"] for c in insp.get_columns("docker_host_tracker")}
        check({"lifetime_min", "revert_min", "cooldown_sec"} <= hcols and "expires" in tcols, "migration adds the missing columns")
        with db.engine.connect() as conn:
            row = conn.execute(text("SELECT expires FROM docker_host_tracker WHERE instance_id='legacy'")).fetchone()
            hv = conn.execute(text("SELECT lifetime_min, revert_min, cooldown_sec FROM docker_hosts WHERE id=1")).fetchone()
        check(row[0] == 1000000 + 7200, "already-running instances get the old 2 h lifetime")
        check(tuple(hv) == (120, 5, 30), "existing hosts get the defaults %s" % (tuple(hv),))
        dc.migrate_columns()
        check(True, "running the migration again is harmless")
        r = admin.get("/admin/docker_config?id=1")
        check(r.status_code == 200, "Docker Config page works after the upgrade")
    destroy_ctfd(app)


def run_memes():
    print("\n== POPUP IMAGES ==")
    app = create_ctfd(enable_plugins=True)
    with app.app_context():
        gen_user(app.db, name="hank", email="hank@x.com")
        hank = login_as_user(app, name="hank")
        from CTFd.plugins import docker_challenges as dc
        d = dc.MEME_DIR
        made = []
        try:
            r = hank.get("/api/v1/docker_memes")
            check(r.status_code == 200 and r.get_json()["data"] == {"solved_list": []}, "no images -> empty list (plain popups)")
            for name in ("cooldown.png", "started.webp", "evil.png", "error.txt"):
                open(os.path.join(d, name), "wb").write(b"x"); made.append(name)
            data = hank.get("/api/v1/docker_memes").get_json()["data"]
            check(set(data) == {"cooldown", "started", "solved_list"}, "only known popup names + image types are listed %s" % sorted(data))
            check(data["cooldown"].startswith("/plugins/docker_challenges/assets/memes/cooldown.png?v="), "URL with cache-buster")
            r = hank.get(data["cooldown"].split("?")[0])
            check(r.status_code == 200 and r.data == b"x", "image file is served to players")
            # correct-flag memes: solved + solved-N, random pick happens in the theme
            for name in ("solved.png", "solved-1.jpg", "solved-3.webp", "solved-21.png", "solved-x.png", "solved-2.txt"):
                open(os.path.join(d, name), "wb").write(b"x"); made.append(name)
            data = hank.get("/api/v1/docker_memes").get_json()["data"]
            names = [u.split("/")[-1].split("?")[0] for u in data["solved_list"]]
            check(names == ["solved.png", "solved-1.jpg", "solved-3.webp"], "solved_list = solved + solved-1..20 images only %s" % names)
            check(data.get("solved", "").startswith("/plugins/docker_challenges/assets/memes/solved.png?v="), "solved also listed as a single popup image")
            check(hank.get(data["solved_list"][1].split("?")[0]).status_code == 200, "solved meme file is served")
            anon = app.test_client()
            check(anon.get("/api/v1/docker_memes").status_code in (302, 403), "not logged in -> no list")
        finally:
            for name in made:
                os.remove(os.path.join(d, name))
    destroy_ctfd(app)


if __name__ == "__main__":
    run_users_mode()
    run_teams_mode()
    run_robustness()
    run_dead_instances()
    run_time_settings()
    run_memes()
    print("\nRESULT: %d passed, %d failed" % (OK, FAIL))
