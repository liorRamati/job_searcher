"""
LLM module — factory for creating LLM clients.

Supports both Claude (Anthropic) and OpenAI-compatible APIs (LM Studio, Ollama).
"""

from typing import Optional

from llm.base import BaseLLM
from llm.claude_client import ClaudeClient, create_claude_client
from llm.lm_studio_client import LMStudioClient, create_lm_studio_client


def create_llm(config: dict) -> BaseLLM:
    """
    Factory function to create an LLM client based on configuration.

    Args:
        config: Dict with 'provider' key ('claude' or 'lm_studio') and provider-specific config.

    Returns:
        A BaseLLM instance (ClaudeClient or LMStudioClient).

    Example config:
        {
            "provider": "lm_studio",
            "lm_studio": {
                "base_url": "http://localhost:1234/v1",
                "model": "gemma-4-26b-a4b-it",
                "max_tokens": 1500
            }
        }
    """
    provider = config.get("provider", "claude")

    if provider == "lm_studio":
        return create_lm_studio_client(config.get("lm_studio"))
    elif provider == "claude":
        return create_claude_client(config.get("claude"))
    else:
        raise ValueError(f"Unknown LLM provider: {provider}. Use 'claude' or 'lm_studio'.")


__all__ = [
    "BaseLLM",
    "ClaudeClient",
    "LMStudioClient",
    "create_llm",
    "create_claude_client",
    "create_lm_studio_client",
]