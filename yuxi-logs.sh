#!/bin/bash
# 跟踪日志: bash yuxi-logs.sh [api|worker|web|milvus|graph|postgres|redis|minio|sandbox-provisioner]
cd "$(dirname "$0")" || exit 1
svc="${1:-api}"
wsl.exe -d Ubuntu -u root -e bash -lc "cd /mnt/e/code/Yuxi-main && docker compose logs -f --tail=100 $svc"
