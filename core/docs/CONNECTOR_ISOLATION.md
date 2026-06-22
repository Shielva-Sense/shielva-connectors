# Advanced-connector isolation & HA reload

Advanced connectors are **AI-generated Python** authored in shielva-agentic-developer,
deployed into `generated_connectors/{tenant}/{pkg}_connector/`, and executed
**in-process** by this gateway (`gateway.py`) via `importlib.exec_module`
(`_load_generated_connectors`). All tenants share one process. Two structural
risks follow from that:

1. **Blast radius** — a buggy/hostile connector can hang the event loop, leak
   another tenant's in-memory secrets, or shell out, taking down *every* tenant's
   advanced connectors.
2. **HA divergence** — `/internal/reload-connectors` only refreshes the pod that
   received the call. In a multi-pod deployment other pods 404 a new connector or
   serve stale code, violating the project rule that shared mutable state
   reconciles cluster-wide.

This document describes the mitigations now in place and the configuration knobs.

---

## 1. Execution hardening (isolation)

Three pragmatic, in-process defense-in-depth layers — **not** a true OS sandbox
(see *Limitations*). They eliminate the obvious classes of failure without a
subprocess rewrite.

### Layer 1 — static AST scan at load (`_scan_connector_package`)
Before **any** of a package's code is executed, every `*.py` in the package
(`connector.py` + `client/` + `helpers/` + `repository/`) is parsed and scanned
for patterns an HTTP/API connector never legitimately uses:

| Category | Blocked |
|---|---|
| Module imports | `subprocess`, `ctypes`, `multiprocessing`, `pty`, `fcntl`, `resource`, `signal`, `mmap`, `_thread` |
| Namespace-escape attrs | `__subclasses__`, `__globals__`, `__bases__`, `__mro__`, `__builtins__`, `__code__`, `__closure__` |
| Arbitrary-code calls | `eval()`, `exec()`, `compile()`, `__import__()` |
| `os` shell-out / proc control | `os.system`, `os.popen`, `os.fork`, `os.exec*`, `os.spawn*`, `os.kill`, `os.setuid`, `os.setgid` |

`os` itself stays importable (connectors legitimately read `os.environ`); only the
process-control attributes are blocked. A flagged connector is **skipped** (not
registered) and the reason is logged with the offending file + pattern.

Controlled by **`CONNECTOR_AST_SCAN`**:
- `enforce` *(default)* — block flagged connectors.
- `warn` — load anyway, log the findings (use only to triage a false positive).
- `off` — disable the scan.

### Layer 2 — import wall-clock (`_exec_module_with_timeout`)
`exec_module` is synchronous; a connector that hangs at module scope (infinite
loop, blocking network call) would stall startup or a hot-reload for everyone. The
exec runs in a **daemon thread**; after the timeout we stop *waiting* and skip the
connector. The runaway thread is a daemon, so it can never block the gateway and
dies with the process. Applies to the connector module and its sub-packages.

Controlled by **`CONNECTOR_IMPORT_TIMEOUT_S`** (default `10`).

### Layer 3 — invocation wall-clock + sync offload
At `POST /connectors/{id}/test/{method}`:
- **async** methods run under `asyncio.wait_for(timeout)`.
- **sync** methods are offloaded to a worker thread (`run_in_executor`) under the
  same timeout — so a blocking/runaway sync method can't freeze the event loop and
  starve every other tenant's requests.

A timeout returns a clean `{"status":"error", ...}` (HTTP 200) rather than hanging
the caller. Controlled by **`CONNECTOR_INVOKE_TIMEOUT_S`** (default `30`).

### Limitations / roadmap
The AST scan is best-effort: it cannot stop every conceivable native escape, and
the import/invoke timeouts cannot forcibly *kill* CPU-bound native code mid-call
(Python threads can't be pre-empted). For **fully untrusted** multi-tenant code
the next step is out-of-process isolation: run each tenant's connector pool in a
**subprocess/container worker** with cgroup CPU/memory limits, seccomp, and a
read-only FS, invoked over a local socket. The layers above are the pragmatic
in-process mitigation until that lands.

---

## 2. Cluster-wide reload fan-out (HA)

The in-process `CONNECTOR_CLASSES` registry is shared mutable state and must
reconcile across every replica. A Redis pub/sub channel fans reloads out:

- **Publish** — after a successful local reload, `/internal/reload-connectors` and
  `/internal/pull-and-reload` publish `{action, origin: <pod-id>, connector_type}`
  to `CONNECTOR_RELOAD_CHANNEL` (default `shielva:connectors:reload`).
- **Subscribe** — every pod runs `_connector_reload_subscriber()` (started in the
  app lifespan). On an event whose `origin` is **not** this pod, it reloads
  `CONNECTOR_CLASSES` from disk off the event loop (`run_in_executor`). The pod
  that originated the event skips its own echo (it already reloaded locally).

```
   deploy / pull-and-reload on pod A
        │  _reload_generated_connectors()  (local)
        │  PUBLISH shielva:connectors:reload {origin: A}
        ▼
   ┌────────────── Redis pub/sub ──────────────┐
   ▼                    ▼                       ▼
 pod A (skip self)   pod B reload          pod C reload
```

If Redis is unavailable the subscriber logs `no_redis` and the gateway degrades to
single-pod reload (publish is best-effort and never fails the local reload).

**Deployment requirement:** the fan-out reloads each pod **from its own disk**, so
`generated_connectors/` must be a **shared (RWX) volume**, or every pod must pull
the same code (the `pull-and-reload` path pulls on the receiving pod only). With a
shared PVC the sequence above is consistent; without one, ensure each pod's
deploy pipeline syncs the code before relying on the broadcast.

Controlled by **`CONNECTOR_RELOAD_CHANNEL`** (default `shielva:connectors:reload`)
and `REDIS_URL`.

---

## Configuration summary

| Env var | Default | Purpose |
|---|---|---|
| `CONNECTOR_AST_SCAN` | `enforce` | `enforce` \| `warn` \| `off` — static load-time scan |
| `CONNECTOR_IMPORT_TIMEOUT_S` | `10` | Wall-clock per module import |
| `CONNECTOR_INVOKE_TIMEOUT_S` | `30` | Wall-clock per method invocation |
| `CONNECTOR_RELOAD_CHANNEL` | `shielva:connectors:reload` | Redis pub/sub channel for HA reload |
| `REDIS_URL` | `redis://localhost:6379/0` | Redis used for reload fan-out (and existing connector state) |
