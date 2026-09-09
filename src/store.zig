const std = @import("std");
const os = @import("os.zig");
const c = os.c;
const a = os.a;
const schema = @import("schema.zig");
pub const state_limit = 128 * 1024;
pub const json_options: std.json.Stringify.Options = .{ .emit_null_optional_fields = false };

// Bound recursive processing and canonicalize objects, never arrays or nulls.
// Number lexemes are retained in raw compositor JSON: 1, 1.0 and 1e0 can have
// different validity in Ouro's integer fields and must not be conflated.
pub fn normalize(v: *std.json.Value, depth: usize) !void {
    if (depth > 64) return error.TooDeep;
    switch (v.*) {
        .object => |*object| {
            for (object.values()) |*child| try normalize(child, depth + 1);
            const Order = struct {
                keys: []const []const u8,
                pub fn lessThan(self: @This(), x: usize, y: usize) bool {
                    return std.mem.lessThan(u8, self.keys[x], self.keys[y]);
                }
            };
            object.sort(Order{ .keys = object.keys() });
        },
        .array => |*array| for (array.items) |*child| try normalize(child, depth + 1),
        else => {},
    }
}

// std.json's typed parser accepts numeric strings. The wire contract does not.
pub fn shape(comptime T: type, v: std.json.Value) !void {
    if (T == std.json.Value) {
        if (v != .object) return error.InvalidShape;
        return;
    }
    switch (@typeInfo(T)) {
        .optional => |o| if (v != .null) {
            try shape(o.child, v);
        },
        .@"struct" => |s| {
            if (v != .object) return error.InvalidShape;
            var it = v.object.iterator();
            while (it.next()) |entry| {
                var known = false;
                inline for (s.fields) |f| {
                    if (std.mem.eql(u8, entry.key_ptr.*, f.name)) {
                        try shape(f.type, entry.value_ptr.*);
                        known = true;
                    }
                }
                if (!known) return error.InvalidShape;
            }
            inline for (s.fields) |f| {
                if (f.default_value_ptr == null and !v.object.contains(f.name)) return error.InvalidShape;
            }
        },
        .pointer => |p| {
            if (p.child == u8) {
                if (v != .string) return error.InvalidShape;
            } else {
                if (v != .array) return error.InvalidShape;
                for (v.array.items) |item| try shape(p.child, item);
            }
        },
        .int => if (v != .integer and (v != .number_string or !std.json.isNumberFormattedLikeAnInteger(v.number_string))) return error.InvalidShape,
        .float => if (v != .integer and v != .float and v != .number_string) return error.InvalidShape,
        .bool => if (v != .bool) return error.InvalidShape,
        .@"enum" => if (v != .string) return error.InvalidShape,
        else => @compileError("unsupported schema type"),
    }
}
pub fn parse(comptime T: type, bytes: []const u8) !std.json.Parsed(T) {
    var value = try std.json.parseFromSlice(std.json.Value, a, bytes, .{ .duplicate_field_behavior = .@"error", .max_value_len = state_limit * 2, .parse_numbers = false, .allocate = .alloc_always });
    errdefer value.deinit();
    try normalize(&value.value, 0);
    try shape(T, value.value);
    // Value.jsonParseFromValue borrows its source; retain the source arena.
    return .{ .arena = value.arena, .value = try std.json.parseFromValueLeaky(T, value.arena.allocator(), value.value, .{}) };
}

fn revisionValid(revision: []const u8) !void {
    if (revision.len != 32) return error.InvalidRevision;
    for (revision) |ch| if (!std.ascii.isHex(ch) or std.ascii.isUpper(ch)) return error.InvalidRevision;
}

fn toValue(allocator: std.mem.Allocator, value: anytype) !std.json.Value {
    const bytes = try std.json.Stringify.valueAlloc(allocator, value, json_options);
    return std.json.parseFromSliceLeaky(std.json.Value, allocator, bytes, .{ .parse_numbers = false });
}

fn migrate(bytes: []const u8) ![]u8 {
    const old = try parse(schema.LegacyState, bytes);
    defer old.deinit();
    if (old.value.version != 1) return error.UnknownVersion;
    try revisionValid(old.value.revision);
    try schema.validateLegacy(old.value.settings);
    const allocator = old.arena.allocator();
    var outputs: std.json.Value = .{ .object = .empty };
    for (old.value.settings.outputs) |rule| {
        try outputs.object.put(allocator, rule.name, try toValue(allocator, .{ .priority = rule.priority, .match = rule.match, .settings = rule.settings }));
    }
    var bindings: std.json.Value = .{ .object = .empty };
    for (old.value.settings.keybindings) |binding| {
        try bindings.object.put(allocator, binding.trigger, try toValue(allocator, .{ .action = binding.action, .repeat = binding.repeat }));
    }
    var compositor: std.json.Value = .{ .object = .empty };
    try compositor.object.put(allocator, "output_rules", outputs);
    try compositor.object.put(allocator, "bindings", bindings);
    try normalize(&compositor, 0);
    const revision = try os.token();
    return std.json.Stringify.valueAlloc(a, schema.State{ .version = 2, .revision = &revision, .settings = .{
        .appearance = old.value.settings.appearance,
        .preferred_output = old.value.settings.preferred_output,
        .wallpaper = old.value.settings.wallpaper,
        .compositor = compositor,
    } }, json_options);
}
pub const Store = struct {
    dir: c_int,
    lock_fd: c_int,
    name: [:0]u8,
    disk: []u8,
    state: std.json.Parsed(schema.State),

    pub fn init(path: []const u8) !Store {
        const dir = try os.directory(std.fs.path.dirname(path) orelse return error.InvalidPath);
        errdefer _ = c.close(dir);
        const name = try os.z(std.fs.path.basename(path));
        errdefer a.free(name);
        const lock_name = try std.fmt.allocPrintSentinel(a, "{s}.lock", .{name}, 0);
        defer a.free(lock_name);
        const lock_fd = try os.lock(dir, lock_name);
        errdefer _ = c.close(lock_fd);
        const existing = try read(dir, name);
        var disk = existing orelse blk: {
            const revision = try os.token();
            const bytes = try std.json.Stringify.valueAlloc(a, schema.State{ .version = 2, .revision = &revision, .settings = schema.defaults }, json_options);
            errdefer a.free(bytes);
            try persist(dir, name, bytes);
            break :blk bytes;
        };
        errdefer a.free(disk);
        // Inspect the version before choosing a schema; never rewrite an
        // unknown/corrupt state or partially migrate an unsupported v1 value.
        const header = try std.json.parseFromSlice(std.json.Value, a, disk, .{ .duplicate_field_behavior = .@"error" });
        defer header.deinit();
        if (header.value != .object) return error.InvalidShape;
        const version = header.value.object.get("version") orelse return error.UnknownVersion;
        if (version != .integer or (version.integer != 1 and version.integer != 2)) return error.UnknownVersion;
        const migrating = version.integer == 1;
        if (migrating) {
            const bytes = try migrate(disk);
            a.free(disk);
            disk = bytes;
        }
        if (disk.len > state_limit) return error.StateTooLarge;
        const state = try parse(schema.State, disk);
        errdefer state.deinit();
        if (state.value.version != 2) return error.UnknownVersion;
        try revisionValid(state.value.revision);
        try schema.validate(state.value.settings);
        if (migrating) try persist(dir, name, disk);
        return .{ .dir = dir, .lock_fd = lock_fd, .name = name, .disk = disk, .state = state };
    }
    pub fn deinit(self: *Store) void {
        self.state.deinit();
        a.free(self.disk);
        a.free(self.name);
        _ = c.close(self.lock_fd);
        _ = c.close(self.dir);
    }
    pub fn set(self: *Store, settings: schema.Settings) !bool {
        // Refuse external edits even for a no-op. Locks serialize cooperating
        // daemons; arbitrary same-UID filesystem mutations are not supported.
        const disk = try read(self.dir, self.name) orelse return error.StateChanged;
        defer a.free(disk);
        if (!std.mem.eql(u8, disk, self.disk)) return error.StateChanged;
        const old = try std.json.Stringify.valueAlloc(a, self.state.value.settings, json_options);
        defer a.free(old);
        const new = try std.json.Stringify.valueAlloc(a, settings, json_options);
        defer a.free(new);
        if (std.mem.eql(u8, old, new)) return false;
        const revision = try os.token();
        const bytes = try std.json.Stringify.valueAlloc(a, schema.State{ .version = 2, .revision = &revision, .settings = settings }, json_options);
        errdefer a.free(bytes);
        if (bytes.len > state_limit) return error.StateTooLarge;
        const parsed = try parse(schema.State, bytes);
        errdefer parsed.deinit();
        try persist(self.dir, self.name, bytes);
        self.state.deinit();
        a.free(self.disk);
        self.disk = bytes;
        self.state = parsed;
        return true;
    }
};
fn read(dir: c_int, name: [:0]const u8) !?[]u8 {
    const fd = c.openat(dir, name, c.O_RDONLY | c.O_NOFOLLOW | c.O_CLOEXEC | c.O_NONBLOCK);
    if (fd < 0) {
        if (c.__errno_location().* == c.ENOENT) return null;
        return error.StateReadFailed;
    }
    defer _ = c.close(fd);
    const st = try os.private(fd, c.S_IFREG, 0o600);
    if (st.st_size < 0 or st.st_size > state_limit) return error.StateTooLarge;
    const bytes = try a.alloc(u8, @intCast(st.st_size));
    errdefer a.free(bytes);
    var n: usize = 0;
    while (n < bytes.len) {
        const got = c.read(fd, bytes[n..].ptr, bytes.len - n);
        if (got < 0 and c.__errno_location().* == c.EINTR) continue;
        try os.check(got > 0);
        n += @intCast(got);
    }
    var tail: u8 = 0;
    try os.check(c.read(fd, &tail, 1) == 0);
    return bytes;
}
fn persist(dir: c_int, name: [:0]const u8, bytes: []const u8) !void {
    const random = try os.token();
    const temp = try std.fmt.allocPrintSentinel(a, ".settings-{s}.tmp", .{random}, 0);
    defer a.free(temp);
    const fd = c.openat(dir, temp, c.O_WRONLY | c.O_CREAT | c.O_EXCL | c.O_NOFOLLOW | c.O_CLOEXEC, @as(c.mode_t, 0o600));
    try os.check(fd >= 0);
    defer _ = c.close(fd);
    defer _ = c.unlinkat(dir, temp, 0);
    try os.writeAll(fd, bytes);
    try os.check(c.fsync(fd) == 0);
    try os.check(c.renameat(dir, temp, dir, name) == 0);
    // The namespace commit has occurred. Do not report an ordinary failed Set
    // (which promises unchanged state) if directory durability is uncertain.
    if (c.fsync(dir) != 0) return error.AmbiguousCommit;
}
