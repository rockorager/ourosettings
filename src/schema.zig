const std = @import("std");
pub const RGB = struct { r: f64, g: f64, b: f64 };
pub const OutputMatch = struct {
    name: ?[]const u8 = null,
    connector_id: ?u32 = null,
    connector_type: ?u32 = null,
    connector_type_id: ?u32 = null,
    width_mm: ?u32 = null,
    height_mm: ?u32 = null,
};
pub const OutputRule = struct {
    name: []const u8,
    priority: i32,
    match: OutputMatch,
    settings: struct {
        enabled: ?bool = null,
        mode: ?struct { width: u32, height: u32, refresh_millihertz: ?u32 = null } = null,
        position: ?struct { x: i32, y: i32 } = null,
        scale: ?f64 = null,
        icc_profile: ?[]const u8 = null,
    },
};
pub const Wallpaper = struct {
    image: ?[]const u8 = null,
    fit: enum { fill, fit, stretch, center, tile },
    fallback: RGB,
};
// Version 1 types remain only to validate and migrate existing state.
pub const LegacySettings = struct {
    appearance: struct { color_scheme: enum { default, light, dark }, accent: ?RGB = null },
    outputs: []const OutputRule,
    preferred_output: ?OutputMatch = null,
    wallpaper: struct {
        default: Wallpaper,
        outputs: []const struct { match: OutputMatch, wallpaper: Wallpaper },
    },
    keybindings: []const struct { trigger: []const u8, action: []const []const u8, repeat: ?bool = null },
};
pub const Settings = struct {
    appearance: @FieldType(LegacySettings, "appearance"),
    compositor: std.json.Value,
    preferred_output: ?OutputMatch = null,
    wallpaper: @FieldType(LegacySettings, "wallpaper"),
};
pub const Section = enum { appearance, compositor, preferred_output, wallpaper };
pub const State = struct { version: u32, revision: []const u8, settings: Settings };
pub const LegacyState = struct { version: u32, revision: []const u8, settings: LegacySettings };
pub const defaults: Settings = .{
    .appearance = .{ .color_scheme = .default },
    .compositor = .{ .object = .empty },
    .wallpaper = .{ .default = .{ .fit = .fill, .fallback = .{ .r = 0, .g = 0, .b = 0 } }, .outputs = &.{} },
};

fn valid(ok: bool) !void {
    if (!ok) return error.InvalidSettings;
}
fn text(s: []const u8) bool {
    return s.len > 0 and std.mem.indexOfScalar(u8, s, 0) == null;
}
fn rgb(color: RGB) !void {
    inline for (.{ color.r, color.g, color.b }) |v| try valid(std.math.isFinite(v) and v >= 0 and v <= 1);
}
fn selector(m: OutputMatch) !void {
    if (m.name) |s| try valid(text(s));
}
fn wallpaper(w: Wallpaper) !void {
    try rgb(w.fallback);
    // Local absolute filesystem paths only. No URI decoding or I/O here.
    if (w.image) |s| try valid(text(s) and s[0] == '/');
}
pub fn validate(s: Settings) !void {
    // Ouro owns the contents, including action/device validation and merge
    // patch semantics. Do not turn null deletions into omitted preferences.
    try valid(s.compositor == .object);
    var it = s.compositor.object.iterator();
    while (it.next()) |entry| {
        const key = entry.key_ptr.*;
        try valid(std.mem.eql(u8, key, "general") or std.mem.eql(u8, key, "bindings") or std.mem.eql(u8, key, "input_rules") or std.mem.eql(u8, key, "output_rules"));
        try valid(entry.value_ptr.* == .object or entry.value_ptr.* == .null);
    }
    try validateLegacy(.{ .appearance = s.appearance, .outputs = &.{}, .preferred_output = s.preferred_output, .wallpaper = s.wallpaper, .keybindings = &.{} });
}
pub fn validateLegacy(s: LegacySettings) !void {
    if (s.appearance.accent) |v| try rgb(v);
    if (s.preferred_output) |v| try selector(v);
    for (s.outputs, 0..) |rule, i| {
        try valid(text(rule.name));
        for (s.outputs[0..i]) |prior| try valid(!std.mem.eql(u8, prior.name, rule.name));
        try selector(rule.match);
        if (rule.settings.mode) |m| try valid(m.width > 0 and m.height > 0);
        if (rule.settings.scale) |v| try valid(std.math.isFinite(v) and v > 0);
        if (rule.settings.icc_profile) |p| try valid(text(p) and p[0] == '/');
    }
    try wallpaper(s.wallpaper.default);
    for (s.wallpaper.outputs, 0..) |w, i| {
        try selector(w.match);
        try wallpaper(w.wallpaper);
        for (s.wallpaper.outputs[0..i]) |prior| try valid(!matchEqual(prior.match, w.match));
    }
    for (s.keybindings, 0..) |binding, i| {
        try valid(text(binding.trigger) and binding.action.len > 0);
        for (binding.action) |arg| try valid(std.mem.indexOfScalar(u8, arg, 0) == null);
        try valid(binding.action[0].len > 0);
        const key = try trigger(binding.trigger);
        for (s.keybindings[0..i]) |prior| {
            const other = try trigger(prior.trigger);
            try valid(key.mods != other.mods or !std.ascii.eqlIgnoreCase(key.key, other.key));
        }
    }
}
fn matchEqual(a: OutputMatch, b: OutputMatch) bool {
    inline for (std.meta.fields(OutputMatch)) |field| {
        const x = @field(a, field.name);
        const y = @field(b, field.name);
        if (x == null or y == null) {
            if ((x == null) != (y == null)) return false;
        } else if (comptime std.mem.eql(u8, field.name, "name")) {
            if (!std.mem.eql(u8, x.?, y.?)) return false;
        } else if (x.? != y.?) return false;
    }
    return true;
}
fn trigger(s: []const u8) !struct { mods: u4, key: []const u8 } {
    var parts = std.mem.splitScalar(u8, s, '+');
    var mods: u4 = 0;
    var part = parts.next().?;
    while (parts.next()) |next| {
        const bit: u4 = if (std.ascii.eqlIgnoreCase(part, "shift")) 1 else if (std.ascii.eqlIgnoreCase(part, "control") or std.ascii.eqlIgnoreCase(part, "ctrl")) 2 else if (std.ascii.eqlIgnoreCase(part, "alt")) 4 else if (std.ascii.eqlIgnoreCase(part, "super") or std.ascii.eqlIgnoreCase(part, "logo") or std.ascii.eqlIgnoreCase(part, "mod4")) 8 else return error.InvalidSettings;
        try valid(mods & bit == 0);
        mods |= bit;
        part = next;
    }
    try valid(text(part));
    return .{ .mods = mods, .key = part };
}

test "finite color and legacy normalized trigger ambiguity" {
    var s = defaults;
    s.appearance.accent = .{ .r = std.math.nan(f64), .g = 0, .b = 1 };
    try std.testing.expectError(error.InvalidSettings, validate(s));
    const legacy: LegacySettings = .{ .appearance = defaults.appearance, .outputs = &.{}, .wallpaper = defaults.wallpaper, .keybindings = &.{ .{ .trigger = "Ctrl+Logo+q", .action = &.{"close"} }, .{ .trigger = "Super+Control+Q", .action = &.{"exit"} } } };
    try std.testing.expectError(error.InvalidSettings, validateLegacy(legacy));
    try validate(defaults);
}
