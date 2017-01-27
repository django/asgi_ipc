from __future__ import unicode_literals

import unittest
from asgi_ipc import IPCChannelLayer, MemoryDict
from asgiref.conformance import ConformanceTestCase


# Default conformance tests
class IPCLayerTests(ConformanceTestCase):

    channel_layer = IPCChannelLayer(expiry=1, group_expiry=2, capacity=5)
    expiry_delay = 1.1
    capacity_limit = 5


# MemoryDict unit tests
class MemoryDictTests(unittest.TestCase):

    def setUp(self):
        self.instance = MemoryDict("/test-md")
        self.instance.flush()

    def test_item_access(self):
        # Make sure the key is not there to start
        with self.assertRaises(KeyError):
            self.instance["test"]
        # Set it and check it twice
        self.instance["test"] = "foo"
        self.assertEqual(self.instance["test"], "foo")
        self.instance["test"] = "bar"
        self.assertEqual(self.instance["test"], "bar")
        # Delete it and make sure it's gone
        del self.instance["test"]
        with self.assertRaises(KeyError):
            self.instance["test"]
