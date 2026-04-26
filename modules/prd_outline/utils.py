# -*- coding: utf-8 -*-
import re
from typing import Any, List


def to_list(value: Any) -> List[str]:
    if isinstance(value, list):
        return [str(x).strip() for x in value if str(x).strip()]
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    return []


def split_lines(text: str) -> List[str]:
    return [str(x).strip() for x in str(text or "").splitlines() if str(x).strip()]


def clean_text(text: str) -> str:
    s = str(text or "").strip()
    s = re.sub(r"[\x00-\x1f\x7f-\x9f]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s
