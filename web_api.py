#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
网页下载 API 蓝图：Bearer Token 鉴权 + 解析预览 / 提交下载 / 查询取消任务。
"""

from __future__ import annotations

import logging
import os
from functools import wraps
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

import requests
from flask import Blueprint, Response, jsonify, request, send_from_directory, stream_with_context

logger = logging.getLogger(__name__)

# 媒体代理允许的 CDN / 站点主机后缀，防止 SSRF
_MEDIA_PROXY_ALLOWED_SUFFIXES = (
    "douyin.com",
    "douyinpic.com",
    "douyincdn.com",
    "douyinvod.com",
    "douyinstatic.com",
    "snssdk.com",
    "byteimg.com",
    "bytevcloud.net",
    "bytecdn.com",
    "ibytedtos.com",
    "xhscdn.com",
    "xiaohongshu.com",
    "kuaishou.com",
    "yximgs.com",
    "kwimgs.com",
    "tiktokcdn.com",
    "tiktok.com",
    "twimg.com",
    "twitter.com",
    "x.com",
    "fbcdn.net",
    "cdninstagram.com",
    "bilivideo.com",
    "hdslb.com",
    "youtube.com",
    "googlevideo.com",
    "ytimg.com",
)


def _host_allowed(hostname: str) -> bool:
    host = (hostname or "").lower().rstrip(".")
    if not host or host in ("localhost", "127.0.0.1", "0.0.0.0", "::1"):
        return False
    # 拒绝内网 IP
    if host.startswith("10.") or host.startswith("192.168.") or host.startswith("169.254."):
        return False
    if host.startswith("172."):
        try:
            second = int(host.split(".")[1])
            if 16 <= second <= 31:
                return False
        except (IndexError, ValueError):
            pass
    return any(host == suffix or host.endswith("." + suffix) for suffix in _MEDIA_PROXY_ALLOWED_SUFFIXES)


def create_web_download_blueprint(
    task_manager: Any,
    auth_token: str,
    static_dir: Optional[str] = None,
) -> Blueprint:
    bp = Blueprint("web_download", __name__)
    static_path = Path(static_dir or os.path.join(os.path.dirname(__file__), "web"))

    def _extract_bearer_token() -> str:
        auth_header = request.headers.get("Authorization", "")
        if auth_header.lower().startswith("bearer "):
            return auth_header[7:].strip()
        # 兼容 query / json / form
        token = request.args.get("token") or ""
        if token:
            return token.strip()
        if request.is_json:
            data = request.get_json(silent=True) or {}
            token = data.get("token") or data.get("password") or ""
            if token:
                return str(token).strip()
        token = request.form.get("token") or request.form.get("password") or ""
        return str(token).strip()

    def require_auth(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            if not auth_token:
                return jsonify({"ok": False, "error": "服务未配置 web_auth_token"}), 503
            provided = _extract_bearer_token()
            if not provided or provided != auth_token:
                return jsonify({"ok": False, "error": "未授权"}), 401
            return fn(*args, **kwargs)

        return wrapper

    @bp.get("/")
    def index_page():
        index_file = static_path / "index.html"
        if index_file.exists():
            return send_from_directory(str(static_path), "index.html")
        return (
            jsonify({"ok": False, "error": "web/index.html 不存在"}),
            404,
        )

    @bp.post("/api/login")
    def login():
        if not auth_token:
            return jsonify({"ok": False, "error": "服务未配置 web_auth_token"}), 503
        data = request.get_json(silent=True) or {}
        provided = (
            data.get("token")
            or data.get("password")
            or request.form.get("token")
            or request.form.get("password")
            or ""
        )
        provided = str(provided).strip()
        if provided != auth_token:
            return jsonify({"ok": False, "error": "Token 不正确"}), 401
        return jsonify({"ok": True, "token": auth_token})

    @bp.post("/api/parse")
    @require_auth
    def parse_media():
        data = request.get_json(silent=True) or {}
        url = (data.get("url") or request.form.get("url") or "").strip()
        if not url:
            return jsonify({"ok": False, "error": "缺少 url"}), 400
        try:
            result = task_manager.parse(url)
            if isinstance(result, dict) and result.get("success") is False:
                return jsonify({"ok": False, "error": result.get("error") or "解析失败", "result": result}), 422
            return jsonify({"ok": True, "result": result})
        except ValueError as e:
            return jsonify({"ok": False, "error": str(e)}), 400
        except Exception as e:
            logger.exception("解析失败: %s", e)
            return jsonify({"ok": False, "error": str(e)}), 500

    @bp.get("/api/media-proxy")
    @require_auth
    def media_proxy():
        """代理媒体直链，供页面预览（带 Referer，绕过部分 CDN 防盗链）。"""
        from html import unescape as html_unescape
        from urllib.parse import unquote

        media_url = (request.args.get("url") or "").strip()
        if not media_url:
            return jsonify({"ok": False, "error": "缺少 url"}), 400
        # 兼容多次编码 / HTML 实体（&amp;）导致签名失效
        for _ in range(3):
            decoded = unquote(media_url)
            if decoded == media_url:
                break
            media_url = decoded
        media_url = html_unescape(media_url).replace("&amp;", "&").strip()

        parsed = urlparse(media_url)
        if parsed.scheme not in ("http", "https"):
            return jsonify({"ok": False, "error": "仅支持 http/https"}), 400
        if not _host_allowed(parsed.hostname or ""):
            logger.warning("media-proxy 拒绝域名: %s", parsed.hostname)
            return jsonify({"ok": False, "error": f"该媒体域名不允许代理: {parsed.hostname}"}), 403

        host = (parsed.hostname or "").lower()
        if any(x in host for x in ("douyin", "snssdk", "byte", "iesdouyin")):
            referer = "https://www.douyin.com/"
            ua = (
                "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) "
                "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.0 "
                "Mobile/15E148 Safari/604.1"
            )
        elif "xhs" in host or "xiaohongshu" in host:
            referer = "https://www.xiaohongshu.com/"
            ua = (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36"
            )
        else:
            referer = f"{parsed.scheme}://{parsed.hostname}/"
            ua = (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36"
            )

        headers = {
            "User-Agent": ua,
            "Referer": referer,
            "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
        }
        range_header = request.headers.get("Range")
        if range_header:
            headers["Range"] = range_header

        try:
            upstream = requests.get(
                media_url,
                headers=headers,
                stream=True,
                timeout=60,
                allow_redirects=True,
            )
        except requests.RequestException as e:
            logger.warning("media-proxy 请求失败: %s", e)
            return jsonify({"ok": False, "error": f"拉取媒体失败: {e}"}), 502

        if upstream.status_code >= 400:
            status = upstream.status_code
            upstream.close()
            logger.warning("media-proxy 上游失败 HTTP %s: %s", status, media_url[:180])
            return jsonify({"ok": False, "error": f"上游 HTTP {status}"}), status

        excluded = {"content-encoding", "transfer-encoding", "connection", "content-length"}
        out_headers = {
            k: v
            for k, v in upstream.headers.items()
            if k.lower() not in excluded
        }
        if "Content-Type" not in out_headers and "content-type" not in {k.lower() for k in out_headers}:
            ctype = "image/jpeg"
            low = media_url.lower()
            if ".png" in low:
                ctype = "image/png"
            elif ".webp" in low:
                ctype = "image/webp"
            elif ".mp4" in low or "mime_type=video" in low:
                ctype = "video/mp4"
            out_headers["Content-Type"] = ctype
        out_headers["Cache-Control"] = "private, max-age=300"

        def generate():
            try:
                for chunk in upstream.iter_content(chunk_size=64 * 1024):
                    if chunk:
                        yield chunk
            finally:
                upstream.close()

        return Response(
            stream_with_context(generate()),
            status=upstream.status_code,
            headers=out_headers,
        )

    @bp.post("/api/download")
    @require_auth
    def create_download():
        data = request.get_json(silent=True) or {}
        url = (data.get("url") or request.form.get("url") or "").strip()
        if not url:
            return jsonify({"ok": False, "error": "缺少 url"}), 400
        try:
            task = task_manager.submit(url)
            return jsonify({"ok": True, "task": task}), 202
        except ValueError as e:
            return jsonify({"ok": False, "error": str(e)}), 400
        except Exception as e:
            logger.exception("提交下载失败: %s", e)
            return jsonify({"ok": False, "error": str(e)}), 500

    @bp.get("/api/tasks")
    @require_auth
    def list_tasks():
        try:
            limit = int(request.args.get("limit", 50))
        except (TypeError, ValueError):
            limit = 50
        tasks = task_manager.list_tasks(limit=limit)
        return jsonify({"ok": True, "tasks": tasks})

    @bp.get("/api/tasks/<task_id>")
    @require_auth
    def get_task(task_id: str):
        task = task_manager.get(task_id)
        if not task:
            return jsonify({"ok": False, "error": "任务不存在"}), 404
        return jsonify({"ok": True, "task": task})

    @bp.post("/api/tasks/<task_id>/cancel")
    @require_auth
    def cancel_task(task_id: str):
        cancelled = task_manager.cancel(task_id)
        if not cancelled:
            task = task_manager.get(task_id)
            if not task:
                return jsonify({"ok": False, "error": "任务不存在"}), 404
            return jsonify({"ok": False, "error": "任务无法取消", "task": task}), 409
        return jsonify({"ok": True, "task": task_manager.get(task_id)})

    return bp
