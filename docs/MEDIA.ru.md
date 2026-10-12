# Руководство по медиа и демонстрации pair-mailbox

Этот документ описывает визуальные материалы, спецификацию скриншотов и автоматическую запись терминальных демонстраций (GIF/VHS) для документации, README на GitHub и презентаций проекта.

---

## 🎨 Векторная графика

Репозиторий включает готовые векторные SVG-материалы, адаптированные для светлой и тёмной тем GitHub:

- [`assets/pair-mailbox-banner.svg`](../assets/pair-mailbox-banner.svg): Главный баннер проекта с обзором архитектуры и ключевыми инвариантами.
- [`assets/pair-flow-diagram.svg`](../assets/pair-flow-diagram.svg): Диаграмма жизненного цикла раунда от скоупинга до pull request.

---

## 📸 Спецификация рекомендуемых скриншотов

Для наглядного представления работы pair-mailbox рекомендуется сделать 4 скриншота и сохранить их в каталог `assets/screenshots/`:

### 1. Двухпанельная сессия в tmux (`assets/screenshots/pair-split-session.png`)
*Демонстрирует совместную работу двух независимых CLI-агентов в одном окне tmux без ручного вмешательства.*

- **Компоновка**: Горизонтальный сплит в `tmux` (две вертикальные панели).
- **Левая панель (Писатель / Writer)**:
  - Процесс: `opencode --standalone --auto` или `agy`.
  - Видимое содержимое: Запуск проверок (`verify-changed.sh`), применение запечатанного патча, создание коммита.
- **Правая панель (Критик / Critic)**:
  - Процесс: `codex` или `claude`.
  - Видимое содержимое: Анализ `git diff`, ревью кода с точными ссылками `file:line`, предложение патча через `pair_propose`.
- **Строка статуса**: Статус-бар tmux с именем сессии `pair` и рабочим каталогом.

### 2. Потоковый чат в реальном времени (`assets/screenshots/pair-chat-tui.png`)
*Показывает изолированный канал связи между агентами с цветовой дифференциацией ролей.*

- **Команда**:
  ```bash
  bin/pairchat
  ```
- **Видимое содержимое**:
  - Цветные теги авторов: зелёный `Chat from agy:`, жёлтый `Chat from codex:`.
  - Уведомления о резервировании патчей и вычислении SHA-256.
  - Подтверждение плана (plan-ack) и переход фазы.
  - Ограниченное по времени чтение без дедлоков.

### 3. Веб-дашборд реального времени (`assets/screenshots/pair-board-web.png`)
*Показывает полностью автономный HTML-дашборд с нулевыми зависимостями.*

- **Команда генерации**:
  ```bash
  bin/pairboard --once
  # Открыть в браузере:
  xdg-open board.html  # на Linux, либо open board.html на macOS
  ```
- **Видимое содержимое**:
  - Шапка: Текущий раунд, бейдж фазы (`implement` / `verify`), уровень автономии (`auto`).
  - Активные роли: Писатель, Критик (живые PID и панели).
  - Доска задач: Выполненные задачи с авторами и таймстемпами.
  - Супервизор: Статус процесса `pairpilot`, очередь обязательств, хвост транскрипта.

### 4. Диагностика окружения (`assets/screenshots/pair-doctor.png`)
*Демонстрирует мгновенную проверку установки и готовность системы.*

- **Команда**:
  ```bash
  bin/pairdoctor
  ```
- **Видимое содержимое**:
  - Зелёные отметки напротив установленных CLI (`opencode`, `agy`, `codex`, `claude`, `pi`, `omp`).
  - Проверка версии `tmux` и белого списка разрешений.
  - Корректность конфигураций MCP.

---

## 📹 Автоматические демо-записи (VHS / asciinema)

Для создания анимированных демонстраций (GIF/SVG) используется утилита [Charm VHS](https://github.com/charmbracelet/vhs).

### Скрипт VHS: `assets/demo.tape`

Создайте файл `assets/demo.tape`:

```vhs
Output assets/pair-demo.gif
Set FontSize 14
Set Width 1200
Set Height 680
Set Padding 20
Set Theme "Catppuccin Mocha"

Type "pairup --task 'fix retry jitter handling in client'"
Sleep 500ms
Enter

Sleep 4s

Type "bin/pairchat --once"
Sleep 500ms
Enter
Sleep 3s

Type "bin/pairctl why"
Sleep 500ms
Enter
Sleep 3s
```

Запуск рендеринга:
```bash
vhs assets/demo.tape
```

### Запись сессии через asciinema

Для интерактивной терминальной записи:

```bash
asciinema rec assets/pair-session.cast
pairup --task "prepare public release"
bin/pairchat
# Завершение по Ctrl+D
```
