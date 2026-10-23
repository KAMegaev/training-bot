#!/usr/bin/env bash
# Run as root on a fresh Ubuntu 24.04 server. Argument: authorized public key file.
set -euo pipefail
[[ $(id -u) -eq 0 ]] || { echo 'Run as root'; exit 1; }
key_file=${1:?Pass the public SSH key file}
ssh-keygen -lf "$key_file" >/dev/null
. /etc/os-release
[[ "$ID" == ubuntu && "$VERSION_ID" == 24.04 ]] || { echo 'Expected Ubuntu 24.04'; exit 1; }

export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq ca-certificates curl git ufw python3
if ! command -v docker >/dev/null; then
    install -m 0755 -d /etc/apt/keyrings
    curl --fail --silent --show-error https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
    chmod 0644 /etc/apt/keyrings/docker.asc
    printf 'Types: deb\nURIs: https://download.docker.com/linux/ubuntu\nSuites: noble\nComponents: stable\nArchitectures: amd64\nSigned-By: /etc/apt/keyrings/docker.asc\n' > /etc/apt/sources.list.d/docker.sources
    apt-get update -qq
    apt-get install -y -qq docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
fi
systemctl enable --now docker
if ! id trainer >/dev/null 2>&1; then
    adduser --disabled-password --gecos 'Training bot deployment' trainer
fi
# Membership is for deployments; Docker access is effectively root-level access.
usermod -aG docker trainer
install -d -m 0700 -o trainer -g trainer /home/trainer/.ssh
touch /home/trainer/.ssh/authorized_keys
if ! grep -Fqx -- "$(cat "$key_file")" /home/trainer/.ssh/authorized_keys; then
    cat "$key_file" >> /home/trainer/.ssh/authorized_keys
fi
chown trainer:trainer /home/trainer/.ssh/authorized_keys
chmod 0600 /home/trainer/.ssh/authorized_keys
install -d -m 0750 -o trainer -g trainer /opt/training-bot

# Allow SSH BEFORE enabling the firewall. Keep the existing hosting panel accessible.
ufw allow 22/tcp
ufw allow 80/tcp
ufw allow 443/tcp
ufw allow 1500/tcp
ufw allow 1501/tcp
ufw default deny incoming
ufw default allow outgoing
ufw --force enable

docker --version
docker compose version
ufw status
