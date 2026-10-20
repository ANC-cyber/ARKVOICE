from __future__ import annotations

import json
import re
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any


NAME_LINE = re.compile(r'^\[name\s*=\s*["\']([^"\']+)["\']\](.*)$')
MULTILINE = re.compile(r'^\[multiline\s*\(\s*name\s*=\s*["\']([^"\']+)["\']\s*\)\](.*)$')


def parse_avg(source: str, language: str = "zh_CN") -> list[dict[str, Any]]:
    """Extract spoken lines from one language's Arknights AVG script."""
    if language not in {"zh_CN", "ja_JP"}:
        raise ValueError("Only zh_CN and ja_JP are supported in the MVP story corpus.")
    lines: list[dict[str, Any]] = []
    for raw in source.lstrip("\ufeff").splitlines():
        match = NAME_LINE.match(raw) or MULTILINE.match(raw)
        if not match:
            continue
        speaker, text = match.groups()
        text = text.strip()
        if not text:
            continue
        lines.append({
            "id": f"YW-ST-2-{len(lines) + 1:04d}",
            "speaker": speaker.strip(),
            "zh_CN": text if language == "zh_CN" else None,
            "ja_JP": text if language == "ja_JP" else None,
        })
    return lines


def normalize_text(text: str) -> str:
    return re.sub(r"[\W_]+", "", text, flags=re.UNICODE).casefold()


def _partial_ratio(left: str, right: str) -> float:
    if not left or not right:
        return 0.0
    shorter, longer = (left, right) if len(left) <= len(right) else (right, left)
    if shorter in longer:
        return 1.0
    if len(shorter) <= 2:
        return SequenceMatcher(None, shorter, longer).ratio()
    matcher = SequenceMatcher(None, shorter, longer, autojunk=False)
    best = 0.0
    for block in matcher.get_matching_blocks():
        start = max(0, block[1] - block[0])
        candidate = longer[start : start + len(shorter)]
        best = max(best, SequenceMatcher(None, shorter, candidate, autojunk=False).ratio())
    return best


def match_line(ocr_text: str, lines: list[dict[str, Any]]) -> tuple[dict[str, Any] | None, float]:
    query = normalize_text(ocr_text)
    if len(query) < 5:
        return None, 0.0
    best_line: dict[str, Any] | None = None
    best_score = 0.0
    for line in lines:
        candidate = normalize_text(str(line.get("zh_CN") or ""))
        if not candidate:
            continue
        score = max(
            SequenceMatcher(None, query, candidate, autojunk=False).ratio(),
            _partial_ratio(query, candidate),
        )
        if score > best_score:
            best_line, best_score = line, score
    return (best_line, best_score) if best_score >= 0.52 else (None, best_score)


def load_story(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload.get("lines"), list):
        raise ValueError(f"剧情文件格式错误：{path}")
    return payload
