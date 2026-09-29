FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1
ENV PIXELCUT_HEADLESS=true

WORKDIR /app

COPY requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt \
    && playwright install --with-deps chromium

COPY vinted_monitor.py rimuovi_sfondo.py ./

CMD ["python", "vinted_monitor.py"]
