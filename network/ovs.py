import logging
import subprocess
from infra.executors import CommandExecutor, LocalExecutor

logger = logging.getLogger("meco.ovs")

# Default executor for OVS commands (usually local)
# In future, this could be injectable if managing remote switches
EXECUTOR = LocalExecutor()

def set_executor(executor: CommandExecutor):
    global EXECUTOR
    EXECUTOR = executor


def _run(cmd, check=True, target: str = None):
    """
    Runs the command. If target is provided, wraps as `incus exec target -- sudo ...`.
    Note: cmd should already start with "sudo" if check=True implies local sudo usage.
    However, meco.py prepends sudo manually.
    Let's standardize: The input cmd list usually starts with "sudo". 
    If target is provided, we construct `["incus", "exec", target, "--"] + cmd`.
    """
    final_cmd = cmd
    if target:
        # meco.py wraps "incus exec ... -- sudo ovs-..."
        # The input 'cmd' usually is ["sudo", "ovs-...", ...]
        # So prepending works fine.
        final_cmd = ["incus", "exec", target, "--"] + cmd
    
    return EXECUTOR.run(final_cmd, check=check, capture_output=True, text=True)

def bridge_exists(bridge: str, target: str = None) -> bool:
    """Checks if an OVS bridge exists."""
    cmd = ["sudo", "ovs-vsctl", "br-exists", bridge]
    try:
        # ovs-vsctl br-exists returns 0 if exists, 2 if not
        _run(cmd, check=True, target=target)
        return True
    except Exception:
        return False

def del_flows(bridge: str, target: str = None) -> bool:
    """Deletes all flows from an OVS bridge."""
    if not bridge_exists(bridge, target):
        return True
        
    cmd = ["sudo", "ovs-ofctl", "del-flows", bridge]
    try:
        _run(cmd, check=True, target=target)
        return True
    except Exception as e:
        logger.error(f"Failed to delete flows from bridge {bridge} (target={target}): {e}")
        return False

def add_flow(bridge: str, flow_rule: str, target: str = None) -> bool:
    """Adds a flow rule to an OVS bridge."""
    cmd = ["sudo", "ovs-ofctl", "add-flow", bridge, flow_rule]
    try:
        _run(cmd, check=True, target=target)
        return True
    except Exception as e:
        logger.error(f"Failed to add flow rule to {bridge} ({flow_rule}) (target={target}): {e}")
        return False

def list_ports(bridge: str, target: str = None) -> list[str]:
    """Lists ports on an OVS bridge."""
    if not bridge_exists(bridge, target):
        return []
        
    cmd = ["sudo", "ovs-vsctl", "list-ports", bridge]
    try:
        result = _run(cmd, target=target)
        return result.stdout.strip().splitlines()
    except Exception as e:
        # If bridge disappears during race, just return empty
        if "no bridge named" in str(e).lower(): return []
        logger.error(f"Failed to list ports on bridge {bridge} (target={target}): {e}")
        return []

def del_port(bridge: str, port: str, target: str = None) -> bool:
    """Deletes a port from an OVS bridge."""
    cmd = ["sudo", "ovs-vsctl", "--if-exists", "del-port", bridge, port]
    try:
        _run(cmd, check=True, target=target)
        return True
    except Exception as e:
        logger.error(f"Failed to delete port {port} from bridge {bridge} (target={target}): {e}")
        return False

def port_to_br(port: str, target: str = None) -> str | None:
    """Gets the bridge a port is attached to."""
    cmd = ["sudo", "ovs-vsctl", "port-to-br", port]
    try:
        result = _run(cmd, check=False, target=target)
        bridge = result.stdout.strip()
        return bridge if bridge else None
    except Exception:
        return None

def get_interface_ofport(interface: str, target: str = None) -> int | None:
    """Gets the OpenFlow port number for an OVS interface."""
    cmd = ["sudo", "ovs-vsctl", "get", "Interface", interface, "ofport"]
    try:
        result = _run(cmd, target=target)
        port_str = result.stdout.strip()
        # Handle cases where output might be multiple lines or error msg
        if "\n" in port_str: 
             port_str = port_str.split("\n")[-1] # Try last line if noise? usually strictly number
             
        if port_str.lstrip("-").isdigit(): # supports negative like -1
            return int(port_str)
    except Exception:
        pass
    return None

