"""Настройка QApplication и сборка компонентов."""
import logging
import sys
from logging.handlers import RotatingFileHandler

from PySide6.QtWidgets import QApplication

from bee_pet.bee_window import BeeWindow
from bee_pet.biting import BiteController
from bee_pet.chat import ChatWindow
from bee_pet.chatter import ChatterController
from bee_pet.frenzy import FrenzyController
from bee_pet.hold import HOLD_TAG, HoldController
from bee_pet.icon_prank import IconPrankController
from bee_pet.llm import BeeClient
from bee_pet.mood import ACTION_PHRASES, Mood
from bee_pet.mood_sync import MoodRequest, MoodSync
from bee_pet.server import log_dir, setup_server
from bee_pet.walk import WalkController
from bee_pet.wander import WanderController

MENU_TAG = "menu"
# Чьи ответы показываются в пузыре над пчелой: команды меню и злость от удержания мышкой.
BUBBLE_SOURCES = (MENU_TAG, HOLD_TAG)


def wire_menu_replies(bee: BeeWindow, client: BeeClient, sync: MoodSync | None = None) -> None:
    """Команды меню уходят модели; её ответ (или заготовка «бзз» без сервера) — в пузыре.

    Так же в пузыре показываются ответы на удержание мышкой (HoldController, HOLD_TAG).

    Настроение выставляет MoodSync: по тегу ответа или по локальному правилу без модели.
    """
    sync = sync if sync is not None else MoodSync(bee, client)
    bee.action_performed.connect(
        lambda action: sync.request(ACTION_PHRASES[action], action, MENU_TAG)
        if action in ACTION_PHRASES else None)

    def on_reply(tag: object, text: str, from_model: bool) -> None:
        if isinstance(tag, MoodRequest) and tag.source in BUBBLE_SOURCES:
            bee.say(f"🐝 {text}")

    client.reply_ready.connect(on_reply)


def wire_history_reset(bee: BeeWindow, client: BeeClient) -> None:
    """Настроение само затухло до neutral — модель забывает диалог и успокаивается вместе с картинкой.

    После «sorry» историю не трогаем: модель видела извинение.
    """
    bee.mood_decayed.connect(
        lambda change: client.clear_history() if change.new is Mood.NEUTRAL else None)


LOG_FORMAT = "%(asctime)s %(name)s: %(message)s"


def setup_logging() -> None:
    """Лог в консоль; без консоли (pythonw, собранный exe) — в %LOCALAPPDATA%\\bee-pet\\bee-pet.log.

    Решения пчелы (укус, отказы, метания, выбор сервера модели) видны в консоли
    при запуске `python -m bee_pet`.
    """
    if sys.stderr is not None:
        handler: logging.Handler = logging.StreamHandler()
    else:
        handler = RotatingFileHandler(log_dir() / "bee-pet.log", maxBytes=1_000_000,
                                      backupCount=1, encoding="utf-8")

        def log_crash(exc_type, exc, tb) -> None:
            # Без консоли падения иначе пропадут бесследно.
            logging.getLogger("bee_pet").critical("необработанная ошибка",
                                                  exc_info=(exc_type, exc, tb))

        sys.excepthook = log_crash
    logging.basicConfig(level=logging.INFO, format=LOG_FORMAT, handlers=[handler])


def main() -> None:
    """Запускает приложение: сервер модели, окно пчелы, клиент модели и чат."""
    setup_logging()
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    # До клиента: BEE_URL / уже запущенный сервер / встроенный llama-server в фоне.
    server = setup_server()
    bee = BeeWindow()
    client = BeeClient()
    if server is not None:
        server.ready.connect(client.mark_online)  # модель загрузилась — без офлайн-паузы
        app.aboutToQuit.connect(server.stop)
    sync = MoodSync(bee, client)  # настроение по ответам модели
    chat = ChatWindow(bee, client, sync)
    wire_menu_replies(bee, client, sync)
    wire_history_reset(bee, client)
    bite = BiteController(bee)  # на не-Windows неактивна (winapi.AVAILABLE = False)
    HoldController(bee, sync)  # долго держат мышкой — злится
    FrenzyController(bee, bite)  # злая пчела мечется, пока укус не ведёт её к окну
    # Дочерние объекты пчелы (живут, пока живёт она); ссылки нужны для связки.
    wander = WanderController(bee, client)  # перелёты по окнам; без окон — на панель задач
    chatter = ChatterController(bee, client, bite)  # жужжит сама по себе; настроение не меняет
    WalkController(bee, wander, chatter, bite)  # спокойная ходит пешком по кромке
    IconPrankController(bee, wander, bite)  # от скуки таскает ярлыки (выключено по умолчанию)
    bee.chat_requested.connect(chat.open_chat)
    app.aboutToQuit.connect(client.shutdown)
    bee.show()
    sys.exit(app.exec())
