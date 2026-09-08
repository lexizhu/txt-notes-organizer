#!/bin/zsh

SCRIPT_DIR="${0:A:h}"

if ! /usr/bin/env python3 "$SCRIPT_DIR/launch_tool.py"; then
	printf '\nThe app could not start. Keep this window open and review data/local-server.log.\n'
	printf 'Press Enter to close this window.'
	read -r
	exit 1
fi
