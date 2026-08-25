"""Live Deployment replica counts for the Fleet tab -- the only place this
API touches the Kubernetes API directly. Read-only (see k8s/base/api-rbac.yaml:
get+list on deployments in the sre-platform namespace only)."""

from kubernetes import client, config
from kubernetes.client.exceptions import ApiException

from db.models import AgentType

FLEET_NAMESPACE = "sre-platform"

# Matches k8s/base/deployment-*.yaml Deployment names exactly.
DEPLOYMENT_NAME = {
    AgentType.TRIAGE: "sre-triage",
    AgentType.DIAGNOSIS: "sre-diagnosis",
    AgentType.REMEDIATION: "sre-remediation",
    AgentType.VALIDATION: "sre-validation",
}

try:
    config.load_incluster_config()
except config.ConfigException:
    # Local/dev fallback only -- the deployed Service always runs in-cluster
    # with the sre-api ServiceAccount mounted (see k8s/base/api-rbac.yaml).
    config.load_kube_config()

apps_v1 = client.AppsV1Api()


def fleet_status() -> list[dict]:
    """One row per AgentType: desired vs. currently-available replicas. A
    Deployment that doesn't exist yet (e.g. before `kubectl apply -k` has
    run) reports zero/zero rather than failing the whole tab."""
    rows = []
    for agent_type, deployment_name in DEPLOYMENT_NAME.items():
        desired = available = 0
        try:
            dep = apps_v1.read_namespaced_deployment(deployment_name, FLEET_NAMESPACE)
            desired = dep.spec.replicas or 0
            available = dep.status.available_replicas or 0
        except ApiException as e:
            if e.status != 404:
                raise
        rows.append({
            "agent_type": agent_type.value,
            "deployment_name": deployment_name,
            "desired_replicas": desired,
            "available_replicas": available,
        })
    return rows
