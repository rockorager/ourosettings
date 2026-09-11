const std = @import("std");
pub fn build(b: *std.Build) void {
    const target = b.standardTargetOptions(.{});
    const optimize = b.standardOptimizeOption(.{});
    const module = b.createModule(.{
        .root_source_file = b.path("src/main.zig"),
        .target = target,
        .optimize = optimize,
        .link_libc = true,
    });
    const exe = b.addExecutable(.{ .name = "ourosettings", .root_module = module });
    b.installArtifact(exe);
    const export_mcp = b.addRunArtifact(exe);
    export_mcp.addArg("--export-mcp-descriptor");
    const descriptor = b.addInstallFile(export_mcp.captureStdOut(.{}), "share/ouro/mcp/apps/ourosettings.json");
    b.getInstallStep().dependOn(&descriptor.step);
    const tests = b.addTest(.{ .root_module = module });
    const run_tests = b.addRunArtifact(tests);
    const integration = b.addSystemCommand(&.{ "python3", "tests/integration.py" });
    integration.addArtifactArg(exe);
    const mcp = b.addSystemCommand(&.{ "python3", "tests/mcp.py" });
    mcp.addArtifactArg(exe);
    const test_step = b.step("test", "Run schema and real Unix daemon integration tests");
    test_step.dependOn(&run_tests.step);
    test_step.dependOn(&integration.step);
    test_step.dependOn(&mcp.step);
}
