"""Unit tests for image table model behavior."""

from __future__ import annotations

import unittest

from models.task_models import ImageTask, TaskStatus

try:
    from PySide6.QtCore import Qt

    from ui.models.image_table_model import ImageTableModel

    PYSIDE_AVAILABLE = True
except ImportError:  # pragma: no cover
    PYSIDE_AVAILABLE = False


@unittest.skipUnless(PYSIDE_AVAILABLE, "PySide6 is required for image model tests.")
class ImageTableModelTest(unittest.TestCase):
    """Validate display columns for MQ button and file-name rendering."""

    def test_mq_column_and_filename_display(self) -> None:
        model = ImageTableModel()
        model.set_tasks(
            [
                ImageTask(
                    request_id="req-1",
                    image_path=r"D:\\data\\images\\sample_01.jpg",
                    folder_path=r"D:\\data\\images",
                    status=TaskStatus.PENDING,
                )
            ]
        )

        mq_index = model.index(0, 0)
        image_index = model.index(0, 1)
        request_id_index = model.index(0, 2)

        self.assertEqual(model.data(mq_index, Qt.DisplayRole), "보기")
        self.assertEqual(model.data(image_index, Qt.DisplayRole), "sample_01.jpg")
        self.assertEqual(model.data(request_id_index, Qt.DisplayRole), "req-1")


if __name__ == "__main__":
    unittest.main()
