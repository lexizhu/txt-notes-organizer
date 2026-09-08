#!/bin/zsh

SCRIPT_DIR="${0:A:h}"

if ! /usr/bin/env python3 "$SCRIPT_DIR/launch_tool.py"; then
	printf '\n启动失败。请保留这个窗口，并联系工具维护者。\n'
	printf '按回车键关闭窗口。'
	read -r
	exit 1
fi