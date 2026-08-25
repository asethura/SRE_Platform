# Build context is the REPO ROOT (not this directory), like playbook-mcp/
# Dockerfile, since this service needs `COPY db/ /app/db/` and `COPY hitl.py`
# to reuse db.models and hitl.approve()/reject() directly rather than
# reimplementing approval logic.
#
# Build from repo root:  docker build -f api/Dockerfile -t sre-api .

FROM node:20-alpine AS ui-build
WORKDIR /ui
COPY ui/package.json ./
RUN npm install
COPY ui/ ./
RUN npm run build

FROM python:3.12-slim
RUN groupadd -g 1000 sre && useradd -u 1000 -g sre -m sre
WORKDIR /app

COPY api/requirements.txt ./api/requirements.txt
RUN pip install --no-cache-dir -r api/requirements.txt

COPY db/ ./db/
COPY hitl.py ./
COPY api/ ./api/
COPY --from=ui-build /ui/dist ./ui/dist

RUN chown -R sre:sre /app
USER sre

EXPOSE 8000
CMD ["uvicorn", "api.app:app", "--host", "0.0.0.0", "--port", "8000"]
