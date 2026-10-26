#!/usr/bin/env bash
# Lock down a CTF challenge server. Run AFTER setup-docker-tls.sh.
#
#   sudo ./firewall/harden-chal-host.sh <platform-ip>
#
# <platform-ip> = the IP the CTFd server uses to reach this box
#   GCP (same VPC)        : platform's INTERNAL IP (10.x.x.x)
#   Contabo / no VPC      : platform's PUBLIC IP
#
# After this:
#   players -> challenge ports   OK
#   containers -> internet / metadata / internal network / host / each other   BLOCKED
#   Docker API :2376             only from <platform-ip>
set -euo pipefail

if [[ $EUID -ne 0 ]]; then echo "Run with sudo"; exit 1; fi
if [[ $# -lt 1 ]]; then echo "Usage: sudo $0 <platform-ip>"; exit 1; fi
PLATFORM_IP=$1
if ! [[ $PLATFORM_IP =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]]; then echo "Not an IPv4 address: $PLATFORM_IP"; exit 1; fi
HERE=$(cd "$(dirname "$0")" && pwd)

echo "[1/4] Install firewall rules + boot service"
install -m 0755 "$HERE/ctf-docker-firewall" /usr/local/sbin/ctf-docker-firewall
cat > /etc/default/ctf-docker-firewall <<EOF
PLATFORM_IP=$PLATFORM_IP
API_PORT=2376
BRIDGE=docker0
EOF
cat > /etc/systemd/system/ctf-docker-firewall.service <<'EOF'
[Unit]
Description=CTF challenge-server firewall (block container egress, restrict Docker API)
After=docker.service
PartOf=docker.service

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/usr/local/sbin/ctf-docker-firewall apply
ExecStop=/usr/local/sbin/ctf-docker-firewall remove

[Install]
WantedBy=multi-user.target docker.service
EOF

echo "[2/4] Docker: containers can't talk to each other (icc=false)"
python3 - <<'EOF'
import json, os
p = "/etc/docker/daemon.json"
cfg = json.load(open(p)) if os.path.exists(p) else {}
cfg["icc"] = False
json.dump(cfg, open(p, "w"), indent=2)
EOF

echo "[3/4] Restart Docker + enable firewall"
systemctl daemon-reload
systemctl restart docker
systemctl enable --now ctf-docker-firewall.service
systemctl restart ctf-docker-firewall.service
/usr/local/sbin/ctf-docker-firewall status

echo
echo "[4/4] Self-test (pulls busybox + curl images)"
docker pull -q busybox:latest >/dev/null
docker pull -q curlimages/curl:latest >/dev/null
docker rm -f ctf-fwtest-srv >/dev/null 2>&1 || true
docker run -d --name ctf-fwtest-srv -p 30999:8080 busybox httpd -f -p 8080 >/dev/null
sleep 1
SRV_IP=$(docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' ctf-fwtest-srv)
HOST_IP=$(hostname -I | awk '{print $1}')

PASS=0; FAIL=0
probe() {  # probe <description> <host:port> <want: open|blocked>
    # raw TCP connect from inside a container; time_connect stays 0 if it never connected
    t=$(docker run --rm curlimages/curl -s --connect-timeout 3 --max-time 4 -o /dev/null \
        -w '%{time_connect}' "telnet://$2" 2>/dev/null || true)
    if [[ -z $t || $t == 0 || $t == 0.000000 ]]; then got=blocked; else got=open; fi
    if [[ $got == "$3" ]]; then PASS=$((PASS+1)); r=PASS; else FAIL=$((FAIL+1)); r=FAIL; fi
    printf "  %s  %-45s %s\n" "$r" "$1" "$got"
}
probe "container -> internet"              "1.1.1.1:80"              blocked
probe "container -> cloud metadata"        "169.254.169.254:80"      blocked
probe "container -> other container"       "$SRV_IP:8080"            blocked
probe "container -> host Docker API"       "$HOST_IP:2376"           blocked
probe "container -> host SSH"              "$HOST_IP:22"             blocked

code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 3 http://127.0.0.1:30999/ || true)
if [[ $code != "000" ]]; then PASS=$((PASS+1)); echo "  PASS  published port 30999 reachable           open"
else FAIL=$((FAIL+1)); echo "  FAIL  published port 30999 reachable           blocked"; fi
docker rm -f ctf-fwtest-srv >/dev/null

echo
echo "Self-test: $PASS passed, $FAIL failed"
if [[ $FAIL -gt 0 ]]; then
    echo "!! Something is wrong. Rules are still installed; check: sudo ctf-docker-firewall status"
    exit 1
fi
cat <<EOF

Done. Rules re-apply on every boot and Docker restart.
  Check rules : sudo ctf-docker-firewall status
  Turn off    : sudo systemctl stop ctf-docker-firewall

Still to do:
  - From your laptop, start a challenge in CTFd and open it -> should work
  - GCP only: give this VM NO service account
      (VM -> Stop -> Edit -> Security -> Service account: "No service account" -> Save -> Start)
EOF
