"""Only a valid login should wake a sleeping Minecraft server."""

import unittest
from unittest import mock

from mc_host.proxy import SleepProxy


class SleepProxyWakeTests(unittest.TestCase):
    def setUp(self):
        self.wake = mock.Mock()
        self.proxy = SleepProxy(0, self.wake)
        self.client = mock.Mock()
        self.client.recv.return_value = b"\x00"

    def test_empty_connection_does_not_wake_server(self):
        self.client.recv.return_value = b""
        with mock.patch.object(self.proxy, "_read_handshake", side_effect=EOFError):
            self.proxy._handle_client(self.client, ("127.0.0.1", 12345))
        self.wake.assert_not_called()

    def test_status_ping_does_not_wake_server(self):
        with mock.patch.object(self.proxy, "_read_handshake", return_value=(1, 1)):
            with mock.patch.object(self.proxy, "_send_status_response"):
                self.proxy._handle_client(self.client, ("127.0.0.1", 12345))
        self.wake.assert_not_called()

    def test_login_wakes_server(self):
        with mock.patch.object(self.proxy, "_read_handshake", return_value=(1, 2)):
            with mock.patch.object(self.proxy, "_send_login_disconnect"):
                self.proxy._handle_client(self.client, ("127.0.0.1", 12345))
        self.wake.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
