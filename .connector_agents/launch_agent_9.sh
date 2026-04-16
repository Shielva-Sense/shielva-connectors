#!/usr/bin/env bash
cd "/Users/vivekvarshavaishvik/Documents/Shielva Automation"
echo "╔══════════════════════════════════════════════╗"
echo "║  Shielva Connector Agent — Terminal 9/10  ║"
echo "╚══════════════════════════════════════════════╝"
echo ""
echo "Starting Claude agent (terminal 9)... output will stream live."
echo ""
# Pipe prompt via stdin — streams output in real time (no buffering)
"/Users/vivekvarshavaishvik/Library/Application Support/Claude/claude-code/2.1.87/claude.app/Contents/MacOS/claude" --dangerously-skip-permissions < "/Users/vivekvarshavaishvik/Documents/Shielva Automation/.connector_agents/agent_9.md"
