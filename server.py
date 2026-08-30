import ipaddress
import os
import socket
from html import escape
from pathlib import PurePosixPath
from urllib.parse import urlparse

import httpx
from fastmcp import FastMCP
from pydantic import BaseModel, ConfigDict, Field
from starlette.requests import Request
from starlette.responses import HTMLResponse, PlainTextResponse

RUNNINGHUB_API_KEY = os.getenv("RUNNINGHUB_API_KEY", "").strip()
RUNNINGHUB_BASE_URL = os.getenv(
    "RUNNINGHUB_BASE_URL", "https://www.runninghub.cn"
).rstrip("/")
MCP_PATH_SECRET = os.getenv("MCP_PATH_SECRET", "").strip().strip("/")
PORT = int(os.getenv("PORT", "8080"))
MAX_REMOTE_FILE_BYTES = int(os.getenv("MAX_REMOTE_FILE_BYTES", str(200 * 1024 * 1024)))

if not RUNNINGHUB_API_KEY:
    raise RuntimeError("RUNNINGHUB_API_KEY is required")
if not MCP_PATH_SECRET:
    raise RuntimeError("MCP_PATH_SECRET is required")

AUTH_HEADERS = {"Authorization": f"Bearer {RUNNINGHUB_API_KEY}"}

mcp = FastMCP("RunningHub Character Replace")


class OpenAIFile(BaseModel):
    """File object supplied by ChatGPT for a declared OpenAI file parameter."""

    model_config = ConfigDict(extra="forbid")

    download_url: str = Field(description="Temporary URL that the MCP server can download")
    file_id: str = Field(description="ChatGPT file identifier")
    # These fields are optional at the object level but remain JSON Schema
    # strings when present, as required by ChatGPT's file-parameter contract.
    mime_type: str = Field(default=None, description="File MIME type")
    file_name: str = Field(default=None, description="Original file name")


def finalize_openai_file_param_schema(tool, *parameter_names: str) -> None:
    """Keep FastMCP's generated schema compliant with ChatGPT file params."""
    properties = tool.parameters.get("properties", {})
    for parameter_name in parameter_names:
        file_schema = properties.get(parameter_name)
        if not isinstance(file_schema, dict):
            raise RuntimeError(f"Missing file parameter schema: {parameter_name}")
        file_schema["additionalProperties"] = False
        for optional_name in ("mime_type", "file_name"):
            optional_schema = file_schema.get("properties", {}).get(optional_name, {})
            optional_schema.pop("default", None)


async def rh_post(path: str, *, json=None, files=None, data=None, timeout=120):
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
        resp = await client.post(
            f"{RUNNINGHUB_BASE_URL}{path}",
            headers=AUTH_HEADERS,
            json=json,
            files=files,
            data=data,
        )
        resp.raise_for_status()
        return resp.json()


def extract_runninghub_filename(result: dict) -> str:
    """Accept both filename and fileName returned by different RunningHub responses."""
    data = result.get("data") or {}
    filename = data.get("filename") or data.get("fileName")
    if not filename:
        raise RuntimeError(f"RunningHub upload failed: {result}")
    return str(filename)


async def upload_bytes_to_runninghub(
    content: bytes,
    filename_in: str,
    content_type: str = "application/octet-stream",
) -> str:
    if not content:
        raise ValueError(f"{filename_in} is empty")

    result = await rh_post(
        "/openapi/v2/media/upload/binary",
        files={"file": (filename_in, content, content_type)},
        timeout=300,
    )
    return extract_runninghub_filename(result)


async def upload_to_runninghub(upload) -> str:
    content = await upload.read()
    filename_in = getattr(upload, "filename", None) or "upload.bin"
    content_type = getattr(upload, "content_type", None) or "application/octet-stream"
    return await upload_bytes_to_runninghub(content, filename_in, content_type)


def _ensure_public_http_url(url: str) -> str:
    """Basic SSRF protection for the URL-based MCP tools."""
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("Only http:// or https:// URLs are supported")
    if not parsed.hostname:
        raise ValueError("URL must include a hostname")

    host = parsed.hostname.lower()
    if host in {"localhost", "localhost.localdomain"}:
        raise ValueError("Localhost URLs are not allowed")

    try:
        infos = socket.getaddrinfo(host, parsed.port or (443 if parsed.scheme == "https" else 80))
    except socket.gaierror as exc:
        raise ValueError(f"Cannot resolve URL hostname: {host}") from exc

    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_multicast
            or ip.is_reserved
            or ip.is_unspecified
        ):
            raise ValueError("Private or local network URLs are not allowed")

    return url


async def download_public_file(url: str, filename_hint: str | None = None) -> tuple[bytes, str, str]:
    """Download a publicly reachable file for forwarding to RunningHub."""
    url = _ensure_public_http_url(url)

    async with httpx.AsyncClient(timeout=300, follow_redirects=True) as client:
        async with client.stream("GET", url) as resp:
            resp.raise_for_status()

            content_length = resp.headers.get("content-length")
            if content_length and int(content_length) > MAX_REMOTE_FILE_BYTES:
                raise ValueError(
                    f"Remote file is too large; limit is {MAX_REMOTE_FILE_BYTES} bytes"
                )

            chunks = []
            total = 0
            async for chunk in resp.aiter_bytes():
                total += len(chunk)
                if total > MAX_REMOTE_FILE_BYTES:
                    raise ValueError(
                        f"Remote file is too large; limit is {MAX_REMOTE_FILE_BYTES} bytes"
                    )
                chunks.append(chunk)

            content = b"".join(chunks)
            if not content:
                raise ValueError("Downloaded file is empty")

            content_type = (
                resp.headers.get("content-type", "application/octet-stream")
                .split(";", 1)[0]
                .strip()
            ) or "application/octet-stream"

    if filename_hint:
        filename = PurePosixPath(filename_hint).name
    else:
        filename = PurePosixPath(urlparse(url).path).name or "remote-upload.bin"

    return content, filename, content_type


async def upload_public_url_to_runninghub(url: str, filename_hint: str | None = None) -> str:
    content, filename, content_type = await download_public_file(url, filename_hint)
    return await upload_bytes_to_runninghub(content, filename, content_type)


async def upload_chatgpt_file_to_runninghub(
    file: OpenAIFile,
    *,
    expected_media_kind: str | None = None,
) -> str:
    """Download a ChatGPT file parameter and forward it to RunningHub."""
    declared_type = (file.mime_type or "").strip().lower()
    if expected_media_kind and declared_type:
        if not declared_type.startswith(f"{expected_media_kind}/"):
            raise ValueError(
                f"Expected a {expected_media_kind} file, got {file.mime_type}"
            )

    safe_name = PurePosixPath(file.file_name or "").name
    if not safe_name:
        safe_name = f"{file.file_id}.bin"

    content, filename, downloaded_type = await download_public_file(
        file.download_url,
        safe_name,
    )
    content_type = declared_type or downloaded_type

    if expected_media_kind and content_type != "application/octet-stream":
        if not content_type.startswith(f"{expected_media_kind}/"):
            raise ValueError(
                f"Expected a {expected_media_kind} file, got {content_type}"
            )

    return await upload_bytes_to_runninghub(content, filename, content_type)


async def submit_character_replace(image_filename: str, video_filename: str) -> dict:
    payload = {
        "299##image": image_filename,
        "275##video": video_filename,
    }
    return await rh_post(
        "/openapi/v2/rhart-video/wan2.2/character-motion-transfer",
        json=payload,
        timeout=120,
    )


@mcp.tool
async def upload_media_from_url(url: str, filename_hint: str = "") -> dict:
    """Upload a publicly reachable image/video URL to RunningHub.

    Use this only when the media already has a URL that this MCP server can access.
    A ChatGPT-local attachment filename by itself is not a usable URL.
    """
    filename = await upload_public_url_to_runninghub(url, filename_hint or None)
    return {"ok": True, "filename": filename}


@mcp.tool(
    meta={"openai/fileParams": ["file"]},
    annotations={
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    },
)
async def upload_media_from_chatgpt(file: OpenAIFile) -> dict:
    """Upload an image or video attached in ChatGPT directly to RunningHub."""
    filename = await upload_chatgpt_file_to_runninghub(file)
    return {
        "ok": True,
        "source_file_id": file.file_id,
        "filename": filename,
    }


finalize_openai_file_param_schema(upload_media_from_chatgpt, "file")


@mcp.tool
async def replace_video_character(image_filename: str, video_filename: str) -> dict:
    """Replace the person in a source video with the person from a reference image.

    image_filename and video_filename must be RunningHub filenames returned by
    either the private upload page or upload_media_from_url.
    """
    return await submit_character_replace(image_filename, video_filename)


@mcp.tool
async def replace_video_character_from_urls(image_url: str, video_url: str) -> dict:
    """Upload a public reference-image URL and public video URL, then start replacement.

    This avoids manually copying RunningHub filenames when both media files are
    already available through URLs reachable by this MCP server.
    """
    image_filename = await upload_public_url_to_runninghub(image_url)
    video_filename = await upload_public_url_to_runninghub(video_url)
    result = await submit_character_replace(image_filename, video_filename)
    return {
        "uploaded": {
            "image_filename": image_filename,
            "video_filename": video_filename,
        },
        "runninghub": result,
    }


@mcp.tool(
    meta={"openai/fileParams": ["image_file", "video_file"]},
    annotations={
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    },
)
async def replace_video_character_from_chatgpt_attachments(
    image_file: OpenAIFile,
    video_file: OpenAIFile,
) -> dict:
    """Use a ChatGPT-attached reference image and source video for replacement.

    ChatGPT supplies temporary download URLs for both declared file parameters.
    This tool downloads them, uploads them to RunningHub, and immediately starts
    the character-motion-transfer workflow.
    """
    image_filename = await upload_chatgpt_file_to_runninghub(
        image_file,
        expected_media_kind="image",
    )
    video_filename = await upload_chatgpt_file_to_runninghub(
        video_file,
        expected_media_kind="video",
    )
    result = await submit_character_replace(image_filename, video_filename)
    return {
        "uploaded": {
            "image_file_id": image_file.file_id,
            "video_file_id": video_file.file_id,
            "image_filename": image_filename,
            "video_filename": video_filename,
        },
        "runninghub": result,
    }


finalize_openai_file_param_schema(
    replace_video_character_from_chatgpt_attachments,
    "image_file",
    "video_file",
)


@mcp.tool
async def query_runninghub_task(task_id: str) -> dict:
    """Query a RunningHub generation task. When it succeeds, return result URLs."""
    return await rh_post(
        "/openapi/v2/query",
        json={"taskId": str(task_id)},
        timeout=120,
    )


@mcp.tool
async def runninghub_connection_check() -> dict:
    """Check this MCP bridge without exposing the RunningHub API key."""
    return {
        "ok": True,
        "service": "RunningHub Character Replace MCP",
        "base_url": RUNNINGHUB_BASE_URL,
        "tools": [
            "upload_media_from_url",
            "upload_media_from_chatgpt",
            "replace_video_character",
            "replace_video_character_from_urls",
            "replace_video_character_from_chatgpt_attachments",
            "query_runninghub_task",
            "runninghub_connection_check",
        ],
    }


@mcp.custom_route("/", methods=["GET"])
async def home(request: Request):
    return HTMLResponse(
        "<h2>RunningHub MCP Bridge is running</h2>"
        "<p>The MCP endpoint is private. Do not share its URL.</p>"
    )


@mcp.custom_route(f"/upload/{MCP_PATH_SECRET}", methods=["GET"])
async def upload_page(request: Request):
    return HTMLResponse(
        """
<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width,initial-scale=1" />
  <title>RunningHub 上传</title>
  <style>
    body { font-family: system-ui, sans-serif; max-width: 720px; margin: 40px auto; padding: 0 18px; }
    label { display:block; margin:18px 0 6px; font-weight:600; }
    button { margin-top:22px; padding:10px 18px; }
    .note { color:#555; line-height:1.6; }
  </style>
</head>
<body>
  <h2>上传人物参考图 + 素材视频</h2>
  <p class="note">文件会直接转发到 RunningHub。上传成功后，可把两个 filename 复制到 ChatGPT 调用换人工具。</p>
  <form method="post" enctype="multipart/form-data">
    <label>人物参考图</label>
    <input name="image" type="file" accept="image/*" required />
    <label>素材视频</label>
    <input name="video" type="file" accept="video/*" required />
    <br/><button type="submit">上传到 RunningHub</button>
  </form>
</body>
</html>
        """
    )


@mcp.custom_route(f"/upload/{MCP_PATH_SECRET}", methods=["POST"])
async def upload_pair(request: Request):
    try:
        form = await request.form()
        image = form.get("image")
        video = form.get("video")

        if image is None or video is None:
            return HTMLResponse("Missing image or video", status_code=400)

        image_filename = await upload_to_runninghub(image)
        video_filename = await upload_to_runninghub(video)
    except httpx.HTTPStatusError as e:
        detail = e.response.text[:2000]
        return HTMLResponse(
            f"<h3>RunningHub HTTP error</h3><pre>{escape(detail)}</pre>",
            status_code=502,
        )
    except Exception as e:
        return HTMLResponse(
            f"<h3>Upload failed</h3><pre>{escape(str(e))}</pre>",
            status_code=500,
        )

    return HTMLResponse(
        f"""
<!doctype html>
<html lang="zh-CN">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="font-family:system-ui,sans-serif;max-width:760px;margin:40px auto;padding:0 18px">
  <h2>上传成功</h2>
  <p>把下面两项完整复制回 ChatGPT：</p>
  <p><b>image_filename</b><br><code>{escape(image_filename)}</code></p>
  <p><b>video_filename</b><br><code>{escape(video_filename)}</code></p>
</body>
</html>
        """
    )


@mcp.custom_route("/health", methods=["GET"])
async def health(request: Request):
    return PlainTextResponse("ok")


if __name__ == "__main__":
    mcp.run(
        transport="http",
        host="0.0.0.0",
        port=PORT,
        path=f"/mcp/{MCP_PATH_SECRET}/",
    )
