# meco
MEga COnstellation emulator

Meco is a gRPC-based application that simulates and manages a Low Earth Orbit (LEO) Mega Constellation. It provides a command-line interface (CLI) to:
- Start a background gRPC server (`on`)
- Stop the server (`off`)
- Send a resource descriptor file to the server (`start`)

## Features
- Manage a gRPC server via CLI
- Supports daemonized execution
- Implements structured logging using Python's logging module
- Supports auto-completion with argcomplete
- Uses gRPC for communication

---

## Installation

### Install Dependencies
```bash
pip install argcomplete grpcio grpcio-tools
```

### Enable CLI Auto-Completion
For Bash users:
```bash
eval "$(register-python-argcomplete meco)"
```
For global auto-completion (one-time setup):
```bash
activate-global-python-argcomplete
```

### Compile gRPC Protocol Buffers
Ensure you have the `meco.proto` file and run:
```bash
python -m grpc_tools.protoc -I. --python_out=. --grpc_python_out=. meco.proto
```

---

## Usage

### Start the gRPC Server (Daemon Mode)
```bash
python meco.py on
```
Runs the server in the background and stores its PID in `/tmp/meco_server.pid`.

### Stop the gRPC Server
```bash
python meco.py off
```
Stops the server by terminating the background process.

### Send a Resource Descriptor File
```bash
python meco.py start config.json
```
Sends `config.json` to the gRPC server for processing.

### Show Help Menu
```bash
python meco.py --help
```

---

## Example Commands

#### Start the Server
```bash
python meco.py on
```
Output:
```
2025-02-07 12:00:00 [INFO] Meco server turned ON in background (PID: 12345).
```

#### Stop the Server
```bash
python meco.py off
```
Output:
```
2025-02-07 12:05:00 [INFO] Turning OFF Meco server (PID: 12345)...
2025-02-07 12:05:01 [INFO] Meco server turned OFF.
```

#### Send a Resource File
```bash
python meco.py start my_config.json
```
If the file exists:
```
2025-02-07 12:10:00 [INFO] Successfully started with resource file: my_config.json
```
If the file is missing:
```
2025-02-07 12:10:05 [ERROR] Error: File 'my_config.json' does not exist.
```

#### Check Available Commands
```bash
python meco.py --help
```
Output:
```
usage: meco [-h] {on,off,start} ...

Emulates a LEO Mega Constellation

positional arguments:
  {on,off,start}  Available commands
    on            Turn the Meco gRPC server ON (daemon mode)
    off           Turn the Meco gRPC server OFF
    start         Send a resource descriptor file to the Meco server

optional arguments:
  -h, --help      show this help message and exit
```

---

## Logging

Meco uses Python's logging module for structured logs.

Log levels used:
- INFO → General information (`logger.info()`)
- WARNING → Server status (`logger.warning()`)
- ERROR → Issues (`logger.error()`)

### Example Logs
```
2025-02-07 12:00:00 [INFO] Meco server started on port 50051.
2025-02-07 12:05:30 [INFO] Start() called with resource descriptor file: config.json
2025-02-07 12:06:10 [ERROR] Error: File 'config.json' does not exist.
```

---

## Development Setup

### Clone the Repository
```bash
git clone https://github.com/your-repo/meco.git
cd meco
```

### Install Dependencies
```bash
pip install -r requirements.txt
```

### Compile gRPC Stubs
```bash
python -m grpc_tools.protoc -I. --python_out=. --grpc_python_out=. meco.proto
```

### Run the Server in Debug Mode
```bash
python meco.py on
```

### Run the Client for Testing
```bash
python meco.py start test.json
```

---

## Troubleshooting

### "server already running" message when running `python meco.py on`
Check if the PID file exists:
```bash
cat /tmp/meco_server.pid
```
Manually stop the process:
```bash
kill -9 $(cat /tmp/meco_server.pid)
rm /tmp/meco_server.pid
```

### "File does not exist" error when running `python meco.py start file.json`
Ensure the file exists:
```bash
ls -lh file.json
```

### Command completion is not working
Re-enable argcomplete:
```bash
eval "$(register-python-argcomplete meco)"
```

---

## License

This project is licensed under the **Apache License 2.0**. See the [LICENSE](LICENSE) file for details.

---

## Future Improvements
- Add Docker support for containerized deployment
- Implement TLS encryption for secure gRPC communication
- Add unit tests for CLI and gRPC services

---

## Contributors
- Your Name - Maintainer
- Contributor Name - Feature Development
- Reviewer Name - Code Review

