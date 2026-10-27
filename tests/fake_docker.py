"""Minimal fake Docker Engine API for testing the plugin."""
import sys, uuid, json
from flask import Flask, request, jsonify

name = sys.argv[1]; port = int(sys.argv[2])
app = Flask(name)
IMAGES = {"ctf/web1:latest": ["80/tcp"], "ctf/crypto1:latest": ["1337/tcp"], "other/junk:1": ["22/tcp"]}
CONTAINERS = {}  # id -> dict
LISTENERS = {}
FAIL_NEXT = []
BREAK_LIST = [0]  # next N container listings answer 500  # messages for the next N /start calls
STARTS = []
import socket, threading, time as _t
LOG = []

def _serve(srv):
    while True:
        try:
            srv.accept()[0].close()
        except OSError:
            return

@app.route("/_ping")
def ping(): return "OK"

@app.route("/images/json")
def images(): return jsonify([{"RepoTags": [t]} for t in IMAGES] + [{"RepoTags": ["<none>:<none>"]}])

@app.route("/images/<path:img>/json")
def image(img):
    if img not in IMAGES: return jsonify({"message": "no such image"}), 404
    return jsonify({"Config": {"ExposedPorts": {p: {} for p in IMAGES[img]}}})

@app.route("/containers/json")
def containers():
    if BREAK_LIST[0] > 0:
        BREAK_LIST[0] -= 1
        return "daemon busy", 500
    return jsonify([{"Id": c["Id"], "Names": ["/" + c["name"]],
                     "Ports": [{"PublicPort": int(b[0]["HostPort"])} for b in c["HostConfig"]["PortBindings"].values()]}
                    for c in list(CONTAINERS.values())])

@app.route("/containers/create", methods=["POST"])
def create():
    body = request.get_json(force=True)
    n = request.args["name"]
    if any(c["name"] == n for c in CONTAINERS.values()):
        return jsonify({"message": "Conflict"}), 409
    cid = uuid.uuid4().hex * 2
    CONTAINERS[cid] = {"Id": cid, "name": n, **body, "running": False}
    LOG.append(("create", n, body))
    return jsonify({"Id": cid}), 201

@app.route("/containers/<cid>/start", methods=["POST"])
def start(cid):
    _t.sleep(0.3)  # pretend container start takes a moment
    STARTS.append(list(CONTAINERS[cid]["HostConfig"]["PortBindings"].values())[0][0]["HostPort"])
    if FAIL_NEXT:
        return jsonify({"message": FAIL_NEXT.pop(0)}), 500
    CONTAINERS[cid]["running"] = True
    for b in CONTAINERS[cid]["HostConfig"]["PortBindings"].values():
        port = int(b[0]["HostPort"])
        srv = socket.socket(); srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            srv.bind(("127.0.0.1", port))
        except OSError:
            return jsonify({"message": "port is already allocated"}), 500
        srv.listen(); LISTENERS[cid] = srv
        threading.Thread(target=_serve, args=(srv,), daemon=True).start()
    return "", 204

@app.route("/containers/<ref>", methods=["DELETE"])
def delete(ref):
    for cid, c in list(CONTAINERS.items()):
        if cid == ref or c["name"] == ref:
            del CONTAINERS[cid]; LOG.append(("delete", c["name"]))
            l = LISTENERS.pop(cid, None)
            if l:
                try: l.shutdown(socket.SHUT_RDWR)
                except OSError: pass
                l.close()
            return "", 204
    return jsonify({"message": "no such container"}), 404

@app.route("/_fail")
def fail():
    msg = {"port": "failed to set up container networking: driver failed programming external connectivity on endpoint x: Bind for 0.0.0.0:30001 failed: port is already allocated",
           "other": "OCI runtime create failed: exec format error"}[request.args["kind"]]
    FAIL_NEXT.extend([msg] * int(request.args["n"]))
    STARTS.clear()
    return "ok"

@app.route("/_starts")
def starts(): return jsonify(STARTS)

@app.route("/containers/<cid>/json")
def inspect(cid):
    c = CONTAINERS.get(cid)
    if not c:
        return jsonify({"message": "No such container: " + cid}), 404
    return jsonify({"Id": cid, "State": {"Running": c["running"], "Paused": False,
                    "OOMKilled": c.get("oom", False), "Status": "running" if c["running"] else "exited"}})

@app.route("/_break")
def brk():
    BREAK_LIST[0] = int(request.args["n"])
    return "ok"

@app.route("/_kill")
def kill():
    """Simulate an OOM kill / crash: container stays but is no longer running."""
    cid = request.args["id"]
    CONTAINERS[cid]["running"] = False
    CONTAINERS[cid]["oom"] = True
    l = LISTENERS.pop(cid, None)
    if l:
        try: l.shutdown(socket.SHUT_RDWR)
        except OSError: pass
        l.close()
    return "ok"

@app.route("/_debug")
def debug(): return jsonify({"containers": list(CONTAINERS.values()), "log": LOG})

if __name__ == "__main__":
    ctx = None
    if len(sys.argv) > 3:   # TLS with client cert verification
        import ssl
        d = sys.argv[3]
        ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH, cafile=f"{d}/ca.pem")
        ctx.load_cert_chain(f"{d}/server-cert.pem", f"{d}/server-key.pem")
        ctx.verify_mode = ssl.CERT_REQUIRED
    app.run("127.0.0.1", port, ssl_context=ctx, threaded=True)
