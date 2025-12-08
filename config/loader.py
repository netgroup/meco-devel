import os
import yaml
import logging

logger = logging.getLogger("meco.config")

# Constants extracted from meco.py
CONFIG_PATH = "config/config.yaml"
SCHEMA_PATH = "config/schema.yaml"

DEFAULT_CONFIG = {
    "defaults": {
        "integration_bridge": "br-int",
        "tunnel_bridge": "br-tun",
        "profile_base_container": "meco-cnt",
        "profile_base_vm": "meco-vm",
        "storage_pool": "default",
        "dns_mode": "dynamic",
        "bridge_driver": "openvswitch",
        "image_container": "images:ubuntu/22.04",
        "image_vm": "images:ubuntu/noble",
    }
}

def load_config(config_path=CONFIG_PATH, default_config=DEFAULT_CONFIG):
    """
    Loads configuration from a YAML file, falling back to defaults.
    """
    config = default_config.copy()
    if os.path.exists(config_path):
        try:
            with open(config_path, "r") as f:
                file_config = yaml.safe_load(f) or {}
            config.update(file_config)
            logger.info(f"Configuration loaded from {config_path}")
        except Exception as e:
            logger.warning(
                f"Failed to load config from {config_path}: {e}. Using defaults."
            )
    else:
        logger.info(
            f"Config file {config_path} not found. Using default configuration."
        )
    return config

def load_schema(schema_path=SCHEMA_PATH):
    """Loads the JSON schema for validation."""
    if not os.path.exists(schema_path):
        logger.error(f"Schema file not found: {schema_path}")
        return None
    try:
        with open(schema_path, "r") as f:
            return yaml.safe_load(f)
    except Exception as e:
        logger.error(f"Failed to load schema from {schema_path}: {e}")
        return None

# Singleton-like access to config
# In a real app, might want to load this explicitly at startup
CONFIG = load_config()
