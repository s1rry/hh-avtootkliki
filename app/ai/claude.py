import json
import re
from pathlib import Path

import httpx
import structlog

try:
    import anthropic
except ImportError:  # self-host на groq может не ставить anthropic
    anthropic = None

from app.config import settings
from app.ai.prompts import (
    SYSTEM_VACANCY_ANALYZER,
    SYSTEM_COVER_LETTER,
    SYSTEM_REPLY_GENERATOR,
    SYSTEM_SENTIMENT_ANALYZER,
)

log = structlog.get_logger()

MODEL = "claude-sonnet-4-6"
# Модель для прохождения тестов вакансий (ответы на вопросы/тесты работодателя)
TEST_MODEL = "claude-haiku-4-5"
AI_STATE_FILE = Path("data/ai_state.json")


class ClaudeAI:
    def __init__(self):
        self.primary = None
        self.fallback = None
        # Anthropic-клиенты создаём только если выбран этот провайдер.
        if settings.ai_provider == "anthropic" and anthropic is not None:
            primary_kwargs = {"api_key": settings.anthropic_api_key}
            if settings.anthropic_base_url:
                primary_kwargs["base_url"] = settings.anthropic_base_url
            self.primary = anthropic.AsyncAnthropic(**primary_kwargs)

            if settings.anthropic_fallback_api_key:
                fb_kwargs = {"api_key": settings.anthropic_fallback_api_key}
                if settings.anthropic_fallback_base_url:
                    fb_kwargs["base_url"] = settings.anthropic_fallback_base_url
                self.fallback = anthropic.AsyncAnthropic(**fb_kwargs)

        # Persistent flag — once primary is exhausted we stick to fallback
        self.use_fallback = self._load_use_fallback()

    def _load_use_fallback(self) -> bool:
        try:
            if AI_STATE_FILE.exists():
                return bool(json.loads(AI_STATE_FILE.read_text()).get("use_fallback", False))
        except Exception:
            pass
        return False

    def _save_use_fallback(self):
        try:
            AI_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
            AI_STATE_FILE.write_text(json.dumps({"use_fallback": self.use_fallback}))
        except Exception as e:
            log.warning("ai_state_save_error", error=str(e))

    def reset_fallback(self):
        """Manual reset — go back to primary after topping it up."""
        self.use_fallback = False
        self._save_use_fallback()
        log.info("ai_fallback_reset")

    def _extract_text(self, response) -> str:
        """Pull text from response.content[], skipping empty/thinking blocks.
        TonWave with low max_tokens can return content=[] if all budget
        was spent on internal thinking — we'd previously crash with IndexError.
        """
        try:
            blocks = list(response.content or [])
        except Exception:
            blocks = []
        for b in blocks:
            text = getattr(b, "text", None)
            if text:
                return text
        return ""

    async def _call_openai_compatible(self, system: str, user_message: str, max_tokens: int) -> tuple[str, int, int]:
        """Вызов любого OpenAI-совместимого эндпоинта (OpenRouter/Cerebras/Mistral/…)."""
        headers = {
            "Authorization": f"Bearer {settings.ai_api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": settings.ai_model,
            "max_tokens": max_tokens,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user_message},
            ],
        }
        async with httpx.AsyncClient(timeout=60) as c:
            r = await c.post(f"{settings.ai_base_url}/chat/completions", headers=headers, json=payload)
        r.raise_for_status()
        d = r.json()
        text = (d.get("choices") or [{}])[0].get("message", {}).get("content", "") or ""
        usage = d.get("usage") or {}
        return text, usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0)

    async def _call(self, system: str, user_message: str, max_tokens: int = 1024, model: str | None = None) -> tuple[str, int, int]:
        # Minimum sane budget — small max_tokens makes models return empty content
        if max_tokens < 800:
            max_tokens = 800

        # OpenAI-совместимый провайдер (по умолчанию для облака/self-host).
        if settings.ai_provider != "anthropic":
            return await self._call_openai_compatible(system, user_message, max_tokens)

        # Permanent fallback: if primary was exhausted before, go straight to fallback.
        if self.use_fallback and self.fallback:
            response = await self.fallback.messages.create(
                model=model or MODEL, max_tokens=max_tokens, system=system,
                messages=[{"role": "user", "content": user_message}],
            )
            text = self._extract_text(response)
            return text, response.usage.input_tokens, response.usage.output_tokens

        try:
            response = await self.primary.messages.create(
                model=model or MODEL, max_tokens=max_tokens, system=system,
                messages=[{"role": "user", "content": user_message}],
            )
            text = self._extract_text(response)
            return text, response.usage.input_tokens, response.usage.output_tokens
        except Exception as e:
            err_str = str(e)
            is_quota = "insufficient_quota" in err_str or "billing" in err_str.lower() or "402" in err_str
            if is_quota and self.fallback:
                log.warning("ai_quota_exhausted_switching_permanently")
                self.use_fallback = True
                self._save_use_fallback()
                response = await self.fallback.messages.create(
                    model=model or MODEL, max_tokens=max_tokens, system=system,
                    messages=[{"role": "user", "content": user_message}],
                )
                text = self._extract_text(response)
                return text, response.usage.input_tokens, response.usage.output_tokens
            raise

    async def analyze_vacancy(self, vacancy_title: str, vacancy_description: str, skills: str = "") -> dict:
        system = SYSTEM_VACANCY_ANALYZER.format(
            resume=settings.resume_text,
            salary_min=settings.desired_salary_min,
            salary_max=settings.desired_salary_max,
        )
        user_msg = f"""Вакансия: {vacancy_title}

Описание:
{vacancy_description}

Навыки: {skills}"""

        text, inp_tok, out_tok = await self._call(system, user_msg)
        log.info("ai_vacancy_analyzed", title=vacancy_title[:60], tokens=inp_tok + out_tok)

        try:
            clean = text.strip()
            if clean.startswith("```"):
                clean = clean.split("\n", 1)[1].rsplit("```", 1)[0]
            result = json.loads(clean)
            result["_input_tokens"] = inp_tok
            result["_output_tokens"] = out_tok
            return result
        except json.JSONDecodeError:
            log.error("ai_json_parse_error", raw=text[:200])
            return {
                "score": 0,
                "reason": "Ошибка парсинга ответа AI",
                "is_relevant": False,
                "seniority": "unknown",
                "red_flags": [],
                "stack_match": 0,
                "_input_tokens": inp_tok,
                "_output_tokens": out_tok,
            }

    async def generate_cover_letter(self, vacancy_title: str, vacancy_description: str, company_name: str = "", resume: str | None = None, custom_prompt: str | None = None) -> tuple[str, int, int]:
        resume_text = resume or settings.resume_text
        if custom_prompt:
            system = f"{custom_prompt}\n\nПрофиль кандидата:\n{resume_text}"
        else:
            system = SYSTEM_COVER_LETTER.format(resume=resume_text)
        user_msg = f"""Напиши сопроводительное письмо для вакансии:

Компания: {company_name}
Позиция: {vacancy_title}
Описание:
{vacancy_description}"""

        text, inp_tok, out_tok = await self._call(system, user_msg, max_tokens=512)
        log.info("ai_cover_letter_generated", title=vacancy_title[:60])
        return text.strip(), inp_tok, out_tok

    async def score_vacancy(self, vacancy_title: str, vacancy_description: str, resume: str) -> int:
        """Оценка соответствия вакансии резюме, 0–100. При ошибке — 100
        (не блокируем отклик, если ИИ недоступен)."""
        system = (
            "Ты помощник по поиску работы. Оцени, насколько вакансия подходит "
            "кандидату по его резюме. Верни ТОЛЬКО число от 0 до 100 — процент "
            "соответствия (навыки, роль, уровень). Без слов, без пояснений.\n\n"
            f"Резюме кандидата:\n{resume[:4000]}"
        )
        user_msg = f"Вакансия: {vacancy_title}\n\nОписание:\n{(vacancy_description or '')[:2500]}"
        try:
            text, _, _ = await self._call(system, user_msg, max_tokens=800)
        except Exception as e:
            log.warning("ai_score_failed", error=str(e))
            return 100
        m = re.search(r"\d{1,3}", text or "")
        if not m:
            return 100
        return max(0, min(100, int(m.group(0))))

    async def generate_reply(self, recruiter_message: str, vacancy_context: str = "", platform: str = "") -> tuple[str, int, int]:
        platform_name = {"hh": "hh.ru", "habr": "Хабр Карьера", "avito": "Авито"}.get(platform, platform or "сайт вакансий")
        system = SYSTEM_REPLY_GENERATOR.format(
            resume=settings.resume_text,
            salary_min=settings.desired_salary_min,
            salary_max=settings.desired_salary_max,
            platform=platform_name,
        )
        user_msg = f"""Сообщение рекрутера:
{recruiter_message}

Контекст вакансии:
{vacancy_context}"""

        text, inp_tok, out_tok = await self._call(system, user_msg, max_tokens=512)
        log.info("ai_reply_generated")
        return text.strip(), inp_tok, out_tok

    async def analyze_sentiment(self, message: str) -> dict:
        text, _, _ = await self._call(SYSTEM_SENTIMENT_ANALYZER, message, max_tokens=256)
        try:
            clean = text.strip()
            if clean.startswith("```"):
                clean = clean.split("\n", 1)[1].rsplit("```", 1)[0]
            return json.loads(clean)
        except json.JSONDecodeError:
            return {"sentiment": "neutral", "intent": "info", "urgency": "low", "summary": message[:100]}


claude_ai = ClaudeAI()
