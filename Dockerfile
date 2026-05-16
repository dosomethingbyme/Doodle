FROM python:3.14-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOST=0.0.0.0 \
    PORT=8000 \
    BOOKING_DB_PATH=/data/bookings.sqlite3 \
    ADMIN_PASSWORD=aiad-admin-2026 \
    SMTP_HOST=your-smtp-host \
    SMTP_PORT=465 \
    SMTP_USER=your-smtp-login \
    SMTP_USE_SSL=1

WORKDIR /app

COPY index.html admin.html server.py ./

RUN useradd --create-home --shell /usr/sbin/nologin appuser \
  && mkdir -p /data \
  && chown -R appuser:appuser /app /data

USER appuser

EXPOSE 8000

CMD ["python", "server.py"]
