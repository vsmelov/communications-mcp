# discord — Discord MCP от user-аккаунта

Часть communications-mcp (рядом с Telegram и `whatsapp/`). Работает от **user-токена** (selfbot), не от бота:
видит все серверы и ЛС аккаунта. Автоматизация user-токена нарушает ToS Discord — поэтому режим по умолчанию
только чтение, пауза 0.6 с между запросами, без параллелизма.

Докер не нужен: у Discord нет долгоживущей сессии, только токен и обычный HTTPS (`discord.com/api/v9`).

## Файлы

- `server.py` — MCP (stdio, FastMCP). Подключён как `discord` (user scope): `claude mcp add discord -s user -e PYTHONUTF8=1 -- python server.py`.
- `dclient.py` — REST-клиент; его же импортируют скрипты (пагинация, пауза, 429, выбор сетевого пути внутри).
- `.env` — `DISCORD_TOKEN`, `DISCORD_WRITE`, `DISCORD_PROXY_CHAIN`. В git не попадает (корневой `.gitignore`).

## Инструменты

| Tool | Что делает |
|---|---|
| `get_session_info` | **Главная ручка**: путь к токену, сетевой путь, флаг записи, готовый импорт `dclient` для скриптов |
| `get_me` | Кто залогинен |
| `list_guilds`, `list_channels(guild)` | Серверы, каналы по категориям |
| `list_dms` | ЛС и групповые, свежие сверху |
| `read_messages(channel)` | Канал: id, `сервер/канал`, `канал`+`guild`, `@username` (ЛС); пагинация before_id/after_id |
| `search_messages(query, guild\|channel)` | Поиск по серверу или каналу, 25 на страницу, `offset` |
| `send_message` | Только при `DISCORD_WRITE=1`; иначе отказ |

Для дампов и обходов — скрипт, не цикл тулов:

```python
import sys; sys.path.insert(0, r"C:\Users\v\PycharmProjects\claude-workspace\communications-mcp\discord")
from dclient import Discord
d = Discord()
for m in d.history(channel_id, limit=5000): ...
```

## Сеть

Из РФ Discord ходит и напрямую, и через прокси VPN-клиента. Порядок: `DISCORD_PROXY_CHAIN` из `.env`, иначе `direct`,
затем http-прокси, найденные `netpath.py` (Happ 10809). Победитель живёт весь процесс; при обрыве подбирается заново.
httpx с `trust_env=False` (системный прокси ломает loopback).

## Токен

- Откуда: Discord Web → F12 → Network → любой запрос к `/api` → заголовок `Authorization`.
- Живёт, пока не сменят пароль, не разлогинят устройства или не выйдут из аккаунта в том браузере. 401 → достать новый.
- В чат токен не вставлять, в логи и ответы не печатать. Если токен засветился (например, в curl из DevTools,
  вставленном в чат), надёжная ротация — смена пароля Discord (выкидывает все токены).
- Первый токен (06.10.2026) был передан через чат; рекомендовано сменить пароль и положить новый токен в `.env`.

## Включение записи

Только пользователь: `DISCORD_WRITE=1` в `.env` + перезапуск MCP. Перед отправкой Claude всегда уточняет текст и адресата.
