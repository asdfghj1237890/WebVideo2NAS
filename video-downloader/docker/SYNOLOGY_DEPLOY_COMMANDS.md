# Synology Deployment Commands Quick Reference

> Copy-paste ready command collection

> Prefer DSM UI? Container Manager can import a Project from a folder containing `docker-compose.yml` (rename `docker-compose.synology.yml` to `docker-compose.yml`, then import `/volume1/docker/m3u8-downloader/docker` in **Container Manager → Projects**).

## 🚀 Initial Deployment (Complete Flow)

### Step 1: Create Directories
```bash
sudo mkdir -p /volume1/docker/m3u8-downloader/{db_data,redis_data,logs}
sudo mkdir -p /volume1/downloads/m3u8/{completed,processing,failed}
sudo chown -R 1026:100 /volume1/docker/m3u8-downloader
sudo chown -R 1026:100 /volume1/downloads/m3u8
```

### Step 2: Navigate to Working Directory
```bash
cd /volume1/docker/m3u8-downloader/docker
```

### Step 3: Create Environment Variables File
```bash
cat > .env << 'EOF'
DB_PASSWORD=ChangeThisPassword123!
API_KEY=change-this-to-a-very-long-secure-key-minimum-32-chars
MAX_DOWNLOAD_WORKERS=10
MAX_RETRY_ATTEMPTS=3
FFMPEG_THREADS=4
LOG_LEVEL=INFO
ALLOWED_ORIGINS=chrome-extension://*
RATE_LIMIT_PER_MINUTE=60
EOF

chmod 600 .env
```

### Step 4: Start Services
```bash
docker-compose -f docker-compose.synology.yml up -d
```

### Step 5: Verify
```bash
docker ps
curl http://localhost:52052/api/health
```

---

## 📋 Daily Management Commands

### Check Status
```bash
# View all containers
docker ps

# View specific container
docker ps | grep m3u8
```

### View Logs
```bash
# All services
docker-compose -f docker-compose.synology.yml logs -f

# API only
docker logs -f m3u8_api

# Worker only
docker logs -f m3u8_worker

# Database
docker logs -f m3u8_db

# Last 100 lines
docker logs --tail 100 m3u8_worker
```

### Restart Services
```bash
# Restart all
docker-compose -f docker-compose.synology.yml restart

# Restart API
docker-compose -f docker-compose.synology.yml restart api

# Restart Worker
docker-compose -f docker-compose.synology.yml restart worker
```

### Stop/Start
```bash
# Stop all services
docker-compose -f docker-compose.synology.yml stop

# Start all services
docker-compose -f docker-compose.synology.yml start

# Completely remove containers (data preserved)
docker-compose -f docker-compose.synology.yml down
```

---

## 🔄 Update and Maintenance

### Update Images
```bash
cd /volume1/docker/m3u8-downloader/docker

# Pull latest images
docker-compose -f docker-compose.synology.yml pull

# Rebuild
docker-compose -f docker-compose.synology.yml build

# Restart
docker-compose -f docker-compose.synology.yml up -d
```

### Clean Up Old Images
```bash
# Clean unused images
docker image prune -a

# Clean all unused resources
docker system prune -a
```

---

## 🗄️ Database Management

### Connect to Database
```bash
docker exec -it m3u8_db psql -U postgres -d m3u8_db
```

### Common SQL Queries
```sql
-- View all jobs
SELECT id, title, status, progress, created_at FROM jobs ORDER BY created_at DESC LIMIT 10;

-- View jobs in progress
SELECT id, title, progress FROM jobs WHERE status = 'downloading';

-- View failed jobs
SELECT id, title, error_message FROM jobs WHERE status = 'failed';

-- Statistics
SELECT status, COUNT(*) FROM jobs GROUP BY status;

-- Clean up old jobs (older than 30 days)
DELETE FROM jobs WHERE created_at < NOW() - INTERVAL '30 days' AND status IN ('completed', 'failed');

-- Exit
\q
```

### Backup Database
```bash
# Create backup
docker exec m3u8_db pg_dump -U postgres m3u8_db > backup_$(date +%Y%m%d).sql

# Restore backup
cat backup_20231012.sql | docker exec -i m3u8_db psql -U postgres -d m3u8_db
```

---

## 🧪 Testing Commands

### API Testing
```bash
# Set variables
export NAS_IP="192.168.1.100"
export API_KEY="your-api-key-here"

# Health check
curl http://$NAS_IP:52052/api/health

# System status
curl http://$NAS_IP:52052/api/status \
  -H "Authorization: Bearer $API_KEY"

# Submit test job
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

# View specific job (replace JOB_ID)
curl http://$NAS_IP:52052/api/jobs/JOB_ID \
  -H "Authorization: Bearer $API_KEY"
```

### Redis Check
```bash
# Connect to Redis
docker exec -it m3u8_redis redis-cli

# Check queue length
LLEN download_queue

# View all keys
KEYS *

# Exit
exit
```

---

## 🔍 Monitoring Commands

### System Resources
```bash
# Container resource usage
docker stats

# Disk space
df -h /volume1

# Download directory size
du -sh /volume1/downloads/m3u8/*
```

### Container Health Status
```bash
# Check health status
docker inspect --format='{{.State.Health.Status}}' m3u8_api
docker inspect --format='{{.State.Health.Status}}' m3u8_db
```

---

## 🛠️ Troubleshooting

### Reset Specific Service
```bash
# Rebuild API
docker-compose -f docker-compose.synology.yml up -d --force-recreate api

# Rebuild Worker
docker-compose -f docker-compose.synology.yml up -d --force-recreate worker
```

### View Detailed Errors
```bash
# Check container startup failure reasons
docker-compose -f docker-compose.synology.yml ps
docker-compose -f docker-compose.synology.yml logs

# Inspect inside container
docker exec -it m3u8_api /bin/sh
docker exec -it m3u8_worker /bin/sh
```

### Reset Database
```bash
# ⚠️ Warning: This will delete all data!
docker-compose -f docker-compose.synology.yml down -v
rm -rf /volume1/docker/m3u8-downloader/db_data/*
docker-compose -f docker-compose.synology.yml up -d
```

### Reset Redis
```bash
docker exec -it m3u8_redis redis-cli FLUSHALL
```

---

## 🔐 Security Checks

### Change API Key
```bash
# 1. Edit .env
vi /volume1/docker/m3u8-downloader/docker/.env

# 2. Change API_KEY value

# 3. Restart API
docker-compose -f docker-compose.synology.yml restart api
```

### Change Database Password
```bash
# 1. Enter database
docker exec -it m3u8_db psql -U postgres

# 2. Change password
ALTER USER postgres WITH PASSWORD 'new_password';
\q

# 3. Update .env
vi /volume1/docker/m3u8-downloader/docker/.env
# Change DB_PASSWORD

# 4. Restart all services
docker-compose -f docker-compose.synology.yml restart
```

---

## 📊 Performance Tuning

### Increase Concurrent Downloads
```bash
# Edit .env
vi /volume1/docker/m3u8-downloader/docker/.env

# Change the following values:
# MAX_DOWNLOAD_WORKERS=15      # Number of threads per video

# Restart Worker
docker-compose -f docker-compose.synology.yml restart worker
```

### Adjust FFmpeg Performance
```bash
# Edit .env
# FFMPEG_THREADS=8  # Increase FFmpeg thread count

# Restart Worker
docker-compose -f docker-compose.synology.yml restart worker
```

---

## 🔄 Complete Redeployment

If you need to start from scratch:

```bash
# 1. Stop and remove all containers
cd /volume1/docker/m3u8-downloader/docker
docker-compose -f docker-compose.synology.yml down -v

# 2. Clean all data (⚠️ Will delete all download records)
sudo rm -rf /volume1/docker/m3u8-downloader/db_data/*
sudo rm -rf /volume1/docker/m3u8-downloader/redis_data/*

# 3. Restart
docker-compose -f docker-compose.synology.yml up -d

# 4. Verify
docker ps
curl http://localhost:52052/api/health
```

---

## 📞 Getting Help

### Collect Debug Information
```bash
# Generate debug report
cat > debug_report.txt << EOF
=== System Info ===
$(uname -a)
$(docker --version)
$(docker-compose --version)

=== Container Status ===
$(docker ps -a)

=== API Logs (last 50 lines) ===
$(docker logs --tail 50 m3u8_api)

=== Worker Logs (last 50 lines) ===
$(docker logs --tail 50 m3u8_worker)

=== Database Status ===
$(docker exec m3u8_db pg_isready -U postgres)

=== Redis Status ===
$(docker exec m3u8_redis redis-cli ping)
EOF

cat debug_report.txt
```

---

**💡 Tips:** 
- Bookmark this file for quick reference
- All commands are executed in Synology SSH environment
- Remember to replace `$NAS_IP` and `$API_KEY` with actual values

