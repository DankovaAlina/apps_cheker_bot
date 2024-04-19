import logging
import os
import requests
import sqlalchemy as db
import sys
from datetime import datetime
from dotenv import load_dotenv
from sqlalchemy.orm import sessionmaker
from telegram import (
    InlineKeyboardButton, InlineKeyboardMarkup,
    ReplyKeyboardMarkup, Update
)
from telegram.ext import (
    Application, CallbackQueryHandler, CommandHandler,
    ContextTypes, filters, MessageHandler
)
from uuid import uuid4


load_dotenv()

APPS_LIST_BUTTON = 'Список приложений'
LAUNCH_LINK_BUTTON = 'Сформировать ссылку для запуска'
FAQ_BUTTON = 'FAQ'


logging.basicConfig(
    format='%(asctime)s [%(levelname)s] %(message)s',
    filename='app_checker.log',
    level=logging.INFO)

logger = logging.getLogger(__name__)
handler = logging.StreamHandler(sys.stdout)
logger.addHandler(handler)


engine = db.create_engine("sqlite:///bot_database.sqlite")
Session = sessionmaker(engine)
conn = engine.connect()
session = Session(bind=conn)
metadata = db.MetaData()

Apps = db.Table(
    'Apps', metadata,
    db.Column('Id', db.Integer(), primary_key=True, autoincrement='auto'),
    db.Column('URL', db.String(255), nullable=False, unique=True),
    db.Column('Name', db.String(255), nullable=False, unique=True),
    db.Column('Launch_link', db.String(255), nullable=False, unique=True),
    db.Column('Status', db.String(255), nullable=False, default='Доступно'),
    db.Column('Update_date', db.DateTime(), nullable=True),
    db.Column('Retries', db.Integer(), nullable=False, default=0)
)
Users = db.Table(
    'Users', metadata,
    db.Column('Id', db.Integer(), primary_key=True, autoincrement='auto'),
    db.Column('Chat_id', db.String(255), nullable=False, unique=True),
    db.Column('Token', db.String(255), nullable=True, unique=True),
    db.Column('Is_admin', db.Boolean(), nullable=False, default=False)
)

metadata.create_all(engine)


def run_job(interval, context):
    current_jobs = context.job_queue.jobs()
    for current_job in current_jobs:
        current_job.schedule_removal()
    context.job_queue.run_repeating(job, interval)
    logger.info(f'Интервал запуска джоба установлен на: {interval} сек.')


async def job(context: ContextTypes.DEFAULT_TYPE):
    logger.info('Начало выполнения джоба.')
    apps = conn.execute(db.select(Apps))
    for app in apps:
        result = False
        try:
            response = requests.get(app.URL)
            if response.status_code == 200:
                conn.execute(
                    db.update(Apps).
                    where(Apps.c.URL == app.URL).
                    values(Status='Доступно',
                           Update_date=datetime.now(),
                           Retries=0
                           )
                    )
                result = True
        except Exception:
            logger.warn(
                f'Ошибка проверки доступности приложения {app.Name}.'
            )
        if not result:
            current_retry = app.Retries + 1
            conn.execute(
                db.update(Apps).
                where(Apps.c.URL == app.URL).
                values(Update_date=datetime.now(),
                       Retries=current_retry
                       )
                    )
            if current_retry == 3:
                conn.execute(
                    db.update(Apps).
                    where(Apps.c.URL == app.URL).
                    values(Status='Недоступно')
                        )
                await send_message_to_subscribers(
                    f'Приложение {app.Name} недоступно',
                    context
                )
    conn.commit()
    logger.info('Завершение выполнения джоба.')


async def send_message_to_subscribers(
        message: str,
        context: ContextTypes.DEFAULT_TYPE
):
    logger.info(f'Сообщение для отправки всем подписчикам: {message}')
    users = conn.execute(db.select(Users))
    for user in users:
        await context.bot.send_message(chat_id=user.Chat_id, text=message)


def check_admin(chat_id):
    return session.execute(
        db.select(Users.c.Is_admin).
        where(Users.c.Chat_id == chat_id)
        ).scalar()


async def reply_restricted(update: Update):
    await update.message.reply_text('Нет прав для выполнения данной команды.')


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.message.chat_id
    token = ''
    if len(context.args) > 0:
        token = context.args[0]
    message = 'Бот запущен.'
    if token:
        user_token = session.execute(
            db.select(Users.c.Token).
            where(Users.c.Chat_id == chat_id)
            ).scalar()
        if token == user_token:
            conn.execute(
                db.update(Users).
                where(Users.c.Chat_id == chat_id).
                values(Is_admin=True)
                )
            conn.commit()
            message = 'Бот запущен с правами администратора.'
    reply_keyboard = [[APPS_LIST_BUTTON, LAUNCH_LINK_BUTTON], [FAQ_BUTTON]]
    await update.message.reply_text(
        message,
        reply_markup=ReplyKeyboardMarkup(reply_keyboard)
    )


async def add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    permission = check_admin(update.message.chat_id)
    if not permission:
        await reply_restricted(update)
        return
    if len(context.args) != 3:
        await update.message.reply_text(
            'Команда "/add" ожидает 3 обязательных аргумента: '
            '[URL приложения] [Название] [Ссылка запуска].'
        )
        return
    url = context.args[0]
    app_name = context.args[1]
    launch_link = context.args[2]
    if not url.startswith('http'):
        await update.message.reply_text(
            'Неверный URL.'
        )
        return
    if not launch_link.startswith('http'):
        await update.message.reply_text(
            'Неверная ссылка запуска.'
        )
        return
    logger.info(
        f'Добавление приложения {app_name}, URL: {url}, '
        f'ссылка для запуска: {launch_link}'
    )
    conn.execute(
        db.insert(Apps).
        values(
            URL=url, Name=app_name, Launch_link=launch_link
            )
        )
    conn.commit()
    await update.message.reply_text(f'Приложение {app_name} добавлено.')


async def subscribe(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.message.chat_id
    user_exists = session.query(
        db.exists(Users).
        where(Users.c.Chat_id == chat_id)
        ).scalar()
    if user_exists:
        await update.message.reply_text('Вы уже подписаны на рассылку.')
    conn.execute(db.insert(Users).values(Chat_id=chat_id))
    conn.commit()
    await update.message.reply_text(
        'Вы подписались на рассылку.'
        )


async def broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    permission = check_admin(update.message.chat_id)
    if not permission:
        await reply_restricted(update)
        return
    if not context.args:
        await update.message.reply_text(
            'Команда "/broadcast" ожидает 1 обязательный аргумент: [текст].'
        )
        return
    text = ' '.join(context.args)
    await send_message_to_subscribers(text, context)


async def remove(update: Update, context: ContextTypes.DEFAULT_TYPE):
    permission = check_admin(update.message.chat_id)
    if not permission:
        await reply_restricted(update)
        return
    apps = conn.execute(db.select(Apps))
    if not apps:
        await update.message.reply_text('Нет доступных приложений.')
        return
    keyboard = []
    for app in apps:
        keyboard.append(
            [InlineKeyboardButton(app.Name, callback_data=app.Name)]
        )
    await update.message.reply_text(
        text='Выберите приложение для удаления:',
        reply_markup=InlineKeyboardMarkup(
            keyboard
        )
    )


async def generatekey(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.message.chat_id
    session = Session(bind=conn)
    token = str(uuid4())
    user_exists = session.query(
        db.exists(Users).
        where(Users.c.Chat_id == chat_id)
        ).scalar()
    if user_exists:
        conn.execute(
            db.update(Users).
            where(Users.c.Chat_id == chat_id).
            values(Token=token)
        )
    else:
        conn.execute(db.insert(Users).values(Chat_id=chat_id, Token=token))
    conn.commit()
    await update.message.reply_text(token)


async def launch_link_button(
        update: Update,
        context: ContextTypes.DEFAULT_TYPE
):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(text=f"Ссылка для запуска: {query.data}")


async def remove_app_button(
        update: Update,
        context: ContextTypes.DEFAULT_TYPE
):
    query = update.callback_query
    await query.answer()
    conn.execute(db.delete(Apps).where(Apps.c.Name == query.data))
    conn.commit()
    message = f'Приложение {query.data} удалено.'
    logger.info(message)
    await query.edit_message_text(message)


async def getlaunchlinks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    apps = conn.execute(db.select(Apps))
    if not apps:
        await update.message.reply_text('Нет доступных приложений.')
        return
    keyboard = []
    for app in apps:
        keyboard.append(
            [InlineKeyboardButton(app.Name, callback_data=app.Launch_link)]
        )
    await update.message.reply_text(
        text='Выберите приложение:',
        reply_markup=InlineKeyboardMarkup(
            keyboard
        )
    )


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    apps = conn.execute(db.select(Apps))
    if not apps:
        await update.message.reply_text('Нет доступных приложений.')
        return
    reply = []
    for app in apps:
        reply.append(
            f'Приложение: {app.Name}, Статус: {app.Status}\r\n{app.URL}'
        )
    await update.message.reply_text(
        text='\r\n'.join(reply)
    )


async def setinterval(update: Update, context: ContextTypes.DEFAULT_TYPE):
    permission = check_admin(update.message.chat_id)
    if not permission:
        await reply_restricted(update)
        return
    if len(context.args) != 1:
        await update.message.reply_text(
            'Команда "/setinterval" ожидает 1 '
            'обязательный аргумент: [интервал].'
        )
        return
    interval = int(context.args[0])
    if interval < 60:
        await update.message.reply_text(
            'Интервал не может быть меньше 60 сек.'
        )
        return
    logger.info(f'Установка интервала джоба: {interval} сек.')
    run_job(interval, context)
    await update.message.reply_text(f'Интервал {interval} сек установлен.')


async def help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        '''
        Команды:
        /start [токен*] - запуск бота, токен передается опционально
        /subscribe - подписка на доступность приложений
        /getlaunchlinks - получение ссылки для запуска приложения
        /status - просмотр текущего статуса приложений
        /generatekey - формирование токена администратора, который передается в /start
        Админ-команды:
        /add [URL приложения] [Название] [Ссылка запуска] - добавление приложения в список
        /remove - удаление приложения из списка
        /broadcast [текст] - отправка сообщения всем подписчикам
        /setinterval [интервал] - установка интервала для запуска джоба, в секундах. Минимальное зачение - 60 сек.
        '''
    )


def main():
    application = Application.builder().token(
        os.getenv('TELEGRAM_TOKEN')
    ).build()
    application.add_handler(CommandHandler('start', start))
    application.add_handler(CommandHandler('add', add))
    application.add_handler(CommandHandler('broadcast', broadcast))
    application.add_handler(CommandHandler('subscribe', subscribe))
    application.add_handler(CommandHandler('remove', remove))
    application.add_handler(CommandHandler('generatekey', generatekey))
    application.add_handler(CommandHandler('getlaunchlinks', getlaunchlinks))
    application.add_handler(CommandHandler('status', status))
    application.add_handler(CommandHandler('setinterval', setinterval))
    application.add_handler(CommandHandler('help', help))
    application.add_handler(
        MessageHandler(
            filters.Regex(f'^({APPS_LIST_BUTTON})$'),
            status
        )
    )
    application.add_handler(
        MessageHandler(
            filters.Regex(f'^({LAUNCH_LINK_BUTTON})$'),
            getlaunchlinks
        )
    )
    application.add_handler(
        MessageHandler(
            filters.Regex(f'^({FAQ_BUTTON})$'),
            help
        )
    )
    application.add_handler(
        CallbackQueryHandler(launch_link_button, '^(http)')
    )
    application.add_handler(CallbackQueryHandler(remove_app_button))
    run_job(900, application)
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
