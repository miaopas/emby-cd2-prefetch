import os
import subprocess
import logging
import threading
from urllib.parse import urlparse
from flask import Flask, request, jsonify

# ─── 配置 ────────────────────────────────────────────────────────────────────
EMBY_PATH_PREFIX  = os.getenv("EMBY_PATH_PREFIX", "/CloudNAS/CloudDrive")
CD2_PATH_PREFIX   = os.getenv("CD2_PATH_PREFIX", "")
CD2_FUSE_MOUNT    = os.getenv("CD2_FUSE_MOUNT", "")      # 容器内 CD2 FUSE 挂载点，如 /mnt/cd2_drive
LOCAL_CACHE_PATH  = os.getenv("LOCAL_CACHE_PATH", "")    # 容器内本地缓存路径，如 /mnt/scratch
DELETE_DELAY      = int(os.getenv("DELETE_DELAY", "60")) # 取消收藏后延迟多少秒再删除
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


_running_copies: dict[str, subprocess.Popen] = {}
_copies_lock = threading.Lock()


def copy_to_local(cd2_path: str, strm_path: str = ""):
    """
    用 rsync 从 CD2 FUSE 挂载复制文件到本地缓存，支持断点续传，并写 .local.strm。
    cd2_path:  /115open/Symedia/.../movie.mkv
    strm_path: /mnt/media_local/.../movie.strm（可选，用于创建 .local.strm）
    """
    if not CD2_FUSE_MOUNT or not LOCAL_CACHE_PATH:
        log.error("[copy] CD2_FUSE_MOUNT 或 LOCAL_CACHE_PATH 未配置")
        return

    with _copies_lock:
        if cd2_path in _running_copies:
            log.info(f"[copy] 已在复制中，跳过重复请求: {cd2_path}")
            return

    fuse_path  = CD2_FUSE_MOUNT + cd2_path
    local_path = LOCAL_CACHE_PATH + cd2_path

    os.makedirs(os.path.dirname(local_path), exist_ok=True)

    try:
        log.info(f"[copy] rsync 开始: {fuse_path} → {local_path}")
        proc = subprocess.Popen(
            ["rsync", "--partial", "-a", fuse_path, local_path],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )
        with _copies_lock:
            _running_copies[cd2_path] = proc

        _, stderr = proc.communicate()

        with _copies_lock:
            _running_copies.pop(cd2_path, None)

        if proc.returncode == 0:
            log.info(f"[copy] rsync 完成: {local_path}")
            if strm_path:
                _write_local_strm(strm_path, local_path)
        elif proc.returncode == -9:
            log.info(f"[copy] rsync 已被终止（取消收藏）: {cd2_path}")
        else:
            log.error(f"[copy] rsync 失败 (exit={proc.returncode}): {stderr.decode().strip()}")
    except FileNotFoundError:
        log.error("[copy] rsync 未找到，请检查 Dockerfile 是否安装了 rsync")
    except Exception as e:
        log.error(f"[copy] rsync 异常: {e}")

# ─── 延迟删除（可取消）────────────────────────────────────────────────────────
_pending_deletes: dict[str, threading.Timer] = {}
_pending_lock = threading.Lock()


def _do_delete(cd2_path: str, strm_path: str):
    with _pending_lock:
        _pending_deletes.pop(cd2_path, None)
    # 如果正在下载，先 kill rsync
    with _copies_lock:
        proc = _running_copies.pop(cd2_path, None)
    if proc:
        proc.kill()
        log.info(f"[delete] 已终止正在进行的下载: {cd2_path}")
    delete_local(cd2_path, strm_path)


def schedule_delete(cd2_path: str, strm_path: str):
    with _pending_lock:
        existing = _pending_deletes.pop(cd2_path, None)
        if existing:
            existing.cancel()
        timer = threading.Timer(DELETE_DELAY, _do_delete, args=(cd2_path, strm_path))
        timer.daemon = True
        timer.start()
        _pending_deletes[cd2_path] = timer
    log.info(f"[delete] 将在 {DELETE_DELAY}s 后删除: {cd2_path}")


def cancel_delete(cd2_path: str):
    with _pending_lock:
        timer = _pending_deletes.pop(cd2_path, None)
    if timer:
        timer.cancel()
        log.info(f"[delete] 已取消待删除任务: {cd2_path}")


# ─── 删除缓存 ─────────────────────────────────────────────────────────────────
def delete_local(cd2_path: str, strm_path: str = ""):
    """取消收藏时删除本地缓存文件和 .local.strm"""
    local_path = LOCAL_CACHE_PATH + cd2_path

    if os.path.exists(local_path):
        try:
            os.remove(local_path)
            log.info(f"[delete] 已删除: {local_path}")
        except Exception as e:
            log.error(f"[delete] 删除失败: {local_path}: {e}")
    else:
        log.info(f"[delete] 缓存文件不存在，跳过: {local_path}")

    if strm_path:
        local_strm = os.path.splitext(strm_path)[0] + ".local.strm"
        if os.path.exists(local_strm):
            try:
                os.remove(local_strm)
                log.info(f"[delete] .local.strm 已删除: {local_strm}")
            except Exception as e:
                log.error(f"[delete] 删除 .local.strm 失败: {local_strm}: {e}")


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

    if not path:
        return jsonify({"status": "ignored", "reason": "no path"}), 200

    # 解析路径（STRM 或直接路径）
    if path.lower().endswith(".strm"):
        real_path = resolve_strm_path(path)
        if real_path is None:
            log.warning(f"[webhook] STRM 无法读取，跳过: {path}")
            return jsonify({"status": "ignored", "reason": "strm unreadable"}), 200
        log.info(f"[webhook] STRM 指向: {real_path}")
        cd2_path  = emby_path_to_cd2(real_path)
        strm_path = path
    else:
        if not path.startswith(EMBY_PATH_PREFIX):
            log.info(f"[webhook] 非网盘路径，跳过: {path}")
            return jsonify({"status": "ignored", "reason": "local path"}), 200
        cd2_path  = emby_path_to_cd2(path)
        strm_path = ""

    log.info(f"[webhook] CD2路径: {cd2_path}")

    is_favorite = (
        data.get("IsFavorite")
        or item.get("IsFavorite")
        or item.get("UserData", {}).get("IsFavorite")
    )

    # 取消收藏 → 延迟删除
    if is_favorite is False or is_favorite == "false":
        log.info(f"[webhook] 取消收藏: {cd2_path}")
        schedule_delete(cd2_path, strm_path)
        return jsonify({"status": "ok", "message": f"将在 {DELETE_DELAY}s 后删除", "cd2_path": cd2_path}), 200

    # 收藏 → 取消待删除任务（如有），然后复制
    cancel_delete(cd2_path)
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
