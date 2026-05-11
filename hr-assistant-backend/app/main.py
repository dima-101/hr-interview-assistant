from fastapi import FastAPI, Request, Form
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from starlette.middleware.sessions import SessionMiddleware
from pydantic import BaseModel

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
import re
from datetime import datetime

from app.config import get_settings


app = FastAPI(title="HR Interview Assistant")

settings = get_settings()

app.add_middleware(
    SessionMiddleware,
    secret_key=settings.session_secret_key,
    same_site="lax",
    https_only=False,
    max_age=60 * 60 * 8,
)

# Папка для логов
LOG_DIR = Path(__file__).parent / "logs"
LOG_DIR.mkdir(exist_ok=True)

log_file = LOG_DIR / "app.log"

# Ротация: максимум 1 МБ, хранить 3 файла
handler = RotatingFileHandler(
    log_file,
    maxBytes=1_000_000,
    backupCount=3,
    encoding="utf-8",
)

formatter = logging.Formatter(
    "%(asctime)s [%(levelname)s] %(name)s — %(message)s"
)

handler.setFormatter(formatter)

logger = logging.getLogger("hr_app")
logger.setLevel(logging.INFO)
logger.addHandler(handler)

app.mount("/static", StaticFiles(directory="app/static"), name="static")
templates = Jinja2Templates(directory="app/templates")

JOB_PROFILES_DIR = Path(r"C:\temp\hr-interview-assistant\job_profiles")

# Папка для сохранённых отчётов
REPORTS_DIR = Path(__file__).parent / "data" / "reports" / "save"
REPORTS_DIR.mkdir(parents=True, exist_ok=True)


def is_hr_logged_in(request: Request) -> bool:
    return request.session.get("hr_authenticated") is True


def require_hr_auth(request: Request):
    if not is_hr_logged_in(request):
        return RedirectResponse(url="/hr/login", status_code=303)
    return None


def slugify(text: str) -> str:
    text = text.lower().strip()
    text = re.sub(r"[^a-z0-9]+", "-", text)
    return text.strip("-")


def extract_section_items(content: str, section_title: str) -> list:
    pattern = rf"(?ms)^###\s+{re.escape(section_title)}\s*\n(.*?)(?=^###\s+|\Z)"
    match = re.search(pattern, content)

    if not match:
        return []

    block = match.group(1)
    items = []

    for line in block.splitlines():
        line = line.strip()
        if line.startswith("- "):
            items.append(line[2:].strip())

    return items


def extract_question_block(content: str, block_title: str) -> list:
    bank_match = re.search(r"(?ms)^###\s+Банк вопросов\s*\n(.*?)(?=^###\s+|\Z)", content)
    if not bank_match:
        return []

    bank_content = bank_match.group(1)

    block_pattern = rf"(?ms)^####\s+{re.escape(block_title)}\s*\n(.*?)(?=^####\s+|\Z)"
    block_match = re.search(block_pattern, bank_content)

    if not block_match:
        return []

    block_text = block_match.group(1)
    questions = []

    for line in block_text.splitlines():
        line = line.strip()
        if re.match(r"^\d+\.\s+", line):
            question = re.sub(r"^\d+\.\s+", "", line)
            questions.append(question.strip())

    return questions


def parse_job_profile(file_path: Path) -> dict:
    content = file_path.read_text(encoding="utf-8")

    vacancy_id_match = re.search(r"(?im)^-\s*vacancy id:\s*`?([^`\n]+)`?", content)
    status_match = re.search(r"(?im)^-\s*статус:\s*`?([^`\n]+)`?", content)
    title_match = re.search(r"(?im)^-\s*название вакансии:\s*(.+)$", content)

    vacancy_id = vacancy_id_match.group(1).strip() if vacancy_id_match else file_path.stem
    status = status_match.group(1).strip() if status_match else "active"
    title = title_match.group(1).strip() if title_match else file_path.stem

    responsibilities = extract_section_items(content, "Обязанности")
    required_requirements = extract_section_items(content, "Обязательные требования")
    preferred_requirements = extract_section_items(content, "Желательные требования")
    red_flags = extract_section_items(content, "Red flags")

    general_questions = extract_question_block(content, "Общие")
    professional_questions = extract_question_block(content, "Профессиональные")
    behavioral_questions = extract_question_block(content, "Поведенческие")

    return {
        "title": title,
        "vacancy_id": vacancy_id,
        "status": status,
        "slug": slugify(vacancy_id),
        "file_path": str(file_path),
        "responsibilities": responsibilities,
        "required_requirements": required_requirements,
        "preferred_requirements": preferred_requirements,
        "red_flags": red_flags,
        "general_questions": general_questions,
        "professional_questions": professional_questions,
        "behavioral_questions": behavioral_questions,
    }


def load_job_profiles() -> list:
    if not JOB_PROFILES_DIR.exists():
        return []

    profiles = []
    for file_path in JOB_PROFILES_DIR.glob("*.md"):
        profiles.append(parse_job_profile(file_path))

    profiles.sort(key=lambda x: x["title"].lower())
    return profiles


def get_vacancy_by_slug(vacancy_slug: str) -> dict | None:
    profiles = load_job_profiles()
    for profile in profiles:
        if profile["slug"] == vacancy_slug:
            return profile
    return None


def get_vacancy_by_id(vacancy_id: str) -> dict | None:
    profiles = load_job_profiles()
    for profile in profiles:
        if profile["vacancy_id"] == vacancy_id:
            return profile
    return None


def save_report_file(
    vacancy_id: str,
    vacancy_title: str,
    candidate_name: str,
    candidate_contact: str,
    summary: str,
    recommendation: str = "Требует дополнительной оценки",
) -> str:
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_candidate = re.sub(r"[^a-zA-Zа-яА-Я0-9_-]+", "_", candidate_name).strip("_")
    safe_candidate = safe_candidate or "candidate"

    filename = f"report_{vacancy_id}_{safe_candidate}_{timestamp}.md"
    file_path = REPORTS_DIR / filename

    content = f"""# Отчёт по интервью

**Дата:** {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
**Вакансия:** {vacancy_title}
**Vacancy ID:** {vacancy_id}
**Кандидат:** {candidate_name}
**Контакт:** {candidate_contact}

## Краткое резюме
{summary}

## Рекомендация
{recommendation}
"""

    file_path.write_text(content, encoding="utf-8")
    logger.info("Сохранён отчёт интервью: %s", file_path.name)

    return file_path.name


class ReportPayload(BaseModel):
    vacancy_id: str
    vacancy_title: str
    candidate_name: str
    candidate_contact: str | None = ""
    summary: str


@app.get("/")
def home(request: Request):
    return templates.TemplateResponse(
        request,
        "index.html",
    )


@app.get("/about")
def about(request: Request):
    return templates.TemplateResponse(
        request,
        "about.html",
    )


@app.get("/vacancies")
def vacancies(request: Request):
    vacancies_list = load_job_profiles()
    logger.info("Страница /vacancies — показано %d вакансий", len(vacancies_list))
    return templates.TemplateResponse(
        request,
        "vacancies.html",
        {
            "vacancies": vacancies_list,
        },
    )


@app.get("/vacancies/{vacancy_slug}")
def vacancy_detail(request: Request, vacancy_slug: str):
    vacancy = get_vacancy_by_slug(vacancy_slug)

    if vacancy is None:
        return templates.TemplateResponse(
            request,
            "vacancydetail.html",
            {
                "vacancy_title": "Вакансия не найдена",
                "vacancy_id": vacancy_slug,
                "vacancy_status": "unknown",
                "responsibilities": [],
                "required_requirements": [],
                "preferred_requirements": [],
                "red_flags": [],
            },
            status_code=404,
        )

    return templates.TemplateResponse(
        request,
        "vacancydetail.html",
        {
            "vacancy_title": vacancy["title"],
            "vacancy_id": vacancy["vacancy_id"],
            "vacancy_status": vacancy["status"],
            "responsibilities": vacancy["responsibilities"],
            "required_requirements": vacancy["required_requirements"],
            "preferred_requirements": vacancy["preferred_requirements"],
            "red_flags": vacancy["red_flags"],
        },
    )


@app.get("/interview", response_class=HTMLResponse)
def interview(request: Request, vacancyid: str = ""):
    vacancy = get_vacancy_by_id(vacancyid) if vacancyid else None

    return templates.TemplateResponse(
        request,
        "interview.html",
        {
            "vacancy_id": vacancyid,
            "vacancy_title": vacancy["title"] if vacancy else "Вакансия не выбрана",
            "vacancy_status": vacancy["status"] if vacancy else "unknown",
            "general_questions": vacancy["general_questions"] if vacancy else [],
            "professional_questions": vacancy["professional_questions"] if vacancy else [],
            "behavioral_questions": vacancy["behavioral_questions"] if vacancy else [],
        },
    )


# --- ЛОГИН / ВЫХОД ДЛЯ HR ---

@app.get("/hr/login", response_class=HTMLResponse)
def hr_login_page(request: Request):
    if is_hr_logged_in(request):
        return RedirectResponse(url="/hr", status_code=303)

    return templates.TemplateResponse(
        request,
        "hr_login.html",
        {"error": None},
    )


@app.post("/hr/login", response_class=HTMLResponse)
def hr_login_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
):
    settings = get_settings()

    if username == settings.hr_username and password == settings.hr_password:
        request.session["hr_authenticated"] = True
        request.session["hr_username"] = username
        return RedirectResponse(url="/hr", status_code=303)

    return templates.TemplateResponse(
        request,
        "hr_login.html",
        {"error": "Неверный логин или пароль"},
        status_code=401,
    )


@app.get("/hr/logout")
def hr_logout(request: Request):
    request.session.clear()
    return RedirectResponse(url="/hr/login", status_code=303)


# --- ЗАЩИЩЁННЫЕ HR-МАРШРУТЫ ---

@app.get("/hr")
def hr_dashboard(request: Request):
    auth_redirect = require_hr_auth(request)
    if auth_redirect:
        return auth_redirect

    return templates.TemplateResponse(
        request,
        "hr_dashboard.html",
        {
            "username": request.session.get("hr_username", "HR"),
            "vacancies_url": "/vacancies",
            "reports_url": "/reports",
            "single_report_url": "/report",
        },
    )


@app.get("/report")
def report(request: Request):
    return templates.TemplateResponse(
        request,
        "report.html",
        {
            "vacancy_title": "Менеджер по продажам программного обеспечения и работе с клиентами",
            "vacancy_id": "sales_soft_client_001",
            "candidate_name": "Иван Иванов",
            "interview_date": "2026-04-16",
            "recommendation": "Рекомендовать",
            "total_score": 82,
            "summary": (
                "Кандидат показал уверенный опыт B2B/B2G-продаж в IT-сфере, "
                "понимание полного цикла сделки и базовое знание специфики работы "
                "с заказчиками в регулируемой среде."
            ),
            "strengths": [
                "Уверенно описывает полный цикл сделки от первого контакта до закрытия.",
                "Хорошо понимает работу с CRM и воронкой продаж.",
                "Способен аргументированно презентовать сложные IT-решения разным ролям заказчика.",
            ],
            "risks": [
                "Нужно глубже проверить практический опыт участия в закупках по 44-ФЗ и 223-ФЗ.",
                "Стоит уточнить глубину понимания требований по 187-ФЗ и смежной регуляторики.",
            ],
            "interviewer_notes": (
                "Производит впечатление структурного и зрелого кандидата. "
                "Подходит для дальнейшего этапа интервью с руководителем."
            ),
        },
    )


@app.post("/api/reports/save")
def save_report(payload: ReportPayload):
    filename = save_report_file(
        vacancy_id=payload.vacancy_id,
        vacancy_title=payload.vacancy_title,
        candidate_name=payload.candidate_name,
        candidate_contact=payload.candidate_contact or "",
        summary=payload.summary,
    )

    return JSONResponse(
        {
            "status": "ok",
            "message": "Отчёт сохранён",
            "filename": filename,
        }
    )


@app.get("/reports")
def reports_archive(request: Request):
    auth_redirect = require_hr_auth(request)
    if auth_redirect:
        return auth_redirect

    report_files = []

    for file_path in sorted(REPORTS_DIR.glob("*.md"), reverse=True):
        report_files.append(
            {
                "filename": file_path.name,
                "created_at": datetime.fromtimestamp(
                    file_path.stat().st_mtime
                ).strftime("%Y-%m-%d %H:%M:%S"),
                "view_url": f"/reports/{file_path.name}",
            }
        )

    return templates.TemplateResponse(
        request,
        "reports.html",
        {
            "reports": report_files,
        },
    )


@app.get("/reports/{filename}")
def report_file_detail(request: Request, filename: str):
    auth_redirect = require_hr_auth(request)
    if auth_redirect:
        return auth_redirect

    file_path = REPORTS_DIR / filename

    if not file_path.exists() or file_path.suffix.lower() != ".md":
        return templates.TemplateResponse(
            request,
            "report_file.html",
            {
                "filename": filename,
                "content": "Файл отчёта не найден.",
            },
            status_code=404,
        )

    content = file_path.read_text(encoding="utf-8")

    return templates.TemplateResponse(
        request,
        "report_file.html",
        {
            "filename": filename,
            "content": content,
        },
    )