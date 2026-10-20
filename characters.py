from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent
CHARACTER_CONFIG = ROOT / "data" / "characters.json"


def _normalize_speaker(value: str) -> str:
    return re.sub(r"[\s\u3000·・_-]+", "", str(value or "").casefold())


@lru_cache(maxsize=1)
def _load_config() -> dict[str, Any]:
    return json.loads(CHARACTER_CONFIG.read_text(encoding="utf-8-sig"))


def resolve_character(speaker: str, text: str = "") -> dict[str, Any]:
    """Resolve story speaker, sprite, and voice reference from the editable registry."""
    config = _load_config()
    characters = config.get("characters", {})
    normalized = _normalize_speaker(speaker)
    character_id = str(config.get("default_character", ""))
    for candidate_id, entry in characters.items():
        aliases = entry.get("speaker_aliases", [])
        if any(_normalize_speaker(alias) == normalized for alias in aliases):
            character_id = candidate_id
            break
    entry = characters.get(character_id) or {}

    candidates = entry.get("reference_candidates", [])
    default_id = str(entry.get("default_reference_id", ""))
    selected = next((item for item in candidates if item.get("id") == default_id), None)
    normalized_text = str(text or "").casefold()
    best_score = 0
    for item in candidates:
        score = sum(1 for keyword in item.get("selection_keywords", []) if str(keyword).casefold() in normalized_text)
        if score > best_score:
            selected, best_score = item, score
    if selected is None:
        selected = {}

    def resolve_path(value: str) -> str:
        if not value:
            return ""
        path = Path(value)
        return str(path if path.is_absolute() else (ROOT / path).resolve())

    reference_audio = resolve_path(str(selected.get("audio", entry.get("reference_audio", ""))))
    sprite = resolve_path(str(entry.get("sprite", "")))
    return {
        "character_id": character_id,
        "display_name": str(entry.get("display_name", character_id)),
        "speaker": str(speaker or "旁白"),
        "sprite_path": str(sprite),
        "reference_audio": str(reference_audio),
        "reference_text": str(selected.get("japanese_text", entry.get("reference_text", ""))),
        "reference_id": str(selected.get("id", default_id)),
        "emotion_label": str(selected.get("emotion_label", "neutral")),
    }


def list_characters() -> dict[str, Any]:
    """Return display-facing character metadata without exposing local reference paths."""
    config = _load_config()
    return {
        "default_character": config.get("default_character", ""),
        "speakers": {
            alias: character_id
            for character_id, entry in config.get("characters", {}).items()
            for alias in entry.get("speaker_aliases", [])
        },
        "characters": {
            character_id: {
                "display_name": entry.get("display_name", character_id),
                "sprite": entry.get("sprite", ""),
                "reference_count": len(entry.get("reference_candidates", [])),
            }
            for character_id, entry in config.get("characters", {}).items()
        },
    }
