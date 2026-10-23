"""Настройка QApplication и сборка компонентов."""
import sys

from PySide6.QtWidgets import QApplication

from bee_pet.bee_window import BeeWindow
from bee_pet.biting import BiteController
from bee_pet.chat import ChatWindow
from bee_pet.llm import BeeClient
from bee_pet.mood import ACTION_PHRASES, Action
from bee_pet.wander import WanderController

MENU_TAG = "menu"


def wire_menu_replies(bee: BeeWindow, client: BeeClient) -> None:
    """Команды меню уходят модели; настоящий ответ показывается в пузыре поверх заготовки."""
    bee.action_performed.connect(
        lambda action: client.request(ACTION_PHRASES[action], tag=(MENU_TAG, action))
        if action in ACTION_PHRASES else None)

    def on_reply(tag: object, text: str, from_model: bool) -> None:
        if isinstance(tag, tuple) and tag and tag[0] == MENU_TAG and from_model:
            bee.say(f"🐝 {text}")

    client.reply_ready.connect(on_reply)


def main() -> None:
    """Запускает приложение: окно пчелы, клиент модели и чат."""
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    bee = BeeWindow()
    client = BeeClient()
    chat = ChatWindow(bee, client)
    wire_menu_replies(bee, client)
    BiteController(bee)  # на не-Windows неактивна (winapi.AVAILABLE = False)
    WanderController(bee, client)  # дочерний объект пчелы; без WinAPI летает к низу экрана
    bee.chat_requested.connect(chat.open_chat)
    app.aboutToQuit.connect(client.shutdown)
    bee.show()
    sys.exit(app.exec())
