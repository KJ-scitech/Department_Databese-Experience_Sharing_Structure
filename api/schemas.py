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


class ExperienceTagsPatch(BaseModel):
    """改一条经验的标签（K8 打标用）。

    ★ extra="forbid" 是这个类存在的全部理由，不要删。
      Pydantic 默认**静默忽略**多余的字段：传
          {"tags": ["设备"], "content": "把正文也改了"}
      会正常返回 200，但正文根本没改——调用方以为改成功了。
      契约（任务拆解 §4.9）说「只接受 tags，别的字段不开放」，
      那就必须让多余字段报 422，而不是假装没看见。

    要改正文/作者请另开接口：正文一改要重新编码向量，
    作者一改就动了双库关联，两件事都不该混在打标这条路上。
    """

    model_config = ConfigDict(extra="forbid")

    tags: list[str] = Field(default_factory=list, description="整份替换，不是追加")


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

# 成员状态。★ 只用于前端下拉框，**服务端不拿它做校验**：
# 写接口现在没有鉴权，机器人那边可能已经用了别的说法，硬拦会把合法记录挡在门外。
# （member.status 在 schema 里是 VARCHAR，本来就是开放的。）
MEMBER_STATUS = ("在部", "退部", "休假")


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

# 活动状态。同 MEMBER_STATUS：只给前端填下拉框，服务端不校验。
ACTIVITY_STATUS = ("计划", "筹备中", "进行中", "已完成", "已取消")


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

# 参与角色。★ 同样只暴露、不校验——理由见 MEMBER_STATUS 上面的注释。
# 取值来自 ParticipationCreate.role 的 description，不是另起一套说法。
PARTICIPATION_ROLES = ("负责人", "主讲", "执行", "志愿者", "观众", "参与")


# 职务的展示顺序，从高到低。
# ★ 它只用来**排序**，不做校验——理由同上：这一列是开放的 VARCHAR，
#   历史数据或机器人可能写别的说法，硬拦会把合法记录挡在门外。
#   排不上号的（空值、"指导老师"这类）统一排到最后，不会丢人。
TITLE_ORDER = ("主席", "部长", "副部长", "组长", "部员")


def title_rank(title: str | None) -> int:
    """职务的排序权重：越小越靠前；排不上号的给一个大值，落在最后。"""
    try:
        return TITLE_ORDER.index(title or "")
    except ValueError:
        return len(TITLE_ORDER)


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
