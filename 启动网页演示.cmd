@echo off
chcp 65001 >nul
title 星河银行 - 网页交互演示
call "%~dp0启动演示.cmd" --web %*
