"""Готовые русские реакции пчелы на действия меню (до подключения модели)."""
from bee_pet.mood import Action

REACTIONS: dict[Action, str] = {
    Action.FLOWER: "ура, цветочек! 🌼",
    Action.PET: "мррр... бзз 😊",
    Action.SWAT: "БЗЗ!! 😠",
    Action.SORRY: "бзз... ладно, прощаю",
    Action.SPRAY: "кхе-кхе! бзз... 😢",
}

# Реакции, когда пчела уже злится.
ANGRY_REACTIONS: dict[Action, str] = {
    Action.FLOWER: "не подлизывайся! 😡",
    Action.LOVE: "не подлизывайся! 😡",
    Action.PET: "не подлизывайся! 😡",
    Action.SWAT: "БЗЗЗ!!! 😡",
    Action.SPRAY: "ЩА КАК ЖАЛЮ! 😡",
    Action.SORRY: "ладно... бзз",
}


def reaction(action: Action, was_angry: bool) -> str:
    """Подбирает реплику; was_angry — злилась ли пчела до действия."""
    if was_angry and action in ANGRY_REACTIONS:
        return ANGRY_REACTIONS[action]
    return REACTIONS.get(action, "бзз")
