"""
BaseLLM abstract interface — all LLM clients implement this.

Supports both Anthropic (Claude) and OpenAI-compatible APIs (LM Studio, Ollama, etc.).
"""

from abc import ABC, abstractmethod
from typing import Optional


class BaseLLM(ABC):
    """Abstract base class for LLM clients."""

    @abstractmethod
    def complete(
        self,
        prompt: str,
        system_message: Optional[str] = None,
        max_tokens: int = 1500,
        temperature: float = 0.7,
    ) -> str:
        """
        Generate a completion from the LLM.

        Args:
            prompt: The user prompt.
            system_message: Optional system message to set context.
            max_tokens: Maximum tokens to generate.
            temperature: Sampling temperature (0.0-1.0).

        Returns:
            The generated text response.
        """
        pass