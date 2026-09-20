#!/usr/bin/env python3
"""Small local browser for the deployed agent's durable conversation log.

Run with: .venv/bin/python conversation_log_gui.py
The bearer token is requested at startup and is never written to disk.
"""
from __future__ import annotations

import json
import os
import queue
import threading
import tkinter as tk
from datetime import datetime, timezone
from tkinter import messagebox, ttk
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


DEFAULT_URL = "https://herbalife-agent.gpisurveys.com"


def format_timing_event(event: dict) -> str:
    """Render structured server/client telemetry compactly for the local viewer."""
    try:
        data = json.loads(event["content"])
    except (KeyError, TypeError, ValueError):
        return event.get("content", "")

    if event.get("kind") == "round_timing":
        lines = ["Latency / agent telemetry:"]
        labels = (
            ("request_id", "Request ID"),
            ("lock_wait_ms", "Lock wait (ms)"),
            ("scope_ms", "Scope (ms)"),
            ("inventory_ms", "Inventory (ms)"),
            ("graph_ms", "Graph (ms)"),
            ("llm_total_ms", "LLM total (ms)"),
            ("llm_turn_count", "LLM turns"),
            ("final_response_ttft_ms", "Final response TTFT (ms)"),
            ("final_response_ttft_from_final_turn_ms", "Final-turn TTFT (ms)"),
            ("final_response_output_tokens", "Final output tokens"),
            ("total_agent_output_tokens", "Total agent output tokens"),
            ("first_llm_input_tokens", "First LLM input tokens"),
            ("first_llm_cached_input_tokens", "First LLM cached input tokens"),
            ("tool_total_ms", "Tools total (ms)"),
            ("tool_call_count", "Tool calls"),
            ("max_silent_gap_ms", "Max silent gap (ms)"),
            ("server_stream_total_ms", "Server total (ms)"),
            ("completed", "Completed"),
            ("client_disconnected", "Client disconnected"),
            ("error_type", "Error"),
        )
        for key, label in labels:
            if data.get(key) is not None:
                lines.append(f"  {label}: {data[key]}")
        return "\n".join(lines)

    if event.get("kind") == "client_timing":
        lines = ["Client / network telemetry:"]
        for key, label in (
            ("request_id", "Request ID"),
            ("response_headers_ms", "Response headers (ms)"),
            ("first_byte_ms", "First byte (ms)"),
            ("first_status_ms", "First status (ms)"),
            ("first_token_ms", "First token (ms)"),
            ("last_event_ms", "Last event (ms)"),
            ("done_ms", "Done (ms)"),
            ("client_total_ms", "Client total (ms)"),
            ("last_event_type", "Last event"),
            ("client_error_type", "Client error"),
            ("client_error_message", "Error message"),
        ):
            if data.get(key) is not None:
                lines.append(f"  {label}: {data[key]}")
        return "\n".join(lines)

    return event.get("content", "")


class ConversationLogGui:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("FlavorAI conversation log")
        self.root.geometry("1200x760")
        self.results: list[dict] = []
        self.result_queue: queue.Queue[tuple[str, object]] = queue.Queue()

        top = ttk.Frame(root, padding=8)
        top.pack(fill="x")
        ttk.Label(top, text="Agent URL").grid(row=0, column=0, sticky="w")
        self.url = ttk.Entry(top, width=48)
        self.url.insert(0, os.getenv("FLAVORAI_AGENT_URL", DEFAULT_URL))
        self.url.grid(row=0, column=1, padx=(6, 16), sticky="ew")
        ttk.Label(top, text="Bearer token").grid(row=0, column=2, sticky="w")
        self.token = ttk.Entry(top, width=38, show="*")
        self.token.insert(0, os.getenv("HERBALIFE_EXTERNAL_ACCESS_SECRET", ""))
        self.token.grid(row=0, column=3, padx=6, sticky="ew")
        ttk.Label(top, text="UTC date").grid(row=0, column=4, sticky="w")
        self.date = ttk.Entry(top, width=12)
        self.date.insert(0, datetime.now(timezone.utc).date().isoformat())
        self.date.grid(row=0, column=5, padx=6)
        self.detailed = tk.BooleanVar(value=True)
        ttk.Checkbutton(top, text="Detailed trace", variable=self.detailed).grid(
            row=0, column=6, padx=6
        )
        self.search_button = ttk.Button(top, text="Load", command=self.load)
        self.search_button.grid(row=0, column=7, padx=(6, 0))
        top.columnconfigure(1, weight=1)
        top.columnconfigure(3, weight=1)

        body = ttk.Panedwindow(root, orient="horizontal")
        body.pack(fill="both", expand=True, padx=8, pady=(0, 8))
        left = ttk.Frame(body, padding=4)
        right = ttk.Frame(body, padding=4)
        body.add(left, weight=1)
        body.add(right, weight=3)

        columns = ("thread", "survey", "events", "last")
        self.tree = ttk.Treeview(left, columns=columns, show="headings")
        headings = {"thread": "Thread", "survey": "Survey ID", "events": "Events", "last": "Last event (UTC)"}
        widths = {"thread": 150, "survey": 260, "events": 70, "last": 190}
        for column in columns:
            self.tree.heading(column, text=headings[column])
            self.tree.column(column, width=widths[column], anchor="w")
        tree_scroll = ttk.Scrollbar(left, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=tree_scroll.set)
        self.tree.pack(side="left", fill="both", expand=True)
        tree_scroll.pack(side="right", fill="y")
        self.tree.bind("<<TreeviewSelect>>", self.show_selected)

        self.output = tk.Text(right, wrap="word", state="disabled")
        output_scroll = ttk.Scrollbar(right, orient="vertical", command=self.output.yview)
        self.output.configure(yscrollcommand=output_scroll.set)
        self.output.pack(side="left", fill="both", expand=True)
        output_scroll.pack(side="right", fill="y")
        self.status = ttk.Label(root, text="Enter the deployment bearer token and choose a date.", padding=8)
        self.status.pack(fill="x")
        self.root.after(100, self.poll_results)

    def load(self) -> None:
        base_url = self.url.get().strip().rstrip("/")
        token = self.token.get().strip()
        day = self.date.get().strip()
        if not base_url or not token or len(day) != 10:
            messagebox.showerror("Missing input", "Agent URL, bearer token, and YYYY-MM-DD date are required.")
            return
        self.search_button.configure(state="disabled")
        self.status.configure(text=f"Loading conversations for {day} UTC...")
        for item in self.tree.get_children():
            self.tree.delete(item)
        self.set_output("")
        threading.Thread(
            target=self.fetch,
            args=(base_url, token, day, self.detailed.get()),
            daemon=True,
        ).start()

    def fetch(self, base_url: str, token: str, day: str, detailed: bool) -> None:
        query = urlencode({"date": day, "detailed": "true" if detailed else "false"})
        request = Request(
            f"{base_url}/admin/conversations?{query}",
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
        )
        try:
            with urlopen(request, timeout=60) as response:
                payload = json.loads(response.read().decode("utf-8"))
            self.result_queue.put(("ok", payload))
        except HTTPError as exc:
            self.result_queue.put(("error", f"HTTP {exc.code}: {exc.read().decode('utf-8', 'replace')}"))
        except (URLError, TimeoutError, ValueError) as exc:
            self.result_queue.put(("error", str(exc)))

    def poll_results(self) -> None:
        try:
            kind, value = self.result_queue.get_nowait()
        except queue.Empty:
            self.root.after(100, self.poll_results)
            return
        self.search_button.configure(state="normal")
        if kind == "error":
            self.status.configure(text="Request failed")
            messagebox.showerror("Could not load log", str(value))
        else:
            self.results = value["conversations"]
            for index, conversation in enumerate(self.results):
                self.tree.insert("", "end", iid=str(index), values=(
                    conversation["thread_id"], conversation["survey_id"],
                    conversation["event_count"], conversation["last_event_at"],
                ))
            self.status.configure(text=f"Loaded {len(self.results)} conversation(s).")
        self.root.after(100, self.poll_results)

    def show_selected(self, _event: object) -> None:
        selection = self.tree.selection()
        if not selection:
            return
        conversation = self.results[int(selection[0])]
        lines = [
            f"Thread: {conversation['thread_id']}",
            f"Survey: {conversation['survey_id']}",
            f"Events: {conversation['event_count']}  Turns: {conversation['turn_count']}",
            f"Range: {conversation['first_event_at']} - {conversation['last_event_at']}",
            "",
        ]
        for event in conversation["events"]:
            label = event["kind"]
            if event.get("name"):
                label += f" ({event['name']})"
            content = (
                format_timing_event(event)
                if event["kind"] in {"round_timing", "client_timing"}
                else event["content"]
            )
            lines.extend([f"[{event['created_at']}] turn {event['turn']} {label}", content, ""])
        self.set_output("\n".join(lines))

    def set_output(self, text: str) -> None:
        self.output.configure(state="normal")
        self.output.delete("1.0", "end")
        self.output.insert("1.0", text)
        self.output.configure(state="disabled")


if __name__ == "__main__":
    app_root = tk.Tk()
    ConversationLogGui(app_root)
    app_root.mainloop()
