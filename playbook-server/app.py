"""
Playbook server — the ONLY thing in this platform that calls the real
Kubernetes API. RemediationAgent.execute_playbook() (agents/remediation.py)
is a plain HTTP client that POSTs to these routes; nothing about "what API
to call" lives in the agent -- these routes ARE the documented API for
each playbook (see db/seed.py's Playbook.endpoint/params_schema).

Auth: a single shared bearer token (PLAYBOOK_SERVER_TOKEN), checked here
directly -- no nginx sidecar, this is an in-cluster-only Service, never
internet-facing.

Restore semantics (scale_hpa/restore_hpa, scale_deployment/restore_deploy-
ment): capture-before-mutate. Before patching a live value, the prior value
is read and stashed as an annotation on the object itself, so "restore"
(called with only {service}, no target value -- see RemediationAgent.
_handle_failure()'s LOW-tier auto-rollback path) restores the exact prior
value rather than a guessed baseline. State lives on the K8s object, not a
new DB/cache -- this process stays stateless.

increase_db_pool (PB-011, POST /api/v1/pool) has no route here on purpose:
its target ("config.internal") is fictional, so Flask's default 404 for an
unmatched route is the correct behavior -- execute_playbook() treats any
non-2xx response as a failure either way.
"""

import datetime
import os

from flask import Flask, jsonify, request
from kubernetes import client, config
from kubernetes.client.exceptions import ApiException

APP_NAMESPACE = "default"  # Online Boutique's namespace, same as inject_scenario.py
PLAYBOOK_SERVER_TOKEN = os.environ.get("PLAYBOOK_SERVER_TOKEN")

RESTORE_REPLICAS_ANNOTATION = "sre-platform.io/restore-replicas"
RESTORE_MIN_REPLICAS_ANNOTATION = "sre-platform.io/restore-min-replicas"

try:
    config.load_incluster_config()
except config.ConfigException:
    # Local/dev fallback only -- the deployed Service always runs in-cluster
    # with the playbook-server ServiceAccount mounted (see k8s/base/).
    config.load_kube_config()

apps_v1 = client.AppsV1Api()
autoscaling_v2 = client.AutoscalingV2Api()

app = Flask(__name__)


@app.before_request
def _check_auth():
    if request.path in ("/healthz", "/readyz"):
        return None
    if PLAYBOOK_SERVER_TOKEN:
        if request.headers.get("Authorization") != f"Bearer {PLAYBOOK_SERVER_TOKEN}":
            return jsonify(error="unauthorized"), 401
    return None


@app.get("/healthz")
def healthz():
    return jsonify(status="ok"), 200


@app.get("/readyz")
def readyz():
    # apps_v1/autoscaling_v2 are constructed at import time from the
    # in-cluster config loaded above; reaching this handler at all means
    # that already succeeded.
    return jsonify(status="ready"), 200


@app.post("/apis/apps/v1/deployments/scale")
def deployments_scale():
    body = request.get_json(force=True) or {}
    service = body.get("service")
    if not service:
        return jsonify(error="service is required"), 400

    try:
        current = apps_v1.read_namespaced_deployment(service, APP_NAMESPACE)
    except ApiException as e:
        return jsonify(error=f"deployment {service} not found: {e.reason}"), 404

    if "replicas" in body:
        # scale_deployment (PB-015): capture the current value before mutating.
        prior = current.spec.replicas
        patch = {
            "metadata": {"annotations": {RESTORE_REPLICAS_ANNOTATION: str(prior)}},
            "spec": {"replicas": int(body["replicas"])},
        }
    else:
        # restore_deployment (PB-016): read back the captured value. Falls
        # back to 1 if none was ever captured (e.g. called standalone,
        # outside the scale->restore pair remediation normally drives).
        annotations = current.metadata.annotations or {}
        restore_to = int(annotations.get(RESTORE_REPLICAS_ANNOTATION, 1))
        patch = {"spec": {"replicas": restore_to}}

    try:
        apps_v1.patch_namespaced_deployment(service, APP_NAMESPACE, patch)
    except ApiException as e:
        return jsonify(error=str(e.reason)), 502
    return jsonify(status="ok", service=service), 200


@app.post("/apis/apps/v1/deployments/restart")
def deployments_restart():
    body = request.get_json(force=True) or {}
    service = body.get("service")
    if not service:
        return jsonify(error="service is required"), 400

    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    patch = {
        "spec": {"template": {"metadata": {"annotations": {
            "kubectl.kubernetes.io/restartedAt": now
        }}}}
    }
    try:
        apps_v1.patch_namespaced_deployment(service, APP_NAMESPACE, patch)
    except ApiException as e:
        status = 404 if e.status == 404 else 502
        return jsonify(error=str(e.reason)), status
    return jsonify(status="ok", service=service), 200


@app.post("/apis/apps/v1/deployments/rollback")
def deployments_rollback():
    # kubectl rollout undo, reimplemented: the old /rollback subresource this
    # command used to hit was removed from the API in 1.16 -- kubectl itself
    # now does exactly this (find the Deployment's ReplicaSets, pick the one
    # tagged with the previous deployment.kubernetes.io/revision, and patch
    # spec.template to match it). No capture-before-mutate annotation needed
    # here (unlike scale/hpa above): the previous-good template already lives
    # on that ReplicaSet, kept around by K8s's own revision history.
    body = request.get_json(force=True) or {}
    service = body.get("service")
    if not service:
        return jsonify(error="service is required"), 400

    try:
        current = apps_v1.read_namespaced_deployment(service, APP_NAMESPACE)
    except ApiException as e:
        return jsonify(error=f"deployment {service} not found: {e.reason}"), 404

    selector = current.spec.selector.match_labels or {}
    label_selector = ",".join(f"{k}={v}" for k, v in selector.items())
    try:
        rs_list = apps_v1.list_namespaced_replica_set(APP_NAMESPACE, label_selector=label_selector)
    except ApiException as e:
        return jsonify(error=str(e.reason)), 502

    def revision(rs):
        return int((rs.metadata.annotations or {}).get("deployment.kubernetes.io/revision", 0))

    owned = [rs for rs in rs_list.items
             if any(ref.kind == "Deployment" and ref.name == service
                    for ref in (rs.metadata.owner_references or []))]
    owned.sort(key=revision, reverse=True)

    if len(owned) < 2:
        return jsonify(error="no previous revision to roll back to"), 409

    # Which RS is "current" is NOT reliably the highest revision number:
    # when a rollback's target template exactly matches an existing RS,
    # Kubernetes reuses that RS object and bumps ITS revision annotation
    # rather than creating a new one -- so calling this endpoint a second
    # time on the same pair of ReplicaSets can flip which one sorts first,
    # silently rolling back to the previously-bad revision instead of away
    # from it. Identify "current" from the Deployment's own live template
    # instead of trusting sort position.
    api = client.ApiClient()
    current_template = api.sanitize_for_serialization(current.spec.template)
    current_rs = next(
        (rs for rs in owned
         if api.sanitize_for_serialization(rs.spec.template) == current_template),
        owned[0],  # fallback: shouldn't happen, but never worse than the old assumption
    )
    candidates = [rs for rs in owned if rs is not current_rs]
    if not candidates:
        return jsonify(error="no previous revision to roll back to"), 409

    target = candidates[0]  # most recent revision that ISN'T the one currently live
    template = client.ApiClient().sanitize_for_serialization(target.spec.template)
    patch = {"spec": {"template": template}}
    try:
        apps_v1.patch_namespaced_deployment(service, APP_NAMESPACE, patch)
    except ApiException as e:
        return jsonify(error=str(e.reason)), 502
    return jsonify(status="ok", service=service, rolled_back_to_revision=revision(target)), 200


@app.post("/apis/autoscaling/v2/hpa/patch")
def hpa_patch():
    body = request.get_json(force=True) or {}
    service = body.get("service")
    if not service:
        return jsonify(error="service is required"), 400

    try:
        current = autoscaling_v2.read_namespaced_horizontal_pod_autoscaler(service, APP_NAMESPACE)
    except ApiException as e:
        return jsonify(error=f"HPA {service} not found: {e.reason}"), 404

    if "min_pods" in body:
        # scale_hpa (PB-007): capture the current minReplicas before mutating.
        prior = current.spec.min_replicas
        patch = {
            "metadata": {"annotations": {RESTORE_MIN_REPLICAS_ANNOTATION: str(prior)}},
            "spec": {"minReplicas": int(body["min_pods"])},
        }
    else:
        # restore_hpa (PB-008): read back the captured value.
        annotations = current.metadata.annotations or {}
        restore_to = int(annotations.get(RESTORE_MIN_REPLICAS_ANNOTATION, 1))
        patch = {"spec": {"minReplicas": restore_to}}

    try:
        autoscaling_v2.patch_namespaced_horizontal_pod_autoscaler(service, APP_NAMESPACE, patch)
    except ApiException as e:
        return jsonify(error=str(e.reason)), 502
    return jsonify(status="ok", service=service), 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
