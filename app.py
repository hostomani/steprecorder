#!/usr/bin/env python3
"""Web app to view and edit step recordings as guides."""

import base64
import io
import json
import os
import shutil
import signal
import subprocess
import sys
from pathlib import Path

from flask import Flask, render_template, request, jsonify, send_from_directory
from PIL import Image

app = Flask(__name__)

BASE_DIR = Path(__file__).parent
RECORDINGS_DIR = BASE_DIR / "recordings"

# Track the recorder subprocess
recorder_process = None
recorder_session_name = None


def load_steps(recording_name):
    steps_file = RECORDINGS_DIR / recording_name / "steps.json"
    if not steps_file.exists():
        return None
    with open(steps_file) as f:
        return json.load(f)


def save_steps(recording_name, data):
    steps_file = RECORDINGS_DIR / recording_name / "steps.json"
    with open(steps_file, "w") as f:
        json.dump(data, f, indent=2, default=str)


@app.route("/")
def index():
    recordings = []
    if RECORDINGS_DIR.exists():
        for entry in sorted(RECORDINGS_DIR.iterdir()):
            if entry.is_dir() and (entry / "steps.json").exists():
                data = load_steps(entry.name)
                if not data:
                    continue
                steps_with_screenshots = [
                    s for s in data.get("steps", []) if s.get("screenshot")
                ]
                thumbnail = None
                if steps_with_screenshots:
                    thumbnail = steps_with_screenshots[0]["screenshot"]
                recordings.append({
                    "name": entry.name,
                    "title": entry.name.replace("_", " ").replace("-", " ").title(),
                    "total_steps": len(steps_with_screenshots),
                    "date": data.get("start_time", "")[:10],
                    "thumbnail": thumbnail,
                })
    return render_template("index.html", recordings=recordings)


@app.route("/recording/<name>")
def recording(name):
    data = load_steps(name)
    if not data:
        return "Recording not found", 404
    steps = [s for s in data.get("steps", []) if s.get("screenshot")]
    title = name.replace("_", " ").replace("-", " ").title()
    preamble = data.get("preamble", "")
    return render_template("recording.html", name=name, title=title, steps=steps, preamble=preamble)


@app.route("/guide/<name>")
def guide(name):
    data = load_steps(name)
    if not data:
        return "Recording not found", 404
    steps = [s for s in data.get("steps", []) if s.get("screenshot")]
    title = name.replace("_", " ").replace("-", " ").title()
    # Collect all guides for the sidebar
    all_guides = []
    if RECORDINGS_DIR.exists():
        for entry in sorted(RECORDINGS_DIR.iterdir()):
            if entry.is_dir() and (entry / "steps.json").exists():
                g_data = load_steps(entry.name)
                if not g_data:
                    continue
                g_steps = [s for s in g_data.get("steps", []) if s.get("screenshot")]
                all_guides.append({
                    "name": entry.name,
                    "title": entry.name.replace("_", " ").replace("-", " ").title(),
                    "total_steps": len(g_steps),
                })
    preamble = data.get("preamble", "")
    return render_template("guide.html", name=name, title=title, steps=steps, all_guides=all_guides, preamble=preamble)


@app.route("/recordings/<path:filepath>")
def serve_screenshot(filepath):
    return send_from_directory(RECORDINGS_DIR, filepath)


@app.route("/recording/<name>/preamble", methods=["POST"])
def update_preamble(name):
    data = load_steps(name)
    if not data:
        return jsonify({"error": "Recording not found"}), 404
    body = request.get_json()
    data["preamble"] = body.get("preamble", "")
    save_steps(name, data)
    return jsonify({"ok": True})


@app.route("/recording/<name>/step/<int:step_num>/description", methods=["POST"])
def update_description(name, step_num):
    data = load_steps(name)
    if not data:
        return jsonify({"error": "Recording not found"}), 404
    body = request.get_json()
    description = body.get("description", "")
    for step in data["steps"]:
        if step["step_number"] == step_num:
            if "details" not in step or step["details"] is None:
                step["details"] = {}
            step["details"]["description"] = description
            save_steps(name, data)
            return jsonify({"ok": True})
    return jsonify({"error": "Step not found"}), 404


@app.route("/recording/<name>/step/<int:step_num>/crop", methods=["POST"])
def crop_screenshot(name, step_num):
    data = load_steps(name)
    if not data:
        return jsonify({"error": "Recording not found"}), 404
    body = request.get_json()
    x = int(body["x"])
    y = int(body["y"])
    w = int(body["width"])
    h = int(body["height"])
    for step in data["steps"]:
        if step["step_number"] == step_num and step.get("screenshot"):
            img_path = RECORDINGS_DIR / name / step["screenshot"]
            if not img_path.exists():
                return jsonify({"error": "Image not found"}), 404
            img = Image.open(img_path)
            cropped = img.crop((x, y, x + w, y + h))
            cropped.save(img_path)
            return jsonify({"ok": True})
    return jsonify({"error": "Step not found"}), 404


@app.route("/recording/<name>/step/<int:step_num>/annotate", methods=["POST"])
def annotate_screenshot(name, step_num):
    data = load_steps(name)
    if not data:
        return jsonify({"error": "Recording not found"}), 404
    body = request.get_json()
    image_data = body.get("image_data", "")
    if not image_data.startswith("data:image/png;base64,"):
        return jsonify({"error": "Invalid image data"}), 400
    raw = base64.b64decode(image_data.split(",", 1)[1])
    overlay = Image.open(io.BytesIO(raw)).convert("RGBA")
    for step in data["steps"]:
        if step["step_number"] == step_num and step.get("screenshot"):
            img_path = RECORDINGS_DIR / name / step["screenshot"]
            if not img_path.exists():
                return jsonify({"error": "Image not found"}), 404
            bg = Image.open(img_path).convert("RGBA")
            if overlay.size != bg.size:
                overlay = overlay.resize(bg.size, Image.LANCZOS)
            composite = Image.alpha_composite(bg, overlay)
            composite.convert("RGB").save(img_path, "PNG")
            return jsonify({"ok": True})
    return jsonify({"error": "Step not found"}), 404


@app.route("/recording/<name>/step/<int:step_num>", methods=["DELETE"])
def delete_step(name, step_num):
    data = load_steps(name)
    if not data:
        return jsonify({"error": "Recording not found"}), 404
    for i, step in enumerate(data["steps"]):
        if step["step_number"] == step_num:
            if step.get("screenshot"):
                img_path = RECORDINGS_DIR / name / step["screenshot"]
                if img_path.exists():
                    img_path.unlink()
            data["steps"].pop(i)
            data["total_steps"] = len(data["steps"])
            save_steps(name, data)
            return jsonify({"ok": True})
    return jsonify({"error": "Step not found"}), 404


@app.route("/recording/<name>", methods=["DELETE"])
def delete_recording(name):
    recording_dir = RECORDINGS_DIR / name
    if not recording_dir.exists() or not recording_dir.is_dir():
        return jsonify({"error": "Recording not found"}), 404
    shutil.rmtree(recording_dir)
    return jsonify({"ok": True})


@app.route("/recorder/start", methods=["POST"])
def recorder_start():
    global recorder_process, recorder_session_name
    if recorder_process and recorder_process.poll() is None:
        return jsonify({"error": "Already recording", "session": recorder_session_name}), 409
    body = request.get_json() or {}
    name = body.get("name", "").strip()
    if not name:
        return jsonify({"error": "Session name is required"}), 400
    # Sanitize: only allow alphanumeric, dash, underscore
    safe_name = "".join(c if c.isalnum() or c in "-_" else "_" for c in name)
    python = sys.executable
    recorder_process = subprocess.Popen(
        [python, str(BASE_DIR / "main.py"), "--name", safe_name],
        cwd=str(BASE_DIR),
    )
    recorder_session_name = safe_name
    return jsonify({"ok": True, "session": safe_name, "pid": recorder_process.pid})


@app.route("/recorder/stop", methods=["POST"])
def recorder_stop():
    global recorder_process, recorder_session_name
    if not recorder_process or recorder_process.poll() is not None:
        recorder_process = None
        recorder_session_name = None
        return jsonify({"error": "Not recording"}), 409
    recorder_process.send_signal(signal.SIGINT)
    try:
        recorder_process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        recorder_process.terminate()
    session = recorder_session_name
    recorder_process = None
    recorder_session_name = None
    return jsonify({"ok": True, "session": session})


@app.route("/recorder/status")
def recorder_status():
    running = recorder_process is not None and recorder_process.poll() is None
    if not running and recorder_process is not None:
        # Process ended on its own — clean up
        global recorder_session_name
        globals()["recorder_process"] = None
        recorder_session_name = None
    return jsonify({"recording": running, "session": recorder_session_name})


if __name__ == "__main__":
    app.run(debug=True, port=5050)
