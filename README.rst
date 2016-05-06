asgi_ipc
========

An ASGI channel layer that uses POSIX shared memory IPC as its backing store
(only works between processes on the same machine)


Usage
-----

You'll need to instantiate the channel layer with a path prefix to create
IPC objects underneath. This path must be writable by any user running this
backend.

* ``prefix``: Prefix to use for IPC objects in the root namespace. Defaults to ``asgi``.

Example::

    channel_layer = IPCChannelLayer(
        prefix="aeracode",
    )
