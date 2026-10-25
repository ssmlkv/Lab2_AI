import sys

from .config import Settings
from .telegram_bot import run_telegram


def error_text(error: Exception) -> str:
    if isinstance(error, BaseExceptionGroup):
        return "; ".join(error_text(child) for child in error.exceptions)
    return f"{type(error).__name__}: {error}"


def main():
    try:
        run_telegram(Settings.from_env())
    except KeyboardInterrupt:
        print("\nБот остановлен.")
    except Exception as exc:
        print("Ошибка: " + error_text(exc), file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    main()

