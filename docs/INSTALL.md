# Installing a control station (`maestro`)

This guide takes you from a bare Linux box to a working `maestro` — the control
station that runs the orchestration API, the web dashboard, and the `dgx-config`
CLI. It is written to be followed on **any** LAN, not just the cluster described
in [`../README.md`](../README.md).

Architecture background lives in [`../README.md`](../README.md). This file stays
a setup guide.

**Two scenarios:**

- **A — joining an existing cluster.** You are adding a second control station
  pointed at compute nodes that already exist and are already configured. Do
  [Part 1](#part-1--prerequisites), [Part 2](#part-2--install-the-control-station),
  and [Part 5](#part-5--verify).
- **B — standing up a new cluster.** Your own compute nodes, your own subnet,
  your own SSH key. Do all six parts.

---

## Part 1 — Prerequisites

### On `maestro`

`maestro` does no GPU work. Any always-on Linux host on the same LAN as the
compute nodes will do — a small server, a VM, an old workstation.

| Requirement | Check | Notes |
| --- | --- | --- |
| Docker Engine | `docker --version` | |
| Docker Compose v2 | `docker compose version` | The plugin, not the old `docker-compose` binary |
| Git | `git --version` | |
| Your user in the `docker` group | `docker ps` runs without `sudo` | `dgx-config` shells out to `docker exec`; without this every CLI call fails |
| TCP `5000` and `5001` free | `ss -ltn '( sport = :5000 or sport = :5001 )'` | Both are published on all interfaces |
| Network route to every compute node's management IP | `ping <node-ip>` | |

Architecture is not a constraint — both images (`python:3.12-slim` and
`nginx:alpine`) are multi-arch, so x86_64 and arm64 hosts both work.

You do **not** need Python, pip, or a virtualenv on `maestro`. Everything runs
inside the container, which is the point — it sidesteps PEP 668 host
restrictions entirely.

### On each compute node

For scenario A these are already done. For scenario B, see
[Part 3](#part-3--prepare-the-compute-nodes-scenario-b).

---

## Part 2 — Install the control station

### 2.1 Clone

```bash
mkdir -p ~/docker && cd ~/docker
git clone https://github.com/imrobertson/orchestrator.git
cd orchestrator
```

If you intend to run this cluster offline, read `USERMANUAL.md`'s
offline section before pre-caching — `cache_cluster_assets.py` is serial,
so a cold cache takes hours rather than minutes, and it does not cover
weights staged outside the shared HuggingFace cache.

The path is a convention, not a requirement — nothing in the code resolves it.
The only place `~/docker/orchestrator` is hardcoded is the hint text in
`dgx-config`'s "container is not running" error. If you install elsewhere, that
one message will point at the wrong directory.

### 2.2 Provide a HuggingFace token

Needed to pull gated models and tokenizer configs.

```bash
cp .secrets.example .secrets
$EDITOR .secrets          # set HF_TOKEN="hf_..."
chmod 600 .secrets
```

`.secrets` is in `.gitignore`. Do not commit it.

The token is resolved by `get_hf_token()` in `common/ssh.py`, in this order —
the first non-empty value wins:

1. the `HF_TOKEN` environment variable
2. an `HF_TOKEN=` line in `<repo root>/.secrets`
3. `~/.cache/huggingface/token`

Inside the container, (2) resolves to `/app/.secrets` (the bind-mounted repo)
and (3) resolves to `/root/.cache/huggingface/token`, which `docker-compose.yml`
mounts read-only from `maestro`'s own HuggingFace cache. So if you have already
run `huggingface-cli login` on `maestro`, path (3) works with no `.secrets` file
at all. If no token is found anywhere, the daemon logs a warning and continues —
public models still work.

`docker-compose.yml` also passes `HF_TOKEN=${HF_TOKEN:-}` through from the
shell environment. That is path (1) and is separate from `.secrets`; either is
sufficient.

### 2.3 Stage the shared cluster SSH key

The daemon SSHes into the compute nodes using a shared key whose **filename**
comes from `ssh_key_name` in `cluster_config.yaml` (default:
`id_dgx_orchestrator`) and which must sit at the **repo root**.

**Scenario A** — copy the existing key from whoever administers the cluster:

```bash
cp /path/to/id_dgx_orchestrator .
chmod 600 id_dgx_orchestrator
```

**Scenario B** — generate a fresh pair; see
[Part 3.2](#32-authorize-the-cluster-key) for installing the public half.

```bash
ssh-keygen -t ed25519 -f ./id_dgx_orchestrator -C "dgx-orchestrator" -N ""
chmod 600 id_dgx_orchestrator
```

Both `id_dgx_orchestrator` and `id_dgx_orchestrator.pub` are in `.gitignore`.

`resolve_user_identity_key()` in `common/ssh.py` copies this key into
`~/.ssh/<ssh_key_name>` at `0600` on first use, to work around OpenSSH refusing
group-readable keys. If the repo-root key is missing it falls back to
`~/.ssh/id_ed25519`. Inside the container `~` is `/root`, and
`docker-compose.yml` bind-mounts `maestro`'s `~/.ssh` there — so that staged
copy lands in the home directory of **whichever account started the Compose
stack**.

### 2.4 Point the config at your cluster (scenario B)

Edit `cluster_config.yaml`. For a different LAN these are the keys that must
change:

```yaml
ssh_user: tetrel                 # the account on the compute nodes
ssh_key_name: id_dgx_orchestrator

hosts:
  # ORDER IS LOAD-BEARING. The first entry becomes PRIMARY_HOST: the head
  # node of every 2-node Ray deploy, and the host whose /metrics feed the
  # session tracker.
  node-a:
    alias: node-a-shortid
    role: head
    management_ip: 10.0.14.41    # SSH / Gloo / --master-addr rides this
    backplane_ip: 192.168.99.1   # the direct fabric between nodes
    volume_mount: /home/tetrel/.cache/huggingface:/root/.cache/huggingface
    active: true
  node-b:
    alias: node-b-shortid
    role: worker
    management_ip: 10.0.14.43
    backplane_ip: 192.168.99.2
    volume_mount: /home/tetrel/.cache/huggingface:/root/.cache/huggingface
    active: true

default_deploy_target: node-b    # optional; must be active and NOT reserved

network:
  topology: switched
  interface: enp1s0f0np0         # management NIC name ON THE COMPUTE NODES
  nccl_ib_hca: rocep1s0f0        # RoCE HCA name ON THE COMPUTE NODES
```

Gotchas worth knowing before you start:

- **`network.interface` and `network.nccl_ib_hca` are the compute nodes'
  interface names, not `maestro`'s.** Get them from the node itself:
  `ip -br link` and `ibv_devices`.
- **`volume_mount`'s host side must match `ssh_user`'s actual home.** It is
  passed straight to `docker run -v` on the node.
- **`default_deploy_target` is validated at load.** Naming an unknown,
  `active: false`, or `reserved: true` host raises at startup with the file path
  and the specific problem. It never silently falls back.
- **Reordering `hosts:` moves the Ray head.** It is not cosmetic — it moves
  `PRIMARY_HOST`, the 2-node Ray head, and the host whose `/metrics` feed
  session tracking, all at once.
- **Reserve the head, and make it the first host.** The head is the node
  guaranteed to have something running that will answer; a worker is not.
  So `hosts:` order, `role: head` and `reserved: true` should move
  together, and `default_deploy_target` should always name the *other*
  node. `common/config.py` validates that last part and nothing enforces
  the rest, so it is a convention you have to hold yourself. See
  `DIRECTION.md`'s decisions of record.

Everything else — `ports:`, `container_names:`, `tuning:` — can stay at its
current value for a first bring-up.

### 2.5 Bring up the stack

```bash
docker compose up -d --build
```

This starts two containers: `dgx-orchestrator-api` (port `5001`) and
`dgx-dashboard-ui` (port `5000`). Both are `restart: unless-stopped`, so they
come back after a reboot of `maestro`.

### 2.6 Install the CLI wrapper

```bash
sudo ln -sf ~/docker/orchestrator/dgx-config /usr/local/bin/dgx-config
```

`dgx-config` is a thin wrapper: it checks the API container is running, flushes
stale SSH multiplex sockets, and `docker exec`s into the container with your
`$USER` injected for attribution. It is not a separate program — it needs the
stack up.

---

## Part 3 — Prepare the compute nodes (scenario B)

Skip this if the nodes are already serving an existing cluster.

### 3.1 Baseline

On each node:

- Create the account named by `ssh_user`, with a home directory matching the
  host side of `volume_mount`.
- Install Docker with the NVIDIA container runtime, and add that account to the
  `docker` group. The orchestrator issues `docker` commands over SSH as
  `ssh_user`; it does not use `sudo`.
- Set the node headless — no `gdm3`, no `gnome-shell`:

  ```bash
  sudo systemctl set-default multi-user.target
  sudo systemctl stop gdm3
  ```

- Create the HuggingFace cache directory that `volume_mount` points at:

  ```bash
  mkdir -p ~/.cache/huggingface
  ```

- Decide your Docker boot policy. On this cluster, Docker auto-start is
  **disabled** on both nodes to avoid GPU driver panics and OOM loops on
  reboot, and started by hand with `systemctl start docker`. If you keep
  auto-start enabled, expect the orchestrator to find containers it did not
  launch after an unplanned reboot.

### 3.2 Authorize the cluster key

From `maestro`, for each node:

```bash
ssh-copy-id -i ./id_dgx_orchestrator.pub <ssh_user>@<management_ip>
```

Then confirm — from `maestro`, non-interactively, exactly as the daemon will:

```bash
ssh -i ./id_dgx_orchestrator -o BatchMode=yes <ssh_user>@<management_ip> \
    'hostname && docker ps && nvidia-smi --query-gpu=name --format=csv,noheader'
```

All three must succeed. If `docker ps` needs `sudo`, group membership has not
taken effect — log the account out and back in.

### 3.3 Node-to-node fabric

The nodes must also reach **each other** over the management subnet
(`--master-addr` rendezvous and Gloo ride it) and over the RoCE fabric
(`backplane_ip`, where NCCL tensor traffic goes). Multi-node deploys need
`GLOO_SOCKET_IFNAME`, `NCCL_SOCKET_IFNAME`, `NCCL_IB_HCA`,
`NCCL_IB_GID_INDEX=3` and `NCCL_CUMEM_ENABLE=0` set per-recipe — see
[`../README.md`](../README.md)'s network section and any existing 2-node recipe
in `recipes/local/` for the exact values.

A 1-node deploy needs none of this. If you are bringing up a new cluster, get a
1-node recipe serving before you touch the fabric.

---

## Part 4 — LAN access and security

Read this before exposing `maestro` beyond a trusted segment.

**Both ports must be open to the browser.** `docker-compose.yml` publishes
`5000:80` and `5001:5001` on all interfaces. The dashboard is served from `5000`,
but its JavaScript builds the API base URL as
`http://${window.location.hostname}:5001/api` (`API_PORT` in `html/index.html`).
A firewall that opens only `5000` produces a page that loads and then shows
"API Connection Lost" — the failure looks like a broken daemon, not a firewall
rule.

**Use a hostname or IP your coworkers can also resolve.** Because the API host
is derived from `window.location.hostname`, reaching the dashboard at
`http://localhost:5000` from an SSH tunnel makes the browser call
`http://localhost:5001` — which only works if that port is tunnelled too.

**The dashboard sends no credentials.** Every `fetch()` in `html/index.html` is
a bare call with no `Authorization` header or token. Deploy, teardown and
network-mode toggle are all plain `POST`s. Treat anyone who can reach port
`5001` as able to deploy and tear down models.

> Not verified in this pass: whether `dgx-orchestrator.py` itself enforces any
> authentication middleware on the FastAPI app. Confirm that before putting
> `maestro` on an untrusted network — do not rely on this note either way.

**Reasonable postures:** keep both ports on a trusted VLAN; or bind them to
`127.0.0.1` in `docker-compose.yml` and reach the dashboard over Tailscale or a
reverse proxy that fronts both ports on the same hostname.

**The stack holds real credentials.** `~/.ssh` is bind-mounted read-write into
the API container, and `.secrets` carries a HuggingFace token. Anyone with shell
access to `maestro` — or `docker` group membership on it — has the cluster key.

---

## Part 5 — Verify

Work down this list. Each step isolates a different failure.

**1. Containers are up.**

```bash
docker compose ps
```

Both `dgx-orchestrator-api` and `dgx-dashboard-ui` should show `running`.

**2. The API answers, and is running the code you expect.**

```bash
curl -s http://localhost:5001/api/status | head -40
```

Check `orchestrator_version` against what you expect from the checkout, and
check `stale` / `stale_for_seconds` — a fresh daemon should not be reporting a
stale snapshot.

**3. The CLI reaches the container.**

```bash
dgx-config status
```

An "container is not running" error here means step 1 failed. A permission error
on the Docker socket means your account is not in the `docker` group.

**4. The daemon reaches the nodes.** `dgx-config status` should show each host
with a live docker status and GPU telemetry (temperature, utilization, power,
free unified memory). `Telemetry Unavailable` or an `OFFLINE` docker status
points at SSH, key permissions, or Docker not being started on that node.

**5. The dashboard renders.** Open `http://<maestro>:5000` **from another
machine on the LAN**, not from `maestro` itself — that is the case that actually
exercises the port-5001 requirement. The header should show a server clock, a
`v:` version badge, and per-host cards.

**6. A deploy would do what you think.** Before any real launch:

```bash
dgx-config deploy --model <recipe-key> --nodes 1 --dry-run
```

This makes no SSH connections and executes nothing. It prints the exact
`docker run` command(s), the resolved image, and the target hosts. Read them.
`--dry-run` is a required gate before trusting any deploy-path change.

**7. Then deploy for real.**

```bash
dgx-config deploy --model <recipe-key> --nodes 1 --wait
```

---

## Part 6 — Troubleshooting the install

| Symptom | Likely cause |
| --- | --- |
| `dgx-config`: "Container 'dgx-orchestrator-api' is not running" | Stack is down, or you are in the wrong directory. `docker compose up -d`. |
| `dgx-config`: permission denied on `/var/run/docker.sock` | Your account is not in the `docker` group. Add it, then log out and back in. |
| Dashboard loads, then "API Connection Lost" | Port `5001` is not reachable from the browser. See [Part 4](#part-4--lan-access-and-security). |
| Daemon startup: `fastapi and uvicorn are required` | The image was built without `requirements.txt` installing cleanly. Rebuild with `docker compose build --no-cache`. |
| `load_cluster_config` raises at startup | The error names the file and the specific problem — usually a `default_deploy_target` pointing at an unknown, inactive, or reserved host. |
| `Permission denied (publickey)` reaching a node | The key at the repo root is missing, or its public half is not in the node's `authorized_keys`. Re-run the [3.2](#32-authorize-the-cluster-key) check. |
| Host shows `OFFLINE` | Docker is not started on the node. Auto-start is disabled by design: `systemctl start docker`. |
| `Telemetry Unavailable` on a host that is otherwise up | `nvidia-smi` is not runnable over SSH as `ssh_user`. |
| "No HF_TOKEN found in env, .secrets, or ~/.cache" | Expected if you skipped [2.2](#22-provide-a-huggingface-token). Public models still work; gated ones will not. |
| Deploy refused with HTTP 423 | The target host is `reserved: true`. Add `--force` on the CLI, or confirm in the dashboard dialog. Note a 2-node deploy always spans both hosts. |
| Code edited but behaviour unchanged | The API container did not restart. `docker compose restart orchestrator-api`, then re-check `orchestrator_version`. |

### Updating an existing install

```bash
cd ~/docker/orchestrator
git pull
docker compose restart orchestrator-api
```

The repo is bind-mounted at `/app`, so a Python change needs a restart, not a
rebuild. Rebuild (`docker compose up -d --build`) only when `requirements.txt`
or the `Dockerfile` changes.

**Always confirm the restart landed** — check the version badge next to Server
Time on the dashboard, or `orchestrator_version` in `/api/status`, against what
you expect. A forgotten `git push` before `git pull`-ing on `maestro` silently
leaves the daemon on old code with no error surfaced anywhere. This has happened
for real; see `TOMBSTONES.md` #65.

Note the version badge hashes `dgx-orchestrator.py` only — a change confined to
`common/*.py` will not move it.

### Files created at runtime

`.gitignore` excludes `*.json`, so `model_ledger.json`,
`pending_launch_state.json` and `benchmark_ledger.csv` are **not** in a fresh
clone. They are created on first use. A new control station therefore starts
with no deploy history, no benchmark ledger, and every recipe showing
"this exact configuration has not been confirmed to launch successfully yet" in
the dashboard — expected, not a fault.

---

## Where to go next

- [`../README.md`](../README.md) — architecture, recipe schema, full CLI reference
- [`USERMANUAL.md`](USERMANUAL.md) — day-to-day usage
- [`TOMBSTONES.md`](TOMBSTONES.md) — every fix and incident, newest first
- [`errata.yaml`](errata.yaml) — the recipe rules a new recipe must not violate
- [`DOCMAP.md`](DOCMAP.md) — which document owns what; paste it into a new session
- [`MODELS.md`](MODELS.md) — the catalog, with measured throughput
- [`SMOKE-TEST-PLAYBOOK.md`](SMOKE-TEST-PLAYBOOK.md) — post-change verification
- [`../tools/verify/purpose.md`](../tools/verify/purpose.md) — the harnesses behind individual tombstones, and how to add one
