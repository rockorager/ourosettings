"""Storage, validation, security and lifecycle regressions over real MCP sockets.

Ported from the original daemon suite. Wire framing/discovery, subscription
limits, backpressure and deadlines are exercised separately in mcp.py.
"""
import copy
import json
import os
import resource
import socket
import subprocess
import sys
import time
import unittest
from urllib.parse import quote

from support import Client, DaemonFixture, EXE, ROOT, frame


class DaemonTests(DaemonFixture):
    def success(self, result):
        self.assertIs(result["isError"], False, result)
        self.assertEqual(result["resultType"], "complete")
        return result["structuredContent"]

    def failure(self, result, code, revision=None):
        self.assertIs(result["isError"], True, result)
        error = result["structuredContent"]["error"]
        self.assertEqual(error["code"], code)
        self.assertIsInstance(error["message"], str)
        if revision is not None:
            self.assertEqual(error["revision"], revision)

    def subscribe(self, uri="ouro://settings"):
        client = self.client()
        client.send("subscriptions/listen", {"notifications": {"resourceSubscriptions": [uri]}}, "watch")
        self.assertEqual(client.receive(), {"jsonrpc": "2.0", "method": "notifications/subscriptions/acknowledged",
            "params": {"_meta": {"io.modelcontextprotocol/subscriptionId": "watch"},
                       "notifications": {"resourceSubscriptions": [uri]}}})
        return client

    def event(self, client, uri="ouro://settings"):
        self.assertEqual(client.receive(), {"jsonrpc": "2.0", "method": "notifications/resources/updated",
            "params": {"_meta": {"io.modelcontextprotocol/subscriptionId": "watch"}, "uri": uri}})

    def selected(self, path):
        reply = self.call("resources/read", {"uri": "ouro://settings" + quote(path, safe="/-._~")})
        return json.loads(reply["result"]["contents"][0]["text"])

    def test_section_replacement_conflict_noop_restart(self):
        process = self.spawn()
        example = json.loads((ROOT / "examples/settings.json").read_text())["settings"]
        old = self.success(self.change(self.get(), "light", example))
        process.terminate(); self.assertEqual(process.wait(timeout=3), 0)
        process = self.spawn(); self.assertEqual(self.get(), old)
        watcher = self.subscribe()
        first, second = self.client(), self.client()
        for client, section, value in ((first, "appearance", {"color_scheme": "dark"}), (second, "compositor", {})):
            client.send("tools/call", {"name": "settings.set_section", "arguments": {
                "expected_revision": old["revision"], "section": section, "value": value}})
        results = [first.receive()["result"], second.receive()["result"]]
        successes = [r for r in results if not r["isError"]]
        conflicts = [r for r in results if r["isError"]]
        self.assertEqual(len(successes), 1); self.assertEqual(len(conflicts), 1)
        current = self.success(successes[0])
        self.failure(conflicts[0], "Conflict", current["revision"]); self.event(watcher)
        changed = self.success(self.section(current, "appearance", {"color_scheme": "dark"}))
        if changed != current: self.event(watcher)
        expected = copy.deepcopy(current["settings"])
        expected["appearance"] = {"color_scheme": "dark"}  # accent removed, not merged
        self.assertEqual(changed["settings"], expected)
        current = self.success(self.section(changed, "compositor", example["compositor"]))
        if current != changed: self.event(watcher)
        expected["compositor"] = example["compositor"]
        self.assertEqual(current["settings"], expected)
        changed = self.success(self.section(current, "appearance", example["appearance"]))
        self.event(watcher); self.assertEqual(changed["settings"], example)
        before = self.state.stat().st_mtime_ns
        self.fault.write_text("write")  # no-op must not attempt persistence
        self.assertEqual(self.success(self.section(changed, "appearance", example["appearance"])), changed)
        self.assertEqual(self.state.stat().st_mtime_ns, before); self.quiet(watcher)
        self.fault.unlink()
        process.terminate(); self.assertEqual(process.wait(timeout=3), 0)
        self.spawn(); self.assertEqual(self.get(), changed)

    def test_section_optional_values_and_validation_order(self):
        self.spawn(); old = self.get()
        for section in ("appearance", "compositor", "wallpaper", "outputs", "keybindings", "unknown"):
            for extra in ({}, {"value": None}, {"value": []}):
                result = self.call("tools/call", {"name": "settings.set_section", "arguments": {
                    "expected_revision": "stale", "section": section, **extra}})["result"]
                self.failure(result, "InvalidParameters")  # validate before conflict
        for params in ({"section": "compositor", "value": {}},
                       {"expected_revision": old["revision"], "value": {}},
                       {"expected_revision": old["revision"], "section": "compositor", "value": {}, "extra": 1}):
            self.failure(self.call("tools/call", {"name": "settings.set_section", "arguments": params})["result"], "InvalidParameters")
        current = self.success(self.section(old, "preferred_output", {"name": "DP-*", "connector_id": 7}))
        cleared = self.success(self.call("tools/call", {"name": "settings.set_section", "arguments": {
            "expected_revision": current["revision"], "section": "preferred_output"}})["result"])
        self.assertEqual(cleared["settings"], old["settings"])
        self.assertNotEqual(cleared["revision"], current["revision"])
        self.assertEqual(self.success(self.section(cleared, "preferred_output", None)), cleared)

    def test_compositor_lossless_patch_semantics_and_restart(self):
        process = self.spawn(); old = self.get()
        self.assertEqual(old["settings"]["compositor"], {})
        config = {
            "general": {"focus_follows_mouse": False, "inner_gap": 4, "outer_gap": 8},
            "bindings": {"SUPER+Return": ["run", "/bin/echo", "hello world", ""],
                         "super+x": {"action": ["exit"], "repeat": True}},
            "input_rules": {"z": {"priority": 2, "match": {"type": "touchpad", "name": "Syn*"}, "settings": {"tap": True, "accel_speed": 0.5}},
                            "a": {"priority": 2, "settings": {"tap": "default"}}},
            "output_rules": {"main": {"match": {"name": "DP-?"}, "settings": {"enabled": True,
                "mode": {"width": 1920, "height": 1080}, "position": {"x": -10, "y": 0},
                "scale": 1.25, "icc_profile": "/usr/share/color/icc/main.icc"}}}}
        current = self.success(self.section(old, "compositor", config))
        self.assertEqual(current["settings"]["compositor"], config)
        watcher = self.subscribe()
        reordered = copy.deepcopy(config)
        reordered["input_rules"] = dict(reversed(list(config["input_rules"].items())))
        self.assertEqual(self.success(self.section(current, "compositor", reordered)), current)
        self.quiet(watcher)
        patches = [
            {"bindings": {"super+q": None, "super+h": {"repeat": None}, "super+x": {"action": ["close"], "repeat": False}},
             "general": {"outer_gap": None},
             "input_rules": {"mouse": {"match": {"name": None}, "settings": {"tap": None, "drag": "default", "scroll_factor": 0, "repeat_delay": "default"}}, "gone": None},
             "output_rules": {"main": {"settings": {"mode": {"refresh_millihertz": None}}}}},
            {"bindings": None, "input_rules": None, "general": None, "output_rules": None},
            {"bindings": {}}, {},
            {"bindings": {"future+key": ["future-action", "b", "a"]}, "input_rules": {"x": {"settings": {"future_setting": 123}}}},
        ]
        for patch in patches:
            changed = self.success(self.section(current, "compositor", patch))
            self.assertNotEqual(changed["revision"], current["revision"])
            self.assertEqual(changed["settings"]["compositor"], patch)
            self.event(watcher); current = changed
        raw = b'{"general":{"inner_gap":1e0,"outer_gap":9007199254740993},"input_rules":{"x":{"settings":{"accel_speed":0.12345678901234567890}}}}'
        request = frame("tools/call", {"name": "settings.set_section", "arguments": {
            "expected_revision": current["revision"], "section": "compositor", "value": "TOKEN"}}).replace(b'"TOKEN"', raw)
        with Client(self.path) as client:
            client.sock.sendall(request); current = self.success(client.receive()["result"])
        self.event(watcher); disk = self.state.read_bytes()
        for lexeme in (b'1e0', b'9007199254740993', b'0.12345678901234567890'):
            self.assertIn(lexeme, disk)
        for value, code in ((b'{"bindings":{"super+x":null,"super+x":["exit"]}}', -32700),
                            (b'{"general":{"nested":' + b'[' * 70 + b'0' + b']' * 70 + b'}}', -32600)):
            with Client(self.path) as client:
                client.sock.sendall(frame("tools/call", {"name": "settings.set_section", "arguments": {
                    "expected_revision": current["revision"], "section": "compositor", "value": "TOKEN"}}).replace(b'"TOKEN"', value))
                self.assertEqual(client.receive()["error"]["code"], code)
        self.assertEqual(self.state.read_bytes(), disk); self.quiet(watcher)
        process.terminate(); self.assertEqual(process.wait(timeout=3), 0)
        self.spawn(); self.assertEqual(self.get(), current); self.assertEqual(self.state.read_bytes(), disk)

    def legacy_state(self):
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
        legacy = self.legacy_state()
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
        self.failure(self.section(legacy, "appearance", {"color_scheme": "dark"}), "Conflict", migrated["revision"])
        self.failure(self.change(legacy, settings=expected), "Conflict", migrated["revision"])
        process.terminate(); self.assertEqual(process.wait(timeout=3), 0)
        process = self.spawn(); self.assertEqual(self.get(), migrated)
        process.terminate(); self.assertEqual(process.wait(timeout=3), 0)
        legacy["settings"]["outputs"] = []; legacy["settings"]["keybindings"] = []
        self.state.write_text(json.dumps(legacy)); self.spawn()
        self.assertEqual(self.get()["settings"]["compositor"], {"bindings": {}, "output_rules": {}})

    def test_v1_invalid_and_failed_migration_never_clobbers(self):
        self.state.parent.mkdir(mode=0o700)
        legacy = self.legacy_state(); cases = []
        for version in (0, 3, 1.0, "1", None): cases.append({**legacy, "version": version})
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

    def test_full_replacement_stale_writer_noop_restart_permissions(self):
        process = self.spawn(); old = self.get(); watcher = self.subscribe()
        changed = self.success(self.change(old))
        self.assertNotEqual(changed["revision"], old["revision"]); self.event(watcher)
        self.failure(self.change(old, "light"), "Conflict", changed["revision"])
        self.assertEqual(self.success(self.change(changed)), changed); self.quiet(watcher)
        process.terminate(); self.assertEqual(process.wait(timeout=3), 0)
        self.assertFalse(self.path.exists()); self.spawn(); self.assertEqual(self.get(), changed)
        self.assertEqual(json.loads(self.state.read_text()), {"version": 2, **changed})
        for path, mode in ((self.state, 0o600), (self.path, 0o600), (self.state.parent, 0o700)):
            self.assertEqual(path.stat().st_mode & 0o777, mode)

    def test_schema_realistic_snapshot_and_rejections(self):
        self.spawn(); old = self.get()
        example = json.loads((ROOT / "examples/settings.json").read_text())["settings"]
        accepted = self.success(self.change(old, "light", example))
        self.assertEqual(accepted["settings"], example)
        cases = []
        def bad(path, value):
            data = copy.deepcopy(example); target = data
            for key in path[:-1]: target = target[key]
            target[path[-1]] = value; cases.append(data)
        for channel, value in (("r", 1.01), ("g", -0.1), ("b", "0.5")): bad(["appearance", "accent", channel], value)
        bad(["appearance", "color_scheme"], "auto")
        for value in (-1, 2**32, 1.5, 1.0, "42"): bad(["preferred_output", "connector_id"], value)
        bad(["compositor"], None)
        for key in ("general", "input_rules", "output_rules", "bindings"): bad(["compositor", key], [])
        bad(["compositor", "unknown"], {}); bad(["outputs"], []); bad(["keybindings"], [])
        bad(["wallpaper", "default", "image"], "https://example.com/image.png")
        bad(["wallpaper", "outputs"], [example["wallpaper"]["outputs"][0]] * 2)
        bad(["preferred_output"], {"make": "invented identity"})
        bad(["appearance"], None); bad(["wallpaper", "default", "image"], "/a\0b")
        missing = copy.deepcopy(example); del missing["compositor"]; cases.append(missing)
        for data in cases:
            with self.subTest(data=data):
                result = self.call("tools/call", {"name": "settings.set", "arguments": {
                    "expected_revision": accepted["revision"], "settings": data}})["result"]
                self.failure(result, "InvalidParameters")
        for raw in (b'0.2,"r":0.3', b'Infinity'):
            request = frame("tools/call", {"name": "settings.set", "arguments": {
                "expected_revision": accepted["revision"], "settings": example}}).replace(b'"r":0.2', b'"r":' + raw)
            with Client(self.path) as client:
                client.sock.sendall(request); self.assertEqual(client.receive()["error"]["code"], -32700)
        self.assertEqual(self.get(), accepted)

    def test_disk_failures_do_not_publish(self):
        process = self.spawn(); old = self.get(); before = self.state.read_bytes(); watcher = self.subscribe()
        for failure in ("write", "file-fsync", "rename"):
            self.fault.write_text(failure)
            self.failure(self.change(old), "PersistenceFailed")
            self.failure(self.section(old, "compositor", {"general": {"inner_gap": 7}}), "PersistenceFailed")
            self.assertEqual(self.get(), old); self.assertEqual(self.state.read_bytes(), before)
            self.assertEqual(list(self.state.parent.glob("*.tmp")), []); self.quiet(watcher)
        self.fault.unlink()
        soft, hard = resource.prlimit(process.pid, resource.RLIMIT_FSIZE)
        resource.prlimit(process.pid, resource.RLIMIT_FSIZE, (64, hard))
        self.failure(self.change(old), "PersistenceFailed")
        resource.prlimit(process.pid, resource.RLIMIT_FSIZE, (soft, hard))
        self.assertEqual(self.state.read_bytes(), before); self.quiet(watcher)
        os.chmod(self.state.parent, 0o500)
        try:
            self.failure(self.change(old), "PersistenceFailed")
            self.assertEqual(self.get(), old); self.quiet(watcher)
        finally:
            os.chmod(self.state.parent, 0o700)
        self.assertNotEqual(self.success(self.change(old))["revision"], old["revision"]); self.event(watcher)

    def test_post_rename_failure_is_ambiguous_and_restart_recovers(self):
        process = self.spawn(); old = self.get(); watcher = self.subscribe()
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
        process = self.spawn(); old = self.get(); original = self.state.read_bytes(); watcher = self.subscribe()
        self.state.write_text("corrupt")
        self.failure(self.change(old), "PersistenceFailed")
        self.failure(self.section(old, "compositor", {}), "PersistenceFailed")
        self.assertEqual(self.get(), old); self.quiet(watcher)
        process.terminate(); process.wait(timeout=3)
        for content in (b"corrupt", original.replace(b'"version":2', b'"version":99'),
                        original.replace(b'"version":2', b'"version":2,"version":2'), b"null"):
            self.state.write_bytes(content)
            failed = self.spawn(wait=False); self.assertEqual(failed.wait(timeout=3), 1)
            self.assertEqual(self.state.read_bytes(), content); self.assertFalse(self.path.exists())
        self.state.write_bytes(original); self.spawn(); self.assertEqual(self.get(), old)

    def test_singleton_and_socket_safety(self):
        process = self.spawn(); before = self.state.read_bytes()
        for path in (self.path, self.root / "other/custom.sock"):
            second = self.spawn(wait=False, path=path); self.assertEqual(second.wait(timeout=3), 1)
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
        process = self.spawn(); process.terminate(); process.wait(timeout=3); before = self.state.read_bytes()
        os.chmod(self.state, 0o644)
        failed = self.spawn(wait=False); self.assertEqual(failed.wait(timeout=3), 1)
        self.assertEqual(self.state.stat().st_mode & 0o777, 0o644); os.chmod(self.state, 0o600)
        os.chmod(self.path.parent, 0o755)
        failed = self.spawn(wait=False); self.assertEqual(failed.wait(timeout=3), 1); os.chmod(self.path.parent, 0o700)
        saved = self.root / "saved"; self.state.rename(saved); self.state.symlink_to(saved)
        failed = self.spawn(wait=False); self.assertEqual(failed.wait(timeout=3), 1)
        self.assertTrue(self.state.is_symlink()); self.assertEqual(saved.read_bytes(), before)
        self.state.unlink(); saved.rename(self.state)
        link = self.root / "linked"; link.symlink_to(self.state.parent)
        failed = self.spawn(wait=False, state=link / "settings.json"); self.assertEqual(failed.wait(timeout=3), 1)
        lock = self.state.with_suffix(".json.lock"); lock.unlink(); lock.symlink_to(self.state)
        failed = self.spawn(wait=False); self.assertEqual(failed.wait(timeout=3), 1)
        self.assertEqual(self.state.read_bytes(), before)

    def test_activation_idle_subscription_reactivation(self):
        self.path.parent.mkdir(mode=0o700)
        with socket.socket(socket.AF_UNIX) as listener:
            listener.bind(str(self.path)); os.chmod(self.path, 0o600); listener.listen(32)
            process = self.spawn(listener=listener, idle=150); old = self.get(); watcher = self.subscribe()
            time.sleep(.3); self.assertIsNone(process.poll())
            watcher.close(); self.assertEqual(process.wait(timeout=3), 0); self.assertTrue(self.path.exists())
            client = self.client(); client.send()
            self.spawn(listener=listener, idle=150)
            self.assertEqual(json.loads(client.receive()["result"]["contents"][0]["text"]),
                             {"revision": old["revision"], "exists": True, "value": old["settings"]})

    def test_invalid_activation(self):
        for env in ({"LISTEN_FDS": "1"}, {"LISTEN_PID": "1"}, {"LISTEN_FDS": "2", "LISTEN_PID": "1"}):
            failed = self.spawn(wait=False, env=env); self.assertEqual(failed.wait(timeout=3), 1)
        # A claimed but absent fd 3 must not be replaced by a directory/lock fd.
        failed = subprocess.run([sys.executable, "-c",
            "import os,sys; os.closerange(3,256); os.environ.update(LISTEN_PID=str(os.getpid()),LISTEN_FDS='1'); os.execv(sys.argv[1],sys.argv[1:])",
            EXE, "--socket", str(self.path), "--state", str(self.state)], capture_output=True, timeout=3)
        self.assertEqual(failed.returncode, 1); self.assertIn(b"InvalidActivation", failed.stderr)
        self.assertFalse(self.path.exists()); self.assertFalse(self.state.exists())
        self.path.parent.mkdir(mode=0o700, exist_ok=True)
        with socket.socket(socket.AF_UNIX) as listener:
            listener.bind(str(self.path)); os.chmod(self.path, 0o600)
            failed = self.spawn(wait=False, listener=listener); self.assertEqual(failed.wait(timeout=3), 1)
            listener.listen(32)
            for count in ("0", "2", "3", "garbage"):
                failed = self.spawn(wait=False, listener=listener, env={"LISTEN_FDS": count})
                self.assertEqual(failed.wait(timeout=3), 1)
            failed = self.spawn(wait=False, listener=listener, path=self.path.with_name("wrong.sock"))
            self.assertEqual(failed.wait(timeout=3), 1)
            os.chmod(self.path, 0o666)
            failed = self.spawn(wait=False, listener=listener); self.assertEqual(failed.wait(timeout=3), 1)

    def test_state_and_client_bounds(self):
        self.spawn(); old = self.get()
        settings = copy.deepcopy(old["settings"])
        settings["wallpaper"]["default"]["image"] = "/" + "a" * 90000
        current = self.success(self.change(old, settings=settings))
        watcher = self.subscribe()
        settings["wallpaper"]["default"]["image"] = "/" + "b" * 140000
        self.failure(self.change(current, settings=settings), "InvalidParameters")
        self.assertEqual(self.get(), current); self.quiet(watcher); watcher.close()
        held = [self.client() for _ in range(32)]; time.sleep(.1)
        with Client(self.path) as excess:
            try:
                excess.send(); excess.receive(); self.fail("33rd client accepted")
            except (EOFError, ConnectionResetError, BrokenPipeError): pass
        for client in held: client.close()

    def test_nullable_fields_and_recreated_state_token(self):
        process = self.spawn(); old = self.get(); settings = copy.deepcopy(old["settings"])
        settings["appearance"]["accent"] = None; settings["preferred_output"] = None
        settings["wallpaper"]["default"]["image"] = None
        self.assertEqual(self.success(self.change(old, "default", settings)), old)
        process.terminate(); process.wait(timeout=3); self.state.unlink(); self.spawn()
        recreated = self.get()
        self.assertNotEqual(recreated["revision"], old["revision"])
        self.assertEqual(recreated["settings"], old["settings"])
        self.failure(self.change(old), "Conflict", recreated["revision"])

    def test_xdg_defaults_home_fallback_single_socket_and_obsolete_flag(self):
        runtime = self.root / "runtime"; runtime.mkdir(mode=0o700)
        config = self.root / "config"; home = self.root / "home"; home.mkdir(mode=0o700)
        path = runtime / "ouro/settings.mcp.sock"
        for config_env, expected in ((str(config), config / "ouro/settings.json"), ("", home / ".config/ouro/settings.json")):
            env = {k: v for k, v in os.environ.items() if not k.startswith("LISTEN_")}
            env.update(XDG_RUNTIME_DIR=str(runtime), XDG_CONFIG_HOME=config_env, HOME=str(home))
            process = subprocess.Popen([EXE, "--idle-ms", "500"], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            self.processes.append(process)
            for _ in range(200):
                if path.exists(): break
                if process.poll() is not None: self.fail(process.communicate()[1])
                time.sleep(.01)
            with Client(path) as client:
                client.send(); self.assertIn("result", client.receive())
                self.assertFalse((path.parent / "settings.sock").exists())
                self.assertEqual([p.name for p in path.parent.iterdir() if p.is_socket()], ["settings.mcp.sock"])
            self.assertEqual(process.wait(timeout=3), 0, process.communicate()[1])
            self.assertEqual(json.loads(expected.read_text())["version"], 2); self.assertFalse(path.exists())
        for args in (["--idle-ms", "0"], ["--idle-ms", "300001"], ["--state", "relative"],
                     ["--unknown"], ["--mcp-socket", str(self.path)]):
            result = subprocess.run([EXE, "--socket", str(self.path), *args], env=env, capture_output=True, timeout=3)
            self.assertNotEqual(result.returncode, 0)
        result = subprocess.run([EXE, "--mcp-socket", str(self.path)], env=env, capture_output=True, timeout=3)
        self.assertEqual(result.returncode, 1); self.assertIn(b"UnknownOption", result.stderr)
        self.assertFalse(self.path.exists()); self.assertFalse(path.exists())

    def test_foreign_owned_state_and_socket_retained(self):
        if os.geteuid() == 0 or subprocess.run(["sudo", "-n", "true"], capture_output=True).returncode:
            self.skipTest("requires passwordless sudo to construct foreign-owned fixtures")
        process = self.spawn(); process.terminate(); process.wait(timeout=3); before = self.state.read_bytes()
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
            "import socket,sys; s=socket.socket(socket.AF_UNIX); s.settimeout(2); s.connect(sys.argv[1]); assert s.recv(4096)==b''",
            str(self.path)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr); self.get()

    def test_pointer_types_array_indices_and_ancestor_replacement(self):
        self.spawn(); old = self.get()
        data = {"": {"": "empty"}, "a/b": {"~key": 7}, "~1": "literal tilde-one", "/": "slash", "雪": "☃",
                "é": 1, "e\u0301": 2, "\0": False, "array": [None, False, 0, "", [], {}, "last"],
                "0": "zero key", "01": "leading zero key", "-": "dash key", "*": "literal star", "%2F": "not decoded", "plain": "scalar"}
        current = self.success(self.section(old, "compositor", {"general": data}))
        for key, expected in data.items():
            path = "/compositor/general/" + key.replace("~", "~0").replace("/", "~1")
            value = self.selected(path)
            self.assertIs(value["exists"], True); self.assertIs(type(value["value"]), type(expected)); self.assertEqual(value["value"], expected)
        for index in ("7", "-", "00", "01", "-1", "+1", "1.0", "1e0", " 1", "١", "", "9" * 100):
            self.assertEqual(self.selected("/compositor/general/array/" + index),
                             {"revision": current["revision"], "exists": False, "value": None})
        uri = "ouro://settings/compositor/output_rules/internal/settings/enabled"; watcher = self.subscribe(uri)
        for value in (None, False, 0, [], {}, "", True):
            current = self.success(self.section(current, "compositor", {"output_rules": {"internal": {"settings": {"enabled": value}}}}))
            self.event(watcher, uri)
            selected = self.selected(uri[len("ouro://settings"):])
            self.assertIs(type(selected["value"]), type(value)); self.assertEqual(selected["value"], value); self.assertTrue(selected["exists"])
            # Ancestor replacement that retains the selected value stays quiet.
            current = self.success(self.section(current, "compositor", {"output_rules": {
                "internal-extra": {}, "internal": {"priority": 7, "settings": {"enabled": value, "scale": 2}}}}))
            self.quiet(watcher)
        current = self.success(self.section(current, "compositor", {})); self.event(watcher, uri)
        self.assertFalse(self.selected(uri[len("ouro://settings"):])["exists"])

    def test_canonical_objects_and_array_replacement_notifications(self):
        self.spawn(); current = self.get()
        path = "/compositor/general/value"; uri = "ouro://settings" + path
        selected = self.subscribe(uri); item = self.subscribe(uri + "/1")
        for raw in ('{"z":false,"a":null}', '{"a":null,"z":false}', '["first",false]', '[false,"first"]', '[]'):
            previous = current
            request = frame("tools/call", {"name": "settings.set_section", "arguments": {
                "expected_revision": current["revision"], "section": "compositor", "value": {"general": {"value": "TOKEN"}}}})
            with Client(self.path) as writer:
                writer.sock.sendall(request.replace(b'"TOKEN"', raw.encode()))
                current = self.success(writer.receive()["result"])
            if raw == '{"a":null,"z":false}':
                self.assertEqual(current["revision"], previous["revision"]); self.quiet(selected)
            else:
                self.assertNotEqual(current["revision"], previous["revision"]); self.event(selected, uri)
            self.assertEqual(self.selected(path)["value"], json.loads(raw))
            if raw.startswith('['):
                self.event(item, uri + "/1")
                expected = json.loads(raw)
                self.assertEqual(self.selected(path + "/1"), {"revision": current["revision"],
                    "exists": len(expected) > 1, "value": expected[1] if expected else None})
            else: self.quiet(item)
        changed = self.success(self.section(current, "compositor", {"general": {"value": [], "other": True}}))
        self.assertNotEqual(changed["revision"], current["revision"])
        self.quiet(selected); self.quiet(item)


if __name__ == "__main__":
    unittest.main(verbosity=2)
