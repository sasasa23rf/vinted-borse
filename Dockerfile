FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY vinted_monitor.py ./vinted_monitor.py

CMD ["python", "vinted_monitor.py"]