import logging
import subprocess
from meco.config.loader import CONFIG
from meco.infra.executors import LocalExecutor

logger = logging.getLogger("meco.ovs")

# Helper for execution
EXECUTOR = LocalExecutor()


def set_executor(executor):
    global EXECUTOR
    EXECUTOR = executor


def _run_raw(cmd, check=False, capture_output=True):
    """
    Executes a command using the global executor.
    Arguments are adapted to match subprocess.run / executor.run style.
    """
    try:
        # Ensure cmd is list
        if isinstance(cmd, str):
            cmd = cmd.split()

        result = EXECUTOR.run(
            cmd, check=check, capture_output=capture_output, text=True
        )
        return result
    except Exception as e:
        # Wrap in a fake result object if executor raises directly, or re-raise
        # But our code expects 'result.returncode' etc.
        # If EXECUTOR.run raises CalledProcessError, we might want to catch it or let it bubble depending on 'check'
        raise e


def _construct_cmd(base_cmd: list[str], target: str = None) -> list[str]:
    """Wraps command for remote execution if target is specified."""
    if target:
        return ["incus", "exec", target, "--"] + base_cmd
    return base_cmd


def bridge_exists(bridge: str, target: str = None) -> bool:
    """Checks if an OVS bridge exists (target specific or local)."""
    cmd = _construct_cmd(["sudo", "ovs-vsctl", "br-exists", bridge], target)
    try:
        _run_raw(cmd, check=True)
        return True
    except Exception:
        return False


def add_bridge(bridge: str, target: str = None) -> bool:
    """Creates an OVS bridge."""
    cmd = _construct_cmd(["sudo", "ovs-vsctl", "--may-exist", "add-br", bridge], target)
    try:
        _run_raw(cmd, check=True)
        return True
    except Exception as e:
        logger.error(f"Failed to create bridge {bridge} on {target}: {e}")
        return False


def del_flows(bridge: str, target: str = None) -> bool:
    """
    Deletes all flows from an OVS bridge.
    If target is None, follows user logic: try local, then try all remotes (Broadcast cleanup?)
    Refined: If target is None, we attempt to find the bridge?
    User's logic was: Try local, fail? Try remotes.
    Notes: User's intent for 'del_flows(br)' without target seems to be 'Cleanup everywhere'.
    But for 'manager.py', target is usually passed.
    """
    # 1. Try Specific Target if provided
    if target:
        cmd = _construct_cmd(["sudo", "ovs-ofctl", "del-flows", bridge], target)
        try:
            _run_raw(cmd, check=True)
            logger.info(f"Cleared flows from {bridge} on {target}")
            return True
        except Exception as e:
            logger.debug(f"del-flows failed on {target} for {bridge}: {e}")
            return False

    # 2. Heuristic / Fallback (User Logic) if target=None
    # Try Local
    cmd_local = ["sudo", "ovs-ofctl", "del-flows", bridge]
    try:
        if _run_raw(cmd_local, check=True).returncode == 0:
            logger.info(f"Cleared flows from {bridge} (local)")
            return True
    except Exception as e:
        logger.debug(f"Local del-flows failed: {e} - trying remotes")

    # Try Remotes
    hypervisors = CONFIG.get("hypervisors", {})
    for hv in hypervisors:
        cmd_remote = _construct_cmd(["sudo", "ovs-ofctl", "del-flows", bridge], hv)
        try:
            _run_raw(cmd_remote, check=True)
            logger.info(f"Cleared flows from {bridge} on {hv}")
            return True  # Return on first success? User code returns True.
        except Exception:
            continue

    logger.error(f"Failed to delete flows from {bridge} (tried local + remotes)")
    return False


def add_flow(bridge: str, flow_rule: str, target: str = None) -> bool:
    """Adds a flow rule."""
    base_cmd = ["sudo", "ovs-ofctl", "add-flow", bridge, flow_rule]
    logger.debug(f"Adding flow to {bridge} (target={target}): {flow_rule}")

    if target:
        cmd = _construct_cmd(base_cmd, target)
        try:
            _run_raw(cmd, check=True)
            return True
        except Exception as e:
            logger.error(f"Failed to add flow on {target}: {e}")
            return False

    # Fallback/Search
    # Try local
    try:
        if _run_raw(base_cmd, check=True).returncode == 0:
            logger.info(f"Added flow to {bridge} (local)")
            return True
    except Exception:
        pass

    for hv in CONFIG.get("hypervisors", {}):
        cmd = _construct_cmd(base_cmd, hv)
        try:
            _run_raw(cmd, check=True)
            logger.info(f"Added flow to {bridge} on {hv}")
            return True
        except Exception:
            pass

    logger.error(f"Failed to add flow to {bridge}")
    return False


def list_ports(bridge: str, target: str = None) -> list[str]:
    """Lists ports."""
    base_cmd = ["sudo", "ovs-vsctl", "list-ports", bridge]

    if target:
        cmd = _construct_cmd(base_cmd, target)
        try:
            res = _run_raw(cmd, check=True)
            return res.stdout.strip().splitlines()
        except Exception:
            return []

    # Fallback
    try:
        res = _run_raw(base_cmd, check=True)
        return res.stdout.strip().splitlines()
    except Exception:
        pass

    for hv in CONFIG.get("hypervisors", {}):
        cmd = _construct_cmd(base_cmd, hv)
        try:
            res = _run_raw(cmd, check=True)
            return res.stdout.strip().splitlines()
        except Exception:
            continue
    return []


def add_patch_port(bridge: str, port: str, peer: str, target: str = None) -> bool:
    """Adds a patch port connecting to a peer."""
    # ovs-vsctl --may-exist add-port <bridge> <port> -- set Interface <port> type=patch options:peer=<peer>
    cmd = [
        "sudo",
        "ovs-vsctl",
        "--may-exist",
        "add-port",
        bridge,
        port,
        "--",
        "set",
        "Interface",
        port,
        "type=patch",
        f"options:peer={peer}",
    ]

    if target:
        try:
            _run_raw(_construct_cmd(cmd, target), check=True)
            return True
        except Exception as e:
            logger.error(f"Failed to add patch port {port} on {target}: {e}")
            return False

    # Local
    try:
        _run_raw(cmd, check=True)
        return True
    except Exception as e:
        logger.error(f"Failed to add patch port {port} locally: {e}")
        return False


def add_vxlan_port(
    bridge: str, port: str, remote_ip: str, key: str = "flow", target: str = None
) -> bool:
    """Adds a VXLAN tunnel port."""
    # ovs-vsctl add-port <bridge> <port> -- set interface <port> type=vxlan options:remote_ip=<ip> options:key=<key>
    cmd = [
        "sudo",
        "ovs-vsctl",
        "--may-exist",
        "add-port",
        bridge,
        port,
        "--",
        "set",
        "interface",
        port,
        "type=vxlan",
        f"options:remote_ip={remote_ip}",
        f"options:key={key}",
    ]

    if target:
        try:
            _run_raw(_construct_cmd(cmd, target), check=True)
            return True
        except Exception as e:
            logger.error(f"Failed to add vxlan port {port} on {target}: {e}")
            return False

    # Local
    try:
        _run_raw(cmd, check=True)
        return True
    except Exception as e:
        logger.error(f"Failed to add vxlan port {port} locally: {e}")
        return False


def del_port(bridge: str, port: str, target: str = None) -> bool:
    base_cmd = ["sudo", "ovs-vsctl", "--if-exists", "del-port", bridge, port]

    if target:
        try:
            _run_raw(_construct_cmd(base_cmd, target), check=True)
            return True
        except Exception:
            return False

    try:
        _run_raw(base_cmd, check=True)
        return True
    except Exception:
        pass

    for hv in CONFIG.get("hypervisors", {}):
        try:
            _run_raw(_construct_cmd(base_cmd, hv), check=True)
            return True
        except Exception:
            pass
    return False


def port_to_br(port: str, target: str = None) -> str | None:
    """Gets the bridge a port is attached to. Uses search fallback if target fails."""
    base_cmd = ["sudo", "ovs-vsctl", "port-to-br", port]

    # helper
    def parse(res):
        val = res.stdout.strip()
        return val if val else None

    if target:
        try:
            res = _run_raw(_construct_cmd(base_cmd, target), check=False)
            if res.returncode == 0 and res.stdout.strip():
                return parse(res)
        except Exception:
            pass

    # Fallback / Search
    # 1. Local
    try:
        res = _run_raw(base_cmd, check=False)
        if res.returncode == 0 and res.stdout.strip():
            logger.debug(f"Found {port} on (local) bridge {res.stdout.strip()}")
            return parse(res)
    except Exception:
        pass

    # 2. Remotes
    for hv in CONFIG.get("hypervisors", {}):
        try:
            # Skip if we already checked this target in the 'if target:' block?
            # Well, safe to re-check or just proceed.
            cmd = _construct_cmd(base_cmd, hv)
            res = _run_raw(cmd, check=False)
            if res.returncode == 0 and res.stdout.strip():
                logger.debug(f"Found {port} on {hv} bridge {res.stdout.strip()}")
                return parse(res)
        except Exception:
            continue

    return None


def get_interface_ofport(interface: str, target: str = None) -> int | None:
    """Gets OFPort. Uses search fallback."""
    base_cmd = ["sudo", "ovs-vsctl", "get", "Interface", interface, "ofport"]

    def parse(res):
        val = res.stdout.strip()
        # Handle potential noise
        if "\n" in val:
            val = val.split("\n")[-1]
        if val.lstrip("-").isdigit():
            return int(val)
        return None

    if target:
        try:
            res = _run_raw(_construct_cmd(base_cmd, target), check=False)
            v = parse(res)
            if v is not None:
                return v
        except Exception:
            pass

    # Search
    try:
        res = _run_raw(base_cmd, check=False)
        v = parse(res)
        if v is not None:
            return v
    except Exception:
        pass

    for hv in CONFIG.get("hypervisors", {}):
        try:
            res = _run_raw(_construct_cmd(base_cmd, hv), check=False)
            v = parse(res)
            if v is not None:
                return v
        except Exception:
            pass

    return None


def del_flows_by_cookie(bridge: str, cookie: str, target: str = None) -> bool:
    """
    Deletes flows from an OVS bridge matching a specific cookie/mask (e.g. '0x5A70/-1').
    """
    base_cmd = ["sudo", "ovs-ofctl", "del-flows", bridge, f"cookie={cookie}"]
    
    if target:
        cmd = _construct_cmd(base_cmd, target)
        try:
            _run_raw(cmd, check=True)
            logger.info(f"Cleared cookie flows {cookie} from {bridge} on {target}")
            return True
        except Exception as e:
            logger.debug(f"del-flows by cookie failed on {target} for {bridge}: {e}")
            return False

    # Fallback / Local
    try:
        if _run_raw(base_cmd, check=True).returncode == 0:
            logger.info(f"Cleared cookie flows {cookie} from {bridge} (local)")
            return True
    except Exception as e:
        logger.debug(f"Local del-flows by cookie failed: {e}")

    # Try Remotes
    for hv in CONFIG.get("hypervisors", {}):
        cmd_remote = _construct_cmd(base_cmd, hv)
        try:
            _run_raw(cmd_remote, check=True)
            logger.info(f"Cleared cookie flows {cookie} from {bridge} on {hv}")
            return True
        except Exception:
            continue

    logger.error(f"Failed to delete flows by cookie {cookie} from {bridge}")
    return False
