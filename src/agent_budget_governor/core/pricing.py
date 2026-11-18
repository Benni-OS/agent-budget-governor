"""Deterministic token, reasoning, tool-fee, and local-tier pricing engine."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from agent_budget_governor.core.models import TokenRateCard

_PER_MILLION = 1_000_000.0


class UnknownModelError(KeyError):
    """Raised when pricing is requested for an unregistered model in strict mode."""


DEFAULT_RATE_CARDS: tuple[TokenRateCard, ...] = (
    TokenRateCard(
        model="gpt-4o",
        provider="openai",
        tier="frontier",
        input_per_1m_usd=2.50,
        cached_input_per_1m_usd=1.25,
        output_per_1m_usd=10.00,
        reasoning_per_1m_usd=10.00,
        tool_call_fee_usd=0.001,
        is_local=False,
        fallback_model="qwen3-30b-a3b-local",
    ),
    TokenRateCard(
        model="o3",
        provider="openai",
        tier="frontier",
        input_per_1m_usd=10.00,
        cached_input_per_1m_usd=2.50,
        output_per_1m_usd=40.00,
        reasoning_per_1m_usd=40.00,
        tool_call_fee_usd=0.002,
        is_local=False,
        fallback_model="qwen3-30b-a3b-local",
    ),
    TokenRateCard(
        model="gpt-4o-mini",
        provider="openai",
        tier="optimal",
        input_per_1m_usd=0.15,
        cached_input_per_1m_usd=0.075,
        output_per_1m_usd=0.60,
        reasoning_per_1m_usd=0.60,
        tool_call_fee_usd=0.0002,
        is_local=False,
        fallback_model="qwen3-30b-a3b-local",
    ),
    TokenRateCard(
        model="claude-3-7-sonnet",
        provider="anthropic",
        tier="frontier",
        input_per_1m_usd=3.00,
        cached_input_per_1m_usd=0.30,
        output_per_1m_usd=15.00,
        reasoning_per_1m_usd=15.00,
        tool_call_fee_usd=0.001,
        is_local=False,
        fallback_model="gemini-flash",
    ),
    TokenRateCard(
        model="claude-opus-4",
        provider="anthropic",
        tier="frontier",
        input_per_1m_usd=15.00,
        cached_input_per_1m_usd=1.50,
        output_per_1m_usd=75.00,
        reasoning_per_1m_usd=75.00,
        tool_call_fee_usd=0.002,
        is_local=False,
        fallback_model="gemini-flash",
    ),
    TokenRateCard(
        model="claude-3-5-haiku",
        provider="anthropic",
        tier="optimal",
        input_per_1m_usd=0.80,
        cached_input_per_1m_usd=0.08,
        output_per_1m_usd=4.00,
        reasoning_per_1m_usd=4.00,
        tool_call_fee_usd=0.0002,
        is_local=False,
        fallback_model="qwen3-30b-a3b-local",
    ),
    TokenRateCard(
        model="gemini-2.5-pro",
        provider="google",
        tier="frontier",
        input_per_1m_usd=1.25,
        cached_input_per_1m_usd=0.3125,
        output_per_1m_usd=10.00,
        reasoning_per_1m_usd=10.00,
        tool_call_fee_usd=0.0005,
        is_local=False,
        fallback_model="gemini-flash",
    ),
    TokenRateCard(
        model="gemini-flash",
        provider="google",
        tier="optimal",
        input_per_1m_usd=0.10,
        cached_input_per_1m_usd=0.025,
        output_per_1m_usd=0.40,
        reasoning_per_1m_usd=0.40,
        tool_call_fee_usd=0.0001,
        is_local=False,
        fallback_model="qwen3-30b-a3b-local",
    ),
    TokenRateCard(
        model="gemini-2.5-flash",
        provider="google",
        tier="optimal",
        input_per_1m_usd=0.15,
        cached_input_per_1m_usd=0.0375,
        output_per_1m_usd=0.60,
        reasoning_per_1m_usd=2.50,
        tool_call_fee_usd=0.0001,
        is_local=False,
        fallback_model="qwen3-30b-a3b-local",
    ),
    TokenRateCard(
        model="qwen3-30b-a3b-local",
        provider="local-vllm",
        tier="local",
        input_per_1m_usd=0.0,
        cached_input_per_1m_usd=0.0,
        output_per_1m_usd=0.0,
        reasoning_per_1m_usd=0.0,
        tool_call_fee_usd=0.0,
        is_local=True,
        fallback_model=None,
    ),
    TokenRateCard(
        model="llama-3.3-70b-local",
        provider="local-llama-cpp",
        tier="local",
        input_per_1m_usd=0.0,
        cached_input_per_1m_usd=0.0,
        output_per_1m_usd=0.0,
        reasoning_per_1m_usd=0.0,
        tool_call_fee_usd=0.0,
        is_local=True,
        fallback_model=None,
    ),
    TokenRateCard(
        model="deepseek-r1-14b-local",
        provider="local-ollama",
        tier="local",
        input_per_1m_usd=0.0,
        cached_input_per_1m_usd=0.0,
        output_per_1m_usd=0.0,
        reasoning_per_1m_usd=0.0,
        tool_call_fee_usd=0.0,
        is_local=True,
        fallback_model=None,
    ),
)


@dataclass(frozen=True)
class CostBreakdown:
    """Itemized deterministic cost calculation result."""

    model: str
    is_local: bool
    input_tokens: int
    cached_input_tokens: int
    output_tokens: int
    reasoning_tokens: int
    tool_calls: int
    input_cost_usd: float
    cached_input_cost_usd: float
    output_cost_usd: float
    reasoning_cost_usd: float
    tool_call_cost_usd: float
    total_cost_usd: float

    @property
    def total_tokens(self) -> int:
        return (
            self.input_tokens
            + self.cached_input_tokens
            + self.output_tokens
            + self.reasoning_tokens
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "is_local": self.is_local,
            "input_tokens": self.input_tokens,
            "cached_input_tokens": self.cached_input_tokens,
            "output_tokens": self.output_tokens,
            "reasoning_tokens": self.reasoning_tokens,
            "total_tokens": self.total_tokens,
            "tool_calls": self.tool_calls,
            "input_cost_usd": self.input_cost_usd,
            "cached_input_cost_usd": self.cached_input_cost_usd,
            "output_cost_usd": self.output_cost_usd,
            "reasoning_cost_usd": self.reasoning_cost_usd,
            "tool_call_cost_usd": self.tool_call_cost_usd,
            "total_cost_usd": self.total_cost_usd,
        }


class PricingCatalog:
    """Deterministic multi-tier token, reasoning, and tool-fee cost calculator."""

    def __init__(
        self,
        rate_cards: tuple[TokenRateCard, ...] | list[TokenRateCard] = DEFAULT_RATE_CARDS,
        *,
        strict: bool = True,
    ) -> None:
        self._strict = strict
        self._cards: dict[str, TokenRateCard] = {}
        for card in rate_cards:
            self.register_rate_card(card)

    def register_rate_card(self, card: TokenRateCard) -> None:
        """Register or update a model's deterministic rate card."""
        self._cards[card.model.strip().lower()] = card

    def list_models(self) -> list[str]:
        return sorted(card.model for card in self._cards.values())

    def get_rate_card(self, model: str) -> TokenRateCard:
        key = model.strip().lower()
        if key in self._cards:
            return self._cards[key]

        # Local naming conventions resolve to $0.00 local execution tier automatically
        if key.endswith("-local") or key.startswith("local/") or key.startswith("local:"):
            return TokenRateCard(
                model=model,
                provider="local",
                tier="local",
                input_per_1m_usd=0.0,
                cached_input_per_1m_usd=0.0,
                output_per_1m_usd=0.0,
                reasoning_per_1m_usd=0.0,
                tool_call_fee_usd=0.0,
                is_local=True,
                fallback_model=None,
            )

        if not self._strict:
            return TokenRateCard(
                model=model,
                provider="fallback-conservative",
                tier="frontier",
                input_per_1m_usd=3.00,
                cached_input_per_1m_usd=1.50,
                output_per_1m_usd=15.00,
                reasoning_per_1m_usd=15.00,
                tool_call_fee_usd=0.001,
                is_local=False,
                fallback_model="qwen3-30b-a3b-local",
            )

        raise UnknownModelError(f"Model '{model}' is not registered in PricingCatalog.")

    def calculate_cost(
        self,
        model: str,
        *,
        input_tokens: int = 0,
        cached_input_tokens: int = 0,
        output_tokens: int = 0,
        reasoning_tokens: int = 0,
        tool_calls: int = 0,
    ) -> CostBreakdown:
        """Compute deterministic USD breakdown for an LLM/tool call."""
        for name, count in (
            ("input_tokens", input_tokens),
            ("cached_input_tokens", cached_input_tokens),
            ("output_tokens", output_tokens),
            ("reasoning_tokens", reasoning_tokens),
            ("tool_calls", tool_calls),
        ):
            if count < 0:
                raise ValueError(f"{name} must be >= 0, got {count}")

        card = self.get_rate_card(model)
        if card.is_local:
            return CostBreakdown(
                model=card.model,
                is_local=True,
                input_tokens=input_tokens,
                cached_input_tokens=cached_input_tokens,
                output_tokens=output_tokens,
                reasoning_tokens=reasoning_tokens,
                tool_calls=tool_calls,
                input_cost_usd=0.0,
                cached_input_cost_usd=0.0,
                output_cost_usd=0.0,
                reasoning_cost_usd=0.0,
                tool_call_cost_usd=0.0,
                total_cost_usd=0.0,
            )

        input_cost = round((input_tokens / _PER_MILLION) * card.input_per_1m_usd, 9)
        cached_cost = round((cached_input_tokens / _PER_MILLION) * card.cached_input_per_1m_usd, 9)
        output_cost = round((output_tokens / _PER_MILLION) * card.output_per_1m_usd, 9)
        reasoning_cost = round(
            (reasoning_tokens / _PER_MILLION) * card.effective_reasoning_per_1m_usd,
            9,
        )
        tool_cost = round(tool_calls * card.tool_call_fee_usd, 9)
        total = round(input_cost + cached_cost + output_cost + reasoning_cost + tool_cost, 9)

        return CostBreakdown(
            model=card.model,
            is_local=False,
            input_tokens=input_tokens,
            cached_input_tokens=cached_input_tokens,
            output_tokens=output_tokens,
            reasoning_tokens=reasoning_tokens,
            tool_calls=tool_calls,
            input_cost_usd=input_cost,
            cached_input_cost_usd=cached_cost,
            output_cost_usd=output_cost,
            reasoning_cost_usd=reasoning_cost,
            tool_call_cost_usd=tool_cost,
            total_cost_usd=total,
        )
