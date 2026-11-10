FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1

WORKDIR /app

COPY agent_gateway.py /app/agent_gateway.py

# 8000: API listener (published by compose). 8010: internal listener for Open WebUI.
EXPOSE 8000 8010

CMD ["python", "/app/agent_gateway.py"]
