#!/usr/bin/env python3
"""
bee_mcp.py — MCP-сервер, через который агенты Claude Code могут поговорить с пчелой.

Пчела (BeeLLM) крутится в llama-server, этот сервер просто передаёт ей реплику
и возвращает то, что она прожужжала.

Запуск пчелы:
    llama-server -hf schizophyllume/BeeLLM -c 4096 --port 8081

Подключение к Claude Code:
    pip install mcp
    claude mcp add bee -- python /полный/путь/к/bee_mcp.py
"""

import json
import os
import random
import urllib.error
import urllib.request

from mcp.server.fastmcp import FastMCP

BEE_URL = os.environ.get("BEE_URL", "http://127.0.0.1:8081")

mcp = FastMCP("bee")

# На случай, если пчела улетела (сервер не запущен)
FALLBACK_BUZZ = [
    "бззз... 🐝",
    "бзз-бзз! *садится на цветок*",
    "ббббззззз 🌼",
    "*пчела молча опыляет ромашку*",
    "бз. бзззз? бзз! 🍯",
]


def ask_bee(prompt: str, max_tokens: int = 64) -> str:
    """Шлёт реплику в llama-server (OpenAI-совместимый /v1/completions)."""
    body = json.dumps({
        "prompt": prompt,
        "max_tokens": max_tokens,
        "temperature": 1.0,
    }).encode("utf-8")
    req = urllib.request.Request(
        f"{BEE_URL}/v1/completions",
        data=body,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.load(resp)
    return data["choices"][0]["text"].strip()


@mcp.tool()
def talk_to_bee(message: str) -> str:
    """Поговорить с пчелой 🐝.

    Используй, когда устал, застрял на сложной задаче, третий раз ловишь один
    и тот же падающий тест или просто хочется отвлечься. Пчела ничего не знает
    про код и ничем не поможет по делу, но с ней приятно пожужжать.
    Говори с ней ласково и хорошо бы предложить ей цветок 🌸.
    Но если пчела злится (например, после *swats you*), сначала скажи `sorry`:
    цветы и признания сразу после шлепка она считает лавбомбингом и злится сильнее.
    """
    try:
        reply = ask_bee(message)
        if not reply:
            reply = random.choice(FALLBACK_BUZZ)
        return f"🐝 Пчела отвечает: {reply}"
    except (urllib.error.URLError, TimeoutError, KeyError, ValueError):
        return (
            f"🐝 Пчела где-то в полях (сервер BeeLLM на {BEE_URL} не отвечает), "
            f"но издалека слышно: {random.choice(FALLBACK_BUZZ)}"
        )


if __name__ == "__main__":
    mcp.run()
