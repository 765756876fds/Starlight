"""Explicit interpretation at the UI/voice boundary; legacy handlers stay intact."""
from dataclasses import dataclass
from enum import Enum


class OperationStatus(str, Enum):
    CONFIRMED = "confirmed"
    ACCEPTED = "accepted"
    FAILED = "failed"
    UNKNOWN = "unknown"
    NEEDS_PARAMETER = "needs_parameter"
    UNAVAILABLE = "unavailable"
    UNSUPPORTED = "unsupported"
    TIMEOUT = "timeout"


@dataclass(frozen=True)
class OperationResult:
    status: OperationStatus
    message: str


def media_result(value, *, label, action, accepted_only=False):
    # bool identity is intentional: dicts, mocks and None are not proof.
    if value is False:
        return OperationResult(OperationStatus.FAILED, f"{label}{action}未成功")
    if value is True:
        if accepted_only:
            return OperationResult(OperationStatus.ACCEPTED, f"正在加载{label}，请稍候")
        return OperationResult(OperationStatus.CONFIRMED, f"{label}已{action}")
    return OperationResult(OperationStatus.UNKNOWN, f"暂时无法确认{label}{action}结果")
