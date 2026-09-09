# ourosettings

A Linux per-user **desired settings store**, written in Zig 0.16.0 with
`std.json` and libc/POSIX. No GUI, compositor, image decoder, database, or runtime
package dependencies beyond libc.

**These settings do not affect Ouro yet.** Ouro currently reads its own
`config.json` / `config.d` snapshots and reloads on SIGHUP; it has no settings
Varlink client. This daemon never writes that config or signals Ouro. A future
compositor consumer must apply desired preferences and own/report live state.
Successful persistence does not prove that a key, action, image, ICC profile,
device, or mode works. No migration or merge with existing Ouro configuration is
performed. Input rules and general/layout settings are deferred.

Native control uses Varlink (`dev.rockorager.ouro.Settings`). D-Bus/portal
translation belongs in the separate ourobridge; Wayland and PipeWire remain
their respective protocols. Keybindings here are user configuration, **not**
portal GlobalShortcuts registration, permission policy, or application ownership.

## Build and isolated execution

```sh
# Debian orb toolchain bootstrap, if needed (checksum-pinned Zig 0.16.0):
.agents/setup
zig build
zig build test
zig-out/bin/ourosettings --help

# Foreground process in a separate terminal, without touching desktop config:
testdir=$(mktemp -d)
zig-out/bin/ourosettings --socket "$testdir/settings.sock" \
  --state "$testdir/settings.json" --idle-ms 300000
```

The explicit paths must be absolute. Missing directories are created with mode
0700; final state/socket directories must be effective-UID-owned and exactly
0700. Every directory component is opened without following symlinks; `.` and
`..` components are rejected. No permissions or credentials bypass is available.
`--idle-ms` accepts 1…300000 (default 30000). The process exits after that interval
with no clients. An active Watch holds it alive. SIGINT/SIGTERM close clients and
clean up a directly bound socket, only if its device/inode still matches.

Default paths:

* `$XDG_RUNTIME_DIR/ouro/settings.sock` (runtime directory required, private).
* `$XDG_CONFIG_HOME/ouro/settings.json`; empty/unset config home falls back to
  `$HOME/.config/ouro/settings.json`. No access to compositor `config.json`.

The intended deployment is one desktop per UID. Both the socket and state path
have private adjacent `.lock` files held with `flock` for the daemon's lifetime.
Explicit paths permit isolated instances; they do not identify extra desktops.
Do not remove lock files while a daemon runs. The build installs only into
`zig-out`; it does not install a desktop service. `.agents/setup` is committed
locally for reproduction; with no remote/default-branch publication it is not
automatically available to future project orbs.

## API and clients

The exact IDLs are in `protocol/`, also returned by
`org.varlink.service.GetInterfaceDescription`. Discovery lists both interfaces.
All replies contain `parameters`, including `{}` for parameterless errors.

* **Get** returns `{revision, settings}` as one consistent complete snapshot.
* **Set** takes `{expected_revision, settings}` and replaces the full snapshot.
  Validation and persistence precede publication. A mismatched token returns
  `dev.rockorager.ouro.Settings.Conflict` with the current `revision`; refetch and
  deliberately merge/retry, rather than blindly replaying a stale replacement.
* **Watch** requires `more: true` (otherwise standard `ExpectedMore`). It sends
  the current complete snapshot immediately and each later committed snapshot,
  all with `continues: true`. Subscribe directly: a preceding Get is unnecessary.
  Requests and commits execute in one event loop, so subscription has no
  Get/subscribe race. EOF means resubscribe and accept the new initial snapshot;
  this is not an audit log or replay protocol.

Revisions are opaque, random 128-bit lowercase hex tokens, persisted with state.
They survive restart and differ after state recreation; they are not counters
or authorization secrets. Restoring an old backup restores its token: offline
restoration must be treated as a new administrative session, with clients
reconnected. A no-op is equality of the typed serialized snapshot (optional
nulls omitted, object key order normalized, array order retained): it preserves
revision, performs no write, and emits no notification. Explicit `repeat: false`
and unspecified repeat remain distinct stored preferences, though both mean no
repeat to a consumer. External edits are checked before even a no-op Set.

With systemd's `varlinkctl` installed (255+ for basic calls, 257+ for the infinite
Watch timeout), replace `address` for an isolated instance if desired:

```sh
address="unix:$XDG_RUNTIME_DIR/ouro/settings.sock"
interface=dev.rockorager.ouro.Settings
varlinkctl info "$address"
varlinkctl introspect "$address" "$interface"
varlinkctl call "$address" "$interface.Get" '{}'

# Read, change one preference locally, then replace using the read token.
# jq is only an example client tool, not a daemon dependency.
snapshot=$(varlinkctl call "$address" "$interface.Get" '{}')
printf '%s' "$snapshot" | jq '{expected_revision: .revision,
  settings: (.settings | .appearance.color_scheme = "dark")}' |
  varlinkctl call "$address" "$interface.Set"

varlinkctl --more --timeout=infinity call "$address" "$interface.Watch" '{}'
varlinkctl validate-idl protocol/dev.rockorager.ouro.Settings.varlink
```

## Version 1 schema and validation boundary

`examples/settings.json` illustrates the persisted envelope
`{version: 1, revision, settings}`. Its token and paths are illustrative, not a
file to copy over a running daemon. Use only its `settings` object in a Set,
with a fresh revision from Get. Initial defaults: default color scheme, no
accent/output rules/preferred output/keybindings, black fallback wallpaper with
fill fit and no image/per-output entries.

All top-level settings sections and their non-optional fields are required.
Unknown fields and duplicate JSON keys (at every depth) are rejected, not
silently ignored. Optional fields may be absent or null, meaning unspecified;
responses omit them. Required fields do not accept null. Numeric strings and
non-integer JSON tokens in integer fields (including `1.0` and `1e0`) are
rejected. Arrays are replacements, not merge patches.

* **Appearance:** `color_scheme` is `default`, `light`, or `dark`; optional RGB
  accent has required finite `r`, `g`, `b` components in [0, 1].
* **Outputs:** `outputs` is an array of `{name, priority, match, settings}`.
  Rule names must be nonempty and unique; priority is i32. The schema preserves
  Ouro's rule identity, not a new make/model/serial/connector identity system.
  Consumers apply all matching rules sorted by ascending `(priority, name)`,
  later specified fields winning. Array order is stored but is not precedence.
  OutputMatch fields are conjunctive: optional byte-glob `name` (`*`/`?`, e.g.
  `DRM-*`), u32 `connector_id`, `connector_type`, `connector_type_id`, `width_mm`,
  `height_mm`. `{}` matches all outputs. Overlap between different rules is
  intentional and allowed. `enabled`, `mode`, `position`, `scale`, `icc_profile`
  are optional. Mode requires positive u32 width/height, optional u32 refresh
  in millihertz (0 is not rejected here). Position uses i32 x/y. Scale must be
  finite and positive; compositor scale quantization and representability are
  consumer responsibilities. ICC profile is an absolute, NUL-free path; no
  profile is loaded. `preferred_output` is a separate optional OutputMatch,
  not a live connected/primary-output result. Selection among multiple matches
  is a compositor decision.
* **Wallpaper:** required `default` preference and `outputs` array of
  `{match, wallpaper}` entries. Matching uses OutputMatch above. Per-output
  entries replace the whole wallpaper preference; if several match, the last
  array entry wins. Identical selectors are rejected to avoid shadowed entries;
  overlapping selectors are allowed. Each preference has optional `image`,
  required fit (`fill`, `fit`, `stretch`, `center`, `tile`) and fallback RGB.
  Image policy: **absolute local filesystem paths only**, NUL-free. All URI
  schemes, including `file:`, and relative paths are rejected. No expansion,
  existence checks, decoding, downloads, rendering, or symlink resolution of
  image/profile paths occur. No image means fallback-only; a consumer also uses
  fallback when it cannot render an image. Fit modes mean aspect-preserving
  cover/contain, nonuniform stretch, native-size center, or native-size tiling.
* **Keybindings:** array of `{trigger, action, repeat?}`. Action is a nonempty
  string argv array using Ouro's JSON vocabulary, e.g. `["run", "foot"]` or
  `["close"]`; first argument nonempty, all arguments NUL-free. No shell or
  command execution occurs. Modifier syntax follows Ouro (`shift`,
  `control`/`ctrl`, `alt`, `super`/`logo`/`mod4`), case-insensitive. Duplicate
  modifiers and equivalent modifier-order/alias plus case-folded key strings
  are rejected. XKB keysym resolution/aliases, action vocabulary/arity,
  executable validity, and actual binding conflicts remain compositor checks.
  Unspecified repeat means false. This is not the compositor's built-in binding
  baseline and does not implicitly migrate or clear its existing bindings.

## Transport, persistence, and failure semantics

Native Linux AF_UNIX stream, bounded NUL-terminated UTF-8 JSON. Same-effective-UID
`SO_PEERCRED` is checked on each accepted connection; other UIDs receive standard
`PermissionDenied` best-effort before disconnect. Private socket mode is 0600.
**This is UID isolation, not sandbox/app authorization.** A process with the same
UID can configure the desktop and access these files; malicious same-UID path
replacement races are outside the trust boundary.

At most 32 clients, 16 accepts per poll iteration, 256 KiB incoming buffer per
client including NUL, 128 KiB serialized state, 256 KiB maximum reply including
NUL. Method/interface strings are limited to 255 bytes. Oversized frames get
`InvalidParameter` and disconnect; excess clients are closed. Malformed JSON,
schema, method fields/options, unknown fields, and unsupported `oneway: true` or
`upgrade: true` receive standard errors. `more: true` is only valid for Watch;
option flags must be booleans. Omitted parameters mean `{}`, explicit null does
not. Fragments and combined NUL frames are supported. Non-Watch requests may be
pipelined: execution/replies remain ordered, one reply queued per connection.
A Watch occupies its connection; further input closes it after its initial
reply, without executing pipelined calls.

Requests/idle ordinary connections and queued writes have 30-second deadlines;
incremental input does not extend a request deadline. A Watch without pending
output has no deadline. Each subscriber has at most **one** pending snapshot
(plus the kernel's bounded socket buffer). A mutation disconnects any subscriber
whose previous snapshot is still pending, including partially sent snapshots.
Clients must discard incomplete frames. Slow clients never block writers on
socket I/O and do not build unbounded userspace queues. Disk fsync is synchronous
and can delay the entire loop; no realtime storage latency guarantee is made.

State and lock files must be regular, singly linked, effective-UID-owned, exactly
0600; symlinks and unsafe existing permissions are refused, never repaired.
Corrupt/unknown-version state aborts startup and remains byte-for-byte intact.
The store retains an open directory fd and compares existing disk bytes before
Set, refusing external edits/deletion rather than overwriting them. Do not edit
or restore state concurrently; the advisory lock coordinates daemon instances,
not arbitrary text editors. A private same-directory O_EXCL temporary file is
written fully, fsynced, renamed over state, then the directory is fsynced. Newly
created directory entries have their parents fsynced as well.

Before rename, failure returns `PersistenceFailed {}` without changing memory,
revision, the old file, or Watch events. Temporary files are cleaned up during
normal failures; a crash can leave harmless private `.settings-*.tmp` orphans
that are not loaded or automatically removed. Rename is the namespace commit
boundary. **If directory fsync fails after rename, durability is ambiguous:**
the daemon exits nonzero, closes all connections, and sends neither a success
nor a misleading ordinary failure. The new file may already be visible and may
or may not survive power loss. Reconnect/restart and Get before deciding what
to retry. Likewise, losing a connection after commit but before its reply does
not imply failure. No software test proves physical storage power-loss behavior.

## Socket activation is opt-in

`systemd/ourosettings.socket` and `.service` are inert example **user** units:
Accept=no, Type=exec, 0600 socket, 0700 directories, no restart loop. Installation
is a separate operator action; adjust `/usr/local/bin/ourosettings` to the actual
installed binary. Nothing here enables, starts, or installs these units.

Activation requires both `LISTEN_PID` equal to this PID and exactly
`LISTEN_FDS=1`. fd 3 must be a listening Unix stream at the exact configured
path; pathname owner/mode/type are checked. The daemon sets nonblocking/CLOEXEC
and preserves activated sockets on exit. The socket owner (systemd) can retain
queued connections and reactivate the process. A directly started daemon never
unlinks an existing endpoint, even a stale socket: after an abnormal exit the
operator must first establish that no daemon owns it, then remove it. Graceful
direct shutdown removes only its own endpoint.

## Verification

```sh
zig fmt --check build.zig build.zig.zon src
zig build && zig build test --summary all
zig build -Doptimize=ReleaseSafe && zig build test -Doptimize=ReleaseSafe --summary all
zig build -Doptimize=ReleaseFast && zig build test -Doptimize=ReleaseFast --summary all
# Optional independent parser/client, not a runtime dependency:
uv run --with varlink==31.0.0 python tests/interop.py zig-out/bin/ourosettings
```

Integration tests start the actual executable on isolated Unix sockets, not mock
services. They cover stale writes, snapshots/no-ops/restart/reset, corruption
retention, field bounds/duplicate keys/nulls, framing/pipelining, slow readers,
client limits, 30-second partial-request timeout, inherited listener validation,
idle/reactivation, private permissions, locks, and safe socket cleanup. A test-only
LD_PRELOAD shim injects ENOSPC, file-fsync, rename and post-rename directory-fsync
failures into real daemon I/O. Kernel RLIMIT_FSIZE and directory permissions also
exercise actual write failures. No fault hooks exist in the daemon. Foreign-UID
and owner tests use passwordless sudo solely for temporary fixtures; these two
tests skip when unavailable. No real systemd manager or compositor is required.

Verified in the initial Linux x86_64 orb on 2026-09-09, as an unprivileged user:

| Command | Result |
| --- | --- |
| `zig fmt --check build.zig build.zig.zon src` | exit 0, no formatting changes |
| `zig build --summary all` | 3/3 steps succeeded (Debug) |
| `zig build test --summary all` | 5/5 steps; 2/2 Zig tests; 18 integration tests, OK, 33.397s |
| `zig build -Doptimize=ReleaseSafe --prefix zig-out/ReleaseSafe --summary all` | 3/3 steps succeeded |
| `zig build test -Doptimize=ReleaseSafe --summary all` | 5/5 steps; 2/2 Zig tests; 18 integration tests, OK, 32.910s |
| `zig build -Doptimize=ReleaseFast --prefix zig-out/ReleaseFast --summary all` | 3/3 steps succeeded |
| `zig build test -Doptimize=ReleaseFast --summary all` | 5/5 steps; 2/2 Zig tests; 18 integration tests, OK, 32.871s |
| `uv run --with varlink==31.0.0 python tests/interop.py zig-out/bin/ourosettings` | both IDLs parsed; independent client discovery/Get/Set/Watch passed |
| `bash -n .agents/setup`, setup twice, fresh login shell `zig version` | success; cold 10.989s, warm 0.056s; version 0.16.0 |

No tests skipped in this orb. `varlinkctl` was unavailable; its documented
commands were not executed, and the independent Python parser/client was used
instead. `systemd-analyze verify systemd/ourosettings.socket
systemd/ourosettings.service` reported only
`Command /usr/local/bin/ourosettings is not executable: No such file or directory`
(exit 1); the executable and units were deliberately not installed. Activation
was tested with a real inherited listener, not a running systemd user manager.
No compositor integration or physical power-loss test was performed.

References: [ouroshot](https://github.com/rockorager/ouroshot) Linux activation,
peer credentials, and framing; [Ouro](https://github.com/rockorager/ouro)
`src/runtime/settings.zig` and `src/config.zig` output/binding semantics. Capture
machinery, portal policy, and compositor validation engines are not copied here.
