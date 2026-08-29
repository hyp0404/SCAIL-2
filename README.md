# RunningHub 换人 + 换背景 MCP

这是一个可部署到 Railway 并连接 ChatGPT 的双阶段 RunningHub MCP：

1. **SCAIL-2 单人人物替换**：使用人物参考图替换原视频人物，保留动作与机位。
2. **Bernini-R 参考背景替换**：使用背景图片替换第一阶段视频的环境。
3. **本地收尾**（默认开启）：恢复原视频音频，并把最终时长严格裁切/补帧到原视频时长。

两个生成阶段都会消耗 RunningHub RH。提交工具要求 `confirm_rh_charge=true`，不会在未确认时扣费。

## 使用的 RunningHub 应用

| 阶段 | 应用 | 默认 WebApp ID |
| --- | --- | --- |
| 换人 | SCAIL-2 单人人物替换 | `2067490689415471105` |
| 换背景 | Bernini-R 参考背景视频替换流程 | `2062558412986216449` |

项目不会把节点编号写死。服务器启动后会从 RunningHub 获取最新 `nodeInfoList`，按字段类型、节点名称和描述自动识别人物图、源视频、背景图与提示词节点。应用作者调整节点后，可通过环境变量覆盖映射，无须修改代码。

## 文件说明

```text
.
├── server.py          # MCP 服务和双阶段编排
├── requirements.txt   # Python 依赖
├── Dockerfile         # Railway Docker 构建
├── railway.json       # Railway 构建、健康检查与重启配置
├── .env.example       # 环境变量模板
├── .dockerignore
├── .gitignore
├── tests/test_server.py # 节点映射与结果解析测试
└── README.md
```

## 一、上传到 GitHub

新建一个空 GitHub 仓库，然后把本目录中的全部文件上传到仓库根目录。不要上传真实 `.env`，更不要把 RunningHub API Key 写入代码或提交到 GitHub。

也可在本地执行：

```bash
git init
git add .
git commit -m "Initial RunningHub person and background MCP"
git branch -M main
git remote add origin https://github.com/你的用户名/你的仓库名.git
git push -u origin main
```

## 二、Railway 部署

### 1. 创建服务

1. 打开 Railway，点击 **New Project**。
2. 选择 **Deploy from GitHub repo**。
3. 选择刚上传的仓库。
4. Railway 会读取 `railway.json` 和 `Dockerfile` 自动构建。

### 2. 添加环境变量

在 Railway 服务的 **Variables** 页面添加：

| 变量 | 值 | 是否必需 |
| --- | --- | --- |
| `RUNNINGHUB_API_KEY` | RunningHub 控制台中的 API Key | 必需 |
| `SCAIL_WEBAPP_ID` | `2067490689415471105` | 建议填写 |
| `BERNINI_WEBAPP_ID` | `2062558412986216449` | 建议填写 |
| `DATA_DIR` | `/data` | 必需（配合 Volume） |
| `PUBLIC_BASE_URL` | 部署后生成的 Railway 域名，如 `https://xxx.up.railway.app` | 部署生成域名后补填 |
| `MAX_UPLOAD_MB` | `300` | 可选 |
| `MAX_DOWNLOAD_MB` | `2048` | 可选 |
| `NODE_CACHE_TTL_SECONDS` | `3600` | 可选 |
| `ALLOW_CONCURRENT_PIPELINES` | `false` | 建议保持 false |
| `KEEP_INTERMEDIATE_FILES` | `false` | 可选 |

不要手工设置 `PORT`，Railway 会自动注入。

### 3. 挂载 Volume

1. 打开 Railway 项目画布。
2. 选择当前服务，进入 **Volumes**。
3. 点击 **Add Volume**。
4. Mount Path 填写：`/data`。
5. 保存并重新部署。

Volume 保存任务状态、原始音频以及最终视频。没有 Volume 时，重新部署可能丢失正在编排的任务和已处理视频。

### 4. 生成域名

1. 进入 **Settings → Networking → Public Networking**。
2. 点击 **Generate Domain**。
3. 复制生成的 HTTPS 域名。
4. 返回 Variables，将 `PUBLIC_BASE_URL` 设置为该域名，末尾不要加 `/`。
5. 再部署一次。

检查：

```text
https://你的域名/health
```

成功时应返回 `"ok": true` 和 `"api_key_configured": true`。

## 三、连接 ChatGPT

MCP 地址：

```text
https://你的域名/mcp
```

在 ChatGPT 的应用/MCP 设置中添加这个地址。若连接界面要求末尾斜杠，也可以使用：

```text
https://你的域名/mcp/
```

连接或重新连接后，先调用：

```text
inspect_person_background_pipeline
```

这是只读检查，不消耗 RH。确认返回的自动映射中：

- SCAIL `person_image` 指向人物参考图节点；
- SCAIL `source_video` 指向驱动视频节点；
- Bernini `source_video` 指向原视频节点；
- Bernini `background_image` 指向背景参考图节点。

如果映射错误或提示歧义，在 Railway Variables 中填写相应的 `*_NODE_ID` 和 `*_FIELD_NAME`。完整变量名见 `.env.example`。修改后重新部署，再次执行检查。

## 四、生成视频

准备三个附件：

1. `reference_person_image_file`：清晰、完整的人物参考图；
2. `source_video_file`：动作驱动视频；
3. `background_image_file`：**无人物、无文字、无水印的干净背景图**。

背景图若仍包含人物，可能生成重复人物或人物残影。应先对图片做人物移除和背景补全。

调用：

```text
submit_person_background_replacement_from_chatgpt_attachments
```

关键参数：

```json
{
  "confirm_rh_charge": true,
  "stage1_output_index": 0,
  "preserve_original_audio_and_duration": true
}
```

提交成功后会返回 `pipeline_id` 和 `stage1_task_id`。继续调用：

```text
wait_person_background_replacement
```

等待最长 900 秒。若超时，保留同一个 `pipeline_id` 再调用一次，不要重复提交，以免重复消耗 RH。

流程状态：

| 状态 | 含义 |
| --- | --- |
| `stage1_running` | SCAIL 正在换人 |
| `stage2_preparing` | 正在下载第一阶段结果并提交 Bernini |
| `stage2_running` | Bernini 正在换背景 |
| `completed` | 完成，读取 `final_video_url` |
| `stage1_failed` | 换人失败，不会提交第二阶段 |
| `stage2_failed` | 背景阶段失败 |

默认禁止同时运行多个编排任务，以避免 RunningHub 并发限制导致无效扣费或 `805` 错误。前一个任务完成后再提交下一个。

## 五、人物替换结果有多个版本

部分 SCAIL 应用会返回多个 MP4。默认使用第一个，即：

```json
{"stage1_output_index": 0}
```

如果第一版本不合适，可在新任务中选择 `1`。不要在同一个 pipeline 中重复切换，因为第二阶段一旦提交已经产生 RH 消耗。

## 六、常见问题

### `RUNNINGHUB_API_KEY is not configured`

Railway 没有添加 API Key，或添加后没有重新部署。

### `Ambiguous image/video input`

应用出现多个同类上传节点。调用检查工具，查看 `all_nodes`，然后设置 `.env.example` 中对应的节点覆盖变量。

### 第一阶段成功但第二阶段没有开始

再次调用 `get_person_background_replacement` 或 `wait_person_background_replacement`。本项目采用可恢复的分阶段编排，避免单个 ChatGPT 请求连接中断后丢失任务。

### 返回 805 或并发错误

等待当前 RunningHub 任务完成。保持 `ALLOW_CONCURRENT_PIPELINES=false`，不要重复提交。

### 背景出现第二个人物

上传的是包含人物的完整参考照片，而不是干净背景图。先移除照片人物并补全被遮挡区域。

### 最终链接打不开

确认 `PUBLIC_BASE_URL` 与 Railway 当前公网域名完全一致，并检查 Volume 是否挂载到 `/data`。原始 Bernini 结果仍保存在 `stage2_results` 中，可作为备用链接。

## 七、本地启动

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# 手动把 .env 中的变量导入当前 shell，或逐项 export
python server.py
```

健康检查：`http://localhost:8000/health`

MCP：`http://localhost:8000/mcp`

运行基础测试：

```bash
python -m unittest discover -s tests -v
```

## API 依据

项目使用 RunningHub 官方接口：

- `GET /api/webapp/apiCallDemo`：获取 AI App 最新 `nodeInfoList`；
- `POST /openapi/v2/media/upload/binary`：上传图片和视频；
- `POST /task/openapi/ai-app/run`：提交 AI App 任务；
- `POST /openapi/v2/query`：查询任务状态和结果。
