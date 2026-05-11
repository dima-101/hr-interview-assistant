import json
import uuid
from typing import Dict, List, Optional

import httpx
from pydantic import BaseModel, Field

from app.config import get_settings

import os
from openai import OpenAI

settings = get_settings()


class CandidateAnswer(BaseModel):
    question_block: str = ""
    question_text: str
    answer_text: str


class EvaluationRequest(BaseModel):
    vacancy_id: str
    candidate_name: str
    candidate_contact: Optional[str] = ""
    interviewer: Optional[str] = ""
    interview_format: Optional[str] = "web"
    answers: List[CandidateAnswer] = Field(default_factory=list)


class EvaluationResult(BaseModel):
    summary: str
    professional_experience_assessment: str
    required_requirements_confirmed: List[str] = Field(default_factory=list)
    required_requirements_partial: List[str] = Field(default_factory=list)
    required_requirements_not_confirmed: List[str] = Field(default_factory=list)
    preferred_strengths: List[str] = Field(default_factory=list)
    preferred_not_confirmed: List[str] = Field(default_factory=list)
    soft_skills_assessment: str
    strengths: List[str] = Field(default_factory=list)
    risks: List[str] = Field(default_factory=list)
    red_flags_found: List[str] = Field(default_factory=list)
    follow_up_checks: List[str] = Field(default_factory=list)
    score_by_dimension: Dict[str, int] = Field(default_factory=dict)
    score_total: int
    pass_threshold: int
    threshold_result: str
    recommendation: str
    recommendation_comment: str
    hiring_manager_note: Optional[str] = ""


class LLMError(Exception):
    pass


def get_pass_threshold(vacancy_profile: dict) -> int:
    rubric = vacancy_profile.get("evaluation_rubric") or {}
    raw_value = rubric.get("pass_threshold", 0)

    try:
        return int(raw_value)
    except (TypeError, ValueError):
        return 0


def normalize_threshold_result(score_total: int, pass_threshold: int) -> str:
    if score_total >= pass_threshold:
        return "pass"

    borderline_threshold = max(pass_threshold - 25, 0)
    if score_total >= borderline_threshold:
        return "borderline"

    return "fail"


def build_profile_for_llm(vacancy_profile: dict) -> dict:
    rubric = vacancy_profile.get("evaluation_rubric") or {}
    question_bank = vacancy_profile.get("question_bank") or {}

    return {
        "vacancy_id": vacancy_profile.get("vacancy_id", ""),
        "title": vacancy_profile.get("title", ""),
        "status": vacancy_profile.get("status", ""),
        "company_context": vacancy_profile.get("company_context", ""),
        "role_goal": vacancy_profile.get("role_goal", ""),
        "responsibilities": vacancy_profile.get("responsibilities", []),
        "required_requirements": (
            vacancy_profile.get("required_skills")
            or vacancy_profile.get("required_requirements")
            or []
        ),
        "preferred_requirements": (
            vacancy_profile.get("preferred_skills")
            or vacancy_profile.get("preferred_requirements")
            or []
        ),
        "key_soft_skills": vacancy_profile.get("key_soft_skills", []),
        "red_flags": vacancy_profile.get("red_flags", []),
        "question_bank": {
            "general": question_bank.get(
                "general",
                vacancy_profile.get("general_questions", []),
            ),
            "technical": question_bank.get(
                "technical",
                vacancy_profile.get("professional_questions", []),
            ),
            "behavioral": question_bank.get(
                "behavioral",
                vacancy_profile.get("behavioral_questions", []),
            ),
        },
        "evaluation_rubric": {
            "summary": rubric.get("summary", ""),
            "key_dimensions": rubric.get("key_dimensions", []),
            "pass_threshold": rubric.get(
                "pass_threshold",
                get_pass_threshold(vacancy_profile),
            ),
        },
    }


def build_system_prompt() -> str:
    return (
        "Ты HR hiring manager assistant. "
        "Нужно оценить кандидата строго по профилю вакансии и его ответам на интервью. "
        "Нельзя придумывать факты, опыт, навыки и достижения, которых нет в ответах. "
        "Если информации недостаточно, это нужно прямо отразить в оценке. "

        "Ты формируешь СТРОГО ОДИН JSON-объект с полями итоговой оценки кандидата. "
        "Никакого markdown, никаких комментариев до или после JSON. "

        "Обязательная структура JSON:\n"
        "{\n"
        '  "summary": "краткое резюме итоговой оценки (1–3 предложения)",\n'
        '  "professional_experience_assessment": "оценка опыта (2–4 предложения)",\n'
        '  "soft_skills_assessment": "оценка soft skills (2–4 предложения)",\n'
        '\n'
        '  "required_requirements_confirmed": ["строка", ...],\n'
        '  "required_requirements_partial": ["строка", ...],\n'
        '  "required_requirements_not_confirmed": ["строка", ...],\n'
        '\n'
        '  "preferred_strengths": ["строка", ...],\n'
        '  "preferred_not_confirmed": ["строка", ...],\n'
        '\n'
        '  "strengths": ["строка", ...],\n'
        '  "risks": ["строка", ...],\n'
        '  "red_flags_found": ["строка", ...],\n'
        '  "follow_up_checks": ["строка", ...],\n'
        '\n'
        '  "score_by_dimension": {\n'
        '    "experience": 0,\n'
        '    "hard_skills": 0,\n'
        '    "soft_skills": 0,\n'
        '    "motivation": 0\n'
        "  },\n"
        '  "score_total": 0,\n'
        '  "pass_threshold": 0,\n'
        '  "threshold_result": "pass",\n'
        '  "recommendation": "Рекомендовать",\n'
        '  "recommendation_comment": "пояснение (1–3 предложения)",\n'
        '  "hiring_manager_note": "краткая заметка для руководителя (может быть пустой строкой)"\n'
        "}\n"
        "\n"
        "ОЦЕНКА ПО БАЛЛАМ:\n"
        "1) Ты оцениваешь кандидата по четырём измерениям:\n"
        "- experience: профессиональный опыт и соответствие задачам роли,\n"
        "- hard_skills: владение техническими и профессиональными навыками,\n"
        "- soft_skills: коммуникация, командность, самостоятельность и ответственность,\n"
        "- motivation: мотивация, интерес к роли и компании.\n"
        "\n"
        "2) Для каждого измерения ставь целое число от 0 до 25:\n"
        "- 0–5: очень слабый уровень или почти нет данных,\n"
        "- 6–12: слабый, ниже ожидаемого,\n"
        "- 13–19: приемлемый уровень с заметными пробелами,\n"
        "- 20–25: сильный уровень, хорошо соответствует требованиям роли.\n"
        "\n"
        "3) Поле score_by_dimension всегда должно иметь вид:\n"
        "{\n"
        '  "experience": 0..25,\n'
        '  "hard_skills": 0..25,\n'
        '  "soft_skills": 0..25,\n'
        '  "motivation": 0..25\n'
        "}\n"
        "Нельзя пропускать ключи, ставить null или оставлять score_by_dimension пустым. "
        "Если данных мало — ставь низкие баллы (0–5) и объясни это в risks.\n"
        "\n"
        "4) Поле score_total вычисляется как сумма всех четырёх измерений:\n"
        "score_total = experience + hard_skills + soft_skills + motivation.\n"
        "\n"
        "5) Поле pass_threshold приходит во входных данных и задаёт порог прохождения.\n"
        "\n"
        "6) threshold_result:\n"
        "- pass, если score_total >= pass_threshold;\n"
        "- borderline, если score_total в диапазоне (pass_threshold - 10) до (pass_threshold - 1);\n"
        "- fail во всех остальных случаях.\n"
        "\n"
        "7) recommendation:\n"
        "- Рекомендовать при threshold_result = pass;\n"
        "- Рассмотреть после доп. проверки при threshold_result = borderline;\n"
        "- Не рекомендовать при threshold_result = fail.\n"
        "\n"
        "Верни только один JSON-объект по этой структуре, без markdown и лишнего текста."
        "\n"
        "ПРИМЕРЫ ЭТАЛОННЫХ ОТВЕТОВ (ОБРАЗЕЦ СТИЛЯ И БАЛЛОВ):\n"
        "\n"
        "Пример 1 — слабый кандидат, мало данных, результат fail:\n"
        "{\n"
        '  "summary": "Кандидат имеет общий опыт работы на складе, но представленных данных недостаточно для уверенной рекомендации. Требуется существенное уточнение опыта и навыков.",\n'
        '  "professional_experience_assessment": "Кандидат указывает опыт работы на складе, но не приводит подробного описания обязанностей, зон ответственности и достижений. Нельзя оценить глубину владения складскими процессами и системами учета.",\n'
        '  "soft_skills_assessment": "По предоставленным ответам оценить коммуникативные и поведенческие компетенции не представляется возможным. Примеры взаимодействия с коллегами, решения конфликтов и работы в условиях стресса отсутствуют.",\n'
        '\n'
        '  "required_requirements_confirmed": [],\n'
        '  "required_requirements_partial": ["Общий опыт работы на складе без детализации обязанностей."],\n'
        '  "required_requirements_not_confirmed": ["Владение учетными системами (1С, WMS).", "Опыт проведения инвентаризаций и работы с первичной документацией."],\n'
        '\n'
        '  "preferred_strengths": [],\n'
        '  "preferred_not_confirmed": ["Опыт работы в крупной компании.", "Опыт работы с адресным хранением и системами штрихкодирования."],\n'
        '\n'
        '  "strengths": ["Есть декларированный опыт работы на складе."],\n'
        '  "risks": ["Отсутствует детализация функционала и достижений.", "Отсутствуют данные о владении учетными системами и точности работы."],\n'
        '  "red_flags_found": [],\n'
        '  "follow_up_checks": ["Запросить подробное описание обязанностей на предыдущих местах работы.", "Уточнить опыт работы с 1С, WMS и инвентаризациями.", "Провести дополнительное интервью с практическими вопросами по складским процессам."],\n'
        '\n'
        '  "score_by_dimension": {\n'
        '    "experience": 10,\n'
        '    "hard_skills": 5,\n'
        '    "soft_skills": 5,\n'
        '    "motivation": 5\n'
        "  },\n"
        '  "score_total": 25,\n'
        '  "pass_threshold": 70,\n'
        '  "threshold_result": "fail",\n'
        '  "recommendation": "Не рекомендовать",\n'
        '  "recommendation_comment": "По текущим данным кандидат существенно не дотягивает до порога прохождения, критические требования не подтверждены. Возможность пересмотра возможна только после детальной дооценки опыта и навыков.",\n'
        '  "hiring_manager_note": "Кандидат может рассматриваться только при острой нехватке персонала и готовности вложиться в дообучение. На текущем этапе — не приоритет." \n'
        "}\n"
        "\n"
        "Пример 2 — сильный кандидат, хороший матч, результат pass:\n"
        "{\n"
        '  "summary": "Кандидат имеет устойчивый опыт работы на складе (5+ лет), уверенно владеет учетными системами и демонстрирует ответственное отношение к точности и срокам. В целом соответствует требованиям позиции.",\n'
        '  "professional_experience_assessment": "Кандидат последовательно описывает опыт работы на нескольких складах с ростом зоны ответственности: от линейного кладовщика до старшего смены. Участвовал в инвентаризациях, отвечает за приемку, отгрузку, размещение и адресное хранение. Приводит конкретные показатели по снижению ошибок и ускорению операций.",\n'
        '  "soft_skills_assessment": "Описание взаимодействия с коллегами и руководителем указывает на адекватный уровень коммуникации и ответственности. Кандидат приводит примеры решения конфликтных ситуаций, признает ошибки и описывает, как их исправляет.",\n'
        '\n'
        '  "required_requirements_confirmed": ["Опыт работы кладовщиком 3+ года.", "Владение 1С и WMS-системами.", "Участие в инвентаризациях и работе с первичной документацией."],\n'
        '  "required_requirements_partial": [],\n'
        '  "required_requirements_not_confirmed": [],\n'
        '\n'
        '  "preferred_strengths": ["Опыт работы на складах с адресным хранением.", "Опыт наставничества новых сотрудников."],\n'
        '  "preferred_not_confirmed": ["Опыт работы в вашей отрасли (можно добрать в процессе адаптации)."],\n'
        '\n'
        '  "strengths": ["Устойчивый релевантный опыт 5+ лет.", "Уверенное владение учетными системами и понимание складских процессов.", "Ответственный подход и ориентация на точность."],\n'
        '  "risks": ["Не работал в вашей отрасли — потребуется период адаптации.", "Важно уточнить готовность к сменному графику/переработкам при пиках нагрузки."],\n'
        '  "red_flags_found": [],\n'
        '  "follow_up_checks": ["Проверить рекомендации с последнего места работы.", "Уточнить ожидания по графику и уровню нагрузки."],\n'
        '\n'
        '  "score_by_dimension": {\n'
        '    "experience": 22,\n'
        '    "hard_skills": 21,\n'
        '    "soft_skills": 18,\n'
        '    "motivation": 19\n'
        "  },\n"
        '  "score_total": 80,\n'
        '  "pass_threshold": 70,\n'
        '  "threshold_result": "pass",\n'
        '  "recommendation": "Рекомендовать",\n'
        '  "recommendation_comment": "Кандидат в целом соответствует требованиям роли, подтвержден релевантный опыт и владение ключевыми системами. Рекомендуется к дальнейшему рассмотрению и выходу на финальные этапы.",\n'
        '  "hiring_manager_note": "Хороший рабочий кандидат для склада. Финальное решение можно принимать после проверки рекомендаций и уточнения деталей графика." \n'
        "}\n"
        "\n"
        "При формировании ответа ориентируйся на эту структуру, стиль формулировок и логику расстановки баллов и рекомендаций, но всегда опирайся на текущие данные по вакансии и ответам конкретного кандидата."
    )


def build_user_prompt(request: EvaluationRequest, vacancy_profile: dict) -> str:
    normalized_profile = build_profile_for_llm(vacancy_profile)

    candidate_data = {
        "vacancy_id": request.vacancy_id,
        "candidate_name": request.candidate_name,
        "candidate_contact": request.candidate_contact,
        "interviewer": request.interviewer,
        "interview_format": request.interview_format,
    }

    answers_data = [
        {
            "question_block": item.question_block,
            "question_text": item.question_text,
            "answer_text": item.answer_text,
        }
        for item in request.answers
        if (item.answer_text or "").strip()
    ]

    profile_json = json.dumps(normalized_profile, ensure_ascii=False, indent=2)
    candidate_json = json.dumps(candidate_data, ensure_ascii=False, indent=2)
    answers_json = json.dumps(answers_data, ensure_ascii=False, indent=2)

    return f"""
Профиль вакансии:
{profile_json}

Данные кандидата:
{candidate_json}

Ответы кандидата:
{answers_json}

Твоя задача — строго по этим данным оценить кандидата по профилю вакансии и вернуть JSON структуры EvaluationResult.

Верни JSON со следующими полями:
summary
professional_experience_assessment
required_requirements_confirmed
required_requirements_partial
required_requirements_not_confirmed
preferred_strengths
preferred_not_confirmed
soft_skills_assessment
strengths
risks
red_flags_found
follow_up_checks
score_by_dimension
score_total
pass_threshold
threshold_result
recommendation
recommendation_comment
hiring_manager_note

Как использовать профиль вакансии:
- Используй normalized_profile как единственный источник требований вакансии.
- В normalized_profile.evaluation_rubric.key_dimensions перечислены измерения оценки.
- У каждого измерения есть name, description и weight.
- weight — это максимальный допустимый балл по измерению.
- pass_threshold возьми из normalized_profile.evaluation_rubric.pass_threshold.
- required_requirements и preferred_requirements оценивай строго по профилю вакансии.
- red_flags учитывай только если они реально следуют из ответов кандидата.

Правила оценки кандидата:
1. Оценивай кандидата только по данным профиля вакансии и ответам интервью.
2. Не придумывай опыт, навыки, достижения, мотивацию или soft skills, которых нет в данных.
3. Если информации мало, прямо отражай это в summary, risks, follow_up_checks и в низких/частичных баллах.
4. required_requirements_confirmed — только явно подтвержденные обязательные требования.
5. required_requirements_partial — обязательные требования, подтвержденные частично.
6. required_requirements_not_confirmed — обязательные требования, по которым нет подтверждения.
7. preferred_strengths — подтвержденные желательные требования.
8. preferred_not_confirmed — желательные требования, по которым нет подтверждения.
9. red_flags_found — только реальные red flags, если они действительно следуют из ответов.
10. follow_up_checks — конкретные пункты, что нужно проверить дополнительно.

Правила заполнения score_by_dimension:
11. score_by_dimension — это словарь, где:
- ключ = точное имя измерения из normalized_profile.evaluation_rubric.key_dimensions,
- значение = целочисленный балл по этому измерению.
12. Для КАЖДОГО измерения из key_dimensions обязательно верни балл.
13. Нельзя пропускать измерения.
14. Нельзя ставить null.
15. Нельзя оставлять score_by_dimension пустым, если в профиле есть key_dimensions.
16. Балл по измерению не должен превышать weight этого измерения.
17. Балл 0 допустим только если по ответам вообще нет информации по этому измерению.
18. Если есть хотя бы частичное подтверждение, ставь частичный балл, а не 0.
19. Если опыт и навыки хорошо соответствуют измерению, ставь баллы ближе к максимальному weight.
20. score_total должен быть равен сумме всех значений из score_by_dimension.
21. Не возвращай score_total = 0, если по ответам видно хотя бы частичное соответствие хотя бы одному измерению.

Правила по статусу и рекомендации:
22. pass_threshold возьми из evaluation_rubric.pass_threshold.
23. threshold_result должен быть одним из значений: "pass", "borderline", "fail".
24. Если score_total >= pass_threshold, ставь threshold_result = "pass".
25. Если score_total меньше pass_threshold, но находится в диапазоне от (pass_threshold - 10) до (pass_threshold - 1), ставь threshold_result = "borderline".
26. Во всех остальных случаях ставь threshold_result = "fail".
27. recommendation должно быть одним из значений:
- "Рекомендовать"
- "Рассмотреть после доп. проверки"
- "Не рекомендовать"
28. recommendation должно соответствовать threshold_result:
- pass -> "Рекомендовать"
- borderline -> "Рассмотреть после доп. проверки"
- fail -> "Не рекомендовать"

Рекомендации по текстовым полям:
29. summary — краткое резюме итоговой оценки кандидата.
30. professional_experience_assessment — отдельный абзац про профессиональный опыт кандидата.
31. soft_skills_assessment — оценка поведения и soft skills.
32. strengths — список конкретных сильных сторон кандидата.
33. risks — список конкретных рисков, пробелов и ограничений.
34. recommendation_comment — короткий вывод для HR/руководителя.
35. hiring_manager_note — короткая практическая заметка для нанимающего руководителя.
36. Если данных мало, обязательно укажи это в risks и follow_up_checks.

Очень важно:
- Верни только один JSON-объект верхнего уровня.
- Без markdown.
- Без комментариев.
- Без пояснений до JSON и после JSON.
- Заполни все поля структуры EvaluationResult.
""".strip()


async def call_openai(request: EvaluationRequest, vacancy_profile: dict) -> dict:
    if not settings.openai_api_key:
        raise LLMError("Не найден OPENAI_API_KEY в .env")

    payload = {
        "model": settings.openai_model,
        "messages": [
            {
                "role": "system",
                "content": build_system_prompt(),
            },
            {
                "role": "user",
                "content": build_user_prompt(request, vacancy_profile),
            },
        ],
        "response_format": {"type": "json_object"},
        "temperature": 0.2,
    }

    timeout = settings.llm_timeout_seconds or 60

    async with httpx.AsyncClient(
        base_url=settings.openai_base_url,
        timeout=timeout,
    ) as client:
        response = await client.post(
            "/chat/completions",
            headers={
                "Authorization": f"Bearer {settings.openai_api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
        )

    if response.status_code >= 400:
        raise LLMError(f"OpenAI API error {response.status_code}: {response.text}")

    try:
        data = response.json()
    except Exception as e:
        raise LLMError(f"Не удалось разобрать ответ OpenAI как JSON: {e}") from e

    try:
        content = data["choices"][0]["message"]["content"]
    except Exception as e:
        raise LLMError(f"Неожиданный формат ответа OpenAI: {data}") from e

    return extract_json_object(content)


async def call_gigachat(request: EvaluationRequest, vacancy_profile: dict) -> dict:
    if not settings.gigachat_api_key:
        raise LLMError("Не найден GIGACHAT_API_KEY в .env")

    auth_url = "https://ngw.devices.sberbank.ru:9443/api/v2/oauth"
    auth_headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "Accept": "application/json",
        "RqUID": str(uuid.uuid4()),
        "Authorization": f"Basic {settings.gigachat_api_key}",
    }
    auth_data = {"scope": "GIGACHAT_API_PERS"}

    timeout = settings.llm_timeout_seconds or 60

    async with httpx.AsyncClient(verify=False, timeout=timeout) as client:
        auth_resp = await client.post(auth_url, headers=auth_headers, data=auth_data)

    if auth_resp.status_code >= 400:
        raise LLMError(f"GigaChat OAuth error {auth_resp.status_code}: {auth_resp.text}")

    try:
        auth_json = auth_resp.json()
        access_token = auth_json["access_token"]
    except Exception as e:
        raise LLMError(
            f"Не удалось получить access_token GigaChat: {e}, ответ: {auth_resp.text}"
        ) from e

    chat_url = "https://gigachat.devices.sberbank.ru/api/v1/chat/completions"
    chat_headers = {
        "Accept": "application/json",
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": settings.gigachat_model or "GigaChat-Pro",
        "messages": [
            {"role": "system", "content": build_system_prompt()},
            {"role": "user", "content": build_user_prompt(request, vacancy_profile)},
        ],
        "temperature": 0.2,
    }

    async with httpx.AsyncClient(verify=False, timeout=timeout) as client:
        chat_resp = await client.post(chat_url, headers=chat_headers, json=payload)

    if chat_resp.status_code >= 400:
        raise LLMError(f"GigaChat API error {chat_resp.status_code}: {chat_resp.text}")

    try:
        data = chat_resp.json()
    except Exception as e:
        raise LLMError(f"Не удалось разобрать ответ GigaChat как JSON: {e}") from e

    try:
        content = data["choices"][0]["message"]["content"]
    except Exception as e:
        raise LLMError(f"Неожиданный формат ответа GigaChat: {data}") from e

    return extract_json_object(content)


def build_stub_response(request: EvaluationRequest, vacancy_profile: dict) -> dict:
    pass_threshold = get_pass_threshold(vacancy_profile)

    answered_count = len(
        [item for item in request.answers if (item.answer_text or "").strip()]
    )

    score_by_dimension = {
        "experience": min(answered_count * 10, 30),
        "hard_skills": min(answered_count * 10, 30),
        "soft_skills": min(answered_count * 5, 20),
        "motivation": min(answered_count * 5, 20),
    }
    score_total = sum(score_by_dimension.values())
    threshold_result = normalize_threshold_result(score_total, pass_threshold)

    required_requirements = (
        vacancy_profile.get("required_skills")
        or vacancy_profile.get("required_requirements")
        or []
    )
    preferred_requirements = (
        vacancy_profile.get("preferred_skills")
        or vacancy_profile.get("preferred_requirements")
        or []
    )

    requirements_for_review = required_requirements[:3]
    requirements_not_confirmed = required_requirements[3:]

    preferred_for_review = preferred_requirements[:3]
    preferred_not_confirmed = preferred_requirements[3:]

    if threshold_result == "pass":
        recommendation = "Рекомендовать"
        recommendation_comment = (
            "Кандидат набирает проходной балл по первичной stub-оценке. "
            "Есть основания переводить его на следующий этап при условии дополнительной проверки."
        )
    elif threshold_result == "borderline":
        recommendation = "Рассмотреть после доп. проверки"
        recommendation_comment = (
            "Часть критериев выглядит потенциально подходящей, но данных недостаточно для уверенного решения. "
            "Нужен дополнительный этап проверки с углублёнными вопросами."
        )
    else:
        recommendation = "Не рекомендовать"
        recommendation_comment = (
            "По текущим данным кандидат не набирает проходной балл. "
            "Подтверждений по обязательным критериям недостаточно, требуется дополнительная проверка."
        )

    return {
        "summary": (
            f"Кандидат {request.candidate_name} прошел первичную оценку по вакансии "
            f"{request.vacancy_id}. Получено содержательных ответов: {answered_count}."
        ),
        "professional_experience_assessment": (
            "Оценка сформирована в stub-режиме без реального semantic-анализа ответов. "
            "Для финального решения требуется LLM-проверка или ручная оценка."
        ),
        "required_requirements_confirmed": [],
        "required_requirements_partial": requirements_for_review,
        "required_requirements_not_confirmed": requirements_not_confirmed,
        "preferred_strengths": [],
        "preferred_not_confirmed": preferred_for_review + preferred_not_confirmed,
        "soft_skills_assessment": (
            "Софт-скиллы требуют дополнительной оценки на следующем этапе интервью."
        ),
        "strengths": [
            "Есть структурированные ответы кандидата",
            "Данные пригодны для первичной автоматизированной оценки",
        ],
        "risks": [
            "Stub-режим не анализирует глубину и качество ответов",
            "Нужна ручная или LLM-проверка обязательных требований",
        ],
        "red_flags_found": [],
        "follow_up_checks": [
            "Проверить релевантный опыт по ключевым требованиям",
            "Уточнить практический уровень владения основными навыками",
        ],
        "score_by_dimension": score_by_dimension,
        "score_total": score_total,
        "pass_threshold": pass_threshold,
        "threshold_result": threshold_result,
        "recommendation": recommendation,
        "recommendation_comment": recommendation_comment,
        "hiring_manager_note": (
            "MVP stub response: требования не анализировались семантически, "
            "подтверждение навыков требует ручной или LLM-проверки."
        ),
    }


def _ensure_list(value):
    if value is None:
        return []
    if isinstance(value, list):
        return [str(x).strip() for x in value if str(x).strip()]
    return [str(value).strip()] if str(value).strip() else []


def _safe_int(value, default=0, field_name: str | None = None):
    try:
        if value is None or value == "":
            return default
        return int(value)
    except Exception:
        # можно логировать с полем, если нужно
        return default


def _normalize_score_map(value):
    if not isinstance(value, dict):
        return {}
    result = {}
    for k, v in value.items():
        key = str(k).strip()
        if not key:
            continue
        result[key] = _safe_int(v, 0)
    return result


def _heuristic_score_from_text(raw_result: dict, vacancy_profile: dict) -> dict:
    summary = (raw_result.get("summary") or "").lower()
    strengths_text = " ".join(raw_result.get("strengths") or []).lower()
    text_blob = summary + " " + strengths_text

    has_warehouse_exp = "склад" in text_blob or "кладовщ" in text_blob
    has_years = any(
        x in text_blob for x in ["5 лет", "5-лет", "пять лет", "3 года", "3-лет", "три года"]
    )
    has_inventory = "инвентаризац" in text_blob
    has_accounting = "учет" in text_blob or "учёт" in text_blob
    has_1c = "1с" in text_blob
    has_excel = "excel" in text_blob

    rubric = vacancy_profile.get("evaluation_rubric") or {}
    dims = rubric.get("key_dimensions") or []

    if not dims:
        dims = [
            {"name": "Опыт работы и понимание склада", "weight": 30},
            {"name": "Работа с 1С и Excel, учет и инвентаризации", "weight": 30},
            {"name": "Точность и ответственность при работе с ТМЦ", "weight": 20},
            {"name": "Организация работы и поведение в стрессовых ситуациях", "weight": 20},
        ]

    score_map: dict[str, int] = {}

    for dim in dims:
        name = str(dim.get("name") or "").strip()
        weight = _safe_int(
            dim.get("weight"),
            default=0,
            field_name=f"weight for dimension {name}",
        )


def extract_json_object(text: str) -> dict:
    text = (text or "").strip()

    # Уберём markdown-обёртку ```json ... ```
    if text.startswith("```"):
        stripped = text.strip("`").strip()
        if stripped.lower().startswith("json"):
            text = stripped[4:].strip()
        else:
            text = stripped

    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise LLMError(f"Ответ модели не содержит JSON-объект: {text}")

    json_text = text[start : end + 1]
    try:
        return json.loads(json_text)
    except Exception as e:
        raise LLMError(f"Не удалось разобрать JSON модели: {e}; content={text}") from e


async def call_llm_evaluate_candidate(
    request: EvaluationRequest,
    vacancy_profile: dict,
) -> EvaluationResult:
    provider = (settings.llm_provider or "").strip().lower()

    if provider == "openai":
        raw_result = await call_openai(request, vacancy_profile)
    elif provider == "gigachat":
        raw_result = await call_gigachat(request, vacancy_profile)
    elif provider == "stub":
        raw_result = build_stub_response(request, vacancy_profile)
    else:
        raise LLMError(f"Неподдерживаемый LLM_PROVIDER: {settings.llm_provider}")

    print("\n=== RAW LLM RESULT START ===")
    print(json.dumps(raw_result, ensure_ascii=False, indent=2))
    print("=== RAW LLM RESULT END ===\n")

    if not isinstance(raw_result, dict):
        raise LLMError(f"LLM вернул неожиданный тип результата: {type(raw_result)}")

    # Нормализуем списки
    list_fields = [
        "required_requirements_confirmed",
        "required_requirements_partial",
        "required_requirements_not_confirmed",
        "preferred_strengths",
        "preferred_not_confirmed",
        "strengths",
        "risks",
        "red_flags_found",
        "follow_up_checks",
    ]
    for field in list_fields:
        raw_result[field] = _ensure_list(raw_result.get(field))

    # Нормализуем score_by_dimension, не даём ему остаться None
    raw_score_map = raw_result.get("score_by_dimension")
    normalized_score_map = _normalize_score_map(raw_score_map)

    if not isinstance(normalized_score_map, dict):
        normalized_score_map = {}

    raw_result["score_by_dimension"] = normalized_score_map

    # Если LLM не вернул карту — пробуем эвристику
    if not raw_result["score_by_dimension"]:
        raw_result["score_by_dimension"] = _heuristic_score_from_text(
            raw_result,
            vacancy_profile,
        )

    # Порог и итоговый балл
    pass_threshold = get_pass_threshold(vacancy_profile)
    raw_result["pass_threshold"] = _safe_int(
        raw_result.get("pass_threshold"),
        pass_threshold,
        field_name="pass_threshold",
    )

    if raw_result.get("score_total") in (None, ""):
        if raw_result["score_by_dimension"]:
            raw_result["score_total"] = sum(raw_result["score_by_dimension"].values())
        else:
            raw_result["score_total"] = 0
    else:
        raw_result["score_total"] = _safe_int(
            raw_result.get("score_total"),
            0,
            field_name="score_total",
        )

    if not raw_result.get("threshold_result"):
        raw_result["threshold_result"] = normalize_threshold_result(
            raw_result["score_total"],
            raw_result["pass_threshold"],
        )

    # Строковые поля
    raw_result["summary"] = str(raw_result.get("summary") or "").strip()
    raw_result["professional_experience_assessment"] = str(
        raw_result.get("professional_experience_assessment") or ""
    ).strip()
    raw_result["soft_skills_assessment"] = str(
        raw_result.get("soft_skills_assessment") or ""
    ).strip()
    raw_result["recommendation"] = str(raw_result.get("recommendation") or "").strip()
    raw_result["recommendation_comment"] = str(
        raw_result.get("recommendation_comment") or ""
    ).strip()
    raw_result["hiring_manager_note"] = str(
        raw_result.get("hiring_manager_note") or ""
    ).strip()

    if not raw_result["summary"]:
        raw_result["summary"] = (
            "Недостаточно данных для уверенной итоговой оценки кандидата."
        )

    if not raw_result["professional_experience_assessment"]:
        raw_result["professional_experience_assessment"] = (
            "Профессиональный опыт кандидата подтвержден частично или описан недостаточно подробно."
        )

    if not raw_result["soft_skills_assessment"]:
        raw_result["soft_skills_assessment"] = (
            "По текущим ответам soft skills кандидата можно оценить только предварительно."
        )

    if not raw_result["recommendation"]:
        if raw_result["threshold_result"] == "pass":
            raw_result["recommendation"] = "Рекомендовать"
        elif raw_result["threshold_result"] == "borderline":
            raw_result["recommendation"] = "Рассмотреть после доп. проверки"
        else:
            raw_result["recommendation"] = "Не рекомендовать"

    if not raw_result["recommendation_comment"]:
        raw_result["recommendation_comment"] = (
            "Рекомендация сформирована автоматически на основе профиля вакансии и ответов кандидата."
        )

    # Финальная страховка: в модель всегда идёт dict
    if not isinstance(raw_result.get("score_by_dimension"), dict):
        raw_result["score_by_dimension"] = {}

    print("\n=== FINAL RAW RESULT BEFORE PYDANTIC ===")
    print("score_by_dimension =", raw_result.get("score_by_dimension"))
    print("type(score_by_dimension) =", type(raw_result.get("score_by_dimension")))
    print(json.dumps(raw_result, ensure_ascii=False, indent=2))
    print("=== END FINAL RAW RESULT ===\n")

    try:
        return EvaluationResult(**raw_result)
    except Exception as e:
        raise LLMError(f"Не удалось провалидировать EvaluationResult: {e}") from e