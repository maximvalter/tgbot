import random
import psycopg2
from telebot import TeleBot, types
import config

# Получаем настройки из config.py
DB_CONFIG = config.DB_CONFIG
TOKEN = config.TELEGRAM_BOT_TOKEN
if not TOKEN:
    raise RuntimeError("TELEGRAM_BOT_TOKEN не найден в config.py")

# В памяти: текущие карточки (по chat_id) и буфер для многошагового ввода
user_quiz = {}  # chat_id -> {'target': str, 'translate': str}
add_buffer = {}  # chat_id -> {'en': str}
user_add_state = {}  # chat_id -> 'wait_en' | 'wait_ru'
user_del_state = {}  # chat_id -> True/False

# Инициализация TeleBot
bot = TeleBot(TOKEN)

# Команды-метки для кнопок
class Command:
    ADD = 'Добавить слово ➕'
    DEL = 'Удалить слово 🔙'
    NEXT = 'Дальше ⏭'

def get_conn():
    conn = psycopg2.connect(
        dbname=DB_CONFIG['dbname'],
        user=DB_CONFIG['user'],
        password=DB_CONFIG['password'],
        host=DB_CONFIG['host'],
        port=DB_CONFIG['port']
    )
    return conn

def init_db():
    conn = get_conn()
    c = conn.cursor()

    c.execute("""
      CREATE TABLE IF NOT EXISTS users (
        id SERIAL PRIMARY KEY,
        telegram_id BIGINT UNIQUE,
        username TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
      );
    """)
    c.execute("""
      CREATE TABLE IF NOT EXISTS words (
        id SERIAL PRIMARY KEY,
        target TEXT NOT NULL,
        translate TEXT NOT NULL,
        added_by INTEGER REFERENCES users(id)
      );
    """)
    c.execute("""
      CREATE TABLE IF NOT EXISTS user_solved (
        id SERIAL PRIMARY KEY,
        user_id INTEGER REFERENCES users(id),
        word_id INTEGER REFERENCES words(id),
        solved_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(user_id, word_id)
      );
    """)
    c.execute("SELECT COUNT(*) FROM words")
    count_result = c.fetchone()
    if count_result[0] == 0:
        initial = [
            ('Red', 'Красный'), ('Blue', 'Синий'), ('Green', 'Зелёный'),
            ('House', 'Дом'), ('Car', 'Машина'), ('Peace', 'Мир'),
            ('Hello', 'Привет'), ('She', 'Она'), ('They', 'Они'),
            ('Table', 'Стол')
        ]
        for t, tr in initial:
            c.execute(
                "INSERT INTO words(target, translate, added_by) VALUES(%s, %s, NULL)",
                (t, tr)
            )
    conn.commit()
    conn.close()

init_db()

@bot.message_handler(commands=['start', 'cards'])
def cmd_start(msg):
    cid = msg.chat.id
    bot.send_message(cid, "Привет! Давай учить английский. 😊")
    conn = get_conn()
    c = conn.cursor()
    print(f"Регистрация пользователя: {cid} — {msg.from_user.username}")
    c.execute(
        "INSERT INTO users(telegram_id, username) VALUES(%s, %s) ON CONFLICT DO NOTHING",
        (cid, msg.from_user.username)
    )
    conn.commit()
    c.execute("SELECT id FROM users WHERE telegram_id = %s", (cid,))
    row = c.fetchone()
    if not row:
        bot.send_message(cid, "Ошибка! Пользователь не найден в базе данных.")
        conn.close()
        return
    conn.close()
    user_quiz.pop(cid, None)
    send_quiz(msg)

def send_quiz(msg):
    cid = msg.chat.id
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT id FROM users WHERE telegram_id = %s", (cid,))
    row = c.fetchone()
    if not row:
        bot.send_message(cid, "Ошибка! Пользователь не найден.")
        conn.close()
        return
    uid = row[0]
    c.execute(
        "SELECT COUNT(*) AS cnt FROM words WHERE added_by IS NULL OR added_by = %s", (uid,)
    )
    count_result = c.fetchone()
    total = count_result[0] if count_result else 0
    c.execute(
        "SELECT COUNT(*) AS cnt FROM user_solved WHERE user_id = %s", (uid,)
    )
    solved_result = c.fetchone()
    solved = solved_result[0] if solved_result else 0
    if solved >= total:
        c.execute("DELETE FROM user_solved WHERE user_id = %s", (uid,))
        conn.commit()
    c.execute("""
        SELECT w.id, w.target, w.translate
          FROM words w
         WHERE (w.added_by IS NULL OR w.added_by = %s)
           AND w.id NOT IN (
               SELECT word_id FROM user_solved WHERE user_id = %s
           )
       ORDER BY RANDOM() LIMIT 1
    """, (uid, uid))
    choice = c.fetchone()
    if not choice:
        bot.send_message(cid, "Все слова отгаданы!")
        conn.close()
        return
    c.execute("""
        SELECT w.target FROM words w
         WHERE (w.added_by IS NULL OR w.added_by = %s)
           AND w.id != %s
       ORDER BY RANDOM() LIMIT 3
    """, (uid, choice[0]))
    distractors = [r[0] for r in c.fetchall()]
    options = distractors + [choice[1]]
    random.shuffle(options)
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    for o in options:
        markup.add(types.KeyboardButton(o))
    markup.add(types.KeyboardButton(Command.NEXT))
    markup.add(types.KeyboardButton(Command.ADD), types.KeyboardButton(Command.DEL))
    bot.send_message(
        cid,
        f"Выберите перевод: 🇷🇺 {choice[2]}",
        reply_markup=markup
    )
    user_quiz[cid] = {'target': choice[1], 'translate': choice[2]}
    conn.close()

@bot.message_handler(func=lambda m: m.text == Command.ADD)
def add_word_step1(msg):
    cid = msg.chat.id
    user_add_state[cid] = 'wait_en'
    bot.send_message(cid, "Введи английское слово, которое хочешь добавить:")

@bot.message_handler(func=lambda m: user_add_state.get(m.chat.id) == 'wait_en')
def add_word_step2(msg):
    cid = msg.chat.id
    add_buffer[cid] = {'en': msg.text.strip()}
    user_add_state[cid] = 'wait_ru'
    bot.send_message(cid, "Теперь введи перевод на русский:")

@bot.message_handler(func=lambda m: user_add_state.get(m.chat.id) == 'wait_ru')
def add_word_finish(msg):
    cid = msg.chat.id
    en = add_buffer[cid]['en']
    ru = msg.text.strip()
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT id FROM users WHERE telegram_id = %s", (cid,))
    user_row = c.fetchone()
    if not user_row:
        bot.send_message(cid, "Ошибка! Сначала нажмите /start.")
        conn.close()
        user_add_state.pop(cid, None)
        add_buffer.pop(cid, None)
        return
    uid = user_row[0]
    # Проверка дубля
    c.execute(
        "SELECT id FROM words WHERE (target = %s OR translate = %s) AND (added_by IS NULL OR added_by = %s)",
        (en, ru, uid)
    )
    if c.fetchone():
        bot.send_message(cid, f"Слово '{en}' или перевод '{ru}' уже есть.")
        conn.close()
        user_add_state.pop(cid, None)
        add_buffer.pop(cid, None)
        return send_quiz(msg)
    c.execute(
        "INSERT INTO words(target, translate, added_by) VALUES(%s, %s, %s)",
        (en, ru, uid)
    )
    conn.commit()
    c.execute(
        "SELECT COUNT(*) FROM words WHERE added_by IS NULL OR added_by = %s", (uid,)
    )
    total_words = c.fetchone()[0]
    conn.close()
    bot.send_message(cid, f"Слово '{en}' → '{ru}' добавлено. Всего слов: {total_words}")
    user_add_state.pop(cid, None)
    add_buffer.pop(cid, None)
    send_quiz(msg)

@bot.message_handler(func=lambda m: m.text == Command.DEL)
def del_word_step1(msg):
    cid = msg.chat.id
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT id FROM users WHERE telegram_id = %s", (cid,))
    user_row = c.fetchone()
    if not user_row:
        bot.send_message(cid, "Ошибка! Сначала нажмите /start.")
        conn.close()
        return
    uid = user_row[0]
    c.execute("SELECT target FROM words WHERE added_by = %s", (uid,))
    rows = c.fetchall()
    conn.close()
    if not rows:
        bot.send_message(cid, "У тебя нет добавленных слов.")
        return
    user_del_state[cid] = True
    user_words = ', '.join(r[0] for r in rows)
    bot.send_message(
        cid,
        f"Твои слова для удаления:\n{user_words}\n\nВведи точное слово для удаления:"
    )

@bot.message_handler(func=lambda m: user_del_state.get(m.chat.id))
def del_word_step2(msg):
    cid = msg.chat.id
    target = msg.text.strip()
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT id FROM users WHERE telegram_id = %s", (cid,))
    user_row = c.fetchone()
    if not user_row:
        bot.send_message(cid, "Ошибка! Сначала нажмите /start.")
        conn.close()
        user_del_state.pop(cid, None)
        return
    uid = user_row[0]
    c.execute("DELETE FROM words WHERE target = %s AND added_by = %s", (target, uid))
    conn.commit()
    if c.rowcount:
        bot.send_message(cid, f"Слово '{target}' удалено.")
    else:
        bot.send_message(cid, f"Слово '{target}' не найдено у тебя.")
    conn.close()
    user_del_state.pop(cid, None)
    send_quiz(msg)

@bot.message_handler(func=lambda m: True)
def handle_answer(msg):
    cid = msg.chat.id
    text = msg.text.strip()
    if cid in user_add_state or cid in user_del_state:
        # Если в процессе добавления/удаления — не отвечаем тут!
        return
    if cid not in user_quiz or text == Command.NEXT:
        return send_quiz(msg)
    correct = user_quiz[cid]['target']
    translate = user_quiz[cid]['translate']
    print(f"Пользователь {cid} дал ответ: {text}, правильный ответ: {correct}")
    if text.lower() == correct.lower():
        conn = get_conn()
        c = conn.cursor()
        c.execute("SELECT id FROM users WHERE telegram_id = %s", (cid,))
        user_row = c.fetchone()
        if not user_row:
            print(f"Пользователь с telegram_id {cid} не найден.")
            bot.reply_to(msg, "Ошибка! Пользователь не найден.")
            conn.close()
            return
        uid = user_row[0]
        c.execute("SELECT id FROM words WHERE target = %s", (correct,))
        wid_result = c.fetchone()
        if not wid_result:
            print(f"Слово {correct} не найдено в базе данных.")
            bot.reply_to(msg, f"Ошибка! Слово {correct} не найдено в базе данных.")
            conn.close()
            return
        wid = wid_result[0]
        c.execute(
            "INSERT INTO user_solved(user_id, word_id) VALUES(%s, %s) ON CONFLICT DO NOTHING",
            (uid, wid)
        )
        conn.commit()
        conn.close()
        bot.reply_to(msg, "Правильно! 🎉")
        send_quiz(msg)
    else:
        print(f"Неправильный ответ: {text}. Ожидался: {translate}")
        bot.reply_to(msg, f"Неправильно. Надо: 🇷🇺 {translate}")

if __name__ == '__main__':
    try:
        print("Бот запускается...")
        bot.infinity_polling(skip_pending=True)
    except Exception as e:
        print(f"Произошла ошибка: {e}")
