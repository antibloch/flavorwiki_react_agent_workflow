# Worked example: tiny benchmark agent

Read this only when creating a first map or resolving ambiguity about composition or semantic evidence. The example is intentionally small.

## Source

```python
# mini_agent.py
SYSTEM_RULE = "Compare only compatible measures against the approved benchmark."
TOOL_DESCRIPTION = "Load the approved benchmark survey packet."
RESULT_GUIDANCE = "Use the returned packet; do not query the same aggregates again."

def build_system_prompt():
    return f"Analyst rules:\n{SYSTEM_RULE}"

compare_to_benchmark.__doc__ = TOOL_DESCRIPTION
model_with_tools = model.bind_tools([compare_to_benchmark])
```

The system rule and tool description explicitly mention benchmarking. `RESULT_GUIDANCE` does not; its benchmarking facet is inferred from the proven `compare_to_benchmark` consumer.

## Complete compact JSON

```json
{
  "concepts": [
    {
      "id": "concept:benchmarking",
      "summary": "Comparison against an approved benchmark or baseline.",
      "aliases": ["baseline comparison", "benchmark", "benchmarks"],
      "facet_labels": ["benchmarking"],
      "workflow_ids": ["workflow:survey-benchmarking"]
    }
  ],
  "workflows": [
    {
      "id": "workflow:survey-benchmarking",
      "summary": "Loads an approved benchmark packet and applies compatible comparison guidance.",
      "entry_facets": ["benchmarking"],
      "related_facets": []
    }
  ],
  "instructions": [
    {
      "id": "instruction:mini_agent.RESULT_GUIDANCE",
      "kind": "tool_feedback",
      "source": {"path": "mini_agent.py", "symbol": "RESULT_GUIDANCE", "start_line": 4, "end_line": 4},
      "excerpt": "Use the returned packet; do not query the same aggregates again.",
      "definition_sha256": "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc",
      "used_by": ["mini_agent.compare_to_benchmark"],
      "delivered_via": "tool_message",
      "condition": "Returned after the benchmark packet tool succeeds.",
      "semantic": {
        "summary": "Tool-result guidance for reusing an approved benchmark packet.",
        "facets": [
          {
            "label": "benchmarking",
            "confidence": "inferred",
            "basis": ["used_by"],
            "refs": ["mini_agent.compare_to_benchmark"]
          }
        ],
        "workflow_ids": ["workflow:survey-benchmarking"]
      }
    },
    {
      "id": "instruction:mini_agent.SYSTEM_PROMPT",
      "kind": "composed_system",
      "source": {"path": "mini_agent.py", "symbol": "build_system_prompt", "start_line": 6, "end_line": 7},
      "excerpt": "System prompt composed from the analyst rule.",
      "definition_sha256": "dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd",
      "used_by": ["mini_agent.call_model"],
      "delivered_via": "system_message",
      "condition": "Present on every model call.",
      "composed_from": ["instruction:mini_agent.SYSTEM_RULE"]
    },
    {
      "id": "instruction:mini_agent.SYSTEM_RULE",
      "kind": "system_fragment",
      "source": {"path": "mini_agent.py", "symbol": "SYSTEM_RULE", "start_line": 2, "end_line": 2},
      "excerpt": "Compare only compatible measures against the approved benchmark.",
      "definition_sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
      "used_by": ["mini_agent.build_system_prompt"],
      "delivered_via": "system_message",
      "condition": "Included by the composed system prompt.",
      "semantic": {
        "summary": "System rule requiring compatible measures for benchmark comparisons.",
        "facets": [
          {"label": "benchmarking", "confidence": "explicit", "basis": ["source_text"]}
        ],
        "workflow_ids": ["workflow:survey-benchmarking"]
      }
    },
    {
      "id": "instruction:mini_agent.TOOL_DESCRIPTION",
      "kind": "tool_description",
      "source": {"path": "mini_agent.py", "symbol": "TOOL_DESCRIPTION", "start_line": 3, "end_line": 3},
      "excerpt": "Load the approved benchmark survey packet.",
      "definition_sha256": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
      "used_by": ["mini_agent.compare_to_benchmark.__doc__"],
      "delivered_via": "tool_schema",
      "condition": "Visible while the model is tool-bound.",
      "semantic": {
        "summary": "Tool guidance for loading an approved benchmark packet.",
        "facets": [
          {"label": "benchmarking", "confidence": "explicit", "basis": ["source_text"]}
        ],
        "workflow_ids": ["workflow:survey-benchmarking"]
      }
    }
  ],
  "delivery_paths": [
    {
      "id": "system_message",
      "condition": "Every model call.",
      "branch_expressions": [],
      "sink": "mini_agent.call_model -> model.invoke",
      "steps": ["build_system_prompt composes SYSTEM_RULE", "call_model sends the SystemMessage"],
      "instruction_ids": ["instruction:mini_agent.SYSTEM_PROMPT", "instruction:mini_agent.SYSTEM_RULE"],
      "dynamic_builders": []
    },
    {
      "id": "tool_schema",
      "condition": "Tool-bound model calls.",
      "branch_expressions": [],
      "sink": "mini_agent.call_model -> model_with_tools.invoke",
      "steps": ["bind_tools exposes compare_to_benchmark and its description"],
      "instruction_ids": ["instruction:mini_agent.TOOL_DESCRIPTION"],
      "dynamic_builders": []
    },
    {
      "id": "tool_message",
      "condition": "The benchmark tool returns a result.",
      "branch_expressions": [],
      "sink": "ToolMessage -> next model.invoke",
      "steps": ["compare_to_benchmark prepends RESULT_GUIDANCE to its result"],
      "instruction_ids": ["instruction:mini_agent.RESULT_GUIDANCE"],
      "dynamic_builders": []
    }
  ]
}
```

## Retrieval example

For `find baseline comparison prompts`, resolve the alias `baseline comparison` to `concept:benchmarking`, then follow its `benchmarking` facet and `workflow:survey-benchmarking` membership. The result set contains:

- `SYSTEM_RULE` and `TOOL_DESCRIPTION` as explicit matches;
- `RESULT_GUIDANCE` as an inferred match with `used_by` evidence;
- no duplicate rows when the same instruction is reached through both the facet and workflow.

If the user also asks how the guidance reaches the model, add the relevant graph context: `SYSTEM_RULE` -> `SYSTEM_PROMPT` -> `system_message` -> `model.invoke`. Load the Markdown detail only when complete wording is requested; inspect source only when the map cannot answer or appears stale.

## Corresponding Markdown

The composed prompt is represented by layout and links, not by repeating the expanded prompt:

```text
## Composed system prompts

### mini_agent.SYSTEM_PROMPT
- Source: mini_agent.py:6 (build_system_prompt)
- Layout: Analyst rules:\n{SYSTEM_RULE}
- Ordered components: mini_agent.SYSTEM_RULE
- Full component content appears in its own detail below.
```

Each static instruction appears exactly once in full:

```html
<details id="instruction-mini-agent-system-rule">
<summary><code>SYSTEM_RULE</code> — system_fragment</summary>

- Stable ID: <code>instruction:mini_agent.SYSTEM_RULE</code>
- Source: mini_agent.py:2
- Delivered via: <code>system_message</code>

Full static content:
<pre><code>Compare only compatible measures against the approved benchmark.</code></pre>
</details>

<details id="instruction-mini-agent-tool-description">
<summary><code>TOOL_DESCRIPTION</code> — tool_description</summary>
<pre><code>Load the approved benchmark survey packet.</code></pre>
</details>

<details id="instruction-mini-agent-result-guidance">
<summary><code>RESULT_GUIDANCE</code> — tool_feedback</summary>
<pre><code>Use the returned packet; do not query the same aggregates again.</code></pre>
</details>
```

The JSON remains compact; the Markdown preserves complete static wording. The inferred facet stays visibly distinct from the two source-text-explicit facets.
