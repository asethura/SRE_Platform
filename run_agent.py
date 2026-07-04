"""
Run a single agent as an independent polling worker — the deployment model.

Each agent type runs as its own process (or pool of processes); they
coordinate only through the shared session DB. Start one per terminal:

    python run_agent.py triage
    python run_agent.py diagnosis
    python run_agent.py remediation
    python run_agent.py validation

Seed the DB once first (python main.py does it, or: python -c
"from db.models import get_engine, init_db; from db.seed import seed;
seed(init_db(get_engine()))").

Intake source for triage is configurable via SRE_ITSM_CLIENT (stub|null) —
"stub" replays the same fixed demo tickets forever, which is fine locally
but NOT what you want running continuously in a cluster with no real ITSM
integration wired up yet. Defaults to "stub" to keep existing local/demo
behavior unchanged; k8s/base/configmap.yaml sets it to "null" instead.
"""

import os
import sys

from dotenv import load_dotenv

from db.models import get_engine, init_db
from integrations.itsm import NullITSMClient, StubITSMClient
from agents.triage import TriageAgent
from agents.diagnosis import DiagnosisAgent
from agents.remediation import RemediationAgent
from agents.validation import ValidationAgent

AGENTS = {
    "triage": TriageAgent,
    "diagnosis": DiagnosisAgent,
    "remediation": RemediationAgent,
    "validation": ValidationAgent,
}

ITSM_CLIENTS = {
    "stub": StubITSMClient,
    "null": NullITSMClient,
}


def main():
    if len(sys.argv) != 2 or sys.argv[1] not in AGENTS:
        print(f"Usage: python run_agent.py [{'|'.join(AGENTS)}]")
        sys.exit(1)

    load_dotenv()
    session_factory = init_db(get_engine(os.environ.get("DATABASE_URL", "sqlite:///sre_platform.db")))
    agent_cls = AGENTS[sys.argv[1]]

    kwargs = {}
    if agent_cls is TriageAgent:
        itsm_name = os.environ.get("SRE_ITSM_CLIENT", "stub")
        if itsm_name not in ITSM_CLIENTS:
            print(f"Unknown SRE_ITSM_CLIENT={itsm_name!r}; expected one of {list(ITSM_CLIENTS)}")
            sys.exit(1)
        kwargs["itsm_client"] = ITSM_CLIENTS[itsm_name]()

    agent = agent_cls(session_factory, **kwargs)
    agent.run_forever()


if __name__ == "__main__":
    main()
