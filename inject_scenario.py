"""
Fault injector — introduce a REAL fault on the Online Boutique app (same GKE
cluster as sre-platform, namespace "default") so the already-running GCP
agents have something genuine to find. This script does nothing else: no DB
writes, no agent/LLM calls, no ticket filing — file the matching Jira
Service Management incident yourself afterward so sre-triage picks it up.

Four distinct, independently real fault mechanisms:

  kill        Delete a running pod. Self-healing -- the Deployment
              recreates it immediately. Any service.
  scale-zero  Scale a deployment to 0 replicas -- a real outage that does
              NOT self-heal. Any service. Undo with: restore <service> scale
  bad-deploy  Patch the container image to a nonexistent tag
              (ImagePullBackOff/CrashLoopBackOff) -- does NOT self-heal.
              Any service. Undo with: restore <service> deploy
  cpu-stress  Exec a bounded CPU-burn loop inside the pod. paymentservice
              only -- it's the one Online Boutique image with a shell and a
              runtime (Node) available; the others are shell-less compiled
              binaries you can't exec anything else into. Self-ending after
              --duration seconds.

Usage:
    python inject_scenario.py --list
    python inject_scenario.py kill checkoutservice
    python inject_scenario.py scale-zero cartservice
    python inject_scenario.py bad-deploy productcatalogservice
    python inject_scenario.py cpu-stress --duration 45
    python inject_scenario.py restore cartservice scale
    python inject_scenario.py restore productcatalogservice deploy

Requires kubectl pointed at the online-boutique cluster:
    gcloud container clusters get-credentials online-boutique \\
        --project project-40306309-d32d-4628-9f6 --region us-central1

Note: RemediationAgent.execute_playbook() is currently stubbed (always
"succeeds" without calling the Kubernetes API), so scale-zero and bad-deploy
faults will NOT be auto-fixed by the pipeline yet -- restore them yourself
with the `restore` subcommand.
"""

import argparse
import subprocess

APP_NAMESPACE = "default"  # Online Boutique's namespace on the shared cluster
CONTAINER_NAME = "server"  # container name inside every Online Boutique pod
CPU_STRESS_SERVICE = "paymentservice"  # only service with a shell + runtime
BAD_IMAGE = "gcr.io/google-samples/microservices-demo/does-not-exist:broken"

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

def fault_kill(service: str):
    _check_service(service)
    pod = _pod_name(service)
    _run(["kubectl", "delete", "pod", pod, "-n", APP_NAMESPACE, "--wait=false"])
    print(f"  deleted {pod} -- deployment will recreate it (self-healing)")
    print(f"  symptom: {SERVICES[service]}")


def fault_scale_zero(service: str):
    _check_service(service)
    _run(["kubectl", "scale", f"deployment/{service}", "-n", APP_NAMESPACE, "--replicas=0"])
    print(f"  scaled {service} to 0 replicas -- stays down until restored")
    print(f"  symptom: {service} is fully unavailable")
    print(f"  undo with: python inject_scenario.py restore {service} scale")


def fault_bad_deploy(service: str):
    _check_service(service)
    _run(["kubectl", "set", "image", f"deployment/{service}",
          f"{CONTAINER_NAME}={BAD_IMAGE}", "-n", APP_NAMESPACE])
    print(f"  patched {service}'s image to a nonexistent tag")
    print("  symptom: new pod stuck in ImagePullBackOff, old pod terminating -- "
          "service degrades until restored")
    print(f"  undo with: python inject_scenario.py restore {service} deploy")


def fault_cpu_stress(duration: int):
    pod = _pod_name(CPU_STRESS_SERVICE)
    print(f"  burning CPU inside {pod} for {duration}s (blocks here until done)...")
    _run(["kubectl", "exec", "-n", APP_NAMESPACE, pod, "--",
          "node", "-e", f"const end=Date.now()+{duration}*1000; while(Date.now()<end){{}}"])
    print("  done -- CPU pressure released")
    print(f"  symptom: {SERVICES[CPU_STRESS_SERVICE]}")


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

    p_kill = sub.add_parser("kill", help="Delete a pod (self-healing)")
    p_kill.add_argument("service")

    p_scale = sub.add_parser("scale-zero", help="Scale a deployment to 0 replicas")
    p_scale.add_argument("service")

    p_deploy = sub.add_parser("bad-deploy", help="Patch a deployment to a broken image")
    p_deploy.add_argument("service")

    p_cpu = sub.add_parser("cpu-stress", help=f"Burn CPU inside {CPU_STRESS_SERVICE}")
    p_cpu.add_argument("--duration", type=int, default=45,
                        help="Seconds to burn CPU for (default 45)")

    p_restore = sub.add_parser("restore", help="Undo scale-zero or bad-deploy")
    p_restore.add_argument("service")
    p_restore.add_argument("fault", choices=["scale", "deploy"])

    args = parser.parse_args()

    if args.list or not args.command:
        _print_services()
        if not args.command:
            print("\nUsage: python inject_scenario.py <kill|scale-zero|bad-deploy|"
                  "cpu-stress|restore> ...  (--help for details)")
        return

    print("=" * 70)
    print(f"FAULT: {args.command}")

    if args.command == "kill":
        fault_kill(args.service)
    elif args.command == "scale-zero":
        fault_scale_zero(args.service)
    elif args.command == "bad-deploy":
        fault_bad_deploy(args.service)
    elif args.command == "cpu-stress":
        fault_cpu_stress(args.duration)
    elif args.command == "restore":
        if args.fault == "scale":
            restore_scale(args.service)
        else:
            restore_deploy(args.service)


if __name__ == "__main__":
    main()
