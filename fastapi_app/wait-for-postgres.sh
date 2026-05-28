#!/bin/sh
# wait-for-postgres.sh

host="postgres"
port=5432
echo "Waiting for Postgres at ${host}:${port}..."

while ! nc -z $host $port; do   
  sleep 2
done

echo "Postgres is up - starting FastAPI."
exec "$@"