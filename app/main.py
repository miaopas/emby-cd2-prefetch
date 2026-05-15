import os
import logging
import threading
import grpc
from flask import Flask, request, jsonify
from google.protobuf import empty_pb2
import clouddrive_pb2
import clouddrive_pb2_grpc

# ─── 配置 ────────────────────────────────────────────────────────────────────
CD2_GRPC_HOST     = os.getenv("CD2_GRPC_HOST", "192.168.1.234:19798")
CD2_API_TOKEN     = os.getenv("CD2_API_TOKEN", "YOUR_CD2_API_TOKEN")
EMBY_PATH_PREFIX  = os.getenv("EMBY_PATH_PREFIX", "/CloudNAS/CloudDrive")   # Emby 路径前缀
CD2_PATH_PREFIX   = os.getenv("CD2_PATH_PREFIX", "")                        # CD2 路径前缀（一般为空）
PREFETCH_PRIORITY = os.getenv("PREFETCH_PRIORITY", "HIGH")                  # LOW / NORMAL / HIGH
PREFETCH_TTL      = int(os.getenv("PREFETCH_TTL", "7200"))                  # 预取保留秒数
PORT              = int(os.getenv("PORT", "8094"))

# ─── 日志 ────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
log = logging.getLogger(__name__)

app = Flask(__name__)

# ─── gRPC 客户端 ─────────────────────────────────────────────────────────────
def get_stub():
    channel = grpc.insecure_channel(CD2_GRPC_HOST)
    return clouddrive_pb2_grpc.CloudDriveFileSrvStub(channel), channel

def auth_metadata():
    return [("authorization", f"Bearer {CD2_API_TOKEN}")]

# ─── 路径转换 ─────────────────────────────────────────────────────────────────
def emby_path_to_cd2(emby_path: str) -> str:
    """
    Emby: /CloudNAS/CloudDrive/115open/Symedia/...
    CD2:  /115open/Symedia/...
    """
    if emby_path.startswith(EMBY_PATH_PREFIX):
        cd2_path = emby_path[len(EMBY_PATH_PREFIX):]
        return CD2_PATH_PREFIX + cd2_path
    return emby_path

# ─── 预取逻辑 ─────────────────────────────────────────────────────────────────
PRIORITY_MAP = {
    "LOW":    clouddrive_pb2.HINT_PRIORITY_LOW,
    "NORMAL": clouddrive_pb2.HINT_PRIORITY_NORMAL,
    "HIGH":   clouddrive_pb2.HINT_PRIORITY_HIGH,
}

def prefetch_file(cd2_path: str):
    """在后台线程中执行预取"""
    try:
        stub, channel = get_stub()
        meta = auth_metadata()

        # 1. 获取文件大小
        log.info(f"[prefetch] 查询文件信息: {cd2_path}")
        try:
            file_info = stub.FindFileByPath(
                clouddrive_pb2.FindFileByPathRequest(
                    parentPath=os.path.dirname(cd2_path),
                    path=cd2_path
                ),
                metadata=meta
            )
            file_size = file_info.size
            log.info(f"[prefetch] 文件大小: {file_size / 1024 / 1024:.1f} MB")
        except grpc.RpcError as e:
            log.error(f"[prefetch] 获取文件信息失败: {e.details()}")
            channel.close()
            return

        if file_size <= 0:
            log.warning(f"[prefetch] 文件大小异常，跳过: {cd2_path}")
            channel.close()
            return

        # 2. 触发全文件预取
        priority = PRIORITY_MAP.get(PREFETCH_PRIORITY, clouddrive_pb2.HINT_PRIORITY_HIGH)
        reply = stub.PrefetchFileRanges(
            clouddrive_pb2.PrefetchFileRangesRequest(
                path=cd2_path,
                ranges=[clouddrive_pb2.ByteRange(start=0, length=file_size)],
                priority=priority,
                ttl_seconds=PREFETCH_TTL,
                replace_existing=True,
            ),
            metadata=meta
        )

        log.info(
            f"[prefetch] 已触发预取 ✓ hint_id={reply.hint_id} "
            f"accepted={reply.accepted_range_count} rejected={reply.rejected_range_count} "
            f"path={cd2_path}"
        )
        channel.close()

    except Exception as e:
        log.exception(f"[prefetch] 预取异常: {e}")

# ─── Emby Webhook 端点 ────────────────────────────────────────────────────────
@app.route("/webhook/emby", methods=["POST"])
def emby_webhook():
    """接收 Emby Webhook，收藏影片时触发 CD2 预取"""
    try:
        data = request.json or {}
    except Exception:
        data = {}

    event = data.get("Event", "")
    item  = data.get("Item", {})
    item_type = item.get("Type", "")
    path  = item.get("Path", "")

    log.info(f"[webhook] Event={event} Type={item_type} Path={path}")

    # 只处理收藏事件
    if event not in ("item.rate",):
        return jsonify({"status": "ignored", "reason": f"event={event}"}), 200

    # 只处理视频类型
    if item_type not in ("Movie", "Episode", "Video", ""):
        return jsonify({"status": "ignored", "reason": f"type={item_type}"}), 200

    # 判断是收藏还是取消收藏，只在收藏时触发预取
    is_favorite = (
        data.get("IsFavorite")
        or item.get("IsFavorite")
        or item.get("UserData", {}).get("IsFavorite")
    )
    if is_favorite is False or is_favorite == "false":
        log.info(f"[webhook] 取消收藏，跳过预取: {path}")
        return jsonify({"status": "ignored", "reason": "unfavorite"}), 200

    if not path:
        return jsonify({"status": "ignored", "reason": "no path"}), 200

    # 转换路径
    cd2_path = emby_path_to_cd2(path)
    log.info(f"[webhook] Emby路径: {path}")
    log.info(f"[webhook] CD2路径:  {cd2_path}")

    # 后台触发预取，不阻塞 webhook 响应
    t = threading.Thread(target=prefetch_file, args=(cd2_path,), daemon=True)
    t.start()

    return jsonify({
        "status": "ok",
        "message": f"预取已触发",
        "cd2_path": cd2_path
    }), 200


# ─── 手动触发端点 ─────────────────────────────────────────────────────────────
@app.route("/prefetch", methods=["POST"])
def manual_prefetch():
    """手动触发预取: POST /prefetch  {"path": "/115open/..."}"""
    data = request.json or {}
    path = data.get("path", "")
    if not path:
        return jsonify({"status": "error", "message": "path 不能为空"}), 400

    t = threading.Thread(target=prefetch_file, args=(path,), daemon=True)
    t.start()
    return jsonify({"status": "ok", "message": "预取已触发", "path": path}), 200


# ─── 缓存状态端点 ─────────────────────────────────────────────────────────────
@app.route("/cache/stats", methods=["GET"])
def cache_stats():
    """查看 CD2 磁盘缓存统计"""
    try:
        stub, channel = get_stub()
        stats = stub.GetFileBufferDiskCacheStats(
            empty_pb2.Empty(), metadata=auth_metadata()
        )
        channel.close()
        return jsonify({
            "enabled":        stats.enabled,
            "used_gb":        round(stats.totalBytes / 1024**3, 2),
            "max_gb":         round(stats.maxBytes / 1024**3, 2),
            "entry_count":    stats.entryCount,
            "segment_count":  stats.segmentCount,
            "root_dir":       stats.rootDir,
            "scan_completed": stats.scanCompleted,
        })
    except grpc.RpcError as e:
        return jsonify({"error": e.details()}), 500


@app.route("/cache/hints", methods=["GET"])
def cache_hints():
    """查看当前活跃的预取任务"""
    try:
        stub, channel = get_stub()
        reply = stub.GetActivePrefetchHints(
            empty_pb2.Empty(), metadata=auth_metadata()
        )
        channel.close()
        hints = [
            {
                "path":                h.path,
                "hint_id":             h.hint_id,
                "total_mb":            round(h.total_bytes / 1024**2, 1),
                "seconds_since_created": h.seconds_since_created,
                "remaining_ttl":       h.remaining_ttl_seconds,
            }
            for h in reply.hints
        ]
        return jsonify({
            "active_hints":   hints,
            "total_created":  reply.hints_created_total,
            "total_cancelled": reply.hints_cancelled_total,
            "total_expired":  reply.hints_expired_total,
        })
    except grpc.RpcError as e:
        return jsonify({"error": e.details()}), 500


@app.route("/cache/purge", methods=["POST"])
def cache_purge():
    """清空全部磁盘缓存"""
    try:
        stub, channel = get_stub()
        stub.PurgeFileBufferDiskCache(empty_pb2.Empty(), metadata=auth_metadata())
        channel.close()
        return jsonify({"status": "ok", "message": "缓存已清空"})
    except grpc.RpcError as e:
        return jsonify({"error": e.details()}), 500


# ─── 健康检查 ─────────────────────────────────────────────────────────────────
@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"})


if __name__ == "__main__":
    log.info(f"服务启动 port={PORT}")
    log.info(f"CD2 gRPC: {CD2_GRPC_HOST}")
    log.info(f"路径前缀转换: {EMBY_PATH_PREFIX} → {CD2_PATH_PREFIX or '/'}")
    app.run(host="0.0.0.0", port=PORT)
