# scitech-kb

> 学术科创部（学创部）知识库
> MySQL 结构化库 + 经验库（向量检索）双库并存，id 关联

## 是什么

沉淀部门资产的知识库系统，第一期聚焦两块：

1. **成员信息** —— 成员档案内外分层，支撑通讯、换届、评优、宣传
2. **活动参与记录** —— 谁在哪个活动里做了什么分工、关联了哪些经验

长期把成员、活动、参与、经验、推文统一成可查询、可交接的知识库。

## 架构

| 子系统 | 存什么 | 技术 |
|--------|--------|------|
| 结构化库 | 成员、活动、参与记录、职务 | MySQL 8.0+ |
| 经验库 | 非结构化经验文本 | 向量库（Chroma） |

两库通过 id 关联。部署在内网服务器。

> **账号**：一期**不接**学校统一身份认证（CAS）。写接口靠**一道部门共享口令**挡着——
> 它挡得住校园网里乱逛进来的人，但**不区分是谁**，所以替代不了 CAS。
> 两者的区别是「能不能写」和「是谁写的」，详见 `docs/开发说明.md` 的接口一节。

## 目录

```
scitech-kb/
├── README.md               # 本文件
├── CONTRIBUTING.md         # 协作规范：提交红线 + PR 流程
├── docs/
│   ├── 需求文档.md           # 需求（what/why）：目标、角色权限、数据模型、功能
│   ├── 交接文档.md           # 交接（how）：数据源、初始化、部署、待办
│   ├── 开发说明.md           # 开发（how to code）：怎么跑起来、代码在哪
│   └── CI-CD说明.md          # 持续集成/部署：Actions 测试 + 服务器自动部署
├── schema/
│   └── schema.sql          # MySQL 建表 DDL + 视图（关系模式）
├── .github/workflows/ci.yml # CI：push/PR 自动跑 pytest
├── api/                    # FastAPI 服务（结构化库 + 向量检索）
├── cli/                    # 命令行工具（QQ 机器人从这里读写）
├── tagging/                # 打标：关键词表 + 规则版打标器（K8）
├── pipeline/               # 数据管道：从在线表格拉到 MySQL
├── scripts/                # 运维脚本：建库 / 重置 / 造演示数据
├── tests/                  # 验收测试（pytest）
├── web/                    # 网页界面（单文件 HTML，不用构建，打开就能用）
├── deploy/                 # 部署：systemd 单元 + 部署说明 + 自动部署（bootstrap / auto-deploy）
└── 技术栈可行性/             # 动手写代码之前的可行性验证
```

## 快速开始

```bash
python -m venv .venv && .venv/Scripts/activate    # Windows；Linux/macOS 用 .venv/bin/activate
pip install -i https://pypi.tuna.tsinghua.edu.cn/simple -r requirements.txt
cp .env.example .env            # 填上自己电脑的数据库密码（.env 不进仓库）
python -m scripts.init_db       # 建库建表
python -m scripts.seed          # 造一份编造的演示数据（可以反复跑）
python -m uvicorn api.main:app --port 8000
```

然后浏览器打开 http://localhost:8000 。详细步骤（装 MySQL、模型下载、常见报错）
见 `docs/开发说明.md`。

## 文档导航

| 想了解 | 看 |
|--------|-----|
| 需求、数据模型、字段定义、验收标准 | `docs/需求文档.md` |
| 数据源现状、初始化步骤、部署、待办 | `docs/交接文档.md` |
| **怎么把项目跑起来、代码放在哪、CLI 怎么用** | `docs/开发说明.md` |
| **CI/CD：自动测试与自动部署** | `docs/CI-CD说明.md` |
| 建表语句 | `schema/schema.sql` |

## 数据源

- 团队表格（SeaTable）：成员表、朋辈导师表、项目台账
- 团队云盘（Seafile）：交接资料（策划案/主持词/名单/分工表）

同步以单向导入为主（在线平台 → MySQL）。

## 协作与安全

- 协作流程、提交红线：见 [`CONTRIBUTING.md`](CONTRIBUTING.md)
- **敏感信息（成员、联系方式、参与记录）只能放内网 MySQL 与内部在线表格，禁止提交本仓库**
- `main` 分支受保护，改动需 PR + owner 审核后 merge
- **读接口是公开的，写接口要口令**：服务绑到校园网之后，`POST/PUT/PATCH/DELETE`
  都要部门口令（`KB_SHARED_PASSWORD`），`GET` 一律放行。本机 `127.0.0.1` 免口令
  （QQ 机器人走这条）。还没上 HTTPS，口令在校内网里是明文——这件事得让用的人知道

## 状态

- 需求、schema：已完成
- 服务端（`api/`）、经验库语义检索、报表统计、CLI：已完成，见 `docs/开发说明.md`
- 网页界面（`web/index.html`）：已完成——检索、录入、筛选分页、活动分工、介绍卡、报表，全部不用敲命令
- 成员/活动介绍卡（`/api/cards/*`）：已完成
- 打标（`tagging/` + `cli exp tag`）：规则版已完成（只补空、幂等）；大模型版未做，见 `docs/开发说明.md`
- 数据管道（`pipeline/`，从在线表格导入）：**代码已完成，暂不启用**——缺一份在线表格的真实表头，拿到就能跑
- **写接口口令（`api/auth.py`）**：已完成——绑到校园网之后，`POST/PUT/PATCH/DELETE`
  要部门口令，`GET` 一律放行。服务器 `.env` **必须**配 `KB_SHARED_PASSWORD`，
  否则启动日志第一屏会大声警告"未启用"
- 部署到内网服务器：见 `deploy/部署说明.md`。服务**跑在一个专职服务账号下**
  （shell 是 `nologin`），我们的账号在同一个组里，所以更新代码**不需要 sudo**；
  还差「把监听从 `127.0.0.1` 改成 `0.0.0.0`」这一步，要服务器管理员做。
  现状和踩过的坑见该文件第九节
- **CI**：workflow 文件刚补进仓库（`.github/workflows/ci.yml`），
  **还没真跑过一次**——第一次 push 上去时要盯一眼。
  **CD**（服务器每 5 分钟轮询自动部署）已经在服务器上跑着了。
  两者都见 `docs/CI-CD说明.md`
- **CAS 统一身份认证：一期不做，列为后续计划**，见 `docs/开发说明.md` 待定事项
- 待确认项见 `docs/需求文档.md` 附录
