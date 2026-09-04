# ADR 0003: Runtime Executor Contract

**Status:** Accepted

## Context

The same control plane supports a simulated executor, local Docker, and Kubernetes. Embedding mode-specific operations throughout the worker would couple lifecycle logic to command details and make failure handling inconsistent.

## Decision

Runtime implementations conform to one executor contract for deploy, stop, health, logs, diagnostics, and reconciliation capabilities. The worker owns the common build and lifecycle pipeline; executors own runtime-specific side effects and metadata.

Kubernetes manifest and Helm deployment modes remain implementations behind the Kubernetes boundary rather than separate control-plane lifecycles.

## Consequences

- Lifecycle and event semantics remain comparable across runtime modes.
- Mode-specific prerequisites and capabilities can be reported explicitly.
- Executors can be tested with shared contracts and injected fakes.
- The common interface limits features that exist in only one runtime.
- Adding a runtime requires both command implementation and honest capability reporting; an executor cannot silently skip a required contract step.
