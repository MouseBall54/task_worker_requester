"""Qt-based single-instance guard for the desktop application."""

from __future__ import annotations

from PySide6.QtNetwork import QLocalServer, QLocalSocket


class SingleInstanceGuard:
    """Own a local server name while the primary app instance is alive."""

    def __init__(self, server_key: str) -> None:
        self._server_key = server_key
        self._server = QLocalServer()
        self._owns_server = False

    @property
    def server_key(self) -> str:
        """Return the unique server key used for duplicate detection."""

        return self._server_key

    def acquire(self) -> bool:
        """Try to become the primary instance.

        Returns ``True`` only when no other running instance is already
        listening on the same local server key.
        """

        probe = QLocalSocket()
        probe.connectToServer(self._server_key)
        if probe.waitForConnected(250):
            probe.disconnectFromServer()
            return False

        probe.abort()
        QLocalServer.removeServer(self._server_key)
        self._owns_server = self._server.listen(self._server_key)
        return self._owns_server

    def release(self) -> None:
        """Release the server key so a later instance can start cleanly."""

        if self._owns_server:
            self._server.close()
            QLocalServer.removeServer(self._server_key)
            self._owns_server = False


def ensure_single_instance(server_key: str) -> SingleInstanceGuard | None:
    """Return a live guard for the primary instance, else ``None``."""

    guard = SingleInstanceGuard(server_key)
    if guard.acquire():
        return guard
    guard.release()
    return None
