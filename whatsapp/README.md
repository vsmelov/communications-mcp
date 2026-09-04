# WhatsApp MCP (GOWA)

WhatsApp-часть communications-mcp. Работает через [GOWA](https://github.com/aldinokemal/go-whatsapp-web-multidevice)
(whatsmeow, только Go, без npm) — подключён субмодулем `gowa/` из нашего форка
`vsmelov/go-whatsapp-web-multidevice`, запинен на проаудированный коммит.

## Модель безопасности

- Субмодуль запинен на SHA `6e00759` — аудит 2026-08-18 (см. ниже). Обновление = merge
  апстрима в форк → ре-аудит диффа → осознанный сдвиг субмодуля.
- Образ собирается локально из субмодуля, готовые образы с Docker Hub не используются.
- Контейнер: non-root (uid 20001), порт только на `127.0.0.1:3000`.
- `.env` (гитигнорится): basic auth обязателен, web UI выключен (самообновляется с GitHub
  мимо пиннинга), вебхуки пустые (в апстримном `.env.example` залиты webhook.site — не
  копировать его бездумно), Chatwoot выключен.
- `storages/` — signal-ключи сессии + база сообщений (plaintext SQLite). Не коммитить,
  бэкапить осознанно.

Итог аудита (6 кандидатов, статический анализ): зловредного кода нет ни в одном;
GOWA выбран за Go-only supply chain (101 модуль, все репутационные), живость (4.6k★),
встроенный MCP и поддержку `WHATSAPP_PROXY` (socks5) для самого WS-соединения.
Известные ограничения: история ищется только с момента пейринга + свежий снапшот от
WhatsApp; send-инструменты нельзя отключить на сервере — гейтятся подтверждением тулзы
в MCP-клиенте; неофициальный клиент = формальное нарушение ToS WhatsApp (риск бана
низкий для личного read-mostly, но не нулевой).

## Запуск

```bash
cd whatsapp
# Windows: перед первой сборкой выключить autocrlf в субмодуле,
# иначе entrypoint.sh получит CRLF и контейнер упадёт на старте
git -C gowa config core.autocrlf false && git -C gowa rm --cached -r -q . && git -C gowa reset --hard HEAD
docker compose up -d --build
# QR для пейринга: логи контейнера или POST /app/login
docker compose logs -f gowa
```

Телефон → WhatsApp → Настройки → Связанные устройства → сканировать QR.

## Подключение к Claude Code

```bash
claude mcp add --transport http whatsapp http://127.0.0.1:3000/mcp --header "Authorization: Basic $(printf 'user:ПАРОЛЬ_ИЗ_ENV' | base64)"
```

Если системный прокси перехватывает localhost — добавить `NO_PROXY=127.0.0.1`.

## MCP-инструменты (5, консолидированные)

- `whatsapp_chat` — список чатов/контактов, чтение и поиск сообщений (read; archive — write)
- `whatsapp_send` — отправка (текст/медиа/локация/опрос/форвард) — **write**
- `whatsapp_message` — react/edit/revoke/delete/mark_read/star, download_media — **write, есть деструктивные**
- `whatsapp_group` — управление группами — **write**
- `whatsapp_app` — статус, QR-логин, reconnect, **logout (сносит сессию)**
