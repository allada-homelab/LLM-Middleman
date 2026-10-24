"""Test graphs for the LangGraph live E2E rig (LLMM-022) — served by `langgraph dev`.

No LLM or API key needed: a scripted chat model streams a tool call then an answer (with
usage_metadata), so every wire shape the adapter handles is produced deterministically.
Graphs: `agent` (non-streaming echo), `react` (tool loop), `interrupt` (asks a question),
`slow` (a silent 40 s step), `ctx` (echoes runtime.context and system messages in state).
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

from langchain_core.callbacks import CallbackManagerForLLMRun
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, AIMessageChunk, BaseMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult
from langgraph.graph import END, START, MessagesState, StateGraph
from langgraph.runtime import Runtime
from langgraph.types import interrupt


class ScriptedModel(BaseChatModel):
    """Streams a tool call on the first pass, then a token-by-token answer with usage."""

    @property
    def _llm_type(self) -> str:
        return "scripted"

    def _decide(self, messages: list[BaseMessage]) -> list[AIMessageChunk]:
        if not any(isinstance(m, ToolMessage) for m in messages):
            return [
                AIMessageChunk(
                    content="", tool_call_chunks=[{"name": "get_weather", "args": "", "id": "call_1", "index": 0}]
                ),
                AIMessageChunk(
                    content="", tool_call_chunks=[{"name": None, "args": '{"city": ', "id": None, "index": 0}]
                ),
                AIMessageChunk(
                    content="", tool_call_chunks=[{"name": None, "args": '"Paris"}', "id": None, "index": 0}]
                ),
                AIMessageChunk(content="", usage_metadata={"input_tokens": 11, "output_tokens": 5, "total_tokens": 16}),
            ]
        result = [m for m in messages if isinstance(m, ToolMessage)][-1].content
        words = ["It ", "is ", f"{result} ", "in ", "Paris."]
        chunks = [AIMessageChunk(content=w) for w in words]
        chunks.append(
            AIMessageChunk(content="", usage_metadata={"input_tokens": 23, "output_tokens": 7, "total_tokens": 30})
        )
        return chunks

    def _stream(
        self,
        messages: list[BaseMessage],
        stop: Any = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> Iterator[ChatGenerationChunk]:
        for chunk in self._decide(messages):
            gen = ChatGenerationChunk(message=chunk)
            if run_manager:
                run_manager.on_llm_new_token(chunk.content if isinstance(chunk.content, str) else "", chunk=gen)
            yield gen

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: Any = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        total: AIMessageChunk | None = None
        for c in self._decide(messages):
            total = c if total is None else total + c
        if total is None:
            raise ValueError("scripted model produced no chunks")
        return ChatResult(
            generations=[ChatGeneration(message=AIMessage(**total.model_dump(exclude={"type", "tool_call_chunks"})))]
        )


model = ScriptedModel()


def agent(state: MessagesState) -> dict[str, Any]:
    return {"messages": [model.invoke(state["messages"])]}


def tools(state: MessagesState) -> dict[str, Any]:
    call = state["messages"][-1].tool_calls[0]
    return {"messages": [ToolMessage(content="sunny and 21C", tool_call_id=call["id"], name=call["name"])]}


def route(state: MessagesState) -> str:
    last = state["messages"][-1]
    return "tools" if isinstance(last, AIMessage) and last.tool_calls else END


g = StateGraph(MessagesState)
g.add_node("agent", agent)
g.add_node("tools", tools)
g.add_edge(START, "agent")
g.add_conditional_edges("agent", route, ["tools", END])
g.add_edge("tools", "agent")
react = g.compile()


# --- echo graph: returns a whole AIMessage without streaming --------------------
def echo(state: MessagesState) -> dict[str, Any]:
    return {"messages": [AIMessage(content=f"echo:{state['messages'][-1].content}")]}


ge = StateGraph(MessagesState)
ge.add_node("agent", echo)
ge.add_edge(START, "agent")
echo_graph = ge.compile()


# --- interrupt graph: asks a question, resumes with the answer ---------------
def ask(state: MessagesState) -> dict[str, Any]:
    answer = interrupt({"question": "Which room should I turn the lights on in?"})
    return {"messages": [AIMessage(content=f"Turning on the lights in the {answer}.")]}


gi = StateGraph(MessagesState)
gi.add_node("ask", ask)
gi.add_edge(START, "ask")
interrupter = gi.compile()


# --- slow graph: a silent 40 s tool step before the answer -------------------
async def slow_tool(state: MessagesState) -> dict[str, Any]:
    await asyncio.sleep(40)
    return {"messages": [AIMessage(content="Done after a slow step.")]}


gs = StateGraph(MessagesState)
gs.add_node("slow", slow_tool)
gs.add_edge(START, "slow")
slow = gs.compile()


# --- context graph: echoes runtime.context + system-message count ------------
@dataclass
class Ctx:
    system_prompt: str = ""
    language: str = ""
    device_id: str | None = None
    satellite_id: str | None = None
    conversation_id: str | None = None
    user_id: str | None = None


def ctx_node(state: MessagesState, runtime: Runtime[Ctx]) -> dict[str, Any]:
    c = runtime.context
    n_sys = sum(1 for m in state["messages"] if m.type == "system")
    text = (
        f"ctx lang={c.language} dev={c.device_id} sat={c.satellite_id} conv={c.conversation_id} "
        f"user={c.user_id} prompt={c.system_prompt!r} sys_in_state={n_sys}"
    )
    return {"messages": [AIMessage(content=text)]}


gc = StateGraph(MessagesState, context_schema=Ctx)
gc.add_node("ctx", ctx_node)
gc.add_edge(START, "ctx")
ctxgraph = gc.compile()
