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

- 触发：向 `main` 的 push 和所有 PR。
- 步骤：起 MariaDB → 装依赖 → 建库建表 → 造演示数据 → `pytest`。
- 云端 runner 在墙外，所以 CI 里 `HF_ENDPOINT` 直接指向 `huggingface.co`（比国内镜像快）。

> 建议把它设成 `main` 的必需检查（Settings → Branches → 要求状态检查通过），这样测试不过的 PR 合不进去。

---

## 三、CD：M710q 定时轮询部署

文件：

| 文件 | 作用 |
|------|------|
| `deploy/auto-deploy.sh` | 拉取 → 比对 → fast-forward → 装依赖（如需要）→ 重启服务 |
| `deploy/auto-deploy.service` | 跑一次脚本的 oneshot 单元 |
| `deploy/auto-deploy.timer` | 每 5 分钟触发一次 |

脚本要点：
- remote 默认走 `https://gh-proxy.com/https://github.com/...`，因为服务器直连 GitHub 不通。
- 只有 commit 变化时才动作；无变化静默退出。
- 工作区有未提交改动时**跳过**部署，避免覆盖。
- 只有 `requirements.txt` 变了才重装依赖。

### 3.1 安装（服务器上，管理员或对应用户）

```bash
# 1. 首次把项目部署好（见 deploy/部署说明.md，或跑 bootstrap.sh）
cd ~/knowledge-base

# 2. 装 systemd 单元
sudo cp deploy/auto-deploy.service /etc/systemd/system/
sudo cp deploy/auto-deploy.timer   /etc/systemd/system/
#   ↑ 打开 service 文件确认 User= 和 WorkingDirectory= 与实际一致
sudo systemctl daemon-reload
sudo systemctl enable --now auto-deploy.timer

# 3. 验证
systemctl list-timers auto-deploy.timer      # 看下次触发时间
sudo systemctl start auto-deploy.service      # 手动跑一次
journalctl -u auto-deploy -n 30               # 看日志
```

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
| 部署后报权限错 | 脚本改动工作区文件权限 | 确保 `knowledge-base` 目录归运行用户所有 |
| timer 装了不跑 | 没 `daemon-reload` / 没 enable | `sudo systemctl daemon-reload && sudo systemctl enable --now auto-deploy.timer` |
| 想立即部署不用等 | 轮询周期 5 分钟 | `sudo systemctl start auto-deploy.service` |

---

## 六、可选：换成 Actions + Tailscale 直推

如果以后 Tailscale 稳定、且希望「合并即部署」（无轮询延迟），可改成：

1. 在 GitHub 仓库 Secrets 存一个 Tailscale auth key。
2. workflow 里用 `tailscale/github-action` 加入 tailnet。
3. 再 SSH（Tailscale IP）到 M710q 执行 `~/knowledge-base/deploy/auto-deploy.sh`。

**暂不采用的原因**：M710q 的 Tailscale 走香港 relay，SSH 握手经常超时，作为部署链路不可靠。轮询方案不依赖它。
