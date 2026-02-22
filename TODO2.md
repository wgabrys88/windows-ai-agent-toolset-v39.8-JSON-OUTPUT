Yes — this change is effectively independent of the earlier normalized-coordinates work, because it only touches the disk artifact writers (`save_turn_data`, `save_annotated`) and adds a config toggle. The rest of the agent loop, panel flow, and screenshot quality/encoding path remain unchanged.

Here is a single unified diff to implement:

* `LOG_LAYOUT` toggle in `config.py` (`"flat"` new behavior, `"turn_dirs"` old behavior)
* flat image naming in the run directory
* append-only `turns.jsonl` (JSONL), with two records per turn: `stage="raw"` and `stage="annotated"`
* full backward compatibility (when `LOG_LAYOUT != "flat"`, it keeps the original per-turn folders + per-turn JSON file)

```diff
--- a/main.py
+++ b/main.py
@@ -344,10 +344,32 @@
     log.info("parse_vlm_json obs_len=%d bboxes=%d actions=%d", len(observation), len(bboxes), len(actions))
     return observation, bboxes, actions
 
 
+def _append_jsonl(path: Path, obj: dict[str, Any]) -> None:
+    try:
+        with path.open("a", encoding="utf-8") as f:
+            f.write(json.dumps(obj, ensure_ascii=False, separators=(",", ":")))
+            f.write("\n")
+    except Exception as e:
+        log.warning("append jsonl failed: %s", e)
+
+
 def save_turn_data(
     run_dir: Path, turn: int, observation: str,
     bboxes: list[dict[str, Any]], actions: list[dict[str, Any]], raw_b64: str,
 ) -> None:
+    layout = str(_cfg("LOG_LAYOUT", "turn_dirs")).lower()
+    if layout == "flat":
+        raw_name = f"turn_{turn:04d}_raw.png"
+        if raw_b64:
+            try:
+                (run_dir / raw_name).write_bytes(base64.b64decode(raw_b64))
+            except Exception as e:
+                log.warning("save raw png failed: %s", e)
+        _append_jsonl(
+            run_dir / "turns.jsonl",
+            {"turn": turn, "stage": "raw", "observation": observation, "bboxes": bboxes, "actions": actions, "raw_png": raw_name},
+        )
+        return
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
+    layout = str(_cfg("LOG_LAYOUT", "turn_dirs")).lower()
+    if layout == "flat":
+        ann_name = f"turn_{turn:04d}_annotated.png"
+        try:
+            (run_dir / ann_name).write_bytes(base64.b64decode(annotated_b64))
+        except Exception as e:
+            log.warning("save annotated png failed: %s", e)
+        _append_jsonl(run_dir / "turns.jsonl", {"turn": turn, "stage": "annotated", "annotated_png": ann_name})
+        return
     td = run_dir / f"turn_{turn:04d}"
     td.mkdir(exist_ok=True)
     try:
         (td / "screenshot_annotated.png").write_bytes(base64.b64decode(annotated_b64))
     except Exception as e:
         log.warning("save annotated png failed: %s", e)
--- a/config.py
+++ b/config.py
@@ -40,6 +40,7 @@
 CAPTURE_DELAY = 0.0
 
 RUNS_DIR = "runs"
+LOG_LAYOUT = "flat"
 
 BOOT_ENABLED = True
 BOOT_VLM_OUTPUT = """\
 {
```

### How to use

* New layout (what you want): `LOG_LAYOUT = "flat"`

  * produces `turn_0001_raw.png`, `turn_0001_annotated.png`, and `turns.jsonl` in the run folder
* Old layout: `LOG_LAYOUT = "turn_dirs"`

  * keeps `turn_0001/` folders and `vlm_output.json` per turn

If you want, next we can also add a small header line creation for `turns.jsonl` (optional), or keep it pure JSONL as above.
