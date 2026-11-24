"""
Newsroom — локален прототип за автоматизирана новинарска медия.
Сайт + админ + RSS/Trends мониторинг + препоръки + чернови + Canva/Instagram/TikTok/имейл куки.

Старт:  pip install fastapi uvicorn feedparser httpx python-multipart jinja2
        python app.py            -> http://127.0.0.1:8000  (админ: /admin)
        python app.py --demo     -> същото + демо данни (без интернет)
Всички ключове, източници, проценти и думи са в Настройки (базата), нищо не е хардкоднато.
"""
import asyncio, base64, email, hashlib, hmac, imaplib, json, math, re, secrets, sqlite3, sys, time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from email.header import decode_header
from pathlib import Path
from urllib.parse import quote, urljoin

import feedparser, httpx, uvicorn
from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from jinja2 import DictLoader, Environment, select_autoescape

import os
# Настройки за машината (не в базата): виж README → „Постоянна работа на друг Mac“
HOST = os.environ.get("NEWSROOM_HOST", "127.0.0.1")    # 0.0.0.0 = достъпно от мрежата (тогава паролата е задължителна)
PORT = int(os.environ.get("NEWSROOM_PORT", "8000"))
PUBLIC_URL = os.environ.get("NEWSROOM_PUBLIC_URL", f"http://127.0.0.1:{PORT}").rstrip("/")
IS_HTTPS = PUBLIC_URL.startswith("https://")   # зад Cloudflare Tunnel → паролата е задължителна, cookie-то е Secure
DB = Path(os.environ.get("NEWSROOM_DB") or Path(__file__).with_name("newsroom.db"))
STATIC = Path(__file__).with_name("static"); (STATIC / "exports").mkdir(parents=True, exist_ok=True)
UA = {"User-Agent": "Mozilla/5.0 NewsroomBot/0.1"}

# ---------------------------------------------------------------- DB
SCHEMA = """
create table if not exists settings(key text primary key, value text);
create table if not exists sources(id integer primary key, name text, kind text, url text,
  weight real default 1, active integer default 1, owner text default '', lean real default 0, tone real default 0.5,
  category text default '', every_min real default 0, last_fetch real default 0, lang text default 'bg');
create table if not exists items(id integer primary key, source_id int, title text, link text unique,
  summary text, image text, published real, fetched real, score real default 0,
  status text default 'new', why text, cluster int, silence real default 0);
create table if not exists trends(id integer primary key, term text, traffic text, fetched real);
create table if not exists posts(id integer primary key, item_id int, title text, body text,
  image text, media_url text, canva_url text, status text default 'draft',
  channels text default '{}', created real, published real,
  hook text, sub text, canva_design_id text, export_path text, auto int default 0);
create table if not exists profiles(id integer primary key, platform text, handle text,
  last_json text, checked real);
create table if not exists log(id integer primary key, ts real, msg text);
create table if not exists feedback(id integer primary key, ts real, kind text, ref text, page text,
  rating int default 0, text text, status text default 'new');
create table if not exists signals(id integer primary key, ts real, item_id int, source_id int, category text, action text);
create table if not exists proposals(id integer primary key, ts real, json text, status text default 'new');
create table if not exists media(owner text primary key, region text default '', topics text default '',
  about text default '', updated real);
create table if not exists entities(id integer primary key, name text unique, kind text, about text);
create table if not exists relations(id integer primary key, subj text, rel text, obj text, note text,
  source_url text, source_date text, confidence text default 'потвърдено', ts real);
create table if not exists promises(id integer primary key, who text, what text, said_on text, deadline text,
  status text default 'чака', source_url text, media text, ts real);
create table if not exists silence_fix(id integer primary key, ts real, item_id int, owner text, category text, action text);
create table if not exists item_notes(id integer primary key, ts real, item_id int, verdict text, reasons text, text text);
create table if not exists mentions(id integer primary key, item_id int, actor text, toward text, stance int, ts real);
create table if not exists media_notes(id integer primary key, ts real, owner text, text text, item_id int, kind text default 'note');
create table if not exists missed(id integer primary key, ts real, url text, title text, domain text, note text,
  found_item int, json text, status text default 'new');
"""
DEFAULT_SETTINGS = {
    "site_name": "Обществото",
    "admin_password": "",                     # генерира се при първи старт
    "session_secret": "",                     # генерира се при първи старт; ключ за cookie-тата
    "fetch_every_min": "5",      # БТА връща само последните ~20 новини → при 15 мин се губят
    "boost_keywords": "Бургас, Евровизия, евро, цени, избори",
    "block_keywords": "хороскоп, промо",
    "w_recency": "25", "w_trend": "25", "w_cross": "15", "w_keyword": "10",  # тежести в %
    "recency_halflife_h": "6",
    "recommend_threshold": "35",
    # ---- Bias / баланс (всичко е хипотеза за тестване, виж /admin/bias)
    "w_silence": "15",            # бонус за „тишина“: голям обществен интерес, но непълно покритие
    "w_balance": "10",            # контратежест срещу наклона на нашата лента за последните N дни
    "balance_window_days": "7",
    "lean_axis": "-1 = проправителствено · +1 = опозиционно",   # оста я дефинира редакцията
    "cluster_min_overlap": "2",   # общи думи, за да са една и съща история
    # „Мълчи“ се брои само за групи, от които се очаква да покрият историята:
    # същата категория + за българска история само медии на български + група, която реално е публикувала
    # ≥ silence_min_items новини в тази категория за последните silence_window_days дни.
    "silence_window_days": "30", "silence_min_items": "3", "silence_min_groups": "2",
    "silence_ignore_owners": "",  # групи, които никога не се броят за „мълчащи“ (със запетая)
    "images_per_cycle": "60",
    # Известие на телефона, когато история стигне „Хвани сега“ (Telegram бот → твоят личен чат, не каналът)
    "alert_telegram": "off", "alert_chat_id": "", "alert_threshold": "45",
    "post_sources_max": "5",      # колко източника (с линк) да влизат в текста на ⚡ Бърз пост
    "region_categories": "България, Бургас и региона, Свят, Балкани и Европа, Европейски гласове",   # кои категории са региони (филтърът „Регион“)
    "silence_fix_min": "2",       # след толкова твои корекции за една медия и категория мълчанието се учи от тях
    # Теми по съдържание: новина влиза в категорията и когато заглавието съдържа някоя от думите (не само по източник)
    "topic_keywords": ("Технологии и AI: nvidia, хуанг, huang, openai, chatgpt, изкуствен интелект, ai, apple, google, microsoft, meta, "
                       "чип, чипове, полупроводник, tesla, мъск, musk, spacex, anthropic, claude, gemini, iphone, samsung, амазон, amazon, "
                       "робот, дрон, кибератака, хакер, стартъп\n"
                       "Бургас и региона: бургас, созопол, несебър, поморие, айтос, царево, приморско, слънчев бряг, сарафово, камено, средец\n"
                       "Бизнес и икономика: инфлация, бнб, еврозона, лихви, борса, акции, бюджет, данъци, заплати, цени\n"
                       "Шоубизнес и светски: певица, актриса, сватба, раздяла, бременна, инфлуенсър, евровизия"),
    "w_learn": "15",              # тежест на наученото от бележките ти (думи), в %
    # Claude чете новините и вади обещания + позиции („за проверка“, докато човек не потвърди)
    "extract_auto": "off",        # on = на всеки цикъл; off = само с бутона в Картата
    "extract_per_run": "20",      # колко новини на едно минаване (по 10 в една заявка към Claude)
    "extract_categories": "България, Бургас и региона, Балкани и Европа, Бизнес и икономика",
    "canva_extra_images": "8",    # колко още снимки от историята да се качат в Canva → Uploads, за да ги сменяш в редактора
    # ---- Учене от твоите избори (→ Чернова / Бърз пост = +, ✕ = −) и Компас
    "learn_window_days": "30", "learn_strength": "50",   # 0 = изкл., 100 = силно; множителят е между ×0.6 и ×1.6
    "compass_hours": "24",        # от колко часа назад Компасът гледа истории
    "compass_fresh_h": "3",       # „расте“ = статии в последните N часа     # колко новини без снимка да се допълват на цикъл (og:image)
    "telegram_bot_token": "", "telegram_chat_id": "",
    "threads_user_id": "", "threads_token": "",
    "gdelt_query": "sourcecountry:bulgaria", "wiki_project": "bg.wikipedia",
    # ---- X (pay-per-use: ~$0.005 на прочетен пост). Бюджетът спира тегленето, когато се изхарчи.
    "x_bearer_token": "", "x_max_per_fetch": "10", "x_monthly_budget_usd": "10",
    "x_price_per_read": "0.005", "x_spent": "0", "x_spent_month": "",
    "donate_url": "",                         # Stripe Payment Link (EUR)
    "live_hls_url": "",                       # .m3u8 за плейъра на живо
    "canva_token": "", "canva_template_id": "",
    "canva_client_id": "", "canva_client_secret": "", "canva_refresh_token": "", "canva_token_expires": "0",
    # имената на полетата в твоя Canva шаблон (Bulk create / Data autofill) — бутонът „Провери шаблона“ ги показва
    "canva_field_headline": "HEADLINE", "canva_field_sub": "SUB", "canva_field_image": "IMAGE", "canva_field_source": "SOURCE",
    # Автопилот: off = само ръчно · draft = сам прави чернова + AI заглавие · canva = + Canva дизайн + PNG. Публикуване винаги ръчно.
    "auto_mode": "off", "auto_threshold": "70", "auto_max_per_day": "10",
    "hook_style": "кратко, ясно, без кликбейт лъжи, до 70 знака; подзаглавие до 110 знака",
    "ig_user_id": "", "ig_token": "",
    # Instagram през Facebook Login (нужно за следене на чужди профили — Business Discovery)
    "fb_app_id": "", "fb_app_secret": "", "ig_username": "", "ig_token_expires": "", "ig_every_min": "30",
    "tiktok_token": "",
    "imap_host": "", "imap_user": "", "imap_pass": "", "imap_secret": "#post",
    "public_base_url": "",
    # Claude: claude_mode = auto (API ключ, ако има; иначе Claude Code CLI) · api · cli · off
    "claude_mode": "auto", "claude_cli_path": "", "claude_cli_model": "", "claude_cli_model_fast": "haiku",
    "anthropic_api_key": "", "claude_model": "claude-sonnet-5-5", "claude_web_searches": "3",
    "claude_brief": "",
    # Категории на източниците — редът тук е редът на табовете в админа. Добавяш/преименуваш оттук.
    "source_categories": "България, Бургас и региона, Свят, Балкани и Европа, Европейски гласове, Бизнес и икономика, Шоубизнес и светски, Мода и лайфстайл, Рап и музика, Спорт, Технологии и AI, Трендове и вирусни",
    # Цвят на етикета на всяка категория: Категория=#hex, разделени със запетая. Новините = студени тонове, лайфстайлът = топли.
    "category_colors": "България=#2563eb, Бургас и региона=#0891b2, Свят=#475569, Балкани и Европа=#4f46e5, Европейски гласове=#7c3aed, Бизнес и икономика=#059669, Шоубизнес и светски=#db2777, Мода и лайфстайл=#c026d3, Рап и музика=#ea580c, Спорт=#65a30d, Технологии и AI=#0f172a, Трендове и вирусни=#f59e0b",
    # Превод на чужди новини (Claude). off = никога · recommended = само препоръчаните · all = всички чужди заглавия
    "translate_titles": "recommended", "translate_model": "claude-haiku-5-5", "translate_batch": "40",
    "intl_style": "Преведи на естествен, разговорен български. Само фактите от оригинала, нищо измислено. Заглавие до 90 знака, текст 3–5 изречения. Накрая: Източник: <медия>.",
    # Reddit API (по желание): без тях се ползва публичният RSS на Reddit, който понякога връща 403.
    "reddit_client_id": "", "reddit_client_secret": "",                    # нужен за Instagram (картинката трябва да е публичен URL)
}
# (име, тип, url, тежест, собственик/група) — наклонът (lean) по подразбиране е 0; калибрира се от редакцията/панел
DEFAULT_SOURCES = [
    ("NOVA", "rss", "https://nova.bg/rss/latest", 1.0, "United Group"),
    ("БНТ (през Google News)", "rss",
     "https://news.google.com/rss/search?q=when:24h+site:bntnews.bg&hl=bg&gl=BG&ceid=BG:bg", 1.0, "обществена медия"),
    ("Reuters World (през Google News)", "rss",
     "https://news.google.com/rss/search?q=when:24h+site:reuters.com/world&hl=en-US&gl=US&ceid=US:en", 0.8, "чужда агенция"),
    ("БНР English", "rss", "https://bnrnews.bg/api/rss/en", 0.6, "обществена медия"),
    ("БТА", "rss", "https://www.bta.bg/bg/rss/free", 1.0, "национална агенция"),
    ("Google Trends BG", "trends", "https://trends.google.com/trending/rss?geo=BG", 1.0, ""),
    ("Wikipedia BG топ четени", "wiki", "auto", 0.7, ""),
    ("GDELT (всички БГ сайтове)", "gdelt", "auto", 0.5, ""),
]

GN = "https://news.google.com/rss/search?q=when:24h+site:{}&hl=bg&gl=BG&ceid=BG:bg"
GNE = "https://news.google.com/rss/search?q=when:24h+site:{}&hl=en-US&gl=US&ceid=US:en"
GNQ = "https://news.google.com/rss/search?q={}+when:24h&hl=bg&gl=BG&ceid=BG:bg"
# категория за вече съществуващите източници (по име)
DEFAULT_CATEGORY = {"NOVA": "България", "БНТ (през Google News)": "България", "БТА": "България",
    "GDELT (всички БГ сайтове)": "България", "Reuters World (през Google News)": "Свят", "БНР English": "България",
    "Google Trends BG": "Трендове и вирусни", "Wikipedia BG топ четени": "Трендове и вирусни"}
# (име, тип, url, тежест, собственик/група, категория, интервал в мин; 0 = общия fetch_every_min)
# Google News връзките са без снимки в RSS — снимката се взима от статията (og:image).
EXTRA_SOURCES = [
    # България
    ("bTV Новини", "rss", GN.format("btvnovinite.bg"), 1.0, "CME / PPF", "България", 15),
    ("Дневник", "rss", "https://www.dnevnik.bg/rss/", 0.9, "Икономедиа", "България", 10),
    ("Капитал", "rss", GN.format("capital.bg"), 0.8, "Икономедиа", "България", 20),
    ("24 часа", "rss", GN.format("24chasa.bg"), 0.8, "Вестникарска група България", "България", 15),
    ("Mediapool", "rss", GN.format("mediapool.bg"), 0.8, "Mediapool", "България", 15),
    ("Сега", "rss", GN.format("segabg.com"), 0.8, "Сега", "България", 15),
    ("OFFNews", "rss", GN.format("offnews.bg"), 0.8, "OFFNews", "България", 15),
    ("Club Z", "rss", GN.format("clubz.bg"), 0.7, "Club Z", "България", 20),
    ("Факти", "rss", GN.format("fakti.bg"), 0.7, "Факти", "България", 15),
    ("Свободна Европа (БГ)", "rss", GN.format("svobodnaevropa.bg"), 0.8, "RFE/RL", "България", 20),
    ("Bird.bg (разследвания)", "rss", GN.format("bird.bg"), 0.7, "Bird.bg", "България", 60),
    # Бургас и региона
    ("Флагман", "rss", GN.format("flagman.bg"), 1.0, "Флагман", "Бургас и региона", 10),
    ("Грамофона", "rss", GN.format("gramofona.com"), 0.9, "Грамофона", "Бургас и региона", 15),
    ("Google News: Бургас", "rss", GNQ.format("Бургас"), 0.9, "", "Бургас и региона", 15),
    ("Google News: Созопол/Несебър/Поморие", "rss", GNQ.format("Созопол+OR+Несебър+OR+Поморие+OR+Айтос"), 0.7, "", "Бургас и региона", 30),
    # Свят
    ("BBC World", "rss", "https://feeds.bbci.co.uk/news/world/rss.xml", 0.8, "BBC", "Свят", 15),
    ("The Guardian World", "rss", "https://www.theguardian.com/world/rss", 0.7, "Guardian Media Group", "Свят", 15),
    ("AP News", "rss", GNE.format("apnews.com"), 0.8, "AP", "Свят", 15),
    ("Al Jazeera", "rss", "https://www.aljazeera.com/xml/rss/all.xml", 0.6, "Al Jazeera", "Свят", 20),
    # Балкани и Европа
    ("Balkan Insight", "rss", "https://balkaninsight.com/feed/", 0.8, "BIRN", "Балкани и Европа", 30),
    ("Euronews", "rss", GNE.format("euronews.com"), 0.7, "Euronews", "Балкани и Европа", 20),
    ("Politico Europe", "rss", "https://www.politico.eu/feed/", 0.7, "Politico / Axel Springer", "Балкани и Европа", 30),
    ("Euractiv", "rss", GNE.format("euractiv.com"), 0.6, "Euractiv", "Балкани и Европа", 30),
    # Шоубизнес и светски
    ("Блиц", "rss", GN.format("blitz.bg"), 0.7, "Блиц", "Шоубизнес и светски", 15),
    ("Edna.bg", "rss", GN.format("edna.bg"), 0.6, "Edna", "Шоубизнес и светски", 30),
    ("Lupa.bg", "rss", GN.format("lupa.bg"), 0.6, "Lupa", "Шоубизнес и светски", 30),
    ("Hotarena", "rss", GN.format("hotarena.net"), 0.6, "Hotarena", "Шоубизнес и светски", 30),
    ("TMZ", "rss", "https://www.tmz.com/rss.xml", 0.7, "TMZ / Fox", "Шоубизнес и светски", 15),
    ("Page Six", "rss", "https://pagesix.com/feed/", 0.7, "News Corp", "Шоубизнес и светски", 15),
    ("People", "rss", GNE.format("people.com"), 0.7, "People Inc.", "Шоубизнес и светски", 20),
    ("Variety", "rss", "https://variety.com/feed/", 0.6, "Penske Media", "Шоубизнес и светски", 20),
    # Мода и лайфстайл
    ("Vogue", "rss", "https://www.vogue.com/feed/rss", 0.7, "Condé Nast", "Мода и лайфстайл", 30),
    ("Architectural Digest", "rss", "https://www.architecturaldigest.com/feed/rss", 0.6, "Condé Nast", "Мода и лайфстайл", 60),
    ("WWD", "rss", "https://wwd.com/feed/", 0.6, "Penske Media", "Мода и лайфстайл", 30),
    ("Business of Fashion", "rss", GNE.format("businessoffashion.com"), 0.6, "BoF", "Мода и лайфстайл", 60),
    ("Hypebeast", "rss", "https://hypebeast.com/feed", 0.5, "Hypebeast", "Мода и лайфстайл", 30),
    # Спорт
    ("Gong.bg", "rss", GN.format("gong.bg"), 0.7, "NOVA / United Group", "Спорт", 15),
    ("Sportal.bg", "rss", GN.format("sportal.bg"), 0.7, "Sportal", "Спорт", 15),
    # Технологии и AI
    ("The Verge", "rss", "https://www.theverge.com/rss/index.xml", 0.6, "Vox Media", "Технологии и AI", 20),
    ("TechCrunch", "rss", "https://techcrunch.com/feed/", 0.6, "Yahoo", "Технологии и AI", 20),
    ("Kaldata", "rss", GN.format("kaldata.com"), 0.5, "Kaldata", "Технологии и AI", 30),
    # Трендове и вирусни
    ("Reddit r/bulgaria", "rss", "https://www.reddit.com/r/bulgaria/top/.rss?t=day", 0.5, "", "Трендове и вирусни", 30),
    ("Reddit r/popculturechat", "rss", "https://www.reddit.com/r/popculturechat/top/.rss?t=day", 0.5, "", "Трендове и вирусни", 30),
]

# (име, тип, url, тежест, собственик, категория, мин, език). Тип instagram: url = потребителско име (нужни ig_user_id + ig_token).
# Тип reddit: url = име на subreddit (с reddit_client_id/secret → API, без тях → публичен RSS).
EXTRA_SOURCES_2 = [
    ("Дарик", "rss", GN.format("dariknews.bg"), 0.9, "Дарик", "България", 15, "bg"),
    ("Deutsche Welle (български)", "rss", GN.format("dw.com/bg"), 0.8, "Deutsche Welle", "България", 30, "bg"),
    ("Deutsche Welle", "rss", "https://rss.dw.com/rdf/rss-en-all", 0.8, "Deutsche Welle", "Свят", 20, "en"),
    ("New York Times", "rss", "https://rss.nytimes.com/services/xml/rss/nyt/World.xml", 0.8, "NYT", "Свят", 20, "en"),
    ("Tagesschau", "rss", "https://www.tagesschau.de/index~rss2.xml", 0.7, "ARD", "Балкани и Европа", 20, "de"),
    # Бизнес и икономика (платени сайтове — заглавията идват, текстът е зад paywall)
    ("Financial Times", "rss", GNE.format("ft.com"), 0.8, "Nikkei", "Бизнес и икономика", 20, "en"),
    ("The Economist", "rss", "https://www.economist.com/latest/rss.xml", 0.8, "The Economist Group", "Бизнес и икономика", 30, "en"),
    ("Bloomberg", "rss", GNE.format("bloomberg.com"), 0.8, "Bloomberg", "Бизнес и икономика", 20, "en"),
    ("Wall Street Journal", "rss", GNE.format("wsj.com"), 0.8, "News Corp", "Бизнес и икономика", 20, "en"),
    ("Forbes", "rss", GNE.format("forbes.com"), 0.6, "Forbes", "Бизнес и икономика", 30, "en"),
    # Европейски гласове: независими/обясняващи/social-first медии в нашия стил → „Международни постове“ с превод
    ("Brut.", "instagram", "brut", 0.7, "Brut", "Европейски гласове", 60, "fr"),
    ("Brut. (сайт)", "rss", GNE.format("brut.media"), 0.6, "Brut", "Европейски гласове", 60, "en"),
    ("Il Post", "rss", "https://www.ilpost.it/feed/", 0.7, "Il Post", "Европейски гласове", 30, "it"),
    ("Spiegel International", "rss", "https://www.spiegel.de/international/index.rss", 0.7, "Spiegel", "Европейски гласове", 30, "en"),
    ("Le Monde in English", "rss", GNE.format("lemonde.fr/en"), 0.7, "Le Monde", "Европейски гласове", 30, "en"),
    ("El País English", "rss", GNE.format("english.elpais.com"), 0.6, "PRISA", "Европейски гласове", 30, "en"),
    ("Telex English (HU)", "rss", GNE.format("telex.hu/english"), 0.7, "Telex", "Европейски гласове", 60, "en"),
    ("Kathimerini English (GR)", "rss", GNE.format("ekathimerini.com"), 0.6, "Kathimerini", "Европейски гласове", 30, "en"),
    ("N1 (RS)", "rss", GN.format("n1info.rs"), 0.6, "United Group", "Европейски гласове", 30, "sr"),
    ("Meduza English", "rss", GNE.format("meduza.io/en"), 0.6, "Meduza", "Европейски гласове", 60, "en"),
    ("Kyiv Independent", "rss", GNE.format("kyivindependent.com"), 0.6, "Kyiv Independent", "Европейски гласове", 30, "en"),
    # Мода
    ("Fashion Week (Google News)", "rss", "https://news.google.com/rss/search?q=%22fashion+week%22+when:24h&hl=en-US&gl=US&ceid=US:en", 0.6, "", "Мода и лайфстайл", 30, "en"),
    ("Vogue Runway (Instagram)", "instagram", "voguerunway", 0.6, "Condé Nast", "Мода и лайфстайл", 60, "en"),
    ("Diet Prada", "instagram", "diet_prada", 0.7, "Diet Prada", "Мода и лайфстайл", 60, "en"),
    # Шоубизнес
    ("DeuxMoi (Instagram)", "instagram", "deuxmoi", 0.7, "DeuxMoi", "Шоубизнес и светски", 60, "en"),
    # Рап и музика
    ("XXL", "rss", "https://www.xxlmag.com/feed/", 0.6, "Townsquare Media", "Рап и музика", 30, "en"),
    ("HotNewHipHop", "rss", GNE.format("hotnewhiphop.com"), 0.6, "HotNewHipHop", "Рап и музика", 30, "en"),
    ("Complex Music", "rss", GNE.format("complex.com/music"), 0.6, "NTWRK", "Рап и музика", 30, "en"),
    ("Rap-Up", "rss", GNE.format("rap-up.com"), 0.5, "Rap-Up", "Рап и музика", 60, "en"),
    ("Български рап (Google News)", "rss", GNQ.format("рапър+OR+%22български+рап%22"), 0.6, "", "Рап и музика", 30, "bg"),
    # Reddit по темите
    ("r/Deuxmoi", "reddit", "Deuxmoi", 0.5, "", "Шоубизнес и светски", 30, "en"),
    ("r/Fauxmoi", "reddit", "Fauxmoi", 0.5, "", "Шоубизнес и светски", 30, "en"),
    ("r/fashion", "reddit", "fashion", 0.4, "", "Мода и лайфстайл", 60, "en"),
    ("r/hiphopheads", "reddit", "hiphopheads", 0.5, "", "Рап и музика", 30, "en"),
    ("r/europe", "reddit", "europe", 0.5, "", "Балкани и Европа", 30, "en"),
    ("r/worldnews", "reddit", "worldnews", 0.5, "", "Свят", 30, "en"),
    ("r/Economics", "reddit", "Economics", 0.4, "", "Бизнес и икономика", 60, "en"),
    ("r/architecture", "reddit", "architecture", 0.4, "", "Мода и лайфстайл", 60, "en"),
]
# език на вече съществуващите чужди източници
FOREIGN_LANG = {"Reuters World (през Google News)": "en", "БНР English": "en", "BBC World": "en", "The Guardian World": "en",
    "AP News": "en", "Al Jazeera": "en", "Balkan Insight": "en", "Euronews": "en", "Politico Europe": "en", "Euractiv": "en",
    "TMZ": "en", "Page Six": "en", "People": "en", "Variety": "en", "Vogue": "en", "Architectural Digest": "en", "WWD": "en",
    "Business of Fashion": "en", "Hypebeast": "en", "The Verge": "en", "TechCrunch": "en", "Reddit r/popculturechat": "en"}

class _Conn(sqlite3.Connection):
    """`with db() as c:` записва И затваря връзката (иначе на Mac се стига до „Too many open files“)."""
    def __exit__(self, *a):
        try: return super().__exit__(*a)
        finally: self.close()

def db():
    c = sqlite3.connect(DB, timeout=15, factory=_Conn); c.row_factory = sqlite3.Row
    c.execute("pragma journal_mode=wal"); return c

def init_db():
    with db() as c:
        c.executescript(SCHEMA)
        for t, col in [("sources", "owner text default ''"), ("sources", "lean real default 0"),
                       ("sources", "tone real default 0.5"), ("items", "cluster int"), ("items", "silence real default 0"),
                       ("posts", "hook text"), ("posts", "sub text"), ("posts", "canva_design_id text"), ("posts", "export_path text"),
                       ("posts", "auto int default 0"), ("sources", "category text default ''"),
                       ("sources", "every_min real default 0"), ("sources", "last_fetch real default 0"),
                       ("sources", "lang text default 'bg'"), ("items", "title_bg text"), ("posts", "intl int default 0"),
                       ("items", "verdict text"), ("posts", "export_json text"), ("media", "tier text default ''"), ("items", "extracted int default 0"),
                       ("promises", "item_id int"), ("relations", "item_id int"),
                       ("items", "topics text default ''"), ("media_notes", "source_id int"),
                       ("feedback", "claude_q text"), ("feedback", "answer text"),
                       ("items", "likes int"), ("items", "comments int"), ("items", "eng_ts real")]:
            try: c.execute(f"alter table {t} add column {col}")
            except sqlite3.OperationalError: pass
        for k, v in DEFAULT_SETTINGS.items():
            c.execute("insert or ignore into settings values(?,?)", (k, v))
        if not c.execute("select value from settings where key='admin_password'").fetchone()[0]:
            pw = secrets.token_urlsafe(8)
            c.execute("update settings set value=? where key='admin_password'", (pw,))
            print(f"\n*** Админ парола: {pw}  (смени я в Настройки) ***\n")
        if not c.execute("select value from settings where key='session_secret'").fetchone()[0]:
            c.execute("update settings set value=? where key='session_secret'", (secrets.token_hex(32),))
        if not c.execute("select count(*) from sources").fetchone()[0]:
            c.executemany("insert into sources(name,kind,url,weight,owner) values(?,?,?,?,?)", DEFAULT_SOURCES)
        for name, cat in DEFAULT_CATEGORY.items():
            c.execute("update sources set category=? where name=? and coalesce(category,'')=''", (cat, name))
        # нови източници: само веднъж (флаг в settings), за да не се връщат, ако ги изтриеш
        if not c.execute("select value from settings where key='_extra_sources_v1'").fetchone():
            have = {r[0] for r in c.execute("select url from sources")}
            for n, k, u, w, o, cat, ev in EXTRA_SOURCES:
                if u not in have:
                    c.execute("insert into sources(name,kind,url,weight,owner,category,every_min) values(?,?,?,?,?,?,?)",
                              (n, k, u, w, o, cat, ev))
            c.execute("insert into settings values('_extra_sources_v1', ?)", (str(int(time.time())),))
        if not c.execute("select value from settings where key='_relations_v1'").fetchone():
            for subj, rel, obj, note, url, d in SEED_RELATIONS:
                conf = "за проверка" if "да се провери" in note else "по медии" if ("krisyotam" in url or "Ivo_Prokopiev" in url) else "потвърдено"
                c.execute("insert into relations(subj,rel,obj,note,source_url,source_date,confidence,ts) values(?,?,?,?,?,?,?,?)",
                          (subj, rel, obj, note, url, d, conf, time.time()))
            for o, t in [("национална агенция", "агенция"), ("чужда агенция", "агенция"), ("AP", "агенция"), ("Bird.bg", "разследваща")]:
                c.execute("insert into media(owner,tier,updated) values(?,?,?) on conflict(owner) do update set tier=excluded.tier "
                          "where coalesce(media.tier,'')=''", (o, t, time.time()))
            c.execute("insert into settings values('_relations_v1', ?)", (str(int(time.time())),))
        if not c.execute("select value from settings where key='_extra_sources_v2'").fetchone():
            have = {(r[0], r[1]) for r in c.execute("select kind, url from sources")}
            for n, k, u, w, o, cat, ev, lg in EXTRA_SOURCES_2:
                if (k, u) not in have:
                    c.execute("insert into sources(name,kind,url,weight,owner,category,every_min,lang) values(?,?,?,?,?,?,?,?)",
                              (n, k, u, w, o, cat, ev, lg))
            for n, lg in FOREIGN_LANG.items(): c.execute("update sources set lang=? where name=?", (lg, n))
            c.execute("update sources set category='Бизнес и икономика' where name='Капитал'")
            # старите Reddit RSS → тип reddit (API, ако има ключ)
            c.execute("update sources set kind='reddit', url='bulgaria', lang='bg' where url like '%reddit.com/r/bulgaria/%'")
            c.execute("update sources set kind='reddit', url='popculturechat' where url like '%reddit.com/r/popculturechat/%'")
            c.execute("insert into settings values('_extra_sources_v2', ?)", (str(int(time.time())),))

# Начални връзки от проучване (22.11.2026). Всеки ред = публичен източник. Без източник не влиза.
SEED_RELATIONS = [
  ("United Group", "притежава", "NOVA (Нова броудкастинг груп)", "Придобита от Advance Media Group; сделката приключи на 22.01.2021",
   "https://united.group/united-group-completes-acquisition-of-bulgarias-nova-broadcasting-group/", "2021"),
  ("BC Partners", "мажоритарен собственик на", "United Group", "Инвестиционен фонд; притежава United Group от 2019",
   "https://ceetv.net/readItem?id=51572", "2020"),
  ("United Group", "друг бизнес", "Телекоми в Югоизточна Европа (в България: Vivacom)", "Стратегия: телеком + медии на едни пазари",
   "https://united.group/united-group-completes-acquisition-of-bulgarias-nova-broadcasting-group/", "2021"),
  ("PPF Group", "притежава", "bTV Media Group (чрез CME)", "bTV Media Group е част от CME, собственост на PPF",
   "https://www.bta.bg/bg/news/920965-bi-ti-vi-shte-bade-s-nova-viziya-ot-1-yuli", "2025"),
  ("Рената Келнерова", "действителен собственик на", "bTV Media Group", "По сайта на bTV (Закон за мерките срещу изпирането на пари)",
   "https://www.btv.bg/english", "2026"),
  ("PPF Group", "друг бизнес", "Телеком (в България: Yettel)", "Конкурент на Vivacom (United Group) — двата най-големи ТВ собственици са и телеком съперници",
   "https://wiki.krisyotam.com/content/wikipedia_en_all_nopic_2026-03/United_Group", "2026"),
  ("Иво Прокопиев", "мажоритарен собственик на", "Икономедиа (Дневник, Капитал)", "51% (Теодор Захов 49%) — данни от 2010, да се провери в регистъра",
   "https://www.wikipedia.org/wiki/Economedia", "2010"),
  ("Иво Прокопиев", "друг бизнес", "A Data Pro, Alfa Finance Holding", "Съосновател и съсобственик",
   "https://en.wikipedia.com/wiki/Ivo_Prokopiev", "—"),
  ("Фондация „Америка за България“", "финансира", "Икономедиа (Дневник, Капитал)", "Донорска подкрепа 2012–2017 и 2018–2020; по думите на издателя без влияние върху редакционната политика",
   "https://www.capital.bg/politika_i_ikonomika/bulgaria/2018/02/06/3124555_fondaciia_amerika_za_bulgariia_produljava_podkrepata/", "2018"),
  ("Сашо Дончев", "притежава", "Сега", "Чрез фондация „Дончев“ (Великобритания)",
   "https://www.capital.bg/biznes/media_i_reklama/2019/12/03/3998983_kak_shte_se_promeni_vestnik_sega/", "2019"),
  ("Сашо Дончев", "друг бизнес", "Овергаз (газ)", "Контролира „Овергаз холдинг“ — Сега пише и за газовия пазар, където собственикът има интерес",
   "https://www.capital.bg/biznes/energetika/2025/09/19/4824581_neshto_stava_na_gazoviia_pazar/", "2025"),
  ("Стояна Георгиева", "издател на", "Mediapool", "Юридическо лице: фондация „Инфоспейс“",
   "https://www.enpa.eu/people/stoyana-georgieva", "2026"),
  ("Венелина Гочева", "мажоритарен собственик на", "24 часа (Медийна група България)", "99% по данни от 2019 — да се провери в регистъра",
   "https://www.capital.bg/politika_i_ikonomika/bulgaria/2019/02/22/3394276_opornite_medii_na_gerb/", "2019"),
  ("Радосвет Радев", "притежава", "Дарик", "Основател и собственик на „Дарик радио“",
   "https://www.bta.bg/en/news/archives/1047287-january-21-2026-darik-radio-goes-on-air-33-years-ago", "2026"),
  ("Държавният бюджет", "финансира", "БНТ, БНР", "Обществени медии: държавна субсидия + реклама",
   "https://media-ownership.eu/?p=9932", "2025"),
]
SEED_OWNER_MAP = {"United Group": "United Group", "NOVA / United Group": "United Group", "CME / PPF": "PPF Group",
                  "Икономедиа": "Иво Прокопиев", "Сега": "Сашо Дончев", "Mediapool": "Стояна Георгиева",
                  "Вестникарска група България": "Венелина Гочева", "Дарик": "Радосвет Радев", "обществена медия": "Държавният бюджет"}

def S(key, cast=str):
    with db() as c:
        r = c.execute("select value from settings where key=?", (key,)).fetchone()
    v = r[0] if r else DEFAULT_SETTINGS.get(key, "")
    try: return cast(v)
    except Exception: return cast(DEFAULT_SETTINGS.get(key, "0"))

def log(msg):
    with db() as c: c.execute("insert into log(ts,msg) values(?,?)", (time.time(), msg))
    print("[log]", msg)

# ---------------------------------------------------------------- Fetch
OG_IMG = re.compile(r'<meta[^>]+(?:property|name)=["\'](?:og:image(?::secure_url|:url)?|twitter:image(?::src)?)["\'][^>]*?content=["\']([^"\']+)', re.I)
OG_IMG2 = re.compile(r'<meta[^>]+content=["\']([^"\']+)["\'][^>]*?(?:property|name)=["\'](?:og:image(?::secure_url|:url)?|twitter:image(?::src)?)["\']', re.I)

def entry_image(e):
    for key in ("media_content", "media_thumbnail"):
        if e.get(key): return e[key][0].get("url")
    for l in e.get("links", []):
        if l.get("type", "").startswith("image"): return l.get("href")
    m = re.search(r'<img[^>]+src=["\']([^"\']+)', e.get("summary", ""))
    return m.group(1) if m else None

async def gnews_url(client, url):
    """Google News връзката (news.google.com/rss/articles/...) → истинският адрес на статията.
    Google вече не я пренасочва директно: взимаме подписа от страницата и питаме batchexecute."""
    m = re.search(r"news\.google\.com/(?:rss/)?(?:articles|read)/([^?/#]+)", url or "")
    if not m: return url
    aid = m.group(1)
    try:
        r = await client.get(f"https://news.google.com/articles/{aid}", headers=UA, timeout=10)
        sg = re.search(r'data-n-a-sg="([^"]+)"', r.text); ts = re.search(r'data-n-a-ts="([^"]+)"', r.text)
        if not (sg and ts): return None
        req = [[["Fbv4je", '["garturlreq",[["X","X",["X","X"],null,null,1,1,"US:en",null,1,null,null,null,null,null,0,1],'
                 f'"X","X",1,[1,1,1],1,1,null,0,0,null,0],"{aid}",{ts.group(1)},"{sg.group(1)}"]']]]
        r = await client.post("https://news.google.com/_/DotsSplashUi/data/batchexecute", headers=UA | {
            "content-type": "application/x-www-form-urlencoded;charset=UTF-8"}, content="f.req=" + quote(json.dumps(req)), timeout=10)
        return json.loads(json.loads(r.text.split("\n\n")[1])[0][2])[1]
    except Exception: return None

async def og_image(client, url):
    try:
        if "news.google.com" in (url or ""):
            url = await gnews_url(client, url)
            if not url: return None
        r = await client.get(url, headers=UA, follow_redirects=True, timeout=10)
        html = r.text[:300_000]
        m = OG_IMG.search(html) or OG_IMG2.search(html)
        if not m: return None
        img = m.group(1).replace("&amp;", "&").strip()
        if img.startswith("//"): img = "https:" + img
        return urljoin(str(r.url), img)
    except Exception: return None

async def fill_images(client):
    """Допълва снимки на новините без снимка (първо тези, които се виждат в админа: най-висок резултат).
    Неуспешните стават '' → не се пробват отново всеки цикъл."""
    with db() as c:
        miss = c.execute("select id,link from items where image is null and status in ('new','recommended','draft') "
                         "order by (status='recommended') desc, score desc, published desc limit ?",
                         (S("images_per_cycle", int),)).fetchall()
    sem_g, sem = asyncio.Semaphore(3), asyncio.Semaphore(8)     # Google се пита по-кротко
    async def one(it):
        async with (sem_g if "news.google.com" in it["link"] else sem):
            img = await og_image(client, it["link"])
        with db() as c: c.execute("update items set image=? where id=?", (img or "", it["id"]))
        return bool(img)
    res = await asyncio.gather(*(one(it) for it in miss))
    if miss: log(f"Снимки: {sum(res)} от {len(miss)} допълнени")

async def fetch_all(force=False):
    with db() as c: srcs = c.execute("select * from sources where active=1").fetchall()
    tnow = time.time()
    srcs = [s for s in srcs if force or not s["every_min"] or tnow - (s["last_fetch"] or 0) >= s["every_min"] * 60 - 30]
    with db() as c:
        for s in srcs: c.execute("update sources set last_fetch=? where id=?", (tnow, s["id"]))
    async with httpx.AsyncClient() as client:
        for s in srcs:
            if s["kind"] in ("wiki", "gdelt", "x", "instagram", "reddit"):
                try: await fetch_special(client, s)
                except Exception as ex: log(f"{s['name']}: грешка {ex}")
                continue
            try:
                r = await client.get(s["url"], headers=UA, timeout=15, follow_redirects=True)
                feed = feedparser.parse(r.content)
            except Exception as ex:
                log(f"{s['name']}: грешка {ex}"); continue
            now, new = time.time(), 0
            with db() as c:
                if s["kind"] == "trends":
                    c.execute("delete from trends where fetched < ?", (now - 48 * 3600,))
                    for e in feed.entries:
                        c.execute("delete from trends where term=?", (e.title,))
                        c.execute("insert into trends(term,traffic,fetched) values(?,?,?)",
                                  (e.title, e.get("ht_approx_traffic", ""), now))
            if s["kind"] == "trends":
                log(f"{s['name']}: {len(feed.entries)} тренда"); continue
            with db() as c:
                for e in feed.entries[:100]:
                    pub = time.mktime(e.published_parsed) if e.get("published_parsed") else now
                    title = e.title
                    if "news.google.com" in s["url"]:              # „Заглавие - БНТ Новини“ → „Заглавие“
                        title = re.sub(r"\s+-\s+[^-]{2,40}$", "", title)
                    summ = re.sub("<[^>]+>", "", e.get("summary", ""))[:600]
                    cur = c.execute("insert or ignore into items(source_id,title,link,summary,image,published,fetched)"
                                    " values(?,?,?,?,?,?,?)",
                                    (s["id"], title, e.link, summ, entry_image(e), pub, now))
                    new += cur.rowcount
            log(f"{s['name']}: {new} нови")
        # og:image за новите без снимка (images_per_cycle на цикъл, паралелно)
        try: await fill_images(client)
        except Exception as ex: log(f"Снимки: грешка {ex}")
    score_all()
    try: await translate_pending()
    except Exception as ex: log(f"Превод: грешка {ex}")

async def fetch_special(client, s):
    now = time.time()
    if s["kind"] == "x": return await fetch_x(client, s)
    if s["kind"] == "instagram": return await fetch_instagram(client, s)
    if s["kind"] == "reddit": return await fetch_reddit(client, s)
    if s["kind"] == "wiki":   # Wikimedia Pageviews: топ четени статии вчера → сигнал за обществен интерес
        d = datetime.now(timezone.utc).date().fromordinal(datetime.now(timezone.utc).date().toordinal() - 1)
        url = f"https://wikimedia.org/api/rest_v1/metrics/pageviews/top/{S('wiki_project')}/all-access/{d:%Y/%m/%d}"
        j = (await client.get(url, headers=UA, timeout=20)).json()
        arts = [a for a in j["items"][0]["articles"] if ":" not in a["article"] and a["article"] != "Начална_страница"][:30]
        with db() as c:
            for a in arts:
                c.execute("delete from trends where term=?", (a["article"].replace("_", " "),))
                c.execute("insert into trends(term,traffic,fetched) values(?,?,?)",
                          (a["article"].replace("_", " "), f"wiki {a['views']}", now))
        log(f"{s['name']}: {len(arts)} статии")
    elif s["kind"] == "gdelt":  # GDELT DOC 2.0: безплатно, без ключ, ~1 заявка/5 сек
        url = "https://api.gdeltproject.org/api/v2/doc/doc"
        j = (await client.get(url, params={"query": S("gdelt_query"), "mode": "artlist", "maxrecords": 75,
                                           "format": "json", "timespan": "24h", "sort": "datedesc"}, timeout=30)).json()
        new = 0
        with db() as c:
            for a in j.get("articles", []):
                pub = datetime.strptime(a["seendate"], "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc).timestamp()
                new += c.execute("insert or ignore into items(source_id,title,link,summary,image,published,fetched)"
                                 " values(?,?,?,?,?,?,?)", (s["id"], a["title"], a["url"], a.get("domain", ""),
                                 a.get("socialimage") or None, pub, now)).rowcount
        log(f"{s['name']}: {new} нови")

def store_items(s, rows):
    """rows: (заглавие, линк, резюме, снимка, време) → items. Връща броя нови."""
    now, new = time.time(), 0
    with db() as c:
        for t, link, summ, img, pub in rows:
            new += c.execute("insert or ignore into items(source_id,title,link,summary,image,published,fetched) values(?,?,?,?,?,?,?)",
                             (s["id"], (t or "")[:300], link, (summ or "")[:600], img, pub or now, now)).rowcount
    return new

def ig_host():
    """Токен от Instagram Login (започва с IG) → graph.instagram.com; от Facebook Login → graph.facebook.com."""
    return "https://graph.instagram.com/v23.0" if S("ig_token").startswith("IG") else "https://graph.facebook.com/v23.0"

async def fetch_instagram(client, s):
    """Instagram Business Discovery: публичните постове на Business/Creator профил. url = потребителско име."""
    uid, tok = S("ig_user_id"), S("ig_token")
    if not (uid and tok): return log(f"{s['name']}: чака API — попълни ig_user_id и ig_token в Настройки")
    if tok.startswith("IG"): return log(f"{s['name']}: следенето на чужди профили иска токен от Facebook Login — виж 📸 Instagram")
    f = (f"business_discovery.username({s['url'].strip().lstrip('@')}){{followers_count,media.limit(20)"
         f"{{caption,media_url,thumbnail_url,permalink,timestamp,like_count,comments_count,media_type}}}}")
    r = await client.get(f"https://graph.facebook.com/v23.0/{uid}", params={"fields": f, "access_token": tok}, timeout=30)
    if r.status_code >= 300: return log(f"{s['name']}: Instagram грешка {r.status_code} {r.text[:200]}")
    rows, eng = [], []
    for m in r.json().get("business_discovery", {}).get("media", {}).get("data", []):
        cap = (m.get("caption") or "").strip()
        rows.append((cap.split("\n")[0][:280] or "(пост без текст)", m["permalink"], cap,
                     m.get("thumbnail_url") or m.get("media_url"),
                     datetime.strptime(m["timestamp"], "%Y-%m-%dT%H:%M:%S%z").timestamp()))
        eng.append((m.get("like_count"), m.get("comments_count"), time.time(), m["permalink"]))
    n = store_items(s, rows)
    with db() as c: c.executemany("update items set likes=?, comments=?, eng_ts=? where link=?", eng)   # реакциите се обновяват всеки път
    log(f"{s['name']}: {n} нови IG поста, обновени реакции на {len(eng)}")

async def fetch_reddit(client, s):
    """Топ постовете за деня в subreddit. С reddit_client_id/secret → официалното API, иначе публичния RSS."""
    sub, cid, sec = s["url"].strip().removeprefix("r/"), S("reddit_client_id"), S("reddit_client_secret")
    rows = []
    if cid and sec:
        t = await client.post("https://www.reddit.com/api/v1/access_token", data={"grant_type": "client_credentials"},
                              auth=(cid, sec), headers=UA, timeout=20)
        r = await client.get(f"https://oauth.reddit.com/r/{sub}/top", params={"t": "day", "limit": 25},
                             headers=UA | {"Authorization": f"Bearer {t.json().get('access_token', '')}"}, timeout=20)
        if r.status_code >= 300: return log(f"{s['name']}: Reddit грешка {r.status_code}")
        for ch in r.json().get("data", {}).get("children", []):
            d = ch["data"]; img = d.get("thumbnail") if str(d.get("thumbnail", "")).startswith("http") else None
            rows.append((d["title"], "https://www.reddit.com" + d["permalink"],
                         f"▲{d.get('score', 0)} · 💬{d.get('num_comments', 0)} · {(d.get('selftext') or '')[:400]}", img, d.get("created_utc")))
    else:
        r = await client.get(f"https://www.reddit.com/r/{sub}/top/.rss?t=day", headers=UA, timeout=20, follow_redirects=True)
        if r.status_code >= 300: return log(f"{s['name']}: Reddit RSS {r.status_code} — сложи reddit_client_id/secret в Настройки")
        for e in feedparser.parse(r.content).entries[:25]:
            rows.append((e.title, e.link, re.sub("<[^>]+>", "", e.get("summary", ""))[:400], entry_image(e),
                         time.mktime(e.updated_parsed) if e.get("updated_parsed") else None))
    log(f"{s['name']}: {store_items(s, rows)} нови")

# ---------------------------------------------------------------- Claude (API или CLI)
_CLI_SEM = asyncio.Semaphore(2)

def claude_cli():
    """Пътят до `claude` (Claude Code CLI). start.command често няма ~/.local/bin в PATH, затова гледаме и обичайните места."""
    import glob, shutil
    p = S("claude_cli_path").strip()
    if p: return p if Path(p).expanduser().exists() else None
    home = Path.home()
    cands = [shutil.which("claude"), home / ".claude/local/claude", home / ".local/bin/claude", "/opt/homebrew/bin/claude",
             "/usr/local/bin/claude", home / ".npm-global/bin/claude", *sorted(glob.glob(str(home / ".nvm/versions/node/*/bin/claude")))[::-1]]
    for c in cands:
        if c and Path(c).exists(): return str(c)
    return None

def claude_how():
    """Кой път ползваме сега: 'api', 'cli' или None."""
    mode = S("claude_mode").strip() or "auto"
    if mode == "off": return None
    if mode in ("auto", "api") and S("anthropic_api_key"): return "api"
    if mode in ("auto", "cli") and claude_cli(): return "cli"
    return None

def claude_ready(): return claude_how() is not None

async def ask_claude(prompt, max_tokens=1500, fast=False, web=False, timeout=180):
    """Един вход към Claude за цялото приложение. API: с ключа от Настройки. CLI: `claude -p` с твоя абонамент
    (без ключ). web=True → позволява търсене в интернет. Връща текста или хвърля RuntimeError."""
    how = claude_how()
    if how == "api":
        body = {"model": S("translate_model") if fast else S("claude_model"), "max_tokens": max_tokens,
                "messages": [{"role": "user", "content": prompt}]}
        if web: body["tools"] = [{"type": "web_search_20250305", "name": "web_search", "max_uses": S("claude_web_searches", int),
                                  "user_location": {"type": "approximate", "country": "BG"}}]
        async with httpx.AsyncClient(timeout=timeout) as cl:
            r = await cl.post("https://api.anthropic.com/v1/messages", json=body,
                              headers={"x-api-key": S("anthropic_api_key"), "anthropic-version": "2023-06-01"})
        if r.status_code >= 300: raise RuntimeError(f"Claude API {r.status_code}: {r.text[:200]}")
        return "\n".join(b.get("text", "") for b in r.json().get("content", []) if b.get("type") == "text").strip()
    if how == "cli":
        import tempfile
        args = [claude_cli(), "-p", "--output-format", "json"]
        model = S("claude_cli_model_fast") if fast else S("claude_cli_model")
        if model.strip(): args += ["--model", model.strip()]
        if web: args += ["--allowedTools", "WebSearch WebFetch", "--max-turns", str(2 + 2 * S("claude_web_searches", int))]
        else: args += ["--disallowedTools", "Bash Edit Write NotebookEdit WebSearch WebFetch Task Read Glob Grep", "--max-turns", "2"]
        async with _CLI_SEM:
            with tempfile.TemporaryDirectory() as tmp:          # празна папка: CLI не вижда файловете на проекта
                pr = await asyncio.create_subprocess_exec(*args, cwd=tmp, stdin=asyncio.subprocess.PIPE,
                                                          stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
                try: out, err = await asyncio.wait_for(pr.communicate(prompt.encode()), timeout)
                except asyncio.TimeoutError:
                    pr.kill(); raise RuntimeError(f"Claude CLI не отговори за {timeout} сек")
        try: j = json.loads(out.decode() or "{}")
        except ValueError: raise RuntimeError(f"Claude CLI: {(out or err).decode()[:200]}")
        if j.get("is_error") or pr.returncode: raise RuntimeError(f"Claude CLI: {(j.get('result') or err.decode())[:200]}")
        return (j.get("result") or "").strip()
    raise RuntimeError("Claude не е свързан: няма anthropic_api_key и не намирам `claude` CLI (виж claude_mode / claude_cli_path)")

async def translate_pending():
    """Превежда заглавията на чуждите новини на български (един пакет към Claude на цикъл)."""
    mode = S("translate_titles")
    if mode == "off" or not claude_ready(): return
    flt = "i.status='recommended'" if mode == "recommended" else "i.status in ('new','recommended')"
    with db() as c:
        rows = c.execute(f"select i.id, i.title from items i join sources s on s.id=i.source_id where coalesce(s.lang,'bg')!='bg' "
                         f"and i.title_bg is null and {flt} order by i.score desc limit ?", (S("translate_batch", int),)).fetchall()
    if not rows: return
    prompt = ("Преведи тези новинарски заглавия на естествен български. Върни САМО JSON масив от низове, в същия ред, без нищо друго.\n"
              + json.dumps([r["title"] for r in rows], ensure_ascii=False))
    try:
        txt = await ask_claude(prompt, max_tokens=4000, fast=True)
        out = json.loads(txt[txt.index("["):txt.rindex("]") + 1])
    except Exception as ex: return log(f"Превод: грешка {ex}")
    with db() as c:
        for row, t in zip(rows, out):
            if isinstance(t, str) and t.strip(): c.execute("update items set title_bg=? where id=?", (t.strip()[:300], row["id"]))
    log(f"Превод: {min(len(rows), len(out))} заглавия")

async def fetch_x(client, s):
    """X Recent Search (последните 7 дни). В полето URL на източника пишеш заявка, напр.
    (from:BTA_BG OR from:novinite) -is:retweet  или  Бургас lang:bg -is:retweet has:media"""
    tok = S("x_bearer_token")
    if not tok: log(f"{s['name']}: липсва x_bearer_token"); return
    month = datetime.now().strftime("%Y-%m")
    if S("x_spent_month") != month:
        with db() as c:
            c.execute("update settings set value=? where key='x_spent_month'", (month,))
            c.execute("update settings set value='0' where key='x_spent'")
    price, budget, spent = S("x_price_per_read", float), S("x_monthly_budget_usd", float), S("x_spent", float)
    n = max(10, min(100, S("x_max_per_fetch", int)))          # API-то иска 10..100
    if spent + n * price > budget: log(f"{s['name']}: X бюджетът (${budget}) е изчерпан"); return
    params = {"query": s["url"], "max_results": n, "tweet.fields": "created_at,entities,public_metrics,lang",
              "expansions": "attachments.media_keys,author_id", "media.fields": "url,preview_image_url,type",
              "user.fields": "username,name"}
    r = await client.get("https://api.x.com/2/tweets/search/recent", params=params,
                         headers={"Authorization": f"Bearer {tok}"}, timeout=30)
    if r.status_code >= 300: log(f"{s['name']}: X грешка {r.status_code} {r.text[:200]}"); return
    j = r.json(); data = j.get("data", [])
    media = {m["media_key"]: m.get("url") or m.get("preview_image_url") for m in j.get("includes", {}).get("media", [])}
    users = {u["id"]: u["username"] for u in j.get("includes", {}).get("users", [])}
    now, new = time.time(), 0
    with db() as c:
        c.execute("update settings set value=? where key='x_spent'", (str(round(spent + len(data) * price, 4)),))
        for t in data:
            img = next((media[k] for k in t.get("attachments", {}).get("media_keys", []) if media.get(k)), None)
            links = [u.get("expanded_url") for u in t.get("entities", {}).get("urls", [])
                     if u.get("expanded_url") and "x.com/" not in u["expanded_url"] and "twitter.com/" not in u["expanded_url"]]
            user = users.get(t.get("author_id"), "i")
            m = t.get("public_metrics", {})
            summ = (f"@{user} · ♥{m.get('like_count', 0)} ↻{m.get('retweet_count', 0)}"
                    + ("\nЛинкове: " + " ".join(links) if links else ""))
            pub = datetime.fromisoformat(t["created_at"].replace("Z", "+00:00")).timestamp()
            new += c.execute("insert or ignore into items(source_id,title,link,summary,image,published,fetched)"
                             " values(?,?,?,?,?,?,?)", (s["id"], t["text"][:280], f"https://x.com/{user}/status/{t['id']}",
                             summ, img, pub, now)).rowcount
    log(f"{s['name']}: {new} нови X поста (~${len(data) * price:.3f})")

async def test_sources():
    """Проверка на всеки активен източник: отговаря ли, колко записа, колко със снимка, последно заглавие."""
    out = []
    with db() as c: srcs = c.execute("select * from sources where active=1").fetchall()
    async with httpx.AsyncClient(headers=UA, timeout=20, follow_redirects=True) as client:
        for s in srcs:
            t0 = time.time(); row = {"name": s["name"], "cat": s["category"] or "", "kind": s["kind"], "ok": False, "n": 0, "img": 0, "last": "", "err": ""}
            try:
                if s["kind"] in ("rss", "trends"):
                    r = await client.get(s["url"]); f = feedparser.parse(r.content)
                    row["code"] = r.status_code; row["n"] = len(f.entries)
                    row["img"] = sum(1 for e in f.entries if entry_image(e) or e.get("ht_picture"))
                    if f.entries: row["last"] = f.entries[0].title
                    if f.bozo and not f.entries: row["err"] = str(f.bozo_exception)[:120]
                elif s["kind"] == "gdelt":
                    r = await client.get("https://api.gdeltproject.org/api/v2/doc/doc", params={"query": S("gdelt_query"),
                                         "mode": "artlist", "maxrecords": 25, "format": "json", "timespan": "24h", "sort": "datedesc"})
                    row["code"] = r.status_code; a = r.json().get("articles", [])
                    row["n"] = len(a); row["img"] = sum(1 for x in a if x.get("socialimage")); row["last"] = a[0]["title"] if a else ""
                elif s["kind"] == "wiki":
                    d = datetime.now(timezone.utc).date().fromordinal(datetime.now(timezone.utc).date().toordinal() - 1)
                    r = await client.get(f"https://wikimedia.org/api/rest_v1/metrics/pageviews/top/{S('wiki_project')}/all-access/{d:%Y/%m/%d}")
                    row["code"] = r.status_code; a = r.json()["items"][0]["articles"]
                    row["n"] = len(a); row["last"] = a[1]["article"] if len(a) > 1 else ""
                elif s["kind"] == "instagram":
                    row["code"] = "-"
                    if not (S("ig_user_id") and S("ig_token")): row["err"] = "⏸ чака API: ig_user_id + ig_token в Настройки"; row["wait"] = True
                    else:
                        f = f"business_discovery.username({s['url'].lstrip('@')}){{media.limit(5){{caption}}}}"
                        r = await client.get(f"https://graph.facebook.com/v23.0/{S('ig_user_id')}", params={"fields": f, "access_token": S("ig_token")})
                        row["code"] = r.status_code; d = r.json().get("business_discovery", {}).get("media", {}).get("data", [])
                        row["n"] = len(d); row["last"] = (d[0].get("caption") or "")[:90] if d else ""
                        if r.status_code >= 300: row["err"] = r.text[:120]
                elif s["kind"] == "reddit":
                    r = await client.get(f"https://www.reddit.com/r/{s['url'].removeprefix('r/')}/top/.rss?t=day")
                    row["code"] = r.status_code; f = feedparser.parse(r.content); row["n"] = len(f.entries)
                    if f.entries: row["last"] = f.entries[0].title
                    if r.status_code >= 300: row["err"] = "RSS блокиран → попълни reddit_client_id/secret (тогава ползва API)" + (" — ключът е попълнен, ще мине през API" if S("reddit_client_id") else "")
                elif s["kind"] == "x":
                    row["code"] = "-"; row["err"] = "X не се тества (струва пари) — пусни „Изтегли сега“" if S("x_bearer_token") else "няма x_bearer_token"
                row["ok"] = row["n"] > 0
            except Exception as ex:
                row["err"] = f"{type(ex).__name__}: {str(ex)[:120]}"
            row["ms"] = int((time.time() - t0) * 1000); out.append(row)
    return out

# ---------------------------------------------------------------- Препоръки
WORD = re.compile(r"[\wа-яА-Я]{4,}", re.U)
def toks(t): return {w.lower()[:6] for w in WORD.findall(t or "")}   # груб стемър: първите 6 букви

# думи, които не правят един тренд „същата история“ (дати, „срещу“ от мачовете и т.н.)
TREND_STOP = {toks(w).pop() for w in ("срещу януари февруари март април май юни юли август септември октомври ноември декември "
              "понеделник вторник сряда четвъртък петък събота неделя днес утре вчера новини news live").split() if toks(w)}

def trend_tokens(term, common=frozenset(), common1=frozenset()):
    """Отличителните думи на тренда. Тренд от една дума се брои само ако думата е рядка („Булгари“, не „пари“)."""
    t = toks(term) - TREND_STOP - set(common)
    return set() if len(t) == 1 and t & set(common1) else t

def common_tokens(tok_sets, share=0.005):
    """Думи, които са в много заглавия (>0.5%) — тренд само от такава дума („пари“, „дете“) не е конкретна история."""
    df = {}
    for t in tok_sets:
        for w in t: df[w] = df.get(w, 0) + 1
    lim = max(5, len(tok_sets) * share)
    return {w for w, n in df.items() if n > lim}

_GENERIC = {"t": 0, "w": set()}
def generic_words(today_sets):
    """Общи думи = чести през седмицата И без скок днес („пари“, „дете“, „храна“).
    Име, което гърми днес („nvidia“, „хуанг“), не е общо, дори да е в много заглавия."""
    now = time.time()
    if now - _GENERIC["t"] > 600:
        with db() as c:
            week = [toks((t or "") + " " + (b or "")) for t, b in c.execute(
                "select title, title_bg from items where published>?", (now - 7 * 86400,))]
        df = {}
        for t in week:
            for w in t: df[w] = df.get(w, 0) + 1
        _GENERIC.update(t=now, df=df, n=max(1, len(week)))
    df, n = _GENERIC["df"], _GENERIC["n"]
    today = {}
    for t in today_sets:
        for w in t: today[w] = today.get(w, 0) + 1
    nt = max(1, len(today_sets))
    return {w for w, k in df.items() if k / n > 0.002 and today.get(w, 0) / nt < 3 * k / n}

def trend_hit(words, tt):
    """Тренд съвпада, ако историята съдържа всички отличителни думи на тренда (при 3+ думи — всички без една)."""
    if not tt: return False
    need = len(tt) if len(tt) <= 2 else len(tt) - 1
    return len(words & tt) >= need

def clusters(rows, tk, k):
    """История = котва (най-силната новина) + новините с ≥k общи корена с котвата.
    Без верижно слепване (A~B, B~C ≠ A~C) и без много честите думи (напр. „българ“), които иначе слепват всичко."""
    df = {}
    for r in rows:
        for w in tk[r["id"]]: df[w] = df.get(w, 0) + 1
    lim = max(8, len(rows) * 0.02)
    common = {w for w, n in df.items() if n > lim}
    order = sorted(rows, key=lambda r: -(r["score"] or 0))
    anchors, out = [], {}
    for r in order:
        t = tk[r["id"]] - common
        for aid, at in anchors:
            if len(t & at) >= k: out[r["id"]] = aid; break
        else:
            anchors.append((r["id"], t)); out[r["id"]] = r["id"]
    return out

def feed_drift():
    """Среден наклон на НАШИТЕ публикации за последните N дни (по наклона на първоизточника)."""
    since = time.time() - S("balance_window_days", float) * 86400
    with db() as c:
        r = c.execute("select avg(s.lean), count(*) from posts p join items i on i.id=p.item_id "
                      "join sources s on s.id=i.source_id where p.status='published' and p.published>?", (since,)).fetchone()
    return (r[0] or 0.0), r[1]

def silence_groups(c):
    """{(категория, е_български): {групи}} — групи, които реално покриват категорията на този език."""
    since = time.time() - S("silence_window_days", float) * 86400
    ign = {x.strip().lower() for x in S("silence_ignore_owners").split(",") if x.strip()}
    n = {}
    for o, cat, lg, k in c.execute(
            "select s.owner, coalesce(s.category,''), coalesce(s.lang,'bg'), count(i.id) from sources s "
            "left join items i on i.source_id=s.id and i.published>? where s.active=1 and s.owner!='' "
            "and s.kind not in ('trends','wiki','gdelt') group by s.id", (since,)):
        if o.lower() not in ign: n[(o, cat, lg == "bg")] = n.get((o, cat, lg == "bg"), 0) + k
    out, mn = {}, S("silence_min_items", int)
    for (o, cat, bg), k in n.items():
        if k >= mn: out.setdefault((cat, bg), set()).add(o)
    # Твоите корекции в историите: ≥ silence_fix_min пъти „не е мълчание“ → медията не се очаква в категорията;
    # ≥ silence_fix_min пъти „трябваше да отрази“ → очаква се
    fmin = S("silence_fix_min", int)
    for o, cat, act, k in c.execute("select owner, category, action, count(*) from silence_fix group by 1,2,3"):
        if k < fmin: continue
        for bg in (True, False):
            if act == "not_silent": out.get((cat, bg), set()).discard(o)
        if act == "should_cover": out.setdefault((cat, True), set()).add(o)
    # Профилът на медията е по-силен от данните: ако са попълнени теми, медията се очаква САМО по тях
    for o, region, topics in c.execute("select owner, region, topics from media where coalesce(topics,'')!=''"):
        if o.lower() in ign: continue
        for v in out.values(): v.discard(o)
        for cat in [t.strip() for t in topics.split(",") if t.strip()]:
            out.setdefault((cat, region != "world"), set()).add(o)
    return out

def all_fixes(c):
    """{item_id: [(owner, action)]} — всички ръчни корекции наведнъж."""
    out = {}
    for iid, o, a in c.execute("select item_id, owner, action from silence_fix"): out.setdefault(iid, []).append((o, a))
    return out

def fixes_for(fixes, ids):
    rm, add = set(), set()
    for i in ids:
        for o, a in fixes.get(i, []): (rm if a == "not_silent" else add).add(o)
    return rm, add

def story_fixes(c, ids):
    """Ръчните корекции за тази история: кои медии махнахме от „мълчат“ и кои добавихме."""
    if not ids: return set(), set()
    q = ",".join("?" * len(ids))
    rm, add = set(), set()
    for o, act in c.execute(f"select owner, action from silence_fix where item_id in ({q})", list(ids)):
        (rm if act == "not_silent" else add).add(o)
    return rm, add

def expected_groups(gmap, members):
    """Кои групи се очаква да покрият историята: същата категория; ако историята е българска — само медии на
    български (Reuters, Vogue, TMZ не „мълчат“ за новина от Бургас)."""
    bg = any((m["lang"] or "bg") == "bg" for m in members)
    # основната категория на историята (най-честата), а не всички — иначе една смесена история „очаква“ всички медии
    cnt = {}
    for m in members: cnt[m["cat"]] = cnt.get(m["cat"], 0) + 1 + (m["score"] or 0) / 1000
    cat = max(cnt, key=cnt.get)
    exp = set(gmap.get((cat, True), set()))
    if not bg: exp |= gmap.get((cat, False), set())
    return exp

def learned_weights(c):
    """Учене от твоите избори: +1 за → Чернова/Бърз пост/Публикувано, −1 за ✕. По източник и по категория.
    Множителят е (харесани+1)/(общо+2) спрямо средното, омекотен с learn_strength, между ×0.6 и ×1.6."""
    since = time.time() - S("learn_window_days", float) * 86400
    k = S("learn_strength", float) / 100
    pos, tot, cpos, ctot = {}, {}, {}, {}
    for sid, cat, act in c.execute("select source_id, coalesce(category,''), action from signals where ts>?", (since,)):
        good = act in GOOD_ACTS
        if act not in GOOD_ACTS + BAD_ACTS: continue
        tot[sid] = tot.get(sid, 0) + 1; pos[sid] = pos.get(sid, 0) + good
        ctot[cat] = ctot.get(cat, 0) + 1; cpos[cat] = cpos.get(cat, 0) + good
    allp, allt = sum(pos.values()), sum(tot.values())
    base = (allp + 1) / (allt + 2)
    f = lambda p, t: max(0.6, min(1.6, 1 + k * (((p + 1) / (t + 2)) / base - 1)))
    return ({sid: f(pos[sid], tot[sid]) for sid in tot}, {cat: f(cpos[cat], ctot[cat]) for cat in ctot})

def add_signal(c, iid, action):
    r = c.execute("select i.source_id, s.category from items i join sources s on s.id=i.source_id where i.id=?", (iid,)).fetchone()
    if r: c.execute("insert into signals(ts,item_id,source_id,category,action) values(?,?,?,?,?)", (time.time(), iid, r[0], r[1], action))

def topic_rules():
    """{категория: [regex]} от topic_keywords. Къси латински думи (ai) се търсят като цели думи."""
    out = {}
    for line in S("topic_keywords").splitlines():
        if ":" not in line: continue
        cat, words = line.split(":", 1)
        rx = [re.compile(r"(?<![\wа-я])" + re.escape(w.strip().lower()) + r"(?![\wа-я])") for w in words.split(",") if w.strip()]
        if rx: out[cat.strip()] = rx
    return out

def item_topics(title, rules):
    t = (title or "").lower()
    return ",".join(cat for cat, rx in rules.items() if any(r.search(t) for r in rx))

GOOD_ACTS, BAD_ACTS = ("draft", "quick", "publish", "would", "missed"), ("reject", "wouldnt")

def learned_words(c, min_n=2):
    """Думи от заглавията на новините, които си харесал/отхвърлил (бутони + бележки). log((за+0.5)/(против+0.5))."""
    since = time.time() - S("learn_window_days", float) * 86400
    lab = {}
    for iid, act in c.execute("select item_id, action from signals where ts>? and item_id is not null "
                              "and action not in ('would','wouldnt')", (since,)):
        lab[iid] = lab.get(iid, 0) + (1 if act in GOOD_ACTS else -1 if act in BAD_ACTS else 0)
    for iid, v in c.execute("select item_id, verdict from item_notes where ts>?", (since,)):
        lab[iid] = lab.get(iid, 0) + (2 if v == "would" else -2 if v == "wouldnt" else 0)
    if not lab: return {}
    pos, neg = {}, {}
    q = ",".join("?" * len(lab))
    for iid, title, tb in c.execute(f"select id, title, title_bg from items where id in ({q})", list(lab)):
        v = lab.get(iid, 0)
        if not v: continue
        for w in toks((title or "") + " " + (tb or "")):
            (pos if v > 0 else neg)[w] = (pos if v > 0 else neg).get(w, 0) + 1
    out = {}
    for w in set(pos) | set(neg):
        p, n = pos.get(w, 0), neg.get(w, 0)
        if p + n >= min_n and abs(p - n) >= 1: out[w] = (math.log((p + 0.5) / (n + 0.5)), p, n)
    return out

def score_all():
    now = time.time()
    hl = S("recency_halflife_h", float) * 3600
    W = {k: S(f"w_{k}", float) / 100 for k in ("recency", "trend", "cross", "keyword", "silence", "balance", "learn")}
    boost = [k.strip().lower() for k in S("boost_keywords").split(",") if k.strip()]
    block = [k.strip().lower() for k in S("block_keywords").split(",") if k.strip()]
    thr = S("recommend_threshold", float)
    drift, _ = feed_drift()
    with db() as c:
        trend_terms = [r["term"] for r in c.execute("select term from trends where fetched>?", (now - 48 * 3600,))]
        gmap, gmin = silence_groups(c), S("silence_min_groups", int)
        lsrc, lcat = learned_weights(c)
        lwords, trules = learned_words(c), topic_rules()
        rows = c.execute("select i.*, s.weight, s.owner, s.lean, s.tone, coalesce(s.category,'') cat, coalesce(s.lang,'bg') lang from items i join sources s on s.id=i.source_id "
                         "where i.published > ? and i.status in ('new','recommended')",
                         (now - 72 * 3600,)).fetchall()
        tk = {r["id"]: toks((r["title"] or "") + " " + (r["title_bg"] or "")) for r in rows}
        for r in rows:
            tp = item_topics((r["title"] or "") + " " + (r["title_bg"] or ""), trules)
            if tp != (r["topics"] or ""): c.execute("update items set topics=? where id=?", (tp, r["id"]))
        cmn, cmn1 = common_tokens(list(tk.values())), common_tokens(list(tk.values()), 0.001)
        trend_t = [tt for tt in (trend_tokens(x, cmn, cmn1) for x in trend_terms) if tt]
        cl = clusters(rows, tk, S("cluster_min_overlap", int))
        by = {}
        for r in rows: by.setdefault(cl[r["id"]], []).append(r)
        exp_by = {k: expected_groups(gmap, ms) for k, ms in by.items()}
        for r in rows:
            t, title = tk[r["id"]], r["title"].lower()
            if any(b in title for b in block):
                c.execute("update items set score=0,status='rejected',why='блок дума' where id=?", (r["id"],)); continue
            mates = by[cl[r["id"]]]
            rec = math.exp(-(now - r["published"]) / hl) if hl else 1
            tr = 1.0 if any(trend_hit(t, tt) for tt in trend_t) else 0
            nsrc = len({m["source_id"] for m in mates}) - 1
            crs = min(nsrc, 3) / 3
            kw = 1.0 if any(k in title for k in boost) else 0
            # ТИШИНА = обществен интерес × липсващо покритие от медийните групи
            covered = {m["owner"] for m in mates if m["owner"]}
            groups = exp_by[cl[r["id"]]]
            gap = 1 - len(covered & groups) / len(groups) if len(groups) >= max(2, gmin) else 0
            silence = tr * gap
            # БАЛАНС = насочва към източници от другата страна на текущия ни наклон
            bal = max(0.0, -drift * (r["lean"] or 0))
            lw = lsrc.get(r["source_id"], 1.0) * lcat.get(r["cat"], 1.0)
            lw = max(0.5, min(2.0, lw))
            hits = sorted(((lwords[w][0], w) for w in t if w in lwords), key=lambda x: -abs(x[0]))[:3]
            lword = max(-1.0, min(1.0, sum(h[0] for h in hits) / 3)) if hits else 0.0
            sc = lw * 100 * r["weight"] * (W["recency"] * rec + W["trend"] * tr + W["cross"] * crs + W["keyword"] * kw
                                      + W["silence"] * silence + W["balance"] * min(bal, 1) + W["learn"] * lword)
            sc = max(0.0, sc)
            why = ", ".join(x for x, y in [("тренд", tr), (f"{nsrc} др. източника", crs), ("ключова дума", kw),
                                           (f"тишина {silence:.0%}", silence), ("баланс", bal),
                                           (f"твоите избори ×{lw:.1f}", abs(lw - 1) >= 0.1),
                                           ("думи, които " + ("харесваш" if lword > 0 else "отхвърляш") + ": " + ", ".join(h[1] for h in hits), abs(lword) >= 0.15)] if y)
            st = "recommended" if sc >= thr else "new"
            c.execute("update items set score=?,status=?,why=?,cluster=?,silence=? where id=?",
                      (round(sc, 1), st, why, cl[r["id"]], round(silence, 2), r["id"]))

# ---------------------------------------------------------------- Интеграции
def best_image(post):
    """Снимката на поста, иначе най-добрата от същата история (първо медии, после X)."""
    if post.get("image"): return post["image"]
    with db() as c:
        it = c.execute("select cluster from items where id=?", (post.get("item_id"),)).fetchone()
        if not it or it[0] is None: return None
        r = c.execute("select i.image from items i join sources s on s.id=i.source_id where i.cluster=? and coalesce(i.image,'')!='' "
                      "order by (s.kind='x'), i.score desc limit 1", (it[0],)).fetchone()
    return r[0] if r else None

async def ai_hook(post):
    """Claude пише текста за шаблона: заглавие-кукичка + подзаглавие. Без ключ → взима заглавието."""
    if not claude_ready(): return post["title"][:70], ""
    prompt = (f"Напиши текст за Instagram визия на българската медия „{S('site_name')}“. Стил: {S('hook_style')}. "
              f"Само факти, които ги има в текста по-долу и в посочените източници — нищо измислено, без преувеличение. Върни точно 2 реда: 1) заглавие 2) подзаглавие.\n\n"
              f"Новина: {post['title']}\n{(post['body'] or '')[:1500]}")
    txt = await ask_claude(prompt, max_tokens=200, timeout=90)
    lines = [l.strip(" -•\"„“1234567890).*") for l in txt.splitlines() if l.strip()]
    return (lines[0] if lines else post["title"])[:90], (lines[1] if len(lines) > 1 else "")[:140]

CANVA_SCOPES = ("asset:read asset:write brandtemplate:meta:read brandtemplate:content:read "
                "design:meta:read design:content:read design:content:write profile:read")
CANVA_REDIRECT = f"{PUBLIC_URL}/admin/canva/callback"   # локално: http://127.0.0.1:PORT (Canva не приема "localhost")
_PKCE = {}

def _set(k, v):
    with db() as c: c.execute("insert or replace into settings values(?,?)", (k, str(v)))

async def canva_oauth_token(data):
    auth = base64.b64encode(f"{S('canva_client_id')}:{S('canva_client_secret')}".encode()).decode()
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.post("https://api.canva.com/rest/v1/oauth/token", data=data,
                         headers={"Authorization": f"Basic {auth}"})
    if r.status_code >= 300: raise RuntimeError(f"Canva OAuth {r.status_code}: {r.text[:200]}")
    j = r.json()
    _set("canva_token", j["access_token"]); _set("canva_token_expires", time.time() + j.get("expires_in", 3600) - 60)
    if j.get("refresh_token"): _set("canva_refresh_token", j["refresh_token"])   # refresh токенът е еднократен

async def canva_tok():
    """Валиден access token; подновява се сам с refresh token."""
    if S("canva_refresh_token") and time.time() > S("canva_token_expires", float):
        await canva_oauth_token({"grant_type": "refresh_token", "refresh_token": S("canva_refresh_token")})
    return S("canva_token")

async def canva_fields():
    tok, tpl = await canva_tok(), S("canva_template_id")
    if not (tok and tpl): return None, "Липсва canva_token/canva_template_id"
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.get(f"https://api.canva.com/rest/v1/brand-templates/{tpl}/dataset", headers={"Authorization": f"Bearer {tok}"})
    if r.status_code >= 300: return None, f"Canva грешка {r.status_code}: {r.text[:200]}"
    return r.json().get("dataset", {}), "ok"

async def canva_make(post, export=False):
    """1 бутон: снимка → Canva asset → autofill на нашия шаблон → линк за редакция (+ PNG export при автопилот)."""
    tok, tpl = await canva_tok(), S("canva_template_id")
    if not (tok and tpl): return None, "Липсва canva_token/canva_template_id"
    fields, msg = await canva_fields()
    if fields is None: return None, msg
    F = {k: S(f"canva_field_{k}") for k in ("headline", "sub", "image", "source")}
    data = {}
    if F["headline"] in fields: data[F["headline"]] = {"type": "text", "text": post["hook"] or post["title"]}
    if F["sub"] in fields and post.get("sub"): data[F["sub"]] = {"type": "text", "text": post["sub"]}
    if F["source"] in fields: data[F["source"]] = {"type": "text", "text": S("site_name")}
    h = {"Authorization": f"Bearer {tok}"}
    async with httpx.AsyncClient(base_url="https://api.canva.com/rest/v1", headers=h, timeout=60) as c:
        img = best_image(post)
        if img and F["image"] in fields:          # Autofill приема само качени assets → първо качваме по URL
            r = await c.post("/url-asset-uploads", json={"name": f"news-{post['id']}", "url": img})
            if r.status_code < 300:
                jid = r.json()["job"]["id"]
                for _ in range(15):
                    await asyncio.sleep(2)
                    j = (await c.get(f"/url-asset-uploads/{jid}")).json()["job"]
                    if j["status"] == "success": data[F["image"]] = {"type": "image", "asset_id": j["asset"]["id"]}; break
                    if j["status"] == "failed": break
        r = await c.post("/autofills", json={"brand_template_id": tpl, "title": (post["hook"] or post["title"])[:200], "data": data})
        if r.status_code >= 300: return None, f"Canva грешка {r.status_code}: {r.text[:200]}"
        job = r.json()["job"]["id"]
        design = None
        for _ in range(30):
            await asyncio.sleep(2)
            j = (await c.get(f"/autofills/{job}")).json()["job"]
            if j["status"] == "success": design = j["result"]["design"]; break
            if j["status"] == "failed": return None, f"Canva autofill failed: {j.get('error')}"
        if not design: return None, "Canva: таймаут"
        edit = design["urls"]["edit_url"]
        with db() as d: d.execute("update posts set canva_url=?, canva_design_id=? where id=?", (edit, design["id"], post["id"]))
        if export:
            r = await c.post("/exports", json={"design_id": design["id"], "format": {"type": "png"}})
            if r.status_code < 300:
                eid = r.json()["job"]["id"]
                for _ in range(30):
                    await asyncio.sleep(2)
                    j = (await c.get(f"/exports/{eid}")).json()["job"]
                    if j["status"] == "success":
                        png = (await c.get(j["urls"][0])).content          # линковете на Canva изтичат → пазим файла
                        path = STATIC / "exports" / f"{post['id']}.png"; path.write_bytes(png)
                        with db() as d: d.execute("update posts set export_path=? where id=?", (f"/static/exports/{post['id']}.png", post["id"]))
                        break
    return edit, "Canva дизайнът е готов"

def story_images(post, limit=12):
    """Всички различни снимки от историята на поста (първо медиите, после X)."""
    out = [post.get("image")] if post.get("image") else []
    with db() as c:
        it = c.execute("select cluster from items where id=?", (post.get("item_id"),)).fetchone() if post.get("item_id") else None
        if it and it[0] is not None:
            for (u,) in c.execute("select i.image from items i join sources s on s.id=i.source_id where i.cluster=? "
                                  "and coalesce(i.image,'')!='' order by (s.kind='x'), i.score desc", (it[0],)):
                if u not in out: out.append(u)
    return out[:limit]

async def canva_upload_all(post):
    """Качва останалите снимки от историята в Canva (папка Uploads), за да ги имаш под ръка в редактора."""
    tok = await canva_tok()
    imgs = story_images(post, 1 + S("canva_extra_images", int))[1:]
    if not (tok and imgs): return 0
    async with httpx.AsyncClient(base_url="https://api.canva.com/rest/v1", headers={"Authorization": f"Bearer {tok}"}, timeout=30) as c:
        rs = await asyncio.gather(*(c.post("/url-asset-uploads", json={"name": f"news-{post['id']}-{n + 2}", "url": u})
                                    for n, u in enumerate(imgs)), return_exceptions=True)
    ok = sum(1 for r in rs if not isinstance(r, Exception) and r.status_code < 300)
    log(f"post {post['id']} → Canva: още {ok} снимки в Uploads")
    return ok

async def canva_pull(post):
    """⬇ Вземи от Canva: експортира редактирания дизайн (всички страници = карусел) като PNG и ги закача към поста."""
    tok = await canva_tok()
    if not (tok and post.get("canva_design_id")): return "Няма Canva дизайн за този пост — първо натисни 🎨"
    async with httpx.AsyncClient(base_url="https://api.canva.com/rest/v1", headers={"Authorization": f"Bearer {tok}"}, timeout=60) as c:
        r = await c.post("/exports", json={"design_id": post["canva_design_id"], "format": {"type": "png"}})
        if r.status_code >= 300: return f"Canva грешка {r.status_code}: {r.text[:200]}"
        eid = r.json()["job"]["id"]
        for _ in range(45):
            await asyncio.sleep(2)
            j = (await c.get(f"/exports/{eid}")).json()["job"]
            if j["status"] == "success": break
            if j["status"] == "failed": return f"Canva export failed: {j.get('error')}"
        else: return "Canva: таймаут при експорта"
        paths = []
        for n, u in enumerate(j["urls"]):             # линковете на Canva изтичат → пазим файловете при нас
            fn = f"{post['id']}-{n + 1}.png"; (STATIC / "exports" / fn).write_bytes((await c.get(u)).content)
            paths.append(f"/static/exports/{fn}")
    with db() as d: d.execute("update posts set export_path=?, export_json=? where id=?", (paths[0], json.dumps(paths), post["id"]))
    return f"Взети {len(paths)} {'снимка' if len(paths) == 1 else 'снимки (карусел)'} от Canva — готови за публикуване"

async def autopilot():
    """Препоръчани истории над прага → чернова + AI текст (+ Canva). Нищо не се публикува само."""
    mode = S("auto_mode")
    if mode not in ("draft", "canva"): return
    day = time.time() - 86400
    with db() as c:
        done = c.execute("select count(*) from posts where auto=1 and created>?", (day,)).fetchone()[0]
        room = S("auto_max_per_day", int) - done
        if room <= 0: return
        seen = set()
        cand = []
        for r in c.execute("select * from items where status='recommended' and coalesce(verdict,'')!='wouldnt' and score>=? "
                           "order by score desc", (S("auto_threshold", float),)):
            if r["cluster"] in seen: continue      # една история = една чернова
            seen.add(r["cluster"]); cand.append(r)
            if len(cand) >= room: break
    for it in cand:
        with db() as c:
            pid = c.execute("insert into posts(item_id,title,body,image,created,status,auto) values(?,?,?,?,?,'ready',1)",
                            (it["id"], it["title"], it["summary"], it["image"], time.time())).lastrowid
            c.execute("update items set status='draft' where cluster=?", (it["cluster"],))
            p = dict(c.execute("select * from posts where id=?", (pid,)).fetchone())
        try:
            hook, sub = await ai_hook(p)
            with db() as c: c.execute("update posts set hook=?, sub=? where id=?", (hook, sub, pid))
            p.update(hook=hook, sub=sub)
            if mode == "canva": await canva_make(p, export=True)
        except Exception as ex: log(f"autopilot {pid}: {ex}")
        log(f"Автопилот: чернова {pid} „{p['title'][:50]}“")

async def instagram_publish(post):
    """Instagram API with Instagram Login: контейнер -> media_publish. Публикува визията от Canva (всички страници =
    карусел), ако я има; иначе снимката на поста. Снимките трябва да са на публичен адрес (public_base_url)."""
    uid, tok = S("ig_user_id"), S("ig_token")
    if not (uid and tok): return "Липсва ig_user_id/ig_token"
    base = S("public_base_url").rstrip("/")
    exports = json.loads(post.get("export_json") or "[]") or ([post["export_path"]] if post.get("export_path") else [])
    if exports:
        if not base: return "Визията от Canva е локална — задай public_base_url (напр. Cloudflare Tunnel), за да я види Instagram"
        imgs = [base + e for e in exports][:10]               # Instagram карусел: до 10
    elif post["image"] and post["image"].startswith("http"): imgs = [post["image"]]
    else: return "Нужна е визия от Canva (⬇ Вземи от Canva) или публична снимка"
    cap = f"{post['title']}\n\n{(post['body'] or '')[:1800]}"
    async with httpx.AsyncClient(base_url=ig_host(), timeout=60) as c:
        if len(imgs) == 1:
            r = await c.post(f"/{uid}/media", data={"image_url": imgs[0], "caption": cap, "access_token": tok})
        else:
            kids = []
            for u in imgs:
                r = await c.post(f"/{uid}/media", data={"image_url": u, "is_carousel_item": "true", "access_token": tok})
                if r.status_code >= 300: return f"IG карусел грешка: {r.text[:200]}"
                kids.append(r.json()["id"])
            r = await c.post(f"/{uid}/media", data={"media_type": "CAROUSEL", "children": ",".join(kids), "caption": cap, "access_token": tok})
        if r.status_code >= 300: return f"IG контейнер грешка: {r.text[:200]}"
        cid = r.json()["id"]
        await asyncio.sleep(5)
        r = await c.post(f"/{uid}/media_publish", data={"creation_id": cid, "access_token": tok})
        kind = "карусел" if len(imgs) > 1 else ("визията от Canva" if exports else "снимката")
        return f"Публикувано в Instagram ({kind})" if r.status_code < 300 else f"IG publish грешка: {r.text[:200]}"

async def telegram_publish(post):
    """Telegram Bot API — безплатно, без одобрение. Ботът трябва да е админ в канала."""
    tok, chat = S("telegram_bot_token"), S("telegram_chat_id")
    if not (tok and chat): return "Липсва telegram_bot_token/telegram_chat_id"
    text = f"<b>{post['title']}</b>\n\n{(post['body'] or '')[:900]}"
    base = f"https://api.telegram.org/bot{tok}"
    async with httpx.AsyncClient(timeout=30) as c:
        if post["image"]:
            r = await c.post(f"{base}/sendPhoto", data={"chat_id": chat, "photo": post["image"], "caption": text[:1024], "parse_mode": "HTML"})
        else:
            r = await c.post(f"{base}/sendMessage", data={"chat_id": chat, "text": text, "parse_mode": "HTML"})
    return "Публикувано в Telegram" if r.json().get("ok") else f"Telegram грешка: {r.text[:200]}"

async def threads_publish(post):
    """Threads API: контейнер → threads_publish. 500 знака, до 250 поста/24ч."""
    uid, tok = S("threads_user_id"), S("threads_token")
    if not (uid and tok): return "Липсва threads_user_id/threads_token"
    data = {"text": f"{post['title']}\n\n{post['body'] or ''}"[:500], "access_token": tok,
            "media_type": "IMAGE" if post["image"] else "TEXT"}
    if post["image"]: data["image_url"] = post["image"]
    async with httpx.AsyncClient(base_url="https://graph.threads.net/v1.0", timeout=60) as c:
        r = await c.post(f"/{uid}/threads", data=data)
        if r.status_code >= 300: return f"Threads грешка: {r.text[:200]}"
        await asyncio.sleep(5)
        r = await c.post(f"/{uid}/threads_publish", data={"creation_id": r.json()["id"], "access_token": tok})
    return "Публикувано в Threads" if r.status_code < 300 else f"Threads грешка: {r.text[:200]}"

async def claude_framing(cluster_id):
    """Сравнява как различните медийни групи рамкират една и съща история (без web търсене)."""
    if not claude_ready(): return "Claude не е свързан (виж claude_mode в Настройки)"
    with db() as c:
        rows = c.execute("select i.title, i.summary, s.name, s.owner from items i join sources s on s.id=i.source_id "
                         "where i.cluster=?", (cluster_id,)).fetchall()
    lines = "\n".join(f"- [{r['owner'] or '?'} / {r['name']}] {r['title']} — {(r['summary'] or '')[:200]}" for r in rows)
    prompt = ("Сравни как различните медии рамкират една и съща история. За всяка: натоварени/оценъчни думи, кой е "
              "активният субект, какво е изпуснато спрямо другите. После дай НЕУТРАЛНО заглавие и 3 факта, общи за всички. "
              "Не преценявай кой е прав. Кратко, на български.\n\n" + lines)
    try: return await ask_claude(prompt, max_tokens=1200)
    except Exception as ex: return f"Claude грешка: {ex}"

async def tiktok_draft(post):
    # TikTok Content Posting API: без audit всичко е SELF_ONLY. Безопасният път е Upload (в inbox като чернова),
    # човек натиска Publish в приложението. Нужни: видео файл + Login Kit OAuth.
    return "TikTok: изпращането като чернова изисква видео + OAuth (виж README) — не е активирано"

async def ig_check_profile(handle):
    uid, tok = S("ig_user_id"), S("ig_token")
    if not (uid and tok): return {"note": "Business Discovery изисква ig токен; само Business/Creator профили"}
    fields = f"business_discovery.username({handle}){{followers_count,media_count,media.limit(5){{caption,like_count,comments_count,permalink,timestamp}}}}"
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.get(f"https://graph.facebook.com/v23.0/{uid}", params={"fields": fields, "access_token": tok})
        return r.json()

def imap_poll():
    """Имейл -> чернова. Само писма, чиято тема съдържа imap_secret (напр. #post)."""
    host, user, pw, tag = S("imap_host"), S("imap_user"), S("imap_pass"), S("imap_secret")
    if not (host and user and pw): return "IMAP не е настроен"
    m = imaplib.IMAP4_SSL(host); m.login(user, pw); m.select("INBOX")
    _, ids = m.search(None, "UNSEEN"); n = 0
    for i in ids[0].split():
        _, data = m.fetch(i, "(RFC822)"); msg = email.message_from_bytes(data[0][1])
        subj = "".join(p.decode(enc or "utf-8") if isinstance(p, bytes) else p
                       for p, enc in decode_header(msg.get("Subject", "")))
        if tag not in subj: continue
        body = ""
        for part in msg.walk():
            if part.get_content_type() == "text/plain":
                body = part.get_payload(decode=True).decode(part.get_content_charset() or "utf-8", "ignore"); break
        with db() as c:
            c.execute("insert into posts(title,body,created) values(?,?,?)",
                      (subj.replace(tag, "").strip(), body.strip(), time.time()))
        n += 1
    m.logout(); return f"Имейл: {n} нови чернови"

async def claude_brief():
    """Един бутон: тегли всичко (безплатно), после Claude анализира топ новините + трендовете
    и с няколко web search-а проверява какво липсва. Връща редакционен бриф."""
    if not claude_ready(): return "Claude не е свързан (виж claude_mode в Настройки)"
    await fetch_all()
    with db() as c:
        tr = [r["term"] + (f" ({r['traffic']})" if r["traffic"] else "") for r in
              c.execute("select term,traffic from trends order by fetched desc, id limit 25")]
        it = [f"[{r['score']}] {r['title']} — {r['src']} — {r['link']}" for r in c.execute(
            "select i.*, s.name src from items i join sources s on s.id=i.source_id "
            "where i.status in ('new','recommended') order by score desc limit 60")]
    prompt = (f"Ти си главен редактор на българската медия „{S('site_name')}“. Днес е {datetime.now():%d.%m.%Y %H:%M}.\n"
              f"Google Trends BG сега:\n- " + "\n- ".join(tr) +
              "\n\nНовини от RSS (резултат в скоби):\n- " + "\n- ".join(it) +
              f"\n\nЗадачи: 1) Обясни накратко защо се търси всеки от топ 10 тренда (ползвай web search само ако не е ясно от новините). "
              f"2) Избери 5–8 теми за публикуване днес: заглавие-кукичка, ъгъл, формат (reel/карусел/пост/статия), източници (линкове). "
              f"3) Какво важно липсва в нашите източници. Ключови думи на медията: {S('boost_keywords')}. Пиши на български, кратко, с точки.")
    try: text = await ask_claude(prompt, max_tokens=3000, web=True, timeout=420)
    except Exception as ex: return f"Claude грешка: {ex}"
    text += f"\n\n— през {'API' if claude_how() == 'api' else 'Claude Code CLI'}"
    with db() as d: d.execute("update settings set value=? where key='claude_brief'", (f"{datetime.now():%d.%m %H:%M}\n{text}",))
    return "Claude брифът е готов"

# ---------------------------------------------------------------- Шаблони
BASE = """<!doctype html><html lang=bg><head><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<meta name=referrer content=no-referrer><title>{{ site }}</title><script src="https://cdn.jsdelivr.net/npm/hls.js@1"></script><style>
:root{--b:#111;--m:#666;--l:#eee;--a:#d12}*{box-sizing:border-box}body{margin:0;font:16px/1.5 system-ui,sans-serif;color:var(--b);background:#fafafa}
header{background:#111;color:#fff;padding:12px 20px;display:flex;gap:16px;align-items:center;flex-wrap:wrap}header a{color:#fff;text-decoration:none}
.logo{font-weight:800;font-size:20px;margin-right:auto}main{max-width:1100px;margin:0 auto;padding:20px}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:16px}.card{background:#fff;border:1px solid var(--l);border-radius:10px;overflow:hidden}
.card img{width:100%;height:180px;object-fit:cover;background:#ddd}.card .t{padding:12px}.card h3{margin:0 0 6px;font-size:17px}
.muted{color:var(--m);font-size:13px}.btn{display:inline-block;background:var(--b);color:#fff;border:0;border-radius:6px;padding:7px 12px;cursor:pointer;text-decoration:none;font-size:14px}
.btn.red{background:var(--a)}.btn.gray{background:#888}table{width:100%;border-collapse:collapse;background:#fff}td,th{border-bottom:1px solid var(--l);padding:8px;text-align:left;vertical-align:top;font-size:14px}
input,textarea,select{width:100%;padding:7px;border:1px solid #ccc;border-radius:6px;font:inherit}textarea{min-height:120px}
.tabs a{margin-right:10px}.flash{background:#ffe;border:1px solid #ec0;padding:8px;border-radius:6px;margin-bottom:12px}
video,iframe{width:100%;aspect-ratio:16/9;background:#000;border:0;border-radius:8px}.thumb{width:90px;height:60px;object-fit:cover}
.tag{display:inline-block;padding:1px 8px;border-radius:999px;font-size:12px;font-weight:600;line-height:18px;white-space:nowrap;border:1px solid transparent}
.tag.src{font-weight:500;filter:saturate(1.1) brightness(.85)}.tabs .tag{margin:2px 2px}.tag.off{opacity:.35}.tag.intl{background:#111;color:#fff}
</style></head><body><header><a class=logo href="/">{{ site }}</a><a href="/live">На живо</a>
{% if donate %}<a class="btn red" href="{{ donate }}" target=_blank>Подкрепи ни</a>{% endif %}<a href="/admin">Админ</a>{% if hot %}<a href="/admin/trends" class="btn red" style="padding:4px 10px">🔥 {{ hot }} за пускане</a>{% endif %}</header>
<main>{% if flash %}<div class=flash>{{ flash }}</div>{% endif %}{% block c %}{% endblock %}</main>
{% if path and path.startswith('/admin') and not path.startswith('/admin/login') %}
<details style="position:fixed;right:16px;bottom:16px;background:#fff;border:1px solid #ccc;border-radius:10px;padding:8px 12px;box-shadow:0 4px 16px rgba(0,0,0,.15);max-width:340px;z-index:9">
<summary style="cursor:pointer;font-weight:600">💬 Коментар за тази страница</summary>
<form method=post action="/admin/feedback" style="margin-top:8px"><input type=hidden name=kind value=feature><input type=hidden name=page value="{{ path }}">
<textarea name=text style="min-height:80px" placeholder="Какво да се промени тук? Кое е бавно, неясно, излишно?"></textarea>
<div style="margin-top:6px"><button class="btn gray" name=rating value=-1>👎</button> <button class=btn name=rating value=0>Изпрати</button> <button class="btn gray" name=rating value=1>👍</button></div></form></details>{% endif %}
</body></html>"""

T = {
"base": BASE,
"home": """{% extends 'base' %}{% block c %}<div class=grid>{% for p in posts %}<a class=card href="/p/{{p.id}}" style="color:inherit;text-decoration:none">
{% if p.image %}<img src="{{ p.image }}" alt="" loading=lazy onerror="this.style.visibility='hidden'">{% else %}<img alt="">{% endif %}<div class=t><h3>{{ p.title }}</h3><div class=muted>{{ p.when }}</div></div></a>
{% else %}<p>Още няма публикации. Влез в <a href=/admin>админа</a>.</p>{% endfor %}</div>{% endblock %}""",
"post": """{% extends 'base' %}{% block c %}<article style="max-width:760px;margin:auto;background:#fff;padding:24px;border-radius:10px">
<h1>{{ p.title }}</h1><div class=muted>{{ p.when }}</div>
{% if p.media_url and ('x.com/' in p.media_url or 'twitter.com/' in p.media_url) %}
<blockquote class="twitter-tweet"><a href="{{ p.media_url|replace('x.com','twitter.com') }}"></a></blockquote><script async src="https://platform.twitter.com/widgets.js"></script>
{% elif yt %}<iframe src="https://www.youtube.com/embed/{{ yt }}" allowfullscreen></iframe>
{% elif p.media_url and p.media_url.endswith('.mp3') %}<audio controls src="{{ p.media_url }}" style="width:100%"></audio>
{% elif p.media_url %}<video controls src="{{ p.media_url }}"></video>
{% elif p.image %}<img src="{{ p.image }}" style="width:100%;border-radius:8px">{% endif %}
<div style="white-space:pre-wrap;margin-top:16px">{{ p.body }}</div>
{% if src %}<p class=muted>Източник: <a href="{{ src }}" target=_blank rel=noopener>{{ src[:60] }}</a></p>{% endif %}
{% if donate %}<p><a class="btn red" href="{{ donate }}" target=_blank>Подкрепи независимата медия</a></p>{% endif %}</article>{% endblock %}""",
"live": """{% extends 'base' %}{% block c %}<h2>На живо</h2>{% if hls %}<video id=v controls autoplay muted></video><script>
const v=document.getElementById('v'),u="{{ hls }}";if(v.canPlayType('application/vnd.apple.mpegurl'))v.src=u;else if(Hls.isSupported()){const h=new Hls();h.loadSource(u);h.attachMedia(v)}</script>
{% else %}<p class=muted>Няма зададен live_hls_url в Настройки.</p>{% endif %}{% endblock %}""",
"login": """{% extends 'base' %}{% block c %}<form method=post style="max-width:320px"><h2>Вход</h2>{% if 'bad=1' in path %}<p style="color:#c33">Грешна парола</p>{% endif %}<input type=password name=pw placeholder="Парола"><br><br><button class=btn>Вход</button></form>{% endblock %}""",
"admin": """{% extends 'base' %}{% block c %}
<div class=tabs><b>Админ:</b> <a href="{{ url(tab='rec') }}" {% if tab=='rec' %}style="font-weight:bold"{% endif %}>Препоръчани</a><a href="{{ url(tab='all') }}" {% if tab=='all' %}style="font-weight:bold"{% endif %}>Всички</a><a href="/admin/posts">Чернови/публикации</a>
<a href="/admin/trends"><b>🧭 Компас</b></a><a href="/admin/logic">🧠 Логика</a><a href="/admin/missed">🕳 Пропусната новина</a><a href="/admin/bias">Баланс</a><a href="/admin/map">🗺 Карта</a><a href="/admin/media">📇 Медии</a><a href="/admin/sources">Източници</a><a href="/admin/instagram">📸 Instagram</a><a href="/admin/canva/setup">🎨 Canva</a><a href="/admin/improve">Подобри</a><a href="/admin/profiles">Профили</a><a href="/admin/settings">Настройки</a><a href="/admin/log">Лог</a></div><br>
<form method=post action="/admin/claude" style="display:inline"><button class="btn red">⟳ Обнови всичко + Claude бриф</button></form>
<a class=btn href="/admin/test">Тест на източниците</a>
<form method=post action="/admin/fetch" style="display:inline"><button class=btn>Изтегли сега</button></form>
<form method=post action="/admin/imap" style="display:inline"><button class="btn gray">Провери имейл</button></form>
<form method=post action="/admin/autopilot" style="display:inline"><button class="btn gray">Автопилот сега</button></form><br><br>
<form method=get style="display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-bottom:8px">
<input type=hidden name=tab value="{{ tab }}"><input type=hidden name=cat value="{{ cat }}"><input type=hidden name=lang value="{{ lang }}">
<input name=q value="{{ q }}" placeholder="Търси в заглавията" style="width:220px">
<select name=src style="width:200px"><option value="">Всички източници</option>{% for s in srcs %}<option value="{{ s.id }}" {% if src==s.id|string %}selected{% endif %}>{{ s.name }}</option>{% endfor %}</select>
<select name=region style="width:170px"><option value="">Всички региони</option>{% for r in regions %}<option {% if region==r %}selected{% endif %}>{{ r }}</option>{% endfor %}</select>
<select name=sort style="width:150px"><option value="">По оценка</option><option value=new {% if sort=='new' %}selected{% endif %}>Най-скорошни</option></select>
<label style="white-space:nowrap"><input type=checkbox name=img value=1 style="width:auto" {% if img=='1' %}checked{% endif %}> само със снимка</label>
<button class=btn>Филтрирай</button> <a href="/admin?tab={{ tab }}">Изчисти</a></form>
<div class=tabs><b>Категория:</b> <a href="{{ url(cat='') }}" {% if not cat %}style="font-weight:bold;text-decoration:underline"{% endif %}>Всички</a>
{% for k in cats %}<a href="{{ url(cat=k) }}" style="text-decoration:none"><span class="tag {% if cat and k!=cat %}off{% endif %}" style="background:{{ colors.get(k, '#6b7280') }};color:{{ txt(colors.get(k, '#6b7280')) }}">{{ k }} · {{ counts.get(k, 0) }}</span></a>{% endfor %}</div><br>
{% if brief %}<details open style="background:#fff;border:1px solid #ddd;border-radius:8px;padding:12px;margin-bottom:12px"><summary><b>Claude бриф</b></summary>
<div style="white-space:pre-wrap;font-size:14px">{{ brief }}</div></details>{% endif %}
<table><tr><th></th><th>Новина</th><th>Резултат</th><th></th></tr>{% for i in items %}<tr>
<td>{% if i.image %}<img class=thumb src="{{ i.image }}" loading=lazy onerror="this.style.display='none'">{% endif %}</td>
<td><a href="{{ i.link }}" target=_blank>{{ i.title_bg or i.title }}</a>{% if i.title_bg %}<div class=muted style="font-style:italic">{{ i.title }}</div>{% endif %}
<div style="margin-top:3px">{{ tag(i.cat) }}{% for t in (i.topics or '').split(',') if t and t!=i.cat %} {{ tag(t) }}{% endfor %} {{ src_tag(i.src, i.cat, i.source_id) }}{% if i.lang and i.lang!='bg' %} <span class="tag intl">{{ i.lang|upper }}</span>{% endif %} <span class=muted>{{ i.when }} · {{ i.why }}</span></div></td>
<td><b>{{ i.score }}</b></td><td style="white-space:nowrap">
<form method=post action="/admin/draft/{{ i.id }}" style="display:inline"><button class=btn>→ Чернова</button></form>
{% if i.lang and i.lang!='bg' %}<form method=post action="/admin/intl/{{ i.id }}" style="display:inline"><button class="btn red" title="Превод на български като международен пост">🌍 Международен</button></form>{% endif %}
{{ vote(i.id, i.verdict) }} {{ note_form(i.id, i.verdict) }}</td></tr>
{% else %}<tr><td colspan=4>{% if q or src or lang or img or cat %}Няма новини за тези филтри.{% else %}Няма. Натисни „Изтегли сега“.{% endif %}</td></tr>{% endfor %}</table>{% endblock %}""",
"posts": """{% extends 'base' %}{% block c %}<p><a href=/admin>← Назад</a> · <a class=btn href="/admin/post/new">+ Нова</a></p>
<table>{% for p in posts %}<tr><td>{% if p.image %}<img class=thumb src="{{ p.image }}">{% endif %}</td><td>{% if p.intl %}<span class="tag intl">🌍 International</span> {% endif %}<a href="/admin/post/{{ p.id }}">{{ p.title }}</a>
<div class=muted>{{ p.status }} · {{ p.ch }}{% if p.canva_url %} · <a href="{{ p.canva_url }}" target=_blank>Canva</a>{% endif %}</div></td>
<td>{% if p.status!='published' %}<form method=post action="/admin/post/{{ p.id }}/delete" onsubmit="return confirm('Да изтрия ли черновата „{{ p.title|e|replace("'", "") }}“?')"><button class="btn gray" title="Изтрий черновата">🗑</button></form>{% endif %}</td></tr>{% endfor %}</table>{% endblock %}""",
"edit": """{% extends 'base' %}{% block c %}<p><a href=/admin/posts>← Назад</a>{% if p.intl %} <span class="tag intl">🌍 International пост — превод</span>{% endif %}</p><form method=post action="/admin/post/{{ p.id or 'new' }}">
Заглавие<input name=title value="{{ p.title or '' }}"><br>Текст<textarea name=body>{{ p.body or '' }}</textarea><br>
<div style="display:flex;gap:12px"><div style="flex:1">Текст за визията — заглавие<input name=hook value="{{ p.hook or '' }}" placeholder="AI го попълва при натискане на Canva бутона"></div>
<div style="flex:1">Подзаглавие<input name=sub value="{{ p.sub or '' }}"></div></div><br>
Снимка (URL){% if p.image %} <img src="{{ p.image }}" style="height:40px;vertical-align:middle"> <a href="#imgfb" class=muted>💬 коментар за снимката</a>{% endif %}<input name=image value="{{ p.image or '' }}"><br>Медия (YouTube / .mp4 / .mp3 / .m3u8)<input name=media_url value="{{ p.media_url or '' }}"><br><br>
<button class=btn name=act value=save>Запази</button> <button class=btn name=act value=publish>Публикувай в сайта</button></form><br>
{% if p.id and p.image %}<form id=imgfb method=post action="/admin/feedback" style="display:flex;gap:8px;align-items:center;background:#fff;border:1px solid #eee;border-radius:8px;padding:8px;margin:8px 0">
<img src="{{ p.image }}" style="height:70px;border-radius:6px"><input type=hidden name=kind value=image><input type=hidden name=ref value="{{ p.image }}"><input type=hidden name=page value="/admin/post/{{ p.id }}">
<input name=text placeholder="Снимката: добра ли е? (напр. „размазана“, „лого на медия“, „искам лице“)" style="flex:1">
<button class="btn gray" name=rating value=-1>👎</button><button class=btn name=rating value=1>👍</button></form>{% endif %}
{% if p.id %}<form method=post action="/admin/canva/{{ p.id }}" target=_blank style="margin:10px 0">
<button class="btn red" style="font-size:17px;padding:12px 18px">🎨 Отвори в Canva с нашия шаблон</button>
<span class=muted> заглавие + подзаглавие + снимка → шаблона; останалите снимки от историята отиват в Uploads в Canva</span></form>
{% if p.canva_url %}<p>Последен дизайн: <a href="{{ p.canva_url }}" target=_blank>отвори в Canva</a>
<form method=post action="/admin/canva-pull/{{ p.id }}" style="display:inline"><button class=btn title="След като го оправиш в Canva: всички страници идват тук като PNG">⬇ Вземи готовата визия от Canva</button></form></p>
{% if p.exports %}<div style="display:flex;gap:8px;flex-wrap:wrap">{% for e in p.exports %}<a href="{{ e }}" target=_blank><img src="{{ e }}" style="height:140px;border-radius:6px;border:1px solid #ddd"></a>{% endfor %}</div>{% endif %}{% endif %}
{% if p.export_path %}<img src="{{ p.export_path }}" style="max-width:320px;border-radius:8px">{% endif %}{% endif %}
{% if related %}<h3>Същата история в други източници</h3><table>{% for r in related %}<tr>
<td>{% if r.image %}<img class=thumb src="{{ r.image }}">{% endif %}</td><td><a href="{{ r.link }}" target=_blank>{{ r.title[:140] }}</a>
<div class=muted>{{ r.src }}{% if r.summary and 'Линкове' in r.summary %} · {{ r.summary.split('Линкове: ')[1] }}{% endif %}</div></td>
<td>{% if r.image %}<form method=post action="/admin/useimg/{{ p.id }}"><input type=hidden name=image value="{{ r.image }}"><button class="btn gray">Ползвай снимката</button></form>{% endif %}
{% if 'x.com/' in r.link %}<form method=post action="/admin/useimg/{{ p.id }}"><input type=hidden name=media value="{{ r.link }}"><button class="btn gray">Вгради поста</button></form>{% endif %}</td></tr>{% endfor %}</table>
<p class=muted>Снимките от X/медии са на авторите им. По-безопасно е „Вгради поста“, отколкото да качваш чужда снимка.</p>{% endif %}
{% if p.id %}<form method=post action="/admin/push/{{ p.id }}">
<button class=btn name=to value=canva>→ Canva шаблон</button> <button class=btn name=to value=instagram>→ Instagram</button>
<button class=btn name=to value=tiktok>→ TikTok (чернова)</button>
<button class=btn name=to value=threads>→ Threads</button> <button class=btn name=to value=telegram>→ Telegram</button></form>
<p class=muted>Нищо не отива в социалните мрежи без да натиснеш бутон. Instagram изисква публичен URL на снимката.</p>{% endif %}{% endblock %}""",
"sources": """{% extends 'base' %}{% block c %}<p><a href=/admin>← Назад</a> · <a class=btn href="/admin/test">Тест на източниците</a></p>
<p><b>Филтър:</b> <a href="/admin/sources">всички</a>{% for k,ic in [('работи','✅'),('бавен','⚠️'),('не работи','❌'),('изключен','⏸'),('сигнал','📡')] %} · <a href="/admin/sources?health={{ k|urlencode }}" {% if health==k %}style="font-weight:bold"{% endif %}>{{ ic }} {{ k }}</a>{% endfor %}
<br><span class=muted>✅ работи = нови новини за 48 ч · ⚠️ бавен = има за 7 дни, но не за 48 ч · ❌ не работи = нищо за 7 дни (виж грешката) · ⏸ изключен ръчно · 📡 сигнал = трендове/Wikipedia. Нищо не се трие.</span></p>
<p class=muted>instagram: в URL пишеш потребителското име (нужни ig_user_id + ig_token) · reddit: името на subreddit-а · Език ≠ bg → заглавията се превеждат и има бутон „Международен“.<br>Категориите и цветовете им се редактират в Настройки → source_categories и category_colors. „Мин“ = през колко минути се тегли източникът (0 = общия fetch_every_min).</p>
{% for g, list in groups %}<h3>{{ tag(g) if g else 'Без категория' }} <span class=muted>({{ list|length }})</span></h3><table><tr><th>Работи?</th><th>Име</th><th>Тип</th><th>URL</th><th>Тежест</th><th>Група/собственик</th><th>Наклон −1..+1</th><th>Категория</th><th>Мин</th><th>Език</th><th>Активен</th><th></th></tr>
{% for s in list %}<tr><form method=post action="/admin/sources/{{ s.id }}"><td style="white-space:nowrap" title="{{ s.err }}">{{ {'работи':'✅','бавен':'⚠️','не работи':'❌','изключен':'⏸','сигнал':'📡'}[s.health] }} {{ s.health }}<div class=muted>{{ s.n2 }} за 48 ч · {{ s.n7 }} за 7 дни<br>последна: {{ s.last_s }}</div>{% if s.err %}<div style="color:#d12;font-size:11px">{{ s.err[:80] }}</div>{% endif %}</td><td><input name=name value="{{ s.name }}"><a href="/admin/source/{{ s.id }}" style="font-size:12px">профил →</a></td><td><select name=kind>
{% for k in ['rss','trends','wiki','gdelt','x','instagram','reddit'] %}<option {% if k==s.kind %}selected{% endif %}>{{k}}</option>{% endfor %}</select></td><td><input name=url value="{{ s.url }}"></td>
<td><input name=weight value="{{ s.weight }}" style="width:50px"></td><td><input name=owner value="{{ s.owner or '' }}"></td>
<td><input name=lean value="{{ s.lean or 0 }}" style="width:50px"></td>
<td><select name=category><option value="">—</option>{% for k in cats %}<option {% if k==s.category %}selected{% endif %}>{{k}}</option>{% endfor %}</select></td>
<td><input name=every_min value="{{ (s.every_min or 0)|int }}" style="width:45px"></td><td><input name=lang value="{{ s.lang or 'bg' }}" style="width:42px"></td>
<td><input type=checkbox name=active value=1 {% if s.active %}checked{% endif %}></td><td><button class=btn>✓</button></td></form></tr>{% endfor %}</table>{% endfor %}
<h3>Нов източник</h3><table><tr><form method=post action="/admin/sources/new"><td><input name=name placeholder="Нов източник"></td><td><select name=kind><option>rss</option><option>instagram</option><option>reddit</option><option>trends</option><option>x</option></select></td>
<td><input name=url placeholder="https://... или X заявка"></td><td><input name=weight value=1 style="width:50px"></td><td><input name=owner placeholder="група"></td><td><input name=lean value=0 style="width:50px"></td>
<td><select name=category><option value="">—</option>{% for k in cats %}<option>{{k}}</option>{% endfor %}</select></td><td><input name=every_min value=0 style="width:45px"></td><td><input name=lang value=bg style="width:42px"></td>
<td><input type=checkbox name=active value=1 checked></td><td><button class=btn>+</button></td></form></tr></table>
<p class=muted>Съвет: всеки сайт без RSS → Google News RSS: https://news.google.com/rss/search?q=when:24h+site:ДОМЕЙН&hl=bg&gl=BG&ceid=BG:bg</p>{% endblock %}""",
"trends": """{% extends 'base' %}{% block c %}<p><a href=/admin>← Назад</a></p>
<h2 style="margin-bottom:4px">🧭 Компас — коя новина да хванеш сега <form method=post action="/admin/fetch" style="display:inline"><button class=btn title="Изтегли новините сега и преизчисли">⟳ Обнови</button></form></h2>
<div style="background:#fff;border:1px solid #ddd;border-radius:8px;padding:10px 12px;margin:8px 0;font-size:14px">
<b>Какво значат точките:</b> колко си струва да пуснеш историята <b>сега</b>. Сборът е от причините под всяка история:
🔎 <b>Търсене</b> до 40 (колко хора я търсят в Google днес) · 📰 <b>Големина</b> до 30 (колко медии я отразяват) · 📈 <b>Расте</b> до 25 (колко източника я пуснаха в последните {{ fresh }} ч) ·
🤫 <b>Мълчание</b> до 15 (търсят я, а очаквани медии мълчат) · 🎯 <b>Твоите бележки</b> ±10 · ⏳ <b>Остарява</b> след 6 ч.
<br><b>45+ = пускай сега · 25–44 = следи.</b> Кликни заглавие за всички статии и подробности. <a href="/admin/logic">Логика на рейтинга →</a></div>
<div class=tabs><a href="/admin/trends" {% if not cat %}style="font-weight:bold;text-decoration:underline"{% endif %}>Всички</a>
{% for k in cats %}<a href="/admin/trends?cat={{ k|urlencode }}" style="text-decoration:none"><span class="tag {% if cat and k!=cat %}off{% endif %}" style="background:{{ colors.get(k, '#6b7280') }};color:{{ txt(colors.get(k, '#6b7280')) }}">{{ k }}</span></a>{% endfor %}</div>
{% macro card(k) %}<div class=card style="display:flex;gap:12px;padding:10px;margin-bottom:10px;align-items:flex-start">
{% if k.img %}<img src="{{ k.img }}" style="width:150px;height:100px;object-fit:cover;border-radius:8px;flex:none" loading=lazy onerror="this.style.display='none'">{% endif %}
<div style="flex:1;min-width:0"><div style="display:flex;justify-content:space-between;gap:8px"><a href="/admin/story/{{ k.item }}" style="font-weight:700;font-size:16px;color:inherit" title="Отвори историята: всички статии и защо има тези точки">{{ k.title }}</a>
<span style="font:800 22px system-ui;color:{{ '#d12' if k.total>=45 else '#111' }}">{{ k.total }}</span></div>
<div class=muted>{{ tag(k.cat) }} · преди {{ k.ago }} · {{ k.n }} статии: {{ k.srcs[:5]|join(', ') }}{% if k.srcs|length>5 %} +{{ k.srcs|length-5 }}{% endif %}</div>
<ul style="margin:6px 0;padding-left:0;list-style:none;font-size:14px">{% for ic,t,p in k.pts %}<li>{{ ic }} {{ t }} <b style="color:{{ '#0a0' if p>0 else '#d12' }}">{{ '%+d' % p }}</b></li>{% endfor %}
{% if k.nmiss %}<li class=muted>Мълчат: {{ media_links(k.miss, k.title, k.item) }}{% if k.nmiss>6 %} и още {{ k.nmiss-6 }}{% endif %}</li>{% endif %}</ul>
{% if k.level!='done' %}<form method=post action="/admin/quick/{{ k.item }}" style="display:inline"><button class="btn red">⚡ Бърз пост</button></form>
<form method=post action="/admin/verdict/{{ k.item }}/would" style="display:inline"><button class="btn gray" title="Бихме я покрили, но не сега">👍 Бихме</button></form>
<form method=post action="/admin/skip/{{ k.item }}" style="display:inline"><button class="btn gray" title="Скрий историята — ще виждаш по-малко такива">👎 Не бихме</button></form>{% else %}<span class=muted>✅ Вече сме я пуснали</span>{% endif %}
</div></div>{% endmacro %}
<h3>🔥 Хвани сега ({{ g.now|length }})</h3>{% for k in g.now %}{{ card(k) }}{% else %}<p class=muted>Нищо горещо в момента.</p>{% endfor %}
<h3>👀 Следи ({{ g.watch|length }})</h3>{% for k in g.watch[:15] %}{{ card(k) }}{% else %}<p class=muted>—</p>{% endfor %}
{% if gaps %}<h3>🕳 Хората търсят, а никой не пише</h3><p class=muted>Трендове в Google без нито една статия при нас — шанс да си първи. Провери дали е новина.</p>
<p>{% for t in gaps %}<a class=tag style="background:#eef;color:#113;margin:3px" href="/admin/trend?term={{ t.term|urlencode }}">{{ t.term }} · {{ t.raw }}</a>{% endfor %}</p>{% endif %}
<details><summary>Слаби ({{ g.low|length }}) и вече пуснати ({{ g.done|length }})</summary>{% for k in g.done %}{{ card(k) }}{% endfor %}{% for k in g.low[:20] %}{{ card(k) }}{% endfor %}</details>
<details><summary>Всички Google трендове (кликни за новините по тях)</summary><table>{% for t in raw %}<tr><td><a href="/admin/trend?term={{ t.term|urlencode }}">{{ t.term }}</a></td><td>{{ t.traffic }}</td></tr>{% endfor %}</table></details>
{% endblock %}""",
"logic": """{% extends 'base' %}{% block c %}<p><a href=/admin>← Назад</a></p>
<h2>🧠 Логика на рейтинга</h2>
<p>Всяка новина получава оценка 0–100. Над <b>{{ thr }}</b> е „Препоръчана“. Оценката е сбор от тези части (тежести в % — сменят се в <a href="/admin/settings">Настройки</a>), умножен по тежестта на източника и по наученото от теб:</p>
<table><tr><th>Част</th><th>Тежест</th><th>Какво мери</th></tr>{% for k,v,d in W %}<tr><td><b>w_{{ k }}</b></td><td>{{ v }}%</td><td>{{ d }}</td></tr>{% endfor %}</table>
<p class=muted>Учи се от {{ nsig }} твои действия (📝 бележки, ⚡ Бърз пост, → Чернова, Публикувай, ✕). Колкото повече бележки, толкова по-точно.</p>
<div style="display:flex;gap:16px;flex-wrap:wrap"><div style="flex:1;min-width:300px">
<h3>Думи, които харесваш</h3><table>{% for w,(v,p,n) in wplus if v>0 %}<tr><td>{{ w }}…</td><td style="color:#0a0">+{{ '%.1f' % v }}</td><td class=muted>{{ p }} за / {{ n }} против</td></tr>{% else %}<tr><td class=muted>Още няма</td></tr>{% endfor %}</table></div>
<div style="flex:1;min-width:300px"><h3>Думи, които отхвърляш</h3><table>{% for w,(v,p,n) in wminus if v<0 %}<tr><td>{{ w }}…</td><td style="color:#d12">{{ '%.1f' % v }}</td><td class=muted>{{ p }} за / {{ n }} против</td></tr>{% else %}<tr><td class=muted>Още няма</td></tr>{% endfor %}</table></div></div>
<div style="display:flex;gap:16px;flex-wrap:wrap"><div style="flex:1;min-width:300px"><h3>Категории</h3><table>{% for c,v in lcat %}<tr><td>{{ c }}</td><td><b>×{{ '%.2f' % v }}</b></td></tr>{% else %}<tr><td class=muted>Още няма</td></tr>{% endfor %}</table></div>
<div style="flex:1;min-width:300px"><h3>Източници</h3><table>{% for n,v in lsrc %}<tr><td>{{ n }}</td><td><b>×{{ '%.2f' % v }}</b></td></tr>{% else %}<tr><td class=muted>Още няма</td></tr>{% endfor %}</table></div></div>
<h3>Причини в бележките</h3><p>{% for r,n in reasons %}<span class=tag style="background:#eee;margin:2px">{{ r }} · {{ n }}</span>{% else %}<span class=muted>Още няма</span>{% endfor %}</p>
<h3>Последни бележки</h3><table>{% for n in notes %}<tr><td class=muted style="width:100px">{{ n.when }}</td><td>{{ {'would':'👍','wouldnt':'👎'}.get(n.verdict,'📝') }} <a href="{{ n.link }}" target=_blank>{{ (n.title_bg or n.title or '')[:90] }}</a>
<div class=muted>{{ n.reasons }}{% if n.text %} — {{ n.text }}{% endif %}</div></td></tr>{% else %}<tr><td class=muted>Натисни 📝 до която и да е новина.</td></tr>{% endfor %}</table>{% endblock %}""",
"source": """{% extends 'base' %}{% block c %}<p><a href=/admin/sources>← Източници</a>{% if s.owner %} · група: <a href="/admin/media/{{ s.owner|urlencode }}">{{ s.owner }}</a>{% endif %}</p>
<h2 style="margin-bottom:2px">{{ s.name }} {% if not s.active %}<span class=muted>(изключен)</span>{% endif %}</h2>
<div class=muted>{{ s.kind }} · {{ tag(s.category) if s.category else 'без категория' }} · език {{ s.lang }} · тежест {{ s.weight }} · <a href="{{ s.url }}" target=_blank>адрес</a></div>
<div style="display:flex;gap:16px;flex-wrap:wrap;margin-top:12px"><div style="flex:1;min-width:300px">
<h3>Какво показват данните</h3><table>
<tr><td>Новини за 7 / 30 дни</td><td><b>{{ st.n7 }}</b> / {{ st.n30 }}</td></tr>
<tr><td>Препоръчани (30 дни)</td><td><b>{{ st.rec }}</b>{% if st.n30 %} ({{ '%.0f' % (100*st.rec/st.n30) }}%){% endif %}</td></tr>
<tr><td>Средна оценка</td><td>{{ st.avg or 0 }}</td></tr><tr><td>Със снимка</td><td>{{ (st.img or 0)|int }}%</td></tr>
<tr><td>Наши постове от него</td><td>{{ st.posts }}</td></tr>
<tr><td>Твоите избори</td><td>👍 {{ (sig.get('would',0) + sig.get('draft',0) + sig.get('quick',0) + sig.get('publish',0)) }} · 👎 {{ sig.get('wouldnt',0) + sig.get('reject',0) }} → множител <b>×{{ '%.2f' % lw }}</b></td></tr></table>
<h3>📝 Бележки за източника</h3><form method=post action="/admin/source/{{ s.id }}/note"><textarea name=text style="min-height:60px" placeholder="Напр. „Бавни, но точни“, „Често PR материали“, „Добри снимки“"></textarea>
<select name=kind style="width:170px;margin-top:6px"><option value=note>Бележка</option><option value=strange>🔍 Странно</option><option value=good>👍 Силна страна</option><option value=bias>⚖ Наклон</option></select> <button class=btn>Запиши</button></form>
<table>{% for n in notes %}<tr><td class=muted style="width:100px">{{ n.when }}</td><td>{{ {'strange':'🔍','good':'👍','bias':'⚖'}.get(n.kind,'📝') }} {{ n.text }}</td></tr>{% endfor %}</table>
{% if inotes %}<h3>Бележки по негови новини</h3><table>{% for n in inotes %}<tr><td>{{ {'would':'👍','wouldnt':'👎'}.get(n.verdict,'📝') }} <a href="{{ n.link }}" target=_blank>{{ n.title[:80] }}</a><div class=muted>{{ n.reasons }} {{ n.text }}</div></td></tr>{% endfor %}</table>{% endif %}
</div><div style="flex:1;min-width:300px"><h3>Последни новини</h3><table>{% for r in recent %}<tr><td class=muted style="width:90px">{{ r.when }}</td><td><a href="{{ r.link }}" target=_blank>{{ r.title_bg or r.title }}</a><div class=muted>{{ r.score }} · {{ r.status }}</div></td><td>{{ note_form(r.id, r.verdict) }}</td></tr>{% endfor %}</table></div></div>{% endblock %}""",
"story": """{% extends 'base' %}{% block c %}<p><a href=/admin/trends>← Компас</a></p>
<div style="display:flex;gap:14px;align-items:flex-start">{% if k.img %}<img src="{{ k.img }}" style="width:240px;height:160px;object-fit:cover;border-radius:10px" onerror="this.style.display='none'">{% endif %}
<div><h2 style="margin:0">{{ k.title }}</h2><div class=muted>{{ tag(k.cat) }} · първа статия преди {{ k.ago }} · {{ k.n }} статии от {{ k.srcs|length }} източника</div>
<div style="font:800 34px system-ui;color:{{ '#d12' if k.total>=45 else '#111' }}">{{ k.total }} <span style="font:400 15px system-ui" class=muted>{{ 'пускай сега' if k.total>=45 else 'следи' if k.total>=25 else 'слаба' }}</span></div>
<form method=post action="/admin/quick/{{ k.item }}" style="display:inline"><button class="btn red">⚡ Бърз пост</button></form> {{ note_form(k.item) }}
<a class=btn href="https://news.google.com/search?q={{ k.title|urlencode }}&hl=bg" target=_blank>Google News</a></div></div>
<h3>Защо {{ k.total }} точки</h3><table>{% for ic,t,p in k.pts %}<tr><td style="width:30px">{{ ic }}</td><td>{{ t }}</td><td style="width:60px"><b style="color:{{ '#0a0' if p>0 else '#d12' }}">{{ '%+d' % p }}</b></td></tr>{% endfor %}
<tr><td></td><td><b>Общо</b></td><td><b>{{ k.total }}</b></td></tr></table>
<p>🤫 Мълчат: {% for o in k.miss_all %}<span style="white-space:nowrap">{{ media_links([o], k.title, k.item) }}<form method=post action="/admin/silence-fix/{{ k.item }}" style="display:inline"><input type=hidden name=owner value="{{ o }}"><button name=action value=not_silent title="Не е мълчание — махни" style="border:0;background:none;cursor:pointer;color:#999">✕</button></form></span>{% if not loop.last %}, {% endif %}{% else %}<span class=muted>никой</span>{% endfor %}</p>
<form method=post action="/admin/silence-fix/{{ k.item }}" style="display:flex;gap:6px"><input name=owner list=groups placeholder="Медия, която трябваше да отрази" style="width:260px"><button class="btn gray" name=action value=should_cover>+ Добави към мълчащите</button></form>
<datalist id=groups>{% for g in all_groups %}<option value="{{ g }}">{% endfor %}</datalist>
<h3>Всички статии (по време)</h3><table>{% for a in arts %}<tr><td class=muted style="width:100px">{{ a.when }}</td><td><a href="{{ a.link }}" target=_blank>{{ a.title_bg or a.title }}</a><div>{{ src_tag(a.src, a.cat, a.sid) }}</div></td><td>{{ note_form(a.id, a.verdict) }}</td></tr>{% endfor %}</table>{% endblock %}""",
"trend": """{% extends 'base' %}{% block c %}<p><a href=/admin/trends>← Компас</a></p>
<h2>🔎 „{{ term }}“ {% if traffic %}<span class=muted>· {{ traffic }} търсения в Google</span>{% endif %}</h2>
<p><a class=btn href="https://news.google.com/search?q={{ term|urlencode }}&hl=bg" target=_blank>Google News</a> <a class=btn href="https://trends.google.com/trends/explore?geo=BG&q={{ term|urlencode }}" target=_blank>Google Trends</a></p>
<h3>Наши новини за това ({{ hits|length }})</h3><table>{% for r in hits %}<tr><td class=muted style="width:100px">{{ r.when }}</td><td><a href="/admin/story/{{ r.id }}">{{ r.title_bg or r.title }}</a><div>{{ src_tag(r.src, r.cat, r.sid) }} <span class=muted>{{ r.score }}</span></div></td>
<td style="white-space:nowrap"><form method=post action="/admin/quick/{{ r.id }}" style="display:inline"><button class="btn red">⚡</button></form> {{ note_form(r.id, r.verdict) }}</td></tr>
{% else %}<tr><td>Нямаме новина за това. Ако е новина — намери статия и я добави от <a href="/admin/missed">🕳 Пропусната новина</a>.</td></tr>{% endfor %}</table>{% endblock %}""",
"instagram": """{% extends 'base' %}{% block c %}<p><a href=/admin>← Назад</a></p>
<h2>📸 Instagram — следене на други профили</h2>
<p>Статус: {% if uid and kind %}<b>@{{ uname }}</b> · {{ kind }}{% if exp %} · токенът изтича на <b>{{ exp }}</b>{% endif %}{% else %}<b style="color:#d12">не е свързан</b>{% endif %}</p>
<details {% if not uid %}open{% endif %} style="background:#fff;border:1px solid #ddd;border-radius:8px;padding:10px 14px"><summary><b>Свързване (веднъж, ~10 мин)</b></summary>
<p class=muted>Meta позволява да четеш чужди профили само през Facebook Login и само ако са <b>Business или Creator</b> (личните профили не се виждат). Твоят профил също трябва да е Business/Creator и вързан за Facebook страница.</p>
<ol style="line-height:1.7">
<li>В Instagram: Settings → Account type and tools → <b>Switch to professional account</b> → свържи с Facebook страницата на Обществото.</li>
<li>Отвори <a href="https://developers.facebook.com/apps" target=_blank>developers.facebook.com/apps</a> → <b>Create app</b> → тип <b>Business</b>. Добави продукт <b>Instagram → API setup with Facebook login</b>.</li>
<li>От <b>App settings → Basic</b> копирай App ID и App secret (за 60-дневен токен).</li>
<li>Отвори <a href="https://developers.facebook.com/tools/explorer/" target=_blank>Graph API Explorer</a>, избери приложението, натисни <b>Generate Access Token</b> и разреши: <code>instagram_basic, instagram_manage_insights, pages_show_list, pages_read_engagement, business_management</code> (+ <code>instagram_content_publish</code>, ако ще публикуваме оттук).</li>
<li>Копирай токена и го постави тук — приложението само ще намери профила ти:
<form method=post action="/admin/instagram/connect" style="display:flex;gap:8px;flex-wrap:wrap;margin:8px 0">
<input name=app_id placeholder="App ID" value="{{ app_id }}" style="width:180px"><input name=app_secret type=password placeholder="{{ 'вече е въведен' if has_secret else 'App secret' }}" style="width:200px">
<input name=token placeholder="Access token от Graph API Explorer" style="flex:1;min-width:260px" required><button class="btn red">Свържи</button></form></li></ol>
<p class=muted>Токенът е за 60 дни — 1 седмица преди датата повтори стъпка 4–5. За собствен профил на Обществото не е нужен App Review.</p></details>
<h3>Добави профили за следене</h3>
<form method=post action="/admin/instagram/add" style="display:flex;gap:8px;flex-wrap:wrap;align-items:flex-start">
<textarea name=handles placeholder="по един на ред: btvnovinite, novinitenanova, @lupa.bg, https://instagram.com/deuxmoi" style="flex:2;min-width:260px;min-height:90px" required></textarea>
<div style="flex:1;min-width:200px"><select name=category>{% for k in cats %}<option>{{ k }}</option>{% endfor %}</select>
<input name=owner placeholder="Медийна група (по желание)" style="margin-top:6px"><button class=btn style="margin-top:6px">+ Добави</button></div></form>
<p class=muted>Всеки профил се тегли на {{ every }} мин (настройка <code>ig_every_min</code>; лимитът на Meta е около 200 заявки на час). Реакциите (❤ и 💬) се обновяват при всяко теглене — пост с 2× повече реакции от обичайното за профила дава до +25 т. в Компаса („💬 Гърми в Instagram“).</p>
<form method=post action="/admin/instagram/fetch"><button class=btn>⟳ Изтегли всички сега</button></form>
<h3>Следени профили ({{ rows|length }})</h3><table><tr><th>Профил</th><th>Категория</th><th>Постове 7 дни</th><th>Обичайни реакции</th><th></th></tr>
{% for r in rows %}<tr><td><a href="https://instagram.com/{{ r.url }}" target=_blank>@{{ r.url }}</a>{% if not r.active %} <span class=muted>(изкл.)</span>{% endif %}{% if r.err %}<div style="color:#d12;font-size:12px">{{ r.err[:140] }}</div>{% endif %}</td>
<td>{{ tag(r.category) if r.category else '' }}</td><td>{{ r.n7 }}</td><td>{{ r.med or '—' }}</td><td><a href="/admin/source/{{ r.id }}">профил →</a></td></tr>
{% else %}<tr><td class=muted>Още няма. Добави горе.</td></tr>{% endfor %}</table>{% endblock %}""",
"canva_setup": """{% extends 'base' %}{% block c %}<p><a href=/admin>← Назад</a></p>
<h2>🎨 Свържи Обществото с Canva</h2>
<p>Статус: {% if connected %}<b style="color:#0a0">свързан ✓</b>{% elif cid and has_secret %}<b>ключовете са въведени — натисни „Свържи Canva“</b>{% else %}<b style="color:#d12">не е свързан</b>{% endif %}
{% if tpl %} · шаблон: <code>{{ tpl }}</code>{% endif %}</p>
<p class=muted>Нужен е Canva Pro, Teams или Enterprise (за попълване на шаблони). Прави се веднъж.</p>
<ol style="line-height:1.7">
<li>Отвори <a href="https://www.canva.com/developers/apps" target=_blank>canva.com/developers → Your apps</a> и натисни <b>Create an app</b>. Избери да ползва <b>Connect API / REST APIs</b>.</li>
<li>В <b>Authentication</b> добави този Redirect URL точно така: <code>{{ redirect }}</code></li>
<li>В <b>Scopes</b> отметни: <code>{{ scopes }}</code></li>
<li>Копирай <b>Client ID</b> и генерирай <b>Client secret</b> и ги постави тук:
<form method=post action="/admin/canva/setup" style="display:flex;gap:8px;flex-wrap:wrap;margin:8px 0"><input name=client_id placeholder="Client ID" value="{{ cid }}" style="width:260px">
<input name=client_secret type=password placeholder="{{ 'вече е въведен — остави празно' if has_secret else 'Client secret' }}" style="width:260px"><button class=btn>Запази</button></form></li>
<li><a class="btn red" href="/admin/canva/connect">Свържи Canva</a> → влез с профила на Обществото и натисни <b>Allow</b>.</li>
<li>В Canva направи шаблон (4:5), с текстово поле за заглавие, текстово за подзаглавие и рамка за снимка. Публикувай го като <b>Brand template</b> и задай <b>Data autofill</b> полета с имена <code>title</code>, <code>subtitle</code>, <code>image</code>.</li>
<li><a class=btn href="/admin/canva/templates">Избери шаблон</a> — после бутонът 🎨 в черновата ще го попълва.</li></ol>{% endblock %}""",
"map": """{% extends 'base' %}{% block c %}<p><a href=/admin>← Назад</a> · CSV за Sheet: <a href="/admin/map.csv?part=media">медии</a> · <a href="/admin/map.csv?part=deps">зависимости</a> · <a href="/admin/map.csv?part=relations">връзки</a> · <a href="/admin/map.csv?part=promises">обещания</a></p>
<h2>🗺 Медийна карта</h2>
<p class=muted>{{ p.n_big }} големи истории (≥3 медийни групи) за {{ p.days }} дни. <b>Важност</b> е балансирана: 35% оригиналност (първа с 10+ мин), 25% влияние (колко групи я следват), 25% обхват, 15% скорост. Така разследваща медия с малко, но първи материали не губи от голяма медия, която препечатва. Ролята се сменя ръчно от профила на медията. Времената от Google News може да закъсняват.</p>
<div style="display:flex;flex-direction:column;align-items:center;gap:6px;margin:14px 0">
{% for t in tiers %}{% if levels[t] %}<div style="width:{{ [40 + loop.index0*10, 100]|min }}%;background:#fff;border:1px solid #ddd;border-radius:8px;padding:8px 10px">
<b>{{ t|capitalize }}</b> <span class=muted>({{ levels[t]|length }})</span><br>{% for m in levels[t] %}<a href="/admin/media/{{ m.owner|urlencode }}" class="tag" style="background:#eef;color:#113;margin:2px" title="важност {{ m.score }} · първа {{ m.first }}/{{ m.cov }}">{{ m.owner }} · {{ m.score }}</a>{% endfor %}</div>{% endif %}{% endfor %}</div>
<h3>Медии</h3><table><tr><th>Група</th><th>Роля</th><th>Важност</th><th>Големи истории</th><th>Първа</th><th>Влияние</th><th>Закъснение</th></tr>
{% for m in p.media %}<tr><td><a href="/admin/media/{{ m.owner|urlencode }}">{{ m.owner }}</a></td><td>{{ m.tier }}{% if m.manual %} ✋{% endif %}</td><td><b>{{ m.score }}</b></td><td>{{ m.cov }}</td>
<td>{{ m.first }} ({{ '%.0f' % (m.orig*100) }}%)</td><td>{{ '%+d' % m.infl }}</td><td>{% if m.lag is not none %}{{ '%.0f' % m.lag }} мин{% endif %}</td></tr>{% endfor %}</table>
<h3>Зависимости: кой пуска, кой следва</h3><p class=muted>A → B = B пуска същата история 10 мин – 6 ч след A, поне 5 пъти и поне 2× по-често, отколкото обратно.</p>
<table><tr><th>Пуска първа</th><th></th><th>Следва я</th><th>Пъти</th><th>Обратно</th></tr>{% for a,b,n,bk in p.deps %}<tr><td>{{ a }}</td><td>→</td><td>{{ b }}</td><td>{{ n }}</td><td class=muted>{{ bk }}</td></tr>
{% else %}<tr><td colspan=5 class=muted>Още малко данни.</td></tr>{% endfor %}</table>
<h3 id=rel>Собственост, пари и интереси {% if who %}<span class=muted>— филтър „{{ who }}“ · <a href="/admin/map#rel">всички</a></span>{% endif %}</h3>
<p class=muted>Само връзки с публичен източник. Стари данни са отбелязани — проверявайте в Търговския регистър и регистъра на Министерството на културата.</p>
<table><tr><th>Кой</th><th>Връзка</th><th>С кого / какво</th><th>Бележка</th><th>Източник</th></tr>{% for r in rels %}<tr><td><a href="/admin/map?who={{ r.subj|urlencode }}#rel">{{ r.subj }}</a></td><td>{{ r.rel }}</td><td>{{ r.obj }}</td><td class=muted>{{ r.note }}</td>
<td><a href="{{ r.source_url }}" target=_blank>линк</a> <span class=muted>{{ r.source_date }} · {{ r.confidence }}</span></td></tr>{% endfor %}</table>
<form method=post action="/admin/map/relation" style="display:flex;gap:6px;flex-wrap:wrap;margin-top:8px"><input name=subj placeholder="Кой (лице/фирма/партия)" style="width:180px" required>
<select name=rel style="width:170px">{% for x in ['притежава','действителен собственик на','мажоритарен собственик на','финансира','рекламира в','друг бизнес','подкрепя публично','атакува публично','свързан с','издател на'] %}<option>{{ x }}</option>{% endfor %}</select>
<input name=obj placeholder="С кого / какво" style="width:180px" required><input name=note placeholder="Бележка" style="width:200px"><input name=source_url placeholder="Източник (линк) — задължителен" style="width:220px" required>
<input name=source_date placeholder="Дата" style="width:90px"><select name=confidence style="width:130px"><option>потвърдено</option><option>по медии</option><option>за проверка</option></select><button class=btn>+ Връзка</button></form>
<h3 id=check>🤖 За проверка — извадено от Claude от новините</h3>
<p class=muted>Claude чете българските новини (категории в Настройки → extract_categories) и вади само изрично казани обещания и позиции. Нищо не влиза в картата, докато не натиснеш ✓.
<form method=post action="/admin/map/extract" style="display:inline"><button class="btn red" {% if not ready %}disabled title="Claude не е свързан"{% endif %}>🤖 Прочети новите новини</button></form></p>
{% if chk_p %}<b>Обещания ({{ chk_p|length }})</b><table>{% for r in chk_p %}<tr><td><b>{{ r.who }}</b>: {{ r.what }}{% if r.deadline %} <span class=muted>(срок: {{ r.deadline }})</span>{% endif %}
<div class=muted>{{ r.media }} · {{ r.said_on }} · <a href="{{ r.source_url }}" target=_blank>статията</a></div></td>
<td style="white-space:nowrap"><form method=post action="/admin/map/check/promise/{{ r.id }}/ok" style="display:inline"><button class=btn>✓</button></form>
<form method=post action="/admin/map/check/promise/{{ r.id }}/no" style="display:inline"><button class="btn gray">✕</button></form></td></tr>{% endfor %}</table>{% endif %}
{% if chk_r %}<b>Позиции ({{ chk_r|length }})</b><table>{% for r in chk_r %}<tr><td><b>{{ r.subj }}</b> {{ 'подкрепя' if r.rel=='подкрепя публично' else 'атакува' }} <b>{{ r.obj }}</b>
<div class=muted>{{ r.note }} · <a href="{{ r.source_url }}" target=_blank>статията</a></div></td>
<td style="white-space:nowrap"><form method=post action="/admin/map/check/relation/{{ r.id }}/ok" style="display:inline"><button class=btn>✓</button></form>
<form method=post action="/admin/map/check/relation/{{ r.id }}/no" style="display:inline"><button class="btn gray">✕</button></form></td></tr>{% endfor %}</table>{% endif %}
{% if not chk_p and not chk_r %}<p class=muted>Няма чакащи. Натисни бутона горе.</p>{% endif %}
<h3>Кой кого промотира (автоматично, 30 дни)</h3>
<p class=muted>За всяка медийна група: за кого нейните новини съобщават позиции „за“ (+) и „против“ (−), и кои имена споменава най-често. Показва посока на отразяването, не доказва намерение. Нужни са поне 3 случая.</p>
<table><tr><th>Група</th><th>Най-често споменава</th><th>Позиции в новините ѝ (+ / −)</th></tr>{% for o in (most.keys()|list + prom_by.keys()|list)|unique %}<tr><td><a href="/admin/media/{{ o|urlencode }}">{{ o }}</a></td>
<td>{% for a,n in most.get(o,[]) %}{{ a }} ({{ n }}){% if not loop.last %}, {% endif %}{% endfor %}</td>
<td>{% for t,pl,mi in prom_by.get(o,[])[:6] %}{{ t }} <b style="color:#0a0">+{{ pl }}</b>/<b style="color:#d12">−{{ mi }}</b>{% if not loop.last %} · {% endif %}{% endfor %}</td></tr>
{% else %}<tr><td colspan=3 class=muted>Още няма данни — натисни „🤖 Прочети новите новини“.</td></tr>{% endfor %}</table>
<h3 id=prom>Обещания</h3><p class=muted>Кой какво е обещал, кога и до кога. Статусът се сменя, когато срокът мине.</p>
<table><tr><th>Кой</th><th>Обещание</th><th>Казано</th><th>Срок</th><th>Статус</th><th>Източник</th></tr>{% for r in proms %}<tr><td>{{ r.who }}</td><td>{{ r.what }}</td><td>{{ r.said_on }}</td><td>{{ r.deadline }}</td>
<td><form method=post action="/admin/map/promise" style="display:flex;gap:4px"><input type=hidden name=pid value="{{ r.id }}"><input type=hidden name=who value="x"><input type=hidden name=what value="x">
<select name=status style="width:120px" onchange="this.form.submit()">{% for x in ['чака','изпълнено','частично','неизпълнено','отказ'] %}<option {% if r.status==x %}selected{% endif %}>{{ x }}</option>{% endfor %}</select></form></td>
<td><a href="{{ r.source_url }}" target=_blank>линк</a> <span class=muted>{{ r.media }}</span></td></tr>{% else %}<tr><td colspan=6 class=muted>Още няма. Добавяй, докато четете новините.</td></tr>{% endfor %}</table>
<form method=post action="/admin/map/promise" style="display:flex;gap:6px;flex-wrap:wrap;margin-top:8px"><input name=who placeholder="Кой" style="width:160px" required><input name=what placeholder="Какво е обещал" style="width:260px" required>
<input name=said_on placeholder="Казано на (дата)" style="width:130px"><input name=deadline placeholder="Срок" style="width:100px"><input name=media placeholder="Медия" style="width:120px"><input name=source_url placeholder="Източник (линк)" style="width:200px" required><button class=btn>+ Обещание</button></form>{% endblock %}""",
"media_list": """{% extends 'base' %}{% block c %}<p><a href=/admin>← Назад</a></p>
<h2>📇 Медии</h2><p class=muted>Профил на всяка медия, която следим: обхват (какво се очаква да отразява), какво показват данните и вашите бележки. Бележки оставяш от профила или като цъкнеш на името на медията навсякъде в приложението.</p>
<table><tr><th>Медия</th><th>Обхват</th><th>Статии 30 дни</th><th>Бележки</th></tr>{% for r in rows %}<tr>
<td><a href="/admin/media/{{ r.owner|urlencode }}"><b>{{ r.owner }}</b></a><div class=muted>{{ r.nsrc }} източника · {{ r.cats }}</div></td>
<td>{% if r.topics %}{{ r.topics }}{% else %}<span class=muted>от данните</span>{% endif %}</td><td>{{ r.n30 }}</td>
<td>{{ r.nnotes }}{% if r.last %}<div class=muted>{{ r.last[:80] }}</div>{% endif %}</td></tr>{% endfor %}</table>{% endblock %}""",
"media": """{% extends 'base' %}{% block c %}<p><a href=/admin/media>← Всички медии</a></p>
<h2 style="margin-bottom:2px">{{ owner }}</h2><div class=muted>{% for s in srcs %}{{ s.name }}{% if not s.active %} (изкл.){% endif %}{% if not loop.last %} · {% endif %}{% endfor %}</div>
<div style="display:flex;gap:16px;flex-wrap:wrap;margin-top:12px"><div style="flex:1;min-width:320px">
<h3>📝 Бележки ({{ notes|length }})</h3>
<form method=post action="/admin/media-note" style="margin-bottom:10px"><input type=hidden name=owner value="{{ owner }}"><input type=hidden name=item_id value="{{ item }}">
<textarea name=text style="min-height:70px" placeholder="Напр. „Винаги първи за Бургас“, „Не пише за прокуратурата“, „Сменят заглавията след 1 час“">{{ note }}</textarea>
<select name=kind style="width:170px;margin-top:6px"><option value=note>Бележка</option><option value=strange {% if note %}selected{% endif %}>🔍 Странно</option><option value=good>👍 Силна страна</option><option value=bias>⚖ Наклон / рамкиране</option></select>
<button class=btn>Запиши</button></form>
<table>{% for n in notes %}<tr><td class=muted style="width:100px">{{ n.when }}</td><td>{{ {'strange':'🔍','good':'👍','bias':'⚖'}.get(n.kind,'📝') }} {{ n.text }}{% if n.title %}<div class=muted><a href="{{ n.link }}" target=_blank>{{ n.title[:90] }}</a></div>{% endif %}</td></tr>
{% else %}<tr><td class=muted>Още няма бележки.</td></tr>{% endfor %}</table></div>
<div style="flex:1;min-width:320px"><h3>Обхват — какво се очаква да отразява</h3>
<form method=post action="/admin/media/{{ owner|urlencode }}"><p>Регион: <select name=region style="width:200px">{% for v,l in [('','—'),('burgas','Бургас и региона'),('bg','България'),('world','Свят / чужда')] %}<option value="{{ v }}" {% if prof.region==v %}selected{% endif %}>{{ l }}</option>{% endfor %}</select></p>
<p>Теми (ако отметнеш, „мълчи“ се брои само по тях; празно = по данните):<br>{% for k in cats %}<label style="display:inline-block;margin:2px 10px 2px 0;white-space:nowrap"><input type=checkbox name=topics value="{{ k }}" style="width:auto" {% if k in (prof.topics or '').split(',') %}checked{% endif %}> {{ k }}</label>{% endfor %}</p>
<p>Роля в пирамидата: <select name=tier style="width:200px"><option value="">— от данните</option>{% for t in ['агенция','разследваща','пробив','усилвател','последовател','нишова','чужда'] %}<option {% if prof.tier==t %}selected{% endif %}>{{ t }}</option>{% endfor %}</select></p>
<textarea name=about style="min-height:60px" placeholder="Кратко описание: собственик, тон, за какво е силна">{{ prof.about or '' }}</textarea><br><button class=btn>Запази профила</button></form>
{% if fixes %}<h3>Твоите корекции на мълчанието</h3><table>{% for cat,act,n in fixes %}<tr><td>{{ cat }}</td><td>{{ 'не се очаква да отразява' if act=='not_silent' else 'трябва да отразява' }}</td><td>{{ n }} пъти</td></tr>{% endfor %}</table>
<p class=muted>След {{ fix_min }} корекции за една категория обхватът на медията се сменя автоматично.</p>{% endif %}
<h3>Собственост и интереси</h3><p><a href="/admin/map?who={{ (owner_hint or owner)|urlencode }}#rel">Виж връзките в Медийната карта →</a></p>
<h3>Какво показват данните (30 дни)</h3>
<p>Отразила {{ st.covered }} от големите истории (≥3 медийни групи) · първа в {{ st.first }}{% if st.median_delay is not none %} · обикновено {{ '%.0f' % st.median_delay }} мин след първата медия{% endif %}</p>
<table><tr><th>Категория</th><th>Отразени</th><th>Дял</th></tr>{% for cat,k,n,r in st.cats %}<tr><td>{{ cat }}</td><td>{{ k }} от {{ n }}</td><td><b style="color:{{ '#0a0' if r>=0.6 else '#d12' if r<0.2 else '#111' }}">{{ '%.0f' % (r*100) }}%</b></td></tr>
{% else %}<tr><td colspan=3 class=muted>Още малко данни.</td></tr>{% endfor %}</table>
<p class=muted>Над 60% = странно е, ако мълчи. Под 20% = странно е, ако пише.</p>
<h3>Последни статии</h3><table>{% for r in recent %}<tr><td class=muted style="width:90px">{{ r.when }}</td><td><a href="{{ r.link }}" target=_blank>{{ r.title_bg or r.title }}</a> {{ tag(r.cat) }}</td>
<td><a class=muted href="/admin/media/{{ owner|urlencode }}?item={{ r.id }}&note=">📝</a></td></tr>{% endfor %}</table></div></div>{% endblock %}""",
"missed": """{% extends 'base' %}{% block c %}<p><a href=/admin>← Назад</a></p>
<h2>🕳 Пропусната новина</h2>
<p class=muted>Намери статия, която бихме покрили, но системата не я показа? Постави линка. Ще видиш защо я нямаме, кои медии я покриват и с един бутон ще добавиш липсващия източник. Тази седмица: <b>{{ week }}</b> пропуснати.</p>
<form method=post action="/admin/missed" style="display:flex;gap:8px;flex-wrap:wrap"><input name=url placeholder="https://..." style="flex:2;min-width:260px" required>
<input name=note placeholder="Защо е важна? (по желание)" style="flex:1;min-width:180px"><button class="btn red">Анализирай</button></form>
{% if cur %}{% set d = cur.d %}<div class=card style="padding:14px;margin-top:14px">
<div style="display:flex;gap:12px">{% if d.image %}<img src="{{ d.image }}" style="width:140px;height:95px;object-fit:cover;border-radius:8px" onerror="this.style.display='none'">{% endif %}
<div><a href="{{ d.url }}" target=_blank style="font-weight:700;font-size:16px">{{ d.title or d.url }}</a><div class=muted>{{ d.site }} · {{ d.domain }}</div>
<p style="margin:8px 0"><b>Защо я нямаме:</b> {{ d.why }}</p></div></div>
<form method=post action="/admin/missed/{{ cur.id }}/draft" style="margin:6px 0"><button class="btn red">⚡ Направи пост сега</button></form>
<h3 style="margin-bottom:4px">Кои медии я покриват ({{ d.covered_by|length }})</h3>
<table><tr><th>Медия</th><th>Тяхното заглавие</th><th>При нас</th><th></th></tr>
{% for x in d.covered_by %}<tr><td><b>{{ x.name }}</b><div class=muted>{{ x.domain }}</div></td><td>{{ x.title }}</td>
<td>{% if x.have and x.have.active %}✅ {{ x.have.name }}{% elif x.have %}⏸ изключен ({{ x.have.name }}){% else %}<span style="color:#d12">няма</span>{% endif %}</td>
<td style="white-space:nowrap">{% if x.have and not x.have.active %}<form method=post action="/admin/missed/{{ cur.id }}/source"><input type=hidden name=sid value="{{ x.have.id }}"><input type=hidden name=domain value="{{ x.domain }}"><button class=btn>Включи</button></form>
{% elif not x.have %}<form method=post action="/admin/missed/{{ cur.id }}/source" style="display:flex;gap:4px"><input type=hidden name=domain value="{{ x.domain }}"><input type=hidden name=name value="{{ x.name }}"><input type=hidden name=lang value="{{ d.lang }}">
<select name=category style="width:150px">{% for k in cats %}<option>{{ k }}</option>{% endfor %}</select><button class=btn>+ Добави</button></form>{% endif %}</td></tr>
{% else %}<tr><td colspan=4 class=muted>Не намерих други медии за тази новина в Google News за последните 3 дни.</td></tr>{% endfor %}</table></div>{% endif %}
<h3>Досега</h3><table>{% for r in rows %}<tr><td class=muted style="width:110px">{{ r.when }}</td><td><a href="/admin/missed?id={{ r.id }}">{{ r.title or r.url }}</a><div class=muted>{{ r.d.why }}</div></td><td>{{ '✅ оправено' if r.status=='fixed' else '' }}</td></tr>
{% else %}<tr><td class=muted>Още няма.</td></tr>{% endfor %}</table>{% endblock %}""",
"improve": """{% extends 'base' %}{% block c %}<p><a href=/admin>← Назад</a></p>
<h2>Подобри системата</h2>
<p>⏱ Средно време до пост (от първата статия до нашата публикация, 30 дни): <b>{% if med is not none %}{{ '%.0f' % med }} мин{% else %}още няма публикации{% endif %}</b> ({{ npub }} поста)</p>
<h3>1. Какво учи от твоите избори</h3><p class=muted>→ Чернова / ⚡ Бърз пост / Публикувай = харесано, ✕ = не. Множителят се прилага към оценката (Настройки: learn_strength, learn_window_days).</p>
<table><tr><th>Категория</th><th>Взети</th><th>Отхвърлени</th><th>Множител</th></tr>{% for c,p,r in sig %}<tr><td>{{ c }}</td><td>{{ p }}</td><td>{{ r }}</td><td><b>×{{ '%.2f' % lcat.get(c,1) }}</b></td></tr>{% else %}<tr><td colspan=4 class=muted>Още няма избори. Ползвай „⚡ Бърз пост“ и „✕“ в Компаса.</td></tr>{% endfor %}</table>
<h3>2. Коментари ({{ fb|selectattr('status','equalto','new')|list|length }} нови)</h3>
<p><form method=post action="/admin/improve/run" style="display:inline"><button class="btn red">🤖 Claude: предложи промени от коментарите</button></form> <a class=btn href="/admin/improve/export" target=_blank>Експорт за Claude (код)</a></p>
{% if prop %}<form method=post action="/admin/improve/apply/{{ pid }}" style="background:#fffbe6;border:1px solid #ec0;border-radius:8px;padding:10px;margin:10px 0"><b>Предложение</b> — отметни кое да се приложи:
<table>{% for k,v in prop.settings.items() %}<tr><td style="width:30px"><input type=checkbox name=k value="{{ k }}" checked style="width:auto"></td><td><b>{{ k }}</b><div class=muted>{{ prop.why.get(k,'') if prop.why else '' }}</div></td><td class=muted>{{ cur.get(k,'') }}</td><td>→ <b>{{ v }}</b></td></tr>{% else %}<tr><td colspan=4 class=muted>Без промени в настройките.</td></tr>{% endfor %}</table>
{% if prop.code %}<p><b>Идеи за кода</b> (записват се в IMPROVE.md):</p><ul>{% for x in prop.code %}<li>{{ x }}</li>{% endfor %}</ul>{% endif %}
<button class=btn>Приложи отметнатите</button></form>{% endif %}
<table>{% for f in fb %}<tr style="{{ 'opacity:.45' if f.status not in ('new','чака изясняване') }}">
<td>{{ '👍' if f.rating>0 else '👎' if f.rating<0 else '' }} {{ f.text }}{% if f.kind=='image' %} <img src="{{ f.ref }}" class=thumb>{% endif %}
<div class=muted>{{ f.when }} · {{ (f.page or '').split('?')[0] }}</div>
{% if f.claude_q %}<div style="background:#fffbe6;border:1px solid #ec0;border-radius:6px;padding:6px 8px;margin-top:6px">🤖 <b>Claude пита:</b> {{ f.claude_q }}
{% if f.answer %}<div>✍ <b>Ти:</b> {{ f.answer }}</div>{% else %}<form method=post action="/admin/feedback/{{ f.id }}/answer" style="display:flex;gap:6px;margin-top:4px"><input name=answer placeholder="Отговор — промяната чака, докато не отговориш" required><button class=btn>Отговори</button></form>{% endif %}</div>{% endif %}</td>
<td class=muted>{{ f.status }}</td>
<td style="white-space:nowrap">{% if f.status=='new' %}<form method=post action="/admin/feedback/{{ f.id }}/dismissed" style="display:inline"><button class="btn gray">скрий</button></form>{% endif %}</td></tr>
{% else %}<tr><td class=muted>Още няма коментари. Бутонът „💬 Коментар“ е долу вдясно на всяка страница, а за снимки — в черновата.</td></tr>{% endfor %}</table>{% endblock %}""",
"profiles": """{% extends 'base' %}{% block c %}<p><a href=/admin>← Назад</a></p><form method=post action="/admin/profiles/new" style="display:flex;gap:8px">
<select name=platform style="width:140px"><option>instagram</option><option>tiktok</option></select><input name=handle placeholder="username"><button class=btn>Добави</button></form><br>
<form method=post action="/admin/profiles/check"><button class=btn>Провери всички</button></form><br><table>
{% for p in rows %}<tr><td>{{ p.platform }}</td><td><a href="{{ 'https://instagram.com/' if p.platform=='instagram' else 'https://tiktok.com/@' }}{{ p.handle }}" target=_blank>@{{ p.handle }}</a></td>
<td><pre style="white-space:pre-wrap;font-size:12px;max-height:160px;overflow:auto">{{ p.last_json or '' }}</pre></td></tr>{% endfor %}</table>{% endblock %}""",
"settings": """{% extends 'base' %}{% block c %}<p><a href=/admin>← Назад</a></p>
<p style="background:#fff;border:1px solid #ddd;border-radius:8px;padding:8px 12px">🤖 Claude: <b>{{ claude_status }}</b>
<form method=post action="/admin/claude-test" style="display:inline"><button class="btn gray">Тествай</button></form></p><form method=post><table>
{% for k,v in rows %}<tr><td style="width:240px"><b>{{ k }}</b></td><td><input name="{{ k }}" value="{{ v }}" {% if 'token' in k or '_pass' in k or 'password' in k or 'api_key' in k or 'secret' in k %}type=password{% endif %}></td></tr>{% endfor %}
</table><br><button class=btn>Запази</button></form>
<p style="margin-top:8px"><a class="btn red" href="/admin/canva/connect">Свържи Canva</a> <a class=btn href="/admin/canva/templates">Избери шаблон</a></p>
<form method=post action="/admin/canva-fields"><button class="btn gray">Провери полетата на Canva шаблона</button></form><p class=muted>w_* са тежести в %, сборът им е добре да е 100. След промяна → „Изтегли сега“ за преизчисляване.</p>{% endblock %}""",
"bias": """{% extends 'base' %}{% block c %}<p><a href=/admin>← Назад</a></p>
<h2>Баланс на лентата <form method=post action="/admin/fetch" style="display:inline"><button class=btn title="Изтегли новините сега и преизчисли">⟳ Обнови</button></form></h2><p>Ос: <b>{{ axis }}</b> · Наш наклон за последните {{ days }} дни: <b>{{ '%+.2f' % drift }}</b> ({{ n }} публикации)
<span class=muted> — 0 е центърът; при отклонение системата бутва източници от другата страна (w_balance).</span></p>
<datalist id=groups>{% for g in all_groups %}<option value="{{ g }}">{% endfor %}</datalist>
<h2>Истории: кой ги покрива и кой мълчи</h2>
<p class=muted>„Мълчи“ = група, от която се очаква да покрие историята: същата категория, за българска история само медии на български, и групата реално е публикувала в тази категория напоследък. Останалите не се броят.</p>
<form method=get class=tabs style="display:flex;gap:8px;flex-wrap:wrap;align-items:center">
<input name=q value="{{ q }}" placeholder="Търси в заглавията" style="width:220px">
<select name=lang style="width:150px"><option value="">Всички езици</option><option value=bg {% if lang=='bg' %}selected{% endif %}>Български истории</option><option value=intl {% if lang=='intl' %}selected{% endif %}>Чужди истории</option></select>
<select name=only style="width:190px"><option value="">Всички истории</option><option value=silent {% if only=='silent' %}selected{% endif %}>Само с мълчащи групи</option><option value=trend {% if only=='trend' %}selected{% endif %}>Само с индекс на тишина</option></select>
<input type=hidden name=cat value="{{ cat }}"><button class=btn>Филтрирай</button> <a href="/admin/bias">Изчисти</a></form>
<div class=tabs style="margin-top:8px"><b>Категория:</b> <a href="{{ url(cat='') }}" {% if not cat %}style="font-weight:bold;text-decoration:underline"{% endif %}>Всички</a>
{% for k in cats %}<a href="{{ url(cat=k) }}" style="text-decoration:none"><span class="tag {% if cat and k!=cat %}off{% endif %}" style="background:{{ colors.get(k, '#6b7280') }};color:{{ txt(colors.get(k, '#6b7280')) }}">{{ k }}</span></a>{% endfor %}</div>
<p class=muted>{{ total }} истории{% if total > stories|length %}, показани първите {{ stories|length }}{% endif %}.</p>
<table><tr><th>История</th><th>Групи, които я покриват</th><th>Мълчат</th><th>Разлика в наклона</th><th>Тишина</th><th></th></tr>
{% for k in stories %}<tr><td>{{ k.title }}<div class=muted>{{ k.n }} статии · {{ k.cats|join(', ') }}{% if not k.bg %} · чужда{% endif %}</div></td>
<td>{{ k.cov|join(', ') }}{% if k.ok %}<div class=muted>{{ k.ncov }} от {{ k.nexp }} очаквани групи</div>{% endif %}</td>
<td style="color:#d12">{% if k.ok %}{% for o in k.miss %}<span style="white-space:nowrap">{{ media_links([o], k.title, k.item) }}<form method=post action="/admin/silence-fix/{{ k.item }}" style="display:inline"><input type=hidden name=owner value="{{ o }}"><button name=action value=not_silent title="Не е мълчание — махни" style="border:0;background:none;cursor:pointer;color:#999">✕</button></form></span>{% if not loop.last %}, {% endif %}{% endfor %}{% else %}<span class=muted>— малко медии в обхвата</span>{% endif %}
<form method=post action="/admin/silence-fix/{{ k.item }}" style="margin-top:4px;display:flex;gap:4px"><input name=owner list=groups placeholder="+ трябваше да отрази…" style="width:160px;font-size:12px;padding:3px"><button class="btn gray" name=action value=should_cover style="padding:3px 8px">+</button></form></td>
<td>{{ '%.2f' % k.spread }}</td><td><b>{{ '%.0f' % (k.silence*100) }}%</b></td>
<td><form method=post action="/admin/framing/{{ k.id }}"><button class="btn gray">Сравни рамкирането</button></form></td></tr>
{% if k.framing %}<tr><td colspan=6 style="white-space:pre-wrap;background:#fffbe6">{{ k.framing }}</td></tr>{% endif %}
{% else %}<tr><td colspan=6>Няма истории за тези филтри.</td></tr>{% endfor %}</table>{% endblock %}""",
"preview": """{% extends 'base' %}{% block c %}<p><a href="/admin/post/{{ p.id }}">← Обратно към черновата</a></p>
<div style="width:540px;height:675px;position:relative;border-radius:12px;overflow:hidden;background:#111 url('{{ p.image or '' }}') center/cover">
<div style="position:absolute;inset:0;background:linear-gradient(transparent 35%,rgba(0,0,0,.85))"></div>
<div style="position:absolute;left:28px;right:28px;bottom:70px;color:#fff;font:800 34px/1.15 system-ui">{{ p.hook or p.title }}</div>
<div style="position:absolute;left:28px;right:28px;bottom:36px;color:#ddd;font:400 17px/1.3 system-ui">{{ p.sub or '' }}</div>
<div style="position:absolute;left:28px;top:24px;background:#d12;color:#fff;font:700 14px system-ui;padding:5px 10px;border-radius:4px">{{ site }}</div></div>
<p class=muted>Това е локален макет 4:5. С canva_token + canva_template_id същият бутон попълва вашия истински Canva шаблон и го отваря.</p>{% endblock %}""",
"raw": """{% extends 'base' %}{% block c %}{{ html|safe }}{% endblock %}""",
"log": """{% extends 'base' %}{% block c %}<p><a href=/admin>← Назад</a></p><table>{% for l in rows %}<tr><td class=muted style="width:160px">{{ l.when }}</td><td>{{ l.msg }}</td></tr>{% endfor %}</table>{% endblock %}""",
}
env = Environment(loader=DictLoader(T), autoescape=select_autoescape(default=True))

def when(ts): return datetime.fromtimestamp(ts or 0).strftime("%d.%m %H:%M")
_CC = {"t": 0, "v": {}}
def cat_colors():
    if time.time() - _CC["t"] < 10: return _CC["v"]
    out = {}
    for part in S("category_colors").split(","):
        if "=" in part: k, v = part.split("=", 1); out[k.strip()] = v.strip()
    _CC.update(t=time.time(), v=out); return out

def _txt_on(hexc):
    h = hexc.lstrip("#")
    try: r, g, b = (int(h[i:i + 2], 16) for i in (0, 2, 4))
    except Exception: return "#fff"
    return "#111" if (0.299 * r + 0.587 * g + 0.114 * b) > 165 else "#fff"

def tag(cat, small=False):
    """Цветен етикет на категория."""
    from markupsafe import Markup, escape
    if not cat: return Markup("")
    c = cat_colors().get(cat, "#6b7280")
    return Markup(f'<span class=tag style="background:{c};color:{_txt_on(c)}">{escape(cat)}</span>')

def src_tag(name, cat, sid=None):
    """Етикет на източника — в тона на категорията му. С sid → линк към профила на източника."""
    from markupsafe import Markup, escape
    c = cat_colors().get(cat or "", "#6b7280")
    t = f'<span class="tag src" style="border-color:{c};color:{c};background:{c}14">{escape(name)}</span>'
    if isinstance(sid, int): return Markup(f'<a href="/admin/source/{sid}" title="Профил на източника" style="text-decoration:none">{t}</a>')
    return Markup(t)

NOTE_REASONS = ["+ важно", "+ Бургас / местно", "+ скандал / разследване", "+ хората го търсят", "+ вирусно / интересно",
                "− скучно", "− PR / реклама", "− дубликат", "− не е за нас", "− жълто"]

def note_form(iid, verdict=None):
    """📝 на всяка новина: причина + текст + бих/не бих. Това учи рейтинга."""
    from markupsafe import Markup
    chips = "".join(f'<label style="display:inline-block;margin:2px 6px 2px 0;font-size:12px;white-space:nowrap">'
                    f'<input type=checkbox name=reasons value="{r}" style="width:auto"> {r}</label>' for r in NOTE_REASONS)
    mark = {"would": "👍", "wouldnt": "👎"}.get(verdict or "", "📝")
    return Markup(f'<details style="display:inline-block;vertical-align:top"><summary class="btn gray" style="list-style:none">{mark}</summary>'
                  f'<form method=post action="/admin/note/{iid}" style="position:absolute;z-index:5;background:#fff;border:1px solid #ccc;'
                  f'border-radius:8px;padding:8px;width:330px;right:20px;box-shadow:0 4px 16px rgba(0,0,0,.15)">{chips}'
                  f'<input name=text placeholder="Защо? (по желание)" style="margin:4px 0">'
                  f'<button class=btn name=verdict value=would>👍 Бих я покрил</button> '
                  f'<button class="btn gray" name=verdict value=wouldnt>👎 Не бих</button></form></details>')

def media_links(owners, title="", item=0):
    """Групи → линкове към профилите им. С title → отваря профила с готова бележка „Не отрази: …“ (1 клик + Запиши)."""
    from markupsafe import Markup, escape
    q = f"?note={quote('Не отрази: ' + title[:120])}&item={item}" if title else ""
    return Markup(", ".join(f'<a href="/admin/media/{quote(o)}{q}" style="color:inherit" title="Профил / бележка">{escape(o)}</a>' for o in owners))

def vote(iid, verdict=None):
    """▲ / ▼ — бърза бележка без причина: ▲ качва новината горе, ▼ я сваля долу. И двете учат рейтинга."""
    from markupsafe import Markup
    up = "red" if verdict == "would" else "gray"; dn = "red" if verdict == "wouldnt" else "gray"
    return Markup(f'<form method=post action="/admin/note/{iid}" style="display:inline"><button class="btn {up}" name=verdict value=would title="Бих я покрил — качва се горе">▲</button></form>'
                  f'<form method=post action="/admin/note/{iid}" style="display:inline"><button class="btn {dn}" name=verdict value=wouldnt title="Не бих — сваля се долу">▼</button></form>')

env.globals.update(tag=tag, src_tag=src_tag, media_links=media_links, note_form=note_form, vote=vote)

_HOT = {"t": 0, "n": 0}
def hot_count():
    """Колко истории са „Хвани сега“ (кеш 2 мин, за да не забавя страниците)."""
    if time.time() - _HOT["t"] > 120:
        st, _ = compass_data()
        _HOT.update(t=time.time(), n=sum(1 for k in st if k["level"] == "now"))
    return _HOT["n"]

def render(name, request=None, **kw):
    if request is not None and request.url.path.startswith("/admin") and "hot" not in kw:
        try: kw["hot"] = hot_count()
        except Exception: kw["hot"] = 0
    kw.update(site=S("site_name"), donate=S("donate_url"), flash=request.query_params.get("m") if request else None,
              path=(request.url.path + ("?" + request.url.query if request.url.query else "")) if request else "")
    return HTMLResponse(env.get_template(name).render(**kw))
def back(url, msg=""): return RedirectResponse(url + (("&" if "?" in url else "?") + "m=" + msg if msg else ""), 303)

# ---------------------------------------------------------------- App
async def scheduler():
    while True:
        try:
            await fetch_all(); await alert_now(); await autopilot()
            if S("extract_auto") == "on": await extract_run()
        except Exception as ex: log(f"scheduler: {ex}")
        await asyncio.sleep(max(1, S("fetch_every_min", int)) * 60)

@asynccontextmanager
async def lifespan(app):
    init_db()
    if "--demo" in sys.argv: seed_demo()
    task = None if "--no-fetch" in sys.argv else asyncio.create_task(scheduler())
    yield
    if task: task.cancel()

app = FastAPI(lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC), name="static")

# Без парола само когато сайтът е достъпен единствено от този компютър. Отвориш ли го към мрежата — паролата е включена.
# Зад tunnel заявките идват от 127.0.0.1, затова при https адрес паролата е задължителна винаги.
ADMIN_LOGIN_OFF = (HOST in ("127.0.0.1", "localhost") and not IS_HTTPS
                   and os.environ.get("NEWSROOM_LOGIN", "off") == "off")

def session_token():
    """Cookie-то не съдържа паролата: HMAC(таен ключ, парола). Смяна на паролата = изход за всички."""
    return hmac.new(S("session_secret").encode(), S("admin_password").encode(), hashlib.sha256).hexdigest()

def set_session(r):
    r.set_cookie("nr", session_token(), httponly=True, samesite="lax", secure=IS_HTTPS, max_age=60 * 60 * 24 * 30)
    return r

@app.middleware("http")
async def guard(request: Request, call_next):
    p = request.url.path
    if (not ADMIN_LOGIN_OFF and p.startswith("/admin") and p != "/admin/login"
            and not hmac.compare_digest(request.cookies.get("nr", ""), session_token())):
        return RedirectResponse("/admin/login", 303)
    return await call_next(request)

@app.get("/healthz")
def healthz():
    """За UptimeRobot: ok, ако базата се чете и някой източник е теглен през последните 30 мин."""
    try:
        with db() as c: last = c.execute("select max(last_fetch) from sources").fetchone()[0] or 0
    except Exception as e:
        return HTMLResponse(f"db error: {e}", 503)
    age = time.time() - last
    return HTMLResponse("ok" if age < 1800 else f"stale: последно теглене преди {int(age // 60)} мин",
                        200 if age < 1800 else 503)

@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    with db() as c:
        rows = [dict(r) | {"when": when(r["published"])} for r in
                c.execute("select * from posts where status='published' order by published desc limit 60")]
    for r in rows:
        if not r["image"]: r["image"] = best_image(r)
    return render("home", request, posts=rows)

@app.get("/p/{pid}", response_class=HTMLResponse)
def post_page(pid: int, request: Request):
    with db() as c:
        p = c.execute("select * from posts where id=? and status='published'", (pid,)).fetchone()
        src = c.execute("select link from items where id=?", (p["item_id"],)).fetchone() if p else None
    if not p: return HTMLResponse("Няма такава", 404)
    m = re.search(r"(?:v=|youtu\.be/)([\w-]{11})", p["media_url"] or "")
    return render("post", request, p=dict(p) | {"when": when(p["published"])}, yt=m.group(1) if m else None,
                  src=src[0] if src else None)

@app.get("/live", response_class=HTMLResponse)
def live(request: Request): return render("live", request, hls=S("live_hls_url"))

@app.get("/admin/login", response_class=HTMLResponse)
def login_form(request: Request): return render("login", request)

@app.post("/admin/login")
def login(pw: str = Form("")):
    r = RedirectResponse("/admin", 303)
    if pw and hmac.compare_digest(pw, S("admin_password")): set_session(r)
    else: r = RedirectResponse("/admin/login?bad=1", 303)
    return r

def regions(): return [x.strip() for x in S("region_categories").split(",") if x.strip()]

REGION_SQL = ("(case when s.category in ({ph}) then s.category when coalesce(s.lang,'bg')='bg' then 'България' else 'Свят' end)")

@app.get("/admin", response_class=HTMLResponse)
def admin(request: Request, tab: str = "rec", cat: str = "", q: str = "", src: str = "", lang: str = "", img: str = "",
          region: str = "", sort: str = ""):
    """Всички филтри (таб, категория, търсене, източник, език, снимка) важат едновременно — и за списъка, и за бройките."""
    base = ["i.status='recommended'" if tab == "rec" else "i.status in ('new','recommended')"]
    args = []
    if q.strip(): base.append("(lower(i.title) like ? or lower(coalesce(i.title_bg,'')) like ?)"); args += [f"%{q.strip().lower()}%"] * 2
    if src.isdigit(): base.append("s.id=?"); args.append(int(src))
    if lang == "bg": base.append("coalesce(s.lang,'bg')='bg'")
    elif lang == "intl": base.append("coalesce(s.lang,'bg')!='bg'")
    rg = regions()
    if region: base.append(REGION_SQL.format(ph=",".join("?" * len(rg))) + "=?"); args += rg + [region]
    if img == "1": base.append("coalesce(i.image,'')!=''")
    flt, fargs = list(base), list(args)
    if cat: flt.append("(coalesce(s.category,'')=? or ','||coalesce(i.topics,'')||',' like ?)"); fargs += [cat, f"%,{cat},%"]
    with db() as c:
        rows = [dict(r) | {"when": when(r["published"])} for r in c.execute(
            "select i.*, s.name src, s.category cat, s.lang lang, s.owner owner from items i join sources s on s.id=i.source_id "
            f"where {' and '.join(flt)} order by (coalesce(i.verdict,'')='would') desc, (coalesce(i.verdict,'')='wouldnt'), "
            + ("i.published desc" if sort == "new" else "score desc") + " limit 150", fargs)]
        # без снимка → снимка от друга статия в същата история
        need = [r for r in rows if not r["image"] and r["cluster"] is not None]
        if need:
            cls = list({r["cluster"] for r in need})
            pics = dict(c.execute(f"select cluster, image from items where cluster in ({','.join('?' * len(cls))}) "
                                  "and coalesce(image,'')!='' group by cluster", cls).fetchall())
            for r in need: r["image"] = pics.get(r["cluster"])
        counts = {k: c.execute("select count(*) from items i join sources s on s.id=i.source_id where " + " and ".join(base) +
                               " and (coalesce(s.category,'')=? or ','||coalesce(i.topics,'')||',' like ?)", args + [k, f"%,{k},%"]).fetchone()[0]
                  for k in categories()}
        srcs = c.execute("select id, name from sources where active=1 and kind not in ('trends','wiki') order by name").fetchall()
    def url(**kw):
        d = {"tab": tab, "cat": cat, "q": q, "src": src, "lang": lang, "img": img, "region": region, "sort": sort} | kw
        return "/admin?" + "&".join(f"{k}={quote(str(v))}" for k, v in d.items() if v)
    return render("admin", request, items=rows, brief=S("claude_brief"), tab=tab, cat=cat, cats=categories(), counts=counts,
                  colors=cat_colors(), txt=_txt_on, q=q, src=src, lang=lang, img=img, srcs=srcs, url=url, region=region,
                  regions=rg, sort=sort)

def categories():
    return [x.strip() for x in S("source_categories").split(",") if x.strip()]

@app.post("/admin/claude")
async def do_claude():
    try: msg = await claude_brief()
    except Exception as ex: msg = f"Claude грешка: {ex}"
    log(msg); return back("/admin", msg)

@app.post("/admin/fetch")
async def do_fetch(request: Request):
    await fetch_all(force=True)
    ref = request.headers.get("referer", "/admin")      # връща те на страницата, от която натисна
    return RedirectResponse((ref if "/admin" in ref else "/admin").split("&m=")[0].split("?m=")[0], 303)

@app.get("/admin/test", response_class=HTMLResponse)
async def test_page(request: Request):
    rows = await test_sources()
    tr = "".join(f"<tr><td>{'✅' if r['ok'] else ('⏸' if r.get('wait') else '❌')}</td><td>{tag(r['cat'])} {r['name']}<div class=muted>{r['kind']} · HTTP {r.get('code', '?')} · {r['ms']} ms</div></td>"
                 f"<td>{r['n']}</td><td>{r['img']}</td><td>{r['last'][:90]}<div style='color:#d12'>{r['err']}</div></td></tr>" for r in rows)
    ok = sum(r["ok"] for r in rows)
    html = (f"<p><a href=/admin>← Назад</a></p><h2>Тест на източниците: {ok} от {len(rows)} работят</h2>"
            f"<table><tr><th></th><th>Източник</th><th>Записи</th><th>Със снимка</th><th>Последно</th></tr>{tr}</table>"
            f"<p class=muted>Ако всичко е ✅ → „Изтегли сега“ в админа и гледай „Препоръчани“ и „Лог“.</p>")
    return render("raw", request, html=html)

@app.post("/admin/autopilot")
async def run_autopilot():
    await autopilot(); return back("/admin/posts", "Автопилотът мина (режим: " + S("auto_mode") + ")")

@app.post("/admin/imap")
def do_imap():
    try: msg = imap_poll()
    except Exception as ex: msg = f"IMAP грешка: {ex}"
    return back("/admin", msg)

@app.post("/admin/reject/{iid}")
def reject(iid: int, request: Request):
    with db() as c:
        c.execute("update items set status='rejected' where id=?", (iid,)); add_signal(c, iid, "reject")
    ref = request.headers.get("referer", "/admin")
    return RedirectResponse(ref if "/admin" in ref else "/admin", 303)

@app.post("/admin/draft/{iid}")
def make_draft(iid: int):
    with db() as c:
        i = c.execute("select * from items where id=?", (iid,)).fetchone()
        cur = c.execute("insert into posts(item_id,title,body,image,created) values(?,?,?,?,?)",
                        (iid, i["title"], i["summary"], i["image"], time.time()))
        c.execute("update items set status='draft' where id=?", (iid,)); add_signal(c, iid, "draft")
    return back(f"/admin/post/{cur.lastrowid}", "Черновата е създадена — пренапиши текста със свои думи")

@app.post("/admin/intl/{iid}")
async def make_intl(iid: int):
    with db() as c:
        i = c.execute("select i.*, s.name src from items i join sources s on s.id=i.source_id where i.id=?", (iid,)).fetchone()
    title, body = i["title_bg"] or i["title"], f"{i['summary'] or ''}\n\nИзточник: {i['src']}"
    msg = "Международен пост — Claude не е свързан, преведи ръчно"
    if claude_ready():
        prompt = (f"{S('intl_style')}\nВърни: първи ред = заглавие, после празен ред, после текста.\n\n"
                  f"Медия: {i['src']}\nЗаглавие: {i['title']}\nТекст: {(i['summary'] or '')[:2500]}\nЛинк: {i['link']}")
        try:
            txt = await ask_claude(prompt, max_tokens=1200)
            title, _, body = txt.partition("\n"); title, body = title.strip(" #*\"„“"), body.strip()
            msg = "Международен пост — преведен. Провери превода преди публикуване"
        except Exception as ex: msg = f"Преводът не стана ({ex}) — черновата е с оригинала"
    with db() as c:
        cur = c.execute("insert into posts(item_id,title,body,image,created,intl) values(?,?,?,?,?,1)",
                        (iid, title[:200], body, i["image"], time.time()))
        c.execute("update items set status='draft' where id=?", (iid,))
    return back(f"/admin/post/{cur.lastrowid}", msg)

@app.get("/admin/posts", response_class=HTMLResponse)
def posts(request: Request):
    with db() as c:
        rows = [dict(r) | {"ch": ", ".join(json.loads(r["channels"] or "{}"))} for r in
                c.execute("select * from posts where status!='deleted' order by (status='ready') desc, created desc limit 200")]
    return render("posts", request, posts=rows)

@app.post("/admin/post/{pid:int}/delete")
def delete_post(pid: int):
    """Изтриване на чернова = скриване (status deleted). Нищо не се губи от базата."""
    with db() as c:
        c.execute("update posts set status='deleted' where id=? and status!='published'", (pid,))
        it = c.execute("select item_id from posts where id=?", (pid,)).fetchone()
        if it and it[0]: c.execute("update items set status='new' where id=? and status='draft'", (it[0],))
    return back("/admin/posts", "Черновата е изтрита")

@app.get("/admin/post/{pid}", response_class=HTMLResponse)
def edit(pid: str, request: Request):
    p, related = {}, []
    if pid != "new":
        with db() as c:
            p = dict(c.execute("select * from posts where id=?", (int(pid),)).fetchone())
            p["exports"] = json.loads(p.get("export_json") or "[]") or ([p["export_path"]] if p.get("export_path") else [])
            it = c.execute("select cluster from items where id=?", (p["item_id"],)).fetchone() if p["item_id"] else None
            if it and it[0] is not None:
                related = c.execute("select i.*, s.name src from items i join sources s on s.id=i.source_id "
                                    "where i.cluster=? and i.id!=? order by i.score desc limit 20", (it[0], p["item_id"])).fetchall()
    return render("edit", request, p=p, related=related)

@app.post("/admin/useimg/{pid}")
def use_img(pid: int, image: str = Form(""), media: str = Form("")):
    with db() as c:
        if image: c.execute("update posts set image=? where id=?", (image, pid))
        if media: c.execute("update posts set media_url=? where id=?", (media, pid))
    return back(f"/admin/post/{pid}", "Добавено")

@app.post("/admin/post/{pid}")
def save(pid: str, title: str = Form(""), body: str = Form(""), image: str = Form(""),
         media_url: str = Form(""), hook: str = Form(""), sub: str = Form(""), act: str = Form("save")):
    with db() as c:
        if pid == "new":
            pid = c.execute("insert into posts(title,body,image,media_url,hook,sub,created) values(?,?,?,?,?,?,?)",
                            (title, body, image, media_url, hook, sub, time.time())).lastrowid
        else:
            c.execute("update posts set title=?,body=?,image=?,media_url=?,hook=?,sub=? where id=?",
                      (title, body, image, media_url, hook, sub, int(pid)))
        if act == "publish":
            c.execute("update posts set status='published',published=? where id=?", (time.time(), int(pid)))
            it = c.execute("select item_id from posts where id=?", (int(pid),)).fetchone()
            if it and it[0]: add_signal(c, it[0], "publish")
    return back(f"/admin/post/{pid}", "Публикувано в сайта" if act == "publish" else "Запазено")

@app.post("/admin/push/{pid}")
async def push(pid: int, to: str = Form(...)):
    with db() as c: p = dict(c.execute("select * from posts where id=?", (pid,)).fetchone())
    fn = {"instagram": instagram_publish, "tiktok": tiktok_draft,
          "telegram": telegram_publish, "threads": threads_publish}[to]
    try: msg = await fn(p)
    except Exception as ex: msg = f"{to} грешка: {ex}"
    if "грешка" not in msg and "Липсва" not in msg and "не е" not in msg:
        ch = json.loads(p["channels"] or "{}"); ch[to] = time.time()
        with db() as c: c.execute("update posts set channels=? where id=?", (json.dumps(ch), pid))
    log(f"post {pid} → {to}: {msg}")
    return back(f"/admin/post/{pid}", msg)

@app.post("/admin/canva/{pid:int}")
async def canva_button(pid: int):
    """ЕДИН БУТОН: AI текст (ако липсва) → снимка → нашия Canva шаблон → отваря дизайна в Canva."""
    with db() as c: p = dict(c.execute("select * from posts where id=?", (pid,)).fetchone())
    if not p.get("hook"):
        try: hook, sub = await ai_hook(p)
        except Exception: hook, sub = p["title"][:70], ""
        with db() as c: c.execute("update posts set hook=?, sub=? where id=?", (hook, sub, pid))
        p.update(hook=hook, sub=sub)
    with db() as c:
        it = c.execute("select link from items where id=?", (p.get("item_id"),)).fetchone() if p.get("item_id") else None
    if it and "news.google.com" not in it[0]:
        async with httpx.AsyncClient() as client:
            big = await og_image(client, it[0])          # обикновено по-голяма снимка от тази в RSS
        if big:
            with db() as c: c.execute("update posts set image=? where id=?", (big, pid))
            p["image"] = big
    if not p.get("image"):
        img = best_image(p)
        if img:
            with db() as c: c.execute("update posts set image=? where id=?", (img, pid))
            p["image"] = img
    if not ((S("canva_token") or S("canva_refresh_token")) and S("canva_template_id")):
        return back(f"/admin/preview/{pid}", "Canva не е свързан — това е локален преглед на шаблона")
    try: url, msg = await canva_make(p)
    except Exception as ex: url, msg = None, f"Canva грешка: {ex}"
    if url:
        try: await canva_upload_all(p)
        except Exception as ex: log(f"post {pid} → Canva снимки: {ex}")
    log(f"post {pid} → canva: {msg}")
    return RedirectResponse(url, 303) if url else back(f"/admin/post/{pid}", msg)

@app.post("/admin/canva-pull/{pid}")
async def canva_pull_route(pid: int):
    with db() as c: p = dict(c.execute("select * from posts where id=?", (pid,)).fetchone())
    try: msg = await canva_pull(p)
    except Exception as ex: msg = f"Canva грешка: {ex}"
    log(f"post {pid} ← canva: {msg}")
    return back(f"/admin/post/{pid}", msg)

@app.get("/admin/canva/connect")
def canva_connect():
    if not (S("canva_client_id") and S("canva_client_secret")):
        return back("/admin/canva/setup", "Първо попълни Client ID и Client secret (стъпка 1–3)")
    ver = secrets.token_urlsafe(64)
    ch = base64.urlsafe_b64encode(hashlib.sha256(ver.encode()).digest()).decode().rstrip("=")
    st = secrets.token_urlsafe(16); _PKCE[st] = ver
    from urllib.parse import urlencode
    q = urlencode({"code_challenge_method": "s256", "response_type": "code", "client_id": S("canva_client_id"),
                   "redirect_uri": CANVA_REDIRECT, "scope": CANVA_SCOPES, "code_challenge": ch, "state": st})
    return RedirectResponse("https://www.canva.com/api/oauth/authorize?" + q, 303)

@app.get("/admin/canva/callback")
async def canva_callback(code: str = "", state: str = "", error: str = ""):
    if error or state not in _PKCE: return back("/admin/canva/setup", f"Canva връзката не стана: {error or 'невалиден state'}")
    try:
        await canva_oauth_token({"grant_type": "authorization_code", "code": code,
                                 "code_verifier": _PKCE.pop(state), "redirect_uri": CANVA_REDIRECT})
        msg = "Canva е свързан ✓ — сега избери шаблон"
    except Exception as ex: msg = str(ex)
    return back("/admin/canva/setup", msg)

@app.get("/admin/canva/templates", response_class=HTMLResponse)
async def canva_templates(request: Request):
    tok = await canva_tok()
    if not tok: return back("/admin/settings", "Първо „Свържи Canva“")
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.get("https://api.canva.com/rest/v1/brand-templates", params={"dataset": "non_empty"},
                        headers={"Authorization": f"Bearer {tok}"})
    items = r.json().get("items", []) if r.status_code < 300 else []
    rows = "".join(f"<tr><td><img class=thumb src='{(t.get('thumbnail') or {}).get('url', '')}'></td><td>{t.get('title')}</td>"
                   f"<td><form method=post action='/admin/canva/use/{t['id']}'><button class=btn>Ползвай този</button></form></td></tr>" for t in items)
    note = "" if r.status_code < 300 else f"<p class=flash>Canva {r.status_code}: {r.text[:200]}</p>"
    html = (f"<p><a href=/admin/settings>← Настройки</a></p>{note}<h2>Шаблони с autofill полета</h2>"
            f"<table>{rows or '<tr><td>Няма. Шаблонът трябва да е публикуван като Brand template и да има Data autofill полета.</td></tr>'}</table>")
    return render("raw", request, html=html)

@app.post("/admin/canva/use/{tid}")
async def canva_use(tid: str):
    _set("canva_template_id", tid)
    return await canva_fields_btn()

@app.post("/admin/canva-fields")
async def canva_fields_btn():
    f, msg = await canva_fields()
    if not f: return back("/admin/settings", msg)
    names = ", ".join(k + " (" + str(v.get("type")) + ")" for k, v in f.items())
    return back("/admin/settings", "Полета в шаблона: " + names)

@app.get("/admin/preview/{pid}", response_class=HTMLResponse)
def preview(pid: int, request: Request):
    with db() as c: p = dict(c.execute("select * from posts where id=?", (pid,)).fetchone())
    return render("preview", request, p=p)

def source_health(c):
    """Работи ли източникът: новини за 48 ч / 7 дни и последна грешка в лога."""
    now = time.time()
    h = {}
    for sid, n2, n7, last in c.execute("select source_id, sum(fetched>?), sum(fetched>?), max(published) from items group by source_id",
                                       (now - 2 * 86400, now - 7 * 86400)):
        h[sid] = dict(n2=n2 or 0, n7=n7 or 0, last=last)
    errs = {}
    for (msg,) in c.execute("select msg from log where ts>? and msg like '%грешка%' order by id", (now - 86400,)):
        errs[msg.split(":")[0]] = msg[:160]
    return h, errs

@app.get("/admin/sources", response_class=HTMLResponse)
def sources(request: Request, health: str = ""):
    cats = categories()
    with db() as c:
        rows = [dict(r) for r in c.execute("select * from sources")]
        h, errs = source_health(c)
    for r in rows:
        x = h.get(r["id"], {"n2": 0, "n7": 0, "last": None})
        r["n2"], r["n7"], r["err"] = x["n2"], x["n7"], errs.get(r["name"], "")
        r["last_s"] = when(x["last"]) if x["last"] else "никога"
        r["health"] = ("изключен" if not r["active"] else "работи" if x["n2"] else "бавен" if x["n7"] else
                       "сигнал" if r["kind"] in ("trends", "wiki", "manual") else "не работи")
    if health: rows = [r for r in rows if r["health"] == health]
    hc = {}
    for r in rows: hc[r["health"]] = hc.get(r["health"], 0) + 1
    order = {k: i for i, k in enumerate(cats)}
    groups = {}
    for r in sorted(rows, key=lambda r: (order.get(r["category"] or "", 99), r["name"])):
        groups.setdefault(r["category"] or "", []).append(r)
    return render("sources", request, groups=list(groups.items()), cats=cats, health=health, hc=hc)

@app.post("/admin/sources/{sid}")
def save_source(sid: str, name: str = Form(""), kind: str = Form("rss"), url: str = Form(""),
                weight: float = Form(1), active: int = Form(0), owner: str = Form(""), lean: float = Form(0),
                category: str = Form(""), every_min: float = Form(0), lang: str = Form("bg")):
    lean = max(-1.0, min(1.0, lean))
    with db() as c:
        if sid == "new":
            if url: c.execute("insert into sources(name,kind,url,weight,active,owner,lean,category,every_min,lang) values(?,?,?,?,?,?,?,?,?,?)",
                              (name, kind, url, weight, active, owner, lean, category, every_min, lang.strip() or "bg"))
        else:
            c.execute("update sources set name=?,kind=?,url=?,weight=?,active=?,owner=?,lean=?,category=?,every_min=?,lang=? where id=?",
                      (name, kind, url, weight, active, owner, lean, category, every_min, lang.strip() or "bg", int(sid)))
    return back("/admin/sources", "Запазено")

def _traffic(t):
    """„2000+“ → 2000, „20K+“ → 20000, „1M+“ → 1000000."""
    m = re.search(r"(\d[\d\s.,]*)\s*([KkMМмхХ]?)", t or "")
    if not m: return 0
    n = int(re.sub(r"\D", "", m.group(1)) or 0)
    return n * {"k": 1000, "х": 1000, "m": 1_000_000, "м": 1_000_000}.get(m.group(2).lower(), 1)

def ig_medians(c):
    """Средни реакции (лайкове + 3×коментари) на последните 30 поста за всеки Instagram източник."""
    out = {}
    for sid in [r[0] for r in c.execute("select id from sources where kind='instagram'")]:
        v = sorted((l or 0) + 3 * (k or 0) for l, k in c.execute(
            "select likes, comments from items where source_id=? and likes is not null order by published desc limit 30", (sid,)))
        if len(v) >= 5: out[sid] = max(1, v[len(v) // 2])
    return out

def compass_data(hours=None):
    """Компасът: истории от последните N часа, подредени по ясни точки, които виждаш една по една."""
    now, hours = time.time(), hours or S("compass_hours", float)
    fresh = S("compass_fresh_h", float) * 3600
    with db() as c:
        gmap, gmin = silence_groups(c), S("silence_min_groups", int)
        _, lcat = learned_weights(c)
        igm = ig_medians(c)
        rows = c.execute("select i.*, s.name src, s.owner, coalesce(s.category,'') cat, coalesce(s.lang,'bg') lang, s.kind skind "
                         "from items i join sources s on s.id=i.source_id where i.published>? and i.status!='rejected' "
                         "and s.kind not in ('trends')", (now - hours * 3600,)).fetchall()
        trends = c.execute("select term, traffic, max(fetched) f from trends where fetched>? group by lower(term)",
                           (now - hours * 3600,)).fetchall()
        posted = {r[0] for r in c.execute("select i.cluster from posts p join items i on i.id=p.item_id where i.cluster is not null")}
        posted_items = {r[0] for r in c.execute("select item_id from posts where item_id is not null")}
    tk = {r["id"]: toks((r["title"] or "") + " " + (r["title_bg"] or "")) for r in rows}
    cl = {r["id"]: r["cluster"] if r["cluster"] is not None else r["id"] for r in rows}   # същите истории като в Баланс и оценката
    by = {}
    for r in rows: by.setdefault(cl[r["id"]], []).append(r)
    with db() as c: fixes = all_fixes(c)
    trend_t = [(t["term"], _traffic(t["traffic"]), t["traffic"], toks(t["term"]) - TREND_STOP) for t in trends]
    used_trends, stories = set(), []
    for cid, ms in by.items():
        hit = None
        for term, n, raw, tt in trend_t:   # трендът трябва да пасне на едно заглавие, не на сбора от думи на историята
            if not tt: continue
            if len(tt) == 1:     # тренд от една дума: трябва да е в поне половината заглавия на историята (централна, не случайна)
                ok = sum(1 for m in ms if tt <= tk[m["id"]]) * 2 >= len(ms) and len({m["source_id"] for m in ms}) >= 2
            else: ok = any(trend_hit(tk[m["id"]], tt) for m in ms)
            if ok and (not hit or n > hit[1]): hit = (term, n, raw, tt)
        if hit: used_trends.add(hit[0].lower())
        news = [m for m in ms if m["skind"] not in ("wiki",)]
        if not news: continue
        srcs = {m["source_id"] for m in news}
        recent = {m["source_id"] for m in news if now - m["published"] <= fresh}
        cov = {m["owner"] for m in news if m["owner"]}
        rm, add = fixes_for(fixes, [m["id"] for m in ms])
        exp = (expected_groups(gmap, news) - rm) | add
        miss = sorted(exp - cov) if len(exp) >= max(2, gmin) else []
        top = max(news, key=lambda m: m["score"] or 0)
        cat = top["cat"]
        first = min(m["published"] for m in news)
        img = next((m["image"] for m in sorted(news, key=lambda m: -(m["score"] or 0)) if m["image"]), None)
        pts = []
        if hit:
            tp = min(40, round(10 * math.log10(max(hit[1], 10))))
            if len(hit[3]) == 1: tp //= 2
            pts.append(("🔎", f"Хората търсят „{hit[0]}“: {hit[2]} търсения в Google днес" + (" (тренд от една дума — половин точки)" if len(hit[3]) == 1 else ""), tp))
        if len(recent) >= 2: pts.append(("📈", f"Расте: {len(recent)} източника я пуснаха в последните {S('compass_fresh_h')} ч", min(25, 5 * len(recent))))
        if len(srcs) >= 2: pts.append(("📰", f"Голяма: {len(srcs)} източника, {len(cov)} медийни групи я отразяват", min(30, 3 * len(cov) + len(srcs))))
        if miss and hit: pts.append(("🤫", f"Хората я търсят, а {len(miss)} от {len(exp)} медии мълчат", round(15 * len(miss) / len(exp))))
        best = max(((((m["likes"] or 0) + 3 * (m["comments"] or 0)) / igm[m["source_id"]], m) for m in ms
                    if m["skind"] == "instagram" and m["likes"] is not None and m["source_id"] in igm), default=None, key=lambda x: x[0])
        if best and best[0] >= 2:
            pts.append(("💬", f"Гърми в Instagram: {best[1]['src']} има {best[0]:.1f}× повече реакции от обичайното "
                              f"({best[1]['likes']} ❤ · {best[1]['comments']} 💬)", min(25, round(6 * best[0]))))
        votes = [m["verdict"] for m in ms if m["verdict"]]
        if "wouldnt" in votes and "would" not in votes: pts.append(("👎", "Маркирал си я с ▼ (не бихме)", -20))
        elif "would" in votes: pts.append(("👍", "Маркирал си я с ▲ (бихме я покрили)", 10))
        lw = lcat.get(cat, 1.0)
        if abs(lw - 1) >= 0.1: pts.append(("🎯" if lw > 1 else "↓", f"Твоите избори: {'често' if lw > 1 else 'рядко'} взимаш „{cat}“", round(10 * (lw - 1) / 0.6)))
        age_h = (now - first) / 3600
        decay = 1 if age_h <= 6 else max(0.4, 1 - (age_h - 6) / 36)
        if decay < 1: pts.append(("⏳", f"Стара: първата статия е от преди {age_h:.0f} ч", -round(sum(p[2] for p in pts) * (1 - decay))))
        total = sum(p[2] for p in pts)
        if total <= 0 and not hit: continue
        done = cid in posted or any(m["id"] in posted_items for m in ms)
        level = "done" if done else ("now" if total >= 45 else "watch" if total >= 25 else "low")
        stories.append(dict(id=cid, item=top["id"], ids=[m["id"] for m in ms], topics={t for m in news for t in (m["topics"] or "").split(",") if t}, title=top["title_bg"] or top["title"], cat=cat, img=img, pts=pts, total=total,
                            level=level, n=len(news), srcs=sorted({m["src"] for m in news}), link=top["link"],
                            ago=f"{age_h:.0f} ч" if age_h >= 1 else f"{age_h * 60:.0f} мин", miss=miss[:6], miss_all=miss, nmiss=len(miss)))
    stories.sort(key=lambda k: -k["total"])
    gaps = [dict(term=t, raw=raw, n=n) for t, n, raw, tt in sorted(trend_t, key=lambda x: -x[1]) if tt and t.lower() not in used_trends][:15]
    return stories, gaps

@app.get("/admin/trends", response_class=HTMLResponse)
def trends(request: Request, cat: str = "", show: str = ""):
    stories, gaps = compass_data()
    if cat: stories = [k for k in stories if k["cat"] == cat or cat in k["topics"]]
    groups = {lv: [k for k in stories if k["level"] == lv] for lv in ("now", "watch", "low", "done")}
    with db() as c: raw = c.execute("select * from trends order by fetched desc, id limit 100").fetchall()
    return render("trends", request, g=groups, gaps=gaps, raw=raw, cat=cat, cats=categories(), show=show,
                  colors=cat_colors(), txt=_txt_on, fresh=S("compass_fresh_h"), hours=S("compass_hours"))

@app.post("/admin/quick/{iid}")
async def quick_post(iid: int):
    """⚡ Бърз пост: чернова с най-добрата снимка от историята + AI текст за визията (ако има ключ) → направо в редактора."""
    with db() as c:
        i = c.execute("select * from items where id=?", (iid,)).fetchone()
        mates = c.execute("select summary from items where cluster=? and id!=? and coalesce(summary,'')!='' order by score desc limit 2",
                          (i["cluster"], iid)).fetchall() if i["cluster"] is not None else []
        body = "\n\n".join([i["summary"] or ""] + [m[0] for m in mates]).strip()
        refs = c.execute("select s.name, i.link, i.published from items i join sources s on s.id=i.source_id where "
                         + ("i.cluster=?" if i["cluster"] is not None else "i.id=?") + " order by i.published limit ?",
                         (i["cluster"] if i["cluster"] is not None else iid, S("post_sources_max", int))).fetchall()
        if refs: body += "\n\nИзточници:\n" + "\n".join(f"— {n} ({datetime.fromtimestamp(t).strftime('%d.%m %H:%M')}): {l}" for n, l, t in refs)
        pid = c.execute("insert into posts(item_id,title,body,image,created) values(?,?,?,?,?)",
                        (iid, i["title_bg"] or i["title"], body, i["image"], time.time())).lastrowid
        c.execute("update items set status='draft' where id=?", (iid,)); add_signal(c, iid, "quick")
    post = {"id": pid, "item_id": iid, "image": i["image"], "title": i["title_bg"] or i["title"], "body": body}
    img = best_image(post)
    hook, sub = "", ""
    try: hook, sub = await ai_hook(post)
    except Exception as ex: log(f"Бърз пост: AI текст грешка {ex}")
    with db() as c: c.execute("update posts set image=?, hook=?, sub=? where id=?", (img, hook, sub, pid))
    return back(f"/admin/post/{pid}", "⚡ Бърз пост: провери текста и снимката, после „Публикувай“")

@app.post("/admin/note/{iid}")
async def item_note(iid: int, request: Request):
    f = await request.form()
    v = f.get("verdict", "")
    with db() as c:
        c.execute("insert into item_notes(ts,item_id,verdict,reasons,text) values(?,?,?,?,?)",
                  (time.time(), iid, v, ", ".join(f.getlist("reasons")), (f.get("text") or "").strip()))
        if v in ("would", "wouldnt"):
            c.execute("update items set verdict=? where id=?", (v, iid))
            add_signal(c, iid, v)
    score_all()
    ref = request.headers.get("referer", "/admin")
    return RedirectResponse(ref if "/admin" in ref else "/admin", 303)

@app.get("/admin/logic", response_class=HTMLResponse)
def logic_page(request: Request):
    """Логиката зад рейтинга — всичко, което влиза в оценката, и какво е научено от бележките."""
    with db() as c:
        lsrc, lcat = learned_weights(c)
        words = learned_words(c)
        names = dict(c.execute("select id, name from sources").fetchall())
        notes = [dict(r) | {"when": when(r["ts"])} for r in c.execute(
            "select n.*, i.title, i.title_bg, i.link from item_notes n left join items i on i.id=n.item_id order by n.ts desc limit 60")]
        reasons = {}
        for (rs,) in c.execute("select reasons from item_notes where coalesce(reasons,'')!=''"):
            for r in rs.split(", "): reasons[r] = reasons.get(r, 0) + 1
        nsig = c.execute("select count(*) from signals").fetchone()[0] + c.execute("select count(*) from item_notes").fetchone()[0]
    W = [(k, S(f"w_{k}"), d) for k, d in [("recency", "колко е прясна (полуразпад recency_halflife_h часа)"),
         ("trend", "съвпада с Google тренд"), ("cross", "колко други източника я имат (до 3)"), ("keyword", "има дума от boost_keywords"),
         ("silence", "хората я търсят, а очаквани медии мълчат"), ("balance", "балансира наклона на лентата ни"),
         ("learn", "думи, които си харесвал/отхвърлял в бележките")]]
    wl = sorted(words.items(), key=lambda x: -x[1][0])
    return render("logic", request, W=W, lsrc=sorted(((names.get(k, k), v) for k, v in lsrc.items()), key=lambda x: -x[1]),
                  lcat=sorted(lcat.items(), key=lambda x: -x[1]), wplus=wl[:30], wminus=wl[::-1][:30], notes=notes,
                  reasons=sorted(reasons.items(), key=lambda x: -x[1]), nsig=nsig, thr=S("recommend_threshold"))

@app.get("/admin/source/{sid}", response_class=HTMLResponse)
def source_profile(sid: int, request: Request):
    since7, since30 = time.time() - 7 * 86400, time.time() - 30 * 86400
    with db() as c:
        s_ = c.execute("select * from sources where id=?", (sid,)).fetchone()
        if not s_: return HTMLResponse("Няма такъв източник", 404)
        st = dict(n7=c.execute("select count(*) from items where source_id=? and published>?", (sid, since7)).fetchone()[0],
                  n30=c.execute("select count(*) from items where source_id=? and published>?", (sid, since30)).fetchone()[0],
                  rec=c.execute("select count(*) from items where source_id=? and published>? and status in ('recommended','draft')", (sid, since30)).fetchone()[0],
                  avg=c.execute("select round(avg(score),1) from items where source_id=? and published>?", (sid, since30)).fetchone()[0],
                  posts=c.execute("select count(*) from posts p join items i on i.id=p.item_id where i.source_id=?", (sid,)).fetchone()[0],
                  img=c.execute("select round(100.0*sum(coalesce(image,'')!='')/max(count(*),1)) from items where source_id=? and published>?", (sid, since30)).fetchone()[0])
        sig = dict(c.execute("select action, count(*) from signals where source_id=? group by 1", (sid,)).fetchall())
        lsrc, _ = learned_weights(c)
        notes = [dict(n) | {"when": when(n["ts"])} for n in c.execute(
            "select n.*, i.title, i.link from media_notes n left join items i on i.id=n.item_id where n.source_id=? order by n.ts desc", (sid,))]
        inotes = [dict(n) | {"when": when(n["ts"])} for n in c.execute(
            "select n.*, i.title, i.link from item_notes n join items i on i.id=n.item_id where i.source_id=? order by n.ts desc limit 30", (sid,))]
        recent = [dict(r) | {"when": when(r["published"])} for r in c.execute(
            "select * from items where source_id=? order by published desc limit 20", (sid,))]
    return render("source", request, s=dict(s_), st=st, sig=sig, lw=lsrc.get(sid, 1.0), notes=notes, inotes=inotes, recent=recent)

@app.post("/admin/source/{sid}/note")
def source_note(sid: int, text: str = Form(...), kind: str = Form("note"), item_id: int = Form(0)):
    with db() as c:
        s_ = c.execute("select name, owner from sources where id=?", (sid,)).fetchone()
        if text.strip(): c.execute("insert into media_notes(ts,owner,text,item_id,kind,source_id) values(?,?,?,?,?,?)",
                                   (time.time(), s_["owner"] or s_["name"], text.strip(), item_id or None, kind, sid))
    return back(f"/admin/source/{sid}", "Бележката е записана")

@app.get("/admin/story/{iid}", response_class=HTMLResponse)
def story_page(iid: int, request: Request):
    """Една история от Компаса: всички статии, кой мълчи и какво значи всяка точка."""
    stories, _ = compass_data(hours=72)
    k = next((x for x in stories if iid in x["ids"]), None)
    if not k: return back("/admin/trends", "Историята вече не е в Компаса (по-стара от 72 ч или отхвърлена)")
    with db() as c:
        q = ",".join("?" * len(k["ids"]))
        arts = [dict(r) | {"when": when(r["published"])} for r in c.execute(
            f"select i.*, s.name src, s.owner, s.category cat, s.id sid from items i join sources s on s.id=i.source_id "
            f"where i.id in ({q}) order by i.published", k["ids"])]
        all_groups = [r[0] for r in c.execute("select distinct owner from sources where owner!='' and active=1 order by 1")]
    return render("story", request, k=k, arts=arts, all_groups=all_groups)

@app.get("/admin/trend", response_class=HTMLResponse)
def trend_page(request: Request, term: str = ""):
    """Тренд от Google → кои наши новини са за него (по думи в заглавието)."""
    tt = toks(term) - TREND_STOP
    with db() as c:
        tr = c.execute("select traffic, max(fetched) f from trends where lower(term)=lower(?)", (term,)).fetchone()
        rows = [dict(r) | {"when": when(r["published"])} for r in c.execute(
            "select i.*, s.name src, s.category cat, s.id sid from items i join sources s on s.id=i.source_id "
            "where i.published>? order by i.published desc limit 8000", (time.time() - 72 * 3600,))]
    hits = []
    for r in rows:
        ov = len(tt & toks((r["title"] or "") + " " + (r["title_bg"] or "")))
        if tt and ov >= max(1, (len(tt) + 1) // 2): hits.append((ov, r))
    hits.sort(key=lambda x: (-x[0], -x[1]["published"]))
    return render("trend", request, term=term, traffic=tr["traffic"] if tr else "", hits=[h[1] for h in hits[:60]])

@app.get("/admin/instagram", response_class=HTMLResponse)
def ig_page(request: Request):
    with db() as c:
        rows = [dict(r) for r in c.execute("select * from sources where kind='instagram' order by active desc, name")]
        h, errs = source_health(c)
        igm = ig_medians(c)
        for r in rows:
            r["n7"] = h.get(r["id"], {}).get("n7", 0); r["err"] = errs.get(r["name"], ""); r["med"] = igm.get(r["id"])
    tok = S("ig_token")
    kind = "" if not tok else ("Instagram Login (може да публикува, но НЕ може да следи чужди профили)" if tok.startswith("IG") else "Facebook Login ✓")
    return render("instagram", request, rows=rows, cats=categories(), app_id=S("fb_app_id"), has_secret=bool(S("fb_app_secret")),
                  uid=S("ig_user_id"), uname=S("ig_username"), kind=kind, exp=S("ig_token_expires"), every=S("ig_every_min"))

@app.post("/admin/instagram/connect")
async def ig_connect(app_id: str = Form(""), app_secret: str = Form(""), token: str = Form("")):
    """Кратък токен от Graph API Explorer → дълъг (60 дни) → намира Instagram профила, вързан за Facebook страницата."""
    if app_id.strip(): _set("fb_app_id", app_id.strip())
    if app_secret.strip(): _set("fb_app_secret", app_secret.strip())
    token = token.strip()
    if not token: return back("/admin/instagram", "Постави токена от Graph API Explorer")
    async with httpx.AsyncClient(base_url="https://graph.facebook.com/v23.0", timeout=30) as c:
        if S("fb_app_id") and S("fb_app_secret"):
            r = await c.get("/oauth/access_token", params={"grant_type": "fb_exchange_token", "client_id": S("fb_app_id"),
                                                          "client_secret": S("fb_app_secret"), "fb_exchange_token": token})
            if r.status_code < 300 and r.json().get("access_token"):
                token = r.json()["access_token"]
                exp = r.json().get("expires_in")
                _set("ig_token_expires", datetime.fromtimestamp(time.time() + exp).strftime("%d.%m.%Y") if exp else "")
        r = await c.get("/me/accounts", params={"fields": "id,name,instagram_business_account{id,username}", "access_token": token})
        if r.status_code >= 300: return back("/admin/instagram", f"Facebook отказа: {r.text[:200]}")
        pages = [p for p in r.json().get("data", []) if p.get("instagram_business_account")]
    if not pages: return back("/admin/instagram", "Не намерих Instagram Business/Creator профил, вързан за твоя Facebook страница")
    ig = pages[0]["instagram_business_account"]
    _set("ig_user_id", ig["id"]); _set("ig_username", ig.get("username", "")); _set("ig_token", token)
    return back("/admin/instagram", f"Свързан @{ig.get('username', '')} (страница {pages[0]['name']})")

@app.post("/admin/instagram/add")
def ig_add(handles: str = Form(...), category: str = Form(""), owner: str = Form("")):
    """Добавя профили за следене: по един на ред или със запетая. Нищо съществуващо не се трие."""
    names = [h.strip().lstrip("@").split("/")[-1] for h in re.split(r"[\n,]", handles) if h.strip()]
    added = 0
    with db() as c:
        have = {r[0].lower() for r in c.execute("select url from sources where kind='instagram'")}
        for h in names:
            if h.lower() in have: continue
            c.execute("insert into sources(name,kind,url,weight,active,owner,category,every_min,lang) values(?,?,?,?,?,?,?,?,?)",
                      (f"@{h}", "instagram", h, 0.8, 1, owner.strip() or f"@{h}", category, S("ig_every_min", float), "bg")); added += 1
    return back("/admin/instagram", f"Добавени {added} профила (вече ги имаше: {len(names) - added})")

@app.post("/admin/instagram/fetch")
async def ig_fetch_now():
    with db() as c: srcs = c.execute("select * from sources where kind='instagram' and active=1").fetchall()
    async with httpx.AsyncClient() as cl:
        for s_ in srcs:
            try: await fetch_instagram(cl, s_)
            except Exception as ex: log(f"{s_['name']}: грешка {ex}")
    score_all()
    return back("/admin/instagram", f"Изтеглени {len(srcs)} профила — виж колоната „Работи?“")

@app.get("/admin/canva/setup", response_class=HTMLResponse)
async def canva_setup(request: Request):
    tok = await canva_tok() if S("canva_client_id") else None
    return render("canva_setup", request, cid=S("canva_client_id"), has_secret=bool(S("canva_client_secret")), connected=bool(tok),
                  tpl=S("canva_template_id"), redirect=CANVA_REDIRECT, scopes=CANVA_SCOPES)

@app.post("/admin/canva/setup")
def canva_setup_save(client_id: str = Form(""), client_secret: str = Form("")):
    if client_id.strip(): _set("canva_client_id", client_id.strip())
    if client_secret.strip(): _set("canva_client_secret", client_secret.strip())
    return back("/admin/canva/setup", "Записано. Сега натисни „Свържи Canva“.")

@app.post("/admin/verdict/{iid}/{v}")
def verdict(iid: int, v: str, request: Request):
    """👍 бихме я покрили (остава + учи) · 👎 не бихме (скрива + учи)."""
    if v in ("would", "wouldnt"):
        with db() as c:
            c.execute("update items set verdict=? where id=?", (v, iid))
            add_signal(c, iid, v)
    ref = request.headers.get("referer", "/admin")
    return RedirectResponse(ref if "/admin" in ref else "/admin", 303)

# ---------- Пропусната новина: линк → защо я нямаме → кои медии я покриват → добави източник
def _domain(u):
    from urllib.parse import urlparse
    d = urlparse(u or "").netloc.lower()
    return d[4:] if d.startswith("www.") else d

OG_TITLE = re.compile(r'<meta[^>]+property=["\']og:title["\'][^>]*?content=["\']([^"\']+)', re.I)
OG_SITE = re.compile(r'<meta[^>]+property=["\']og:site_name["\'][^>]*?content=["\']([^"\']+)', re.I)

async def analyze_missed(url, note=""):
    import html as _h
    now = time.time()
    res = {"url": url, "covered_by": [], "have": None, "why": ""}
    async with httpx.AsyncClient(headers=UA, follow_redirects=True, timeout=15) as cl:
        if "news.google.com" in url: url = await gnews_url(cl, url) or url
        res["url"] = url
        title = site = img = ""
        try:
            r = await cl.get(url); t = r.text[:300_000]
            m = OG_TITLE.search(t) or re.search(r"<title[^>]*>([^<]+)", t, re.I); title = _h.unescape(m.group(1)).strip() if m else ""
            m = OG_SITE.search(t); site = _h.unescape(m.group(1)).strip() if m else ""
            m = OG_IMG.search(t) or OG_IMG2.search(t); img = urljoin(str(r.url), m.group(1)) if m else ""
        except Exception as ex: res["why"] = f"страницата не се отвори ({ex}); "
        dom = _domain(url)
        title = re.sub(r"\s+[-|–—]\s+[^-|–—]{2,40}$", "", title)
        res.update(title=title, site=site or dom, domain=dom, image=img)
        bg = bool(re.search("[а-яА-Я]", title))
        res["lang"] = "bg" if bg else "en"
        # кои медии я покриват: Google News търсене по заглавието
        words = [w for w in WORD.findall(title) if len(w) > 3][:8]
        if words:
            q = quote(" ".join(words))
            gurl = (f"https://news.google.com/rss/search?q={q}+when:3d&hl=bg&gl=BG&ceid=BG:bg" if bg
                    else f"https://news.google.com/rss/search?q={q}+when:3d&hl=en-US&gl=US&ceid=US:en")
            try:
                feed = feedparser.parse((await cl.get(gurl)).content)
                seen = set()
                for e in feed.entries[:25]:
                    src = e.get("source") or {}
                    d = _domain(src.get("href", ""))
                    if d and d not in seen:
                        seen.add(d); res["covered_by"].append({"name": src.get("title") or d, "domain": d,
                                                               "title": re.sub(r"\s+-\s+[^-]{2,40}$", "", e.title)})
            except Exception as ex: res["why"] += f"Google News търсенето не стана ({ex}); "
    if dom and dom not in {x["domain"] for x in res["covered_by"]}:
        res["covered_by"].insert(0, {"name": res["site"], "domain": dom, "title": title})
    with db() as c:
        srcs = c.execute("select id, name, url, active from sources").fetchall()
        for x in res["covered_by"]:
            hit = next((s for s in srcs if x["domain"] and x["domain"] in (s["url"] or "")), None)
            x["have"] = dict(id=hit["id"], name=hit["name"], active=hit["active"]) if hit else None
        # имаме ли я вече? по линк или по заглавие
        it = c.execute("select i.*, s.name src, s.active sact from items i join sources s on s.id=i.source_id where i.link=?", (url,)).fetchone()
        if not it and title:
            tt = toks(title)
            for r in c.execute("select i.*, s.name src, s.active sact from items i join sources s on s.id=i.source_id "
                               "where i.published>? order by i.published desc limit 6000", (now - 7 * 86400,)):
                if len(tt & toks((r["title"] or "") + " " + (r["title_bg"] or ""))) >= max(3, len(tt) // 2): it = r; break
        if it:
            res["have"] = dict(id=it["id"], title=it["title"], src=it["src"], score=it["score"], status=it["status"])
            thr = S("recommend_threshold", float)
            res["why"] += (f"Имахме я от {it['src']}, но е отхвърлена." if it["status"] == "rejected" else
                           f"Имахме я от {it['src']} с резултат {it['score']} (прагът е {thr:.0f}) — оценката е била ниска."
                           if (it["score"] or 0) < thr else f"Имахме я от {it['src']} като препоръчана — просто не е взета.")
        else:
            have_any = [x for x in res["covered_by"] if x["have"] and x["have"]["active"]]
            res["why"] += ("Нито един от нашите източници не я е пуснал." if not have_any else
                           f"Имаме {', '.join(x['have']['name'] for x in have_any)}, но новината не е дошла — фийдът може да изпуска новини.")
        mid = c.execute("insert into missed(ts,url,title,domain,note,found_item,json) values(?,?,?,?,?,?,?)",
                        (now, url, title, dom, note, it["id"] if it else None, json.dumps(res, ensure_ascii=False))).lastrowid
        if it: add_signal(c, it["id"], "missed")
    return mid

@app.get("/admin/missed", response_class=HTMLResponse)
def missed_page(request: Request, id: int = 0):
    with db() as c:
        rows = [dict(r) | {"when": when(r["ts"]), "d": json.loads(r["json"] or "{}")} for r in
                c.execute("select * from missed order by ts desc limit 50")]
    cur = next((r for r in rows if r["id"] == id), None)
    week = sum(1 for r in rows if r["ts"] > time.time() - 7 * 86400)
    return render("missed", request, rows=rows, cur=cur, cats=categories(), week=week)

@app.post("/admin/missed")
async def missed_add(url: str = Form(...), note: str = Form("")):
    url = url.strip()
    if not url.startswith("http"): return back("/admin/missed", "Постави цял линк (https://...)")
    mid = await analyze_missed(url, note)
    return back(f"/admin/missed?id={mid}", "Анализирано")

@app.post("/admin/missed/{mid}/source")
def missed_source(mid: int, domain: str = Form(...), name: str = Form(""), category: str = Form(""), lang: str = Form("bg"),
                  sid: int = Form(0)):
    """Добавя медията като източник (Google News feed за сайта) или включва вече съществуващ, изключен източник."""
    with db() as c:
        if sid:
            c.execute("update sources set active=1 where id=?", (sid,)); msg = "Източникът е включен отново"
        else:
            url = (GN if lang == "bg" else GNE).format(domain)
            c.execute("insert into sources(name,kind,url,weight,active,owner,category,every_min,lang) values(?,?,?,?,?,?,?,?,?)",
                      (name or domain, "rss", url, 0.8, 1, name or domain, category, 15, lang))
            msg = f"Добавен източник: {name or domain}"
        c.execute("update missed set status='fixed' where id=?", (mid,))
    log(f"Пропусната новина #{mid}: {msg}")
    return back(f"/admin/missed?id={mid}", msg + " — ще се тегли при следващия цикъл")

@app.post("/admin/missed/{mid}/draft")
def missed_draft(mid: int):
    """Новината отива направо в черновите (през източник „Ръчно добавени“)."""
    with db() as c:
        m = c.execute("select * from missed where id=?", (mid,)).fetchone(); d = json.loads(m["json"] or "{}")
        if m["found_item"]: iid = m["found_item"]
        else:
            sid = c.execute("select id from sources where kind='manual'").fetchone()
            sid = sid[0] if sid else c.execute("insert into sources(name,kind,url,weight,active,category) values"
                                               "('Ръчно добавени','manual','manual',1,0,'')").lastrowid
            c.execute("insert or ignore into items(source_id,title,link,summary,image,published,fetched) values(?,?,?,?,?,?,?)",
                      (sid, d.get("title") or m["url"], m["url"], "", d.get("image") or None, time.time(), time.time()))
            iid = c.execute("select id from items where link=?", (m["url"],)).fetchone()[0]
        p = c.execute("select * from items where id=?", (iid,)).fetchone()
        pid = c.execute("insert into posts(item_id,title,body,image,created) values(?,?,?,?,?)",
                        (iid, p["title_bg"] or p["title"], p["summary"], p["image"], time.time())).lastrowid
        c.execute("update items set status='draft' where id=?", (iid,))
    return back(f"/admin/post/{pid}", "Пропуснатата новина е в черновите")

# ---------- Профили на медиите: обхват (ръчен) + какво показват данните + бележки, трупани докато работим
def media_stats(owner, days=30):
    """От данните: по категория — какъв дял от „големите“ истории (≥3 групи) медията е отразила; скорост; първа ли е."""
    since = time.time() - days * 86400
    with db() as c:
        rows = c.execute("select i.cluster, i.published, s.owner, coalesce(s.category,'') cat from items i join sources s on s.id=i.source_id "
                         "where i.published>? and i.cluster is not null and s.owner!=''", (since,)).fetchall()
    by = {}
    for r in rows: by.setdefault(r["cluster"], []).append(r)
    cats, delays, first, total = {}, [], 0, 0
    for ms in by.values():
        owners = {m["owner"] for m in ms}
        if len(owners) < 3: continue
        cnt = {}
        for m in ms: cnt[m["cat"]] = cnt.get(m["cat"], 0) + 1
        cat = max(cnt, key=cnt.get)
        d = cats.setdefault(cat, [0, 0]); d[1] += 1
        mine = [m["published"] for m in ms if m["owner"] == owner]
        if mine:
            d[0] += 1; total += 1
            t0 = min(m["published"] for m in ms)
            if min(mine) - t0 <= 24 * 3600: delays.append((min(mine) - t0) / 60)   # по-късно = вече друга новина
            if min(mine) <= t0 + 60: first += 1
    tab = sorted(((cat, k, n, k / n) for cat, (k, n) in cats.items() if n >= 3), key=lambda x: -x[3])
    delays.sort()
    return dict(cats=tab, median_delay=delays[len(delays) // 2] if delays else None, first=first, covered=total)

TIERS = ["агенция", "разследваща", "пробив", "усилвател", "последовател", "нишова", "чужда"]

def pyramid(days=30):
    """Медийната пирамида от данните. За всяка група: оригиналност (първа с >10 мин), влияние (колко групи я следват
    нетно до 6 ч след нея), обхват (колко големи истории покрива), скорост. Плюс зависимости A → B."""
    since = time.time() - days * 86400
    with db() as c:
        rows = c.execute("select i.cluster, i.published, s.owner, coalesce(s.lang,'bg') lang from items i join sources s on s.id=i.source_id "
                         "where i.published>? and i.cluster is not null and s.owner!=''", (since,)).fetchall()
        tiers = dict(c.execute("select owner, coalesce(tier,'') from media").fetchall())
    by = {}
    for r in rows: by.setdefault(r["cluster"], []).append(r)
    first, cov, lag, edges, lang, n_big = {}, {}, {}, {}, {}, 0
    for ms in by.values():
        t = {}
        for m in ms:
            t[m["owner"]] = min(t.get(m["owner"], 1e18), m["published"]); lang[m["owner"]] = m["lang"]
        if len(t) < 3: continue
        n_big += 1
        order = sorted(t.items(), key=lambda x: x[1]); t0 = order[0][1]
        if order[1][1] - t0 > 600: first[order[0][0]] = first.get(order[0][0], 0) + 1
        for o, tt in order:
            cov[o] = cov.get(o, 0) + 1
            if tt - t0 <= 86400: lag.setdefault(o, []).append((tt - t0) / 60)
        for i, (a, ta) in enumerate(order):
            for b, tb in order[i + 1:]:
                if 600 < tb - ta <= 6 * 3600: edges[(a, b)] = edges.get((a, b), 0) + 1
    deps = [(a, b, n, edges.get((b, a), 0)) for (a, b), n in edges.items() if n >= 5 and n >= 2 * edges.get((b, a), 0)]
    deps.sort(key=lambda x: -(x[2] - x[3]))
    infl = {}
    for a, b, n, back in deps: infl[a] = infl.get(a, 0) + 1; infl[b] = infl.get(b, 0) - 1
    med = {o: sorted(v)[len(v) // 2] for o, v in lag.items() if v}
    mx = lambda d: max(d.values()) if d else 1
    out = []
    for o in cov:
        orig = first.get(o, 0) / cov[o]
        score = 100 * (0.35 * min(orig / 0.3, 1) + 0.25 * max(0, infl.get(o, 0)) / max(1, mx(infl))
                       + 0.25 * cov[o] / mx(cov) + 0.15 * (1 - min(med.get(o, 600), 600) / 600))
        tier = tiers.get(o) or ("чужда" if lang.get(o) != "bg" else "пробив" if orig >= 0.2 and infl.get(o, 0) > 0
                                else "последовател" if infl.get(o, 0) < -1 else "усилвател" if cov[o] >= 0.5 * mx(cov) else "нишова")
        out.append(dict(owner=o, cov=cov[o], first=first.get(o, 0), orig=orig, infl=infl.get(o, 0), lag=med.get(o),
                        score=round(score), tier=tier, manual=bool(tiers.get(o))))
    out.sort(key=lambda x: -x["score"])
    return dict(media=out, deps=deps[:60], n_big=n_big, days=days)

EXTRACT_PROMPT = """Ти си помощник на редакция. За всяка новина по-долу извади САМО това, което е изрично казано в текста.
Не предполагай, не тълкувай, не добавяй знания отвън. Ако няма нищо — празни списъци.
- promises: конкретно обещание на назован човек или институция („ще…“, „обещавам…“, „до края на годината…“). who, what (до 20 думи), deadline (ако е казан, иначе "").
- stances: назован човек/партия/институция заема публична позиция към друг човек, партия, институция или мярка. actor, toward, stance = "за" | "против".
- actors: назованите хора, партии и институции (до 6).
Върни САМО JSON масив, по един обект на новина: [{"i":0,"promises":[],"stances":[],"actors":[]}]

Новини:
"""

async def extract_batch(rows):
    """Една заявка към Claude за до 10 новини → обещания, позиции, споменавания. Всичко влиза като „за проверка“."""
    txt = "\n".join(f"[{n}] ({r['src']}) {r['title_bg'] or r['title']} — {(r['summary'] or '')[:500]}" for n, r in enumerate(rows))
    out = await ask_claude(EXTRACT_PROMPT + txt, max_tokens=3000, timeout=240)
    data = json.loads(out[out.index("["):out.rindex("]") + 1])
    now, n_p, n_s = time.time(), 0, 0
    with db() as c:
        for d in data:
            try: r = rows[int(d.get("i", -1))]
            except (ValueError, IndexError): continue
            for p in d.get("promises") or []:
                if p.get("who") and p.get("what"):
                    c.execute("insert into promises(who,what,said_on,deadline,status,source_url,media,ts,item_id) values(?,?,?,?,?,?,?,?,?)",
                              (p["who"][:80], p["what"][:200], datetime.fromtimestamp(r["published"]).strftime("%d.%m.%Y"),
                               (p.get("deadline") or "")[:40], "за проверка", r["link"], r["src"], now, r["id"])); n_p += 1
            for st in d.get("stances") or []:
                if st.get("actor") and st.get("toward") and st.get("stance") in ("за", "против"):
                    c.execute("insert into mentions(item_id,actor,toward,stance,ts) values(?,?,?,?,?)",
                              (r["id"], st["actor"][:80], st["toward"][:80], 1 if st["stance"] == "за" else -1, now))
                    c.execute("insert into relations(subj,rel,obj,note,source_url,source_date,confidence,ts,item_id) values(?,?,?,?,?,?,?,?,?)",
                              (st["actor"][:80], "подкрепя публично" if st["stance"] == "за" else "атакува публично", st["toward"][:80],
                               f"Автоматично от {r['src']}: {(r['title_bg'] or r['title'])[:100]}", r["link"],
                               datetime.fromtimestamp(r["published"]).strftime("%d.%m.%Y"), "за проверка", now, r["id"])); n_s += 1
            for a in (d.get("actors") or [])[:6]:
                c.execute("insert into mentions(item_id,actor,toward,stance,ts) values(?,?,?,?,?)", (r["id"], str(a)[:80], "", 0, now))
        c.executemany("update items set extracted=1 where id=?", [(r["id"],) for r in rows])
    return n_p, n_s

_ALERTED = set()
async def alert_now():
    """История стигна прага → съобщение в личния ти Telegram чат: заглавие, защо, източници. Веднъж на история."""
    if S("alert_telegram") != "on" or not (S("telegram_bot_token") and S("alert_chat_id")): return
    stories, _ = compass_data()
    thr = S("alert_threshold", float)
    async with httpx.AsyncClient(timeout=20) as cl:
        for k in stories:
            if k["level"] == "done" or k["total"] < thr or k["id"] in _ALERTED: continue
            _ALERTED.add(k["id"])
            why = "\n".join(f"{ic} {t} ({p:+d})" for ic, t, p in k["pts"])
            text = (f"🔥 <b>{k['total']} т.</b> — {k['title']}\n\n{why}\n\n📰 {', '.join(k['srcs'][:5])}\n"
                    f"{k['link']}\n\nОтвори: {PUBLIC_URL}/admin/story/{k['item']}")
            r = await cl.post(f"https://api.telegram.org/bot{S('telegram_bot_token')}/sendMessage",
                              data={"chat_id": S("alert_chat_id"), "text": text[:4000], "parse_mode": "HTML", "disable_web_page_preview": "true"})
            log(f"Известие: {k['title'][:60]} → {'ок' if r.status_code < 300 else r.text[:120]}")

async def extract_run():
    if not claude_ready(): return "Claude не е свързан (виж Настройки)"
    cats = [x.strip() for x in S("extract_categories").split(",") if x.strip()]
    with db() as c:
        rows = c.execute(f"select i.*, s.name src from items i join sources s on s.id=i.source_id where coalesce(i.extracted,0)=0 "
                         f"and coalesce(s.lang,'bg')='bg' and s.category in ({','.join('?' * len(cats))}) and i.published>? "
                         "order by i.score desc limit ?", (*cats, time.time() - 3 * 86400, S("extract_per_run", int))).fetchall()
    tp = ts = 0; errs = []
    for k in range(0, len(rows), 10):
        try: p, s_ = await extract_batch(rows[k:k + 10]); tp += p; ts += s_
        except Exception as ex: errs.append(str(ex)[:120])
    msg = f"Прочетени {len(rows)} новини: {tp} обещания и {ts} позиции за проверка" + (f" · грешки: {'; '.join(errs)}" if errs else "")
    log(f"Извличане: {msg}")
    return msg

def promoted(days=30, min_n=3):
    """Кой кого промотира: за всяка медийна група — за кои назовани хора/партии нейните новини съобщават позиции „за“ и „против“.
    Автоматично и грубо: показва посока, не доказва намерение."""
    since = time.time() - days * 86400
    with db() as c:
        rows = c.execute("select s.owner, m.toward, sum(m.stance>0), sum(m.stance<0), count(*) from mentions m "
                         "join items i on i.id=m.item_id join sources s on s.id=i.source_id where m.ts>? and m.stance!=0 "
                         "and s.owner!='' group by s.owner, m.toward having count(*)>=? order by count(*) desc", (since, min_n)).fetchall()
        top = c.execute("select s.owner, m.actor, count(*) n from mentions m join items i on i.id=m.item_id "
                        "join sources s on s.id=i.source_id where m.ts>? and m.stance=0 and s.owner!='' "
                        "group by s.owner, m.actor order by n desc limit 300", (since,)).fetchall()
    by, most = {}, {}
    for o, t, plus, minus, n in rows: by.setdefault(o, []).append((t, plus, minus))
    for o, a, n in top:
        if len(most.setdefault(o, [])) < 5: most[o].append((a, n))
    return by, most

@app.post("/admin/map/extract")
async def extract_route():
    return back("/admin/map#check", await extract_run())

@app.post("/admin/map/check/{kind}/{rid}/{v}")
def check_route(kind: str, rid: int, v: str):
    """✓ / ✕ на автоматично извадено обещание или позиция."""
    with db() as c:
        if kind == "promise": c.execute("update promises set status=? where id=?", ("чака" if v == "ok" else "отхвърлено", rid))
        elif kind == "relation": c.execute("update relations set confidence=? where id=?", ("по медии" if v == "ok" else "отхвърлено", rid))
    return back("/admin/map#check")

@app.get("/admin/map", response_class=HTMLResponse)
def media_map(request: Request, who: str = ""):
    pyr = pyramid()
    with db() as c:
        rels = [dict(r) for r in c.execute("select * from relations where confidence not in ('за проверка','отхвърлено') order by subj, rel")]
        proms = [dict(r) for r in c.execute("select * from promises where status not in ('за проверка','отхвърлено') order by ts desc limit 200")]
        chk_p = [dict(r) for r in c.execute("select * from promises where status='за проверка' order by ts desc limit 60")]
        chk_r = [dict(r) for r in c.execute("select * from relations where confidence='за проверка' and item_id is not null order by ts desc limit 60")]
    if who: rels = [r for r in rels if who.lower() in (r["subj"] + " " + r["obj"]).lower()]
    levels = {t: [m for m in pyr["media"] if m["tier"] == t] for t in TIERS}
    prom_by, most = promoted()
    return render("map", request, p=pyr, levels=levels, tiers=TIERS, rels=rels, proms=proms, who=who, owner_map=SEED_OWNER_MAP,
                  chk_p=chk_p, chk_r=chk_r, prom_by=prom_by, most=most, ready=claude_ready())

@app.post("/admin/map/relation")
def add_relation(subj: str = Form(...), rel: str = Form(...), obj: str = Form(...), note: str = Form(""),
                 source_url: str = Form(""), source_date: str = Form(""), confidence: str = Form("потвърдено")):
    if not source_url.startswith("http"): return back("/admin/map", "Връзка без източник не се записва — добави линк")
    with db() as c: c.execute("insert into relations(subj,rel,obj,note,source_url,source_date,confidence,ts) values(?,?,?,?,?,?,?,?)",
                              (subj.strip(), rel.strip(), obj.strip(), note.strip(), source_url.strip(), source_date.strip(), confidence, time.time()))
    return back("/admin/map#rel", "Връзката е записана")

@app.post("/admin/map/promise")
def add_promise(who: str = Form(...), what: str = Form(...), said_on: str = Form(""), deadline: str = Form(""),
                source_url: str = Form(""), media: str = Form(""), status: str = Form("чака"), pid: int = Form(0)):
    with db() as c:
        if pid: c.execute("update promises set status=? where id=?", (status, pid))
        else:
            if not source_url.startswith("http"): return back("/admin/map#prom", "Обещание без източник не се записва")
            c.execute("insert into promises(who,what,said_on,deadline,status,source_url,media,ts) values(?,?,?,?,?,?,?,?)",
                      (who.strip(), what.strip(), said_on, deadline, status, source_url.strip(), media.strip(), time.time()))
    return back("/admin/map#prom", "Записано")

@app.get("/admin/map.csv")
def map_csv(part: str = "media"):
    """CSV за Sheet-а: media | deps | relations | promises."""
    import csv, io
    buf = io.StringIO(); w = csv.writer(buf)
    if part == "media":
        w.writerow(["Група", "Роля", "Важност", "Големи истории", "Първа", "Оригиналност %", "Влияние", "Закъснение мин"])
        for m in pyramid()["media"]:
            w.writerow([m["owner"], m["tier"], m["score"], m["cov"], m["first"], round(m["orig"] * 100), m["infl"], round(m["lag"] or 0)])
    elif part == "deps":
        w.writerow(["Пуска първа", "Следва я", "Пъти", "Обратно"])
        for a, b, n, back_ in pyramid()["deps"]: w.writerow([a, b, n, back_])
    else:
        with db() as c:
            rows = c.execute("select subj,rel,obj,note,source_url,source_date,confidence from relations" if part == "relations"
                             else "select who,what,said_on,deadline,status,source_url,media from promises").fetchall()
        w.writerow(["Кой", "Връзка", "С кого/какво", "Бележка", "Източник", "Дата", "Сигурност"] if part == "relations"
                   else ["Кой", "Обещание", "Казано на", "Срок", "Статус", "Източник", "Медия"])
        w.writerows(rows)
    return HTMLResponse(buf.getvalue(), media_type="text/csv; charset=utf-8")

@app.get("/admin/media", response_class=HTMLResponse)
def media_list(request: Request):
    since = time.time() - 30 * 86400
    with db() as c:
        rows = c.execute(
            "select s.owner, count(distinct s.id) nsrc, group_concat(distinct s.category) cats, "
            "(select count(*) from items i join sources s2 on s2.id=i.source_id where s2.owner=s.owner and i.published>?) n30, "
            "(select count(*) from media_notes n where n.owner=s.owner) nnotes, "
            "(select text from media_notes n where n.owner=s.owner order by ts desc limit 1) last, "
            "(select coalesce(topics,'') from media m where m.owner=s.owner) topics "
            "from sources s where s.owner!='' and s.active=1 group by s.owner order by nnotes desc, n30 desc", (since,)).fetchall()
    return render("media_list", request, rows=rows)

@app.get("/admin/media/{owner}", response_class=HTMLResponse)
def media_profile(owner: str, request: Request, note: str = "", item: int = 0):
    with db() as c:
        prof = c.execute("select * from media where owner=?", (owner,)).fetchone()
        srcs = c.execute("select * from sources where owner=? order by active desc, name", (owner,)).fetchall()
        notes = [dict(n) | {"when": when(n["ts"])} for n in c.execute(
            "select n.*, i.title, i.link from media_notes n left join items i on i.id=n.item_id where n.owner=? order by n.ts desc limit 100", (owner,))]
        recent = [dict(r) | {"when": when(r["published"])} for r in c.execute(
            "select i.*, s.name src, s.category cat from items i join sources s on s.id=i.source_id where s.owner=? order by i.published desc limit 15", (owner,))]
    with db() as c: fixes = c.execute("select category, action, count(*) from silence_fix where owner=? group by 1,2", (owner,)).fetchall()
    return render("media", request, fixes=fixes, fix_min=S("silence_fix_min"), owner_hint=SEED_OWNER_MAP.get(owner, owner), owner=owner, prof=dict(prof) if prof else {}, srcs=srcs, notes=notes, recent=recent,
                  st=media_stats(owner), cats=categories(), note=note, item=item)

@app.post("/admin/media/{owner}")
async def media_save(owner: str, request: Request):
    f = await request.form()
    with db() as c:
        c.execute("insert into media(owner,region,topics,about,updated,tier) values(?,?,?,?,?,?) on conflict(owner) do update set "
                  "region=excluded.region, topics=excluded.topics, about=excluded.about, updated=excluded.updated, tier=excluded.tier",
                  (owner, f.get("region", ""), ",".join(f.getlist("topics")), f.get("about", "").strip(), time.time(), f.get("tier", "")))
    score_all()
    return back(f"/admin/media/{quote(owner)}", "Профилът е запазен — тишината вече се смята по него")

@app.post("/admin/media-note")
def media_note(request: Request, owner: str = Form(...), text: str = Form(...), item_id: int = Form(0), kind: str = Form("note"),
               back_to: str = Form("")):
    if text.strip():
        with db() as c: c.execute("insert into media_notes(ts,owner,text,item_id,kind) values(?,?,?,?,?)",
                                  (time.time(), owner, text.strip(), item_id or None, kind))
    dest = back_to if back_to.startswith("/admin") else f"/admin/media/{quote(owner)}"
    return back(dest, f"Бележката за {owner} е записана")

@app.post("/admin/skip/{iid}")
def skip_story(iid: int):
    """✕ в Компаса: цялата история отпада + сигнал, че такива не ти трябват."""
    with db() as c:
        i = c.execute("select cluster from items where id=?", (iid,)).fetchone()
        add_signal(c, iid, "wouldnt"); c.execute("update items set verdict='wouldnt' where id=?", (iid,))
        if i and i[0] is not None: c.execute("update items set status='rejected' where cluster=? and status in ('new','recommended')", (i[0],))
        else: c.execute("update items set status='rejected' where id=?", (iid,))
    return back("/admin/trends", "Скрито. Ще виждаш по-малко такива.")

# ---------- обратна връзка + подобрения
@app.post("/admin/feedback")
def feedback(request: Request, kind: str = Form("feature"), ref: str = Form(""), page: str = Form(""),
             text: str = Form(""), rating: int = Form(0)):
    if text.strip() or rating:
        with db() as c: c.execute("insert into feedback(ts,kind,ref,page,rating,text) values(?,?,?,?,?,?)",
                                  (time.time(), kind, ref, page, rating, text.strip()))
    dest = page if page.startswith("/admin") else "/admin"
    return back(dest, "Благодаря — записано в „Подобри“")

SECRET_KEYS = ("token", "_pass", "password", "api_key", "secret")   # не „pass“ — иначе хваща и „compass“
async def claude_improve():
    with db() as c:
        fb = c.execute("select * from feedback where status='new' order by ts desc limit 200").fetchall()
        st = {k: v for k, v in c.execute("select key, value from settings") if not k.startswith("_") and not any(x in k for x in SECRET_KEYS)}
        sig = [tuple(r) for r in c.execute("select coalesce(category,''), action, count(*) from signals where ts>? group by 1,2",
                                           (time.time() - 30 * 86400,))]
    lines = "\n".join(f"- #{f['id']} [{f['kind']}] {f['page'] or ''} {'👍' if f['rating'] > 0 else '👎' if f['rating'] < 0 else ''} {f['text'] or ''}"
                      f"{(' (снимка: ' + f['ref'][:80] + ')') if f['kind'] == 'image' else ''}{(' | Въпрос: ' + f['claude_q'] + ' | Отговор: ' + f['answer']) if f['answer'] else ''}" for f in fb)
    prompt = (f"Ти оптимизираш новинарската система „{S('site_name')}“. Цел: максимално бързо постване на правилните новини.\n"
              f"Коментари от редакцията:\n{lines or '(няма)'}\n\nИзбори за 30 дни (категория, действие, брой): {sig}\n\n"
              f"Текущи настройки (JSON): {json.dumps(st, ensure_ascii=False)}\n\n"
              "Върни САМО JSON: {\"settings\": {ключ: нова стойност като текст}, \"why\": {ключ: защо, 1 изречение}, "
              "\"code\": [идеи за промени в кода, които не стават с настройки, по 1 изречение], "
              "\"questions\": {\"номер на коментара\": въпрос към редактора}}. "
              "Ако коментар е неясен или има няколко тълкувания, НЕ предлагай промяна по него — задай въпрос в questions. "
              "Променяй само съществуващи ключове. Не измисляй коментари, които ги няма.")
    txt = await ask_claude(prompt, max_tokens=2000, timeout=240)
    m = re.search(r"\{.*\}", txt, re.S)
    data = json.loads(m.group(0)) if m else {"settings": {}, "why": {}, "code": [txt[:500]]}
    data["settings"] = {k: str(v) for k, v in (data.get("settings") or {}).items() if k in st}
    with db() as c:
        for fid, q in (data.get("questions") or {}).items():
            fid = re.sub(r"\D", "", str(fid))
            if fid: c.execute("update feedback set claude_q=?, answer=null, status='чака изясняване' where id=?", (str(q)[:500], int(fid)))
        c.execute("insert into proposals(ts,json) values(?,?)", (time.time(), json.dumps(data, ensure_ascii=False)))

def speed_stats():
    """Колко бързо постваме: от първата статия за историята до нашата публикация."""
    with db() as c:
        rows = c.execute("select p.published - (select min(j.published) from items j where j.cluster=i.cluster) "
                         "from posts p join items i on i.id=p.item_id where p.status='published' and p.published>?",
                         (time.time() - 30 * 86400,)).fetchall()
    d = sorted(r[0] for r in rows if r[0] is not None and r[0] > 0)
    return (d[len(d) // 2] / 60, len(d)) if d else (None, 0)

@app.get("/admin/improve", response_class=HTMLResponse)
def improve(request: Request):
    with db() as c:
        fb = [dict(f) | {"when": when(f["ts"])} for f in c.execute("select * from feedback order by status='new' desc, ts desc limit 200")]
        pr = c.execute("select * from proposals where status='new' order by ts desc limit 1").fetchone()
        sig = c.execute("select coalesce(category,''), sum(action in ('draft','quick','publish')), sum(action='reject') "
                        "from signals where ts>? group by 1 order by 2 desc", (time.time() - S("learn_window_days", float) * 86400,)).fetchall()
        cur = dict(c.execute("select key, value from settings").fetchall())
        _, lcat = learned_weights(c)
    prop = json.loads(pr["json"]) if pr else None
    med, n = speed_stats()
    return render("improve", request, fb=fb, prop=prop, pid=pr["id"] if pr else None, cur=cur, sig=sig, lcat=lcat, med=med, npub=n)

@app.post("/admin/claude-test")
async def claude_test():
    try:
        t = await ask_claude("Отговори само с: работи", max_tokens=20, fast=True, timeout=90)
        return back("/admin/settings", f"Claude ({claude_how()}) отговори: {t[:60]}")
    except Exception as ex: return back("/admin/settings", f"Claude: {ex}")

@app.post("/admin/improve/run")
async def improve_run():
    try: await claude_improve(); return back("/admin/improve", "Claude предложи промени — прегледай ги")
    except Exception as ex: return back("/admin/improve", f"Грешка: {ex}")

@app.post("/admin/improve/apply/{pid}")
async def improve_apply(pid: int, request: Request):
    form = await request.form()
    keys = form.getlist("k")
    with db() as c:
        pr = c.execute("select json from proposals where id=?", (pid,)).fetchone()
        data = json.loads(pr[0]) if pr else {}
        for k in keys:
            if k in data.get("settings", {}) and not any(x in k for x in SECRET_KEYS):
                c.execute("update settings set value=? where key=?", (data["settings"][k], k))
        c.execute("update proposals set status='done' where id=?", (pid,))
        if keys: c.execute("update feedback set status='applied' where status='new'")
    if data.get("code"):
        with open(Path(__file__).with_name("IMPROVE.md"), "a", encoding="utf-8") as f:
            f.write(f"\n## {datetime.now():%d.%m.%Y %H:%M} — идеи за кода (от Подобри)\n" + "\n".join(f"- {x}" for x in data["code"]) + "\n")
    score_all()
    return back("/admin/improve", f"Приложени {len(keys)} настройки. Идеите за код са в IMPROVE.md")

@app.post("/admin/feedback/{fid}/answer")
def feedback_answer(fid: int, answer: str = Form(...)):
    """Отговор на въпроса на Claude → коментарът пак е „new“ и влиза в следващото предложение с отговора."""
    with db() as c: c.execute("update feedback set answer=?, status='new' where id=?", (answer.strip(), fid))
    return back("/admin/improve", "Отговорът е записан — натисни „Claude: предложи промени“ отново")

@app.post("/admin/feedback/{fid}/{st}")
def feedback_status(fid: int, st: str):
    if st in ("new", "applied", "dismissed", "чака изясняване"):
        with db() as c: c.execute("update feedback set status=? where id=?", (st, fid))
    return back("/admin/improve")

@app.get("/admin/improve/export")
def improve_export():
    """Всички коментари като текст — за поставяне в Claude, когато подобряваме кода."""
    with db() as c: fb = c.execute("select * from feedback where status='new' order by kind, ts").fetchall()
    txt = "# Коментари от редакцията\n\n" + "\n".join(f"- [{f['kind']}] {f['page'] or ''} {f['rating']:+d} {f['text'] or ''} {f['ref'] if f['kind'] == 'image' else ''}" for f in fb)
    return HTMLResponse(txt, media_type="text/plain; charset=utf-8")

@app.get("/admin/profiles", response_class=HTMLResponse)
def profiles(request: Request):
    with db() as c: rows = c.execute("select * from profiles").fetchall()
    return render("profiles", request, rows=rows)

@app.post("/admin/profiles/new")
def add_profile(platform: str = Form(...), handle: str = Form(...)):
    with db() as c: c.execute("insert into profiles(platform,handle) values(?,?)", (platform, handle.lstrip("@")))
    return back("/admin/profiles")

@app.post("/admin/profiles/check")
async def check_profiles():
    with db() as c: rows = c.execute("select * from profiles where platform='instagram'").fetchall()
    for p in rows:
        try: j = await ig_check_profile(p["handle"])
        except Exception as ex: j = {"error": str(ex)}
        with db() as c: c.execute("update profiles set last_json=?,checked=? where id=?",
                                  (json.dumps(j, ensure_ascii=False, indent=1)[:4000], time.time(), p["id"]))
    return back("/admin/profiles", "Проверено (TikTok профилите са само линкове — няма официален API за чужди профили)")

@app.get("/admin/settings", response_class=HTMLResponse)
def settings(request: Request):
    with db() as c: rows = [(r[0], r[1]) for r in c.execute("select key,value from settings where key!='session_secret' order by key")]
    how = claude_how()
    status = {"api": "през API ключ", "cli": f"през Claude Code CLI ({claude_cli()}) — без ключ, с твоя абонамент",
              None: "не е свързан — няма ключ и не намирам `claude`. Задай claude_cli_path или anthropic_api_key"}[how]
    return render("settings", request, claude_status=status, rows=rows)

@app.post("/admin/settings")
async def save_settings(request: Request):
    form = await request.form()
    with db() as c:
        for k, v in form.items(): c.execute("update settings set value=? where key=?", (v, k))
    score_all()
    r = back("/admin/settings", "Запазено")
    return set_session(r)   # ако паролата е сменена, текущият браузър остава вписан

FRAMING = {}

@app.get("/admin/bias", response_class=HTMLResponse)
def bias(request: Request, cat: str = "", q: str = "", only: str = "", lang: str = ""):
    drift, n = feed_drift()
    with db() as c:
        gmap, gmin = silence_groups(c), S("silence_min_groups", int)
        rows = c.execute("select i.*, s.owner, s.lean, coalesce(s.category,'') cat, coalesce(s.lang,'bg') lang from items i "
                         "join sources s on s.id=i.source_id "
                         "where i.cluster is not null and i.status in ('new','recommended','draft') and i.published>?",
                         (time.time() - 72 * 3600,)).fetchall()
        fixes = all_fixes(c)
    by = {}
    for r in rows: by.setdefault(r["cluster"], []).append(r)
    stories, ql = [], q.strip().lower()
    for cid, ms in by.items():
        cats = {m["cat"] for m in ms}
        bg = any(m["lang"] == "bg" for m in ms)
        if cat and cat not in cats: continue
        if lang == "bg" and not bg or lang == "intl" and bg: continue
        if ql and not any(ql in (m["title"] or "").lower() or ql in (m["title_bg"] or "").lower() for m in ms): continue
        top = max(ms, key=lambda m: m["score"] or 0)
        leans = [m["lean"] or 0 for m in ms]
        cov = {m["owner"] for m in ms if m["owner"]}
        rm, add = fixes_for(fixes, [m["id"] for m in ms])
        exp = (expected_groups(gmap, ms) - rm) | add
        ok = len(exp) >= max(2, gmin)
        miss = sorted(exp - cov) if ok else []
        sil = max(m["silence"] or 0 for m in ms)
        if only == "silent" and not miss: continue
        if only == "trend" and not sil: continue
        stories.append(dict(id=cid, item=top["id"], scat=top["cat"], title=top["title_bg"] or top["title"], n=len(ms), cov=sorted(cov), miss=miss,
                            nexp=len(exp), ncov=len(cov & exp), ok=ok, cats=sorted(cats), bg=bg,
                            spread=max(leans) - min(leans), silence=sil, framing=FRAMING.get(cid), rank=(top["score"] or 0)))
    stories.sort(key=lambda k: (-k["silence"], -len(k["miss"]), -k["n"], -k["rank"]))
    def url(**kw):
        d = {"cat": cat, "q": q, "only": only, "lang": lang} | kw
        return "/admin/bias?" + "&".join(f"{k}={quote(str(v))}" for k, v in d.items() if v)
    with db() as c: all_groups = [r[0] for r in c.execute("select distinct owner from sources where owner!='' and active=1 order by 1")]
    return render("bias", request, all_groups=all_groups, stories=stories[:80], total=len(stories), drift=drift, n=n, days=S("balance_window_days"),
                  axis=S("lean_axis"), cat=cat, q=q, only=only, lang=lang, cats=categories(), url=url,
                  colors=cat_colors(), txt=_txt_on)

@app.post("/admin/silence-fix/{iid}")
async def silence_fix(iid: int, request: Request):
    """Ръчна корекция на мълчанието в една история: ✕ = не е мълчание · + = трябваше да отрази."""
    f = await request.form()
    owner, act = (f.get("owner") or "").strip(), f.get("action")
    if owner and act in ("not_silent", "should_cover"):
        with db() as c:
            r = c.execute("select coalesce(s.category,'') from items i join sources s on s.id=i.source_id where i.id=?", (iid,)).fetchone()
            c.execute("insert into silence_fix(ts,item_id,owner,category,action) values(?,?,?,?,?)", (time.time(), iid, owner, r[0] if r else "", act))
            c.execute("insert into media_notes(ts,owner,text,item_id,kind) values(?,?,?,?,?)",
                      (time.time(), owner, "Не е мълчание — не се очаква да отрази такава история" if act == "not_silent"
                       else "Трябваше да отрази тази история, а мълчи", iid, "strange" if act == "should_cover" else "note"))
        score_all()
    ref = request.headers.get("referer", "/admin/bias")
    return RedirectResponse(ref if "/admin" in ref else "/admin/bias", 303)

@app.post("/admin/framing/{cid}")
async def framing(cid: int):
    try: FRAMING[cid] = await claude_framing(cid)
    except Exception as ex: FRAMING[cid] = f"Грешка: {ex}"
    return back("/admin/bias")

@app.get("/admin/log", response_class=HTMLResponse)
def logs(request: Request):
    with db() as c: rows = [dict(r) | {"when": when(r["ts"])} for r in c.execute("select * from log order by id desc limit 200")]
    return render("log", request, rows=rows)

# ---------------------------------------------------------------- Демо данни (без интернет)
def seed_demo():
    now = time.time()
    with db() as c:
        if c.execute("select count(*) from items").fetchone()[0]: return
        sid = {r["name"]: r["id"] for r in c.execute("select id,name from sources")}
        demo = [
            ("NOVA", "Бургас кандидатства за домакин на фен зони за Евровизия 2027", 1),
            ("БНТ (през Google News)", "Евровизия 2027: Бургас подготвя фен зони и национални пространства", 2),
            ("NOVA", "Цените на горивата растат за трета поредна седмица", 3),
            ("Reuters World (през Google News)", "EU leaders meet in Brussels over defence spending", 2),
            ("БНТ (през Google News)", "Нов хороскоп за седмицата", 5),
            ("NOVA", "Футбол: Черноморец Бургас с важна победа", 8),
            ("БНР English", "Bulgaria fuel prices rise again", 4),
        ]
        for i, (s, t, h) in enumerate(demo):
            c.execute("insert into items(source_id,title,link,summary,image,published,fetched) values(?,?,?,?,?,?,?)",
                      (sid[s], t, f"https://example.com/demo/{i}", "Демо резюме на новината.",
                       f"https://picsum.photos/seed/n{i}/600/400", now - h * 3600, now))
        c.execute("insert into sources(name,kind,url,weight,owner) values('X: Бургас','x','Бургас lang:bg -is:retweet has:media',0.6,'')")
        xs = c.execute("select id from sources where kind='x'").fetchone()[0]
        c.execute("insert into items(source_id,title,link,summary,image,published,fetched) values(?,?,?,?,?,?,?)",
                  (xs, "Бургас и Евровизия 2027: фен зоните ще са на Морското казино? (снимка)", "https://x.com/demo/status/1",
                   "@demo · ♥120 ↻30\nЛинкове: https://example.com/burgas-eurovision", "https://picsum.photos/seed/x1/600/400", now - 1800, now))
        # ДЕМО стойности на наклона — измислени, само за да се види механиката
        c.execute("update sources set lean=-0.4 where name='NOVA'"); c.execute("update sources set lean=0.3 where name like 'БНТ%'")
        for term in ["Евровизия", "цени горива", "Черноморец"]:
            c.execute("insert into trends(term,traffic,fetched) values(?,?,?)", (term, "2000+", now))
        c.execute("insert into posts(item_id,title,body,image,media_url,status,created,published) values(1,?,?,?,?,?,?,?)",
                  ("Добре дошли в демото", "Това е примерна публикация с видео плейър.",
                   "https://picsum.photos/seed/w/600/400", "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
                   "published", now, now))
    score_all(); log("Демо данни заредени")

if __name__ == "__main__" and "--test" in sys.argv:      # python app.py --test → таблица в терминала
    init_db()
    for r in asyncio.run(test_sources()):
        print(f"{'OK ' if r['ok'] else 'ERR'} {r['name'][:32]:32} {str(r.get('code','?')):>4} записи:{r['n']:>3} снимки:{r['img']:>3}  {(r['last'] or r['err'])[:70]}")
    sys.exit()

if __name__ == "__main__":
    try:
        import resource
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        resource.setrlimit(resource.RLIMIT_NOFILE, (min(8192, hard if hard > 0 else 8192), hard))
    except Exception: pass
    uvicorn.run(app, host=HOST, port=PORT)
