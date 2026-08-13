"""Integration tests for the production SQLite-backed store facade."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from models.task_models import TaskResult, TaskStatus
from state.sqlite_task_store import SqliteTaskStore


class SqliteTaskStoreTest(unittest.TestCase):
    def test_lazy_descriptor_scan_claim_and_result_flow(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = SqliteTaskStore(Path(temp_dir) / "tasks.sqlite3")
            try:
                self.assertEqual(
                    store.register_folder_descriptors(
                        ["folder"],
                        [("Recipe A", "recipes/a.json"), ("Recipe B", "recipes/b.json")],
                    ),
                    1,
                )
                self.assertEqual(store.overall_stats()["total"], 0)
                self.assertFalse(store.overall_stats()["total_final"])

                self.assertEqual(store.insert_task_batch("folder", ["a.jpg", "b.jpg"]), 4)
                self.assertFalse(store.overall_stats()["total_final"])
                store.set_folder_scan_state("folder", "SCANNED")
                self.assertTrue(store.overall_stats()["total_final"])
                messages = store.claim_pending_messages(
                    ["folder"], "RUN", "result.queue", priority=3, limit=2
                )
                self.assertEqual(len(messages), 2)
                self.assertTrue(all(message.priority == 3 for message in messages))
                claimed_record = store.repository.get_task_record(messages[0].request_id)
                self.assertEqual(claimed_record["action"], "RUN")
                self.assertEqual(claimed_record["result_queue"], "result.queue")
                self.assertEqual(claimed_record["priority"], 3)

                request_id = messages[0].request_id
                store.mark_task_sent(request_id)
                self.assertEqual(store.get_task(request_id).status, TaskStatus.SENT)
                self.assertTrue(
                    store.apply_result(TaskResult(request_id=request_id, result=["PASS"]))
                )
                self.assertEqual(store.get_task(request_id).status, TaskStatus.SUCCESS)
                self.assertEqual(store.get_folder_summary("folder").success, 1)
            finally:
                store.close()

    def test_detail_reads_are_page_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = SqliteTaskStore(Path(temp_dir) / "tasks.sqlite3")
            try:
                store.register_folder_descriptors(["folder"], [("R", "r.json")])
                store.insert_task_batch("folder", [f"{index}.jpg" for index in range(700)])

                self.assertEqual(len(store.get_image_tasks("folder")), 500)
                self.assertEqual(len(store.get_image_tasks("folder", offset=500)), 200)
            finally:
                store.close()

    def test_resumable_work_detects_waiting_and_nonterminal_tasks(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = SqliteTaskStore(Path(temp_dir) / "tasks.sqlite3")
            try:
                self.assertFalse(store.has_resumable_work())
                store.register_folder_descriptors(["folder"], [("R", "r.json")])
                self.assertTrue(store.has_resumable_work())
                store.set_folder_scan_state("folder", "EMPTY")
                self.assertFalse(store.has_resumable_work())
                store.insert_task_batch("folder", ["a.jpg"])
                self.assertTrue(store.has_resumable_work())
            finally:
                store.close()

    def test_auto_resume_requires_explicit_start_authorization(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = SqliteTaskStore(Path(temp_dir) / "tasks.sqlite3")
            try:
                store.register_folder_descriptors(["folder"], [("R", "r.json")])
                self.assertTrue(store.has_resumable_work())
                self.assertFalse(store.should_auto_resume())

                store.save_runtime_settings("RUN", "result.queue", 0, 1)
                self.assertTrue(store.should_auto_resume())

                store.disable_auto_resume()
                self.assertFalse(store.should_auto_resume())
                self.assertTrue(store.has_resumable_work())
            finally:
                store.close()

    def test_large_pending_folder_delete_does_not_materialize_request_ids(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = SqliteTaskStore(Path(temp_dir) / "tasks.sqlite3")
            try:
                store.register_folder_descriptors(["folder"], [("R", "r.json")])
                store.insert_task_batch("folder", [f"{index}.jpg" for index in range(1200)])

                removed, blocked, request_ids, removed_count = (
                    store.remove_pending_only_folders(["folder"])
                )

                self.assertEqual(removed, ["folder"])
                self.assertEqual(blocked, [])
                self.assertEqual(request_ids, [])
                self.assertEqual(removed_count, 1200)
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
