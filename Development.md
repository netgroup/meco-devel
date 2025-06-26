# Developers' Manual

A comprehensive guide for contributing to and extending the MECO emulator system.

---

## Table of Contents

- [API Architecture](#architecture)
- [Development Environment Setup](#dev-setup)
- [Development Workflow](#development-workflow)
- [Protobuf & gRPC](#protobuf-grpc)
- [Logging & Monitoring](#logging)
- [Testing Strategies](#testing)
- [Contribution Guidelines](#contribution)
- [Future Roadmap](#future)

---

## API Architecture <a id="architecture"></a>

### Core Components Diagram

```mermaid
graph TD
    A[Client] -->|gRPC| B[Server]
    B --> C{YAML Processor}
    C --> D[Validation Engine]
    C --> E[File Storage]
    D --> G[Instance Orchestrator]
    G --> H[Instance Deployer (Container | System Container | VM)]
```

### gRPC Service Definition (`meco.proto`)

```protobuf
syntax = "proto3";

service MecoService {
  // Diagnostic echo endpoint
  rpc MecoCall(MecoRequest) returns (MecoResponse);

  // Main configuration endpoint
  // NOTE: Deployment target (container, system container, or VM) is now inferred from each node's type in the YAML configuration. The user does not specify this explicitly; the server determines the correct deployment mode for each node.
  rpc Start(ResourceDescriptor) returns (StartResponse);
}

message ResourceDescriptor {
  oneof file_data {
    string server_file_path = 1;    // Path to existing server-side file
    string client_file_content = 2; // Direct YAML content payload
  }
  optional string save_as = 3;      // Server-side persistence name
  optional bool dry_run = 4;        // Validation-only mode flag
}

message StartResponse {
  bool success = 1;
  string message = 2;
}
```

### Key Architectural Features

#### Server-Side Components

- **YAML Validation Pipeline**:
  - Syntax validation using PyYAML
  - Semantic validation (minimum node requirements)
  - Dependency resolution check
- **Resource Management**:
  - PID-based process tracking
  - Graceful shutdown sequence
  - File versioning in `/tmp/meco_uploads`
- **gRPC Interface**:
  - Thread-pooled request handling
  - Structured error propagation
- **Instance Orchestrator** (formerly Execution Engine):
  - Determines deployment mode for each node based on its type (e.g., Satellite → app container, Router → system container, NetworkOrchestrator → VM)
  - Uses `incus launch` with `--vm` flag for VMs, or standard container launch for others
  - Maintains a registry of all active instances (containers and VMs)

#### Client-Side Components

- **Configuration Management**:
  - Interactive Nano editor integration
  - File diffing for version comparisons
  - Batch processing support
- **Connection Management**:
  - Automatic retry logic
  - Timeout handling
  - TLS support (future)
- **Instance Cleanup Logic**:
  - The client automatically deletes both containers and VMs associated with MECO deployments (not just containers)

---

## Development Environment Setup <a id="dev-setup"></a>

### 1. Clone the Repository

```bash
git clone https://github.com/netgroup/meco-devel.git
cd meco-devel
```

### 2. Python Virtual Environment

```bash
python3 -m venv venv
source venv/bin/activate
```

### 3. Install Dependencies

```bash
pip install -r requirements.txt
pip install -r requirements-dev.txt  # For development tools (black, ruff, pytest, etc.)
```

### 4. Compile Protobuf/gRPC Stubs

```bash
python -m grpc_tools.protoc -I. --python_out=. --grpc_python_out=. meco.proto
```

### 5. (Optional) Enable CLI Auto-completion

```bash
eval "$(register-python-argcomplete meco)"
eval "$(register-python-argcomplete client)"
```

### 6. Incus Setup (for local emulation)

- Ensure Incus is installed and initialized (see main README).
- Add your user to the `incus-admin` group and re-login.
- For VM support, ensure `qemu-system` is installed on your system.

---

## Development Workflow <a id="development-workflow"></a>

### Making Code Changes

1. **Create a Feature Branch**

   ```bash
   git checkout -b feature/my-feature
   ```

2. **Edit Code**

   - Update Python modules in `meco-devel/meco.py`, `client`, etc.
   - If you change the gRPC interface, update `meco.proto` and recompile stubs.

3. **Format and Lint**

   ```bash
   black .
   ruff .
   ```

4. **Run Tests**

   ```bash
   pytest tests/ -v
   ```

5. **Commit and Push**

   ```bash
   git add .
   git commit -m "Describe your change"
   git push origin feature/my-feature
   ```

6. **Open a Pull Request**

   - Follow the contribution guidelines for PRs.

---

## Protobuf & gRPC <a id="protobuf-grpc"></a>

- Edit `meco.proto` to change the API.
- Recompile stubs after changes:

  ```bash
  python -m grpc_tools.protoc -I. --python_out=. --grpc_python_out=. meco.proto
  ```

- Update both server (`meco.py`) and client as needed.

---

## Logging & Monitoring <a id="logging"></a>

### Log Structure

```python
{
  "timestamp": "YYYY-MM-DD HH:MM:SS",
  "component": "server|client",
  "level": "INFO|WARN|ERROR",
  "operation": "Start|Validate|Persist",
  "instance_type": "container|vm",  # Use this field for clarity in future logs
  "duration_ms": 45,
  "message": "Descriptive message",
  "metadata": {}
}
```

### Viewing Logs

```bash
tail -f /tmp/meco_server.log | jq
tail -f /tmp/meco_client.log | jq
```

---

## Testing Strategies <a id="testing"></a>

### 1. Unit Tests

- Located in `tests/unit/`
- Run with:

  ```bash
  pytest tests/unit -v
  ```

### 2. Integration Tests

- Located in `tests/`
- Run with:

  ```bash
  pytest tests/ -v
  ```
- Ensure both container and VM deployment paths are tested:
  - Use a YAML sample for container-only deployment (e.g., `samples/containers_only.yaml`)
  - Use a YAML sample with mixed container and VM roles (e.g., `samples/with_vm.yaml`)
  - Example VM test:
    ```bash
    client start --filepath samples/with_vm.yaml
    ```

### 3. Manual Testing

- Start the server:

  ```bash
  meco on
  ```

- Deploy a topology  
  ```bash
  client start --filepath profiles/example.yaml --dryrun
  ```

- Check status:

  ```bash
  meco status
  # Output now lists both containers and VMs under active instances
  ```

- Shutdown:

  ```bash
  client shutdown
  ```

### 4. Debugging

- Server introspection:

  ```bash
  meco status
  # Shows all active containers and VMs
  ```

- gRPC debugging:

  ```bash
  GRPC_VERBOSITY=DEBUG GRPC_TRACE=all meco on
  ```

---

## Contribution Guidelines <a id="contribution"></a>

- Fork the repository and create a feature branch from `main`.
- Keep commits atomic and descriptive.
- Rebase before merging.
- Run all tests and linters before submitting a PR.
- Squash merge for features.
- If modifying deployment logic, ensure VM/container compatibility is preserved.

---

## Future Roadmap <a id="future"></a>

- [ ] TLS support for gRPC
- [ ] Parallel deployment of instances
- [ ] Adding Benchmarks

---

**For any questions or support, open a GitHub issue or contact the maintainers.**
