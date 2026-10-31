# Installing the upgrade agent on OpenShift

The agent runs as an agent-sandbox `Sandbox` on the MCE (hub) cluster, installed with the Helm chart in
`chart/upgrade-agent/`. It is read-only: it diagnoses and proposes, it never changes a cluster.
`README.md` has the full detail; this file is the install order.

## Prerequisites

- `oc` logged in as cluster admin on the MCE/hub, and `helm` 3.
- `podman` (or use the in-cluster binary build below).
- An OpenAI-compatible model endpoint (vLLM, a gateway, ...) reachable from the cluster.
- For an air gap: every image in `images-to-mirror.txt` mirrored into your registry first
  (for example with `skopeo copy` or `oc image mirror`).

## 1. Install agent-sandbox (once per MCE)

```bash
oc apply -f https://github.com/kubernetes-sigs/agent-sandbox/releases/download/v1.0.9/sandbox.yaml
oc -n agent-sandbox-system rollout status deploy/agent-sandbox-controller
oc get crd sandboxes.agents.x-k8s.io
```

Air gap: download `sandbox.yaml`, rewrite the controller image to your mirror
(`registry.k8s.io/agent-sandbox/agent-sandbox-controller:v1.0.9`), and `oc apply -f` the copy.
The controller runs under the default `restricted-v2` SCC.

## 2. Create the namespace and build the image

```bash
oc new-project upgrade-agent
```

Either build in-cluster (the chart's default image, `image-registry.openshift-image-registry.svc:5000/upgrade-agent/upgrade-agent:latest`):

```bash
oc new-build --name upgrade-agent --binary --strategy docker
oc start-build upgrade-agent --from-dir agent --follow
```

Or build and push to your own registry, then set `image.repository` / `image.tag` in the values:

```bash
podman build -t <registry>/upgrade-agent:0.1.0 agent/
podman push <registry>/upgrade-agent:0.1.0
```

The Dockerfile's base image is `python:3.12-slim` (in `images-to-mirror.txt`).

## 3. Create the Secrets

The chart never holds a credential; it references Secrets that already exist.

```bash
# model API key (skip for an endpoint without auth)
oc -n upgrade-agent create secret generic upgrade-agent-model --from-literal=api-key='<api key>'
# bearer token the upgrade-agent-worker must send (optional)
oc -n upgrade-agent create secret generic upgrade-agent-token --from-literal=token='<random token>'
```

## 4. Give the agent a read-only identity on each hosted cluster

Never use the HostedCluster's admin kubeconfig for the agent. Create its own reader:

```bash
oc --kubeconfig <hosted kubeconfig> apply -f hosted-identity/upgrade-agent-reader.yaml
hosted-identity/make-kubeconfig-secret.sh <hosted kubeconfig> <cluster> upgrade-agent
# -> Secret upgrade-agent-hosted-<cluster> in namespace upgrade-agent
```

`<cluster>` is the day1 cluster name. A cluster without a Secret is still diagnosed, from evidence and
the MCE side only.

## 5. Write the values and install the chart

```yaml
# values-<mce>.yaml
model:
  baseUrl: http://<vllm service>.<namespace>.svc:8000/v1
  name: <served model name>
  apiKeySecret: {secretName: upgrade-agent-model}
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

vLLM needs `--enable-auto-tool-choice --tool-call-parser <parser>`, or set `model.toolMode: prompt`.

## 6. Verify

```bash
oc -n upgrade-agent port-forward svc/upgrade-agent 8080:8080 &
curl -s localhost:8080/healthz
curl -s localhost:8080/readyz
curl -s -X POST localhost:8080/v1/invoke \
  -H 'Content-Type: application/json' -H "Authorization: Bearer <token>" \
  --data @sample-request.json | jq .
```

The worker's endpoint is `http://upgrade-agent.upgrade-agent.svc:8080`.

## Operating notes

- After a `helm upgrade` that changes the image, config or a mount, delete the pod; the controller does
  not recreate it: `oc -n upgrade-agent delete pod -l app.kubernetes.io/instance=upgrade-agent`.
- `persistence.enabled` cannot be toggled on an existing Sandbox; reinstall.
- Optional hardening: `networkPolicy.enabled: true` with `networkPolicy.egress` rules (DNS, model, MCE
  and hosted API servers).

## Air gap: load the saved images

`images/*.tar` are `podman save` archives of the images in `images-to-mirror.txt`, built for
`linux/amd64` and stored with Git LFS (`git lfs pull` after cloning).

```bash
podman load -i images/agent-sandbox-controller-v1.0.9-amd64.tar
podman load -i images/python-3.12-slim-amd64.tar
podman tag registry.k8s.io/agent-sandbox/agent-sandbox-controller:v1.0.9 <registry>/agent-sandbox/agent-sandbox-controller:v1.0.9
podman push <registry>/agent-sandbox/agent-sandbox-controller:v1.0.9
```
