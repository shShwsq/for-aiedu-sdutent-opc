"""任务交付物上传路由

POST /uploads(multipart,需登录):
- .zip 结尾:校验(zip-slip / 大小 / 条目数)后解压存树
- 其余:单文件原样存(为合同场景预留)

返回 upload_id,提交任务时放进 TaskCreateRequest.upload_id。
上传长期保留在服务端,任务失败重试 / 完成后追问 resume 都可复用
(上传内容 agent 无法自行重新获取,必须由服务端保留)。
"""
import logging

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile

from app.config import settings
from app.deps import get_current_user
from app.models.user import User
from app.schemas.uploads import UploadResponse
from app.services.uploads import UploadError, save_upload

logger = logging.getLogger(__name__)

router = APIRouter(tags=["uploads"])

# 分块读取大小(1MB;分块是为了超限时能尽早中断,不把整个大文件读进内存)
_CHUNK_SIZE = 1024 * 1024


@router.post("/uploads", response_model=UploadResponse)
async def upload_task_files(
    file: UploadFile = File(..., description=".zip 压缩包或单个文件"),
    current_user: User = Depends(get_current_user),
) -> UploadResponse:
    """上传任务交付物(ZIP 或单文件,需登录)"""
    max_bytes = settings.UPLOAD_MAX_FILE_MB * 1024 * 1024
    chunks: list[bytes] = []
    read = 0
    while True:
        chunk = await file.read(_CHUNK_SIZE)
        if not chunk:
            break
        read += len(chunk)
        if read > max_bytes:
            raise HTTPException(
                status_code=413,
                detail=f"上传内容超过大小上限 {settings.UPLOAD_MAX_FILE_MB}MB",
            )
        chunks.append(chunk)
    data = b"".join(chunks)

    try:
        meta = save_upload(data, file.filename or "upload", current_user.id)
    except UploadError as e:
        raise HTTPException(status_code=422, detail=str(e))
    return UploadResponse(**meta)
