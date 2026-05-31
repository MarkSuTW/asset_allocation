# Financial-Grade System Blueprint

## Target Structure

- app/core: configuration, logging, risk controls, and shared guards.
- app/models: request/response models and domain entities.
- app/repositories: SQL persistence access and query modules.
- app/services: market data, holdings engine, dividend engine, and audit flow.
- app/api: API routers and transport adapters.
- tests: integration, regression, and data-quality checks.

## Finance Controls Added in Current Version

- Audit trail table and APIs for operational traceability.
- Multi-source quote retrieval with deterministic fallback order.
- Missing-price list and manual remediation endpoint.
- Dividend auto-sync detail reporting with per-symbol provenance.

## Migration Principle

The current `main.py` remains the runtime entrypoint for backward compatibility.
New features should be added through layered modules first, then backported into routers.
