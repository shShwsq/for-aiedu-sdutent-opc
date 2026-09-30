"""上传接口的请求/响应 schema"""
from pydantic import BaseModel, Field


class UploadResponse(BaseModel):
    """POST /uploads 的响应

    提交任务时把 upload_id 放进 TaskCreateRequest.upload_id,
    orchestrator 据此把上传内容传输进任务沙箱工作区。
    """

    upload_id: str = Field(..., description="上传唯一标识(提交任务时引用)")
    kind: str = Field(..., description="交付物类型:zip(解压成树) / file(单文件)")
    filename: str = Field(..., description="客户端原始文件名")
    size: int = Field(..., description="上传体字节数")
    file_count: int = Field(..., description="实际文件数(zip 为解压后条目数,单文件为 1)")
