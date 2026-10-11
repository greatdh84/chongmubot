"""오류 응답 형식: {"error": {"code": "...", "message": "...", "detail": ...}}"""
from __future__ import annotations

from typing import Any


class AppError(Exception):
    def __init__(self, status: int, code: str, message: str, detail: Any = None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.detail = detail


def not_found(what: str = "대상") -> AppError:
    # 다른 행사·팀의 데이터는 존재 여부도 알리지 않도록 403 대신 404 를 쓴다.
    return AppError(404, "NOT_FOUND", f"{what}을(를) 찾을 수 없습니다")


def forbidden(message: str = "권한이 없습니다") -> AppError:
    return AppError(403, "FORBIDDEN", message)


def unauthorized(message: str = "로그인이 필요합니다") -> AppError:
    return AppError(401, "UNAUTHENTICATED", message)


def conflict(code: str, message: str, detail: Any = None) -> AppError:
    return AppError(409, code, message, detail)


def invalid(code: str, message: str, detail: Any = None) -> AppError:
    return AppError(422, code, message, detail)
