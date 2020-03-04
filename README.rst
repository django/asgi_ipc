asgi_ipc
========

.. image:: https://api.travis-ci.org/django/asgi_ipc.svg
    :target: https://travis-ci.org/django/asgi_ipc

.. image:: https://img.shields.io/pypi/v/asgi_ipc.svg
    :target: https://pypi.python.org/pypi/asgi_ipc
    
**NOTE: This is for Channels 1 only. It will not work on Channels 2 and there's no plans to port it.**

An ASGI channel layer that uses POSIX shared memory IPC as its backing store
(only works between processes on the same machine).

IPC is still a bit of a wild west of UNIX compatibility, so if you find weird
errors, please file an issue with details and exact system specifications. In
particular, this module is tested and works well on Linux kernels and the Windows
Subsystem for Linux; it also works on Mac OS but will not be able to detect
deadlock situations due to limitations in the kernel.


Usage
-----

You'll need to instantiate the channel layer with a path prefix to create
IPC objects underneath; any channel layers with the same prefix will talk to
each other as long as they're on the same machine.

Example:

.. code-block:: python

    import asgi_ipc as asgi

    channel_layer = asgi.IPCChannelLayer(
        prefix="aeracode",
        message_memory=200 * 1024 * 1024,
    )

    channel_layer.send("my_channel", {"text": "Hello ASGI"})
    print(channel_layer.receive(["my_channel", ]))

``prefix``
~~~~~~~~~~

Prefix to use for IPC objects under the root namespace. Defaults to ``asgi``.
IPC layers on the same machine with the same prefix will talk to each other.

``message_memory``
~~~~~~~~~~~~~~~~~~

The amount of shared memory to allocate to the channel storage, in bytes.
Defaults to 100MB. All of your in-flight messages must fit into this,
otherwise you'll get ``ChannelFull`` errors if the memory space is full up.

ASGI messages can be a maximum of one megabyte, and are usually much smaller.
The IPC routing metadata on top of each message is approximately 50 bytes.

``group_memory``
~~~~~~~~~~~~~~~~

The amount of shared memory to allocate to the group storage, in bytes.
Defaults to 20MB. All of your group membership data must fit into this space,
otherwise your group memberships may fail to persist.

You can fit approximately 4000 group-channel membership associations into one
megabyte of memory.

``expiry``
~~~~~~~~~~

Message expiry in seconds. Defaults to ``60``. You generally shouldn't need
to change this, but you may want to turn it down if you have peaky traffic you
wish to drop, or up if you have peaky traffic you want to backlog until you
get to it.

``group_expiry``
~~~~~~~~~~~~~~~~

Group expiry in seconds. Defaults to ``86400``. Interface servers will drop
connections after this amount of time; it's recommended you reduce it for a
healthier system that encourages disconnections.

``capacity``
~~~~~~~~~~~~

Default channel capacity. Defaults to ``100``. Once a channel is at capacity,
it will refuse more messages. How this affects different parts of the system
varies; a HTTP server will refuse connections, for example, while Django
sending a response will just wait until there's space.

``channel_capacity``
~~~~~~~~~~~~~~~~~~~~

Per-channel capacity configuration. This lets you tweak the channel capacity
based on the channel name, and supports both globbing and regular expressions.

It should be a dict mapping channel name pattern to desired capacity; if the
dict key is a string, it's interpreted as a glob, while if it's a compiled
``re`` object, it's treated as a regular expression.

This example sets ``http.request`` to 200, all ``http.response!`` channels
to 10, and all ``websocket.send!`` channels to 20:

.. code-block:: python

    channel_capacity={
        "http.request": 200,
        "http.response!*": 10,
        re.compile(r"^websocket.send\!.+"): 20,
    }

If you want to enforce a matching order, use an ``OrderedDict`` as the
argument; channels will then be matched in the order the dict provides them.

Dependencies
------------

All Channels projects currently support Python 2.7, 3.4 and 3.5.

Contributing
------------

Please refer to the
`main Channels contributing docs <https://github.com/django/channels/blob/master/CONTRIBUTING.rst>`_.
That also contains advice on how to set up the development environment and run the tests.

Maintenance and Security
------------------------

To report security issues, please contact security@djangoproject.com. For GPG
signatures and more security process information, see
https://docs.djangoproject.com/en/dev/internals/security/.

To report bugs or request new features, please open a new GitHub issue.

This repository is part of the Channels project. For the shepherd and maintenance team, please see the
`main Channels readme <https://github.com/django/channels/blob/master/README.rst>`_.
