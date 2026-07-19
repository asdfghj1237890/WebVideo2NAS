# Synology Deployment Commands Quick Reference

> Copy-paste ready command collection. All names below match
> [`docker-compose.synology.yml`](./docker-compose.synology.yml): services
> `db` / `redis` / `api` / `worker` / `worker2` / `worker3` / `db_cleanup`,
> containers `video_db` / `video_redis` / `video_api` / `video_worker_1..3` /
> `video_db_cleanup`, database `video_db` (user `postgres`), image
> `ghcr.io/asdfghj1237890/webvideo2nas`.

> **Prefer the DSM UI?** Container Manager can import a Project from a folder
> containing `docker-compose.yml`: rename `docker-compose.synology.yml` to
> `docker-compose.yml` under `/volume1/docker/video-downloader/docker`, then
> **Container Manager → Projects → Create** and point it at that folder. See the
> root [README](../../README.md) "Synology Container Manager" section for the
> full click-through.

> **Compose CLI:** these commands use the Docker Compose **v2** syntax
> (`docker compose`, space). If your DSM ships only the legacy v1 binary
> (`docker-compose`, hyphen), substitute it — the arguments are identical.

---

## 🚀 Initial Deployment (Complete Flow)

### Step 1: Create Directories
```bash
# Container-manager state (DB / Redis / logs) lives under /volume1/docker/...
sudo mkdir -p /volume1/docker/video-downloader/{db_data,redis_data,logs}
# Finished videos live on the media volume (matches the compose `volumes:` map)
sudo mkdir -p /volume1/video-downloader/downloads
# uid:gid 1026:100 = the `user:` the compose runs every service as
sudo chown -R 1026:100 /volume1/docker/video-downloader
sudo chown -R 1026:100 /volume1/video-downloader/downloads
```

### Step 2: Navigate to Working Directory
```bash
cd /volume1/docker/video-downloader/docker
```

### Step 3: Create Environment Variables File
```bash
# The two REQUIRED values are API_KEY and DB_PASSWORD. See .env.example for the
# full annotated list (worker tuning, per-host throttle, CORS, SSRF guard, ...).
cat > .env << 'EOF'
API_KEY=change-this-to-a-very-long-secure-key-minimum-32-chars
DB_PASSWORD=ChangeThisPassword123!
LOG_LEVEL=INFO
ALLOWED_ORIGINS=chrome-extension://*
RATE_LIMIT_PER_MINUTE=60
MAX_DOWNLOAD_WORKERS=20
MAX_RETRY_ATTEMPTS=3
FFMPEG_THREADS=2
# Pin a release instead of tracking :latest, e.g. IMAGE_TAG=3.1.10
#IMAGE_TAG=latest
EOF

chmod 600 .env
```

### Step 4: Pull and Start Services
```bash
docker compose -f docker-compose.synology.yml pull
docker compose -f docker-compose.synology.yml up -d
```

### Step 5: Verify
```bash
docker ps

# /api/health REQUIRES the API key (the in-container HEALTHCHECK sends it too).
# Host port is 52052 → container 8000.
curl -fsS -H "Authorization: Bearer $API_KEY" http://localhost:52052/api/health
# → {"status":"healthy"}
```

---

## 📋 Daily Management Commands

### Check Status
```bash
# All containers
docker ps

# Just this project's containers
docker ps | grep video_
```

### View Logs
```bash
# All services
docker compose -f docker-compose.synology.yml logs -f

# API only
docker logs -f video_api

# Workers (three by default)
docker logs -f video_worker_1
docker logs -f video_worker_2
docker logs -f video_worker_3

# Database
docker logs -f video_db

# Last 100 lines of a worker
docker logs --tail 100 video_worker_1
```

### Restart Services
```bash
# Restart all
docker compose -f docker-compose.synology.yml restart

# Restart API
docker compose -f docker-compose.synology.yml restart api

# Restart all workers
docker compose -f docker-compose.synology.yml restart worker worker2 worker3
```

### Stop/Start
```bash
# Stop all services
docker compose -f docker-compose.synology.yml stop

# Start all services
docker compose -f docker-compose.synology.yml start

# Remove containers (host-path data under /volume1 is preserved)
docker compose -f docker-compose.synology.yml down
```

---

## 🔄 Update and Maintenance

### Update Images
```bash
cd /volume1/docker/video-downloader/docker

# Pull the newest image from GHCR (no local build — it's a prebuilt image)
docker compose -f docker-compose.synology.yml pull

# Recreate containers on the new image
docker compose -f docker-compose.synology.yml up -d
```

### Clean Up Old Images
```bash
# Remove unused images
docker image prune -a

# Remove all unused resources
docker system prune -a
```

---

## 🗄️ Database Management

### Connect to Database
```bash
docker exec -it video_db psql -U postgres -d video_db
```

### Common SQL Queries
```sql
-- Recent jobs
SELECT id, title, status, progress, created_at FROM jobs ORDER BY created_at DESC LIMIT 10;

-- Jobs in progress
SELECT id, title, progress FROM jobs WHERE status = 'downloading';

-- Failed jobs (note: the column is error_message)
SELECT id, title, error_message FROM jobs WHERE status = 'failed';

-- Status breakdown
SELECT status, COUNT(*) FROM jobs GROUP BY status;

-- Manually prune old finished jobs (the db_cleanup service already does this)
DELETE FROM jobs WHERE created_at < NOW() - INTERVAL '30 days'
  AND status IN ('completed', 'failed', 'cancelled');

-- Exit
\q
```

### Backup / Restore Database
```bash
# Backup
docker exec video_db pg_dump -U postgres video_db > backup_$(date +%Y%m%d).sql

# Restore
cat backup_20260101.sql | docker exec -i video_db psql -U postgres -d video_db
```

---

## 🧪 Testing Commands

### API Testing
```bash
# Set variables (replace with your NAS LAN IP + real key)
export NAS_IP="192.168.1.100"
export API_KEY="your-api-key-here"

# Health check (requires the Authorization header)
curl -fsS -H "Authorization: Bearer $API_KEY" http://$NAS_IP:52052/api/health

# System status
curl http://$NAS_IP:52052/api/status \
  -H "Authorization: Bearer $API_KEY"

# Submit a NAS-direct test job (public HLS test stream)
curl -X POST http://$NAS_IP:52052/api/download \
  -H "Authorization: Bearer $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "url": "https://test-streams.mux.dev/x36xhzz/x36xhzz.m3u8",
    "title": "Test Video"
  }'

# List jobs
curl http://$NAS_IP:52052/api/jobs \
  -H "Authorization: Bearer $API_KEY"

# One job (replace JOB_ID)
curl http://$NAS_IP:52052/api/jobs/JOB_ID \
  -H "Authorization: Bearer $API_KEY"
```

### Redis Check
```bash
# Connect to Redis
docker exec -it video_redis redis-cli

# NAS-direct queue length
LLEN download_queue

# Browser-side finalize queue length
LLEN browser_finalize_queue

# All keys
KEYS *

# Exit
exit
```

---

## 🔍 Monitoring Commands

### System Resources
```bash
# Per-container resource usage
docker stats

# Disk space
df -h /volume1

# Downloads directory size
du -sh /volume1/video-downloader/downloads/*
```

### Container Health Status
```bash
# Only db / redis / api define a healthcheck. The worker containers disable the
# inherited API healthcheck (they don't bind a port), so their Health is empty.
docker inspect --format='{{.State.Health.Status}}' video_api
docker inspect --format='{{.State.Health.Status}}' video_db
docker inspect --format='{{.State.Health.Status}}' video_redis
```

---

## 🛠️ Troubleshooting

### Recreate a Specific Service
```bash
docker compose -f docker-compose.synology.yml up -d --force-recreate api
docker compose -f docker-compose.synology.yml up -d --force-recreate worker
```

### View Detailed Errors
```bash
docker compose -f docker-compose.synology.yml ps
docker compose -f docker-compose.synology.yml logs

# Shell into a container
docker exec -it video_api /bin/sh
docker exec -it video_worker_1 /bin/sh
```

### Reset Database
```bash
# ⚠️ Deletes all job data
docker compose -f docker-compose.synology.yml down
sudo rm -rf /volume1/docker/video-downloader/db_data/*
docker compose -f docker-compose.synology.yml up -d
```

### Reset Redis
```bash
docker exec -it video_redis redis-cli FLUSHALL
```

---

## 🔐 Security Checks

### Change API Key
```bash
# 1. Edit .env
vi /volume1/docker/video-downloader/docker/.env

# 2. Change API_KEY value (also update the Chrome extension settings to match)

# 3. Restart API
docker compose -f docker-compose.synology.yml restart api
```

### Change Database Password
```bash
# 1. Enter database
docker exec -it video_db psql -U postgres

# 2. Change password
ALTER USER postgres WITH PASSWORD 'new_password';
\q

# 3. Update .env (DB_PASSWORD) — the API/worker DATABASE_URL is derived from it
vi /volume1/docker/video-downloader/docker/.env

# 4. Restart all services
docker compose -f docker-compose.synology.yml restart
```

---

## 📊 Performance Tuning

### Increase Segment Concurrency
```bash
# Edit .env
vi /volume1/docker/video-downloader/docker/.env

# MAX_DOWNLOAD_WORKERS=20   # per-worker thread pool for HLS segment downloads
# (If a CDN throttles you, use HOST_CONCURRENCY_CAP / HOST_CONCURRENCY_OVERRIDES
#  instead — see .env.example. Lower this if the NAS CPU saturates.)

# Restart workers
docker compose -f docker-compose.synology.yml restart worker worker2 worker3
```

### Adjust FFmpeg Threads
```bash
# .env: FFMPEG_THREADS=4   # threads ffmpeg uses during merge
docker compose -f docker-compose.synology.yml restart worker worker2 worker3
```

---

## 🔄 Complete Redeployment

Start from scratch (⚠️ deletes all download records and staged bytes):

```bash
# 1. Stop and remove containers
cd /volume1/docker/video-downloader/docker
docker compose -f docker-compose.synology.yml down

# 2. Wipe persisted state
sudo rm -rf /volume1/docker/video-downloader/db_data/*
sudo rm -rf /volume1/docker/video-downloader/redis_data/*

# 3. Pull + start
docker compose -f docker-compose.synology.yml pull
docker compose -f docker-compose.synology.yml up -d

# 4. Verify
docker ps
curl -fsS -H "Authorization: Bearer $API_KEY" http://localhost:52052/api/health
```

---

## 📞 Getting Help

### Collect Debug Information
```bash
cat > debug_report.txt << EOF
=== System Info ===
$(uname -a)
$(docker --version)
$(docker compose version)

=== Container Status ===
$(docker ps -a | grep video_)

=== API Logs (last 50 lines) ===
$(docker logs --tail 50 video_api)

=== Worker Logs (last 50 lines) ===
$(docker logs --tail 50 video_worker_1)

=== Database Status ===
$(docker exec video_db pg_isready -U postgres)

=== Redis Status ===
$(docker exec video_redis redis-cli ping)
EOF

cat debug_report.txt
```

---

**💡 Tips:**
- All commands run in the Synology SSH environment.
- Replace `$NAS_IP` and `$API_KEY` with your real values.
- `/api/health` always needs `Authorization: Bearer $API_KEY` — a bare
  `curl http://.../api/health` returns **401**.
