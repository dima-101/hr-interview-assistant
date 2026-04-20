# HR Interview Assistant

**HR Interview Assistant** — учебный проект внутреннего HR‑сервиса для проведения структурированных собеседований, просмотра профилей вакансий и формирования отчётов по кандидатам.

Проект подготовлен как для учебной сдачи, так и для последующего размещения на сервере предприятия.

## О проекте

Сервис решает следующие задачи:

- показывает список вакансий и карточки должностей;
- позволяет проводить интервью по заранее подготовленным профилям;
- использует набор вопросов по конкретной вакансии;
- формирует и сохраняет отчёты по результатам интервью;
- предоставляет защищённую HR‑панель для внутренней работы.

## Структура репозитория

Репозиторий организован следующим образом:

```text
hr-interview-assistant/
│   README.md
│   .gitignore
│   candidate_evaluation_template.md
│   company_knowledge_base.md
│   demo_scenarios.md
│   project_notes_for_submission.docx
│   project_notes_for_submission.md
│   system_prompt_core.md
│
├── job_profiles/
│   ├── sales_soft_client_001.md
│   ├── info_security_specialist_001.md
│   └── design_engineer_rkd_001.md
│
└── hr-assistant-backend/
    │   requirements.txt
    │   .env.example
    │   README_PROJECT_CONCEPT.md
    │   CHECKLIST_PRODUCTION.md
    │
    ├── app/
    │   ├── main.py
    │   ├── config.py
    │   ├── templates/
    │   ├── static/
    │   ├── data/
    │   └── logs/
    │
    └── job_profiles/
        └── … (копии профилей для backend)
```

Основная рабочая backend‑часть проекта находится в папке `hr-assistant-backend/` — там расположены код FastAPI‑приложения, шаблоны, стили, статические файлы, профили вакансий и конфигурация окружения.

## Что реализовано

На текущем этапе реализованы:

- главная страница `/`;
- страница `/about`;
- список вакансий `/vacancies`;
- карточки вакансий `/vacancies/{slug}`;
- страница интервью `/interview?vacancyid=...`;
- тестовая страница отчёта `/report`;
- сохранение отчётов в Markdown;
- защищённый HR‑вход `/hr/login`;
- HR‑панель `/hr`;
- архив отчётов `/reports`.

## Основной стек

- Python 3.11+
- FastAPI
- Jinja2 Templates
- HTML / CSS / JavaScript
- python-dotenv
- python-multipart
- SessionMiddleware

## Как запускать проект локально

Все команды запуска выполняются **внутри папки `hr-assistant-backend`**.

### Windows PowerShell

```powershell
cd C:\temp\hr-interview-assistant\hr-assistant-backend

python -m venv .venv
.venv\Scripts\Activate.ps1

pip install -r requirements.txt

# создать .env на основе .env.example и заполнить значения

uvicorn app.main:app --reload
```

После запуска приложение открывается по адресу:

```text
http://127.0.0.1:8000
```

## Переменные окружения

Для запуска проекта используется файл `.env`, который создаётся локально внутри папки:

```text
hr-assistant-backend/
```

В репозиторий включается только шаблон:

```text
hr-assistant-backend/.env.example
```

В нём должны быть описаны переменные:

- `HR_USERNAME`
- `HR_PASSWORD`
- `SESSION_SECRET_KEY`

Реальный `.env` не должен попадать в Git.

## Документы проекта

В репозитории используются дополнительные документы:

- `hr-assistant-backend/README_PROJECT_CONCEPT.md` — исходная концепция проекта;
- `hr-assistant-backend/CHECKLIST_PRODUCTION.md` — чеклист подготовки к размещению на сервере;
- `hr-assistant-backend/.env.example` — шаблон переменных окружения.

## Подготовка к размещению

Проект подготовлен к следующему этапу:

- публикации в GitHub;
- установке на VPS / сервер предприятия;
- настройке `uvicorn`, `systemd`, `nginx` и HTTPS;
- привязке к корпоративному поддомену.

## Примечание

Ранняя версия проекта описывала HR Interview Assistant как AI‑ассистента для GPT / Playground.  
Текущая версия представляет собой полноценное веб‑приложение на FastAPI с HTML‑интерфейсом и HR‑авторизацией.