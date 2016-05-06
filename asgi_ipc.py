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


__version__ = pkg_resources.require('asgi_ipc')[0].version


class IPCChannelLayer(object):
    """
    Posix IPC backed channel layer, using the posix_ipc module and
    MessageQueues.
    """

    def __init__(self, prefix="asgi", expiry=60, group_expiry=86400, capacity=100):
        self.prefix = prefix
        self.expiry = expiry
        self.capacity = capacity
        self.group_expiry = group_expiry
        # Set that contains all queues we created so we can flush them
        self.queue_set = MemorySet("/%s-queueset" % self.prefix)

    ### ASGI API ###

    extensions = ["flush"]

    class MessageTooLarge(Exception):
        pass

    class ChannelFull(Exception):
        pass

    def send(self, channel, message):
        # Typecheck
        assert isinstance(message, dict), "message is not a dict"
        assert isinstance(channel, six.text_type), "%s is not unicode" % channel
        # Write message into the correct message queue
        queue = self._message_queue(channel)
        try:
            queue.send(self.serialize(message), timeout=0)
        except posix_ipc.BusyError:
            raise self.ChannelFull

    def receive_many(self, channels, block=False):
        if not channels:
            return None, None
        channels = list(channels)
        assert all(isinstance(channel, six.text_type) for channel in channels)
        random.shuffle(channels)
        # Try to pop off all of the named channels
        for channel in channels:
            queue = self._message_queue(channel)
            try:
                message = self.deserialize(queue.receive(0)[0])
                return channel, message
            except posix_ipc.BusyError:
                continue
        return None, None

    def new_channel(self, pattern):
        assert isinstance(pattern, six.text_type)
        # Keep making channel names till one isn't present.
        while True:
            random_string = "".join(random.choice(string.ascii_letters) for i in range(12))
            assert pattern.endswith("!")
            new_name = pattern + random_string
            # To see if it's present we open the queue without O_CREAT
            try:
                posix_ipc.MessageQueue(self._channel_path(new_name))
            except posix_ipc.ExistentialError:
                return new_name
            else:
                continue

    ### Flush extension ###

    def flush(self):
        """
        Deletes all messages and groups.
        """
        for path in self.queue_set:
            try:
                posix_ipc.MessageQueue(path).unlink()
            except posix_ipc.ExistentialError:
                # Already deleted.
                pass

    ### Serialization ###

    def serialize(self, message):
        """
        Serializes message to a byte string.
        """
        return msgpack.packb(message, use_bin_type=True)

    def deserialize(self, message):
        """
        Deserializes from a byte string.
        """
        return msgpack.unpackb(message, encoding="utf8")

    ### Internal functions ###

    def _channel_path(self, channel):
        return "/%s-channel-%s" % (self.prefix, channel.encode("ascii"))

    def _message_queue(self, channel):
        """
        Returns an IPC MessageQueue object for the given channel.
        """
        assert isinstance(channel, six.text_type)
        self.queue_set.add(self._channel_path(channel))
        return posix_ipc.MessageQueue(
            self._channel_path(channel),
            flags=posix_ipc.O_CREAT,
            mode=0o660,
            max_messages=self.capacity,
            max_message_size=1024*1024,  # 1MB
        )

    def __str__(self):
        return "%s(hosts=%s)" % (self.__class__.__name__, self.hosts)


class MemorySet(object):
    """
    A set-like object that backs itself onto a shared memory segment by path.
    Uses a semaphore to lock and unlock the set and msgpack to encode values.
    """

    size = 1024 * 1024 * 20
    death_timeout = 2

    def __init__(self, path):
        self.path = path
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

    def _get_value(self):
        try:
            self.semaphore.acquire(self.death_timeout)
        except posix_ipc.BusyError:
            self._reset()
            self.semaphore.acquire(0)
        try:
            # Seek to start of memory segment
            self.mmap.seek(0)
            # The first four bytes should be "ASGI", followed by four bytes
            # of version (we're looking for 0001)
            signature = self.mmap.read(8)
            if signature != b"ASGI0001":
                # Start fresh
                return set()
            else:
                # There should then be four bytes of length
                size = struct.unpack("!I", self.mmap.read(4))[0]
                return set(msgpack.unpackb(self.mmap.read(size), encoding="utf8"))
        finally:
            self.semaphore.release()

    def _set_value(self, value):
        assert isinstance(value, set)
        try:
            self.semaphore.acquire(self.death_timeout)
        except posix_ipc.BusyError:
            self._reset()
            self.semaphore.acquire(0)
        try:
            self.mmap.seek(0)
            self.mmap.write(b"ASGI0001")
            towrite = msgpack.packb(list(value), use_bin_type=True)
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
        self.mmap.write("\0\0\0\0\0\0\0\0")
        # Unlink and remake the semaphore
        self.semaphore.unlink()
        self.semaphore = posix_ipc.Semaphore(
            self.path + "-semaphore",
            flags=posix_ipc.O_CREX,
            mode=0o660,
            initial_value=1,
        )

    def __iter__(self):
        return iter(self._get_value())

    def __contains__(self, item):
        return item in self._get_value()

    def add(self, item):
        value = self._get_value()
        value.add(item)
        self._set_value(value)

    def discard(self, item):
        value = self._get_value()
        value.add(item)
        self._set_value(value)

    def flush(self):
        self._set_value(set())
