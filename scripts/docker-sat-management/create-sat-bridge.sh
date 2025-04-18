#!/bin/bash

# Usage: ./create-sat.net.sh <SAT_HOST_CIDR> [SAT_HOST] [SSH_USERNAME]
# Example: ./create-sat-net.sh 172.20.0.0/16
#          ./create-sat-net.sh 172.20.0.0/16 192.168.1.10 alice

# Enable command echoing
#set -x

# Input arguments
SAT_HOST_CIDR="$1"
SAT_HOST="${2:-127.0.0.1}"        # Default to localhost if not provided
SSH_USERNAME="${3:-$(whoami)}"      # Default to 'ubuntu' if not provided
SAT_HOST_BRIDGE_NAME="sat-bridge"

# Validate required argument
if [ -z "$SAT_HOST_CIDR" ]; then
  echo "Usage: $0 <SAT_HOST_CIDR> [SAT_HOST] [SSH_USERNAME]"
  exit 1
fi

# Check if the Docker network already exists on the remote host
if ssh "$SSH_USERNAME@$SAT_HOST" docker network inspect "$SAT_HOST_BRIDGE_NAME" >/dev/null 2>&1; then
  echo "Docker network '$SAT_HOST_BRIDGE_NAME' already exists on $SAT_HOST. Skipping creation."
else
  echo "Creating Docker network '$SAT_HOST_BRIDGE_NAME' on $SAT_HOST with subnet $SAT_HOST_CIDR..."
  ssh "$SSH_USERNAME@$SAT_HOST" docker network create \
    --driver=bridge \
    --subnet="$SAT_HOST_CIDR" \
    -o com.docker.network.bridge.enable_ip_masquerade=false \
    "$SAT_HOST_BRIDGE_NAME"
  echo "✅ Docker network '$SAT_HOST_BRIDGE_NAME' created on $SAT_HOST."
fi

