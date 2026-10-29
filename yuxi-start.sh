#!/bin/bash
# Yuxi 启动（Git Bash 中运行: bash yuxi-start.sh）
# 容器跑在 WSL Ubuntu 的 docker 里；代码在本目录改，api/worker/web 均热重载
cd "$(dirname "$0")" || exit 1

# WSL 保活：WSL 闲置约 1 分钟自动关机会连带停掉 docker 和全部容器
if ! pgrep -f "wsl.exe.*sleep infinity" > /dev/null 2>&1; then
  nohup wsl.exe -d Ubuntu -u root -e sleep infinity > /dev/null 2>&1 &
  echo "WSL keepalive started"
fi

wsl.exe -d Ubuntu -u root -e bash -lc 'systemctl is-active --quiet docker || systemctl start docker; cd /mnt/e/code/Yuxi-main && docker compose up -d' || exit 1

echo "waiting for api ready ..."
for i in $(seq 1 60); do
  curl -sf http://localhost:5050/api/system/ready >/dev/null 2>&1 && break
  sleep 5
done
curl -s http://localhost:5050/api/system/ready; echo
echo "web      -> http://localhost:15173"
echo "api doc  -> http://localhost:5050/docs"
