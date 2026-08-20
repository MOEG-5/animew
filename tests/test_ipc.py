import json
import socket
import tempfile
import threading
import time
import unittest
from pathlib import Path

from animew.ipc import watch_socket

DEFAULT_EPISODE = "/media/Anime/[Demo] Lv999 no Murabito - 05 (1080p) [ABC].mkv"


class FakeMpv(threading.Thread):
    """Minimal fake mpv: accepts one connection, answers observe_property and
    get_property commands, and optionally emits playback events."""

    def __init__(
        self,
        sock_path: str,
        path: str | None = None,
        filename: str | None = None,
        emit_playback: bool = True,
    ):
        super().__init__(daemon=True)
        self.sock_path = sock_path
        self.path = path
        self.filename = filename
        self.emit_playback = emit_playback
        self.server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.server.bind(sock_path)
        self.server.listen(1)
        self.error = None

    @staticmethod
    def _send(conn: socket.socket, obj: dict) -> None:
        conn.sendall((json.dumps(obj) + "\n").encode())

    def run(self):
        try:
            conn, _ = self.server.accept()
            conn.settimeout(2)
            f = conn.makefile("rb")
            deadline = time.time() + 2
            while time.time() < deadline:
                try:
                    line = f.readline()
                except socket.timeout:
                    break
                if not line:
                    break
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    continue
                cmd = msg.get("command") or []
                rid = msg.get("request_id")
                if cmd and cmd[0] == "get_property":
                    name = cmd[1]
                    data = self.path if name == "path" else (self.filename if name == "filename" else None)
                    self._send(conn, {"request_id": rid, "error": "success", "data": data})
                elif cmd and cmd[0] == "observe_property":
                    self._send(conn, {"request_id": rid, "error": "success", "data": None})
            if self.emit_playback:
                self._send(conn, {"event": "start-file", "playlist_entry_id": 1, "filename": self.path or DEFAULT_EPISODE})
                self._send(conn, {"event": "property-change", "name": "pause", "data": False, "id": 3})
            time.sleep(0.2)
            conn.close()
        except Exception as exc:  # pragma: no cover
            self.error = exc
        finally:
            self.server.close()


def run_watch(sock: str) -> list[dict]:
    events = []
    for ev in watch_socket(sock, reconnect=False):
        events.append(ev)
        if ev["type"] == "disconnected":
            break
    return events


class TestIpc(unittest.TestCase):
    def test_watch_socket_emits_file_event(self):
        with tempfile.TemporaryDirectory() as tmp:
            sock = str(Path(tmp) / "mpv.sock")
            fake = FakeMpv(sock)
            fake.start()
            time.sleep(0.2)
            events = run_watch(sock)
            fake.join(timeout=5)
            self.assertIsNone(fake.error)
            self.assertEqual(events[0]["type"], "connected")
            files = [e for e in events if e["type"] == "file"]
            self.assertEqual(len(files), 1)
            self.assertTrue(files[0]["filename"].endswith("Lv999 no Murabito - 05 (1080p) [ABC].mkv"))
            props = [e for e in events if e["type"] == "property"]
            self.assertTrue(any(e["name"] == "pause" and e["data"] is False for e in props))

    def test_initial_file_when_already_playing(self):
        # mpv already playing when we connect: no start-file event is emitted,
        # but the initial get_property query must surface the current file.
        with tempfile.TemporaryDirectory() as tmp:
            sock = str(Path(tmp) / "mpv.sock")
            fake = FakeMpv(sock, path=DEFAULT_EPISODE, filename="[Demo] Lv999 no Murabito - 05 (1080p) [ABC].mkv", emit_playback=False)
            fake.start()
            time.sleep(0.2)
            events = run_watch(sock)
            fake.join(timeout=5)
            self.assertIsNone(fake.error)
            files = [e for e in events if e["type"] == "file"]
            self.assertEqual(len(files), 1, events)
            self.assertEqual(files[0]["filename"], DEFAULT_EPISODE)
            self.assertTrue(files[0].get("initial"))

    def test_no_initial_file_when_idle(self):
        # mpv idle (no file loaded): get_property returns null, no file event.
        with tempfile.TemporaryDirectory() as tmp:
            sock = str(Path(tmp) / "mpv.sock")
            fake = FakeMpv(sock, emit_playback=False)
            fake.start()
            time.sleep(0.2)
            events = run_watch(sock)
            fake.join(timeout=5)
            self.assertIsNone(fake.error)
            files = [e for e in events if e["type"] == "file"]
            self.assertEqual(files, [])

    def test_multiple_paths_falls_back(self):
        # First candidate socket does not exist; the second one is live.
        with tempfile.TemporaryDirectory() as tmp:
            missing = str(Path(tmp) / "missing.sock")
            present = str(Path(tmp) / "mpv.sock")
            fake = FakeMpv(present, path=DEFAULT_EPISODE, filename="[Demo] Lv999 no Murabito - 05 (1080p) [ABC].mkv", emit_playback=False)
            fake.start()
            time.sleep(0.2)
            events = list(watch_socket([missing, present], reconnect=False, retry_delay=0.05))
            fake.join(timeout=5)
            self.assertIsNone(fake.error)
            self.assertEqual(events[0]["type"], "connected")
            self.assertEqual(events[0]["socket"], present)
            files = [e for e in events if e["type"] == "file"]
            self.assertEqual(len(files), 1)
            self.assertEqual(files[0]["filename"], DEFAULT_EPISODE)

    def test_reconnect_waits_for_socket(self):
        with tempfile.TemporaryDirectory() as tmp:
            sock = str(Path(tmp) / "mpv.sock")
            # Socket does not exist yet: with reconnect=False we should give up.
            events = list(watch_socket(sock, reconnect=False, retry_delay=0.05))
            self.assertEqual(events, [])


if __name__ == "__main__":
    unittest.main()
