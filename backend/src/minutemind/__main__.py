"""Entry point: `python -m minutemind`.

Launched by Electron's main process. It:
  * reads the session token from the MINUTEMIND_TOKEN environment variable
    (kept out of argv so it doesn't show up in process listings),
  * binds 127.0.0.1 on a free port chosen by the OS and prints
    `MINUTEMIND_PORT=<port>` on stdout so Electron knows where to connect,
  * exits on its own if the parent process (Electron) disappears.
"""

from __future__ import annotations

import argparse
import os
import socket
import sys
import threading
import time

import psutil
import uvicorn

from minutemind.api import create_app

PORT_PREFIX = "MINUTEMIND_PORT="


def _watch_parent(parent_pid: int, interval: float = 2.0) -> None:
    """Hard-exit if the parent process dies, so no orphaned backend is left behind."""

    def loop() -> None:
        while True:
            if not psutil.pid_exists(parent_pid):
                os._exit(0)
            time.sleep(interval)

    threading.Thread(target=loop, name="parent-watchdog", daemon=True).start()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="minutemind")
    parser.add_argument("--parent-pid", type=int, default=None)
    parser.add_argument("--port", type=int, default=0, help="0 = let the OS pick")
    args = parser.parse_args(argv)

    token = os.environ.get("MINUTEMIND_TOKEN", "")
    if not token:
        print("MINUTEMIND_TOKEN is not set", file=sys.stderr)
        return 2

    if args.parent_pid:
        _watch_parent(args.parent_pid)

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", args.port))
    port = sock.getsockname()[1]
    print(f"{PORT_PREFIX}{port}", flush=True)

    config = uvicorn.Config(
        create_app(token),
        log_level="warning",
        access_log=False,
    )
    uvicorn.Server(config).run(sockets=[sock])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
