FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1

WORKDIR /app

COPY agent_gateway.py /app/agent_gateway.py

EXPOSE 8010

CMD ["python", "/app/agent_gateway.py"]
