#!/usr/bin/env bash
cd "/Users/vivekvarshavaishvik/Documents/Shielva Automation"
echo "╔══════════════════════════════════════════════╗"
echo "║  Shielva Connector Agent — Terminal 1/4  ║"
echo "╚══════════════════════════════════════════════╝"
echo ""
echo "Streaming Claude agent output live (terminal 1)..."
echo ""
"/opt/homebrew/bin/claude" --dangerously-skip-permissions < "/Users/vivekvarshavaishvik/Documents/Shielva Automation/.connector_agents/agent_1.md"
