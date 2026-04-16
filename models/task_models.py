"""Core domain models for task publish and result tracking."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any


class TaskStatus(StrEnum):
    """Possible states for an image task lifecycle."""

    PENDING = "PENDING"
    SENT = "SENT"
    RUNNING = "RUNNING"
    SUCCESS = "SUCCESS"
    FAIL = "FAIL"
    TIMEOUT = "TIMEOUT"
    ERROR = "ERROR"
    CANCELLED = "CANCELLED"

    @property
    def is_done(self) -> bool:
        """Return whether this status is terminal."""

        return self in {
            TaskStatus.SUCCESS,
            TaskStatus.FAIL,
            TaskStatus.TIMEOUT,
            TaskStatus.ERROR,
            TaskStatus.CANCELLED,
        }


@dataclass(slots=True)
class TaskMessage:
    """Outbound request message payload."""

    request_id: str
    action: str
    QUEUE_NAME: str
    RECIPE_PATH: str
    IMG_LIST: list[str]
    priority: int = 0

    def to_dict(self) -> dict[str, Any]:
        """Serialize as dictionary for JSON body."""

        return {
            "request_id": self.request_id,
            "action": self.action,
            "QUEUE_NAME": self.QUEUE_NAME,
            "RECIPE_PATH": self.RECIPE_PATH,
            "IMG_LIST": self.IMG_LIST,
        }


@dataclass(slots=True)
class TaskResult:
    """Inbound result message payload after parsing."""

    request_id: str
    result: list[str] = field(default_factory=list)
    status: str = ""
    error: str | None = None
    completed_at: str | None = None

    @property
    def is_success(self) -> bool:
        """Success rule from requirements: PASS in result list."""

        return any(item.upper() == "PASS" for item in self.result)


@dataclass(slots=True)
class ImageTask:
    """Runtime status container for one image request."""

    request_id: str
    image_path: str
    folder_path: str
    status: TaskStatus = TaskStatus.PENDING
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    sent_at: datetime | None = None
    completed_at: datetime | None = None
    result: list[str] = field(default_factory=list)
    error_message: str | None = None
    expected_message: dict[str, Any] | None = None
    published_message: dict[str, Any] | None = None
    received_message: dict[str, Any] | None = None
    publish_meta: dict[str, Any] = field(default_factory=dict)
    received_meta: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class FolderSummary:
    """Pre-computed folder aggregate row for fast table updates."""

    folder_path: str
    total: int
    completed: int
    success: int
    fail: int
    timeout: int
    error: int
    progress: float
    status: TaskStatus


@dataclass(slots=True)
class FolderTaskGroup:
    """Image task IDs grouped by image-containing folder."""

    folder_path: str
    task_ids: list[str] = field(default_factory=list)

    def to_summary(self, task_index: dict[str, ImageTask]) -> FolderSummary:
        """Compute aggregate counts for this folder."""

        tasks = [task_index[task_id] for task_id in self.task_ids if task_id in task_index]
        total = len(tasks)
        success = sum(task.status == TaskStatus.SUCCESS for task in tasks)
        fail = sum(task.status == TaskStatus.FAIL for task in tasks)
        timeout = sum(task.status == TaskStatus.TIMEOUT for task in tasks)
        error = sum(task.status == TaskStatus.ERROR for task in tasks)
        completed = sum(task.status.is_done for task in tasks)
        inflight = sum(task.status in {TaskStatus.SENT, TaskStatus.RUNNING} for task in tasks)

        if total == 0:
            status = TaskStatus.PENDING
        elif completed == total:
            if fail or timeout or error:
                status = TaskStatus.FAIL
            else:
                status = TaskStatus.SUCCESS
        elif inflight or completed:
            status = TaskStatus.RUNNING
        else:
            status = TaskStatus.PENDING

        progress = (completed / total * 100.0) if total else 0.0

        return FolderSummary(
            folder_path=self.folder_path,
            total=total,
            completed=completed,
            success=success,
            fail=fail,
            timeout=timeout,
            error=error,
            progress=progress,
            status=status,
        )
