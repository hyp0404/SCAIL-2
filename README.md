# SCAIL-2 + Wan2.2 VACE 人物与背景替换 MCP

这个项目把两个 RunningHub AI 应用串成一条自动流水线：

1. SCAIL-2 根据参考图替换原视频人物；
2. Wan2.2 VACE 使用同一张参考图替换背景；
3. FFmpeg 接回原视频音频，并将最终视频校准为原视频时长；
4. 通过 MCP 将任务提交、查询、重试和最终下载链接提供给 ChatGPT。

默认应用：

- SCAIL-2：`2064610888811900929`
- Wan2.2 VACE 无限时长换背景：`2035730491302744066`

> 两阶段完整生成会消耗两次 RunningHub RH。工具要求 `confirm_rh_charge=true`，必须先取得用户明确确认。

## 一、项目文件

| 文件 | 用途 |
|---|---|
| `server.py` | MCP 服务、RunningHub 编排、任务持久化及 FFmpeg 后处理 |
| `Dockerfile` | Railway Docker 构建，内置 FFmpeg |
| `requirements.txt` | Python 依赖 |
| `railway.json` | Railway 构建、健康检查及重启策略 |
| `.env.example` | 环境变量模板 |

## 二、准备 RunningHub

### 1. 获取 API Key

登录 RunningHub，在个人中心或 API 调用页面复制 API Key。不要把 API Key 写进 GitHub。

### 2. 先在网页各运行一次应用

先分别打开两个应用，使用测试素材成功运行一次：

- SCAIL-2：<https://www.runninghub.cn/ai-detail/2064610888811900929>
- Wan2.2 VACE：<https://www.runninghub.cn/ai-detail/2035730491302744066>

这样可以确认应用仍可调用，并确认账户 RH 余额充足。

### 3. 查看 API 节点

在应用详情页点击“API 调用”，确认对外暴露了以下输入：

SCAIL-2：

- 人物参考图；
- 驱动/原视频；
- 提示词。

Wan2.2 VACE：

- SCAIL 输出视频；
- 目标背景参考图；
- 场景/背景描述；
- 可选的视频帧数。

服务启动后会自动调用 RunningHub 的 `apiCallDemo` 获取 `nodeInfoList` 并根据类型、名称和描述映射节点。通常不需要手动填写节点 ID。

如果 `inspect_scail_vace_pipeline` 报“Cannot uniquely map node”，再把 API 调用页面中的节点 ID 写入 Railway 环境变量。所有覆盖变量都列在 `.env.example` 中。

## 三、上传到 GitHub

1. 在 GitHub 新建空仓库，例如 `scail-vace-mcp`。
2. 解压本项目，将解压后的文件放在仓库根目录。
3. 提交并推送：

```bash
git init
git add .
git commit -m "Initial SCAIL VACE MCP"
git branch -M main
git remote add origin https://github.com/YOUR_NAME/scail-vace-mcp.git
git push -u origin main
```

确认 GitHub 中没有 `.env` 文件和真实 API Key。

## 四、Railway 完整配置

### 1. 创建服务

1. Railway → **New Project**。
2. 选择 **Deploy from GitHub repo**。
3. 选择刚创建的仓库。
4. Railway 会自动识别根目录的 `Dockerfile`。

### 2. 添加 Volume

必须挂载持久化 Volume，否则重新部署后任务记录和最终视频会丢失。

1. 打开项目画布。
2. 选择服务 → **Volumes** → **Add Volume**。
3. Mount Path 填：

```text
/data
```

4. 不要挂载到 `/app`，否则会覆盖程序文件。

### 3. 添加环境变量

进入服务 → **Variables**，添加：

| 变量 | 必填 | 值 |
|---|---:|---|
| `RUNNINGHUB_API_KEY` | 是 | 你的 RunningHub API Key |
| `SCAIL_WEBAPP_ID` | 是 | `2064610888811900929` |
| `VACE_WEBAPP_ID` | 是 | `2035730491302744066` |
| `DATA_DIR` | 是 | `/data` |
| `MAX_INPUT_BYTES` | 建议 | `524288000` |
| `HTTP_TIMEOUT_SECONDS` | 建议 | `120` |
| `MCP_ALLOWED_ORIGINS` | 建议 | `https://chatgpt.com,https://chat.openai.com` |
| `PUBLIC_BASE_URL` | 域名生成后填写 | `https://你的域名.up.railway.app` |

`PORT` 不需要手动添加，Railway 会自动注入。

### 4. 生成公网域名

1. 服务 → **Settings** → **Networking**。
2. 点击 **Generate Domain**。
3. 将完整 HTTPS 域名写回 `PUBLIC_BASE_URL`，末尾不要加 `/`。
4. Railway 会重新部署一次。

服务会从 `PUBLIC_BASE_URL` 或 Railway 自动提供的 `RAILWAY_PUBLIC_DOMAIN` 建立 MCP Host 允许列表，防止部署后出现 `421 Invalid Host Header`。

### 5. 健康检查

`railway.json` 已设置：

```text
Healthcheck Path: /health
Timeout: 300 秒
Restart Policy: ON_FAILURE
```

部署完成后浏览器访问：

```text
https://你的域名.up.railway.app/health
```

应返回：

```json
{"status":"ok","service":"scail-vace-mcp"}
```

MCP 地址为：

```text
https://你的域名.up.railway.app/mcp
```

## 五、在 ChatGPT 中连接

1. 在 ChatGPT Work 的插件/MCP 管理页面新增远程 MCP。
2. 地址填写 Railway 的 `/mcp` 地址。
3. 保存并连接。
4. 首先调用只读工具：

```text
inspect_scail_vace_pipeline
```

必须看到：

```json
{"connected": true, "rh_charge": false}
```

并检查 SCAIL 和 VACE 的人物图、视频、背景图、提示词映射是否正确。

## 六、正常使用顺序

用户上传：

- 一张同时包含目标人物和目标背景的参考图片；
- 一段需要替换人物和背景的原视频。

提交工具：

```text
submit_scail_vace_replacement_from_chatgpt_attachments
```

关键参数：

```json
{
  "reference_person_and_background_image_file": "参考图附件",
  "source_video_file": "原视频附件",
  "confirm_rh_charge": true,
  "preserve_original_audio_and_duration": true,
  "stage1_output_index": 0
}
```

提交后会返回 `pipeline_id`。继续调用：

```text
wait_scail_vace_replacement
```

如果长等待被网关中断，使用：

```text
get_scail_vace_replacement
```

读取同一个 `pipeline_id`，不要重新提交收费任务。

完成后返回带随机令牌的 `final_video_url`。最终文件由 Railway `/data` Volume 保存。

## 七、为什么能保持原时长

提交前，服务使用 `ffprobe` 读取原视频：

- 精确时长；
- FPS；
- 分辨率；
- 是否包含音频。

VACE 完成后，服务使用 FFmpeg：

1. 按原视频时长调整生成视频时间轴；
2. 按原视频 FPS 输出；
3. 丢弃生成视频自带音轨；
4. 接回原视频音频；
5. 使用 `-t` 精确裁剪到原视频时长。

最终状态中的 `final_video_info.duration_delta_seconds` 可用于核对误差。

## 八、失败与安全重试

### 第一阶段失败

查看返回的 `stage1_task_id`，到 RunningHub 任务列表检查具体节点错误。不要直接重新提交，先确认节点、余额和输入素材。

### 第二阶段失败

调用：

```text
retry_vace_background_only
```

它会复用已成功的 SCAIL 视频，不重新消耗 SCAIL RH；但会消耗一次新的 VACE RH，因此仍要求：

```json
{"confirm_rh_charge": true}
```

### 自动节点识别失败

调用 RunningHub 应用的 API 示例，找到正确节点 ID，然后在 Railway Variables 添加对应的 `*_NODE_ID` 和 `*_FIELD_NAME`。

### ChatGPT 附件路径不存在

工具同时支持服务器可读路径和 HTTP(S) 临时下载地址。如果附件已过期，重新上传附件并再次调用；不要把 Windows 本地路径传给 Railway。

### 最终链接打不开

检查：

1. `PUBLIC_BASE_URL` 是否为当前 Railway HTTPS 域名；
2. Volume 是否挂载在 `/data`；
3. Railway 服务是否仍在运行；
4. 下载 URL 中的 `token` 参数是否完整。

## 九、上线前检查清单

- [ ] 两个 RunningHub 应用都能在网页端单独成功运行；
- [ ] Railway `/data` Volume 已挂载；
- [ ] `RUNNINGHUB_API_KEY` 只保存在 Railway Variables；
- [ ] `/health` 返回 `ok`；
- [ ] `/mcp` 已连接 ChatGPT；
- [ ] `inspect_scail_vace_pipeline` 映射正确且不消耗 RH；
- [ ] 先用 5～10 秒、单人物、镜头稳定的视频测试；
- [ ] 提交前明确确认会消耗两次 RH；
- [ ] 完成后核对 `duration_delta_seconds` 和原音频。

## 十、官方参考

- RunningHub AI 应用提交接口：<https://www.runninghub.cn/runninghub-api-doc-cn/api-425749010>
- RunningHub AI 应用 API 示例：<https://www.runninghub.cn/runninghub-api-doc-cn/api-425749011>
- RunningHub 完整接入示例：<https://www.runninghub.cn/runninghub-api-doc-cn/doc-8287340>
- Railway Dockerfile：<https://docs.railway.com/builds/dockerfiles>
- Railway Config as Code：<https://docs.railway.com/config-as-code/reference>
- OpenAI MCP 文档：<https://developers.openai.com/api/docs/mcp>
