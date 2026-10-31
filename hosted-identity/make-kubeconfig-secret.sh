#!/usr/bin/env bash
# Build the agent's hosted-cluster kubeconfig Secret on the MCE from the token of
# hosted-identity/upgrade-agent-reader.yaml. No file is left behind: the kubeconfig
# goes straight from a pipe into `oc create secret`. The server is a placeholder the
# agent replaces with the KAS Service (chart value hostedApiServer); the CA is the
# hosted cluster's, as its token Secret carries it.
#
#   hosted-identity/make-kubeconfig-secret.sh <hosted kubeconfig> <cluster> <agent namespace on the MCE>
#
# <cluster> is the day1 cluster name, the one the workflow sends as AgentRequest.cluster;
# the chart value is then hostedClusters: [{name: <cluster>, secretName: upgrade-agent-hosted-<cluster>}].
set -euo pipefail
hosted_kubeconfig="$1" cluster="$2" namespace="$3"

token="$(oc --kubeconfig "$hosted_kubeconfig" -n upgrade-agent get secret upgrade-agent-token -o jsonpath='{.data.token}' | base64 -d)"
ca="$(oc --kubeconfig "$hosted_kubeconfig" -n upgrade-agent get secret upgrade-agent-token -o jsonpath='{.data.ca\.crt}')"

oc -n "$namespace" create secret generic "upgrade-agent-hosted-$cluster" --from-file=kubeconfig=/dev/stdin <<KUBECONFIG
apiVersion: v1
kind: Config
current-context: upgrade-agent
contexts: [{name: upgrade-agent, context: {cluster: hosted, user: upgrade-agent}}]
clusters: [{name: hosted, cluster: {server: "https://kube-apiserver:6443", certificate-authority-data: "$ca"}}]
users: [{name: upgrade-agent, user: {token: "$token"}}]
KUBECONFIG
