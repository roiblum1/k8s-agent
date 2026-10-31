"""The read tools: allowlists, the implied MCE namespace, credentials, and no Secrets ever."""

from __future__ import annotations

import base64
from dataclasses import replace

import httpx
import pytest

from tests.conftest import FAILED_PRUNER_JOB, HOSTED_URL, MCE_HOST, make_request
from upgrade_agent import kube, runbooks
from upgrade_agent.tools import Toolbox, summary

MCE_URL = f"https://{MCE_HOST}:443"
HOSTED_CLUSTER = {"metadata": {"name": "demo-hc", "namespace": "hcp-demo-hc", "uid": "hc-1"},
                  "status": {"version": {"desired": {"image": "x"}}, "conditions": [
                      {"type": "Available", "status": "True"}]}}


@pytest.fixture
def mce_hostedclusters(mock_http):
    return mock_http.get(f"{MCE_URL}/apis/hypershift.openshift.io/v1beta1/hostedclusters").mock(
        return_value=httpx.Response(200, json={"items": [HOSTED_CLUSTER]}))


async def _call(settings, tool, **args):
    async with Toolbox(make_request(), settings) as tools:
        return await tools.call(tool, args)


@pytest.mark.parametrize(("tool", "args"), [
    ("hosted_get", {"kind": "Secret", "name": "pull-secret", "namespace": "openshift-config"}),
    ("hosted_list", {"kind": "secrets", "namespace": "openshift-config"}),
    ("mce_get", {"kind": "Secret", "name": "admin-kubeconfig"}),
    ("mce_list", {"kind": "Secret"}),
])
async def test_secrets_are_never_read(mock_http, settings, tool, args):
    result = await _call(settings, tool, **args)
    assert result == "error: Secrets are never read"
    assert not mock_http.calls


async def test_a_kind_outside_the_allowlist_is_refused(settings):
    result = await _call(settings, "hosted_list", kind="ServiceAccount")
    assert result.startswith("error: kind 'ServiceAccount' is not readable here")


async def test_names_are_checked_before_any_request(mock_http, settings):
    result = await _call(settings, "hosted_get", kind="Pod", name="../../secrets/x", namespace="default")
    assert "is not a Kubernetes name" in result
    assert not mock_http.calls


async def test_unknown_arguments_are_an_error_not_a_crash(settings):
    assert (await _call(settings, "evidence", section="events", verb="delete")).startswith("error: bad arguments")


async def test_no_such_tool(settings):
    assert await _call(settings, "hosted_delete", kind="Pod") == "error: no tool named 'hosted_delete'"


async def test_the_mce_namespace_is_the_clusters_hcp_namespace(mock_http, settings, mce_hostedclusters):
    pods = mock_http.get(f"{MCE_URL}/api/v1/namespaces/hcp-demo-hc-demo-hc/pods").mock(
        return_value=httpx.Response(200, json={"items": [{"metadata": {"name": "cluster-version-operator-7d9f",
                                                                        "namespace": "hcp-demo-hc-demo-hc"},
                                                           "status": {"phase": "Running"}}]}))
    result = await _call(settings, "mce_list", kind="Pod")
    assert pods.called and result.startswith("1 Pod\n")
    assert pods.calls.last.request.headers["Authorization"] == "Bearer mce-sa-token"


async def test_the_hostedcluster_named_in_the_request_is_read_where_it_says(mock_http, settings):
    # The day1 file is demo-hc; the chart renders HostedCluster openshift-demo-hc in hcp-demo-hc.
    hc = {"metadata": {"name": "openshift-demo-hc", "namespace": "hcp-demo-hc"}}
    get = mock_http.get(f"{MCE_URL}/apis/hypershift.openshift.io/v1beta1/namespaces/hcp-demo-hc/hostedclusters/"
                        "openshift-demo-hc").mock(return_value=httpx.Response(200, json=hc))
    pods = mock_http.get(f"{MCE_URL}/api/v1/namespaces/hcp-demo-hc-openshift-demo-hc/pods").mock(
        return_value=httpx.Response(200, json={"items": []}))
    nodepools = mock_http.get(f"{MCE_URL}/apis/hypershift.openshift.io/v1beta1/namespaces/hcp-demo-hc/nodepools").mock(
        return_value=httpx.Response(200, json={"items": [
            {"metadata": {"name": "workers"}, "spec": {"clusterName": "openshift-demo-hc", "replicas": 2}},
            {"metadata": {"name": "theirs"}, "spec": {"clusterName": "other"}}]}))
    request = make_request(hosted_cluster="openshift-demo-hc", hosted_cluster_namespace="hcp-demo-hc")
    async with Toolbox(request, settings) as tools:
        assert (await tools.call("mce_list", {"kind": "Pod"})).startswith("0 Pods")
        listed = await tools.call("mce_list", {"kind": "NodePool"})
        refused = await tools.call("mce_get", {"kind": "HostedCluster", "name": "demo-hc"})
    assert get.call_count == 1 and pods.called and nodepools.called
    assert listed.startswith("1 NodePools\n") and "workers" in listed and "theirs" not in listed
    assert "only this cluster's HostedCluster (openshift-demo-hc)" in refused


async def test_only_this_clusters_hostedcluster_is_readable_on_the_mce(settings, mce_hostedclusters):
    assert "only this cluster's HostedCluster" in await _call(settings, "mce_get", kind="HostedCluster",
                                                              name="other-hc")


async def test_two_hostedclusters_of_one_name_is_an_error(mock_http, settings):
    twin = {**HOSTED_CLUSTER, "metadata": {**HOSTED_CLUSTER["metadata"], "namespace": "elsewhere"}}
    mock_http.get(f"{MCE_URL}/apis/hypershift.openshift.io/v1beta1/hostedclusters").mock(
        return_value=httpx.Response(200, json={"items": [HOSTED_CLUSTER, twin]}))
    assert "2 HostedClusters named demo-hc" in await _call(settings, "mce_events")


async def test_log_tails_are_capped(mock_http, settings):
    logs = mock_http.get(f"{HOSTED_URL}/api/v1/namespaces/openshift-image-registry/pods/p-1/log").mock(
        return_value=httpx.Response(200, text="line\n"))
    await _call(settings, "hosted_logs", namespace="openshift-image-registry", pod="p-1", tail_lines=100000)
    assert logs.calls.last.request.url.params["tailLines"] == "300"


async def test_a_forbidden_read_is_a_result_the_model_can_reason_about(mock_http, settings):
    mock_http.get(f"{HOSTED_URL}/api/v1/nodes").mock(return_value=httpx.Response(
        403, json={"message": 'nodes is forbidden: User "system:serviceaccount:x:upgrade-agent" cannot list'}))
    assert (await _call(settings, "hosted_list", kind="Node")).startswith("error: HTTP 403: nodes is forbidden")


async def test_no_credential_for_the_cluster_says_so(settings, tmp_path):
    result = await _call(replace(settings, hosted_credentials_dir=str(tmp_path / "none")), "hosted_list", kind="Node")
    assert "no hosted-cluster credential is mounted for demo-hc" in result


async def test_the_hosted_server_is_the_kas_service_seen_from_the_mce(mock_http, settings, mce_hostedclusters):
    nodes = mock_http.get("https://kube-apiserver.hcp-demo-hc-demo-hc.svc:6443/api/v1/nodes").mock(
        return_value=httpx.Response(200, json={"items": []}))
    await _call(replace(settings, hosted_api_server="https://kube-apiserver.{hcp_namespace}.svc:6443"),
                "hosted_list", kind="Node")
    assert nodes.called


async def test_a_credential_with_a_broken_ca_is_an_error_not_a_crash(settings, hosted_credentials):
    kubeconfig = hosted_credentials / "demo-hc" / "kubeconfig"
    kubeconfig.write_text(kubeconfig.read_text().replace(
        "server: https://hosted-api.test:6443", "server: https://hosted-api.test:6443, certificate-authority-data: bm90IGEgY2VydA=="))
    result = await _call(settings, "hosted_list", kind="Node")
    assert result.startswith("error: the hosted cluster cannot be read: its credential for demo-hc is unusable")


def test_a_kubeconfig_that_skips_tls_verification_is_refused():
    with pytest.raises(kube.KubeError, match="turns TLS verification off"):
        kube.target_from_kubeconfig("""
current-context: c
contexts: [{name: c, context: {cluster: k, user: u}}]
clusters: [{name: k, cluster: {server: "https://x:6443", insecure-skip-tls-verify: true}}]
users: [{name: u, user: {token: t}}]
""")


def test_a_kubeconfig_with_an_exec_plugin_is_refused():
    with pytest.raises(kube.KubeError, match="exec or auth-provider"):
        kube.target_from_kubeconfig("""
current-context: c
contexts: [{name: c, context: {cluster: k, user: u}}]
clusters: [{name: k, cluster: {server: "https://x:6443"}}]
users: [{name: u, user: {exec: {command: oc}}}]
""")


def test_a_token_secret_shaped_like_a_managed_service_account_works(tmp_path):
    directory = tmp_path / "demo-hc"
    directory.mkdir()
    (directory / "token").write_text("msa-token\n")
    target = kube.hosted_target(str(tmp_path), "demo-hc", "https://kube-apiserver.ns.svc:6443")
    assert (target.server, target.token, target.ca_pem) == ("https://kube-apiserver.ns.svc:6443", "msa-token", None)


def test_kubeconfig_ca_data_is_decoded():
    ca = "-----BEGIN CERTIFICATE-----\nMIIB\n-----END CERTIFICATE-----\n"
    target = kube.target_from_kubeconfig(f"""
current-context: c
contexts: [{{name: c, context: {{cluster: k, user: u}}}}]
clusters: [{{name: k, cluster: {{server: "https://x:6443/", certificate-authority-data: {base64.b64encode(ca.encode()).decode()}}}}}]
users: [{{name: u, user: {{token: t}}}}]
""")
    assert (target.server, target.ca_pem) == ("https://x:6443", ca)


def test_a_job_summary_shows_what_a_diagnosis_needs():
    line = summary("Job", FAILED_PRUNER_JOB)
    assert line.startswith("openshift-image-registry/image-pruner-29890999 uid=5b2c-9a1e")
    assert "failed=1" in line and "owner=CronJob/image-pruner" in line and "Failed=True(BackoffLimitExceeded)" in line


def test_runbooks_are_picked_by_profile_and_tags(tmp_path):
    (tmp_path / "hypershift").mkdir()
    (tmp_path / "hypershift/upgrade-generic.md").write_text("# generic\n")
    (tmp_path / "hypershift/upgrade-zoned.md").write_text("# zoned\n")
    (tmp_path / "hypershift/pruner.md").write_text("---\ntags: [upgrade, image-registry]\napplies_to: [ocp-4.20]\n---\n")
    (tmp_path / "hypershift/old.md").write_text("---\ntags: [upgrade]\napplies_to: [ocp-4.16]\n---\n")
    (tmp_path / "hypershift/other.md").write_text("---\ntags: [dhcp]\n---\n")
    loaded = runbooks.load(str(tmp_path), profile="generic", phase="preflight", gate="preflight", version="4.20.18")
    assert [r.name for r in loaded] == ["hypershift/upgrade-generic.md", "hypershift/pruner.md"]
    assert runbooks.load("", profile="generic", phase="p", gate="g", version="4.20.1") == []
