"""Локализация: все тексты бота лежат в locales/<язык>.json, код берёт их по ключу.

Язык клиентов задаёт BOT_LANGUAGE (ru | tk), язык админских сообщений — ADMIN_LANGUAGE
(пусто = как BOT_LANGUAGE). Ключи, начинающиеся с «adm.», считаются админскими.
Нет ключа в выбранном языке — берётся русский; нет и там — возвращается сам ключ (и пишется ошибка в лог).
Чтобы добавить язык, положите рядом ещё один JSON с теми же ключами.
"""
from __future__ import annotations

import contextvars
import json
import logging
import os
from contextlib import contextmanager
from pathlib import Path
from string import Formatter

from app.config import get_settings

log = logging.getLogger(__name__)

BASE_LANG = "ru"
LOCALES_DIR = Path(os.environ.get("LOCALES_DIR") or Path(__file__).resolve().parent.parent / "locales")

_cache: dict[str, dict[str, str]] = {}
_warned: set = set()
_override: dict[str, str] | None = None   # только для тестов
# язык пользователя, чей апдейт сейчас обрабатывается (None — язык по умолчанию, BOT_LANGUAGE)
_user_lang: contextvars.ContextVar[str | None] = contextvars.ContextVar("user_lang", default=None)


def available_languages() -> list[str]:
    return sorted(p.stem for p in LOCALES_DIR.glob("*.json"))


def _load(lang: str) -> dict[str, str]:
    if lang not in _cache:
        path = LOCALES_DIR / f"{lang}.json"
        try:
            _cache[lang] = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            _cache[lang] = {}
        except json.JSONDecodeError as e:
            log.error("locale %s: invalid JSON: %s", path, e)
            _cache[lang] = {}
    return _cache[lang]


def reload() -> None:
    _cache.clear()
    _warned.clear()


def use(bot: str | None, admin: str | None = None) -> None:
    """Принудительно задать языки (тесты). use(None) — вернуться к настройкам из .env."""
    global _override
    _override = None if bot is None else {"bot": bot, "admin": admin or bot}


def _lang_for(key: str) -> str:
    """Админские ключи (adm.*) всегда на ADMIN_LANGUAGE. Остальные: язык пользователя из контекста,
    иначе язык по умолчанию (BOT_LANGUAGE)."""
    if key.startswith("adm."):
        lang = _override["admin"] if _override else get_settings().admin_lang
    else:
        lang = _user_lang.get() or (_override["bot"] if _override else get_settings().bot_lang)
    lang = (lang or BASE_LANG).strip().lower()
    if not _load(lang):
        if lang not in _warned:
            _warned.add(lang)
            log.error("language %r not found in %s, falling back to %s", lang, LOCALES_DIR, BASE_LANG)
        return BASE_LANG
    return lang


def push_user_lang(lang: str | None):
    """Задать язык текущего апдейта. Вернёт токен для pop_user_lang()."""
    return _user_lang.set(lang if lang in available_languages() else None)


def pop_user_lang(token) -> None:
    _user_lang.reset(token)


def set_current_lang(lang: str | None) -> None:
    """Сменить язык внутри обработчика (после выбора языка пользователем)."""
    _user_lang.set(lang if lang in available_languages() else None)


@contextmanager
def lang_ctx(lang: str | None):
    """Собрать текст на языке конкретного получателя: with lang_ctx(lang): text = t(...)."""
    token = push_user_lang(lang)
    try:
        yield
    finally:
        pop_user_lang(token)


def current_lang() -> str:
    """Язык, на котором сейчас формируются клиентские тексты."""
    return _lang_for("client.key")


def language_name(lang: str) -> str:
    """Название языка на нём самом (ключ lang.name в файле языка)."""
    return _load(lang).get("lang.name", lang)


def t(key: str, **kw) -> str:
    lang = _lang_for(key)
    s = _load(lang).get(key)
    if s is None:
        s = _load(BASE_LANG).get(key)
        if s is None:
            log.error("missing i18n key: %s", key)
            return key
        if (lang, key) not in _warned:
            _warned.add((lang, key))
            log.warning("i18n key %s missing in %s, using %s", key, lang, BASE_LANG)
    try:
        return s.format(**kw)
    except (KeyError, IndexError, ValueError) as e:
        log.error("i18n key %s: bad placeholders (%s)", key, e)
        return s


def placeholders(s: str) -> set[str]:
    return {name for _, name, _, _ in Formatter().parse(s) if name}


def check_locales() -> list[str]:
    """Сверка всех языков с русским: отсутствующие/лишние ключи и несовпадение плейсхолдеров."""
    problems: list[str] = []
    base = _load(BASE_LANG)
    for lang in available_languages():
        if lang == BASE_LANG:
            continue
        cur = _load(lang)
        for k in sorted(base.keys() - cur.keys()):
            problems.append(f"{lang}: нет ключа {k}")
        for k in sorted(cur.keys() - base.keys()):
            problems.append(f"{lang}: лишний ключ {k}")
        for k in sorted(base.keys() & cur.keys()):
            if placeholders(base[k]) != placeholders(cur[k]):
                problems.append(f"{lang}: плейсхолдеры {k}: {sorted(placeholders(cur[k]))} вместо {sorted(placeholders(base[k]))}")
    return problems
