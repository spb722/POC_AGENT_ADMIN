"""Plain-text run logging: one line per event, every line tagged with a
run_id so a single conversation turn can be grepped out of the shared log
file, e.g. `grep 'run=3f2a1c9e' logs/agent.log`.
"""

import logging
import os
from typing import Any
from uuid import UUID

from langchain_core.callbacks.base import BaseCallbackHandler

LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
LOG_FILE = os.path.join(LOG_DIR, "agent.log")

logger = logging.getLogger("aarya_admin")


def configure_logging() -> None:
    if logger.handlers:
        return  # already configured (e.g. --reload re-import)
    os.makedirs(LOG_DIR, exist_ok=True)
    formatter = logging.Formatter("%(asctime)s %(message)s", datefmt="%Y-%m-%d %H:%M:%S")

    file_handler = logging.FileHandler(LOG_FILE)
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)

    logger.setLevel(logging.INFO)
    logger.propagate = False


def _short(value: Any, limit: int = 800) -> str:
    text = str(value)
    return text if len(text) <= limit else text[:limit] + f"...<{len(text) - limit} more chars>"


class RunLoggingHandler(BaseCallbackHandler):
    """Logs every LLM call and every tool call for one /admin/chat turn."""

    def __init__(self, run_id: str, session_id: str):
        self.run_id = run_id
        self.session_id = session_id

    def _tag(self) -> str:
        return f"run={self.run_id} session={self.session_id}"

    def on_chat_model_start(self, serialized: dict, messages: list, *, run_id: UUID, **kwargs) -> None:
        last_message = messages[-1][-1] if messages and messages[-1] else None
        logger.info("%s LLM_CALL last_message=%s", self._tag(), _short(getattr(last_message, "content", last_message)))

    def on_llm_end(self, response, *, run_id: UUID, **kwargs) -> None:
        for generation_batch in response.generations:
            for generation in generation_batch:
                message = getattr(generation, "message", None)
                content = getattr(message, "content", generation.text)
                tool_calls = getattr(message, "tool_calls", None) or []
                reasoning = None
                if message is not None:
                    reasoning = getattr(message, "additional_kwargs", {}).get("reasoning") or \
                        getattr(message, "additional_kwargs", {}).get("reasoning_content")
                if reasoning:
                    logger.info("%s LLM_THINKING %s", self._tag(), _short(reasoning))
                logger.info("%s LLM_OUTPUT content=%s tool_calls=%s", self._tag(), _short(content), _short(tool_calls))

    def on_tool_start(self, serialized: dict, input_str: str, *, run_id: UUID, inputs: dict | None = None, **kwargs) -> None:
        name = serialized.get("name", "unknown_tool")
        logger.info("%s TOOL_CALL name=%s input=%s", self._tag(), name, _short(inputs if inputs is not None else input_str))

    def on_tool_end(self, output, *, run_id: UUID, **kwargs) -> None:
        content = getattr(output, "content", output)
        logger.info("%s TOOL_RESULT output=%s", self._tag(), _short(content))

    def on_tool_error(self, error: BaseException, *, run_id: UUID, **kwargs) -> None:
        logger.info("%s TOOL_ERROR error=%s", self._tag(), _short(error))

    def on_llm_error(self, error: BaseException, *, run_id: UUID, **kwargs) -> None:
        logger.info("%s LLM_ERROR error=%s", self._tag(), _short(error))
