from __future__ import unicode_literals

from asgi_ipc import IPCChannelLayer
from asgiref.conformance import ConformanceTestCase


# Default conformance tests
class IPCLayerTests(ConformanceTestCase):

    channel_layer = IPCChannelLayer(expiry=1, group_expiry=2)
    expiry_delay = 1.1
