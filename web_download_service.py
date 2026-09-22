#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
网页下载任务管理器
复用 VideoDownloader.download_video，通过 message_updater 写入内存任务状态。
"""

from __future__ import annotations

import asyncio
import logging
import re
import threading
import time
import uuid
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _format_speed(speed_bytes_s: Optional[float]) -> str:
    if not speed_bytes_s or speed_bytes_s <= 0:
        return ""
    if speed_bytes_s >= 1024 * 1024:
        return f"{speed_bytes_s / (1024 * 1024):.2f} MB/s"
    if speed_bytes_s >= 1024:
        return f"{speed_bytes_s / 1024:.1f} KB/s"
    return f"{speed_bytes_s:.0f} B/s"


def _format_eta(eta_seconds: Optional[float]) -> str:
    try:
        seconds = int(eta_seconds or 0)
        if seconds <= 0:
            return ""
        minutes, sec = divmod(seconds, 60)
        hours, minutes = divmod(minutes, 60)
        if hours:
            return f"{hours:02d}:{minutes:02d}:{sec:02d}"
        return f"{minutes:02d}:{sec:02d}"
    except (TypeError, ValueError):
        return ""


def _extract_percent_from_text(text: str) -> Optional[float]:
    match = re.search(r"(\d+(?:\.\d+)?)\s*%", text)
    if match:
        return float(match.group(1))
    return None


class DownloadTaskManager:
    """管理网页触发的下载任务。"""

    def __init__(self, downloader: Any, max_tasks: int = 200):
        self.downloader = downloader
        self.max_tasks = max_tasks
        self._tasks: Dict[str, Dict[str, Any]] = {}
        self._lock = threading.RLock()

        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._loop_thread: Optional[threading.Thread] = None
        self._loop_ready = threading.Event()
        self._started = False

    def start(self) -> None:
        if self._started:
            return

        def _run_loop() -> None:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            self._loop = loop
            self._loop_ready.set()
            logger.info("✅ DownloadTaskManager 事件循环已启动")
            loop.run_forever()

        self._loop_thread = threading.Thread(
            target=_run_loop, name="web-download-loop", daemon=True
        )
        self._loop_thread.start()
        if not self._loop_ready.wait(timeout=10):
            raise RuntimeError("DownloadTaskManager 事件循环启动超时")
        self._started = True

    def stop(self) -> None:
        if self._loop and self._loop.is_running():
            self._loop.call_soon_threadsafe(self._loop.stop)
        self._started = False

    def _ensure_started(self) -> asyncio.AbstractEventLoop:
        if not self._started or not self._loop:
            self.start()
        assert self._loop is not None
        return self._loop

    def _trim_tasks_locked(self) -> None:
        if len(self._tasks) <= self.max_tasks:
            return
        finished = [
            (tid, t)
            for tid, t in self._tasks.items()
            if t.get("status") in ("completed", "failed", "cancelled")
        ]
        finished.sort(key=lambda item: item[1].get("updated_at", 0))
        overflow = len(self._tasks) - self.max_tasks
        for tid, _ in finished[:overflow]:
            del self._tasks[tid]

    def _create_task_record(self, url: str) -> Dict[str, Any]:
        now = time.time()
        task_id = uuid.uuid4().hex[:12]
        return {
            "id": task_id,
            "url": url,
            "status": "queued",
            "percent": 0.0,
            "speed": "",
            "eta": "",
            "message": "等待开始",
            "filename": "",
            "result": None,
            "error": None,
            "cancelled": False,
            "created_at": now,
            "updated_at": now,
            "_asyncio_task": None,
        }

    def _public_view(self, task: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "id": task["id"],
            "url": task["url"],
            "status": task["status"],
            "percent": task.get("percent", 0.0),
            "speed": task.get("speed", ""),
            "eta": task.get("eta", ""),
            "message": task.get("message", ""),
            "filename": task.get("filename", ""),
            "result": task.get("result"),
            "error": task.get("error"),
            "created_at": task.get("created_at"),
            "updated_at": task.get("updated_at"),
        }

    def _update_task(self, task_id: str, **fields: Any) -> None:
        with self._lock:
            task = self._tasks.get(task_id)
            if not task:
                return
            task.update(fields)
            task["updated_at"] = time.time()

    def _apply_progress(self, task_id: str, payload: Any) -> None:
        with self._lock:
            task = self._tasks.get(task_id)
            if not task or task.get("cancelled"):
                return

            if isinstance(payload, str):
                text = payload.strip()
                task["message"] = text[:500]
                percent = _extract_percent_from_text(text)
                if percent is not None:
                    task["percent"] = max(0.0, min(100.0, percent))
                if task["status"] not in ("completed", "failed", "cancelled"):
                    task["status"] = "downloading"
                task["updated_at"] = time.time()
                return

            if not isinstance(payload, dict):
                task["message"] = str(payload)[:500]
                task["updated_at"] = time.time()
                return

            status = payload.get("status")
            filename = payload.get("filename") or payload.get("tmpfilename") or ""
            if filename:
                task["filename"] = str(filename).rsplit("/", 1)[-1].rsplit("\\", 1)[-1]

            if status == "downloading":
                total = _safe_float(
                    payload.get("total_bytes") or payload.get("total_bytes_estimate")
                )
                downloaded = _safe_float(payload.get("downloaded_bytes"))
                speed = _safe_float(payload.get("speed"))
                eta = payload.get("eta")
                percent = payload.get("percent")
                if percent is None and total > 0:
                    percent = downloaded * 100.0 / total
                if percent is not None:
                    task["percent"] = max(0.0, min(100.0, _safe_float(percent)))
                task["speed"] = _format_speed(speed) if speed else task.get("speed", "")
                task["eta"] = _format_eta(eta) if eta else task.get("eta", "")
                task["status"] = "downloading"
                task["message"] = payload.get("message") or f"下载中 {task['percent']:.1f}%"
            elif status in ("finished", "complete", "completed"):
                task["percent"] = max(task.get("percent", 0.0), 100.0)
                task["status"] = "downloading"
                task["message"] = payload.get("message") or "下载完成，处理中..."
                task["speed"] = ""
                task["eta"] = ""
            elif status == "error":
                task["status"] = "failed"
                task["error"] = str(payload.get("error") or payload.get("message") or "下载失败")
                task["message"] = task["error"]
            else:
                if payload.get("message"):
                    task["message"] = str(payload.get("message"))[:500]
                if payload.get("percent") is not None:
                    task["percent"] = max(0.0, min(100.0, _safe_float(payload.get("percent"))))
                if task["status"] not in ("completed", "failed", "cancelled"):
                    task["status"] = "downloading"

            task["updated_at"] = time.time()

    def _make_message_updater(self, task_id: str) -> Callable:
        manager = self

        async def message_updater(payload: Any = None, *args: Any, **kwargs: Any) -> None:
            data = payload
            if data is None and args:
                data = args[0]
            manager._apply_progress(task_id, data)

        # 同步/异步均可调用：部分 hook 会直接调用非 async 函数
        def sync_or_async_updater(payload: Any = None, *args: Any, **kwargs: Any):
            data = payload
            if data is None and args:
                data = args[0]
            manager._apply_progress(task_id, data)

        return sync_or_async_updater

    async def _run_download(self, task_id: str, url: str) -> None:
        self._update_task(task_id, status="downloading", message="开始下载")
        updater = self._make_message_updater(task_id)
        try:
            with self._lock:
                cancelled = self._tasks.get(task_id, {}).get("cancelled", False)
            if cancelled:
                self._update_task(task_id, status="cancelled", message="已取消")
                return

            result = await self.downloader.download_video(
                url, message_updater=updater
            )

            with self._lock:
                cancelled = self._tasks.get(task_id, {}).get("cancelled", False)
            if cancelled:
                self._update_task(task_id, status="cancelled", message="已取消")
                return

            if isinstance(result, dict) and result.get("error"):
                self._update_task(
                    task_id,
                    status="failed",
                    error=str(result.get("error")),
                    message=str(result.get("error")),
                    result=result,
                    percent=self.get(task_id).get("percent", 0) if self.get(task_id) else 0,
                )
                return

            success = True
            if isinstance(result, dict) and "success" in result:
                success = bool(result.get("success"))

            if success:
                public_result = result if isinstance(result, dict) else {"raw": str(result)}
                self._update_task(
                    task_id,
                    status="completed",
                    percent=100.0,
                    message="下载完成",
                    result=public_result,
                    speed="",
                    eta="",
                )
            else:
                err = ""
                if isinstance(result, dict):
                    err = str(result.get("error") or result.get("message") or "下载失败")
                self._update_task(
                    task_id,
                    status="failed",
                    error=err or "下载失败",
                    message=err or "下载失败",
                    result=result if isinstance(result, dict) else None,
                )
        except asyncio.CancelledError:
            self._update_task(task_id, status="cancelled", message="已取消")
            raise
        except Exception as e:
            logger.exception("网页下载任务失败 [%s]: %s", task_id, e)
            self._update_task(
                task_id,
                status="failed",
                error=str(e),
                message=str(e),
            )

    def submit(self, url: str) -> Dict[str, Any]:
        url = (url or "").strip()
        if not url:
            raise ValueError("url 不能为空")

        loop = self._ensure_started()
        with self._lock:
            task = self._create_task_record(url)
            task_id = task["id"]
            self._tasks[task_id] = task
            self._trim_tasks_locked()

        async def _spawn() -> None:
            aio_task = asyncio.create_task(self._run_download(task_id, url))
            with self._lock:
                if task_id in self._tasks:
                    self._tasks[task_id]["_asyncio_task"] = aio_task

            def _cleanup(done: asyncio.Task) -> None:
                with self._lock:
                    current = self._tasks.get(task_id)
                    if current and current.get("_asyncio_task") is done:
                        current["_asyncio_task"] = None

            aio_task.add_done_callback(_cleanup)

        fut = asyncio.run_coroutine_threadsafe(_spawn(), loop)
        fut.result(timeout=10)
        return self.get(task_id)

    def parse(self, url: str, timeout: float = 90.0) -> Dict[str, Any]:
        """同步调用 downloader.parse_media，供网页解析预览。"""
        url = (url or "").strip()
        if not url:
            raise ValueError("url 不能为空")
        loop = self._ensure_started()

        async def _run() -> Dict[str, Any]:
            return await self.downloader.parse_media(url)

        fut = asyncio.run_coroutine_threadsafe(_run(), loop)
        try:
            return fut.result(timeout=timeout)
        except Exception as e:
            logger.exception("网页解析失败: %s", e)
            raise

    def get(self, task_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            task = self._tasks.get(task_id)
            if not task:
                return None
            return self._public_view(task)

    def list_tasks(self, limit: int = 50) -> List[Dict[str, Any]]:
        with self._lock:
            items = sorted(
                self._tasks.values(),
                key=lambda t: t.get("created_at", 0),
                reverse=True,
            )
            return [self._public_view(t) for t in items[: max(1, min(limit, 200))]]

    def cancel(self, task_id: str) -> bool:
        loop = self._ensure_started()
        with self._lock:
            task = self._tasks.get(task_id)
            if not task:
                return False
            if task.get("status") in ("completed", "failed", "cancelled"):
                return False
            task["cancelled"] = True
            task["status"] = "cancelled"
            task["message"] = "正在取消..."
            task["updated_at"] = time.time()
            aio_task = task.get("_asyncio_task")

        if aio_task and not aio_task.done():
            loop.call_soon_threadsafe(aio_task.cancel)

        self._update_task(task_id, status="cancelled", message="已取消")
        return True
