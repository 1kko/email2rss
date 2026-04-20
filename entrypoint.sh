#!/bin/sh
# Entrypoint that conditionally wraps the command with opentelemetry-instrument.
# OTel is enabled only when OTEL_EXPORTER_OTLP_ENDPOINT is set — keeps the
# image usable for forks that don't run their own OTel collector.
set -e

mode="${1:-serve}"
case "$mode" in
  serve)
    cmd="gunicorn --bind 0.0.0.0:${port:-8000} --workers 1 --threads 4 --access-logfile - feed_server:app"
    ;;
  fetch)
    cmd="python -c \"from start import fetch_and_generate; fetch_and_generate()\""
    ;;
  *)
    echo "entrypoint: unknown mode '$mode' (expected: serve, fetch)" >&2
    exit 1
    ;;
esac

if [ -n "${OTEL_EXPORTER_OTLP_ENDPOINT:-}" ]; then
  echo "entrypoint: OTel enabled — endpoint=${OTEL_EXPORTER_OTLP_ENDPOINT}"
  exec sh -c "opentelemetry-instrument $cmd"
else
  echo "entrypoint: OTel disabled (set OTEL_EXPORTER_OTLP_ENDPOINT to enable)"
  exec sh -c "$cmd"
fi
