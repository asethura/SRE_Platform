FROM python:3.12-slim

WORKDIR /app

RUN groupadd --gid 1000 sre && useradd --uid 1000 --gid sre --create-home --shell /usr/sbin/nologin sre

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN chown -R sre:sre /app
USER sre

# One image for all four agent types — Kubernetes Deployments pick the type
# via `args: ["triage"|"diagnosis"|"remediation"|"validation"]` (see k8s/base).
ENTRYPOINT ["python", "run_agent.py"]
CMD ["triage"]
