"""Tests for the bounded-memory SQLite task repository."""

from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

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
            self.assertEqual(repository.register_folder_descriptors(["folder"], recipes), 2)
            queue_rows = repository.list_folder_descriptors()
            queue_keys = [str(row["folder_path"]) for row in queue_rows]

            self.assertEqual(repository.insert_task_batch(queue_keys[0], ["a.jpg", "b.jpg"]), 2)
            self.assertEqual(repository.insert_task_batch(queue_keys[1], ["a.jpg", "b.jpg"]), 2)
            self.assertEqual(repository.insert_task_batch("folder", ["a.jpg"]), 0)

            first_rows = repository.get_tasks_page(queue_keys[0], limit=10)
            second_rows = repository.get_tasks_page(queue_keys[1], limit=10)
            self.assertEqual({task.recipe_path for task in first_rows}, {"recipes/a.json"})
            self.assertEqual({task.recipe_path for task in second_rows}, {"recipes/b.json"})
            self.assertTrue(all(row["source_path"] == "folder" for row in queue_rows))
            self.assertEqual(repository.overall_counts()["pending"], 4)
        finally:
            repository.close()

    def test_existing_multi_recipe_folder_is_migrated_to_independent_queue_rows(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database = Path(temp_dir) / "tasks.sqlite3"
            repository = TaskRepository(database)
            repository.register_folder_descriptors(["folder"], [("A", "a.json")])
            repository.insert_task_batch("folder", ["a.jpg"])
            session_id = repository.session_id
            with repository._transaction() as cursor:  # noqa: SLF001
                cursor.execute(
                    """
                    UPDATE folders SET source_path = '', recipe_alias = '', recipe_path = '',
                        recipes_json = ?, total_count = 2, pending_count = 2
                    WHERE session_id = ? AND folder_path = 'folder'
                    """,
                    (json.dumps([("A", "a.json"), ("B", "b.json")]), session_id),
                )
                cursor.execute(
                    """
                    INSERT INTO tasks(
                        session_id, request_id, folder_path, image_path, recipe_alias,
                        recipe_path, status, created_at
                    ) VALUES (?, 'legacy-b', 'folder', 'a.jpg', 'B', 'b.json', 'PENDING', ?)
                    """,
                    (session_id, "2026-08-13T00:00:00+00:00"),
                )
            repository.close()

            restored = TaskRepository(database, session_id=session_id)
            try:
                summaries = restored.get_folder_summaries()
                self.assertEqual(len(summaries), 2)
                self.assertEqual([row.source_path for row in summaries], ["folder", "folder"])
                self.assertEqual([row.recipe_aliases for row in summaries], [("A",), ("B",)])
                self.assertEqual([row.total for row in summaries], [1, 1])
            finally:
                restored.close()

    def test_scanned_sibling_inventory_is_reused_for_another_recipe(self) -> None:
        repository = TaskRepository()
        try:
            repository.register_folder_descriptors(
                ["folder"], [("A", "a.json"), ("B", "b.json")]
            )
            first_key, second_key = [
                str(row["folder_path"]) for row in repository.list_folder_descriptors()
            ]
            repository.insert_task_batch(first_key, ["1.jpg", "2.jpg"])
            repository.set_folder_scan_state(first_key, "SCANNED")

            self.assertTrue(repository.populate_folder_from_scanned_sibling(second_key))

            second_tasks = repository.get_tasks_page(second_key, limit=10)
            self.assertEqual([task.image_path for task in second_tasks], ["1.jpg", "2.jpg"])
            self.assertEqual({task.recipe_path for task in second_tasks}, {"b.json"})
            self.assertEqual(repository.get_folder_summary(second_key).total, 2)
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
                self.assertTrue(repository.is_resume_enabled())
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

    def test_user_pause_survives_restart_without_auto_resume_state(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = Path(temp_dir) / "tasks.sqlite3"
            first = TaskRepository(database_path)
            first.register_folder_descriptors(["folder"], [("R", "r.json")])
            first.insert_task_batch("folder", ["a.jpg", "b.jpg"])
            first.save_runtime_settings("RUN", "result.queue", 1, 5)
            first.pause_by_user()
            session_id = first.session_id
            first.close()

            restored = TaskRepository(database_path)
            try:
                self.assertEqual(restored.session_id, session_id)
                self.assertEqual(restored.session_state(), "PAUSED_BY_USER")
                self.assertTrue(restored.is_resume_enabled())
                self.assertEqual(restored.paused_work_count(), 2)
                restored.resume_by_user()
                self.assertEqual(restored.session_state(), "ACTIVE")
            finally:
                restored.close()

    def test_folder_reorder_and_hold_only_allow_unclaimed_work(self) -> None:
        repository = TaskRepository()
        try:
            repository.register_folder_descriptors(
                ["first", "second", "third"], [("R", "r.json")]
            )
            for folder in ("first", "second", "third"):
                repository.insert_task_batch(folder, [f"{folder}.jpg"])

            moved, blocked = repository.reorder_folders(["third"], "top")
            self.assertEqual(moved, ["third"])
            self.assertEqual(blocked, [])
            self.assertEqual(
                [row["folder_path"] for row in repository.list_folder_descriptors()],
                ["third", "first", "second"],
            )

            held, blocked = repository.set_folders_held(["second"], True)
            self.assertEqual(held, ["second"])
            self.assertEqual(blocked, [])
            claimed = repository.claim_pending(["second", "third"], limit=2)
            self.assertEqual([task.folder_path for task in claimed], ["third"])

            moved, blocked = repository.reorder_folders(["third"], "down")
            self.assertEqual(moved, [])
            self.assertEqual(blocked, ["third"])
        finally:
            repository.close()

    def test_folder_priority_persists_and_bottom_keeps_running_slots_fixed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = Path(temp_dir) / "tasks.sqlite3"
            repository = TaskRepository(database_path)
            try:
                repository.register_folder_descriptors(
                    ["first", "running", "third", "fourth"], [("R", "r.json")]
                )
                for folder in ("first", "running", "third", "fourth"):
                    repository.insert_task_batch(folder, [f"{folder}.jpg"])
                task = repository.claim_pending(["running"], limit=1)[0]
                repository.transition_task(
                    task.request_id,
                    TaskStatus.SENT,
                    {TaskStatus.CLAIMED},
                    sent_at="2026-08-13T00:00:00+00:00",
                )

                moved, blocked = repository.reorder_folders(["first"], "bottom")

                self.assertEqual(moved, ["first"])
                self.assertEqual(blocked, [])
                self.assertEqual(
                    [row["folder_path"] for row in repository.list_folder_descriptors()],
                    ["third", "running", "fourth", "first"],
                )
                priorities = {
                    summary.folder_path: summary.queue_priority
                    for summary in repository.get_folder_summaries()
                }
                self.assertEqual(
                    priorities,
                    {"third": 1, "running": 2, "fourth": 3, "first": 4},
                )
            finally:
                repository.close()

            restored = TaskRepository(database_path)
            try:
                self.assertEqual(
                    [row["folder_path"] for row in restored.list_folder_descriptors()],
                    ["third", "running", "fourth", "first"],
                )
                self.assertEqual(
                    [summary.queue_priority for summary in restored.get_folder_summaries()],
                    [1, 2, 3, 4],
                )
            finally:
                restored.close()

    def test_reset_archives_history_and_starts_fresh_session(self) -> None:
        repository = TaskRepository()
        try:
            original_session = repository.session_id
            repository.register_folder_descriptors(["folder"], [("R", "r.json")])
            repository.insert_task_batch("folder", ["a.jpg"])

            repository.clear_session()

            self.assertNotEqual(repository.session_id, original_session)
            self.assertEqual(repository.list_folder_descriptors(), [])
            history = repository.list_run_history()
            self.assertEqual(len(history), 1)
            self.assertEqual(history[0].session_id, original_session)
            self.assertEqual(history[0].state, "RESET")
            self.assertEqual(history[0].total, 1)
            self.assertEqual(len(repository.get_history_task_rows(original_session)), 1)
        finally:
            repository.close()

    def test_history_reports_average_and_error_types_and_prunes_oldest(self) -> None:
        repository = TaskRepository()
        try:
            for index in range(3):
                folder = f"folder-{index}"
                repository.register_folder_descriptors([folder], [("R", "r.json")])
                repository.insert_task_batch(folder, [f"{index}.jpg"])
                task = repository.claim_pending([folder], limit=1)[0]
                repository.transition_task(
                    task.request_id,
                    TaskStatus.SENT,
                    {TaskStatus.CLAIMED},
                    sent_at="2026-08-13T00:00:00+00:00",
                )
                repository.transition_task(
                    task.request_id,
                    TaskStatus.ERROR,
                    {TaskStatus.SENT},
                    completed_at="2026-08-13T00:00:10+00:00",
                    error_message="worker unavailable",
                )
                repository.clear_session()

            history = repository.list_run_history()
            self.assertEqual(len(history), 3)
            self.assertAlmostEqual(history[0].avg_processing_seconds or 0.0, 10.0, places=2)
            self.assertEqual(history[0].error_types, ("worker unavailable (1)",))

            self.assertEqual(repository.prune_history(1), 2)
            self.assertEqual(len(repository.list_run_history()), 1)
        finally:
            repository.close()


if __name__ == "__main__":
    unittest.main()
