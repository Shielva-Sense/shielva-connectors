#!/usr/bin/env bash
cd "/Users/vivekvarshavaishvik/Documents/Shielva Automation"
echo "╔══════════════════════════════════════════════╗"
echo "║  Shielva Connector Agent — Terminal 4/4  ║"
echo "╚══════════════════════════════════════════════╝"
echo ""
echo "Streaming Claude agent output live (terminal 4)..."
echo ""
"/opt/homebrew/bin/claude" --dangerously-skip-permissions < "/Users/vivekvarshavaishvik/Documents/Shielva Automation/.connector_agents/agent_4.md"
