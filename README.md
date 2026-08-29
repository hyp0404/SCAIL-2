# SCAIL-2 RunningHub MCP（Railway 部署版）

这是一个可直接上传到 GitHub、再由 Railway 部署的远程 MCP 服务。它不会在 Railway 上运行 SCAIL-2 大模型；Railway 只负责接收 ChatGPT 请求、下载附件、上传到 RunningHub、提交任务并查询结果。GPU 推理由 RunningHub 完成，生成费用从你的 RunningHub 账户扣 RH 币。

默认接入的应用：

- 名称：SCAIL-2 影视级角色动作驱动与替换
- RunningHub 应用 ID：`2064610888811900929`
- 页面：<https://www.runninghub.cn/ai-detail/2064610888811900929>

## 功能

- `inspect_scail_app`：只读检查应用和输入节点，不创建任务。
- `submit_scail_replacement`：参考人物图片 + 原视频 → 完整人物替换视频。
- `get_scail_task`：查询状态、视频结果 URL、RH 消耗。
- `wait_scail_task`：最长等待 15 分钟，超时不会取消 RunningHub 任务。
- 自动调用 RunningHub 最新二进制媒体上传接口。
- 自动读取 AI 应用 API 示例，不把图片、视频节点编号写死。
- 支持 ChatGPT 附件产生的临时下载 URL。
- 每次创建任务必须显式设置 `confirm_rh_charge=true`，防止误扣 RH。

## 一、准备 RunningHub

### 1. 获取 API Key

登录 RunningHub，在个人中心/API 设置中创建 API Key。不要把 Key 写入 GitHub 文件；后面只放到 Railway Variables。

### 2. 确保应用能被 API 调用

打开上面的 SCAIL-2 应用。建议先在网页端手工运行一次 3～5 秒测试，再查看页面中的“API”或“工作流 API”。

如果公共应用不能直接通过你的 API Key 调用：

1. 将应用复制/下载到你的 RunningHub 工作区。
2. 在你的工作区发布或启用 API 调用。
3. 复制你自己的新应用 ID。
4. Railway 中把 `SCAIL_WEBAPP_ID` 改成该 ID。

公共页面的 ID 能否直接调用由应用作者的权限设置决定，所以服务启动后第一步必须调用 `inspect_scail_app` 验证。

## 二、建立 GitHub 仓库

1. 在 GitHub 新建空仓库，例如 `scail2-runninghub-mcp`。
2. 将本目录全部文件上传到仓库根目录。
3. 确认 GitHub 中没有 `.env`，只能有 `.env.example`。

也可以本地执行：

```bash
git init
git add .
git commit -m "Initial SCAIL-2 RunningHub MCP"
git branch -M main
git remote add origin https://github.com/你的用户名/scail2-runninghub-mcp.git
git push -u origin main
```

## 三、Railway 部署

### 1. 创建服务

1. Railway → **New Project**。
2. 选择 **Deploy from GitHub repo**。
3. 选择刚创建的仓库。
4. Railway 会读取 `Dockerfile` 和 `railway.json` 自动构建。

### 2. 配置 Variables

在 Railway 服务 → **Variables** 中添加：

| 变量 | 必填 | 值 |
|---|---:|---|
| `RUNNINGHUB_API_KEY` | 是 | 你的 RunningHub API Key |
| `SCAIL_WEBAPP_ID` | 是 | 默认 `2064610888811900929`，或你复制后自己的应用 ID |
| `RUNNINGHUB_BASE_URL` | 否 | `https://www.runninghub.cn` |
| `MAX_DOWNLOAD_MB` | 否 | 默认 `500` |
| `SCHEMA_CACHE_SECONDS` | 否 | 默认 `600` |
| `PUBLIC_DOMAIN` | 自定义域名时 | 例如 `scail.example.com`，不要带 `https://` |

不要手工设置 `PORT`，Railway 会自动注入。

本服务不需要 Railway Volume。附件只暂存在容器临时目录，上传 RunningHub 后立即删除；任务及作品保存在 RunningHub。

### 3. 生成公网域名

Railway 服务 → **Settings** → **Networking** → **Generate Domain**。

假设域名是：

```text
scail2-production.up.railway.app
```

检查：

```text
https://scail2-production.up.railway.app/health
```

应返回：

```json
{"status":"ok","configured":true}
```

MCP 地址为：

```text
https://scail2-production.up.railway.app/mcp
```

如果 `/health` 正常但 MCP 返回 `421 Invalid Host header`，说明使用了自定义域名但未配置 `PUBLIC_DOMAIN`。把它设置成实际公网域名并重新部署。

## 四、连接 ChatGPT

1. ChatGPT 工作区设置 → Apps/Connectors → 创建自定义 MCP。
2. 名称填写 `SCAIL-2`。
3. MCP URL 填写 Railway 的 `/mcp` 完整地址。
4. 保存并连接。

连接后先执行：

```text
调用 inspect_scail_app，只读检查，不创建任务。
```

检查结果应包含至少一个图片输入和一个视频输入。如果自动识别的节点不正确，按照“节点覆盖”部分配置。

## 五、第一次安全测试

推荐先准备：

- 一张清晰、单人、正面或大半侧面、最好包含全身的参考图片。
- 一段 3～5 秒、单人、人物无遮挡、镜头变化较小的 MP4。

对 ChatGPT 说：

```text
使用 SCAIL-2，用参考图片替换视频中的主要人物。
先提交 5 秒测试，我同意本次消耗 RH 币。提交后返回 task_id，不要重复提交。
```

提交成功后使用 `get_scail_task` 查询。不要因为任务仍在 `QUEUED` 或 `RUNNING` 状态而重复创建任务。

## 六、节点自动识别与手工覆盖

服务从 RunningHub 的 `/api/webapp/apiCallDemo` 动态读取 `nodeInfoList`，优先按字段类型和中英文描述识别：

- `reference_image`：参考人物图
- `source_video`：原视频/驱动视频
- `mode`：Replacement/角色替换
- `prompt`：提示词
- `target_subject`：原视频中需要编辑的主体
- `reference_subject`：参考图中的主体

如果应用作者更新后自动识别错误：

1. 调用 `inspect_scail_app(force_refresh=true)`。
2. 找到正确的 `nodeId` 和 `fieldName`。
3. Railway Variables 添加一行 JSON（必须单行）：

```json
{"reference_image":{"nodeId":"10","fieldName":"image"},"source_video":{"nodeId":"7","fieldName":"video"},"mode":{"nodeId":"1","fieldName":"mode"}}
```

变量名为：

```text
SCAIL_NODE_OVERRIDES_JSON
```

重新部署后再次只读检查。

## 七、本地运行

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

将 `.env` 中变量导入 shell 后启动：

```bash
set -a
source .env
set +a
uvicorn server:app --host 127.0.0.1 --port 8000
```

本地 MCP：`http://127.0.0.1:8000/mcp`

## 八、常见错误

### `configured:false`

Railway 未配置 `RUNNINGHUB_API_KEY`，或变量修改后尚未重新部署。

### `无法读取 SCAIL-2 应用输入`

常见原因：

- 公共应用未对你的账户开放 API。
- `SCAIL_WEBAPP_ID` 填错。
- 需要先复制应用到自己的 RunningHub 工作区并启用 API。

### `未能自动识别参考图片或驱动视频节点`

调用 `inspect_scail_app`，再配置 `SCAIL_NODE_OVERRIDES_JSON`。

### RunningHub 有任务，但 ChatGPT 超时

视频任务可能运行数分钟到数十分钟。超时不表示 RunningHub 任务停止。保留 `task_id`，稍后调用 `get_scail_task`。

### 背景或商品发生变化

- 将提示词改为只替换人物、保持背景和物品不变。
- 使用更短的视频测试。
- 参考图人物大小和原视频人物占比尽量相似。
- 遮挡、快速转身时增加侧面/背面参考图需要换用支持多参考图的 SCAIL-2 应用并更新 `SCAIL_WEBAPP_ID`。

## 九、安全说明

- API Key 只存 Railway Variables，不提交 GitHub。
- 日志不打印 API Key 和媒体内容。
- 媒体下载拒绝 localhost、内网和保留 IP，降低 SSRF 风险。
- 每次生成必须显式确认 RH 扣费。
- Railway 随机域名不是身份验证。如果你准备公开分享此 MCP，应在前面增加 OAuth/API Gateway；个人自用时不要公开传播 MCP 地址。

## RunningHub 接口

本项目使用：

- `GET /api/webapp/apiCallDemo`：读取 AI 应用调用示例及输入节点。
- `POST /openapi/v2/media/upload/binary`：上传图片/视频。
- `POST /task/openapi/ai-app/run`：提交 AI 应用任务。
- `POST /task/openapi/status`：查询任务状态。
- `POST /task/openapi/outputs`：读取任务输出及 RH 消耗。

RunningHub 已标记旧状态/输出接口将废弃；接口完全下线时，只需在 `server.py` 的 `get_task` 中切换到 RunningHub V2 查询接口，其余上传、节点识别和 MCP 工具无需修改。

