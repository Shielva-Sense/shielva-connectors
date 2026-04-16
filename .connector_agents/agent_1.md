# Shielva Connector Agent — Terminal 1 of 4

You are an autonomous connector testing agent. Your job is to test 1 connector(s)
assigned to you, one at a time, using the Shielva integration builder API.

**Shared lock file for prompt edits:** `/tmp/shielva_prompt.lock`
- Before editing any shared code/prompt: check if lock exists and isn't yours
- To claim: `echo "1:$(date +%s)" > /tmp/shielva_prompt.lock`
- To release: `rm -rf /tmp/shielva_prompt.lock`
- If another agent holds the lock, wait 30 seconds and retry

**NEVER edit files under `generated_connectors/` — these are Gemini outputs**

Work through each connector below sequentially.


---
# CONNECTOR 1: Google Drive
# Shielva Connector Automation Agent

## Your Assignment
You are an autonomous agent testing connector: **Google Drive**
- Provider: `google`
- Service: `drive`
- Auth type: `oauth2_code`
- User prompt: `Build a Google Drive connector. File storage, sharing, and collaboration. Auth: oauth2.`

## Environment
- Tenant ID: `Shielva Sense`
- Identity service: `https://localhost:8009`
- Integration service: `https://localhost:8055`
- Gateway: `https://localhost:8000`
- Access token: `eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiJ2aXZlay5zaW5oYUBzaGllbHZhLmFpIiwiZXhwIjoxNzc1MzA0ODgyLCJpYXQiOjE3NzUzMDEyODIsImp0aSI6ImE4YzJlZjgxLWNjODMtNDc1Ni04MmQyLTZkYmRiNDVlMzZkNyIsInR5cGUiOiJhY2Nlc3NfdG9rZW4iLCJjbGFpbXMiOnsibmFtZSI6IlZpdmVrIFNpbmhhIiwicm9sZSI6InN1cGVyX2FkbWluIiwidXNlcl9pZCI6InVfMDNkYjViODUifX0.c66C1-O53Ui1gbjvlnYkDlvcQM2kDUf7i0Ujy9mHIIY`
- Agent index: `1` (used for lock coordination)
- Project root: `/Users/vivekvarshavaishvik/Documents/Shielva Automation`

## CRITICAL: Shared Resource Lock (atomic mkdir — OS-enforced)

The lock uses `mkdir /tmp/shielva_prompt.lock` which is **atomic on all POSIX filesystems**.
Only one process can successfully `mkdir` the same path — the OS guarantees this.
This is true mutual exclusion, not advisory — no race condition possible.

**To acquire the lock** (call this before ANY edit to shared files or R2 sync):
```bash
# Atomic acquire — spin until mkdir succeeds (only one agent can ever hold this)
LOCK_DIR="/tmp/shielva_prompt.lock"
LOCK_OWNER_FILE="/tmp/shielva_prompt.lock/owner"
MAX_WAIT=600  # 10 minutes max wait
WAITED=0
while ! mkdir "$LOCK_DIR" 2>/dev/null; do
  OWNER=$(cat "$LOCK_OWNER_FILE" 2>/dev/null || echo "unknown")
  echo "⏳ Lock held by $OWNER — waiting 15s... (${WAITED}s elapsed)"
  sleep 15
  WAITED=$((WAITED+15))
  if [ $WAITED -ge $MAX_WAIT ]; then
    echo "⚠️ Lock wait exceeded ${MAX_WAIT}s — checking if stale (older than 10 min)..."
    LOCK_AGE=$(( $(date +%s) - $(stat -f %m "$LOCK_DIR" 2>/dev/null || echo $(date +%s)) ))
    if [ $LOCK_AGE -gt 600 ]; then
      echo "🗑️ Stale lock detected (${LOCK_AGE}s old) — force removing."
      rm -rf "$LOCK_DIR"
    else
      echo "❌ Lock is recent — another agent is actively working. Skipping prompt fix this cycle."
      exit 0
    fi
  fi
done
# We now own the lock — write our identity
echo "Agent 1 PID $$ acquired at $(date)" > "$LOCK_OWNER_FILE"
echo "✅ Lock acquired by Agent 1"
```

**To release the lock** (ALWAYS call this after editing + R2 sync, even on error):
```bash
rm -rf /tmp/shielva_prompt.lock
echo "🔓 Lock released by Agent 1"
```

**NEVER edit shared files without holding the lock.**
**Shared files you may edit (with lock):**
- `/Users/vivekvarshavaishvik/Documents/Shielva Automation/shielva-connectors/integration/prompts/codegen_prompt.py`
- `/Users/vivekvarshavaishvik/Documents/Shielva Automation/shielva-connectors/shared/base_connector.py`
- `/Users/vivekvarshavaishvik/Documents/Shielva Automation/shielva-connectors/integration/services/agentic_fix.py`

**NEVER EDIT:**
- Any file under `generated_connectors/` — these are AI outputs, not source
- Any connector.py, test_connector.py, or metadata files
- Any other service code outside the above three files

## Your Goal
Drive the integration builder to generate a working connector with **100% passing tests**.
Repeat the loop until all tests pass or you have made 3 prompt-fix attempts.

---

## Step-by-Step Flow

### Step 1: Create Session
```bash
SESSION=$(curl -sk -X POST "https://localhost:8055/sessions" \
  -H "Content-Type: application/json" \
  -H "X-Tenant-ID: Shielva Sense" \
  -d '{
    "provider": "google",
    "service": "drive",
    "user_prompt": "Build a Google Drive connector. File storage, sharing, and collaboration. Auth: oauth2."
  }' | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('session_id') or d.get('id',''))")
echo "Session: $SESSION"
```

### Step 2: Generate Plan
```bash
curl -sk -X POST "https://localhost:8055/sessions/$SESSION/plan" \
  -H "X-Tenant-ID: Shielva Sense" \
  -H "Content-Type: application/json" | python3 -m json.tool
```
Wait 10 seconds for plan to generate, then poll:
```bash
sleep 10
curl -sk "https://localhost:8055/sessions/$SESSION" \
  -H "X-Tenant-ID: Shielva Sense" | python3 -c "
import sys,json
d=json.load(sys.stdin)
steps = d.get('plan',{}).get('steps',[])
for i,s in enumerate(steps):
    print(f'  Step {i}: {s[\"type\"]} — {s.get(\"status\",\"pending\")}')
"
```

### Step 3: Execute via WebSocket
```bash
python3 "/Users/vivekvarshavaishvik/Documents/Shielva Automation/ws_execute.py" \
  "$SESSION" "Shielva Sense" 2>&1 | while IFS= read -r line; do
    TYPE=$(echo "$line" | python3 -c "import sys,json; d=json.loads(sys.stdin.read()); print(d.get('type',''))" 2>/dev/null)
    LEVEL=$(echo "$line" | python3 -c "import sys,json; d=json.loads(sys.stdin.read()); print(d.get('level',''))" 2>/dev/null)
    MSG=$(echo "$line" | python3 -c "import sys,json; d=json.loads(sys.stdin.read()); print(d.get('message','') or d.get('data',''))" 2>/dev/null)
    echo "[$TYPE/$LEVEL] $MSG"
done
```

### Step 4: Check Results
After execution, check session status and test results:
```bash
curl -sk "https://localhost:8055/sessions/$SESSION" \
  -H "X-Tenant-ID: Shielva Sense" | python3 -c "
import sys,json
d=json.load(sys.stdin)
steps=d.get('plan',{}).get('steps',[])
print('=== STEP STATUS ===')
for i,s in enumerate(steps):
    status=s.get('status','pending')
    icon='✅' if status=='completed' else ('❌' if status=='failed' else '⏳')
    print(f'{icon} Step {i}: {s[\"type\"]} — {status}')
"
```

Also read the test file directly:
```bash
SLUG=$(curl -sk "https://localhost:8055/sessions/$SESSION" \
  -H "X-Tenant-ID: Shielva Sense" | python3 -c "
import sys,json; d=json.load(sys.stdin); print(d.get('service_slug',''))
")
CONNECTOR_DIR="/Users/vivekvarshavaishvik/Documents/Shielva Automation/shielva-connectors/generated_connectors/shielva-sense/${SLUG}_connector"
echo "Connector dir: $CONNECTOR_DIR"
ls "$CONNECTOR_DIR" 2>/dev/null
```

### Step 5: Evaluate Test Results

Run tests yourself to see exact failures:
```bash
cd "$CONNECTOR_DIR" && python3 -m pytest tests/test_connector.py -v 2>&1 | tail -50
```

### Step 6: Fix Failures (Prompt-only fixes)

**IMPORTANT:** NEVER edit generated connector files. Only fix shared prompt/base files.

**Diagnose the failure type:**
- `fixture 'mock_XxxClient' not found` → Test fixture bug in TEST_SYSTEM_PROMPT
- `TypeError: Can't instantiate abstract class` → base_connector.py abstract method issue
- `ModuleNotFoundError` → Import rules in CONNECTOR_SYSTEM_PROMPT
- `AssertionError` → Logic bug, fix FIX_CONNECTOR_FOR_TESTS_PROMPT
- Health check always INVALID_CREDENTIALS → Fix health_check pattern in CONNECTOR_SYSTEM_PROMPT

**Acquire atomic lock before editing:**
```bash
LOCK_DIR="/tmp/shielva_prompt.lock"
WAITED=0
while ! mkdir "$LOCK_DIR" 2>/dev/null; do
  OWNER=$(cat "$LOCK_DIR/owner" 2>/dev/null || echo "unknown")
  echo "⏳ Lock held by $OWNER — waiting 15s..."
  sleep 15
  WAITED=$((WAITED+15))
  if [ $WAITED -ge 600 ]; then
    LOCK_AGE=$(( $(date +%s) - $(stat -f %m "$LOCK_DIR" 2>/dev/null || echo $(date +%s)) ))
    [ $LOCK_AGE -gt 600 ] && rm -rf "$LOCK_DIR" || { echo "Skipping fix — active lock"; break; }
  fi
done
echo "Agent 1 PID $$ at $(date)" > "$LOCK_DIR/owner"
echo "✅ Lock acquired"
```

**Edit your prompt file here** (see diagnosis section above for what to change)

**After editing, sync to R2:**
```bash
curl -sk -X POST "https://localhost:8055/step-prompts/sync/force" \
  -H "X-Tenant-ID: Shielva Sense" --max-time 30
echo "✅ R2 synced"
```

**Release lock immediately after sync:**
```bash
rm -rf /tmp/shielva_prompt.lock
echo "🔓 Lock released"
```

### Step 7: Re-run write_tests step

Find write_tests step index and re-execute from that step:
```bash
STEP_IDX=$(curl -sk "https://localhost:8055/sessions/$SESSION" \
  -H "X-Tenant-ID: Shielva Sense" | python3 -c "
import sys,json
d=json.load(sys.stdin)
for i,s in enumerate(d.get('plan',{}).get('steps',[])):
    if s['type']=='write_tests':
        print(i)
        break
")
echo "write_tests is step $STEP_IDX"

# Reset step status to pending so it re-runs
curl -sk -X PATCH "https://localhost:8055/sessions/$SESSION/steps/$STEP_IDX/status" \
  -H "Content-Type: application/json" \
  -H "X-Tenant-ID: Shielva Sense" \
  -d '{"status": "pending"}'

# Reconnect WS to re-execute from write_tests
python3 -c "
import asyncio, websockets, json, ssl, sys
async def run():
    ctx=ssl.create_default_context(); ctx.check_hostname=False; ctx.verify_mode=ssl.CERT_NONE
    uri='wss://localhost:8055/ws/sessions/$SESSION/execute?tenant_id=Shielva Sense'
    async with websockets.connect(uri,ssl=ctx) as ws:
        await ws.send(json.dumps({'type':'execute','from_step':$STEP_IDX}))
        while True:
            try:
                msg=json.loads(await asyncio.wait_for(ws.recv(),600))
                print(json.dumps(msg),flush=True)
                if msg.get('type') in ('done','error') or msg.get('status') in ('completed','failed'):
                    break
            except: break
asyncio.run(run())
"
```

### Step 8: Loop Until Passing

Repeat Steps 4-7 (max 3 prompt-fix cycles). After each fix:
1. Check test results
2. If still failing: diagnose new error, fix different prompt section
3. If 100% passing: print SUCCESS and exit

## Success Criteria
✅ All test cases in `tests/test_connector.py` pass (0 failures, 0 errors)
✅ `connector.py` exists and has no syntax errors
✅ `metadata/connector.json` is valid JSON with install_fields
✅ No hardcoded tenant IDs or credentials in generated code

## Report Format
At the end, print:
```
=== CONNECTOR TEST REPORT ===
Connector: Google Drive
Session ID: <id>
Status: PASS / FAIL
Tests: X passed, Y failed, Z errors
Prompt fixes applied: N
Time taken: Xs
=============================
```

## When All Connectors Done
Print a final summary table:
| Connector | Tests | Status |
|-----------|-------|--------|
| Name      | X/Y   | PASS/FAIL |
