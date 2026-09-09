"""Real daemon/socket tests, only private temporary paths; no desktop services."""
import copy
import json
import os
from pathlib import Path
import resource
import socket
import subprocess
import sys
import tempfile
import time
import unittest

IFACE = "dev.rockorager.ouro.Settings"
STANDARD = "org.varlink.service."
ROOT = Path(__file__).resolve().parents[1]

if len(sys.argv) > 1 and sys.argv[1] == "--activate":
    fd = int(sys.argv[2])
    os.dup2(fd, 3, inheritable=True)
    if fd != 3:
        os.close(fd)
    os.environ["LISTEN_PID"] = str(os.getpid())
    os.environ.setdefault("LISTEN_FDS", "1")
    os.execv(sys.argv[3], sys.argv[3:])

EXE = str(Path(sys.argv.pop(1)).resolve()) if len(sys.argv) > 1 else str(ROOT / "zig-out/bin/ourosettings")


def frame(method="Get", params=None, **options):
    return json.dumps({"method": method if "." in method else IFACE + "." + method,
                       "parameters": {} if params is None else params, **options}).encode() + b"\0"


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
        while b"\0" not in self.buffer:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise EOFError
            self.buffer += chunk
        message, self.buffer = self.buffer.split(b"\0", 1)
        return json.loads(message)

    def close(self):
        self.sock.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


class DaemonTests(unittest.TestCase):
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
        self.path = self.root / "runtime/settings.sock"
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
                        client.receive()
                    break
                except (FileNotFoundError, ConnectionRefusedError):
                    time.sleep(.01)
            else:
                self.fail("daemon not ready")
        return process

    def client(self):
        client = Client(self.path)
        self.clients.append(client)
        return client

    def call(self, *args, **kwargs):
        with Client(self.path) as client:
            client.send(*args, **kwargs)
            return client.receive()

    def get(self):
        return self.call()["parameters"]

    def change(self, old, color="dark", settings=None):
        value = copy.deepcopy(settings or old["settings"])
        value["appearance"]["color_scheme"] = color
        return self.call("Set", {"expected_revision": old["revision"], "settings": value})

    def quiet(self, client):
        client.sock.settimeout(.12)
        with self.assertRaises(socket.timeout):
            client.receive()
        client.sock.settimeout(2)

    def test_stale_writers_watch_noop_restart(self):
        process = self.spawn()
        old = self.get()
        first, second = self.client(), self.client()
        first.send(); second.send()
        self.assertEqual(first.receive()["parameters"], old)
        self.assertEqual(second.receive()["parameters"], old)
        watcher = self.client()
        watcher.send("Watch", more=True)
        self.assertEqual(watcher.receive(), {"parameters": old, "continues": True})
        changed = self.change(old)["parameters"]
        self.assertNotEqual(changed["revision"], old["revision"])
        self.assertEqual(watcher.receive(), {"parameters": changed, "continues": True})
        stale = self.change(old, "light")
        self.assertEqual(stale, {"error": IFACE + ".Conflict", "parameters": {"revision": changed["revision"]}})
        self.assertEqual(self.change(changed)["parameters"], changed)
        self.quiet(watcher)
        process.terminate(); self.assertEqual(process.wait(timeout=3), 0)
        self.assertFalse(self.path.exists())
        for client in self.clients: client.close()
        self.spawn()
        self.assertEqual(self.get(), changed)
        self.assertEqual(json.loads(self.state.read_text()), {"version": 1, **changed})
        self.assertEqual(self.state.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.state.parent.stat().st_mode & 0o777, 0o700)

    def test_framing_fragmentation_pipelining_discovery(self):
        self.spawn()
        client = self.client()
        data = frame()
        for chunk in (data[:3], data[3:17], data[17:-1]):
            client.sock.sendall(chunk)
            self.quiet(client)
        client.sock.sendall(data[-1:])
        expected = client.receive()
        client.sock.sendall(frame() + frame("org.varlink.service.GetInfo") + frame())
        self.assertEqual(client.receive(), expected)
        self.assertEqual(client.receive()["parameters"]["interfaces"], ["org.varlink.service", IFACE])
        self.assertEqual(client.receive(), expected)
        for interface in (IFACE, "org.varlink.service"):
            result = self.call("org.varlink.service.GetInterfaceDescription", {"interface": interface})
            self.assertEqual(result["parameters"]["description"], (ROOT / f"protocol/{interface}.varlink").read_text())

    def test_invalid_protocol_and_bounds(self):
        self.spawn()
        invalid = [b"", b"{", b"[]", b"{}", b'{"method":3}', b'{"method":"x","method":"y"}',
                   frame(more=None)[:-1], frame(upgrade=True)[:-1], frame(oneway=True)[:-1], frame(more=True)[:-1],
                   b'{"method":"dev.rockorager.ouro.Settings.Get","parameters":null}',
                   frame("Get", {"extra": 1})[:-1], b'{"method":"dev.rockorager.ouro.Settings.Get","extra":0}',
                   frame("Set", {"settings": {}})[:-1], frame("Set", {"expected_revision": None, "settings": {}})[:-1],
                   b'{"' + b'x' * 250000 + b'":0}', frame("x" * 250000)[:-1],
                   frame("org.varlink.service.GetInterfaceDescription", {"interface": "x" * 250000})[:-1],
                   b'[' * 20000 + b']' * 20000, b'{"method":"\xff"}']
        for data in invalid:
            with self.subTest(data=data), Client(self.path) as client:
                client.sock.sendall(data + b"\0")
                self.assertEqual(client.receive()["error"], STANDARD + "InvalidParameter")
        self.assertEqual(self.call("Watch"), {"error": STANDARD + "ExpectedMore", "parameters": {}})
        self.assertEqual(self.call("Missing")["error"], STANDARD + "MethodNotFound")
        self.assertEqual(self.call("bad.iface.Get")["error"], STANDARD + "InterfaceNotFound")
        with Client(self.path) as client:
            client.sock.sendall(b"x" * (256 * 1024))
            self.assertEqual(client.receive()["error"], STANDARD + "InvalidParameter")
            with self.assertRaises(EOFError): client.receive()
        self.get()

    def test_schema_realistic_snapshot_and_rejections(self):
        self.spawn()
        old = self.get()
        example = json.loads((ROOT / "examples/settings.json").read_text())["settings"]
        accepted = self.change(old, "light", example)["parameters"]
        self.assertEqual(accepted["settings"], example)
        cases = []
        def bad(path, value):
            data = copy.deepcopy(example)
            target = data
            for key in path[:-1]: target = target[key]
            target[path[-1]] = value
            cases.append(data)
        bad(["appearance", "accent", "r"], 1.01)
        bad(["appearance", "accent", "g"], -0.1)
        bad(["appearance", "color_scheme"], "auto")
        bad(["appearance", "accent", "b"], "0.5")
        bad(["outputs", 0, "settings", "scale"], 0)
        bad(["outputs", 0, "settings", "scale"], 1e309)
        bad(["outputs", 0, "settings", "mode", "width"], -1)
        bad(["outputs", 0, "settings", "mode", "width"], 2**32)
        bad(["outputs", 0, "settings", "mode", "width"], 1.5)
        bad(["outputs", 0, "priority"], 2**31)
        bad(["outputs", 0, "match", "connector_id"], "42")
        bad(["outputs", 0, "settings", "icc_profile"], "relative.icc")
        bad(["wallpaper", "default", "image"], "https://example.com/image.png")
        bad(["keybindings", 0, "action"], [])
        bad(["keybindings", 0, "trigger"], "ctrl+control+q")
        bad(["outputs"], [example["outputs"][0]] * 2)
        bad(["keybindings"], [{"trigger": "logo+ctrl+q", "action": ["close"]}, {"trigger": "Control+Super+Q", "action": ["exit"]}])
        bad(["wallpaper", "outputs"], [example["wallpaper"]["outputs"][0]] * 2)
        bad(["outputs", 0, "settings", "scale_120"], 120)
        bad(["preferred_output"], {"make": "invented identity"})
        bad(["appearance"], None)
        bad(["wallpaper", "default", "image"], "/a\0b")
        for data in cases:
            with self.subTest(data=data):
                result = self.call("Set", {"expected_revision": accepted["revision"], "settings": data})
                self.assertEqual(result["error"], STANDARD + "InvalidParameter")
        missing = copy.deepcopy(example); del missing["outputs"]
        self.assertEqual(self.change(accepted, settings=missing)["error"], STANDARD + "InvalidParameter")
        # Duplicate keys must be rejected deep inside the snapshot too.
        raw = frame("Set", {"expected_revision": accepted["revision"], "settings": example}).replace(b'"r": 0.2', b'"r": 0.2, "r": 0.3')
        with Client(self.path) as client:
            client.sock.sendall(raw)
            self.assertEqual(client.receive()["error"], STANDARD + "InvalidParameter")
        self.assertEqual(self.get(), accepted)

    def test_disk_failures_do_not_publish(self):
        process = self.spawn()
        old = self.get(); before = self.state.read_bytes()
        watcher = self.client(); watcher.send("Watch", more=True); watcher.receive()
        for failure in ("write", "file-fsync", "rename"):
            self.fault.write_text(failure)
            self.assertEqual(self.change(old), {"error": IFACE + ".PersistenceFailed", "parameters": {}})
            self.assertEqual(self.get(), old)
            self.assertEqual(self.state.read_bytes(), before)
            self.assertEqual(list(self.state.parent.glob("*.tmp")), [])
            self.quiet(watcher)
        self.fault.unlink()
        # Real kernel write failure, including a partially written temporary file.
        soft, hard = resource.prlimit(process.pid, resource.RLIMIT_FSIZE)
        resource.prlimit(process.pid, resource.RLIMIT_FSIZE, (64, hard))
        self.assertEqual(self.change(old)["error"], IFACE + ".PersistenceFailed")
        resource.prlimit(process.pid, resource.RLIMIT_FSIZE, (soft, hard))
        self.assertEqual(self.state.read_bytes(), before); self.quiet(watcher)
        os.chmod(self.state.parent, 0o500)
        try:
            self.assertEqual(self.change(old)["error"], IFACE + ".PersistenceFailed")
            self.assertEqual(self.get(), old); self.quiet(watcher)
        finally:
            os.chmod(self.state.parent, 0o700)
        self.assertNotEqual(self.change(old)["parameters"]["revision"], old["revision"])

    def test_post_rename_failure_is_ambiguous_and_restart_recovers(self):
        process = self.spawn(); old = self.get()
        watcher = self.client(); watcher.send("Watch", more=True); watcher.receive()
        self.fault.write_text("directory-fsync")
        with self.assertRaises(EOFError): self.change(old)
        self.assertEqual(process.wait(timeout=3), 1)
        with self.assertRaises(EOFError): watcher.receive()
        persisted = json.loads(self.state.read_text())
        self.assertEqual(persisted["settings"]["appearance"]["color_scheme"], "dark")
        self.assertNotEqual(persisted["revision"], old["revision"])
        self.fault.unlink(); self.spawn()
        self.assertEqual(self.get(), {k: persisted[k] for k in ("revision", "settings")})

    def test_corrupt_unknown_and_external_state_retained(self):
        process = self.spawn(); old = self.get()
        original = self.state.read_bytes()
        self.state.write_text("corrupt")
        self.assertEqual(self.change(old)["error"], IFACE + ".PersistenceFailed")
        self.assertEqual(self.get(), old)
        process.terminate(); process.wait(timeout=3)
        for content in (b"corrupt", original.replace(b'"version":1', b'"version":2'),
                        original.replace(b'"version":1', b'"version":1,"version":1'), b"null"):
            self.state.write_bytes(content)
            failed = self.spawn(wait=False)
            self.assertEqual(failed.wait(timeout=3), 1)
            self.assertEqual(self.state.read_bytes(), content)
            self.assertFalse(self.path.exists())
        self.state.write_bytes(original)
        self.spawn()
        self.assertEqual(self.get(), old)

    def test_singleton_and_socket_safety(self):
        process = self.spawn(); before = self.state.read_bytes()
        for path in (self.path, self.root / "other/settings.sock"):
            second = self.spawn(wait=False, path=path)
            self.assertEqual(second.wait(timeout=3), 1)
        self.assertEqual(self.state.read_bytes(), before); self.get()
        process.terminate(); process.wait(timeout=3)
        self.path.write_text("not a socket")
        failed = self.spawn(wait=False); self.assertEqual(failed.wait(timeout=3), 1)
        self.assertEqual(self.path.read_text(), "not a socket")
        self.path.unlink(); self.path.symlink_to(self.state)
        failed = self.spawn(wait=False); self.assertEqual(failed.wait(timeout=3), 1)
        self.assertTrue(self.path.is_symlink()); self.assertEqual(self.state.read_bytes(), before)
        self.path.unlink()
        stale = socket.socket(socket.AF_UNIX); stale.bind(str(self.path)); stale.close()
        inode = self.path.stat().st_ino
        failed = self.spawn(wait=False); self.assertEqual(failed.wait(timeout=3), 1)
        self.assertEqual(self.path.stat().st_ino, inode)

    def test_unsafe_state_directory_symlink_and_lock(self):
        process = self.spawn(); process.terminate(); process.wait(timeout=3)
        before = self.state.read_bytes()
        os.chmod(self.state, 0o644)
        failed = self.spawn(wait=False); self.assertEqual(failed.wait(timeout=3), 1)
        self.assertEqual(self.state.stat().st_mode & 0o777, 0o644)
        os.chmod(self.state, 0o600)
        os.chmod(self.path.parent, 0o755)
        failed = self.spawn(wait=False); self.assertEqual(failed.wait(timeout=3), 1)
        os.chmod(self.path.parent, 0o700)
        saved = self.root / "saved"; self.state.rename(saved); self.state.symlink_to(saved)
        failed = self.spawn(wait=False); self.assertEqual(failed.wait(timeout=3), 1)
        self.assertTrue(self.state.is_symlink()); self.assertEqual(saved.read_bytes(), before)
        self.state.unlink(); saved.rename(self.state)
        link = self.root / "linked"; link.symlink_to(self.state.parent)
        failed = self.spawn(wait=False, state=link / "settings.json"); self.assertEqual(failed.wait(timeout=3), 1)
        lock = self.state.with_suffix(".json.lock"); lock.unlink(); lock.symlink_to(self.state)
        failed = self.spawn(wait=False); self.assertEqual(failed.wait(timeout=3), 1)
        self.assertEqual(self.state.read_bytes(), before)

    def test_cleanup_does_not_unlink_replacement(self):
        process = self.spawn()
        self.path.unlink(); self.path.write_text("replacement")
        process.terminate(); self.assertEqual(process.wait(timeout=3), 0)
        self.assertEqual(self.path.read_text(), "replacement")

    def test_activation_idle_subscription_reactivation(self):
        self.path.parent.mkdir(mode=0o700)
        with socket.socket(socket.AF_UNIX) as listener:
            listener.bind(str(self.path)); os.chmod(self.path, 0o600); listener.listen(32)
            process = self.spawn(listener=listener, idle=150)
            old = self.get()
            watcher = self.client(); watcher.send("Watch", more=True); watcher.receive()
            time.sleep(.3); self.assertIsNone(process.poll())
            watcher.close(); self.assertEqual(process.wait(timeout=3), 0)
            self.assertTrue(self.path.exists())
            # Queued connection survives service exit and is handled by new exec.
            client = self.client(); client.send()
            self.spawn(listener=listener, idle=150)
            self.assertEqual(client.receive()["parameters"], old)

    def test_invalid_activation(self):
        for env in ({"LISTEN_FDS": "1"}, {"LISTEN_PID": "1"}, {"LISTEN_FDS": "2", "LISTEN_PID": "1"}):
            failed = self.spawn(wait=False, env=env)
            self.assertEqual(failed.wait(timeout=3), 1)
        self.path.parent.mkdir(mode=0o700, exist_ok=True)
        with socket.socket(socket.AF_UNIX) as listener:
            listener.bind(str(self.path)); os.chmod(self.path, 0o600)
            failed = self.spawn(wait=False, listener=listener)
            self.assertEqual(failed.wait(timeout=3), 1)  # not listening
            listener.listen(32)
            failed = self.spawn(wait=False, listener=listener, path=self.path.with_name("wrong.sock"))
            self.assertEqual(failed.wait(timeout=3), 1)
            os.chmod(self.path, 0o666)
            failed = self.spawn(wait=False, listener=listener)
            self.assertEqual(failed.wait(timeout=3), 1)

    def test_slow_subscriber_and_client_bound(self):
        self.spawn()
        old = self.get()
        settings = copy.deepcopy(old["settings"])
        settings["wallpaper"]["default"]["image"] = "/" + "a" * 90000
        current = self.change(old, settings=settings)["parameters"]
        watcher = self.client(); watcher.send("Watch", more=True)
        # Do not read: initial and later large snapshots fill the Unix buffer.
        for i in range(15):
            current = self.change(current, "light" if i % 2 == 0 else "dark")["parameters"]
        self.assertEqual(self.get(), current)
        count = 0
        try:
            while True:
                watcher.receive(); count += 1
        except EOFError:
            pass
        self.assertLess(count, 16)
        self.assertGreaterEqual(count, 1)
        # Oversized but structurally valid state is refused, not published.
        settings["wallpaper"]["default"]["image"] = "/" + "b" * 140000
        self.assertEqual(self.change(current, settings=settings)["error"], STANDARD + "InvalidParameter")
        self.assertEqual(self.get(), current)
        held = [self.client() for _ in range(32)]
        time.sleep(.1)
        with Client(self.path) as excess:
            try:
                excess.send(); excess.receive()
                self.fail("33rd client accepted")
            except (EOFError, ConnectionResetError, BrokenPipeError):
                pass
        for client in held: client.close()

    def test_nullable_fields_and_recreated_state_token(self):
        process = self.spawn(); old = self.get()
        settings = copy.deepcopy(old["settings"])
        settings["appearance"]["accent"] = None
        settings["preferred_output"] = None
        settings["wallpaper"]["default"]["image"] = None
        self.assertEqual(self.call("Set", {"expected_revision": old["revision"], "settings": settings})["parameters"], old)
        process.terminate(); process.wait(timeout=3)
        self.state.unlink()
        self.spawn()
        recreated = self.get()
        self.assertNotEqual(recreated["revision"], old["revision"])
        self.assertEqual(recreated["settings"], old["settings"])
        self.assertEqual(self.change(old)["error"], IFACE + ".Conflict")

    def test_xdg_defaults_home_fallback_and_direct_idle(self):
        runtime = self.root / "runtime"; runtime.mkdir(mode=0o700)
        config = self.root / "config"
        home = self.root / "home"; home.mkdir(mode=0o700)
        for config_env, expected in ((str(config), config / "ouro/settings.json"), ("", home / ".config/ouro/settings.json")):
            env = {k: v for k, v in os.environ.items() if not k.startswith("LISTEN_")}
            env.update(XDG_RUNTIME_DIR=str(runtime), XDG_CONFIG_HOME=config_env, HOME=str(home))
            process = subprocess.Popen([EXE, "--idle-ms", "100"], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            self.processes.append(process)
            self.assertEqual(process.wait(timeout=3), 0, process.communicate()[1])
            self.assertEqual(json.loads(expected.read_text())["version"], 1)
            self.assertFalse((runtime / "ouro/settings.sock").exists())
        for args in (["--idle-ms", "0"], ["--idle-ms", "300001"], ["--state", "relative"], ["--unknown"]):
            result = subprocess.run([EXE, "--socket", str(self.path), *args], env=env, capture_output=True, timeout=3)
            self.assertNotEqual(result.returncode, 0)

    def test_partial_request_deadline_and_watch_pipeline_policy(self):
        process = self.spawn(idle=100)
        client = self.client(); client.sock.sendall(b'{"method":')
        time.sleep(.2); self.assertIsNone(process.poll())
        # Exercise the actual 30-second request deadline without a production
        # credential or timeout bypass. A subscriber has no such idle deadline.
        client.sock.settimeout(32)
        with self.assertRaises(EOFError): client.receive()
        self.assertEqual(process.wait(timeout=3), 0)
        self.spawn()
        watcher = self.client(); watcher.sock.sendall(frame("Watch", more=True) + frame())
        self.assertTrue(watcher.receive()["continues"])
        with self.assertRaises(EOFError): watcher.receive()
        self.get()

    def test_foreign_owned_state_and_socket_retained(self):
        if os.geteuid() == 0 or subprocess.run(["sudo", "-n", "true"], capture_output=True).returncode:
            self.skipTest("requires passwordless sudo to construct foreign-owned fixtures")
        process = self.spawn(); process.terminate(); process.wait(timeout=3)
        before = self.state.read_bytes()
        subprocess.run(["sudo", "-n", "chown", "0:0", str(self.state)], check=True)
        try:
            failed = self.spawn(wait=False); self.assertEqual(failed.wait(timeout=3), 1)
            self.assertEqual(self.state.stat().st_uid, 0)
        finally:
            subprocess.run(["sudo", "-n", "chown", f"{os.getuid()}:{os.getgid()}", str(self.state)], check=True)
        self.assertEqual(self.state.read_bytes(), before)
        with socket.socket(socket.AF_UNIX) as listener:
            listener.bind(str(self.path)); listener.listen(32); os.chmod(self.path, 0o600)
            subprocess.run(["sudo", "-n", "chown", "0:0", str(self.path)], check=True)
            failed = self.spawn(wait=False, listener=listener); self.assertEqual(failed.wait(timeout=3), 1)
            self.assertEqual(self.path.stat().st_uid, 0)

    def test_same_uid_peer_credentials(self):
        if os.geteuid() == 0 or subprocess.run(["sudo", "-n", "true"], capture_output=True).returncode:
            self.skipTest("requires non-root daemon and passwordless sudo for foreign-UID client")
        self.spawn()
        result = subprocess.run(["sudo", "-n", sys.executable, "-c",
            "import socket,sys; s=socket.socket(socket.AF_UNIX); s.settimeout(2); s.connect(sys.argv[1]); print(s.recv(4096).decode())",
            str(self.path)], capture_output=True, text=True, check=True)
        self.assertEqual(json.loads(result.stdout.strip().rstrip("\0")), {"error": STANDARD + "PermissionDenied", "parameters": {}})
        self.get()


if __name__ == "__main__":
    unittest.main(verbosity=2)
