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

    def section(self, old, section, value):
        return self.call("SetSection", {"expected_revision": old["revision"], "section": section, "value": value})

    def path_event(self, client, revision, exists, value_json):
        self.assertEqual(client.receive(), {"parameters": {
            "revision": revision, "exists": exists, "value_json": value_json}, "continues": True})

    def test_watch_path_initial_rfc6901_and_types(self):
        self.spawn(); old = self.get()
        data = {"": {"": "empty"}, "a/b": {"~key": 7}, "~1": "literal tilde-one",
                "/": "slash", "雪": "☃", "é": 1, "e\u0301": 2, "\0": False,
                "array": [None, False, 0, "", [], {}, "last"],
                "0": "zero key", "01": "leading zero key", "-": "dash key",
                "*": "literal star", "%2F": "not decoded", "plain": "scalar"}
        current = self.section(old, "compositor", {"general": data})["parameters"]
        cases = [("", True, current["settings"]), ("/compositor", True, {"general": data}),
                 ("/appearance/color_scheme", True, "default"), ("/appearance/accent", False, None),
                 ("/preferred_output", False, None), ("/settings", False, None), ("/revision", False, None),
                 ("/", False, None), ("/missing/deeper", False, None)]
        base = "/compositor/general/"
        cases += [(base + path, True, value) for path, value in (
            ("/", "empty"), ("a~1b/~0key", 7), ("~01", "literal tilde-one"), ("~1", "slash"),
            ("雪", "☃"), ("é", 1), ("e\u0301", 2), ("\0", False), ("0", "zero key"),
            ("01", "leading zero key"), ("-", "dash key"), ("*", "literal star"), ("%2F", "not decoded"),
            ("array", data["array"]))]
        cases += [(base + "array/" + str(i), True, value) for i, value in enumerate(data["array"])]
        cases += [(base + "array/" + index, False, None) for index in (
            "7", "-", "00", "01", "-1", "+1", "1.0", "1e0", " 1", "١", "", "9" * 100)]
        cases += [(base + "plain/0", False, None), (base + "array/0/x", False, None)]
        for path, exists, expected in cases:
            with self.subTest(path=path), Client(self.path) as client:
                client.send("WatchPath", {"path": path}, more=True)
                reply = client.receive()
                self.assertEqual(set(reply), {"parameters", "continues"})
                self.assertIs(reply["continues"], True)
                params = reply["parameters"]
                self.assertEqual(set(params), {"revision", "exists", "value_json"})
                self.assertEqual(params["revision"], current["revision"])
                self.assertIs(params["exists"], exists)
                self.assertIsInstance(params["value_json"], str)
                decoded = json.loads(params["value_json"])
                self.assertIs(type(decoded), type(expected))  # false is not zero
                self.assertEqual(decoded, expected)
                if not exists: self.assertEqual(params["value_json"], "null")

    def test_watch_path_validation_framing_and_lifecycle(self):
        process = self.spawn(idle=100); old = self.get()
        self.assertEqual(self.call("WatchPath", {"path": ""}), {"error": STANDARD + "ExpectedMore", "parameters": {}})
        for path in ("appearance", "#", "#/compositor", "$", "/~", "/~2", "/~00~", "/missing/~x"):
            result = self.call("WatchPath", {"path": path}, more=True)
            self.assertEqual(result, {"error": STANDARD + "InvalidParameter", "parameters": {"parameter": "path"}})
        for params in ({}, {"path": None}, {"path": 0}, {"path": []}, {"path": "", "extra": 0}):
            self.assertEqual(self.call("WatchPath", params, more=True)["error"], STANDARD + "InvalidParameter")
        for options in ({"more": None}, {"more": True, "oneway": True}, {"more": True, "upgrade": True}):
            self.assertEqual(self.call("WatchPath", {"path": ""}, **options)["error"], STANDARD + "InvalidParameter")
        watcher = self.client()
        request = frame("WatchPath", {"path": "/appearance/color_scheme"}, more=True)
        watcher.sock.sendall(request[:-1]); self.quiet(watcher)
        watcher.sock.sendall(request[-1:]); self.path_event(watcher, old["revision"], True, '"default"')
        time.sleep(.2); self.assertIsNone(process.poll())
        watcher.close(); self.assertEqual(process.wait(timeout=3), 0); self.assertFalse(self.path.exists())
        process = self.spawn(idle=100)
        watcher = self.client()
        # WatchPath occupies its connection exactly like Watch: never execute
        # the pipelined mutation, and free the path/selection when closing.
        watcher.sock.sendall(frame("WatchPath", {"path": "/missing"}, more=True) +
            frame("SetSection", {"expected_revision": old["revision"], "section": "compositor", "value": {"bindings": None}}))
        self.path_event(watcher, old["revision"], False, "null")
        with self.assertRaises(EOFError): watcher.receive()
        self.assertEqual(self.get(), old)
        self.assertEqual(process.wait(timeout=3), 0)

    def test_watch_path_large_encoded_values_and_paths(self):
        self.spawn()
        current = self.section(self.get(), "compositor", {"general": {"value": "\\" * 63000}})["parameters"]
        with Client(self.path) as watcher:
            watcher.send("WatchPath", {"path": ""}, more=True)
            reply = watcher.receive()
            self.assertEqual(json.loads(reply["parameters"]["value_json"]), current["settings"])
            # JSON text adds another escaping layer but stays within the same
            # reply limit, including NUL, without truncation or daemon failure.
            encoded = json.dumps(reply, separators=(",", ":")).encode() + b"\0"
            self.assertGreater(len(encoded), 250000)
            self.assertLessEqual(len(encoded), 256 * 1024)
        key = "/" * 63000
        current = self.section(current, "compositor", {"general": {key: 7}})["parameters"]
        with Client(self.path) as watcher:
            watcher.send("WatchPath", {"path": "/compositor/general/" + "~1" * 63000}, more=True)
            self.path_event(watcher, current["revision"], True, "7")
            current = self.section(current, "compositor", {})["parameters"]
            self.path_event(watcher, current["revision"], False, "null")
        with Client(self.path) as watcher:
            watcher.sock.sendall(frame("WatchPath", {"path": "/" * (256 * 1024)}, more=True))
            self.assertEqual(watcher.receive()["error"], STANDARD + "InvalidParameter")
            with self.assertRaises((EOFError, ConnectionResetError)): watcher.receive()
        self.assertEqual(self.get(), current)

    def test_watch_path_changes_ancestors_and_revision_skips(self):
        self.spawn(); old = self.get()
        full = self.client(); full.send("Watch", more=True); full.receive()
        root = self.client(); root.send("WatchPath", {"path": ""}, more=True)
        self.assertEqual(json.loads(root.receive()["parameters"]["value_json"]), old["settings"])
        selected = self.client(); selected.send("WatchPath", {"path": "/compositor/output_rules/internal/settings/enabled"}, more=True)
        self.path_event(selected, old["revision"], False, "null")
        current = self.change(old)["parameters"]
        self.assertEqual(full.receive()["parameters"], current)
        self.assertEqual(json.loads(root.receive()["parameters"]["value_json"]), current["settings"])
        self.quiet(selected)
        self.assertEqual(self.section(old, "compositor", {})["error"], IFACE + ".Conflict")
        previous = current
        # Whole parent replacement: unchanged leaf must stay quiet even when
        # nearby names share a textual prefix. Missing/null/false/0 are distinct.
        steps = [({"internal-extra": {"settings": {"enabled": True}}}, False, None),
                 ({"internal": {"settings": {"enabled": None}}}, True, "null"),
                 ({"internal": {"settings": {"enabled": False}}}, True, "false"),
                 ({"internal": {"priority": 7, "settings": {"enabled": False, "scale": 2}}}, False, None),
                 ({"internal": {"settings": {"enabled": 0}}}, True, "0"),
                 ({"internal": {"settings": {"enabled": []}}}, True, "[]"),
                 ({"internal": {"settings": {"enabled": {}}}}, True, "{}"),
                 ({"internal": {"settings": {"enabled": ""}}}, True, '""'),
                 ({"internal": None}, True, None),
                 ({"internal": {"settings": {"enabled": True}}}, True, "true")]
        for index, (rules, changed, value) in enumerate(steps):
            config = {"output_rules": rules}
            if index % 2:
                settings = copy.deepcopy(current["settings"]); settings["compositor"] = config
                current = self.call("Set", {"expected_revision": current["revision"], "settings": settings})["parameters"]
            else:
                current = self.section(current, "compositor", config)["parameters"]
            self.assertNotEqual(previous["revision"], current["revision"])
            self.assertEqual(full.receive()["parameters"], current)
            root_event = root.receive()["parameters"]
            self.assertEqual(root_event["revision"], current["revision"])
            self.assertEqual(json.loads(root_event["value_json"]), current["settings"])
            if changed: self.path_event(selected, current["revision"], value is not None, value or "null")
            else: self.quiet(selected)
            previous = current
        # No-op, failed persistence, or stale request never publish to either kind.
        self.assertEqual(self.section(current, "compositor", config)["parameters"], current)
        self.fault.write_text("rename")
        self.assertEqual(self.section(current, "compositor", {})["error"], IFACE + ".PersistenceFailed")
        self.assertEqual(self.get(), current)
        for client in (full, root, selected): self.quiet(client)

    def test_watch_path_canonical_numbers_and_array_replacement(self):
        self.spawn(); current = self.get()
        selected = self.client(); selected.send("WatchPath", {"path": "/compositor/general/value"}, more=True)
        item = self.client(); item.send("WatchPath", {"path": "/compositor/general/value/1"}, more=True)
        self.path_event(selected, current["revision"], False, "null")
        self.path_event(item, current["revision"], False, "null")
        for raw in ("1", "1.0", "1e0", "9007199254740993", "0.12345678901234567890",
                    '{"z":false,"a":null}', '{"a":null,"z":false}', '["first",false]', '[false,"first"]', '[]'):
            previous = current
            request = frame("SetSection", {"expected_revision": current["revision"], "section": "compositor", "value": {}})
            request = request.replace(b'"value": {}', ('"value":{"general":{"value":' + raw + '}}').encode())
            with Client(self.path) as writer:
                writer.sock.sendall(request); current = writer.receive()["parameters"]
            if raw == '{"a":null,"z":false}':
                self.assertEqual(current["revision"], previous["revision"]); self.quiet(selected)
            else:
                self.assertNotEqual(current["revision"], previous["revision"])
                self.path_event(selected, current["revision"], True, '{"a":null,"z":false}' if raw.startswith('{') else raw)
            if raw in ('["first",false]', '[false,"first"]', '[]'):
                self.path_event(item, current["revision"], raw != '[]', 'false' if raw == '["first",false]' else ('"first"' if raw == '[false,"first"]' else 'null'))
            else: self.quiet(item)
        # Raw subtree equality remains unchanged even if sibling replacement
        # advances the global revision; the next relevant event skips that token.
        prior = current
        current = self.section(current, "compositor", {"general": {"value": [], "other": True}})["parameters"]
        self.assertNotEqual(current["revision"], prior["revision"])
        self.quiet(selected); self.quiet(item)

    def test_watch_path_slow_subscriber_isolation(self):
        self.path.parent.mkdir(mode=0o700)
        with socket.socket(socket.AF_UNIX) as listener:
            # The test shim sets SO_SNDBUF on accepted sockets. A 90KB selection
            # cannot flush until the client reads: test pending output, not luck.
            self.fault.write_text("small-send-buffer")
            listener.bind(str(self.path)); os.chmod(self.path, 0o600); listener.listen(32)
            self.spawn(listener=listener)
            current = self.section(self.get(), "compositor", {"general": {"large": "A" * 90000}})["parameters"]
            initial = current
            slow = self.client(); slow.send("WatchPath", {"path": "/compositor/general/large"}, more=True)
            self.assertEqual(slow.sock.recv(1, socket.MSG_PEEK), b'{')
            full = self.client(); full.send("Watch", more=True); full.receive()
            fast = self.client(); fast.send("WatchPath", {"path": "/appearance/color_scheme"}, more=True); fast.receive()
            for color in ("dark", "light", "dark"):
                current = self.section(current, "appearance", {"color_scheme": color})["parameters"]
                self.assertEqual(full.receive()["parameters"], current)
                self.path_event(fast, current["revision"], True, json.dumps(color))
            # Unrelated writes must not disconnect a partially sent initial reply.
            self.path_event(slow, initial["revision"], True, json.dumps("A" * 90000)); self.quiet(slow)
            current = self.section(current, "compositor", {"general": {"large": "B" * 90000}})["parameters"]
            pending = current
            self.assertEqual(slow.sock.recv(1, socket.MSG_PEEK), b'{')
            self.assertEqual(full.receive()["parameters"], current)
            current = self.section(current, "appearance", {"color_scheme": "light"})["parameters"]
            self.assertEqual(full.receive()["parameters"], current)
            self.path_event(fast, current["revision"], True, '"light"')
            self.path_event(slow, pending["revision"], True, json.dumps("B" * 90000)); self.quiet(slow)
            current = self.section(current, "compositor", {"general": {"large": "C" * 90000}})["parameters"]
            self.assertEqual(slow.sock.recv(1, socket.MSG_PEEK), b'{')
            self.assertEqual(full.receive()["parameters"], current)
            current = self.section(current, "compositor", {"general": {"large": "D" * 90000}})["parameters"]
            self.assertEqual(full.receive()["parameters"], current)
            with self.assertRaises(EOFError): slow.receive()  # discard incomplete C
            self.quiet(fast); self.assertEqual(self.get(), current)

    def test_section_replacement_conflict_noop_restart(self):
        process = self.spawn()
        example = json.loads((ROOT / "examples/settings.json").read_text())["settings"]
        old = self.change(self.get(), "light", example)["parameters"]
        # Full Set with non-default compositor values must survive restart too.
        process.terminate(); self.assertEqual(process.wait(timeout=3), 0)
        process = self.spawn(); self.assertEqual(self.get(), old)
        watcher = self.client(); watcher.send("Watch", more=True)
        self.assertEqual(watcher.receive(), {"parameters": old, "continues": True})
        first, second = self.client(), self.client()
        first.send("SetSection", {"expected_revision": old["revision"], "section": "appearance", "value": {"color_scheme": "dark"}})
        second.send("SetSection", {"expected_revision": old["revision"], "section": "compositor", "value": {}})
        results = [first.receive(), second.receive()]
        successes = [r["parameters"] for r in results if "error" not in r]
        conflicts = [r for r in results if "error" in r]
        self.assertEqual(len(successes), 1); self.assertEqual(len(conflicts), 1)
        current = successes[0]
        self.assertEqual(conflicts[0], {"error": IFACE + ".Conflict", "parameters": {"revision": current["revision"]}})
        self.assertEqual(watcher.receive(), {"parameters": current, "continues": True})
        # Whichever request won, retry the appearance edit from the fresh token.
        changed = self.section(current, "appearance", {"color_scheme": "dark"})["parameters"]
        if changed != current: self.assertEqual(watcher.receive()["parameters"], changed)
        expected = copy.deepcopy(current["settings"])
        expected["appearance"] = {"color_scheme": "dark"}  # accent removed, not merged
        self.assertEqual(changed["settings"], expected)
        # Restore a non-default compositor and verify every unrelated section.
        current = self.section(changed, "compositor", example["compositor"])["parameters"]
        if current != changed: self.assertEqual(watcher.receive()["parameters"], current)
        expected["compositor"] = example["compositor"]
        self.assertEqual(current["settings"], expected)
        changed = self.section(current, "appearance", example["appearance"])["parameters"]
        self.assertEqual(watcher.receive()["parameters"], changed)
        self.assertEqual(changed["settings"], example)
        before = self.state.stat().st_mtime_ns
        self.fault.write_text("write")  # no-op must not attempt persistence
        self.assertEqual(self.section(changed, "appearance", example["appearance"])["parameters"], changed)
        self.assertEqual(self.state.stat().st_mtime_ns, before); self.quiet(watcher)
        self.fault.unlink()
        process.terminate(); self.assertEqual(process.wait(timeout=3), 0)
        self.spawn(); self.assertEqual(self.get(), changed)

    def test_section_optional_values_and_failures(self):
        self.spawn(); old = self.get()
        for section in ("appearance", "compositor", "wallpaper", "outputs", "keybindings", "unknown"):
            for extra in ({}, {"value": None}, {"value": []}):
                result = self.call("SetSection", {"expected_revision": old["revision"], "section": section, **extra})
                self.assertEqual(result["error"], STANDARD + "InvalidParameter")
        for params in ({"section": "compositor", "value": {}},
                       {"expected_revision": old["revision"], "value": {}},
                       {"expected_revision": old["revision"], "section": "compositor", "value": {}, "extra": 1}):
            self.assertEqual(self.call("SetSection", params)["error"], STANDARD + "InvalidParameter")
        current = self.section(old, "preferred_output", {"name": "DP-*", "connector_id": 7})["parameters"]
        cleared = self.call("SetSection", {"expected_revision": current["revision"], "section": "preferred_output"})["parameters"]
        self.assertEqual(cleared["settings"], old["settings"])
        self.assertNotEqual(cleared["revision"], current["revision"])
        self.assertEqual(self.section(cleared, "preferred_output", None)["parameters"], cleared)
        watcher = self.client(); watcher.send("Watch", more=True); watcher.receive()
        before = self.state.read_bytes()
        for failure in ("write", "file-fsync", "rename"):
            self.fault.write_text(failure)
            self.assertEqual(self.section(cleared, "compositor", {"general": {"inner_gap": 7}}),
                             {"error": IFACE + ".PersistenceFailed", "parameters": {}})
            self.assertEqual(self.get(), cleared); self.assertEqual(self.state.read_bytes(), before)
            self.quiet(watcher)
        self.fault.unlink()
        self.state.write_text("external edit")
        self.assertEqual(self.section(cleared, "compositor", {})["error"], IFACE + ".PersistenceFailed")
        self.assertEqual(self.get(), cleared); self.quiet(watcher)

    def test_compositor_ouro_examples_and_lossless_patch_semantics(self):
        process = self.spawn(); old = self.get()
        self.assertEqual(old["settings"]["compositor"], {})  # no daemon defaults
        # From Ouro config.zig tests at 1394e361: rule names, not array order,
        # determine tie-breaking; shorthand argv boundaries must remain intact.
        config = {
            "general": {"focus_follows_mouse": False, "inner_gap": 4, "outer_gap": 8},
            "bindings": {"SUPER+Return": ["run", "/bin/echo", "hello world", ""],
                         "super+x": {"action": ["exit"], "repeat": True}},
            "input_rules": {"z": {"priority": 2, "match": {"type": "touchpad", "name": "Syn*"}, "settings": {"tap": True, "accel_speed": 0.5}},
                            "a": {"priority": 2, "settings": {"tap": "default"}}},
            "output_rules": {"main": {"match": {"name": "DP-?"}, "settings": {"enabled": True,
                "mode": {"width": 1920, "height": 1080}, "position": {"x": -10, "y": 0},
                "scale": 1.25, "icc_profile": "/usr/share/color/icc/main.icc"}}}}
        current = self.section(old, "compositor", config)["parameters"]
        self.assertEqual(current["settings"]["compositor"], config)
        watcher = self.client(); watcher.send("Watch", more=True); watcher.receive()
        reordered = copy.deepcopy(config)
        reordered["input_rules"] = dict(reversed(list(config["input_rules"].items())))
        self.assertEqual(self.section(current, "compositor", reordered)["parameters"], current)
        self.quiet(watcher)
        patches = [
            {"bindings": {"super+q": None, "super+h": {"repeat": None}, "super+x": {"action": ["close"], "repeat": False}},
             "general": {"outer_gap": None},
             "input_rules": {"mouse": {"match": {"name": None}, "settings": {"tap": None, "drag": "default", "scroll_factor": 0, "repeat_delay": "default"}}, "gone": None},
             "output_rules": {"main": {"settings": {"mode": {"refresh_millihertz": None}}}}},
            {"bindings": None, "input_rules": None, "general": None, "output_rules": None},
            {"bindings": {}}, {},
            # Inner validation intentionally belongs to Ouro, including future
            # actions/settings and invalid hardware values. No vocabulary fork.
            {"bindings": {"future+key": ["future-action", "b", "a"]}, "input_rules": {"x": {"settings": {"future_setting": 123}}}},
        ]
        for patch in patches:
            changed = self.section(current, "compositor", patch)["parameters"]
            self.assertNotEqual(changed["revision"], current["revision"])
            self.assertEqual(changed["settings"]["compositor"], patch)
            self.assertEqual(watcher.receive()["parameters"], changed)
            current = changed
        # Raw number lexemes must not be rounded, coerced to strings, or erased.
        raw = b'{"general":{"inner_gap":1e0,"outer_gap":9007199254740993},"input_rules":{"x":{"settings":{"accel_speed":0.12345678901234567890}}}}'
        request = frame("SetSection", {"expected_revision": current["revision"], "section": "compositor", "value": {}}).replace(b'"value": {}', b'"value": ' + raw)
        with Client(self.path) as client:
            client.sock.sendall(request); current = client.receive()["parameters"]
        self.assertEqual(watcher.receive()["parameters"], current)
        disk = self.state.read_bytes()
        for lexeme in (b'1e0', b'9007199254740993', b'0.12345678901234567890'):
            self.assertIn(lexeme, disk)
        # Duplicate nested config keys and excessive raw depth fail closed.
        for value in (b'{"bindings":{"super+x":null,"super+x":["exit"]}}',
                      b'{"general":{"nested":' + b'[' * 70 + b'0' + b']' * 70 + b'}}'):
            with Client(self.path) as client:
                client.sock.sendall(frame("SetSection", {"expected_revision": current["revision"], "section": "compositor", "value": {}}).replace(b'"value": {}', b'"value": ' + value))
                self.assertEqual(client.receive()["error"], STANDARD + "InvalidParameter")
        self.assertEqual(self.state.read_bytes(), disk); self.quiet(watcher)
        process.terminate(); self.assertEqual(process.wait(timeout=3), 0)
        self.spawn(); self.assertEqual(self.get(), current)
        self.assertEqual(self.state.read_bytes(), disk)

    def legacy(self):
        return {"version": 1, "revision": "0123456789abcdef0123456789abcdef", "settings": {
            "appearance": {"color_scheme": "light", "accent": {"r": 0.2, "g": 0.4, "b": 0.8}},
            "preferred_output": {"connector_id": 42},
            "wallpaper": {"default": {"image": "/picture.png", "fit": "tile", "fallback": {"r": 0.1, "g": 0.2, "b": 0.3}},
                          "outputs": [{"match": {"name": "DP-*"}, "wallpaper": {"fit": "center", "fallback": {"r": 1, "g": 0, "b": 0}}}]},
            "outputs": [{"name": "z", "priority": -7, "match": {"name": "DP-*"}, "settings": {"scale": 1.25, "position": {"x": -123, "y": 45}}},
                        {"name": "a", "priority": -7, "match": {}, "settings": {"enabled": False}}],
            "keybindings": [{"trigger": "super+x", "action": ["run", "foot", "two words", ""]},
                            {"trigger": "super+h", "action": ["focus-left"], "repeat": True},
                            {"trigger": "super+q", "action": ["close"], "repeat": False}]}}

    def test_v1_migration_preserves_preferences_and_invalidates_revision(self):
        legacy = self.legacy()
        self.state.parent.mkdir(mode=0o700)
        self.state.write_text(json.dumps(legacy)); self.state.chmod(0o600)
        process = self.spawn(); migrated = self.get()
        expected = copy.deepcopy(legacy["settings"])
        del expected["outputs"]; del expected["keybindings"]
        expected["compositor"] = {"output_rules": {
            "z": {"priority": -7, "match": {"name": "DP-*"}, "settings": {"scale": 1.25, "position": {"x": -123, "y": 45}}},
            "a": {"priority": -7, "match": {}, "settings": {"enabled": False}}},
            "bindings": {"super+x": {"action": ["run", "foot", "two words", ""]},
                         "super+h": {"action": ["focus-left"], "repeat": True},
                         "super+q": {"action": ["close"], "repeat": False}}}
        self.assertEqual(migrated["settings"], expected)
        self.assertNotEqual(migrated["revision"], legacy["revision"])
        self.assertEqual(json.loads(self.state.read_text()), {"version": 2, **migrated})
        self.assertEqual(self.section(legacy, "appearance", {"color_scheme": "dark"})["error"], IFACE + ".Conflict")
        self.assertEqual(self.call("Set", {"expected_revision": legacy["revision"], "settings": expected})["error"], IFACE + ".Conflict")
        watcher = self.client(); watcher.send("Watch", more=True)
        self.assertEqual(watcher.receive()["parameters"], migrated)
        process.terminate(); self.assertEqual(process.wait(timeout=3), 0)
        process = self.spawn(); self.assertEqual(self.get(), migrated)
        process.terminate(); self.assertEqual(process.wait(timeout=3), 0)
        legacy["settings"]["outputs"] = []; legacy["settings"]["keybindings"] = []
        self.state.write_text(json.dumps(legacy)); self.spawn()
        self.assertEqual(self.get()["settings"]["compositor"], {"bindings": {}, "output_rules": {}})

    def test_v1_invalid_and_failed_migration_never_clobbers(self):
        self.state.parent.mkdir(mode=0o700)
        legacy = self.legacy(); cases = []
        for version in (0, 3, 1.0, "1", None):
            cases.append({**legacy, "version": version})
        bad = copy.deepcopy(legacy); bad["settings"]["outputs"].append(bad["settings"]["outputs"][0]); cases.append(bad)
        bad = copy.deepcopy(legacy); bad["settings"]["keybindings"].append({"trigger": "LOGO+X", "action": ["exit"]}); cases.append(bad)
        bad = copy.deepcopy(legacy); bad["settings"]["general"] = {"inner_gap": 7}; cases.append(bad)
        bad = copy.deepcopy(legacy); bad["settings"]["outputs"][0]["priority"] = 1.0; cases.append(bad)
        bad = copy.deepcopy(legacy); bad["revision"] = "bad"; cases.append(bad)
        bad = copy.deepcopy(legacy); del bad["settings"]["keybindings"]; cases.append(bad)
        contents = [json.dumps(v).encode() for v in cases] + [b'{"version":1,"version":1}', b'{"version":1,']
        for content in contents:
            self.state.write_bytes(content); self.state.chmod(0o600)
            failed = self.spawn(wait=False); self.assertEqual(failed.wait(timeout=3), 1)
            self.assertEqual(self.state.read_bytes(), content)
        for failure in ("write", "file-fsync", "rename"):
            content = json.dumps(legacy).encode(); self.state.write_bytes(content)
            self.fault.write_text(failure)
            failed = self.spawn(wait=False); self.assertEqual(failed.wait(timeout=3), 1)
            self.assertEqual(self.state.read_bytes(), content)
            self.assertEqual(list(self.state.parent.glob("*.tmp")), [])
        self.fault.unlink(); self.spawn()
        self.assertEqual(json.loads(self.state.read_text())["version"], 2)

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
        self.assertEqual(json.loads(self.state.read_text()), {"version": 2, **changed})
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
        bad(["appearance", "accent", "r"], 1e309)
        for value in (-1, 2**32, 1.5, 1.0, "42"):
            bad(["preferred_output", "connector_id"], value)
        bad(["compositor"], None)
        bad(["compositor", "general"], [])
        bad(["compositor", "input_rules"], [])
        bad(["compositor", "output_rules"], [])
        bad(["compositor", "bindings"], [])
        bad(["compositor", "unknown"], {})
        bad(["outputs"], [])
        bad(["keybindings"], [])
        bad(["wallpaper", "default", "image"], "https://example.com/image.png")
        bad(["wallpaper", "outputs"], [example["wallpaper"]["outputs"][0]] * 2)
        bad(["preferred_output"], {"make": "invented identity"})
        bad(["appearance"], None)
        bad(["wallpaper", "default", "image"], "/a\0b")
        for data in cases:
            with self.subTest(data=data):
                result = self.call("Set", {"expected_revision": accepted["revision"], "settings": data})
                self.assertEqual(result["error"], STANDARD + "InvalidParameter")
        missing = copy.deepcopy(example); del missing["compositor"]
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
        for content in (b"corrupt", original.replace(b'"version":2', b'"version":99'),
                        original.replace(b'"version":2', b'"version":2,"version":2'), b"null"):
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
        # Establish the subscription before racing writes. A writer may legally
        # disconnect a Watch with its initial frame still pending.
        self.assertEqual(watcher.receive(), {"parameters": current, "continues": True})
        # Now stop reading: later large snapshots fill the Unix buffer.
        committed = []
        for i in range(15):
            current = self.section(current, "appearance", {"color_scheme": "light" if i % 2 == 0 else "dark"})["parameters"]
            committed.append(current)
        self.assertEqual(self.get(), current)
        count = 0
        try:
            while True:
                snapshot = watcher.receive()
                self.assertEqual(snapshot, {"parameters": committed[count], "continues": True})
                count += 1
        except EOFError:
            pass
        self.assertLess(count, len(committed))
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
            self.assertEqual(json.loads(expected.read_text())["version"], 2)
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
