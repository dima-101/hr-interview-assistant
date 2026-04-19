# README_DEPLOYMENT.md

## Проект

**HR Interview Assistant** — внутренний веб‑сервис предприятия для просмотра вакансий, проведения интервью по профилям должностей и формирования отчётов по кандидатам.

## Текущий стек

- Python
- FastAPI
- Jinja2 templates
- HTML / CSS / JavaScript
- Markdown-файлы для профилей вакансий и сохранённых отчётов
- `.env` для конфигурации
- SessionMiddleware для HR‑авторизации

## Текущая структура интерфейса

- `/` — главная
- `/about` — о проекте
- `/vacancies` — список вакансий
- `/vacancies/{slug}` — карточка вакансии
- `/interview?vacancyid=...` — страница интервью
- `/report` — страница отчёта
- `/hr/login` — вход в HR‑панель
- `/hr` — HR‑панель
- `/reports` — архив отчётов

## Цель production-развёртывания

Разместить сервис на поддомене 3-го уровня предприятия, например:

- `hr.company.ru`
- `assistant.company.ru`
- `interview.company.ru`

С основного сайта предприятия должна быть ссылка на сервис, а на самом сервисе — ссылка на основной сайт предприятия.

---

## Вариант 1. Если хостинг не поддерживает Python

### Что можно сделать

Если провайдер даёт только обычный shared-хостинг, можно разместить:

- статические страницы;
- HTML/CSS/JS;
- демонстрационную версию интерфейса.

### Ограничения

В этом режиме не получится полноценно использовать FastAPI как backend‑приложение.

### Вывод

Если Python запускать нельзя, тогда:

- frontend размещается на поддомене;
- backend переносится на отдельный VPS или сервер предприятия;
- frontend обращается к backend API по HTTPS.

---

## Вариант 2. Полноценное развёртывание FastAPI

### Когда подходит

Если есть:

- VPS;
- Linux-сервер;
- внутренняя виртуальная машина предприятия;
- доступ для запуска Python-приложения как сервиса.

### Что размещается

- FastAPI-приложение;
- Jinja2-шаблоны;
- static-файлы;
- `.env` для настроек;
- reverse proxy через Nginx;
- домен/поддомен предприятия.

### Рекомендуемая схема

1. Пользователь открывает `https://hr.company.ru`
2. Nginx принимает запрос
3. Nginx проксирует запрос в `uvicorn`
4. FastAPI отдаёт HTML и backend-логику
5. При необходимости приложение обращается к внешним API

---

## Рекомендуемая структура проекта на сервере

```text
/opt/hr-assistant-backend/
  app/
    main.py
    config.py
    templates/
    static/
    data/
    logs/
  job_profiles/
  requirements.txt
  .env
  .env.example
  README_DEPLOYMENT.md
  CHECKLIST_PRODUCTION.md
```

---

## Минимальные требования к серверу

- Ubuntu 22.04+ или аналогичный Linux
- Python 3.11+
- pip
- venv
- Nginx
- systemd
- SSL-сертификат для поддомена
- доступ к DNS-настройке поддомена

---

## Установка на Linux / VPS

### 1. Копирование проекта

Скопировать проект на сервер, например в:

```bash
/opt/hr-assistant-backend
```

### 2. Создание виртуального окружения

```bash
cd /opt/hr-assistant-backend
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### 3. Подготовка `.env`

Создать файл `.env` на сервере на основе `.env.example` и заполнить реальные значения.

Пример:

```env
HR_USERNAME=your_hr_login
HR_PASSWORD=your_hr_password
SESSION_SECRET_KEY=your-long-random-secret-key
```

### 4. Тестовый запуск

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

После этого можно проверить, что приложение открывается по IP сервера и порту `8000`.

### 5. Настройка systemd

Создать сервис:

```text
/etc/systemd/system/hr-assistant.service
```

Пример содержимого:

```ini
[Unit]
Description=HR Interview Assistant
After=network.target

[Service]
User=www-data
Group=www-data
WorkingDirectory=/opt/hr-assistant-backend
Environment="PATH=/opt/hr-assistant-backend/venv/bin"
ExecStart=/opt/hr-assistant-backend/venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000
Restart=always

[Install]
WantedBy=multi-user.target
```

Команды:

```bash
sudo systemctl daemon-reload
sudo systemctl enable hr-assistant.service
sudo systemctl start hr-assistant.service
sudo systemctl status hr-assistant.service
```

### 6. Настройка Nginx

Создать конфигурационный файл, например:

```text
/etc/nginx/sites-available/hr.company.ru
```

Пример:

```nginx
server {
    listen 80;
    server_name hr.company.ru;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

Затем выполнить:

```bash
sudo ln -s /etc/nginx/sites-available/hr.company.ru /etc/nginx/sites-enabled/
sudo nginx -t
sudo systemctl reload nginx
```

### 7. Подключение HTTPS

Рекомендуется использовать:

- `certbot` и Let's Encrypt;
- либо корпоративный SSL-сертификат предприятия.

---

## Что проверить после установки

- сайт открывается по домену;
- `/vacancies` работает;
- `/interview?vacancyid=sales_soft_client_001` работает;
- `/report` открывается;
- `/hr/login` открывается;
- вход в HR‑панель работает;
- `/reports` открывается только после входа;
- логи пишутся без критических ошибок.

---

## Безопасность

Перед публикацией в production нужно проверить:

- `.env` не хранится в git;
- реальные секреты не попадают в репозиторий;
- используется HTTPS;
- серверные права доступа настроены корректно;
- debug-режим отключён.

---

## Практический вывод

Для учебной сдачи проект уже готов в локальном режиме.

Для размещения на сервере предприятия дальше нужны:

1. GitHub-репозиторий;
2. VPS или сервер с поддержкой Python;
3. настройка `uvicorn`;
4. настройка `systemd`;
5. настройка `nginx`;
6. подключение HTTPS;
7. финальная проверка по `CHECKLIST_PRODUCTION.md`.