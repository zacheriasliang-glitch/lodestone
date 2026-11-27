"""Native loopback lifecycle regression; no models or external connections."""
import errno
import os
from pathlib import Path
import socket
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "desktop/runtime/src"))
from lodestone_machinery import loom_runtime_worker as worker


class LoopbackListenerTests(unittest.TestCase):
    @unittest.skipUnless(os.name == "posix", "POSIX TIME_WAIT semantics")
    def test_closed_connection_reuses_port_while_plain_bind_reproduces_failure(self):
        port = 0
        for encounter in (1, 2):
            with self.subTest(encounter=encounter):
                # Match a server that permits restarts, including Go's Ollama
                # listener. Make the accepted server side actively close first
                # so its local port enters TIME_WAIT after all sockets close.
                with socket.socket() as listener:
                    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                    listener.settimeout(2)
                    listener.bind(("127.0.0.1", port))
                    port = listener.getsockname()[1]
                    listener.listen(1)
                    with socket.create_connection(("127.0.0.1", port), timeout=2) as client:
                        accepted, _ = listener.accept()
                        with accepted:
                            accepted.settimeout(2)
                            accepted.sendall(b"x")
                            self.assertEqual(client.recv(1), b"x")
                            accepted.shutdown(socket.SHUT_WR)
                            self.assertEqual(client.recv(1), b"")
                with socket.socket() as old_probe:
                    with self.assertRaises(OSError) as refused:
                        old_probe.bind(("127.0.0.1", port))
                    self.assertEqual(refused.exception.errno, errno.EADDRINUSE)
                # The second iteration starts a real listener on the same port.
                worker.ensure_loopback_port_available(port)

    def test_live_listener_is_refused_before_starting_any_provider_process(self):
        with socket.socket() as listener, tempfile.TemporaryDirectory() as directory:
            if os.name == "posix":
                listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind(("127.0.0.1", 0))
            listener.listen(1)
            port = listener.getsockname()[1]
            probe = worker.ensure_loopback_port_available
            with mock.patch.object(worker, "ensure_loopback_port_available", side_effect=lambda: probe(port)), \
                    mock.patch.object(worker.subprocess, "Popen") as spawn:
                with self.assertRaises(OSError) as refused:
                    worker.start_ollama({"ollama_binary": sys.executable, "ollama_models": directory},
                                        Path(directory) / "server.log")
            self.assertIn(refused.exception.errno, (errno.EADDRINUSE, errno.EACCES))
            spawn.assert_not_called()
            self.assertFalse((Path(directory) / "server.log").exists())

    def test_windows_probe_does_not_enable_address_reuse(self):
        with mock.patch.object(worker.os, "name", "nt"), \
                mock.patch.object(worker.socket, "socket") as factory:
            worker.ensure_loopback_port_available(12345)
        probe = factory.return_value.__enter__.return_value
        probe.setsockopt.assert_not_called()
        probe.bind.assert_called_once_with(("127.0.0.1", 12345))


if __name__ == "__main__":
    unittest.main()
