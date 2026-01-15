#!/usr/bin/env python
import argparse
import logging
import sys
import os
import signal
import time
import psutil
import argcomplete
import subprocess

# Ensure package is in path content
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from meco.service.server import serve
from meco.emulation.lifecycle import LifecycleManager
from meco.network.manager import NetworkManager
from meco.infra.incus import IncusClient
from meco.config.loader import CONFIG

# Logging Setup
from meco.utils.logger import setup_logging

logger = setup_logging("meco.main")

# Constants
PID_FILE = "/tmp/meco_server.pid"
PID_LIST_FILE = "/tmp/meco_pids.txt"
ACTIVITY_FLAG = "/tmp/meco_activity"
UPLOADS_DIR = "/tmp/meco_uploads"


def is_running(pid):
    """Checks if a process with the given PID is running."""
    try:
        if psutil.pid_exists(pid):
            # Optional: Check if the process name matches roughly what we expect
            # p = psutil.Process(pid)
            # if "python" in p.name(): return True
            return True
    except Exception:
        pass
    return False


def signal_handler(sig, frame):
    """Handles termination signals."""
    logger.info("Signal received. Cleaning up...")
    try:
        if os.path.exists(PID_FILE):
            os.remove(PID_FILE)
    except Exception as e:
        logger.error(f"Error removing PID file: {e}")
    sys.exit(0)


def server_on():
    """Starts the server in daemon mode."""
    # 1. Check if already running
    if os.path.exists(PID_FILE):
        try:
            with open(PID_FILE, "r") as f:
                old_pid = int(f.read().strip())
            if is_running(old_pid):
                logger.warning(f"Meco server is already ON (PID: {old_pid}).")
                sys.exit(0)
            else:
                logger.info("Stale PID file found. removing.")
                os.remove(PID_FILE)
        except Exception:
            if os.path.exists(PID_FILE):
                os.remove(PID_FILE)

    # 1.5 Check Hypervisor Connectivity
    hypervisors = CONFIG.get("hypervisors", {})
    if hypervisors:
        logger.info("Checking connectivity to configured hypervisors...")
        unreachable = []
        client = IncusClient()
        for remote in hypervisors:
            if not client.check_remote_connection(remote):
                unreachable.append(remote)

        if unreachable:
            logger.error(
                f"The following hypervisors are unreachable or stopped: {', '.join(unreachable)}"
            )
            logger.error(
                "Please ensure all hypervisors are running and reachable before starting the server."
            )
            sys.exit(1)
        logger.info("Mentioned hypervisors are reachable.")

    # 2. Setup Infrastructure (Bridges, Profiles)
    logger.info("Setting up infrastructure...")
    try:
        nm = NetworkManager()
        nm.setup_infrastructure()
    except Exception as e:
        logger.error(f"Infrastructure setup failed: {e}")
        sys.exit(1)

    # 3. Daemonize
    logger.info("Forking to background...")
    pid = os.fork()
    if pid > 0:
        sys.exit(0)  # Exit parent

    os.setsid()  # New session
    pid2 = os.fork()
    if pid2 > 0:
        sys.exit(0)  # Exit 2nd parent

    # 4. Write PID
    actual_pid = os.getpid()
    with open(PID_FILE, "w") as f:
        f.write(str(actual_pid) + "\n")

    with open(PID_LIST_FILE, "a") as f:
        f.write(str(actual_pid) + "\n")

    logger.info(f"Meco server started in background (PID: {actual_pid}).")

    # 5. Start Server
    # Register signal handlers for clean exit of the daemon
    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)

    try:
        serve()
    except Exception as e:
        logger.error(f"Server crashed: {e}")
    finally:
        if os.path.exists(PID_FILE):
            os.remove(PID_FILE)


def server_off(force=False):
    """Stops the server and cleans up."""
    logger.info("Stopping Meco server...")

    if os.path.exists(ACTIVITY_FLAG) and not force:
        logger.warning(
            "Active emulation detected. Use 'meco off --force' to force stop."
        )
        return

    if os.path.exists(ACTIVITY_FLAG) or force:
        if force:
            logger.info("Force stop requested. Cleaning up emulation...")
        try:
            lm = LifecycleManager()
            for msg in lm.stop_emulation(force=True):
                if isinstance(msg, str):
                    logger.info(msg)
        except Exception as e:
            logger.error(f"Cleanup failed: {e}")

    # Reset Infra if forced? meco.py did _teardown_bridges logic on exit?
    # meco.py's server_off calls delete_meco_instances(force=True) then kills PIDs.
    # It also called _teardown_bridges at the very end of main() in meco.py effectively if running as script?
    # Actually serve_forever blocks. The signal handler exits.

    # Teardown infrastructure
    try:
        nm = NetworkManager()
        nm.teardown_infrastructure()
    except Exception:
        pass

    killed = False
    if os.path.exists(PID_LIST_FILE):
        try:
            with open(PID_LIST_FILE, "r") as f:
                pids = [int(line.strip()) for line in f if line.strip()]

            for pid in pids:
                if is_running(pid):
                    logger.info(f"Killing PID {pid}")
                    try:
                        os.kill(pid, signal.SIGTERM)
                        killed = True
                    except Exception as e:
                        logger.error(f"Failed to kill {pid}: {e}")

            # Allow time to shut down
            if killed:
                time.sleep(1)

            # Cleanup file
            if os.path.exists(PID_LIST_FILE):
                os.remove(PID_LIST_FILE)
            if os.path.exists(PID_FILE):
                os.remove(PID_FILE)

            logger.info("Server stopped.")

        except Exception as e:
            logger.error(f"Error stopping server: {e}")
    else:
        logger.info("No active server PIDs found.")


def server_status():
    """Checks server status."""
    running = False
    if os.path.exists(PID_FILE):
        try:
            with open(PID_FILE, "r") as f:
                pid = int(f.read().strip())
            if is_running(pid):
                logger.info(f"Meco server is RUNNING (PID: {pid})")
                running = True
            else:
                logger.info("Meco server is NOT running (stale PID file).")
        except (OSError, ValueError):
            logger.info("Meco server is NOT running.")
    else:
        logger.info("Meco server is NOT running.")

    if running and os.path.exists(ACTIVITY_FLAG):
        try:
            with open(ACTIVITY_FLAG, "r") as f:
                content = f.read().strip()
            logger.info(f"Active Emulation: {content}")
        except (OSError, ValueError):
            pass


def server_logs(follow=True, lines=50):
    """
    Tails the server log file.
    """
    log_file = "/tmp/meco_server.log"
    if not os.path.exists(log_file):
        print(f"Log file {log_file} does not exist yet. Is the server running?")
        return

    cmd = ["tail", "-n", str(lines)]
    if follow:
        cmd.append("-f")
    cmd.append(log_file)

    try:
        # Use simple subprocess call to takeover
        subprocess.run(cmd)
    except KeyboardInterrupt:
        pass
    except Exception as e:
        print(f"Error reading logs: {e}")


def main():
    parser = argparse.ArgumentParser(description="MECO Emulator (Modular Refactor)")
    subparsers = parser.add_subparsers(dest="command", required=True)

    # Commands
    subparsers.add_parser("on", help="Start the gRPC Server (Daemon)")

    off_parser = subparsers.add_parser("off", help="Stop the Server")
    off_parser.add_argument("--force", "-f", action="store_true", help="Force cleanup")

    subparsers.add_parser("status", help="Show Server Status")

    log_parser = subparsers.add_parser("logs", help="View Server Logs")
    log_parser.add_argument(
        "-n", "--lines", type=int, default=50, help="Number of lines to show"
    )
    log_parser.add_argument(
        "--no-follow",
        action="store_true",
        help="Do not follow output (just show last N lines)",
    )

    argcomplete.autocomplete(parser)
    args = parser.parse_args()

    if args.command == "on":
        server_on()
    elif args.command == "off":
        server_off(force=args.force)
    elif args.command == "status":
        server_status()
    elif args.command == "logs":
        server_logs(follow=not args.no_follow, lines=args.lines)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
