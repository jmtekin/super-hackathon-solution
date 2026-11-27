# STAND-IN Nordvik Handel app. One image per app version: SCHEMA drops the
# migrations a version does not know yet (app 1.0.0 = schema 1, 2.0.0 = schema 2).
FROM python:3.12-slim-bookworm
RUN pip install --no-cache-dir fastapi==0.115.6 uvicorn==0.34.0 "psycopg[binary]==3.2.3" bcrypt==4.2.1
ARG APP_VERSION=1.0.0
ARG SCHEMA=1
WORKDIR /app
COPY app.py traffic.py ./
COPY migrations/ migrations/
RUN for f in migrations/*.sql; do n=$(basename "$f" | cut -c1-3); [ "$n" -le "$SCHEMA" ] || rm "$f"; done
ENV APP_VERSION=$APP_VERSION PYTHONUNBUFFERED=1
USER 65534
EXPOSE 8000
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
