"""Public (OpenAI-compatible) request/response schemas.

These types define the gateway's *public contract*. No Qwen-specific field ever
appears here — the provider adapters translate in both directions.
"""

from __future__ import annotations

import time
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.utils.ids import completion_id

Role = Literal["system", "user", "assistant", "tool", "function", "developer"]


class FunctionCall(BaseModel):
    name: str
    arguments: str = "{}"


class ToolCall(BaseModel):
    id: str
    type: Literal["function"] = "function"
    function: FunctionCall


class ToolCallDelta(BaseModel):
    index: int = 0
    id: str | None = None
    type: Literal["function"] | None = None
    function: dict[str, Any] | None = None


class ChatMessage(BaseModel):
    model_config = ConfigDict(extra="allow")

    role: Role
    #: string, or OpenAI multi-part content blocks
    content: str | list[dict[str, Any]] | None = None
    name: str | None = None
    tool_calls: list[ToolCall] | None = None
    tool_call_id: str | None = None

    def text(self) -> str:
        """Flatten content parts into plain text."""
        if self.content is None:
            return ""
        if isinstance(self.content, str):
            return self.content
        parts: list[str] = []
        for block in self.content:
            if not isinstance(block, dict):
                continue
            if block.get("type") in (None, "text", "input_text") and block.get("text"):
                parts.append(str(block["text"]))
        return "\n".join(parts)


class FunctionDefinition(BaseModel):
    model_config = ConfigDict(extra="allow")

    name: str
    description: str | None = None
    parameters: dict[str, Any] | None = None


class ToolDefinition(BaseModel):
    type: Literal["function"] = "function"
    function: FunctionDefinition


class StreamOptions(BaseModel):
    include_usage: bool = False


class ChatCompletionRequest(BaseModel):
    """OpenAI ``POST /v1/chat/completions`` request body."""

    model_config = ConfigDict(extra="allow")

    model: str = Field(default="qwen", max_length=200)
    messages: list[ChatMessage] = Field(min_length=1)
    stream: bool = False
    stream_options: StreamOptions | None = None

    temperature: float | None = Field(default=None, ge=0.0, le=2.0)
    top_p: float | None = Field(default=None, gt=0.0, le=1.0)
    max_tokens: int | None = Field(default=None, gt=0, le=1_000_000)
    max_completion_tokens: int | None = Field(default=None, gt=0, le=1_000_000)
    presence_penalty: float | None = Field(default=None, ge=-2.0, le=2.0)
    frequency_penalty: float | None = Field(default=None, ge=-2.0, le=2.0)
    stop: str | list[str] | None = None
    n: int | None = Field(default=1, ge=1, le=1)
    seed: int | None = None
    user: str | None = Field(default=None, max_length=256)

    tools: list[ToolDefinition] | None = None
    tool_choice: str | dict[str, Any] | None = None
    #: Legacy OpenAI function-calling API (still used by some clients).
    functions: list[FunctionDefinition] | None = None
    function_call: str | dict[str, Any] | None = None

    #: Qwen-style reasoning switches accepted for convenience; they are mapped
    #: by the adapter and never forwarded blindly.
    enable_thinking: bool | None = None
    reasoning_effort: Literal["none", "low", "medium", "high"] | None = None

    @field_validator("messages")
    @classmethod
    def _validate_messages(cls, value: list[ChatMessage]) -> list[ChatMessage]:
        if not value:
            raise ValueError("messages must not be empty")
        return value

    def effective_tools(self) -> list[ToolDefinition]:
        """Merge modern ``tools`` with the legacy ``functions`` field."""
        if self.tools:
            return self.tools
        if self.functions:
            return [ToolDefinition(function=fn) for fn in self.functions]
        return []

    def effective_max_tokens(self) -> int | None:
        return self.max_tokens or self.max_completion_tokens

    def wants_reasoning(self) -> bool:
        if self.enable_thinking is not None:
            return self.enable_thinking
        if self.reasoning_effort is not None:
            return self.reasoning_effort != "none"
        return False


class Usage(BaseModel):
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


class ResponseMessage(BaseModel):
    model_config = ConfigDict(extra="allow")

    role: Literal["assistant"] = "assistant"
    content: str | None = None
    tool_calls: list[ToolCall] | None = None
    #: Present only when reasoning exposure is enabled (EXPOSE_REASONING=true).
    reasoning_content: str | None = None


class Choice(BaseModel):
    index: int = 0
    message: ResponseMessage
    finish_reason: Literal["stop", "length", "tool_calls", "content_filter"] | None = "stop"
    logprobs: None = None


class ChatCompletionResponse(BaseModel):
    id: str = Field(default_factory=completion_id)
    object: Literal["chat.completion"] = "chat.completion"
    created: int = Field(default_factory=lambda: int(time.time()))
    model: str
    choices: list[Choice]
    usage: Usage = Field(default_factory=Usage)
    system_fingerprint: str | None = None


class ChoiceDelta(BaseModel):
    model_config = ConfigDict(extra="allow")

    role: Literal["assistant"] | None = None
    content: str | None = None
    tool_calls: list[ToolCallDelta] | None = None
    reasoning_content: str | None = None


class ChunkChoice(BaseModel):
    index: int = 0
    delta: ChoiceDelta
    finish_reason: str | None = None
    logprobs: None = None


class ChatCompletionChunk(BaseModel):
    id: str
    object: Literal["chat.completion.chunk"] = "chat.completion.chunk"
    created: int = Field(default_factory=lambda: int(time.time()))
    model: str
    choices: list[ChunkChoice]
    usage: Usage | None = None


class ModelCard(BaseModel):
    id: str
    object: Literal["model"] = "model"
    created: int = Field(default_factory=lambda: int(time.time()))
    owned_by: str = "qwen"
    #: Non-standard but harmless extras used by the dashboard.
    aliases: list[str] = Field(default_factory=list)
    supports_tools: bool = True
    supports_reasoning: bool = False


class ModelList(BaseModel):
    object: Literal["list"] = "list"
    data: list[ModelCard]
