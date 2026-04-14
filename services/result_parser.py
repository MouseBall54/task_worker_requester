"""Result message parser utilities."""

from __future__ import annotations

from typing import Any

from models.task_models import TaskResult


def parse_task_result(
    payload: dict[str, Any],
    correlation_id: str | None = None,
    message_id: str | None = None,
) -> TaskResult:
    """Parse raw broker payload into validated TaskResult.

    The parser tolerates partially malformed payloads and normalizes fields.
    """

    request_id = str(payload.get("request_id") or correlation_id or message_id or "").strip()
    if not request_id:
        raise ValueError("request_id 를 찾을 수 없습니다.")

    raw_result = payload.get("result", [])
    if isinstance(raw_result, list):
        result = [str(item) for item in raw_result]
    elif raw_result is None:
        result = []
    else:
        result = [str(raw_result)]

    error_raw = payload.get("error")
    error = str(error_raw) if error_raw not in (None, "") else None

    return TaskResult(
        request_id=request_id,
        result=result,
        status=str(payload.get("status", "")),
        error=error,
        completed_at=str(payload.get("completed_at")) if payload.get("completed_at") else None,
    )
