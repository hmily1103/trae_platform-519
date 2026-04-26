#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
TDD Agent - 将 TDD 工作流自动化，应用于每次功能构建。

流程：Red（生成/建议测试）→ 运行测试 → Green（可选：根据失败输出建议实现）→ 输出报告。
支持无 LLM 模式：仅运行测试、使用模板生成占位测试；有 LLM 时可用自然语言生成测试与实现建议。
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

# 项目根目录
_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)


@dataclass
class TDDCycleResult:
    """单次 TDD 循环结果"""
    step: str  # red / green / refactor
    test_file: Optional[str] = None
    test_code: Optional[str] = None
    tests_passed: Optional[bool] = None
    tests_output: Optional[str] = None
    implementation_suggestion: Optional[str] = None
    error: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "step": self.step,
            "test_file": self.test_file,
            "test_code": self.test_code,
            "tests_passed": self.tests_passed,
            "tests_output": self.tests_output,
            "implementation_suggestion": self.implementation_suggestion,
            "error": self.error,
        }


class TDDAgent:
    """
    TDD Agent：根据需求描述生成测试、运行测试、根据失败结果建议实现。
    可在每次构建/功能开发时调用（CLI 或 import 后 run_cycle）。
    """

    TESTS_DIR = "tests"
    PYTEST_ARGS = ["-v", "--tb=short"]

    SYSTEM_PROMPT_TESTS = """你是一个 TDD 专家，为 Python 项目编写 pytest 风格的单元测试。

项目约定：
- 测试文件放在 tests/ 目录，命名 test_<模块或功能>.py
- 使用 pytest，类名 Test*，方法名 test_*
- 优先测纯逻辑（无 Flask 请求、无 ADB 等外部依赖）

请根据用户的「需求描述」和「目标模块路径」，只输出一个完整的 Python 测试文件内容。
要求：
1. 只输出代码，不要 markdown 代码块包裹，不要解释
2. 文件内 import 使用绝对 import（如 from shared.unified.orchestrator import create_run）
3. 测试要描述「期望行为」，可先写会失败的断言（Red）
4. 若需求涉及新函数/新接口，测试中直接调用并断言，函数名与行为由需求推断
"""

    SYSTEM_PROMPT_IMPL = """你是一个 TDD 专家，根据 pytest 的失败输出，给出最小实现建议。

用户会提供：1) 需求描述 2) 失败测试的输出 3) 目标模块的代码片段（可选）。

请只输出「建议在目标模块中新增或修改的 Python 代码」，让对应测试通过。要求：
1. 只输出代码，不要 markdown 代码块包裹，不要解释
2. 最小实现，不过度设计
3. 若目标模块是 shared.xxx 或模块路径，代码需符合该模块的现有风格
"""

    def __init__(self, project_root: Optional[str] = None, llm_config_path: Optional[str] = None):
        self.project_root = project_root or _ROOT
        self.llm_config_path = llm_config_path
        self._llm_available: Optional[bool] = None

    def _llm_available_check(self) -> bool:
        if self._llm_available is not None:
            return self._llm_available
        try:
            from utils.llm_client import load_llm_config
            load_llm_config(self.llm_config_path)
            self._llm_available = True
        except Exception:
            self._llm_available = False
        return self._llm_available

    def _call_llm(self, messages: List[Dict[str, str]], timeout: int = 60) -> str:
        try:
            from utils.llm_client import call_llm
            return call_llm(
                messages,
                config_path=self.llm_config_path,
                stream=False,
                timeout=timeout
            )
        except Exception as e:
            raise RuntimeError(f"LLM 调用失败: {e}") from e

    def _module_to_test_file(self, module_path: str) -> str:
        """将模块路径转为建议的测试文件名。如 shared.unified.orchestrator -> test_unified_orchestrator.py"""
        parts = module_path.replace(".", " ").split()
        name = "_".join(p for p in parts if p not in ("shared", "modules", "core"))
        return f"test_{name}.py" if name else "test_module.py"

    def generate_tests(
        self,
        feature_description: str,
        module_path: str,
        existing_code: Optional[str] = None,
    ) -> Tuple[str, str]:
        """
        Red 步骤：根据需求描述生成测试代码。
        :return: (test_file_path, test_code)
        """
        test_file = self._module_to_test_file(module_path)
        test_path = os.path.join(self.project_root, self.TESTS_DIR, test_file)

        if self._llm_available_check():
            try:
                user_content = f"""【需求描述】\n{feature_description}\n\n【目标模块】\n{module_path}\n\n"""
                if existing_code:
                    user_content += f"【现有代码片段（可选参考）】\n```\n{existing_code[:3000]}\n```\n"
                user_content += "\n请直接输出完整测试文件内容（仅代码，无 markdown 包裹）。"
                messages = [
                    {"role": "system", "content": self.SYSTEM_PROMPT_TESTS},
                    {"role": "user", "content": user_content},
                ]
                code = self._call_llm(messages, timeout=90)
                code = code.strip()
                if code.startswith("```"):
                    code = re.sub(r"^```\w*\n?", "", code)
                    code = re.sub(r"\n?```\s*$", "", code)
                return test_path, code
            except Exception as e:
                return test_path, self._template_test(feature_description, module_path, str(e))
        return test_path, self._template_test(feature_description, module_path, None)

    def _template_test(self, feature_description: str, module_path: str, llm_error: Optional[str]) -> str:
        """无 LLM 或 LLM 失败时的占位测试模板"""
        err_note = f"  # LLM 生成失败: {llm_error}\n" if llm_error else ""
        return f'''"""
TDD 占位测试 - 请根据需求补充断言。
需求: {feature_description}
模块: {module_path}
{err_note}
运行: pytest {self.TESTS_DIR}/test_*.py -v
"""
import pytest


class TestGenerated:
    """根据需求补充测试用例"""

    def test_placeholder(self):
        """先写期望行为，再实现（Red -> Green）"""
        # 从 {module_path} 导入并调用，写断言
        pytest.skip("请根据需求描述补充测试逻辑")
'''

    def run_tests(self, test_path_or_pattern: str) -> Tuple[bool, str]:
        """
        运行 pytest，返回 (是否全部通过, 标准输出+标准错误)
        """
        if os.path.isabs(test_path_or_pattern):
            path = test_path_or_pattern
        elif test_path_or_pattern.startswith((self.TESTS_DIR + os.sep, self.TESTS_DIR + "/")):
            path = os.path.join(self.project_root, test_path_or_pattern)
        else:
            path = os.path.join(self.project_root, self.TESTS_DIR, test_path_or_pattern)
        cmd = [sys.executable, "-m", "pytest", path] + self.PYTEST_ARGS
        try:
            result = subprocess.run(
                cmd,
                cwd=self.project_root,
                capture_output=True,
                text=True,
                timeout=120,
            )
            out = (result.stdout or "") + (result.stderr or "")
            return result.returncode == 0, out
        except subprocess.TimeoutExpired:
            return False, "pytest 运行超时"
        except Exception as e:
            return False, str(e)

    def suggest_implementation(
        self,
        failure_output: str,
        feature_description: str,
        module_path: str,
        module_snippet: Optional[str] = None,
    ) -> str:
        """
        Green 步骤（可选）：根据测试失败输出，建议最小实现。
        """
        if not self._llm_available_check():
            return "（未配置 LLM，无法生成实现建议；请根据失败输出手动实现）"
        try:
            user_content = f"""【需求描述】\n{feature_description}\n\n【目标模块】\n{module_path}\n\n【pytest 失败输出】\n```\n{failure_output[:4000]}\n```\n"""
            if module_snippet:
                user_content += f"\n【现有模块代码片段】\n```\n{module_snippet[:2000]}\n```\n"
            user_content += "\n请只输出建议新增/修改的 Python 代码（无 markdown 包裹）。"
            messages = [
                {"role": "system", "content": self.SYSTEM_PROMPT_IMPL},
                {"role": "user", "content": user_content},
            ]
            code = self._call_llm(messages, timeout=90)
            code = code.strip()
            if code.startswith("```"):
                code = re.sub(r"^```\w*\n?", "", code)
                code = re.sub(r"\n?```\s*$", "", code)
            return code
        except Exception as e:
            return f"（实现建议生成失败: {e}）"

    def run_cycle(
        self,
        feature_description: str,
        module_path: str,
        *,
        write_test_file: bool = True,
        run_after_generate: bool = True,
        suggest_on_failure: bool = True,
        existing_code: Optional[str] = None,
    ) -> TDDCycleResult:
        """
        执行一次 TDD 循环：生成测试 →（可选）写入文件 → 运行测试 →（若失败且启用）建议实现。
        """
        result = TDDCycleResult(step="red")
        try:
            test_path, test_code = self.generate_tests(
                feature_description, module_path, existing_code
            )
            result.test_file = test_path
            result.test_code = test_code

            if write_test_file:
                os.makedirs(os.path.dirname(test_path), exist_ok=True)
                with open(test_path, "w", encoding="utf-8") as f:
                    f.write(test_code)

            if run_after_generate:
                passed, output = self.run_tests(test_path)
                result.tests_passed = passed
                result.tests_output = output
                result.step = "green" if passed else "red"

                if not passed and suggest_on_failure:
                    snippet = None
                    try:
                        mod_file = module_path.replace(".", os.sep) + ".py"
                        full = os.path.join(self.project_root, mod_file)
                        if os.path.isfile(full):
                            with open(full, "r", encoding="utf-8") as f:
                                snippet = f.read(4000)
                    except Exception:
                        pass
                    result.implementation_suggestion = self.suggest_implementation(
                        output, feature_description, module_path, snippet
                    )
        except Exception as e:
            result.error = str(e)
        return result


def main():
    """CLI：在每次功能构建时可调用。"""
    import argparse
    parser = argparse.ArgumentParser(description="TDD Agent：根据需求生成测试、运行测试、建议实现")
    parser.add_argument("feature", nargs="?", default="", help="需求描述，例如：新增 run_has_performance_monitor 函数")
    parser.add_argument("--module", "-m", default="shared.unified.orchestrator", help="目标模块路径")
    parser.add_argument("--no-write", action="store_true", help="只生成测试代码，不写入文件")
    parser.add_argument("--no-run", action="store_true", help="生成后不运行 pytest")
    parser.add_argument("--no-suggest", action="store_true", help="测试失败时不调用 LLM 建议实现")
    parser.add_argument("--run-only", type=str, metavar="PATH", help="仅运行指定测试路径，不生成")
    args = parser.parse_args()

    agent = TDDAgent()
    if args.run_only:
        passed, out = agent.run_tests(args.run_only)
        print(out)
        sys.exit(0 if passed else 1)
    if not args.feature:
        parser.print_help()
        sys.exit(0)

    result = agent.run_cycle(
        args.feature,
        args.module,
        write_test_file=not args.no_write,
        run_after_generate=not args.no_run,
        suggest_on_failure=not args.no_suggest,
    )
    if result.error:
        print("Error:", result.error, file=sys.stderr)
        sys.exit(1)
    if result.test_file:
        print("Test file:", result.test_file)
    if result.tests_passed is not None:
        print("Tests passed:", result.tests_passed)
    if result.tests_output:
        print(result.tests_output)
    if result.implementation_suggestion:
        print("\n--- 实现建议 ---\n", result.implementation_suggestion)
    sys.exit(0 if result.tests_passed else 1)


if __name__ == "__main__":
    main()
