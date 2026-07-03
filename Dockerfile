FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY scraper.py uploader.py main.py ./
COPY docs/ ./docs/

CMD ["python", "main.py"]