from datetime import datetime
import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path
import re

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from starlette.middleware.sessions import SessionMiddleware
import yaml

from app.config import get_settings
from app.llmservice import (
    CandidateAnswer,
    EvaluationRequest,
    LLMError,
    call_llm_evaluate_candidate,
)


app = FastAPI(title="HR Interview Assistant")

settings = get_settings()

app.add_middleware(
    SessionMiddleware,
    secret_key=settings.session_secret_key,
    same_site="lax",
    https_only=False,
    max_age=60 * 60 * 8,
)

LOG_DIR = Path(__file__).parent / "logs"
LOG_DIR.mkdir(exist_ok=True)

log_file = LOG_DIR / "app.log"
handler = RotatingFileHandler(
    log_file,
    maxBytes=1_000_000,
    backupCount=3,
    encoding="utf-8",
)
formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s — %(message)s")
handler.setFormatter(formatter)

logger = logging.getLogger("hr_app")
logger.setLevel(logging.INFO)
if not logger.handlers:
    logger.addHandler(handler)

app.mount("/static", StaticFiles(directory="app/static"), name="static")
templates = Jinja2Templates(directory="app/templates")

APP_DIR = Path(__file__).resolve().parent
BACKEND_DIR = APP_DIR.parent
PROJECT_ROOT = BACKEND_DIR.parent

JOB_PROFILES_DIR = PROJECT_ROOT / "job_profiles"
REPORTS_DIR = APP_DIR / "data" / "reports" / "save"
REPORTS_DIR.mkdir(parents=True, exist_ok=True)


def is_hr_logged_in(request: Request) -> bool:
    return request.session.get("hr_authenticated") is True


def require_hr_auth(request: Request):
    if not is_hr_logged_in(request):
        return RedirectResponse(url="/hr/login", status_code=303)
    return None


def slugify(text: str) -> str:
    text = (text or "").lower().strip()
    text = re.sub(r"[^a-z0-9а-яё_-]+", "-", text)
    return text.strip("-")


def normalize_newlines(text: str) -> str:
    return (text or "").replace("\r\n", "\n").replace("\r", "\n")


def split_lines(text: str) -> list[str]:
    if not text:
        return []
    return [line.strip() for line in text.splitlines() if line.strip()]


def join_lines(items) -> str:
    if not items or not isinstance(items, list):
        return ""
    return "\n".join(str(item).strip() for item in items if str(item).strip())


def extract_section_items(content: str, section_title: str) -> list[str]:
    aliases = {
        "Обязанности": ["Обязанности", "Responsibilities"],
        "Обязательные требования": [
            "Обязательные требования",
            "Требования",
            "Required skills",
            "Required requirements",
        ],
        "Желательные требования": [
            "Желательные требования",
            "Будет плюсом",
            "Preferred skills",
            "Preferred requirements",
        ],
        "Red flags": ["Red flags", "Red Flags", "Красные флаги"],
    }

    titles = aliases.get(section_title, [section_title])
    normalized = normalize_newlines(content)

    for title in titles:
        pattern = rf"(?ms)^###\s+{re.escape(title)}\s*\n(.*?)(?=^###\s+|\Z)"
        match = re.search(pattern, normalized)
        if not match:
            continue

        block = match.group(1)
        items = []

        for line in block.splitlines():
            line = line.strip()
            if not line:
                continue
            if line.startswith("- "):
                items.append(line[2:].strip())
            elif re.match(r"^\d+\.\s+", line):
                items.append(re.sub(r"^\d+\.\s+", "", line).strip())

        if items:
            return items

    return []


def extract_question_block(content: str, block_title: str) -> list[str]:
    bank_titles = ["Банк вопросов", "Question bank", "Questions"]
    block_aliases = {
        "Общие": ["Общие", "General"],
        "Профессиональные": ["Профессиональные", "Technical", "Professional"],
        "Поведенческие": ["Поведенческие", "Behavioral"],
    }

    normalized = normalize_newlines(content)

    bank_content = None
    for bank_title in bank_titles:
        bank_match = re.search(
            rf"(?ms)^###\s+{re.escape(bank_title)}\s*\n(.*?)(?=^###\s+|\Z)",
            normalized,
        )
        if bank_match:
            bank_content = bank_match.group(1)
            break

    if not bank_content:
        return []

    titles = block_aliases.get(block_title, [block_title])

    for title in titles:
        block_pattern = rf"(?ms)^####\s+{re.escape(title)}\s*\n(.*?)(?=^####\s+|^###\s+|\Z)"
        block_match = re.search(block_pattern, bank_content)
        if not block_match:
            continue

        block_text = block_match.group(1)
        questions = []

        for line in block_text.splitlines():
            line = line.strip()
            if not line:
                continue
            if re.match(r"^\d+\.\s+", line):
                questions.append(re.sub(r"^\d+\.\s+", "", line).strip())
            elif line.startswith("- "):
                questions.append(line[2:].strip())

        if questions:
            return questions

    return []


def is_yaml_profile(file_path: Path) -> bool:
    try:
        text = file_path.read_text(encoding="utf-8")
    except Exception:
        return False

    normalized = normalize_newlines(text).lstrip()

    if not normalized.startswith("---"):
        return False

    lines = normalized.split("\n")
    if not lines or lines[0].strip() != "---":
        return False

    closing_index = None
    for i in range(1, min(len(lines), 200)):
        if lines[i].strip() == "---":
            closing_index = i
            break

    if closing_index is not None:
        yaml_block = "\n".join(lines[1:closing_index]).strip()
        if yaml_block:
            try:
                data = yaml.safe_load(yaml_block)
                return isinstance(data, dict)
            except Exception:
                return False

    yaml_block = "\n".join(lines[1:]).strip()
    if yaml_block:
        try:
            data = yaml.safe_load(yaml_block)
            return isinstance(data, dict)
        except Exception:
            return False

    return False


def load_yaml_profile(file_path: Path) -> dict:
    text = file_path.read_text(encoding="utf-8")
    normalized = normalize_newlines(text).lstrip()

    if not normalized.startswith("---"):
        raise ValueError(f"Файл {file_path} не является YAML-профилем (нет начального ---)")

    lines = normalized.split("\n")
    if not lines or lines[0].strip() != "---":
        raise ValueError(f"Файл {file_path} не является YAML-профилем (первая строка не ---)")

    closing_index = None
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            closing_index = i
            break

    if closing_index is not None:
        yaml_block = "\n".join(lines[1:closing_index]).strip()
    else:
        yaml_block = "\n".join(lines[1:]).strip()

    if not yaml_block:
        raise ValueError(f"Файл {file_path} не содержит YAML-данных между ---")

    try:
        data = yaml.safe_load(yaml_block)
    except Exception as e:
        raise ValueError(f"Ошибка парсинга YAML в файле {file_path}: {e}")

    if not isinstance(data, dict):
        raise ValueError(f"YAML в файле {file_path} должен быть словарём, получен {type(data)}")

    return data


def save_yaml_profile(data: dict) -> Path:
    vacancy_id = data["vacancy_id"]
    file_path = JOB_PROFILES_DIR / f"{vacancy_id}.md"

    data.setdefault("version", 1.0)
    data["updated_at"] = datetime.now().strftime("%Y-%m-%d")

    yaml_block = yaml.safe_dump(
        data,
        allow_unicode=True,
        sort_keys=False,
        width=100,
    )

    content = f"---\n{yaml_block}---\n"
    file_path.write_text(content, encoding="utf-8")
    logger.info("Сохранён YAML-профиль вакансии: %s", file_path.name)
    return file_path


def get_vacancy_file_path(vacancy_id: str) -> Path | None:
    file_path = JOB_PROFILES_DIR / f"{vacancy_id}.md"
    if file_path.exists():
        return file_path
    return None


def extract_md_field(content: str, labels: list[str], default: str = "") -> str:
    normalized = normalize_newlines(content)

    for label in labels:
        pattern = rf"(?im)^-\s*{re.escape(label)}:\s*(.+)$"
        match = re.search(pattern, normalized)
        if match:
            value = match.group(1).strip()
            return value.strip("`").strip()

    return default


def extract_md_context_field(content: str, labels: list[str], default: str = "") -> str:
    normalized = normalize_newlines(content)

    for label in labels:
        pattern = rf"(?im)^-\s*{re.escape(label)}:\s*(.+)$"
        match = re.search(pattern, normalized)
        if match:
            return match.group(1).strip()

    return default


def extract_md_rubric_items(content: str) -> list[dict]:
    normalized = normalize_newlines(content)
    match = re.search(
        r"(?ms)^###\s+Рубрика оценки\s*\n(.*?)(?=^###\s+|\Z)",
        normalized,
    )
    if not match:
        return []

    block = match.group(1)
    items = []

    for line in block.splitlines():
        line = line.strip()
        if not line or not line.startswith("- "):
            continue

        raw = line[2:].strip()
        m = re.match(r"^(.*?)\s*:\s*вес\s*(\d+)\s*$", raw, flags=re.IGNORECASE)
        if m:
            items.append(
                {
                    "name": m.group(1).strip(),
                    "weight": int(m.group(2)),
                    "guidance": "",
                }
            )
        else:
            items.append(
                {
                    "name": raw,
                    "weight": 0,
                    "guidance": "",
                }
            )

    return items


def extract_md_pass_threshold(content: str, default: int = 70) -> int:
    normalized = normalize_newlines(content)
    match = re.search(
        r"(?ms)^###\s+Порог прохождения\s*\n(.*?)(?=^###\s+|\Z)",
        normalized,
    )
    if not match:
        return default

    block = match.group(1)
    numbers = []

    for line in block.splitlines():
        line = line.strip()
        if not line:
            continue
        found = re.findall(r"\d+", line)
        numbers.extend(int(x) for x in found)

    if not numbers:
        return default

    return max(numbers)


def extract_report_recommendation_content(content: str) -> str:
    match = re.search(r"(?im)^##\s*Recommendation\s*$", content)
    if match:
        tail = content[match.end():].strip()
        if tail:
            first_line = tail.splitlines()[0].strip()
            if first_line:
                return first_line

    match = re.search(r"(?im)^Recommendation:\s*(.+)$", content)
    if match:
        return match.group(1).strip()

    return ""


def extract_report_candidate_name(content: str) -> str:
    match = re.search(r"(?im)^Candidate:\s*(.+)$", content)
    if match:
        return match.group(1).strip()
    return "—"


def extract_report_vacancy_id(content: str) -> str:
    match = re.search(r"(?im)^Vacancy ID:\s*(.+)$", content)
    if match:
        return match.group(1).strip()
    return "—"


def validate_yaml_profile(data: dict) -> str | None:
    required_str_fields = ["vacancy_id", "title", "company_context", "role_goal"]
    for field in required_str_fields:
        if not data.get(field) or not str(data[field]).strip():
            return f"Поле '{field}' обязательно для заполнения."

    if data.get("status") not in {"active", "closed", "paused"}:
        return "Поле 'status' должно быть active, closed или paused."

    def check_list(name: str):
        items = data.get(name) or []
        if not isinstance(items, list) or len(items) == 0:
            return f"Список '{name}' должен содержать хотя бы один пункт."
        return None

    for list_field in ["responsibilities", "required_skills", "red_flags"]:
        err = check_list(list_field)
        if err:
            return err

    qb = data.get("question_bank") or {}
    for block in ["general", "technical", "behavioral"]:
        items = qb.get(block) or []
        if not items:
            return f"В блоке вопросов '{block}' должен быть хотя бы один вопрос."

    rubric = data.get("evaluation_rubric") or {}
    if not rubric.get("summary"):
        return "Нужно заполнить summary в evaluation_rubric."

    dims = rubric.get("key_dimensions") or []
    if len(dims) < 1:
        return "В рубрике должно быть минимум одно измерение (key_dimensions)."

    weights = [d.get("weight", 0) for d in dims]
    if any(w <= 0 for w in weights):
        return "Все веса в рубрике должны быть положительными числами."

    threshold = rubric.get("pass_threshold", 0)
    if not isinstance(threshold, int) or threshold <= 0:
        return "pass_threshold должен быть положительным числом."

    return None


def profile_to_form_data(file_path: Path) -> dict:
    if is_yaml_profile(file_path):
        data = load_yaml_profile(file_path)
        qb = data.get("question_bank") or {}
        rubric = data.get("evaluation_rubric") or {}
        dims = rubric.get("key_dimensions") or []

        return {
            "source_format": "yaml",
            "vacancy_id": data.get("vacancy_id", file_path.stem),
            "status": data.get("status", "active"),
            "title": data.get("title", ""),
            "department": data.get("department", ""),
            "location_mode": data.get("location_mode", "on-site"),
            "work_schedule": data.get("work_schedule", ""),
            "company_context": data.get("company_context", ""),
            "role_goal": data.get("role_goal", ""),
            "responsibilities": join_lines(data.get("responsibilities")),
            "required_experience_years": data.get("required_experience", {}).get("years", ""),
            "required_experience_background": data.get("required_experience", {}).get("background", ""),
            "required_skills": join_lines(data.get("required_skills")),
            "preferred_skills": join_lines(data.get("preferred_skills")),
            "key_soft_skills": join_lines(data.get("key_soft_skills")),
            "red_flags": join_lines(data.get("red_flags")),
            "questions_general": join_lines(qb.get("general")),
            "questions_technical": join_lines(qb.get("technical")),
            "questions_behavioral": join_lines(qb.get("behavioral")),
            "rubric_summary": rubric.get("summary", ""),
            "rubric_dim1_name": dims[0].get("name", "") if len(dims) >= 1 else "",
            "rubric_dim1_weight": dims[0].get("weight", 0) if len(dims) >= 1 else 0,
            "rubric_dim1_guidance": dims[0].get("guidance", "") if len(dims) >= 1 else "",
            "rubric_dim2_name": dims[1].get("name", "") if len(dims) >= 2 else "",
            "rubric_dim2_weight": dims[1].get("weight", 0) if len(dims) >= 2 else 0,
            "rubric_dim2_guidance": dims[1].get("guidance", "") if len(dims) >= 2 else "",
            "pass_threshold": rubric.get("pass_threshold", 70),
        }

    content = file_path.read_text(encoding="utf-8")
    rubric_items = extract_md_rubric_items(content)

    return {
        "source_format": "markdown",
        "vacancy_id": extract_md_field(content, ["Vacancy ID", "vacancy_id"], file_path.stem),
        "status": extract_md_field(content, ["Статус", "Status"], "active"),
        "title": extract_md_field(content, ["Название вакансии", "Title"], file_path.stem),
        "department": extract_md_field(content, ["Подразделение", "Department"], ""),
        "location_mode": "on-site",
        "work_schedule": extract_md_field(content, ["Формат работы", "Work schedule"], ""),
        "company_context": extract_md_context_field(content, ["Краткое описание компании"], ""),
        "role_goal": extract_md_context_field(content, ["Цель роли"], ""),
        "responsibilities": join_lines(extract_section_items(content, "Обязанности")),
        "required_experience_years": extract_md_field(content, ["Минимальный опыт"], ""),
        "required_experience_background": extract_md_context_field(
            content,
            ["Продукт / направление", "Ключевой результат работы"],
            "",
        ),
        "required_skills": join_lines(extract_section_items(content, "Обязательные требования")),
        "preferred_skills": join_lines(extract_section_items(content, "Желательные требования")),
        "key_soft_skills": "",
        "red_flags": join_lines(extract_section_items(content, "Red flags")),
        "questions_general": join_lines(extract_question_block(content, "Общие")),
        "questions_technical": join_lines(extract_question_block(content, "Профессиональные")),
        "questions_behavioral": join_lines(extract_question_block(content, "Поведенческие")),
        "rubric_summary": "",
        "rubric_dim1_name": rubric_items[0].get("name", "") if len(rubric_items) >= 1 else "",
        "rubric_dim1_weight": rubric_items[0].get("weight", 0) if len(rubric_items) >= 1 else 0,
        "rubric_dim1_guidance": "",
        "rubric_dim2_name": rubric_items[1].get("name", "") if len(rubric_items) >= 2 else "",
        "rubric_dim2_weight": rubric_items[1].get("weight", 0) if len(rubric_items) >= 2 else 0,
        "rubric_dim2_guidance": "",
        "pass_threshold": extract_md_pass_threshold(content, 70),
    }


def build_form_data(
    vacancy_id: str,
    status: str,
    title: str,
    department: str,
    location_mode: str,
    work_schedule: str,
    company_context: str,
    role_goal: str,
    responsibilities: str,
    required_experience_years: str,
    required_experience_background: str,
    required_skills: str,
    preferred_skills: str,
    key_soft_skills: str,
    red_flags: str,
    questions_general: str,
    questions_technical: str,
    questions_behavioral: str,
    rubric_summary: str,
    rubric_dim1_name: str,
    rubric_dim1_weight: int,
    rubric_dim1_guidance: str,
    rubric_dim2_name: str,
    rubric_dim2_weight: int,
    rubric_dim2_guidance: str,
    pass_threshold: int,
) -> dict:
    return {
        "vacancy_id": vacancy_id,
        "status": status.strip(),
        "title": title.strip(),
        "department": department.strip(),
        "location_mode": location_mode.strip() or "on-site",
        "work_schedule": work_schedule.strip(),
        "company_context": company_context.strip(),
        "role_goal": role_goal.strip(),
        "responsibilities": split_lines(responsibilities),
        "required_experience_years": required_experience_years.strip(),
        "required_experience_background": required_experience_background.strip(),
        "required_skills": split_lines(required_skills),
        "preferred_skills": split_lines(preferred_skills),
        "key_soft_skills": split_lines(key_soft_skills),
        "red_flags": split_lines(red_flags),
        "questions_general": split_lines(questions_general),
        "questions_technical": split_lines(questions_technical),
        "questions_behavioral": split_lines(questions_behavioral),
        "rubric_summary": rubric_summary.strip(),
        "rubric_dim1_name": rubric_dim1_name.strip(),
        "rubric_dim1_weight": int(rubric_dim1_weight or 0),
        "rubric_dim1_guidance": rubric_dim1_guidance.strip(),
        "rubric_dim2_name": rubric_dim2_name.strip(),
        "rubric_dim2_weight": int(rubric_dim2_weight or 0),
        "rubric_dim2_guidance": rubric_dim2_guidance.strip(),
        "pass_threshold": int(pass_threshold or 70),
    }


def validate_form_data(form_data: dict) -> str | None:
    if form_data["status"] not in {"active", "closed", "paused"}:
        return "Некорректный статус вакансии."

    if not form_data["title"]:
        return "Нужно заполнить название вакансии."

    if not form_data["company_context"]:
        return "Нужно заполнить краткое описание компании."

    if not form_data["role_goal"]:
        return "Нужно заполнить цель роли."

    if not form_data["responsibilities"]:
        return "Нужно заполнить хотя бы одну обязанность."

    if not form_data["required_skills"]:
        return "Нужно заполнить хотя бы одно обязательное требование."

    if not form_data["red_flags"]:
        return "Нужно заполнить хотя бы один red flag."

    if not form_data["questions_general"]:
        return "Нужно добавить хотя бы один общий вопрос."

    if not form_data["questions_technical"]:
        return "Нужно добавить хотя бы один профессиональный/технический вопрос."

    if not form_data["questions_behavioral"]:
        return "Нужно добавить хотя бы один поведенческий вопрос."

    if form_data["pass_threshold"] <= 0:
        return "Порог прохождения должен быть положительным числом."

    return None


def build_yaml_data_from_form(form_data: dict) -> dict:
    dimensions = []

    if form_data["rubric_dim1_name"]:
        dimensions.append(
            {
                "name": form_data["rubric_dim1_name"],
                "weight": form_data["rubric_dim1_weight"],
                "guidance": form_data["rubric_dim1_guidance"],
            }
        )

    if form_data["rubric_dim2_name"]:
        dimensions.append(
            {
                "name": form_data["rubric_dim2_name"],
                "weight": form_data["rubric_dim2_weight"],
                "guidance": form_data["rubric_dim2_guidance"],
            }
        )

    return {
        "vacancy_id": form_data["vacancy_id"],
        "status": form_data["status"],
        "version": 1.0,
        "title": form_data["title"],
        "department": form_data["department"],
        "location_mode": form_data["location_mode"],
        "work_schedule": form_data["work_schedule"],
        "company_context": form_data["company_context"],
        "role_goal": form_data["role_goal"],
        "responsibilities": form_data["responsibilities"],
        "required_experience": {
            "years": form_data["required_experience_years"],
            "background": form_data["required_experience_background"],
        },
        "required_skills": form_data["required_skills"],
        "preferred_skills": form_data["preferred_skills"],
        "key_soft_skills": form_data["key_soft_skills"],
        "red_flags": form_data["red_flags"],
        "question_bank": {
            "general": form_data["questions_general"],
            "technical": form_data["questions_technical"],
            "behavioral": form_data["questions_behavioral"],
        },
        "evaluation_rubric": {
            "summary": form_data["rubric_summary"],
            "key_dimensions": dimensions,
            "pass_threshold": form_data["pass_threshold"],
        },
    }


def _md_bullets(items: list[str]) -> str:
    if not items:
        return "- (не заполнено)"
    return "\n".join(f"- {item}" for item in items)


def _md_numbered(items: list[str], start: int = 1) -> str:
    if not items:
        return f"{start}. (не заполнено)"
    return "\n".join(f"{idx}. {item}" for idx, item in enumerate(items, start=start))


def build_markdown_profile_text(form_data: dict) -> str:
    rubric_lines = []

    if form_data["rubric_dim1_name"]:
        rubric_lines.append(f"- {form_data['rubric_dim1_name']}: вес {form_data['rubric_dim1_weight']}")
    if form_data["rubric_dim2_name"]:
        rubric_lines.append(f"- {form_data['rubric_dim2_name']}: вес {form_data['rubric_dim2_weight']}")
    if not rubric_lines:
        rubric_lines.append("- Критерий оценки: вес 100")

    return f"""## Карточка вакансии: {form_data['title']}

### Основная информация
- Vacancy ID: `{form_data['vacancy_id']}`
- Статус: `{form_data['status']}`
- Версия: `1.1`
- Дата обновления: `{datetime.now().strftime("%Y-%m-%d")}`
- Название вакансии: {form_data['title']}
- Подразделение: {form_data['department']}
- Формат работы: {form_data['work_schedule'] or form_data['location_mode']}

### Контекст компании и роли
- Краткое описание компании: {form_data['company_context']}
- Цель роли: {form_data['role_goal']}
- Доп. контекст опыта: {form_data['required_experience_background']}

### Обязанности
{_md_bullets(form_data['responsibilities'])}

### Обязательные требования
{_md_bullets(form_data['required_skills'])}

### Желательные требования
{_md_bullets(form_data['preferred_skills'])}

### Опыт и инструменты
- Минимальный опыт: {form_data['required_experience_years']}
- Дополнительный контекст: {form_data['required_experience_background']}

### Soft skills
{_md_bullets(form_data['key_soft_skills'])}

### Red flags
{_md_bullets(form_data['red_flags'])}

### Банк вопросов

#### Общие
{_md_numbered(form_data['questions_general'], start=1)}

#### Профессиональные
{_md_numbered(form_data['questions_technical'], start=1)}

#### Поведенческие
{_md_numbered(form_data['questions_behavioral'], start=1)}

### Рубрика оценки
{chr(10).join(rubric_lines)}

### Комментарий по рубрике
- Summary: {form_data['rubric_summary'] or "—"}
- Guidance 1: {form_data['rubric_dim1_guidance'] or "—"}
- Guidance 2: {form_data['rubric_dim2_guidance'] or "—"}

### Порог прохождения
- Рекомендовать: от {form_data['pass_threshold']} баллов
"""


def save_profile_from_form(file_path: Path, form_data: dict) -> None:
    if is_yaml_profile(file_path):
        yaml_data = build_yaml_data_from_form(form_data)
        validation_error = validate_yaml_profile(yaml_data)
        if validation_error:
            raise ValueError(validation_error)
        save_yaml_profile(yaml_data)
        return

    markdown_text = build_markdown_profile_text(form_data)
    file_path.write_text(markdown_text.strip() + "\n", encoding="utf-8")
    logger.info("Сохранён markdown-профиль вакансии: %s", file_path.name)


def update_vacancy_status(vacancy_id: str, new_status: str) -> bool:
    if new_status not in {"active", "closed", "paused"}:
        logger.warning("Некорректный статус: %s", new_status)
        return False

    file_path = JOB_PROFILES_DIR / f"{vacancy_id}.md"
    if not file_path.exists():
        logger.warning("Файл профиля не найден: %s", file_path)
        return False

    text = file_path.read_text(encoding="utf-8")

    if is_yaml_profile(file_path):
        data = load_yaml_profile(file_path)
        old_status = data.get("status", "active")
        data["status"] = new_status
        save_yaml_profile(data)
    else:
        pattern = r"(?im)^-\s*Статус:\s*`?([^`\n]+)`?"
        if not re.search(pattern, text):
            logger.warning("Строка статуса не найдена в файле: %s", file_path)
            return False

        new_text = re.sub(pattern, f"- Статус: `{new_status}`", text)
        file_path.write_text(new_text, encoding="utf-8")
        old_status = "unknown"

    logger.info("Статус вакансии изменён: vacancy_id=%s, %s -> %s", vacancy_id, old_status, new_status)
    return True


def delete_vacancy_file(vacancy_id: str) -> bool:
    file_path = JOB_PROFILES_DIR / f"{vacancy_id}.md"
    if not file_path.exists():
        logger.warning("Попытка удалить несуществующий профиль: %s", file_path)
        return False

    file_path.unlink()
    logger.info("Удалён профиль вакансии: %s", file_path.name)
    return True


def parse_job_profile(file_path: Path) -> dict:
    content = file_path.read_text(encoding="utf-8")

    if is_yaml_profile(file_path):
        data = load_yaml_profile(file_path)
        qb = data.get("question_bank") or {}
        rubric = data.get("evaluation_rubric") or {}

        vacancy_id = str(data.get("vacancy_id") or file_path.stem).strip()
        title = str(data.get("title") or file_path.stem).strip()
        status = str(data.get("status") or "active").strip()

        responsibilities = data.get("responsibilities") or []
        required_requirements = data.get("required_skills") or data.get("required_requirements") or []
        preferred_requirements = data.get("preferred_skills") or data.get("preferred_requirements") or []
        red_flags = data.get("red_flags") or []

        general_questions = qb.get("general") or []
        professional_questions = qb.get("technical") or []
        behavioral_questions = qb.get("behavioral") or []

        return {
            "title": title,
            "vacancy_id": vacancy_id,
            "status": status,
            "slug": slugify(vacancy_id),
            "file_path": str(file_path),
            "company_context": data.get("company_context", ""),
            "role_goal": data.get("role_goal", ""),
            "responsibilities": responsibilities if isinstance(responsibilities, list) else [],
            "required_requirements": required_requirements if isinstance(required_requirements, list) else [],
            "preferred_requirements": preferred_requirements if isinstance(preferred_requirements, list) else [],
            "red_flags": red_flags if isinstance(red_flags, list) else [],
            "general_questions": general_questions if isinstance(general_questions, list) else [],
            "professional_questions": professional_questions if isinstance(professional_questions, list) else [],
            "behavioral_questions": behavioral_questions if isinstance(behavioral_questions, list) else [],
            "required_skills": data.get("required_skills", []),
            "preferred_skills": data.get("preferred_skills", []),
            "key_soft_skills": data.get("key_soft_skills", []),
            "question_bank": qb,
            "evaluation_rubric": rubric,
        }

    normalized = normalize_newlines(content)

    def first_match(patterns: list[str], default: str) -> str:
        for pattern in patterns:
            m = re.search(pattern, normalized)
            if m:
                return m.group(1).strip()
        return default

    title = first_match(
        [
            r"(?im)^-\s*название вакансии:\s*(.+)$",
            r"(?im)^-\s*title:\s*(.+)$",
            r"(?im)^title:\s*(.+)$",
        ],
        file_path.stem,
    )
    vacancy_id = first_match(
        [
            r"(?im)^-\s*vacancy id:\s*`?([^`\n]+)`?",
            r"(?im)^-\s*vacancy_id:\s*`?([^`\n]+)`?",
            r"(?im)^vacancy_id:\s*`?([^`\n]+)`?",
        ],
        file_path.stem,
    )
    status = first_match(
        [
            r"(?im)^-\s*статус:\s*`?([^`\n]+)`?",
            r"(?im)^-\s*status:\s*`?([^`\n]+)`?",
            r"(?im)^status:\s*`?([^`\n]+)`?",
        ],
        "active",
    )

    responsibilities = extract_section_items(normalized, "Обязанности")
    required_requirements = extract_section_items(normalized, "Обязательные требования")
    preferred_requirements = extract_section_items(normalized, "Желательные требования")
    red_flags = extract_section_items(normalized, "Red flags")
    general_questions = extract_question_block(normalized, "Общие")
    professional_questions = extract_question_block(normalized, "Профессиональные")
    behavioral_questions = extract_question_block(normalized, "Поведенческие")

    return {
        "title": title,
        "vacancy_id": vacancy_id,
        "status": status,
        "slug": slugify(vacancy_id),
        "file_path": str(file_path),
        "company_context": "",
        "role_goal": "",
        "responsibilities": responsibilities,
        "required_requirements": required_requirements,
        "preferred_requirements": preferred_requirements,
        "red_flags": red_flags,
        "general_questions": general_questions,
        "professional_questions": professional_questions,
        "behavioral_questions": behavioral_questions,
        "required_skills": required_requirements,
        "preferred_skills": preferred_requirements,
        "key_soft_skills": [],
        "question_bank": {
            "general": general_questions,
            "technical": professional_questions,
            "behavioral": behavioral_questions,
        },
        "evaluation_rubric": {
            "summary": "",
            "key_dimensions": extract_md_rubric_items(normalized),
            "pass_threshold": extract_md_pass_threshold(normalized, 70),
        },
    }


def load_job_profiles() -> list[dict]:
    profiles = []
    used_ids = set()

    if not JOB_PROFILES_DIR.exists():
        logger.warning("Папка с профилями не найдена: %s", JOB_PROFILES_DIR)
        return []

    for file_path in JOB_PROFILES_DIR.glob("*.md"):
        try:
            profile = parse_job_profile(file_path)
        except Exception as e:
            logger.warning("Пропуск файла %s из-за ошибки: %s", file_path.name, e)
            continue

        vacancy_id = profile["vacancy_id"]
        if vacancy_id in used_ids:
            logger.warning("Дублирующий vacancy_id пропущен: %s (%s)", vacancy_id, file_path.name)
            continue

        profiles.append(profile)
        used_ids.add(vacancy_id)

    profiles.sort(key=lambda x: x["title"].lower())
    logger.info("Загружено профилей вакансий: %d", len(profiles))
    return profiles


def get_vacancy_by_slug(vacancy_slug: str) -> dict | None:
    for profile in load_job_profiles():
        if profile["slug"] == vacancy_slug:
            return profile
    return None


def get_vacancy_by_id(vacancy_id: str) -> dict | None:
    for profile in load_job_profiles():
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
    professional_experience_assessment: str = "",
    required_requirements_confirmed: list[str] | None = None,
    required_requirements_partial: list[str] | None = None,
    required_requirements_not_confirmed: list[str] | None = None,
    preferred_strengths: list[str] | None = None,
    preferred_not_confirmed: list[str] | None = None,
    soft_skills_assessment: str = "",
    strengths: list[str] | None = None,
    risks: list[str] | None = None,
    red_flags_found: list[str] | None = None,
    follow_up_checks: list[str] | None = None,
    score_by_dimension: dict | None = None,
    score_total: int | None = None,
    pass_threshold: int | None = None,
    threshold_result: str = "",
    recommendation_comment: str = "",
    hiring_manager_note: str = "",
) -> str:
    required_requirements_confirmed = required_requirements_confirmed or []
    required_requirements_partial = required_requirements_partial or []
    required_requirements_not_confirmed = required_requirements_not_confirmed or []
    preferred_strengths = preferred_strengths or []
    preferred_not_confirmed = preferred_not_confirmed or []
    strengths = strengths or []
    risks = risks or []
    red_flags_found = red_flags_found or []
    follow_up_checks = follow_up_checks or []
    score_by_dimension = score_by_dimension or {}

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_candidate = (
        re.sub(r"[^a-zA-Zа-яА-Я0-9_-]+", "_", candidate_name).strip("_") or "candidate"
    )
    filename = f"report_{vacancy_id}_{safe_candidate}_{timestamp}.md"
    file_path = REPORTS_DIR / filename

    # Блок с числовой оценкой
    score_block_lines: list[str] = []
    if score_total is not None or score_by_dimension:
        if score_total is not None:
            score_block_lines.append(f"- Итоговый балл: {score_total}")
        if pass_threshold is not None:
            score_block_lines.append(f"- Порог прохождения: {pass_threshold}")
        if threshold_result:
            score_block_lines.append(f"- Итоговый статус: {threshold_result}")
        if score_by_dimension:
            score_block_lines.append("- Баллы по измерениям:")
            for dim_name, dim_score in score_by_dimension.items():
                score_block_lines.append(f"  - {dim_name}: {dim_score}")
        score_block_lines.append("")

    score_block = "\n".join(score_block_lines)

    content = f"""{datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
# {vacancy_title}
Vacancy ID: {vacancy_id}
Candidate: {candidate_name}
Contact: {candidate_contact or ""}

## Оценка по баллам
{score_block or "- Нет данных"}

## Summary
{summary}

## Professional experience
{professional_experience_assessment or "—"}

## Required requirements
- Подтверждены:
{os.linesep.join(f"- {item}" for item in required_requirements_confirmed) or "- —"}
- Частично подтверждены:
{os.linesep.join(f"- {item}" for item in required_requirements_partial) or "- —"}
- Не подтверждены:
{os.linesep.join(f"- {item}" for item in required_requirements_not_confirmed) or "- —"}

## Preferred requirements
- Подтвержденные сильные стороны:
{os.linesep.join(f"- {item}" for item in preferred_strengths) or "- —"}
- Не подтверждены / требуют проверки:
{os.linesep.join(f"- {item}" for item in preferred_not_confirmed) or "- —"}

## Soft skills
{soft_skills_assessment or "—"}

## Strengths
{os.linesep.join(f"- {item}" for item in strengths) or "- —"}

## Risks
{os.linesep.join(f"- {item}" for item in risks) or "- —"}

## Red flags
{os.linesep.join(f"- {item}" for item in red_flags_found) or "- —"}

## Follow-up checks
{os.linesep.join(f"- {item}" for item in follow_up_checks) or "- —"}

## Recommendation
{recommendation}

## Recommendation comment
{recommendation_comment or "—"}

## Hiring manager note
{hiring_manager_note or "—"}
"""

    file_path.write_text(content.strip() + "\n", encoding="utf-8")
    logger.info("Сохранён отчёт: %s", file_path.name)
    return file_path.name


def parse_and_validate_yaml_text(raw_text: str) -> tuple[dict | None, str | None]:
    text = normalize_newlines(raw_text or "").strip()
    if not text:
        return None, "YAML-текст пуст."

    yaml_block = text

    if text.startswith("---"):
        lines = text.split("\n")
        if lines and lines[0].strip() == "---":
            closing_index = None
            for i in range(1, len(lines)):
                if lines[i].strip() == "---":
                    closing_index = i
                    break

            if closing_index is not None:
                yaml_block = "\n".join(lines[1:closing_index]).strip()
            else:
                yaml_block = "\n".join(lines[1:]).strip()

    if not yaml_block:
        return None, "YAML-блок пуст."

    try:
        data = yaml.safe_load(yaml_block)
    except Exception as e:
        return None, f"Ошибка YAML: {e}"

    if not isinstance(data, dict):
        return None, "YAML должен описывать объект вакансии (словарь)."

    validation_error = validate_yaml_profile(data)
    if validation_error:
        return None, validation_error

    return data, None


def extract_report_recommendation(content: str) -> str:
    match = re.search(r"(?im)^\*\*(.+?)\*\*\s*$", content)
    if match:
        return match.group(1).strip()

    block_match = re.search(r"(?ms)^##\s+Рекомендация\s*\n(.*?)(?=^##\s+|\Z)", content)
    if block_match:
        first_line = block_match.group(1).strip().splitlines()[0].strip()
        if first_line:
            return first_line

    return ""


class ReportPayload(BaseModel):
    vacancy_id: str
    vacancy_title: str
    candidate_name: str
    candidate_contact: str | None = ""
    summary: str
    recommendation: str = "Требует дополнительной оценки"


class InterviewAnswer(BaseModel):
    question_block: str
    question_text: str
    answer_text: str


class InterviewSubmission(BaseModel):
    vacancy_id: str
    vacancy_title: str
    candidate_name: str
    candidate_contact: str | None = ""
    interview_format: str | None = "web"
    interviewer: str | None = ""
    answers: list[InterviewAnswer]


class YamlValidateRequest(BaseModel):
    raw_yaml: str


@app.get("/")
def home(request: Request):
    return templates.TemplateResponse(request, "index.html", {})


@app.get("/about")
def about(request: Request):
    return templates.TemplateResponse(request, "about.html", {})


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
def interview(request: Request, vacancyid: str = "", vacancy_id: str = ""):
    resolved_vacancy_id = vacancyid or vacancy_id
    vacancy = get_vacancy_by_id(resolved_vacancy_id) if resolved_vacancy_id else None

    return templates.TemplateResponse(
        request,
        "interview.html",
        {
            "vacancy_id": resolved_vacancy_id,
            "vacancy_title": vacancy["title"] if vacancy else "Вакансия не выбрана",
            "vacancy_status": vacancy["status"] if vacancy else "unknown",
            "general_questions": vacancy["general_questions"] if vacancy else [],
            "professional_questions": vacancy["professional_questions"] if vacancy else [],
            "behavioral_questions": vacancy["behavioral_questions"] if vacancy else [],
        },
    )


@app.get("/report")
def report(request: Request):
    return templates.TemplateResponse(request, "report.html", {})


@app.post("/api/reports/save")
def save_report(payload: ReportPayload):
    filename = save_report_file(
        vacancy_id=payload.vacancy_id,
        vacancy_title=payload.vacancy_title,
        candidate_name=payload.candidate_name,
        candidate_contact=payload.candidate_contact or "",
        summary=payload.summary,
        recommendation=payload.recommendation,
    )

    return JSONResponse(
        {
            "status": "ok",
            "message": "Отчёт сохранён",
            "filename": filename,
        }
    )


@app.post("/api/interviews/evaluate")
async def evaluate_interview(payload: InterviewSubmission):
    vacancy = get_vacancy_by_id(payload.vacancy_id)

    if vacancy is None:
        return JSONResponse(
            {
                "status": "error",
                "message": f"Вакансия с id '{payload.vacancy_id}' не найдена",
            },
            status_code=404,
        )

    filled_answers = [
        answer
        for answer in payload.answers
        if answer.answer_text and answer.answer_text.strip()
    ]

    if not filled_answers:
        return JSONResponse(
            {
                "status": "error",
                "message": "Нет заполненных ответов кандидата",
            },
            status_code=400,
        )

    llm_request = EvaluationRequest(
        vacancy_id=payload.vacancy_id,
        candidate_name=payload.candidate_name,
        candidate_contact=payload.candidate_contact or "",
        interviewer=payload.interviewer or "",
        interview_format=payload.interview_format or "web",
        answers=[
            CandidateAnswer(
                question_block=item.question_block,
                question_text=item.question_text,
                answer_text=item.answer_text,
            )
            for item in filled_answers
        ],
    )

    try:
        result = await call_llm_evaluate_candidate(llm_request, vacancy)
        logger.info(
            "LLM result scores: total=%s, by_dimension=%s, pass_threshold=%s, threshold_result=%s",
            getattr(result, "score_total", None),
            getattr(result, "score_by_dimension", None),
            getattr(result, "pass_threshold", None),
            getattr(result, "threshold_result", None),
        )
    except LLMError as e:
        logger.exception("Ошибка LLM-оценки интервью: %s", e)
        return JSONResponse(
            {
                "status": "error",
                "message": f"Ошибка LLM-оценки: {e}",
            },
            status_code=500,
        )
    except Exception as e:
        logger.exception("Непредвиденная ошибка при оценке интервью: %s", e)
        return JSONResponse(
            {
                "status": "error",
                "message": "Внутренняя ошибка сервера при оценке интервью",
            },
            status_code=500,
        )

    filename = save_report_file(
        vacancy_id=payload.vacancy_id,
        vacancy_title=payload.vacancy_title or vacancy.get("title", ""),
        candidate_name=payload.candidate_name,
        candidate_contact=payload.candidate_contact or "",
        summary=result.summary,
        professional_experience_assessment=result.professional_experience_assessment,
        required_requirements_confirmed=result.required_requirements_confirmed,
        required_requirements_partial=result.required_requirements_partial,
        required_requirements_not_confirmed=result.required_requirements_not_confirmed,
        preferred_strengths=result.preferred_strengths,
        preferred_not_confirmed=result.preferred_not_confirmed,
        soft_skills_assessment=result.soft_skills_assessment,
        strengths=result.strengths,
        risks=result.risks,
        red_flags_found=result.red_flags_found,
        follow_up_checks=result.follow_up_checks,
        score_by_dimension=result.score_by_dimension,
        score_total=result.score_total,
        pass_threshold=result.pass_threshold,
        threshold_result=result.threshold_result,
        recommendation=result.recommendation,
        recommendation_comment=result.recommendation_comment,
        hiring_manager_note=result.hiring_manager_note or "",
    )

    return JSONResponse(
        {
            "status": "ok",
            "message": "Интервью оценено и отчёт сохранён",
            "filename": filename,
            "vacancy_id": payload.vacancy_id,
            "candidate_name": payload.candidate_name,
            "score_by_dimension": result.score_by_dimension,
            "score_total": result.score_total,
            "pass_threshold": result.pass_threshold,
            "threshold_result": result.threshold_result,
            "recommendation": result.recommendation,
            "summary": result.summary,
            "strengths": result.strengths,
            "risks": result.risks,
            "red_flags_found": result.red_flags_found,
            "follow_up_checks": result.follow_up_checks,
        }
    )


@app.get("/hr/login", response_class=HTMLResponse)
def hr_login_page(request: Request):
    if is_hr_logged_in(request):
        return RedirectResponse(url="/hr", status_code=303)

    return templates.TemplateResponse(request, "hr_login.html", {"error": None})


@app.post("/hr/login", response_class=HTMLResponse)
def hr_login_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
):
    current_settings = get_settings()

    if username == current_settings.hr_username and password == current_settings.hr_password:
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
            "vacancies_url": "/hr/vacancies",
            "reports_url": "/reports",
            "single_report_url": "/report",
        },
    )


@app.get("/hr/vacancies", response_class=HTMLResponse)
def hr_vacancies(request: Request):
    auth_redirect = require_hr_auth(request)
    if auth_redirect:
        return auth_redirect

    vacancies_list = load_job_profiles()
    logger.info("HR-панель: список вакансий — %d шт.", len(vacancies_list))

    return templates.TemplateResponse(
        request,
        "hr_vacancies.html",
        {
            "username": request.session.get("hr_username", "HR"),
            "vacancies": vacancies_list,
            "error": None,
        },
    )


@app.get("/hr/vacancies/new", response_class=HTMLResponse)
def hr_vacancy_new_form(request: Request):
    auth_redirect = require_hr_auth(request)
    if auth_redirect:
        return auth_redirect

    return templates.TemplateResponse(
        request,
        "hr_vacancy_new.html",
        {
            "username": request.session.get("hr_username"),
            "error": None,
        },
    )


@app.get("/hr/vacancies/{vacancy_id}/edit", response_class=HTMLResponse)
def hr_vacancy_edit_form(request: Request, vacancy_id: str):
    auth_redirect = require_hr_auth(request)
    if auth_redirect:
        return auth_redirect

    file_path = get_vacancy_file_path(vacancy_id)
    if not file_path:
        return templates.TemplateResponse(
            request,
            "hr_vacancy_edit.html",
            {
                "username": request.session.get("hr_username"),
                "vacancy_id": vacancy_id,
                "error": "Вакансия не найдена.",
            },
            status_code=404,
        )

    context = {
        "username": request.session.get("hr_username"),
        "error": None,
        **profile_to_form_data(file_path),
    }

    return templates.TemplateResponse(request, "hr_vacancy_edit.html", context)


@app.post("/hr/vacancies/{vacancy_id}/edit", response_class=HTMLResponse)
async def hr_vacancy_edit_submit(
    request: Request,
    vacancy_id: str,
    status: str = Form("active"),
    title: str = Form(...),
    department: str = Form(""),
    location_mode: str = Form("on-site"),
    work_schedule: str = Form(""),
    company_context: str = Form(""),
    role_goal: str = Form(""),
    responsibilities: str = Form(""),
    required_experience_years: str = Form(""),
    required_experience_background: str = Form(""),
    required_skills: str = Form(""),
    preferred_skills: str = Form(""),
    key_soft_skills: str = Form(""),
    red_flags: str = Form(""),
    questions_general: str = Form(""),
    questions_technical: str = Form(""),
    questions_behavioral: str = Form(""),
    rubric_summary: str = Form(""),
    rubric_dim1_name: str = Form(""),
    rubric_dim1_weight: int = Form(0),
    rubric_dim1_guidance: str = Form(""),
    rubric_dim2_name: str = Form(""),
    rubric_dim2_weight: int = Form(0),
    rubric_dim2_guidance: str = Form(""),
    pass_threshold: int = Form(70),
):
    auth_redirect = require_hr_auth(request)
    if auth_redirect:
        return auth_redirect

    file_path = get_vacancy_file_path(vacancy_id)
    if not file_path:
        return templates.TemplateResponse(
            request,
            "hr_vacancy_edit.html",
            {
                "username": request.session.get("hr_username"),
                "vacancy_id": vacancy_id,
                "error": "Вакансия не найдена.",
            },
            status_code=404,
        )

    form_data = build_form_data(
        vacancy_id=vacancy_id,
        status=status,
        title=title,
        department=department,
        location_mode=location_mode,
        work_schedule=work_schedule,
        company_context=company_context,
        role_goal=role_goal,
        responsibilities=responsibilities,
        required_experience_years=required_experience_years,
        required_experience_background=required_experience_background,
        required_skills=required_skills,
        preferred_skills=preferred_skills,
        key_soft_skills=key_soft_skills,
        red_flags=red_flags,
        questions_general=questions_general,
        questions_technical=questions_technical,
        questions_behavioral=questions_behavioral,
        rubric_summary=rubric_summary,
        rubric_dim1_name=rubric_dim1_name,
        rubric_dim1_weight=rubric_dim1_weight,
        rubric_dim1_guidance=rubric_dim1_guidance,
        rubric_dim2_name=rubric_dim2_name,
        rubric_dim2_weight=rubric_dim2_weight,
        rubric_dim2_guidance=rubric_dim2_guidance,
        pass_threshold=pass_threshold,
    )

    validation_error = validate_form_data(form_data)
    if validation_error:
        return templates.TemplateResponse(
            request,
            "hr_vacancy_edit.html",
            {
                "username": request.session.get("hr_username"),
                "error": validation_error,
                **form_data,
            },
            status_code=400,
        )

    try:
        save_profile_from_form(file_path, form_data)
    except ValueError as e:
        return templates.TemplateResponse(
            request,
            "hr_vacancy_edit.html",
            {
                "username": request.session.get("hr_username"),
                "error": str(e),
                **form_data,
            },
            status_code=400,
        )

    logger.info("HR-редактирование вакансии сохранено: vacancy_id=%s", vacancy_id)
    return RedirectResponse(url="/hr/vacancies", status_code=303)


@app.post("/hr/vacancies/{vacancy_id}/status", response_class=HTMLResponse)
async def hr_vacancy_status_submit(
    request: Request,
    vacancy_id: str,
    status: str = Form(...),
):
    auth_redirect = require_hr_auth(request)
    if auth_redirect:
        return auth_redirect

    if status not in {"active", "paused", "closed"}:
        profiles = load_job_profiles()
        return templates.TemplateResponse(
            request,
            "hr_vacancies.html",
            {
                "username": request.session.get("hr_username"),
                "vacancies": profiles,
                "error": "Некорректный статус вакансии.",
            },
            status_code=400,
        )

    updated = update_vacancy_status(vacancy_id, status)
    if not updated:
        profiles = load_job_profiles()
        return templates.TemplateResponse(
            request,
            "hr_vacancies.html",
            {
                "username": request.session.get("hr_username"),
                "vacancies": profiles,
                "error": "Не удалось обновить статус вакансии.",
            },
            status_code=400,
        )

    return RedirectResponse(url="/hr/vacancies", status_code=303)


@app.get("/hr/vacancies/{vacancy_id}/delete", response_class=HTMLResponse)
def hr_vacancy_delete_confirm(request: Request, vacancy_id: str):
    auth_redirect = require_hr_auth(request)
    if auth_redirect:
        return auth_redirect

    file_path = get_vacancy_file_path(vacancy_id)
    if not file_path:
        profiles = load_job_profiles()
        return templates.TemplateResponse(
            request,
            "hr_vacancies.html",
            {
                "username": request.session.get("hr_username"),
                "vacancies": profiles,
                "error": "Вакансия не найдена.",
            },
            status_code=404,
        )

    form_data = profile_to_form_data(file_path)

    return templates.TemplateResponse(
        request,
        "hr_vacancy_delete.html",
        {
            "username": request.session.get("hr_username"),
            "error": None,
            "vacancy_id": form_data["vacancy_id"],
            "title": form_data["title"],
            "status": form_data["status"],
            "department": form_data.get("department", ""),
            "source_format": form_data.get("source_format", "unknown"),
        },
    )


@app.post("/hr/vacancies/{vacancy_id}/delete", response_class=HTMLResponse)
async def hr_vacancy_delete_submit(
    request: Request,
    vacancy_id: str,
    confirm_vacancy_id: str = Form(...),
):
    auth_redirect = require_hr_auth(request)
    if auth_redirect:
        return auth_redirect

    file_path = get_vacancy_file_path(vacancy_id)
    if not file_path:
        profiles = load_job_profiles()
        return templates.TemplateResponse(
            request,
            "hr_vacancies.html",
            {
                "username": request.session.get("hr_username"),
                "vacancies": profiles,
                "error": "Вакансия не найдена.",
            },
            status_code=404,
        )

    form_data = profile_to_form_data(file_path)

    if confirm_vacancy_id.strip() != vacancy_id:
        return templates.TemplateResponse(
            request,
            "hr_vacancy_delete.html",
            {
                "username": request.session.get("hr_username"),
                "vacancy_id": form_data["vacancy_id"],
                "title": form_data["title"],
                "status": form_data["status"],
                "department": form_data.get("department", ""),
                "source_format": form_data.get("source_format", "unknown"),
                "error": "Подтверждение не совпадает с vacancy_id.",
            },
            status_code=400,
        )

    deleted = delete_vacancy_file(vacancy_id)
    if not deleted:
        return templates.TemplateResponse(
            request,
            "hr_vacancy_delete.html",
            {
                "username": request.session.get("hr_username"),
                "vacancy_id": form_data["vacancy_id"],
                "title": form_data["title"],
                "status": form_data["status"],
                "department": form_data.get("department", ""),
                "source_format": form_data.get("source_format", "unknown"),
                "error": "Не удалось удалить вакансию.",
            },
            status_code=400,
        )

    logger.info("HR-удаление вакансии: vacancy_id=%s", vacancy_id)
    return RedirectResponse(url="/hr/vacancies", status_code=303)


@app.post("/api/hr/vacancies/yaml/validate")
async def validate_vacancy_yaml(payload: YamlValidateRequest):
    data, error = parse_and_validate_yaml_text(payload.raw_yaml)
    if error:
        return JSONResponse({"ok": False, "error": error}, status_code=400)

    return JSONResponse({"ok": True, "vacancy_id": data.get("vacancy_id")})


@app.post("/api/hr/vacancies/import")
async def import_vacancy_from_yaml(payload: YamlValidateRequest):
    data, error = parse_and_validate_yaml_text(payload.raw_yaml)
    if error:
        return JSONResponse({"ok": False, "error": error}, status_code=400)

    save_yaml_profile(data)
    logger.info("Создана новая вакансия из YAML-текста через HR-панель: vacancy_id=%s", data.get("vacancy_id"))
    return JSONResponse({"ok": True, "vacancy_id": data.get("vacancy_id")})


@app.post("/api/hr/vacancies/upload")
async def upload_vacancy_yaml_file(request: Request):
    form = await request.form()
    uploaded_file = form.get("file")

    if uploaded_file is None:
        return JSONResponse({"ok": False, "error": "Файл не был передан."}, status_code=400)

    try:
        raw_bytes = await uploaded_file.read()
        raw_text = raw_bytes.decode("utf-8")
    except Exception:
        return JSONResponse({"ok": False, "error": "Файл должен быть UTF-8."}, status_code=400)

    data, error = parse_and_validate_yaml_text(raw_text)
    if error:
        return JSONResponse({"ok": False, "error": error}, status_code=400)

    save_yaml_profile(data)
    return JSONResponse({"ok": True, "vacancy_id": data.get("vacancy_id")})


@app.post("/hr/vacancies/upload-yaml", response_class=HTMLResponse)
async def import_vacancy_from_yaml_form(
    request: Request,
    file: UploadFile | None = File(None),
    yaml_file: UploadFile | None = File(None),
):
    auth_redirect = require_hr_auth(request)
    if auth_redirect:
        return auth_redirect

    uploaded_file = file or yaml_file
    if uploaded_file is None:
        return templates.TemplateResponse(
            request,
            "hr_vacancy_new.html",
            {
                "username": request.session.get("hr_username"),
                "error": "Файл не был передан. Выберите .md, .yaml или .yml файл и попробуйте снова.",
            },
            status_code=400,
        )

    try:
        raw_yaml = (await uploaded_file.read()).decode("utf-8")
    except UnicodeDecodeError:
        return templates.TemplateResponse(
            request,
            "hr_vacancy_new.html",
            {
                "username": request.session.get("hr_username"),
                "error": "Файл должен быть текстовым UTF-8 (.md, .yaml, .yml).",
            },
            status_code=400,
        )
    except Exception as e:
        return templates.TemplateResponse(
            request,
            "hr_vacancy_new.html",
            {
                "username": request.session.get("hr_username"),
                "error": f"Не удалось прочитать файл: {e}",
            },
            status_code=400,
        )

    data, error = parse_and_validate_yaml_text(raw_yaml)
    if error:
        return templates.TemplateResponse(
            request,
            "hr_vacancy_new.html",
            {
                "username": request.session.get("hr_username"),
                "error": error,
            },
            status_code=400,
        )

    save_yaml_profile(data)
    logger.info("Создана новая вакансия из YAML-файла через HR-панель: vacancy_id=%s", data.get("vacancy_id"))
    return RedirectResponse(url="/hr/vacancies", status_code=303)


@app.get("/reports")
def reports_archive(request: Request):
    auth_redirect = require_hr_auth(request)
    if auth_redirect:
        return auth_redirect

    selected_vacancy_id = request.query_params.get("vacancy_id", "").strip()

    report_files = []
    vacancy_ids_set: set[str] = set()

    for filepath in sorted(REPORTS_DIR.glob("*.md"), reverse=True):
        try:
            content = filepath.read_text(encoding="utf-8")
        except Exception:
            continue

        recommendation_text = extract_report_recommendation_content(content) or ""
        candidate_name = extract_report_candidate_name(content)
        vacancy_id = extract_report_vacancy_id(content)

        if vacancy_id and vacancy_id not in {"", "—"}:
            vacancy_ids_set.add(vacancy_id)

        if selected_vacancy_id and vacancy_id != selected_vacancy_id:
            continue

        rec = recommendation_text.strip()

        if rec.startswith("Рекомендовать"):
            status = "pass"
        elif rec.startswith("Не рекомендовать"):
            status = "fail"
        else:
            status = "borderline"

        report_files.append(
            {
                "filename": filepath.name,
                "created_at": datetime.fromtimestamp(
                    filepath.stat().st_mtime
                ).strftime("%Y-%m-%d %H:%M:%S"),
                "view_url": f"/reports/{filepath.name}",
                "recommendation": recommendation_text or "Требует дополнительной оценки",
                "status": status,
                "candidate_name": candidate_name,
                "vacancy_id": vacancy_id,
            }
        )

    vacancy_options = sorted(vacancy_ids_set)

    return templates.TemplateResponse(
        request,
        "reports.html",
        {
            "reports": report_files,
            "vacancy_options": vacancy_options,
            "selected_vacancy_id": selected_vacancy_id,
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
            "reportfile.html",
            {
                "filename": filename,
                "content": "Файл отчёта не найден.",
            },
            status_code=404,
        )

    content = file_path.read_text(encoding="utf-8")
    return templates.TemplateResponse(
        request,
        "reportfile.html",
        {
            "filename": filename,
            "content": content,
        },
    )