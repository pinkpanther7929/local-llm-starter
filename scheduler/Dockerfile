FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1

WORKDIR /app

COPY llm_scheduler.py /app/llm_scheduler.py

EXPOSE 8000

CMD ["python", "/app/llm_scheduler.py"]
