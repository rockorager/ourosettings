"""Disposable daemon fixtures and independent newline JSON-RPC client."""
import copy
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
VERSION = "2026-07-28"
META = {"io.modelcontextprotocol/protocolVersion": VERSION,
        "io.modelcontextprotocol/clientCapabilities": {},
        "io.modelcontextprotocol/clientInfo": {"name": "independent-test", "version": "1"}}

if len(sys.argv) > 1 and sys.argv[1] == "--activate":
    fd = int(sys.argv[2])
    os.dup2(fd, 3, inheritable=True)
    if fd != 3:
        os.close(fd)
    os.environ["LISTEN_PID"] = str(os.getpid())
    os.environ.setdefault("LISTEN_FDS", "1")
    os.execv(sys.argv[3], sys.argv[3:])

EXE = str(Path(sys.argv.pop(1)).resolve()) if len(sys.argv) > 1 else str(ROOT / "zig-out/bin/ourosettings")


def frame(method="resources/read", params=None, id=1):
    if params is None:
        params = {"uri": "ouro://settings"} if method == "resources/read" else {}
    return (json.dumps({"jsonrpc": "2.0", "id": id, "method": method,
                        "params": {"_meta": META, **params}}, separators=(",", ":")) + "\n").encode()


class Client:
    def __init__(self, path):
        self.sock = socket.socket(socket.AF_UNIX)
        self.sock.settimeout(2)
        try:
            self.sock.connect(str(path))
        except Exception:
            self.sock.close()
            raise
        self.buffer = b""

    def send(self, *args, **kwargs):
        self.sock.sendall(frame(*args, **kwargs))

    def receive(self):
        while b"\n" not in self.buffer:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise EOFError
            self.buffer += chunk
        message, self.buffer = self.buffer.split(b"\n", 1)
        self.last_record_size = len(message) + 1
        return json.loads(message)

    def close(self):
        self.sock.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


class DaemonFixture(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.build = tempfile.TemporaryDirectory(prefix="ourosettings-shim-")
        cls.shim = str(Path(cls.build.name) / "faults.so")
        subprocess.run(["cc", "-shared", "-fPIC", "-Wall", "-Wextra", "-Werror", "-o", cls.shim,
                        str(ROOT / "tests/faults.c"), "-ldl"], check=True)

    @classmethod
    def tearDownClass(cls):
        cls.build.cleanup()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ourosettings-")
        self.root = Path(self.temp.name)
        self.path = self.root / "runtime/settings.mcp.sock"
        self.state = self.root / "config/settings.json"
        self.fault = self.root / "fault"
        self.processes = []
        self.clients = []

    def tearDown(self):
        for client in self.clients:
            client.close()
        for process in self.processes:
            if process.poll() is None:
                process.terminate()
            process.communicate(timeout=5)
        self.temp.cleanup()

    def spawn(self, *, wait=True, idle=2000, listener=None, env=None, path=None, state=None):
        command = [EXE, "--socket", str(path or self.path), "--state", str(state or self.state), "--idle-ms", str(idle)]
        environ = {k: v for k, v in os.environ.items() if not k.startswith("LISTEN_")}
        environ.update({"LD_PRELOAD": self.shim, "OURO_TEST_FAULT_FILE": str(self.fault)})
        environ.update(env or {})
        fds = ()
        if listener is not None:
            fds = (listener.fileno(),)
            command = [sys.executable, __file__, "--activate", str(listener.fileno()), *command]
        process = subprocess.Popen(command, env=environ, pass_fds=fds, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.processes.append(process)
        if wait:
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    self.fail(process.communicate()[1].decode())
                try:
                    with Client(path or self.path) as client:
                        client.send()
                        self.assertIn("result", client.receive())
                    break
                except (FileNotFoundError, ConnectionRefusedError):
                    time.sleep(.01)
            else:
                self.fail("daemon not ready")
        return process

    def client(self, path=None):
        client = Client(path or self.path)
        self.clients.append(client)
        return client

    def call(self, *args, **kwargs):
        with Client(self.path) as client:
            client.send(*args, **kwargs)
            return client.receive()

    def get(self):
        selected = json.loads(self.call()["result"]["contents"][0]["text"])
        self.assertIs(selected["exists"], True)
        return {"revision": selected["revision"], "settings": selected["value"]}

    def change(self, old, color="dark", settings=None):
        value = copy.deepcopy(old["settings"] if settings is None else settings)
        value["appearance"]["color_scheme"] = color
        return self.call("tools/call", {"name": "settings.set", "arguments": {
            "expected_revision": old["revision"], "settings": value}})["result"]

    def section(self, old, section, value):
        return self.call("tools/call", {"name": "settings.set_section", "arguments": {
            "expected_revision": old["revision"], "section": section, "value": value}})["result"]

    def quiet(self, client):
        client.sock.settimeout(.12)
        with self.assertRaises(socket.timeout):
            client.receive()
        client.sock.settimeout(2)
