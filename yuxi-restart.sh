#!/bin/bash
# 手动重启 api/worker：Windows bind mount 上 inotify 不一定穿透，改了 Python 代码没自动重载时用
wsl.exe -d Ubuntu -u root -e bash -lc 'cd /mnt/e/code/Yuxi-main && docker compose restart api worker'
