// Test-only LD_PRELOAD shim. No test fault hooks or credential bypass in daemon.
#define _GNU_SOURCE
#include <dlfcn.h>
#include <errno.h>
#include <fcntl.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>

static int fault(const char *kind) {
    const char *path = getenv("OURO_TEST_FAULT_FILE");
    if (!path) return 0;
    int fd = open(path, O_RDONLY);
    if (fd < 0) return 0;
    char value[32] = {0};
    ssize_t n = read(fd, value, sizeof(value) - 1);
    close(fd);
    return n > 0 && strcmp(value, kind) == 0;
}
ssize_t write(int fd, const void *buf, size_t n) {
    struct stat st;
    if (fstat(fd, &st) == 0 && S_ISREG(st.st_mode) && fault("write")) {
        errno = ENOSPC;
        return -1;
    }
    ssize_t (*real)(int, const void *, size_t) = dlsym(RTLD_NEXT, "write");
    return real(fd, buf, n);
}
int fsync(int fd) {
    struct stat st;
    if (fstat(fd, &st) == 0 && fault(S_ISDIR(st.st_mode) ? "directory-fsync" : "file-fsync")) {
        errno = EIO;
        return -1;
    }
    int (*real)(int) = dlsym(RTLD_NEXT, "fsync");
    return real(fd);
}
int renameat(int olddir, const char *old, int newdir, const char *new) {
    if (fault("rename")) {
        errno = EIO;
        return -1;
    }
    int (*real)(int, const char *, int, const char *) = dlsym(RTLD_NEXT, "renameat");
    return real(olddir, old, newdir, new);
}
