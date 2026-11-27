# -*- coding: utf-8 -*-
import unittest
from unittest.mock import Mock, patch

import core.session as session_module
from core.session import BrowserSession, _is_loopback_proxy


class SessionProxyChainTests(unittest.TestCase):
    def test_loopback_detection(self):
        for value in ("http://127.0.0.1:11456", "socks5://localhost:7897",
                      "http://[::1]:1080", "127.0.0.1:8080"):
            with self.subTest(value=value):
                self.assertTrue(_is_loopback_proxy(value))
        for value in ("", "socks5h://proxy.test:10000",
                      "http://user:pass@proxy.example.test:10000"):
            with self.subTest(value=value):
                self.assertFalse(_is_loopback_proxy(value))

    def test_local_relay_is_not_wrapped_again(self):
        """查活已建好的本地中继不能再套一层，否则会把 127.0.0.1 发到远端出口。"""
        with patch("core.proxy_chain.open_proxy_pool_proxy") as open_proxy:
            session = BrowserSession(proxy="http://127.0.0.1:11456", detect_exit_geo=False)
            try:
                open_proxy.assert_not_called()
                self.assertEqual(session.transport_proxy, "http://127.0.0.1:11456")
                self.assertIsNone(session._proxy_pool_relay)
            finally:
                session.close()

    def test_remote_target_proxy_still_chained_through_upstream(self):
        relay = Mock()
        with patch("core.proxy_chain.open_proxy_pool_proxy",
                   return_value=("http://127.0.0.1:19999", relay)) as open_proxy:
            session = BrowserSession(proxy="socks5h://user:pass@proxy.test:10000",
                                     detect_exit_geo=False)
            try:
                open_proxy.assert_called_once_with("socks5h://user:pass@proxy.test:10000")
                self.assertEqual(session.transport_proxy, "http://127.0.0.1:19999")
                self.assertIs(session._proxy_pool_relay, relay)
            finally:
                session.close()
        relay.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
