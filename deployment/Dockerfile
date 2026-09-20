# syntax=docker/dockerfile:1
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

RUN groupadd --gid 10001 app \
    && useradd --uid 10001 --gid app --create-home --shell /usr/sbin/nologin app \
    && mkdir -p /app/data \
    && chown 10001:10001 /app/data

COPY --chown=10001:10001 funda_agent_exp.py api_funda_agent_exp.py agent_instructions.py tool_prompts.py plsr.py clustering.py ./
COPY --chown=10001:10001 scripts ./scripts

USER 10001:10001

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=10s --start-period=40s --retries=5 \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=5).read()"]

CMD ["uvicorn", "api_funda_agent_exp:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
