"""Tests must not leak LLM circuit-breaker state into each other (CI flake, 14 Oct 2026)."""
from collectors import llm_client


def test_a_tripped_breaker_in_one_test():
    llm_client.trip_circuit_breaker("groq", seconds=300, reason="simulated")
    assert llm_client.is_circuit_open("groq")


def test_does_not_pause_the_next_test():
    assert not llm_client.is_circuit_open("groq")
