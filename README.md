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
| Teams mode | "One container" check crashes | Works (one container per team) |
| Dead server | Can hang CTFd | 10s timeout, clean error, UP/DOWN shown in admin |

| Stop | Only after 5 min | Any time (30s cooldown before restarting that challenge) |
| Hidden challenges | Can't be tested | Admins can start instances to test before publishing |

Unchanged: one container per user/team at a time, revert after 5 min, auto-remove after 2h, container killed on solve, admin status page.

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

## Add servers in CTFd

**Admin → Plugins → Docker Config → + Add host**

| Field | Example |
|---|---|
| Name | `web-srv` |
| Docker API address | `10.148.0.5:2376` (private IP) |
| Public address | `chal1.example.com` or the public IP |
| Port min / max | `30000` / `31000` (must match firewall) |
| Limits | 256 MB, 0.5 CPU, 256 processes |
| TLS | Yes + upload the 3 files |

Save, then pick the **allowed repositories** and save again.

## Create a challenge

**Challenges → New → type `docker`** → pick **Docker Host**, then **Docker Image**.
Connection info: `http://host:port` or `nc host port`. `host` and `port` get filled in for each player.

## Tested

73 end-to-end checks on CTFd 3.7.7 (Users + Teams mode) against two fake Docker hosts, one plain HTTP and one TLS with client certs: host config, image filtering, start/stop/revert, stop cooldown, port ranges, limits, one-container rule, solve cleanup, admin nuke, dead host, challenge delete.
