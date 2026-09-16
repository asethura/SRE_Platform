"""
Semantic layer v1 — loads config/service_graph.yaml: the Online Boutique
service dependency graph, one canonical metric-query template per named
signal, and a ticket-language -> signal hint table.

Static and hand-authored (see the file's own header for what that means and
doesn't mean). Read by DiagnosisAgent and ValidationAgent via build_context()
so both reason against the same vocabulary instead of each inventing or
duplicating metric names inline in prompt text.
"""

import functools
from pathlib import Path

import yaml

_GRAPH_PATH = Path(__file__).resolve().parent.parent / "config" / "service_graph.yaml"


@functools.lru_cache(maxsize=1)
def load_service_graph() -> dict:
    """Cached for the life of the process — this file only changes on a
    deploy, not per incident, so there's no reason to re-read/re-parse it
    on every agent poll."""
    with open(_GRAPH_PATH, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def known_services() -> list[str]:
    return list(load_service_graph()["services"].keys())


def neighbors(service: str) -> list[str]:
    """Direct dependency names of `service`, or [] if it's not in the graph
    (e.g. the incident's service field is a Jira project key, not a real
    deployment name — the caller/agent still has to resolve that itself).
    Drops each edge's `criticality` — callers that need it should read
    `services[name].depends_on` from load_service_graph() directly."""
    deps = load_service_graph()["services"].get(service, {}).get("depends_on", [])
    return [d["service"] for d in deps]


def hinted_metric(description: str) -> str | None:
    """Best-effort keyword match against symptom_to_metric, case-insensitive
    substring — a hint for which signal to check first, not a resolution the
    agent should trust blindly."""
    text = description.lower()
    for keyword, metric in load_service_graph()["symptom_to_metric"].items():
        if keyword in text:
            return metric
    return None
