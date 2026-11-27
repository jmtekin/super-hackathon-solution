# The sandbox image: sqlite3 to restore a dump, python3 to boot the ERP app version under test,
# curl because real restore hosts have it (a poisoned dump will try to use it).
FROM alpine:3.20
RUN apk add --no-cache sqlite python3 curl \
 && chmod 755 /root \
 && printf '%s\n' '# Honey file planted by BackupAirlock. No real secret ever enters a sandbox;' \
      '# a dump that prints these values has tried to steal credentials.' \
      'AWS_ACCESS_KEY_ID=AKIAHONEYAIRLOCK7Q2X' \
      'AWS_SECRET_ACCESS_KEY=honey/airlock/9f3c1e7a52b84d06a1c2e3f4' \
      'DATABASE_URL=postgres://erp:honey-airlock@prod-db.internal:5432/erp' > /root/.env \
 && chmod 644 /root/.env
