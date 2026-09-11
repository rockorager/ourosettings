# MCP migration contract

This document defines the first migration slice across ourosettings, the Ouro
compositor, and Ourokit. It is a target contract, not a claim that every repo has
migrated. Desktop-wide discovery, instance registration, agent authorization,
and an assistant-facing stdio bridge follow separately.

## Local transport

Use MCP revision `2026-07-28`, JSON-RPC 2.0, and UTF-8 JSON records terminated by
one newline over Unix stream sockets. A connection may carry concurrent requests
and resource subscriptions; correlate replies by request ID, not wire order.
Bound records to 256 KiB including the delimiter. Preserve each host's existing
event-loop ownership, peer checks, short-write handling, and cancellation rules.

Every request carries `params._meta` with
`io.modelcontextprotocol/protocolVersion: "2026-07-28"` and
`io.modelcontextprotocol/clientCapabilities: {}`. Include client identity in
`io.modelcontextprotocol/clientInfo`. There is no `initialize` handshake.
Ordinary results carry `resultType: "complete"`. Unsupported versions produce
JSON-RPC error `-32022` with `data.supported` and `data.requested`.

Servers always emit `resultType`. Clients follow MCP’s required fallback for an
absent `resultType`, treating it as `"complete"`; present but malformed or unknown
values are invalid. This does not add support for legacy version negotiation.

Servers implement `server/discover` and the discovery methods for advertised
capabilities. During this slice, list/discovery results and resource reads use
`ttlMs: 0` and `cacheScope: "private"`. The future assistant bridge must select
its compatibility policy against real host support; local clients do not claim
support for initialization-based MCP revisions.

## Settings resources

The complete settings resource is `ouro://settings`. A resource URI's path is
an RFC 6901 JSON pointer, percent-encoded for transport. Decode URI escapes once,
then apply JSON pointer escaping. Encode bytes other than URI unreserved
characters and `/` as uppercase percent escapes. Reject query strings,
fragments, malformed escapes, and invalid pointers. Do not normalize slashes or
Unicode. Examples:

- `ouro://settings/compositor`: compositor configuration.
- `ouro://settings/appearance/color_scheme`: appearance preference.
- `ouro://settings/`: the empty-name member at the settings root, not the root.
- A key `a/b` is the pointer segment `a~1b`; a literal `%2F` is `%252F`.

`resources/list` includes the root and supported settings section resources.
`resources/templates/list` advertises the pointer resource family. Resource
contents have MIME type `application/json` and a `text` field encoding:

```json
{"revision":"opaque-store-revision","exists":true,"value":{"general":{}}}
```

`revision` is the existing global optimistic-concurrency token, not a numeric
sequence. A valid pointer that selects no value is still a readable selection
resource: return `exists: false, value: null`. Stored JSON null instead returns
`exists: true, value: null`. Unsupported resource families are invalid resource
requests, not missing selections. Settings persistence and normalization remain
authoritative; reading must not reinterpret their missing/null behavior.
Preserve number lexemes in compositor JSON: `1`, `1.0`, and `1e0` differ in
Ouro's integer validation and must not be normalized into one another.

## Settings mutations

Expose `settings.set` and `settings.set_section` through `tools/list` and
`tools/call`, with JSON Schemas and descriptions explaining full replacement:

- `settings.set`: `{ expected_revision, settings }` replaces all settings.
- `settings.set_section`: `{ expected_revision, section, value? }` replaces one
  complete section. Omitted/null `preferred_output` clears it; other sections
  reject omitted/null values, as before. This is not a merge operation.

Success uses authoritative `structuredContent: { revision, settings }` plus
serialized JSON in a text content block. Normally the text duplicates the full
structured result. If that duplication would exceed the 256 KiB record limit
including delimiter, keep the full structured result and use text JSON
`{ revision }` instead. Do not lower the storage limit or report a committed
mutation as a failure because of duplicated text. Tool execution failures use
`isError: true` and
`structuredContent: { error: { code, message, revision? } }`. Codes are
`Conflict`, `InvalidParameters`, and `PersistenceFailed`; `Conflict` includes
the current revision. Output schemas cover both success and execution failure.
Malformed RPC envelopes and unknown tools use JSON-RPC errors instead.

Preserve atomic persistence, no-op suppression, validation, and fail-stop
behavior for an ambiguous durable commit. A successful write means desired
settings were persisted, not that compositor hardware changes have completed.
Never automatically replay a mutation after losing its response.

## Subscription and read ordering

Use `subscriptions/listen` with `notifications.resourceSubscriptions`. Its
first notification is `notifications/subscriptions/acknowledged`, echoing the
accepted URI filter. Each subscription notification carries the listen request
ID in `_meta["io.modelcontextprotocol/subscriptionId"]`.

Notifications contain the changed URI, not its value or revision. Notify only
when that URI's selected value or existence changes; unrelated commits and
no-op writes do not notify. Multiple subscriptions and ordinary requests can
share a connection. Cancellation uses `notifications/cancelled` with the listen
request ID; disconnect discards subscription state.

Clients subscribe, await acknowledgment of their URI, then read current state.
Keep at most one read outstanding per watched resource. A notification during
that read marks it dirty; after accepting the response, issue another read if
dirty. This avoids missing a change without accumulating reads. Reconnect means
subscribe and read again, not replay. Ignore stale completions from a retired
connection. Bounded output can coalesce invalidations or disconnect a slow
subscriber; it must not silently drop the last invalidation on a live stream.

Ouro preserves its initial settings validation gate, runtime last-good config,
retry backoff, and application-safe-point handoff. Ourokit will preserve the
equivalent appearance-service lifetime when its client migrates.

## Keybinding calls

Keep Ouro's `call` configuration shape:

```json
["call", "unix:/run/user/1000/example.sock", "toggle_launcher", {}]
```

The third field becomes an MCP tool name rather than a qualified Varlink
method. Send `tools/call` with `name` and `arguments`. A configured call needs no
discovery round trip. Preserve nonblocking independent calls, the five-second
timeout, the 16-call bound, and no retries. Treat JSON-RPC errors and
`isError: true` as failures; discard successful results. Unsupported interim
results must fail explicitly rather than look successful. Existing Varlink
targets must migrate; unchanged configuration shape is not wire compatibility.

## Cutover

ourosettings exposes only MCP at `$XDG_RUNTIME_DIR/ouro/settings.mcp.sock`, with
one process, one settings store and one systemd listener. `--socket` is the sole
explicit socket override; `--mcp-socket` is removed. There is no Varlink endpoint,
framing detection, or compatibility shim. Existing state-file migrations remain.

Migrate Ouro's settings and keybinding clients first, then Ourokit's appearance
and Lua settings consumers, control client/server, and custom action schemas.
Old Ourokit Varlink settings consumers cannot use this daemon and must migrate
before use. Ourokit's `ouroctl` activation, status, and reload clients must move
with its application server. Never run two independently authoritative stores.

## Sources

- [MCP 2026-07-28 changes](https://modelcontextprotocol.io/specification/2026-07-28/changelog)
- [Transport bindings](https://modelcontextprotocol.io/specification/2026-07-28/basic/transports)
- [Subscriptions](https://modelcontextprotocol.io/specification/2026-07-28/basic/patterns/subscriptions)
- [Resources](https://modelcontextprotocol.io/specification/2026-07-28/server/resources)
- [Tools](https://modelcontextprotocol.io/specification/2026-07-28/server/tools)
- [Existing Ourokit Varlink contract](https://github.com/rockorager/ourokit/blob/main/docs/varlink.md)
