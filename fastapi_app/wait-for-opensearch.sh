#!/bin/sh
# wait-for-opensearch.sh

host="opensearch"
port=9200

echo "Waiting for OpenSearch at ${host}:${port}..."

# Wait until the TCP port is open (ignoring SSL negotiation)
while ! nc -z $host $port; do   
  sleep 2
done

echo "OpenSearch TCP port is open. Waiting 10 seconds for cluster initialization..."
# Give OpenSearch a few seconds to finish its Java initialization after the port opens
sleep 10

echo "OpenSearch is Ready! Starting FastAPI..."
exec "$@"