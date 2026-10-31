"""The catalog checks proposals with the workflow's own rules (its action_catalog.py, same cases).

The workflows repo's tests/test_upgrade_agent_contract.py runs a table of
cases through BOTH catalogs when it can find this folder, so the two cannot
drift apart unnoticed.
"""

from __future__ import annotations

import pytest

from upgrade_agent import catalog


@pytest.mark.parametrize(("kind", "target"), [
    ("delete_pod", "openshift-console/downloads-a"),
    ("delete_hcp_pod", "cluster-version-operator-7d9f"),
    ("rerun_cronjob", "openshift-operator-lifecycle-manager/collect-profiles"),
    ("delete_failed_job", "openshift-image-registry/image-pruner-29365920"),
])
def test_in_the_catalog(kind, target):
    catalog.check(kind, target)


@pytest.mark.parametrize(("kind", "target", "why"), [
    ("delete_deployment", "openshift-console/console", "not in the catalog"),
    ("delete_pod", "shop/db-0", "only namespaces openshift-*"),
    ("delete_pod", "openshift-ovn-kubernetes/ovnkube-node-x", "never deleted"),
    ("delete_pod", "openshift-multus/multus-x", "never deleted"),
    ("delete_pod", "downloads-a", "<namespace>/<name>"),
    ("delete_pod", "openshift-console/", "<namespace>/<name>"),
    ("delete_hcp_pod", "etcd-0", "never deleted"),
    ("delete_hcp_pod", "kube-apiserver-5f6d", "never deleted"),
    ("delete_hcp_pod", "ns/pod", "pod in the cluster's HCP namespace"),
    ("rerun_cronjob", "default/backup", "only namespaces openshift-*"),
    ("delete_failed_job", "shop/nightly-report-1", "only namespaces openshift-*"),
    ("delete_job", "openshift-image-registry/image-pruner-1", "not in the catalog"),
])
def test_refused(kind, target, why):
    with pytest.raises(catalog.Refused, match=why):
        catalog.check(kind, target)


def test_accept_keeps_valid_ones_with_stable_url_safe_ids_and_only_the_uid():
    kept, notes = catalog.accept("upgrade-cluster-x/1", [
        catalog.Proposal("delete_failed_job", "openshift-image-registry/image-pruner-1",
                         {"uid": "u-1", "force": "true"}, "failed Job"),
        catalog.Proposal("delete_namespace", "openshift-image-registry", {}, "no"),
        catalog.Proposal("rerun_cronjob", "openshift-image-registry/image-pruner", {}, "re-run"),
        catalog.Proposal("rerun_cronjob", "openshift-image-registry/image-pruner", {}, "twice"),
    ])
    assert [(a.action_id, a.kind, a.args) for a in kept] == [
        (catalog.action_id("upgrade-cluster-x/1", 1), "delete_failed_job", {"uid": "u-1"}),
        (catalog.action_id("upgrade-cluster-x/1", 2), "rerun_cronjob", {}),
    ]
    assert all(a.action_id.replace("-", "").isalnum() for a in kept)
    assert catalog.action_id("upgrade-cluster-x/1", 1) != catalog.action_id("upgrade-cluster-x/2", 1)
    assert any("ignored args force" in n for n in notes)
    assert any(n.startswith("dropped proposal delete_namespace") for n in notes)
