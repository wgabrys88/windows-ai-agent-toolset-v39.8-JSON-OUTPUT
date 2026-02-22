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
