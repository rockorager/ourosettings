# ourosettings

A Linux per-user **desired settings store**, written in Zig 0.16.0 with
`std.json` and libc/POSIX. No GUI, compositor, image decoder, database, or runtime
package dependencies beyond libc.

**These settings do not affect Ouro yet.** Ouro currently reads its own
`config.json` / `config.d` snapshots and reloads on SIGHUP; it has no settings
Varlink client. This daemon never writes that config or signals Ouro. A future
compositor consumer must apply desired preferences and own/report live state.
Successful persistence does not prove that a key, action, image, ICC profile,
device, or mode works. No import or merge with Ouro's existing configuration
files is performed. General/layout settings, bindings, input rules and output
rules belong together under `settings.compositor`, in Ouro's own JSON format.

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
* **SetSection** takes `{expected_revision, section, value}` and replaces exactly
  one of `appearance`, `compositor`, `wallpaper`, or `preferred_output`. It uses
  the same global revision as Set, even for writers editing different sections.
  Every unrelated preference is retained. `value` must be the complete section
  object, not a merge patch: omitted optional fields inside it are cleared.
  Only `preferred_output` permits omitted/null `value`, clearing that preference;
  `{}` instead stores an all-output selector. Other sections require an object.
  It returns the same complete snapshot as Set and shares its validation,
  no-op, conflict, persistence and Watch behavior. On conflict, refetch and
  reconsider the section replacement; unrelated writes also invalidate a token.
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
reconnected. A no-op is equality of the serialized snapshot (desktop optional
nulls omitted, object key order normalized, array order retained): it preserves
revision, performs no write, and emits no notification. Explicit `repeat: false`
and unspecified repeat remain distinct stored preferences, though both mean no
repeat in a final Ouro binding. Within `compositor`, nulls, omitted members,
shorthand/object bindings and number lexemes are preserved: `1`, `1.0` and `1e0`
are distinct because Ouro rejects non-integer tokens in integer fields. JSON
whitespace, string escaping and object order are not preserved. External edits
are checked before even a no-op Set or SetSection.

With systemd's `varlinkctl` installed (255+ for basic calls, 257+ for the infinite
Watch timeout), replace `address` for an isolated instance if desired:

```sh
address="unix:$XDG_RUNTIME_DIR/ouro/settings.sock"
interface=dev.rockorager.ouro.Settings
varlinkctl info "$address"
varlinkctl introspect "$address" "$interface"
varlinkctl call "$address" "$interface.Get" '{}'

# Read the token and replace appearance without resubmitting compositor data.
# jq is only an example client tool, not a daemon dependency.
snapshot=$(varlinkctl call "$address" "$interface.Get" '{}')
printf '%s' "$snapshot" | jq '{expected_revision: .revision,
  section: "appearance", value: (.settings.appearance | .color_scheme = "dark")}' |
  varlinkctl call "$address" "$interface.SetSection"

varlinkctl --more --timeout=infinity call "$address" "$interface.Watch" '{}'
varlinkctl validate-idl protocol/dev.rockorager.ouro.Settings.varlink
```

## Version 2 schema and validation boundary

`examples/settings.json` illustrates the persisted envelope
`{version: 2, revision, settings}`. Its token and paths are illustrative, not a
file to copy over a running daemon. Use only its `settings` object in a Set,
with a fresh revision from Get. Initial defaults: default color scheme, no
accent/preferred output, black fallback wallpaper with fill fit and no
image/per-output entries, and `compositor: {}`. No compositor defaults are
invented or copied into this daemon.

`appearance`, `compositor`, and `wallpaper` are required; `preferred_output` is
optional. Unknown fields in daemon-owned typed structures and duplicate JSON
keys at every depth are rejected. Desktop optional fields may be absent or
null, meaning unspecified; responses omit them. Required fields reject null.
Numeric strings and non-integer tokens in desktop integer fields (including
`1.0` and `1e0`) are rejected. Arrays are replacements, not merge patches.

* **Appearance:** `color_scheme` is `default`, `light`, or `dark`; optional RGB
  accent has required finite `r`, `g`, `b` components in [0, 1].
* **Preferred output:** a separate desktop-wide OutputMatch preference, not a
  live connected/primary-output result and not duplicated under `compositor`.
  OutputMatch fields are conjunctive: optional byte-glob `name` (`*`/`?`, e.g.
  `DRM-*`), u32 `connector_id`, `connector_type`, `connector_type_id`, `width_mm`,
  `height_mm`. `{}` matches all outputs. Selection among multiple matches is a
  consumer decision.
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
* **Compositor:** a standard Varlink `object`, not a JSON-encoded string or a
  daemon-specific action/rule schema. The daemon requires an object whose only
  members are `general`, `bindings`, `input_rules`, and `output_rules`, each
  either an object or null. Their contents remain raw JSON, including unknown
  nested fields, partial rules/bindings, numbers and nulls. The daemon does not
  claim these contents pass Ouro validation. Ouro owns field types/ranges,
  triggers/XKB, action vocabulary/arity, rule matching, hardware support, scale
  quantization, and ICC loading/application. This boundary avoids a second
  incompatible schema and supports input reset unions and dynamic keys without
  normalizing away their meaning. Desktop sections remain typed in the IDL;
  `SetSection.value` is `?object` because its type depends on `section`.

Authoritative config contract inspected in upstream Ouro
[`1394e361`](https://github.com/rockorager/ouro/commit/1394e361993e100775d680ef9585fe9a1a7ce4ce),
`src/config.zig` (parser and tests) and `src/runtime/settings.zig`:

* `general` contains `focus_follows_mouse`, `inner_gap`, `outer_gap`.
* `bindings` maps exact trigger strings to action argv arrays or
  `{action: [...], repeat?: bool}`. Argument order, case and empty arguments
  remain intact. No actions are executed here.
* `input_rules` and `output_rules` are **objects keyed by rule name**, not
  arrays. Rules carry optional `priority`, `match`, `settings`. Ouro applies
  matching rules in ascending `(priority, name)` order, with later specified
  settings winning. JSON object order is irrelevant; names and priorities are
  retained. Output settings include enabled/mode/position/scale/ICC preferences;
  input settings include device toggles, acceleration, scrolling and repeat.
* Ouro layers config sources using RFC 7396 over its built-in defaults. An
  omitted member leaves the underlying value alone; null deletes that member.
  `bindings: null` clears the binding class; `bindings: {}` does not clear
  built-in bindings. Null nested members remove overrides. Input `"default"`
  explicitly resets to a device/software default and is distinct from absent
  or null, which contributes no override to rule resolution. General and input
  omissions leave Ouro's own default behavior in charge.

The stored `compositor` is one desired Ouro-format configuration source, not
resolved live state. Set/SetSection replace that entire stored source; they do
**not** apply RFC 7396 to the previous stored source. Future consumers must
define how this source participates in Ouro's configuration layering; none is
implemented here. A partial source can be valid only in combination with other
sources, so persistence cannot guarantee final compositor validity.

## Version 1 migration is atomic and fail-closed

Startup automatically validates version 1 using its original strict schema,
then converts `outputs` to `compositor.output_rules` keyed by each rule's name,
and `keybindings` to `compositor.bindings` keyed by each exact trigger. Priority,
match/settings, argv order and optional repeat are retained. Array order was
not rule precedence in version 1; `(priority, name)` still determines it.
Appearance, preferred output and wallpaper remain unchanged (legacy optional
nulls still mean absent). General and input rules remain absent. Empty legacy
collections become empty objects, not null class deletions; migration neither
imports nor clears Ouro's existing configuration.

The converted envelope is version 2 with a **fresh global revision**, atomically
persisted before any requests or initial Watch snapshot are served. Old tokens
cannot overwrite migrated preferences; clients must refetch and use the new
schema. Version 2 retains its revision on subsequent restarts. Migration is
one-way; back up the stopped daemon's file before upgrading if rollback is
needed. The old executable will refuse version 2 rather than downgrade it.

Invalid revisions, duplicate/ambiguous legacy names or triggers, unknown fields,
malformed JSON, unsupported versions or an oversized result abort startup
without rewriting the original file. Pre-rename migration write/fsync/rename
failures also leave the original intact. Post-rename directory-fsync failure
has the same ambiguous-commit behavior described below; restart and inspect.
No best-effort dropping of unsupported data and no repair of unsafe files occurs.

## Transport, persistence, and failure semantics

Native Linux AF_UNIX stream, bounded NUL-terminated UTF-8 JSON (maximum nesting
depth 64 from the request/envelope root). Same-effective-UID
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
services. They cover Ouro config examples, null/default/omission and argv
preservation, rule names/priorities, atomic section replacement, v1 migration
and no-clobber failures, stale writes, snapshots/no-ops/restart/reset, corruption
retention, field bounds/duplicate keys/nulls, framing/pipelining, slow readers,
client limits, 30-second partial-request timeout, inherited listener validation,
idle/reactivation, private permissions, locks, and safe socket cleanup. A test-only
LD_PRELOAD shim injects ENOSPC, file-fsync, rename and post-rename directory-fsync
failures into real daemon I/O. Kernel RLIMIT_FSIZE and directory permissions also
exercise actual write failures. No fault hooks exist in the daemon. Foreign-UID
and owner tests use passwordless sudo solely for temporary fixtures; these two
tests skip when unavailable. No real systemd manager or compositor is required.

Verified for version 2 in this Linux x86_64 orb on 2026-09-09, as an
unprivileged user with Zig 0.16.0:

| Command | Result |
| --- | --- |
| `zig fmt --check build.zig build.zig.zon src` | exit 0, no formatting changes |
| `zig build --summary all` | 3/3 steps succeeded (Debug) |
| `zig build test --summary all` | 5/5 steps; 2/2 Zig tests; 23 integration tests, OK, 34.459s |
| `zig build -Doptimize=ReleaseSafe --prefix zig-out/ReleaseSafe --summary all` | 3/3 steps succeeded |
| `zig build test -Doptimize=ReleaseSafe --summary all` | 5/5 steps; 2/2 Zig tests; 23 integration tests, OK, 33.994s |
| `zig build -Doptimize=ReleaseFast --prefix zig-out/ReleaseFast --summary all` | 3/3 steps succeeded |
| `zig build test -Doptimize=ReleaseFast --summary all` | 5/5 steps; 2/2 Zig tests; 23 integration tests, OK, 33.934s |
| `uv run --with varlink==31.0.0 python tests/interop.py zig-out/bin/ourosettings` | both IDLs parsed; independent client discovery/Get/Set/SetSection/Watch passed |

No tests skipped in this orb. `varlinkctl` was unavailable; its documented
commands were not executed, and the independent Python parser/client was used
instead. An initial ReleaseSafe run exposed a pre-existing slow-reader test
race: a write can disconnect a Watch before its initial snapshot is flushed.
The test now consumes and checks the initial snapshot before withholding reads,
then checks ordered committed snapshots and eventual backpressure disconnect.
All three full suites above passed after that correction. Activation was tested
with a real inherited listener, not a running systemd user manager. No services
were installed/enabled, and no compositor integration, live desktop mutation or
physical power-loss test was performed.

References: [ouroshot](https://github.com/rockorager/ouroshot) Linux activation,
peer credentials, and framing; [Ouro](https://github.com/rockorager/ouro)
`src/runtime/settings.zig` and `src/config.zig` output/binding semantics. Capture
machinery, portal policy, and compositor validation engines are not copied here.
