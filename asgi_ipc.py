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

    def __init__(self, prefix="asgi", expiry=60, group_expiry=86400, capacity=10, channel_capacity=None):
        super(IPCChannelLayer, self).__init__(
            expiry=expiry,
            group_expiry=group_expiry,
            capacity=capacity,
            channel_capacity=channel_capacity,
        )
        self.thread_lock = threading.Lock()
        self.prefix = prefix
        self.channel_set = MemorySet("/%s-channelset" % self.prefix)
        # Set containing all groups to flush
        self.group_set = MemorySet("/%s-groupset" % self.prefix)

    ### ASGI API ###

    extensions = ["flush", "groups"]

    def send(self, channel, message):
        # Typecheck
        assert isinstance(message, dict), "message is not a dict"
        assert self.valid_channel_name(channel), "channel name not valid"
        # Write message into the correct message queue
        channel_list = self._channel_list(channel)
        with self.thread_lock:
            if len(channel_list) >= self.get_capacity(channel):
                raise self.ChannelFull
            else:
                channel_list.append([message, time.time() + self.expiry])

    def receive_many(self, channels, block=False):
        if not channels:
            return None, None
        channels = list(channels)
        assert all(self.valid_channel_name(channel) for channel in channels), "one or more channel names invalid"
        random.shuffle(channels)
        # Try to pop off all of the named channels
        with self.thread_lock:
            for channel in channels:
                channel_list = self._channel_list(channel)
                # Keep looping on the channel until we hit no messages or an unexpired one
                while True:
                    try:
                        message, expires = channel_list.popleft()
                        if expires <= time.time():
                            continue
                        return channel, message
                    except IndexError:
                        break
        return None, None

    def new_channel(self, pattern):
        assert isinstance(pattern, six.text_type)
        # Keep making channel names till one isn't present.
        while True:
            random_string = "".join(random.choice(string.ascii_letters) for i in range(12))
            assert pattern.endswith("!") or pattern.endswith("?")
            new_name = pattern + random_string
            # To see if it's present we open the queue without O_CREAT
            if not MemoryList.exists(self._channel_path(new_name)):
                return new_name
            else:
                continue

    ### Groups extension ###

    def group_add(self, group, channel):
        """
        Adds the channel to the named group
        """
        assert self.valid_group_name(group), "Invalid group name"
        group_dict = self._group_dict(group)
        with self.thread_lock:
            group_dict[channel] = time.time() + self.group_expiry

    def group_discard(self, group, channel):
        """
        Removes the channel from the named group if it is in the group;
        does nothing otherwise (does not error)
        """
        assert self.valid_group_name(group), "Invalid group name"
        group_dict = self._group_dict(group)
        with self.thread_lock:
            group_dict.discard(channel)

    def send_group(self, group, message):
        """
        Sends a message to the entire group.
        """
        assert self.valid_group_name(group), "Invalid group name"
        group_dict = self._group_dict(group)
        with self.thread_lock:
            items = list(group_dict.items())
        for channel, expires in items:
            if expires <= time.time():
                with self.thread_lock:
                    group_dict.discard(channel)
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
            for path in self.channel_set:
                MemoryList(path).flush()
            for path in self.group_set:
                MemoryDict(path).flush()

    ### Internal functions ###

    def _channel_path(self, channel):
        assert isinstance(channel, six.text_type)
        return "/%s-channel-%s" % (self.prefix, channel.encode("ascii"))

    def _group_path(self, group):
        assert isinstance(group, six.text_type)
        return "/%s-group-%s" % (self.prefix, group.encode("ascii"))

    def _channel_list(self, channel):
        """
        Returns a MemoryList object for the channel
        """
        self.channel_set.add(self._channel_path(channel))
        return MemoryList(self._channel_path(channel), size=1024*1024*self.capacity)

    def _group_dict(self, group):
        """
        Returns a MemoryDict object for the named group
        """
        self.group_set.add(self._group_path(group))
        return MemoryDict(self._group_path(group), size=1024*1024*10)

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
        self.shm = posix_ipc.SharedMemory(
            self.path,
            flags=posix_ipc.O_CREAT,
            mode=0o660,
            size=self.size,
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

    def discard(self, item):
        value = self._get_value()
        if item in value:
            del value[item]
        self._set_value(value)


class MemorySet(MemoryDict):
    """
    Like MemoryDict but just presents a set interface (using dict keys)
    """

    def add(self, item):
        value = self._get_value()
        value[item] = None
        self._set_value(value)


class MemoryList(MemoryDatastructure):
    """
    Memory-backed list. Used for channels.
    """

    signature = b"ASGL0001"

    datatype = list

    def append(self, item):
        value = self._get_value()
        value.append(item)
        self._set_value(value)

    def popleft(self):
        value = self._get_value()
        self._set_value(value[1:])
        return value[0]
