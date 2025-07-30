import os
import time
import logging
import json
import uuid
import threading
import requests
from datetime import datetime
from apscheduler.schedulers.background import BackgroundScheduler
from flask import Flask, request, jsonify, Response, render_template
from werkzeug.middleware.proxy_fix import ProxyFix

# ------------- 基础配置 -------------
os.environ['TZ'] = 'Asia/Shanghai'
time.tzset()
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# ------------- Targon 端点 -------------
TARGON_CHAT = "https://api.targon.com/v1/chat/completions"
TARGON_MODELS = "https://api.targon.com/v1/models"

# ------------- Flask 初始化 -------------
app = Flask(__name__)
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1)

# ------------- 全局变量 -------------
text_models = []
all_keys = []               # 所有 key 一视同仁
model_key_idx = {}          # 轮询索引
data_lock = threading.Lock()
request_timestamps = []
token_counts = []
request_timestamps_day = []
token_counts_day = []

# ------------- 工具函数 -------------
def load_keys():
    global all_keys
    raw = os.environ.get("KEYS", "")
    all_keys = [k.strip() for k in raw.split(",") if k.strip()]
    logging.info(f"已加载 {len(all_keys)} 个 API Key")

def refresh_models():
    global text_models
    if not all_keys:
        logging.warning("没有可用 key，无法拉取模型列表")
        text_models = []
        return
    key = all_keys[0]
    try:
        resp = requests.get(
            TARGON_MODELS,
            headers={"Authorization": f"Bearer {key}"},
            timeout=10
        )
        resp.raise_for_status()
        data = resp.json()
        text_models = [m["id"] for m in data.get("data", []) if m.get("id")]
    except Exception as e:
        logging.error(f"拉取模型列表失败: {e}")
        text_models = []
    logging.info(f"已拉取 {len(text_models)} 个 text 模型")

def select_key(model_name):
    if not all_keys:
        return None
    idx = model_key_idx.get(model_name, 0) % len(all_keys)
    key = all_keys[idx]
    model_key_idx[model_name] = idx + 1
    return key

def check_auth(req):
    ak = os.environ.get("AUTHORIZATION_KEY")
    if not ak:
        return True
    return req.headers.get("Authorization") == f"Bearer {ak}"

# ------------- 路由 -------------
@app.route("/")
def index():
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    rpm, tpm, rpd, tpd = 0, 0, 0, 0
    with data_lock:
        rpm, tpm = len(request_timestamps), sum(token_counts)
        rpd, tpd = len(request_timestamps_day), sum(token_counts_day)
    key_balances = [{"key": "*" * 10 + k[-6:], "balance": "-"} for k in all_keys]
    return render_template("index.html", rpm=rpm, tpm=tpm, rpd=rpd, tpd=tpd,
                           key_balances=key_balances, now=now)

@app.route("/v1/models", methods=["GET"])
def list_models():
    if not check_auth(request):
        return jsonify({"error": "Unauthorized"}), 401
    payload = [{
        "id": m,
        "object": "model",
        "created": 1678888888,
        "owned_by": "targon",
        "root": m,
        "parent": None
    } for m in text_models]
    return jsonify({"object": "list", "data": payload})

@app.route("/v1/chat/completions", methods=["POST"])
def chat_completions():
    if not check_auth(request):
        return jsonify({"error": "Unauthorized"}), 401

    data = request.get_json()
    model = data.get("model")
    if model not in text_models:
        return jsonify({"error": "Invalid model"}), 400

    # 1. 默认 max_tokens = 32000
    if "max_tokens" not in data:
        data["max_tokens"] = 32000

    key = select_key(model)
    if not key:
        return jsonify({"error": "No available key"}), 429

    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}

    try:
        resp = requests.post(
            TARGON_CHAT,
            headers=headers,
            json=data,
            stream=data.get("stream", False),
            timeout=120
        )
        if resp.status_code == 429:
            return jsonify(resp.json()), 429

        if data.get("stream"):
            def gen():
                total_tokens = 0
                buffer = b""
                for chunk in resp.iter_content(chunk_size=2048):
                    buffer += chunk
                    yield chunk
                # 解析最后一个 chunk
                try:
                    # 找到最后一个 data: {...} 块
                    lines = buffer.decode("utf-8").strip().splitlines()
                    for line in reversed(lines):
                        if line.startswith("data: "):
                            payload = line[6:]
                            if payload == "[DONE]":
                                continue
                            js = json.loads(payload)
                            total_tokens = js.get("usage", {}).get("total_tokens", 0)
                            break
                except Exception:
                    total_tokens = 0

                with data_lock:
                    now_ts = time.time()
                    request_timestamps.append(now_ts)
                    request_timestamps_day.append(now_ts)
                    token_counts.append(total_tokens)
                    token_counts_day.append(total_tokens)

            return Response(gen(), content_type="text/event-stream")
        else:
            js = resp.json()
            total_tokens = js.get("usage", {}).get("total_tokens", 0)

            with data_lock:
                now_ts = time.time()
                request_timestamps.append(now_ts)
                request_timestamps_day.append(now_ts)
                token_counts.append(total_tokens)
                token_counts_day.append(total_tokens)

            return jsonify(js)

    except Exception as e:
        logging.error(f"转发失败: {e}")
        return jsonify({"error": str(e)}), 500

# ------------- 启动 -------------
load_keys()
refresh_models()
scheduler = BackgroundScheduler()
scheduler.add_job(refresh_models, "interval", hours=1)
scheduler.start()

if __name__ == "__main__":
    app.run(debug=False, host="0.0.0.0", port=int(os.environ.get("PORT", 7860)))
