# Synthetic scenario sources

The traces and rules are fictional fixtures derived from ToolCall Replay's versioned
`examples/synthetic-suite.json` demonstration. They contain no real person, organization or request.
The safe candidate is byte-identical to the baseline. The risky candidate is byte-identical to the
versioned faulty candidate in `examples/traces/`; it changes only synthetic scope, tool and approval
behavior to exercise the three documented rule failures.
