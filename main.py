import os
import random
import sqlite3
from telebot import TeleBot, types, custom_filters
from telebot.storage import StateMemoryStorage
from telebot.handler_backends import State, StatesGroup
import config

# Настройки: путь к базе и токен бота
DB_PATH = os.getenv('DB_PATH', 'english_bot.db')           # можно переопределить через env переменную
TOKEN   = config.TELEGRAM_BOT_TOKEN                        # хранится в config.py
if not TOKEN:
    raise RuntimeError("TELEGRAM_BOT_TOKEN не найден в config.py")

# В памяти: текущие карточки (по chat_id) и буфер для многошагового ввода
user_quiz  = {}  # chat_id -> {'target': str, 'translate': str}
add_buffer = {}  # chat_id -> {'word': str}

# Инициализация TeleBot с поддержкой состояний
bot = TeleBot(TOKEN, state_storage=StateMemoryStorage())

# Команды-метки для кнопок
class Command:
    ADD  = 'Добавить слово ➕'
    DEL  = 'Удалить слово 🔙'
    NEXT = 'Дальше ⏭'

# Состояния диалога (для добавления/удаления слова)
class MyStates(StatesGroup):
    entering_word      = State()  # после кнопки ADD ждём ввода английского слова
    entering_translate = State()  # потом ждём ввода перевода
    deleting           = State()  # после кнопки DEL ждём ввода слова для удаления

def get_conn():
    """
    Открывает соединение с SQLite и настраивает row_factory,
    чтобы получать результаты запросов как dict-подобные Row.
    """
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    """
    Создаёт таблицы users, words и user_solved (если их нет).
    Если таблица words полностью пуста, заполняет её 10 базовыми словами.
    """
    conn = get_conn()
    c = conn.cursor()
    # Таблица пользователей
    c.execute("""
      CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        telegram_id INTEGER UNIQUE,
        username TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
      );
    """)
    # Таблица слов: target–translate, added_by=NULL для глобальных
    c.execute("""
      CREATE TABLE IF NOT EXISTS words (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        target TEXT NOT NULL,
        translate TEXT NOT NULL,
        added_by INTEGER REFERENCES users(id)
      );
    """)
    # Таблица отгаданных слов: связывает user_id и word_id
    c.execute("""
      CREATE TABLE IF NOT EXISTS user_solved (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER REFERENCES users(id),
        word_id INTEGER REFERENCES words(id),
        solved_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(user_id, word_id)
      );
    """)
    # Если нет ни одной записи — вставляем 10 предустановленных слов
    count = c.execute("SELECT COUNT(*) AS cnt FROM words").fetchone()['cnt']
    if count == 0:
        initial = [
          ('Red','Красный'), ('Blue','Синий'), ('Green','Зелёный'),
          ('House','Дом'),  ('Car','Машина'),   ('Peace','Мир'),
          ('Hello','Привет'),('She','Она'),     ('They','Они'),
          ('Table','Стол')
        ]
        for t, tr in initial:
            c.execute(
                "INSERT INTO words(target,translate,added_by) VALUES(?,?,NULL)",
                (t, tr)
            )
    conn.commit()
    conn.close()

# Инициализируем БД один раз при старте
init_db()

@bot.message_handler(commands=['start','cards'])
def cmd_start(msg):
    """
    При команде /start или /cards:
    - Приветствуем пользователя
    - Регистрируем его в таблице users, если ещё не был
    - Сбрасываем прошлую карточку (если была)
    - Запускаем отправку новой карточки
    """
    cid = msg.chat.id
    bot.send_message(cid, "Привет! Давай учить английский. 😊")
    conn = get_conn(); c = conn.cursor()
    c.execute(
        "INSERT OR IGNORE INTO users(telegram_id,username) VALUES(?,?)",
        (cid, msg.from_user.username)
    )
    conn.commit(); conn.close()
    user_quiz.pop(cid, None)    # удаляем прошлую цель, если была
    send_quiz(msg)

def send_quiz(msg):
    """
    Основная логика квиза:
    - Берёт user_id по telegram_id
    - Сбрасывает прогресс (user_solved), если все слова уже отгаданы
    - Выбирает случайное слово, которое ещё не отгадано
    - Формирует 4 варианта ответа и отправляет клавиатуру
    """
    cid = msg.chat.id
    conn = get_conn(); c = conn.cursor()

    # Получаем внутренний ID пользователя
    row = c.execute(
        "SELECT id FROM users WHERE telegram_id = ?", (cid,)
    ).fetchone()
    if not row:
        conn.close()
        return cmd_start(msg)  # если пользователя нет, запускаем регистрацию
    uid = row['id']

    # Считаем общее число слов и число уже отгаданных
    total  = c.execute(
        "SELECT COUNT(*) AS cnt FROM words WHERE added_by IS NULL OR added_by = ?", (uid,)
    ).fetchone()['cnt']
    solved = c.execute(
        "SELECT COUNT(*) AS cnt FROM user_solved WHERE user_id = ?", (uid,)
    ).fetchone()['cnt']
    # Если все решены — сбрасываем таблицу user_solved
    if solved >= total:
        c.execute("DELETE FROM user_solved WHERE user_id = ?", (uid,))
        conn.commit()

    # Берём случайную карточку, не отгаданную ранее
    choice = c.execute("""
      SELECT w.id, w.target, w.translate
        FROM words w
       WHERE (w.added_by IS NULL OR w.added_by = ?)
         AND w.id NOT IN (
             SELECT word_id FROM user_solved WHERE user_id = ?
         )
       ORDER BY RANDOM()
       LIMIT 1
    """, (uid, uid)).fetchone()
    conn.close()

    # Сохраняем правильный ответ в памяти
    user_quiz[cid] = {'target': choice['target'], 'translate': choice['translate']}

    # Формируем пул вариантов (все доступные target)
    pool = [r['target'] for r in get_conn().execute(
        "SELECT target FROM words WHERE added_by IS NULL OR added_by = ?", (uid,)
    )]
    # Три случайных «отвлекающих» + правильный
    opts = random.sample([t for t in pool if t != choice['target']], 3) + [choice['target']]
    random.shuffle(opts)

    # Строим клавиатуру с кнопками-ответами
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    for o in opts:
        markup.add(types.KeyboardButton(o))
    # Добавляем кнопки «Дальше», «Добавить слово», «Удалить слово»
    markup.add(types.KeyboardButton(Command.NEXT))
    markup.add(types.KeyboardButton(Command.ADD), types.KeyboardButton(Command.DEL))

    bot.send_message(
        cid,
        f"Выберите перевод: 🇷🇺 {choice['translate']}",
        reply_markup=markup
    )

@bot.message_handler(func=lambda m: m.text == Command.ADD, state=None)
def on_add_request(msg):
    """
    Шаг 1 при добавлении слова:
    Пользователь нажал «Добавить слово», бот запрашивает английское слово.
    """
    cid = msg.chat.id
    bot.send_message(cid, "Введите слово (английский):")
    bot.set_state(msg.from_user.id, MyStates.entering_word, cid)

@bot.message_handler(state=MyStates.entering_word, content_types=['text'])
def on_add_word(msg):
    """
    Запоминаем введённое английское слово и запрашиваем перевод.
    """
    cid = msg.chat.id
    add_buffer[cid] = {'word': msg.text.strip()}
    bot.send_message(cid, "Введите перевод (русский):")
    bot.set_state(msg.from_user.id, MyStates.entering_translate, cid)

@bot.message_handler(state=MyStates.entering_translate, content_types=['text'])
def on_add_translate(msg):
    """
    Шаг 2 при добавлении слова:
    - Проверяем буфер с английским словом
    - Валидируем пользователя
    - Проверяем дубли (target или translate)
    - Вставляем в DB и считаем общее число слов
    - Отвечаем пользователю и возвращаемся к викторине
    """
    cid = msg.chat.id
    buf = add_buffer.pop(cid, {})
    if 'word' not in buf:
        bot.reply_to(msg, "Ошибка, попробуйте снова через кнопку Добавить слово.")
        bot.set_state(msg.from_user.id, None, cid)
        return send_quiz(msg)

    tgt = buf['word']       # английское слово
    tr  = msg.text.strip()  # перевод

    conn = get_conn(); c = conn.cursor()
    user_row = c.execute(
        "SELECT id FROM users WHERE telegram_id = ?", (cid,)
    ).fetchone()
    if not user_row:
        conn.close()
        bot.reply_to(msg, "Сначала нажмите /start")
        bot.set_state(msg.from_user.id, None, cid)
        return

    uid = user_row['id']
    # Проверяем, нет ли уже такого target или translate
    dup = c.execute("""
      SELECT COUNT(*) AS cnt
        FROM words
       WHERE (target = ? OR translate = ?)
         AND (added_by IS NULL OR added_by = ?)
    """, (tgt, tr, uid)).fetchone()['cnt']
    if dup:
        bot.reply_to(msg, f"Слово '{tgt}' или перевод '{tr}' уже есть.")
        conn.close()
        bot.set_state(msg.from_user.id, None, cid)
        return send_quiz(msg)

    # Вставляем новое слово для этого пользователя
    c.execute(
        "INSERT INTO words(target,translate,added_by) VALUES(?,?,?)",
        (tgt, tr, uid)
    )
    # Считаем теперь общее количество слов (глобальных + личных)
    total_words = c.execute(
        "SELECT COUNT(*) AS cnt FROM words WHERE added_by IS NULL OR added_by = ?", (uid,)
    ).fetchone()['cnt']
    conn.commit(); conn.close()

    bot.reply_to(msg, f"Слово '{tgt}' → '{tr}' добавлено. Всего слов: {total_words}")
    bot.set_state(msg.from_user.id, None, cid)
    send_quiz(msg)

@bot.message_handler(func=lambda m: m.text == Command.DEL, state=None)
def on_delete_request(msg):
    """
    При нажатии «Удалить слово»:
    - Проверяем, что пользователь зарегистрирован
    - Выбираем все его добавленные слова
    - Выводим их одной строкой и ждём ввода слова для удаления
    """
    cid = msg.chat.id
    conn = get_conn(); c = conn.cursor()
    user_row = c.execute(
        "SELECT id FROM users WHERE telegram_id = ?", (cid,)
    ).fetchone()
    if not user_row:
        conn.close()
        bot.reply_to(msg, "Сначала нажмите /start")
        return
    uid  = user_row['id']
    rows = c.execute(
        "SELECT target FROM words WHERE added_by = ?", (uid,)
    ).fetchall()
    conn.close()

    if not rows:
        bot.send_message(cid, "У вас нет добавленных слов.")
        return
    user_words = ', '.join(r['target'] for r in rows)
    bot.send_message(
        cid,
        f"Ваши слова для удаления:\n{user_words}\n\nВведите точное слово:"
    )
    bot.set_state(msg.from_user.id, MyStates.deleting, cid)

@bot.message_handler(state=MyStates.deleting, content_types=['text'])
def on_delete_confirm(msg):
    """
    Шаг удаления: получаем слово от пользователя, удаляем его из таблицы words
    только если added_by = этот пользователь.
    """
    cid    = msg.chat.id
    target = msg.text.strip()
    conn = get_conn(); c = conn.cursor()
    user_row = c.execute(
        "SELECT id FROM users WHERE telegram_id = ?", (cid,)
    ).fetchone()
    if not user_row:
        conn.close()
        bot.reply_to(msg, "Сначала нажмите /start")
        bot.set_state(msg.from_user.id, None, cid)
        return
    uid = user_row['id']

    # Выполняем удаление
    res = c.execute(
        "DELETE FROM words WHERE target = ? AND added_by = ?", (target, uid)
    )
    conn.commit(); conn.close()

    if res.rowcount:
        bot.reply_to(msg, f"Слово '{target}' удалено.")
    else:
        bot.reply_to(msg, f"Слово '{target}' не найдено у вас.")
    bot.set_state(msg.from_user.id, None, cid)
    send_quiz(msg)

@bot.message_handler(func=lambda m: True, state=None, content_types=['text'])
def handle_answer(msg):
    """
    Общий хендлер для всех текстовых сообщений в «основном» состоянии:
    - Если нажали «Дальше» или карточки нет → новый вызов send_quiz
    - Иначе сравниваем ввод пользователя с правильным target
      и либо отмечаем отгадку в user_solved, либо даём подсказку
    """
    cid, text = msg.chat.id, msg.text.strip()
    if cid not in user_quiz or text == Command.NEXT:
        return send_quiz(msg)

    correct   = user_quiz[cid]['target']
    translate = user_quiz[cid]['translate']
    if text.lower() == correct.lower():
        conn = get_conn(); c = conn.cursor()
        user_row = c.execute(
            "SELECT id FROM users WHERE telegram_id = ?", (cid,)
        ).fetchone()
        if user_row:
            uid = user_row['id']
            # Помечаем слово решённым
            wid = c.execute(
                "SELECT id FROM words WHERE target = ?", (correct,)
            ).fetchone()['id']
            c.execute(
                "INSERT OR IGNORE INTO user_solved(user_id,word_id) VALUES(?,?)",
                (uid, wid)
            )
            conn.commit()
        conn.close()
        bot.reply_to(msg, "Правильно! 🎉")
        return send_quiz(msg)
    else:
        bot.reply_to(msg, f"Неправильно. Надо: 🇷🇺 {translate}")

# Включаем middleware для работы состояний
bot.add_custom_filter(custom_filters.StateFilter(bot))

if __name__ == '__main__':
    # Запускаем бесконечный poller
    bot.infinity_polling(skip_pending=True)
