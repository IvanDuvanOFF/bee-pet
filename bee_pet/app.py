"""Настройка QApplication и сборка компонентов."""
import sys

from PySide6.QtWidgets import QApplication

from bee_pet.bee_window import BeeWindow
from bee_pet.biting import BiteController
from bee_pet.chat import ChatWindow
from bee_pet.llm import BeeClient
from bee_pet.mood import ACTION_PHRASES, Mood
from bee_pet.mood_sync import MoodRequest, MoodSync
from bee_pet.wander import WanderController

MENU_TAG = "menu"


def wire_menu_replies(bee: BeeWindow, client: BeeClient, sync: MoodSync | None = None) -> None:
    """Команды меню уходят модели; её ответ (или заготовка «бзз» без сервера) — в пузыре.

    Настроение выставляет MoodSync: по тегу ответа или по локальному правилу без модели.
    """
    sync = sync if sync is not None else MoodSync(bee, client)
    bee.action_performed.connect(
        lambda action: sync.request(ACTION_PHRASES[action], action, MENU_TAG)
        if action in ACTION_PHRASES else None)

    def on_reply(tag: object, text: str, from_model: bool) -> None:
        if isinstance(tag, MoodRequest) and tag.source == MENU_TAG:
            bee.say(f"🐝 {text}")

    client.reply_ready.connect(on_reply)


def wire_history_reset(bee: BeeWindow, client: BeeClient) -> None:
    """Настроение само затухло до neutral — модель забывает диалог и успокаивается вместе с картинкой.

    После «sorry» историю не трогаем: модель видела извинение.
    """
    bee.mood_decayed.connect(
        lambda change: client.clear_history() if change.new is Mood.NEUTRAL else None)


def main() -> None:
    """Запускает приложение: окно пчелы, клиент модели и чат."""
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    bee = BeeWindow()
    client = BeeClient()
    sync = MoodSync(bee, client)  # настроение по ответам модели
    chat = ChatWindow(bee, client, sync)
    wire_menu_replies(bee, client, sync)
    wire_history_reset(bee, client)
    BiteController(bee)  # на не-Windows неактивна (winapi.AVAILABLE = False)
    WanderController(bee, client)  # дочерний объект пчелы; без WinAPI летает к низу экрана
    bee.chat_requested.connect(chat.open_chat)
    app.aboutToQuit.connect(client.shutdown)
    bee.show()
    sys.exit(app.exec())
