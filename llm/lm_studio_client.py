"""
LM Studio client — OpenAI-compatible API (localhost:1234).

Handles local LLMs served via LM Studio, Ollama, or any OpenAI-compatible endpoint.
"""

import os
from typing import Optional

from openai import OpenAI

from llm.base import BaseLLM


class LMStudioClient(BaseLLM):
    """LM Studio client using the OpenAI-compatible API."""

    def __init__(
        self,
        base_url: str = "http://localhost:1234/v1",
        model: str = "gemma-4-26b-a4b-it",
        max_tokens: int = 1500,
    ):
        """
        Initialize the LM Studio client.

        Args:
            base_url: The base URL of the OpenAI-compatible API.
            model: The model name to use.
            max_tokens: Default max tokens for completions.
        """
        self.client = OpenAI(base_url=base_url, api_key="not-needed")
        self.model = model
        self.default_max_tokens = max_tokens

    def complete(
        self,
        prompt: str,
        system_message: Optional[str] = None,
        max_tokens: int = 1500,
        temperature: float = 0.7,
    ) -> str:
        """
        Generate a completion using LM Studio.

        Args:
            prompt: The user prompt.
            system_message: Optional system message.
            max_tokens: Maximum tokens to generate.
            temperature: Sampling temperature.

        Returns:
            The generated text response.
        """
        messages = []
        if system_message:
            messages.append({"role": "system", "content": system_message})
        messages.append({"role": "user", "content": prompt})

        response = self.client.chat.completions.create(
            model=self.model,
            messages=messages,
            max_tokens=max_tokens,
            temperature=temperature,
        )

        return response.choices[0].message.content


def create_lm_studio_client(config: Optional[dict] = None) -> LMStudioClient:
    """
    Factory function to create LMStudioClient from config.

    Args:
        config: Optional config dict with base_url, model, max_tokens.

    Returns:
        Configured LMStudioClient instance.
    """
    if config is None:
        return LMStudioClient()

    return LMStudioClient(
        base_url=config.get("base_url", "http://localhost:1234/v1"),
        model=config.get("model", "gemma-4-26b-a4b-it"),
        max_tokens=config.get("max_tokens", 1500),
    )