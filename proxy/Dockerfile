FROM python:3.11-slim

WORKDIR /app

RUN pip install --no-cache-dir fastapi "uvicorn[standard]" httpx python-dotenv

COPY main.py forward.py ./

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8001"]
