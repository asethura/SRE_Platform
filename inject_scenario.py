"""
Fault injector — introduce a REAL fault on the Online Boutique app (same GKE
cluster as sre-platform, namespace "default") so the already-running GCP
agents have something genuine to find. This script does nothing else: no DB
writes, no agent/LLM calls, no ticket filing — file the matching Jira
Service Management incident yourself afterward so sre-triage picks it up.

Five distinct, independently real fault mechanisms — all non-self-healing
(or long enough to matter), so the pipeline's remediation has to actually do
something to resolve them:

  scale-zero  Scale a deployment to 0 replicas -- a real outage that does
              NOT self-heal. Any service. Undo with: restore <service> scale
  bad-deploy  Patch the container image to a nonexistent tag
              (ImagePullBackOff/CrashLoopBackOff) -- does NOT self-heal.
              Any service. Undo with: restore <service> deploy
  redis-down  Scale redis-cart to 0 -- a dependency-failure variant of
              scale-zero: cartservice depends on redis-cart (see
              config/service_graph.yaml), so cart reads/writes start
              erroring even though cartservice itself never changed.
              Exercises the service graph's dependency reasoning, not
              just single-service symptoms. Undo with: restore redis-cart scale
  cpu-stress  Exec a bounded CPU-burn loop inside the pod. paymentservice
              only -- it's the one Online Boutique image with a shell and a
              runtime (Node) available; the others are shell-less compiled
              binaries you can't exec anything else into. Self-ending after
              --duration seconds.
  bad-config  Repoint checkoutservice's PAYMENT_SERVICE_ADDR at
              shippingservice:50051 instead of the real paymentservice:50051
              -- a pure functional/config bug, unlike the four above. Every
              pod involved stays Running/Ready (no crash, no restart loop,
              no image problem, no resource pressure) since nothing here
              touches replica counts, images, or CPU -- only checkoutservice
              actually calling PaymentService.Charge on a server that only
              implements ShippingService, which fails every checkout with a
              gRPC UNIMPLEMENTED error. Nothing about the symptom shows up
              as a resource/replica/image signal, only as checkoutservice's
              own request-path error rate -- diagnosis has to notice "every
              downstream service reports healthy" doesn't mean the wiring
              between them is still correct. `kubectl set env` creates a new
              ReplicaSet revision just like bad-deploy's `set image`, so it's
              fixable the same way. Undo with: restore checkoutservice deploy

Usage:
    python inject_scenario.py --list
    python inject_scenario.py scale-zero cartservice
    python inject_scenario.py bad-deploy productcatalogservice
    python inject_scenario.py redis-down
    python inject_scenario.py cpu-stress --duration 45
    python inject_scenario.py bad-config
    python inject_scenario.py restore cartservice scale
    python inject_scenario.py restore productcatalogservice deploy
    python inject_scenario.py restore redis-cart scale
    python inject_scenario.py restore checkoutservice deploy

Requires kubectl pointed at the online-boutique cluster:
    gcloud container clusters get-credentials online-boutique \\
        --project project-40306309-d32d-4628-9f6 --region us-central1

Note: RemediationAgent.execute_playbook() POSTs to playbook-server
(k8s/base/deployment-playbook-server.yaml), which genuinely calls the
Kubernetes API -- scale_hpa/scale_deployment/rolling_restart really mutate
the cluster, with capture-before-mutate restore semantics (see
playbook-server/app.py). So scale-zero and bad-deploy CAN be auto-fixed by
the pipeline now; the `restore` subcommand here is just a manual fallback
if you'd rather not wait on/trust the pipeline.
"""

import argparse
import subprocess

APP_NAMESPACE = "default"  # Online Boutique's namespace on the shared cluster
# Container name inside each Online Boutique pod -- "server" for every
# service except redis-cart, which runs the stock redis:alpine image.
DEFAULT_CONTAINER_NAME = "server"
CONTAINER_NAMES = {"redis-cart": "redis"}
CPU_STRESS_SERVICE = "paymentservice"  # only service with a shell + runtime
BAD_IMAGE = "gcr.io/google-samples/microservices-demo/does-not-exist:broken"
BAD_CONFIG_TARGET = "checkoutservice"
BAD_CONFIG_ENV_VAR = "PAYMENT_SERVICE_ADDR"
BAD_CONFIG_REAL_VALUE = "paymentservice:50051"
BAD_CONFIG_WRONG_VALUE = "shippingservice:50051"  # live, healthy, wrong service

# Real Online Boutique deployments (kubectl get deployments -n default) and a
# one-line hint of the symptom each fault produces, for whoever files the
# Jira ticket by hand afterward -- not used by this script otherwise.
SERVICES = {
    "adservice": "ads may fail to load intermittently until the new pod is ready",
    "cartservice": "add-to-cart / view-cart requests may fail intermittently",
    "checkoutservice": "checkout may be unavailable until the new pod is ready",
    "currencyservice": "currency conversion may fail intermittently",
    "emailservice": "order confirmation emails may be delayed or dropped",
    "frontend": "the storefront may be briefly unavailable",
    "paymentservice": "checkout payment step may fail with connection errors",
    "productcatalogservice": "product listing/detail pages may fail intermittently",
    "recommendationservice": "product recommendations may fail intermittently",
    "redis-cart": "cart reads/writes may fail until the new pod is ready",
    "shippingservice": "shipping cost/quote calls may fail intermittently",
}


def _run(cmd: list[str]):
    subprocess.run(cmd, check=True)


def _pod_name(service: str) -> str:
    pod = subprocess.run(
        ["kubectl", "get", "pod", "-n", APP_NAMESPACE, "-l", f"app={service}",
         "-o", "jsonpath={.items[0].metadata.name}"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    if not pod:
        raise SystemExit(f"No running pod found for app={service} in {APP_NAMESPACE}")
    return pod


def _check_service(service: str):
    if service not in SERVICES:
        raise SystemExit(f"Unknown service '{service}'. Choices: {', '.join(SERVICES)}")


# ---------------------------------------------------------------------- #
# Faults
# ---------------------------------------------------------------------- #

def fault_scale_zero(service: str):
    _check_service(service)
    _run(["kubectl", "scale", f"deployment/{service}", "-n", APP_NAMESPACE, "--replicas=0"])
    print(f"  scaled {service} to 0 replicas -- stays down until restored")
    print(f"  symptom: {service} is fully unavailable")
    print(f"  undo with: python inject_scenario.py restore {service} scale")


def fault_bad_deploy(service: str):
    _check_service(service)
    container = CONTAINER_NAMES.get(service, DEFAULT_CONTAINER_NAME)
    _run(["kubectl", "set", "image", f"deployment/{service}",
          f"{container}={BAD_IMAGE}", "-n", APP_NAMESPACE])
    print(f"  patched {service}'s image to a nonexistent tag")
    print("  symptom: new pod stuck in ImagePullBackOff, old pod terminating -- "
          "service degrades until restored")
    print(f"  undo with: python inject_scenario.py restore {service} deploy")


def fault_redis_down():
    fault_scale_zero("redis-cart")
    print("  cartservice depends on redis-cart (config/service_graph.yaml) -- "
          "add-to-cart/view-cart requests should start failing even though "
          "cartservice itself was never touched")


def fault_cpu_stress(duration: int):
    pod = _pod_name(CPU_STRESS_SERVICE)
    print(f"  burning CPU inside {pod} for {duration}s (blocks here until done)...")
    _run(["kubectl", "exec", "-n", APP_NAMESPACE, pod, "--",
          "node", "-e", f"const end=Date.now()+{duration}*1000; while(Date.now()<end){{}}"])
    print("  done -- CPU pressure released")
    print(f"  symptom: {SERVICES[CPU_STRESS_SERVICE]}")


def fault_bad_config():
    _run(["kubectl", "set", "env", f"deployment/{BAD_CONFIG_TARGET}", "-n", APP_NAMESPACE,
          f"{BAD_CONFIG_ENV_VAR}={BAD_CONFIG_WRONG_VALUE}"])
    print(f"  repointed {BAD_CONFIG_TARGET}'s {BAD_CONFIG_ENV_VAR} from "
          f"{BAD_CONFIG_REAL_VALUE} to {BAD_CONFIG_WRONG_VALUE} (a real, "
          "healthy, but WRONG service)")
    print("  symptom: every pod involved stays Running/Ready -- no crash, no "
          "restart loop, no image problem, no CPU/latency spike. Every "
          "checkout fails at the payment step (gRPC UNIMPLEMENTED: "
          "shippingservice doesn't speak the PaymentService interface). A "
          "functional/config bug, not an infra fault -- only checkoutservice's "
          "own error rate shows anything wrong.")
    print(f"  undo with: python inject_scenario.py restore {BAD_CONFIG_TARGET} deploy")


# ---------------------------------------------------------------------- #
# Restore (scale-zero and bad-deploy don't self-heal)
# ---------------------------------------------------------------------- #

def restore_scale(service: str):
    _check_service(service)
    _run(["kubectl", "scale", f"deployment/{service}", "-n", APP_NAMESPACE, "--replicas=1"])
    print(f"  scaled {service} back to 1 replica")


def restore_deploy(service: str):
    _check_service(service)
    _run(["kubectl", "rollout", "undo", f"deployment/{service}", "-n", APP_NAMESPACE])
    print(f"  rolled {service} back to its previous working image")


def _print_services():
    print("Available services:")
    for name, symptom in SERVICES.items():
        print(f"  {name:24s} {symptom}")
    print(f"\ncpu-stress only runs against: {CPU_STRESS_SERVICE}")


def main():
    parser = argparse.ArgumentParser(
        description="Inject a real fault onto the Online Boutique app."
    )
    parser.add_argument("--list", action="store_true",
                         help="List available services and exit")
    sub = parser.add_subparsers(dest="command")

    p_scale = sub.add_parser("scale-zero", help="Scale a deployment to 0 replicas")
    p_scale.add_argument("service")

    p_deploy = sub.add_parser("bad-deploy", help="Patch a deployment to a broken image")
    p_deploy.add_argument("service")

    sub.add_parser("redis-down", help="Scale redis-cart to 0 -- cartservice depends on it")

    p_cpu = sub.add_parser("cpu-stress", help=f"Burn CPU inside {CPU_STRESS_SERVICE}")
    p_cpu.add_argument("--duration", type=int, default=45,
                        help="Seconds to burn CPU for (default 45)")

    sub.add_parser("bad-config", help=f"Repoint {BAD_CONFIG_TARGET}'s {BAD_CONFIG_ENV_VAR} "
                                       "at a live-but-wrong service (functional bug, no crash)")

    p_restore = sub.add_parser("restore", help="Undo scale-zero, bad-deploy, redis-down, or bad-config")
    p_restore.add_argument("service")
    p_restore.add_argument("fault", choices=["scale", "deploy"])

    args = parser.parse_args()

    if args.list or not args.command:
        _print_services()
        if not args.command:
            print("\nUsage: python inject_scenario.py <scale-zero|bad-deploy|"
                  "redis-down|cpu-stress|bad-config|restore> ...  (--help for details)")
        return

    print("=" * 70)
    print(f"FAULT: {args.command}")

    if args.command == "scale-zero":
        fault_scale_zero(args.service)
    elif args.command == "bad-deploy":
        fault_bad_deploy(args.service)
    elif args.command == "redis-down":
        fault_redis_down()
    elif args.command == "cpu-stress":
        fault_cpu_stress(args.duration)
    elif args.command == "bad-config":
        fault_bad_config()
    elif args.command == "restore":
        if args.fault == "scale":
            restore_scale(args.service)
        else:
            restore_deploy(args.service)


if __name__ == "__main__":
    main()
