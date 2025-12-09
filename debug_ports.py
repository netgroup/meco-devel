
import logging
import sys
import subprocess
import json

# Setup logging
logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger("debug_ports")

def run(cmd):
    logger.debug(f"Running: {' '.join(cmd)}")
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        logger.error(f"Failed: {result.stderr}")
    else:
        logger.debug(f"Success: {result.stdout.strip()}")
    return result

def main():
    # 1. Get remotes from config if possible, or just list incus remotes
    # We will assume 'incus remote list' works or we just try 'incus list' on default
    
    print("--- Listing Incus Remotes ---")
    run(["incus", "remote", "list"])
    
    # Check if there are running instances
    print("\n--- Listing Instances ---")
    # Try local and a typical remote if known, or just parse from config manually if needed.
    # Logic: list all instances from 'incus list --format json'
    
    res = run(["incus", "list", "--all-projects", "--format=json"])
    if res.returncode != 0:
        print("Failed to list instances")
        return

    instances = json.loads(res.stdout)
    if not instances:
        print("No instances found.")
        return

    target_inst = None
    target_remote = None # None implies local or default
    
    # Try to find a running one
    for i in instances:
        if i.get('status') == 'Running':
            target_inst = i
            # Check location
            loc = i.get('location') # 'none' usually means local/default in some versions, or the cluster member name
            if loc and loc != 'none':
                target_remote = loc
            # But 'incus list' usually shows location as the node name if clustered.
            # If standard remote, it might be hidden. 
            # We'll assume local for start, or user can edit script.
            break
            
    if not target_inst:
        print("No running instances found locally.")
        # Try to guess a remote from user context?
        return

    print(f"\nTarget Instance: {target_inst['name']}")
    
    # Get state
    cmd = ["incus", "query", f"/1.0/instances/{target_inst['name']}/state"]
    # If remote, we might need 'remote:...'? 
    # 'location' field in 'incus list' gives the cluster member. 
    # If using standalone remotes (e.g. 'lab1:instance'), 'incus list' output differs.
    
    res = run(cmd)
    if res.returncode != 0:
        print("Failed to query state")
        return
        
    state = json.loads(res.stdout)
    net = state.get('network', {})
    
    for iface, data in net.items():
        if data.get('host_name'):
            host_dev = data['host_name']
            print(f"\nInterface {iface} -> Host Dev: {host_dev}")
            
            # Try port-to-br locally
            print("Checking local OVS...")
            run(["sudo", "ovs-vsctl", "port-to-br", host_dev])
            
            # If checking remote, we would do:
            # run(["incus", "exec", REMOTE, "--", "sudo", "ovs-vsctl", "port-to-br", host_dev])
            
if __name__ == "__main__":
    main()
