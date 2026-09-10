-- ============================================================
-- 学创部知识库 · MySQL Schema v1.0
-- 字符集：utf8mb4 / utf8mb4_unicode_ci
-- 版本要求：MySQL 8.0+
-- 部署：M710q（Tailscale 内网）
-- ============================================================

CREATE DATABASE IF NOT EXISTS scitech_kb
    DEFAULT CHARACTER SET utf8mb4
    DEFAULT COLLATE utf8mb4_unicode_ci;

USE scitech_kb;

-- ------------------------------------------------------------
-- 1. 成员表
-- 内外分层：对外简版 = name + org_unit + title + bio_short + avatar + skills
-- 敏感字段：student_no / class_no / birthday / phone / qq / wechat / email
-- ------------------------------------------------------------
CREATE TABLE member (
    id            BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    name          VARCHAR(50)     NOT NULL COMMENT '姓名',
    student_no    VARCHAR(30)     DEFAULT NULL COMMENT '学号/工号（唯一，敏感）',
    gender        TINYINT         DEFAULT NULL COMMENT '0未知 1男 2女',
    grade         VARCHAR(10)     DEFAULT NULL COMMENT '年级，如2024级',
    major         VARCHAR(100)    DEFAULT NULL COMMENT '专业',
    class_no      VARCHAR(50)     DEFAULT NULL COMMENT '班级（敏感）',
    birthday      DATE            DEFAULT NULL COMMENT '生日（敏感）',
    join_term     VARCHAR(20)     DEFAULT NULL COMMENT '入部学年，如2025-2026',
    status        VARCHAR(20)     NOT NULL DEFAULT '在部' COMMENT '在部/退部/毕业/请假',
    org_unit      VARCHAR(50)     DEFAULT NULL COMMENT '所属组（枚举待定）',
    title         VARCHAR(50)     DEFAULT NULL COMMENT '职务：部长/副部长/组长/部员',
    member_type   VARCHAR(20)     NOT NULL DEFAULT '本部门' COMMENT '本部门/老师/嘉宾/外部/朋辈导师',
    phone         VARCHAR(20)     DEFAULT NULL COMMENT '手机（敏感）',
    qq            VARCHAR(20)     DEFAULT NULL COMMENT 'QQ（敏感）',
    wechat        VARCHAR(50)     DEFAULT NULL COMMENT '微信（敏感）',
    email         VARCHAR(100)    DEFAULT NULL COMMENT '邮箱（敏感）',
    skills        JSON            DEFAULT NULL COMMENT '技能标签数组',
    bio_short     VARCHAR(200)    DEFAULT NULL COMMENT '一句话简介（对外）',
    bio_long      TEXT            DEFAULT NULL COMMENT '详细介绍（内部）',
    avatar        VARCHAR(255)    DEFAULT NULL COMMENT '头像URL',
    honors        JSON            DEFAULT NULL COMMENT '获奖荣誉数组',
    contribution  TEXT            DEFAULT NULL COMMENT '对部门贡献（评优用，内部）',
    remark        TEXT            DEFAULT NULL COMMENT '备注（内部）',
    created_at    DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at    DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    deleted_at    DATETIME        DEFAULT NULL COMMENT '软删除',
    PRIMARY KEY (id),
    UNIQUE KEY uk_student_no (student_no),
    KEY idx_term (join_term),
    KEY idx_status (status),
    KEY idx_org (org_unit)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='成员';

-- ------------------------------------------------------------
-- 2. 活动系列表
-- ------------------------------------------------------------
CREATE TABLE activity_series (
    id          BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    name        VARCHAR(100)    NOT NULL COMMENT '系列名，如惟学沙龙',
    category    VARCHAR(50)     DEFAULT NULL COMMENT '类别：学业支持/科研启蒙/讲座课堂/师生交流/导师制度/竞赛赛事/组织建设',
    description TEXT            DEFAULT NULL COMMENT '系列定位',
    is_official TINYINT         NOT NULL DEFAULT 0 COMMENT '是否18项官方活动',
    created_at  DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at  DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    UNIQUE KEY uk_series_name (name)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='活动系列';

-- ------------------------------------------------------------
-- 3. 活动场次表
-- ------------------------------------------------------------
CREATE TABLE activity (
    id          BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    series_id   BIGINT UNSIGNED DEFAULT NULL COMMENT '所属系列，可空（一次性活动）',
    name        VARCHAR(200)    NOT NULL COMMENT '场次名，如惟学沙龙第3期',
    term        VARCHAR(20)     DEFAULT NULL COMMENT '学年/学期',
    event_date  DATE            DEFAULT NULL COMMENT '活动日期',
    location    VARCHAR(200)    DEFAULT NULL COMMENT '地点',
    status      VARCHAR(20)     NOT NULL DEFAULT '计划' COMMENT '计划/进行中/已完成/取消',
    article_url VARCHAR(500)    DEFAULT NULL COMMENT '关联推文链接',
    description TEXT            DEFAULT NULL COMMENT '活动说明',
    created_at  DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at  DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    KEY idx_series (series_id),
    KEY idx_term (term),
    KEY idx_date (event_date),
    CONSTRAINT fk_activity_series FOREIGN KEY (series_id)
        REFERENCES activity_series (id) ON DELETE SET NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='活动场次';

-- ------------------------------------------------------------
-- 4. 参与记录表（核心）
-- 以活动为外键，记录成员在该活动中的分工与相关经验
-- ------------------------------------------------------------
CREATE TABLE participation (
    id           BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    activity_id  BIGINT UNSIGNED NOT NULL COMMENT '→ activity.id',
    member_id    BIGINT UNSIGNED NOT NULL COMMENT '→ member.id',
    role         VARCHAR(50)     NOT NULL DEFAULT '参与' COMMENT '分工/角色：负责人/主讲/执行/志愿者/观众',
    contribution TEXT            DEFAULT NULL COMMENT '贡献说明（做了什么）',
    hours        DECIMAL(5,1)    DEFAULT NULL COMMENT '投入工时（小时），可选',
    honor        VARCHAR(100)    DEFAULT NULL COMMENT '评优/获奖',
    exp_ids      JSON            DEFAULT NULL COMMENT '关联经验id数组（Chroma的exp-xxx）',
    created_at   DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at   DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    UNIQUE KEY uk_act_member_role (activity_id, member_id, role),
    KEY idx_member (member_id),
    CONSTRAINT fk_part_activity FOREIGN KEY (activity_id)
        REFERENCES activity (id) ON DELETE CASCADE,
    CONSTRAINT fk_part_member FOREIGN KEY (member_id)
        REFERENCES member (id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='活动参与记录';

-- ------------------------------------------------------------
-- 5. 职务表（多职务 / 换届）
-- ------------------------------------------------------------
CREATE TABLE position (
    id         BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    member_id  BIGINT UNSIGNED NOT NULL COMMENT '→ member.id',
    title      VARCHAR(50)     NOT NULL COMMENT '部长/副部长/组长/组员',
    org_unit   VARCHAR(50)     DEFAULT NULL COMMENT '所属组',
    term       VARCHAR(20)     NOT NULL COMMENT '任期学年',
    is_current TINYINT         NOT NULL DEFAULT 1 COMMENT '是否现任',
    created_at DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    KEY idx_member (member_id),
    KEY idx_term (term),
    CONSTRAINT fk_pos_member FOREIGN KEY (member_id)
        REFERENCES member (id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='成员职务任期';

-- ------------------------------------------------------------
-- 6. 隐私授权表
-- ------------------------------------------------------------
CREATE TABLE privacy_consent (
    id             BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    member_id      BIGINT UNSIGNED NOT NULL COMMENT '→ member.id',
    consented      TINYINT         NOT NULL DEFAULT 0 COMMENT '是否同意入库/注册',
    scope          VARCHAR(50)     DEFAULT NULL COMMENT '授权范围：入库/公开卡片',
    policy_version VARCHAR(20)     DEFAULT NULL COMMENT '声明版本',
    consented_at   DATETIME        DEFAULT NULL COMMENT '同意时间',
    revoked_at     DATETIME        DEFAULT NULL COMMENT '撤回时间',
    PRIMARY KEY (id),
    KEY idx_member (member_id),
    CONSTRAINT fk_consent_member FOREIGN KEY (member_id)
        REFERENCES member (id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='隐私授权记录';

-- ------------------------------------------------------------
-- 7. 账号表（南大统一身份 CAS 绑定）
-- ------------------------------------------------------------
CREATE TABLE account (
    id            BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    member_id     BIGINT UNSIGNED NOT NULL COMMENT '→ member.id',
    cas_uid       VARCHAR(50)     NOT NULL COMMENT '南大统一身份uid（学号/工号）',
    role          VARCHAR(20)     NOT NULL DEFAULT '部员' COMMENT '管理员/管理层/部员/外部',
    last_login_at DATETIME        DEFAULT NULL,
    created_at    DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    UNIQUE KEY uk_cas_uid (cas_uid),
    KEY idx_member (member_id),
    CONSTRAINT fk_acct_member FOREIGN KEY (member_id)
        REFERENCES member (id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='账号（CAS绑定）';

-- ============================================================
-- 视图：对外简版成员卡（自动脱敏）
-- ============================================================
CREATE OR REPLACE VIEW v_member_public AS
SELECT
    id, name, gender, grade, major,
    join_term, status, org_unit, title,
    skills, bio_short, avatar, honors
FROM member
WHERE deleted_at IS NULL;

-- ============================================================
-- 视图：成员活动履历（自动聚合，不用手填）
-- ============================================================
CREATE OR REPLACE VIEW v_member_history AS
SELECT
    p.member_id,
    m.name        AS member_name,
    a.id          AS activity_id,
    a.name        AS activity_name,
    s.name        AS series_name,
    a.term,
    a.event_date,
    p.role,
    p.contribution,
    p.honor,
    p.exp_ids
FROM participation p
JOIN member m   ON m.id = p.member_id
JOIN activity a ON a.id = p.activity_id
LEFT JOIN activity_series s ON s.id = a.series_id;
