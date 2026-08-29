from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import mimetypes
import os
import re
import socket
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urljoin, urlparse

import httpx
from mcp.server import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse, Response


logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("scail2-runninghub-mcp")


@dataclass(frozen=True)
class Settings:
    runninghub_api_key: str
    runninghub_base_url: str
    scail_webapp_id: str
    public_domain: str
    max_download_mb: int
    schema_cache_seconds: int
    node_overrides: dict[str, dict[str, str]]

    @classmethod
    def from_env(cls) -> "Settings":
        raw_overrides = os.getenv("SCAIL_NODE_OVERRIDES_JSON", "{}").strip() or "{}"
        try:
            overrides = json.loads(raw_overrides)
        except json.JSONDecodeError as exc:
            raise RuntimeError("SCAIL_NODE_OVERRIDES_JSON 不是有效 JSON") from exc
        if not isinstance(overrides, dict):
            raise RuntimeError("SCAIL_NODE_OVERRIDES_JSON 必须是 JSON 对象")

        public_domain = (
            os.getenv("PUBLIC_DOMAIN", "").strip()
            or os.getenv("RAILWAY_PUBLIC_DOMAIN", "").strip()
        )
        public_domain = public_domain.removeprefix("https://").removeprefix("http://").rstrip("/")

        return cls(
            runninghub_api_key=os.getenv("RUNNINGHUB_API_KEY", "").strip(),
            runninghub_base_url=os.getenv(
                "RUNNINGHUB_BASE_URL", "https://www.runninghub.cn"
            ).rstrip("/"),
            scail_webapp_id=os.getenv(
                "SCAIL_WEBAPP_ID", "2064610888811900929"
            ).strip(),
            public_domain=public_domain,
            max_download_mb=max(1, int(os.getenv("MAX_DOWNLOAD_MB", "500"))),
            schema_cache_seconds=max(30, int(os.getenv("SCHEMA_CACHE_SECONDS", "600"))),
            node_overrides=overrides,
        )


SETTINGS = Settings.from_env()


class RunningHubError(RuntimeError):
    pass


def _headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {SETTINGS.runninghub_api_key}",
        "User-Agent": "scail2-runninghub-mcp/1.0",
    }


def _require_configured() -> None:
    if not SETTINGS.runninghub_api_key:
        raise RunningHubError("Railway 尚未配置 RUNNINGHUB_API_KEY")
    if not SETTINGS.scail_webapp_id:
        raise RunningHubError("Railway 尚未配置 SCAIL_WEBAPP_ID")


def _rh_message(payload: Any) -> str:
    if isinstance(payload, dict):
        for key in ("msg", "message", "error", "errorMessage"):
            value = payload.get(key)
            if value:
                return str(value)
        data = payload.get("data")
        if isinstance(data, dict):
            return _rh_message(data)
    return "RunningHub 请求失败"


async def _request_json(
    method: str,
    path: str,
    *,
    params: dict[str, Any] | None = None,
    json_body: dict[str, Any] | None = None,
    files: dict[str, Any] | None = None,
    timeout: float = 90.0,
    attempts: int = 3,
) -> dict[str, Any]:
    _require_configured()
    url = f"{SETTINGS.runninghub_base_url}{path}"
    last_error: Exception | None = None

    for attempt in range(1, attempts + 1):
        try:
            if files:
                for value in files.values():
                    if isinstance(value, tuple) and len(value) >= 2 and hasattr(value[1], "seek"):
                        value[1].seek(0)
            async with httpx.AsyncClient(
                follow_redirects=True,
                timeout=httpx.Timeout(timeout, connect=20.0),
            ) as client:
                response = await client.request(
                    method,
                    url,
                    headers=_headers(),
                    params=params,
                    json=json_body,
                    files=files,
                )
            if response.status_code in {429, 500, 502, 503, 504} and attempt < attempts:
                await asyncio.sleep(2 ** (attempt - 1))
                continue
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, dict):
                raise RunningHubError("RunningHub 返回了非 JSON 对象")
            code = payload.get("code")
            if code not in (None, 0, "0", 200, "200"):
                raise RunningHubError(f"RunningHub 错误 {code}: {_rh_message(payload)}")
            return payload
        except RunningHubError:
            raise
        except (httpx.HTTPError, ValueError) as exc:
            last_error = exc
            if attempt < attempts:
                await asyncio.sleep(2 ** (attempt - 1))
                continue
            break
    raise RunningHubError(f"RunningHub 网络请求失败: {last_error}")


_schema_cache: tuple[float, dict[str, Any]] | None = None
_schema_lock = asyncio.Lock()


async def fetch_app_schema(force: bool = False) -> dict[str, Any]:
    global _schema_cache
    now = time.monotonic()
    if not force and _schema_cache and now - _schema_cache[0] < SETTINGS.schema_cache_seconds:
        return _schema_cache[1]

    async with _schema_lock:
        now = time.monotonic()
        if not force and _schema_cache and now - _schema_cache[0] < SETTINGS.schema_cache_seconds:
            return _schema_cache[1]
        payload = await _request_json(
            "GET",
            "/api/webapp/apiCallDemo",
            params={
                "apiKey": SETTINGS.runninghub_api_key,
                "webappId": SETTINGS.scail_webapp_id,
            },
        )
        data = payload.get("data")
        if not isinstance(data, dict) or not isinstance(data.get("nodeInfoList"), list):
            raise RunningHubError(
                "无法读取 SCAIL-2 应用输入。请确认该应用已复制/可 API 调用，并检查 SCAIL_WEBAPP_ID。"
            )
        _schema_cache = (now, data)
        return data


def _node_text(node: dict[str, Any]) -> str:
    fields = (
        node.get("nodeName"),
        node.get("fieldName"),
        node.get("fieldType"),
        node.get("description"),
        node.get("descriptionEn"),
    )
    return " ".join(str(value) for value in fields if value).lower()


def _score_node(
    node: dict[str, Any],
    *,
    required_any: tuple[str, ...] = (),
    preferred: tuple[str, ...] = (),
    rejected: tuple[str, ...] = (),
) -> int:
    text = _node_text(node)
    if required_any and not any(word in text for word in required_any):
        return -10_000
    score = sum(8 for word in preferred if word in text)
    score -= sum(20 for word in rejected if word in text)
    return score


def _override_node(nodes: list[dict[str, Any]], role: str) -> dict[str, Any] | None:
    override = SETTINGS.node_overrides.get(role)
    if not isinstance(override, dict):
        return None
    node_id = str(override.get("nodeId", ""))
    field_name = str(override.get("fieldName", ""))
    for node in nodes:
        if str(node.get("nodeId", "")) == node_id and str(node.get("fieldName", "")) == field_name:
            return node
    raise RunningHubError(f"{role} 的节点覆盖在当前应用中不存在: {node_id}/{field_name}")


def select_node(nodes: list[dict[str, Any]], role: str) -> dict[str, Any] | None:
    overridden = _override_node(nodes, role)
    if overridden:
        return overridden

    rules: dict[str, dict[str, tuple[str, ...]]] = {
        "reference_image": {
            "required_any": ("image", "图片", "图像"),
            "preferred": ("reference", "参考", "人物", "角色", "主图", "loadimage"),
            "rejected": ("background", "背景", "mask", "遮罩", "video", "视频"),
        },
        "source_video": {
            "required_any": ("video", "视频"),
            "preferred": ("reference", "source", "drive", "驱动", "参考", "原视频", "loadvideo"),
            "rejected": ("output", "输出", "image", "图片"),
        },
        "mode": {
            "required_any": ("mode", "模式", "replace", "替换", "animation", "动作迁移"),
            "preferred": ("mode", "模式", "replace", "替换"),
            "rejected": (),
        },
        "prompt": {
            "required_any": ("prompt", "提示词", "提示"),
            "preferred": ("prompt", "提示词"),
            "rejected": ("negative", "负面"),
        },
        "target_subject": {
            "required_any": ("subject", "主体", "人物", "对象", "描述"),
            "preferred": ("video", "视频", "target", "目标", "编辑"),
            "rejected": ("image", "图片", "参考图", "background", "背景"),
        },
        "reference_subject": {
            "required_any": ("subject", "主体", "人物", "对象", "描述"),
            "preferred": ("image", "图片", "参考图", "reference"),
            "rejected": ("video", "视频", "background", "背景"),
        },
    }
    rule = rules[role]

    def eligible(node: dict[str, Any]) -> bool:
        field_type = str(node.get("fieldType", "")).upper()
        node_name = str(node.get("nodeName", "")).lower()
        field_name = str(node.get("fieldName", "")).lower()
        if role == "reference_image":
            return field_type == "IMAGE" or "loadimage" in node_name or field_name == "image"
        if role == "source_video":
            return field_type == "VIDEO" or "loadvideo" in node_name or field_name == "video"
        if role == "mode":
            return field_type in {"LIST", "BOOLEAN", "BOOL", "STRING"} or "mode" in field_name
        return field_type in {"STRING", "TEXT", "MULTILINE", "PROMPT"} or field_name in {
            "prompt",
            "text",
            "description",
            "subject",
        }

    eligible_nodes = [node for node in nodes if eligible(node)]
    ranked = sorted(
        (
            (_score_node(node, **rule), index, node)
            for index, node in enumerate(eligible_nodes)
        ),
        key=lambda item: (item[0], -item[1]),
        reverse=True,
    )
    if not ranked or ranked[0][0] <= -10_000:
        return None
    return ranked[0][2]


def _field_data_options(node: dict[str, Any]) -> list[dict[str, Any]]:
    raw = node.get("fieldData")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            return []
    return [item for item in raw if isinstance(item, dict)] if isinstance(raw, list) else []


def replacement_mode_value(node: dict[str, Any]) -> Any:
    options = _field_data_options(node)
    for item in options:
        text = " ".join(str(item.get(key, "")) for key in ("name", "index", "description")).lower()
        if "replacement" in text or "replace" in text or "角色替换" in text or "人物替换" in text:
            return item.get("index", item.get("name"))

    current = node.get("fieldValue")
    field_type = str(node.get("fieldType", "")).upper()
    text = _node_text(node)
    if field_type == "BOOLEAN" or isinstance(current, bool) or "true=" in text:
        return "true" if isinstance(current, str) else True
    return current


def _node_key(node: dict[str, Any]) -> tuple[str, str]:
    return str(node.get("nodeId", "")), str(node.get("fieldName", ""))


def build_node_info_list(
    schema: dict[str, Any],
    *,
    image_file_name: str,
    video_file_name: str,
    prompt: str,
    target_subject: str,
    reference_subject: str,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    source_nodes = schema.get("nodeInfoList", [])
    nodes = [dict(item) for item in source_nodes if isinstance(item, dict)]
    selected = {
        role: select_node(nodes, role)
        for role in (
            "reference_image",
            "source_video",
            "mode",
            "prompt",
            "target_subject",
            "reference_subject",
        )
    }
    if not selected["reference_image"] or not selected["source_video"]:
        raise RunningHubError(
            "未能自动识别参考图片或驱动视频节点。请先调用 inspect_scail_app，"
            "再通过 SCAIL_NODE_OVERRIDES_JSON 指定节点。"
        )

    replacements: dict[tuple[str, str], Any] = {
        _node_key(selected["reference_image"]): image_file_name,
        _node_key(selected["source_video"]): video_file_name,
    }
    if selected["mode"]:
        replacements[_node_key(selected["mode"])] = replacement_mode_value(selected["mode"])
    if prompt and selected["prompt"]:
        replacements[_node_key(selected["prompt"])] = prompt
    if target_subject and selected["target_subject"]:
        replacements[_node_key(selected["target_subject"])] = target_subject
    if reference_subject and selected["reference_subject"]:
        replacements[_node_key(selected["reference_subject"])] = reference_subject

    result: list[dict[str, Any]] = []
    allowed_keys = {"nodeId", "fieldName", "fieldValue", "description", "fieldData"}
    for node in nodes:
        item = {key: value for key, value in node.items() if key in allowed_keys and value is not None}
        key = _node_key(node)
        if key in replacements:
            item["fieldValue"] = replacements[key]
        if item.get("nodeId") and item.get("fieldName"):
            result.append(item)

    selected_summary = {
        role: (
            {
                "nodeId": node.get("nodeId"),
                "fieldName": node.get("fieldName"),
                "description": node.get("description"),
            }
            if node
            else None
        )
        for role, node in selected.items()
    }
    return result, selected_summary


def _is_public_ip(address: str) -> bool:
    ip = ipaddress.ip_address(address)
    return not (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


async def _validate_remote_url(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise RunningHubError("媒体地址必须是有效的 http/https URL")
    if parsed.username or parsed.password:
        raise RunningHubError("媒体地址不能包含用户名或密码")
    try:
        results = await asyncio.to_thread(
            socket.getaddrinfo, parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80)
        )
    except socket.gaierror as exc:
        raise RunningHubError(f"无法解析媒体地址域名: {parsed.hostname}") from exc
    addresses = {item[4][0] for item in results}
    if not addresses or any(not _is_public_ip(address) for address in addresses):
        raise RunningHubError("出于安全原因，不允许下载内网、回环或保留地址")


def _safe_filename(url: str, content_type: str, expected_type: str) -> str:
    name = Path(unquote(urlparse(url).path)).name
    name = re.sub(r"[^A-Za-z0-9._-]", "_", name)[:120]
    suffix = Path(name).suffix.lower()
    allowed = {
        "image": {".jpg", ".jpeg", ".png", ".webp"},
        "video": {".mp4", ".avi", ".mov", ".mkv"},
    }[expected_type]
    if suffix not in allowed:
        guessed = mimetypes.guess_extension(content_type.split(";", 1)[0].strip()) or ""
        suffix = guessed if guessed in allowed else (".jpg" if expected_type == "image" else ".mp4")
        name = f"input{suffix}"
    return name


async def download_to_temp(url: str, expected_type: str) -> tuple[str, str, str]:
    limit = SETTINGS.max_download_mb * 1024 * 1024
    temp = tempfile.NamedTemporaryFile(prefix=f"scail-{expected_type}-", delete=False)
    temp_path = temp.name
    temp.close()
    total = 0
    try:
        async with httpx.AsyncClient(
            follow_redirects=False,
            timeout=httpx.Timeout(180.0, connect=20.0),
        ) as client:
            current_url = url
            for _ in range(6):
                await _validate_remote_url(current_url)
                async with client.stream(
                    "GET", current_url, headers={"User-Agent": "scail2-runninghub-mcp/1.0"}
                ) as response:
                    if response.status_code in {301, 302, 303, 307, 308}:
                        location = response.headers.get("location")
                        if not location:
                            raise RunningHubError("媒体地址重定向响应缺少 Location")
                        current_url = urljoin(current_url, location)
                        continue
                    response.raise_for_status()
                    content_type = response.headers.get("content-type", "application/octet-stream")
                    media_type = content_type.split(";", 1)[0].lower()
                    if expected_type == "image" and not (
                        media_type.startswith("image/") or media_type == "application/octet-stream"
                    ):
                        raise RunningHubError(f"参考图 URL 返回的类型不是图片: {content_type}")
                    if expected_type == "video" and not (
                        media_type.startswith("video/") or media_type == "application/octet-stream"
                    ):
                        raise RunningHubError(f"驱动视频 URL 返回的类型不是视频: {content_type}")
                    with open(temp_path, "wb") as handle:
                        async for chunk in response.aiter_bytes(1024 * 1024):
                            total += len(chunk)
                            if total > limit:
                                raise RunningHubError(
                                    f"媒体文件超过 MAX_DOWNLOAD_MB={SETTINGS.max_download_mb} MB"
                                )
                            handle.write(chunk)
                    filename = _safe_filename(current_url, content_type, expected_type)
                    break
            else:
                raise RunningHubError("媒体地址重定向次数过多")
        if total == 0:
            raise RunningHubError("媒体 URL 返回空文件")
        return temp_path, filename, content_type
    except Exception:
        Path(temp_path).unlink(missing_ok=True)
        raise


async def upload_remote_media(url: str, expected_type: str) -> str:
    path, filename, content_type = await download_to_temp(url, expected_type)
    try:
        with open(path, "rb") as handle:
            payload = await _request_json(
                "POST",
                "/openapi/v2/media/upload/binary",
                files={"file": (filename, handle, content_type)},
                timeout=300.0,
            )
        data = payload.get("data")
        file_name = data.get("fileName") if isinstance(data, dict) else None
        if not file_name:
            raise RunningHubError("RunningHub 上传成功响应中缺少 fileName")
        return str(file_name)
    finally:
        Path(path).unlink(missing_ok=True)


def _extract_task_id(payload: dict[str, Any]) -> str:
    data = payload.get("data")
    candidates: list[Any] = [payload.get("taskId"), payload.get("task_id")]
    if isinstance(data, dict):
        candidates.extend((data.get("taskId"), data.get("task_id"), data.get("id")))
    elif isinstance(data, (str, int)):
        candidates.append(data)
    for value in candidates:
        if value not in (None, ""):
            return str(value)
    raise RunningHubError(f"提交响应中缺少 taskId: {_rh_message(payload)}")


async def submit_task(node_info_list: list[dict[str, Any]]) -> str:
    payload = await _request_json(
        "POST",
        "/task/openapi/ai-app/run",
        json_body={
            "webappId": SETTINGS.scail_webapp_id,
            "apiKey": SETTINGS.runninghub_api_key,
            "nodeInfoList": node_info_list,
        },
        timeout=120.0,
    )
    return _extract_task_id(payload)


def _normalize_status(payload: dict[str, Any]) -> str:
    data = payload.get("data")
    candidates: list[Any] = [payload.get("status")]
    if isinstance(data, dict):
        candidates.extend((data.get("status"), data.get("taskStatus"), data.get("state")))
    else:
        candidates.append(data)
    for value in candidates:
        if isinstance(value, str) and value:
            upper = value.upper()
            for known in ("SUCCESS", "FAILED", "CANCEL", "RUNNING", "QUEUED", "CREATE"):
                if known in upper:
                    return known
    return "UNKNOWN"


async def get_task(task_id: str) -> dict[str, Any]:
    body = {"apiKey": SETTINGS.runninghub_api_key, "taskId": task_id}
    status_payload = await _request_json(
        "POST", "/task/openapi/status", json_body=body, attempts=2
    )
    status = _normalize_status(status_payload)
    result: dict[str, Any] = {
        "task_id": task_id,
        "status": status,
        "status_response": status_payload,
        "outputs": [],
    }
    if status in {"SUCCESS", "FAILED", "CANCEL", "UNKNOWN"}:
        try:
            output_payload = await _request_json(
                "POST", "/task/openapi/outputs", json_body=body, attempts=2
            )
            result["output_response"] = output_payload
            data = output_payload.get("data")
            if isinstance(data, list):
                result["outputs"] = [
                    {
                        "url": item.get("fileUrl") or item.get("url") or item.get("outputUrl"),
                        "type": item.get("fileType") or item.get("outputType"),
                        "node_id": item.get("nodeId"),
                        "consume_rh": item.get("consumeCoins"),
                        "third_party_cost": item.get("thirdPartyConsumeMoney"),
                    }
                    for item in data
                    if isinstance(item, dict)
                ]
        except RunningHubError as exc:
            result["output_query_error"] = str(exc)
    return result


def _sanitize_schema(data: dict[str, Any]) -> dict[str, Any]:
    nodes = data.get("nodeInfoList", [])
    clean_nodes = []
    for node in nodes:
        if not isinstance(node, dict):
            continue
        clean_nodes.append(
            {
                key: node.get(key)
                for key in (
                    "nodeId",
                    "nodeName",
                    "fieldName",
                    "fieldType",
                    "fieldValue",
                    "description",
                    "descriptionEn",
                )
                if node.get(key) is not None
            }
        )
    return {
        "webapp_id": SETTINGS.scail_webapp_id,
        "webapp_name": data.get("webappName"),
        "inputs": clean_nodes,
    }


mcp = MCPServer(
    "SCAIL-2 RunningHub",
    instructions=(
        "使用 RunningHub 的 SCAIL-2 应用完成参考图片人物替换视频人物。"
        "提交任务会消耗 RH 币，只有用户明确同意且 confirm_rh_charge=true 时才提交。"
        "任务通常运行较久，提交后用 get_scail_task 查询。"
    ),
)


@mcp.tool()
async def inspect_scail_app(force_refresh: bool = False) -> dict[str, Any]:
    """只读检查 SCAIL-2 应用、API Key 连通性和当前输入节点；不会创建任务或消耗生成 RH。"""
    schema = await fetch_app_schema(force=force_refresh)
    return _sanitize_schema(schema)


@mcp.tool()
async def submit_scail_replacement(
    reference_image_url: str,
    source_video_url: str,
    confirm_rh_charge: bool,
    prompt: str = "保持原视频背景、镜头、动作和节奏，只替换目标人物；保持参考人物身份、脸部、发型、服装和身体比例稳定。",
    target_subject: str = "视频中的主要人物",
    reference_subject: str = "参考图片中的人物",
) -> dict[str, Any]:
    """上传一张人物参考图和一个驱动视频，创建完整人物替换任务。该操作会消耗 RunningHub RH 币。"""
    if not confirm_rh_charge:
        return {
            "submitted": False,
            "message": "未提交：confirm_rh_charge=false。请先取得用户对本次 RH 扣费的明确同意。",
        }

    schema = await fetch_app_schema()
    image_file, video_file = await asyncio.gather(
        upload_remote_media(reference_image_url, "image"),
        upload_remote_media(source_video_url, "video"),
    )
    node_info_list, selected_nodes = build_node_info_list(
        schema,
        image_file_name=image_file,
        video_file_name=video_file,
        prompt=prompt,
        target_subject=target_subject,
        reference_subject=reference_subject,
    )
    task_id = await submit_task(node_info_list)
    logger.info("SCAIL task submitted task_id=%s webapp_id=%s", task_id, SETTINGS.scail_webapp_id)
    return {
        "submitted": True,
        "task_id": task_id,
        "status": "SUBMITTED",
        "webapp_id": SETTINGS.scail_webapp_id,
        "selected_nodes": selected_nodes,
        "next_action": "稍后调用 get_scail_task(task_id) 查询结果。",
    }


@mcp.tool()
async def get_scail_task(task_id: str) -> dict[str, Any]:
    """查询已提交 SCAIL-2 任务的状态、结果视频 URL 和 RH 消耗；不会创建新任务。"""
    return await get_task(task_id)


@mcp.tool()
async def wait_scail_task(
    task_id: str,
    timeout_seconds: int = 600,
    poll_interval_seconds: int = 15,
) -> dict[str, Any]:
    """等待 SCAIL-2 任务完成；最多等待 900 秒，超时仍返回 task_id 供后续继续查询。"""
    timeout_seconds = min(max(timeout_seconds, 15), 900)
    poll_interval_seconds = min(max(poll_interval_seconds, 5), 60)
    deadline = time.monotonic() + timeout_seconds
    last: dict[str, Any] = {"task_id": task_id, "status": "UNKNOWN"}
    while time.monotonic() < deadline:
        last = await get_task(task_id)
        if last.get("status") in {"SUCCESS", "FAILED", "CANCEL"}:
            return last
        await asyncio.sleep(poll_interval_seconds)
    return {
        **last,
        "timed_out": True,
        "message": "等待超时，任务没有被取消；请稍后继续调用 get_scail_task。",
    }


@mcp.custom_route("/", methods=["GET"])
async def root(_: Request) -> Response:
    return JSONResponse(
        {
            "service": "SCAIL-2 RunningHub MCP",
            "configured": bool(SETTINGS.runninghub_api_key and SETTINGS.scail_webapp_id),
            "webapp_id": SETTINGS.scail_webapp_id,
            "mcp_url": "/mcp",
            "health_url": "/health",
        }
    )


@mcp.custom_route("/health", methods=["GET"])
async def health(_: Request) -> Response:
    return JSONResponse(
        {
            "status": "ok",
            "configured": bool(SETTINGS.runninghub_api_key and SETTINGS.scail_webapp_id),
        },
        status_code=200,
    )


@mcp.custom_route("/docs", methods=["GET"])
async def docs_redirect(_: Request) -> Response:
    return RedirectResponse(
        "https://www.runninghub.cn/ai-detail/2064610888811900929", status_code=307
    )


def _transport_security() -> TransportSecuritySettings:
    if SETTINGS.public_domain:
        return TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=[
                SETTINGS.public_domain,
                f"{SETTINGS.public_domain}:*",
                "localhost:*",
                "127.0.0.1:*",
            ],
            allowed_origins=[
                f"https://{SETTINGS.public_domain}",
                f"https://{SETTINGS.public_domain}:*",
                "http://localhost:*",
                "http://127.0.0.1:*",
            ],
        )
    # 本地运行时保持严格 localhost；Railway 会自动提供 RAILWAY_PUBLIC_DOMAIN。
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=["localhost:*", "127.0.0.1:*"],
        allowed_origins=["http://localhost:*", "http://127.0.0.1:*"],
    )


app = mcp.streamable_http_app(
    transport_security=_transport_security(),
    stateless_http=True,
    json_response=True,
)
