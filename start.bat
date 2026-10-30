@echo off
cd /d %~dp0
echo 启动 MediaHub，请先确认已安装依赖: pip install -r server\requirements.txt
python server\main.py
