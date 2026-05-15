# Emby → CD2 预取服务

播放 Emby 影片时自动触发 CloudDrive2 缓存，实现秒开。

## 部署步骤

### 1. 修改配置
编辑 `docker-compose.yml`，填入：
- `CD2_API_TOKEN` — CD2 的 API Token（CD2 Web界面 → 设置 → API Token）
- `EMBY_PATH_PREFIX` — Emby 路径中需要去掉的前缀（默认 `/CloudNAS/CloudDrive`）

### 2. 构建并启动
```bash
docker compose up -d --build
```

### 3. 配置 Emby Webhook
Emby 控制台 → 高级 → Webhooks → 添加 Webhook：
- URL: `http://192.168.1.234:8094/webhook/emby`
- 事件: 勾选 **播放** (Playback Start / media.play)
- 通知类型: JSON

### 4. 验证
```bash
# 健康检查
curl http://192.168.1.234:8094/health

# 查看缓存状态
curl http://192.168.1.234:8094/cache/stats

# 查看当前预取任务
curl http://192.168.1.234:8094/cache/hints

# 手动触发预取（测试用）
curl -X POST http://192.168.1.234:8094/prefetch \
  -H "Content-Type: application/json" \
  -d '{"path": "/115open/Symedia/已归档/华语电影/寒战 (2012) {tmdb-137409}/寒战 (2012).iso"}'

# 清空缓存
curl -X POST http://192.168.1.234:8094/cache/purge
```

## 路径映射说明

| Emby 路径 | CD2 路径 |
|-----------|----------|
| `/CloudNAS/CloudDrive/115open/Symedia/...` | `/115open/Symedia/...` |

通过 `EMBY_PATH_PREFIX` 环境变量控制，去掉前缀即为 CD2 路径。

## API 端点

| 端点 | 方法 | 说明 |
|------|------|------|
| `/webhook/emby` | POST | 接收 Emby Webhook |
| `/prefetch` | POST | 手动触发预取 |
| `/cache/stats` | GET | 缓存磁盘统计 |
| `/cache/hints` | GET | 当前预取任务 |
| `/cache/purge` | POST | 清空全部缓存 |
| `/health` | GET | 健康检查 |

## 查看日志
```bash
docker logs emby-cd2-prefetch -f
```
