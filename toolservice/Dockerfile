FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends ripgrep \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1

WORKDIR /app

COPY tool_service.py /app/tool_service.py

EXPOSE 8020

CMD ["python", "/app/tool_service.py"]
