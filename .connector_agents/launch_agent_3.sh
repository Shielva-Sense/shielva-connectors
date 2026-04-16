#!/usr/bin/env bash
cd "/Users/vivekvarshavaishvik/Documents/Shielva Automation"
echo "╔══════════════════════════════════════════════╗"
echo "║  Shielva Connector Agent — Terminal 3/4  ║"
echo "╚══════════════════════════════════════════════╝"
echo ""
echo "Streaming Claude agent output live (terminal 3)..."
echo ""
"/opt/homebrew/bin/claude" --dangerously-skip-permissions < "/Users/vivekvarshavaishvik/Documents/Shielva Automation/.connector_agents/agent_3.md"
