import logging
import sys
import copy


class LogColors:
    RESET = "\033[0m"
    RED = "\033[31m"
    GREEN = "\033[32m"
    YELLOW = "\033[33m"
    CYAN = "\033[36m"
    GRAY = "\033[90m"
    BRIGHT_YELLOW = "\033[93m"


class ServerColorFormatter(logging.Formatter):
    def format(self, record):
        # Create a copy to avoid side effects on other handlers
        record = copy.copy(record)

        level_color = {
            "DEBUG": LogColors.GRAY,
            "INFO": LogColors.CYAN,
            "WARNING": LogColors.YELLOW,
            "ERROR": LogColors.RED,
            "CRITICAL": LogColors.RED,
        }.get(record.levelname, LogColors.RESET)

        record.levelname = f"[Server-{record.levelname}]"
        record.msg = f"{level_color}{record.msg}{LogColors.RESET}"
        return super().format(record)


def setup_logging(name="meco", level=logging.INFO):
    """
    Sets up the logger with the unified colorful formatter for stderr
    and a file handler for persistent logging.
    """
    logger = logging.getLogger(name)
    logger.setLevel(level)
    logger.propagate = False  # Avoid duplicates if root logger is configured

    # Check if handlers already exist to avoid duplicates
    if not logger.handlers:
        # 1. Console Handler
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setFormatter(
            ServerColorFormatter(
                "%(asctime)s %(levelname)s %(name)s: %(message)s", datefmt="%H:%M:%S"
            )
        )
        logger.addHandler(console_handler)

        # 2. File Handler
        try:
            file_handler = logging.FileHandler("/tmp/meco_server.log")
            file_handler.setFormatter(
                ServerColorFormatter(
                    "%(asctime)s %(levelname)s %(name)s: %(message)s",
                    datefmt="%Y-%m-%d %H:%M:%S",
                )
            )
            logger.addHandler(file_handler)
        except Exception:
            # Fallback if we can't write to /tmp (unlikely but safe)
            pass

    return logger
