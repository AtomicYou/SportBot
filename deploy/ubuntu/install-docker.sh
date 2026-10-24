#!/usr/bin/env bash
set -euo pipefail
if [[ ${EUID} -ne 0 ]]; then echo 'Run: sudo bash deploy/ubuntu/install-docker.sh' >&2; exit 1; fi
source /etc/os-release
if [[ ${ID} != ubuntu ]]; then echo 'This installer supports Ubuntu only.' >&2; exit 1; fi
case "$(dpkg --print-architecture)" in amd64|arm64) ;; *) echo 'SportBot requires amd64 or arm64 for Chromium.' >&2; exit 1;; esac
if command -v docker >/dev/null 2>&1; then
    docker compose version
    systemctl enable --now docker
    echo 'Existing Docker installation preserved.'
    exit 0
fi
for package in docker.io docker-compose docker-compose-v2 docker-doc docker-buildx podman-docker containerd runc; do
    if dpkg-query -W -f='${Status}' "$package" 2>/dev/null | grep -q 'install ok installed'; then
        echo "Conflicting package: $package. Review the existing NAS containers before changing Docker packages." >&2
        exit 1
    fi
done
apt-get update
apt-get install -y ca-certificates curl git python3
install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
chmod a+r /etc/apt/keyrings/docker.asc
cat > /etc/apt/sources.list.d/docker.sources <<EOF
Types: deb
URIs: https://download.docker.com/linux/ubuntu
Suites: ${UBUNTU_CODENAME:-$VERSION_CODENAME}
Components: stable
Architectures: $(dpkg --print-architecture)
Signed-By: /etc/apt/keyrings/docker.asc
EOF
apt-get update
apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
systemctl enable --now docker
docker compose version
echo 'Docker installed. No user was added to the privileged docker group.'
