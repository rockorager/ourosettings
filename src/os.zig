const std = @import("std");
pub const c = @cImport({
    // glibc fortified variadic declarations cannot be translated by Zig 0.16.
    @cUndef("_FORTIFY_SOURCE");
    @cDefine("_GNU_SOURCE", "1");
    @cInclude("sys/socket.h");
    @cInclude("sys/un.h");
    @cInclude("sys/stat.h");
    @cInclude("sys/file.h");
    @cInclude("sys/random.h");
    @cInclude("fcntl.h");
    @cInclude("unistd.h");
    @cInclude("signal.h");
    @cInclude("errno.h");
    @cInclude("poll.h");
    @cInclude("time.h");
    @cInclude("stdlib.h");
    @cInclude("stdio.h");
});
pub const a = std.heap.smp_allocator;
pub fn check(ok: bool) !void {
    if (!ok) return error.SystemCallFailed;
}
pub fn z(s: []const u8) ![:0]u8 {
    if (std.mem.indexOfScalar(u8, s, 0) != null) return error.InvalidPath;
    return a.dupeZ(u8, s);
}
pub fn now() i64 {
    var t: c.timespec = undefined;
    _ = c.clock_gettime(c.CLOCK_MONOTONIC, &t);
    return t.tv_sec * 1000 + @divTrunc(t.tv_nsec, 1000000);
}
pub fn token() ![32]u8 {
    var bytes: [16]u8 = undefined;
    try check(c.getrandom(&bytes, bytes.len, 0) == bytes.len);
    return std.fmt.bytesToHex(bytes, .lower);
}
pub fn private(fd: c_int, kind: c.mode_t, mode: c.mode_t) !c.struct_stat {
    var st: c.struct_stat = undefined;
    try check(c.fstat(fd, &st) == 0);
    if (st.st_uid != c.geteuid() or st.st_mode & c.S_IFMT != kind or st.st_mode & 0o7777 != mode) return error.UnsafePermissions;
    if (kind == c.S_IFREG and st.st_nlink != 1) return error.UnsafeLink;
    return st;
}
// Open every component without following symlinks. Only the final directory
// must be private; ordinary /home and /tmp ancestors are allowed.
pub fn directory(path: []const u8) !c_int {
    if (path.len == 0 or path[0] != '/') return error.AbsolutePathRequired;
    var fd = c.open("/", c.O_RDONLY | c.O_DIRECTORY | c.O_CLOEXEC);
    try check(fd >= 0);
    errdefer _ = c.close(fd);
    var parts = std.mem.tokenizeScalar(u8, path, '/');
    while (parts.next()) |part| {
        if (std.mem.eql(u8, part, ".") or std.mem.eql(u8, part, "..")) return error.InvalidPath;
        const name = try z(part);
        defer a.free(name);
        if (c.mkdirat(fd, name, 0o700) == 0) {
            // Persist newly created directory entries, not just the state file.
            try check(c.fsync(fd) == 0);
        } else if (c.__errno_location().* != c.EEXIST) return error.SystemCallFailed;
        const next = c.openat(fd, name, c.O_RDONLY | c.O_DIRECTORY | c.O_NOFOLLOW | c.O_CLOEXEC);
        try check(next >= 0);
        _ = c.close(fd);
        fd = next;
    }
    _ = try private(fd, c.S_IFDIR, 0o700);
    return fd;
}
pub fn lock(dir: c_int, name: [:0]const u8) !c_int {
    const fd = c.openat(dir, name, c.O_RDWR | c.O_CREAT | c.O_NOFOLLOW | c.O_CLOEXEC | c.O_NONBLOCK, @as(c.mode_t, 0o600));
    try check(fd >= 0);
    errdefer _ = c.close(fd);
    _ = try private(fd, c.S_IFREG, 0o600);
    if (c.flock(fd, c.LOCK_EX | c.LOCK_NB) != 0) return error.AlreadyRunning;
    return fd;
}
pub fn writeAll(fd: c_int, bytes: []const u8) !void {
    var n: usize = 0;
    while (n < bytes.len) {
        const wrote = c.write(fd, bytes[n..].ptr, bytes.len - n);
        if (wrote < 0 and c.__errno_location().* == c.EINTR) continue;
        try check(wrote > 0);
        n += @intCast(wrote);
    }
}
