const std = @import("std");
const a = @import("os.zig").a;
const storage = @import("store.zig");
const pointer = @import("pointer.zig");
const schema = @import("schema.zig");
const schemas = @import("mcp_schema.zig");
const V = std.json.Value;
pub const version = "2026-07-28";
pub const record_limit = 256 * 1024; // Includes the newline delimiter.
const root = "ouro://settings";

fn eq(x: []const u8, y: []const u8) bool {
    return std.mem.eql(u8, x, y);
}
fn get(v: V, key: []const u8) V {
    return if (v == .object) v.object.get(key) orelse .null else .null;
}
fn text(v: V, expected: []const u8) bool {
    return v == .string and eq(v.string, expected);
}
fn keys(v: V, allowed: []const []const u8) bool {
    if (v != .object) return false;
    for (v.object.keys()) |key| {
        for (allowed) |candidate| {
            if (eq(key, candidate)) break;
        } else return false;
    }
    return true;
}
fn idValue(v: V) ?V {
    return switch (v) {
        .string => if (v.string.len <= 255) v else null,
        .number_string => .{ .integer = std.fmt.parseInt(i64, v.number_string, 10) catch return null },
        else => null,
    };
}
fn sameId(x: V, y: V) bool {
    return if (x == .string and y == .string) eq(x.string, y.string) else if (x == .integer and y == .integer) x.integer == y.integer else false;
}

// A URI path is decoded once, then validated as string-form JSON Pointer.
// The URI identifies a selection, even when that selection does not exist.
fn uriPath(allocator: std.mem.Allocator, uri: []const u8) ![]u8 {
    if (uri.len > 4096 or !std.mem.startsWith(u8, uri, root)) return error.InvalidUri;
    const suffix = uri[root.len..];
    if (suffix.len != 0 and suffix[0] != '/') return error.InvalidUri;
    const decoded = try allocator.alloc(u8, suffix.len);
    var used: usize = 0;
    var i: usize = 0;
    while (i < suffix.len) : (i += 1) {
        const ch = suffix[i];
        if (ch == '%') {
            if (i + 2 >= suffix.len) return error.InvalidUri;
            if (!std.ascii.isHex(suffix[i + 1]) or !std.ascii.isHex(suffix[i + 2])) return error.InvalidUri;
            decoded[used] = std.fmt.parseInt(u8, suffix[i + 1 ..][0..2], 16) catch return error.InvalidUri;
            i += 2;
        } else {
            if (!(std.ascii.isAlphanumeric(ch) or std.mem.indexOfScalar(u8, "-._~/", ch) != null)) return error.InvalidUri;
            decoded[used] = ch;
        }
        used += 1;
    }
    const path = decoded[0..used];
    if (!std.unicode.utf8ValidateSlice(path)) return error.InvalidUri;
    try pointer.validate(path);
    return path;
}

pub fn rpcError(client: anytype, id: V, code: i32, message: []const u8) !bool {
    // MCP's error schema omits an unreadable ID; it never carries a null ID.
    try client.reply(.{ .jsonrpc = "2.0", .id = if (id == .null) @as(?V, null) else id, .@"error" = .{ .code = code, .message = message } });
    return false;
}
fn result(client: anytype, id: V, value: anytype) !bool {
    try client.reply(.{ .jsonrpc = "2.0", .id = id, .result = value });
    return false;
}
fn toolResult(client: anytype, id: V, value: anytype, is_error: bool, revision: ?[]const u8) !void {
    const bytes = try std.json.Stringify.valueAlloc(a, value, storage.json_options);
    defer a.free(bytes);
    var envelope = .{ .jsonrpc = "2.0", .id = id, .result = .{ .resultType = "complete", .isError = is_error, .structuredContent = value, .content = .{.{ .type = "text", .text = bytes }} } };
    const full = try std.json.Stringify.valueAlloc(a, envelope, storage.json_options);
    defer a.free(full);
    if (full.len < record_limit or revision == null) return client.replyBytes(full);
    // Do not lower the storage limit or turn a committed mutation into failure
    // just because duplicating its snapshot in escaped text exceeds the wire cap.
    const summary = try std.json.Stringify.valueAlloc(a, .{ .revision = revision.? }, storage.json_options);
    defer a.free(summary);
    envelope.result.content[0].text = summary;
    try client.reply(envelope);
}

fn replaceSettings(store: *storage.Store, allocator: std.mem.Allocator, params: V, section_write: bool) !bool {
    var settings = store.state.value.settings;
    const expected_revision = if (section_write) blk: {
        const SetSection = struct { expected_revision: []const u8, section: schema.Section, value: ?V = null };
        storage.shape(SetSection, params) catch return error.Parameters;
        const update = std.json.parseFromValueLeaky(SetSection, allocator, params, .{}) catch return error.Section;
        const value = update.value orelse V.null;
        switch (update.section) {
            inline else => |section| {
                const T = @FieldType(schema.Settings, @tagName(section));
                storage.shape(T, value) catch return error.Value;
                @field(settings, @tagName(section)) = std.json.parseFromValueLeaky(T, allocator, value, .{}) catch return error.Value;
            },
        }
        break :blk update.expected_revision;
    } else blk: {
        const Set = struct { expected_revision: []const u8, settings: schema.Settings };
        storage.shape(Set, params) catch return error.Parameters;
        const value = std.json.parseFromValueLeaky(Set, allocator, params, .{}) catch return error.Settings;
        settings = value.settings;
        break :blk value.expected_revision;
    };
    schema.validate(settings) catch return error.Settings;
    if (!eq(expected_revision, store.state.value.revision)) return error.Conflict;
    return store.set(settings) catch |err| switch (err) {
        error.AmbiguousCommit => return err,
        error.StateTooLarge => error.Settings,
        else => error.PersistenceFailed,
    };
}

const Selection = struct { uri: []const u8, path: []const u8, selected: ?[]u8 };
const Subscription = struct {
    parsed: std.json.Parsed(V),
    id: V,
    selections: []Selection,

    fn deinit(self: *Subscription) void {
        for (self.selections) |s| if (s.selected) |value| a.free(value);
        self.parsed.deinit();
    }
};

pub const Peer = struct {
    subscriptions: [8]?Subscription = @splat(null),

    pub fn deinit(self: *Peer) void {
        for (&self.subscriptions) |*sub| if (sub.*) |*s| s.deinit();
        self.* = .{};
    }
    pub fn listening(self: *const Peer) bool {
        for (self.subscriptions) |sub| if (sub != null) return true;
        return false;
    }
    pub fn notify(self: *Peer, client: anytype, store: *storage.Store) !void {
        for (&self.subscriptions) |*slot| {
            const sub = if (slot.*) |*s| s else continue;
            for (sub.selections) |*selection| {
                const selected = try pointer.selection(store.state.value.settings, selection.path);
                const equal = if (selected) |value| (if (selection.selected) |prior| eq(value, prior) else false) else selection.selected == null;
                if (equal) {
                    if (selected) |value| a.free(value);
                    continue;
                }
                if (selection.selected) |value| a.free(value);
                selection.selected = selected;
                try client.reply(.{ .jsonrpc = "2.0", .method = "notifications/resources/updated", .params = .{ ._meta = .{ .@"io.modelcontextprotocol/subscriptionId" = sub.id }, .uri = selection.uri } });
            }
        }
    }

    // Returns whether a commit occurred, including when its reply cannot fit.
    // The host must publish before servicing another request.
    pub fn request(self: *Peer, client: anytype, store: *storage.Store, bytes: []const u8) !bool {
        var parsed = std.json.parseFromSlice(V, a, bytes, .{ .duplicate_field_behavior = .@"error", .parse_numbers = false, .allocate = .alloc_always }) catch return rpcError(client, .null, -32700, "Parse error");
        var retained = false;
        defer if (!retained) parsed.deinit();
        storage.normalize(&parsed.value, 0) catch return rpcError(client, .null, -32600, "Invalid request");
        const obj = parsed.value;
        if (!keys(obj, &.{ "jsonrpc", "id", "method", "params" }) or !text(get(obj, "jsonrpc"), "2.0") or get(obj, "method") != .string) return rpcError(client, .null, -32600, "Invalid request");
        const method = get(obj, "method").string;
        const params = get(obj, "params");
        if (!obj.object.contains("id")) {
            // Notifications never receive a response, including unknown ones.
            if (eq(method, "notifications/cancelled")) {
                const cancelled = idValue(get(params, "requestId")) orelse return false;
                for (&self.subscriptions) |*slot| if (slot.*) |*sub| {
                    if (sameId(sub.id, cancelled)) {
                        sub.deinit();
                        slot.* = null;
                    }
                };
            }
            return false;
        }
        const id = idValue(get(obj, "id")) orelse return rpcError(client, .null, -32600, "Invalid request ID");
        for (self.subscriptions) |slot| if (slot) |sub| {
            if (sameId(sub.id, id)) return rpcError(client, id, -32600, "Request ID is already active");
        };
        const meta = get(params, "_meta");
        const requested = get(meta, "io.modelcontextprotocol/protocolVersion");
        if (requested != .string or get(meta, "io.modelcontextprotocol/clientCapabilities") != .object) return rpcError(client, id, -32602, "Required request metadata missing or invalid");
        if (!eq(requested.string, version)) {
            try client.reply(.{ .jsonrpc = "2.0", .id = id, .@"error" = .{ .code = @as(i32, -32022), .message = "Unsupported protocol version", .data = .{ .supported = .{version}, .requested = requested.string } } });
            return false;
        }
        const allocator = parsed.arena.allocator();
        if (eq(method, "server/discover")) {
            if (!keys(params, &.{"_meta"})) return rpcError(client, id, -32602, "Invalid params");
            return result(client, id, .{ .resultType = "complete", .supportedVersions = .{version}, .capabilities = .{ .tools = struct {}{}, .resources = .{ .subscribe = true } }, ._meta = .{ .@"io.modelcontextprotocol/serverInfo" = .{ .name = "ourosettings", .version = "0.1.0" } }, .ttlMs = 0, .cacheScope = "private" });
        }
        if (eq(method, "tools/list")) {
            if (!keys(params, &.{"_meta"})) return rpcError(client, id, -32602, "Invalid params");
            return result(client, id, .{ .resultType = "complete", .tools = try schemas.tools(allocator), .ttlMs = 0, .cacheScope = "private" });
        }
        if (eq(method, "resources/list")) {
            if (!keys(params, &.{"_meta"})) return rpcError(client, id, -32602, "Invalid params");
            return result(client, id, .{ .resultType = "complete", .resources = .{
                .{ .uri = root, .name = "settings", .mimeType = "application/json" },
                .{ .uri = root ++ "/appearance", .name = "appearance", .mimeType = "application/json" },
                .{ .uri = root ++ "/compositor", .name = "compositor", .mimeType = "application/json" },
                .{ .uri = root ++ "/preferred_output", .name = "preferred_output", .mimeType = "application/json" },
                .{ .uri = root ++ "/wallpaper", .name = "wallpaper", .mimeType = "application/json" },
            }, .ttlMs = 0, .cacheScope = "private" });
        }
        if (eq(method, "resources/templates/list")) {
            if (!keys(params, &.{"_meta"})) return rpcError(client, id, -32602, "Invalid params");
            return result(client, id, .{ .resultType = "complete", .resourceTemplates = .{.{ .uriTemplate = root ++ "{+pointer}", .name = "settings-pointer", .description = "A percent-encoded RFC 6901 pointer, empty for all settings. Missing selections have exists false; stored null has exists true.", .mimeType = "application/json" }}, .ttlMs = 0, .cacheScope = "private" });
        }
        if (eq(method, "resources/read")) {
            const uri = get(params, "uri");
            if (!keys(params, &.{ "_meta", "uri" }) or uri != .string) return rpcError(client, id, -32602, "Invalid params");
            const path = uriPath(allocator, uri.string) catch return rpcError(client, id, -32602, "Invalid resource URI");
            const selected = try pointer.selection(store.state.value.settings, path);
            defer if (selected) |value| a.free(value);
            const value = try std.json.parseFromSliceLeaky(V, allocator, selected orelse "null", .{ .parse_numbers = false });
            const content = try std.json.Stringify.valueAlloc(allocator, .{ .revision = store.state.value.revision, .exists = selected != null, .value = value }, storage.json_options);
            return result(client, id, .{ .resultType = "complete", .contents = .{.{ .uri = uri.string, .mimeType = "application/json", .text = content }}, .ttlMs = 0, .cacheScope = "private" });
        }
        if (eq(method, "tools/call")) {
            const name = get(params, "name");
            const args = get(params, "arguments");
            if (!keys(params, &.{ "_meta", "name", "arguments" }) or name != .string or (params.object.contains("arguments") and args != .object)) return rpcError(client, id, -32602, "Invalid params");
            const section = text(name, "settings.set_section");
            if (!section and !text(name, "settings.set")) return rpcError(client, id, -32602, "Unknown tool");
            const changed = replaceSettings(store, allocator, args, section) catch |err| {
                if (err == error.AmbiguousCommit) return err;
                const code: []const u8 = switch (err) {
                    error.Conflict => "Conflict",
                    error.PersistenceFailed => "PersistenceFailed",
                    else => "InvalidParameters",
                };
                const message: []const u8 = switch (err) {
                    error.Conflict => "Revision conflict; read current settings and reconsider the replacement",
                    error.PersistenceFailed => "Could not persist settings",
                    else => "Invalid settings replacement",
                };
                try toolResult(client, id, .{ .@"error" = .{ .code = code, .message = message, .revision = if (err == error.Conflict) @as(?[]const u8, store.state.value.revision) else null } }, true, null);
                return false;
            };
            toolResult(client, id, .{ .revision = store.state.value.revision, .settings = store.state.value.settings }, false, store.state.value.revision) catch |err| switch (err) {
                error.ReplyTooLarge => client.close(),
                else => return err,
            };
            return changed;
        }
        if (eq(method, "subscriptions/listen")) {
            const filter = get(params, "notifications");
            if (!keys(params, &.{ "_meta", "notifications" }) or !keys(filter, &.{ "resourceSubscriptions", "toolsListChanged", "resourcesListChanged", "promptsListChanged" })) return rpcError(client, id, -32602, "Invalid params");
            inline for (.{ "toolsListChanged", "resourcesListChanged", "promptsListChanged" }) |key| {
                if (filter.object.contains(key) and get(filter, key) != .bool) return rpcError(client, id, -32602, "Invalid notification filter");
            }
            const uris = get(filter, "resourceSubscriptions");
            if (filter.object.contains("resourceSubscriptions") and uris != .array) return rpcError(client, id, -32602, "Invalid resource subscriptions");
            const items: []const V = if (uris == .array) uris.array.items else &.{};
            var count: usize = items.len;
            var available: ?*?Subscription = null;
            for (&self.subscriptions) |*slot| {
                if (slot.*) |sub| count += sub.selections.len else available = slot;
            }
            if (available == null or count > 16) return rpcError(client, id, -32602, "Subscription limit exceeded");
            const selections = try allocator.alloc(Selection, items.len);
            var initialized: usize = 0;
            defer if (!retained) {
                for (selections[0..initialized]) |s| if (s.selected) |value| a.free(value);
            };
            for (items, 0..) |uri, i| {
                if (uri != .string) return rpcError(client, id, -32602, "Invalid resource URI");
                const path = uriPath(allocator, uri.string) catch return rpcError(client, id, -32602, "Invalid resource URI");
                selections[i] = .{ .uri = uri.string, .path = path, .selected = try pointer.selection(store.state.value.settings, path) };
                initialized += 1;
            }
            try client.reply(.{ .jsonrpc = "2.0", .method = "notifications/subscriptions/acknowledged", .params = .{ ._meta = .{ .@"io.modelcontextprotocol/subscriptionId" = id }, .notifications = .{ .resourceSubscriptions = items } } });
            available.?.* = .{ .parsed = parsed, .id = id, .selections = selections };
            retained = true;
            return false;
        }
        return rpcError(client, id, -32601, "Method not found; supported MCP version is 2026-07-28");
    }
};
