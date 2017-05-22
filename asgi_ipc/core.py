from __future__ import unicode_literals

import msgpack
import random
import six
import string
import time
from asgiref.base_layer import BaseChannelLayer

from .store import ChannelMemoryStore, GroupMemoryStore


class IPCChannelLayer(BaseChannelLayer):
    """
    Posix IPC backed channel layer, using the posix_ipc module's shared memory
    and sempahore components.

    It uses mmap'd shared memory areas to store msgpack'd versions of the
    datastructures; see the store module for more information.
    """

    def __init__(self,
        prefix="asgi",
        expiry=60,
        group_expiry=86400,
        capacity=10,
        channel_capacity=None,
        message_memory=100*1024*1024,
        group_memory=20*1024*1024,
    ):
        super(IPCChannelLayer, self).__init__(
            expiry=expiry,
            group_expiry=group_expiry,
            capacity=capacity,
            channel_capacity=channel_capacity,
        )
        self.prefix = prefix
        # Table with queued messages
        self.message_store = ChannelMemoryStore("%s-messages" % prefix, message_memory)
        # Table containing all groups to flush
        self.group_store = GroupMemoryStore("%s-groups" % prefix, group_memory)

    # --------
    # ASGI API
    # --------

    extensions = ["flush", "groups"]

    def send(self, channel, message):
        # Type check
        assert isinstance(message, dict), "message is not a dict"
        assert self.valid_channel_name(channel), "channel name not valid"
        # Make sure the message does not contain reserved keys
        assert "__asgi_channel__" not in message
        # If it's a process-local channel, strip off local part and stick full name in message
        if "!" in channel:
            message = dict(message.items())
            message['__asgi_channel__'] = channel
            channel = self.non_local_name(channel)
        # Write message into the correct message queue with a size check
        channel_size = self.message_store.length(channel)
        if channel_size >= self.get_capacity(channel):
            raise self.ChannelFull()
        else:
            self.message_store.append(
                channel,
                msgpack.packb(message, use_bin_type=True),
                time.time() + self.expiry,
            )

    def receive(self, channels, block=False):
        if not channels:
            return None, None
        channels = list(channels)
        assert all(
            self.valid_channel_name(channel, receive=True) for channel in channels
        ), "one or more channel names invalid"
        random.shuffle(channels)
        # Try to pop off all of the named channels
        for channel in channels:
            # See if there is an unexpired message
            try:
                message = self.message_store.pop(channel)
            except IndexError:
                continue
            message = msgpack.unpackb(message, encoding="utf8")
            # If there is a full channel name stored in the message, unpack it.
            if "__asgi_channel__" in message:
                channel = message['__asgi_channel__']
                del message['__asgi_channel__']
            return channel, message
        return None, None

    def new_channel(self, pattern):
        assert isinstance(pattern, six.text_type)
        # Keep making channel names till one isn't present.
        while True:
            random_string = "".join(random.sample(string.ascii_letters, 12))
            assert pattern.endswith("?")
            new_name = pattern + random_string
            if not self.message_store.length(new_name):
                return new_name
            else:
                continue

    # ----------------
    # Groups extension
    # ----------------

    def group_add(self, group, channel):
        """
        Adds the channel to the named group
        """
        assert self.valid_group_name(group), "Invalid group name"
        self.group_store.add(group, channel, time.time() + self.group_expiry)

    def group_discard(self, group, channel):
        """
        Removes the channel from the named group if it is in the group;
        does nothing otherwise (does not error)
        """
        assert self.valid_group_name(group), "Invalid group name"
        self.group_store.discard(group, channel)

    def send_group(self, group, message):
        """
        Sends a message to the entire group.
        """
        assert self.valid_group_name(group), "Invalid group name"
        for channel in self.group_channels(group):
            try:
                self.send(channel, message)
            except self.ChannelFull:
                pass

    def group_channels(self, group):
        return self.group_store.flush_expired(group)

    # ---------------
    # Flush extension
    # ---------------

    def flush(self):
        """
        Deletes all messages and groups.
        """
        self.message_store.flush_all()
        self.group_store.flush_all()

    def __str__(self):
        return "%s(prefix=%s)" % (self.__class__.__name__, self.prefix)
