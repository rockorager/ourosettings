const std = @import("std");
const schema = @import("schema.zig");
const storage = @import("store.zig");
const a = @import("os.zig").a;

// RFC 6901 string form, not URI fragments. Validate the entire pointer even
// when an earlier token would fail to resolve. Empty tokens and NUL are legal.
pub fn validate(path: []const u8) !void {
    if (path.len == 0) return;
    if (path[0] != '/') return error.InvalidPointer;
    var i: usize = 1;
    while (i < path.len) : (i += 1) {
        if (path[i] != '~') continue;
        i += 1;
        if (i == path.len or (path[i] != '0' and path[i] != '1')) return error.InvalidPointer;
    }
}

fn resolve(root: std.json.Value, path: []const u8, scratch: []u8) ?std.json.Value {
    if (path.len == 0) return root;
    var value = root;
    var tokens = std.mem.splitScalar(u8, path[1..], '/');
    while (tokens.next()) |token| {
        var i: usize = 0;
        var n: usize = 0;
        while (i < token.len) : (i += 1) {
            scratch[n] = if (token[i] == '~') blk: {
                i += 1;
                break :blk if (token[i] == '0') @as(u8, '~') else @as(u8, '/');
            } else token[i];
            n += 1;
        }
        const key = scratch[0..n];
        value = switch (value) {
            .object => |object| object.get(key) orelse return null,
            .array => |array| blk: {
                if (key.len == 0 or (key.len > 1 and key[0] == '0')) return null;
                for (key) |ch| if (ch < '0' or ch > '9') return null;
                const index = std.fmt.parseInt(usize, key, 10) catch return null;
                if (index >= array.items.len) return null;
                break :blk array.items[index];
            },
            else => return null,
        };
    }
    return value;
}

// Resolve against the same typed serialization Get/Watch expose, not disk
// JSON (which can still contain optional nulls). Keep raw number lexemes and
// existing field ordering so byte equality is the store's no-op equality.
// Only the selected JSON text survives the call; no snapshot history is kept.
pub fn selection(settings: schema.Settings, path: []const u8) !?[]u8 {
    const bytes = try std.json.Stringify.valueAlloc(a, settings, storage.json_options);
    defer a.free(bytes);
    const parsed = try std.json.parseFromSlice(std.json.Value, a, bytes, .{ .parse_numbers = false });
    defer parsed.deinit();
    const scratch = try a.alloc(u8, path.len);
    defer a.free(scratch);
    const value = resolve(parsed.value, path, scratch) orelse return null;
    return try std.json.Stringify.valueAlloc(a, value, storage.json_options);
}
