#!/bin/bash

# Usage:
# ./assign_bridge_ips.sh <SAT_NAME> <N_ANTENNAS> <SAT_NET_CIDR> [SAT_HOST] [SSH_USERNAME]
# Example:
# ./assign_bridge_ips.sh sat1 3 192.168.100.0/24
# ./assign_bridge_ips.sh sat1 3 192.168.100.0/24 192.168.1.100 alice

set -e

# Input arguments
SAT_NAME="$1"
N_ANTENNAS="$2"
SAT_NET_CIDR="$3"
SAT_HOST="${4:-127.0.0.1}"
SSH_USERNAME="${5:-$(whoami)}"

# Validate input
if [ -z "$SAT_NAME" ] || [ -z "$N_ANTENNAS" ] || [ -z "$SAT_NET_CIDR" ]; then
  echo "Usage: $0 <SAT_NAME> <N_ANTENNAS> <SAT_NET_CIDR> [SAT_HOST] [SSH_USERNAME]"
  exit 1
fi

# Check if the container exists on the remote host
if ! ssh "$SSH_USERNAME@$SAT_HOST" docker ps -a --format '{{.Names}}' | grep -Fxq "$SAT_NAME"; then
  echo "❌ Container '$SAT_NAME' does not exist on host '$SAT_HOST'. Aborting."
  exit 1
fi

# Extract base IP and subnet
IP_BASE=$(echo "$SAT_NET_CIDR" | cut -d'.' -f1-3)
SUBNET=$(echo "$SAT_NET_CIDR" | cut -d'/' -f2)

# Loop to assign IPs
for ((i=1; i<=N_ANTENNAS; i++)); do
  BRIDGE_NAME="br$i"
  IP_ADDR="${IP_BASE}.$i/32"

  echo "Checking if $BRIDGE_NAME exists in container $SAT_NAME on $SAT_HOST..."

  if ssh "$SSH_USERNAME@$SAT_HOST" docker exec "$SAT_NAME" ip link show "$BRIDGE_NAME" >/dev/null 2>&1; then
    echo "Assigning $IP_ADDR to $BRIDGE_NAME"
    ssh "$SSH_USERNAME@$SAT_HOST" docker exec "$SAT_NAME" ip addr add "$IP_ADDR" dev "$BRIDGE_NAME"
    ssh "$SSH_USERNAME@$SAT_HOST" docker exec "$SAT_NAME" ip link set "$BRIDGE_NAME" up
  else
    echo "⚠️  Bridge $BRIDGE_NAME does not exist in container $SAT_NAME. Skipping."
  fi
done

echo "✅ Finished assigning IPs from $SAT_NET_CIDR to available bridges in $SAT_NAME on $SAT_HOST."
