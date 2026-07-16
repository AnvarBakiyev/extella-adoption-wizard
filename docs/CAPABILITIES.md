# Extella — единый реестр возможностей (Capability Registry v0)

**Что это:** одна инвентаризация возможностей платформы для всех четырёх поверхностей
(Chat, Wizard, Composer, Workspaces) — вместо четырёх раздельных каталогов (ТЗ v2 §8.9).
Файл генерируется скриптом `scripts/capability_registry.py`; машинное зеркало — KV
`capability:registry` (global, скоуп agent_extella_default; мост отдаёт через `/x/registry`).
**Не редактировать руками** — перегенерируйте скриптом.

_Сгенерировано: 2026-07-16T11:41Z · всего возможностей: 260_

## Автоматизации (витрина — карточки процессов и паков) · 18

| id | название | описание | поверхности | источник |
|---|---|---|---|---|
| `Competitor Intelligence` | Competitor Intelligence | Daily competitor digest — GitHub releases, RSS, Reddit, Hacker News → your Qwen fine-tune → Slack. In-contour, on schedu | wizard workspace chat | _mkt_automations |
| `Автоматизированная подготовка кадровых документов (ТК РК)` | Автоматизированная подготовка кадровых документов (ТК РК) | Кадровый приказ — за минуты, без ошибок | wizard workspace chat | _mkt_automations |
| `⭐ Google Reviews Thank-You Automation` | ⭐ Google Reviews Thank-You Automation | Pull new 5-star Google reviews and post thank-you drafts to Slack | wizard workspace chat | _mkt_automations |
| `🏷️ Мониторинг цен конкурентов` | 🏷️ Мониторинг цен конкурентов | Еженедельный сбор цен конкурентов с сайтов и отправка сводки на почту | wizard workspace chat | _mkt_automations |
| `💳 Забытые подписки в выписках` | 💳 Забытые подписки в выписках | Прекращу утечку денег на забытые подписки | wizard workspace chat | _mkt_automations |
| `💳 Поиск забытых подписок` | 💳 Поиск забытых подписок | Анализ банковских выписок для обнаружения забытых или неиспользуемых подписок | wizard workspace chat | _mkt_automations |
| `📚 База знаний из PDF` | 📚 База знаний из PDF | Индексация папки с PDF-документами в приватную локальную базу знаний для поиска и ответов на вопросы. | wizard workspace chat | _mkt_automations |
| `📸 Архив семейных фото` | 📸 Архив семейных фото | Создание руководства по организации семейного фотоархива (фактическая обработка изображений недоступна) | wizard workspace chat | _mkt_automations |
| `📸 Оцифровка и архив семейных фото` | 📸 Оцифровка и архив семейных фото | Планирование структуры и создание шаблонов для семейного фотоархива без использования OCR. | wizard workspace chat | _mkt_automations |
| `📸 Оцифровка и архивация семейных фото` | 📸 Оцифровка и архивация семейных фото | Частичная автоматизация оцифровки отсканированных фотографий в формате PDF с созданием локальной поисковой базы знаний с | wizard workspace chat | _mkt_automations |
| `📸 Подбор фотографий для бизнес-интервью` | 📸 Подбор фотографий для бизнес-интервью | Создание чек-листа и рекомендаций по отбору фотографий с iPhone для публикации в деловом издании, так как прямой доступ  | wizard workspace chat | _mkt_automations |
| `📸 Семейный архив: оцифровка и систематизация` | 📸 Семейный архив: оцифровка и систематизация | Частичная автоматизация создания семейного архива через OCR отсканированных документов и индексацию в локальную базу зна | wizard workspace chat | _mkt_automations |
| `📸 Семейный фотоархив` | 📸 Семейный фотоархив | Подготовка структуры и метаданных для семейного архива, так как в каталоге нет инструментов для обработки изображений. | wizard workspace chat | _mkt_automations |
| `📸 оцифровка и архивация семейных фото` | 📸 оцифровка и архивация семейных фото | Создание плана и структуры для оцифровки старых фотографий и организации семейного архива с распознаванием лиц. | wizard workspace chat | _mkt_automations |
| `🔍 Поиск забытых подписок` | 🔍 Поиск забытых подписок | Сканирует банковские выписки в CSV-формате, выявляет повторяющиеся подписки и рекомендует, от каких стоит отказаться. | wizard workspace chat | _mkt_automations |
| `🕵️ Дайджест конкурентов` | 🕵️ Дайджест конкурентов | Сбор упоминаний конкурентов из RSS, GitHub, Reddit и Hacker News с формированием аналитического дайджеста. | wizard workspace chat | _mkt_automations |
| `🖼️ Оцифровка и архив фото` | 🖼️ Оцифровка и архив фото | Структурирование семейного фотоархива с использованием доступных текстовых инструментов, так как обработка изображений о | wizard workspace chat | _mkt_automations |
| `🗂️ Личная база знаний из файлов` | 🗂️ Личная база знаний из файлов | Сканирует локальные файлы пользователя, извлекает текст, индексирует в RAG-базу знаний и формирует структурированный отч | wizard workspace chat | _mkt_automations |

## Прикладные эксперты (исполняемые блоки платформы) · 159

| id | название | описание | поверхности | источник |
|---|---|---|---|---|
| `bwk_run_pipeline` | bwk_run_pipeline | Auto-generated process orchestrator: runs the contract pipeline (bwk_parse_sales_data -> bwk_calculate_sales_summary) on | chat wizard composer | experts_db |
| `cap_audio_effect` | cap_audio_effect | Аудио-эффекты: темп/тон/эхо/шумоподавление/громкость (через Audacity-обвязку+sox) | chat wizard composer | experts_db |
| `cap_audio_resolver` | cap_audio_resolver | CLI Capability аудио-эффекты (Audacity/sox) — резолвер | chat wizard composer | experts_db |
| `cap_calibre_resolver` | cap_calibre_resolver | Установка и проверка инструмента «Calibre (конвертация книг)» (brew). Зови ПЕРЕД первым использованием этой способности. | chat wizard composer | experts_db |
| `cap_calibre_to_epub` | cap_calibre_to_epub | Конвертер книг — EPUB, MOBI, PDF, FB2 — конвертация электронных книг, локально Операция «В EPUB», один файл. Локально/оф | chat wizard composer | experts_db |
| `cap_calibre_to_epub_batch` | cap_calibre_to_epub_batch | Конвертер книг — EPUB, MOBI, PDF, FB2 — конвертация электронных книг, локально Пакетно: операция «В EPUB» ко всем подход | chat wizard composer | experts_db |
| `cap_calibre_to_pdf` | cap_calibre_to_pdf | Конвертер книг — EPUB, MOBI, PDF, FB2 — конвертация электронных книг, локально Операция «В PDF», один файл. Локально/офл | chat wizard composer | experts_db |
| `cap_calibre_to_pdf_batch` | cap_calibre_to_pdf_batch | Конвертер книг — EPUB, MOBI, PDF, FB2 — конвертация электронных книг, локально Пакетно: операция «В PDF» ко всем подходя | chat wizard composer | experts_db |
| `cap_cwebp_resolver` | cap_cwebp_resolver | Установка и проверка инструмента «cwebp (в WebP)» (brew). Зови ПЕРЕД первым использованием этой способности. | chat wizard composer | experts_db |
| `cap_cwebp_to_webp` | cap_cwebp_to_webp | В WebP — Картинки в лёгкий формат WebP для сайтов — пачкой, локально Операция «В WebP», один файл. Локально/офлайн. Зови | chat wizard composer | experts_db |
| `cap_cwebp_to_webp_batch` | cap_cwebp_to_webp_batch | В WebP — Картинки в лёгкий формат WebP для сайтов — пачкой, локально Пакетно: операция «В WebP» ко всем подходящим файла | chat wizard composer | experts_db |
| `cap_exiftool_resolver` | cap_exiftool_resolver | Установка и проверка инструмента «ExifTool (метаданные фото)» (brew). Зови ПЕРЕД первым использованием этой способности. | chat wizard composer | experts_db |
| `cap_exiftool_strip` | cap_exiftool_strip | Очистить метаданные фото — Убирает геолокацию и данные камеры из фото — приватность, локально Операция «Удалить метаданн | chat wizard composer | experts_db |
| `cap_exiftool_strip_batch` | cap_exiftool_strip_batch | Очистить метаданные фото — Убирает геолокацию и данные камеры из фото — приватность, локально Пакетно: операция «Удалить | chat wizard composer | experts_db |
| `cap_ffmpeg_extract_audio` | cap_ffmpeg_extract_audio | Видео и аудио — Перекодировать, сжать и извлечь аудио из медиатеки Операция «Извлечь аудио (MP3)», один файл. Локально/о | chat wizard composer | experts_db |
| `cap_ffmpeg_extract_audio_batch` | cap_ffmpeg_extract_audio_batch | Видео и аудио — Перекодировать, сжать и извлечь аудио из медиатеки Пакетно: операция «Извлечь аудио (MP3)» ко всем подхо | chat wizard composer | experts_db |
| `cap_ffmpeg_resolver` | cap_ffmpeg_resolver | Установка и проверка инструмента «ffmpeg (видео и аудио)» (brew). Зови ПЕРЕД первым использованием этой способности. | chat wizard composer | experts_db |
| `cap_ffmpeg_to_mp4` | cap_ffmpeg_to_mp4 | Видео и аудио — Перекодировать, сжать и извлечь аудио из медиатеки Операция «В MP4 (H.264)», один файл. Локально/офлайн. | chat wizard composer | experts_db |
| `cap_ffmpeg_to_mp4_batch` | cap_ffmpeg_to_mp4_batch | Видео и аудио — Перекодировать, сжать и извлечь аудио из медиатеки Пакетно: операция «В MP4 (H.264)» ко всем подходящим  | chat wizard composer | experts_db |
| `cap_flac_encode` | cap_flac_encode | Сжать аудио (FLAC) — WAV в FLAC без потери качества — пачкой, локально Операция «WAV → FLAC», один файл. Локально/офлайн | chat wizard composer | experts_db |
| `cap_flac_encode_batch` | cap_flac_encode_batch | Сжать аудио (FLAC) — WAV в FLAC без потери качества — пачкой, локально Пакетно: операция «WAV → FLAC» ко всем подходящим | chat wizard composer | experts_db |
| `cap_flac_resolver` | cap_flac_resolver | Установка и проверка инструмента «FLAC (сжать аудио)» (brew). Зови ПЕРЕД первым использованием этой способности. | chat wizard composer | experts_db |
| `cap_ghostscript_compress_pdf` | cap_ghostscript_compress_pdf | Сжать PDF — Ужать PDF на 50–70% — локально, файлы не уходят Операция «Сжать PDF», один файл. Локально/офлайн. Зови ЭТОТ  | chat wizard composer | experts_db |
| `cap_ghostscript_compress_pdf_batch` | cap_ghostscript_compress_pdf_batch | Сжать PDF — Ужать PDF на 50–70% — локально, файлы не уходят Пакетно: операция «Сжать PDF» ко всем подходящим файлам в па | chat wizard composer | experts_db |
| `cap_ghostscript_resolver` | cap_ghostscript_resolver | Установка и проверка инструмента «Ghostscript (сжатие PDF)» (brew). Зови ПЕРЕД первым использованием этой способности. | chat wizard composer | experts_db |
| `cap_gifsicle_optimize` | cap_gifsicle_optimize | Сжать GIF — Оптимизирует и уменьшает GIF-анимации — пачкой, локально Операция «Оптимизировать GIF», один файл. Локально/ | chat wizard composer | experts_db |
| `cap_gifsicle_optimize_batch` | cap_gifsicle_optimize_batch | Сжать GIF — Оптимизирует и уменьшает GIF-анимации — пачкой, локально Пакетно: операция «Оптимизировать GIF» ко всем подх | chat wizard composer | experts_db |
| `cap_gifsicle_resolver` | cap_gifsicle_resolver | Установка и проверка инструмента «Gifsicle (оптимизация GIF)» (brew). Зови ПЕРЕД первым использованием этой способности. | chat wizard composer | experts_db |
| `cap_graphviz_resolver` | cap_graphviz_resolver | Установка и проверка инструмента «Graphviz (схемы из текста)» (brew). Зови ПЕРЕД первым использованием этой способности. | chat wizard composer | experts_db |
| `cap_graphviz_to_png` | cap_graphviz_to_png | Схемы из текста — Рисует диаграммы и графы из .dot-описания — локально Операция «Схема → PNG», один файл. Локально/офлай | chat wizard composer | experts_db |
| `cap_graphviz_to_png_batch` | cap_graphviz_to_png_batch | Схемы из текста — Рисует диаграммы и графы из .dot-описания — локально Пакетно: операция «Схема → PNG» ко всем подходящи | chat wizard composer | experts_db |
| `cap_imagemagick_resize` | cap_imagemagick_resize | Пакет картинок — Размер и формат тысяч изображений разом — локально Операция «Уменьшить», один файл. Локально/офлайн. Зо | chat wizard composer | experts_db |
| `cap_imagemagick_resize_batch` | cap_imagemagick_resize_batch | Пакет картинок — Размер и формат тысяч изображений разом — локально Пакетно: операция «Уменьшить» ко всем подходящим фай | chat wizard composer | experts_db |
| `cap_imagemagick_resolver` | cap_imagemagick_resolver | Установка и проверка инструмента «ImageMagick (пакет картинок)» (brew). Зови ПЕРЕД первым использованием этой способност | chat wizard composer | experts_db |
| `cap_imagemagick_to_jpg` | cap_imagemagick_to_jpg | Пакет картинок — Размер и формат тысяч изображений разом — локально Операция «В JPG», один файл. Локально/офлайн. Зови Э | chat wizard composer | experts_db |
| `cap_imagemagick_to_jpg_batch` | cap_imagemagick_to_jpg_batch | Пакет картинок — Размер и формат тысяч изображений разом — локально Пакетно: операция «В JPG» ко всем подходящим файлам  | chat wizard composer | experts_db |
| `cap_img2pdf_resolver` | cap_img2pdf_resolver | Установка и проверка инструмента «img2pdf (картинки → PDF)» (brew). Зови ПЕРЕД первым использованием этой способности. | chat wizard composer | experts_db |
| `cap_img2pdf_to_pdf` | cap_img2pdf_to_pdf | Картинки → PDF — Собрать PDF из картинок и сканов — локально Операция «Картинка → PDF», один файл. Локально/офлайн. Зови | chat wizard composer | experts_db |
| `cap_img2pdf_to_pdf_batch` | cap_img2pdf_to_pdf_batch | Картинки → PDF — Собрать PDF из картинок и сканов — локально Пакетно: операция «Картинка → PDF» ко всем подходящим файла | chat wizard composer | experts_db |
| `cap_libreoffice_resolver` | cap_libreoffice_resolver | Установка и проверка инструмента «LibreOffice (Office → PDF)» (brew). Зови ПЕРЕД первым использованием этой способности. | chat wizard composer | experts_db |
| `cap_libreoffice_to_pdf` | cap_libreoffice_to_pdf | Office → PDF — Word, Excel, PowerPoint → PDF целыми папками Операция «Office → PDF», один файл. Локально/офлайн. Зови ЭТ | chat wizard composer | experts_db |
| `cap_libreoffice_to_pdf_batch` | cap_libreoffice_to_pdf_batch | Office → PDF — Word, Excel, PowerPoint → PDF целыми папками Пакетно: операция «Office → PDF» ко всем подходящим файлам в | chat wizard composer | experts_db |
| `cap_local_ask` | cap_local_ask | Спросить локальную модель через Ollama (данные не уходят в облако) | chat wizard composer | experts_db |
| `cap_localmodel_install` | cap_localmodel_install | Установка локальной модели через Ollama (headless, приватно) | chat wizard composer | experts_db |
| `cap_ocr_resolver` | cap_ocr_resolver | Установка и проверка инструмента «OCR (поиск по сканам)» (brew). Зови ПЕРЕД первым использованием этой способности. | chat wizard composer | experts_db |
| `cap_ocr_searchable` | cap_ocr_searchable | Поиск по сканам (OCR) — Сканы и фото-PDF → документы с полнотекстовым поиском Операция «Скан → PDF с поиском», один файл | chat wizard composer | experts_db |
| `cap_ocr_searchable_batch` | cap_ocr_searchable_batch | Поиск по сканам (OCR) — Сканы и фото-PDF → документы с полнотекстовым поиском Пакетно: операция «Скан → PDF с поиском» к | chat wizard composer | experts_db |
| `cap_oxipng_optimize` | cap_oxipng_optimize | PNG без потерь — Ужимает PNG без потери качества (lossless) — пачкой, локально Операция «Сжать PNG без потерь», один фай | chat wizard composer | experts_db |
| `cap_oxipng_optimize_batch` | cap_oxipng_optimize_batch | PNG без потерь — Ужимает PNG без потери качества (lossless) — пачкой, локально Пакетно: операция «Сжать PNG без потерь»  | chat wizard composer | experts_db |
| `cap_oxipng_resolver` | cap_oxipng_resolver | Установка и проверка инструмента «oxipng (сжать PNG без потерь)» (brew). Зови ПЕРЕД первым использованием этой способнос | chat wizard composer | experts_db |
| `cap_pandoc_md_to_docx` | cap_pandoc_md_to_docx | Документы из Markdown — Собрать Word/HTML из шаблона и данных — локально, пачкой Операция «Markdown → Word», один файл.  | chat wizard composer | experts_db |
| `cap_pandoc_md_to_docx_batch` | cap_pandoc_md_to_docx_batch | Документы из Markdown — Собрать Word/HTML из шаблона и данных — локально, пачкой Пакетно: операция «Markdown → Word» ко  | chat wizard composer | experts_db |
| `cap_pandoc_md_to_html` | cap_pandoc_md_to_html | Документы из Markdown — Собрать Word/HTML из шаблона и данных — локально, пачкой Операция «Markdown → HTML», один файл.  | chat wizard composer | experts_db |
| `cap_pandoc_md_to_html_batch` | cap_pandoc_md_to_html_batch | Документы из Markdown — Собрать Word/HTML из шаблона и данных — локально, пачкой Пакетно: операция «Markdown → HTML» ко  | chat wizard composer | experts_db |
| `cap_pandoc_resolver` | cap_pandoc_resolver | Установка и проверка инструмента «Pandoc (конвертация документов)» (brew). Зови ПЕРЕД первым использованием этой способн | chat wizard composer | experts_db |
| `cap_pdftotext_resolver` | cap_pdftotext_resolver | Установка и проверка инструмента «pdftotext (PDF → текст)» (brew). Зови ПЕРЕД первым использованием этой способности. | chat wizard composer | experts_db |
| `cap_pdftotext_to_txt` | cap_pdftotext_to_txt | Текст из PDF — Вытаскивает чистый текст из PDF — пачкой, локально Операция «PDF → текст», один файл. Локально/офлайн. Зо | chat wizard composer | experts_db |
| `cap_pdftotext_to_txt_batch` | cap_pdftotext_to_txt_batch | Текст из PDF — Вытаскивает чистый текст из PDF — пачкой, локально Пакетно: операция «PDF → текст» ко всем подходящим фай | chat wizard composer | experts_db |
| `cap_pngquant_compress` | cap_pngquant_compress | Сжать PNG — Ужимает PNG без видимой потери качества — пачкой, локально Операция «Сжать PNG», один файл. Локально/офлайн. | chat wizard composer | experts_db |
| `cap_pngquant_compress_batch` | cap_pngquant_compress_batch | Сжать PNG — Ужимает PNG без видимой потери качества — пачкой, локально Пакетно: операция «Сжать PNG» ко всем подходящим  | chat wizard composer | experts_db |
| `cap_pngquant_resolver` | cap_pngquant_resolver | Установка и проверка инструмента «pngquant (сжать PNG)» (brew). Зови ПЕРЕД первым использованием этой способности. | chat wizard composer | experts_db |
| `cap_qpdf_optimize` | cap_qpdf_optimize | Повернуть и оптимизировать PDF — Поворот и веб-оптимизация PDF без потери качества Операция «Оптимизировать для веба», о | chat wizard composer | experts_db |
| `cap_qpdf_optimize_batch` | cap_qpdf_optimize_batch | Повернуть и оптимизировать PDF — Поворот и веб-оптимизация PDF без потери качества Пакетно: операция «Оптимизировать для | chat wizard composer | experts_db |
| `cap_qpdf_resolver` | cap_qpdf_resolver | Установка и проверка инструмента «qpdf (структура PDF)» (brew). Зови ПЕРЕД первым использованием этой способности. | chat wizard composer | experts_db |
| `cap_qpdf_rotate` | cap_qpdf_rotate | Повернуть и оптимизировать PDF — Поворот и веб-оптимизация PDF без потери качества Операция «Повернуть», один файл. Лока | chat wizard composer | experts_db |
| `cap_qpdf_rotate_batch` | cap_qpdf_rotate_batch | Повернуть и оптимизировать PDF — Поворот и веб-оптимизация PDF без потери качества Пакетно: операция «Повернуть» ко всем | chat wizard composer | experts_db |
| `cap_rsvg_resolver` | cap_rsvg_resolver | Установка и проверка инструмента «rsvg-convert (SVG → PNG)» (brew). Зови ПЕРЕД первым использованием этой способности. | chat wizard composer | experts_db |
| `cap_rsvg_to_png` | cap_rsvg_to_png | SVG в картинку — Векторные SVG → обычные PNG — пачкой, локально Операция «SVG → PNG», один файл. Локально/офлайн. Зови Э | chat wizard composer | experts_db |
| `cap_rsvg_to_png_batch` | cap_rsvg_to_png_batch | SVG в картинку — Векторные SVG → обычные PNG — пачкой, локально Пакетно: операция «SVG → PNG» ко всем подходящим файлам  | chat wizard composer | experts_db |
| `cos_run_pipeline` | cos_run_pipeline | orchestrator w/ encrypted-file resolver + honest fail | chat wizard composer | experts_db |
| `cx_analyze_channels` | cx_analyze_channels | CX core: Analyzes source-channel distribution in a preprocessed contact-center dialogues dataset (pandas DataFrame pickl | chat wizard composer | experts_db |
| `cx_analyze_dialogue_timing` | cx_analyze_dialogue_timing | CX core: Performs message-by-message timing analysis for a single contact-center dialogue from a pickled chats DataFrame | chat wizard composer | experts_db |
| `cx_analyze_long_dialogues` | cx_analyze_long_dialogues | CX core: Analyzes abnormally long dialogues (above a duration threshold) to determine whether they are real conversation | chat wizard composer | experts_db |
| `cx_anonymize_dataset` | cx_anonymize_dataset | CX core: consistent pseudonymization of a contact-center dialogue dataset before any external LLM call. Builds global ma | chat wizard composer | experts_db |
| `cx_build_dashboard` | cx_build_dashboard | CX core: orchestrator that sequentially runs the five dashboard page generators (cx_page_index, cx_page_quality, cx_page | chat wizard composer | experts_db |
| `cx_build_dashboard_data` | cx_build_dashboard_data | CX core: transforms pipeline artifacts (checklist evaluation JSON + parsed dialogues pkl) into the evidence-dashboard da | chat wizard composer | experts_db |
| `cx_build_demo_version` | cx_build_demo_version | CX core: Builds an anonymized NDA-safe DEMO copy of an HTML dashboard by copying .html/.css/.js files into a demo folder | chat wizard composer | experts_db |
| `cx_check_llm_batch` | cx_check_llm_batch | CX core: Polls an OpenAI-compatible Batch API for the status of every job listed in a jobs-registry JSON file, aggregate | chat wizard composer | experts_db |
| `cx_clean_dialogue` | cx_clean_dialogue | CX core: cleans contact-center dialogue text by removing bot/auto-response noise lines before LLM evaluation. Generic RU | chat wizard composer | experts_db |
| `cx_compute_agreement` | cx_compute_agreement | CX core: Matches human-expert-evaluated dialogues from an xlsx report with per-dialogue LLM evaluation results (phone-ma | chat wizard composer | experts_db |
| `cx_compute_concurrent_chats` | cx_compute_concurrent_chats | CX core: Computes the peak (maximum) number of concurrent chats per operator from a pickled dialogues DataFrame using an | chat wizard composer | experts_db |
| `cx_compute_daily_stats` | cx_compute_daily_stats | CX core: Aggregates contact-center dialogues by day from xlsx export files (daily totals, average CSAT, no-answer counts | chat wizard composer | experts_db |
| `cx_compute_operator_load` | cx_compute_operator_load | CX core: Computes concurrent-dialogue load per operator (max and average concurrent dialogues, percent of time above a n | chat wizard composer | experts_db |
| `cx_compute_operator_stats` | cx_compute_operator_stats | CX core: builds full per-operator statistics by joining a parsed dialogue dataset (pkl) with LLM evaluation results (bat | chat wizard composer | experts_db |
| `cx_compute_redirects` | cx_compute_redirects | CX core: Detects transfers/redirects using a structural transfers-count field (transfers > 1) in a CX analytics data JSO | chat wizard composer | experts_db |
| `cx_compute_survey_metrics` | cx_compute_survey_metrics | CX core: Parses a customer-survey xlsx export and aggregates FRR, tNPS and AP metrics with detractor share by operator,  | chat wizard composer | experts_db |
| `cx_detect_header_row` | cx_detect_header_row | CX core: Detects the real header row in an xlsx export that has metadata rows above the actual column headers by scannin | chat wizard composer | experts_db |
| `cx_detect_misclassification` | cx_detect_misclassification | CX core: Detects contact-reason misclassification in contact-center dialogues. Compares the STATED contact reason (what  | chat wizard composer | experts_db |
| `cx_discover_topics` | cx_discover_topics | CX core: Unsupervised topic discovery for contact-center dialogues using BERTopic with multilingual sentence-transformer | chat wizard composer | experts_db |
| `cx_estimate_llm_cost` | cx_estimate_llm_cost | CX core: Estimates time and cost (COGS) of an LLM evaluation run over a dialogue dataset. Loads a pickled pandas DataFra | chat wizard composer | experts_db |
| `cx_evaluate_checklist` | cx_evaluate_checklist | CX core: evaluates a batch of contact-center dialogues against a client checklist using an LLM judge with evidence-first | chat wizard composer | experts_db |
| `cx_export_xlsx` | cx_export_xlsx | CX core: Exports contact-center CX-analytics pilot results to a client-friendly Excel workbook with four sheets (summary | chat wizard composer | experts_db |
| `cx_extract_design_tokens` | cx_extract_design_tokens | CX core: Extracts CSS variables (:root design tokens), body/font styles, color palette, nav structure and component (car | chat wizard composer | experts_db |
| `cx_generate_findings` | cx_generate_findings | CX core: turns pipeline aggregate metrics and optional per-dialogue quote samples into a draft of narrative FINDING bloc | chat wizard composer | experts_db |
| `cx_generate_llm_batch` | cx_generate_llm_batch | CX core: Generates OpenAI-compatible Batch API JSONL files from an LLM-input JSONL by applying an externally supplied ev | chat wizard composer | experts_db |
| `cx_generate_synthetic_dialogues` | cx_generate_synthetic_dialogues | CX core: Generates a fully synthetic contact-center chat export (.xlsx) mimicking a real client export for NDA-safe demo | chat wizard composer | experts_db |
| `cx_inspect_xlsx_columns` | cx_inspect_xlsx_columns | CX core: Reports the column names (raw and whitespace-stripped) plus a sample of the first data row of an xlsx export fi | chat wizard composer | experts_db |
| `cx_llm_worker` | cx_llm_worker | CX core: Parallel batch worker (cspl=parallel_task) that scores a slice of anonymized contact-center dialogues from a pi | chat wizard composer | experts_db |
| `cx_merge_llm_results` | cx_merge_llm_results | CX core: Merges per-batch LLM evaluation JSON files (batch_*.json) from an input directory into one aggregated results J | chat wizard composer | experts_db |
| `cx_package_deliverables` | cx_package_deliverables | CX core: Packages all CX-analytics deliverables (report/export files, an interactive dashboard folder, a generated dashb | chat wizard composer | experts_db |
| `cx_page_calibration` | cx_page_calibration | CX core: Renders the AI-calibration evidence-dashboard page (expert vs LLM score agreement, per-criterion gap table, sco | chat wizard composer | experts_db |
| `cx_page_dialogs` | cx_page_dialogs | CX core: Generates the 'Operator load' (Нагрузка операторов) evidence-dashboard page — concurrent-chat overload signals, | chat wizard composer | experts_db |
| `cx_page_index` | cx_page_index | CX core: Renders the evidence-dashboard summary (index) page — top findings, incident signals, KPI blocks and charts bui | chat wizard composer | experts_db |
| `cx_page_predict` | cx_page_predict | CX core: renders the "predict" evidence-dashboard page (customer experience and churn risk: FRR/tNPS KPIs, detractor quo | chat wizard composer | experts_db |
| `cx_page_quality` | cx_page_quality | CX core: Generates the service-quality evidence page (quality KPIs, applicability FINDING, checklist-violation quote exa | chat wizard composer | experts_db |
| `cx_page_reasons` | cx_page_reasons | CX core: Generates the "Reasons and redirects" evidence-dashboard page (redirect volumes, sentiment escalations, cause m | chat wizard composer | experts_db |
| `cx_page_trends` | cx_page_trends | CX core: renders the "Trends" (Динамика) evidence-dashboard page via the cx_page_dsl handler — period KPIs (total dialog | chat wizard composer | experts_db |
| `cx_parse_dialogues` | cx_parse_dialogues | CX core: Parses one or more contact-center chat/WhatsApp xlsx exports into a combined dialogue DataFrame with per-dialog | chat wizard composer | experts_db |
| `cx_parse_expert_labels` | cx_parse_expert_labels | CX core: Parses a human-expert call-evaluation xlsx export into a gold-set calibration JSON with per-criterion pass rate | chat wizard composer | experts_db |
| `cx_prepare_llm_input` | cx_prepare_llm_input | CX core: Converts an anonymized dialogue dataset (pkl or csv) into a JSONL LLM-input file where each line is {"id", "tex | chat wizard composer | experts_db |
| `cx_register_page_dsl` | cx_register_page_dsl | CX core: registers the custom CSPL handler 'cx_page_dsl' (evidence-dashboard page generator DSL with PAGE/DATA/KPI/CHART | chat wizard composer | experts_db |
| `cx_run_calibration` | cx_run_calibration | CX core: Runs LLM evaluation on the calibration subset (expert-labeled dialogues matched to transcripts by phone and dat | chat wizard composer | experts_db |
| `cx_run_pipeline` | cx_run_pipeline | CX core: v0 one-command orchestrator for the CX analytics pipeline. Runs the core stages on one contact-center export by | chat wizard composer | experts_db |
| `cx_show_dialogue` | cx_show_dialogue | CX core: dialogue drill-down (evidence-explorer primitive). Shows the full cleaned dialogue text, metadata and per-crite | chat wizard composer | experts_db |
| `cx_submit_llm_batch` | cx_submit_llm_batch | CX core: Submits dialogue-evaluation JSONL input to an OpenAI-compatible Batch API as an async background (nohup) job —  | chat wizard composer | experts_db |
| `ett_run_pipeline` | ett_run_pipeline | ET-Tech procurement pilot orchestrator (nohup, canon-compliant v2 rebuilt by harness engineer). Raw top-level python (no | chat wizard composer | experts_db |
| `hr_anonymize_employee_data` | hr_anonymize_employee_data | Псевдонимизация персональных данных сотрудника (ФИО, ИИН, оклад, даты) перед передачей в ИИ-генератор. Консистентные ток | chat wizard composer | experts_db |
| `hr_generate_hr_document_draft` | hr_generate_hr_document_draft | ИИ-генерация черновика кадрового документа (приказ или уведомление) по псевдонимизированным данным сотрудника. Подбирает | chat wizard composer | experts_db |
| `hr_render_hr_document_final` | hr_render_hr_document_final | Оформление черновика в финальный документ Word/PDF по шаблону компании (фирменный бланк, реквизиты). Адаптация: вместо п | chat wizard composer | experts_db |
| `hr_run_pipeline` | hr_run_pipeline | Auto-generated process orchestrator: runs the contract pipeline (hr_anonymize_employee_data -> hr_generate_hr_document_d | chat wizard composer | experts_db |
| `hvk_anonymize_hr_data` | hvk_anonymize_hr_data | Псевдонимизация ПДн сотрудника (ФИО, ИИН, оклад) перед передачей в ИИ | chat wizard composer | experts_db |
| `hvk_deliver_hr_document` | hvk_deliver_hr_document | Доставка сгенерированного документа и протокола кадровику | chat wizard composer | experts_db |
| `hvk_generate_hr_document` | hvk_generate_hr_document | Генерация проекта кадрового приказа и проверка реквизитов | chat wizard composer | experts_db |
| `hvk_run_pipeline` | hvk_run_pipeline | Auto-generated process orchestrator: runs the contract pipeline (hvk_anonymize_hr_data -> hvk_search_labor_code_rk -> hv | chat wizard composer | experts_db |
| `hvk_search_labor_code_rk` | hvk_search_labor_code_rk | Knowledge grounding stage (reuses kp_ask on pack 'trud_rk'): finds relevant articles and adds legal_context for the next | chat wizard composer | experts_db |
| `kc_run_pipeline` | kc_run_pipeline | Auto-generated process orchestrator: runs the contract pipeline (kc_sales_report_inspect -> kc_calculate_sales_summary - | chat wizard composer | experts_db |
| `kfb_run_pipeline` | kfb_run_pipeline | Auto-generated process orchestrator: runs the contract pipeline (kfb_parse_sales_excel) on a source file, cleans headers | chat wizard composer | experts_db |
| `kp_ask` | kp_ask | База знаний: отвечает на вопрос ПО загруженным документам (RAG, локальный поиск + синтез). Параметры: name, question. | chat wizard composer | experts_db |
| `kp_ingest` | kp_ingest | База знаний: загружает документы (.txt/.md/.pdf) из папки, режет на куски и векторизует ЛОКАЛЬНО (nomic-embed-text). Пар | chat wizard composer | experts_db |
| `kp_install_pack` | kp_install_pack | База знаний: устанавливает ГОТОВУЮ базу (кодекс/справочник) — качает официальный корпус или статьи Википедии, чанкует, в | chat wizard composer | experts_db |
| `kp_resolver` | kp_resolver | База знаний: ставит движок (Ollama + модель эмбеддингов nomic-embed-text) на устройство. Зови ПЕРЕД первой сборкой базы  | chat wizard composer | experts_db |
| `p2d4_run_pipeline` | p2d4_run_pipeline | Auto-generated process orchestrator: runs the contract pipeline (p2d4_parse_contracts -> p2d4_load_legal_standards -> p2 | chat wizard composer | experts_db |
| `p55a_run_pipeline` | p55a_run_pipeline | Orchestrator for procurement-summary process: chains p55a_parse_excel_requests -> p55a_analyze_procurement_data -> p55a_ | chat wizard composer | experts_db |
| `st_run_pipeline` | st_run_pipeline | Auto-generated process orchestrator: runs the contract pipeline (st_read_sales_excel -> st_calculate_sales_summary -> st | chat wizard composer | experts_db |
| `svc_books` | svc_books | Сервис: поиск книг (автор, год, издания) в Open Library. Параметр: query. | chat wizard composer | experts_db |
| `svc_crypto` | svc_crypto | Сервис: курс криптовалюты (bitcoin, ethereum, the-open-network, solana и др.) в usd/kzt/rub. Источник CoinGecko. Парамет | chat wizard composer | experts_db |
| `svc_currency` | svc_currency | Сервис: актуальный курс валют (USD, EUR, KZT, RUB и др.) и конвертация суммы. Источник exchangerate-api. Параметры: base | chat wizard composer | experts_db |
| `svc_github` | svc_github | Сервис: сведения о репозитории GitHub (звёзды, язык, описание, форки). Параметр: repo (owner/name). | chat wizard composer | experts_db |
| `svc_hackernews` | svc_hackernews | Сервис: топ технологических новостей Hacker News (заголовки + ссылки). Параметр: count. | chat wizard composer | experts_db |
| `svc_holidays` | svc_holidays | Сервис: государственные праздники и выходные страны за год. Источник Nager.Date. Параметры: country (KZ/RU/US...), year. | chat wizard composer | experts_db |
| `svc_ipgeo` | svc_ipgeo | Сервис: геолокация по IP-адресу (страна, город, провайдер). Источник ip-api. Параметр: ip. | chat wizard composer | experts_db |
| `svc_postal` | svc_postal | Сервис: населённый пункт по почтовому индексу. Источник Zippopotam. Параметры: country (us/de/ru...), code. | chat wizard composer | experts_db |
| `svc_qr` | svc_qr | Сервис: генерирует QR-код по тексту/ссылке (возвращает ссылку на картинку PNG). Параметр: data. | chat wizard composer | experts_db |
| `svc_translate` | svc_translate | Сервис: перевод короткого текста между языками. Источник MyMemory. Параметры: text, src (en/ru/kk...), to. | chat wizard composer | experts_db |
| `svc_weather` | svc_weather | Сервис: текущая погода в городе (температура, ветер, влажность). Источник Open-Meteo. Параметр: city. | chat wizard composer | experts_db |
| `svc_wiki` | svc_wiki | Сервис: краткая справка из Википедии по теме (определение + резюме). Параметр: topic. | chat wizard composer | experts_db |
| `svc_worldbank` | svc_worldbank | Сервис: экономический показатель страны (по умолчанию ВВП). Источник World Bank. Параметры: country (KZ/RU/US), indicato | chat wizard composer | experts_db |
| `uc_anonymize_invoice_data` | uc_anonymize_invoice_data | Псевдонимизация банковских реквизитов и ФИО индивидуальных предпринимателей перед передачей в ИИ. Консистентные токены д | chat wizard composer | experts_db |
| `uc_calibrate_reconciliation_ai` | uc_calibrate_reconciliation_ai | Калибровка ИИ-сверки на разметке экспертов-бухгалтеров: вычисление согласия ИИ с экспертной разметкой per-criterion (сум | chat wizard composer | experts_db |
| `uc_compute_reconciliation_stats` | uc_compute_reconciliation_stats | Детерминированная аналитика результатов сверки без ИИ-домыслов: доля автосопоставленных документов, количество расхожден | chat wizard composer | experts_db |
| `uc_generate_reconciliation_xlsx` | uc_generate_reconciliation_xlsx | Генерация Excel-отчёта для ручной проверки бухгалтерами: вкладки «Расхождения», «Дубли», «Сводка», «Подтверждено автомат | chat wizard composer | experts_db |
| `uc_load_nk_rk_standards` | uc_load_nk_rk_standards | Knowledge grounding stage (reuses kp_ask on pack 'nalog_rk'): finds relevant articles and adds legal_context for the nex | chat wizard composer | experts_db |
| `uc_parse_invoices_acts` | uc_parse_invoices_acts | Парсинг входящих счетов-фактур и актов из почты и файловых папок, чтение выгрузок 1С и реестра договоров. Извлечение рек | chat wizard composer | experts_db |
| `uc_read_1c_export` | uc_read_1c_export | Knowledge grounding stage (reuses kp_ask on pack 'nalog_rk'): finds relevant articles and adds legal_context for the nex | chat wizard composer | experts_db |
| `uc_read_onec_contracts` | uc_read_onec_contracts | Knowledge grounding stage (reuses kp_ask on pack 'nalog_rk'): finds relevant articles and adds legal_context for the nex | chat wizard composer | experts_db |
| `uc_read_onec_export` | uc_read_onec_export | Knowledge grounding stage (reuses kp_ask on pack 'nalog_rk'): finds relevant articles and adds legal_context for the nex | chat wizard composer | experts_db |
| `uc_reconcile_invoice_batch` | uc_reconcile_invoice_batch | Пакетная ИИ-сверка документов с данными 1С и договорами по утверждённому чек-листу (реквизиты, суммы, НДС, контрагент, п | chat wizard composer | experts_db |
| `uc_run_pipeline` | uc_run_pipeline | Auto-generated process orchestrator: runs the contract pipeline (uc_parse_invoices_acts -> uc_anonymize_invoice_data ->  | chat wizard composer | experts_db |
| `uc_validate_vat_compliance` | uc_validate_vat_compliance | Knowledge grounding stage (reuses kp_ask on pack 'nalog_rk'): finds relevant articles and adds legal_context for the nex | chat wizard composer | experts_db |

## Блоки Композитора (вет-проверенный whitelist) · 13

| id | название | описание | поверхности | источник |
|---|---|---|---|---|
| `cap_ghostscript_compress_pdf_batch` | cap_ghostscript_compress_pdf_batch | Compress all PDFs in a folder 50-95% (local, batch). | composer chat | composer:catalog |
| `cap_local_ask` | cap_local_ask | Ask a locally downloaded Ollama model a question (private, free, offline). Use for classification/reasoning over data pr | composer chat | composer:catalog |
| `cap_ocr_searchable_batch` | cap_ocr_searchable_batch | OCR scanned/photo PDFs in a folder into searchable PDFs (local, batch). | composer chat | composer:catalog |
| `cap_pdftotext_to_txt_batch` | cap_pdftotext_to_txt_batch | Extract plain text from all PDFs in a folder (local, batch). | composer chat | composer:catalog |
| `fin_scan_statements` | fin_scan_statements | Scan a local folder of bank/card CSV statements; returns recurring subscriptions (merchant, price, cadence, price histor | composer chat | composer:catalog |
| `kp_ask` | kp_ask | Ask a question against a local RAG knowledge base; answers cite sources, fully offline. | composer chat | composer:catalog |
| `kp_ingest` | kp_ingest | Index a local folder of documents (.txt/.md/.pdf) into a private local RAG knowledge base (Ollama embeddings). | composer chat | composer:catalog |
| `svc_currency` | svc_currency | Live currency rate / conversion. | composer chat | composer:catalog |
| `svc_github_releases` | svc_github_releases | Latest releases of given GitHub repos (tag, date, notes). | composer chat | composer:catalog |
| `svc_hackernews` | svc_hackernews | Top Hacker News stories. | composer chat | composer:catalog |
| `svc_reddit` | svc_reddit | Top posts of subreddits (via RSS). | composer chat | composer:catalog |
| `svc_rss` | svc_rss | Fresh entries from RSS/Atom feeds (blogs, changelogs). | composer chat | composer:catalog |
| `svc_weather` | svc_weather | Current weather for a city. | composer chat | composer:catalog |

## Локальные модели (это устройство) · 6

| id | название | описание | поверхности | источник |
|---|---|---|---|---|
| `glm-ocr:latest` | glm-ocr:latest | локальная модель (ollama, это устройство) | composer chat | ollama:local |
| `hf.co/TheBloke/law-chat-GGUF:Q4_K_M` | hf.co/TheBloke/law-chat-GGUF:Q4_K_M | локальная модель (ollama, это устройство) | composer chat | ollama:local |
| `hf.co/mradermacher/II-Medical-8B-GGUF:Q4_K_M` | hf.co/mradermacher/II-Medical-8B-GGUF:Q4_K_M | локальная модель (ollama, это устройство) | composer chat | ollama:local |
| `martain7r/finance-llama-8b:Q4_K_M` | martain7r/finance-llama-8b:Q4_K_M | локальная модель (ollama, это устройство) | composer chat | ollama:local |
| `nomic-embed-text:latest` | nomic-embed-text:latest | локальная модель (ollama, это устройство) | composer chat | ollama:local |
| `qwen2.5:3b` | qwen2.5:3b | локальная модель (ollama, это устройство) | composer chat | ollama:local |

## CLI-инструменты · 2

| id | название | описание | поверхности | источник |
|---|---|---|---|---|
| `figlet` | figlet | Banner text CLI | composer wizard | _mkt_installed |
| `primaprashant/hns` | hns | hns is a speech-to-text CLI tool to transcribe your voice from your microphone directly to clipboard. Integrate hns with | composer wizard | _mkt_installed |

## Навыки · 3

| id | название | описание | поверхности | источник |
|---|---|---|---|---|
| `firecrawl-scraper` | firecrawl-scraper | Deep web scraping via Firecrawl | composer wizard | _mkt_installed |
| `https://github.com/github/awesome-copilot/tree/main/skills/image-manipulation-im` | image-manipulation-image-magick | Process and manipulate images using ImageMagick. Supports resizing, format conversion, batch processing, and retrieving  | composer wizard | _mkt_installed |
| `https://github.com/microsoft/skills/tree/main/.github/skills/azure-ai-vision-ima` | azure-ai-vision-imageanalysis-py | Azure AI Vision Image Analysis SDK for captions, tags, objects, OCR, people detection, and smart cropping. Use for compu | composer wizard | _mkt_installed |

## Служебные эксперты визарда (wz_*) · 59

| id | название | описание | поверхности | источник |
|---|---|---|---|---|
| `wz_agent_runlog` | wz_agent_runlog | Serverside run history (append/list) for wizard-built agents; KV agent_runs:<id> | wizard | experts_db |
| `wz_as_fix` | wz_as_fix | Copilot self-fix: repairs failed AppleScript from osascript error (deterministic quirk corrector + per-app dictionary hi | wizard | experts_db |
| `wz_ask_agent` | wz_ask_agent | Adoption Wizard: agent-to-agent handoff bridge - lets one agent call another as a service (the platform free-graph doctr | wizard | experts_db |
| `wz_auto_compose` | wz_auto_compose | wz_auto_compose | wizard | experts_db |
| `wz_build_plan` | wz_build_plan | Blueprint → план стройки (задачи/приёмка/переиспользование). Keyless на платформенной модели, max_output_tokens против о | wizard | experts_db |
| `wz_build_runner` | wz_build_runner | Adoption Wizard Build layer: deterministic single-task build executor - the honest workhorse of the IMPLEMENT gate. Desi | wizard | experts_db |
| `wz_capability_install` | wz_capability_install | Install a capability AFTER click-confirm: model->Ollama, mcp->mcp_connect, cli->brew (real install); registers in _mkt_i | wizard | experts_db |
| `wz_capability_search` | wz_capability_search | Search capabilities across live sources: HF GGUF models, npm MCP servers, brew formulas + GitHub CLIs, Smithery skills,  | wizard | experts_db |
| `wz_capability_uninstall` | wz_capability_uninstall | REAL device uninstall: model->Ollama api/delete (frees GB, tag-mismatch guarded), mcp/service->allowlist exact-match rem | wizard | experts_db |
| `wz_child_from_device` | wz_child_from_device | child saved from within a device-executed expert | wizard | experts_db |
| `wz_cli_capability_pack` | wz_cli_capability_pack | Фабрика CLI-Способностей: анкета spec -> вся Способность + карточка в витрину | wizard | experts_db |
| `wz_cli_installer` | wz_cli_installer | honest two-track CLI installer (brew/apt), returns resolved abs path | wizard | experts_db |
| `wz_connector_email` | wz_connector_email | Email output connector (SMTP, stdlib): decrypt creds from vault on hosting, send mail. validate/send. Password not logge | wizard | experts_db |
| `wz_connector_slack` | wz_connector_slack | Slack output connector (Incoming Webhook): decrypt from vault on hosting, post message. | wizard | experts_db |
| `wz_connector_sms` | wz_connector_sms | SMS output connector (Twilio): decrypt from vault, send SMS. | wizard | experts_db |
| `wz_connector_telegram` | wz_connector_telegram | Коннектор Telegram: validate/send/send_document(файл)/poll/webhook_info/clear_webhook/set_webhook; секрет из vault, токе | wizard | experts_db |
| `wz_connector_whatsapp` | wz_connector_whatsapp | WhatsApp output connector (Meta Cloud API): decrypt from vault, send text. +GREEN-API provider | wizard | experts_db |
| `wz_data_reality_check` | wz_data_reality_check | wz_data_reality_check | wizard | experts_db |
| `wz_deploy_agent` | wz_deploy_agent | Deploy production agent from blueprint. v2: update mode (pass agent_id of a UI-copy on platform model Qwen - no BYOK nee | wizard | experts_db |
| `wz_digest_pipeline` | wz_digest_pipeline | Generic digest process: materializes a source from the shared store (same resolver as generated orchestrators), parses x | wizard | experts_db |
| `wz_embedding_canary` | wz_embedding_canary | Embedding worker canary: adds a unique probe concept, waits for semantic indexing, removes it. worker_alive=false means  | wizard | experts_db |
| `wz_expert_janitor` | wz_expert_janitor | Storage janitor: detects duplicate expert names within and across agent scopes (report-only). Duplicate name = nondeterm | wizard | experts_db |
| `wz_extract_doc` | wz_extract_doc | Извлекает текст из одного файла (PDF→pdftotext, Word/rtf/odt→pandoc, txt→read). Возвращает {status,name,text}. | wizard | experts_db |
| `wz_extract_doc_b64` | wz_extract_doc_b64 | Извлекает текст из файла, переданного как base64 (PDF/Word/txt). Пишет во временный файл, извлекает, чистит. {status,nam | wizard | experts_db |
| `wz_flow_run` | wz_flow_run | wz_flow_run | wizard | experts_db |
| `wz_generate_blueprint` | wz_generate_blueprint | wz_generate_blueprint | wizard | experts_db |
| `wz_install_autostart` | wz_install_autostart | Onboarding step 4: installs the wizard bridge as an autostart service on THIS device (macOS launchd LaunchAgent / Linux  | wizard | experts_db |
| `wz_nohup_probe` | wz_nohup_probe | Minimal nohup probe for remote target testing. Prints json with msg param. | wizard | experts_db |
| `wz_onboard_device` | wz_onboard_device | One-click device onboarding (A2): unifies the 4 manual steps into ONE call, each dispatched pinned to <target> via /api/ | wizard | experts_db |
| `wz_open_wizard` | wz_open_wizard | Adoption Wizard: native entry point for chat agents - opens the wizard UI on the user's device with one secret-free call | wizard | experts_db |
| `wz_ops_report` | wz_ops_report | Daily ops digest for the process factory: VPS target liveness (wz_ping), expert library duplicates (wz_expert_janitor),  | wizard | experts_db |
| `wz_persist_probe_g` | wz_persist_probe_g | probe global | wizard | experts_db |
| `wz_persist_probe_s` | wz_persist_probe_s | probe scoped | wizard | experts_db |
| `wz_pick_path` | wz_pick_path | Открывает НАТИВНЫЙ диалог выбора папки/файла на устройстве (macOS osascript) и возвращает POSIX-путь. Для кнопки «Выбрат | wizard | experts_db |
| `wz_ping` | wz_ping | Target liveness pulse: returns hostname, platform, user and local time of the device that executed it. Used to verify a  | wizard | experts_db |
| `wz_programs_harvest` | wz_programs_harvest | Harvest MCP-server repos → _mkt_programs, categorized to toolbar cat-ids via topics | wizard | experts_db |
| `wz_project_spec` | wz_project_spec | Adoption Wizard: assembles the LIVING PROJECT SPEC (ТЗ проекта) as a markdown document from all session artifacts - the  | wizard | experts_db |
| `wz_publish_pack` | wz_publish_pack | Publish: генерит Process Pack из зарегистрированной композиции — тянет код экспертов (/api/expert/get), пишет карточку/R | wizard | experts_db |
| `wz_run_demo` | wz_run_demo | Adoption Wizard: one-command synthetic demo run for the wizard Test step. Orchestrates via Extella REST (like cx_run_pip | wizard | experts_db |
| `wz_save_from_device` | wz_save_from_device | probe: saves a child expert from within device execution | wizard | experts_db |
| `wz_scheduler_tick` | wz_scheduler_tick | wz_scheduler_tick | wizard | experts_db |
| `wz_seed_library` | wz_seed_library | Seeds the CX industry-library layer (matrix processes x industries) onto a device: writes ~/extella_wizard/library/ (man | wizard | experts_db |
| `wz_session` | wz_session | wz_session | wizard | experts_db |
| `wz_session_prune` | wz_session_prune | Удаление сессий Визарда — одной по id или старых по возрасту; безопасно (превью по умолчанию, apply=true чтобы удалить,  | wizard | experts_db |
| `wz_source_1c_file` | wz_source_1c_file | B3 1C file source: honest-fail on truncation. | wizard | experts_db |
| `wz_source_1c_winrm` | wz_source_1c_winrm | B3 1C WinRM source: require SUCCESS marker + COUNT match (no silent zero-rows). | wizard | experts_db |
| `wz_source_amocrm` | wz_source_amocrm | B3 data source amoCRM/Kommo: runs on hosting device, decrypts sec:<client>:src_amocrm (long-lived JWT token + base_url/s | wizard | experts_db |
| `wz_source_bitrix24` | wz_source_bitrix24 | B3 data source Bitrix24: runs on hosting device, decrypts sec:<client>:src_bitrix24 (incoming webhook URL) from vault, c | wizard | experts_db |
| `wz_source_gsheets` | wz_source_gsheets | B3 data source Google Sheets: runs on hosting device, decrypts sec:<client>:src_gsheets from vault, mints RS256 JWT with | wizard | experts_db |
| `wz_source_mysql` | wz_source_mysql | B3 MySQL source: honest-fail on truncation. | wizard | experts_db |
| `wz_source_postgres` | wz_source_postgres | B3 PostgreSQL source: honest-fail on truncation (>cap rows not delivered as full). | wizard | experts_db |
| `wz_task_plan` | wz_task_plan | Copilot brain: plans an action (reuse catalog / app adapter / web MCP / codegen) from NL task + screen context. Returns  | wizard | experts_db |
| `wz_task_run` | wz_task_run | wz_task_run | wizard | experts_db |
| `wz_vault_provision` | wz_vault_provision | provision vault.key on hosting device from client PIN (PBKDF2); returns key sha256 for cross-device match check, never t | wizard | experts_db |
| `wz_vault_selftest` | wz_vault_selftest | host-side vault consumer: decrypt + VERIFY envelope binding to (client,connector); reject transplanted ciphertext | wizard | experts_db |
| `wz_wizard_serve` | wz_wizard_serve | Adoption Wizard UI: deploys and starts the local wizard bridge server on this device (Listener). Unpacks the embedded br | wizard | experts_db |
| `wz_wizard_stop` | wz_wizard_stop | Adoption Wizard UI: stops the local wizard bridge server started by wz_wizard_serve on this device. Reads the pidfile fr | wizard | experts_db |
| `wz_workspace` | wz_workspace | Extella Workspace engine ws-v1.4: chat op (workspace Q&A over full project context: goals/tasks/facts/registry/writes; s | wizard | experts_db |
| `wz_ws_autopilot_run` | wz_ws_autopilot_run | Runs local Workspace autopilot driver; canonical path ~/extella-plugins/workspace/ first, env override, dev fallbacks. R | wizard | experts_db |
