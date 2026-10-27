"""
CTFd Docker Challenges - multi-host fork.

Based on offsecginger/CTFd-Docker-Challenges (CTFd 3.7.x version).

Changes vs upstream:
  - Multiple Docker hosts. Each docker challenge is pinned to one host.
  - Each host has a separate API address (private) and public address (shown to players).
  - Per-host port range for player instances (default 30000-31000).
  - Per-host container limits (memory, CPU, pids).
  - "One container at a time" check works in Teams mode.
  - Timeouts on Docker API calls, so one dead host can't hang CTFd.
  - Container names include the full image name (no clashes between images with the same tag).
"""
import hashlib
import json
import random
import re
import tempfile
import traceback
from datetime import datetime
from pathlib import Path

import requests
from flask import Blueprint, abort, redirect, render_template, request, url_for
from flask_restx import Namespace, Resource

from CTFd.api import CTFd_API_v1
from CTFd.cache import cache
from CTFd.forms import BaseForm
from CTFd.models import (
    ChallengeFiles,
    Challenges,
    Fails,
    Flags,
    Hints,
    Solves,
    Tags,
    Teams,
    Users,
    db,
)
from CTFd.plugins import register_plugin_assets_directory
from CTFd.plugins.challenges import CHALLENGE_CLASSES, BaseChallenge
from CTFd.plugins.flags import get_flag_class
from CTFd.utils.config import is_teams_mode
from CTFd.utils.dates import unix_time
from CTFd.utils.decorators import admins_only, authed_only
from CTFd.utils.uploads import delete_file
from CTFd.utils.user import get_current_team, get_current_user, get_ip, is_admin

API_TIMEOUT = 10          # seconds for any Docker API call
# Defaults for new hosts; each host can override them on the Docker Config page.
DEFAULT_LIFETIME_MIN = 120  # instance removed after this long (0 = no limit)
DEFAULT_REVERT_MIN = 5      # Revert unlocks after this long
DEFAULT_COOLDOWN_SEC = 30   # wait after pressing Stop before starting that challenge again
LEGACY_LIFETIME_SEC = 7200  # for instances created before lifetimes were stored
PORT_HOLD = 120           # a picked port stays reserved this long, so parallel starts can't pick it too
START_ATTEMPTS = 3        # retries with fresh ports if Docker says a port is taken
SWEEP_EVERY = 60          # global stale-container sweep runs at most once per this many seconds
SWEEP_BATCH = 25          # max containers removed per sweep (keeps one request from taking long)
SWEEP_TIMEOUT = 3         # Docker API timeout used by the sweep
ALIVE_TIMEOUT = 3         # Docker API timeout when checking a player's container is still running


# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #
class DockerConfig(db.Model):
    """One row per Docker host."""

    __tablename__ = "docker_hosts"
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(64), unique=True)
    hostname = db.Column(db.String(128))       # API address, e.g. 10.148.0.5:2376
    public_host = db.Column(db.String(128))    # what players connect to, e.g. chal1.example.com
    tls_enabled = db.Column(db.Boolean, default=False)
    ca_cert = db.Column(db.Text)
    client_cert = db.Column(db.Text)
    client_key = db.Column(db.Text)
    repositories = db.Column(db.Text)          # comma-separated repo names allowed for challenges
    port_min = db.Column(db.Integer, default=30000)
    port_max = db.Column(db.Integer, default=31000)
    mem_limit_mb = db.Column(db.Integer, default=256)
    cpu_limit = db.Column(db.Float, default=0.5)
    pids_limit = db.Column(db.Integer, default=256)
    lifetime_min = db.Column(db.Integer, default=DEFAULT_LIFETIME_MIN)
    revert_min = db.Column(db.Integer, default=DEFAULT_REVERT_MIN)
    cooldown_sec = db.Column(db.Integer, default=DEFAULT_COOLDOWN_SEC)

    @property
    def lifetime_seconds(self):
        """None = no limit."""
        m = DEFAULT_LIFETIME_MIN if self.lifetime_min is None else int(self.lifetime_min)
        return None if m <= 0 else m * 60

    @property
    def revert_seconds(self):
        m = DEFAULT_REVERT_MIN if self.revert_min is None else int(self.revert_min)
        return max(0, m) * 60

    @property
    def cooldown_seconds(self):
        c = DEFAULT_COOLDOWN_SEC if self.cooldown_sec is None else int(self.cooldown_sec)
        return max(0, c)

    @property
    def display_host(self):
        if self.public_host:
            return self.public_host
        return str(self.hostname or "").split(":")[0]


class DockerChallengeTracker(db.Model):
    """Active player containers."""

    __tablename__ = "docker_host_tracker"
    id = db.Column(db.Integer, primary_key=True)
    team_id = db.Column(db.String(64), index=True)
    user_id = db.Column(db.String(64), index=True)
    docker_image = db.Column(db.String(128), index=True)
    timestamp = db.Column(db.Integer, index=True)
    revert_time = db.Column(db.Integer, index=True)
    expires = db.Column(db.Integer, index=True)  # unix time; NULL = no limit
    instance_id = db.Column(db.String(128), index=True)
    ports = db.Column(db.String(128))
    host = db.Column(db.String(128))           # public host shown to the player
    host_id = db.Column(db.Integer, index=True)
    challenge_id = db.Column(db.Integer, index=True)
    challenge = db.Column(db.String(256))


class DockerChallenge(Challenges):
    __tablename__ = "docker_host_challenge"
    __mapper_args__ = {"polymorphic_identity": "docker"}
    id = db.Column(None, db.ForeignKey("challenges.id", ondelete="CASCADE"), primary_key=True)
    docker_image = db.Column(db.String(128), index=True)
    docker_host_id = db.Column(db.Integer, index=True)


class DockerConfigForm(BaseForm):
    """Only used for the CSRF nonce."""


# --------------------------------------------------------------------------- #
# Docker API helpers
# --------------------------------------------------------------------------- #
class DockerError(Exception):
    pass


def get_host(host_id):
    try:
        host_id = int(host_id)
    except (TypeError, ValueError):
        return None
    return DockerConfig.query.filter_by(id=host_id).first()


def _write_tmp(content):
    f = tempfile.NamedTemporaryFile(delete=False)
    f.write(content.encode() if isinstance(content, str) else content)
    f.close()
    return f.name


def _to_text(value):
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


def do_request(docker, path, method="GET", data=None, headers=None, timeout=API_TIMEOUT):
    """Call the Docker API on one host. Returns a requests.Response or raises DockerError."""
    if docker is None or not docker.hostname:
        raise DockerError("Docker host not configured")
    prefix = "https" if docker.tls_enabled else "http"
    url = "%s://%s%s" % (prefix, docker.hostname, path)
    kwargs = {"headers": headers, "timeout": timeout}
    if data is not None:
        kwargs["data"] = data
    tmp_files = []
    try:
        if docker.tls_enabled:
            ca = _write_tmp(docker.ca_cert or "")
            crt = _write_tmp(docker.client_cert or "")
            key = _write_tmp(docker.client_key or "")
            tmp_files = [ca, crt, key]
            kwargs["cert"] = (crt, key)
            kwargs["verify"] = ca
        return requests.request(method, url, **kwargs)
    except requests.RequestException as e:
        raise DockerError("Cannot reach Docker host '%s': %s" % (docker.name, e))
    finally:
        for p in tmp_files:
            Path(p).unlink(missing_ok=True)


def get_json(docker, path, timeout=API_TIMEOUT):
    """GET a Docker API path and return parsed JSON.

    Read-only, so one retry on a server error or garbled answer (Docker can
    hiccup under heavy load). Anything still wrong becomes a DockerError, which
    callers turn into a clean message for the player instead of a crash.
    """
    last = None
    for _ in range(2):
        r = do_request(docker, path, timeout=timeout)
        if r.status_code == 200:
            try:
                return r.json()
            except ValueError:
                last = "unreadable answer"
        else:
            last = "HTTP %s: %s" % (r.status_code, r.text[:120])
            if r.status_code < 500:
                break
    raise DockerError("Docker on %s returned an error for %s (%s)" % (docker.name, path.split("?")[0], last))


def get_repositories(docker, tags=False, repos=None):
    images = get_json(docker, "/images/json?all=1")
    allowed = None
    if repos:
        allowed = set(x for x in repos.split(",") if x) if isinstance(repos, str) else set(repos)
    result = set()
    for img in images:
        for tag in img.get("RepoTags") or []:
            repo = tag.rsplit(":", 1)[0]
            if repo == "<none>":
                continue
            if allowed is not None and repo not in allowed:
                continue
            result.add(tag if tags else repo)
    return sorted(result)


def get_unavailable_ports(docker):
    used = set()
    for c in get_json(docker, "/containers/json?all=1"):
        for p in c.get("Ports") or []:
            if p.get("PublicPort"):
                used.add(int(p["PublicPort"]))
    return used


def get_required_ports(docker, image):
    r = do_request(docker, "/images/%s/json" % requests.utils.quote(image, safe=""))
    if r.status_code != 200:
        raise DockerError("Image %s not found on host %s" % (image, docker.name))
    try:
        info = r.json()
    except ValueError:
        raise DockerError("Unreadable image info for %s on %s" % (image, docker.name))
    exposed = (info.get("Config") or {}).get("ExposedPorts") or {}
    return list(exposed.keys())


PORT_CLASH_HINTS = ("port is already allocated", "address already in use", "external connectivity", "networking")


def reserve_ports(docker, count, exclude):
    """Pick `count` free ports and reserve them in the shared cache.

    cache.add is atomic (Redis SETNX), so two starts running at the same time
    on any worker can never be handed the same port.
    """
    used = get_unavailable_ports(docker) | exclude
    pmin, pmax = int(docker.port_min or 30000), int(docker.port_max or 31000)
    free = [p for p in range(pmin, pmax + 1) if p not in used]
    random.shuffle(free)
    chosen = []
    for p in free:
        if cache.add("docker_port_%s_%s" % (docker.id, p), 1, timeout=PORT_HOLD):
            chosen.append(p)
            if len(chosen) == count:
                return chosen
    raise DockerError("No free ports left on host %s" % docker.name)


def create_container(docker, image, owner_name):
    """Create and start a container. Returns (container_id, [host ports])."""
    needed = get_required_ports(docker, image)

    host_config_base = {"SecurityOpt": ["no-new-privileges"]}
    if docker.mem_limit_mb:
        host_config_base["Memory"] = int(docker.mem_limit_mb) * 1024 * 1024
    if docker.cpu_limit:
        host_config_base["NanoCpus"] = int(float(docker.cpu_limit) * 1e9)
    if docker.pids_limit:
        host_config_base["PidsLimit"] = int(docker.pids_limit)

    owner = hashlib.md5(owner_name.encode("utf-8")).hexdigest()[:10]
    safe_image = re.sub(r"[^a-zA-Z0-9_.-]", "_", image)
    name = "%s_%s" % (safe_image, owner)
    headers = {"Content-Type": "application/json"}

    tried = set()
    last_error = None
    for attempt in range(START_ATTEMPTS):
        chosen = reserve_ports(docker, len(needed), tried)
        tried.update(chosen)
        exposed, bindings = {}, {}
        for container_port, host_port in zip(needed, chosen):
            exposed[container_port] = {}
            bindings[container_port] = [{"HostPort": str(host_port)}]
        host_config = dict(host_config_base, PortBindings=bindings)
        payload = json.dumps({"Image": image, "ExposedPorts": exposed, "HostConfig": host_config})

        # Remove a leftover container with the same name (CTFd restart, failed attempt)
        do_request(docker, "/containers/%s?force=true" % name, method="DELETE", headers=headers)

        r = do_request(docker, "/containers/create?name=%s" % name, method="POST", data=payload, headers=headers)
        if r.status_code not in (200, 201):
            raise DockerError("Create failed on %s: %s" % (docker.name, r.text[:200]))
        try:
            cid = r.json()["Id"]
        except (ValueError, KeyError):
            raise DockerError("Unreadable answer creating container on %s" % docker.name)
        st = do_request(docker, "/containers/%s/start" % cid, method="POST", headers=headers)
        if st.status_code in (204, 304):
            return cid, chosen
        delete_container(docker, cid)
        last_error = st.text[:200]
        if not any(h in st.text.lower() for h in PORT_CLASH_HINTS):
            break  # not a port problem, retrying won't help
    raise DockerError("Start failed on %s: %s" % (docker.name, last_error))


def delete_container(docker, instance_id, timeout=API_TIMEOUT):
    """Force-remove a container. Returns False if the host couldn't be reached."""
    try:
        do_request(
            docker,
            "/containers/%s?force=true" % instance_id,
            method="DELETE",
            headers={"Content-Type": "application/json"},
            timeout=timeout,
        )
        return True
    except DockerError:
        traceback.print_exc()
        return False


def remove_tracked(entry):
    """Kill a tracked container on its own host and drop the tracker row."""
    delete_container(get_host(entry.host_id), entry.instance_id)
    DockerChallengeTracker.query.filter_by(id=entry.id).delete()
    db.session.commit()


def sweep_stale(force=False):
    """Remove EVERY player's containers whose lifetime is over.

    Runs as a side effect of normal traffic (players opening docker challenges,
    starting containers, admins opening Docker Status), at most once per
    SWEEP_EVERY seconds across all workers. A host that can't be reached is
    skipped for the rest of the sweep; its tracker rows are still dropped.
    """
    if not force and not cache.add("docker_sweep_lock", 1, timeout=SWEEP_EVERY):
        return 0
    now = unix_time(datetime.utcnow())
    stale = (
        DockerChallengeTracker.query.filter(
            DockerChallengeTracker.expires.isnot(None), DockerChallengeTracker.expires <= now
        )
        .order_by(DockerChallengeTracker.expires)
        .limit(SWEEP_BATCH)
        .all()
    )
    dead_hosts = set()
    for t in stale:
        if t.host_id not in dead_hosts:
            host = get_host(t.host_id)
            if host is None or not delete_container(host, t.instance_id, timeout=SWEEP_TIMEOUT):
                dead_hosts.add(t.host_id)
        DockerChallengeTracker.query.filter_by(id=t.id).delete()
    if stale:
        db.session.commit()
    return len(stale)


def container_alive(docker, instance_id):
    """True = running, False = gone/stopped (OOM-killed, crashed, removed by hand),
    None = can't tell (host unreachable)."""
    try:
        r = do_request(docker, "/containers/%s/json" % instance_id, timeout=ALIVE_TIMEOUT)
    except DockerError:
        return None
    if r.status_code == 404:
        return False
    if r.status_code != 200:
        return None
    try:
        state = r.json().get("State") or {}
    except ValueError:
        return None
    return bool(state.get("Running") or state.get("Paused"))


def prune_dead(owner_id):
    """Forget this player's/team's containers that are no longer running.

    Lets the player press Start again right away (no Stop, no cooldown) when
    their instance died. Containers on unreachable hosts are left alone.
    """
    removed = 0
    for t in owner_filter(DockerChallengeTracker.query, owner_id).all():
        host = get_host(t.host_id)
        if host is None:
            continue
        if container_alive(host, t.instance_id) is False:
            delete_container(host, t.instance_id, timeout=ALIVE_TIMEOUT)  # remove the stopped leftover
            DockerChallengeTracker.query.filter_by(id=t.id).delete()
            removed += 1
    if removed:
        db.session.commit()
    return removed


def _human(seconds):
    if seconds % 3600 == 0 and seconds >= 3600:
        return "%d h" % (seconds // 3600)
    if seconds % 60 == 0:
        return "%d min" % (seconds // 60)
    return "%d s" % seconds


def req_arg(name):
    """Read a parameter from the JSON body (POST) or the form."""
    data = request.get_json(silent=True) or {}
    if name in data:
        return data[name]
    return request.form.get(name)


def owner_filter(query, owner_id):
    if is_teams_mode():
        return query.filter_by(team_id=str(owner_id))
    return query.filter_by(user_id=str(owner_id))


# --------------------------------------------------------------------------- #
# Admin pages
# --------------------------------------------------------------------------- #
def define_docker_admin(app):
    bp = Blueprint("admin_docker_config", __name__, template_folder="templates", static_folder="assets")

    @bp.route("/admin/docker_config", methods=["GET", "POST"])
    @admins_only
    def docker_config():
        form = DockerConfigForm()
        errors = []
        hosts = DockerConfig.query.order_by(DockerConfig.id).all()
        edit_id = request.args.get("id")
        current = get_host(edit_id) if edit_id else None

        if request.method == "POST":
            action = request.form.get("action", "save")
            if action == "delete" and current:
                in_use = DockerChallenge.query.filter_by(docker_host_id=current.id).count()
                if in_use:
                    errors.append("Host is used by %d challenge(s). Move them first." % in_use)
                else:
                    for t in DockerChallengeTracker.query.filter_by(host_id=current.id).all():
                        remove_tracked(t)
                    db.session.delete(current)
                    db.session.commit()
                    return redirect(url_for("admin_docker_config.docker_config"))
            else:
                b = current or DockerConfig()
                b.name = request.form.get("name", "").strip() or "host-%d" % (len(hosts) + 1)
                b.hostname = request.form.get("hostname", "").strip()
                b.public_host = request.form.get("public_host", "").strip()
                b.tls_enabled = request.form.get("tls_enabled") == "True"
                for field in ("ca_cert", "client_cert", "client_key"):
                    f = request.files.get(field)
                    content = f.stream.read() if f else b""
                    if content:
                        setattr(b, field, _to_text(content))
                if not b.tls_enabled:
                    b.ca_cert = b.client_cert = b.client_key = None
                try:
                    b.port_min = int(request.form.get("port_min") or 30000)
                    b.port_max = int(request.form.get("port_max") or 31000)
                    b.mem_limit_mb = int(request.form.get("mem_limit_mb") or 0)
                    b.cpu_limit = float(request.form.get("cpu_limit") or 0)
                    b.pids_limit = int(request.form.get("pids_limit") or 0)
                    # blank / missing -> keep the current value (or the default), never "0 = no limit" by accident
                    for field, default in (("lifetime_min", DEFAULT_LIFETIME_MIN),
                                           ("revert_min", DEFAULT_REVERT_MIN),
                                           ("cooldown_sec", DEFAULT_COOLDOWN_SEC)):
                        raw = (request.form.get(field) or "").strip()
                        if raw == "":
                            cur = getattr(b, field)
                            setattr(b, field, default if cur is None else cur)
                        else:
                            setattr(b, field, int(raw))
                except ValueError:
                    errors.append("Ports, limits and times must be whole numbers")
                if b.port_min > b.port_max:
                    errors.append("Port min must be <= port max")
                if min(b.lifetime_min or 0, b.revert_min or 0, b.cooldown_sec or 0) < 0:
                    errors.append("Times can't be negative")
                if (b.lifetime_min or 0) > 0 and (b.revert_min or 0) >= b.lifetime_min:
                    errors.append("'Revert after' must be shorter than the instance lifetime")
                b.repositories = ",".join(request.form.getlist("repositories")) or None
                if not errors:
                    if not current:
                        db.session.add(b)
                    db.session.commit()
                    return redirect(url_for("admin_docker_config.docker_config", id=b.id))
                db.session.rollback()

        repos, selected = [], []
        if current:
            try:
                repos = get_repositories(current)
            except Exception as e:
                errors.append("Can't list images on %s: %s" % (current.name, e))
            selected = (current.repositories or "").split(",")

        status = {}
        for h in hosts:
            try:
                r = do_request(h, "/_ping")
                status[h.id] = r.status_code == 200
            except DockerError:
                status[h.id] = False

        return render_template(
            "docker_config.html",
            form=form,
            hosts=hosts,
            config=current,
            repos=repos,
            selected=selected,
            status=status,
            errors=errors,
        )

    app.register_blueprint(bp)


def define_docker_status(app):
    bp = Blueprint("admin_docker_status", __name__, template_folder="templates", static_folder="assets")

    @bp.route("/admin/docker_status", methods=["GET"])
    @admins_only
    def docker_admin():
        sweep_stale(force=True)
        hosts = {h.id: h.name for h in DockerConfig.query.all()}
        rows = []
        for t in DockerChallengeTracker.query.all():
            if is_teams_mode():
                owner = Teams.query.filter_by(id=t.team_id).first()
            else:
                owner = Users.query.filter_by(id=t.user_id).first()
            rows.append(
                {
                    "id": t.id,
                    "owner": owner.name if owner else "?",
                    "docker_image": t.docker_image,
                    "challenge": t.challenge,
                    "instance_id": t.instance_id,
                    "host_name": hosts.get(t.host_id, "?"),
                    "address": "%s:%s" % (t.host, t.ports),
                    "timestamp": t.timestamp,
                    "expires": t.expires,
                }
            )
        return render_template(
            "admin_docker_status.html", dockers=rows, owner_label="Team" if is_teams_mode() else "User"
        )

    app.register_blueprint(bp)


# --------------------------------------------------------------------------- #
# Challenge type
# --------------------------------------------------------------------------- #
class DockerChallengeType(BaseChallenge):
    id = "docker"
    name = "docker"
    templates = {
        "create": "/plugins/docker_challenges/assets/create.html",
        "update": "/plugins/docker_challenges/assets/update.html",
        "view": "/plugins/docker_challenges/assets/view.html",
    }
    scripts = {
        "create": "/plugins/docker_challenges/assets/create.js",
        "update": "/plugins/docker_challenges/assets/update.js",
        "view": "/plugins/docker_challenges/assets/view.js",
    }
    route = "/plugins/docker_challenges/assets"
    blueprint = Blueprint("docker_challenges", __name__, template_folder="templates", static_folder="assets")
    challenge_model = DockerChallenge

    @classmethod
    def read(cls, challenge):
        challenge = DockerChallenge.query.filter_by(id=challenge.id).first()
        data = super().read(challenge)
        data["docker_image"] = challenge.docker_image
        data["docker_host_id"] = challenge.docker_host_id
        return data

    @classmethod
    def update(cls, challenge, request):
        data = request.form or request.get_json()
        for attr, value in data.items():
            if attr in ("requirements", "next_id"):
                continue
            setattr(challenge, attr, value)
        db.session.commit()
        return challenge

    @classmethod
    def delete(cls, challenge):
        for t in DockerChallengeTracker.query.filter_by(challenge_id=challenge.id).all():
            remove_tracked(t)
        Fails.query.filter_by(challenge_id=challenge.id).delete()
        Solves.query.filter_by(challenge_id=challenge.id).delete()
        Flags.query.filter_by(challenge_id=challenge.id).delete()
        for f in ChallengeFiles.query.filter_by(challenge_id=challenge.id).all():
            delete_file(f.id)
        ChallengeFiles.query.filter_by(challenge_id=challenge.id).delete()
        Tags.query.filter_by(challenge_id=challenge.id).delete()
        Hints.query.filter_by(challenge_id=challenge.id).delete()
        DockerChallenge.query.filter_by(id=challenge.id).delete()
        Challenges.query.filter_by(id=challenge.id).delete()
        db.session.commit()

    @classmethod
    def attempt(cls, challenge, request):
        data = request.form or request.get_json()
        submission = data["submission"].strip()
        for flag in Flags.query.filter_by(challenge_id=challenge.id).all():
            if get_flag_class(flag.type).compare(flag, submission):
                return True, "Correct"
        return False, "Incorrect"

    @classmethod
    def solve(cls, user, team, challenge, request):
        owner_id = team.id if is_teams_mode() else user.id
        q = owner_filter(DockerChallengeTracker.query, owner_id).filter_by(challenge_id=challenge.id)
        for t in q.all():
            remove_tracked(t)
        super().solve(user, team, challenge, request)


# --------------------------------------------------------------------------- #
# API
# --------------------------------------------------------------------------- #
container_namespace = Namespace("container", description="Start/stop/revert player containers")


@container_namespace.route("", methods=["POST"])
class ContainerAPI(Resource):
    @authed_only
    def post(self):
        chal_id = req_arg("id")
        chal = DockerChallenge.query.filter_by(id=chal_id).first() if chal_id else None
        if chal is None:
            return abort(403, "Unknown challenge")
        if chal.state != "visible" and not is_admin():
            return abort(403, "Challenge not available")
        docker = get_host(chal.docker_host_id)
        if docker is None:
            return abort(403, "This challenge has no Docker host set. Tell an admin.")

        if is_teams_mode():
            owner = get_current_team()
            if owner is None:
                return abort(403, "You need a team first")
        else:
            owner = get_current_user()

        # One start/stop/revert at a time per player (or team): stops double-clicks
        # and two teammates from creating two containers at once.
        lock_key = "docker_owner_lock_%s_%s" % ("team" if is_teams_mode() else "user", owner.id)
        if not cache.add(lock_key, 1, timeout=60):
            return abort(429, "Your previous request is still running, try again in a few seconds.")
        try:
            return self._handle(chal, docker, owner)
        finally:
            cache.delete(lock_key)

    def _handle(self, chal, docker, owner):
        sweep_stale()
        prune_dead(owner.id)
        now = unix_time(datetime.utcnow())

        # Remove this owner's expired containers
        for t in owner_filter(DockerChallengeTracker.query, owner.id).all():
            if t.expires is not None and now >= int(t.expires):
                remove_tracked(t)

        check = owner_filter(DockerChallengeTracker.query, owner.id).filter_by(challenge_id=chal.id).first()

        cooldown_key = "docker_cooldown_%s_%s_%s" % ("team" if is_teams_mode() else "user", owner.id, chal.id)

        # Stop: allowed any time
        if req_arg("stop"):
            if check is None:
                return abort(403, "No running container for this challenge")
            remove_tracked(check)
            if docker.cooldown_seconds:
                cache.set(cooldown_key, now + docker.cooldown_seconds, timeout=docker.cooldown_seconds)
            return {"success": True, "result": "Container stopped"}

        # Revert: only once revert_time is reached
        if check is not None and now < int(check.revert_time or 0):
            wait = int(check.revert_time) - now
            return abort(
                403,
                "To prevent abuse, Revert unlocks %s after starting (%d:%02d left). You can Stop any time."
                % (_human(docker.revert_seconds), wait // 60, wait % 60),
            )
        if check is not None:
            remove_tracked(check)
        else:
            ready_at = cache.get(cooldown_key)
            if ready_at and int(ready_at) > now:
                return abort(403, "Please wait %d seconds before starting a new container." % (int(ready_at) - now))

        other = owner_filter(DockerChallengeTracker.query, owner.id).first()
        if other is not None:
            return abort(
                403,
                "Another container is already running for challenge:<br><i><b>%s</b></i>.<br>"
                "Please stop it first. You can only run one container." % other.challenge,
            )

        try:
            allowed = get_repositories(docker, tags=True, repos=docker.repositories)
            if chal.docker_image not in allowed:
                return abort(403, "Image for this challenge is not available. Tell an admin.")
            cid, ports = create_container(docker, chal.docker_image, owner.name)
        except DockerError as e:
            traceback.print_exc()
            return abort(500, "Could not start container: %s" % e)

        entry = DockerChallengeTracker(
            team_id=str(owner.id) if is_teams_mode() else None,
            user_id=str(owner.id) if not is_teams_mode() else None,
            docker_image=chal.docker_image,
            timestamp=now,
            revert_time=now + docker.revert_seconds,
            expires=(now + docker.lifetime_seconds) if docker.lifetime_seconds else None,
            instance_id=cid,
            ports=",".join(str(p) for p in ports),
            host=docker.display_host,
            host_id=docker.id,
            challenge_id=chal.id,
            challenge=chal.name,
        )
        db.session.add(entry)
        db.session.commit()
        return {"success": True}


active_docker_namespace = Namespace("docker_status", description="Current user's/team's containers")


@active_docker_namespace.route("", methods=["GET"])
class DockerStatus(Resource):
    @authed_only
    def get(self):
        sweep_stale()
        if is_teams_mode():
            owner = get_current_team()
            if owner is None:
                return {"success": True, "data": []}
        else:
            owner = get_current_user()
        prune_dead(owner.id)
        data = []
        for t in owner_filter(DockerChallengeTracker.query, owner.id).all():
            data.append(
                {
                    "id": t.id,
                    "challenge_id": t.challenge_id,
                    "docker_image": t.docker_image,
                    "timestamp": t.timestamp,
                    "revert_time": t.revert_time,
                    "expires": t.expires,
                    "instance_id": t.instance_id,
                    "ports": t.ports.split(",") if t.ports else [],
                    "host": t.host,
                }
            )
        return {"success": True, "data": data}


docker_namespace = Namespace("docker", description="Images for challenge creation (admin)")


@docker_namespace.route("", methods=["GET"])
class DockerAPI(Resource):
    @admins_only
    def get(self):
        docker = get_host(request.args.get("host_id"))
        if docker is None:
            return {"success": False, "data": [{"name": "Pick a Docker host first"}]}, 400
        try:
            images = get_repositories(docker, tags=True, repos=docker.repositories)
        except Exception as e:
            return {"success": False, "data": [{"name": "Error talking to %s: %s" % (docker.name, e)}]}, 400
        if not images:
            return {"success": False, "data": [{"name": "No allowed images on %s" % docker.name}]}, 400
        return {"success": True, "data": [{"name": i} for i in images]}


hosts_namespace = Namespace("docker_hosts", description="List Docker hosts (admin)")


@hosts_namespace.route("", methods=["GET"])
class DockerHostsAPI(Resource):
    @admins_only
    def get(self):
        hosts = DockerConfig.query.order_by(DockerConfig.id).all()
        return {"success": True, "data": [{"id": h.id, "name": h.name, "public_host": h.display_host} for h in hosts]}


kill_container = Namespace("nuke", description="Admin: kill containers")


@kill_container.route("", methods=["POST"])
class KillContainerAPI(Resource):
    @admins_only
    def post(self):
        container = req_arg("container")
        if req_arg("all") is True:
            for t in DockerChallengeTracker.query.all():
                remove_tracked(t)
            return True
        t = DockerChallengeTracker.query.filter_by(instance_id=container).first()
        if t is None:
            return False
        remove_tracked(t)
        return True


# --------------------------------------------------------------------------- #
# Plugin entry
# --------------------------------------------------------------------------- #
def migrate_columns():
    """Add columns introduced after a host/tracker table already existed.

    create_all() only creates missing TABLES, not missing columns, so an
    existing CTFd database would crash on the new fields without this.
    """
    from sqlalchemy import inspect, text

    insp = inspect(db.engine)
    tables = set(insp.get_table_names())
    wanted = {
        "docker_hosts": [
            ("lifetime_min", "INTEGER DEFAULT %d" % DEFAULT_LIFETIME_MIN),
            ("revert_min", "INTEGER DEFAULT %d" % DEFAULT_REVERT_MIN),
            ("cooldown_sec", "INTEGER DEFAULT %d" % DEFAULT_COOLDOWN_SEC),
        ],
        "docker_host_tracker": [("expires", "INTEGER")],
    }
    with db.engine.begin() as conn:
        for table, cols in wanted.items():
            if table not in tables:
                continue
            have = {c["name"] for c in insp.get_columns(table)}
            for name, ddl in cols:
                if name in have:
                    continue
                conn.execute(text("ALTER TABLE %s ADD COLUMN %s %s" % (table, name, ddl)))
                if table == "docker_host_tracker" and name == "expires":
                    # instances already running keep the old fixed 2 h lifetime
                    conn.execute(text(
                        "UPDATE docker_host_tracker SET expires = timestamp + %d WHERE expires IS NULL"
                        % LEGACY_LIFETIME_SEC
                    ))


def load(app):
    app.db.create_all()
    migrate_columns()
    CHALLENGE_CLASSES["docker"] = DockerChallengeType

    @app.template_filter("datetimeformat")
    def datetimeformat(value, format="%Y-%m-%d %H:%M:%S"):
        return datetime.fromtimestamp(value).strftime(format)

    register_plugin_assets_directory(app, base_path="/plugins/docker_challenges/assets")
    define_docker_admin(app)
    define_docker_status(app)
    CTFd_API_v1.add_namespace(docker_namespace, "/docker")
    CTFd_API_v1.add_namespace(hosts_namespace, "/docker_hosts")
    CTFd_API_v1.add_namespace(container_namespace, "/container")
    CTFd_API_v1.add_namespace(active_docker_namespace, "/docker_status")
    CTFd_API_v1.add_namespace(kill_container, "/nuke")
