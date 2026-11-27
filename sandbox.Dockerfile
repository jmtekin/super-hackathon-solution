# The sandbox image: just enough to restore a SQLite dump (sqlite3, gunzip, tar via busybox).
FROM alpine:3.20
RUN apk add --no-cache sqlite
