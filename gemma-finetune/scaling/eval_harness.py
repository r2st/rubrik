"""LLM-as-judge evaluation harness.

Replaces the lexical-only ROUGE-L evaluation from `../code/judge_compare.py`
with a structured rubric scored by Claude / GPT-4-class. Used in two places:

  1. **Active learning** (`active_learning.py`) — score a single inference,
     decide if the model's output is good enough or if we should prefer the
     judge's own answer for the training queue
  2. **Champion / challenger** — score N candidate adapters head-to-head on a
     held-out eval set; gate promotion of new fine-tunes

Both use the same `LLMJudge` interface; only the calling pattern differs.

Why LLM-as-judge over ROUGE: v3's meeting-1 output captured every fact in
the reference but scored ROUGE-L 0.34 because it used different words. ROUGE
is lexical; we need semantic + structural + faithfulness scoring. The cost
(~$0.005 per call) is fine at evaluation scale.
"""
from __future__ import annotations

import asyncio
import json
import logging
import textwrap
from dataclasses import dataclass
from typing import Any, Optional

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Rubric — what the judge is asked to score
# ---------------------------------------------------------------------------
RUBRIC = textwrap.dedent("""\
    You are evaluating a model-generated meeting summary against the reference.
    Score each dimension on a 0..1 scale, then assign an overall score.

    Dimensions:

      1. **Faithfulness** — does every claim in the summary follow from the
         transcript? Mark down for any hallucination, however small.
      2. **Completeness** — does the summary capture the key facts a busy
         reader would want? Mark down for missing the punchline.
      3. **Format** — does it follow the expected structure (one paragraph,
         then `Owner: task` action-item bullets)?
      4. **Style match** — does it sound like the dataset's reference summaries
         (concise, business-formal, no fluff)?

    Return a JSON object:
      {
        "faithfulness": 0..1,
        "completeness": 0..1,
        "format": 0..1,
        "style": 0..1,
        "overall": 0..1,
        "feedback": "1-2 sentences",
        "preferred_output": "your version of the summary IF the candidate is
                             materially worse, else exactly the candidate"
      }
""")


# ---------------------------------------------------------------------------
# Verdict (mirrors active_learning.JudgeVerdict)
# ---------------------------------------------------------------------------
@dataclass
class JudgeVerdict:
    judge_id: str
    overall_score: float
    is_acceptable: bool
    preferred_output: str
    feedback: str
    cost_usd: float
    sub_scores: dict[str, float]


# ---------------------------------------------------------------------------
# LLM Judge
# ---------------------------------------------------------------------------
class LLMJudge:
    """Provider-agnostic LLM-as-judge wrapper.

    Two providers supported today: Anthropic (Claude) and OpenAI (GPT-4-class).
    Same rubric, same return shape — switch via the `provider` arg.

    Production cost: ~$0.005 per evaluation at Anthropic Sonnet pricing.
    Daily budget guard lives in the calling worker, not here.
    """

    def __init__(
        self,
        provider: str = "anthropic",
        model: str = "claude-3.5-sonnet",
        max_retries: int = 3,
    ) -> None:
        self.provider = provider
        self.model = model
        self.max_retries = max_retries
        self.judge_id = f"{provider}/{model}@harness-v2"

    async def score(
        self,
        prompt: str,
        candidate: str,
        *,
        reference: Optional[str] = None,
        tenant_id: Optional[str] = None,
        acceptance_threshold: float = 0.70,
    ) -> JudgeVerdict:
        """Run the rubric. If `reference` is provided, the judge compares
        candidate to reference; otherwise it scores candidate against the
        prompt directly."""
        body = self._build_judge_prompt(prompt, candidate, reference)
        raw, cost_usd = await self._call_provider(body)
        parsed = self._parse_response(raw, fallback_candidate=candidate)
        return JudgeVerdict(
            judge_id=self.judge_id,
            overall_score=parsed["overall"],
            is_acceptable=parsed["overall"] >= acceptance_threshold,
            preferred_output=parsed.get("preferred_output", candidate),
            feedback=parsed.get("feedback", ""),
            cost_usd=cost_usd,
            sub_scores={
                k: parsed[k]
                for k in ("faithfulness", "completeness", "format", "style")
                if k in parsed
            },
        )

    async def score_batch(
        self,
        items: list[dict[str, Any]],
        *,
        max_concurrency: int = 10,
        acceptance_threshold: float = 0.70,
    ) -> list[JudgeVerdict]:
        """Score N items with bounded concurrency. Used by the champion-
        challenger evaluator on a held-out eval set."""
        sem = asyncio.Semaphore(max_concurrency)

        async def _one(item: dict[str, Any]) -> JudgeVerdict:
            async with sem:
                return await self.score(
                    prompt=item["prompt"],
                    candidate=item["candidate"],
                    reference=item.get("reference"),
                    tenant_id=item.get("tenant_id"),
                    acceptance_threshold=acceptance_threshold,
                )

        return await asyncio.gather(*(_one(i) for i in items))

    # ---------------------------------------------------------------------
    # Provider plumbing — lazy imports so this module imports without SDKs
    # ---------------------------------------------------------------------
    def _build_judge_prompt(
        self, prompt: str, candidate: str, reference: Optional[str],
    ) -> str:
        parts = [RUBRIC, f"\nPROMPT (transcript):\n{prompt[:6000]}"]
        if reference:
            parts.append(f"\nREFERENCE SUMMARY:\n{reference}")
        parts.append(f"\nCANDIDATE SUMMARY:\n{candidate}")
        return "\n\n".join(parts)

    async def _call_provider(self, body: str) -> tuple[str, float]:
        if self.provider == "anthropic":
            return await self._anthropic(body)
        if self.provider == "openai":
            return await self._openai(body)
        raise ValueError(f"Unknown provider: {self.provider}")

    async def _anthropic(self, body: str) -> tuple[str, float]:
        try:
            from anthropic import AsyncAnthropic  # noqa: PLC0415
        except ImportError as e:
            raise RuntimeError("`pip install anthropic` for the Anthropic judge") from e
        client = AsyncAnthropic()
        resp = await client.messages.create(
            model=self.model,
            max_tokens=600,
            messages=[{"role": "user", "content": body}],
        )
        text = resp.content[0].text
        # Sonnet 3.5 pricing — input + output tokens
        cost = (resp.usage.input_tokens * 3.0 + resp.usage.output_tokens * 15.0) / 1_000_000
        return text, cost

    async def _openai(self, body: str) -> tuple[str, float]:
        try:
            from openai import AsyncOpenAI  # noqa: PLC0415
        except ImportError as e:
            raise RuntimeError("`pip install openai` for the OpenAI judge") from e
        client = AsyncOpenAI()
        resp = await client.chat.completions.create(
            model=self.model,
            messages=[{"role": "user", "content": body}],
            max_tokens=600,
        )
        text = resp.choices[0].message.content
        # GPT-4o pricing — input + output tokens
        cost = (resp.usage.prompt_tokens * 5.0 + resp.usage.completion_tokens * 15.0) / 1_000_000
        return text, cost

    def _parse_response(self, raw: str, fallback_candidate: str) -> dict[str, Any]:
        """The judge is asked for JSON; tolerate fences and stray prose."""
        try:
            # Strip ```json fences if present
            cleaned = raw.strip()
            if cleaned.startswith("```"):
                cleaned = cleaned.split("```", 2)[1]
                if cleaned.lstrip().startswith("json"):
                    cleaned = cleaned.split("\n", 1)[1]
                cleaned = cleaned.rsplit("```", 1)[0]
            parsed = json.loads(cleaned.strip())
        except (json.JSONDecodeError, IndexError):
            log.warning("Judge returned non-JSON; defaulting to neutral verdict")
            parsed = {
                "overall": 0.5, "feedback": "judge response unparseable",
                "preferred_output": fallback_candidate,
            }
        # Normalize bounds
        for k in ("faithfulness", "completeness", "format", "style", "overall"):
            if k in parsed:
                parsed[k] = max(0.0, min(1.0, float(parsed[k])))
        return parsed


# ---------------------------------------------------------------------------
# Champion / challenger evaluator — gate promotion of new adapters
# ---------------------------------------------------------------------------
async def champion_challenger(
    eval_set: list[dict[str, Any]],
    champion_outputs: list[str],
    challenger_outputs: list[str],
    *,
    judge_provider: str = "anthropic",
    promote_threshold: float = 0.05,
) -> dict[str, Any]:
    """Score champion vs challenger on the same eval set; recommend promote.

    `eval_set[i]` is a dict like {prompt, reference}. Champion and challenger
    outputs are aligned by index.

    Returns a verdict dict:
      {
        "champion_score": float,
        "challenger_score": float,
        "delta": float,
        "promote": bool,
        "verdicts": [...],
      }
    """
    judge = LLMJudge(provider=judge_provider)
    champ_items = [
        {"prompt": e["prompt"], "candidate": c, "reference": e.get("reference")}
        for e, c in zip(eval_set, champion_outputs)
    ]
    chall_items = [
        {"prompt": e["prompt"], "candidate": c, "reference": e.get("reference")}
        for e, c in zip(eval_set, challenger_outputs)
    ]
    champ, chall = await asyncio.gather(
        judge.score_batch(champ_items),
        judge.score_batch(chall_items),
    )
    champ_mean = sum(v.overall_score for v in champ) / max(len(champ), 1)
    chall_mean = sum(v.overall_score for v in chall) / max(len(chall), 1)
    delta = chall_mean - champ_mean

    return {
        "champion_score": round(champ_mean, 4),
        "challenger_score": round(chall_mean, 4),
        "delta": round(delta, 4),
        "promote": delta >= promote_threshold,
        "n_eval_items": len(eval_set),
        "judge_id": judge.judge_id,
    }
