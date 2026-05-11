import asyncio

from app.llmservice import (
    EvaluationRequest,
    CandidateAnswer,
    call_llm_evaluate_candidate,
)
from app.main import get_vacancy_by_id


async def main():
    vacancy_id = "storekeeper_001"  # подставь реальный ID

    vacancy = get_vacancy_by_id(vacancy_id)
    if vacancy is None:
        print(f"Вакансия с id {vacancy_id} не найдена")
        return

    req = EvaluationRequest(
        vacancy_id=vacancy_id,
        candidate_name="Тестовый Кандидат",
        candidate_contact="test@example.com",
        interviewer="HR",
        interview_format="web",
        answers=[
            CandidateAnswer(
                question_block="Общие",
                question_text="Расскажите о себе",
                answer_text="5 лет опыта в разработке, Python/FastAPI...",
            )
        ],
    )

    result = await call_llm_evaluate_candidate(req, vacancy)
    print("OK, получили EvaluationResult:")
    print("score_total:", result.score_total)
    print("pass_threshold:", result.pass_threshold)
    print("threshold_result:", result.threshold_result)
    print("recommendation:", result.recommendation)
    print("summary:", result.summary)


if __name__ == "__main__":
    asyncio.run(main())