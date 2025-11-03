#!/usr/bin/env python3
"""
IPC spin-lock using a 1-byte flag in POSIX shared memory (/dev/shm) + CAS (compare-and-swap).
This script compiles a tiny C shared library at runtime that exposes atomic CAS and atomic store
for a uint8_t pointer, and uses ctypes to call it on the mmap'ed shared memory.
"""

import os
import tempfile
import mmap
import ctypes
import subprocess
import time

# ---------- C code for atomic CAS and store ----------
C_CODE = r'''
#include <stdint.h>
#include <stdatomic.h>

#ifdef __cplusplus
extern "C" {
#endif

// Use GCC/clang builtin for atomic compare-exchange for portability across compilers.
int cas_u8(uint8_t *ptr, uint8_t expected, uint8_t desired) {
    // __atomic_compare_exchange_n returns true(1) on success, false(0) on failure.
    return __atomic_compare_exchange_n(ptr, &expected, desired, 0, __ATOMIC_SEQ_CST, __ATOMIC_SEQ_CST);
}

void store_u8(uint8_t *ptr, uint8_t value) {
    __atomic_store_n(ptr, value, __ATOMIC_SEQ_CST);
}

#ifdef __cplusplus
}
#endif
'''

def build_c_shared_lib(tmpdir: str):
    """Write C code and compile a shared object. Returns path to .so"""
    c_path = os.path.join(tmpdir, "atomic_cas.c")
    so_path = os.path.join(tmpdir, "libatomic_cas.so")
    with open(c_path, "w") as f:
        f.write(C_CODE)
    # Compile with gcc
    cmd = ["gcc", "-std=gnu11", "-shared", "-fPIC", "-O2", c_path, "-o", so_path]
    subprocess.check_call(cmd)
    return so_path

# ---------- IPC Spinlock class ----------
class ShmLock:
    """
    Inter-process spinlock using a flag byte at a specific offset in shared memory.
    If shared memory file does not exist and create=True, it will be created.

    Args:
        shm_path (str): Path to shared memory file, e.g. /dev/shm/myregion
        offset (int): Byte offset in shared memory to use for the flag
        create (bool): If True, create file if it doesn't exist
        so_path (str): Optional path to prebuilt atomic CAS .so
    """
    def __init__(
        self,
        shm_path: str,
        offset: int = 0,
        create: bool = True,
        so_path: str = None
    ):
        self.shm_path = shm_path
        self.offset = offset
        self.size = offset + 1  # we need at least 1 byte past the offset
        self._ensure_shm_file(create)
        self.mm = self._open_mmap()

        # Build or load atomic CAS library
        if so_path is None:
            tmpdir = tempfile.mkdtemp(prefix="ipc_cas_")
            so_path = build_c_shared_lib(tmpdir)
        self.lib = ctypes.CDLL(so_path)
        self.lib.cas_u8.argtypes = (ctypes.POINTER(ctypes.c_uint8), ctypes.c_uint8, ctypes.c_uint8)
        self.lib.cas_u8.restype = ctypes.c_int
        self.lib.store_u8.argtypes = (ctypes.POINTER(ctypes.c_uint8), ctypes.c_uint8)
        self.lib.store_u8.restype = None

        # Get pointer to the byte at the specified offset
        buf = (ctypes.c_uint8 * 1).from_buffer(self.mm, self.offset)
        self.ptr = ctypes.cast(buf, ctypes.POINTER(ctypes.c_uint8))

    def _ensure_shm_file(self, create: bool):
        """Ensure shared memory file exists and is large enough."""
        flags = os.O_RDWR | (os.O_CREAT if create else 0)
        fd = os.open(self.shm_path, flags, 0o600)
        try:
            st = os.fstat(fd)
            if st.st_size < self.size:
                if not create:
                    raise ValueError(f"Shared memory {self.shm_path} too small for offset {self.offset}")
                os.ftruncate(fd, self.size)
                os.lseek(fd, 0, os.SEEK_SET)
        finally:
            os.close(fd)

    def _open_mmap(self):
        fd = os.open(self.shm_path, os.O_RDWR)
        try:
            return mmap.mmap(fd, self.size, mmap.MAP_SHARED, mmap.PROT_READ | mmap.PROT_WRITE)
        finally:
            os.close(fd)

    def acquire(self, timeout=None):
        """
        Try to acquire lock (0 -> 1). Returns True on success, False on timeout.
        """
        start = time.time()
        backoff = 0.00001
        while True:
            res = self.lib.cas_u8(self.ptr, ctypes.c_uint8(0), ctypes.c_uint8(1))
            if res:
                return True
            if timeout and (time.time() - start) >= timeout:
                return False
            time.sleep(backoff)
            backoff = min(0.01, backoff * 2)

    def release(self):
        """Release the lock by atomically storing 0."""
        self.lib.store_u8(self.ptr, ctypes.c_uint8(0))

    def is_locked(self):
        """Check if flag is nonzero (non-atomic read - best effort)."""
        self.mm.seek(self.offset)
        return self.mm.read(1) != b'\x00'

    def close(self):
        try:
            self.mm.close()
        except Exception:
            pass
