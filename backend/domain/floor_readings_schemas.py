from typing import Annotated, Optional

from pydantic import BaseModel, ConfigDict, FiniteFloat, StringConstraints

NonEmptyText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=160)]


class RecordStep(BaseModel):
    # 允许现场设备附带 source/client_key 等兼容字段；核心字段仍保持原契约。
    model_config = ConfigDict(extra='allow')
    code: NonEmptyText
    panel: NonEmptyText
    survey: NonEmptyText
    x_cm: int
    y_cm: int
    slide: FiniteFloat
    source: Optional[NonEmptyText] = None
    client_key: Optional[NonEmptyText] = None
    claim_code: Optional[NonEmptyText] = None


class VerifyStep(BaseModel):
    model_config = ConfigDict(extra='allow')
    point: NonEmptyText


class ClaimPoint(BaseModel):
    model_config = ConfigDict(extra='allow', populate_by_name=True)
    code: Optional[NonEmptyText] = None
    panel: Optional[NonEmptyText] = None
    survey: Optional[NonEmptyText] = None
    grouping_code: Optional[NonEmptyText] = None
    scan: Optional[NonEmptyText] = None
    client_key: Optional[NonEmptyText] = None
    spacing_cm: Optional[int] = 20


class ResolveConflict(BaseModel):
    model_config = ConfigDict(extra='allow', populate_by_name=True)
    code: Optional[NonEmptyText] = None
    conflict: Optional[NonEmptyText] = None
    conflict_code: Optional[NonEmptyText] = None
    point: Optional[NonEmptyText] = None
    selected_code: Optional[NonEmptyText] = None
    reading_code: Optional[NonEmptyText] = None
    choice: Optional[NonEmptyText] = None
    selected: Optional[NonEmptyText] = None
    winner: Optional[NonEmptyText] = None
    panel: Optional[NonEmptyText] = None
    survey: Optional[NonEmptyText] = None
    x_cm: Optional[int] = None
    y_cm: Optional[int] = None
    note: Optional[str] = None
