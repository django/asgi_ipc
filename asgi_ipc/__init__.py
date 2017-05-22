import pkg_resources
__version__ = pkg_resources.require('asgi_ipc')[0].version

from .core import IPCChannelLayer  # noqa
