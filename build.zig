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
    module.addAnonymousImport("settings_idl", .{ .root_source_file = b.path("protocol/dev.rockorager.ouro.Settings.varlink") });
    module.addAnonymousImport("service_idl", .{ .root_source_file = b.path("protocol/org.varlink.service.varlink") });
    const exe = b.addExecutable(.{ .name = "ourosettings", .root_module = module });
    b.installArtifact(exe);
    const tests = b.addTest(.{ .root_module = module });
    const run_tests = b.addRunArtifact(tests);
    const integration = b.addSystemCommand(&.{ "python3", "tests/integration.py" });
    integration.addArtifactArg(exe);
    const test_step = b.step("test", "Run schema and real Unix daemon integration tests");
    test_step.dependOn(&run_tests.step);
    test_step.dependOn(&integration.step);
}
