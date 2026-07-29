FROM python:3.11-slim

# Instala o FFmpeg, curl e garante a presença e atualização dos certificados raiz CA do sistema
RUN apt-get update && apt-get install -y \
    ffmpeg \
    curl \
    ca-certificates \
    && update-ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 5000

CMD ["gunicorn", "--bind", "0.0.0.0:5000", "app:app", "--timeout", "120"]