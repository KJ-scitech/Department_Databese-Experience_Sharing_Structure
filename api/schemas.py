"""Pydantic 模型：定义接口收什么、返回什么。

分成 In / Out 两套：
  In  = 前端传来的（只含允许写的字段）
  Out = 返回给前端的（不含敏感字段）
"""
from datetime import date, datetime

from pydantic import BaseModel, ConfigDict, Field


# ══ 经验（K1 / K2 / K3）══════════════════════════════════════

# 经验的类型，对应需求文档 K1 的「牢骚/问题/笔记/解答」
EXPERIENCE_TYPES = ("牢骚", "问题", "笔记", "解答")


class ExperienceCreate(BaseModel):
    """录入一条经验（K1）。"""

    content: str = Field(..., min_length=1, description="经验正文")
    type: str = Field("笔记", description="牢骚 / 问题 / 笔记 / 解答")
    tags: list[str] = Field(default_factory=list, description="标签")
    author_id: int | None = Field(None, description="关联 member.id")
    activity_id: int | None = Field(None, description="关联 activity.id")
    participation_id: int | None = Field(None, description="关联 participation.id")
    source: str | None = Field(None, description="来源")


class ExperienceOut(BaseModel):
    """单条经验（K3 元信息）。"""

    exp_id: str
    content: str
    metadata: dict


class SearchHit(BaseModel):
    """一条检索结果（K2）。"""

    exp_id: str
    content: str
    similarity: float = Field(..., description="余弦相似度，越接近 1 越像")
    metadata: dict


class KeywordHit(BaseModel):
    """一条关键词命中（对照用，没有相似度这个概念）。"""

    exp_id: str
    content: str
    metadata: dict


# ══ 成员（K4）════════════════════════════════════════════════

class MemberCreate(BaseModel):
    name: str = Field(..., min_length=1)
    gender: int | None = None
    grade: str | None = None
    major: str | None = None
    join_term: str | None = None
    status: str = "在部"
    org_unit: str | None = None
    title: str | None = None
    member_type: str = "本部门"
    bio_short: str | None = None
    skills: list[str] = Field(default_factory=list)
    # 敏感字段：可以写，但不会从对外接口读出来
    student_no: str | None = None
    phone: str | None = None
    email: str | None = None


class MemberOut(BaseModel):
    """成员对外信息——字段与 v_member_public 视图一致（已脱敏）。"""

    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    gender: int | None = None
    grade: str | None = None
    major: str | None = None
    join_term: str | None = None
    status: str | None = None
    org_unit: str | None = None
    title: str | None = None
    skills: list | None = None
    bio_short: str | None = None
    avatar: str | None = None
    honors: list | None = None


# ══ 活动（K4）════════════════════════════════════════════════

class SeriesCreate(BaseModel):
    name: str
    category: str | None = None
    description: str | None = None
    is_official: bool = False


class SeriesOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    category: str | None = None
    description: str | None = None


class ActivityCreate(BaseModel):
    name: str
    series_id: int | None = None
    term: str | None = None
    event_date: date | None = None
    location: str | None = None
    status: str = "计划"
    description: str | None = None


class ActivityOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    series_id: int | None = None
    name: str
    term: str | None = None
    event_date: date | None = None
    location: str | None = None
    status: str | None = None
    description: str | None = None


# ══ 参与记录（K4）════════════════════════════════════════════

class ParticipationCreate(BaseModel):
    activity_id: int
    member_id: int
    role: str = Field("参与", description="负责人/主讲/执行/志愿者/观众")
    contribution: str | None = None
    hours: float | None = None
    honor: str | None = None
    exp_ids: list[str] = Field(default_factory=list, description="关联的经验 id")


class ParticipationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    activity_id: int
    member_id: int
    role: str
    contribution: str | None = None
    hours: float | None = None
    honor: str | None = None
    exp_ids: list | None = None


# ══ 报表（K5）════════════════════════════════════════════════

class MemberWorkload(BaseModel):
    """按人聚合的工作量。"""

    member_id: int
    name: str
    total: int = Field(..., description="参与总次数")
    roles: dict[str, int] = Field(default_factory=dict, description="各角色次数")
    honors: int = 0


class HistoryItem(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    member_id: int
    member_name: str
    activity_id: int
    activity_name: str
    series_name: str | None = None
    term: str | None = None
    event_date: date | None = None
    role: str
    contribution: str | None = None
    honor: str | None = None
    exp_ids: list | None = None


class HealthOut(BaseModel):
    ok: bool
    database: str
    experience_count: int
    model: str
    embedding_dim: int
