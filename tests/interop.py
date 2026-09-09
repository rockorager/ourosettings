"""Optional independent IDL/client check: uv run --with varlink==31.0.0 ..."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import varlink

root = Path(__file__).resolve().parents[1]
exe = str(Path(sys.argv[1]).resolve())
interface = "dev.rockorager.ouro.Settings"
for path in (root / "protocol").glob("*.varlink"):
    assert varlink.Interface(path.read_text()).name == path.stem
    print("IDL parsed:", path.name)
with tempfile.TemporaryDirectory(prefix="ourosettings-interop-") as temp:
    path = Path(temp) / "settings.sock"
    process = subprocess.Popen([exe, "--socket", str(path), "--state", str(Path(temp) / "settings.json")])
    try:
        for _ in range(200):
            if path.exists():
                break
            assert process.poll() is None
            time.sleep(.01)
        with varlink.Client(address="unix:" + str(path)) as client:
            with client.open(interface) as settings:
                old = settings.Get()
                example = json.loads((root / "examples/settings.json").read_text())["settings"]
                new = settings.Set(expected_revision=old["revision"], settings=example)
                assert new["settings"] == example
                assert new["revision"] != old["revision"]
                assert settings.Get() == new
            with client.open(interface) as settings:
                stream = settings.Watch(_more=True)
                assert next(stream) == new
        print("Independent Varlink client: discovery, Get, Set, Watch passed")
    finally:
        process.terminate()
        assert process.wait(timeout=3) == 0
