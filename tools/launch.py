"""Точка входа для PyInstaller: `-m bee_pet` не скрипт, поэтому запускаем main() отсюда."""
from bee_pet.app import main

if __name__ == "__main__":
    main()
