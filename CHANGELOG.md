# Changelog

## 1.1.0 — 2026-07-10

Срез живого состояния после трёх дней работы (07–10.07). Мост поднят с v3.47 до **v3.53**.

### Мост и UI (ui/server.py, ui/wizard.html)
- **Нативная стройка на fine-tune Qwen** (v3.48–3.49): эксперт строится «действием» модели (create, store:true), мост усыновляет его в global и гоняет приёмку на реальном входе. Новые опциональные поля `config.json`: `llm_agent_id` (кодоген), `design_agent_id` (план/чертёж). Без них всё работает на основном `agent_id`.
- **Чат-память per-session** (v3.50): платформа не хранит keyless-разговоры — мост сам ведёт стенограмму `sessions/<sid>_chat.json` и подаёт историю целиком.
- **План/чертёж на Qwen** (v3.51–3.52): design-split, вычищены мёртвые gpt-4o-заглушки. Канон: клиентские агенты — только платформенный Qwen.
- **Фикс вечного спиннера** (v3.53): пропущенный `return` в run_expert-ветке моста.
- **Вкладка Composer + витрина**: новые маршруты `/x/publish`, `/x/automations`, `/x/compose`, `/x/run_flow`, `/x/run_process`, `/x/my_library`, `/x/cap_search`, `/x/cap_install`, `/x/cap_remove`, `/x/configure`; EN/RU-переключатель.

### Эксперты
- Новые: `wz_session_prune` (чистка сессий, опасное действие только при `apply=true`), `wz_publish_pack` («Поделиться» → карточка в витрину), композитор — `wz_auto_compose`, `wz_flow_run`, `wz_capability_search`, `wz_capability_install`, `wz_capability_uninstall` (честное удаление с устройства).
- Обновлены: `wz_build_plan` (max_output_tokens 16000 — фикс обрезки больших планов), `wz_cli_capability_factory` (богатые описания + фразы для семантического поиска), `wz_connector_telegram` (+`mode='send_document'` — отправка файлов), `wz_scheduler_tick` (+обработка входящих через `reply_expert`).

## 1.0.0 — 2026-07-07
Первая передача: 31 эксперт, мост v3.47, инструкции Визарда и Строителя, INSTALL/HANDOUT.
