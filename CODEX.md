# HTMLDICE — Codex 开发指南

CoC7 线上跑团 Web 应用：无密码用户名登录、房间、投骰、人物卡、战斗、场景地图、线索手札。

## 快速启动（本地）

```bash
cd HTMLDICE
pip install -r requirements.txt   # openpyxl，人物卡 xlsx 导入
python server.py                  # http://127.0.0.1:8080
```

首次运行会自动创建空的 `users.json` / `characters.json`；`rooms.json` 在首次建房后写入。

## 生产环境

| 项 | 值 |
|----|-----|
| 公网 | `http://47.81.210.196:8080` |
| 路径 | `/opt/htmldice/` |
| 进程 | `python3 server.py`（无 systemd，nohup 后台） |
| SSH 密钥 | 本地 `d:\codex\irene.pem`，用户 `root` |

### 部署（只同步代码，不覆盖线上数据）

```powershell
scp -i "d:\codex\irene.pem" server.py index.html cthulhu.css monsters.json character_import.py requirements.txt CODEX.md root@47.81.210.196:/opt/htmldice/
```

**不要覆盖**：`users.json`、`characters.json`、`rooms.json`、`assets/scenes/*`、`assets/handouts/*`

重启服务（PowerShell 用 `;` 不要用 `&&`）：

```powershell
ssh -i "d:\codex\irene.pem" root@47.81.210.196 "pkill -f 'python3 server.py' || true; sleep 1; cd /opt/htmldice; mkdir -p assets/scenes assets/handouts; nohup python3 server.py >> server.log 2>&1 </dev/null & disown; sleep 2; curl -s http://127.0.0.1:8080/api/me"
```

## 架构（极简）

```
index.html + cthulhu.css     单页前端，面板切换 + 2s 轮询
server.py                    stdlib HTTP，ThreadingHTTPServer，rooms 内存 + JSON 持久化
character_import.py          xlsx 人物卡解析
monsters.json                静态怪物图鉴
```

无 WebSocket、无数据库、无框架。会话 Cookie `session`，房间状态在 `rooms.json`。

## 核心文件

| 文件 | 作用 |
|------|------|
| `server.py` | 全部 API、权限、战斗/场景/手札逻辑 |
| `index.html` | UI + 客户端 JS（投骰、房间、战斗、场景、线索） |
| `cthulhu.css` | 样式 |
| `users.json` | 用户名 → `{ role: player\|kp\|admin }`（**运行时，不入库**） |
| `characters.json` | v2 结构：`accounts[pl].pcs[pc_id]`（**运行时，不入库**） |
| `rooms.json` | 房间：骰点、战斗、场景、手札（**运行时，不入库**） |
| `monsters.json` | 图鉴模板，可入库 |
| `assets/scenes/<room_id>/` | KP 场景图（gitignore） |
| `assets/handouts/<room_id>/` | 线索配图（gitignore） |

## 角色与权限

- **player**：注册默认角色；加入房间、公开骰、看己方人物卡、只读场景/战斗/线索。
- **kp / admin**：可创建房间；房间内 KP 可暗骰、战斗、场景编辑、发放线索、暂离/解散房间。
- KP 与 PL **不能同时占一个「当前房间槽」**（`user_rooms` 一人一间）。

## 主要 API

### 认证
- `POST /api/register` `POST /api/login` `POST /api/logout` `GET /api/me`

### 房间
- `GET/POST /api/rooms` — 列表 / KP 创建
- `POST /api/room/join|leave|rejoin|delete`
- `GET /api/room` — 当前房间、`last_roll_id`、`last_handout_id`
- `GET /api/room/rolls?since=` — 增量骰点
- `POST /api/room/roll` — 投骰（KP 可 `hidden` 暗骰）

### 人物卡
- `GET /api/character` `GET /api/players` `POST /api/character/status`（KP）
- `GET /api/pcs` `POST /api/pcs/select` `POST /api/pcs/upload`（xlsx）

### 战斗
- `GET /api/combat` — 状态（KP 或参战 PL）
- `POST /api/combat/start|end` — 结束可 `writeback_hp` 回写人物卡
- `POST /api/combat/monster|hp|kill|next-turn|skill-roll|random-target`
- `GET /api/monsters` — 图鉴

### 场景地图
- `GET /api/scene`
- `POST /api/scene/image|token|sync-pcs|clear`（KP）

### 线索手札
- `GET /api/handouts?since=` — PL 看自己的；KP 看全部含已撤回
- `POST /api/handouts` — KP 发放 `{ title, body, targets, image_b64? }`
- `POST /api/handouts/revoke|read`

## 房间数据结构（`rooms.json` 单房间）

```json
{
  "name": "房间名",
  "kp": "kp用户名",
  "players": ["pl1"],
  "rolls": [],
  "last_roll_id": 0,
  "next_roll_id": 1,
  "combat": { "active": true, "players": [], "monsters": [], "log": [] },
  "scene": { "image": "scenes/<id>/scene.jpg", "tokens": [] },
  "handouts": [],
  "next_handout_id": 1,
  "last_handout_id": 0,
  "handout_reads": { "pl1": [1, 2] }
}
```

## 前端约定

- 轮询间隔：`ROLL_POLL_MS` / `COMBAT_POLL_MS` / `SCENE_POLL_MS` / `HANDOUT_POLL_MS` = 2000ms
- 面板：`appPanel`（投骰主页）、`combatPanel`、`scenePanel`、`handoutPanel`、`charPanel`、`playersPanel`
- `canSecretRoll` = 房间内 KP；驱动暗骰、战斗/场景管理、手札发放
- 战斗面板：仅 `updated_at` 变化时全量重绘，避免选中怪物被轮询冲掉
- CoC7 大失败：**96–100**（`evaluate_skill_check` / 前端 `DICE.d100.critFail`）

## 已实现功能清单

1. 用户登录注册、角色（player/kp/admin）
2. 房间：KP 创建、PL 加入、KP 暂离（不删房间）、回到房间、解散房间
3. 公开/暗骰、房间骰点记录
4. 人物卡 xlsx 导入、KP 改 HP/SAN
5. 战斗：地图、玩家、怪物图鉴、回合、HP、技能检定、结束 HP 回写
6. 场景地图：KP 上传背景、拖拽 PC/NPC 棋子，PL 只读
7. 线索手札：KP 向指定/全体 PL 发文字+图，PL 未读角标

## 常见坑

- **PowerShell**：链式命令用 `;`，不要用 `&&`
- **SSH nohup**：用 `</dev/null & disown`，避免挂起
- **部署**：切勿用本地 `users.json` / `characters.json` / `rooms.json` 覆盖云端
- **手机访问慢**：检查 VPN；8080 非标准端口，流量网络可能更慢
- **测试 KP**：需在 `users.json` 里把对应用户的 `role` 设为 `kp` 或 `admin`

## 后续可做（未实现）

- 统一房间时间线（骰点 + 战斗 log + 线索）
- Nginx 80/443 反代（改善手机访问）
- WebSocket 替代轮询
- KP 暂离横幅、PL 体验打磨

## Git

- 远程：`https://github.com/Seeunever/HTMLDICE.git`，分支 `main`
- 入库：代码、`monsters.json`、`requirements.txt`、本文件
- 不入库：`.gitignore` 中的运行时 JSON 与 `assets/scenes|handouts` 用户上传内容
