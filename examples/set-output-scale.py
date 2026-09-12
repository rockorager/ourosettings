"""Change one output rule through MCP, preserving other settings. No retries."""
import argparse
import copy
import json
import math
import os
from pathlib import Path
import subprocess
import sys


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("rule", help="Existing compositor output rule name")
    parser.add_argument("scale", type=float, help="For example 1.25 or 1.5")
    parser.add_argument("--socket", default=os.path.join(
        os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}"),
        "ouro/settings.mcp.sock"))
    parser.add_argument("--backup", required=True, type=Path,
                        help="New file for the pre-change settings snapshot")
    args = parser.parse_args()
    if not math.isfinite(args.scale) or args.scale <= 0:
        parser.error("scale must be finite and positive")

    def call(method, params):
        result = subprocess.run([
            sys.executable, str(Path(__file__).with_name("mcp.py")),
            args.socket, method, json.dumps(params, allow_nan=False),
        ], capture_output=True, text=True)
        if result.returncode:
            raise RuntimeError(result.stderr or result.stdout or "MCP request failed")
        return json.loads(result.stdout)["result"]

    def read():
        result = call("resources/read", {"uri": "ouro://settings"})
        entry = next(item for item in result["contents"]
                     if item["uri"] == "ouro://settings")
        snapshot = json.loads(entry["text"])
        if not snapshot["exists"]:
            raise RuntimeError("Settings resource is missing")
        return {"revision": snapshot["revision"], "settings": snapshot["value"]}

    before = read()
    desired = copy.deepcopy(before["settings"])
    rule = desired["compositor"]["output_rules"][args.rule]
    old_scale = rule["settings"].get("scale")
    rule["settings"]["scale"] = args.scale
    with os.fdopen(os.open(args.backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as backup:
        json.dump(before, backup, indent=2, allow_nan=False)
        backup.write("\n")
    result = call("tools/call", {
        "name": "settings.set_section",
        "arguments": {
            "expected_revision": before["revision"],
            "section": "compositor",
            "value": desired["compositor"],
        },
    })["structuredContent"]
    if result["settings"] != desired:
        raise RuntimeError("Mutation response differs from requested settings; inspect state before retrying")
    after = read()
    if after != result:
        raise RuntimeError("Settings changed after the write; inspect state before retrying")
    print(f"Stored {args.rule} scale: {old_scale} -> {args.scale}; readback verified.")
    print("Ouro applies this asynchronously; verify its output before assuming success.")


if __name__ == "__main__":
    main()
