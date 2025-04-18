#!/bin/bash

# Usage:
# ./create_satellite.sh <SAT_NAME> <N_ANTENNAS> [SAT_HOST] [SSH_USERNAME]
# Example:
#   ./create_satellite.sh sat1 2
#   ./create_satellite.sh sat2 3 192.168.1.20 myuser

#set -x

# Input arguments
SAT_NAME="$1"
N_ANTENNAS="$2"
SAT_HOST="${3:-127.0.0.1}"
SSH_USERNAME="${4:-$(whoami)}"
SAT_HOST_BRIDGE_NAME="sat-bridge"

# Validate input
if [ -z "$SAT_NAME" ] || [ -z "$N_ANTENNAS" ]; then
  echo "Usage: $0 <SAT_NAME> <N_ANTENNAS> [SAT_HOST] [SSH_USERNAME]"
  exit 1
fi

# Ensure Docker network exists on remote host
if ssh "$SSH_USERNAME@$SAT_HOST" docker network inspect "$SAT_HOST_BRIDGE_NAME" >/dev/null 2>&1; then
  echo "Docker network '$SAT_HOST_BRIDGE_NAME' already exists on $SAT_HOST. Skipping creation."
else
  echo "Creating Docker network '$SAT_HOST_BRIDGE_NAME' on $SAT_HOST..."
  ssh "$SSH_USERNAME@$SAT_HOST" docker network create "$SAT_HOST_BRIDGE_NAME"
fi

# Create the satellite container
ssh "$SSH_USERNAME@$SAT_HOST" docker run -d \
  --name "$SAT_NAME" \
  --net="$SAT_HOST_BRIDGE_NAME" \
  --privileged \
  shahramdd/sat:3.1

echo "✅ Satellite container '$SAT_NAME' created on host '$SAT_HOST' and network '$SAT_HOST_BRIDGE_NAME'."
