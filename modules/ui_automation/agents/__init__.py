"""UI 自动化 Agent 包：闭环探索（observe → act → verify → retry/replan）。"""

from .explore_agent import ExploreAgent

__all__ = ["ExploreAgent"]
