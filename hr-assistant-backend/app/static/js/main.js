// app/static/js/main.js

console.log("HR Interview Assistant loaded");

document.addEventListener("DOMContentLoaded", function () {
    const form = document.querySelector(".interview-form");
    const currentQuestionEl = document.querySelector("#current-question");

    // Если это не страница интервью — дальше ничего не делаем
    if (!form || !currentQuestionEl) {
        return;
    }

    const nameInput = form.querySelector("#candidate-name");
    const contactInput = form.querySelector("#candidate-contact");
    const answerInput = form.querySelector("#candidate-answer");
    const consentCheckbox = form.querySelector("#candidate-consent");

    const note = document.createElement("p");
    note.className = "form-note";

    // -------- 1. Собираем все вопросы по блокам --------

    const questions = [];

    const collectFromList = (selector, blockName) => {
        const list = document.querySelector(selector);
        if (!list) return;
        list.querySelectorAll("li").forEach(li => {
            questions.push({
                text: li.textContent.trim(),
                block: blockName
            });
        });
    };

    collectFromList("#general-questions-list", "Общие");
    collectFromList("#professional-questions-list", "Профессиональные");
    collectFromList("#behavioral-questions-list", "Поведенческие");

    let currentIndex = 0;
    const answers = new Array(questions.length).fill("");

    // -------- 2. Находим элементы навигации по вопросам --------

    const blockLabel = document.querySelector("[data-question-block-label]");
    const progressLabel = document.querySelector("[data-question-progress]");
    const prevBtn = document.querySelector("[data-question-prev]");
    const nextBtn = document.querySelector("[data-question-next]");

    // -------- 3. Функция отображения текущего вопроса --------

    function renderCurrentQuestion() {
        if (questions.length === 0) {
            currentQuestionEl.textContent = "Вопросы не найдены.";
            if (blockLabel) blockLabel.textContent = "";
            if (progressLabel) progressLabel.textContent = "";
            if (prevBtn) prevBtn.disabled = true;
            if (nextBtn) nextBtn.disabled = true;
            return;
        }

        const q = questions[currentIndex];
        currentQuestionEl.textContent = q.text;
        if (blockLabel) blockLabel.textContent = q.block;
        if (progressLabel) {
            progressLabel.textContent = `Вопрос ${currentIndex + 1} из ${questions.length}`;
        }

        // подставляем сохранённый ответ, если он был
        answerInput.value = answers[currentIndex] || "";

        if (prevBtn) {
            prevBtn.disabled = currentIndex === 0;
            prevBtn.style.opacity = prevBtn.disabled ? "0.6" : "1";
        }
        if (nextBtn) {
            // кнопка "Следующий" теперь используется как резервный переход
            // Основной сценарий — переход через "Отправить ответ"
            nextBtn.disabled = currentIndex === questions.length - 1;
            nextBtn.style.opacity = nextBtn.disabled ? "0.6" : "1";
        }
    }

    renderCurrentQuestion();

    // -------- 4. Переключение вопросов --------

    function saveCurrentAnswer() {
        if (questions.length === 0) return;
        answers[currentIndex] = answerInput.value.trim();
    }

    function goToNextQuestion() {
        if (currentIndex < questions.length - 1) {
            currentIndex += 1;
            renderCurrentQuestion();
        }
    }

    if (prevBtn) {
        prevBtn.addEventListener("click", function () {
            saveCurrentAnswer();
            if (currentIndex > 0) {
                currentIndex -= 1;
                renderCurrentQuestion();
            }
        });
    }

    if (nextBtn) {
        nextBtn.addEventListener("click", function () {
            // резервный переход без сохранения
            saveCurrentAnswer();
            goToNextQuestion();
        });
    }

    // -------- 5. Отправка ответа --------

    form.addEventListener("submit", function (event) {
        event.preventDefault();

        const name = nameInput.value.trim();
        const contact = contactInput ? contactInput.value.trim() : "";
        const answer = answerInput.value.trim();

        if (!name || !answer) {
            note.textContent = "Заполните имя кандидата и ответ перед отправкой.";
            note.style.color = "#b91c1c";
            form.appendChild(note);
            return;
        }

        if (consentCheckbox && !consentCheckbox.checked) {
            note.textContent = "Поставьте отметку о согласии на обработку персональных данных.";
            note.style.color = "#b91c1c";
            form.appendChild(note);
            return;
        }

        // Здесь позже можно будет вызвать настоящий backend-API
        saveCurrentAnswer();

        note.textContent = `Ответ для вопроса ${currentIndex + 1} сохранён.`;
        note.style.color = "#047857";
        form.appendChild(note);

        // очищаем поле ответа
        answerInput.value = "";
        answers[currentIndex] = "";

        // автоматически переходим к следующему вопросу (если он есть)
        goToNextQuestion();
    });
});