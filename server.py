"""RunningHub two-stage person + background replacement MCP server.

Stage 1: SCAIL-2 replaces the person while keeping motion/camera structure.
Stage 2: Bernini-R replaces the environment from a background reference image.

The server discovers each public AI App's configurable nodeInfoList at runtime,
so it does not depend on brittle hard-coded node IDs. Optional environment
variables can override automatic node selection when an app author changes the
published workflow.
"""

from __future__ import annotations

import asyncio
import json
import mimetypes
import os
import shutil
import subprocess
import time
import uuid
from pathlib import Path
from typing import Annotated, Any

import httpx
from fastmcp import FastMCP
from pydantic import Field
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse


RUNNINGHUB_BASE_URL = os.getenv("RUNNINGHUB_BASE_URL", "https://www.runninghub.cn").rstrip("/")
RUNNINGHUB_API_KEY = os.getenv("RUNNINGHUB_API_KEY", "").strip()
SCAIL_WEBAPP_ID = os.getenv("SCAIL_WEBAPP_ID", "2067490689415471105").strip()
BERNINI_WEBAPP_ID = os.getenv("BERNINI_WEBAPP_ID", "2062558412986216449").strip()
DATA_DIR = Path(os.getenv("DATA_DIR", "/data")).expanduser()
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "").rstrip("/")
MAX_UPLOAD_MB = int(os.getenv("MAX_UPLOAD_MB", "300"))
MAX_DOWNLOAD_MB = int(os.getenv("MAX_DOWNLOAD_MB", "2048"))
NODE_CACHE_TTL_SECONDS = int(os.getenv("NODE_CACHE_TTL_SECONDS", "3600"))
ALLOW_CONCURRENT_PIPELINES = os.getenv("ALLOW_CONCURRENT_PIPELINES", "false").lower() == "true"
KEEP_INTERMEDIATE_FILES = os.getenv("KEEP_INTERMEDIATE_FILES", "false").lower() == "true"

if not DATA_DIR.parent.exists():
    DATA_DIR = Path("./data")
DATA_DIR.mkdir(parents=True, exist_ok=True)
PIPELINES_DIR = DATA_DIR / "pipelines"
PIPELINES_DIR.mkdir(parents=True, exist_ok=True)

DEFAULT_STAGE1_PROMPT = (
    "Replace only the target person in the source video with the person from the reference image. "
    "Preserve the source video's complete motion, pose, timing, camera movement, framing, props and "
    "background. Preserve the reference person's identity, face, hair, body proportions, complete outfit "
    "and footwear. Natural temporal consistency, no flicker, no extra limbs, no text, no logo, no watermark."
)

DEFAULT_STAGE2_PROMPT = (
    "Replace only the entire environment and background with the scene from the background reference image. "
    "Keep the foreground person's identity, face, hair, body, clothing, footwear, hand-held objects, pose, "
    "motion, timing and camera framing unchanged. Use only the environment from the background reference; "
    "ignore and remove any person, text, logo or watermark contained in that reference. Match perspective, "
    "shadows, lighting and color naturally. No duplicate person and no background remnants."
)

mcp = FastMCP(
    name="RunningHub Person + Background Replacement",
    instructions=(
        "Two-stage RunningHub video editor. First use SCAIL-2 to replace the person, then Bernini-R "
        "to replace the background. Submitting a pipeline consumes RunningHub RH. Always require the "
        "user's explicit confirm_rh_charge=true before submission."
    ),
)

_node_cache: dict[str, tuple[float, list[dict[str, Any]]]] = {}
_state_lock = asyncio.Lock()


def _require_api_key() -> None:
    if not RUNNINGHUB_API_KEY:
        raise ValueError("RUNNINGHUB_API_KEY is not configured on Railway")


def _headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {RUNNINGHUB_API_KEY}",
        "Accept": "application/json",
        "User-Agent": "runninghub-person-background-mcp/1.0",
    }


def _client() -> httpx.AsyncClient:
    timeout = httpx.Timeout(connect=30.0, read=300.0, write=300.0, pool=30.0)
    return httpx.AsyncClient(timeout=timeout, follow_redirects=True, headers=_headers())


def _unwrap_response(payload: dict[str, Any]) -> dict[str, Any]:
    if "code" in payload and payload.get("code") not in (0, "0", None):
        message = payload.get("msg") or payload.get("message") or "RunningHub request failed"
        raise RuntimeError(f"RunningHub error {payload.get('code')}: {message}")
    data = payload.get("data")
    return data if isinstance(data, dict) else payload


async def _get_nodes(webapp_id: str, force_refresh: bool = False) -> list[dict[str, Any]]:
    _require_api_key()
    cached = _node_cache.get(webapp_id)
    if cached and not force_refresh and time.time() - cached[0] < NODE_CACHE_TTL_SECONDS:
        return cached[1]

    endpoint = f"{RUNNINGHUB_BASE_URL}/api/webapp/apiCallDemo"
    async with _client() as client:
        response = await client.get(endpoint, params={"webappId": webapp_id})
        response.raise_for_status()
        payload = response.json()
        try:
            data = _unwrap_response(payload)
        except RuntimeError:
            # Some RunningHub deployments still require apiKey in the query as
            # well as the Bearer header. Retry only after the safer form fails.
            response = await client.get(
                endpoint,
                params={"webappId": webapp_id, "apiKey": RUNNINGHUB_API_KEY},
            )
            response.raise_for_status()
            data = _unwrap_response(response.json())

    nodes = data.get("nodeInfoList")
    if not isinstance(nodes, list) or not nodes:
        raise RuntimeError(f"No configurable nodeInfoList returned for AI App {webapp_id}")
    normalized = [dict(node) for node in nodes if isinstance(node, dict)]
    _node_cache[webapp_id] = (time.time(), normalized)
    return normalized


def _node_text(node: dict[str, Any]) -> str:
    return " ".join(
        str(node.get(key, ""))
        for key in ("nodeName", "fieldName", "fieldType", "description", "descriptionEn")
    ).lower()


def _is_type(node: dict[str, Any], kind: str) -> bool:
    text = _node_text(node)
    field_type = str(node.get("fieldType", "")).upper()
    field_name = str(node.get("fieldName", "")).lower()
    node_name = str(node.get("nodeName", "")).lower()
    if kind == "image":
        return "IMAGE" in field_type or field_name in {"image", "images"} or "loadimage" in node_name
    if kind == "video":
        return "VIDEO" in field_type or "video" in field_name or "loadvideo" in node_name
    if kind == "prompt":
        return (
            field_name in {"prompt", "text", "positive", "positive_prompt"}
            or "STRING" in field_type
            or "prompt" in text
        )
    return False


def _env_override(prefix: str) -> tuple[str, str] | None:
    node_id = os.getenv(f"{prefix}_NODE_ID", "").strip()
    field_name = os.getenv(f"{prefix}_FIELD_NAME", "").strip()
    if node_id and field_name:
        return node_id, field_name
    if node_id or field_name:
        raise ValueError(f"Set both {prefix}_NODE_ID and {prefix}_FIELD_NAME")
    return None


def _score_node(node: dict[str, Any], positive: list[str], negative: list[str]) -> int:
    text = _node_text(node)
    score = sum(3 for token in positive if token.lower() in text)
    score -= sum(4 for token in negative if token.lower() in text)
    if str(node.get("fieldName", "")).lower() == "prompt":
        score += 2
    return score


def _resolve_node(
    nodes: list[dict[str, Any]],
    *,
    kind: str,
    env_prefix: str,
    positive: list[str],
    negative: list[str] | None = None,
    required: bool = True,
) -> dict[str, Any] | None:
    override = _env_override(env_prefix)
    if override:
        node_id, field_name = override
        for node in nodes:
            if str(node.get("nodeId")) == node_id and str(node.get("fieldName")) == field_name:
                return node
        raise ValueError(
            f"Configured {env_prefix}={node_id}/{field_name} is absent from the current nodeInfoList"
        )

    candidates = [node for node in nodes if _is_type(node, kind)]
    if not candidates:
        if required:
            raise ValueError(f"No {kind} input found; configure {env_prefix}_NODE_ID/FIELD_NAME")
        return None
    ranked = sorted(
        ((_score_node(node, positive, negative or []), node) for node in candidates),
        key=lambda item: item[0],
        reverse=True,
    )
    if len(ranked) == 1 or ranked[0][0] > ranked[1][0]:
        return ranked[0][1]
    if ranked[0][0] > 0:
        tied = [node for score, node in ranked if score == ranked[0][0]]
        if len(tied) == 1:
            return tied[0]
    options = [f"{n.get('nodeId')}/{n.get('fieldName')} ({n.get('description', '')})" for _, n in ranked]
    if required:
        raise ValueError(
            f"Ambiguous {kind} input for {env_prefix}. Set node override after inspect: {options}"
        )
    return None


def _resolve_stage1(nodes: list[dict[str, Any]]) -> dict[str, dict[str, Any] | None]:
    return {
        "person_image": _resolve_node(
            nodes,
            kind="image",
            env_prefix="SCAIL_REFERENCE_IMAGE",
            positive=["参考", "人物", "角色", "主图", "reference", "character", "subject"],
            negative=["背景", "background"],
        ),
        "source_video": _resolve_node(
            nodes,
            kind="video",
            env_prefix="SCAIL_SOURCE_VIDEO",
            positive=["驱动", "原视频", "参考视频", "source", "driving", "motion", "video"],
            negative=["背景", "background"],
        ),
        "prompt": _resolve_node(
            nodes,
            kind="prompt",
            env_prefix="SCAIL_PROMPT",
            positive=["提示", "prompt", "描述", "text"],
            required=False,
        ),
    }


def _resolve_stage2(nodes: list[dict[str, Any]]) -> dict[str, dict[str, Any] | None]:
    return {
        "source_video": _resolve_node(
            nodes,
            kind="video",
            env_prefix="BERNINI_SOURCE_VIDEO",
            positive=["原视频", "参考视频", "source", "input", "video"],
            negative=["背景视频", "background video"],
        ),
        "background_image": _resolve_node(
            nodes,
            kind="image",
            env_prefix="BERNINI_BACKGROUND_IMAGE",
            positive=["背景", "环境", "场景", "background", "environment", "scene"],
            negative=["人物", "character", "person"],
        ),
        "prompt": _resolve_node(
            nodes,
            kind="prompt",
            env_prefix="BERNINI_PROMPT",
            positive=["提示", "prompt", "替换要求", "描述", "text"],
            required=False,
        ),
    }


def _public_node(node: dict[str, Any] | None) -> dict[str, Any] | None:
    if node is None:
        return None
    return {
        "nodeId": str(node.get("nodeId", "")),
        "nodeName": node.get("nodeName"),
        "fieldName": node.get("fieldName"),
        "fieldType": node.get("fieldType"),
        "description": node.get("description") or node.get("descriptionEn"),
        "fieldValue": node.get("fieldValue"),
    }


def _override(node: dict[str, Any], value: Any) -> dict[str, Any]:
    return {
        "nodeId": str(node["nodeId"]),
        "fieldName": str(node["fieldName"]),
        "fieldValue": value,
    }


def _validate_local_file(path_text: str, allowed_suffixes: set[str]) -> Path:
    path = Path(path_text).expanduser().resolve()
    if not path.is_file():
        raise ValueError(f"Attachment/local file does not exist: {path}")
    if path.suffix.lower() not in allowed_suffixes:
        raise ValueError(f"Unsupported file type {path.suffix}; allowed: {sorted(allowed_suffixes)}")
    if path.stat().st_size > MAX_UPLOAD_MB * 1024 * 1024:
        raise ValueError(f"File exceeds MAX_UPLOAD_MB={MAX_UPLOAD_MB}: {path.name}")
    return path


async def _upload_file(path: Path) -> dict[str, Any]:
    endpoint = f"{RUNNINGHUB_BASE_URL}/openapi/v2/media/upload/binary"
    mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    async with _client() as client:
        with path.open("rb") as handle:
            response = await client.post(endpoint, files={"file": (path.name, handle, mime)})
        response.raise_for_status()
        data = _unwrap_response(response.json())
    file_name = data.get("fileName") or data.get("file_name")
    if not file_name:
        raise RuntimeError(f"RunningHub upload returned no fileName for {path.name}")
    return {
        "fileName": file_name,
        "download_url": data.get("download_url") or data.get("downloadUrl"),
        "type": data.get("type") or data.get("fileType"),
        "size": data.get("size"),
    }


async def _submit_app(webapp_id: str, overrides: list[dict[str, Any]]) -> dict[str, Any]:
    endpoint = f"{RUNNINGHUB_BASE_URL}/task/openapi/ai-app/run"
    body = {
        "webappId": webapp_id,
        "apiKey": RUNNINGHUB_API_KEY,
        "nodeInfoList": overrides,
    }
    async with _client() as client:
        response = await client.post(endpoint, json=body)
        response.raise_for_status()
        data = _unwrap_response(response.json())
    task_id = str(data.get("taskId") or "")
    if not task_id:
        raise RuntimeError(f"RunningHub accepted no taskId for AI App {webapp_id}")
    node_errors: dict[str, Any] = {}
    prompt_tips = data.get("promptTips")
    if isinstance(prompt_tips, str):
        try:
            node_errors = json.loads(prompt_tips).get("node_errors") or {}
        except (json.JSONDecodeError, AttributeError):
            pass
    if node_errors:
        raise RuntimeError(f"RunningHub workflow node validation failed: {node_errors}")
    return {"task_id": task_id, "task_status": data.get("taskStatus"), "raw": data}


async def _query_task(task_id: str) -> dict[str, Any]:
    endpoint = f"{RUNNINGHUB_BASE_URL}/openapi/v2/query"
    async with _client() as client:
        response = await client.post(endpoint, json={"taskId": task_id})
        response.raise_for_status()
        payload = response.json()
    if "code" in payload and payload.get("code") in (804, 813):
        return {"status": "RUNNING" if payload.get("code") == 804 else "QUEUED", "results": []}
    if "code" in payload and payload.get("code") == 805:
        return {"status": "FAILED", "error": payload.get("msg") or payload.get("data")}
    data = _unwrap_response(payload)
    status = str(data.get("status") or data.get("taskStatus") or "UNKNOWN").upper()
    return {
        "status": status,
        "results": data.get("results") or data.get("data") or [],
        "error": data.get("errorMessage") or data.get("failedReason") or data.get("errorCode"),
        "raw": data,
    }


def _video_results(results: Any) -> list[dict[str, Any]]:
    if isinstance(results, dict):
        results = [results]
    if not isinstance(results, list):
        return []
    videos = []
    for item in results:
        if isinstance(item, str):
            item = {"url": item}
        if not isinstance(item, dict):
            continue
        url = str(item.get("url") or item.get("fileUrl") or item.get("download_url") or "")
        output_type = str(item.get("outputType") or item.get("type") or "").lower()
        if url and (output_type in {"mp4", "mov", "video"} or url.lower().split("?")[0].endswith((".mp4", ".mov", ".webm"))):
            videos.append(dict(item, url=url))
    return videos


async def _download(url: str, destination: Path) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".part")
    total = 0
    async with _client() as client:
        async with client.stream("GET", url) as response:
            response.raise_for_status()
            with temporary.open("wb") as handle:
                async for chunk in response.aiter_bytes(1024 * 1024):
                    total += len(chunk)
                    if total > MAX_DOWNLOAD_MB * 1024 * 1024:
                        raise ValueError(f"Download exceeds MAX_DOWNLOAD_MB={MAX_DOWNLOAD_MB}")
                    handle.write(chunk)
    temporary.replace(destination)
    return destination


def _pipeline_dir(pipeline_id: str) -> Path:
    try:
        normalized = str(uuid.UUID(pipeline_id))
    except ValueError as exc:
        raise ValueError("Invalid pipeline_id") from exc
    path = PIPELINES_DIR / normalized
    path.mkdir(parents=True, exist_ok=True)
    return path


def _state_path(pipeline_id: str) -> Path:
    return _pipeline_dir(pipeline_id) / "state.json"


def _load_state(pipeline_id: str) -> dict[str, Any]:
    path = _state_path(pipeline_id)
    if not path.is_file():
        raise ValueError(f"Unknown pipeline_id: {pipeline_id}")
    return json.loads(path.read_text(encoding="utf-8"))


def _save_state(state: dict[str, Any]) -> None:
    state["updated_at"] = time.time()
    path = _state_path(str(state["pipeline_id"]))
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def _state_for_user(state: dict[str, Any]) -> dict[str, Any]:
    keep = {
        "pipeline_id", "status", "stage", "created_at", "updated_at",
        "stage1_task_id", "stage2_task_id", "stage1_results", "stage2_results",
        "final_video_url", "final_runninghub_upload_url", "warning", "error",
        "preserve_original_audio_and_duration",
    }
    result = {key: value for key, value in state.items() if key in keep and value not in (None, "", [])}
    if state.get("status") in {"stage1_running", "stage2_running"}:
        result["next_action"] = "Call wait_person_background_replacement or get_person_background_replacement"
    return result


def _active_pipeline() -> dict[str, Any] | None:
    for path in sorted(PIPELINES_DIR.glob("*/state.json"), reverse=True):
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if state.get("status") in {"stage1_running", "stage2_running", "stage2_preparing"}:
            return state
    return None


def _ffprobe_duration(path: Path) -> float:
    command = [
        "ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1", str(path),
    ]
    output = subprocess.check_output(command, text=True, timeout=30).strip()
    duration = float(output)
    if duration <= 0:
        raise ValueError("Source video has no positive duration")
    return duration


def _restore_audio_and_duration(raw_video: Path, source_video: Path, output: Path) -> None:
    duration = _ffprobe_duration(source_video)
    command = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-i", str(raw_video), "-i", str(source_video),
        "-filter_complex", f"[0:v]tpad=stop_mode=clone:stop_duration={duration:.6f}[v]",
        "-map", "[v]", "-map", "1:a:0?", "-t", f"{duration:.6f}",
        "-c:v", "libx264", "-preset", "medium", "-crf", "18",
        "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k",
        "-movflags", "+faststart", str(output),
    ]
    subprocess.run(command, check=True, timeout=1800)


async def _finalize_stage2(state: dict[str, Any], videos: list[dict[str, Any]]) -> dict[str, Any]:
    state["stage2_results"] = videos
    state["status"] = "completed"
    state["stage"] = 2
    state["final_video_url"] = videos[0]["url"]
    if not state.get("preserve_original_audio_and_duration"):
        _save_state(state)
        return state

    pipeline_dir = _pipeline_dir(state["pipeline_id"])
    source_video = pipeline_dir / "source_original.mp4"
    raw_video = pipeline_dir / "stage2_raw.mp4"
    final_video = pipeline_dir / "final.mp4"
    try:
        await _download(videos[0]["url"], raw_video)
        await asyncio.to_thread(_restore_audio_and_duration, raw_video, source_video, final_video)
        uploaded = await _upload_file(final_video)
        state["final_runninghub_upload_url"] = uploaded.get("download_url")
        if PUBLIC_BASE_URL:
            state["final_video_url"] = f"{PUBLIC_BASE_URL}/files/{state['pipeline_id']}/final.mp4"
        elif uploaded.get("download_url"):
            state["final_video_url"] = uploaded["download_url"]
        else:
            state["warning"] = "Final video is stored locally; set PUBLIC_BASE_URL to expose it"
    except Exception as exc:  # Preserve the successful Bernini output on post-processing failure.
        state["warning"] = f"Background replacement succeeded, but final audio/duration restoration failed: {exc}"
    finally:
        if raw_video.exists() and not KEEP_INTERMEDIATE_FILES:
            raw_video.unlink(missing_ok=True)
    _save_state(state)
    return state


async def _advance_pipeline(state: dict[str, Any]) -> dict[str, Any]:
    status = state.get("status")
    if status == "stage1_running":
        query = await _query_task(state["stage1_task_id"])
        remote_status = query["status"]
        if remote_status in {"FAILED", "ERROR", "CANCELLED", "CANCELED"}:
            state.update(status="stage1_failed", error=query.get("error") or query.get("raw"))
            _save_state(state)
            return state
        if remote_status != "SUCCESS":
            return state
        videos = _video_results(query.get("results"))
        if not videos:
            state.update(status="stage1_failed", error="SCAIL task succeeded but returned no video")
            _save_state(state)
            return state
        state["stage1_results"] = videos
        output_index = int(state.get("stage1_output_index", 0))
        if output_index >= len(videos):
            state.update(
                status="stage1_failed",
                error=f"stage1_output_index={output_index}, but SCAIL returned {len(videos)} video(s)",
            )
            _save_state(state)
            return state
        state["status"] = "stage2_preparing"
        _save_state(state)

        pipeline_dir = _pipeline_dir(state["pipeline_id"])
        stage1_video = pipeline_dir / "stage1.mp4"
        try:
            await _download(videos[output_index]["url"], stage1_video)
            stage1_upload = await _upload_file(stage1_video)
            nodes = await _get_nodes(BERNINI_WEBAPP_ID)
            mapping = _resolve_stage2(nodes)
            overrides = [
                _override(mapping["source_video"], stage1_upload["fileName"]),
                _override(mapping["background_image"], state["background_upload_file_name"]),
            ]
            if mapping["prompt"] is not None:
                overrides.append(_override(mapping["prompt"], state["stage2_prompt"]))
            submitted = await _submit_app(BERNINI_WEBAPP_ID, overrides)
            state.update(
                status="stage2_running",
                stage=2,
                stage2_task_id=submitted["task_id"],
            )
            _save_state(state)
        except Exception as exc:
            state.update(status="stage2_failed", error=f"Unable to submit Bernini stage: {exc}")
            _save_state(state)
        finally:
            if stage1_video.exists() and not KEEP_INTERMEDIATE_FILES:
                stage1_video.unlink(missing_ok=True)
        return state

    if status == "stage2_running":
        query = await _query_task(state["stage2_task_id"])
        remote_status = query["status"]
        if remote_status in {"FAILED", "ERROR", "CANCELLED", "CANCELED"}:
            state.update(status="stage2_failed", error=query.get("error") or query.get("raw"))
            _save_state(state)
            return state
        if remote_status != "SUCCESS":
            return state
        videos = _video_results(query.get("results"))
        if not videos:
            state.update(status="stage2_failed", error="Bernini task succeeded but returned no video")
            _save_state(state)
            return state
        return await _finalize_stage2(state, videos)
    return state


@mcp.custom_route("/health", methods=["GET"])
async def health(_: Request) -> JSONResponse:
    return JSONResponse(
        {
            "ok": True,
            "service": "runninghub-person-background-mcp",
            "api_key_configured": bool(RUNNINGHUB_API_KEY),
            "scail_webapp_id": SCAIL_WEBAPP_ID,
            "bernini_webapp_id": BERNINI_WEBAPP_ID,
        }
    )


@mcp.custom_route("/files/{pipeline_id}/final.mp4", methods=["GET"])
async def serve_final(request: Request) -> FileResponse | JSONResponse:
    pipeline_id = request.path_params["pipeline_id"]
    try:
        path = _pipeline_dir(pipeline_id) / "final.mp4"
    except ValueError:
        return JSONResponse({"error": "invalid pipeline id"}, status_code=404)
    if not path.is_file():
        return JSONResponse({"error": "final video not found"}, status_code=404)
    return FileResponse(path, media_type="video/mp4", filename=f"{pipeline_id}-final.mp4")


@mcp.tool(
    description=(
        "只读检查 SCAIL 与 Bernini-R 两个 RunningHub 应用的可调用节点和自动映射结果。"
        "不会创建任务或消耗 RH。部署后应先调用一次。"
    )
)
async def inspect_person_background_pipeline(force_refresh: bool = False) -> dict[str, Any]:
    stage1_nodes, stage2_nodes = await asyncio.gather(
        _get_nodes(SCAIL_WEBAPP_ID, force_refresh),
        _get_nodes(BERNINI_WEBAPP_ID, force_refresh),
    )
    stage1 = _resolve_stage1(stage1_nodes)
    stage2 = _resolve_stage2(stage2_nodes)
    return {
        "connected": True,
        "rh_charge": False,
        "stage1": {
            "webapp_id": SCAIL_WEBAPP_ID,
            "resolved": {key: _public_node(value) for key, value in stage1.items()},
            "all_nodes": [_public_node(node) for node in stage1_nodes],
        },
        "stage2": {
            "webapp_id": BERNINI_WEBAPP_ID,
            "resolved": {key: _public_node(value) for key, value in stage2.items()},
            "all_nodes": [_public_node(node) for node in stage2_nodes],
        },
    }


@mcp.tool(
    description=(
        "直接使用 ChatGPT 的人物参考图、原视频和干净背景图启动两阶段替换任务。"
        "第一阶段 SCAIL 换人，成功后自动由 Bernini-R 换背景；会消耗两次 RunningHub RH，"
        "必须显式确认 confirm_rh_charge=true。"
    )
)
async def submit_person_background_replacement_from_chatgpt_attachments(
    reference_person_image_file: Annotated[
        str,
        Field(description="ChatGPT 人物参考图片附件的绝对本地路径", json_schema_extra={"x-openai-file": True}),
    ],
    source_video_file: Annotated[
        str,
        Field(description="ChatGPT 原始动作视频附件的绝对本地路径", json_schema_extra={"x-openai-file": True}),
    ],
    background_image_file: Annotated[
        str,
        Field(description="ChatGPT 干净背景图片附件的绝对本地路径", json_schema_extra={"x-openai-file": True}),
    ],
    confirm_rh_charge: Annotated[
        bool,
        Field(description="必须为 true，确认同意 SCAIL 与 Bernini-R 两阶段均会消耗 RH"),
    ],
    stage1_prompt: str = DEFAULT_STAGE1_PROMPT,
    stage2_prompt: str = DEFAULT_STAGE2_PROMPT,
    stage1_output_index: Annotated[int, Field(ge=0, le=10)] = 0,
    preserve_original_audio_and_duration: bool = True,
) -> dict[str, Any]:
    if not confirm_rh_charge:
        raise ValueError("RH charge was not confirmed; set confirm_rh_charge=true to submit")
    _require_api_key()
    if not ALLOW_CONCURRENT_PIPELINES:
        active = _active_pipeline()
        if active:
            raise RuntimeError(
                f"Pipeline {active['pipeline_id']} is still active ({active['status']}). "
                "Finish/query it before starting another, or set ALLOW_CONCURRENT_PIPELINES=true."
            )

    person = _validate_local_file(reference_person_image_file, {".jpg", ".jpeg", ".png", ".webp"})
    source = _validate_local_file(source_video_file, {".mp4", ".mov", ".mkv", ".avi", ".webm"})
    background = _validate_local_file(background_image_file, {".jpg", ".jpeg", ".png", ".webp"})

    pipeline_id = str(uuid.uuid4())
    pipeline_dir = _pipeline_dir(pipeline_id)
    source_copy = pipeline_dir / "source_original.mp4"
    shutil.copy2(source, source_copy)
    state: dict[str, Any] = {
        "pipeline_id": pipeline_id,
        "status": "uploading",
        "stage": 1,
        "created_at": time.time(),
        "updated_at": time.time(),
        "stage1_prompt": stage1_prompt,
        "stage2_prompt": stage2_prompt,
        "stage1_output_index": stage1_output_index,
        "preserve_original_audio_and_duration": preserve_original_audio_and_duration,
    }
    _save_state(state)

    try:
        person_upload, source_upload, background_upload = await asyncio.gather(
            _upload_file(person), _upload_file(source), _upload_file(background)
        )
        nodes = await _get_nodes(SCAIL_WEBAPP_ID)
        mapping = _resolve_stage1(nodes)
        overrides = [
            _override(mapping["person_image"], person_upload["fileName"]),
            _override(mapping["source_video"], source_upload["fileName"]),
        ]
        if mapping["prompt"] is not None:
            overrides.append(_override(mapping["prompt"], stage1_prompt))
        submitted = await _submit_app(SCAIL_WEBAPP_ID, overrides)
        state.update(
            status="stage1_running",
            stage1_task_id=submitted["task_id"],
            background_upload_file_name=background_upload["fileName"],
        )
        _save_state(state)
        return _state_for_user(state)
    except Exception as exc:
        state.update(status="stage1_failed", error=str(exc))
        _save_state(state)
        raise


@mcp.tool(
    description=(
        "查询并推进两阶段换人换背景任务。SCAIL 成功后会使用已授权的 RH 自动提交 Bernini-R；"
        "最终返回背景替换视频链接。"
    )
)
async def get_person_background_replacement(pipeline_id: str) -> dict[str, Any]:
    async with _state_lock:
        state = _load_state(pipeline_id)
        state = await _advance_pipeline(state)
        return _state_for_user(state)


@mcp.tool(
    description=(
        "等待两阶段换人换背景任务完成。最长可等待 900 秒；超时会返回 pipeline_id，之后可继续调用。"
    )
)
async def wait_person_background_replacement(
    pipeline_id: str,
    timeout_seconds: Annotated[int, Field(ge=10, le=900)] = 900,
    poll_interval_seconds: Annotated[int, Field(ge=5, le=60)] = 20,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    terminal = {"completed", "stage1_failed", "stage2_failed"}
    while time.monotonic() < deadline:
        async with _state_lock:
            state = _load_state(pipeline_id)
            state = await _advance_pipeline(state)
        if state.get("status") in terminal:
            return _state_for_user(state)
        await asyncio.sleep(poll_interval_seconds)
    state = _load_state(pipeline_id)
    result = _state_for_user(state)
    result["wait_timed_out"] = True
    return result


@mcp.tool(description="列出 Railway Volume 中最近的换人换背景任务；不会消耗 RH。")
async def list_person_background_replacements(limit: Annotated[int, Field(ge=1, le=100)] = 20) -> list[dict[str, Any]]:
    states: list[dict[str, Any]] = []
    for path in PIPELINES_DIR.glob("*/state.json"):
        try:
            states.append(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            continue
    states.sort(key=lambda item: item.get("updated_at", 0), reverse=True)
    return [_state_for_user(state) for state in states[:limit]]


def main() -> None:
    port = int(os.getenv("PORT", "8000"))
    mcp.run(transport="http", host="0.0.0.0", port=port, path="/mcp")


if __name__ == "__main__":
    main()
