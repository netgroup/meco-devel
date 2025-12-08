#!/bin/bash

# Usage: ./setup_ssh.sh <username> "<ssh-public-key>"

USERNAME=$1
PUBKEY=$2
SSH_DIR="/home/$USERNAME/.ssh"
AUTH_KEYS="$SSH_DIR/authorized_keys"

# Create .ssh directory
sudo mkdir -p "$SSH_DIR"

# Add public key to authorized_keys
echo "$PUBKEY" | sudo tee -a "$AUTH_KEYS" > /dev/null

# Set ownership and permissions
sudo chown -R "$USERNAME:$USERNAME" "$SSH_DIR"
sudo chmod 700 "$SSH_DIR"
sudo chmod 600 "$AUTH_KEYS"

echo "SSH key setup completed for user: $USERNAME"