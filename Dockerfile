FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1
ENV REMBG_MODEL=u2netp

WORKDIR /app

# Dipendenze di sistema per onnxruntime / pillow
RUN apt-get update \
    && apt-get install -y --no-install-recommends libgl1 libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt \
    && python -c "from rembg import new_session; new_session('u2netp')"

COPY vinted_monitor.py rimuovi_sfondo.py ./

CMD ["python", "vinted_monitor.py"]
