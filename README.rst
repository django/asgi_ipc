asgi_ipc
========

.. image:: https://api.travis-ci.org/andrewgodwin/asgi_ipc.svg
    :target: https://travis-ci.org/andrewgodwin/asgi_ipc

.. image:: https://img.shields.io/pypi/v/asgi_ipc.svg
    :target: https://pypi.python.org/pypi/asgi_ipc

An ASGI channel layer that uses POSIX shared memory IPC as its backing store
(only works between processes on the same machine).

Beta - please file issues if it doesn't work or fails weirdly (shared memory
and IPC can be tricky)


Usage
-----

You'll need to instantiate the channel layer with a path prefix to create
IPC objects underneath; any channel layers with the same prefix will talk to
each other as long as they're on the same machine.

Example::

    channel_layer = IPCChannelLayer(
        prefix="aeracode",
        channel_memory=200 * 1024 * 1024,
    )

prefix
~~~~~~

Prefix to use for IPC objects under the root namespace. Defaults to ``asgi``.
IPC layers on the same machine with the same prefix will talk to each other.

channel_memory
~~~~~~~~~~~~~~

The amount of shared memory to allocate to the channel storage, in bytes.
Defaults to 100MB. All of your in-flight messages must fit into this,
otherwise you'll get ``ChannelFull`` errors if the memory space is full up.

ASGI messages can be a maximum of one megabyte, and are usually much smaller.
The IPC routing metadata on top of each message is approximately 50 bytes.

group_memory
~~~~~~~~~~~~

The amount of shared memory to allocate to the group storage, in bytes.
Defaults to 20MB. All of your group membership data must fit into this space,
otherwise your group memberships may fail to persist.

You can fit approximately 4000 group-channel membership associations into one
megabyte of memory.

expiry
~~~~~~

Message expiry in seconds. Defaults to ``60``. You generally shouldn't need
to change this, but you may want to turn it down if you have peaky traffic you
wish to drop, or up if you have peaky traffic you want to backlog until you
get to it.

group_expiry
~~~~~~~~~~~~

Group expiry in seconds. Defaults to ``86400``. Interface servers will drop
connections after this amount of time; it's recommended you reduce it for a
healthier system that encourages disconnections.

capacity
~~~~~~~~

Default channel capacity. Defaults to ``100``. Once a channel is at capacity,
it will refuse more messages. How this affects different parts of the system
varies; a HTTP server will refuse connections, for example, while Django
sending a response will just wait until there's space.

channel_capacity
~~~~~~~~~~~~~~~~

Per-channel capacity configuration. This lets you tweak the channel capacity
based on the channel name, and supports both globbing and regular expressions.

It should be a dict mapping channel name pattern to desired capacity; if the
dict key is a string, it's intepreted as a glob, while if it's a compiled
``re`` object, it's treated as a regular expression.

This example sets ``http.request`` to 200, all ``http.response!`` channels
to 10, and all ``websocket.send!`` channels to 20::

    channel_capacity={
        "http.request": 200,
        "http.response!*": 10,
        re.compile(r"^websocket.send\!.+"): 20,
    }

If you want to enforce a matching order, use an ``OrderedDict`` as the
argument; channels will then be matched in the order the dict provides them.
