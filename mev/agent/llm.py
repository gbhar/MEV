"""Optional assessment through an OpenAI-compatible chat endpoint (NVIDIA NIM by default). Off until
MEV_LLM_API_KEY is set. Advisory: the verdict is recorded and may veto, never approve."""
import json
import logging

import aiohttp

from mev.config import Settings

log = logging.getLogger("mev.agent.llm")

SYSTEM = ("You review a cross-DEX arbitrage candidate found by a deterministic scanner on Base. "
          "Given the opportunity, the on-chain verification and the pool context, answer with JSON only: "
          '{"verdict": "proceed"|"skip"|"investigate", "confidence": 0..1, "reasons": [..]}. '
          "Skip when the spread looks like stale or anomalous liquidity, a token problem, or a trap.")


def enabled(s: Settings) -> bool:
    return bool(s.llm_api_key)


async def assess(s: Settings, context: dict) -> dict:
    url = s.llm_base_url.rstrip("/") + "/chat/completions"
    body = {"model": s.llm_model, "temperature": 0.1, "max_tokens": 600,
            "messages": [{"role": "system", "content": SYSTEM},
                         {"role": "user", "content": json.dumps(context, default=str)}]}
    headers = {"Authorization": f"Bearer {s.llm_api_key}", "Content-Type": "application/json"}
    async with aiohttp.ClientSession() as sess:
        async with sess.post(url, json=body, headers=headers, timeout=aiohttp.ClientTimeout(total=60)) as r:
            data = await r.json()
            if r.status >= 300:
                raise RuntimeError(f"llm {r.status}: {str(data)[:200]}")
    text = data["choices"][0]["message"]["content"]
    start, end = text.find("{"), text.rfind("}")
    verdict = json.loads(text[start:end + 1]) if start >= 0 else {"verdict": "investigate", "reasons": [text[:200]]}
    verdict.setdefault("verdict", "investigate")
    if verdict["verdict"] not in ("proceed", "skip", "investigate"):
        verdict["verdict"] = "investigate"
    verdict["model"] = s.llm_model
    return verdict
