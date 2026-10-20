from __future__ import annotations

import base64
import ctypes
import hashlib
import json
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from typing import Any
from ctypes import wintypes

ROOT = Path(__file__).resolve().parent
for candidate in (ROOT / ".deps", ROOT.parent / "work" / "arkvoice-mvp-deps", ROOT):
    if candidate.exists():
        sys.path.insert(0, str(candidate))
os.environ.setdefault("ARKVOICE_MVP_ROOT", str(ROOT))

import httpx  # noqa: E402
from PIL import Image, ImageChops, ImageEnhance, ImageFilter, ImageGrab, ImageOps, ImageStat  # noqa: E402

import server  # noqa: E402
from story_data import match_line  # noqa: E402
from window_overlay import GameWindowOverlay, _find_arknights_window  # noqa: E402


CONFIG_PATH = ROOT / "logs" / "console-settings.json"
STATE_PATH = ROOT / "logs" / "console-state.json"
TRANSLATION_CACHE_PATH = ROOT / "logs" / "translation-cache.json"
SETTINGS_SCRIPT = ROOT / "settings.ps1"
SETTINGS_LOG = ROOT / "logs" / "settings-window.log"
RUNTIME_LOG = ROOT / "logs" / "console-runtime.log"
DEFAULT_CONFIG: dict[str, Any] = {
    "stage_id": "MAIN-14-01",
    "api_base": "https://api.openai.com/v1/chat/completions",
    "model": "gpt-4o-mini",
    "api_key_protected": "",
    "speed": 1.0,
    "dialogue_bottom_percent": 10,
    "change_threshold": 0.0008,
    "listening": False,
}


def _read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return default


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(path.suffix + ".tmp")
    temp_path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temp_path.replace(path)


def _read_text_tail(path: Path, limit: int = 160) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace").strip()
        return text[-limit:].replace("\r", " ").replace("\n", " ")
    except OSError:
        return ""


def read_config() -> dict[str, Any]:
    config = dict(DEFAULT_CONFIG)
    config.update(_read_json(CONFIG_PATH, {}))
    config["speed"] = min(1.5, max(0.6, float(config.get("speed", 1.0))))
    config["dialogue_bottom_percent"] = min(30, max(5, int(config.get("dialogue_bottom_percent", 10))))
    config["change_threshold"] = min(0.03, max(0.0001, float(config.get("change_threshold", 0.0008))))
    return config


class Runtime:
    def __init__(self) -> None:
        self.overlay = GameWindowOverlay(
            ROOT / "assets" / "character_chibi_leaning_clean.png",
            on_click=self.open_settings,
            on_previous=lambda: self._navigate_story(-1),
            on_next=lambda: self._navigate_story(1),
        )
        self.stop_event = threading.Event()
        self.audio_queue: queue.Queue[tuple[int, int, tuple[str, ...], Future[dict[str, Any]], float] | None] = queue.Queue()
        self.prepare_pool = ThreadPoolExecutor(max_workers=3, thread_name_prefix="arkvoice-prepare")
        self.config_lock = threading.RLock()
        self.state_lock = threading.RLock()
        self.navigation_lock = threading.RLock()
        self.prepare_lock = threading.RLock()
        self.translation_lock = threading.RLock()
        self.playback_lock = threading.RLock()
        self.state: dict[str, Any] = {"status": "等待明日方舟窗口", "listening": False, "line": "", "updated_at": time.time()}
        self.translation_cache = _read_json(TRANSLATION_CACHE_PATH, {})
        self.prepared_futures: dict[tuple[str, ...], Future[dict[str, Any]]] = {}
        self.playback_revision = 0
        self.initial_position_pending = False
        self.ocr_position_unknown = False
        self.ocr_engine: Any = None
        self.last_config_mtime = 0
        self.line_index = -1
        self.active_story: dict[str, Any] | None = None
        self.sample_baseline: Image.Image | None = None
        self.sample_candidate: Image.Image | None = None
        self.last_dialogue_active: bool | None = None
        self.stable_count = 0
        self.last_line_trigger = 0.0
        self.mouse_left_down = False
        self.pending_game_clicks = 0
        self.game_click_count = 0
        self.seconds_per_reveal_character = 0.045
        self.visual_line_index: int | None = None
        self.visual_line_char_count = 0
        self.visual_reveal_started_at = 0.0
        self.visual_last_change_at = 0.0
        self.visual_had_change = False
        self.visual_calibrate_from_click = False
        self.visual_suppress_until = 0.0
        self.visual_monitor_deadline = 0.0
        self._settings_lock = threading.Lock()
        self._settings_process: subprocess.Popen[bytes] | None = None
        self._settings_log_handle: Any = None

    def open_settings(self) -> None:
        with self._settings_lock:
            if self._settings_process is not None and self._settings_process.poll() is None:
                return
            try:
                if self._settings_log_handle is not None:
                    self._settings_log_handle.close()
                powershell = shutil.which("powershell.exe") or str(Path(os.environ.get("WINDIR", r"C:\Windows")) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe")
                self._settings_log_handle = SETTINGS_LOG.open("ab", buffering=0)
                self._settings_process = subprocess.Popen(
                    [powershell, "-NoProfile", "-STA", "-ExecutionPolicy", "Bypass", "-File", str(SETTINGS_SCRIPT)],
                    cwd=str(ROOT), stdin=subprocess.DEVNULL, stdout=self._settings_log_handle, stderr=subprocess.STDOUT,
                )
            except OSError as exc:
                self.set_status(f"参数窗口启动失败：{exc}")

    def set_status(self, message: str, line: str | None = None, listening: bool | None = None) -> None:
        with self.state_lock:
            if self.state.get("status") == message and (line is None or self.state.get("line") == line) and (listening is None or self.state.get("listening") == listening):
                return
            if line is not None:
                self.state["line"] = line
            if listening is not None:
                self.state["listening"] = listening
            self.state["status"] = message
            self.state["updated_at"] = time.time()
            state_copy = dict(self.state)
        _write_json(STATE_PATH, state_copy)
        entry = f"[{time.strftime('%H:%M:%S')}] {message}" + (f" | {state_copy['line']}" if state_copy.get("line") else "")
        print(entry, flush=True)
        self.overlay.append_log_entry(entry)

    @staticmethod
    def _log_runtime(message: str) -> None:
        try:
            with RUNTIME_LOG.open("a", encoding="utf-8") as log:
                log.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}\n")
        except OSError:
            pass

    def _decrypt_api_key(self, protected: str) -> str:
        if not protected:
            return ""
        if os.name != "nt":
            return ""
        class DATA_BLOB(ctypes.Structure):
            _fields_ = [("cbData", ctypes.c_ulong), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]
        raw = base64.b64decode(protected)
        buffer = (ctypes.c_ubyte * len(raw)).from_buffer_copy(raw)
        blob = DATA_BLOB(len(raw), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
        output = DATA_BLOB()
        crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        crypt32.CryptUnprotectData.argtypes = [ctypes.POINTER(DATA_BLOB), ctypes.c_void_p, ctypes.POINTER(DATA_BLOB), ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32, ctypes.POINTER(DATA_BLOB)]
        crypt32.CryptUnprotectData.restype = ctypes.c_int
        kernel32.LocalFree.argtypes = [ctypes.c_void_p]
        kernel32.LocalFree.restype = ctypes.c_void_p
        if not crypt32.CryptUnprotectData(ctypes.byref(blob), None, None, None, None, 0x1, ctypes.byref(output)):
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            return ctypes.string_at(output.pbData, output.cbData).decode("utf-8")
        finally:
            kernel32.LocalFree(output.pbData)

    def translate(self, stage_id: str, line: dict[str, Any], config: dict[str, Any]) -> tuple[str, bool]:
        japanese = str(line.get("ja_JP") or "").strip()
        if japanese:
            return japanese, True
        chinese = str(line.get("zh_CN") or "").strip()
        if not chinese:
            raise ValueError("该剧情行没有中文文本。")
        cache_key = hashlib.sha256(f"{stage_id}\0{line.get('id')}\0{chinese}".encode("utf-8")).hexdigest()
        with self.translation_lock:
            cached = self.translation_cache.get(cache_key)
        if cached:
            return cached, True
        api_key = self._decrypt_api_key(str(config.get("api_key_protected") or ""))
        if not api_key:
            raise RuntimeError("请点击小人配置翻译 API Key。")
        endpoint = str(config.get("api_base") or DEFAULT_CONFIG["api_base"]).strip()
        model = str(config.get("model") or DEFAULT_CONFIG["model"]).strip()
        prompt = (
            "你是明日方舟剧情本地化译者。把下面中文台词翻成自然的日语游戏台词，保留专名、语气和标点；"
            "只返回译文，不要解释，不要添加说话人名称。\n\n" + chinese
        )
        response = httpx.post(
            endpoint,
            headers={"Authorization": f"Bearer {api_key}"},
            json={"model": model, "messages": [{"role": "user", "content": prompt}], "temperature": 0.25},
            timeout=45.0,
        )
        response.raise_for_status()
        translated = str(response.json()["choices"][0]["message"]["content"]).strip().strip("「」『』\"'")
        if not translated:
            raise RuntimeError("翻译 API 返回了空文本。")
        with self.translation_lock:
            self.translation_cache[cache_key] = translated
            _write_json(TRANSLATION_CACHE_PATH, self.translation_cache)
        return translated, False

    def prepare_line(self, stage_id: str, line: dict[str, Any], config: dict[str, Any], report_progress: bool = True) -> dict[str, Any]:
        if report_progress:
            self.set_status("正在翻译中", f"{line.get('speaker') or '旁白'} · {line.get('id')}")
        translation_started = time.perf_counter()
        japanese, cache_hit = self.translate(stage_id, line, config)
        translation_seconds = time.perf_counter() - translation_started
        profile = server.resolve_character(str(line.get("speaker") or ""), japanese)
        speed = float(config.get("speed", 1.0))
        audio_key = server._audio_cache_key(stage_id, line, japanese, speed, profile)
        cache_path = server.STORY_AUDIO_CACHE / f"{audio_key}.wav"
        if cache_path.is_file():
            synthesis_seconds = 0.0
            audio_cache_hit = True
        else:
            if report_progress:
                self.set_status("正在生成语音", japanese)
            audio, synthesis_seconds, _trimmed = server._synthesize(
                japanese,
                speed_factor=speed,
                character_profile=profile,
            )
            server.STORY_AUDIO_CACHE.mkdir(parents=True, exist_ok=True)
            cache_path.write_bytes(audio)
            audio_cache_hit = False
        return {
            "audio_path": str(cache_path),
            "japanese": japanese,
            "speaker": line.get("speaker") or "旁白",
            "id": line.get("id", ""),
            "character_id": profile["character_id"],
            "character_name": profile["display_name"],
            "sprite_path": profile["sprite_path"],
            "reference_id": profile["reference_id"],
            "emotion_label": profile["emotion_label"],
            "synthesis_seconds": synthesis_seconds,
            "translation_seconds": translation_seconds,
            "translation_cache_hit": cache_hit,
            "audio_cache_hit": audio_cache_hit,
        }

    def _stop_current_audio(self) -> None:
        try:
            import winsound
            # SND_PURGE is unsupported on modern Windows; None with no flags
            # stops the waveform currently playing in this process.
            winsound.PlaySound(None, 0)
        except Exception:
            pass

    def _cancel_queued_playback(self) -> None:
        with self.playback_lock:
            self._stop_current_audio()
            with self.state_lock:
                self.playback_revision += 1
        while True:
            try:
                self.audio_queue.get_nowait()
            except queue.Empty:
                break

    @staticmethod
    def _prepare_key(stage_id: str, line: dict[str, Any], config: dict[str, Any]) -> tuple[str, ...]:
        return (
            stage_id,
            str(line.get("id", "")),
            str(line.get("speaker", "")),
            str(line.get("zh_CN") or ""),
            str(line.get("ja_JP") or ""),
            str(config.get("api_base", "")),
            str(config.get("model", "")),
            str(config.get("api_key_protected", "")),
            str(float(config.get("speed", 1.0))),
        )

    def _schedule_preparation(
        self, stage_id: str, line: dict[str, Any], config: dict[str, Any], report_progress: bool = False
    ) -> tuple[tuple[str, ...], Future[dict[str, Any]]]:
        key = self._prepare_key(stage_id, line, config)
        with self.prepare_lock:
            future = self.prepared_futures.get(key)
            if future is None:
                future = self.prepare_pool.submit(self.prepare_line, stage_id, line, dict(config), report_progress)
                self.prepared_futures[key] = future
            return key, future

    def _recognize_current_line(
        self, game: dict[str, Any], story: dict[str, Any]
    ) -> tuple[dict[str, Any] | None, float, str]:
        if self.ocr_engine is None:
            from rapidocr import RapidOCR
            self.ocr_engine = RapidOCR()
        x, y, width, height = game["client"]
        # OCR the dialogue band between 20% and 5% above the client area's bottom.
        crop_top = y + round(height * 0.80)
        crop_bottom = y + round(height * 0.95)
        image = ImageGrab.grab(bbox=(x, crop_top, x + width, crop_bottom)).convert("RGB")
        original_size = image.size
        # Dialogue fonts are small against the dark box; enlarge and increase contrast
        # before OCR so startup matching also works at lower game resolutions.
        image = ImageOps.autocontrast(image.convert("L")).convert("RGB")
        image = ImageEnhance.Contrast(image).enhance(1.6).filter(ImageFilter.SHARPEN)
        image = image.resize((image.width * 2, image.height * 2), Image.Resampling.LANCZOS)
        result = self.ocr_engine(image)
        text_parts = [str(text).strip() for text in (getattr(result, "txts", None) or ()) if str(text).strip()]
        ocr_text = "".join(text_parts)
        scores = getattr(result, "scores", None) or ()
        self._log_runtime(
            f"startup OCR capture band=bottom20%-5% client={game['client']} crop={original_size} enhanced={image.size} "
            f"boxes={len(text_parts)} scores={[round(float(s), 3) for s in scores]}"
        )
        if not text_parts:
            try:
                debug_path = ROOT / "logs" / "ocr-startup-crop.png"
                debug_path.parent.mkdir(parents=True, exist_ok=True)
                image.save(debug_path)
                self._log_runtime(f"startup OCR empty; diagnostic crop saved={debug_path}")
            except OSError as exc:
                self._log_runtime(f"startup OCR diagnostic save failed: {exc}")
        candidates = text_parts + ([ocr_text] if len(text_parts) > 1 else [])
        best_line: dict[str, Any] | None = None
        best_score = 0.0
        for candidate in candidates:
            matched, score = match_line(candidate, story.get("lines", []))
            if matched is not None and score > best_score:
                best_line, best_score = matched, score
        return best_line, best_score, ocr_text

    def _audio_worker(self) -> None:
        import winsound
        while not self.stop_event.is_set():
            item = self.audio_queue.get()
            if item is None:
                break
            revision, index, key, future, queued_at = item
            try:
                was_ready = future.done()
                prepared = future.result()
                with self.prepare_lock:
                    if self.prepared_futures.get(key) is future:
                        self.prepared_futures.pop(key, None)
                wait_seconds = max(0.0, time.monotonic() - queued_at)
                self._log_runtime(
                    f"line={prepared['id']} ready_after_click={wait_seconds:.3f}s "
                    f"translation={prepared['translation_seconds']:.3f}s "
                    f"tts={prepared['synthesis_seconds']:.3f}s "
                    f"character={prepared['character_id']} reference={prepared['reference_id']} "
                    f"emotion={prepared['emotion_label']} "
                    f"translation_cache={prepared['translation_cache_hit']} "
                    f"audio_cache={prepared['audio_cache_hit']} prefetched={was_ready}"
                )
                with self.playback_lock:
                    with self.state_lock:
                        is_current = revision == self.playback_revision
                    if not is_current:
                        continue
                    self.overlay.set_image(Path(prepared["sprite_path"]))
                    self.set_status("正在播放", f"{prepared['speaker']} · {prepared['id']} · {prepared['japanese']}")
                    winsound.PlaySound(
                        prepared["audio_path"],
                        winsound.SND_FILENAME | winsound.SND_ASYNC,
                    )
            except Exception as exc:
                with self.prepare_lock:
                    if self.prepared_futures.get(key) is future:
                        self.prepared_futures.pop(key, None)
                self._log_runtime(f"line_index={index} preparation failed: {exc}")
                with self.state_lock:
                    is_current = revision == self.playback_revision
                if is_current:
                    self.set_status(f"处理失败：{str(exc)[:44]}", f"第 {index + 1} 句")
        self.set_status("已停止", listening=False)

    def _sample_dialogue(self, game: dict[str, Any], bottom_percent: int) -> Image.Image | None:
        x, y, width, height = game["client"]
        crop_height = max(30, int(height * bottom_percent / 100))
        if width < 5 or crop_height < 5:
            return None
        screen = ImageGrab.grab(bbox=(x, y + height - crop_height, x + width, y + height)).convert("L")
        sample = screen.resize((480, 64), Image.Resampling.BILINEAR)
        return sample

    @staticmethod
    def _dialogue_coverage(sample: Image.Image) -> tuple[float, float, bool]:
        histogram = sample.histogram()
        total = max(1, sum(histogram))
        dark_ratio = sum(histogram[:71]) / total
        white_ratio = sum(histogram[180:]) / total
        # Short lines can occupy only a few pixels; use darkness as the main panel gate.
        # The tiny floor rejects a truly blank panel while accepting short dialogue.
        is_dialogue = dark_ratio >= 0.50 and 0.00005 <= white_ratio <= 0.18
        return dark_ratio, white_ratio, is_dialogue

    @staticmethod
    def _point_inside_client(game: dict[str, Any], x: int, y: int) -> bool:
        left, top, width, height = game["client"]
        return left <= x < left + width and top <= y < top + height

    def _poll_game_left_click(self, game: dict[str, Any]) -> bool:
        """Return True once for a left-button press inside the game client area."""
        user32 = ctypes.windll.user32
        key_state = int(user32.GetAsyncKeyState(0x01))
        is_down = bool(key_state & 0x8000)
        pressed_edge = (is_down and not self.mouse_left_down) or bool(key_state & 0x0001)
        self.mouse_left_down = is_down
        if not pressed_edge:
            return False
        point = wintypes.POINT()
        if not user32.GetCursorPos(ctypes.byref(point)):
            return False
        return self._point_inside_client(game, point.x, point.y)

    @staticmethod
    def _change_ratio(left: Image.Image, right: Image.Image) -> float:
        # Compare bright text pixels only. Dark/white CG transitions are filtered by
        # _dialogue_coverage before this is used to advance the story.
        left_text = left.point(lambda value: 255 if value >= 150 else 0)
        right_text = right.point(lambda value: 255 if value >= 150 else 0)
        diff = ImageChops.difference(left_text, right_text).point(lambda value: 255 if value > 0 else 0)
        return ImageStat.Stat(diff).mean[0] / 255.0

    @staticmethod
    def _line_character_count(line: dict[str, Any]) -> int:
        text = str(line.get("zh_CN") or line.get("ja_JP") or "")
        return max(1, sum(not character.isspace() for character in text))

    def _begin_visual_monitor(
        self, line: dict[str, Any], index: int, started_at: float | None = None, calibrate_from_click: bool = False
    ) -> None:
        if self.visual_line_index == index:
            if calibrate_from_click:
                self.visual_reveal_started_at = started_at or time.monotonic()
                self.visual_last_change_at = self.visual_reveal_started_at
                self.visual_had_change = False
                self.visual_calibrate_from_click = True
            return
        now = started_at if started_at is not None else time.monotonic()
        self.visual_line_index = index
        self.visual_line_char_count = self._line_character_count(line)
        self.visual_reveal_started_at = now
        self.visual_last_change_at = now
        self.visual_had_change = False
        self.visual_calibrate_from_click = calibrate_from_click
        expected_seconds = self.visual_line_char_count * self.seconds_per_reveal_character
        self.visual_suppress_until = now + expected_seconds + 0.25
        self.visual_monitor_deadline = self.visual_suppress_until + max(
            0.75, min(2.0, self.visual_line_char_count * 0.01)
        )
        self._log_runtime(
            f"typewriter monitor line={line.get('id')} chars={self.visual_line_char_count} "
            f"char_seconds={self.seconds_per_reveal_character:.4f} wait={expected_seconds + 0.25:.2f}s "
            f"click_calibration={calibrate_from_click}"
        )

    def _update_visual_monitor(self, sample: Image.Image, now: float, threshold: float) -> bool:
        """Track typewriter pixels and suppress line-change detection until they settle."""
        # Once the reveal window has ended, leave the normal stable-change detector alone.
        # Resetting its candidate here on every polling cycle prevented it from ever firing.
        if self.visual_line_index is None and now >= self.visual_suppress_until:
            return False
        if self.sample_baseline is not None:
            delta = self._change_ratio(sample, self.sample_baseline)
            if delta >= max(0.0001, threshold * 0.25):
                self.visual_last_change_at = now
                self.visual_had_change = True
            self.sample_baseline = sample
        self.sample_candidate = None
        self.stable_count = 0

        quiet_seconds = now - self.visual_last_change_at
        if self.visual_line_index is not None and self.visual_had_change and quiet_seconds >= 0.30:
            elapsed = max(0.0, self.visual_last_change_at - self.visual_reveal_started_at)
            if self.visual_calibrate_from_click:
                observed = min(0.15, max(0.015, elapsed / self.visual_line_char_count))
                old_speed = self.seconds_per_reveal_character
                self.seconds_per_reveal_character = old_speed * 0.7 + observed * 0.3
                self._log_runtime(
                    f"typewriter calibrated line_index={self.visual_line_index} chars={self.visual_line_char_count} "
                    f"elapsed={elapsed:.3f}s observed_char_seconds={observed:.4f} "
                    f"new_char_seconds={self.seconds_per_reveal_character:.4f}"
                )
            self.visual_line_index = None
            self.visual_calibrate_from_click = False
        elif self.visual_line_index is not None and now >= self.visual_suppress_until and not self.visual_had_change:
            self.visual_line_index = None
            self.visual_calibrate_from_click = False
        elif self.visual_line_index is not None and now >= self.visual_monitor_deadline:
            self._log_runtime(
                f"typewriter monitor timeout line_index={self.visual_line_index}; releasing color detection"
            )
            self.visual_line_index = None
            self.visual_calibrate_from_click = False

        return now < self.visual_suppress_until or self.visual_line_index is not None

    def _navigate_story(self, direction: int) -> None:
        with self.navigation_lock:
            story = self.active_story
            config = read_config()
            if not story or not config.get("listening"):
                self._log_runtime(f"manual navigation ignored direction={direction} listening={bool(config.get('listening'))} story_loaded={bool(story)}")
                self.set_status("请先开始监听剧情", "")
                return
            current = self.line_index
            target = 0 if current < 0 else max(0, min(len(story.get("lines", [])) - 1, current + direction))
            self._log_runtime(f"manual navigation direction={direction} from={current} to={target}")
            self._trigger_line_locked(story, target, config)

    def _trigger_line(self, story: dict[str, Any], index: int, config: dict[str, Any]) -> None:
        with self.navigation_lock:
            self._trigger_line_locked(story, index, config)

    def _prefetch_next_voiced_lines(
        self, lines: list[dict[str, Any]], index: int, stage_id: str, config: dict[str, Any]
    ) -> None:
        prepared_count = 0
        for candidate in lines[index + 1:]:
            if not str(candidate.get("speaker") or "").strip():
                continue
            self._schedule_preparation(stage_id, candidate, config)
            prepared_count += 1
            if prepared_count == 2:
                break

    def _trigger_line_locked(self, story: dict[str, Any], index: int, config: dict[str, Any]) -> None:
        lines = story.get("lines", [])
        if index < 0 or index >= len(lines):
            self._cancel_queued_playback()
            self.set_status("剧情脚本已播放完", listening=False)
            config = read_config()
            config["listening"] = False
            _write_json(CONFIG_PATH, config)
            return
        self.line_index = index
        line = lines[index]
        self._begin_visual_monitor(line, index)
        stage_id = str(story["stage_id"])
        self._cancel_queued_playback()
        with self.state_lock:
            revision = self.playback_revision
        speaker = str(line.get("speaker") or "").strip()
        if not speaker:
            # Empty-speaker entries are silent beats, but each keeps its own story index.
            self.set_status("剧情停顿 · 点击继续", str(line.get("id") or ""))
            self._prefetch_next_voiced_lines(lines, index, stage_id, config)
            return
        self.set_status("正在准备当前台词", f"{speaker} · {line.get('id')}")
        key, future = self._schedule_preparation(stage_id, line, config, report_progress=True)
        while True:
            try:
                self.audio_queue.get_nowait()
            except queue.Empty:
                break
        self.audio_queue.put((revision, index, key, future, time.monotonic()))
        # Keep the next two voiced lines ready; silent beats still occupy their own clicks.
        self._prefetch_next_voiced_lines(lines, index, stage_id, config)

    def run(self) -> None:
        if os.name != "nt":
            raise RuntimeError("控制台小人和游戏窗口捕获目前只支持 Windows。")
        self.overlay.start()
        self.overlay._active_lease_seconds = 86400
        self.overlay.set_active(True)
        self.set_status("控制台已启动 · 点击小人配置参数")
        self.open_settings()
        audio_thread = threading.Thread(target=self._audio_worker, name="arkvoice-audio", daemon=True)
        audio_thread.start()
        previous_settings_mtime = 0
        config = read_config()
        active_listening = False
        story: dict[str, Any] | None = None
        last_sample_at = 0.0
        try:
            self._ensure_config_file()
            while not self.stop_event.is_set():
                try:
                    mtime = CONFIG_PATH.stat().st_mtime_ns
                except OSError:
                    mtime = 0
                if mtime != previous_settings_mtime:
                    previous_settings_mtime = mtime
                    config = read_config()
                    selected_story = server._story_for(str(config.get("stage_id", "MAIN-10-01")))
                    if selected_story != story:
                        with self.navigation_lock:
                            if active_listening:
                                self._cancel_queued_playback()
                        story = selected_story
                        self.active_story = story
                        self.line_index = -1
                        self.ocr_position_unknown = False
                        self.sample_baseline = None
                        self.last_dialogue_active = None
                        self.initial_position_pending = active_listening
                    should_listen = bool(config.get("listening"))
                    if should_listen != active_listening:
                        active_listening = should_listen
                        self.overlay.set_active(active_listening)
                        self.line_index = -1
                        self.ocr_position_unknown = False
                        self.sample_baseline = None
                        self.sample_candidate = None
                        self.last_dialogue_active = None
                        self.stable_count = 0
                        self.visual_line_index = None
                        self.visual_suppress_until = 0.0
                        self.visual_monitor_deadline = 0.0
                        if active_listening:
                            self.initial_position_pending = True
                            self.last_line_trigger = 0.0
                            if not story:
                                self.set_status("找不到所选剧情脚本", listening=False)
                                active_listening = False
                                config["listening"] = False
                            else:
                                self.set_status("等待识别当前剧情台词", listening=True)
                        else:
                            self._cancel_queued_playback()
                            self.set_status("监听已暂停", listening=False)
                if self._settings_process is not None and self._settings_process.poll() is not None:
                    return_code = self._settings_process.returncode
                    if self._settings_log_handle is not None:
                        self._settings_log_handle.close()
                        self._settings_log_handle = None
                    self._settings_process = None
                    if return_code:
                        detail = _read_text_tail(SETTINGS_LOG, 100)
                        self.set_status(f"参数窗口启动失败：{detail or '查看 settings-window.log'}")
                if not active_listening:
                    time.sleep(0.25)
                    continue
                game = _find_arknights_window()
                if not game or not game["visible"] or game["minimized"] or not game["foreground"]:
                    self.mouse_left_down = False
                    self.set_status("等待明日方舟窗口位于前台", listening=True)
                    time.sleep(0.4)
                    continue
                if self._poll_game_left_click(game):
                    self.pending_game_clicks = min(20, self.pending_game_clicks + 1)
                    self.game_click_count += 1
                    self._log_runtime(
                        f"game-window left click count={self.game_click_count} pending={self.pending_game_clicks}"
                    )
                now = time.monotonic()
                if now - last_sample_at < 0.12:
                    time.sleep(0.03)
                    continue
                last_sample_at = now
                sample = self._sample_dialogue(game, int(config.get("dialogue_bottom_percent", 10)))
                if sample is None:
                    continue
                dark_ratio, white_ratio, is_dialogue = self._dialogue_coverage(sample)
                if is_dialogue != self.last_dialogue_active:
                    self._log_runtime(
                        f"dialogue region active={is_dialogue} dark={dark_ratio:.3f} white={white_ratio:.3f}"
                    )
                    self.last_dialogue_active = is_dialogue
                if not is_dialogue:
                    self.pending_game_clicks = 0
                    self.sample_candidate = None
                    self.stable_count = 0
                    time.sleep(0.025)
                    continue
                if self.initial_position_pending and story:
                    self.pending_game_clicks = 0
                    self.initial_position_pending = False
                    self.sample_baseline = sample
                    self.set_status("正在识别当前台词", listening=True)
                    try:
                        matched, score, ocr_text = self._recognize_current_line(
                            game, story
                        )
                        ocr_error = ""
                    except Exception as exc:
                        matched, score, ocr_text = None, 0.0, ""
                        ocr_error = str(exc)
                        self._log_runtime(f"startup OCR failed: {exc}")
                    if matched is not None:
                        matched_index = next(i for i, candidate in enumerate(story["lines"]) if candidate is matched)
                        self.ocr_position_unknown = False
                        self._log_runtime(
                            f"startup OCR matched line={matched.get('id')} score={score:.3f} text={ocr_text[:100]}"
                        )
                        self._trigger_line(story, matched_index, config)
                    else:
                        self.line_index = -1
                        self.ocr_position_unknown = True
                        if ocr_error:
                            self.set_status("OCR识别失败", ocr_error[:72])
                        else:
                            self.set_status("OCR未匹配到当前剧情台词", ocr_text[:72] or "请确认台词框区域")
                        self._log_runtime(f"startup OCR no match text={ocr_text[:160]}")
                    continue
                if self.sample_baseline is None:
                    if self.ocr_position_unknown or self.line_index < 0:
                        self.pending_game_clicks = 0
                    self.sample_baseline = sample
                    if self.ocr_position_unknown and story:
                        try:
                            matched, score, ocr_text = self._recognize_current_line(game, story)
                        except Exception as exc:
                            self._log_runtime(f"dialogue appeared OCR failed: {exc}")
                            matched, score, ocr_text = None, 0.0, ""
                        if matched is not None:
                            self.line_index = next(i for i, candidate in enumerate(story["lines"]) if candidate is matched)
                            self.ocr_position_unknown = False
                            self._log_runtime(
                                f"dialogue appeared OCR matched line={matched.get('id')} score={score:.3f} text={ocr_text[:100]}"
                            )
                            self._trigger_line(story, self.line_index, config)
                        else:
                            self.set_status("等待识别剧情台词", "请确认台词框区域")
                    else:
                        self.set_status("监听中 · 点击游戏推进台词", listening=True)
                    continue
                if self.pending_game_clicks and story and self.line_index >= 0 and not self.ocr_position_unknown:
                    if now - self.last_line_trigger > 0.35:
                        clicks = self.pending_game_clicks
                        self.pending_game_clicks = 0
                        previous_index = self.line_index
                        target_index = previous_index + clicks
                        # Anchor the next capture after the click-driven screen update so
                        # its visual change cannot advance the same line a second time.
                        self.sample_baseline = None
                        self.sample_candidate = None
                        self.stable_count = 0
                        self.last_line_trigger = now
                        self._log_runtime(
                            f"mouse-assisted advance clicks={clicks} from={previous_index} to={target_index}"
                        )
                        if target_index < len(story["lines"]):
                            self._begin_visual_monitor(
                                story["lines"][target_index], target_index, now,
                                calibrate_from_click=(clicks == 1),
                            )
                        self._trigger_line(story, target_index, config)
                    continue
                if self._update_visual_monitor(
                    sample, now, float(config.get("change_threshold", 0.0008))
                ):
                    continue
                delta = self._change_ratio(sample, self.sample_baseline)
                if delta < float(config.get("change_threshold", 0.0008)):
                    self.sample_candidate = None
                    self.stable_count = 0
                    continue
                if self.sample_candidate is not None and self._change_ratio(sample, self.sample_candidate) < 0.0015:
                    self.stable_count += 1
                else:
                    self.sample_candidate = sample
                    self.stable_count = 1
                # One stable changed frame pair is enough to confirm a new line.
                if self.stable_count >= 2 and now - self.last_line_trigger > 0.35:
                    self.pending_game_clicks = 0
                    self.sample_baseline = sample
                    self.sample_candidate = None
                    self.stable_count = 0
                    self.last_line_trigger = now
                    if story:
                        if self.ocr_position_unknown:
                            try:
                                matched, score, ocr_text = self._recognize_current_line(
                                    game, story
                                )
                            except Exception as exc:
                                self._log_runtime(f"dialogue change OCR failed: {exc}")
                                matched, score, ocr_text = None, 0.0, ""
                            if matched is not None:
                                self.line_index = next(i for i, candidate in enumerate(story["lines"]) if candidate is matched)
                                self.ocr_position_unknown = False
                                self._log_runtime(
                                    f"dialogue change OCR matched line={matched.get('id')} score={score:.3f} text={ocr_text[:100]}"
                                )
                                self._trigger_line(story, self.line_index, config)
                            else:
                                self._log_runtime(f"dialogue change OCR no match text={ocr_text[:160]}")
                                self.set_status("等待识别剧情台词", "请确认台词框区域")
                        else:
                            self._trigger_line(story, self.line_index + 1, config)
                time.sleep(0.025)
        except KeyboardInterrupt:
            pass
        finally:
            self.stop_event.set()
            try:
                import winsound
                winsound.PlaySound(None, winsound.SND_PURGE)
            except Exception:
                pass
            self.overlay.set_active(False)
            self.overlay.stop()
            self.audio_queue.put(None)
            audio_thread.join(timeout=3)
            self.prepare_pool.shutdown(wait=False, cancel_futures=True)
            server._shutdown_tts_api()
            self.set_status("控制台已退出", listening=False)

    def _ensure_config_file(self) -> None:
        if not CONFIG_PATH.exists():
            _write_json(CONFIG_PATH, DEFAULT_CONFIG)


if __name__ == "__main__":
    runtime = Runtime()
    try:
        server._ensure_tts_api()
    except Exception as exc:
        print(f"语音服务启动失败：{exc}", flush=True)
        input("按 Enter 退出……")
        raise SystemExit(1)
    runtime.run()
