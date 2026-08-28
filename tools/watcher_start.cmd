@echo off
rem LLM Wiki 触发文件 Watcher 启动器（双击运行 / 放入 shell:startup 开机自启）
rem 功能：常驻轮询 vault/_triggers/，发现纸条自动唤起 Claude Code headless 编译
cd /d "%~dp0.."
title LLM Wiki Trigger Watcher
echo [LLM Wiki Watcher] 启动中... 日志: tools\watcher.log
python tools\trigger_watcher.py
pause