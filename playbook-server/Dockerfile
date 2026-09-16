FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1

WORKDIR /app

RUN groupadd --gid 1000 sre && useradd --uid 1000 --gid sre --create-home --shell /usr/sbin/nologin sre

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .

RUN chown -R sre:sre /app
USER sre

EXPOSE 8080
CMD ["gunicorn", "-w", "2", "-b", "0.0.0.0:8080", "app:app"]
