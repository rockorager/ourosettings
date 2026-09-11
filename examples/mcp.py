"""One MCP request on an explicit Unix socket. No mutation retries."""
import argparse
import json
import socket
import sys

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("socket", help="Absolute settings.mcp.sock path")
parser.add_argument("method", help="For example resources/read or tools/call")
parser.add_argument("params", nargs="?", default="{}", help="JSON object")
args = parser.parse_args()
params = json.loads(args.params)
params["_meta"] = {
    "io.modelcontextprotocol/protocolVersion": "2026-07-28",
    "io.modelcontextprotocol/clientCapabilities": {},
    "io.modelcontextprotocol/clientInfo": {"name": "ourosettings-example", "version": "1"},
}
request = json.dumps({"jsonrpc": "2.0", "id": 1, "method": args.method, "params": params}).encode() + b"\n"
if len(request) > 4 * 1024 * 1024:
    parser.error("request exceeds 4 MiB")

with socket.socket(socket.AF_UNIX) as connection:
    connection.settimeout(None if args.method == "subscriptions/listen" else 5)
    connection.connect(args.socket)
    connection.sendall(request)
    try:
        with connection.makefile("rb") as stream:
            while True:
                line = stream.readline(4 * 1024 * 1024 + 1)
                if not line or not line.endswith(b"\n") or len(line) > 4 * 1024 * 1024:
                    sys.exit("Incomplete response; a mutation may have committed. Read current settings before deciding what to retry.")
                reply = json.loads(line)
                print(json.dumps(reply, ensure_ascii=False), flush=True)
                if reply.get("id") == 1 or "error" in reply:
                    result = reply.get("result", {})
                    sys.exit(1 if "error" in reply or result.get("isError") or result.get("resultType", "complete") != "complete" else 0)
    except KeyboardInterrupt:
        connection.sendall(b'{"jsonrpc":"2.0","method":"notifications/cancelled","params":{"requestId":1}}\n')
