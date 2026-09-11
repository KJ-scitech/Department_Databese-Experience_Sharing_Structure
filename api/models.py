"""ORM 模型：与 schema/schema.sql 一一对应。

⚠️ schema/schema.sql 是关系模式的唯一来源，由部长维护，我们不要手改。
   要改表结构，就在本仓库开分支改、提 PR。这个文件只是它的 Python 映射。

7 张表：member / activity_series / activity / participation
        member_position / privacy_consent / account
2 个视图：v_member_public（脱敏）/ v_member_history（履历聚合）
"""
from datetime import date, datetime

from sqlalchemy import (
    JSON,
    BigInteger,
    Date,
    DateTime,
    Integer,
    Numeric,
    String,
    Text,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base

# 表名统一用反引号包住的前提是：不要用 MySQL 关键字做表名。
# 这就是为什么职务表叫 member_position 而不是 position
# （POSITION(x IN y) 是 MySQL 内置函数，裸写会报 ER_PARSE_ERROR）。


class Member(Base):
    """成员档案。内外分层：敏感字段只内部可见，对外走 v_member_public 视图。"""

    __tablename__ = "member"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(50))

    # ↓ 敏感字段（学号/班级/生日/联系方式），对外不可见
    student_no: Mapped[str | None] = mapped_column(String(30))
    class_no: Mapped[str | None] = mapped_column(String(50))
    birthday: Mapped[date | None] = mapped_column(Date)
    phone: Mapped[str | None] = mapped_column(String(20))
    qq: Mapped[str | None] = mapped_column(String(20))
    wechat: Mapped[str | None] = mapped_column(String(50))
    email: Mapped[str | None] = mapped_column(String(100))

    # ↓ 非敏感字段
    gender: Mapped[int | None] = mapped_column(Integer, comment="0未知 1男 2女")
    grade: Mapped[str | None] = mapped_column(String(10))
    major: Mapped[str | None] = mapped_column(String(100))
    join_term: Mapped[str | None] = mapped_column(String(20))
    status: Mapped[str] = mapped_column(String(20), default="在部")
    org_unit: Mapped[str | None] = mapped_column(String(50))
    title: Mapped[str | None] = mapped_column(String(50))
    member_type: Mapped[str] = mapped_column(String(20), default="本部门")
    skills: Mapped[dict | None] = mapped_column(JSON)
    bio_short: Mapped[str | None] = mapped_column(String(200))
    avatar: Mapped[str | None] = mapped_column(String(255))
    honors: Mapped[dict | None] = mapped_column(JSON)

    # ↓ 内部字段
    bio_long: Mapped[str | None] = mapped_column(Text)
    contribution: Mapped[str | None] = mapped_column(Text)
    remark: Mapped[str | None] = mapped_column(Text)

    # ⚠️ 必须写 server_default。
    # schema 里是 `NOT NULL DEFAULT CURRENT_TIMESTAMP`——
    # 不给 server_default 的话 SQLAlchemy 会把它当普通列，显式插入 NULL，
    # 然后 MySQL 报 1048 "Column 'created_at' cannot be null"。
    # 声明了 server_default，SQLAlchemy 才知道「这列交给数据库填」，从而不在 INSERT 里带它。
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), server_onupdate=func.now()
    )
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime, comment="软删除")


class ActivitySeries(Base):
    """活动系列：品牌/栏目，如「惟学沙龙」。"""

    __tablename__ = "activity_series"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(100))
    category: Mapped[str | None] = mapped_column(String(50))
    description: Mapped[str | None] = mapped_column(Text)
    is_official: Mapped[int] = mapped_column(Integer, default=0)
    # ⚠️ 必须写 server_default。
    # schema 里是 `NOT NULL DEFAULT CURRENT_TIMESTAMP`——
    # 不给 server_default 的话 SQLAlchemy 会把它当普通列，显式插入 NULL，
    # 然后 MySQL 报 1048 "Column 'created_at' cannot be null"。
    # 声明了 server_default，SQLAlchemy 才知道「这列交给数据库填」，从而不在 INSERT 里带它。
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), server_onupdate=func.now()
    )


class Activity(Base):
    """活动场次：系列下具体的一次。series_id 可空（一次性活动）。"""

    __tablename__ = "activity"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    series_id: Mapped[int | None] = mapped_column(BigInteger)
    name: Mapped[str] = mapped_column(String(200))
    term: Mapped[str | None] = mapped_column(String(20))
    event_date: Mapped[date | None] = mapped_column(Date)
    location: Mapped[str | None] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(20), default="计划")
    article_url: Mapped[str | None] = mapped_column(String(500))
    description: Mapped[str | None] = mapped_column(Text)
    # ⚠️ 必须写 server_default。
    # schema 里是 `NOT NULL DEFAULT CURRENT_TIMESTAMP`——
    # 不给 server_default 的话 SQLAlchemy 会把它当普通列，显式插入 NULL，
    # 然后 MySQL 报 1048 "Column 'created_at' cannot be null"。
    # 声明了 server_default，SQLAlchemy 才知道「这列交给数据库填」，从而不在 INSERT 里带它。
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), server_onupdate=func.now()
    )


class Participation(Base):
    """参与记录：谁 × 哪个活动 × 什么分工。

    ⚠️ 唯一键是 (activity_id, member_id, role)，不是 (活动, 人)。
       也就是说**一个人在一个活动里可以有多个角色**（既当主讲又当执行）。
    """

    __tablename__ = "participation"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    activity_id: Mapped[int] = mapped_column(BigInteger)
    member_id: Mapped[int] = mapped_column(BigInteger)
    role: Mapped[str] = mapped_column(String(50), default="参与")
    contribution: Mapped[str | None] = mapped_column(Text)
    hours: Mapped[float | None] = mapped_column(Numeric(5, 1))
    honor: Mapped[str | None] = mapped_column(String(100))

    # 关联向量库里的经验 id，形如 ["exp-3", "exp-7"]
    # 这是 MySQL 侧通往向量库的唯一入口（经验本体不在 MySQL 里）
    exp_ids: Mapped[list | None] = mapped_column(JSON)

    # ⚠️ 必须写 server_default。
    # schema 里是 `NOT NULL DEFAULT CURRENT_TIMESTAMP`——
    # 不给 server_default 的话 SQLAlchemy 会把它当普通列，显式插入 NULL，
    # 然后 MySQL 报 1048 "Column 'created_at' cannot be null"。
    # 声明了 server_default，SQLAlchemy 才知道「这列交给数据库填」，从而不在 INSERT 里带它。
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), server_onupdate=func.now()
    )


class MemberPosition(Base):
    """职务/任期：支持多职务、换届。"""

    __tablename__ = "member_position"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    member_id: Mapped[int] = mapped_column(BigInteger)
    title: Mapped[str] = mapped_column(String(50))
    org_unit: Mapped[str | None] = mapped_column(String(50))
    term: Mapped[str] = mapped_column(String(20))
    is_current: Mapped[int] = mapped_column(Integer, default=1)
    # ⚠️ 必须写 server_default。
    # schema 里是 `NOT NULL DEFAULT CURRENT_TIMESTAMP`——
    # 不给 server_default 的话 SQLAlchemy 会把它当普通列，显式插入 NULL，
    # 然后 MySQL 报 1048 "Column 'created_at' cannot be null"。
    # 声明了 server_default，SQLAlchemy 才知道「这列交给数据库填」，从而不在 INSERT 里带它。
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class PrivacyConsent(Base):
    """隐私授权记录。"""

    __tablename__ = "privacy_consent"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    member_id: Mapped[int] = mapped_column(BigInteger)
    consented: Mapped[int] = mapped_column(Integer, default=0)
    scope: Mapped[str | None] = mapped_column(String(50))
    policy_version: Mapped[str | None] = mapped_column(String(20))
    consented_at: Mapped[datetime | None] = mapped_column(DateTime)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime)


class Account(Base):
    """账号：学校统一身份（CAS）绑定。"""

    __tablename__ = "account"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    member_id: Mapped[int] = mapped_column(BigInteger)
    cas_uid: Mapped[str] = mapped_column(String(50))
    role: Mapped[str] = mapped_column(String(20), default="部员")
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime)
    # ⚠️ 必须写 server_default。
    # schema 里是 `NOT NULL DEFAULT CURRENT_TIMESTAMP`——
    # 不给 server_default 的话 SQLAlchemy 会把它当普通列，显式插入 NULL，
    # 然后 MySQL 报 1048 "Column 'created_at' cannot be null"。
    # 声明了 server_default，SQLAlchemy 才知道「这列交给数据库填」，从而不在 INSERT 里带它。
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class MemberPublic(Base):
    """★ 对外简版成员卡（视图，自动脱敏）。

    **这是脱敏规则的唯一事实来源。** 写查询时直接查这个视图，
    不要在代码里再抄一份字段清单——抄了就有两处规则要维护，
    视图改了代码没改，就会报 Unknown column。
    """

    __tablename__ = "v_member_public"

    # 视图在 SQLAlchemy 里也要声明主键（这里用 id），否则无法映射
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    name: Mapped[str] = mapped_column(String(50))
    gender: Mapped[int | None] = mapped_column(Integer)
    grade: Mapped[str | None] = mapped_column(String(10))
    major: Mapped[str | None] = mapped_column(String(100))
    join_term: Mapped[str | None] = mapped_column(String(20))
    status: Mapped[str | None] = mapped_column(String(20))
    org_unit: Mapped[str | None] = mapped_column(String(50))
    title: Mapped[str | None] = mapped_column(String(50))
    skills: Mapped[dict | None] = mapped_column(JSON)
    bio_short: Mapped[str | None] = mapped_column(String(200))
    avatar: Mapped[str | None] = mapped_column(String(255))
    honors: Mapped[dict | None] = mapped_column(JSON)


class MemberHistory(Base):
    """★ 成员活动履历（视图）。

    视图已经把 participation × member × activity × activity_series
    四张表连好了，查履历不用自己写 JOIN。
    视图没有单列主键，这里用 (member_id, activity_id, role) 组成复合主键，
    和 participation 的唯一键对应。
    """

    __tablename__ = "v_member_history"

    member_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    activity_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    role: Mapped[str] = mapped_column(String(50), primary_key=True)

    member_name: Mapped[str | None] = mapped_column(String(50))
    activity_name: Mapped[str | None] = mapped_column(String(200))
    series_name: Mapped[str | None] = mapped_column(String(100))
    term: Mapped[str | None] = mapped_column(String(20))
    event_date: Mapped[date | None] = mapped_column(Date)
    contribution: Mapped[str | None] = mapped_column(Text)
    honor: Mapped[str | None] = mapped_column(String(100))
    exp_ids: Mapped[list | None] = mapped_column(JSON)
