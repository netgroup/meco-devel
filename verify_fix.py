
import logging
import sys
# Hack path
sys.path.append(".")
from network.manager import NetworkManager
from config.loader import CONFIG

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("verify_fix")

def main():
    print("--- Verifying _resolve_remote_port ---")
    nm = NetworkManager()
    
    hypervisors = CONFIG.get("hypervisors", {})
    if not hypervisors:
        print("No hypervisors configured.")
        return

    # Pick first hv
    hv_name = list(hypervisors.keys())[0]
    print(f"Testing against hypervisor: {hv_name}")
    
    # We need a valid interface to test. 
    # Let's try to list ports first via nm._scan_ports()? 
    # But _scan_ports uses the new logic, so if it works, we get a map!
    
    print("Running nm._scan_ports() ...")
    port_map = nm._scan_ports()
    
    print(f"\nMapped {len(port_map)} ports.")
    for k, v in port_map.items():
        print(f"  {k} -> {v}")
        
    if not port_map:
        print("No ports mapped. This might be correct if no instances are running.")
        print("But if instances are running on remote, it indicates failure.")

if __name__ == "__main__":
    main()
