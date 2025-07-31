import os
import time
import logging
import json
import functools
import requests
from datetime import datetime
from flask import Flask, request, jsonify, Response, render_template
from werkzeug.middleware.proxy_fix import ProxyFix

# ---------- 基础配置 ----------
os.environ['TZ'] = 'Asia/Shanghai'
time.tzset()
logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s - %(levelname)s - %(message)s')

TARGON_CHAT  = "https://api.targon.com/v1/chat/completions"
TARGON_MODELS = "https://api.targon.com/v1/models"

# ---------- Flask ----------
app = Flask(__name__, template_folder='templates')
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1)

# ---------- 工具 ----------
def load_keys():
    raw = os.environ.get("KEYS", "")
    return [k.strip() for k in raw.split(",") if k.strip()]

ALL_KEYS = load_keys()

@functools.lru_cache(maxsize=1)
def get_models():
    """拉取并缓存模型列表（进程级缓存，冷启动后 1 个实例内有效）"""
    if not ALL_KEYS:
        return []
    try:
        r = requests.get(TARGON_MODELS,
                         headers={"Authorization": f"Bearer {ALL_KEYS[0]}"},
                         timeout=8)
        r.raise_for_status()
        return [m["id"] for m in r.json().get("data", [])]
    except Exception as e:
        logging.warning("fetch models failed: %s", e)
        return []

def select_key(model: str) -> str | None:
    """最简单的轮询：用模型名的 hash 选 key，保证并发实例间无共享状态"""
    if not ALL_KEYS:
        return None
    idx = hash(model) % len(ALL_KEYS)
    return ALL_KEYS[idx]

def check_auth(req):
    ak = os.environ.get("AUTHORIZATION_KEY")
    return (not ak) or req.headers.get("Authorization") == f"Bearer {ak}"

# ---------- 路由 ----------
@app.route("/")
def index():
    # 仅展示当前实例瞬时信息，刷新即变
    rpm = tpm = 0   # 无状态，固定 0；如要真实统计需外接 KV
    key_balances = [{"key": "*" * 10 + k[-6:], "balance": "-"} for k in ALL_KEYS]
    return render_template("index.html",
                           rpm=rpm, tpm=tpm,
                           rpd=0, tpd=0,
                           key_balances=key_balances,
                           now=datetime.now().strftime("%Y-%m-%d %H:%M:%S"))

@app.route("/v1/models", methods=["GET"])
def list_models():
    if not check_auth(request):
        return jsonify({"error": "Unauthorized"}), 401
    models = get_models()
    data = [{
        "id": m,
        "object": "model",
        "created": 1678888888,
        "owned_by": "targon",
        "root": m,
        "parent": None
    } for m in models]
    return jsonify({"object": "list", "data": data})

@app.route("/v1/chat/completions", methods=["POST"])
def chat_completions():
    if not check_auth(request):
        return jsonify({"error": "Unauthorized"}), 401

    data = request.get_json()
    model = data.get("model")
    if model not in get_models():
        return jsonify({"error": "Invalid model"}), 400

    if "max_tokens" not in data:
        data["max_tokens"] = 0

    key = select_key(model)
    if not key:
        return jsonify({"error": "No available key"}), 429

    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json"
    }

    try:
        resp = requests.post(
            TARGON_CHAT,
            headers=headers,
            json=data,
            stream=data.get("stream", False),
            timeout=55  # Vercel 上限 60 s
        )
        if resp.status_code == 429:
            return jsonify(resp.json()), 429

        if data.get("stream"):
            def gen():
                for chunk in resp.iter_content(chunk_size=2048):
                    yield chunk
            return Response(gen(), content_type="text/event-stream")

        return jsonify(resp.json())

    except Exception as e:
        logging.error("proxy error: %s", e)
        return jsonify({"error": str(e)}), 500

# ---------- Vercel 入口 ----------
# 保持文件名 api/index.py，vercel 会自动识别
# 无需 app.run()，vercel 使用 gunicorn 托管
