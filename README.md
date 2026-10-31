# k8s-agent — the HCP upgrade agent on kubernetes-sigs agent-sandbox

The **upgrade agent** of the ClickCluster HCP auto-upgrade: the LLM agent the Temporal workflow
calls only when an upgrade gate fails or a watch stalls. It reads the evidence and the clusters,
diagnoses, and **proposes** fixes from a fixed catalog for a person to approve. It never writes to a
cluster and never decides that the upgrade continues: control goes back to the same gate.

```
agent/                 the service (Python 3.12, FastAPI + httpx), its Dockerfile and tests
chart/upgrade-agent/   Helm chart: the agent as an agent-sandbox Sandbox, its Service, RBAC, config
hosted-identity/       the agent's own read-only identity inside a hosted cluster, and its Secret
sample-request.json    an AgentRequest for a smoke test (the live image-pruner case)
```

## Why agent-sandbox

The owner asked for "Kubernetes Agents under the SIGs of k8s". The SIG project that exists is
**[kubernetes-sigs/agent-sandbox](https://github.com/kubernetes-sigs/agent-sandbox)** (SIG Apps,
release **v1.0.9**); `kubernetes-sigs/kube-agents` is an empty placeholder. agent-sandbox is a
sandbox **orchestrator**, not an LLM framework: its `Sandbox` CRD (`agents.x-k8s.io/v1beta1`) runs
one stateful pod with a stable identity, its own volume and a recreate-on-failure policy. So the
agent itself is our own small service, run in a `Sandbox`, talking to any OpenAI-compatible model.
This replaces the earlier kagent plan (decision D9 of the design).

On the sandbox cluster it is pointed at a small vLLM model on the lab and is **expected not to
diagnose well there**: the plumbing (contract, tools, budgets, idempotency, the catalog fence,
`needs_human` on every failure) is what that run proves. With a real model (GLM 5.3, or a gateway)
in the owner's environment, the same image and chart do the real work.

## Architecture

```
hub                                   MCE  (one of each per MCE)
UpgradeClusterWorkflow ──queue──►  upgrade-agent-worker            (workflows repo, no k8s credential)
  (gate failed)        upgrade-agent-  │ POST /v1/invoke  AgentRequest → AgentVerdict, heartbeating
                       activity-<mce>  ▼
                                   Service upgrade-agent :8080
                                       │
                                   Sandbox upgrade-agent  (agents.x-k8s.io/v1beta1, this chart)
                                     agent/ : tool-calling loop ──► model (OpenAI-compatible /chat/completions)
                                       │ tier-0 READ tools (typed GETs, allowlisted kinds, never Secrets)
                                       ├──► MCE API: this cluster's HostedCluster, NodePools, HCP namespace
                                       │      as the pod's own ServiceAccount (read-only RBAC, chart)
                                       └──► hosted cluster API (kube-apiserver.<hcp-ns>.svc:6443)
                                              as the agent's own reader identity (hosted-identity/)
```

What one invocation does (`agent/upgrade_agent/loop.py`):
1. loads the Brain runbooks for the profile and step from `RUNBOOK_DIR`, if mounted;
2. sends the model the system prompt (`prompt.py`) and the request, with the evidence fenced as data;
3. runs the read tools the model calls and sends back their results, until it calls `submit_verdict`;
4. checks every proposal against the tier-2 catalog (`catalog.py`, the same rules as the
   workflow's `action_catalog.py`). One outside it is dropped and named in the diagnosis, and that
   verdict is never `retry_gate`;
5. answers. Each tool call is in `actions_taken`, as a `ToolCallRecord`.

When anything goes wrong it still answers, with `needs_human` and the reason: model unreachable or
refusing (an HTTP 400 for "no tool calling" included), no usable tool call after `REPAIR_ATTEMPTS`
repair prompts, or a budget spent (`max_tool_calls`, `max_model_calls`, `wall_clock_seconds` from
the request). A run cut short also says what the agent had read so far.

**Idempotent on `invocation_id`.** A retry with the same id gets the stored verdict, or joins the
run still in flight. Verdicts are kept in memory and in `STATE_DIR` (the Sandbox's volume), so a
retry after a pod restart gets the same verdict.

**The tools** (`tools.py`): `evidence`, `hosted_get`, `hosted_list`, `hosted_logs`,
`hosted_events`, `mce_get`, `mce_list`, `mce_logs`, `mce_events`, and `submit_verdict`. There is no
write tool. On the MCE the namespace is never an argument: it is this cluster's HCP namespace (or its
HostedCluster's), found from the HostedCluster named in the request.

**The contract** (`contract.py`) is a vendored copy of `AgentRequest` / `AgentVerdict` from the
workflows repo. `contract.schema.json` is its snapshot, and the workflows repo's
`tests/test_upgrade_agent_contract.py` checks its originals against the same snapshot. Run with
`UPGRADE_AGENT_SERVICE_DIR=<this folder>`, it also compares this folder's models and catalog with
the workflow's directly. To change the contract: change it in the workflows repo, copy the
classes here unchanged, regenerate with `UPDATE_CONTRACT=1 .venv/bin/pytest agent/tests/test_contract.py`,
and copy the snapshot back to `tests/fixtures/upgrade_agent_contract.schema.json` there.

## Tests

```bash
uv venv --python 3.12 .venv            # or: python3.12 -m venv .venv
uv pip install --python .venv/bin/python -r agent/requirements-dev.txt
cd agent && ../.venv/bin/pytest -q
```

No network: a fake OpenAI endpoint and fake API servers (respx). They cover the live image-pruner
case (it reads the failed Job and proposes `rerun_cronjob openshift-image-registry/image-pruner`),
malformed model output, every budget, prompt injection in the evidence, out-of-catalog proposals,
idempotency, auth, and the read tools' fences (no Secrets, names checked, the implied namespace).

## 1. Install agent-sandbox v1.0.9 (cluster admin, once per MCE)

Release assets (`https://github.com/kubernetes-sigs/agent-sandbox/releases/tag/v1.0.9`):

| Manifest | What |
|---|---|
| `https://github.com/kubernetes-sigs/agent-sandbox/releases/download/v1.0.9/sandbox.yaml` | core: the `Sandbox` CRD and the controller, namespace `agent-sandbox-system` (all this chart needs) |
| `https://github.com/kubernetes-sigs/agent-sandbox/releases/download/v1.0.9/extensions.yaml` | extensions: SandboxTemplate, SandboxClaim, SandboxWarmPool (not used here) |
| `https://github.com/kubernetes-sigs/agent-sandbox/releases/download/v1.0.9/sandbox-with-extensions.yaml` | both |

```bash
oc apply -f https://github.com/kubernetes-sigs/agent-sandbox/releases/download/v1.0.9/sandbox.yaml
oc -n agent-sandbox-system rollout status deploy/agent-sandbox-controller
oc get crd sandboxes.agents.x-k8s.io
```

The controller image is `registry.k8s.io/agent-sandbox/agent-sandbox-controller:v1.0.9`. In the air
gap, mirror it and apply a copy of `sandbox.yaml` with the image rewritten. On OpenShift the
controller's pod sets no securityContext, so `restricted-v2` gives it an arbitrary UID
(`UNVERIFIED` until the sandbox install below is done).

## 2. Build the image

From this folder:

```bash
podman build -t <registry>/upgrade-agent:0.1.0 agent/
podman push <registry>/upgrade-agent:0.1.0
```

Or an OpenShift binary build into the internal registry (the chart's default image):

```bash
oc new-project upgrade-agent        # or: oc project upgrade-agent
oc new-build --name upgrade-agent --binary --strategy docker
oc start-build upgrade-agent --from-dir agent --follow
# -> image-registry.openshift-image-registry.svc:5000/upgrade-agent/upgrade-agent:latest
```

The image runs as UID 1001, group 0, with group-writable files, so it also runs under OpenShift's
arbitrary UID. The chart sets `readOnlyRootFilesystem` with an emptyDir `/tmp`.

## 3. Secrets (by name; the chart never holds a credential)

All in the release namespace, values shown as placeholders:

```bash
# The model endpoint's key (skip for an endpoint without auth)
oc -n upgrade-agent create secret generic upgrade-agent-model --from-literal=api-key='<api key>'
# The bearer token the upgrade-agent-worker must send (optional; the worker's chart names the same Secret)
oc -n upgrade-agent create secret generic upgrade-agent-token --from-literal=token='<random token>'
```

### The agent's identity on a hosted cluster

The agent never uses the HostedCluster's admin kubeconfig (AGENT.md #26). Give it its own reader
in each hosted cluster, then a kubeconfig Secret for it on the MCE:

```bash
oc --kubeconfig <hosted kubeconfig> apply -f hosted-identity/upgrade-agent-reader.yaml
hosted-identity/make-kubeconfig-secret.sh <hosted kubeconfig> <cluster> upgrade-agent
# -> Secret upgrade-agent-hosted-<cluster>, key `kubeconfig`
```

`<cluster>` is the day1 cluster name (`AgentRequest.cluster`). The chart mounts each Secret at
`/var/run/upgrade-agent/hosted/<cluster>/`. A Secret with `token` (+ `ca.crt`) keys instead of a
kubeconfig works too, which is the shape of a ManagedServiceAccount token Secret (production,
DESIGN §9 item 5). The agent dials the hosted API server through its Service on the MCE,
`https://kube-apiserver.<hcp-namespace>.svc:6443` (`hostedApiServer`). `UNVERIFIED`: that the CA
in the reader's token Secret verifies that Service's certificate. If TLS fails, put the CA from
the HostedCluster's own kubeconfig (`certificate-authority-data`, public data) into the Secret's
kubeconfig instead.

A cluster with no Secret is still diagnosed, but only from the evidence and the MCE side.

## 4. Install the chart

```yaml
# values-<mce>.yaml
model:
  baseUrl: http://<vllm service>.<namespace>.svc:8000/v1
  name: <served model name>
  apiKeySecret: {secretName: upgrade-agent-model}   # or secretName: "" for no auth
apiToken: {secretName: upgrade-agent-token}
hostedClusters:
  - {name: <cluster>, secretName: upgrade-agent-hosted-<cluster>}
rbac:
  hcpNamespaces: [<hc-namespace>-<hostedcluster-name>]   # or allNamespaces: true
```

```bash
helm install upgrade-agent chart/upgrade-agent -n upgrade-agent -f values-<mce>.yaml
oc -n upgrade-agent get sandbox upgrade-agent      # Ready=True
oc -n upgrade-agent get pods -l app.kubernetes.io/instance=upgrade-agent
```

The endpoint for the workflows repo's `upgrade-agent-worker` (`upgradeAgent.url` in its chart) is
`http://upgrade-agent.upgrade-agent.svc:8080`.

- **RBAC on the MCE (read only):** get/list on HostedClusters and NodePools cluster-wide (to find
  the cluster), then pods, logs, events, services, configmaps, deployments, statefulsets,
  replicasets, jobs, PDBs, HostedControlPlanes and CAPI machines in each listed HCP namespace
  (`<namespace>-<name>`; RBAC has no label selector), or in all namespaces with
  `rbac.allNamespaces`. Never Secrets, never a write verb.
- **NetworkPolicy** (`networkPolicy.enabled`): ingress only from the `upgrade-agent-worker` pods;
  egress open unless `networkPolicy.egress` lists rules (DNS, the model, the MCE and hosted API servers).
- **Runbooks:** `runbooks.configMap` mounts a ConfigMap of Brain markdown (`upgrade-<profile>.md`
  first, then pages tagged for the profile, phase, gate or `upgrade`).
- **Rolling a change:** the agent-sandbox controller does not recreate the pod when
  `podTemplate.spec` changes. After a `helm upgrade` that changes the image, the config or a mount,
  delete the pod: `oc -n upgrade-agent delete pod -l app.kubernetes.io/instance=upgrade-agent`.
- **Persistence:** `volumeClaimTemplates` are immutable on a Sandbox, so toggling
  `persistence.enabled` needs a reinstall.

## 5. Point it at a model

Any OpenAI-compatible Chat Completions API (`POST {baseUrl}/chat/completions`, tools in the `tools`
parameter):

| Endpoint | `model.baseUrl` | `model.name` | Notes |
|---|---|---|---|
| vLLM | `http://<vllm>:8000/v1` | the `--served-model-name` | serve with `--enable-auto-tool-choice --tool-call-parser <parser for the model>` (e.g. `hermes`); without them vLLM answers 400 and every verdict is `needs_human` naming it. Or set `model.toolMode: prompt` |
| OpenAI | `https://api.openai.com/v1` | e.g. `<gpt model>` | `apiKeySecret` required |
| A gateway (one in front of Anthropic, GLM, ...) | `https://<gateway>/v1` | the gateway's model id | as long as it speaks Chat Completions with `tools` |

`model.toolMode: prompt` describes the tools in the system prompt and reads JSON from the reply,
for an endpoint without tool calling. A small model also often writes its tool call as text (a JSON
object, a ```json block, `<tool_call>` tags); the agent reads those in either mode.
`model.caConfigMap` takes a PEM CA for an endpoint signed by an internal CA.

## 6. Smoke test

```bash
oc -n upgrade-agent port-forward svc/upgrade-agent 8080:8080 &
curl -s localhost:8080/healthz
curl -s localhost:8080/readyz          # the model it would use; never the key
curl -s -X POST localhost:8080/v1/invoke \
  -H 'Content-Type: application/json' -H "Authorization: Bearer <token>" \
  --data @sample-request.json | jq .
```

`sample-request.json` is the image-pruner pre-flight failure seen live (cluster `demo-hc`). Without a
credential mounted for `demo-hc` the hosted reads answer "no hosted-cluster credential is mounted",
and a small model will most likely come back `needs_human`; a capable one proposes
`rerun_cronjob openshift-image-registry/image-pruner`. A second POST with the same `invocation_id`
returns the same verdict at once. Change the id to run again.

## Configuration reference (environment, set by the chart)

`MODEL_BASE_URL`, `MODEL_NAME`, `MODEL_API_KEY`, `MODEL_TOOL_MODE` (`native`|`prompt`),
`MODEL_TIMEOUT_SECONDS`, `MODEL_MAX_TOKENS`, `MODEL_TEMPERATURE`, `MODEL_TLS_CA`, `REPAIR_ATTEMPTS`,
`AGENT_API_TOKEN`, `STATE_DIR`, `RUNBOOK_DIR`, `MAX_CONCURRENT`, `WALL_CLOCK_MARGIN_SECONDS` (the
agent answers this long before the request's wall clock), `TOOL_RESULT_CHARS`,
`HOSTED_CREDENTIALS_DIR`, `HOSTED_API_SERVER`, `MCE_TOKEN_FILE`, `MCE_CA_FILE`,
`KUBE_TIMEOUT_SECONDS`. See `agent/upgrade_agent/config.py`.

## Not built yet

- Tier 1 (the agent executing a known-safe fix itself in waves 0–1), the judge model, and the
  ValidatingAdmissionPolicies that would fence it. Everything is a tier-2 proposal; `tier1_enabled`
  is read and ignored.
- The ManagedServiceAccount + ManifestWork delivery of the hosted identity (DESIGN §9 item 5);
  `hosted-identity/` is the hand-applied stand-in.
- Brain incident writing stays in the workflow (`write_incident`, hub limb).

## How it was installed on the sandbox

2026-10-31, for the first end-to-end upgrade demo (workspace `deploy/upgrade-demo/`
in Cluster-AutoUpgrade has the scripts). Everything lives in namespace
`upgrade-demo` on the sandbox hub (OCP 4.20.35, MCE 2.17.3), and is deleted with it.

1. **agent-sandbox v1.0.9, core only** (`sandbox.yaml`: namespace
   `agent-sandbox-system`, the controller, the `Sandbox` CRD). The controller runs
   under OpenShift's default `restricted-v2` with no change.

   ```bash
   oc apply -f https://github.com/kubernetes-sigs/agent-sandbox/releases/download/v1.0.9/sandbox.yaml
   ```

2. **The image, built in-cluster** from `agent/`:

   ```bash
   oc -n upgrade-demo new-build --name upgrade-agent --binary --strategy docker
   oc -n upgrade-demo start-build upgrade-agent --from-dir agent --follow
   ```

3. **A key for the model.** The model is the lab's Qwen2.5-0.5B (`qwen-tiny`, vLLM
   on CPU) behind the lab's AI gateway, which takes a key per tenant. The key was
   made in aigw-ui on the hub with its admin token: a tenant `upgrade-agent-demo`,
   a daily quota on `qwen-tiny` (without one the gateway answers 429), then a key,
   piped straight into Secret `upgrade-demo/upgrade-agent-model` (`api-key`) —
   `deploy/upgrade-demo/model-key.sh create`. Neither the admin token nor the key
   was printed or written to a file.

4. **The agent's own reader in the hosted cluster** (`hosted-identity/upgrade-agent-reader.yaml`,
   applied with the hosted cluster's admin kubeconfig held in memory), and Secret
   `upgrade-demo/upgrade-agent-hosted-ocp4-upgrade-demo` built from that reader's
   token — the steps of `make-kubeconfig-secret.sh`, each `oc` call reading the
   admin kubeconfig from its own process substitution.

5. **The chart**, with `deploy/upgrade-demo/agent-values.yaml` (`model.name: qwen-tiny`,
   `model.toolMode: prompt`, the two Secrets, the HCP namespace) and the gateway URL
   taken from aigw-ui's own `GATEWAY_URL` at install time (`install-agent.sh`), so
   no hostname is in a file. A random `upgrade-agent-token` Secret is shared with the
   worker.

**Result:** the Sandbox is Ready and `/v1/invoke` answers. With the lab model the
gateway accepts the key (its `/v1/models` gives 200 with it, 401 without) but the
model times out (`504`), so the agent returns `needs_human` naming the 504 — as
expected on the sandbox. Point `model.baseUrl` / `model.name` at a working model
and the same install gives a diagnosis.
