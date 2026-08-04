"""Reproducible bounded-memory stress check for the SQLite task repository."""

from __future__ import annotations

import argparse
import ctypes
from pathlib import Path
import tempfile
import time
import tracemalloc
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from state.task_repository import TaskRepository


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tasks", type=int, help="legacy shortcut for one Recipe/image count")
    parser.add_argument("--images", type=int, default=300_000)
    parser.add_argument("--recipes", type=int, default=1)
    parser.add_argument("--folders", type=int, default=1)
    parser.add_argument("--insert-chunk", type=int, default=1_000)
    parser.add_argument("--publish-chunk", type=int, default=500)
    parser.add_argument("--database", type=Path)
    args = parser.parse_args()
    image_count = args.tasks if args.tasks is not None else args.images

    if args.database is not None:
        args.database.parent.mkdir(parents=True, exist_ok=True)
        return _run(
            args.database,
            image_count,
            args.recipes,
            args.folders,
            args.insert_chunk,
            args.publish_chunk,
        )

    with tempfile.TemporaryDirectory() as temp_dir:
        return _run(
            Path(temp_dir) / "stress.sqlite3",
            image_count,
            args.recipes,
            args.folders,
            args.insert_chunk,
            args.publish_chunk,
        )


def _run(
    database: Path,
    image_count: int,
    recipe_count: int,
    folder_count: int,
    insert_chunk: int,
    publish_chunk: int,
) -> int:
    if image_count <= 0 or recipe_count <= 0 or folder_count <= 0:
        raise ValueError("images, recipes, folders must all be positive")
    repository = TaskRepository(database)
    folder_paths = [f"stress-folder-{index:04d}" for index in range(folder_count)]
    recipes = [
        (f"Recipe {index + 1}", f"recipe_{index + 1}.json")
        for index in range(recipe_count)
    ]
    repository.register_folder_descriptors(folder_paths, recipes)
    working_set_before = _windows_working_set()
    tracemalloc.start()
    started = time.perf_counter()
    base_image_count, remainder = divmod(image_count, folder_count)
    global_index = 0
    for folder_index, folder_path in enumerate(folder_paths):
        images_in_folder = base_image_count + (1 if folder_index < remainder else 0)
        for base in range(0, images_in_folder, insert_chunk):
            batch_count = min(insert_chunk, images_in_folder - base)
            repository.insert_task_batch(
                folder_path,
                [
                    f"D:/stress/{folder_index:04d}/image_{global_index + index:08d}.jpg"
                    for index in range(batch_count)
                ],
            )
            global_index += batch_count
    insert_seconds = time.perf_counter() - started
    current, peak = tracemalloc.get_traced_memory()
    working_set_after = _windows_working_set()

    claim_started = time.perf_counter()
    claimed = repository.claim_pending(folder_paths, publish_chunk)
    claim_seconds = time.perf_counter() - claim_started
    page = repository.get_tasks_page(folder_paths[0], limit=500)
    total = repository.count_tasks()
    expected_tasks = image_count * recipe_count
    database_mib = database.stat().st_size / 1024 / 1024
    repository.close()

    print(f"images={image_count}")
    print(f"recipes={recipe_count}")
    print(f"folders={folder_count}")
    print(f"tasks={total}")
    print(f"insert_seconds={insert_seconds:.2f}")
    print(f"python_current_mib={current / 1024 / 1024:.1f}")
    print(f"python_peak_mib={peak / 1024 / 1024:.1f}")
    if working_set_before is not None and working_set_after is not None:
        print(f"working_set_mib={working_set_after[0] / 1024 / 1024:.1f}")
        print(
            f"working_set_delta_mib="
            f"{(working_set_after[0] - working_set_before[0]) / 1024 / 1024:.1f}"
        )
        print(f"process_peak_working_set_mib={working_set_after[1] / 1024 / 1024:.1f}")
    print(f"database_mib={database_mib:.1f}")
    print(f"claim_seconds={claim_seconds:.3f}")
    print(f"claimed={len(claimed)}")
    print(f"page_rows={len(page)}")
    return 0 if total == expected_tasks and len(claimed) <= publish_chunk and len(page) <= 500 else 1


def _windows_working_set() -> tuple[int, int] | None:
    if sys.platform != "win32":
        return None

    class ProcessMemoryCounters(ctypes.Structure):
        _fields_ = [
            ("cb", ctypes.c_ulong),
            ("page_fault_count", ctypes.c_ulong),
            ("peak_working_set_size", ctypes.c_size_t),
            ("working_set_size", ctypes.c_size_t),
            ("quota_peak_paged_pool_usage", ctypes.c_size_t),
            ("quota_paged_pool_usage", ctypes.c_size_t),
            ("quota_peak_non_paged_pool_usage", ctypes.c_size_t),
            ("quota_non_paged_pool_usage", ctypes.c_size_t),
            ("pagefile_usage", ctypes.c_size_t),
            ("peak_pagefile_usage", ctypes.c_size_t),
        ]

    counters = ProcessMemoryCounters()
    counters.cb = ctypes.sizeof(counters)
    kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
    psapi = ctypes.windll.psapi  # type: ignore[attr-defined]
    kernel32.GetCurrentProcess.restype = ctypes.c_void_p
    psapi.GetProcessMemoryInfo.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(ProcessMemoryCounters),
        ctypes.c_ulong,
    ]
    psapi.GetProcessMemoryInfo.restype = ctypes.c_int
    success = psapi.GetProcessMemoryInfo(
        kernel32.GetCurrentProcess(),
        ctypes.byref(counters),
        counters.cb,
    )
    if not success:
        return None
    return int(counters.working_set_size), int(counters.peak_working_set_size)


if __name__ == "__main__":
    raise SystemExit(main())
