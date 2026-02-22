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
