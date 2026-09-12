const std = @import("std");
const schema = @import("schema.zig");
const V = std.json.Value;
const A = std.mem.Allocator;

fn json(a: A, text: []const u8) !V {
    return std.json.parseFromSliceLeaky(V, a, text, .{});
}
fn string(text: []const u8) V {
    return .{ .string = text };
}
fn put(a: A, v: *V, key: []const u8, value: V) !void {
    try v.object.put(a, key, value);
}

// Derive the typed desktop shapes from their owner. Semantic checks such as
// duplicate wallpaper selectors remain in schema.validate, not a second policy.
fn shape(comptime T: type, a: A) !V {
    if (T == V) return json(a,
        \\{"type":"object","additionalProperties":false,"properties":{"general":{"type":["object","null"]},"bindings":{"type":["object","null"]},"input_rules":{"type":["object","null"]},"output_rules":{"type":["object","null"]}}}
    );
    var result = try json(a, "{}");
    switch (@typeInfo(T)) {
        .optional => |o| {
            var choices: V = .{ .array = std.array_list.Managed(V).init(a) };
            try choices.array.append(try shape(o.child, a));
            try choices.array.append(try json(a, "{\"type\":\"null\"}"));
            try put(a, &result, "anyOf", choices);
        },
        .@"struct" => |s| {
            try put(a, &result, "type", string("object"));
            try put(a, &result, "additionalProperties", .{ .bool = false });
            var properties = try json(a, "{}");
            var required: V = .{ .array = std.array_list.Managed(V).init(a) };
            inline for (s.fields) |f| {
                try put(a, &properties, f.name, try shape(f.type, a));
                if (f.default_value_ptr == null) try required.array.append(string(f.name));
            }
            try put(a, &result, "properties", properties);
            try put(a, &result, "required", required);
        },
        .pointer => |p| if (p.child == u8) {
            try put(a, &result, "type", string("string"));
        } else {
            try put(a, &result, "type", string("array"));
            try put(a, &result, "items", try shape(p.child, a));
        },
        .int => {
            try put(a, &result, "type", string("integer"));
            try put(a, &result, "minimum", .{ .integer = std.math.minInt(T) });
            try put(a, &result, "maximum", .{ .integer = std.math.maxInt(T) });
        },
        .float => try put(a, &result, "type", string("number")),
        .bool => try put(a, &result, "type", string("boolean")),
        .@"enum" => |e| {
            try put(a, &result, "type", string("string"));
            var values: V = .{ .array = std.array_list.Managed(V).init(a) };
            inline for (e.fields) |f| try values.array.append(string(f.name));
            try put(a, &result, "enum", values);
        },
        else => @compileError("unsupported schema type"),
    }
    if (T == schema.RGB) {
        const props = result.object.getPtr("properties").?;
        for (props.object.values()) |*component| {
            try put(a, component, "minimum", .{ .integer = 0 });
            try put(a, component, "maximum", .{ .integer = 1 });
        }
    }
    if (T == schema.OutputMatch) {
        const name = result.object.getPtr("properties").?.object.getPtr("name").?;
        try put(a, &name.object.getPtr("anyOf").?.array.items[0], "pattern", string("^[^\\u0000]+$"));
    }
    if (T == schema.Wallpaper) {
        const image = result.object.getPtr("properties").?.object.getPtr("image").?;
        try put(a, &image.object.getPtr("anyOf").?.array.items[0], "pattern", string("^/[^\\u0000]*$"));
    }
    return result;
}

pub fn tools(a: A) !V {
    var output = try json(a,
        \\{"type":"object","oneOf":[{}, {"type":"object","additionalProperties":false,"required":["error"],"properties":{"error":{"type":"object","additionalProperties":false,"required":["code","message"],"properties":{"code":{"enum":["Conflict","InvalidParameters","PersistenceFailed"]},"message":{"type":"string"},"revision":{"type":"string"}},"if":{"properties":{"code":{"const":"Conflict"}}},"then":{"required":["revision"]}}}}]}
    );
    output.object.getPtr("oneOf").?.array.items[0] = try shape(struct { revision: []const u8, settings: schema.Settings }, a);
    var full = try shape(struct { expected_revision: []const u8, settings: schema.Settings }, a);
    try put(a, &full, "$schema", string("https://json-schema.org/draft/2020-12/schema"));
    var read = try shape(struct {}, a);
    try put(a, &read, "$schema", string("https://json-schema.org/draft/2020-12/schema"));
    var section = try json(a,
        \\{"$schema":"https://json-schema.org/draft/2020-12/schema","type":"object","additionalProperties":false,"required":["expected_revision","section"],"properties":{"expected_revision":{"type":"string"},"section":{"enum":["appearance","compositor","preferred_output","wallpaper"]},"value":{}},"oneOf":[]}
    );
    inline for (std.meta.fields(schema.Section)) |f| {
        var branch = try json(a, "{\"properties\":{\"section\":{}},\"required\":[]}");
        const props = branch.object.getPtr("properties").?;
        try put(a, props.object.getPtr("section").?, "const", string(f.name));
        try put(a, props, "value", try shape(@FieldType(schema.Settings, f.name), a));
        if (!std.mem.eql(u8, f.name, "preferred_output")) try branch.object.getPtr("required").?.array.append(string("value"));
        try section.object.getPtr("oneOf").?.array.append(branch);
    }
    var list: V = .{ .array = std.array_list.Managed(V).init(a) };
    inline for (.{
        .{ "settings.get", "Read all current desired settings and their global revision before preparing a replacement. Accepts no arguments and does not modify settings. The revision can be used as expected_revision for either settings write tool.", read },
        .{ "settings.set", "Replace all desired settings using the current global expected_revision. Persistence does not confirm compositor application. On Conflict, read and reconsider; never blindly replay a lost response.", full },
        .{ "settings.set_section", "Replace one complete section, not a merge patch. Omitted fields in that section are cleared. Only preferred_output accepts omitted/null value. Unrelated sections are retained; expected_revision is global. On Conflict, read and reconsider.", section },
    }) |entry| {
        var tool = try json(a, "{}");
        try put(a, &tool, "name", string(entry[0]));
        try put(a, &tool, "description", string(entry[1]));
        try put(a, &tool, "inputSchema", entry[2]);
        try put(a, &tool, "outputSchema", output);
        try list.array.append(tool);
    }
    return list;
}
