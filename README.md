# MECO - MEga COnstellation Emulator

[![License](https://img.shields.io/badge/License-Apache%202.0-blue.svg)](https://opensource.org/licenses/Apache-2.0)

A scalable emulator for LEO mega constellation networks. Designed to support research, testing, and development of networking protocols and systems.

---

## Key Features

- **Real-time gRPC API** (Port 50051)
- **Flexible YAML Configuration** (File, Inline, or Nano Editor)
- **Validation Engine** (Syntax & Semantic checks)
- **Persistent Server Management** (PID tracking)
- **Single-run Emulation guard** (Using activity flag)
- **Structured Logging** (Server & Client logs, colored and labeled in terminal)
- **System / App containers & VMs**
- **Easy Integration** (Python API, CLI, gRPC)
- **Parallel launch of nodes** (configurable thread-pool) for faster spin-up.

---

## Key Concepts

MECO supports three instance types, mapped automatically by node kind:

| Category             | How MECO deploys it                          | Example node-types                           |
| -------------------- | -------------------------------------------- | -------------------------------------------- |
| **App container**    | Lightweight container (single process)       | `SatelliteBig`, `SatelliteSmall`, `Terminal` |
| **System container** | Full OS container (systemd, routing daemons) | `Router`, `Gateway`                          |
| **VM**               | Hardware-virtualised Incus VM                | `NetworkOrchestrator`                        |

This mapping is hard-coded for user convenience.

---

## Table of Contents

1. [Installation](#installation)
2. [Wrapper Setup](#wrapper-setup)
3. [Quick Start](#quick-start)
4. [Configuration Guide](#configuration-guide)
5. [Command Reference](#command-reference)
6. [Troubleshooting](#troubleshooting)
7. [Contributing](#contributing)
8. [License](#license)

---

Here's the updated **Installation** section of your `README.md`, rewritten to fully reflect the new Incus setup guidance:

---

## Installation <a id="installation"></a>

### Prerequisites

| Requirement | Version                       |
| ----------- | ----------------------------- |
| Python      | 3.8+                          |
| Incus       | See below                     |
| OS          | Ubuntu 22.04+, ideally 24.04+ |

---

### Step 1: Install Incus

Check your Ubuntu version:

```bash
lsb_release -a
```

#### 🔹 For Ubuntu 24.04+ (native support):

```bash
sudo apt update
sudo apt install incus qemu-system
# Optional if migrating from LXD:
sudo apt install incus-tools
```

#### 🔹 For Ubuntu 20.04 / 22.04 (recommended via Zabbly):

Follow official instructions:
👉 [https://github.com/zabbly/incus](https://github.com/zabbly/incus)

These packages are actively maintained and include all Incus features.

---

### Step 2: Post-install configuration

#### Add your user to the `incus-admin` group:

```bash
sudo groupadd incus-admin  # only if it doesn't already exist
sudo usermod -aG incus-admin $USER
```

Then re-login (or run `newgrp incus-admin`) to apply group membership.

> **Security tip**: Only trusted users should belong to `incus-admin`.
> This group allows **full control** over containers and VMs.

#### Initialize Incus with:

```bash
incus admin init
```

You’ll be prompted to configure:

* Whether to use clustering (select **no** for standalone)
* A **storage pool** (ZFS or Btrfs recommended)
* A **network bridge** (e.g., `incusbr0` with auto IP)
* Optionally, a **remote listener** for TLS management
* Whether to print a **YAML preseed** at the end

##### Example (loop-backed Btrfs with bridge):

```bash
sudo apt install btrfs-progs  # if using btrfs
incus admin init
```

> You can re-run this at any time to reconfigure or dump a `preseed.yaml`:

```bash
incus admin init --dump > preseed.yaml
incus admin init --preseed < preseed.yaml
```

---

### Step 3: Set up MECO

```bash
git clone https://github.com/netgroup/meco-devel.git
cd meco-devel

# Python environment
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# Compile gRPC
python -m grpc_tools.protoc -I. \
    --python_out=. --grpc_python_out=. meco.proto
```

---


### (Optional) Enable CLI Auto-completion

```bash
eval "$(register-python-argcomplete meco)"
eval "$(register-python-argcomplete client)"
```

---

## Wrapper Setup <a id="wrapper-setup"></a>

This step allows you to run `meco` and `client` from anywhere:

```bash
sudo ln -s $PWD/meco /usr/local/bin/meco
sudo ln -s $PWD/client /usr/local/bin/client
sudo chmod +x /usr/local/bin/meco /usr/local/bin/client
```

### Verify Installation

```bash
which meco client  # Should show /usr/local/bin paths
```

---

## Quick Start <a id="quick-start"></a>

### Basic Workflow

```bash
meco on                                  # 1. start daemon
client start --filepath topology.yaml    # 2. deploy
meco status                               # 3. check status
client shutdown                           # 4. tear-down
meco off                                  # 5. stop daemon
```

**Note:** Only one emulation can run at a time. If you try to start a second while one is active, you'll see:
```
Emulation based on "topology.yaml" is running. please shut it down before starting a new one.
```

### Live Log Monitoring

```bash
tail -f /tmp/meco_server.log  # Server logs (colored output)
tail -f /tmp/meco_client.log  # Client logs (colored output)
```

### YAML Example

A minimal configuration with a `NetworkOrchestrator` node:

```yaml
nodes:
  - name: NetworkOrchestrator
    type: orchestrator
    image: ubuntu:22.04
    resources:
      cpu: 2
      memory: 2GB
  - name: Satellite1
    type: satellite
    image: ubuntu:22.04
  - name: Terminal
    type: terminal
    image: ubuntu:22.04
links:
  - endpoints: [NetworkOrchestrator, Satellite1]
  - endpoints: [Satellite1, Terminal]
```

---

## Configuration Guide <a id="configuration-guide"></a>

MECO uses YAML files to define network topologies and simulation parameters. You can:

- **Upload a local YAML file:**
  ```bash
  client start --filepath config.yaml
  ```
- **Use a server-side YAML file:**
  ```bash
  client start --filename saved_config.yaml
  ```
- **Directly input YAML content (opens Nano editor):**
  ```bash
  client start --content
  ```
- **Validate only (no execution):**
  ```bash
  client start --filepath config.yaml --dryrun
  ```
- **Save configuration on server:**
  ```bash
  client start --filepath config.yaml --saveas my_config
  ```

> **Tip:** Example YAML profiles are available in the `profiles/` directory.

---

## Command Reference <a id="command-reference"></a>

### Server Management

| Command         | Description                                 | Example            |
| --------------- | ------------------------------------------- | ------------------ |
| `on`            | Start server                                | `meco on`          |
| `off`           | Stop server                                | `meco off`         |
| `off --force`   | Stop server **and** auto-teardown emulation | `meco off --force` |
| `status`        | Show server status                          | `meco status`      |

> **Note:** Without `--force`, `meco off` will refuse to stop if an emulation is active.

### Client Operations

| Command/Flag    | Description                                      | Example                                          |
| -------------- | ------------------------------------------------ | ------------------------------------------------ |
| `--filepath`   | Upload local YAML                                | `client start --filepath config.yaml`            |
| `--filename`   | Use server-side file                             | `client start --filename saved_config.yaml`      |
| `--content`    | Direct YAML input                                | `client start --content`                         |
| `--saveas`     | Save configuration                               | `client start --filepath cfg.yaml --saveas prod` |
| `--dryrun`     | Validate only (full JSON-Schema validation)      | `client start --filepath cfg.yaml --dryrun`      |
| `shutdown`     | Gracefully stop active emulation & delete instances | `client shutdown`                             |

---

## Troubleshooting <a id="troubleshooting"></a>

### Common Issues

**Server won't start:**

```bash
# Force remove existing PID
rm -f /tmp/meco_server.pid
meco on
```

**Missing dependencies:**

```bash
deactivate && rm -rf venv
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

**gRPC connection issues:**

```bash
lsof -i :50051  # Check port availability
```

**Permission denied for wrapper scripts:**

```bash
sudo chmod +x /usr/local/bin/meco /usr/local/bin/client
```

**Manual cleanup of all MECO-tagged instances:**

```bash
incus list --format=json | jq -r '.[] | select(.config["user.meco"]=="true") | .name' | xargs -I{} incus delete {} --force
```

**Incus permission error**
```bash
sudo usermod -aG incus-admin $USER && re-login
```

**Validation fails**
```bash
run `client start --dryrun` and read schema path in error
```

**Left-over instances**
```bash
incus list --format=json | jq -r '.[] | select(.config["user.meco"]=="true").name' | xargs -r incus delete --force
```

### Schema validation & dry-run

- The `--dryrun` flag performs full JSON-Schema validation of your YAML.
- If validation fails, error details are printed and **no instances are launched**.

### Known Limitations

- Instances deploy sequentially for now, soon to be updated to parallel deployment.

---

## Contributing <a id="contributing"></a>

We welcome contributions! Please:

1. Fork the repository
2. Create a feature branch
3. Submit a pull request (PR) with tests

See [Development Workflow](Development.md) for detailed guidelines.

---

## License <a id="license"></a>

Licensed under [Apache 2.0](https://github.com/netgroup/meco-devel/blob/main/LICENSE).

---

> **Maintainers**: Stefano Salsano, Max Miraftab  
> **Support**: Open an issue on [GitHub](https://github.com/netgroup/meco-devel/issues)

---

## Further Resources

- [Example Topology](Topologies/)
- [Issue Tracker](https://github.com/netgroup/meco-devel/issues)
