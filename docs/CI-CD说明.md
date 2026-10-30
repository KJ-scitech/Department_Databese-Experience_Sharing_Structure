# CI/CD 说明

> 目标：`main` 有新提交后，代码自动部署到部门服务器（M710q），不需要人工 SSH 上去操作。

## 一、为什么拆成两半

服务器（M710q）在网络上有两个硬约束：

| 约束 | 影响 |
|------|------|
| GitHub **直连不通**（443 被墙），只有 `gh-proxy.com` 镜像可达 | 服务器上任何 GitHub 操作都要走镜像 |
| 服务器在内网，**公网够不到** | GitHub 云端 Actions 无法直接 SSH 进来部署 |

所以标准的「Actions 一条龙」在这里走不通，拆成两半：

```
      push / PR
         │
         ▼
  ┌──────────────┐   CI：跑测试，红了不许合
  │ GitHub Actions│ ──────────────────────────┐
  └──────────────┘                             │
                                               ▼
                                        ┌─────────────┐
   merge 到 main                         │  GitHub     │
                                               └─────────────┘
                                                     ▲
                          每 5 分钟用 gh-proxy 拉一次 │
                                                     │
   ┌─────────────────────────────────────────────────┴──┐
   │ M710q：systemd timer → auto-deploy.sh               │
   │   发现新 commit → git pull → 重启 scitech-kb 服务     │
   └─────────────────────────────────────────────────────┘
```

- **CI（GitHub Actions）**：在云端 runner 跑 `pytest`，作为合并门禁。
- **CD（M710q 定时轮询）**：服务器主动来拉，绕开「云端够不到内网」的问题。

代价：部署有最长 5 分钟延迟（轮询周期）。换来的是**不依赖服务器被公网访问、不需要开端口、不需要 Tailscale**，最稳。

---

## 二、CI：GitHub Actions

文件：`.github/workflows/ci.yml`

- 触发：向 `main` 的 push、所有 PR，以及手动 `workflow_dispatch`。
- 步骤：起 MySQL → 装依赖 → 建库建表 → 造演示数据 → `pytest`。
- 云端 runner 在墙外，所以 CI 里 `HF_ENDPOINT` 直接指向 `huggingface.co`（比国内镜像快）。
- 缓存：`~/.cache/huggingface`（bge-small-zh 约 93MB）+ pip 的 wheel 缓存。

> **这份文件是补上的。** 从 PR #2 起本文和 README 就写着「CI 跑 pytest、红了不许合」，
> 但仓库里一直没有 `.github/workflows/` 这个目录——也就是说着文档里的门禁从来没生效过，
> PR 一次都没跑过测试。2026-09-21 才把它补上（顺便修掉下面这条库不一致）。
>
> 补的时候特意**不跳过**模型相关的用例：`tests/conftest.py` 的 `client` 夹具一进 lifespan
> 就要加载 embedding 模型，而 K1/K2/K5 的核心验收全用 `seeded`——跳掉等于把核心验收
> 全跳掉，门禁就白设了。贵的一次性成本用缓存摊掉。

> 建议把它设成 `main` 的必需检查（Settings → Branches → 要求状态检查通过），这样测试不过的 PR 合不进去。

### 2.1 一处刻意的版本不一致（待办）

| 环境 | 数据库 | 为什么 |
|------|--------|--------|
| 开发机 / CI | **MySQL 8.0** | CI 要挡的是**开发机上的**回归，所以跟开发机对齐 |
| 部门服务器（M710q） | **MariaDB** | `deploy/bootstrap.sh` 装的是 `mariadb-server` |

两者在字段类型、`ONLY_FULL_GROUP_BY`、JSON 函数上都有差异，也就是说
**CI 绿了不等于服务器上就没问题**。

**待办**：等哪次在服务器上真的踩到差异，再把 CI 也换成 MariaDB（或者把服务器换成 MySQL）。
现在不动的原因是「别一次换两个变量」——同时换数据库和数据库版本，
真红了分不清是代码的锅还是环境的锅。

### 2.2 CI 没跑到的地方（首次 push 时留意）

这份 YAML **在本地无法真正验证**（要 push 才会执行）。本地做过的验证是：
建库建表/造数据/pytest 三步都在开发机手跑通过、YAML 过了解析器、环境变量名逐个对过 `api/config.py`。

本地验证不到、真红了先怀疑的几处，都写在 `ci.yml` 文件头的注释里。

---

## 三、CD：M710q 定时轮询部署

文件：

| 文件 | 作用 |
|------|------|
| `deploy/auto-deploy.sh` | 拉取 → 比对 → fast-forward → 装依赖（如需要）→ 重启服务 |
| `deploy/auto-deploy.service` | 跑一次脚本的 oneshot 单元 |
| `deploy/auto-deploy.timer` | 每 5 分钟触发一次 |

脚本要点：
- remote 默认走 `https://gh-proxy.com/https://github.com/...`（服务器上 git 没配代理，走镜像更省事）；已配代理的机器可用 `KB_REMOTE` 直连。
- 只有 commit 变化时才动作；无变化静默退出。
- 工作区有未提交改动时**跳过**部署，避免覆盖。
- 只有 `requirements.txt` 变了才重装依赖。
- 单元以**服务账号**（不是 root）运行，脚本最后要 `systemctl restart` 系统级服务，
  所以还得给这个账号一条免密规则；脚本内用
  `git config --global --add safe.directory` 处理仓库属主不符时的 dubious ownership 报错。

> ★ **脚本最后一步是重启服务**，而重启会重新加载向量模型（20–30 秒）。
> 所以「HEAD 和 origin/main 不一致」+「工作区干净」同时成立时，
> 服务会被**每 5 分钟重启一次**，页面隔几分钟卡一次。
> 在服务器上放还没 push 的代码时要避开这个状态，详见 `deploy/部署说明.md` 第九节。

### 3.1 安装（服务器上，管理员或对应用户）

```bash
# 1. 首次把项目部署好（见 deploy/部署说明.md，或跑 bootstrap.sh）
cd <部署目录>

# 2. 装 systemd 单元
sudo cp deploy/auto-deploy.service /etc/systemd/system/
sudo cp deploy/auto-deploy.timer   /etc/systemd/system/
#   ↑ 打开 service 文件，确认 <服务账号> / <部署目录> 这些占位符都换成真实值了
sudo systemctl daemon-reload
sudo systemctl enable --now auto-deploy.timer

# 3. 验证
systemctl list-timers auto-deploy.timer       # 看下次触发时间
sudo systemctl start auto-deploy.service       # 手动跑一次
journalctl -u auto-deploy -n 30                # 看日志
```

> ⚠️ `journalctl -u` 看别人的服务日志，要 `systemd-journal`（或 `adm`）组权限。
> 不在组里时它**不报错**，只回一句 `-- No entries --`，
> 看着像"这单元从没跑过"，其实只是没权限看。别被这句话带偏。

> ⚠️ **开自动部署之前，服务器 `.env` 里必须已经填好 `KB_SHARED_PASSWORD`。**
> 自动部署的最后一步是**重启服务**。如果那个值是空的，重起来的服务
> 写接口就是**不设防**的——而这件事只有启动日志第一屏会说，不注意就漏了。
> 服务绑了校园网的话，漏这一步等于把库敞开。

> ⚠️ **别图省事写 `User=root`。** root 拉下来的新文件属主是 root，而服务是以
> 服务账号跑的，服务随后可能写不动 `data/`，表现是「自动部署成功，但服务起不来」。
> 单元里该写服务账号；重启权限靠一条只放开 `systemctl restart scitech-kb`
> 的免密规则补（见 `deploy/部署说明.md` 第五节）。

> 没有 sudo 时，可用用户级单元（`~/.config/systemd/user/`）+ `loginctl enable-linger`。
> 但 `enable-linger` 一般也要权限，过渡方案，不如找管理员配系统级。

### 3.2 手动触发一次

```bash
sudo systemctl start auto-deploy.service
```

---

## 四、首次部署（从零到能自动部署）

用 `deploy/bootstrap.sh`：

```bash
bash <(curl -sL https://gh-proxy.com/https://raw.githubusercontent.com/KJ-scitech/Department_Databese-Experience_Sharing_Structure/main/deploy/bootstrap.sh)
```

或本地有仓库时直接 `bash deploy/bootstrap.sh`。它会：配 gh-proxy 镜像 → clone → 建 venv 装依赖 → 生成 `.env` → 建库造数据。

随后按 `deploy/部署说明.md` 配 systemd 服务，再按本文 §3 开自动部署。

**前提**：服务器要有 MySQL/MariaDB 和 Python 3.10+。MySQL 首次需要装（`sudo apt install mariadb-server`），这步要管理员。

---

## 五、常见问题

| 现象 | 原因 | 处理 |
|------|------|------|
| `auto-deploy` 一直没动作 | GitHub 拉取失败 | 手动 `systemctl start auto-deploy.service` 看日志；确认 gh-proxy 可达 |
| 部署了但服务没换代码 | 服务名/路径不对 | 核对 `auto-deploy.service` 的 `User=`、`WorkingDirectory=`、`KB_SERVICE` |
| 部署后报权限错 | 拉下来的文件属主 ≠ 跑服务的用户 | 部署目录属主设服务账号、组设共享组并开 setgid（`2770`），服务账号靠组权限就够用 |
| timer 装了不跑 | 没 `daemon-reload` / 没 enable | `sudo systemctl daemon-reload && sudo systemctl enable --now auto-deploy.timer` |
| 想立即部署不用等 | 轮询周期 5 分钟 | `sudo systemctl start auto-deploy.service` |
| git 报 `dubious ownership` | 仓库属主 ≠ 跑 git 的用户 | 脚本已加 `safe.directory`；**但它是写进 `$HOME/.gitconfig` 的，home 不可写就静默失效**——单元里把 `HOME` / `GIT_CONFIG_GLOBAL` 指到可写目录 |
| `journalctl -u auto-deploy` 回 `-- No entries --`，像从没跑过 | 不在 `systemd-journal`/`adm` 组里，看不到别人的服务日志 | 把自己加进组，或换 `sudo journalctl -u ...` |
| 日志说「工作区有未提交改动，跳过本次部署」，但**根本没人改过代码** | 从 Windows 拷过去的文件带 **CRLF**，仓库里存的是 LF，于是 `git status` 报一堆"改动"，其实一个字都没变 | 在服务器上 `git checkout -- .` 把行尾归一。这是「Windows 上手动传代码到 Linux」的典型症状，用 `git clone` 不会有 |
| 服务重起来之后写接口不设防了 | 服务器 `.env` 里 `KB_SHARED_PASSWORD` 是空的 | 填上再重启。启动日志第一屏会明说当前是"已启用"还是"未启用"——但它只在那时候说 |
| `After=mysql.service` 没起作用 | 这台机器上的单元叫 `mariadb.service` | `After=` 写一个**不存在的**单元名不会报错，只是静默失效。先查：`systemctl list-units --type=service --all \| grep -iE "maria\|mysql"` |

---

## 六、可选：换成 Actions + Tailscale 直推

如果以后 Tailscale 稳定、且希望「合并即部署」（无轮询延迟），可改成：

1. 在 GitHub 仓库 Secrets 存一个 Tailscale auth key。
2. workflow 里用 `tailscale/github-action` 加入 tailnet。
3. 再 SSH（Tailscale IP）到 M710q 执行 `<部署目录>/deploy/auto-deploy.sh`。

**暂不采用的原因**：M710q 的 Tailscale 走香港 relay，SSH 握手经常超时，作为部署链路不可靠。轮询方案不依赖它。
