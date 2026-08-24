"""CodeGraph 影响分析适配层。

把 CodeGraph（local-first 代码知识图谱，tree-sitter 解析）作为「代码影响透视层」
接入精准回归：研发给出 diff -> 这里追出「受影响的测试文件」与「改动符号的影响链」，
作为 precision_test 风险推导与测试点生成的硬证据（回退安全：工具不可用时不阻断主流程）。

文档依据（CodeGraph 官方能力）：
- codegraph impact <symbol> -j      : 分析改动符号的影响范围（调用链/文件级，符号级稳定）
- codegraph affected <files> -f <g>  : 沿依赖追受影响测试文件（CI 收敛用，但 -f 不支持递归 glob，
                                       且依赖 shell 展开，不可靠；故本适配层以 impact 为主数据源推导）
- codegraph init / sync             : 本地建索引 / 增量同步（100% local，无云）

实现要点（已在 PoC 验证）：
1. 所有子命令用 cwd=repo_path 调用，**不传 -p**（实测 -p 在 Windows 下解析异常导致 not found）。
2. 以 impact（符号级）为主数据源：从影响链节点筛出测试文件/函数作为「该跑测试集」。
3. -f glob 不可靠，弃用；affected 仅作可选保留。
4. 静态分析看不到运行时动态分发/反射/AOP，业务场景映射仍需 precision_test 自身补全。
"""
import os
import re
import json
import subprocess

DEFAULT_TEST_GLOB = "**/test_*.py"


def _win(p):
    """把 Git Bash 风格 /d/foo 转 Windows D:\\foo，供 subprocess cwd 使用。
    实测：python 的 subprocess(cwd='/d/...') 把正斜杠传给 Windows 程序，
    codegraph 会在错误目录找索引导致 not found；shell 里 Git Bash 会自动转 D:\\。"""
    if not p:
        return p
    p = re.sub(r'^/([a-zA-Z])/', lambda m: m.group(1).upper() + ':\\\\', p)
    return p.replace('/', '\\\\')

_BIN_CANDIDATES = [
    "codegraph",
    r"C:\Users\58857\AppData\Roaming\npm\codegraph.cmd",
    os.path.expanduser(r"~\.workbuddy\binaries\node\versions\22.22.2\node_modules\.bin\codegraph.cmd"),
    "/usr/local/bin/codegraph",
]

# 各语言函数/类定义提取（按常见语法分别捕获名称）
_DEF_RE_LIST = [
    re.compile(r'^\s*(?:def|class|interface|struct|enum|trait|impl)\s+([A-Za-z_]\w*)'),
    re.compile(r'^\s*func\s+(?:\([^)]*\)\s*)?([A-Za-z_]\w*)'),
    re.compile(r'^\s*function\s+([A-Za-z_]\w*)'),
    re.compile(r'^\s*(?:const|let|var|val|fun)\s+([A-Za-z_]\w*)\s*='),
    re.compile(r'^\s*(?:public|private|protected|internal|static|final|async|override|virtual|'
               r'void|[\w<>\[\],.\s]+?)\s+([A-Za-z_]\w*)\s*\('),
]

# 启发式排除的内置/常见库函数，避免误当改动符号去查 impact
_BUILTIN = {
    "round", "len", "print", "int", "str", "list", "dict", "set", "tuple", "min", "max",
    "abs", "sum", "sorted", "range", "open", "isinstance", "type", "float", "bool",
    "enumerate", "zip", "map", "filter", "format", "super", "self", "cls",
}

_TEST_RE = re.compile(r'(?:^|[/\\])(?:test_[\w.\-]*|[\w.\-]*_test|\w+\.spec)\.')


def is_test_file(path):
    base = (path or "").replace("\\", "/").split("/")[-1]
    return bool(_TEST_RE.search(base))


def find_codegraph_bin():
    env = os.environ.get("CODEGRAPH_BIN")
    if env and os.path.exists(env):
        return env
    for c in _BIN_CANDIDATES:
        try:
            subprocess.run([c, "--version"], capture_output=True, text=True, timeout=10)
            return c
        except Exception:
            continue
    return None


def is_available():
    return find_codegraph_bin() is not None


def load_config(config_path=None):
    if config_path is None:
        config_path = os.path.join(os.path.dirname(__file__), "codegraph_config.json")
    cfg = {"enabled": False, "repo_path": "", "test_glob": DEFAULT_TEST_GLOB}
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            cfg.update(json.load(f))
    except Exception:
        pass
    if os.environ.get("CODEGRAPH_REPO_PATH"):
        cfg["repo_path"] = os.environ["CODEGRAPH_REPO_PATH"]
        cfg["enabled"] = True
    cfg["enabled"] = bool(cfg.get("enabled")) and bool(cfg.get("repo_path"))
    return cfg


def ensure_indexed(repo_path, bin_path=None):
    """确保仓库已建索引；返回 (indexed: bool, error: str)。"""
    bin_path = bin_path or find_codegraph_bin()
    if not bin_path:
        return False, "codegraph binary not found"
    repo = _win(repo_path)
    cg_dir = os.path.join(repo, ".codegraph")
    if os.path.isdir(cg_dir):
        return True, ""
    try:
        r = subprocess.run([bin_path, "init", repo],
                           capture_output=True, text=True, timeout=600, cwd=repo)
        if r.returncode == 0 and os.path.isdir(cg_dir):
            return True, ""
        return False, (r.stderr or r.stdout).strip()[-300:]
    except Exception as e:
        return False, str(e)


def parse_diff_changed_files(diff_text):
    """从 git diff 文本提取改动文件路径（去重）。"""
    files = []
    for line in (diff_text or "").splitlines():
        if line.startswith("diff --git"):
            parts = line.split()
            if len(parts) >= 4:
                f = parts[3][2:] if parts[3].startswith("b/") else parts[3]
                files.append(f)
        elif line.startswith("+++ "):
            f = line[4:].strip()
            if f and f != "/dev/null":
                files.append(f[2:] if f.startswith("b/") else f)
    seen, out = set(), []
    for f in files:
        if f and f not in seen:
            seen.add(f)
            out.append(f)
    return out


def extract_changed_symbols(diff_text):
    """启发式：从 diff hunk 提取改动附近的函数/类定义名，作为 impact 输入。"""
    symbols = []
    lines = (diff_text or "").splitlines()
    for i, line in enumerate(lines):
        if line.startswith("@@"):
            for nxt in lines[i + 1:i + 8]:
                stripped = nxt.lstrip("+").lstrip("-").lstrip()
                if not stripped or stripped.startswith("#") or stripped.startswith("//"):
                    continue
                for rx in _DEF_RE_LIST:
                    m = rx.match(stripped)
                    if m:
                        name = m.group(1)
                        if name and name not in symbols and name not in _BUILTIN:
                            symbols.append(name)
                        break
    return symbols


def get_affected_tests(repo_path, changed_files, test_glob=None, bin_path=None):
    """可选：直接用 codegraph affected（需正确的 -f，shell 展开依赖强，故非主路径）。"""
    bin_path = bin_path or find_codegraph_bin()
    if not bin_path or not changed_files:
        return []
    test_glob = test_glob or DEFAULT_TEST_GLOB
    cmd = [bin_path, "affected", "-f", test_glob, "-j"] + list(changed_files)
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=120, cwd=_win(repo_path))
        if r.returncode != 0:
            return []
        return json.loads(r.stdout).get("affectedTests", [])
    except Exception:
        return []


def get_impact(repo_path, symbol, bin_path=None):
    bin_path = bin_path or find_codegraph_bin()
    if not bin_path or not symbol:
        return []
    cmd = [bin_path, "impact", symbol, "-j"]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=120, cwd=_win(repo_path))
        if r.returncode != 0:
            return []
        return json.loads(r.stdout).get("affected", [])
    except Exception:
        return []


def analyze_diff_impact(repo_path, diff_text, test_glob=None, bin_path=None):
    """聚合影响分析：改动文件 -> 受影响测试 + 改动符号的影响链。

    返回结构（始终可安全序列化，工具不可用时 available=False 且不抛异常）：
    {
      "available": bool,
      "enabled": bool,
      "changed_files": [str],
      "affected_tests": [str],
      "impact_symbols": [ {name,kind,filePath,startLine} ],
      "error": str
    }
    """
    bin_path = bin_path or find_codegraph_bin()
    result = {
        "available": bool(bin_path),
        "enabled": True,
        "changed_files": [],
        "affected_tests": [],
        "impact_symbols": [],
        "error": "",
    }
    if not bin_path:
        result["error"] = "codegraph binary not found"
        result["enabled"] = False
        return result

    changed = parse_diff_changed_files(diff_text)
    result["changed_files"] = changed
    if not changed:
        result["error"] = "no changed files parsed from diff"
        return result

    ok, err = ensure_indexed(repo_path, bin_path)
    if not ok:
        result["error"] = "index failed: " + err
        result["enabled"] = False
        return result

    # 主数据源：impact（符号级，稳定）。从影响链推导「该跑测试文件」+ 影响范围。
    impact_map = {}
    test_files = []
    for sym in extract_changed_symbols(diff_text):
        for node in get_impact(repo_path, sym, bin_path):
            key = (node.get("filePath"), node.get("name"), node.get("startLine"))
            if key in impact_map:
                continue
            impact_map[key] = node
            fp = node.get("filePath", "") or ""
            if node.get("kind") == "file" and is_test_file(fp) and fp not in test_files:
                test_files.append(fp)
            elif node.get("kind") == "function" and (node.get("name") or "").startswith("test_") \
                    and fp and fp not in test_files:
                test_files.append(fp)
    result["impact_symbols"] = list(impact_map.values())
    result["affected_tests"] = test_files
    return result


if __name__ == "__main__":
    import sys
    poc = sys.argv[1] if len(sys.argv) > 1 else r"D:\trae-code\_cg_poc"
    sample_diff = (
        "diff --git a/app/calculator.py b/app/calculator.py\n"
        "--- a/app/calculator.py\n+++ b/app/calculator.py\n"
        "@@ -4,6 +4,7 @@ def add(a, b):\n"
        " def refund_fee(amount, rate):\n"
        "     # 改动：退款手续费增加四舍五入\n"
        "     platform_fee = 1.0\n"
        "     return round(amount * rate, 2) + platform_fee\n"
    )
    print(json.dumps(analyze_diff_impact(poc, sample_diff), ensure_ascii=False, indent=2))
