from __future__ import unicode_literals
import mmap
import msgpack
import os
import pkg_resources
import posix_ipc
import random
import six
import string
import struct
import threading
import time
from asgiref.base_layer import BaseChannelLayer


__version__ = pkg_resources.require('asgi_ipc')[0].version
MB = 1024 * 1024


class IPCChannelLayer(BaseChannelLayer):
    """
    Posix IPC backed channel layer, using the posix_ipc module's shared memory
    and sempahore components.

    It uses mmap'd shared memory areas to store msgpack'd versions of the
    datastructures, with a semaphore as a read/write lock to control access
    to the data area (all operations currently lock the entire memory segment).

    POSIX IPC Message Queues are not used as their default limits under most
    kernels are too small (8KB messages and 256 queues max); channels is a
    little... heavier than that.
    """

    def __init__(self, prefix="asgi", expiry=60, group_expiry=86400, capacity=10, channel_capacity=None, channel_memory=100*MB, group_memory=20*MB):
        super(IPCChannelLayer, self).__init__(
            expiry=expiry,
            group_expiry=group_expiry,
            capacity=capacity,
            channel_capacity=channel_capacity,
        )
        self.thread_lock = threading.Lock()
        self.prefix = prefix
        self.channel_store = MemoryDict("/%s-channel-dict" % self.prefix, size=channel_memory)
        # Set containing all groups to flush
        self.group_store = MemoryDict("/%s-group-dict" % self.prefix, size=group_memory)

    ### ASGI API ###

    extensions = ["flush", "groups"]

    def send(self, channel, message):
        # Typecheck
        assert isinstance(message, dict), "message is not a dict"
        assert self.valid_channel_name(channel), "channel name not valid"
        # Write message into the correct message queue
        with self.thread_lock:
            channel_list = self.channel_store.get(channel, [])
            if len(channel_list) >= self.get_capacity(channel):
                raise self.ChannelFull
            else:
                channel_list.append([message, time.time() + self.expiry])
                self.channel_store[channel] = channel_list

    def receive_many(self, channels, block=False):
        if not channels:
            return None, None
        channels = list(channels)
        assert all(self.valid_channel_name(channel) for channel in channels), "one or more channel names invalid"
        random.shuffle(channels)
        # Try to pop off all of the named channels
        with self.thread_lock:
            for channel in channels:
                channel_list = self.channel_store.get(channel, [])
                # Keep looping on the channel until we hit no messages or an unexpired one
                while True:
                    try:
                        # Popleft equivalent
                        message, expires = channel_list[0]
                        channel_list = channel_list[1:]
                        self.channel_store[channel] = channel_list
                        if expires <= time.time():
                            continue
                        return channel, message
                    except IndexError:
                        break
                    # If the channel is now empty, delete its key
                    if not channel_list and channel in self.channel_store:
                        del self.channel_store[channel]
        return None, None

    def new_channel(self, pattern):
        assert isinstance(pattern, six.text_type)
        # Keep making channel names till one isn't present.
        while True:
            random_string = "".join(random.choice(string.ascii_letters) for i in range(12))
            assert pattern.endswith("!") or pattern.endswith("?")
            new_name = pattern + random_string
            # To see if it's present we open the queue without O_CREAT
            with self.thread_lock:
                if new_name not in self.channel_store:
                    return new_name
                else:
                    continue

    ### Groups extension ###

    def group_add(self, group, channel):
        """
        Adds the channel to the named group
        """
        assert self.valid_group_name(group), "Invalid group name"
        with self.thread_lock:
            group_dict = self.group_store.get(group, {})
            group_dict[channel] = time.time() + self.group_expiry
            self.group_store[group] = group_dict

    def group_discard(self, group, channel):
        """
        Removes the channel from the named group if it is in the group;
        does nothing otherwise (does not error)
        """
        assert self.valid_group_name(group), "Invalid group name"
        with self.thread_lock:
            group_dict = self.group_store.get(group, {})
            if channel in group_dict:
                del group_dict[channel]
                if not group_dict:
                    del self.group_store[group]
                else:
                    self.group_store[group] = group_dict

    def send_group(self, group, message):
        """
        Sends a message to the entire group.
        """
        assert self.valid_group_name(group), "Invalid group name"
        with self.thread_lock:
            group_dict = self.group_store.get(group, {})
        for channel, expires in list(group_dict.items()):
            if expires <= time.time():
                del group_dict[channel]
                with self.thread_lock:
                    self.group_store[group] = group_dict
            else:
                try:
                    self.send(channel, message)
                except self.ChannelFull:
                    pass

    ### Flush extension ###

    def flush(self):
        """
        Deletes all messages and groups.
        """
        with self.thread_lock:
            self.channel_store.flush()
            self.group_store.flush()

    def __str__(self):
        return "%s(hosts=%s)" % (self.__class__.__name__, self.hosts)


class MemoryDatastructure(object):
    """
    Generic memory datastructure class; used for sets for flush tracking,
    dicts for group membership, and lists for channels.
    """

    # Maximum size of the datastructure. Try and override to not use up memory.
    size = 1024 * 1024 * 5

    # How long to wait for the semaphore before declaring deadlock and flushing
    death_timeout = 2

    # Datatype to store in here
    datatype = dict

    # Version signature - 8 bytes.
    signature = None

    def __init__(self, path, size=None):
        if self.signature is None:
            raise ValueError("No signature for this memory datastructure")
        if size:
            self.size = size
        self.path = path
        # TODO: Investigate having separate read and write locks to allow
        # concurrent reads.
        self.semaphore = posix_ipc.Semaphore(
            self.path + "-semaphore",
            flags=posix_ipc.O_CREAT,
            mode=0o660,
            initial_value=1,
        )
        try:
            self.shm = posix_ipc.SharedMemory(
                self.path,
                flags=posix_ipc.O_CREAT,
                mode=0o660,
                size=self.size,
            )
        except ValueError as e:
            raise ValueError(
                "Unable to allocate shared memory segment (potentially out of memory).\n" +
                "Error was: %s" % e
            )
        self.mmap = mmap.mmap(self.shm.fd, self.size)

    @classmethod
    def exists(cls, path):
        """
        Returns True if this already exists.
        """
        # Open without O_CREAT so it doesn't make if not exist
        try:
            posix_ipc.SharedMemory(path)
        except posix_ipc.ExistentialError:
            return False
        else:
            return True

    def _get_value(self):
        # TODO: Look into version number for contents coupled with internal
        # cache to avoid re-reading mmap for every access
        try:
            self.semaphore.acquire(self.death_timeout)
        except posix_ipc.BusyError:
            self._reset()
            self.semaphore.acquire(0)
        try:
            # Seek to start of memory segment
            self.mmap.seek(0)
            # The first four bytes should be "ASGD", followed by four bytes
            # of version (we're looking for 0001)
            signature = self.mmap.read(8)
            if signature != self.signature:
                # Start fresh
                return self.datatype()
            else:
                # There should then be four bytes of length
                size = struct.unpack("!I", self.mmap.read(4))[0]
                return msgpack.unpackb(self.mmap.read(size), encoding="utf8")
        finally:
            self.semaphore.release()

    def _set_value(self, value):
        assert isinstance(value, self.datatype)
        try:
            self.semaphore.acquire(self.death_timeout)
        except posix_ipc.BusyError:
            self._reset()
            self.semaphore.acquire(0)
        try:
            self.mmap.seek(0)
            self.mmap.write(self.signature)
            towrite = msgpack.packb(value, use_bin_type=True)
            self.mmap.write(struct.pack("!I", len(towrite)))
            self.mmap.write(towrite)
        finally:
            self.semaphore.release()

    def _reset(self):
        """
        Resets the semaphore if it's got stuck by a process that exited without
        releasing it.
        """
        # Make the mmap empty enough that get will ignore it
        self.mmap.seek(0)
        self.mmap.write(b"\0\0\0\0\0\0\0\0")
        # Unlink and remake the semaphore
        self.semaphore.unlink()
        self.semaphore = posix_ipc.Semaphore(
            self.path + "-semaphore",
            flags=posix_ipc.O_CREX,
            mode=0o660,
            initial_value=1,
        )

    def __del__(self):
        """
        Explicitly closes the shared memory area.

        The semaphore is not closed as this is not threadsafe; closing it
        prevents any other layers in the same process from seeing it.
        """
        self.mmap.close()
        self.shm.close_fd()

    def flush(self):
        self._set_value(self.datatype())

    def __iter__(self):
        return iter(self._get_value())

    def __contains__(self, item):
        return item in self._get_value()

    def __getitem__(self, key):
        return self._get_value()[key]

    def __setitem__(self, key, value):
        d = self._get_value()
        d[key] = value
        self._set_value(d)

    def __delitem__(self, key):
        d = self._get_value()
        del d[key]
        self._set_value(d)

    def __len__(self):
        return len(self._get_value())


class MemoryDict(MemoryDatastructure):
    """
    Memory backed dict. Used for group membership.
    """

    signature = b"ASGD0001"

    def items(self):
        return self._get_value().items()

    def keys(self):
        return self._get_value().keys()

    def values(self):
        return self._get_value().values()

    def get(self, key, default):
        return self._get_value().get(key, default)

    def discard(self, item):
        value = self._get_value()
        if item in value:
            del value[item]
        self._set_value(value)
