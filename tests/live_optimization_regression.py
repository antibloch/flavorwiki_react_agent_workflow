"""Focused live checks for regression.xlsx rows 59, 60 and 73.

This is test-only instrumentation: production agent state and responses remain untouched.
Run from final_agent_work with the BE environment available.
"""
from __future__ import annotations

import hashlib
import json
import sys
import threading
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from langchain_core.messages import HumanMessage
from langchain_core.tools import BaseTool
from langchain_openai import ChatOpenAI
from langgraph.checkpoint.memory import InMemorySaver

import funda_agent_exp as agent


ROWS = {
    59: {
        "prompt": "What are the products tested in this survey? Just list their names.",
        "client": "c0b3b212-bc35-45ef-b69d-8257d3735a90",
        "org": "46671273-0b12-49a5-b9de-60f81d192818",
        "survey": "39af3240-42a8-4e35-8c7d-c61703d5ce3f",
    },
    60: {
        "prompt": "Now give me the mean Aroma score for each of those products.",
        "client": "c0b3b212-bc35-45ef-b69d-8257d3735a90",
        "org": "46671273-0b12-49a5-b9de-60f81d192818",
        "survey": "39af3240-42a8-4e35-8c7d-c61703d5ce3f",
    },
    73: {
        "prompt": (
            "For each product in this survey, summarize the three temporal tasks in one result: "
            "first selected TDS dominant attribute and t_ms, all TCATA attributes ever selected "
            "with earliest t_ms, and maximum time-intensity score with the earliest t_ms at that "
            "maximum. Keep the three event streams independently aggregated before joining, and "
            "include distinct respondent count for each task."
        ),
        "client": "7b445d7f-3fc1-4154-8a23-586e09c781a3",
        "org": "8dd4fd29-cd84-46a1-abcd-1459c547023b",
        "survey": "0cf1a562-a6e0-4da8-b236-5be1926db7a3",
    },
}

LLM_CALLS: list[dict] = []
TOOL_CALLS: list[dict] = []
_LOCK = threading.Lock()


def _prefix_hash(messages) -> str:
    # System plus message 0 is the stable cross-turn prefix this change must preserve.
    text = "\x1e".join(str(getattr(message, "content", "")) for message in messages[:2])
    return hashlib.sha256(text.encode()).hexdigest()


def _install_probes() -> None:
    original_llm = ChatOpenAI.invoke
    original_tool = BaseTool.invoke

    def invoke_llm(self, messages, config=None, *, stop=None, **kwargs):
        started = time.perf_counter()
        response = original_llm(self, messages, config, stop=stop, **kwargs)
        usage = getattr(response, "usage_metadata", None) or {}
        with _LOCK:
            LLM_CALLS.append({
                "model": self.model_name,
                "wall_s": round(time.perf_counter() - started, 3),
                "input_tokens": usage.get("input_tokens") or 0,
                "cached_input_tokens": (
                    usage.get("input_token_details") or {}
                ).get("cache_read", 0),
                "output_tokens": usage.get("output_tokens") or 0,
                "prefix_hash": _prefix_hash(messages),
                "tool_calls": len(getattr(response, "tool_calls", None) or []),
            })
        return response

    def invoke_tool(self, args, config=None, **kwargs):
        started = time.perf_counter()
        result = original_tool(self, args, config, **kwargs)
        with _LOCK:
            TOOL_CALLS.append({
                "name": self.name,
                "wall_s": round(time.perf_counter() - started, 3),
                "thread": threading.current_thread().name,
            })
        return result

    ChatOpenAI.invoke = invoke_llm
    BaseTool.invoke = invoke_tool


def _scratch(row: dict, inventory: str) -> dict:
    return {
        "thread_survey_id": row["survey"],
        "inventory_attached": bool(inventory),
        "inventory_chars": len(inventory),
        "number_of_steps": 0,
        "last_sql_observations": {},
        "zero_row_streak": 0,
    }


def _run(graph, config: dict, row_number: int, text: str, inventory: str) -> dict:
    before_llm, before_tool = len(LLM_CALLS), len(TOOL_CALLS)
    started = time.perf_counter()
    result = graph.invoke(
        {"messages": [HumanMessage(content=text)], **_scratch(ROWS[row_number], inventory)},
        config=config,
    )
    turns = LLM_CALLS[before_llm:]
    tools = TOOL_CALLS[before_tool:]
    return {
        "row": row_number,
        "prompt": ROWS[row_number]["prompt"],
        "wall_s": round(time.perf_counter() - started, 3),
        "llm_turns": len(turns),
        "sql_calls": sum(call["name"] == "nl2sql_tool" for call in tools),
        "max_parallel_calls_emitted": max((call["tool_calls"] for call in turns), default=0),
        "tokens": {
            "input": sum(call["input_tokens"] for call in turns),
            "cached_input": sum(call["cached_input_tokens"] for call in turns),
            "uncached_input": sum(
                call["input_tokens"] - call["cached_input_tokens"] for call in turns
            ),
            "output": sum(call["output_tokens"] for call in turns),
        },
        "prefix_hashes": [call["prefix_hash"] for call in turns],
        "tool_threads": [call["thread"] for call in tools],
        "answer": agent.message_to_text(result["messages"][-1]),
    }


def main() -> None:
    _install_probes()
    results = []

    first = ROWS[59]
    agent._SCOPE.clear()
    agent._SCOPE.update({
        "client_id": first["client"], "organization_id": first["org"],
        "survey_id": first["survey"],
    })
    agent._clear_inventory_cache()
    inventory = agent.survey_inventory(first["survey"])
    graph = agent.build_graph(InMemorySaver())
    config = {
        "recursion_limit": 2 * agent.MAX_LLM_STEPS + 2,
        "configurable": {"thread_id": "live-prefix-59-60"},
    }
    first_text = agent.scoped_query(
        first["prompt"], first["client"], first["org"], first["survey"]
    ) + inventory
    results.append(_run(graph, config, 59, first_text, inventory))
    results.append(_run(graph, config, 60, ROWS[60]["prompt"], inventory))

    temporal = ROWS[73]
    agent._SCOPE.clear()
    agent._SCOPE.update({
        "client_id": temporal["client"], "organization_id": temporal["org"],
        "survey_id": temporal["survey"],
    })
    temporal_inventory = agent.survey_inventory(temporal["survey"])
    temporal_graph = agent.build_graph(InMemorySaver())
    temporal_config = {
        "recursion_limit": 2 * agent.MAX_LLM_STEPS + 2,
        "configurable": {"thread_id": "live-temporal-73"},
    }
    temporal_text = agent.scoped_query(
        temporal["prompt"], temporal["client"], temporal["org"], temporal["survey"]
    ) + temporal_inventory
    results.append(_run(temporal_graph, temporal_config, 73, temporal_text, temporal_inventory))

    results[1]["same_message0_prefix_as_row59"] = (
        results[0]["prefix_hashes"][0] == results[1]["prefix_hashes"][0]
    )
    print("LIVE_OPTIMIZATION_RESULTS=" + json.dumps(results, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
