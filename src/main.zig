const std = @import("std");
const os = @import("os.zig");
const c = os.c;
const a = os.a;
const storage = @import("store.zig");
const schema = @import("schema.zig");
const mcp = @import("mcp.zig");
const limit = mcp.record_limit;
var stopped: c.sig_atomic_t = 0;
fn stop(_: c_int) callconv(.c) void {
    @as(*volatile c.sig_atomic_t, &stopped).* = 1;
}

const Connection = struct {
    fd: c_int = -1,
    input: []u8 = &.{},
    used: usize = 0,
    output: ?[]u8 = null,
    sent: usize = 0,
    deadline: i64 = 0,
    request_deadline: i64 = 0,
    closing: bool = false,
    peer: mcp.Peer = .{},

    pub fn close(self: *Connection) void {
        if (self.fd >= 0) _ = c.close(self.fd);
        if (self.output) |bytes| a.free(bytes);
        self.peer.deinit();
        a.free(self.input);
        self.* = .{};
    }
    pub fn reply(self: *Connection, value: anytype) !void {
        const bytes = try std.json.Stringify.valueAlloc(a, value, storage.json_options);
        defer a.free(bytes);
        try self.replyBytes(bytes);
    }
    pub fn replyBytes(self: *Connection, bytes: []const u8) !void {
        if (bytes.len >= limit) return error.ReplyTooLarge;
        if (self.output) |prior| {
            const remaining = prior.len - self.sent;
            if (remaining + bytes.len + 1 > limit * 4) return error.ReplyTooLarge;
            const joined = try a.alloc(u8, remaining + bytes.len + 1);
            @memcpy(joined[0..remaining], prior[self.sent..]);
            @memcpy(joined[remaining..][0..bytes.len], bytes);
            joined[joined.len - 1] = '\n';
            a.free(prior);
            self.output = joined;
            self.sent = 0;
            // Appending output must not extend the oldest queued deadline.
            return;
        }
        self.output = try a.alloc(u8, bytes.len + 1);
        @memcpy(self.output.?[0..bytes.len], bytes);
        self.output.?[bytes.len] = '\n';
        self.sent = 0;
        self.deadline = os.now() + 30_000;
    }
};

const Service = struct {
    store: *storage.Store,
    listener: c_int,
    clients: [32]Connection = @splat(.{}),

    fn publish(self: *Service) !void {
        for (&self.clients) |*subscriber| {
            if (subscriber.fd < 0) continue;
            subscriber.peer.notify(subscriber, self.store) catch |err| switch (err) {
                error.ReplyTooLarge => subscriber.close(),
                else => return err,
            };
        }
    }

    fn request(self: *Service, client: *Connection, bytes: []const u8) !void {
        if (try client.peer.request(client, self.store, bytes)) try self.publish();
    }

    fn loop(self: *Service, idle_ms: i64) !void {
        var idle_since = os.now();
        while (@as(*volatile c.sig_atomic_t, &stopped).* == 0) {
            var polls: [33]c.pollfd = undefined;
            polls[0] = .{ .fd = self.listener, .events = c.POLLIN, .revents = 0 };
            var count: usize = 0;
            for (&self.clients, 0..) |*client, i| {
                const active_deadline = !client.peer.listening() or client.output != null;
                const partial_expired = client.used != 0 and os.now() >= client.request_deadline;
                if (client.fd >= 0 and (partial_expired or (active_deadline and os.now() >= client.deadline))) client.close();
                if (client.fd >= 0) count += 1;
                polls[i + 1] = .{ .fd = client.fd, .events = if (client.output != null) c.POLLOUT else c.POLLIN, .revents = 0 };
            }
            if (count != 0) idle_since = os.now();
            if (count == 0 and os.now() - idle_since >= idle_ms) return;
            const result = c.poll(&polls, polls.len, 20);
            if (result < 0) {
                if (c.__errno_location().* == c.EINTR) continue;
                return error.PollFailed;
            }
            for (&self.clients, 0..) |*client, i| {
                if (client.fd < 0) continue;
                const events = polls[i + 1].revents;
                if (events & (c.POLLERR | c.POLLNVAL | c.POLLHUP) != 0) {
                    client.close();
                    continue;
                }
                if (client.output) |bytes| {
                    if (events & c.POLLOUT != 0) {
                        const sent = c.send(client.fd, bytes[client.sent..].ptr, bytes.len - client.sent, c.MSG_NOSIGNAL);
                        if (sent > 0) {
                            client.sent += @intCast(sent);
                            if (client.sent == bytes.len) {
                                a.free(bytes);
                                client.output = null;
                                client.deadline = os.now() + 30_000;
                                if (client.closing) {
                                    client.close();
                                    continue;
                                }
                            }
                        } else if (sent == 0 or (c.__errno_location().* != c.EAGAIN and c.__errno_location().* != c.EINTR)) {
                            client.close();
                            continue;
                        }
                    }
                } else if (events & c.POLLIN != 0) {
                    const got = c.recv(client.fd, client.input[client.used..].ptr, client.input.len - client.used, 0);
                    if (got > 0) {
                        if (client.used == 0) client.request_deadline = os.now() + 30_000;
                        if (client.used == 0 and client.peer.listening()) client.deadline = os.now() + 30_000;
                        client.used += @intCast(got);
                    } else if (got == 0 or (c.__errno_location().* != c.EAGAIN and c.__errno_location().* != c.EINTR)) {
                        client.close();
                        continue;
                    }
                }
                if (client.output == null and client.used > 0) {
                    if (std.mem.indexOfScalar(u8, client.input[0..client.used], '\n')) |end| {
                        self.request(client, client.input[0..end]) catch |err| switch (err) {
                            error.ReplyTooLarge => client.close(),
                            else => return err,
                        };
                        if (client.fd < 0) continue;
                        std.mem.copyForwards(u8, client.input[0..], client.input[end + 1 .. client.used]);
                        client.used -= end + 1;
                        if (client.output == null) client.deadline = os.now() + 30_000;
                    } else if (client.used == limit) {
                        _ = try mcp.rpcError(client, .null, -32600, "Record exceeds 256 KiB");
                        client.closing = true;
                    }
                }
            }
            {
                if (polls[0].revents & c.POLLIN != 0) for (0..16) |_| {
                    const fd = c.accept4(self.listener, .{ .__sockaddr__ = null }, null, c.SOCK_NONBLOCK | c.SOCK_CLOEXEC);
                    if (fd < 0) break;
                    var credentials: c.struct_ucred = undefined;
                    var size: c.socklen_t = @sizeOf(c.struct_ucred);
                    if (c.getsockopt(fd, c.SOL_SOCKET, c.SO_PEERCRED, &credentials, &size) != 0 or size != @sizeOf(c.struct_ucred) or credentials.uid != c.geteuid()) {
                        _ = c.close(fd);
                        continue;
                    }
                    var accepted = false;
                    for (&self.clients) |*client| if (client.fd < 0) {
                        const input = a.alloc(u8, limit) catch {
                            _ = c.close(fd);
                            return error.OutOfMemory;
                        };
                        client.* = .{ .fd = fd, .input = input, .deadline = os.now() + 30_000 };
                        accepted = true;
                        break;
                    };
                    if (!accepted) _ = c.close(fd);
                };
            }
        }
    }
};

const Listener = struct {
    fd: c_int,
    dir: c_int,
    lock_fd: c_int,
    name: [:0]u8,
    inode: c.struct_stat,
    activated: bool,

    fn init(path: []const u8, inherited: ?c_int) !Listener {
        const dir = try os.directory(std.fs.path.dirname(path) orelse return error.InvalidPath);
        errdefer _ = c.close(dir);
        const name = try os.z(std.fs.path.basename(path));
        errdefer a.free(name);
        const lock_name = try std.fmt.allocPrintSentinel(a, "{s}.lock", .{name}, 0);
        defer a.free(lock_name);
        const lock_fd = try os.lock(dir, lock_name);
        errdefer _ = c.close(lock_fd);
        if (std.mem.indexOfScalar(u8, path, 0) != null) return error.InvalidPath;
        var address: c.struct_sockaddr_un = std.mem.zeroes(c.struct_sockaddr_un);
        address.sun_family = c.AF_UNIX;
        if (path.len >= address.sun_path.len) return error.SocketPathTooLong;
        @memcpy(@as([*]u8, @ptrCast(&address.sun_path))[0..path.len], path);
        const activated = inherited != null;
        const fd: c_int = inherited orelse c.socket(c.AF_UNIX, c.SOCK_STREAM | c.SOCK_NONBLOCK | c.SOCK_CLOEXEC, 0);
        try os.check(fd >= 0);
        errdefer _ = c.close(fd);
        var bound = false;
        errdefer if (bound) {
            _ = c.unlinkat(dir, name, 0);
        };
        if (activated) {
            var actual: c.struct_sockaddr_un = std.mem.zeroes(c.struct_sockaddr_un);
            var size: c.socklen_t = @sizeOf(c.struct_sockaddr_un);
            var accepting: c_int = 0;
            var kind: c_int = 0;
            var option_size: c.socklen_t = @sizeOf(c_int);
            if (c.getsockname(fd, .{ .__sockaddr_un__ = &actual }, &size) != 0 or size > @sizeOf(c.struct_sockaddr_un) or actual.sun_family != c.AF_UNIX or !std.mem.eql(u8, std.mem.sliceTo(&actual.sun_path, 0), path)) return error.InvalidActivation;
            if (c.getsockopt(fd, c.SOL_SOCKET, c.SO_ACCEPTCONN, &accepting, &option_size) != 0 or accepting != 1) return error.InvalidActivation;
            if (c.getsockopt(fd, c.SOL_SOCKET, c.SO_TYPE, &kind, &option_size) != 0 or kind != c.SOCK_STREAM) return error.InvalidActivation;
        } else {
            // Existing live, stale, foreign, symlink, and non-socket paths are
            // all left untouched. Recovery from a crash is an operator action.
            if (c.bind(fd, .{ .__sockaddr_un__ = &address }, @sizeOf(c.struct_sockaddr_un)) != 0) return error.BindFailed;
            bound = true;
            try os.check(c.fchmodat(dir, name, 0o600, 0) == 0);
            try os.check(c.listen(fd, 32) == 0);
        }
        var inode: c.struct_stat = undefined;
        if (c.fstatat(dir, name, &inode, c.AT_SYMLINK_NOFOLLOW) != 0 or inode.st_uid != c.geteuid() or inode.st_mode & c.S_IFMT != c.S_IFSOCK or inode.st_mode & 0o7777 != 0o600) return error.UnsafeSocket;
        try os.check(c.fcntl(fd, c.F_SETFL, @as(c_int, c.O_NONBLOCK)) == 0);
        try os.check(c.fcntl(fd, c.F_SETFD, @as(c_int, c.FD_CLOEXEC)) == 0);
        return .{ .fd = fd, .dir = dir, .lock_fd = lock_fd, .name = name, .inode = inode, .activated = activated };
    }
    fn deinit(self: *Listener) void {
        _ = c.close(self.fd);
        if (!self.activated) {
            var current: c.struct_stat = undefined;
            if (c.fstatat(self.dir, self.name, &current, c.AT_SYMLINK_NOFOLLOW) == 0 and current.st_ino == self.inode.st_ino and current.st_dev == self.inode.st_dev) _ = c.unlinkat(self.dir, self.name, 0);
        }
        _ = c.close(self.lock_fd);
        _ = c.close(self.dir);
        a.free(self.name);
    }
};

// Check fd 3 before opening directories/locks so a missing inherited fd cannot
// accidentally become a newly opened descriptor. Listener.init checks its path,
// listening state, socket type and ownership before accepting clients.
fn activation(environ: std.process.Environ.Map) !?c_int {
    const fds = environ.get("LISTEN_FDS");
    const pid = environ.get("LISTEN_PID");
    if (fds == null and pid == null) return null;
    if (fds == null or pid == null or (std.fmt.parseInt(c.pid_t, pid.?, 10) catch return error.InvalidActivation) != c.getpid()) return error.InvalidActivation;
    const count = std.fmt.parseInt(usize, fds.?, 10) catch return error.InvalidActivation;
    if (count != 1 or c.fcntl(3, c.F_GETFD) < 0) return error.InvalidActivation;
    return 3;
}

pub fn main(init: std.process.Init) void {
    run(init) catch |err| {
        std.debug.print("ourosettings: {s}\n", .{@errorName(err)});
        std.process.exit(1);
    };
}
fn run(init: std.process.Init) !void {
    _ = c.umask(0o077);
    _ = c.signal(c.SIGTERM, stop);
    _ = c.signal(c.SIGINT, stop);
    _ = c.signal(c.SIGXFSZ, c.SIG_IGN);
    const args = try init.minimal.args.toSlice(init.arena.allocator());
    var socket_path: ?[]const u8 = null;
    var state_path: ?[]const u8 = null;
    var idle_ms: i64 = 30_000;
    var i: usize = 1;
    while (i < args.len) : (i += 1) {
        if (std.mem.eql(u8, args[i], "--help")) return os.writeAll(1, "ourosettings [--socket ABSOLUTE_PATH] [--state ABSOLUTE_PATH] [--idle-ms 1..300000] [--export-mcp-descriptor]\n" ++
            "Desired settings only: does not configure or signal Ouro.\n" ++
            "MCP: $XDG_RUNTIME_DIR/ouro/settings.mcp.sock.\n" ++
            "Accepts one validated systemd listener at fd 3.\n" ++
            "State: $XDG_CONFIG_HOME/ouro/settings.json, fallback $HOME/.config/ouro/settings.json.\n" ++
            "Explicit paths isolate tests; same-UID credentials and private permissions always apply.\n");
        if (std.mem.eql(u8, args[i], "--export-mcp-descriptor")) {
            try os.writeAll(1, try mcp.descriptor(init.arena.allocator()));
            return os.writeAll(1, "\n");
        }
        if (i + 1 >= args.len) return error.UnknownOption;
        if (std.mem.eql(u8, args[i], "--socket")) socket_path = args[i + 1] else if (std.mem.eql(u8, args[i], "--state")) state_path = args[i + 1] else if (std.mem.eql(u8, args[i], "--idle-ms")) idle_ms = try std.fmt.parseInt(i64, args[i + 1], 10) else return error.UnknownOption;
        i += 1;
    }
    if (idle_ms < 1 or idle_ms > 300_000) return error.InvalidTimeout;
    const arena = init.arena.allocator();
    if (socket_path == null) {
        const runtime = init.minimal.environ.getPosix("XDG_RUNTIME_DIR") orelse return error.MissingRuntimeDirectory;
        const root = try os.directory(runtime);
        _ = c.close(root);
        socket_path = try std.fmt.allocPrint(arena, "{s}/ouro/settings.mcp.sock", .{runtime});
    }
    if (state_path == null) {
        const config = init.minimal.environ.getPosix("XDG_CONFIG_HOME");
        const base = if (config != null and config.?.len != 0) config.? else try std.fmt.allocPrint(arena, "{s}/.config", .{init.minimal.environ.getPosix("HOME") orelse return error.MissingHome});
        state_path = try std.fmt.allocPrint(arena, "{s}/ouro/settings.json", .{base});
    }
    var env = try init.minimal.environ.createMap(a);
    defer env.deinit();
    const inherited = try activation(env);
    var listener = try Listener.init(socket_path.?, inherited);
    defer listener.deinit();
    var store = try storage.Store.init(state_path.?);
    defer store.deinit();
    var service: Service = .{ .store = &store, .listener = listener.fd };
    defer for (&service.clients) |*client| client.close();
    try service.loop(idle_ms);
}
test {
    _ = schema;
}
