from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import re
import socket
import subprocess
import threading
import time
import wave
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from story_data import load_story
from characters import list_characters, resolve_character
from window_overlay import GameWindowOverlay


ROOT = Path(os.environ.get("ARKVOICE_MVP_ROOT", Path(__file__).resolve().parent))
WORKSPACE = ROOT.parent
SOVITS_ROOT = Path(os.environ.get("ARKVOICE_SOVITS_DIR", str(ROOT / "external" / "GPT-SoVITS")))
SOVITS_PYTHON = SOVITS_ROOT / "runtime" / "python.exe"
SOVITS_API = SOVITS_ROOT / "api_v2.py"
SOVITS_CONFIG = Path(
    os.environ.get("ARKVOICE_TTS_CONFIG", str(ROOT / "models" / "v5turbo.yaml"))
)
REFERENCE_AUDIO = Path(
    os.environ.get("ARKVOICE_REFERENCE_AUDIO", str(ROOT / "voices" / "reference.wav"))
)
REFERENCE_TEXT = os.environ.get("ARKVOICE_REFERENCE_TEXT", "").strip()
if not REFERENCE_TEXT:
    reference_text_file = Path(os.environ.get("ARKVOICE_REFERENCE_TEXT_FILE", str(ROOT / "voices" / "reference.txt")))
    try:
        REFERENCE_TEXT = reference_text_file.read_text(encoding="utf-8-sig").strip()
    except OSError:
        REFERENCE_TEXT = ""
TTS_HOST = "127.0.0.1"
TTS_PORT = int(os.environ.get("ARKVOICE_TTS_PORT", "9887"))
TTS_URL = f"http://{TTS_HOST}:{TTS_PORT}/tts"
LOG_DIR = ROOT / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)
OVERLAY_IMAGE = ROOT / "assets" / "character_chibi_leaning_clean.png"
overlay_manager = GameWindowOverlay(OVERLAY_IMAGE)

def _port_open(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=0.4):
            return True
    except OSError:
        return False


tts_process: subprocess.Popen[bytes] | None = None
tts_log_handle: Any = None


def _ensure_tts_api() -> None:
    global tts_process, tts_log_handle
    if _port_open(TTS_HOST, TTS_PORT):
        return
    for required in (SOVITS_PYTHON, SOVITS_API, SOVITS_CONFIG, REFERENCE_AUDIO):
        if not required.exists():
            raise RuntimeError(f"找不到 GPT-SoVITS 文件：{required}")

    log_path = LOG_DIR / "gpt-sovits-api.log"
    tts_log_handle = log_path.open("ab", buffering=0)
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    tts_process = subprocess.Popen(
        [
            str(SOVITS_PYTHON),
            "-I",
            str(SOVITS_API),
            "-a",
            TTS_HOST,
            "-p",
            str(TTS_PORT),
            "-c",
            str(SOVITS_CONFIG),
        ],
        cwd=str(SOVITS_ROOT),
        stdin=subprocess.DEVNULL,
        stdout=tts_log_handle,
        stderr=subprocess.STDOUT,
        creationflags=creationflags,
    )
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        if _port_open(TTS_HOST, TTS_PORT):
            return
        if tts_process.poll() is not None:
            tail = log_path.read_text(encoding="utf-8", errors="replace")[-3000:]
            raise RuntimeError(f"GPT-SoVITS 启动失败。日志末尾：\n{tail}")
        time.sleep(0.4)
    raise RuntimeError("GPT-SoVITS 启动超时，请查看 arkvoice_mvp/logs/gpt-sovits-api.log。")


def _shutdown_tts_api() -> None:
    global tts_process, tts_log_handle
    if tts_process is not None and tts_process.poll() is None:
        tts_process.terminate()
        try:
            tts_process.wait(timeout=8)
        except subprocess.TimeoutExpired:
            tts_process.kill()
    if tts_log_handle is not None:
        tts_log_handle.close()
        tts_log_handle = None


@asynccontextmanager
async def lifespan(_: FastAPI):
    overlay_manager.start()
    try:
        _ensure_tts_api()
        yield
    finally:
        overlay_manager.set_active(False)
        overlay_manager.stop()
        _shutdown_tts_api()


app = FastAPI(title="ArkVoice MVP", version="0.1.0", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")


class StoryLineRequest(BaseModel):
    stage_id: str = ""
    line_index: int


class OverlayStateRequest(BaseModel):
    active: bool


def _normalize_tts_text(text: str) -> str:
    """Map long ellipses to a Japanese pause mark understood by the local TTS model."""
    return re.sub(r"(?:…|⋯|\.{3,}|·{2,})+", "、", text.strip())


def _silence_wav(seconds: float = 0.7) -> bytes:
    output = io.BytesIO()
    with wave.open(output, "wb") as silence:
        silence.setnchannels(1)
        silence.setsampwidth(2)
        silence.setframerate(32000)
        silence.writeframes(b"\x00\x00" * int(32000 * seconds))
    return output.getvalue()


def _synthesize(
    text: str,
    speed_factor: float = 1.0,
    character_profile: dict[str, Any] | None = None,
) -> tuple[bytes, float, float]:
    normalized_text = _normalize_tts_text(text)
    if not any(char.isalnum() for char in normalized_text):
        # A dialogue line containing only an ellipsis is a dramatic pause, not a
        # speakable utterance. Keep its place in the dialogue sequence as silence.
        return _silence_wav(), 0.0, 0.0
    profile = character_profile or {}
    reference_audio = Path(profile.get("reference_audio") or REFERENCE_AUDIO)
    reference_text = str(profile.get("reference_text") or REFERENCE_TEXT)
    request = {
        "text": normalized_text,
        "text_lang": "ja",
        "ref_audio_path": str(reference_audio),
        "prompt_lang": "ja",
        "prompt_text": reference_text,
        "text_split_method": "cut0",
        "media_type": "wav",
        "streaming_mode": False,
        "sample_steps": 4,
        "speed_factor": min(1.5, max(0.6, float(speed_factor))),
    }
    with _tts_lock:
        started = time.perf_counter()
        response = httpx.post(TTS_URL, json=request, timeout=180.0)
        response.raise_for_status()
        synthesis_seconds = time.perf_counter() - started
        return response.content, synthesis_seconds, 0.0


STORY_DIR = ROOT / "data" / "stories"
STORY_AUDIO_CACHE = LOG_DIR / "story-audio-cache"
_tts_lock = threading.Lock()
_prefetch_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="arkvoice-prefetch")
_story_futures: dict[tuple[str, str], Future[dict[str, Any]]] = {}
_story_futures_lock = threading.RLock()


def _story_for(stage_id: str) -> dict[str, Any] | None:
    if not stage_id:
        return None
    # Stage IDs are file stems; never accept an arbitrary path from the browser.
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", stage_id):
        return None
    path = STORY_DIR / f"{stage_id}.json"
    if not path.is_file():
        return None
    try:
        return load_story(path)
    except (OSError, ValueError, json.JSONDecodeError):
        return None


def _audio_cache_key(
    stage_id: str,
    line: dict[str, Any],
    japanese: str,
    speed_factor: float = 1.0,
    character_profile: dict[str, Any] | None = None,
) -> str:
    profile = character_profile or resolve_character(str(line.get("speaker") or ""), japanese)
    reference_audio = Path(profile.get("reference_audio") or REFERENCE_AUDIO)
    ref_sig = ""
    try:
        stat = reference_audio.stat()
        ref_sig = f"{reference_audio.resolve()}:{stat.st_size}:{stat.st_mtime_ns}"
    except OSError:
        ref_sig = str(reference_audio)
    config_sig = ""
    try:
        config_stat = SOVITS_CONFIG.stat()
        config_sig = f"{SOVITS_CONFIG.resolve()}:{config_stat.st_size}:{config_stat.st_mtime_ns}"
    except OSError:
        config_sig = str(SOVITS_CONFIG)
    value = json.dumps(
        [
            stage_id, line.get("id"), japanese, profile.get("character_id"),
            profile.get("reference_id"), ref_sig, config_sig, float(speed_factor),
            "raw-audio-no-reference-trim-v1",
        ],
        ensure_ascii=False,
    )
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _build_story_line(stage_id: str, line: dict[str, Any]) -> dict[str, Any]:
    japanese = str(line.get("ja_JP") or "").strip()
    if not japanese:
        raise ValueError(f"{line.get('id', '')} 缺少日文原文；已停用翻译，无法生成该句语音。")

    profile = resolve_character(str(line.get("speaker") or ""), japanese)
    audio_key = _audio_cache_key(stage_id, line, japanese, character_profile=profile)
    cache_path = STORY_AUDIO_CACHE / f"{audio_key}.wav"
    if cache_path.is_file():
        audio = cache_path.read_bytes()
        synthesis_seconds, trimmed_seconds, cache_hit = 0.0, 0.0, True
    else:
        audio, synthesis_seconds, trimmed_seconds = _synthesize(japanese, character_profile=profile)
        STORY_AUDIO_CACHE.mkdir(parents=True, exist_ok=True)
        cache_path.write_bytes(audio)
        cache_hit = False
    return {
        "japanese_text": japanese,
        "audio": audio,
        "synthesis_seconds": synthesis_seconds,
        "trimmed_reference_seconds": trimmed_seconds,
        "cache_hit": cache_hit,
        "speaker": line.get("speaker", ""),
        "character_id": profile["character_id"],
        "character_name": profile["display_name"],
        "sprite_path": profile["sprite_path"],
        "reference_id": profile["reference_id"],
        "emotion_label": profile["emotion_label"],
        "line_id": line.get("id", ""),
    }


def _schedule_story_line(stage_id: str, line: dict[str, Any]) -> Future[dict[str, Any]]:
    key = (stage_id, str(line.get("id", "")))
    with _story_futures_lock:
        future = _story_futures.get(key)
        if future is None:
            future = _prefetch_pool.submit(_build_story_line, stage_id, line)
            _story_futures[key] = future
            future.add_done_callback(lambda done, k=key: _forget_story_future(k, done))
        return future


def _forget_story_future(key: tuple[str, str], future: Future[dict[str, Any]]) -> None:
    with _story_futures_lock:
        if _story_futures.get(key) is future:
            del _story_futures[key]


def _schedule_following_lines(story: dict[str, Any], current_line: dict[str, Any], count: int = 2) -> int:
    lines = story.get("lines", [])
    try:
        index = next(i for i, item in enumerate(lines) if item.get("id") == current_line.get("id"))
    except StopIteration:
        return 0
    scheduled = 0
    for line in lines[index + 1 : index + 1 + count]:
        if not str(line.get("ja_JP") or "").strip():
            continue
        _schedule_story_line(str(story["stage_id"]), line)
        scheduled += 1
    return scheduled


def _save_audio(audio: bytes) -> Path:
    audio_path = LOG_DIR / "latest.wav"
    audio_path.write_bytes(audio)
    return audio_path


@app.get("/")
def index() -> FileResponse:
    return FileResponse(ROOT / "static" / "index.html")


@app.get("/api/health")
def health() -> dict[str, Any]:
    tts_ready = _port_open(TTS_HOST, TTS_PORT)
    return {
        "ok": tts_ready,
        "ocr": "已暂停；使用台词框像素变化",
        "tts": "GPT-SoVITS v5turbo",
        "translator": "已停用；仅使用剧情日文原文",
        "tts_ready": tts_ready,
        "reference_prefix_trim_seconds": 0.0,
        "overlay": overlay_manager.get_status(),
    }


@app.get("/api/overlay/status")
def overlay_status() -> dict[str, Any]:
    return overlay_manager.get_status()


@app.post("/api/overlay")
def set_overlay_state(request: OverlayStateRequest) -> dict[str, Any]:
    overlay_manager.set_active(request.active)
    return overlay_manager.get_status()


@app.get("/api/stories")
def stories() -> dict[str, Any]:
    available = []
    if STORY_DIR.is_dir():
        for path in sorted(STORY_DIR.glob("*.json")):
            story = _story_for(path.stem)
            if story:
                available.append({
                    "stage_id": story.get("stage_id", path.stem),
                    "event_id": story.get("event_id", ""),
                    "title": story.get("title", path.stem),
                    "line_count": len(story.get("lines", [])),
                    "japanese_line_count": sum(bool(str(line.get("ja_JP") or "").strip()) for line in story.get("lines", [])),
                    "languages": story.get("languages", ["zh_CN", "ja_JP"]),
                })
    return {"stories": available}


@app.get("/api/characters")
def characters() -> dict[str, Any]:
    return list_characters()


@app.post("/api/story-line")
def speak_story_line(request: StoryLineRequest) -> dict[str, Any]:
    story = _story_for(request.stage_id)
    if not story:
        raise HTTPException(status_code=404, detail=f"没有找到剧情脚本：{request.stage_id}")
    lines = story.get("lines", [])
    if request.line_index < 0 or request.line_index >= len(lines):
        raise HTTPException(status_code=404, detail="剧情已经播放到最后一句。")
    line = lines[request.line_index]
    if not str(line.get("ja_JP") or "").strip():
        raise HTTPException(
            status_code=409,
            detail=f"{line.get('id', '')} 尚无日文原文。在线翻译已停用；导入该关卡的日文脚本后才能配音。",
        )
    try:
        prepared = _schedule_story_line(request.stage_id, line).result()
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"日文原文配音失败：{exc}") from exc
    prefetch_scheduled = _schedule_following_lines(story, line)
    audio_path = _save_audio(prepared["audio"])
    return {
        "stage_id": request.stage_id,
        "story_title": story.get("title", request.stage_id),
        "line_index": request.line_index,
        "line_count": len(lines),
        "line_id": prepared["line_id"],
        "speaker": prepared["speaker"],
        "character_id": prepared["character_id"],
        "character_name": prepared["character_name"],
        "reference_id": prepared["reference_id"],
        "emotion_label": prepared["emotion_label"],
        "japanese_text": prepared["japanese_text"],
        "audio_base64": base64.b64encode(prepared["audio"]).decode("ascii"),
        "audio_mime": "audio/wav",
        "audio_cache_hit": prepared["cache_hit"],
        "prefetch_scheduled": prefetch_scheduled,
        "trimmed_reference_seconds": round(prepared["trimmed_reference_seconds"], 3),
        "synthesis_seconds": round(prepared["synthesis_seconds"], 2),
        "saved_audio": str(audio_path),
    }


