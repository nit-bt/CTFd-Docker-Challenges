#!/usr/bin/env bash
# Run on a CHALLENGE server. Exposes the Docker API on :2376 with TLS + client certs.
#
#   sudo ./setup-docker-tls.sh <private-ip> [public-ip-or-domain ...]
#
# Example (GCP):  sudo ./setup-docker-tls.sh 10.148.0.5
#
# Output:
#   /etc/docker/tls/            server certs (stay on this box)
#   ~/docker-client-certs/      ca.pem, cert.pem, key.pem -> upload these in CTFd > Docker Config
set -euo pipefail

if [[ $EUID -ne 0 ]]; then echo "Run with sudo"; exit 1; fi
if [[ $# -lt 1 ]]; then echo "Usage: sudo $0 <private-ip> [extra-ip-or-dns ...]"; exit 1; fi

if ! systemctl cat docker.service >/dev/null 2>&1; then
  echo "Docker isn't installed on this server yet."
  echo "Install it first (ctf-infra: scripts/install-docker.sh), then rerun this."
  exit 1
fi

# The first IP must belong to THIS machine, or CTFd's TLS check will fail later
LOCAL_IPS=" $(hostname -I) "
if [[ $1 =~ ^[0-9.]+$ && $LOCAL_IPS != *" $1 "* ]]; then
  echo "!! $1 is not an IP of this machine."
  echo "   This machine has:$LOCAL_IPS"
  echo "   Use this server's own private IP (first one from 'hostname -I')."
  read -r -p "Continue anyway? [y/N] " answer || answer=n
  [[ $answer == y || $answer == Y ]] || exit 1
fi

TLS_DIR=/etc/docker/tls
REAL_USER=${SUDO_USER:-root}
REAL_HOME=$(getent passwd "$REAL_USER" | cut -d: -f6)
OUT_DIR="$REAL_HOME/docker-client-certs"
DAYS=825

# SAN list: every IP/DNS the platform might use to reach this box
SAN="IP:127.0.0.1"
for h in "$@"; do
  if [[ $h =~ ^[0-9.]+$ ]]; then SAN="$SAN,IP:$h"; else SAN="$SAN,DNS:$h"; fi
done

mkdir -p "$TLS_DIR" "$OUT_DIR"
cd "$TLS_DIR"

echo "[1/5] CA"
[[ -f ca-key.pem ]] || openssl genrsa -out ca-key.pem 4096
openssl req -new -x509 -days $DAYS -key ca-key.pem -sha256 -subj "/CN=ctf-docker-ca" -out ca.pem

echo "[2/5] Server cert ($SAN)"
openssl genrsa -out server-key.pem 4096
openssl req -subj "/CN=$(hostname)" -sha256 -new -key server-key.pem -out server.csr
printf "subjectAltName = %s\nextendedKeyUsage = serverAuth\n" "$SAN" > extfile.cnf
openssl x509 -req -days $DAYS -sha256 -in server.csr -CA ca.pem -CAkey ca-key.pem \
  -CAcreateserial -out server-cert.pem -extfile extfile.cnf

echo "[3/5] Client cert (for CTFd)"
openssl genrsa -out key.pem 4096
openssl req -subj "/CN=ctfd-client" -new -key key.pem -out client.csr
echo "extendedKeyUsage = clientAuth" > extfile-client.cnf
openssl x509 -req -days $DAYS -sha256 -in client.csr -CA ca.pem -CAkey ca-key.pem \
  -CAcreateserial -out cert.pem -extfile extfile-client.cnf

rm -f server.csr client.csr extfile.cnf extfile-client.cnf
chmod 0400 ca-key.pem server-key.pem key.pem
chmod 0444 ca.pem server-cert.pem cert.pem

cp ca.pem cert.pem key.pem "$OUT_DIR/"
chown -R "$REAL_USER":"$REAL_USER" "$OUT_DIR"
chmod 0600 "$OUT_DIR"/key.pem

echo "[4/5] Docker daemon config"
cat > /etc/docker/daemon.json <<EOF
{
  "hosts": ["unix:///var/run/docker.sock", "tcp://0.0.0.0:2376"],
  "tls": true,
  "tlsverify": true,
  "tlscacert": "$TLS_DIR/ca.pem",
  "tlscert": "$TLS_DIR/server-cert.pem",
  "tlskey": "$TLS_DIR/server-key.pem",
  "log-driver": "json-file",
  "log-opts": { "max-size": "10m", "max-file": "3" }
}
EOF
# Ubuntu's docker.service passes -H fd://, which conflicts with "hosts" above
mkdir -p /etc/systemd/system/docker.service.d
cat > /etc/systemd/system/docker.service.d/override.conf <<EOF
[Service]
ExecStart=
ExecStart=/usr/bin/dockerd --containerd=/run/containerd/containerd.sock
EOF

echo "[5/5] Restart Docker"
systemctl daemon-reload
systemctl restart docker
sleep 2

echo
echo "=== Test (must print OK) ==="
curl -s --cacert "$OUT_DIR/ca.pem" --cert "$OUT_DIR/cert.pem" --key "$OUT_DIR/key.pem" \
  "https://127.0.0.1:2376/_ping"; echo
echo
echo "Without client cert (must FAIL):"
curl -s -k --max-time 3 "https://127.0.0.1:2376/_ping" && echo "!! NOT SAFE" || echo "rejected, good"
echo
echo "Client certs for CTFd are in: $OUT_DIR"
echo "Copy them to your laptop, then upload in CTFd > Docker Config."
