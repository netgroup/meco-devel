import subprocess
import logging
import shlex
from abc import ABC, abstractmethod
from typing import List, Optional, Union

logger = logging.getLogger("meco.executors")


class CommandExecutor(ABC):
    """Abstract base class for executing shell commands."""

    @abstractmethod
    def run(
        self,
        cmd: List[str],
        check: bool = True,
        capture_output: bool = True,
        text: bool = True,
        background: bool = False,
        timeout: Optional[int] = None,
    ) -> subprocess.CompletedProcess:
        """
        Execute a command.

        Args:
            cmd: List of command components.
            check: Raise CalledProcessError if return code is non-zero.
            capture_output: Capture stdout and stderr.
            text: Return output as text (str) instead of bytes.
            background: If True, attempt to run in background/detached mode (useful for SSH).
            timeout: Timeout in seconds. Raises TimeoutExpired if exceeded.
        """
        pass

    @abstractmethod
    def upload_file(self, local_path: str, remote_path: str) -> bool:
        """
        Upload a file from local filesystem to the target execution environment.

        Args:
            local_path: Path to the source file on the local machine.
            remote_path: Destination path on the target (local or remote).
        """
        pass


class LocalExecutor(CommandExecutor):
    """Executes commands on the local machine."""

    def run(
        self,
        cmd: List[str],
        check: bool = True,
        capture_output: bool = True,
        text: bool = True,
        background: bool = False,
        timeout: Optional[int] = None,
    ) -> subprocess.CompletedProcess:
        return subprocess.run(
            cmd,
            check=check,
            capture_output=capture_output,
            text=text,
            timeout=timeout,
        )

    def upload_file(self, local_path: str, remote_path: str) -> bool:
        import shutil

        try:
            shutil.copy2(local_path, remote_path)
            return True
        except Exception as e:
            logger.error(f"Local file copy failed: {e}")
            return False


class SshExecutor(CommandExecutor):
    """Executes commands on a remote machine via SSH."""

    def __init__(
        self,
        host: str,
        user: Optional[str] = None,
        port: int = 22,
        identity_file: Optional[str] = None,
    ):
        self.host = host
        self.user = user
        self.port = port
        self.identity_file = identity_file

    def run(
        self,
        cmd: List[str],
        check: bool = True,
        capture_output: bool = True,
        text: bool = True,
        background: bool = False,
        timeout: Optional[int] = None,
    ) -> subprocess.CompletedProcess:
        ssh_base = ["ssh", "-p", str(self.port)]

        # Enable SSH Multiplexing for performance
        # ControlMaster=auto: Try to use existing master, otherwise create one
        # ControlPersist=600: Keep master open for 10 minutes after last client
        ssh_base.extend(
            [
                "-o",
                "ControlMaster=auto",
                "-o",
                "ControlPath=/tmp/meco-%r@%h:%p",
                "-o",
                "ControlPersist=600",
            ]
        )

        # Always use non-interactive mode preventing hangs on stdin
        ssh_base.append("-n")

        if background:
            # -f requests ssh to go to background just before command execution
            ssh_base.append("-f")

        if self.identity_file:
            ssh_base.extend(["-i", self.identity_file])

        # Disable StrictHostKeyChecking for automation ease (optional, but robust for ephemeral environments)
        ssh_base.extend(
            ["-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null"]
        )

        target = f"{self.user}@{self.host}" if self.user else self.host
        ssh_base.append(target)

        # When running remote commands via SSH, we append the command parts.
        # SSH joins these with spaces on the remote end.
        ssh_base.extend(cmd)

        return subprocess.run(
            ssh_base,
            check=check,
            capture_output=capture_output,
            text=text,
            timeout=timeout,
        )

    def upload_file(self, local_path: str, remote_path: str) -> bool:
        target = f"{self.user}@{self.host}" if self.user else self.host
        # -B: Batch mode (prevents asking for passwords/passphrases)
        # -P: Port
        scp_cmd = ["scp", "-B", "-P", str(self.port)]

        if self.identity_file:
            scp_cmd.extend(["-i", self.identity_file])

        # Add host key checking bypass to match run() behavior
        scp_cmd.extend(
            ["-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null"]
        )

        scp_cmd.extend([local_path, f"{target}:{remote_path}"])

        try:
            # Capture output to log stderr on failure
            subprocess.run(scp_cmd, check=True, capture_output=True)
            return True
        except subprocess.CalledProcessError as e:
            err_msg = e.stderr.decode().strip() if e.stderr else str(e)
            logger.error(f"SCP failed: {err_msg}")
            return False
