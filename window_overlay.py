from __future__ import annotations

import ctypes
import os
import threading
import time
from pathlib import Path
from typing import Any


class GameWindowOverlay:
    """A per-pixel-alpha, clickable sprite attached to the Arknights window."""

    def __init__(
        self,
        image_path: Path,
        poll_seconds: float = 0.15,
        on_click: Any = None,
        on_previous: Any = None,
        on_next: Any = None,
    ) -> None:
        self.image_path = Path(image_path)
        self.poll_seconds = poll_seconds
        self._active = threading.Event()
        self._stopping = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._status = "stopped"
        self._last_active_at = 0.0
        self._active_lease_seconds = 5.0
        self._thread_id = 0
        self._hwnd: int | None = None
        self._on_click = on_click
        self._on_previous = on_previous
        self._on_next = on_next
        self._image_revision = 0
        self._log_entries: list[str] = []
        self._log_revision = 0
        self._log_scroll_offset = 0

    @property
    def supported(self) -> bool:
        return os.name == "nt"

    def start(self) -> None:
        if not self.supported:
            with self._lock:
                self._status = "unsupported"
            return
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass
        if self._thread and self._thread.is_alive():
            return
        self._stopping.clear()
        self._thread = threading.Thread(target=self._run, name="arkvoice-overlay", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stopping.set()
        self._active.clear()
        if self._thread_id:
            try:
                self._post_thread_quit(self._thread_id)
            except Exception:
                pass
        if self._thread:
            self._thread.join(timeout=2)
        with self._lock:
            self._status = "stopped"

    def set_active(self, active: bool) -> None:
        if active:
            self._last_active_at = time.monotonic()
            self._active.set()
        else:
            self._active.clear()
        if not self.supported:
            with self._lock:
                self._status = "unsupported"
        elif not self._thread or not self._thread.is_alive():
            with self._lock:
                self._status = "stopped"

    def set_image(self, image_path: Path) -> None:
        image_path = Path(image_path)
        with self._lock:
            if image_path != self.image_path:
                self.image_path = image_path
                self._image_revision += 1

    def append_log_entry(self, entry: str) -> None:
        with self._lock:
            self._log_entries.append(entry)
            del self._log_entries[:-500]
            self._log_scroll_offset = 0
            self._log_revision += 1

    def _scroll_log(self, delta: int) -> None:
        with self._lock:
            self._log_scroll_offset = max(0, self._log_scroll_offset + (3 if delta > 0 else -3))
            self._log_revision += 1

    def get_status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "supported": self.supported,
                "active": self._active.is_set(),
                "state": self._status,
                "image_exists": self.image_path.is_file(),
            }

    @staticmethod
    def _post_thread_quit(thread_id: int) -> None:
        ctypes.windll.user32.PostThreadMessageW(thread_id, 0x0012, 0, 0)

    def _set_status(self, state: str) -> None:
        with self._lock:
            self._status = state

    def _run(self) -> None:
        try:
            from PIL import Image

            with self._lock:
                initial_path = self.image_path
            image = Image.open(initial_path).convert("RGBA")
            image.load()
            alpha_bounds = image.getchannel("A").getbbox()
            alpha_bottom = alpha_bounds[3] if alpha_bounds else image.height
            loaded_image_revision = self._image_revision
        except Exception:
            self._set_status("image_missing" if not initial_path.is_file() else "image_error")
            return

        try:
            native = _Win32Overlay(image, self._on_click)
            navigation_native = _Win32Overlay(
                Image.new("RGBA", (1, 1), (0, 0, 0, 0)),
                on_action=self._handle_navigation_action,
            )
            log_native = _Win32Overlay(
                Image.new("RGBA", (1, 1), (0, 0, 0, 0)),
                on_scroll=self._scroll_log,
            )
            self._thread_id = native.thread_id
            self._hwnd = native.hwnd
            navigation_revision = 0
            last_navigation_hover: bool | None = None
            last_log_render_key: tuple[int, int, int, int] | None = None
            log_image = None
            while not self._stopping.is_set():
                with self._lock:
                    requested_path = self.image_path
                    requested_revision = self._image_revision
                if requested_revision != loaded_image_revision:
                    try:
                        replacement = Image.open(requested_path).convert("RGBA")
                        replacement.load()
                        image = replacement
                        alpha_bounds = image.getchannel("A").getbbox()
                        alpha_bottom = alpha_bounds[3] if alpha_bounds else image.height
                        loaded_image_revision = requested_revision
                    except Exception:
                        self._set_status("image_missing" if not requested_path.is_file() else "image_error")
                if self._active.is_set() and time.monotonic() - self._last_active_at > self._active_lease_seconds:
                    self._active.clear()
                game = _find_arknights_window()
                if game and game["visible"] and not game["minimized"] and game["foreground"]:
                    outer_left, outer_top, _outer_right, outer_bottom = game["outer"]
                    log_height = max(1, outer_bottom - outer_top)
                    log_width = max(1, log_height // 2)
                    with self._lock:
                        log_entries = list(self._log_entries)
                        log_revision = self._log_revision
                        log_scroll_offset = self._log_scroll_offset
                    log_render_key = (log_revision, log_width, log_height, log_scroll_offset)
                    if log_render_key != last_log_render_key:
                        log_image = _render_log_panel(log_width, log_height, log_entries, log_scroll_offset)
                        last_log_render_key = log_render_key
                    log_native.show(max(0, outer_left - log_width - 8), outer_top, log_image, log_revision)
                else:
                    log_native.hide()
                if not self._active.is_set():
                    native.hide()
                    navigation_native.hide()
                    self._set_status("inactive")
                else:
                    if game is None:
                        native.hide()
                        navigation_native.hide()
                        self._set_status("waiting_for_game")
                    elif not game["visible"] or game["minimized"] or not game["foreground"]:
                        native.hide()
                        navigation_native.hide()
                        self._set_status("waiting_for_foreground")
                    else:
                        x, y, client_width, client_height = game["client"]
                        if client_width <= 0 or client_height <= 0:
                            native.hide()
                            navigation_native.hide()
                            self._set_status("waiting_for_game")
                        else:
                            draw_width = max(1, min(360, int(client_width * 0.28)))
                            sprite_height = max(1, round(draw_width * image.height / image.width))
                            if sprite_height > int(client_height * 0.38):
                                sprite_height = max(1, int(client_height * 0.38))
                                draw_width = max(1, round(sprite_height * image.width / image.height))
                            outer_left, outer_top, outer_right, _outer_bottom = game["outer"]
                            draw_x = x + (client_width - draw_width) // 2
                            sprite = image.resize((draw_width, sprite_height), Image.Resampling.LANCZOS)
                            # Align the lowest visible pixels (the character's paws) with the
                            # outer top edge of the game window's black title border.
                            content_bottom = round(sprite_height * alpha_bottom / image.height)
                            sprite_y = outer_top - content_bottom + 10
                            native.show(draw_x, sprite_y, sprite, loaded_image_revision)
                            button_size = max(38, min(52, draw_width // 7))
                            button_gap = 10
                            screen_width = _user32.GetSystemMetrics(0)
                            left_button_x = max(0, draw_x - button_gap - button_size)
                            right_button_x = min(screen_width - button_size, draw_x + draw_width + button_gap)
                            button_y = sprite_y + (sprite_height - button_size) // 2
                            controls_left = min(left_button_x, draw_x)
                            controls_right = max(right_button_x + button_size, draw_x + draw_width)
                            controls_top = min(button_y, sprite_y)
                            controls_bottom = max(button_y + button_size, sprite_y + sprite_height)
                            cursor = _POINT()
                            _user32.GetCursorPos(ctypes.byref(cursor))
                            hovering = (
                                controls_left <= cursor.x < controls_right
                                and controls_top <= cursor.y < controls_bottom
                            )
                            if hovering != last_navigation_hover:
                                navigation_revision += 1
                                last_navigation_hover = hovering
                            if hovering:
                                nav_image = _render_navigation_panel(
                                    controls_right - controls_left,
                                    controls_bottom - controls_top,
                                    button_size,
                                    left_button_x - controls_left,
                                    right_button_x - controls_left,
                                    button_y - controls_top,
                                )
                                regions = [
                                    (left_button_x - controls_left, button_y - controls_top,
                                     button_size, button_size, "previous"),
                                    (right_button_x - controls_left, button_y - controls_top,
                                     button_size, button_size, "next"),
                                ]
                                navigation_native.show(
                                    controls_left, controls_top, nav_image, navigation_revision,
                                    action_regions=regions,
                                )
                            else:
                                navigation_native.hide()
                            if hovering:
                                navigation_native.promote()
                            self._set_status("visible")
                native.pump_messages()
                self._stopping.wait(self.poll_seconds)
            native.destroy()
            navigation_native.destroy()
            log_native.destroy()
        except Exception:
            self._set_status("error")
        finally:
            self._hwnd = None
            self._thread_id = 0

    def _handle_navigation_action(self, action: str) -> None:
        callback = self._on_previous if action == "previous" else self._on_next if action == "next" else None
        if callback:
            callback()


if os.name == "nt":
    from ctypes import wintypes

    _LRESULT = ctypes.c_ssize_t
    _WNDPROC = ctypes.WINFUNCTYPE(_LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)

    class _WNDCLASSW(ctypes.Structure):
        _fields_ = [
            ("style", wintypes.UINT),
            ("lpfnWndProc", _WNDPROC),
            ("cbClsExtra", ctypes.c_int),
            ("cbWndExtra", ctypes.c_int),
            ("hInstance", wintypes.HINSTANCE),
            ("hIcon", wintypes.HICON),
            ("hCursor", ctypes.c_void_p),
            ("hbrBackground", wintypes.HBRUSH),
            ("lpszMenuName", wintypes.LPCWSTR),
            ("lpszClassName", wintypes.LPCWSTR),
        ]

    class _POINT(ctypes.Structure):
        _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]

    class _SIZE(ctypes.Structure):
        _fields_ = [("cx", wintypes.LONG), ("cy", wintypes.LONG)]

    class _BLENDFUNCTION(ctypes.Structure):
        _fields_ = [
            ("BlendOp", wintypes.BYTE),
            ("BlendFlags", wintypes.BYTE),
            ("SourceConstantAlpha", wintypes.BYTE),
            ("AlphaFormat", wintypes.BYTE),
        ]

    class _BITMAPINFOHEADER(ctypes.Structure):
        _fields_ = [
            ("biSize", wintypes.DWORD),
            ("biWidth", wintypes.LONG),
            ("biHeight", wintypes.LONG),
            ("biPlanes", wintypes.WORD),
            ("biBitCount", wintypes.WORD),
            ("biCompression", wintypes.DWORD),
            ("biSizeImage", wintypes.DWORD),
            ("biXPelsPerMeter", wintypes.LONG),
            ("biYPelsPerMeter", wintypes.LONG),
            ("biClrUsed", wintypes.DWORD),
            ("biClrImportant", wintypes.DWORD),
        ]

    class _RGBQUAD(ctypes.Structure):
        _fields_ = [
            ("rgbBlue", wintypes.BYTE),
            ("rgbGreen", wintypes.BYTE),
            ("rgbRed", wintypes.BYTE),
            ("rgbReserved", wintypes.BYTE),
        ]

    class _BITMAPINFO(ctypes.Structure):
        _fields_ = [("bmiHeader", _BITMAPINFOHEADER), ("bmiColors", _RGBQUAD * 1)]

    class _MSG(ctypes.Structure):
        _fields_ = [
            ("hwnd", wintypes.HWND),
            ("message", wintypes.UINT),
            ("wParam", wintypes.WPARAM),
            ("lParam", wintypes.LPARAM),
            ("time", wintypes.DWORD),
            ("pt", _POINT),
        ]

    _user32 = ctypes.WinDLL("user32", use_last_error=True)
    _gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    _user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    _user32.DefWindowProcW.restype = _LRESULT
    _user32.RegisterClassW.argtypes = [ctypes.POINTER(_WNDCLASSW)]
    _user32.RegisterClassW.restype = wintypes.ATOM
    _user32.CreateWindowExW.argtypes = [
        wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
        ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
        wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID,
    ]
    _user32.CreateWindowExW.restype = wintypes.HWND
    _user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    _user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.UINT]
    _user32.SetCapture.argtypes = [wintypes.HWND]
    _user32.SetCapture.restype = wintypes.HWND
    _user32.GetCapture.restype = wintypes.HWND
    _user32.ReleaseCapture.restype = wintypes.BOOL
    _user32.UpdateLayeredWindow.argtypes = [
        wintypes.HWND, wintypes.HDC, ctypes.POINTER(_POINT), ctypes.POINTER(_SIZE),
        wintypes.HDC, ctypes.POINTER(_POINT), wintypes.COLORREF,
        ctypes.POINTER(_BLENDFUNCTION), wintypes.DWORD,
    ]
    _user32.PeekMessageW.argtypes = [ctypes.POINTER(_MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT, wintypes.UINT]
    _user32.TranslateMessage.argtypes = [ctypes.POINTER(_MSG)]
    _user32.DispatchMessageW.argtypes = [ctypes.POINTER(_MSG)]
    _user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    _user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    _user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    _user32.EnumWindows.argtypes = [ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM), wintypes.LPARAM]
    _user32.IsWindowVisible.argtypes = [wintypes.HWND]
    _user32.IsIconic.argtypes = [wintypes.HWND]
    _user32.GetClientRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    _user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    _user32.ClientToScreen.argtypes = [wintypes.HWND, ctypes.POINTER(_POINT)]
    _user32.GetForegroundWindow.restype = wintypes.HWND
    _user32.GetSystemMetrics.argtypes = [ctypes.c_int]
    _user32.GetSystemMetrics.restype = ctypes.c_int
    _user32.GetCursorPos.argtypes = [ctypes.POINTER(_POINT)]
    _user32.GetCursorPos.restype = wintypes.BOOL
    _gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
    _gdi32.CreateDIBSection.argtypes = [wintypes.HDC, ctypes.POINTER(_BITMAPINFO), wintypes.UINT, ctypes.POINTER(ctypes.c_void_p), wintypes.HANDLE, wintypes.DWORD]
    _gdi32.CreateDIBSection.restype = wintypes.HBITMAP
    _gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
    _gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
    _gdi32.DeleteDC.argtypes = [wintypes.HDC]
    _user32.GetDC.argtypes = [wintypes.HWND]
    _user32.GetDC.restype = wintypes.HDC
    _user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
    _kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
    _kernel32.GetModuleHandleW.restype = wintypes.HMODULE
    _kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _kernel32.OpenProcess.restype = wintypes.HANDLE
    _kernel32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
    _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

    _PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    _WS_POPUP = 0x80000000
    _WS_EX_LAYERED = 0x00080000
    _WS_EX_TRANSPARENT = 0x00000020
    _WS_EX_TOOLWINDOW = 0x00000080
    _WS_EX_TOPMOST = 0x00000008
    _WS_EX_NOACTIVATE = 0x08000000
    _HWND_TOPMOST = wintypes.HWND(-1)
    _SW_SHOWNOACTIVATE = 4
    _SW_HIDE = 0
    _SWP_NOACTIVATE = 0x0010
    _SWP_SHOWWINDOW = 0x0040
    _SWP_NOMOVE = 0x0002
    _SWP_NOSIZE = 0x0001
    _PM_REMOVE = 0x0001
    _BI_RGB = 0
    _DIB_RGB_COLORS = 0
    _ULW_ALPHA = 0x00000002
    _AC_SRC_OVER = 0
    _AC_SRC_ALPHA = 1
    _WM_QUIT = 0x0012
    _WM_DESTROY = 0x0002
    _WM_NCHITTEST = 0x0084
    _WM_MOUSEACTIVATE = 0x0021
    _WM_LBUTTONDOWN = 0x0201
    _WM_LBUTTONUP = 0x0202
    _WM_MOUSEWHEEL = 0x020A
    _HTCLIENT = 1
    _HTTRANSPARENT = -1
    _MA_NOACTIVATE = 3

    class _Win32Overlay:
        def __init__(self, image: Any, on_click: Any = None, on_action: Any = None, on_scroll: Any = None) -> None:
            self.image = image
            self.on_click = on_click
            self.on_action = on_action
            self.on_scroll = on_scroll
            self.action_regions: list[tuple[int, int, int, int, str]] = []
            self.thread_id = _kernel32.GetCurrentThreadId()
            # Each overlay needs its own registered window procedure. Windows binds
            # the procedure to the class at RegisterClassW time; reusing one class
            # name makes every later window dispatch to the first overlay instance.
            self.class_name = f"ArkVoiceOverlay_{self.thread_id}_{id(self):x}"
            self._wnd_proc = _WNDPROC(self._window_proc)
            instance = _kernel32.GetModuleHandleW(None)
            wc = _WNDCLASSW(0, self._wnd_proc, 0, 0, instance, None, None, None, None, self.class_name)
            atom = _user32.RegisterClassW(ctypes.byref(wc))
            if not atom and ctypes.get_last_error() != 1410:  # ERROR_CLASS_ALREADY_EXISTS
                raise ctypes.WinError(ctypes.get_last_error())
            ex_style = _WS_EX_LAYERED | _WS_EX_TOOLWINDOW | _WS_EX_TOPMOST | _WS_EX_NOACTIVATE
            self.hwnd = _user32.CreateWindowExW(
                ex_style, self.class_name, "ArkVoice character overlay", _WS_POPUP,
                0, 0, 1, 1, None, None, instance, None,
            )
            if not self.hwnd:
                raise ctypes.WinError(ctypes.get_last_error())
            self._rendered_size: tuple[int, int] | None = None
            self._revision = -1
            self._visible = False
            self._position = (0, 0)
            self._base_image = image.copy()
            self._pressed_action: str | None = None
            self._pressed_click = False

        def _window_proc(self, hwnd: int, message: int, wparam: int, lparam: int) -> int:
            if message == _WM_NCHITTEST:
                screen_x = ctypes.c_short(lparam & 0xFFFF).value
                screen_y = ctypes.c_short((lparam >> 16) & 0xFFFF).value
                local_x = screen_x - self._position[0]
                local_y = screen_y - self._position[1]
                if 0 <= local_x < self.image.width and 0 <= local_y < self.image.height:
                    try:
                        return _HTCLIENT if self.image.getpixel((local_x, local_y))[3] > 16 else _HTTRANSPARENT
                    except Exception:
                        return _HTCLIENT
                return _HTTRANSPARENT
            if message == _WM_MOUSEACTIVATE:
                return _MA_NOACTIVATE
            if message == _WM_MOUSEWHEEL and self.on_scroll:
                delta = ctypes.c_short((wparam >> 16) & 0xFFFF).value
                self.on_scroll(delta)
                return 0
            if message == _WM_LBUTTONDOWN:
                local_x = ctypes.c_short(lparam & 0xFFFF).value
                local_y = ctypes.c_short((lparam >> 16) & 0xFFFF).value
                self._pressed_action = self._action_at(local_x, local_y)
                self._pressed_click = self._pressed_action is None
                _user32.SetCapture(hwnd)
                if self._pressed_action:
                    self._show_pressed_feedback(self._pressed_action)
                return 0
            if message == _WM_LBUTTONUP:
                local_x = ctypes.c_short(lparam & 0xFFFF).value
                local_y = ctypes.c_short((lparam >> 16) & 0xFFFF).value
                released_action = self._action_at(local_x, local_y)
                action = self._pressed_action if released_action == self._pressed_action else None
                should_click = self._pressed_click and action is None
                self._pressed_action = None
                self._pressed_click = False
                if _user32.GetCapture() == hwnd:
                    _user32.ReleaseCapture()
                if action:
                    self._restore_button_feedback()
                callback = self.on_action if action and self.on_action else self.on_click if should_click else None
                if callback:
                    args = (action,) if action and self.on_action else ()
                    threading.Thread(target=callback, args=args, name="arkvoice-overlay-action", daemon=True).start()
                return 0
            if message == _WM_DESTROY:
                return 0
            return _user32.DefWindowProcW(hwnd, message, wparam, lparam)

        def _action_at(self, x: int, y: int) -> str | None:
            return next((
                item[4] for item in self.action_regions
                if item[0] <= x < item[0] + item[2]
                and item[1] <= y < item[1] + item[3]
            ), None)

        def _show_pressed_feedback(self, action: str) -> None:
            from PIL import ImageDraw

            highlighted = self._base_image.copy()
            draw = ImageDraw.Draw(highlighted)
            region = next((item for item in self.action_regions if item[4] == action), None)
            if region:
                x, y, width, height, _ = region
                draw.rounded_rectangle(
                    (x + 2, y + 2, x + width - 3, y + height - 3),
                    radius=max(6, width // 4), fill=(255, 255, 255, 74),
                )
                self._upload(highlighted, *self._position)

        def _restore_button_feedback(self) -> None:
            self._upload(self._base_image, *self._position)

        def _premultiplied_bgra(self, image: Any) -> bytes:
            rgba = image.tobytes("raw", "RGBA")
            output = bytearray(len(rgba))
            for offset in range(0, len(rgba), 4):
                r, g, b, a = rgba[offset : offset + 4]
                output[offset] = b * a // 255
                output[offset + 1] = g * a // 255
                output[offset + 2] = r * a // 255
                output[offset + 3] = a
            return bytes(output)

        def show(
            self,
            x: int,
            y: int,
            image: Any,
            revision: int,
            action_regions: list[tuple[int, int, int, int, str]] | None = None,
        ) -> None:
            size = image.size
            self.image = image
            self._base_image = image.copy()
            self._position = (x, y)
            self.action_regions = action_regions or []
            if self._rendered_size != size or self._revision != revision:
                self._upload(image, x, y)
                self._rendered_size = size
                self._revision = revision
            else:
                _user32.SetWindowPos(self.hwnd, _HWND_TOPMOST, x, y, size[0], size[1], _SWP_NOACTIVATE | _SWP_SHOWWINDOW)
            if not self._visible:
                _user32.ShowWindow(self.hwnd, _SW_SHOWNOACTIVATE)
                self._visible = True

        def _upload(self, image: Any, x: int, y: int) -> None:
            width, height = image.size
            screen_dc = _user32.GetDC(None)
            memory_dc = _gdi32.CreateCompatibleDC(screen_dc)
            bits = ctypes.c_void_p()
            info = _BITMAPINFO()
            info.bmiHeader = _BITMAPINFOHEADER(
                ctypes.sizeof(_BITMAPINFOHEADER), width, -height, 1, 32,
                _BI_RGB, width * height * 4, 0, 0, 0, 0,
            )
            bitmap = _gdi32.CreateDIBSection(screen_dc, ctypes.byref(info), _DIB_RGB_COLORS, ctypes.byref(bits), None, 0)
            if not bitmap or not bits:
                _gdi32.DeleteDC(memory_dc)
                _user32.ReleaseDC(None, screen_dc)
                raise ctypes.WinError(ctypes.get_last_error())
            old_bitmap = _gdi32.SelectObject(memory_dc, bitmap)
            ctypes.memmove(bits, self._premultiplied_bgra(image), width * height * 4)
            destination = _POINT(x, y)
            source = _POINT(0, 0)
            dimensions = _SIZE(width, height)
            blend = _BLENDFUNCTION(_AC_SRC_OVER, 0, 255, _AC_SRC_ALPHA)
            success = _user32.UpdateLayeredWindow(
                self.hwnd, screen_dc, ctypes.byref(destination), ctypes.byref(dimensions),
                memory_dc, ctypes.byref(source), 0, ctypes.byref(blend), _ULW_ALPHA,
            )
            _gdi32.SelectObject(memory_dc, old_bitmap)
            _gdi32.DeleteObject(bitmap)
            _gdi32.DeleteDC(memory_dc)
            _user32.ReleaseDC(None, screen_dc)
            if not success:
                raise ctypes.WinError(ctypes.get_last_error())

        def hide(self) -> None:
            if self._visible:
                _user32.ShowWindow(self.hwnd, _SW_HIDE)
                self._visible = False

        def promote(self) -> None:
            if self.hwnd and self._visible:
                _user32.SetWindowPos(
                    self.hwnd, _HWND_TOPMOST, 0, 0, 0, 0,
                    _SWP_NOMOVE | _SWP_NOSIZE | _SWP_NOACTIVATE | _SWP_SHOWWINDOW,
                )

        def pump_messages(self) -> None:
            message = _MSG()
            while _user32.PeekMessageW(ctypes.byref(message), None, 0, 0, _PM_REMOVE):
                if message.message == _WM_QUIT:
                    return
                _user32.TranslateMessage(ctypes.byref(message))
                _user32.DispatchMessageW(ctypes.byref(message))

        def destroy(self) -> None:
            if self.hwnd:
                _user32.DestroyWindow(self.hwnd)
                self.hwnd = None


    def _window_title(hwnd: int) -> str:
        length = _user32.GetWindowTextLengthW(hwnd)
        if length <= 0:
            return ""
        buffer = ctypes.create_unicode_buffer(length + 1)
        _user32.GetWindowTextW(hwnd, buffer, len(buffer))
        return buffer.value

    def _process_image(pid: int) -> str:
        handle = _kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return ""
        try:
            buffer = ctypes.create_unicode_buffer(32768)
            length = wintypes.DWORD(len(buffer))
            if _kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(length)):
                return buffer.value
            return ""
        finally:
            _kernel32.CloseHandle(handle)

    def _find_arknights_window() -> dict[str, Any] | None:
        candidates: list[dict[str, Any]] = []
        callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

        def collect(hwnd: int, _: int) -> bool:
            if not _user32.IsWindowVisible(hwnd):
                return True
            title = _window_title(hwnd)
            pid = wintypes.DWORD()
            _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            image_path = _process_image(pid.value)
            process_name = Path(image_path).name.casefold()
            title_folded = title.casefold()
            matched = (
                "明日方舟" in title
                or "arknights" in title_folded
                or "arknights" in process_name
                or process_name == "hypergryph.exe"
            )
            if not matched:
                return True
            rect = wintypes.RECT()
            outer = wintypes.RECT()
            origin = _POINT(0, 0)
            if not _user32.GetClientRect(hwnd, ctypes.byref(rect)) or not _user32.ClientToScreen(hwnd, ctypes.byref(origin)) or not _user32.GetWindowRect(hwnd, ctypes.byref(outer)):
                return True
            candidates.append({
                "hwnd": hwnd,
                "title": title,
                "visible": bool(_user32.IsWindowVisible(hwnd)),
                "minimized": bool(_user32.IsIconic(hwnd)),
                "foreground": _user32.GetForegroundWindow() == hwnd,
                "client": (origin.x, origin.y, rect.right - rect.left, rect.bottom - rect.top),
                "outer": (outer.left, outer.top, outer.right, outer.bottom),
            })
            return True

        _user32.EnumWindows(callback_type(collect), 0)
        if not candidates:
            return None
        return next((item for item in candidates if item["foreground"]), candidates[0])

else:
    class _Win32Overlay:  # pragma: no cover - Windows-only implementation
        def __init__(self, image: Any) -> None:
            raise OSError("The character overlay requires Windows.")


    def _find_arknights_window() -> None:  # pragma: no cover - Windows-only implementation
        return None


def _render_navigation_panel(
    width: int,
    height: int,
    button_size: int,
    left_x: int,
    right_x: int,
    button_y: int,
) -> Any:
    from PIL import Image, ImageDraw

    panel = Image.new("RGBA", (max(1, width), max(1, height)), (0, 0, 0, 0))
    draw = ImageDraw.Draw(panel)
    for x, direction in ((left_x, "left"), (right_x, "right")):
        draw.ellipse(
            (x + 1, button_y + 1, x + button_size - 2, button_y + button_size - 2),
            fill=(31, 35, 43, 222),
            outline=(245, 245, 250, 235),
            width=2,
        )
        center_x = x + button_size // 2
        center_y = button_y + button_size // 2
        if direction == "left":
            points = [(center_x + 4, center_y - 10), (center_x - 6, center_y), (center_x + 4, center_y + 10)]
        else:
            points = [(center_x - 4, center_y - 10), (center_x + 6, center_y), (center_x - 4, center_y + 10)]
        draw.line(points, fill=(255, 255, 255, 250), width=4, joint="curve")
    return panel


def _render_log_panel(width: int, height: int, entries: list[str], scroll_offset: int) -> Any:
    from PIL import Image, ImageDraw, ImageFont

    width, height = max(1, width), max(1, height)
    panel = Image.new("RGBA", (width, height), (14, 18, 26, 232))
    draw = ImageDraw.Draw(panel)
    draw.rounded_rectangle((0, 0, width - 1, height - 1), radius=12, fill=(14, 18, 26, 232), outline=(128, 164, 205, 220), width=2)
    font_size = max(12, min(18, width // 22))
    font_path = Path(r"C:\Windows\Fonts\msyh.ttc")
    try:
        font = ImageFont.truetype(str(font_path), font_size) if font_path.is_file() else ImageFont.load_default()
        title_font = ImageFont.truetype(str(font_path), font_size + 2) if font_path.is_file() else font
    except OSError:
        font = ImageFont.load_default()
        title_font = font
    pad = max(9, width // 28)
    header_height = font_size + 20
    draw.text((pad, 8), "ARKVOICE \u8fd0\u884c\u65e5\u5fd7", font=title_font, fill=(234, 240, 248, 255))
    draw.text((pad, header_height - 1), "\u6eda\u8f6e\u67e5\u770b\u5386\u53f2 \u00b7 \u65b0\u6d88\u606f\u81ea\u52a8\u8ddf\u968f", font=font, fill=(145, 164, 187, 255))
    draw.line((pad, header_height + font_size + 8, width - pad, header_height + font_size + 8), fill=(95, 117, 146, 180), width=1)
    text_top = header_height + font_size + 17
    line_height = font_size + 7
    text_width = max(20, width - 2 * pad - 9)
    wrapped: list[str] = []
    for entry in entries or ["\u7b49\u5f85\u8fd0\u884c\u72b6\u6001\u2026\u2026"]:
        current = ""
        for char in entry:
            if current and draw.textlength(current + char, font=font) > text_width:
                wrapped.append(current)
                current = char
            else:
                current += char
        if current:
            wrapped.append(current)
    visible_count = max(1, (height - text_top - pad) // line_height)
    max_scroll = max(0, len(wrapped) - visible_count)
    scroll_offset = max(0, min(max_scroll, scroll_offset))
    end = len(wrapped) - scroll_offset
    visible = wrapped[max(0, end - visible_count):end]
    y = text_top
    for text in visible:
        color = (238, 241, 246, 255) if text.startswith("[") else (198, 211, 226, 255)
        draw.text((pad, y), text, font=font, fill=color)
        y += line_height
    if max_scroll:
        track_top, track_bottom = text_top, height - pad
        track_height = max(1, track_bottom - track_top)
        thumb_height = max(20, int(track_height * visible_count / len(wrapped)))
        thumb_top = track_top + int((track_height - thumb_height) * (max_scroll - scroll_offset) / max_scroll)
        draw.rounded_rectangle((width - pad + 1, track_top, width - pad + 4, track_bottom), radius=2, fill=(65, 78, 96, 180))
        draw.rounded_rectangle((width - pad + 1, thumb_top, width - pad + 4, thumb_top + thumb_height), radius=2, fill=(173, 196, 221, 230))
    return panel
