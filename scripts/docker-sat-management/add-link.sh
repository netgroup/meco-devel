#!/bin/bash

# Usage:
# ./add-link.sh <SRC_SAT> <SRC_ANTENNA> [SRC_SAT_HOST] <DST_SAT> <DST_ANTENNA> [DST_SAT_HOST] [SSH_USERNAME]

# Enable command echoing
# set -x

# Input arguments
SRC_SAT="$1"
SRC_ANTENNA="$2"
SRC_SAT_HOST="${3:-127.0.0.1}"
DST_SAT="$4"
DST_ANTENNA="$5"
DST_SAT_HOST="${6:-127.0.0.1}"
SSH_USERNAME="${7:-$(whoami)}"

# Validate required arguments
if [ -z "$SRC_SAT" ] || [ -z "$SRC_ANTENNA" ] || [ -z "$DST_SAT" ] || [ -z "$DST_ANTENNA" ]; then
  echo "Usage: $0 <SRC_SAT> <SRC_ANTENNA> [SRC_SAT_HOST] <DST_SAT> <DST_ANTENNA> [DST_SAT_HOST] [SSH_USERNAME]"
  exit 1
fi

# Check if the container exists on the remote host
if ! ssh "$SSH_USERNAME@$SRC_SAT_HOST" docker ps -a --format '{{.Names}}' | grep -Fxq "$SRC_SAT"; then
  echo "❌ Container '$SRC_SAT' does not exist on host '$SRC_SAT_HOST'. Aborting."
  exit 1
fi
if ! ssh "$SSH_USERNAME@$DST_SAT_HOST" docker ps -a --format '{{.Names}}' | grep -Fxq "$DST_SAT"; then
  echo "❌ Container '$DST_SAT' does not exist on host '$DST_SAT_HOST'. Aborting."
  exit 1
fi

# Interface names (shortened and customized)
vxlan_if_in_SRC="${DST_SAT}_a${DST_ANTENNA}"
vxlan_if_in_DST="${SRC_SAT}_a${SRC_ANTENNA}"

# Generate unique VXLAN VNI
vni_input="${SRC_SAT}_${SRC_ANTENNA}_${DST_SAT}_${DST_ANTENNA}"
vxlan_vni=$(echo -n "$vni_input" | cksum | awk '{print $1 % 16777215 + 1}')

# Get IPs from remote containers
SRC_SAT_IP=$(ssh "$SSH_USERNAME@$SRC_SAT_HOST" "docker exec $SRC_SAT ip -4 addr show eth0 | grep -oP '(?<=inet\\s)\\d+(\\.\\d+){3}'")
DST_SAT_IP=$(ssh "$SSH_USERNAME@$DST_SAT_HOST" "docker exec $DST_SAT ip -4 addr show eth0 | grep -oP '(?<=inet\\s)\\d+(\\.\\d+){3}'")

# Function to setup VXLAN remotely
setup_vxlan_remote() {
  local HOST="$1"
  local SSH_USERNAME="$2"
  local CONTAINER="$3"
  local REMOTE_IP="$4"
  local BRIDGE="br$5"
  local VXLAN_IF="$6"

  ssh "$SSH_USERNAME@$HOST" bash -c "'
    docker exec \"$CONTAINER\" ip link add \"$VXLAN_IF\" type vxlan id \"$vxlan_vni\" dev eth0 dstport 4789
    docker exec \"$CONTAINER\" bridge fdb append to 00:00:00:00:00:00 dst \"$REMOTE_IP\" dev \"$VXLAN_IF\"
    docker exec \"$CONTAINER\" ip link set \"$VXLAN_IF\" master \"$BRIDGE\"
    docker exec \"$CONTAINER\" ip link set dev \"$VXLAN_IF\" up
  '"
}

# Execute VXLAN setup on each remote machine
setup_vxlan_remote "$SRC_SAT_HOST" "$SSH_USERNAME" "$SRC_SAT" "$DST_SAT_IP" "$SRC_ANTENNA" "$vxlan_if_in_SRC"
setup_vxlan_remote "$DST_SAT_HOST" "$SSH_USERNAME" "$DST_SAT" "$SRC_SAT_IP" "$DST_ANTENNA" "$vxlan_if_in_DST"

# Summary
echo "========================================"
echo "VXLAN VNI       : $vxlan_vni"
echo "SRC_SAT         : $SRC_SAT ($SRC_SAT_IP@$SRC_SAT_HOST) -> $vxlan_if_in_SRC"
echo "DST_SAT         : $DST_SAT ($DST_SAT_IP@$DST_SAT_HOST) -> $vxlan_if_in_DST"
echo "Bridges         : br$SRC_ANTENNA, br$DST_ANTENNA"
echo "========================================"
