#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
源码只读索引器（Agent 2.0 阶段二 B1）

职责：
- 在配置的源码根目录下，构建 类名/文件名 → 文件路径 的只读索引
- 提供 read_snippet() 按行号读取源码片段（±N 行），**只读打开，永不写入**
- 未配置源码目录时自动跳过（is_enabled() 返回 False），Planner 计划中明示"源码关联未启用"

配置方式：
- 环境变量 LOG_SOURCE_CODE_ROOT：源码根目录绝对路径
- 环境变量 LOG_SOURCE_CODE_MAX_FILE_KB：单文件大小上限（默认 512KB，超出跳过）
- 环境变量 LOG_SOURCE_CODE_MAX_FILES：索引文件数上限（默认 20000，超出停止扫描）

安全红线：
- 只读：本模块不含任何 write / os.remove / shutil 操作
- 路径锚定：read_snippet 只允许读取 root 子树内的文件，防路径穿越
- 扫描跳过：.git / build / .gradle / node_modules / __pycache__ 等目录不扫
"""
import logging
import os
import re
import threading
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------
_DEFAULT_ROOT = os.environ.get("LOG_SOURCE_CODE_ROOT", "").strip()
_MAX_FILE_KB = int(os.environ.get("LOG_SOURCE_CODE_MAX_FILE_KB", "512"))
_MAX_FILES = int(os.environ.get("LOG_SOURCE_CODE_MAX_FILES", "20000"))

# 支持的源码文件扩展名
_SOURCE_EXTS = {".java", ".kt"}

# 扫描时跳过的目录名（版本控制 / 构建产物 / IDE 缓存）
_SKIP_DIRS = frozenset({
    ".git", ".svn", ".hg", "build", "out", "target",
    ".gradle", ".idea", ".vscode",
    "node_modules", "__pycache__", ".cxx",
})

# Java/Kotlin 类声明正则：class/interface/enum/object Name
_CLASS_RE = re.compile(
    r"^\s*(?:public|private|protected|internal|abstract|final|open|sealed|data|"
    r"companion|static|\s)*"
    r"(?:class|interface|enum|object)\s+([A-Z][A-Za-z0-9_]*)",
    re.MULTILINE,
)


def _resolve_root(raw: str) -> Optional[str]:
    """安全解析源码根目录，返回规范化的绝对路径（不存在则 None）。"""
    if not raw:
        return None
    root = os.path.abspath(raw)
    if not os.path.isdir(root):
        logger.warning("[source_index] 配置的源码根目录不存在: %s", root)
        return None
    return root


class SourceCodeIndex:
    """源码只读索引器（线程安全懒加载单例）。"""

    def __init__(
        self,
        root: Optional[str] = None,
        max_file_kb: int = _MAX_FILE_KB,
        max_files: int = _MAX_FILES,
    ) -> None:
        self._root: Optional[str] = _resolve_root(root if root is not None else _DEFAULT_ROOT)
        self._max_file_bytes = max_file_kb * 1024
        self._max_files = max_files
        self._lock = threading.Lock()
        self._built = False
        # class_name → [file_path, ...]  （同名类可能多个文件）
        self._class_index: Dict[str, List[str]] = {}
        # simple_filename（如 PlayerManager.java）→ [file_path, ...]
        self._filename_index: Dict[str, List[str]] = {}
        self._file_count = 0
        self._scan_errors = 0
        self._build_ms = 0

    # ------------------------------------------------------------------ 公开

    @property
    def root(self) -> Optional[str]:
        return self._root

    def is_enabled(self) -> bool:
        """是否启用了源码索引（配置了根目录且目录存在）。"""
        return self._root is not None

    def status(self) -> Dict[str, Any]:
        """返回索引状态摘要，供 Planner / 前端展示。"""
        self._ensure_built()
        return {
            "enabled": self.is_enabled(),
            "root": self._root or "",
            "file_count": self._file_count,
            "class_count": len(self._class_index),
            "scan_errors": self._scan_errors,
            "build_ms": self._build_ms,
        }

    def find_files(self, class_or_filename: str) -> List[str]:
        """按类名或文件名查找源码文件路径。

        查找顺序：
        1. 精确类名匹配（如 PlayerManager）
        2. 精确文件名匹配（如 PlayerManager.java）
        3. 类名后缀模糊（如传 PlayerManager.java 先剥扩展名再查类名）
        """
        if not class_or_filename or not self.is_enabled():
            return []
        self._ensure_built()
        key = class_or_filename.strip()
        # 先查类名索引
        hits = self._class_index.get(key)
        if hits:
            return list(hits)
        # 再查文件名索引
        hits = self._filename_index.get(os.path.basename(key))
        if hits:
            return list(hits)
        # 剥扩展名再查一次类名
        stem = os.path.splitext(os.path.basename(key))[0]
        if stem and stem != key:
            hits = self._class_index.get(stem)
            if hits:
                return list(hits)
        return []

    def read_snippet(
        self,
        file_path: str,
        target_line: int,
        context: int = 15,
    ) -> Optional[Dict[str, Any]]:
        """只读读取源码片段（target_line ± context 行）。

        返回结构：
        {
            "file": file_path,
            "target_line": int,
            "start_line": int,
            "end_line": int,
            "lines": [{"lineno": int, "text": str, "is_target": bool}, ...],
            "truncated": bool,
        }
        路径不在 root 子树内或文件过大 → 返回 None。
        """
        if not self.is_enabled():
            return None
        safe = self._safe_path(file_path)
        if safe is None:
            logger.warning("[source_index] 路径不在源码根目录内，拒绝读取: %s", file_path)
            return None
        if not os.path.isfile(safe):
            return None
        try:
            size = os.path.getsize(safe)
            if size > self._max_file_bytes:
                logger.info("[source_index] 文件过大(%.0fKB)，跳过: %s", size / 1024, safe)
                return None
            with open(safe, "r", encoding="utf-8", errors="replace") as fh:
                all_lines = fh.readlines()
        except OSError as exc:
            logger.warning("[source_index] 读取失败: %s — %s", safe, exc)
            return None

        total = len(all_lines)
        if total == 0:
            return None
        # 行号从 1 开始；target_line 超出范围时 clamp
        clamped = max(1, min(target_line, total))
        start = max(1, clamped - context)
        end = min(total, clamped + context)
        snippet_lines = []
        for ln in range(start, end + 1):
            text = all_lines[ln - 1].rstrip("\n\r")
            snippet_lines.append({
                "lineno": ln,
                "text": text,
                "is_target": ln == clamped,
            })
        return {
            "file": safe,
            "target_line": clamped,
            "start_line": start,
            "end_line": end,
            "lines": snippet_lines,
            "truncated": (start > 1 or end < total),
        }

    # ------------------------------------------------------------------ 内部

    def _ensure_built(self) -> None:
        """懒构建索引（首次访问时触发，线程安全）。"""
        if self._built or not self.is_enabled():
            return
        with self._lock:
            if self._built:
                return
            self._build()
            self._built = True

    def _build(self) -> None:
        """扫描源码根目录，构建类名/文件名索引。"""
        import time
        t0 = time.monotonic()
        file_count = 0
        scan_errors = 0
        for dirpath, dirnames, filenames in os.walk(self._root):
            # 剪枝：跳过版本控制/构建产物目录
            dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
            for fname in filenames:
                ext = os.path.splitext(fname)[1]
                if ext not in _SOURCE_EXTS:
                    continue
                if file_count >= self._max_files:
                    logger.warning(
                        "[source_index] 文件数达上限 %d，停止扫描", self._max_files,
                    )
                    break
                fpath = os.path.join(dirpath, fname)
                file_count += 1
                # 文件名索引
                self._filename_index.setdefault(fname, []).append(fpath)
                # 类名索引：读文件提取 class/interface/enum/object 声明
                try:
                    size = os.path.getsize(fpath)
                    if size > self._max_file_bytes:
                        continue
                    with open(fpath, "r", encoding="utf-8", errors="replace") as fh:
                        content = fh.read()
                    for m in _CLASS_RE.finditer(content):
                        cname = m.group(1)
                        # 避免同文件重复添加
                        bucket = self._class_index.setdefault(cname, [])
                        if fpath not in bucket:
                            bucket.append(fpath)
                except OSError as exc:
                    scan_errors += 1
                    logger.debug("[source_index] 扫描失败: %s — %s", fpath, exc)
            else:
                continue
            # 内层 break（达到上限）→ 外层也停
            if file_count >= self._max_files:
                break
        self._file_count = file_count
        self._scan_errors = scan_errors
        self._build_ms = int((time.monotonic() - t0) * 1000)
        logger.info(
            "[source_index] 索引构建完成: %d 文件, %d 类名, %d 错误, %dms",
            file_count, len(self._class_index), scan_errors, self._build_ms,
        )

    def _safe_path(self, raw: str) -> Optional[str]:
        """路径锚定：确保 raw 在 root 子树内，返回规范化绝对路径或 None。"""
        if not self._root:
            return None
        # 如果是相对路径，以 root 为基
        if not os.path.isabs(raw):
            candidate = os.path.normpath(os.path.join(self._root, raw))
        else:
            candidate = os.path.normpath(raw)
        # 防路径穿越：规范化后必须以 root 开头
        root_norm = os.path.normpath(self._root)
        if not (candidate == root_norm or candidate.startswith(root_norm + os.sep)):
            return None
        return candidate


# ---------------------------------------------------------------------------
# 模块级单例（懒加载）
# ---------------------------------------------------------------------------
_singleton: Optional[SourceCodeIndex] = None
_singleton_lock = threading.Lock()


def get_index() -> SourceCodeIndex:
    """获取全局 SourceCodeIndex 单例（首次调用时按环境变量初始化）。"""
    global _singleton
    if _singleton is not None:
        return _singleton
    with _singleton_lock:
        if _singleton is None:
            _singleton = SourceCodeIndex()
        return _singleton


def reset_index(root: str = "") -> SourceCodeIndex:
    """重置单例（用于测试或运行时切换源码目录）。

    传空字符串 → 禁用源码索引。
    """
    global _singleton
    with _singleton_lock:
        _singleton = SourceCodeIndex(root=root)
        return _singleton
