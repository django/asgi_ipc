from __future__ import unicode_literals

import contextlib
import msgpack
import os
import random
import six
import sqlite3
import string
import sys
import tempfile
import threading
import time
from asgiref.base_layer import BaseChannelLayer

import pkg_resources
import posix_ipc

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

    def __init__(self, prefix="asgi", expiry=60, group_expiry=86400,
                 capacity=10, channel_capacity=None):
        super(IPCChannelLayer, self).__init__(
            expiry=expiry,
            group_expiry=group_expiry,
            capacity=capacity,
            channel_capacity=channel_capacity,
        )
        self.prefix = prefix
        # Work out where to store the data
        temp_dir = tempfile.gettempdir()
        sqlite_path = os.path.join(temp_dir, prefix + '.sqlite')
        # Table with queued messages
        self.message_store = MessageTable(sqlite_path, prefix)
        # Table containing all groups to flush
        self.group_store = GroupTable(sqlite_path, prefix)

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
        # Write message into the correct message queue
        channel_size = self.message_store.get_message_count(channel)

        if channel_size >= self.get_capacity(channel):
            raise self.ChannelFull
        else:
            towrite = msgpack.packb(message, use_bin_type=True)
            self.message_store.add_message(
                message=towrite, channel=channel,
                expiry=time.time() + self.expiry
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
            # Keep looping on the channel until
            # we hit no messages or an unexpired one
            while True:
                message, expires = self.message_store.pop_message(channel)
                if message is None:
                    break
                message = msgpack.unpackb(message, encoding="utf8")
                if expires <= time.time():
                    continue
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
            if new_name not in self.message_store:
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
        self.group_store.add_channel(group=group, channel=channel, expiry=time.time() + self.group_expiry)

    def group_discard(self, group, channel):
        """
        Removes the channel from the named group if it is in the group;
        does nothing otherwise (does not error)
        """
        assert self.valid_group_name(group), "Invalid group name"
        self.group_store.discard_channel(group, channel)

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
        result = [channel[0] for channel in self.group_store.get_current_channels(group)]
        if sys.version_info[0] == 2:
            result = [channel.decode() for channel in result]
        return result

    # ---------------
    # Flush extension
    # ---------------

    def flush(self):
        """
        Deletes all messages and groups.
        """
        self.message_store.flush()
        self.group_store.flush()

    def __str__(self):
        return "%s(prefix=%s)" % (self.__class__.__name__, self.prefix)


class SqliteTable(object):
    """
    Generic sqlite datastructure class; used for dicts for group membership,
    and lists for channels.
    """
    # How long to wait for the semaphore before declaring deadlock and flushing
    death_timeout = 2

    # Threadlocal connection storage
    _locals = threading.local()

    def __init__(self, db_path, identifier):
        self.db_path = db_path
        self.identifier = '/' + identifier + self.table_name + '-sem'
        self._execute(self.table_structure)

    @property
    def connection(self):
        """
        Caching, threadlocal connection accessor.
        """
        if not hasattr(self._locals, "connection"):
            self._locals.connection = sqlite3.connect(
                self.db_path,
                isolation_level="IMMEDIATE",
            )
            self._locals.connection.text_factory = str
        return self._locals.connection

    @property
    def semaphore(self):
        """
        Caching, threadlocal sempahore accessor.
        """
        # TODO: Investigate having separate read and write locks to allow
        # concurrent reads.
        if not hasattr(self._locals, "semaphore"):
            self._locals.semaphore = posix_ipc.Semaphore(
                self.identifier,
                flags=posix_ipc.O_CREAT,
                mode=0o660,
                initial_value=1,
            )
        return self._locals.semaphore

    def _reset(self):
        """
        Resets the semaphore if it's got stuck by a process that exited without
        releasing it.
        """
        # Unlink and remake the semaphore
        self.semaphore.unlink()
        delattr(self._locals, "semaphore")

    def flush(self):
        with self.semaphore_manager():
            self._execute('DELETE FROM {table_name}'.format(table_name=self.table_name))

    def _execute(self, query, *args):
        cursor = self.connection.cursor()
        cursor.execute(query.format(table=self.table_name), args)
        self.connection.commit()
        return cursor.fetchall()

    @contextlib.contextmanager
    def semaphore_manager(self):
        try:
            self.semaphore.acquire(self.death_timeout)
        except posix_ipc.BusyError:
            self._reset()
            self.semaphore.acquire(0)
        try:
            yield
        finally:
            self.semaphore.release()


class MessageTable(SqliteTable):
    table_name = 'messages'
    table_structure = '''
        CREATE TABLE IF NOT EXISTS messages
        (id integer primary key, channel text, message text, expiry datetime)
    '''

    def get_messages(self, channel):
        with self.semaphore_manager():
            return (
                self._execute('SELECT message, expiry FROM {table} WHERE channel=?', channel) or
                (None, None)
            )

    def get_message_count(self, channel):
        with self.semaphore_manager():
            return self._execute('SELECT COUNT(*) FROM {table} WHERE channel=?', channel)[0][0]

    def add_message(self, message, expiry, channel):
        with self.semaphore_manager():
            self._execute('INSERT INTO {table} (channel, message, expiry) VALUES (?,?,?)', channel, message, expiry)

    def pop_message(self, channel):
        """
        Atomically reads and removes a message from the messages table.
        """
        with self.semaphore_manager():
            cursor = self.connection.cursor()
            cursor.execute("BEGIN")
            cursor.execute(
                'SELECT id, message, expiry FROM {table} WHERE channel=? LIMIT 1'.format(table=self.table_name),
                (channel, )
            )
            result = cursor.fetchall()
            self.connection.commit()
            if not result:
                return None, None
            row = result[0]
            cursor.execute('DELETE FROM {table} WHERE id=?'.format(table=self.table_name), (row[0], ))
            self.connection.commit()
            return row[1], row[2]

    def __contains__(self, value):
        with self.semaphore_manager():
            return bool(self._execute('SELECT COUNT(*) FROM {table} WHERE channel=?', value)[0][0])


class GroupTable(SqliteTable):
    table_name = 'groups'
    table_structure = '''
        CREATE TABLE IF NOT EXISTS groups
        (channel text, group_name text, expiry datetime)
    '''

    def add_channel(self, group, channel, expiry):
        with self.semaphore_manager():
            self._execute('INSERT INTO {table} (channel, group_name, expiry) VALUES (?,?,?)', channel, group, expiry)

    def discard_channel(self, group, channel):
        with self.semaphore_manager():
            self._execute('DELETE FROM {table} WHERE group_name=? AND channel=?', group, channel)

    def _cleanup(self, group):
        with self.semaphore_manager():
            self._execute('DELETE FROM {table} WHERE group_name=? AND expiry<=?', group, time.time())

    def get_current_channels(self, group):
        with self.semaphore_manager():
            self._cleanup(group)
            return self._execute('SELECT DISTINCT channel FROM {table} WHERE group_name=?', group)
