"""空间记忆 API 端点。参考 docs/01_api_spec.md §5/§6。

----
认证模型（临时方案）:
  所有端点通过 X-User-Id 请求头识别用户。缺失时默认 "anonymous"。
  此方案仅提供最低限度的跨用户隔离，生产环境必须替换为 JWT / OAuth2。
  user_id 存储在 memory JSON 的 "user_id" 字段中。
----
"""

from fastapi import APIRouter, HTTPException, Request

from app.utils.memory import MemoryStore

router = APIRouter()
memory_store = MemoryStore()


def _get_user_id(request: Request) -> str:
    """从 X-User-Id 请求头提取用户标识，缺失时默认 "anonymous"。

    临时认证方案：生产环境应替换为 JWT / OAuth2 并校验签名。
    """
    return request.headers.get("X-User-Id", "anonymous").strip() or "anonymous"


@router.get("/api/memory/{session_id}")
async def get_memory(session_id: str, request: Request):
    stored_user = await memory_store.get_user_id(session_id)
    request_user = _get_user_id(request)
    if stored_user != "anonymous" and request_user != "anonymous" and stored_user != request_user:
        raise HTTPException(
            status_code=403,
            detail={
                "code": "FORBIDDEN",
                "message": "无权访问该会话的记忆",
            },
        )
    memories = await memory_store.get_memories(session_id)
    return {"session_id": session_id, "memories": memories}


@router.delete("/api/memory/{session_id}")
async def delete_memory(session_id: str, request: Request):
    stored_user = await memory_store.get_user_id(session_id)
    request_user = _get_user_id(request)
    if stored_user != "anonymous" and request_user != "anonymous" and stored_user != request_user:
        raise HTTPException(
            status_code=403,
            detail={
                "code": "FORBIDDEN",
                "message": "无权删除该会话的记忆",
            },
        )
    await memory_store.clear_memories(session_id)
    return {"deleted": True, "session_id": session_id}
