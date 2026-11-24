# Обществото — Newsroom

Локално приложение за медията „Обществото“: тегли новини от ~100 източника (RSS, Google News, Reddit, Instagram, Google Trends, Wikipedia, GDELT), подрежда ги в **🧭 Компас** (коя история да хванеш сега и защо), прави чернови и визии (Canva) и публикува (сайт, Instagram, Threads, Telegram). Учи се от твоите бележки (▲ ▼ 📝).

Всичко е в един файл — `app.py` (FastAPI + SQLite). Данните и настройките (включително токени и пароли) са в `newsroom.db`, **която не е в GitHub**.

## Постоянна работа на друг Mac (напр. M1, 16 GB)

### 1. На стария Mac — спри го и вземи базата
```bash
cd ~/dev/projects/newsroom
./uninstall-service.sh 2>/dev/null; lsof -ti tcp:8000 | xargs kill 2>/dev/null   # спира приложението
```
Копирай `newsroom.db` на новия Mac (AirDrop, флашка или `scp`). В нея е всичко: новини, бележки, профили на медии, настройки, токени.
> Пускай приложението само на **един** Mac — иначе автопилотът и известията ще вървят двойно.

### 2. На новия Mac — веднъж
```bash
xcode-select --install                     # ако няма git/python3
git clone https://github.com/<твоят-акаунт>/obshtestvoto-newsroom.git ~/newsroom
cd ~/newsroom
# сложи копираната newsroom.db в тази папка
./setup.sh                                 # виртуална среда + пакети
./install-service.sh                       # работи постоянно, рестартира се само при срив
```
Отвори http://127.0.0.1:8000/admin.

### 3. Настройки на macOS, за да не спира
- **System Settings → Energy** (или Battery → Options): включи *Prevent automatic sleeping when the display is off* и *Start up automatically after a power failure*.
- **System Settings → Users & Groups → Automatically log in as** → твоя потребител (услугата тръгва при влизане). При включен FileVault автоматичното влизане не е налично — тогава влез ръчно след рестарт.
- **Claude**: инсталирай Claude Code и влез веднъж (`claude`, после `/login`). Приложението го намира само (или задай `claude_cli_path` в Настройки).
- **Canva**: свържи от самия Mac (адресът за връщане е `127.0.0.1`) — 🎨 Canva в менюто.

### 4. Достъп от телефона / лаптопа (по желание)
Най-лесно и сигурно: [Tailscale](https://tailscale.com) на двата Mac-а и телефона. После:
```bash
NEWSROOM_HOST=0.0.0.0 NEWSROOM_PUBLIC_URL=http://<tailscale-име>:8000 ./install-service.sh
```
Щом сайтът е отворен към мрежата, **паролата за админа се включва сама** (тя е в Настройки → `admin_password`).

## Ежедневна работа
| Какво | Команда |
| --- | --- |
| Нов код от GitHub + рестарт | `./update.sh` (прави копие на базата преди това) |
| Рестарт | `launchctl kickstart -k gui/$(id -u)/bg.obshtestvoto.newsroom` |
| Спиране | `./uninstall-service.sh` (нищо не се трие) |
| Лог | `tail -f logs/app.log` |
| Копие на базата | всеки ден в 04:00 в `backups/` (последните 14); ръчно: `./backup.sh` |
| Проверка на източниците | `.venv/bin/python app.py --test` |

## Променливи на средата
| Име | По подразбиране | Какво |
| --- | --- | --- |
| `NEWSROOM_HOST` | `127.0.0.1` | `0.0.0.0` = достъп от мрежата (паролата се включва) |
| `NEWSROOM_PORT` | `8000` | порт |
| `NEWSROOM_DB` | `./newsroom.db` | път до базата |
| `NEWSROOM_PUBLIC_URL` | `http://127.0.0.1:8000` | адрес в Telegram известията |
| `NEWSROOM_LOGIN` | `off` | `on` = парола и на 127.0.0.1 |

## Основни страници в админа
🧭 Компас · 🧠 Логика (как се смята оценката) · 🗺 Карта (пирамида, собственост, обещания) · 📇 Медии и профили на източници · Баланс (кой мълчи) · 🕳 Пропусната новина · 📸 Instagram · 🎨 Canva · Подобри (коментари → Claude предлага промени) · Настройки.

## Сигурност
- `newsroom.db`, копията и експортите са в `.gitignore` — **не ги качвай** (има токени и пароли).
- Repo-то в GitHub да е **Private**.
- Токенът за Instagram е за 60 дни — датата се вижда в 📸 Instagram.
