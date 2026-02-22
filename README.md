Here are the three fully refactored files:

---

## `main.py`

```python
from __future__ import annotations

import asyncio
import base64
import ctypes
import ctypes.wintypes as W
import http.client
import json
import logging
import os
import signal
import struct
import time
import urllib.parse
import webbrowser
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final, cast

HERE: Final[Path] = Path(__file__).resolve().parent
CONFIG_PATH: Final[Path] = HERE / "config.py"
PANEL_HTML: Final[Path] = HERE / "panel.html"


def _load_config() -> Any:
    import importlib.util
    spec = importlib.util.spec_from_file_location("config", str(CONFIG_PATH))
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


CFG: Any = _load_config()

HOST: Final[str] = str(getattr(CFG, "HOST", "127.0.0.1"))
PORT: Final[int] = int(getattr(CFG, "PORT", 1234))

log = logging.getLogger("franz")


def _cfg(name: str, default: Any = None) -> Any:
    return getattr(CFG, name, default)


def setup_logging(run_dir: Path) -> None:
    level = getattr(logging, str(_cfg("LOG_LEVEL", "INFO")).upper(), logging.INFO)
    fmt = logging.Formatter(
        "[%(name)s][%(asctime)s.%(msecs)03d][%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )
    root = logging.getLogger()
    root.setLevel(level)
    root.handlers.clear()
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    root.addHandler(sh)
    if bool(_cfg("LOG_TO_FILE", True)):
        fh = logging.FileHandler(run_dir / "main.log", encoding="utf-8")
        fh.setFormatter(fmt)
        root.addHandler(fh)
    log.info("logging ready level=%s run_dir=%s", logging.getLevelName(level), run_dir)


def make_run_dir() -> Path:
    runs_base = HERE / str(_cfg("RUNS_DIR", "runs"))
    runs_base.mkdir(exist_ok=True)
    existing = sorted(
        [d for d in runs_base.iterdir() if d.is_dir() and d.name.startswith("run_")],
        key=lambda d: d.name,
    )
    run_dir = runs_base / f"run_{len(existing) + 1:04d}"
    run_dir.mkdir(parents=True, exist_ok=True)
    return run_dir


@dataclass
class EngineState:
    phase: str = "init"
    error: str | None = None
    turn: int = 0
    run_dir: Path | None = None
    annotated_b64: str = ""
    raw_b64: str = ""
    vlm_json: str = ""
    observation: str = ""
    actions_text: str = ""
    bboxes: list[dict[str, Any]] = field(default_factory=list)
    actions: list[dict[str, Any]] = field(default_factory=list)
    msg_id: int = 0
    pending_seq: int = 0
    annotated_seq: int = -1
    annotated_event: asyncio.Event = field(default_factory=asyncio.Event)
    next_vlm_json: str | None = None
    next_event: asyncio.Event = field(default_factory=asyncio.Event)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


S: EngineState
STOP: asyncio.Event


def set_phase(phase: str, error: str | None = None) -> None:
    S.phase = phase
    S.error = error
    log.info("phase=%s error=%s", phase, error)


SRCCOPY: Final[int] = 0x00CC0020
CAPTUREBLT: Final[int] = 0x40000000
BI_RGB: Final[int] = 0
DIB_RGB: Final[int] = 0
HALFTONE: Final[int] = 4

try:
    ctypes.WinDLL("shcore", use_last_error=True).SetProcessDpiAwareness(2)
except Exception:
    pass

_user32 = ctypes.WinDLL("user32", use_last_error=True)
_gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)


def _sig(dll: Any, name: str, argtypes: list[Any], restype: Any) -> None:
    fn = getattr(dll, name)
    fn.argtypes = argtypes
    fn.restype = restype


_sig(_user32, "GetDC", [W.HWND], W.HDC)
_sig(_user32, "ReleaseDC", [W.HWND, W.HDC], ctypes.c_int)
_sig(_user32, "GetSystemMetrics", [ctypes.c_int], ctypes.c_int)
_sig(_gdi32, "CreateCompatibleDC", [W.HDC], W.HDC)
_sig(_gdi32, "CreateDIBSection",
     [W.HDC, ctypes.c_void_p, W.UINT, ctypes.POINTER(ctypes.c_void_p), W.HANDLE, W.DWORD], W.HBITMAP)
_sig(_gdi32, "SelectObject", [W.HDC, W.HGDIOBJ], W.HGDIOBJ)
_sig(_gdi32, "BitBlt",
     [W.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
      W.HDC, ctypes.c_int, ctypes.c_int, W.DWORD], W.BOOL)
_sig(_gdi32, "StretchBlt",
     [W.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
      W.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, W.DWORD], W.BOOL)
_sig(_gdi32, "SetStretchBltMode", [W.HDC, ctypes.c_int], ctypes.c_int)
_sig(_gdi32, "SetBrushOrgEx", [W.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_void_p], W.BOOL)
_sig(_gdi32, "DeleteObject", [W.HGDIOBJ], W.BOOL)
_sig(_gdi32, "DeleteDC", [W.HDC], W.BOOL)
_sig(_user32, "SetCursorPos", [ctypes.c_int, ctypes.c_int], W.BOOL)
_sig(_user32, "mouse_event",
     [W.DWORD, W.DWORD, W.DWORD, W.DWORD, ctypes.c_ulong], None)

MOUSEEVENTF_LEFTDOWN: Final[int] = 0x0002
MOUSEEVENTF_LEFTUP: Final[int] = 0x0004
MOUSEEVENTF_RIGHTDOWN: Final[int] = 0x0008
MOUSEEVENTF_RIGHTUP: Final[int] = 0x0010


class _BIH(ctypes.Structure):
    _fields_ = [
        ("biSize", W.DWORD), ("biWidth", W.LONG),
        ("biHeight", W.LONG), ("biPlanes", W.WORD),
        ("biBitCount", W.WORD), ("biCompression", W.DWORD),
        ("biSizeImage", W.DWORD), ("biXPelsPerMeter", W.LONG),
        ("biYPelsPerMeter", W.LONG), ("biClrUsed", W.DWORD),
        ("biClrImportant", W.DWORD),
    ]


class _BMI(ctypes.Structure):
    _fields_ = [("bmiHeader", _BIH), ("bmiColors", W.DWORD * 3)]


def _make_bmi(w: int, h: int) -> _BMI:
    bmi = _BMI()
    hdr = bmi.bmiHeader
    hdr.biSize = ctypes.sizeof(_BIH)
    hdr.biWidth = w
    hdr.biHeight = -h
    hdr.biPlanes = 1
    hdr.biBitCount = 32
    hdr.biCompression = BI_RGB
    return bmi


def _screen_size() -> tuple[int, int]:
    w, h = int(_user32.GetSystemMetrics(0)), int(_user32.GetSystemMetrics(1))
    return (w, h) if w > 0 and h > 0 else (1920, 1080)


def _create_dib(dc: Any, w: int, h: int) -> tuple[Any, int]:
    bits = ctypes.c_void_p()
    hbmp = _gdi32.CreateDIBSection(
        dc, ctypes.byref(_make_bmi(w, h)), DIB_RGB,
        ctypes.byref(bits), None, 0,
    )
    return (hbmp, int(bits.value)) if hbmp and bits.value else (None, 0)


def _capture_bgra_full() -> tuple[bytes, int, int] | None:
    sw, sh = _screen_size()
    sdc = _user32.GetDC(0)
    if not sdc:
        return None
    memdc = _gdi32.CreateCompatibleDC(sdc)
    if not memdc:
        _user32.ReleaseDC(0, sdc)
        return None
    hbmp, bits = _create_dib(sdc, sw, sh)
    if not hbmp:
        _gdi32.DeleteDC(memdc)
        _user32.ReleaseDC(0, sdc)
        return None
    old = _gdi32.SelectObject(memdc, hbmp)
    _gdi32.BitBlt(memdc, 0, 0, sw, sh, sdc, 0, 0, SRCCOPY | CAPTUREBLT)
    raw = bytes((ctypes.c_ubyte * (sw * sh * 4)).from_address(bits))
    _gdi32.SelectObject(memdc, old)
    _gdi32.DeleteObject(hbmp)
    _gdi32.DeleteDC(memdc)
    _user32.ReleaseDC(0, sdc)
    return raw, sw, sh


def _crop_bgra(bgra: bytes, sw: int, sh: int, crop: dict[str, int]) -> tuple[bytes, int, int]:
    x1, y1 = max(0, min(crop["x1"], sw)), max(0, min(crop["y1"], sh))
    x2, y2 = max(x1, min(crop["x2"], sw)), max(y1, min(crop["y2"], sh))
    cw, ch = x2 - x1, y2 - y1
    if cw <= 0 or ch <= 0:
        return bgra, sw, sh
    out = bytearray(cw * ch * 4)
    ss, ds = sw * 4, cw * 4
    for y in range(ch):
        so, do = (y1 + y) * ss + x1 * 4, y * ds
        out[do:do + ds] = bgra[so:so + ds]
    return bytes(out), cw, ch


def _stretch_bgra(bgra: bytes, sw: int, sh: int, dw: int, dh: int) -> bytes | None:
    sdc = _user32.GetDC(0)
    if not sdc:
        return None
    src_dc = _gdi32.CreateCompatibleDC(sdc)
    dst_dc = _gdi32.CreateCompatibleDC(sdc)
    if not src_dc or not dst_dc:
        if src_dc:
            _gdi32.DeleteDC(src_dc)
        if dst_dc:
            _gdi32.DeleteDC(dst_dc)
        _user32.ReleaseDC(0, sdc)
        return None
    src_bmp, src_bits = _create_dib(sdc, sw, sh)
    if not src_bmp:
        _gdi32.DeleteDC(src_dc)
        _gdi32.DeleteDC(dst_dc)
        _user32.ReleaseDC(0, sdc)
        return None
    ctypes.memmove(src_bits, bgra, sw * sh * 4)
    old_src = _gdi32.SelectObject(src_dc, src_bmp)
    dst_bmp, dst_bits = _create_dib(sdc, dw, dh)
    if not dst_bmp:
        _gdi32.SelectObject(src_dc, old_src)
        _gdi32.DeleteObject(src_bmp)
        _gdi32.DeleteDC(src_dc)
        _gdi32.DeleteDC(dst_dc)
        _user32.ReleaseDC(0, sdc)
        return None
    old_dst = _gdi32.SelectObject(dst_dc, dst_bmp)
    _gdi32.SetStretchBltMode(dst_dc, HALFTONE)
    _gdi32.SetBrushOrgEx(dst_dc, 0, 0, None)
    _gdi32.StretchBlt(dst_dc, 0, 0, dw, dh, src_dc, 0, 0, sw, sh, SRCCOPY)
    result = bytes((ctypes.c_ubyte * (dw * dh * 4)).from_address(dst_bits))
    _gdi32.SelectObject(dst_dc, old_dst)
    _gdi32.SelectObject(src_dc, old_src)
    _gdi32.DeleteObject(dst_bmp)
    _gdi32.DeleteObject(src_bmp)
    _gdi32.DeleteDC(dst_dc)
    _gdi32.DeleteDC(src_dc)
    _user32.ReleaseDC(0, sdc)
    return result


def _bgra_to_png(bgra: bytes, w: int, h: int) -> bytes:
    stride = w * 4
    raw = bytearray()
    for y in range(h):
        raw.append(0)
        row = bgra[y * stride:(y + 1) * stride]
        for i in range(0, len(row), 4):
            raw.extend((row[i + 2], row[i + 1], row[i], 255))

    def chunk(tag: bytes, body: bytes) -> bytes:
        return struct.pack(">I", len(body)) + tag + body + struct.pack(">I", zlib.crc32(tag + body) & 0xFFFFFFFF)

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(bytes(raw), 6))
        + chunk(b"IEND", b"")
    )


def capture_screenshot() -> tuple[str, int, int]:
    if (delay := float(_cfg("CAPTURE_DELAY", 0.0))) > 0:
        time.sleep(delay)
    if (cap := _capture_bgra_full()) is None:
        return "", 0, 0
    bgra, w, h = cap
    if (crop := _cfg("CAPTURE_CROP")) and isinstance(crop, dict) and all(k in crop for k in ("x1", "y1", "x2", "y2")):
        bgra, w, h = _crop_bgra(bgra, w, h, crop)
    out_w, out_h = int(_cfg("CAPTURE_WIDTH", 0)), int(_cfg("CAPTURE_HEIGHT", 0))
    if out_w > 0 and out_h > 0 and (w, h) != (out_w, out_h):
        if s := _stretch_bgra(bgra, w, h, out_w, out_h):
            bgra, w, h = s, out_w, out_h
    b64 = base64.b64encode(_bgra_to_png(bgra, w, h)).decode("ascii")
    log.info("capture done %dx%d b64len=%d", w, h, len(b64))
    return b64, w, h


def parse_vlm_json(raw: str) -> tuple[str, list[dict[str, Any]], list[dict[str, Any]]]:
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError:
        start, end = raw.find("{"), raw.rfind("}")
        if start >= 0 and end > start:
            try:
                obj = json.loads(raw[start:end + 1])
            except json.JSONDecodeError:
                log.warning("vlm json parse failed completely")
                return raw, [], []
        else:
            return raw, [], []
    observation = str(obj.get("observation", ""))
    bboxes: list[dict[str, Any]] = []
    for b in obj.get("bboxes", []):
        if isinstance(b, dict) and all(k in b for k in ("x1", "y1", "x2", "y2")):
            bboxes.append({"x1": int(b["x1"]), "y1": int(b["y1"]), "x2": int(b["x2"]), "y2": int(b["y2"])})
    actions: list[dict[str, Any]] = []
    for a in obj.get("actions", []):
        if isinstance(a, dict) and "name" in a and "x1" in a and "y1" in a:
            entry: dict[str, Any] = {"name": str(a["name"]).lower(), "x1": int(a["x1"]), "y1": int(a["y1"])}
            if "x2" in a and "y2" in a:
                entry["x2"] = int(a["x2"])
                entry["y2"] = int(a["y2"])
            actions.append(entry)
    log.info("parse_vlm_json obs_len=%d bboxes=%d actions=%d", len(observation), len(bboxes), len(actions))
    return observation, bboxes, actions


def save_turn_data(
    run_dir: Path, turn: int, observation: str,
    bboxes: list[dict[str, Any]], actions: list[dict[str, Any]], raw_b64: str,
) -> None:
    td = run_dir / f"turn_{turn:04d}"
    td.mkdir(exist_ok=True)
    (td / "vlm_output.json").write_text(
        json.dumps({"turn": turn, "observation": observation, "bboxes": bboxes, "actions": actions},
                   ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    if raw_b64:
        try:
            (td / "screenshot_raw.png").write_bytes(base64.b64decode(raw_b64))
        except Exception as e:
            log.warning("save raw png failed: %s", e)


def save_annotated(run_dir: Path, turn: int, annotated_b64: str) -> None:
    td = run_dir / f"turn_{turn:04d}"
    td.mkdir(exist_ok=True)
    try:
        (td / "screenshot_annotated.png").write_bytes(base64.b64decode(annotated_b64))
    except Exception as e:
        log.warning("save annotated png failed: %s", e)


def _move_to(x: int, y: int) -> None:
    _user32.SetCursorPos(x, y)


def _mouse(flags: int) -> None:
    _user32.mouse_event(flags, 0, 0, 0, 0)


def execute_actions(actions: list[dict[str, Any]]) -> None:
    if not bool(_cfg("PHYSICAL_EXECUTION", True)):
        log.info("PHYSICAL_EXECUTION=False, skipping %d actions", len(actions))
        return
    action_delay = float(_cfg("ACTION_DELAY_SECONDS", 0.05))
    drag_steps = int(_cfg("DRAG_DURATION_STEPS", 20))
    drag_step_d = float(_cfg("DRAG_STEP_DELAY", 0.01))
    for a in actions:
        name = a.get("name", "")
        x1, y1 = int(a.get("x1", 0)), int(a.get("y1", 0))
        x2, y2 = int(a.get("x2", x1)), int(a.get("y2", y1))
        log.info("execute action=%s x1=%d y1=%d x2=%d y2=%d", name, x1, y1, x2, y2)
        match name:
            case "move":
                _move_to(x1, y1)
            case "click":
                _move_to(x1, y1)
                time.sleep(0.03)
                _mouse(MOUSEEVENTF_LEFTDOWN)
                time.sleep(0.03)
                _mouse(MOUSEEVENTF_LEFTUP)
            case "right_click":
                _move_to(x1, y1)
                time.sleep(0.03)
                _mouse(MOUSEEVENTF_RIGHTDOWN)
                time.sleep(0.03)
                _mouse(MOUSEEVENTF_RIGHTUP)
            case "double_click":
                _move_to(x1, y1)
                time.sleep(0.03)
                _mouse(MOUSEEVENTF_LEFTDOWN)
                time.sleep(0.03)
                _mouse(MOUSEEVENTF_LEFTUP)
                time.sleep(0.08)
                _mouse(MOUSEEVENTF_LEFTDOWN)
                time.sleep(0.03)
                _mouse(MOUSEEVENTF_LEFTUP)
            case "drag":
                _move_to(x1, y1)
                time.sleep(0.05)
                _mouse(MOUSEEVENTF_LEFTDOWN)
                time.sleep(0.05)
                for step in range(1, drag_steps + 1):
                    t = step / drag_steps
                    _move_to(int(x1 + (x2 - x1) * t), int(y1 + (y2 - y1) * t))
                    time.sleep(drag_step_d)
                _mouse(MOUSEEVENTF_LEFTUP)
            case _:
                log.warning("unknown action name=%s", name)
        time.sleep(action_delay)


def call_vlm(observation: str, annotated_b64: str) -> tuple[str, dict[str, Any], str | None]:
    url = str(_cfg("API_URL", ""))
    u = urllib.parse.urlparse(url)
    host, port = u.hostname or "127.0.0.1", u.port or 80
    path = u.path or "/v1/chat/completions"
    t = float(_cfg("VLM_HTTP_TIMEOUT_SECONDS", 0) or 0)
    timeout = None if t <= 0 else t
    system_prompt = str(_cfg("SYSTEM_PROMPT", ""))
    payload = {
        "model": str(_cfg("MODEL", "")),
        "temperature": float(_cfg("TEMPERATURE", 0.7)),
        "top_p": float(_cfg("TOP_P", 0.9)),
        "max_tokens": int(_cfg("MAX_TOKENS", 1000)),
        "messages": [
            {"role": "system", "content": system_prompt},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": observation},
                    {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{annotated_b64}"}},
                ],
            },
        ],
    }
    body = json.dumps(payload).encode("utf-8")
    log.info("vlm POST %s:%d%s story_len=%d img_len=%d", host, port, path, len(observation), len(annotated_b64))
    try:
        conn = http.client.HTTPConnection(host, port, timeout=timeout)
        conn.request("POST", path, body=body, headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Connection": "close",
        })
        resp = conn.getresponse()
        data = resp.read()
        conn.close()
        if resp.status < 200 or resp.status >= 300:
            return "", {}, f"HTTP {resp.status}"
        obj = json.loads(data.decode("utf-8", "replace"))
        text = cast(str, obj["choices"][0]["message"]["content"])
        usage = cast(dict[str, Any], obj.get("usage", {}) or {})
        return text, usage, None
    except Exception as e:
        log.error("vlm error: %s", e)
        return "", {}, str(e)


async def engine_loop(run_dir: Path) -> None:
    S.run_dir = run_dir
    boot_enabled = bool(_cfg("BOOT_ENABLED", True))
    boot_text = str(_cfg("BOOT_VLM_OUTPUT", ""))
    set_phase("boot" if boot_enabled else "running")
    if boot_enabled and boot_text.strip():
        log.info("engine: injecting boot VLM text len=%d", len(boot_text))
        S.next_vlm_json = boot_text
        S.next_event.set()
    else:
        log.info("engine: waiting for first /inject")
        set_phase("waiting_inject")
    while not STOP.is_set():
        try:
            await asyncio.wait_for(S.next_event.wait(), timeout=0.5)
        except asyncio.TimeoutError:
            continue
        async with S.lock:
            vlm_raw = S.next_vlm_json or ""
            S.next_vlm_json = None
            S.next_event.clear()
        if not vlm_raw.strip():
            continue
        S.turn += 1
        turn = S.turn
        log.info("engine: === TURN %d ===", turn)
        set_phase("running")
        observation, bboxes, actions = parse_vlm_json(vlm_raw)
        async with S.lock:
            S.vlm_json = vlm_raw
            S.observation = observation
            S.actions_text = json.dumps(actions)
            S.bboxes = bboxes
            S.actions = actions
            S.msg_id += 1
        set_phase("executing")
        await asyncio.get_event_loop().run_in_executor(None, execute_actions, actions)
        set_phase("capturing")
        raw_b64, w, h = await asyncio.get_event_loop().run_in_executor(None, capture_screenshot)
        if not raw_b64:
            set_phase("error", "capture failed")
            continue
        S.raw_b64 = raw_b64
        await asyncio.get_event_loop().run_in_executor(
            None, save_turn_data, run_dir, turn, observation, bboxes, actions, raw_b64,
        )
        async with S.lock:
            S.pending_seq = turn
            S.annotated_seq = -1
            S.annotated_b64 = ""
            S.annotated_event.clear()
        set_phase("waiting_annotated")
        log.info("engine: waiting for browser annotated seq=%d", turn)
        while not STOP.is_set():
            try:
                await asyncio.wait_for(S.annotated_event.wait(), timeout=0.5)
                break
            except asyncio.TimeoutError:
                continue
        if STOP.is_set():
            break
        async with S.lock:
            annotated_b64 = S.annotated_b64
        await asyncio.get_event_loop().run_in_executor(None, save_annotated, run_dir, turn, annotated_b64)
        set_phase("calling_vlm")
        new_vlm_text, usage, err = await asyncio.get_event_loop().run_in_executor(
            None, call_vlm, observation, annotated_b64,
        )
        if err:
            log.error("vlm error turn=%d: %s", turn, err)
            S.error = err
            set_phase("vlm_error")
            continue
        log.info("vlm ok turn=%d response_len=%d usage=%s", turn, len(new_vlm_text), usage)
        async with S.lock:
            S.next_vlm_json = new_vlm_text
            S.next_event.set()
        set_phase("running")


class AsyncHTTPServer:
    def __init__(self, host: str, port: int) -> None:
        self._host = host
        self._port = port
        self._server: asyncio.Server | None = None

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._handle_connection, self._host, self._port)
        log.info("server http://%s:%d", self._host, self._port)

    async def stop(self) -> None:
        if self._server:
            self._server.close()
            await self._server.wait_closed()

    async def _handle_connection(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            await self._process(reader, writer)
        except (ConnectionResetError, ConnectionAbortedError, asyncio.IncompleteReadError):
            pass
        except Exception as e:
            if isinstance(e, OSError) and getattr(e, "winerror", None) in (10053, 10054):
                pass
            else:
                log.warning("connection error: %s", e)
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

    async def _process(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        raw_line = await asyncio.wait_for(reader.readline(), timeout=30)
        if not raw_line:
            return
        request_line = raw_line.decode("utf-8", "replace").strip()
        parts = request_line.split(" ")
        if len(parts) < 2:
            return
        method, full_path = parts[0], parts[1]
        path = full_path.split("?", 1)[0]
        headers: dict[str, str] = {}
        while True:
            hl = await asyncio.wait_for(reader.readline(), timeout=10)
            if not hl or hl in (b"\r\n", b"\n"):
                break
            decoded = hl.decode("utf-8", "replace").strip()
            if ":" in decoded:
                k, v = decoded.split(":", 1)
                headers[k.strip().lower()] = v.strip()
        body = b""
        if cl := int(headers.get("content-length", "0")):
            body = await asyncio.wait_for(reader.readexactly(cl), timeout=60)
        match method:
            case "GET":
                await self._do_get(path, writer)
            case "POST":
                await self._do_post(path, body, writer)
            case "OPTIONS":
                await self._send_json(writer, {}, 200)
            case _:
                await self._send_error(writer, 405)

    async def _do_get(self, path: str, writer: asyncio.StreamWriter) -> None:
        match path:
            case "/" | "/index.html":
                data = PANEL_HTML.read_bytes()
                await self._send_raw(writer, 200, "text/html; charset=utf-8", data)
            case "/config":
                await self._send_json(writer, {
                    "ui": _cfg("UI_CONFIG", {}),
                    "capture_width": int(_cfg("CAPTURE_WIDTH", 512)),
                    "capture_height": int(_cfg("CAPTURE_HEIGHT", 288)),
                })
            case "/state":
                async with S.lock:
                    await self._send_json(writer, {
                        "phase": S.phase,
                        "error": S.error,
                        "turn": S.turn,
                        "msg_id": S.msg_id,
                        "pending_seq": S.pending_seq,
                        "annotated_seq": S.annotated_seq,
                        "raw_b64": S.raw_b64,
                        "bboxes": S.bboxes,
                        "actions": S.actions,
                        "observation": S.observation,
                        "vlm_json": S.vlm_json,
                    })
            case _:
                await self._send_error(writer, 404)

    async def _do_post(self, path: str, body: bytes, writer: asyncio.StreamWriter) -> None:
        match path:
            case "/annotated":
                try:
                    obj = json.loads(body.decode("utf-8"))
                except Exception:
                    await self._send_json(writer, {"ok": False, "err": "invalid json"}, 400)
                    return
                seq = obj.get("seq")
                img = obj.get("image_b64", "")
                async with S.lock:
                    expected = S.pending_seq
                if seq != expected:
                    await self._send_json(writer, {"ok": False, "err": f"seq mismatch: got {seq} expected {expected}"}, 409)
                    return
                if not isinstance(img, str) or len(img) < 100:
                    await self._send_json(writer, {"ok": False, "err": "image_b64 too short"}, 400)
                    return
                async with S.lock:
                    S.annotated_b64 = img
                    S.annotated_seq = seq
                    S.annotated_event.set()
                await self._send_json(writer, {"ok": True, "seq": seq})
            case "/inject":
                try:
                    obj = json.loads(body.decode("utf-8"))
                except Exception:
                    await self._send_json(writer, {"ok": False, "err": "invalid json"}, 400)
                    return
                text = obj.get("vlm_text", "")
                if not isinstance(text, str) or not text.strip():
                    await self._send_json(writer, {"ok": False, "err": "vlm_text empty"}, 400)
                    return
                async with S.lock:
                    S.next_vlm_json = text
                    S.next_event.set()
                await self._send_json(writer, {"ok": True})
            case _:
                await self._send_error(writer, 404)

    async def _send_raw(self, writer: asyncio.StreamWriter, code: int, content_type: str, data: bytes) -> None:
        status = {200: "OK", 400: "Bad Request", 404: "Not Found", 405: "Method Not Allowed", 409: "Conflict"}.get(code, "OK")
        hdr = (
            f"HTTP/1.1 {code} {status}\r\n"
            f"Content-Type: {content_type}\r\n"
            f"Content-Length: {len(data)}\r\n"
            f"Cache-Control: no-cache\r\n"
            f"Access-Control-Allow-Origin: *\r\n"
            f"Access-Control-Allow-Methods: GET, POST, OPTIONS\r\n"
            f"Access-Control-Allow-Headers: Content-Type\r\n"
            f"Connection: close\r\n"
            f"\r\n"
        )
        writer.write(hdr.encode("utf-8") + data)
        await writer.drain()

    async def _send_json(self, writer: asyncio.StreamWriter, obj: Any, code: int = 200) -> None:
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        await self._send_raw(writer, code, "application/json", data)

    async def _send_error(self, writer: asyncio.StreamWriter, code: int) -> None:
        await self._send_json(writer, {"error": code}, code)


async def async_main() -> None:
    global S, STOP
    S = EngineState()
    STOP = asyncio.Event()
    run_dir = make_run_dir()
    setup_logging(run_dir)
    log.info("Franz starting run_dir=%s", run_dir)
    log.info("panel=%s config=%s", PANEL_HTML, CONFIG_PATH)
    server = AsyncHTTPServer(HOST, PORT)
    await server.start()
    loop = asyncio.get_event_loop()
    loop.add_signal_handler(signal.SIGINT, lambda: STOP.set()) if hasattr(loop, "add_signal_handler") and os.name != "nt" else None
    try:
        webbrowser.open(f"http://{HOST}:{PORT}")
    except Exception as e:
        log.warning("webbrowser.open failed: %s", e)
    engine_task = asyncio.create_task(engine_loop(run_dir))
    try:
        await STOP.wait()
    except KeyboardInterrupt:
        STOP.set()
    engine_task.cancel()
    await server.stop()
    log.info("Franz stopped")


def main() -> None:
    try:
        asyncio.run(async_main())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
```

---

## `config.py`

```python
HOST = "127.0.0.1"
PORT = 1234

LOG_LEVEL = "INFO"
LOG_TO_FILE = True

API_URL = "http://127.0.0.1:1235/v1/chat/completions"
MODEL = "huihui-qwen3-vl-2b-instruct-abliterated"
TEMPERATURE = 0.7
TOP_P = 0.9
MAX_TOKENS = 1000
VLM_HTTP_TIMEOUT_SECONDS = 0.0

SYSTEM_PROMPT = (
    "You are controlling a Windows desktop via a vision loop.\n"
    "You receive an annotated screenshot where:\n"
    "  - Orange heatmap = regions where actions were previously executed\n"
    "  - Blue heatmap = regions you previously marked as interesting\n\n"
    "You MUST respond with a single JSON object, no other text.\n"
    "Schema:\n"
    "{\n"
    '  "observation": "<your observations, updated world model, max 200 words>",\n'
    '  "bboxes": [\n'
    '    {"x1": int, "y1": int, "x2": int, "y2": int}\n'
    "  ],\n"
    '  "actions": [\n'
    '    {"name": "click"|"right_click"|"double_click"|"drag"|"move", "x1": int, "y1": int, "x2": int, "y2": int}\n'
    "  ]\n"
    "}\n\n"
    "Rules:\n"
    "- x2/y2 are only required for drag.\n"
    "- At most 8 bboxes, at most 6 actions.\n"
    "- Never fabricate feedback. Only describe what you see.\n"
    "- Output ONLY the JSON object, nothing else.\n"
)

CAPTURE_CROP = {"x1": 0, "y1": 0, "x2": 1920, "y2": 1080}
CAPTURE_WIDTH = 512
CAPTURE_HEIGHT = 288
CAPTURE_DELAY = 0.0

RUNS_DIR = "runs"

BOOT_ENABLED = True
BOOT_VLM_OUTPUT = """\
{
  "observation": "I observe the desktop. There is a canvas area in the center of the screen. I will begin by clicking in the center to focus it, then drawing a shape.",
  "bboxes": [
    {"x1": 200, "y1": 150, "x2": 800, "y2": 600}
  ],
  "actions": [
    {"name": "click", "x1": 500, "y1": 400},
    {"name": "drag", "x1": 300, "y1": 300, "x2": 700, "y2": 300},
    {"name": "drag", "x1": 700, "y1": 300, "x2": 700, "y2": 600},
    {"name": "drag", "x1": 700, "y1": 600, "x2": 300, "y2": 600},
    {"name": "drag", "x1": 300, "y1": 600, "x2": 300, "y2": 300}
  ]
}
"""

PHYSICAL_EXECUTION = True
ACTION_DELAY_SECONDS = 0.05
DRAG_DURATION_STEPS = 20
DRAG_STEP_DELAY = 0.01

UI_CONFIG = {
    "executed_heat": {
        "enabled": True,
        "radius_scale": 0.22,
        "stops": [
            [0.00, "rgba(255,40,0,0.88)"],
            [0.25, "rgba(255,80,0,0.70)"],
            [0.55, "rgba(255,120,0,0.35)"],
            [1.00, "rgba(255,160,0,0.00)"],
        ],
    },
    "bbox_heat": {
        "enabled": True,
        "border": "rgba(80,160,255,0.75)",
        "border_width": 2,
        "fill_stops": [
            [0.00, "rgba(80,160,255,0.28)"],
            [0.50, "rgba(80,160,255,0.12)"],
            [1.00, "rgba(80,160,255,0.00)"],
        ],
    },
}
```

---

## `panel.html`

```html
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>Franz</title>
<style>
  *,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
  :root{
    --bg:#0d0d0f;--surface:#16161a;--border:#2a2a35;
    --accent:#4a9eff;--accent2:#ff6b35;
    --text:#e8e8f0;--text-dim:#6b6b80;--text-mid:#a0a0b8;
    --ok:#3ecf8e;--warn:#f0a000;--err:#ff4455;
    --radius:8px;--mono:"Cascadia Code","Fira Code","Consolas",monospace;
    --split-x:62%;--split-y:55%;
  }
  html,body{width:100%;height:100%;overflow:hidden;background:var(--bg);color:var(--text);font-family:system-ui,-apple-system,sans-serif;font-size:13px}
  #root{display:grid;width:100vw;height:100vh;grid-template-columns:var(--split-x) 4px 1fr;grid-template-rows:var(--split-y) 4px 1fr}
  #gutter-v{grid-column:2;grid-row:1/4;background:var(--border);cursor:col-resize;transition:background .15s;z-index:10}
  #gutter-v:hover{background:var(--accent)}
  #gutter-h{grid-column:3;grid-row:2;background:var(--border);cursor:row-resize;transition:background .15s;z-index:10}
  #gutter-h:hover{background:var(--accent)}
  #cross{grid-column:2;grid-row:2;background:var(--accent);cursor:move;z-index:20;border-radius:2px}
  #pane-canvas{grid-column:1;grid-row:1;overflow:hidden;position:relative}
  #pane-vlm{grid-column:3;grid-row:1;overflow:hidden;display:flex;flex-direction:column}
  #pane-log{grid-column:1;grid-row:3;overflow:hidden;display:flex;flex-direction:column}
  #pane-inject{grid-column:3;grid-row:3;overflow:hidden;display:flex;flex-direction:column}
  .pane-header{display:flex;align-items:center;gap:8px;padding:6px 10px;background:var(--surface);border-bottom:1px solid var(--border);flex-shrink:0;font-size:11px;font-weight:600;letter-spacing:.06em;text-transform:uppercase;color:var(--text-dim)}
  .pane-header .badge{margin-left:auto;padding:1px 7px;border-radius:20px;font-size:10px;font-weight:700;letter-spacing:.04em;background:var(--border);color:var(--text-mid)}
  .pane-header .badge.ok{background:#1a3d2e;color:var(--ok)}
  .pane-header .badge.warn{background:#3d2e00;color:var(--warn)}
  .pane-header .badge.err{background:#3d0a10;color:var(--err)}
  .pane-body{flex:1;overflow:auto;padding:10px;scrollbar-width:thin;scrollbar-color:var(--border) transparent}
  #canvas-wrap{width:100%;height:100%;display:flex;align-items:center;justify-content:center;background:#080809;position:relative}
  #canvas-stack{position:relative}
  #canvas-stack canvas{position:absolute;top:0;left:0}
  #c-base{position:relative;display:block}
  #c-heat{pointer-events:none}
  #c-label{pointer-events:none}
  .canvas-status{position:absolute;bottom:8px;right:10px;font-size:10px;color:var(--text-dim);font-family:var(--mono);pointer-events:none}
  #vlm-raw{font-family:var(--mono);font-size:12px;line-height:1.6;white-space:pre-wrap;word-break:break-word;color:var(--text-mid)}
  #vlm-raw .obs{color:#a0d4ff}
  #vlm-raw .acts{color:#ffd080}
  #log-list{font-family:var(--mono);font-size:11px;line-height:1.5;list-style:none}
  #log-list li{padding:1px 0;border-bottom:1px solid #1a1a20}
  #log-list li.info{color:var(--text-dim)}
  #log-list li.ok{color:var(--ok)}
  #log-list li.warn{color:var(--warn)}
  #log-list li.error{color:var(--err)}
  #log-list li time{color:#3a3a50;margin-right:6px}
  #inject-area{flex:1;display:flex;flex-direction:column;gap:8px;padding:10px}
  #inject-textarea{flex:1;background:#0a0a0e;border:1px solid var(--border);border-radius:var(--radius);color:var(--text);font-family:var(--mono);font-size:12px;padding:8px;resize:none;outline:none;transition:border-color .15s;line-height:1.6}
  #inject-textarea:focus{border-color:var(--accent)}
  .inject-row{display:flex;gap:8px;align-items:center;flex-shrink:0}
  button{padding:6px 16px;border-radius:var(--radius);border:1px solid var(--border);background:var(--surface);color:var(--text);font-size:12px;font-weight:600;cursor:pointer;transition:background .12s,border-color .12s,color .12s;white-space:nowrap}
  button:hover{background:var(--border)}
  button.primary{background:var(--accent);color:#000;border-color:var(--accent)}
  button.primary:hover{background:#6ab8ff}
  button.danger{background:transparent;color:var(--err);border-color:var(--err)}
  button.danger:hover{background:#3d0a10}
  #inject-status{font-size:11px;font-family:var(--mono);color:var(--text-dim);flex:1}
  #statusbar{position:fixed;bottom:0;left:0;right:0;height:22px;line-height:22px;background:var(--surface);border-top:1px solid var(--border);display:flex;gap:0;font-size:11px;z-index:100}
  .sb-item{padding:0 12px;border-right:1px solid var(--border);color:var(--text-dim)}
  .sb-item span{color:var(--text-mid)}
  .sb-phase{color:var(--accent)!important}
  ::-webkit-scrollbar{width:6px;height:6px}
  ::-webkit-scrollbar-track{background:transparent}
  ::-webkit-scrollbar-thumb{background:var(--border);border-radius:3px}
  ::-webkit-scrollbar-thumb:hover{background:#44445a}
</style>
</head>
<body>
<div id="root">
  <div id="pane-canvas">
    <div class="pane-header">
      Annotated View
      <span class="badge" id="badge-img">--</span>
    </div>
    <div id="canvas-wrap">
      <div id="canvas-stack">
        <canvas id="c-base"></canvas>
        <canvas id="c-heat"></canvas>
        <canvas id="c-label"></canvas>
      </div>
      <div class="canvas-status" id="canvas-status">no frame</div>
    </div>
  </div>
  <div id="gutter-v"></div>
  <div id="pane-vlm">
    <div class="pane-header">
      VLM Output
      <span class="badge" id="badge-turn">turn 0</span>
    </div>
    <div class="pane-body">
      <pre id="vlm-raw">Waiting for first response...</pre>
    </div>
  </div>
  <div id="cross"></div>
  <div id="gutter-h"></div>
  <div id="pane-log">
    <div class="pane-header">
      Event Log
      <span class="badge" id="badge-phase">init</span>
    </div>
    <div class="pane-body">
      <ul id="log-list"></ul>
    </div>
  </div>
  <div id="pane-inject">
    <div class="pane-header">
      Manual Inject
      <span class="badge" id="badge-inject">idle</span>
    </div>
    <div id="inject-area">
      <textarea id="inject-textarea" placeholder='Paste JSON VLM output here: {"observation":"...","bboxes":[...],"actions":[...]} then click Inject.'></textarea>
      <div class="inject-row">
        <button class="primary" id="btn-inject">Inject</button>
        <button id="btn-clear-inject">Clear</button>
        <span id="inject-status"></span>
      </div>
    </div>
  </div>
</div>
<div id="statusbar">
  <div class="sb-item">Franz</div>
  <div class="sb-item">phase: <span class="sb-phase" id="sb-phase">--</span></div>
  <div class="sb-item">turn: <span id="sb-turn">0</span></div>
  <div class="sb-item">msg: <span id="sb-msg">0</span></div>
  <div class="sb-item">seq: <span id="sb-seq">--</span></div>
  <div class="sb-item" id="sb-error" style="color:var(--err);display:none"></div>
</div>
<script type="module">
'use strict';

let CFG={ui:{},capture_width:512,capture_height:288};

async function loadConfig(){
  try{const r=await fetch('/config');if(r.ok)CFG=await r.json();uiLog('config loaded','ok')}
  catch(e){uiLog(`config load failed: ${e}`,'error')}
}

const logList=document.getElementById('log-list');
const MAX_LOG=200;
function uiLog(msg,level='info'){
  const li=document.createElement('li');li.className=level;
  const t=document.createElement('time');
  t.textContent=new Date().toLocaleTimeString('en-GB',{hour12:false});
  li.appendChild(t);li.appendChild(document.createTextNode(msg));
  logList.prepend(li);
  while(logList.children.length>MAX_LOG)logList.removeChild(logList.lastChild);
}

const root=document.getElementById('root');
const LS_X='franz_split_x',LS_Y='franz_split_y';
let splitX=parseFloat(localStorage.getItem(LS_X)||'62');
let splitY=parseFloat(localStorage.getItem(LS_Y)||'55');

function applyLayout(){
  root.style.gridTemplateColumns=`${splitX}% 4px 1fr`;
  root.style.gridTemplateRows=`${splitY}% 4px 1fr`;
}
applyLayout();

function makeDragger(onMove){
  return function(e){
    e.preventDefault();
    const move=ev=>onMove(ev);
    const up=()=>{window.removeEventListener('mousemove',move);window.removeEventListener('mouseup',up)};
    window.addEventListener('mousemove',move);window.addEventListener('mouseup',up);
  };
}

document.getElementById('gutter-v').addEventListener('mousedown',makeDragger(e=>{
  splitX=Math.max(15,Math.min(85,(e.clientX/window.innerWidth)*100));
  localStorage.setItem(LS_X,splitX);applyLayout();fitCanvas();
}));
document.getElementById('gutter-h').addEventListener('mousedown',makeDragger(e=>{
  splitY=Math.max(15,Math.min(85,(e.clientY/window.innerHeight)*100));
  localStorage.setItem(LS_Y,splitY);applyLayout();fitCanvas();
}));
document.getElementById('cross').addEventListener('mousedown',makeDragger(e=>{
  splitX=Math.max(15,Math.min(85,(e.clientX/window.innerWidth)*100));
  splitY=Math.max(15,Math.min(85,(e.clientY/window.innerHeight)*100));
  localStorage.setItem(LS_X,splitX);localStorage.setItem(LS_Y,splitY);applyLayout();fitCanvas();
}));

const cBase=document.getElementById('c-base');
const cHeat=document.getElementById('c-heat');
const cLabel=document.getElementById('c-label');
const stack=document.getElementById('canvas-stack');
const wrap=document.getElementById('canvas-wrap');
const ctxBase=cBase.getContext('2d');
const ctxHeat=cHeat.getContext('2d');
const ctxLabel=cLabel.getContext('2d');

let canvasW=0,canvasH=0;

function resizeCanvases(w,h){
  if(canvasW===w&&canvasH===h)return;
  canvasW=w;canvasH=h;
  [cBase,cHeat,cLabel].forEach(c=>{c.width=w;c.height=h});
  stack.style.width=`${w}px`;stack.style.height=`${h}px`;
}

function fitCanvas(){
  if(!canvasW||!canvasH)return;
  const ww=wrap.clientWidth-4,wh=wrap.clientHeight-4;
  const scale=Math.min(ww/canvasW,wh/canvasH,1);
  const dw=Math.round(canvasW*scale),dh=Math.round(canvasH*scale);
  stack.style.width=`${dw}px`;stack.style.height=`${dh}px`;
  [cBase,cHeat,cLabel].forEach(c=>{c.style.width=`${dw}px`;c.style.height=`${dh}px`});
}
window.addEventListener('resize',fitCanvas);

function clearLayer(ctx){ctx.clearRect(0,0,canvasW,canvasH)}

function drawExecutedHeat(actions){
  const cfg=(CFG.ui?.executed_heat)||{};
  if(cfg.enabled===false)return;
  const radiusScale=cfg.radius_scale??0.22;
  const stops=cfg.stops??[[0,'rgba(255,40,0,0.88)'],[0.25,'rgba(255,80,0,0.70)'],[0.55,'rgba(255,120,0,0.35)'],[1,'rgba(255,160,0,0)']];
  const r=Math.max(canvasW,canvasH)*radiusScale;
  for(const a of actions){
    const x=a.x1,y=a.y1;
    const grad=ctxHeat.createRadialGradient(x,y,0,x,y,r);
    for(const[pos,col]of stops)grad.addColorStop(pos,col);
    ctxHeat.beginPath();ctxHeat.arc(x,y,r,0,Math.PI*2);ctxHeat.fillStyle=grad;ctxHeat.fill();
    if(a.x2!==undefined&&a.y2!==undefined){
      const x2=a.x2,y2=a.y2;
      const g2=ctxHeat.createRadialGradient(x2,y2,0,x2,y2,r*0.6);
      for(const[pos,col]of stops)g2.addColorStop(pos,col);
      ctxHeat.beginPath();ctxHeat.arc(x2,y2,r*0.6,0,Math.PI*2);ctxHeat.fillStyle=g2;ctxHeat.fill();
      ctxHeat.beginPath();ctxHeat.moveTo(x,y);ctxHeat.lineTo(x2,y2);
      ctxHeat.strokeStyle='rgba(255,100,20,0.35)';ctxHeat.lineWidth=2;ctxHeat.stroke();
    }
  }
}

function drawBboxHeat(bboxes){
  const cfg=(CFG.ui?.bbox_heat)||{};
  if(cfg.enabled===false)return;
  const border=cfg.border??'rgba(80,160,255,0.75)';
  const borderWidth=cfg.border_width??2;
  const fillStops=cfg.fill_stops??[[0,'rgba(80,160,255,0.28)'],[0.5,'rgba(80,160,255,0.12)'],[1,'rgba(80,160,255,0)']];
  for(const bb of bboxes){
    const{x1,y1,x2,y2}=bb;
    const bw=x2-x1,bh=y2-y1;
    if(bw<=0||bh<=0)continue;
    const cx=x1+bw/2,cy=y1+bh/2,rr=Math.max(bw,bh)/2;
    const grad=ctxHeat.createRadialGradient(cx,cy,0,cx,cy,rr);
    for(const[pos,col]of fillStops)grad.addColorStop(pos,col);
    ctxHeat.fillStyle=grad;ctxHeat.fillRect(x1,y1,bw,bh);
    ctxHeat.strokeStyle=border;ctxHeat.lineWidth=borderWidth;
    ctxHeat.strokeRect(x1+borderWidth/2,y1+borderWidth/2,bw-borderWidth,bh-borderWidth);
  }
}

function drawLabels(actions){
  clearLayer(ctxLabel);
  ctxLabel.font='bold 10px "Cascadia Code","Fira Code",Consolas,monospace';
  ctxLabel.textBaseline='bottom';
  actions.forEach((a,i)=>{
    const label=`${i+1}. ${a.name}(${a.x1},${a.y1})`;
    const x=a.x1+6,y=a.y1-3;
    ctxLabel.fillStyle='rgba(0,0,0,0.7)';
    const m=ctxLabel.measureText(label);
    ctxLabel.fillRect(x-2,y-11,m.width+4,13);
    ctxLabel.fillStyle='#fff';ctxLabel.fillText(label,x,y);
  });
}

function loadBaseImage(b64){
  return new Promise((resolve,reject)=>{
    const img=new Image();
    img.onload=()=>{resizeCanvases(img.naturalWidth,img.naturalHeight);ctxBase.drawImage(img,0,0);fitCanvas();resolve()};
    img.onerror=reject;
    img.src=`data:image/png;base64,${b64}`;
  });
}

function exportAnnotated(){
  const off=new OffscreenCanvas(canvasW,canvasH);
  const ctx=off.getContext('2d');
  ctx.drawImage(cBase,0,0);ctx.drawImage(cHeat,0,0);ctx.drawImage(cLabel,0,0);
  return new Promise(resolve=>{
    off.convertToBlob({type:'image/png'}).then(blob=>{
      const reader=new FileReader();
      reader.onload=()=>resolve(reader.result.split(',')[1]);
      reader.readAsDataURL(blob);
    });
  });
}

const vlmRaw=document.getElementById('vlm-raw');

function renderVlmJson(raw,bboxes,actions){
  const escape=s=>s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
  try{
    const obj=JSON.parse(raw);
    const obs=obj.observation||'';
    const actsStr=JSON.stringify(obj.actions||[],null,2);
    const bboxStr=JSON.stringify(obj.bboxes||[],null,2);
    vlmRaw.innerHTML=
      `<span class="obs">observation:\n${escape(obs)}</span>\n\n`+
      `bboxes:\n${escape(bboxStr)}\n\n`+
      `<span class="acts">actions:\n${escape(actsStr)}</span>`;
  }catch{
    vlmRaw.innerHTML=escape(raw);
  }
}

function updateStatusBar(state){
  document.getElementById('sb-phase').textContent=state.phase??'--';
  document.getElementById('sb-turn').textContent=state.turn??0;
  document.getElementById('sb-msg').textContent=state.msg_id??0;
  document.getElementById('sb-seq').textContent=state.pending_seq??'--';
  const errEl=document.getElementById('sb-error');
  if(state.error){errEl.style.display='';errEl.textContent=`error: ${state.error}`}
  else{errEl.style.display='none'}
  const bp=document.getElementById('badge-phase');
  bp.textContent=state.phase??'--';
  bp.className=
    state.phase==='error'||state.phase==='vlm_error'?'badge err':
    state.phase==='running'||state.phase==='calling_vlm'?'badge ok':'badge warn';
  document.getElementById('badge-turn').textContent=`turn ${state.turn}`;
  document.getElementById('canvas-status').textContent=canvasW?`${canvasW}x${canvasH}`:'no frame';
}

let lastMsgId=-1,lastPendingSeq=-1,processing=false;

async function postAnnotated(seq,b64){
  try{
    const r=await fetch('/annotated',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({seq,image_b64:b64})});
    const j=await r.json();
    uiLog(`/annotated seq=${seq} ok=${j.ok}`,j.ok?'ok':'error');return j.ok;
  }catch(e){uiLog(`/annotated POST failed: ${e}`,'error');return false}
}

async function handleNewFrame(state){
  if(processing)return;processing=true;
  try{
    const seq=state.pending_seq,b64=state.raw_b64;
    uiLog(`new frame seq=${seq} b64len=${b64?.length}`,'info');
    document.getElementById('badge-img').textContent=`seq ${seq}`;
    document.getElementById('badge-img').className='badge warn';
    await loadBaseImage(b64);
    clearLayer(ctxHeat);
    drawBboxHeat(state.bboxes||[]);
    drawExecutedHeat(state.actions||[]);
    drawLabels(state.actions||[]);
    if(state.vlm_json)renderVlmJson(state.vlm_json,state.bboxes,state.actions);
    const annotatedB64=await exportAnnotated();
    uiLog(`exported annotated len=${annotatedB64.length}`,'ok');
    const ok=await postAnnotated(seq,annotatedB64);
    document.getElementById('badge-img').textContent=ok?`seq ${seq} ok`:`seq ${seq} fail`;
    document.getElementById('badge-img').className=ok?'badge ok':'badge err';
  }catch(e){uiLog(`handleNewFrame error: ${e}`,'error')}
  finally{processing=false}
}

async function poll(){
  try{
    const r=await fetch('/state');
    if(!r.ok){uiLog(`/state HTTP ${r.status}`,'warn');return}
    const state=await r.json();
    updateStatusBar(state);
    if(state.msg_id!==lastMsgId&&state.vlm_json){
      lastMsgId=state.msg_id;
      uiLog(`new vlm msg_id=${state.msg_id} turn=${state.turn}`,'ok');
      renderVlmJson(state.vlm_json,state.bboxes,state.actions);
    }
    if(state.phase==='waiting_annotated'&&state.pending_seq>0&&state.pending_seq!==lastPendingSeq&&state.raw_b64?.length>100){
      lastPendingSeq=state.pending_seq;
      await handleNewFrame(state);
    }
  }catch(e){uiLog(`poll error: ${e}`,'warn')}
}

setInterval(poll,400);

const injectTA=document.getElementById('inject-textarea');
const injectStatus=document.getElementById('inject-status');
const badgeInject=document.getElementById('badge-inject');

document.getElementById('btn-inject').addEventListener('click',async()=>{
  const text=injectTA.value.trim();
  if(!text){injectStatus.textContent='nothing to inject';return}
  try{
    badgeInject.textContent='sending...';badgeInject.className='badge warn';
    const r=await fetch('/inject',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({vlm_text:text})});
    const j=await r.json();
    if(j.ok){injectStatus.textContent='injected';badgeInject.textContent='ok';badgeInject.className='badge ok';uiLog('manual inject sent','ok')}
    else{injectStatus.textContent=`fail: ${j.err}`;badgeInject.textContent='error';badgeInject.className='badge err';uiLog(`inject failed: ${j.err}`,'error')}
  }catch(e){injectStatus.textContent=`${e}`;badgeInject.textContent='error';badgeInject.className='badge err';uiLog(`inject error: ${e}`,'error')}
});

document.getElementById('btn-clear-inject').addEventListener('click',()=>{
  injectTA.value='';injectStatus.textContent='';badgeInject.textContent='idle';badgeInject.className='badge';
});

(async()=>{
  uiLog('Franz panel starting','info');
  await loadConfig();
  uiLog(`capture size: ${CFG.capture_width}x${CFG.capture_height}`,'info');
  uiLog('polling /state every 400ms','info');
})();
</script>
</body>
</html>
```

---

## Summary of changes

**Architecture:**
- Replaced `threading` + `ThreadingMixIn` HTTP server with a fully `asyncio`-based TCP server implementing raw HTTP/1.1 parsing and response. All coordination uses `asyncio.Event`, `asyncio.Lock`, `asyncio.Task`. Blocking operations (WinAPI capture, mouse actuation, VLM HTTP call, disk I/O) are dispatched via `run_in_executor`.

**VLM output format:**
- Removed all regex parsing (`PART_SPLIT_PATTERN`, `BBOX_REGEX`, `ACTION_REGEX`, `split_parts`, `extract_bboxes`, `extract_actions`). VLM now must output strict JSON: `{"observation": "...", "bboxes": [...], "actions": [...]}`. The `parse_vlm_json` function handles this with a fallback to find the first `{...}` block.
- Updated `SYSTEM_PROMPT` to instruct the VLM to output only JSON.
- Updated `BOOT_VLM_OUTPUT` to be valid JSON.
- Config no longer contains regex fields.

**Single Source of Truth:**
- The browser renders base image + heatmap overlays + labels on layered canvases, then composites them via `OffscreenCanvas` and sends the exact composited PNG back as the annotated image. The VLM receives precisely what the user sees.

**Python 3.13:**
- `match`/`case` with guards for HTTP routing and action dispatch.
- Walrus operator throughout (`:=` in conditionals).
- Modern type union syntax (`str | None`, `tuple[...] | None`).

**Code reduction:**
- Eliminated `_load_config` boilerplate with `_cfg` helper.
- Compressed PNG encoder.
- Deduplicated mouse event helpers into single `_mouse(flags)`.
- Removed all comments, dead code, emojis, non-ASCII from Python.
- Config stripped of regex fields, HTML placeholder text updated for JSON.

**Robustness:**
- `Connection: close` on every HTTP response.
- No timeouts on async event waits (uses 0.5s polling loops).
- Graceful error recovery in connection handler.
- WinError 10053/10054 silenced.







## Prompt Used (trick with HTML-> BASE64)

```
import base64
with open("panel.html", "rb") as f:
    print(base64.b64encode(f.read()).decode("ascii"))
```

```
You are an elite systems programmer who produces extremely clean, minimal, high-performance, and reliable code under hard constraints.

PROJECT: Franz — Full Vision-Action Loop Agent (major refactor)

TASK: Completely refactor and modernize the Franz system.

STRICT REQUIREMENTS — follow every rule without exception:

• Python 3.13 only. Aggressively use every modern 3.13 feature that reduces code or improves clarity (match-case with guards, walrus operator, structural pattern matching, type statements, etc.).
• Windows 11 only — delete all legacy, compatibility, or cross-platform code.
• Latest Google Chrome only for panel.html.
• Zero pip dependencies ever. Only stdlib + ctypes + WinAPI.
• Maximum code deduplication and compression.
• No emojis, no non-ASCII characters, no comments, no dead/commented code.
• Strict Pylance/pyright compatibility and perfect IntelliSense.
• Keep and optimize the pure-Python PNG encoder (make it compact).
• VLM must output strict JSON only — update the system prompt and all parsing logic accordingly (remove all regex/PART 1/PART 2 logic).
• Use a modern asyncio-based server and engine coordination instead of threading + ThreadingMixIn to eliminate threading problems. Prefer asyncio.Event, asyncio.Lock, asyncio.Task, and stdlib asyncio HTTP handling where possible while keeping zero dependencies.
• Ensure the entire system has no timeouts: long-running connections, keep-alive, stable polling, and ability to run autonomously for 10+ hours with near-zero memory growth and no resource leaks.
• SINGLE SOURCE OF TRUTH RULE: The exact image the user sees in the browser panel (raw screenshot + all overlays/heatmaps/labels rendered on the canvas) must be the precise image that gets exported to base64 PNG and sent back to the engine via /annotated. The VLM must receive exactly what the user visually sees — no discrepancy allowed.


Python main will be provided in next message, await for it then await once more, for the config and html file which will be encoded in base64.
```