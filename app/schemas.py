"""请求/响应模型。时间一律要求带时区（AwareDatetime），入库前归一化为 UTC。"""
from datetime import datetime
from typing import Optional

from pydantic import AwareDatetime, BaseModel, Field, field_validator


def _clean_allergens(values: list[str]) -> list[str]:
    cleaned = sorted({v.strip() for v in values if v and v.strip()})
    return cleaned


class MaterialCreate(BaseModel):
    batch_id: str = Field(min_length=1)
    allergens: list[str] = []
    pending: bool = False
    declared_at: Optional[AwareDatetime] = None  # 缺省取服务器当前时间

    @field_validator("allergens")
    @classmethod
    def _v_allergens(cls, v):
        return _clean_allergens(v)


class DeclarationCreate(BaseModel):
    """原料声明更正：追加一个新版本，当前有效声明以最新版本为准。"""

    allergens: list[str] = []
    pending: bool = False
    declared_at: Optional[AwareDatetime] = None

    @field_validator("allergens")
    @classmethod
    def _v_allergens(cls, v):
        return _clean_allergens(v)


def _clean_batch_refs(values: list[str]) -> list[str]:
    """批次引用列表：去空白、去空项；空列表或含重复项一律拒绝。"""
    ids = [x.strip() for x in values if x and x.strip()]
    if len(ids) != len(set(ids)):
        raise ValueError("批次号重复")
    if not ids:
        raise ValueError("不能为空")
    return ids


class ProcessingCreate(BaseModel):
    processing_id: str = Field(min_length=1)
    equipment_id: str = Field(min_length=1)
    start_utc: AwareDatetime
    end_utc: AwareDatetime
    inputs: list[str] = Field(min_length=1)
    outputs: list[str] = Field(min_length=1)

    @field_validator("inputs", "outputs")
    @classmethod
    def _v_batches(cls, v):
        return _clean_batch_refs(v)


class ProcessingInputCorrectionCreate(BaseModel):
    """加工投入纠正：完整的正确投入列表（整表替换，非增量补丁）。"""

    inputs: list[str] = Field(min_length=1)

    @field_validator("inputs")
    @classmethod
    def _v_inputs(cls, v):
        return _clean_batch_refs(v)


class CleaningCreate(BaseModel):
    equipment_id: str = Field(min_length=1)
    cleaned_at: AwareDatetime
    verified: bool = True


class LabelCreate(BaseModel):
    supersedes_id: Optional[int] = None  # 关联被替代的旧标签


class InvestigationCreate(BaseModel):
    """调查封存登记输入：批次号、唯一调查号、原因（疑似交叉接触）。"""

    batch_id: str = Field(min_length=1)
    investigation_id: str = Field(min_length=1)
    reason: str = Field(min_length=1)

    @field_validator("batch_id", "investigation_id", "reason")
    @classmethod
    def _v_non_blank(cls, v):
        v = v.strip()
        if not v:
            raise ValueError("不能为空或纯空白")
        return v


# 声明更正预演请求与正式更正同构；清洗撤销预演/凭据提交无请求体，
# 仅以路径参数（清洗 id / 预演凭据）定位。
class DeclarationPreviewCreate(DeclarationCreate):
    """声明更正预演：拟更正内容（不落库）。"""
