"""mpv JSON IPC client (PRD F1).

Talks to mpv over the Unix socket configured with::

    input-ipc-server=/tmp/mpv.sock

Protocol: newline-delimited JSON. We observe a set of properties and expose
the interesting mpv events as simple dicts::

    {"type": "connected"}
    {"type": "file", "filename": "..."}          # a new file started
    {"type": "property", "name": "pause", "data": false}
    {"type": "idle"}
    {"type": "shutdown"}
    {"type": "disconnected"}
"""

from __future__ import annotations

import json
import socket
import time
from typing import Iterator, Sequence

# observe_property ids -> mpv property names (F1)
OBSERVED_PROPERTIES: dict[int, str] = {
    1: "path",
    2: "filename",
    3: "pause",
    4: "duration",
    5: "time-pos",
    6: "idle-active",
    7: "playlist-count",
    8: "eof-reached",
}


class MpvClient:
    """One connection to a running mpv instance."""

    def __init__(self, socket_path: str, io_timeout: float = 5.0):
        self.socket_path = socket_path
        self.io_timeout = io_timeout
        self._sock: socket.socket | None = None
        self._file = None

    def connect(self) -> None:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.io_timeout)
        try:
            sock.connect(self.socket_path)
        except OSError:
            sock.close()
            raise
        self._sock = sock
        self._file = sock.makefile("rb")
        for obs_id, prop in OBSERVED_PROPERTIES.items():
            self._send({"command": ["observe_property", obs_id, prop], "request_id": obs_id})

    def close(self) -> None:
        if self._file is not None:
            try:
                self._file.close()
            except OSError:
                pass
            self._file = None
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None

    def _send(self, obj: dict) -> None:
        if self._sock is None:
            raise ConnectionError("not connected")
        self._sock.sendall((json.dumps(obj) + "\n").encode("utf-8"))

    def get_property(self, name: str, request_id: int) -> None:
        """Query a property; the response arrives as a request_id-tagged line."""
        self._send({"command": ["get_property", name], "request_id": request_id})

    def read_event(self) -> dict | None:
        """Read one JSON line. Returns None on timeout or disconnect."""
        if self._file is None:
            return None
        try:
            line = self._file.readline()
        except socket.timeout:
            return None
        except OSError:
            return None
        if not line:
            return None
        try:
            return json.loads(line)
        except json.JSONDecodeError:
            return None


def watch_socket(
    socket_paths: str | Sequence[str],
    reconnect: bool = True,
    retry_delay: float = 2.0,
) -> Iterator[dict]:
    """Yield normalized events from mpv, cycling through candidate socket paths.

    mpv exposes its IPC socket at one path at a time (SVP4 uses
    /tmp/mpvsocket, plain mpv uses /tmp/mpv.sock), so a list of candidates
    is tried in order until one accepts a connection. With reconnect=False
    the generator stops after trying every path once.
    """
    if isinstance(socket_paths, str):
        socket_paths = [socket_paths]
    paths = list(socket_paths)
    if not paths:
        return

    idx = 0
    failed = 0
    while True:
        path = paths[idx % len(paths)]
        idx += 1
        client = MpvClient(path)
        try:
            client.connect()
        except OSError:
            failed += 1
            if not reconnect and failed >= len(paths):
                break
            time.sleep(retry_delay)
            continue
        failed = 0

        yield {"type": "connected", "socket": path}
        # If mpv is already playing a file when we connect, no start-file
        # event will fire for it — query the current file explicitly.
        initial_pending = 2
        initial_file: str | None = None
        client.get_property("path", 900)
        client.get_property("filename", 901)

        try:
            while True:
                raw = client.read_event()
                if raw is None:
                    break
                if "event" not in raw:  # command response
                    rid = raw.get("request_id")
                    if rid in (900, 901) and initial_pending > 0:
                        if raw.get("data"):
                            initial_file = raw["data"] if initial_file is None else initial_file
                        initial_pending -= 1
                        if initial_pending == 0:
                            if initial_file:
                                yield {"type": "file", "filename": initial_file, "initial": True}
                            initial_pending = -1
                    continue
                event = raw.get("event")
                if event == "property-change":
                    name = raw.get("name")
                    data = raw.get("data")
                    if name == "path" and data:
                        # Fallback if start-file was missed.
                        yield {"type": "file", "filename": data}
                    else:
                        yield {"type": "property", "name": name, "data": data}
                elif event == "start-file":
                    fname = raw.get("filename")
                    if fname:
                        yield {"type": "file", "filename": fname}
                elif event == "idle":
                    yield {"type": "idle"}
                elif event == "shutdown":
                    yield {"type": "shutdown"}
                    break
        finally:
            client.close()
        yield {"type": "disconnected", "socket": path}
        if not reconnect:
            break
        time.sleep(retry_delay)
