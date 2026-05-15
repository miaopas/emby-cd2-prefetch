import os
import shutil
import logging
import threading
from urllib.parse import urlparse
from flask import Flask, request, jsonify

# ─── 配置 ────────────────────────────────────────────────────────────────────
EMBY_PATH_PREFIX  = os.getenv("EMBY_PATH_PREFIX", "/CloudNAS/CloudDrive")
CD2_PATH_PREFIX   = os.getenv("CD2_PATH_PREFIX", "")
CD2_FUSE_MOUNT    = os.getenv("CD2_FUSE_MOUNT", "")      # 容器内 CD2 FUSE 挂载点，如 /mnt/cd2_drive
LOCAL_CACHE_PATH  = os.getenv("LOCAL_CACHE_PATH", "")    # 容器内本地缓存路径，如 /mnt/scratch
PORT              = int(os.getenv("PORT", "8094"))

# ─── 日志 ────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
log = logging.getLogger(__name__)

app = Flask(__name__)

# ─── 路径转换 ─────────────────────────────────────────────────────────────────
def emby_path_to_cd2(emby_path: str) -> str:
    """
    /CloudNAS/CloudDrvie/115open/Symedia/... → /115open/Symedia/...
    """
    if emby_path.startswith(EMBY_PATH_PREFIX):
        return CD2_PATH_PREFIX + emby_path[len(EMBY_PATH_PREFIX):]
    return emby_path

# ─── STRM 解析 ───────────────────────────────────────────────────────────────
def resolve_strm_path(strm_path: str) -> str | None:
    """读取 .strm 文件，返回实际媒体路径（支持本地路径和 URL 格式）"""
    try:
        with open(strm_path, "r", encoding="utf-8") as f:
            content = f.read().strip()
        if not content:
            return None
        if content.startswith(("http://", "https://")):
            return urlparse(content).path
        return content
    except OSError as e:
        log.warning(f"[strm] 无法读取（容器是否挂载了媒体目录？）: {strm_path}: {e}")
        return None

# ─── 复制缓存 ─────────────────────────────────────────────────────────────────
def _write_local_strm(strm_path: str, local_file_path: str):
    """在原 .strm 旁边创建 .local.strm，内容指向本地缓存文件"""
    local_strm = os.path.splitext(strm_path)[0] + ".local.strm"
    with open(local_strm, "w", encoding="utf-8") as f:
        f.write(local_file_path)
    log.info(f"[copy] .local.strm 已创建: {local_strm}")


def copy_to_local(cd2_path: str, strm_path: str = ""):
    """
    从 CD2 FUSE 挂载复制文件到本地缓存，并写 .local.strm。
    cd2_path:  /115open/Symedia/.../movie.mkv
    strm_path: /mnt/media_local/.../movie.strm（可选，用于创建 .local.strm）
    """
    if not CD2_FUSE_MOUNT or not LOCAL_CACHE_PATH:
        log.error("[copy] CD2_FUSE_MOUNT 或 LOCAL_CACHE_PATH 未配置")
        return

    fuse_path  = CD2_FUSE_MOUNT + cd2_path
    local_path = LOCAL_CACHE_PATH + cd2_path

    # 已存在则跳过复制，直接写 .local.strm
    if os.path.exists(local_path):
        log.info(f"[copy] 已存在，跳过: {local_path}")
        if strm_path:
            _write_local_strm(strm_path, local_path)
        return

    os.makedirs(os.path.dirname(local_path), exist_ok=True)
    tmp_path = local_path + ".tmp"

    try:
        file_size = os.path.getsize(fuse_path)
        log.info(f"[copy] 开始: {fuse_path} ({file_size / 1024**3:.1f} GB)")

        copied = 0
        last_log = 0
        chunk_size = 4 * 1024 * 1024   # 4 MB
        log_interval = 512 * 1024 * 1024  # 每 512 MB 打一次日志

        with open(fuse_path, "rb") as src, open(tmp_path, "wb") as dst:
            while data := src.read(chunk_size):
                dst.write(data)
                copied += len(data)
                if copied - last_log >= log_interval:
                    last_log = copied
                    log.info(f"[copy] 进度 {copied / 1024**3:.1f} / {file_size / 1024**3:.1f} GB")

        os.rename(tmp_path, local_path)
        log.info(f"[copy] 完成: {local_path}")

        if strm_path:
            _write_local_strm(strm_path, local_path)

    except Exception as e:
        log.error(f"[copy] 失败: {e}")
        if os.path.exists(tmp_path):
            os.remove(tmp_path)

# ─── Emby Webhook 端点 ────────────────────────────────────────────────────────
@app.route("/webhook/emby", methods=["POST"])
def emby_webhook():
    try:
        data = request.json or {}
    except Exception:
        data = {}

    event     = data.get("Event", "")
    item      = data.get("Item", {})
    item_type = item.get("Type", "")
    path      = item.get("Path", "")

    log.info(f"[webhook] Event={event} Type={item_type} Path={path}")

    if event not in ("item.rate",):
        return jsonify({"status": "ignored", "reason": f"event={event}"}), 200

    if item_type not in ("Movie", "Episode", "Video", ""):
        return jsonify({"status": "ignored", "reason": f"type={item_type}"}), 200

    is_favorite = (
        data.get("IsFavorite")
        or item.get("IsFavorite")
        or item.get("UserData", {}).get("IsFavorite")
    )
    if is_favorite is False or is_favorite == "false":
        log.info(f"[webhook] 取消收藏，跳过: {path}")
        return jsonify({"status": "ignored", "reason": "unfavorite"}), 200

    if not path:
        return jsonify({"status": "ignored", "reason": "no path"}), 200

    # STRM 文件：读取实际媒体路径
    if path.lower().endswith(".strm"):
        real_path = resolve_strm_path(path)
        if real_path is None:
            log.warning(f"[webhook] STRM 无法读取，跳过: {path}")
            return jsonify({"status": "ignored", "reason": "strm unreadable"}), 200
        log.info(f"[webhook] STRM 指向: {real_path}")
        cd2_path = emby_path_to_cd2(real_path)
        strm_path = path
    else:
        if not path.startswith(EMBY_PATH_PREFIX):
            log.info(f"[webhook] 非网盘路径，跳过: {path}")
            return jsonify({"status": "ignored", "reason": "local path"}), 200
        cd2_path = emby_path_to_cd2(path)
        strm_path = ""

    log.info(f"[webhook] CD2路径: {cd2_path}")

    t = threading.Thread(target=copy_to_local, args=(cd2_path, strm_path), daemon=True)
    t.start()

    return jsonify({"status": "ok", "message": "复制已触发", "cd2_path": cd2_path}), 200


# ─── 手动触发端点 ─────────────────────────────────────────────────────────────
@app.route("/prefetch", methods=["POST"])
def manual_prefetch():
    """手动触发: POST /prefetch  {"path": "/115open/...", "strm_path": "/mnt/media_local/...（可选）"}"""
    data      = request.json or {}
    path      = data.get("path", "")
    strm_path = data.get("strm_path", "")
    if not path:
        return jsonify({"status": "error", "message": "path 不能为空"}), 400

    t = threading.Thread(target=copy_to_local, args=(path, strm_path), daemon=True)
    t.start()
    return jsonify({"status": "ok", "message": "复制已触发", "path": path}), 200


# ─── 健康检查 ─────────────────────────────────────────────────────────────────
@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"})


if __name__ == "__main__":
    log.info(f"服务启动 port={PORT}")
    log.info(f"路径前缀: {EMBY_PATH_PREFIX} → CD2_FUSE={CD2_FUSE_MOUNT}")
    log.info(f"本地缓存: {LOCAL_CACHE_PATH}")
    app.run(host="0.0.0.0", port=PORT)
