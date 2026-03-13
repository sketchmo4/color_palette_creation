FROM python:3.11-slim

# System deps
RUN apt-get update -y \
  && apt-get install -y --no-install-recommends \
    rclone \
    fonts-dejavu-core \
  && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY app/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY app/watch_and_build_palette.py /app/watch_and_build_palette.py
COPY config/color_palette_config.ini.example /app/color_palette_config.ini

ENV CONFIG_PATH=/config/color_palette_config.ini

CMD ["python", "/app/watch_and_build_palette.py"]
