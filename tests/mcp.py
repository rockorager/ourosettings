"""Independent MCP 2026-07-28 envelopes against the real Unix daemon."""
import copy
import json
import os
import socket
import subprocess
import sys
import time
import unittest
from urllib.parse import quote

import support
from support import frame, VERSION, META

ROOT = "ouro://settings"
CACHE = {"resultType": "complete", "ttlMs": 0, "cacheScope": "private"}

# Optional second check against the official, downloaded 2026-07-28 schema.
# The default suite remains dependency-free and checks independent expectations.
OFFICIAL = None
if os.environ.get("MCP_OFFICIAL_SCHEMA"):
    import jsonschema
    with open(os.environ["MCP_OFFICIAL_SCHEMA"]) as f:
        OFFICIAL = json.load(f)


def official(message):
    if OFFICIAL is None:
        return
    if "error" in message:
        name = "JSONRPCErrorResponse"
    elif "method" in message:
        name = {"notifications/subscriptions/acknowledged": "SubscriptionsAcknowledgedNotification",
                "notifications/resources/updated": "ResourceUpdatedNotification"}[message["method"]]
    else:
        names = {"supportedVersions": "Discover", "tools": "ListTools", "resources": "ListResources",
                 "resourceTemplates": "ListResourceTemplates", "contents": "ReadResource", "structuredContent": "CallTool"}
        name = next(value + "ResultResponse" for key, value in names.items() if key in message["result"])
    jsonschema.Draft202012Validator({"$ref": "#/$defs/" + name, "$defs": OFFICIAL["$defs"]}).validate(message)


def notification(id, method, **params):
    return {"jsonrpc": "2.0", "method": "notifications/" + method,
            "params": {"_meta": {"io.modelcontextprotocol/subscriptionId": id}, **params}}


class Client(support.Client):
    def receive(self):
        value = super().receive()
        official(value)
        return value


# These expected schemas are independently authored from the settings contract;
# no production schema generator, response capture, or implementation import.
def obj(properties, required):
    return {"type": "object", "additionalProperties": False,
            "properties": properties, "required": required}


def nullable(value):
    return {"anyOf": [value, {"type": "null"}]}


STRING = {"type": "string"}
RGB = obj({k: {"type": "number", "minimum": 0, "maximum": 1} for k in ("r", "g", "b")}, ["r", "g", "b"])
MATCH = obj({"name": nullable({"type": "string", "pattern": "^[^\\u0000]+$"}),
             **{k: nullable({"type": "integer", "minimum": 0, "maximum": 4294967295}) for k in
                ("connector_id", "connector_type", "connector_type_id", "width_mm", "height_mm")}}, [])
WALLPAPER = obj({"image": nullable({"type": "string", "pattern": "^/[^\\u0000]*$"}),
                 "fit": {"type": "string", "enum": ["fill", "fit", "stretch", "center", "tile"]},
                 "fallback": RGB}, ["fit", "fallback"])
SECTIONS = {
    "appearance": obj({"color_scheme": {"type": "string", "enum": ["default", "light", "dark"]},
                       "accent": nullable(RGB)}, ["color_scheme"]),
    "compositor": {"type": "object", "additionalProperties": False, "properties": {
        k: {"type": ["object", "null"]} for k in ("general", "bindings", "input_rules", "output_rules")}},
    "preferred_output": nullable(MATCH),
    "wallpaper": obj({"default": WALLPAPER, "outputs": {"type": "array", "items":
                      obj({"match": MATCH, "wallpaper": WALLPAPER}, ["match", "wallpaper"])}}, ["default", "outputs"])}
SETTINGS = obj(SECTIONS, ["appearance", "compositor", "wallpaper"])
OUTPUT = {"type": "object", "oneOf": [obj({"revision": STRING, "settings": SETTINGS}, ["revision", "settings"]),
    obj({"error": {**obj({"code": {"enum": ["Conflict", "InvalidParameters", "PersistenceFailed"]},
                         "message": STRING, "revision": STRING}, ["code", "message"]),
                    "if": {"properties": {"code": {"const": "Conflict"}}}, "then": {"required": ["revision"]}}}, ["error"])]}
FULL_INPUT = {"$schema": "https://json-schema.org/draft/2020-12/schema",
              **obj({"expected_revision": STRING, "settings": SETTINGS}, ["expected_revision", "settings"])}
SECTION_INPUT = {"$schema": "https://json-schema.org/draft/2020-12/schema",
                 **obj({"expected_revision": STRING, "section": {"enum": list(SECTIONS)}, "value": {}},
                       ["expected_revision", "section"]),
                 "oneOf": [{"properties": {"section": {"const": name}, "value": shape},
                            "required": [] if name == "preferred_output" else ["value"]} for name, shape in SECTIONS.items()]}


class MCPTests(support.DaemonFixture):
    def client(self, path=None):
        client = Client(path or self.path)
        self.clients.append(client)
        return client

    def rpc(self, client, method, params=None, id=1):
        client.send(method, params, id)
        reply = client.receive()
        self.assertEqual(reply.get("id"), id, reply)
        self.assertEqual(reply["jsonrpc"], "2.0")
        return reply

    def read(self, client, path="", id=1):
        uri = ROOT + quote(path, safe="/-._~")
        reply = self.rpc(client, "resources/read", {"uri": uri}, id)
        result = reply["result"]
        self.assertEqual({k: result[k] for k in CACHE}, CACHE)
        self.assertEqual(set(result), {*CACHE, "contents"})
        self.assertEqual(len(result["contents"]), 1)
        content = result["contents"][0]
        self.assertEqual(set(content), {"uri", "mimeType", "text"})
        self.assertEqual((content["uri"], content["mimeType"]), (uri, "application/json"))
        return json.loads(content["text"])

    def listen(self, client, id, paths):
        uris = [ROOT + quote(path, safe="/-._~") for path in paths]
        client.send("subscriptions/listen", {"notifications": {"resourceSubscriptions": uris}}, id)
        self.assertEqual(client.receive(), notification(id, "subscriptions/acknowledged", notifications={"resourceSubscriptions": uris}))

    def tool(self, client, args, name="settings.set_section", id=1):
        return self.rpc(client, "tools/call", {"name": name, "arguments": args}, id)["result"]

    def check_tool(self, result, expected, error=False):
        self.assertEqual(set(result), {"resultType", "isError", "structuredContent", "content"})
        self.assertEqual(result["resultType"], "complete")
        self.assertIs(result["isError"], error)
        self.assertEqual(result["structuredContent"], expected)
        self.assertEqual(len(result["content"]), 1)
        self.assertEqual(result["content"][0]["type"], "text")
        self.assertEqual(json.loads(result["content"][0]["text"]), expected)
        if OFFICIAL is not None:
            jsonschema.Draft202012Validator(OUTPUT).validate(result["structuredContent"])

    def test_exact_discovery_and_schemas(self):
        self.spawn(); c = self.client()
        self.assertEqual(self.rpc(c, "server/discover", id="discover"), {"jsonrpc": "2.0", "id": "discover", "result": {
            **CACHE, "supportedVersions": [VERSION], "capabilities": {"tools": {}, "resources": {"subscribe": True}},
            "_meta": {"io.modelcontextprotocol/serverInfo": {"name": "ourosettings", "version": "0.1.0"}}}})
        listed = self.rpc(c, "tools/list")["result"]
        self.assertEqual({k: listed[k] for k in CACHE}, CACHE)
        self.assertEqual(len(listed["tools"]), 2)
        exported = subprocess.check_output([support.EXE, "--export-mcp-descriptor"], env={}, timeout=3)
        self.assertTrue(exported.endswith(b"\n"))
        self.assertEqual(exported.count(b"\n"), 1)
        self.assertEqual(json.loads(exported), {
            "schema_version": 1, "application_id": "ourosettings",
            "endpoint": {"runtime_path": "ouro/settings.mcp.sock"},
            "tools": listed["tools"],
        })
        for tool, name, shape in zip(listed["tools"], ("settings.set", "settings.set_section"), (FULL_INPUT, SECTION_INPUT)):
            self.assertEqual(set(tool), {"name", "description", "inputSchema", "outputSchema"})
            self.assertEqual(tool["name"], name)
            self.assertIn("Replace", tool["description"])
            self.assertEqual(tool["inputSchema"], shape)
            self.assertEqual(tool["outputSchema"], OUTPUT)
            if OFFICIAL is not None:
                jsonschema.Draft202012Validator.check_schema(tool["inputSchema"])
                jsonschema.Draft202012Validator.check_schema(tool["outputSchema"])
        self.assertEqual(self.rpc(c, "resources/list")["result"], {**CACHE, "resources": [
            {"uri": ROOT + ("/" + name if name != "settings" else ""), "name": name, "mimeType": "application/json"}
            for name in ("settings", "appearance", "compositor", "preferred_output", "wallpaper")]})
        self.assertEqual(self.rpc(c, "resources/templates/list")["result"], {**CACHE, "resourceTemplates": [{
            "uriTemplate": ROOT + "{+pointer}", "name": "settings-pointer", "mimeType": "application/json",
            "description": "A percent-encoded RFC 6901 pointer, empty for all settings. Missing selections have exists false; stored null has exists true."}]})

    def test_multiple_client_writes_noops_and_filtered_notifications(self):
        self.spawn(); c = self.client()
        self.listen(c, "color", ["/appearance/color_scheme"])
        old = self.read(c, "/appearance/color_scheme")
        self.assertEqual((old["exists"], old["value"]), (True, "default"))
        v = self.client()
        self.listen(v, "other", ["/appearance/color_scheme"])
        self.assertEqual(self.read(v, "/appearance/color_scheme")["value"], "default")
        args = {"expected_revision": old["revision"], "section": "appearance", "value": {"color_scheme": "dark"}}
        changed = self.tool(c, args)
        snapshot = self.get()
        self.check_tool(changed, snapshot)
        self.assertNotEqual(snapshot["revision"], old["revision"])
        self.assertEqual(c.receive(), notification("color", "resources/updated", uri=ROOT + "/appearance/color_scheme"))
        self.assertEqual(v.receive(), notification("other", "resources/updated", uri=ROOT + "/appearance/color_scheme"))
        args["expected_revision"] = snapshot["revision"]
        self.check_tool(self.tool(c, args), snapshot)
        self.quiet(c); self.quiet(v)
        unrelated = self.section(snapshot, "compositor", {"general": {"inner_gap": 17}})["structuredContent"]
        self.quiet(c); self.quiet(v)
        light = self.change(unrelated, "light")["structuredContent"]
        self.assertEqual(c.receive(), notification("color", "resources/updated", uri=ROOT + "/appearance/color_scheme"))
        self.assertEqual(v.receive(), notification("other", "resources/updated", uri=ROOT + "/appearance/color_scheme"))
        self.assertEqual(self.read(c, "/appearance/color_scheme"), {"revision": light["revision"], "exists": True, "value": "light"})

    def test_structured_errors_full_set_and_optional_section(self):
        self.spawn(); c = self.client(); old = self.get()
        args = {"expected_revision": old["revision"], "settings": old["settings"]}
        self.check_tool(self.tool(c, args, "settings.set"), old)
        args["expected_revision"] = "stale"
        self.check_tool(self.tool(c, args, "settings.set"), {"error": {"code": "Conflict", "message":
            "Revision conflict; read current settings and reconsider the replacement", "revision": old["revision"]}}, True)
        for bad in ({}, {"section": "appearance", "value": None}, {"section": "compositor", "value": {"bad": {}}},
                    {"section": "appearance", "value": {"color_scheme": "blue"}}, {"section": "wallpaper", "value": {}},
                    {"section": "bogus", "value": {}}, {"section": "preferred_output", "value": {"connector_id": "1"}}):
            self.check_tool(self.tool(c, {"expected_revision": old["revision"], **bad}),
                            {"error": {"code": "InvalidParameters", "message": "Invalid settings replacement"}}, True)
        for value in ({}, None):
            old = self.get()
            result = self.tool(c, {"expected_revision": old["revision"], "section": "preferred_output", "value": value})
            self.check_tool(result, self.get())
        old = self.get()
        self.check_tool(self.tool(c, {"expected_revision": old["revision"], "section": "preferred_output"}), old)
        self.assertFalse(self.read(c, "/preferred_output")["exists"])
        for fault in ("write", "file-fsync", "rename"):
            self.fault.write_text(fault)
            self.check_tool(self.tool(c, {"expected_revision": old["revision"], "section": "appearance", "value": {"color_scheme": "dark"}}),
                            {"error": {"code": "PersistenceFailed", "message": "Could not persist settings"}}, True)
            self.assertEqual(self.get(), old)
        self.fault.unlink()
        self.state.write_text(self.state.read_text() + " ")
        self.check_tool(self.tool(c, {"expected_revision": old["revision"], "settings": old["settings"]}, "settings.set"),
                        {"error": {"code": "PersistenceFailed", "message": "Could not persist settings"}}, True)

    def test_uri_escaping_types_missing_and_raw_numbers(self):
        self.spawn(); c = self.client(); old = self.get()
        data = {"a/b": {"~key": 7}, "%2F": "percent", "~1": "tilde-one", "雪": "☃", "": {"": "empty"},
                "array": [None, False, 0, "", [], {}, "last"], "\0": True, "?": "question", "#": "hash"}
        snapshot = self.section(old, "compositor", {"general": data})["structuredContent"]
        for path, expected in [("/compositor/general/a~1b/~0key", 7), ("/compositor/general/%2F", "percent"),
                ("/compositor/general/~01", "tilde-one"), ("/compositor/general/雪", "☃"), ("/compositor/general//", "empty"),
                ("/compositor/general/\0", True), ("/compositor/general/?", "question"), ("/compositor/general/#", "hash"),
                *[("/compositor/general/array/" + str(i), v) for i, v in enumerate(data["array"])]]:
            self.assertEqual(self.read(c, path), {"revision": snapshot["revision"], "exists": True, "value": expected})
        for path in ("/", "/missing", "/preferred_output", "/compositor/general/array/01", "/compositor/general/array/-", "/compositor/general/array/999", "/compositor/general/array/0/child"):
            self.assertEqual(self.read(c, path), {"revision": snapshot["revision"], "exists": False, "value": None})
        self.assertEqual(self.read(c)["value"], snapshot["settings"])
        for uri in (ROOT + "?q=1", ROOT + "#x", ROOT + "/%", ROOT + "/%GG", ROOT + "/%+F", ROOT + "/%FF", ROOT + "/bad~2", ROOT + "evil", "file:///etc/passwd"):
            self.assertEqual(self.rpc(c, "resources/read", {"uri": uri})["error"]["code"], -32602)
        self.listen(c, "raw", ["/compositor/general/n"])
        for token in ("1", "1.0", "1e0"):
            old = self.get()
            c.sock.sendall(frame("tools/call", {"name": "settings.set_section", "arguments": {
                "expected_revision": old["revision"], "section": "compositor", "value": {"general": {"n": "TOKEN"}}}}).replace(b'"TOKEN"', token.encode()))
            self.assertFalse(c.receive()["result"]["isError"])
            self.assertEqual(c.receive(), notification("raw", "resources/updated", uri=ROOT + "/compositor/general/n"))
            content = self.rpc(c, "resources/read", {"uri": ROOT + "/compositor/general/n"})["result"]["contents"][0]["text"]
            self.assertIn('"value":' + token + '}', content)
        for general, exists in (({"n": None}, True), ({}, False)):
            current = self.section(self.get(), "compositor", {"general": general})["structuredContent"]
            self.assertEqual(c.receive(), notification("raw", "resources/updated", uri=ROOT + "/compositor/general/n"))
            self.assertEqual(self.read(c, "/compositor/general/n"), {"revision": current["revision"], "exists": exists, "value": None})
        self.section(self.get(), "compositor", {"general": {"unrelated": 1}})
        self.quiet(c)

    def test_multiple_subscriptions_cancel_and_coalesced_frames(self):
        self.spawn(); c = self.client()
        c.sock.sendall(frame("subscriptions/listen", {"notifications": {"resourceSubscriptions": [ROOT]}}, "all") +
                       frame("resources/read", {"uri": ROOT}, "read"))
        self.assertEqual(c.receive(), notification("all", "subscriptions/acknowledged", notifications={"resourceSubscriptions": [ROOT]}))
        self.assertEqual(c.receive()["id"], "read")
        self.listen(c, 2, ["/appearance", "/compositor"])
        self.assertEqual(self.rpc(c, "resources/read", {"uri": ROOT}, "all")["error"]["code"], -32600)
        old = self.get(); changed = self.change(old)["structuredContent"]
        received = [c.receive(), c.receive()]
        self.assertCountEqual(received, [notification("all", "resources/updated", uri=ROOT), notification(2, "resources/updated", uri=ROOT + "/appearance")])
        c.sock.sendall(b'{"jsonrpc":"2.0","method":"notifications/cancelled","params":{"requestId":"all"}}\n' + frame("resources/read", {"uri": ROOT}, 3))
        self.assertEqual(c.receive()["id"], 3)
        self.change(changed, "light")
        self.assertEqual(c.receive(), notification(2, "resources/updated", uri=ROOT + "/appearance"))
        self.quiet(c)
        c.sock.sendall(b'{"jsonrpc":"2.0","method":"notifications/cancelled","params":{"requestId":2}}\n')
        self.assertEqual(self.rpc(c, "server/discover", id=4)["id"], 4)
        self.change(self.get(), "default"); self.quiet(c)
        self.listen(c, "all", [""])

    def test_fragmented_malformed_and_version_metadata(self):
        self.spawn(); c = self.client()
        data = frame("server/discover", id="fragment")
        for part in (data[:1], data[1:21], data[21:-1]):
            c.sock.sendall(part); self.quiet(c)
        c.sock.sendall(data[-1:] + frame("tools/list", id=23))
        self.assertEqual(c.receive()["id"], "fragment"); self.assertEqual(c.receive()["id"], 23)
        bad = copy.deepcopy(META); bad["io.modelcontextprotocol/protocolVersion"] = "2025-11-25"
        self.assertEqual(self.rpc(c, "server/discover", {"_meta": bad}, 8), {"jsonrpc": "2.0", "id": 8, "error": {
            "code": -32022, "message": "Unsupported protocol version", "data": {"supported": [VERSION], "requested": "2025-11-25"}}})
        for meta in ({}, {"io.modelcontextprotocol/protocolVersion": VERSION}, {**META, "io.modelcontextprotocol/clientCapabilities": None}):
            self.assertEqual(self.rpc(c, "resources/list", {"_meta": meta})["error"]["code"], -32602)
        for raw, code in ((b'{broken}\n', -32700), (b'[]\n', -32600), (b'null\n', -32600),
                          (b'{"jsonrpc":"2.0","id":null,"method":"server/discover"}\n', -32600),
                          (b'{"jsonrpc":"2.0","jsonrpc":"2.0"}\n', -32700)):
            c.sock.sendall(raw)
            response = c.receive()
            self.assertEqual(response["error"]["code"], code)
            self.assertNotIn("id", response)
        self.assertEqual(self.rpc(c, "tools/call", {"name": "missing", "arguments": {}})["error"]["code"], -32602)
        self.assertEqual(self.rpc(c, "unknown")["error"]["code"], -32601)
        c.sock.sendall(b'{"jsonrpc":"2.0","method":"notifications/unknown"}\n')
        self.quiet(c)
        self.assertEqual(self.rpc(c, "resources/list")["result"]["resultType"], "complete")
        exact = frame("server/discover", id="boundary")
        c.sock.sendall(exact[:-1] + b" " * (256 * 1024 - len(exact)) + b"\n")
        self.assertEqual(c.receive()["id"], "boundary")
        c.sock.sendall(b"x" * (256 * 1024))
        self.assertEqual(c.receive()["error"]["code"], -32600)
        with self.assertRaises(EOFError): c.receive()

    def test_subscription_limits_and_unsupported_filters(self):
        self.spawn(); c = self.client()
        c.send("subscriptions/listen", {"notifications": {"toolsListChanged": True}}, "empty")
        self.assertEqual(c.receive(), notification("empty", "subscriptions/acknowledged", notifications={"resourceSubscriptions": []}))
        for i in range(7): self.listen(c, i, ["/appearance"])
        self.assertEqual(self.rpc(c, "subscriptions/listen", {"notifications": {}}, 99)["error"]["code"], -32602)
        other = self.client()
        self.listen(other, "sixteen", ["/missing/" + str(i) for i in range(16)])
        self.assertEqual(self.rpc(other, "subscriptions/listen", {"notifications": {"resourceSubscriptions": [ROOT]}}, 99)["error"]["code"], -32602)

    def test_bounded_slow_subscriber_disconnect_and_final_invalidation(self):
        self.spawn(); self.fault.write_text("small-send-buffer")
        c = self.client()
        # Long URI makes bounded queue pressure deterministic in a few commits.
        key = "k" * 3900
        path = "/compositor/general/" + key
        self.listen(c, "slow", [path] * 16)
        current = self.get()
        for i in range(25):
            current = self.section(current, "compositor", {"general": {key: i}})["structuredContent"]
        c.sock.settimeout(3)
        complete = 0
        try:
            while True:
                event = c.receive()
                self.assertEqual(event, notification("slow", "resources/updated", uri=ROOT + path))
                complete += 1
        except EOFError:
            pass
        self.assertGreater(complete, 0)
        self.assertLess(complete, 25 * 16)
        fast = self.client(); self.listen(fast, "fresh", [path])
        self.assertEqual(self.read(fast, path)["value"], 24)
        self.section(current, "compositor", {"general": {key: 25}})
        self.assertEqual(fast.receive(), notification("fresh", "resources/updated", uri=ROOT + path))
        self.assertEqual(self.read(fast, path)["value"], 25)

    def test_pending_read_keeps_final_invalidation_and_unrelated_suppression(self):
        self.spawn(); self.fault.write_text("small-send-buffer")
        current = self.section(self.get(), "compositor", {"general": {"value": "x" * 60000}})["structuredContent"]
        c = self.client(); self.listen(c, "pending", ["/compositor/general/value"])
        c.send("resources/read", {"uri": ROOT + "/compositor/general/value"}, "old-read")
        # A normal request/response on another socket is an event-loop barrier.
        # The 60 KiB read cannot fit in this connection's 8 KiB kernel buffer.
        self.get()
        current = self.change(current, "light")["structuredContent"]
        for value in ("first", "final"):
            current = self.section(current, "compositor", {"general": {"value": value}})["structuredContent"]
        old = c.receive()
        self.assertEqual(old["id"], "old-read")
        self.assertEqual(json.loads(old["result"]["contents"][0]["text"])["value"], "x" * 60000)
        for _ in range(2):
            self.assertEqual(c.receive(), notification("pending", "resources/updated", uri=ROOT + "/compositor/general/value"))
        self.quiet(c)
        self.assertEqual(self.read(c, "/compositor/general/value"), {"revision": current["revision"], "exists": True, "value": "final"})

    def test_large_mutation_success_uses_bounded_text_summary(self):
        self.spawn(); c = self.client(); writer = self.client()
        self.listen(c, "small", ["/compositor/general/n"])
        old = self.get()
        # Near-limit valid state; duplicating it into escaped text would exceed
        # the wire cap. The complete structured snapshot must still succeed.
        large = '"\\' * 32500
        writer.send("tools/call", {"name": "settings.set_section", "arguments": {
            "expected_revision": old["revision"], "section": "compositor", "value": {"general": {"large": large, "n": 19}}}})
        response = writer.receive()
        self.assertLessEqual(writer.last_record_size, 256 * 1024)
        self.assertEqual(response["id"], 1)
        self.assertIs(response["result"]["isError"], False)
        self.assertEqual(response["result"]["resultType"], "complete")
        self.assertEqual(c.receive(), notification("small", "resources/updated", uri=ROOT + "/compositor/general/n"))
        current = self.get()
        self.assertNotEqual(current["revision"], old["revision"])
        self.assertEqual(current["settings"]["compositor"]["general"]["large"], large)
        self.assertEqual(response["result"]["structuredContent"], current)
        self.assertEqual(json.loads(response["result"]["content"][0]["text"]), {"revision": current["revision"]})
        self.assertEqual(self.read(c, "/compositor/general/n"), {"revision": current["revision"], "exists": True, "value": 19})
        self.assertEqual(self.read(writer)["value"], current["settings"])

    def test_partial_request_deadline_survives_subscription_output(self):
        self.spawn(idle=300000); c = self.client(); idle = self.client()
        self.listen(c, "partial", ["/appearance"])
        self.listen(idle, "idle", [])
        c.sock.sendall(b'{"jsonrpc":')
        start = time.monotonic()
        current = self.get()
        for i in range(7):
            time.sleep(4)
            current = self.change(current, "dark" if i % 2 == 0 else "light")["structuredContent"]
            self.assertEqual(c.receive(), notification("partial", "resources/updated", uri=ROOT + "/appearance"))
            c.sock.sendall(b" ")
        c.sock.settimeout(5)
        with self.assertRaises(EOFError): c.receive()
        self.assertLess(time.monotonic() - start, 34)
        # A subscription with no partial input remains alive; cancelling it
        # after 30 seconds must not apply an already expired ordinary deadline.
        idle.sock.sendall(b'{"jsonrpc":"2.0","method":"notifications/cancelled","params":{"requestId":"idle"}}\n')
        self.quiet(idle)
        self.assertEqual(self.rpc(idle, "server/discover")["result"]["supportedVersions"], [VERSION])

    def test_documented_example_client(self):
        self.spawn()
        command = [sys.executable, str(support.ROOT / "examples/mcp.py"), str(self.path)]
        response = subprocess.run([*command, "resources/read", '{"uri":"ouro://settings"}'], capture_output=True, check=True, timeout=5)
        selected = json.loads(json.loads(response.stdout)["result"]["contents"][0]["text"])
        args = {"name": "settings.set_section", "arguments": {"expected_revision": selected["revision"], "section": "appearance", "value": {"color_scheme": "dark"}}}
        response = subprocess.run([*command, "tools/call", json.dumps(args)], capture_output=True, check=True, timeout=5)
        self.assertEqual(json.loads(response.stdout)["result"]["structuredContent"], self.get())
        stale = subprocess.run([*command, "tools/call", json.dumps(args)], capture_output=True, timeout=5)
        self.assertEqual(stale.returncode, 1)
        self.assertEqual(json.loads(stale.stdout)["result"]["structuredContent"]["error"]["code"], "Conflict")

    def test_ambiguous_commit_closes_writer_and_subscribers(self):
        process = self.spawn(); c = self.client(); old = self.get()
        self.listen(c, "all", [""])
        v = self.client(); self.listen(v, "other", [""])
        self.fault.write_text("directory-fsync")
        c.send("tools/call", {"name": "settings.set_section", "arguments": {"expected_revision": old["revision"], "section": "appearance", "value": {"color_scheme": "dark"}}})
        with self.assertRaises(EOFError): c.receive()
        with self.assertRaises(EOFError): v.receive()
        self.assertNotEqual(process.wait(timeout=3), 0)
        self.fault.unlink(); self.spawn()
        self.assertEqual(self.get()["settings"]["appearance"]["color_scheme"], "dark")

    def test_activation_queued_request_and_inode_safe_reactivation(self):
        self.path.parent.mkdir(mode=0o700)
        with socket.socket(socket.AF_UNIX) as listener:
            listener.bind(str(self.path)); os.chmod(self.path, 0o600); listener.listen()
            inode = self.path.stat().st_ino
            for _ in range(2):
                first = self.client()
                first.send("resources/read", {"uri": ROOT})
                p = self.spawn(listener=listener, idle=100)
                self.assertTrue(json.loads(first.receive()["result"]["contents"][0]["text"])["exists"])
                first.close()
                self.assertEqual(p.wait(timeout=3), 0, p.communicate()[1])
                self.assertEqual(self.path.stat().st_ino, inode)
                self.assertFalse(self.path.with_name("settings.sock").exists())
            p = self.spawn(listener=listener)
            self.path.unlink(); self.path.write_text("replacement")
            p.terminate(); self.assertEqual(p.wait(timeout=3), 0)
            self.assertEqual(self.path.read_text(), "replacement")

    def test_explicit_mcp_path_and_inode_safe_cleanup(self):
        mcp_path = self.root / "other/custom.sock"
        p = self.spawn(path=mcp_path)
        c = self.client(mcp_path); self.assertTrue(self.read(c)["exists"])
        self.assertEqual(mcp_path.stat().st_mode & 0o777, 0o600)
        self.assertFalse(mcp_path.with_name("settings.sock").exists())
        self.assertFalse(mcp_path.with_name("settings.mcp.sock").exists())
        mcp_path.unlink(); mcp_path.write_text("replacement"); os.chmod(mcp_path, 0o600)
        p.terminate(); self.assertEqual(p.wait(timeout=3), 0)
        self.assertEqual(mcp_path.read_text(), "replacement")


if __name__ == "__main__":
    unittest.main(verbosity=2)
