"""
Claude client — Anthropic SDK for Claude models.

Supports prompt caching for cost optimization on long contexts.
"""

import os
from typing import Optional

from anthropic import Anthropic

from llm.base import BaseLLM


class ClaudeClient(BaseLLM):
    """Claude client using the Anthropic SDK."""

    def __init__(
        self,
        model: str = "claude-sonnet-4-6",
        api_key_env: str = "ANTHROPIC_API_KEY",
        max_tokens: int = 1500,
    ):
        """
        Initialize the Claude client.

        Args:
            model: The Claude model to use (e.g., claude-sonnet-4-6).
            api_key_env: Environment variable name containing the API key.
            max_tokens: Default max tokens for completions.
        """
        api_key = os.environ.get(api_key_env)
        if not api_key:
            raise ValueError(f"API key not found in environment variable: {api_key_env}")

        self.client = Anthropic(api_key=api_key)
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
        Generate a completion using Claude.

        Args:
            prompt: The user prompt.
            system_message: Optional system message.
            max_tokens: Maximum tokens to generate.
            temperature: Sampling temperature.

        Returns:
            The generated text response.
        """
        messages = [{"role": "user", "content": prompt}]
        if system_message:
            messages.insert(0, {"role": "system", "content": system_message})

        response = self.client.messages.create(
            model=self.model,
            system=system_message,
            messages=messages,
            max_tokens=max_tokens,
            temperature=temperature,
        )

        return response.content[0].text


def create_claude_client(config: Optional[dict] = None) -> ClaudeClient:
    """
    Factory function to create ClaudeClient from config.

    Args:
        config: Optional config dict with model, api_key_env, max_tokens.

    Returns:
        Configured ClaudeClient instance.
    """
    if config is None:
        return ClaudeClient()

    return ClaudeClient(
        model=config.get("model", "claude-sonnet-4-6"),
        api_key_env=config.get("api_key_env", "ANTHROPIC_API_KEY"),
        max_tokens=config.get("max_tokens", 1500),
    )