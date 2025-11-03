# A Simple Atomic CAS-Based Spin Lock for Python IPC 

Python's multiprocessing.shared_memory module, introduced in Python 3.8, provides a way to implement named shared memory for inter-process communication. It is somewhat analogous to the POSIX shared memory API.

However, Python's standard library does not directly support named semaphores based on the POSIX sem_open concept for inter-process synchronization by name. This is a very serious inconvenience when you work with shared memory and need to synchronize data access to this memory from multiple processes.

As one solution [pypi.org](pypi.org) has the following library: https://pypi.org/project/named_semaphores/

This repo is a very simplified version of just a spin lock, based on the compare and swap operation. It is using a 1 byte flag in shared memory at a given memory offset offest.

The need for something like this appeared when I was writing trading strategies that were somewhat pushing Python's performance boundaries. I needed to share the whole strategy's order cache (locally stored list of orders that are still open in the market) across processes of my market making strategy, each one serving it's own order book depth. I couldn't trust the standard Python IPC API because it is not designed for low-latency applications.
