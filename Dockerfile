FROM python:3.13-slim
ARG JOURNALMAX_VERSION=dev
ARG VCS_REF=unknown
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
ENV JOURNALMAX_VERSION=${JOURNALMAX_VERSION}
LABEL org.opencontainers.image.title="JOURNALMAX" \
      org.opencontainers.image.version="${JOURNALMAX_VERSION}" \
      org.opencontainers.image.revision="${VCS_REF}" \
      org.opencontainers.image.source="https://github.com/murmuur22/journal-maxx"
RUN groupadd --gid 10001 diary && useradd --uid 10001 --gid diary --home-dir /app --no-create-home --shell /usr/sbin/nologin diary
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt gunicorn
COPY . .
RUN mkdir -p /var/lib/diary-state /data/cards && chown -R diary:diary /app /var/lib/diary-state /data/cards
USER diary
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health/ready')"
CMD ["sh", "-c", "python manage.py migrate --noinput && python manage.py collectstatic --noinput && gunicorn diary.wsgi:application --bind 0.0.0.0:8000 --workers 2 --timeout 60"]
