import base64
import hashlib
import os
import re
import subprocess
import threading
from urllib.parse import urlparse
from urllib.parse import unquote


MAX_GIT_DIFF_CHARS = 100_000
MAX_CHANGED_FILES = 80
MAX_FILE_DIFF_CHARS = 20_000
SAFE_REF = re.compile(r"^[^\s~^:?*\[\\]+$")
_LOCKS = {}
_LOCKS_GUARD = threading.Lock()


def parse_repository_input(value):
    raw = str(value or "").strip()
    markdown = re.match(r"^\[[^\]]*\]\((https://[^)]+)\)$", raw)
    if markdown:
        raw = markdown.group(1)
    raw = raw.strip("<> \t\r\n")
    target_ref = ""
    for marker in ("/-/tree/", "/-/commit/"):
        if marker in raw:
            url, encoded_ref = raw.split(marker, 1)
            target_ref = unquote(encoded_ref.split("?", 1)[0].split("#", 1)[0]).strip("/")
            break
    else:
        url = raw
    if "/-/tree/" in url:
        url = url.split("/-/tree/", 1)[0]
    if "/-/commit/" in url:
        url = url.split("/-/commit/", 1)[0]
    if url and not url.endswith(".git"):
        url += ".git"
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.netloc:
        raise ValueError("目前只支持 HTTPS Git 仓库地址")
    if parsed.username or parsed.password:
        raise ValueError("仓库地址中不能包含账号或密码")
    return {"repository_url": url, "target_ref": target_ref}


def normalize_repository_url(value):
    return parse_repository_input(value)["repository_url"]


def validate_ref(value, field_name):
    ref = str(value or "").strip()
    if not ref:
        raise ValueError(f"{field_name}不能为空")
    if len(ref) > 200 or not SAFE_REF.match(ref) or ".." in ref or ref.startswith("-"):
        raise ValueError(f"{field_name}格式不合法")
    return ref


def _cache_dir(repo_url):
    root = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
    digest = hashlib.sha256(repo_url.encode("utf-8")).hexdigest()[:20]
    return os.path.join(root, "data", "precision_test", "git_cache", digest)


def _git_env():
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    token = str(os.environ.get("PRECISION_GIT_TOKEN") or "").strip()
    username = str(os.environ.get("PRECISION_GIT_USERNAME") or "").strip()
    password = str(os.environ.get("PRECISION_GIT_PASSWORD") or "").strip()
    secret = token or password
    if secret:
        identity = username or ("oauth2" if token else "")
        basic = base64.b64encode(f"{identity}:{secret}".encode("utf-8")).decode("ascii")
        env["GIT_CONFIG_COUNT"] = "1"
        env["GIT_CONFIG_KEY_0"] = "http.extraHeader"
        env["GIT_CONFIG_VALUE_0"] = f"Authorization: Basic {basic}"
    return env


def _run_git(args, cwd=None, timeout=120):
    proc = subprocess.run(
        ["git"] + list(args),
        cwd=cwd,
        env=_git_env(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
    )
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "Git 命令失败").strip()
        detail = re.sub(r"https://[^@\s]+@", "https://***@", detail)
        raise RuntimeError(detail[-1200:])
    return proc.stdout


def _repo_lock(repo_url):
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(repo_url, threading.Lock())


def _ensure_cache(repo_url):
    cache_dir = _cache_dir(repo_url)
    os.makedirs(os.path.dirname(cache_dir), exist_ok=True)
    if not os.path.exists(os.path.join(cache_dir, "HEAD")):
        os.makedirs(cache_dir, exist_ok=True)
        _run_git(["init", "--bare"], cwd=cache_dir, timeout=30)
        _run_git(["remote", "add", "origin", repo_url], cwd=cache_dir, timeout=30)
    else:
        current = _run_git(["remote", "get-url", "origin"], cwd=cache_dir, timeout=30).strip()
        if current != repo_url:
            raise RuntimeError("Git 缓存地址不一致")
    return cache_dir


def _fetch_ref(cache_dir, ref, local_ref):
    refspecs = [
        f"+refs/heads/{ref}:{local_ref}",
        f"+refs/tags/{ref}:{local_ref}",
        f"+{ref}:{local_ref}",
    ]
    last_error = None
    for refspec in refspecs:
        try:
            _run_git(
                ["fetch", "--no-tags", "--filter=blob:none", "--depth=500", "origin", refspec],
                cwd=cache_dir,
                timeout=180,
            )
            return
        except RuntimeError as exc:
            last_error = exc
    raise RuntimeError(f"无法拉取引用 {ref}: {last_error}")


def _is_analysis_file(path):
    lower = path.lower()
    excluded_parts = (
        "/build/", "/dist/", "/target/", "/node_modules/", "/.gradle/",
        "/generated/", "/vendor/", "/third_party/", "/assets/", "/res/drawable",
    )
    if any(part in f"/{lower}" for part in excluded_parts):
        return False
    excluded_ext = (
        ".apk", ".aar", ".jar", ".so", ".a", ".zip", ".7z", ".rar", ".png",
        ".jpg", ".jpeg", ".gif", ".webp", ".mp3", ".mp4", ".wav", ".ttf",
        ".otf", ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".keystore",
    )
    return not lower.endswith(excluded_ext)


def build_git_diff(repo_url, target_ref, base_commit="", comparison_mode="release"):
    parsed = parse_repository_input(repo_url)
    repo_url = parsed["repository_url"]
    target_ref = target_ref or parsed["target_ref"]
    target_ref = validate_ref(target_ref, "目标分支/Commit")
    if comparison_mode not in {"release", "latest_commit"}:
        raise ValueError("不支持的代码对比模式")
    if comparison_mode == "release":
        base_commit = validate_ref(base_commit, "上一个发布 Commit")
    with _repo_lock(repo_url):
        cache_dir = _ensure_cache(repo_url)
        _fetch_ref(cache_dir, target_ref, "refs/precision/target")
        target_sha = _run_git(["rev-parse", "refs/precision/target"], cwd=cache_dir).strip()
        if comparison_mode == "latest_commit":
            try:
                base_sha = _run_git(
                    ["rev-parse", "refs/precision/target^1"],
                    cwd=cache_dir,
                ).strip()
            except RuntimeError:
                raise ValueError("目标 Commit 没有父提交，无法仅对比本次提交")
            base_commit = base_sha
        else:
            _fetch_ref(cache_dir, base_commit, "refs/precision/base")
            base_sha = _run_git(["rev-parse", "refs/precision/base"], cwd=cache_dir).strip()
        target_commit = _run_git(
            ["show", "-s", "--format=%H%x09%an%x09%cs%x09%s", target_sha],
            cwd=cache_dir,
        ).strip().split("\t", 3)
        commits = _run_git(
            ["log", "--no-merges", "--format=%h%x09%an%x09%s", "--max-count=30", f"{base_sha}..{target_sha}"],
            cwd=cache_dir,
        )
        files = _run_git(["diff", "--name-status", base_sha, target_sha], cwd=cache_dir)
        changed_files = [
            {"status": parts[0], "path": parts[-1]}
            for line in files.splitlines()
            if line.strip()
            for parts in [line.split("\t")]
        ]
        analysis_files = [
            item for item in changed_files if _is_analysis_file(item["path"])
        ][:MAX_CHANGED_FILES]
        chunks = []
        included_files = []
        truncated = len(analysis_files) < len([item for item in changed_files if _is_analysis_file(item["path"])])
        for item in analysis_files:
            try:
                chunk = _run_git(
                    ["diff", "--no-ext-diff", "--unified=3", base_sha, target_sha, "--", item["path"]],
                    cwd=cache_dir,
                    timeout=60,
                )
            except RuntimeError:
                continue
            if not chunk:
                continue
            if len(chunk) > MAX_FILE_DIFF_CHARS:
                chunk = chunk[:MAX_FILE_DIFF_CHARS] + "\n... [单文件 Diff 已截断]\n"
                truncated = True
            if sum(len(value) for value in chunks) + len(chunk) > MAX_GIT_DIFF_CHARS:
                truncated = True
                break
            chunks.append(chunk)
            included_files.append(item["path"])
        diff = "\n".join(chunks)
        if not diff:
            raise ValueError("未提取到可分析的源码 Diff；变更可能仅包含二进制或构建产物")
        return {
            "repository_url": repo_url,
            "target_ref": target_ref,
            "target_sha": target_sha,
            "base_commit": base_commit,
            "base_sha": base_sha,
            "comparison_mode": comparison_mode,
            "comparison_label": "仅本次提交" if comparison_mode == "latest_commit" else "上次发布版本",
            "target_commit": {
                "sha": target_commit[0],
                "author": target_commit[1] if len(target_commit) > 1 else "",
                "date": target_commit[2] if len(target_commit) > 2 else "",
                "subject": target_commit[3] if len(target_commit) > 3 else "",
            },
            "changed_files": changed_files,
            "included_files": included_files,
            "diff_truncated": truncated,
            "analysis_note": (
                f"变更共 {len(changed_files)} 个文件；为保证分析速度，"
                f"已提取 {len(included_files)} 个源码文件，Diff 上限 {MAX_GIT_DIFF_CHARS:,} 字符。"
            ),
            "commits": [
                {"sha": parts[0], "author": parts[1], "subject": parts[2]}
                for line in commits.splitlines()
                if line.strip()
                for parts in [line.split("\t", 2)]
            ],
            "code_diff": diff,
        }


def detect_release_baseline(repo_url, target_ref):
    parsed = parse_repository_input(repo_url)
    repo_url = parsed["repository_url"]
    target_ref = target_ref or parsed["target_ref"]
    target_ref = validate_ref(target_ref, "目标分支/Commit")
    with _repo_lock(repo_url):
        cache_dir = _ensure_cache(repo_url)
        _fetch_ref(cache_dir, target_ref, "refs/precision/target")
        _run_git(["fetch", "--tags", "--filter=blob:none", "origin"], cwd=cache_dir, timeout=180)
        target_sha = _run_git(["rev-parse", "refs/precision/target"], cwd=cache_dir).strip()
        output = _run_git(
            [
                "tag",
                "--merged",
                target_sha,
                "--sort=-creatordate",
                "--format=%(creatordate:short)%09%(refname:short)%09%(objectname)",
                "--list",
            ],
            cwd=cache_dir,
        )
        branch_tokens = {
            token.lower()
            for token in re.split(r"[^a-zA-Z0-9]+", target_ref)
            if len(token) >= 2 and token.lower() not in {"dev", "feature", "release"}
        }
        candidates = []
        for line in output.splitlines():
            parts = line.split("\t", 2)
            if len(parts) != 3:
                continue
            date, tag, sha = parts
            lower = tag.lower()
            release_marker = any(marker in lower for marker in ("release", "stable", "version", "tag"))
            version_marker = bool(re.search(r"(^|[^a-z])v?\d+(?:\.\d+){1,}", lower))
            matches = sorted(token for token in branch_tokens if token in lower)
            if not release_marker and not version_marker:
                continue
            score = 20
            if release_marker:
                score += 30
            if version_marker:
                score += 20
            score += min(len(matches) * 15, 45)
            candidates.append({
                "tag": tag,
                "sha": sha,
                "date": date,
                "score": score,
                "branch_matches": matches,
            })

        if not candidates:
            raise RuntimeError("目标分支没有可达的发布 Tag，请手工填写上一个发布 Commit")

        candidates.sort(key=lambda item: (item["score"], item["date"]), reverse=True)
        selected = candidates[0]
        exact_product_match = bool(selected["branch_matches"])
        return {
            "repository_url": repo_url,
            "target_ref": target_ref,
            "target_sha": target_sha,
            "base_commit": selected["sha"],
            "tag": selected["tag"],
            "tag_date": selected["date"],
            "confidence": "high" if exact_product_match else "medium",
            "reason": (
                "发布 Tag 与目标分支产品关键词匹配，且为最新可达候选"
                if exact_product_match
                else "选择目标分支可达的最新正式发布 Tag；Tag 未包含完整产品关键词，请确认"
            ),
            "candidates": candidates[:5],
        }
