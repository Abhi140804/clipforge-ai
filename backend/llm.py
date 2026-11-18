"""Minimal Anthropic Messages API client (plain REST) with retry / rate-limit handling.

No SDK dependency. Falls back gracefully: callers check `llm.available` and use
deterministic heuristics when no key is configured.
"""
import json
import random
import re
import time

import requests

RETRY_STATUS = {408, 409, 429, 500, 502, 503, 504, 529}


class LLMError(Exception):
    pass


class LLM:
    def __init__(self, api_key: str = "", model: str = "claude-sonnet-5-5",
                 base_url: str = "https://api.anthropic.com", timeout: int = 180, enabled: bool = True):
        self.api_key = api_key
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.enabled = enabled
        self.calls = 0
        self.input_tokens = 0
        self.output_tokens = 0

    @classmethod
    def for_user(cls, ucfg: dict) -> "LLM":
        return cls(ucfg.get("anthropic_api_key", ""), ucfg.get("anthropic_model", "claude-sonnet-5-5"),
                   ucfg.get("anthropic_base_url", "https://api.anthropic.com"), enabled=ucfg.get("llm_enabled", True))

    @property
    def available(self) -> bool:
        return bool(self.api_key) and self.enabled

    def messages(self, system: str, messages: list, max_tokens: int = 4096, temperature: float | None = None,
                 tools: list | None = None, max_retries: int = 5) -> dict:
        if not self.available:
            raise LLMError("No Anthropic API key configured.")
        body = {"model": self.model, "max_tokens": max_tokens, "system": system, "messages": messages}
        if temperature is not None:
            body["temperature"] = temperature
        if tools:
            body["tools"] = tools
        headers = {"x-api-key": self.api_key, "anthropic-version": "2023-06-01", "content-type": "application/json"}
        last = ""
        for attempt in range(max_retries):
            try:
                r = requests.post(f"{self.base_url}/v1/messages", headers=headers, data=json.dumps(body), timeout=self.timeout)
            except (requests.ConnectionError, requests.Timeout) as e:
                last = f"network error: {e}"
                self._sleep(attempt, None)
                continue
            if r.status_code == 200:
                data = r.json()
                self.calls += 1
                u = data.get("usage") or {}
                self.input_tokens += u.get("input_tokens", 0)
                self.output_tokens += u.get("output_tokens", 0)
                return data
            last = f"HTTP {r.status_code}: {r.text[:300]}"
            if r.status_code in RETRY_STATUS:
                ra = r.headers.get("retry-after")
                self._sleep(attempt, float(ra) if ra and ra.replace(".", "", 1).isdigit() else None)
                continue
            if r.status_code in (401, 403):
                raise LLMError("Anthropic API rejected the credentials (check the API key in Settings).")
            raise LLMError(last)
        raise LLMError(f"Anthropic API failed after {max_retries} attempts ({last})")

    @staticmethod
    def _sleep(attempt: int, retry_after: float | None):
        delay = min(retry_after if retry_after is not None else (2 ** attempt) + random.random(), 60)
        time.sleep(delay)

    @staticmethod
    def text_of(resp: dict) -> str:
        return "".join(b.get("text", "") for b in resp.get("content", []) if b.get("type") == "text")

    def complete(self, system: str, user: str, max_tokens: int = 4096, temperature: float | None = None) -> str:
        return self.text_of(self.messages(system, [{"role": "user", "content": user}], max_tokens, temperature))

    def complete_json(self, system: str, user: str, max_tokens: int = 4096):
        """Ask for JSON; parse robustly; one repair round-trip if the first answer is not valid JSON."""
        sys2 = system + "\n\nRespond with a single valid JSON value and nothing else (no markdown fences, no commentary)."
        text = self.complete(sys2, user, max_tokens)
        try:
            return extract_json(text)
        except ValueError:
            fixed = self.complete(sys2, f"The following was supposed to be valid JSON but could not be parsed. "
                                        f"Return only the corrected JSON.\n\n{text[:12000]}", max_tokens)
            try:
                return extract_json(fixed)
            except ValueError as e:
                raise LLMError(f"Model returned invalid JSON: {e}")


def extract_json(text: str):
    t = text.strip()
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", t, flags=re.S).strip()
    try:
        return json.loads(t)
    except ValueError:
        pass
    # find the outermost {...} or [...]
    for open_c, close_c in (("{", "}"), ("[", "]")):
        s, e = t.find(open_c), t.rfind(close_c)
        if s != -1 and e > s:
            try:
                return json.loads(t[s:e + 1])
            except ValueError:
                continue
    raise ValueError("no JSON found")
