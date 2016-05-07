asgi_ipc
========

An ASGI channel layer that uses POSIX shared memory IPC as its backing store
(only works between processes on the same machine).

Beta - please file issues if it doesn't work or fails weirdly (shared memory
and IPC can be tricky)


Usage
-----

You'll need to instantiate the channel layer with a path prefix to create
IPC objects underneath; any channel layers with the same prefix will talk to
each other.

* ``prefix``: Prefix to use for IPC objects in the root namespace. Defaults to ``asgi``.

Example::

    channel_layer = IPCChannelLayer(
        prefix="aeracode",
    )
