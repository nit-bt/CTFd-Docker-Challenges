# CTFd Docker Challenges (multi-host fork)

Fork of [offsecginger/CTFd-Docker-Challenges](https://github.com/offsecginger/CTFd-Docker-Challenges) for **CTFd 3.7.7**.
Lets players spin up their own container per challenge, and lets you spread challenges across **several Docker servers**.

## What's different from upstream

| | Upstream | This fork |
|---|---|---|
| Docker servers | 1 | Many. Each challenge picks its server |
| Address shown to players | The Docker API address (often a private IP) | Separate **public address** per server |
| Player ports | Random 30000-60000 | Per-server range (default 30000-31000) |
| Container limits | None | Per-server memory / CPU / max processes |
| Instance times | Fixed in code (2 h lifetime, 5 min revert) | Per server on the Docker Config page: lifetime (0 = no limit), revert after, cooldown after Stop. Players see a countdown |
| Teams mode | "One container" check crashes | Works (one container per team) |
| Dead server | Can hang CTFd | 10s timeout, clean error, UP/DOWN shown in admin |

| Stop | Only after 5 min | Any time (cooldown before restarting that challenge, default 30 s) |
| Old containers (2 h+) | Removed only when the *same* player starts another one, so abandoned ones run forever | Removed for **everyone**, checked at most once a minute whenever anyone uses a docker challenge, and every time an admin opens Docker Status |
| Busy starts (many players at once) | Two players can get the same random port → "driver failed programming external connectivity" | Ports reserved in Redis first (atomic), plus up to 3 retries with fresh ports if Docker still says a port is taken |
| Double-click Start | Can create 2 containers | One request at a time per player/team; a second one gets "still running, try again" |
| Instance died (OOM-killed, crashed, removed by hand) | CTFd still shows it as running; player must Stop, wait, Start | Detected when the player opens the challenge or presses Start; Start button comes back right away, no cooldown. Unreachable hosts are left alone |
| Docker answers with an error | Unhandled crash (generic 500) | Read calls retried once; otherwise a clean "Could not start container: …" message |
| Hidden challenges | Can't be tested | Admins can start instances to test before publishing |
| Start / stop / nuke | GET (a malicious link could trigger it) | POST + CSRF token |
| Privilege escalation in containers | Allowed | `no-new-privileges` |

Unchanged: one container per user/team at a time, revert after 5 min, container killed on solve, admin status page.

## Install

```bash
cd /opt/ctf
git clone https://github.com/<you>/CTFd-Docker-Challenges.git
cp -r CTFd-Docker-Challenges/docker_challenges CTFd/CTFd/plugins/
cd CTFd && sudo docker compose restart ctfd
```

Use a **fresh database**. This fork uses new table names, so it won't pick up data from the upstream plugin.

## Set up a challenge server

On each challenge server (Docker already installed):

```bash
sudo ./setup-docker-tls.sh <private-ip>
```

This turns on the Docker API on port 2376 with TLS + client certs, and puts `ca.pem`, `cert.pem`, `key.pem` in `~/docker-client-certs/`.
Firewall: 2376 only from the CTFd server, the player port range open to players.

## Lock down a challenge server (firewall)

After `setup-docker-tls.sh`:

```bash
sudo ./firewall/harden-chal-host.sh <platform-ip>
```

`<platform-ip>` = the IP CTFd uses to reach this box (GCP: platform's internal IP; Contabo: its public IP).

| Traffic | Result |
|---|---|
| Players -> challenge ports | allowed |
| Containers -> internet, cloud metadata (169.254.169.254), internal network, the host, other containers | blocked |
| Docker API :2376 | only from `<platform-ip>` |

Rules live in `/usr/local/sbin/ctf-docker-firewall` and re-apply on every boot and Docker restart. The script ends with a self-test from inside a real container.
Check: `sudo ctf-docker-firewall status` · Turn off: `sudo systemctl stop ctf-docker-firewall`

Note: containers have no outbound network at all (no DNS, no downloads). Bake everything a challenge needs into its image.

On GCP also give the challenge VM **no service account**.

## Add servers in CTFd

**Admin → Plugins → Docker Config → + Add host**

| Field | Example |
|---|---|
| Name | `web-srv` |
| Docker API address | `10.148.0.5:2376` (private IP) |
| Public address | `chal1.example.com` or the public IP |
| Port min / max | `30000` / `31000` (must match firewall) |
| Limits | 256 MB, 0.5 CPU, 256 processes |
| Times | lifetime 120 min, revert after 5 min, cooldown 30 s |
| TLS | Yes + upload the 3 files |

Save, then pick the **allowed repositories** and save again.

## Create a challenge

**Challenges → New → type `docker`** → pick **Docker Host**, then **Docker Image**.
Connection info: `http://host:port` or `nc host port`. `host` and `port` get filled in for each player.

## Load test

On the platform server, with an admin access token (CTFd → Settings → Access Tokens):

```bash
export CTFD_TOKEN=ctfd_xxx
python3 tools/load-test.py                 # 25 hidden fake players press Start at once
```

Reports how many started, how long each took, whether each instance answers, and how they spread over the hosts. Then stops them all. Users mode only.
If `ctf-chal-ports` only allows your own IP, add `--map <chal-public-ip>=<chal-private-ip>`. `--delete-users` removes the fake players afterwards. `--help` for the rest.

## Tested

184 end-to-end checks on CTFd 3.7.7 (Users + Teams mode) against two fake Docker hosts, one plain HTTP and one TLS with client certs: host config, image filtering, start/stop/revert, stop cooldown, port ranges, limits, one-container rule, solve cleanup, admin nuke, CSRF on start/stop/nuke, dead host, challenge delete, port-clash retry, parallel port reservation, global old-container cleanup, double-start lock, dead-instance detection, Docker API errors, per-host times, upgrading an existing database.

Firewall: 37 checks in a simulated challenge server (network namespaces with Docker's NAT rules), before vs after `apply`, re-apply, Docker inserting rules above ours, and `remove`.
