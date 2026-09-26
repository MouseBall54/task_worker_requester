import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from app.updater import find_newer_installer
from config.models import AppConfig, RabbitMQConfig, UpdateConfig

try:
    from PySide6.QtWidgets import QApplication, QMessageBox

    from ui.main_window import MainWindow

    PYSIDE_AVAILABLE = True
except ImportError:  # pragma: no cover
    PYSIDE_AVAILABLE = False


class FindNewerInstallerTest(unittest.TestCase):
    def test_picks_highest_numeric_version_above_current(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            for name in (
                "IPDK_plusSetup_26.9.23.exe",
                "IPDK_plusSetup_26.10.1.exe",
                "IPDK_plusSetup_26.9.30.exe",
                "notes.txt",
            ):
                (Path(tmp) / name).touch()

            result = find_newer_installer(tmp, "26.9.23")

        self.assertIsNotNone(result)
        self.assertEqual(result[0], "26.10.1")
        self.assertEqual(result[1].name, "IPDK_plusSetup_26.10.1.exe")

    def test_returns_none_when_current_is_latest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "IPDK_plusSetup_26.9.23.exe").touch()

            self.assertIsNone(find_newer_installer(tmp, "26.9.23"))

    def test_missing_share_raises_os_error(self) -> None:
        with self.assertRaises(OSError):
            find_newer_installer(Path(tempfile.gettempdir()) / "no_such_ipdk_share", "1.0")


@unittest.skipUnless(PYSIDE_AVAILABLE, "PySide6 is required for update UI tests.")
class MainWindowUpdateCheckTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._app = QApplication.instance() or QApplication([])

    def _run_check(self, share_dir: Path, answer=None):  # noqa: ANN001, ANN202
        """Run a manual update check against ``share_dir`` and return the patched mocks."""

        root = Path(self._temp.name)
        window = MainWindow(
            AppConfig(
                rabbitmq=RabbitMQConfig(host="127.0.0.1", port=5672, username="guest", password="guest"),
                update=UpdateConfig(share_dir=str(share_dir)),
                mock_mode=True,
            ),
            folder_index_database_path=root / "index.sqlite3",
            ui_settings_path=root / "ui_state.ini",
        )
        window.show()
        with (
            patch.object(QMessageBox, "question", return_value=answer or QMessageBox.StandardButton.No) as question,
            patch.object(QMessageBox, "information") as information,
            patch.object(QMessageBox, "warning") as warning,
            patch("ui.main_window.os.startfile", create=True) as startfile,
        ):
            window.check_for_updates(manual=True)
            deadline = time.monotonic() + 5
            while not window.action_check_update.isEnabled() and time.monotonic() < deadline:
                self._app.processEvents()
                time.sleep(0.01)
            self.assertTrue(window.action_check_update.isEnabled(), "update check did not finish")
        visible = window.isVisible()
        window.close()
        return question, information, warning, startfile, visible

    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.share = Path(self._temp.name) / "share"
        self.share.mkdir()

    def tearDown(self) -> None:
        self._temp.cleanup()

    def test_newer_installer_is_launched_and_window_closes_when_accepted(self) -> None:
        installer = self.share / "IPDK_plusSetup_99.0.0.exe"
        installer.touch()

        question, _info, warning, startfile, visible = self._run_check(
            self.share, answer=QMessageBox.StandardButton.Yes
        )

        self.assertIn("99.0.0", question.call_args.args[2])
        startfile.assert_called_once_with(installer)
        warning.assert_not_called()
        self.assertFalse(visible)

    def test_declining_keeps_window_open_without_launching(self) -> None:
        (self.share / "IPDK_plusSetup_99.0.0.exe").touch()

        question, _info, _warning, startfile, visible = self._run_check(self.share)

        question.assert_called_once()
        startfile.assert_not_called()
        self.assertTrue(visible)

    def test_up_to_date_share_reports_latest(self) -> None:
        (self.share / "IPDK_plusSetup_1.0.0.exe").touch()

        question, information, _warning, startfile, _visible = self._run_check(self.share)

        question.assert_not_called()
        startfile.assert_not_called()
        self.assertIn("최신 버전", information.call_args.args[2])

    def test_unreachable_share_warns(self) -> None:
        _q, _info, warning, startfile, visible = self._run_check(self.share / "missing")

        startfile.assert_not_called()
        self.assertIn("읽을 수 없습니다", warning.call_args.args[2])
        self.assertTrue(visible)


if __name__ == "__main__":
    unittest.main()
