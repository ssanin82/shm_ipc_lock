import os
import time
import tempfile
from multiprocessing import Process

from shm_ipc_lock import ShmLock, build_c_shared_lib

"""
Demo script for the IPC spinlock with shared memory offset.
"""

def worker(name, offset, so_path, worker_id, hold_time):
    lock = ShmLock(name, offset=offset, create=False, so_path=so_path)
    print(f"[worker {worker_id}] trying to acquire...")
    if not lock.acquire(timeout=5.0):
        print(f"[worker {worker_id}] timeout waiting for lock")
        return
    print(f"[worker {worker_id}] acquired -> working {hold_time:.1f}s")
    time.sleep(hold_time)
    lock.release()
    print(f"[worker {worker_id}] released")
    lock.close()

def main():
    shm_path = "/dev/shm/ipc_region_demo"
    offset = 128  # place the lock flag at byte offset 128
    tmpdir = tempfile.mkdtemp(prefix="ipc_demo_")
    so_path = build_c_shared_lib(tmpdir)

    # Create a shared memory region large enough
    fd = os.open(shm_path, os.O_RDWR | os.O_CREAT, 0o600)
    os.ftruncate(fd, offset + 1)
    os.close(fd)

    print(f"Shared memory: {shm_path} (flag at offset {offset})")

    # Start processes sharing the same region
    p1 = Process(target=worker, args=(shm_path, offset, so_path, 1, 2.0))
    p2 = Process(target=worker, args=(shm_path, offset, so_path, 2, 1.0))
    p1.start()
    time.sleep(0.1)
    p2.start()
    p1.join()
    p2.join()
    print("Demo complete.")

if __name__ == "__main__":
    main()
