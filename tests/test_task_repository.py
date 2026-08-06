"""Tests for the bounded-memory SQLite task repository."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
import sqlite3

from models.task_models import TaskStatus
from state.task_repository import TaskRepository


class TaskRepositoryTest(unittest.TestCase):
    def test_descriptors_do_not_create_tasks_until_images_are_inserted(self) -> None:
        repository = TaskRepository()
        try:
            added = repository.register_folder_descriptors(
                ["folder_a", "folder_b"],
                [("Recipe A", "recipes/a.json")],
            )

            self.assertEqual(added, 2)
            self.assertEqual(repository.count_tasks(), 0)
            self.assertEqual(
                [row["folder_path"] for row in repository.list_folder_descriptors()],
                ["folder_a", "folder_b"],
            )
        finally:
            repository.close()

    def test_batch_insert_preserves_recipe_snapshots_and_deduplicates_pairs(self) -> None:
        repository = TaskRepository()
        try:
            recipes = [("Recipe A", "recipes/a.json"), ("Recipe B", "recipes/b.json")]
            repository.register_folder_descriptors(["folder"], recipes)

            self.assertEqual(repository.insert_task_batch("folder", ["a.jpg", "b.jpg"]), 4)
            self.assertEqual(repository.insert_task_batch("folder", ["a.jpg"]), 0)

            rows = repository.get_tasks_page("folder", limit=10)
            self.assertEqual(len(rows), 4)
            self.assertEqual({task.recipe_path for task in rows}, {"recipes/a.json", "recipes/b.json"})
            self.assertEqual(repository.overall_counts()["pending"], 4)
        finally:
            repository.close()

    def test_claim_is_chunked_and_updates_counters(self) -> None:
        repository = TaskRepository()
        try:
            repository.register_folder_descriptors(["folder"], [("R", "r.json")])
            repository.insert_task_batch("folder", [f"{index}.jpg" for index in range(12)])

            claimed = repository.claim_pending(["folder"], limit=5)

            self.assertEqual(len(claimed), 5)
            self.assertTrue(all(task.status == TaskStatus.CLAIMED for task in claimed))
            counts = repository.overall_counts()
            self.assertEqual(counts["pending"], 7)
            self.assertEqual(counts["claimed"], 5)
        finally:
            repository.close()

    def test_transition_updates_folder_and_overall_counters_in_constant_time(self) -> None:
        repository = TaskRepository()
        try:
            repository.register_folder_descriptors(["folder"], [("R", "r.json")])
            repository.insert_task_batch("folder", ["a.jpg"])
            task = repository.claim_pending(["folder"], limit=1)[0]

            self.assertTrue(
                repository.transition_task(
                    task.request_id,
                    TaskStatus.SENT,
                    {TaskStatus.CLAIMED},
                    sent_at="2026-08-04T00:00:00+00:00",
                )
            )
            self.assertTrue(
                repository.transition_task(
                    task.request_id,
                    TaskStatus.SUCCESS,
                    {TaskStatus.SENT, TaskStatus.RUNNING},
                    completed_at="2026-08-04T00:00:01+00:00",
                    result_json='["PASS"]',
                )
            )

            summary = repository.get_folder_summary("folder")
            self.assertIsNotNone(summary)
            self.assertEqual(summary.completed, 1)
            self.assertEqual(summary.success, 1)
            self.assertEqual(summary.status, TaskStatus.SUCCESS)
        finally:
            repository.close()

    def test_claimed_tasks_are_recovered_after_restart(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = Path(temp_dir) / "tasks.sqlite3"
            first = TaskRepository(database_path)
            first.register_folder_descriptors(["folder"], [("R", "r.json")])
            first.insert_task_batch("folder", ["a.jpg", "b.jpg"])
            first.claim_pending(["folder"], limit=1)
            session_id = first.session_id
            first.close()

            resumed = TaskRepository(database_path)
            try:
                self.assertEqual(resumed.session_id, session_id)
                counts = resumed.overall_counts()
                self.assertEqual(counts["pending"], 2)
                self.assertEqual(counts["claimed"], 0)
            finally:
                resumed.close()

    def test_interrupted_scan_is_recovered_to_waiting_and_deduplicates_rescan(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = Path(temp_dir) / "tasks.sqlite3"
            first = TaskRepository(database_path)
            first.register_folder_descriptors(["folder"], [("R", "r.json")])
            first.set_folder_scan_state("folder", "SCANNING")
            first.insert_task_batch("folder", ["a.jpg"])
            first.close()

            resumed = TaskRepository(database_path)
            try:
                self.assertEqual(resumed.list_folder_descriptors()[0]["scan_state"], "WAITING")
                self.assertEqual(resumed.insert_task_batch("folder", ["a.jpg", "b.jpg"]), 1)
                self.assertEqual(resumed.count_tasks(), 2)
            finally:
                resumed.close()

    def test_detail_query_returns_only_requested_page(self) -> None:
        repository = TaskRepository()
        try:
            repository.register_folder_descriptors(["folder"], [("R", "r.json")])
            repository.insert_task_batch("folder", [f"image_{index:04d}.jpg" for index in range(1200)])

            first_page = repository.get_tasks_page("folder", offset=0, limit=500)
            second_page = repository.get_tasks_page("folder", offset=500, limit=500)

            self.assertEqual(len(first_page), 500)
            self.assertEqual(len(second_page), 500)
            self.assertNotEqual(first_page[0].request_id, second_page[0].request_id)
        finally:
            repository.close()

    def test_detail_pages_sort_numeric_filename_stems_across_page_boundaries(self) -> None:
        repository = TaskRepository()
        try:
            repository.register_folder_descriptors(["folder"], [("R", "r.json")])
            repository.insert_task_batch(
                "folder",
                ["folder/20.jpg", "folder/3.jpg", "folder/10.jpg", "folder/2.jpg", "folder/1.jpg"],
            )

            first_page = repository.get_tasks_page("folder", offset=0, limit=2)
            second_page = repository.get_tasks_page("folder", offset=2, limit=2)
            third_page = repository.get_tasks_page("folder", offset=4, limit=2)

            ordered_names = [
                Path(task.image_path).name
                for task in [*first_page, *second_page, *third_page]
            ]
            self.assertEqual(ordered_names, ["1.jpg", "2.jpg", "3.jpg", "10.jpg", "20.jpg"])
        finally:
            repository.close()

    def test_runtime_settings_survive_restart(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = Path(temp_dir) / "tasks.sqlite3"
            first = TaskRepository(database_path)
            first.save_runtime_settings("RUN", "result.saved", 4, 7)
            session_id = first.session_id
            first.close()

            resumed = TaskRepository(database_path)
            try:
                self.assertEqual(resumed.session_id, session_id)
                self.assertEqual(
                    resumed.get_runtime_settings(),
                    {
                        "action": "RUN",
                        "result_queue": "result.saved",
                        "priority": 4,
                        "polling_interval": 7,
                    },
                )
            finally:
                resumed.close()

    def test_existing_database_adds_restart_metadata_columns(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = Path(temp_dir) / "tasks.sqlite3"
            connection = sqlite3.connect(database_path)
            connection.execute(
                """
                CREATE TABLE sessions(
                    session_id TEXT PRIMARY KEY,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    state TEXT NOT NULL
                )
                """
            )
            connection.commit()
            connection.close()

            repository = TaskRepository(database_path)
            try:
                repository.save_runtime_settings("RUN", "result.saved", 2, 3)
                self.assertEqual(repository.get_runtime_settings()["priority"], 2)
            finally:
                repository.close()

    def test_late_scan_batch_cannot_recreate_deleted_descriptor(self) -> None:
        repository = TaskRepository()
        try:
            repository.register_folder_descriptors(["folder"], [("R", "r.json")])
            removed, blocked, _ = repository.delete_pending_folders(
                ["folder"], include_request_ids=False
            )

            self.assertEqual(removed, ["folder"])
            self.assertEqual(blocked, [])
            self.assertEqual(repository.insert_task_batch("folder", ["late.jpg"]), 0)
            self.assertEqual(repository.list_folder_descriptors(), [])
        finally:
            repository.close()


if __name__ == "__main__":
    unittest.main()
