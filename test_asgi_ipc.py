from __future__ import unicode_literals

from asgi_ipc import IPCChannelLayer
from asgiref.conformance import ConformanceTestCase


# Default conformance tests
class IPCLayerTests(ConformanceTestCase):

    channel_layer = IPCChannelLayer(expiry=1, group_expiry=2, capacity=5)
    expiry_delay = 1.1
    capacity_limit = 5
