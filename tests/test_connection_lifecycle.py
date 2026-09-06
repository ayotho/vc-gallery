from __future__ import annotations

import unittest
from http.server import ThreadingHTTPServer
from unittest.mock import Mock, patch

import vc_gallery_serve as serve


class RequestConnectionLifecycleTests(unittest.TestCase):
    def test_request_thread_always_closes_its_gallery_connection(self) -> None:
        for error in (None, RuntimeError("request failed")):
            with self.subTest(error=error):
                state = Mock()
                server = object.__new__(serve.GalleryHTTPServer)
                original_state = serve.STATE
                serve.STATE = state
                try:
                    with patch.object(
                        ThreadingHTTPServer,
                        "process_request_thread",
                        side_effect=error,
                    ):
                        if error is None:
                            server.process_request_thread(object(), ("127.0.0.1", 1))
                        else:
                            with self.assertRaises(RuntimeError):
                                server.process_request_thread(object(), ("127.0.0.1", 1))
                finally:
                    serve.STATE = original_state

                state.close_thread_connection.assert_called_once_with()

    def test_close_thread_connection_releases_and_forgets_the_handle(self) -> None:
        state = serve.State()
        connection = Mock()
        state._conn_local.conn = connection
        state._conn_local.gen = state._conn_gen

        state.close_thread_connection()

        connection.close.assert_called_once_with()
        self.assertIsNone(state._conn_local.conn)


if __name__ == "__main__":
    unittest.main()
