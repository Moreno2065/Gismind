"""会话管理 API 端点。参考 docs/01_api_spec.md §5。

----
认证模型（临时方案）:
  所有端点通过 X-User-Id 请求头识别用户。缺失时默认 "anonymous"。
  此方案仅提供最低限度的跨用户隔离，生产环境必须替换为 JWT / OAuth2。
  user_id 存储在 session JSON 的 "user_id" 字段，list_all 按 user_id 过滤。
----
"""

import re
from typing import Optional

from fastapi import APIRouter, HTTPException, Path, Request, Response, status

from app.models.schemas import (
    CreateSessionResponse,
    RenameRequest,
    SessionListResponse,
    SessionMessagesResponse,
    SessionMeta,
)
from app.utils.memory import MemoryStore
from app.utils.session import SessionStore

router = APIRouter()
session_store = SessionStore()
memory_store = MemoryStore()

# session_id 校验：1-128 字符，字母数字 + -_
_SESSION_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,128}$")


def _get_user_id(request: Request) -> str:
    """从 X-User-Id 请求头提取用户标识，缺失时默认 "anonymous"。

    临时认证方案：生产环境应替换为 JWT / OAuth2 并校验签名。
    """
    return request.headers.get("X-User-Id", "anonymous").strip() or "anonymous"


async def _check_session_owner(
    session_id: str, request_user_id: str
) -> Optional[str]:
    """验证 session 归属。返回 session 的 user_id，不匹配时返回 None。"""
    stored_user = await session_store.get_user_id(session_id)
    if stored_user == "anonymous" or request_user_id == "anonymous":
        return stored_user
    if stored_user != request_user_id:
        return None
    return stored_user


@router.post(
    "/api/sessions",
    response_model=CreateSessionResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_session(request: Request):
    """创建一个新会话，返回 session_id 与初始 meta。"""
    user_id = _get_user_id(request)
    session_id = await session_store.create(user_id=user_id)
    meta = await session_store.get_meta(session_id)
    if meta is None:
        # 极端情况：刚 create 又被外部清掉
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={
                "code": "SESSION_CREATE_FAILED",
                "message": "创建会话失败",
            },
        )
    return CreateSessionResponse(
        id=meta.id,
        title=meta.title,
        created_at=meta.created_at,
        updated_at=meta.updated_at,
    )


@router.get("/api/sessions", response_model=SessionListResponse)
async def list_sessions(request: Request):
    """列出当前用户的会话，按 updated_at 降序。"""
    user_id = _get_user_id(request)
    items = await session_store.list_all(limit=200, user_id=user_id)
    return SessionListResponse(items=items)


@router.get("/api/sessions/{session_id}", response_model=SessionMeta)
async def get_session_meta(
    request: Request,
    session_id: str = Path(..., description="会话 ID，1-128 字符，alnum + -_"),
):
    """获取单个会话的元信息。"""
    if not _SESSION_ID_PATTERN.match(session_id):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "INVALID_SESSION_ID",
                "message": "session_id 长度须在 1-128 之间，且只允许字母数字、连字符、下划线",
            },
        )
    user_id = _get_user_id(request)
    owner = await _check_session_owner(session_id, user_id)
    if owner is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "code": "FORBIDDEN",
                "message": "无权访问该会话",
            },
        )
    meta = await session_store.get_meta(session_id)
    if meta is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "code": "SESSION_NOT_FOUND",
                "message": f"会话不存在：{session_id}",
            },
        )
    return meta


@router.get(
    "/api/sessions/{session_id}/messages",
    response_model=SessionMessagesResponse,
)
async def get_session_messages(
    request: Request,
    session_id: str = Path(..., description="会话 ID，1-128 字符，alnum + -_"),
):
    """获取会话的完整消息列表。"""
    if not _SESSION_ID_PATTERN.match(session_id):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "INVALID_SESSION_ID",
                "message": "session_id 长度须在 1-128 之间，且只允许字母数字、连字符、下划线",
            },
        )
    user_id = _get_user_id(request)
    owner = await _check_session_owner(session_id, user_id)
    if owner is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "code": "FORBIDDEN",
                "message": "无权访问该会话",
            },
        )
    messages = await session_store.list_messages(session_id)
    if messages is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "code": "SESSION_NOT_FOUND",
                "message": f"会话不存在：{session_id}",
            },
        )
    return SessionMessagesResponse(messages=messages)


@router.patch("/api/sessions/{session_id}", status_code=status.HTTP_204_NO_CONTENT)
async def rename_session(
    req: RenameRequest,
    request: Request,
    session_id: str = Path(..., description="会话 ID，1-128 字符，alnum + -_"),
):
    """重命名会话。"""
    if not _SESSION_ID_PATTERN.match(session_id):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "INVALID_SESSION_ID",
                "message": "session_id 长度须在 1-128 之间，且只允许字母数字、连字符、下划线",
            },
        )
    user_id = _get_user_id(request)
    owner = await _check_session_owner(session_id, user_id)
    if owner is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "code": "FORBIDDEN",
                "message": "无权访问该会话",
            },
        )
    title = (req.title or "").strip()
    if not title:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "INVALID_TITLE",
                "message": "title 不能为空",
            },
        )
    ok = await session_store.rename(session_id, title)
    if not ok:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "code": "SESSION_NOT_FOUND",
                "message": f"会话不存在：{session_id}",
            },
        )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.delete("/api/sessions/{session_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_session(
    request: Request,
    session_id: str = Path(..., description="会话 ID，1-128 字符，alnum + -_"),
):
    """删除会话，并级联清除其空间记忆。

    即便 session 已过期（不存在），也按幂等成功处理，避免给前端 500。
    """
    if not _SESSION_ID_PATTERN.match(session_id):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "INVALID_SESSION_ID",
                "message": "session_id 长度须在 1-128 之间，且只允许字母数字、连字符、下划线",
            },
        )
    user_id = _get_user_id(request)
    owner = await _check_session_owner(session_id, user_id)
    if owner is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "code": "FORBIDDEN",
                "message": "无权访问该会话",
            },
        )
    try:
        await session_store.clear_session(session_id)
        await memory_store.clear_memories(session_id)
    except Exception:
        # 仅在底层真正异常（如 Redis 连接错误）才上抛 500
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={
                "code": "SESSION_DELETE_FAILED",
                "message": f"删除会话失败：{session_id}",
            },
        )
    return Response(status_code=status.HTTP_204_NO_CONTENT)