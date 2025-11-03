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
from multiprocessing import Process
from pathlib import Path

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
class IPCSpinLock:
    """
    Simple inter-process spinlock using a 1-byte shared-memory flag.
      0 = unlocked
      1 = locked
    The CAS operation sets 0 -> 1 atomically. Release sets the byte to 0 atomically.
    """
    def __init__(self, name: str, so_path: str = None):
        """
        name: name for shared memory file under /dev/shm (e.g. "mylock")
        so_path: optional path to prebuilt shared library; if None the module will compile one
        """
        self.name = name
        self.shm_path = f"/dev/shm/{name}.ipcflag"
        self.size = 1  # single byte
        self._ensure_shm_file()
        self.mm = self._open_mmap()
        # build/load C lib if needed
        if so_path is None:
            tmpdir = tempfile.mkdtemp(prefix="ipc_cas_")
            so_path = build_c_shared_lib(tmpdir)
        self.lib = ctypes.CDLL(so_path)
        # define argument/result types
        self.lib.cas_u8.argtypes = (ctypes.POINTER(ctypes.c_uint8), ctypes.c_uint8, ctypes.c_uint8)
        self.lib.cas_u8.restype = ctypes.c_int
        self.lib.store_u8.argtypes = (ctypes.POINTER(ctypes.c_uint8), ctypes.c_uint8)
        self.lib.store_u8.restype = None

        # Get pointer to the mmap buffer as uint8 pointer
        buf = (ctypes.c_uint8 * 1).from_buffer(self.mm)
        self.ptr = ctypes.cast(buf, ctypes.POINTER(ctypes.c_uint8))

    def _ensure_shm_file(self):
        """Create the backing file if it doesn't exist and set size to 1 byte."""
        fd = os.open(self.shm_path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            # Ensure size 1
            cur = os.fstat(fd).st_size
            if cur < self.size:
                os.ftruncate(fd, self.size)
                # initialize to 0
                os.write(fd, b'\x00')
                os.lseek(fd, 0, os.SEEK_SET)
        finally:
            os.close(fd)

    def _open_mmap(self):
        fd = os.open(self.shm_path, os.O_RDWR)
        try:
            mm = mmap.mmap(fd, self.size, mmap.MAP_SHARED, mmap.PROT_READ | mmap.PROT_WRITE)
            return mm
        finally:
            os.close(fd)

    def acquire(self, timeout=None):
        """
        Try to acquire the lock by atomically setting 0 -> 1 with CAS.
        Spins with exponential backoff.
        Returns True on acquired, False on timeout.
        """
        start = time.time()
        backoff = 0.00001  # 10us initial
        max_backoff = 0.01  # 10ms
        while True:
            # Attempt CAS: expected 0 -> desired 1
            res = self.lib.cas_u8(self.ptr, ctypes.c_uint8(0), ctypes.c_uint8(1))
            if res:
                # acquired
                return True
            # not acquired, check timeout
            if timeout is not None and (time.time() - start) >= timeout:
                return False
            # exponential backoff + jitter
            time.sleep(backoff * (0.5 + 0.5 * os.getpid() % 2))  # tiny jitter based on pid
            backoff = min(max_backoff, backoff * 2)

    def release(self):
        """Release the lock by atomically storing 0."""
        self.lib.store_u8(self.ptr, ctypes.c_uint8(0))

    def is_locked(self):
        """Non-atomic read of current flag (best-effort)."""
        self.mm.seek(0)
        b = self.mm.read(1)
        return b != b'\x00'

    def close(self):
        try:
            self.mm.close()
        except Exception:
            pass

# ---------------- Example usage with multiprocessing ----------------
def worker(name, so_path, worker_id, hold_time):
    lock = IPCSpinLock(name, so_path=so_path)
    print(f"[worker {worker_id}] trying to acquire")
    got = lock.acquire(timeout=10.0)
    if not got:
        print(f"[worker {worker_id}] failed to acquire (timeout)")
        return
    print(f"[worker {worker_id}] acquired, doing work for {hold_time:.2f}s")
    time.sleep(hold_time)
    lock.release()
    print(f"[worker {worker_id}] released")
    lock.close()

def demo():
    # Build the shared library once, pass path to each child
    tmpdir = tempfile.mkdtemp(prefix="ipc_cas_demo_")
    so_path = build_c_shared_lib(tmpdir)

    name = "demo_ipc_lock_example"
    # Ensure shm file removed before demo start (optional):
    shm_path = f"/dev/shm/{name}.ipcflag"
    try:
        os.remove(shm_path)
    except FileNotFoundError:
        pass

    # Start two processes that contend for the lock
    p1 = Process(target=worker, args=(name, so_path, 1, 2.0))
    p2 = Process(target=worker, args=(name, so_path, 2, 1.0))
    p1.start()
    time.sleep(0.1)  # stagger start so contention is likely
    p2.start()
    p1.join()
    p2.join()
    print("Demo finished.")

if __name__ == "__main__":
    demo()
