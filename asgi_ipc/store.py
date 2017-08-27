import posix_ipc
import mmap
import time
import contextlib
from six.moves import cPickle as pickle


class BaseMemoryStore(object):
    """
    Implements the base of shared memory stores, providing an mmap-ed shared
    memory area, a semaphore for controlling access to it, and common logic
    to mutate a pickled value in that memory area atomically.

    POSIX IPC Message Queues are not used as their default limits under most
    kernels are too small (8KB messages and 256 queues max); channels is a
    little... heavier than that.
    """

    # The number of seconds to wait for a lock before raising an error
    TIMEOUT = 10

    # The default value's factory
    DEFAULT_FACTORY = dict

    def __init__(self, path, memory_size):
        self.memory_size = memory_size
        self.path = path
        self.semaphore = posix_ipc.Semaphore(path, flags=posix_ipc.O_CREAT, initial_value=1)
        self.memory = posix_ipc.SharedMemory(path, flags=posix_ipc.O_CREAT, size=self.memory_size)
        self.mmap = mmap.mmap(self.memory.fd, self.memory.size)
        self.deadlock_error = RuntimeError(
            "Semaphore appears to be deadlocked. Kill all channels processes "
            "and remove files named like %s in /dev/shm" % self.path
        )

    @contextlib.contextmanager
    def mutate_value(self):
        """
        Allows mutation of the value safely.
        """
        # Get the semaphore with an emergency timeout to detect deadlock conditions
        try:
            self.semaphore.acquire(self.TIMEOUT)
        except posix_ipc.BusyError:
            raise self.deadlock_error
        try:
            # Load the value from the shared memory segment (if populated)
            self.mmap.seek(0)
            # Memory can be empty but have a length.  Pickle opcodes
            # starts at 0x80.  If we read zero, memory was not
            # initiated yet.
            if not self.mmap.read_byte():
                value = self.DEFAULT_FACTORY()
            else:
                self.mmap.seek(0)
                try:
                    value = pickle.load(self.mmap)
                except EOFError:
                    value = self.DEFAULT_FACTORY()
            # Let the inside run
            yield value
            # Dump the value back into the shared memory segment
            self.mmap.seek(0)
            pickle.dump(value, self.mmap, protocol=2)
        finally:
            # Release semaphore
            self.semaphore.release()

    def get_value(self):
        """
        Returns the value in the store safely, but does not allow safe mutation.
        """
        # Get the semaphore with an emergency timeout to detect deadlock conditions
        try:
            self.semaphore.acquire(self.TIMEOUT)
        except posix_ipc.BusyError:
            raise self.deadlock_error
        try:
            # Load the value from the shared memory segment (if populated)
            self.mmap.seek(0)
            try:
                value = pickle.load(self.mmap)
            except EOFError:
                value = self.DEFAULT_FACTORY()
            return value
        finally:
            # Release semaphore
            self.semaphore.release()

    def flush_all(self):
        # Get the semaphore with an emergency timeout to detect deadlock conditions
        try:
            self.semaphore.acquire(self.TIMEOUT)
        except posix_ipc.BusyError:
            raise self.deadlock_error
        try:
            # Just write over the mmap area
            self.mmap.seek(0)
            pickle.dump(self.DEFAULT_FACTORY(), self.mmap, protocol=2)
        finally:
            # Release semaphore
            self.semaphore.release()


class ChannelMemoryStore(BaseMemoryStore):
    """
    Implements a shared memory store that maps unicode strings to ordered
    channels of binary blobs with expiry times.
    """

    def append(self, name, item, expiry):
        """
        Adds a binary blob to the right of the channel.
        """
        with self.mutate_value() as value:
            value.setdefault(name, []).append((item, expiry))

    def pop(self, name):
        """
        Tries to pop an item off of the left of the channel, raising IndexError if
        no item was available to pop.
        """
        with self.mutate_value() as value:
            # See if the channel even exists
            if name not in value:
                raise IndexError("Channel %s is empty" % name)
            # Go through the channel until we find a nonexpired message
            for i, item_and_expiry in enumerate(value[name]):
                if item_and_expiry[1] >= time.time():
                    value[name] = value[name][i + 1:]
                    return item_and_expiry[0]
            # All messages are expired!
            del value[name]
            raise IndexError("Channel %s is empty" % name)

    def length(self, name):
        """
        Removes all expired items from the channel, and returns the
        number of messages in the channel.
        """
        with self.mutate_value() as value:
            new_contents = [
                (item, expiry)
                for item, expiry
                in value.get(name, [])
                if expiry >= time.time()
            ]
            if new_contents:
                value[name] = new_contents
                return len(new_contents)
            else:
                if name in value:
                    del value[name]
                return 0


class GroupMemoryStore(BaseMemoryStore):
    """
    Implements a shared memory store that maps unicode strings to sets
    of (unicode string, expiry time).
    """

    def add(self, name, item, expiry):
        """
        Adds a group member with an expiry time. If it already exists,
        update the expiry time.
        """
        with self.mutate_value() as value:
            value.setdefault(name, {})[item] = expiry

    def discard(self, name, item):
        """
        Removes a group member if it exists. If not, silently returns.
        """
        with self.mutate_value() as value:
            if name in value and item in value[name]:
                del value[name][item]

    def flush(self, name):
        """
        Removes all members from the group
        """
        with self.mutate_value() as value:
            if name in value:
                del value[name]

    def flush_expired(self, name):
        """
        Removes all members from the group who have expired, and returns the
        new list of members.
        """
        try:
            with self.mutate_value() as value:
                value[name] = {
                    item: expiry
                    for item, expiry in value[name].items()
                    if expiry >= time.time()
                }
        except KeyError:
            return []
        return value[name].keys()

