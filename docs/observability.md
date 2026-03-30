# Observability

CUA includes built-in OpenTelemetry instrumentation for distributed tracing, metrics, and structured logging.

## Traces

Every session produces a single trace linking the outer API request to every agent step inside the sandbox:

```text
cua.session                          -> API request lifecycle
  cua.sandbox.create                 -> Modal sandbox creation
  cua.agent.run                      -> Full agent run
    cua.agent.setup                  -> Browser launch + blinders init
    cua.agent.iteration [xN]         -> One per loop iteration
      cua.llm.call                   -> Claude API call
      cua.tool.execute [xM]          -> Each browser action
        cua.guardrail.check          -> Safety verification
        cua.browser.action           -> Patchright execution
    cua.recording.start              -> Recording initialization
    cua.recording.stop               -> Recording finalization
```

Spans include GenAI semantic convention attributes (`gen_ai.usage.input_tokens`, `gen_ai.usage.output_tokens`), action details, guardrail decisions, and timing.

## Configuration

| Env Var | Default | Description |
|---|---|---|
| `OTEL_SDK_DISABLED` | `true` | Set to `false` to enable tracing and metrics |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | `http://localhost:4317` | OTLP gRPC collector endpoint |
| `OTEL_EXPORTER_OTLP_INSECURE` | `false` | Set to `true` for insecure local collector |
| `OTEL_RESOURCE_ENV` | `local` | Deployment environment label |
| `OTEL_TRACES_SAMPLER_ARG` | `1.0` | Sampling rate (0.0-1.0) |

## Quick Start with Jaeger

```bash
docker run -d --name jaeger -p 16686:16686 -p 4317:4317 jaegertracing/all-in-one
OTEL_SDK_DISABLED=false python scripts/run_local.py --directive "..."
# Open http://localhost:16686 to see traces
```
