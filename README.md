# 束线联锁控制器规程升级 · 静止输出复核（ioco）

工程师在网页分别录入**旧规程**与**候选规程**（有限位置、初始位置、输入命令、
带输入或输出标签的迁移，输出可明确标记为“静止”），提交后由后端接口按
**ioco（输入输出一致性）**语义复核：纳入内部静默迁移（`tau`）与可观察静止（`δ`），
候选在旧规程可执行历史后可能给出的每项输出，都必须为旧规程在同一历史允许。

## 判定方法

- 对“候选位置闭包 × 旧规程位置闭包”做广度优先共归纳探索：两侧先求 `tau` 闭包，
  再沿旧规程的悬挂迹（输入、输出、`δ`）同步推进。
- 每到达一对闭包，核对 `out(候选) ⊆ out(旧规程)`；越界即不一致。
- 反例展示：按 ASCII 顺序确定的**最短历史**、两侧**逐步闭包状态集**、
  **候选输出**与**旧规程允许输出**。
- 不使用状态名对应、不做有限长度枚举、不做随机回放；
  仅位置改名而行为相同的两份规程判为一致。

## 录入语法

- 位置 / 输入命令：逗号或空白分隔的名称列表。
- 迁移：每行一条 `源 -> 目标 : 标签`；`#` 开头为注释。
- 标签：`?命令` 输入（须先在输入命令中声明）；`!名称` 输出；
  `tau` 内部静默迁移；`delta` 将输出明确标记为“静止”（δ）。
  没有任何输出 / 静默 / 静止迁移的位置被视为可观察静止。
- 非法标签、悬空迁移、缺少初始位置、同一标签下重复状态声明等，
  接口将按侧别、字段与行号定位反馈，页面同时清除先前结论。

## 接口

- `GET /`：复核页；`GET /healthz`：健康检查。
- `POST /api/check`：请求体 `{"old": {...}, "candidate": {...}}`，
  每份规程含 `locations / initial / inputs / transitions` 四个文本字段。
  - 复核通过受理：`{"ok": true, "result": {"verdict": "consistent" | "inconsistent", ...}}`
  - 录入非法（HTTP 422）：`{"ok": false, "errors": [{side, field, line, message}, ...]}`

## 运行（Docker / Compose）

```bash
docker compose up --build web          # 打开 http://localhost:8080
PORT=9000 docker compose up --build web  # 可配置端口
```

健康检查已内置于 Dockerfile 与 Compose（`GET /healthz`）。

## 验证（verify 服务）

```bash
docker compose up --build --exit-code-from verify verify
```

`verify` 依次执行**代码测试**（单元测试）、**构建检查**（源码编译、文件齐备、
模块导入、引擎级核对）、**HTTP 冒烟**（健康检查、复核页、经接口核对
“静默后报警”反例、一致对照、非法录入反馈），完成即退出并打印退出码
（0 通过 / 1 失败）。

## 本地运行（无 Docker）

```bash
python3 server.py                 # PORT=9000 python3 server.py 可改端口
python3 -m unittest discover -s tests -v
WEB_URL=http://127.0.0.1:8080 python3 verify.py
```
