import logging
import os
import time
import functools
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed

from config.loader import CONFIG
from infra.executors import LocalExecutor, SshExecutor
from infra.incus import IncusClient
from network.manager import NetworkManager
from network import ovs
from emulation import generator

logger = logging.getLogger("meco.lifecycle")

# Dependency initialization
# We keep these for local operations or as defaults
local_executor = LocalExecutor()
local_incus_client = IncusClient(local_executor)
net_manager = NetworkManager()

ACTIVITY_FLAG = "/tmp/meco_activity"

class LifecycleManager:
    """
    Orchestrates the deployment, start, and stop of emulations.
    """
    
    def __init__(self):
        self.clients = {"local": local_incus_client}
        self.node_locations = {} # name -> remote_alias (or None for local)
        self._init_clients()

    def _init_clients(self):
        hypervisors = CONFIG.get("hypervisors", {})
        
        # 1. Build Client Map
        for name, data in hypervisors.items():
            ip = data.get("ip")
            if not ip: continue
            
            # Assume user 'ubuntu' and standard port for now, or add to config later
            logger.info(f"Initializing SSH client for {name} ({ip})...")
            executor = SshExecutor(host=ip, user="ubuntu")
            self.clients[name] = IncusClient(executor)
            
        # 2. Build Instance Location Map
        # hypervisors: { 'hv1': { 'instances': ['1-Sat', ...], ... } }
        for hv_name, data in hypervisors.items():
            for inst in data.get("instances", []):
                self.node_locations[inst] = hv_name

    def start_emulation(self, topology_data, dry_run=False):
        if os.path.exists(ACTIVITY_FLAG) and not dry_run:
             raise RuntimeError("Emulation already running.")

        if dry_run:
            logger.info("Dry run complete. No resources created.")
            return True

        # 1. Setup Infra
        net_manager.setup_infrastructure()
        
        # 2. Deploy Nodes
        try:
            self._deploy_nodes(topology_data)
        except Exception as e:
            logger.error(f"Deployment failed: {e}")
            self.stop_emulation(force=True)
            raise

        # 3. Configure Network (Port Mapping & Flows)
        try:
            self._configure_network(topology_data)
        except Exception as e:
            logger.error(f"Network configuration failed: {e}")
            self.stop_emulation(force=True)
            raise
            
        # 4. Set Activity Flag
        with open(ACTIVITY_FLAG, "w") as f:
            f.write(str(time.time()))
            
        return True

    def stop_emulation(self, force=True):
        if not os.path.exists(ACTIVITY_FLAG) and not force:
             logger.warning("No active emulation to shut down.")
             return False

        logger.info("Shutting down emulation...")
        
        # 1. Delete Instances
        self._delete_all_instances(force)
        
        # 2. Teardown Network
        net_manager.teardown_infrastructure()
        
        # 3. Clear Flag
        if os.path.exists(ACTIVITY_FLAG):
            try:
                os.remove(ACTIVITY_FLAG)
            except FileNotFoundError:
                pass
            
        logger.info("Shutdown complete.")
        return True

    def _deploy_nodes(self, data):
        nodes = data.get("nodes", [])
        
        tasks = []
        node_map = {} # Map instance name -> remote alias (or None for local)
        
        for node in nodes:
            name = f"{node['id']}-{node['type']}"
            is_vm = node["type"].lower() == "groundstation"
            
            # Identify where this node goes
            remote = self.node_locations.get(name)
            node_map[name] = remote
            
            # Generate Cloud Init
            cloud_content = generator.generate_cloudinit(node)
            cloud_file = f"/tmp/{name}-cloud.yaml"
            with open(cloud_file, "w") as f:
                f.write(cloud_content)
                
            tasks.append(functools.partial(
                self._launch_node, 
                name=name, 
                is_vm=is_vm, 
                local_cloud_file=cloud_file
            ))
            
        # 1. Launch all nodes in parallel (Fire & Forget)
        # SshExecutor with background=True means this returns almost instantly
        with ThreadPoolExecutor(max_workers=16) as pool:
             futures = [pool.submit(t) for t in tasks]
             for fut in as_completed(futures):
                 fut.result()
                 
        # 2. Wait for all nodes to be ready (Bulk Check)
        self._wait_for_deployment(node_map)

    def _wait_for_deployment(self, node_map):
        """
        Wait for instances to start, grouped by hypervisor to minimize remote calls.
        """
        logger.info("Waiting for all instances to reach Running state...")
        
        # Group by client
        client_batches = defaultdict(list)
        for name, remote in node_map.items():
            client = self.clients.get(remote, local_incus_client) if remote else local_incus_client
            client_batches[client].append(name)
        
        def wait_batch(client, names):
            if not client.wait_for_instances(names, "Running", timeout=300):
                raise RuntimeError(f"Timeout waiting for instances on client {client}")
                
        with ThreadPoolExecutor(max_workers=len(client_batches)) as pool:
            futures = [
                pool.submit(wait_batch, client, names) 
                for client, names in client_batches.items()
            ]
            for fut in as_completed(futures):
                fut.result()
                
        logger.info("All instances are Running.")

    def _launch_node(self, name, is_vm, local_cloud_file):
        remote = self.node_locations.get(name)
        client = self.clients.get(remote, local_incus_client) if remote else local_incus_client
        
        # Determine paths
        final_cloud_file = local_cloud_file
        
        if remote:
            # Upload file if remote
            final_cloud_file = f"/tmp/{name}-cloud.yaml" # Target path on remote
            logger.info(f"Uploading {local_cloud_file} to {remote}:{final_cloud_file}...")
            if not client.executor.upload_file(local_cloud_file, final_cloud_file):
                raise RuntimeError(f"Failed to upload cloud-init for {name} to {remote}")
        
        profile_base = net_manager.profile_vm if is_vm else net_manager.profile_container
        
        image = CONFIG["defaults"]["image_vm"] if is_vm else CONFIG["defaults"]["image_container"]
        
        # Tag the instance for identifying ownership
        instance_config = {"user.meco": "true"}

        logger.info(f"Launching {name} on {remote or 'local'}...")
        # Note: launching with background=True (implied by SshExecutor changes)
        if not client.launch_instance(
            image=image, 
            name=name, 
            profiles=[profile_base], 
            is_vm=is_vm, 
            cloud_init_file=final_cloud_file,
            config=instance_config
        ):
             raise RuntimeError(f"Failed to launch {name} on {remote or 'local'}")
        # Note: NO waiting here.

    def _delete_all_instances(self, force):
        if not hasattr(self, 'clients'):
             self.clients = {"local": local_incus_client}
             self._init_clients()
             
        def delete_on_client(item):
            name, client = item
            try:
                instances = client.list_instances()
                to_delete = [
                    i["name"] for i in instances 
                    if i.get("config", {}).get("user.meco") == "true"
                ]
                for inst in to_delete:
                    client.delete_instance(inst, force)
            except Exception as e:
                logger.warning(f"Error cleaning up on {name}: {e}")

        with ThreadPoolExecutor(max_workers=8) as pool:
            pool.map(delete_on_client, self.clients.items())

    def _configure_network(self, data):
        logger.info("Configuring network flows...")
        
        # 1. Build Port Map (waits for IPs)
        port_map = net_manager.build_port_map(timeout=60)
        
        if not port_map:
             logger.warning("Port map empty. Skipping flow generation.")
             return

        # 2. Generate Flows
        hv_rules = net_manager.generate_flows(data, port_map)
        
        # 3. Apply Flows
        for hv, bridges in hv_rules.items():
            for br, rules in bridges.items():
                logger.info(f"Applying {len(rules)} rules to {hv or 'local'}:{br}")
                target = hv if hv else None
                net_manager.apply_flows(br, rules, target=target)
