#!/bin/bash
set -e
USER=${1:-deployer}
KEY_PATH="/home/${USER}/.ssh/github_actions"
mkdir -p "/home/${USER}/.ssh"
chmod 700 "/home/${USER}/.ssh"
if [ -f "${KEY_PATH}" ]; then
    read -p "Overwrite? (y/N): " confirm
    [[ "$confirm" =~ ^[Yy]$ ]] || exit 0
    rm -f "${KEY_PATH}" "${KEY_PATH}.pub"
fi
ssh-keygen -t ed25519 -a 100 -f "${KEY_PATH}" -N "" -C "github-actions-${USER}@$(hostname)"
cat "${KEY_PATH}.pub" >> "/home/${USER}/.ssh/authorized_keys"
chmod 600 "/home/${USER}/.ssh/authorized_keys"
chown -R "${USER}:${USER}" "/home/${USER}/.ssh"
echo ""
echo "📋 PRIVATE KEY (copy to GitHub Secret 'VPS_SSH_KEY'):"
echo "---"
cat "${KEY_PATH}"
echo "---"
