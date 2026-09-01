from __future__ import annotations

import asyncio
import json
import mimetypes
import os
import re
import secrets
import shutil
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response


RH_BASE_URL = os.getenv("RUNNINGHUB_BASE_URL", "https://www.runninghub.cn").rstrip("/")
RH_API_KEY = os.getenv("RUNNINGHUB_API_KEY", "").strip()
SCAIL_WEBAPP_ID = os.getenv("SCAIL_WEBAPP_ID", "2064610888811900929").strip()
VACE_WEBAPP_ID = os.getenv("VACE_WEBAPP_ID", "2035730491302744066").strip()
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "").rstrip("/")
RAILWAY_PUBLIC_DOMAIN = os.getenv("RAILWAY_PUBLIC_DOMAIN", "").strip()
DATA_DIR = Path(os.getenv("DATA_DIR", "/data")).resolve()
JOBS_DIR = DATA_DIR / "jobs"
STATE_FILE = DATA_DIR / "pipelines.json"
MAX_INPUT_BYTES = int(os.getenv("MAX_INPUT_BYTES", str(500 * 1024 * 1024)))
HTTP_TIMEOUT = float(os.getenv("HTTP_TIMEOUT_SECONDS", "120"))

DATA_DIR.mkdir(parents=True, exist_ok=True)
JOBS_DIR.mkdir(parents=True, exist_ok=True)

_public_host = urlparse(PUBLIC_BASE_URL).netloc or RAILWAY_PUBLIC_DOMAIN
_allowed_hosts = ["localhost:*", "127.0.0.1:*"]
if _public_host:
    _allowed_hosts.extend([_public_host, f"{_public_host}:*"])
_allowed_origins = [
    value.strip()
    for value in os.getenv(
        "MCP_ALLOWED_ORIGINS",
        "https://chatgpt.com,https://chat.openai.com",
    ).split(",")
    if value.strip()
]
if PUBLIC_BASE_URL:
    _allowed_origins.append(PUBLIC_BASE_URL)

mcp = FastMCP(
    "SCAIL-2 + Wan2.2 VACE Person and Background Replacement",
    instructions=(
        "Two-stage RunningHub video editor. Stage 1 replaces the person with SCAIL-2; "
        "stage 2 replaces the background with Wan2.2 VACE. Costly tools require explicit RH confirmation."
    ),
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=list(dict.fromkeys(_allowed_hosts)),
        allowed_origins=list(dict.fromkeys(_allowed_origins)),
    ),
)

_state_lock = asyncio.Lock()
_node_cache: dict[str, tuple[float, list[dict[str, Any]]]] = {}


def _require_config() -> None:
    missing = []
    if not RH_API_KEY:
        missing.append("RUNNINGHUB_API_KEY")
    if not SCAIL_WEBAPP_ID:
        missing.append("SCAIL_WEBAPP_ID")
    if not VACE_WEBAPP_ID:
        missing.append("VACE_WEBAPP_ID")
    if missing:
        raise RuntimeError("Missing environment variables: " + ", ".join(missing))


def _read_state_unlocked() -> dict[str, dict[str, Any]]:
    if not STATE_FILE.exists():
        return {}
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def _write_state_unlocked(state: dict[str, dict[str, Any]]) -> None:
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, STATE_FILE)


async def _get_pipeline(pipeline_id: str) -> dict[str, Any]:
    async with _state_lock:
        item = _read_state_unlocked().get(pipeline_id)
        if not item:
            raise ValueError(f"Unknown pipeline_id: {pipeline_id}")
        return dict(item)


async def _save_pipeline(item: dict[str, Any]) -> None:
    item["updated_at"] = time.time()
    async with _state_lock:
        state = _read_state_unlocked()
        state[item["pipeline_id"]] = item
        _write_state_unlocked(state)


def _safe_pipeline_id(value: str) -> str:
    if not re.fullmatch(r"[0-9a-fA-F-]{36}", value):
        raise ValueError("Invalid pipeline_id")
    return value


def _json_response_or_raise(response: httpx.Response) -> dict[str, Any]:
    response.raise_for_status()
    try:
        data = response.json()
    except ValueError as exc:
        raise RuntimeError(f"RunningHub returned non-JSON HTTP {response.status_code}") from exc
    if not isinstance(data, dict):
        raise RuntimeError("RunningHub returned an unexpected response")
    return data


def _auth_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {RH_API_KEY}"}


async def _api_call_demo(webapp_id: str, force_refresh: bool = False) -> list[dict[str, Any]]:
    _require_config()
    cached = _node_cache.get(webapp_id)
    if cached and not force_refresh and time.time() - cached[0] < 300:
        return cached[1]
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT, follow_redirects=True) as client:
        response = await client.get(
            f"{RH_BASE_URL}/api/webapp/apiCallDemo",
            params={"apiKey": RH_API_KEY, "webappId": webapp_id},
            headers=_auth_headers(),
        )
    payload = _json_response_or_raise(response)
    if payload.get("code") != 0:
        raise RuntimeError(f"Cannot inspect RunningHub app {webapp_id}: {payload}")
    nodes = (payload.get("data") or {}).get("nodeInfoList") or []
    if not isinstance(nodes, list):
        raise RuntimeError(f"RunningHub app {webapp_id} returned no nodeInfoList")
    _node_cache[webapp_id] = (time.time(), nodes)
    return nodes


def _node_text(node: dict[str, Any]) -> str:
    return " ".join(
        str(node.get(k, ""))
        for k in ("nodeId", "nodeName", "fieldName", "fieldType", "description", "descriptionEn")
    ).lower()


def _env_node(prefix: str, field_name: str, field_value: Any) -> dict[str, Any] | None:
    node_id = os.getenv(f"{prefix}_NODE_ID", "").strip()
    configured_field = os.getenv(f"{prefix}_FIELD_NAME", field_name).strip()
    if not node_id:
        return None
    return {"nodeId": node_id, "fieldName": configured_field, "fieldValue": field_value}


def _find_node(
    nodes: list[dict[str, Any]],
    field_types: set[str],
    include_groups: list[list[str]],
    exclude: tuple[str, ...] = (),
    fallback_unique: bool = True,
) -> dict[str, Any]:
    typed = [n for n in nodes if str(n.get("fieldType", "")).upper() in field_types]
    for words in include_groups:
        matches = []
        for node in typed:
            text = _node_text(node)
            if all(word.lower() in text for word in words) and not any(x.lower() in text for x in exclude):
                matches.append(node)
        if len(matches) == 1:
            return matches[0]
    filtered = [n for n in typed if not any(x.lower() in _node_text(n) for x in exclude)]
    if fallback_unique and len(filtered) == 1:
        return filtered[0]
    candidates = [
        {k: n.get(k) for k in ("nodeId", "nodeName", "fieldName", "fieldType", "description")}
        for n in filtered
    ]
    raise RuntimeError(f"Cannot uniquely map node. Candidates: {candidates}")


def _resolve_scail(nodes: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    person = _find_node(
        nodes,
        {"IMAGE"},
        [["参考", "图"], ["person"], ["人物"]],
        exclude=("背景", "background"),
    )
    video = _find_node(nodes, {"VIDEO"}, [["宣传视频"], ["驱动", "视频"], ["video"]])
    prompt = _find_node(nodes, {"STRING"}, [["提示词"], ["prompt"], ["text"]])
    return {"person_image": person, "source_video": video, "prompt": prompt}


def _resolve_vace(nodes: list[dict[str, Any]]) -> dict[str, dict[str, Any] | None]:
    video = _find_node(
        nodes,
        {"VIDEO"},
        [["原始视频"], ["原视频"], ["上传原始视频"], ["input", "video"], ["loadvideo"]],
        exclude=("音频", "audio"),
    )
    background = _find_node(
        nodes,
        {"IMAGE"},
        [["背景", "参考"], ["目标背景"], ["背景图"], ["background"], ["loadimage"]],
    )
    prompt = _find_node(
        nodes,
        {"STRING"},
        [["场景描述"], ["背景", "描述"], ["提示词"], ["prompt"], ["text"]],
    )
    frame = None
    try:
        frame = _find_node(
            nodes,
            {"INT", "INTEGER"},
            [["总帧"], ["帧数"], ["frame", "count"], ["length"]],
            fallback_unique=False,
        )
    except RuntimeError:
        pass
    return {"source_video": video, "background_image": background, "prompt": prompt, "frame_count": frame}


def _override(node: dict[str, Any], prefix: str, value: Any) -> dict[str, Any]:
    return _env_node(prefix, str(node.get("fieldName", "value")), value) or {
        "nodeId": str(node["nodeId"]),
        "fieldName": str(node["fieldName"]),
        "fieldValue": value,
    }


async def _materialize_input(value: str, destination: Path) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if value.startswith("file://"):
        value = value[7:]
    source = Path(value)
    if source.is_file():
        if source.resolve() != destination.resolve():
            shutil.copy2(source, destination)
        return destination
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError(
            "Attachment is neither a readable server-local file nor an HTTP(S) download URL. "
            "Reconnect the plugin and attach the file again."
        )
    total = 0
    async with httpx.AsyncClient(timeout=httpx.Timeout(HTTP_TIMEOUT, read=600), follow_redirects=True) as client:
        async with client.stream("GET", value) as response:
            response.raise_for_status()
            with destination.open("wb") as handle:
                async for chunk in response.aiter_bytes():
                    total += len(chunk)
                    if total > MAX_INPUT_BYTES:
                        raise ValueError(f"Input exceeds MAX_INPUT_BYTES ({MAX_INPUT_BYTES})")
                    handle.write(chunk)
    return destination


async def _download_result(url: str, destination: Path) -> Path:
    return await _materialize_input(url, destination)


async def _rh_upload(path: Path, file_type: str) -> str:
    mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    async with httpx.AsyncClient(timeout=httpx.Timeout(HTTP_TIMEOUT, read=600)) as client:
        with path.open("rb") as handle:
            response = await client.post(
                f"{RH_BASE_URL}/task/openapi/upload",
                headers=_auth_headers(),
                data={"apiKey": RH_API_KEY, "fileType": file_type.lower()},
                files={"file": (path.name, handle, mime)},
            )
    payload = _json_response_or_raise(response)
    if payload.get("code") != 0 or not (payload.get("data") or {}).get("fileName"):
        raise RuntimeError(f"RunningHub upload failed: {payload}")
    return str(payload["data"]["fileName"])


async def _rh_submit(webapp_id: str, node_info_list: list[dict[str, Any]]) -> str:
    body = {"webappId": webapp_id, "apiKey": RH_API_KEY, "nodeInfoList": node_info_list}
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
        response = await client.post(
            f"{RH_BASE_URL}/task/openapi/ai-app/run",
            headers={**_auth_headers(), "Content-Type": "application/json"},
            json=body,
        )
    payload = _json_response_or_raise(response)
    task_id = (payload.get("data") or {}).get("taskId")
    if payload.get("code") != 0 or not task_id:
        raise RuntimeError(f"RunningHub task submission failed: {payload}")
    return str(task_id)


async def _rh_outputs(task_id: str) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
        response = await client.post(
            f"{RH_BASE_URL}/task/openapi/outputs",
            headers={**_auth_headers(), "Content-Type": "application/json"},
            json={"apiKey": RH_API_KEY, "taskId": task_id},
        )
    return _json_response_or_raise(response)


def _extract_urls(value: Any) -> list[str]:
    urls: list[str] = []
    if isinstance(value, str):
        if value.startswith(("http://", "https://")):
            urls.append(value)
        else:
            try:
                urls.extend(_extract_urls(json.loads(value)))
            except (ValueError, TypeError):
                pass
    elif isinstance(value, list):
        for item in value:
            urls.extend(_extract_urls(item))
    elif isinstance(value, dict):
        for key, item in value.items():
            if key.lower() in {"url", "fileurl", "file_url", "video_url"} and isinstance(item, str):
                if item.startswith(("http://", "https://")):
                    urls.append(item)
            else:
                urls.extend(_extract_urls(item))
    return list(dict.fromkeys(urls))


def _task_state(payload: dict[str, Any]) -> tuple[str, list[str], Any]:
    code = payload.get("code")
    data = payload.get("data")
    if code == 0 and data:
        urls = _extract_urls(data)
        return "succeeded", urls, None
    if code == 805:
        return "failed", [], data or payload.get("msg") or "RunningHub workflow failed"
    if code in (804, 813) or not data:
        return "running", [], None
    return "running", [], None


def _run_json(command: list[str]) -> dict[str, Any]:
    completed = subprocess.run(command, check=True, capture_output=True, text=True)
    return json.loads(completed.stdout)


def _probe_video(path: Path) -> dict[str, Any]:
    payload = _run_json(
        [
            "ffprobe", "-v", "error", "-show_entries",
            "format=duration:stream=index,codec_type,width,height,avg_frame_rate",
            "-of", "json", str(path),
        ]
    )
    streams = payload.get("streams") or []
    video = next((s for s in streams if s.get("codec_type") == "video"), {})
    fps_text = str(video.get("avg_frame_rate") or "0/1")
    try:
        num, den = fps_text.split("/", 1)
        fps = float(num) / float(den) if float(den) else 0.0
    except (ValueError, ZeroDivisionError):
        fps = 0.0
    return {
        "duration_seconds": float((payload.get("format") or {}).get("duration") or 0),
        "fps": fps or 30.0,
        "width": int(video.get("width") or 0),
        "height": int(video.get("height") or 0),
        "has_audio": any(s.get("codec_type") == "audio" for s in streams),
    }


def _finalize_video(generated: Path, source: Path, output: Path, preserve: bool) -> dict[str, Any]:
    source_info = _probe_video(source)
    if not preserve:
        shutil.copy2(generated, output)
        return _probe_video(output)
    generated_info = _probe_video(generated)
    target = source_info["duration_seconds"]
    current = generated_info["duration_seconds"]
    if target <= 0 or current <= 0:
        raise RuntimeError("Cannot determine source or generated video duration")
    ratio = target / current
    vf = f"setpts={ratio:.12f}*PTS,fps={source_info['fps']:.8f},format=yuv420p"
    command = [
        "ffmpeg", "-y", "-i", str(generated), "-i", str(source),
        "-filter:v", vf, "-map", "0:v:0", "-map", "1:a:0?",
        "-t", f"{target:.9f}", "-c:v", "libx264", "-preset", "fast", "-crf", "18",
        "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", str(output),
    ]
    completed = subprocess.run(command, capture_output=True, text=True)
    if completed.returncode != 0:
        raise RuntimeError("FFmpeg finalization failed: " + completed.stderr[-2000:])
    final_info = _probe_video(output)
    final_info["source_duration_seconds"] = target
    final_info["duration_delta_seconds"] = final_info["duration_seconds"] - target
    return final_info


def _public_result(item: dict[str, Any]) -> dict[str, Any]:
    hidden = {"job_dir", "source_video_path", "background_image_path", "download_token"}
    result = {k: v for k, v in item.items() if k not in hidden}
    if item.get("status") == "completed" and item.get("download_token"):
        base = PUBLIC_BASE_URL or ""
        path = f"/files/{item['pipeline_id']}.mp4?token={item['download_token']}"
        result["final_video_url"] = base + path if base else path
    return result


async def _submit_vace(item: dict[str, Any], stage1_url: str) -> dict[str, Any]:
    job_dir = Path(item["job_dir"])
    stage1_path = job_dir / "stage1.mp4"
    await _download_result(stage1_url, stage1_path)
    stage1_upload = await _rh_upload(stage1_path, "video")
    background_upload = await _rh_upload(Path(item["background_image_path"]), "image")
    nodes = await _api_call_demo(VACE_WEBAPP_ID)
    resolved = _resolve_vace(nodes)
    node_info = [
        _override(resolved["source_video"], "VACE_SOURCE_VIDEO", stage1_upload),
        _override(resolved["background_image"], "VACE_BACKGROUND_IMAGE", background_upload),
        _override(resolved["prompt"], "VACE_PROMPT", item["stage2_prompt"]),
    ]
    if resolved.get("frame_count") and item.get("vace_frame_count"):
        node_info.append(_override(resolved["frame_count"], "VACE_FRAME_COUNT", item["vace_frame_count"]))
    task_id = await _rh_submit(VACE_WEBAPP_ID, node_info)
    item.update({"stage": 2, "status": "stage2_running", "stage2_task_id": task_id, "stage1_selected_url": stage1_url})
    await _save_pipeline(item)
    return item


async def _advance(item: dict[str, Any]) -> dict[str, Any]:
    if item["status"] == "stage1_running":
        payload = await _rh_outputs(item["stage1_task_id"])
        status, urls, error = _task_state(payload)
        if status == "failed":
            item.update({"status": "stage1_failed", "error": error})
            await _save_pipeline(item)
        elif status == "succeeded":
            videos = [u for u in urls if ".mp4" in u.lower()] or urls
            item["stage1_results"] = videos
            index = int(item.get("stage1_output_index", 0))
            if not videos or index >= len(videos):
                item.update({"status": "stage1_failed", "error": f"No stage-1 video at index {index}"})
                await _save_pipeline(item)
            else:
                item = await _submit_vace(item, videos[index])
    if item["status"] == "stage2_running":
        payload = await _rh_outputs(item["stage2_task_id"])
        status, urls, error = _task_state(payload)
        if status == "failed":
            item.update({"status": "stage2_failed", "error": error})
            await _save_pipeline(item)
        elif status == "succeeded":
            videos = [u for u in urls if ".mp4" in u.lower()] or urls
            if not videos:
                item.update({"status": "stage2_failed", "error": "VACE returned no video URL"})
                await _save_pipeline(item)
            else:
                job_dir = Path(item["job_dir"])
                raw_path = job_dir / "stage2_raw.mp4"
                final_path = job_dir / "final.mp4"
                await _download_result(videos[0], raw_path)
                final_info = await asyncio.to_thread(
                    _finalize_video,
                    raw_path,
                    Path(item["source_video_path"]),
                    final_path,
                    bool(item["preserve_original_audio_and_duration"]),
                )
                item.update({
                    "status": "completed", "stage": 3, "stage2_results": videos,
                    "final_video_info": final_info, "download_token": secrets.token_urlsafe(24),
                })
                await _save_pipeline(item)
    return item


@mcp.tool()
async def inspect_scail_vace_pipeline(force_refresh: bool = False) -> dict[str, Any]:
    """Read-only inspection of SCAIL-2 and Wan2.2 VACE nodes. Does not create tasks or consume RH."""
    scail_nodes, vace_nodes = await asyncio.gather(
        _api_call_demo(SCAIL_WEBAPP_ID, force_refresh),
        _api_call_demo(VACE_WEBAPP_ID, force_refresh),
    )
    return {
        "connected": True,
        "rh_charge": False,
        "stage1": {"webapp_id": SCAIL_WEBAPP_ID, "resolved": _resolve_scail(scail_nodes), "all_nodes": scail_nodes},
        "stage2": {"webapp_id": VACE_WEBAPP_ID, "resolved": _resolve_vace(vace_nodes), "all_nodes": vace_nodes},
    }


@mcp.tool()
async def submit_scail_vace_replacement_from_chatgpt_attachments(
    reference_person_and_background_image_file: str,
    source_video_file: str,
    confirm_rh_charge: bool,
    preserve_original_audio_and_duration: bool = True,
    stage1_prompt: str = "使用参考图片中的人物替换原视频人物，保持动作、姿态、镜头和节奏一致。",
    stage2_prompt: str = "将视频背景完整替换为参考图片中的场景，保持人物、动作、镜头构图和节奏不变，光影自然融合。",
    stage1_output_index: int = 0,
    vace_frame_count: int | None = None,
) -> dict[str, Any]:
    """Start SCAIL person replacement followed automatically by Wan2.2 VACE background replacement.

    The two file parameters accept a ChatGPT attachment passed as a readable server-local path or HTTP(S)
    temporary download URL. This operation consumes one SCAIL RH task and one VACE RH task, so
    confirm_rh_charge must be true.
    """
    if not confirm_rh_charge:
        raise ValueError("Set confirm_rh_charge=true only after the user explicitly confirms two RH tasks")
    if stage1_output_index < 0:
        raise ValueError("stage1_output_index must be 0 or greater")
    _require_config()
    pipeline_id = str(uuid.uuid4())
    job_dir = JOBS_DIR / pipeline_id
    job_dir.mkdir(parents=True, exist_ok=False)
    source_path = await _materialize_input(source_video_file, job_dir / "source.mp4")
    image_suffix = Path(urlparse(reference_person_and_background_image_file).path).suffix.lower()
    if image_suffix not in {".jpg", ".jpeg", ".png", ".webp"}:
        image_suffix = ".jpg"
    image_path = await _materialize_input(
        reference_person_and_background_image_file, job_dir / f"reference{image_suffix}"
    )
    source_info = await asyncio.to_thread(_probe_video, source_path)
    person_upload, video_upload = await asyncio.gather(
        _rh_upload(image_path, "image"), _rh_upload(source_path, "video")
    )
    nodes = await _api_call_demo(SCAIL_WEBAPP_ID)
    resolved = _resolve_scail(nodes)
    node_info = [
        _override(resolved["person_image"], "SCAIL_PERSON_IMAGE", person_upload),
        _override(resolved["source_video"], "SCAIL_SOURCE_VIDEO", video_upload),
        _override(resolved["prompt"], "SCAIL_PROMPT", stage1_prompt),
    ]
    task_id = await _rh_submit(SCAIL_WEBAPP_ID, node_info)
    item: dict[str, Any] = {
        "pipeline_id": pipeline_id,
        "status": "stage1_running",
        "stage": 1,
        "created_at": time.time(),
        "updated_at": time.time(),
        "stage1_task_id": task_id,
        "stage1_output_index": stage1_output_index,
        "preserve_original_audio_and_duration": preserve_original_audio_and_duration,
        "source_video_info": source_info,
        "stage1_prompt": stage1_prompt,
        "stage2_prompt": stage2_prompt,
        "vace_frame_count": vace_frame_count,
        "job_dir": str(job_dir),
        "source_video_path": str(source_path),
        "background_image_path": str(image_path),
    }
    await _save_pipeline(item)
    return _public_result(item)


@mcp.tool()
async def get_scail_vace_replacement(pipeline_id: str) -> dict[str, Any]:
    """Query and advance one pipeline. Completed responses include a tokenized final video URL."""
    _safe_pipeline_id(pipeline_id)
    item = await _get_pipeline(pipeline_id)
    if item["status"] in {"stage1_running", "stage2_running"}:
        item = await _advance(item)
    return _public_result(item)


@mcp.tool()
async def wait_scail_vace_replacement(
    pipeline_id: str,
    timeout_seconds: int = 300,
    poll_interval_seconds: int = 10,
) -> dict[str, Any]:
    """Wait for a pipeline for up to 900 seconds. A timeout does not cancel the RunningHub tasks."""
    _safe_pipeline_id(pipeline_id)
    timeout_seconds = max(1, min(timeout_seconds, 900))
    poll_interval_seconds = max(3, min(poll_interval_seconds, 60))
    deadline = time.monotonic() + timeout_seconds
    while True:
        item = await _get_pipeline(pipeline_id)
        if item["status"] in {"stage1_running", "stage2_running"}:
            item = await _advance(item)
        if item["status"] not in {"stage1_running", "stage2_running"}:
            return _public_result(item)
        if time.monotonic() >= deadline:
            result = _public_result(item)
            result["wait_timed_out"] = True
            return result
        await asyncio.sleep(poll_interval_seconds)


@mcp.tool()
async def retry_vace_background_only(
    pipeline_id: str,
    confirm_rh_charge: bool,
    stage1_output_index: int | None = None,
    stage2_prompt: str | None = None,
    vace_frame_count: int | None = None,
) -> dict[str, Any]:
    """Retry only Wan2.2 VACE, reusing a successful SCAIL video. Consumes one additional VACE RH task."""
    if not confirm_rh_charge:
        raise ValueError("Set confirm_rh_charge=true only after explicit confirmation of one VACE RH task")
    _safe_pipeline_id(pipeline_id)
    item = await _get_pipeline(pipeline_id)
    videos = item.get("stage1_results") or []
    if not videos:
        raise ValueError("This pipeline has no successful SCAIL result to reuse")
    index = int(item.get("stage1_output_index", 0) if stage1_output_index is None else stage1_output_index)
    if index < 0 or index >= len(videos):
        raise ValueError(f"stage1_output_index must be between 0 and {len(videos) - 1}")
    if stage2_prompt:
        item["stage2_prompt"] = stage2_prompt
    if vace_frame_count is not None:
        item["vace_frame_count"] = vace_frame_count
    item["stage1_output_index"] = index
    attempts = item.setdefault("stage2_attempts", [])
    if item.get("stage2_task_id"):
        attempts.append({"task_id": item["stage2_task_id"], "status": item.get("status"), "error": item.get("error")})
    item.pop("error", None)
    item = await _submit_vace(item, videos[index])
    return _public_result(item)


@mcp.tool()
async def list_scail_vace_replacements(limit: int = 20) -> list[dict[str, Any]]:
    """List recent local pipeline records without consuming RH."""
    limit = max(1, min(limit, 100))
    async with _state_lock:
        items = list(_read_state_unlocked().values())
    items.sort(key=lambda x: x.get("created_at", 0), reverse=True)
    return [_public_result(item) for item in items[:limit]]


@mcp.custom_route("/health", methods=["GET"])
async def health(_: Request) -> Response:
    return JSONResponse({"status": "ok", "service": "scail-vace-mcp"})


@mcp.custom_route("/files/{pipeline_id}.mp4", methods=["GET"])
async def download_final(request: Request) -> Response:
    pipeline_id = _safe_pipeline_id(request.path_params["pipeline_id"])
    try:
        item = await _get_pipeline(pipeline_id)
    except ValueError:
        return JSONResponse({"error": "not found"}, status_code=404)
    if request.query_params.get("token") != item.get("download_token"):
        return JSONResponse({"error": "forbidden"}, status_code=403)
    path = Path(item.get("job_dir", "")) / "final.mp4"
    if item.get("status") != "completed" or not path.is_file():
        return JSONResponse({"error": "not ready"}, status_code=404)
    return FileResponse(path, media_type="video/mp4", filename=f"{pipeline_id}.mp4")


app = mcp.streamable_http_app()
