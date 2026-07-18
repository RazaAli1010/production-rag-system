"""F13 observability: request_logs writes, structlog config, request metrics, admin stats.

Deliberately empty of re-exports: `core.middleware` imports `observability.metrics`, and
`observability.request_log` imports `core.middleware` — eager re-exports here would run that cycle at
package-init time. Callers import the submodule they need directly.
"""
