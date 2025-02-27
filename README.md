# MECO - MEga COnstellation Emulator

[![License](https://img.shields.io/badge/License-Apache%202.0-blue.svg)](https://opensource.org/licenses/Apache-2.0)

A scalable emulator for LEO mega constellation networks. Designed to support research, testing, and development of networking protocols and systems.

---

## Key Features

- **Real-time gRPC API** (Port 50051)
- **YAML Configuration** (Files/Inline/Nano Editor)
- **Validation Engine** (Syntax + Semantic checks)
- **Persistent Server Management** (PID tracking)
- **Structured Logging** (Server & Client logs)
- **CLI Auto-completion**

---

## Table of Contents

1. [Installation](#installation)
2. [Wrapper Setup](#wrapper-setup)
3. [Quick Start](#quick-start)
4. [Command Reference](#command-reference)
5. [Troubleshooting](#troubleshooting)
6. [Contributing](#contributing)
7. [License](#license)

---

## Installation <a id="installation"></a>

### Requirements

- Python 3.8+
- Virtual Environment (Recommended)

```bash
git clone https://github.com/netgroup/meco-devel.git
cd meco-devel
python3 -m venv venv
source venv/bin/activate  # Linux/macOS
pip install -r requirements.txt
python -m grpc_tools.protoc -I. --python_out=. --grpc_python_out=. meco.proto
```

---

## Wrapper Setup <a id="wrapper-setup"></a>

### Global Access Installation

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
# Start server
meco on

# Send local configuration
client start --filepath satellite_network.yaml

# Stop server
meco off
```

### Live Log Monitoring

```bash
tail -f /tmp/meco_server.log  # Server logs
tail -f /tmp/meco_client.log  # Client logs
```

---

## Command Reference <a id="command-reference"></a>

### Server Management

| Command  | Description        | Example       |
| -------- | ------------------ | ------------- |
| `on`     | Start server       | `meco on`     |
| `off`    | Stop server        | `meco off`    |
| `status` | Show server status | `meco status` |

### Client Operations

| Flag         | Description          | Example                                          |
| ------------ | -------------------- | ------------------------------------------------ |
| `--filepath` | Upload local YAML    | `client start --filepath config.yaml`            |
| `--filename` | Use server-side file | `client start --filename saved_config.yaml`      |
| `--content`  | Direct YAML input    | `client start --content "nodes: [...]"`          |
| `--saveas`   | Save configuration   | `client start --filepath cfg.yaml --saveas prod` |
| `--dryrun`   | Validate only        | `client start --filepath cfg.yaml --dryrun`      |

### Interactive Configuration

```bash
client start --content  # Opens Nano editor
```

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

---

## Contributing <a id="contributing"></a>

We welcome contributions! Please:

1. Fork the repository
2. Create a feature branch
3. Submit PR with tests

See [Development Workflow](Development.md) for detailed guidelines.

---

## License <a id="license"></a>

Licensed under [Apache 2.0](https://github.com/netgroup/meco-devel/blob/main/LICENSE).

---

> **Maintainers**: Stefano Salsano, Max Miraftab  
> **Support**: Open an issue on [GitHub](https://github.com/netgroup/meco-devel/issues)
