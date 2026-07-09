FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1
ENV TZ=Europe/Madrid

WORKDIR /app

# System deps for lxml + timezone
RUN apt-get update && apt-get install -y --no-install-recommends \
    tzdata \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Persistent data dir (mount a Railway volume here)
RUN mkdir -p /app/data

# Run the scheduler: scrape + digest + feedback poll
CMD ["python", "main.py", "scrape"]
