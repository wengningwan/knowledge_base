#!/bin/bash
# 停止 Yuxi（默认只停容器保留数据；连容器一起删用: bash yuxi-stop.sh down）
# 数据都在 WSL ext4 的 /home/asus/yuxi-state，down 也不会丢
cd "$(dirname "$0")" || exit 1
if [ "$1" = "down" ]; then
  wsl.exe -d Ubuntu -u root -e bash -lc 'cd /mnt/e/code/Yuxi-main && docker compose down'
else
  wsl.exe -d Ubuntu -u root -e bash -lc 'cd /mnt/e/code/Yuxi-main && docker compose stop'
fi
