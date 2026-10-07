# Контракт дизайн-токенов (theme tokens)

> **Откуда файл.** Скопирован без изменений из `agent-starter-kit/docs/theme-tokens.md` —
> репозитория, на который `docs/project-structure.md` §2 прямо ссылается как на
> источник шкалы. Копия, а не ссылка: контракт, на котором стоит вёрстка
> (`mcp/pair_mailbox/assets/board.css`), обязан лежать рядом с той вёрсткой, иначе
> «соглашения нет» выглядит как «соглашение другое».
>
> **Кто его соблюдает.** `mcp/pair_mailbox/assets/board.css` объявляет набор §1
> в `:root` и в `[data-theme="dark"]` (плюс служебные `--ok/--warn/--bad/--unknown`
> для статусов и `--gap/--pad/--row` для раскладки — они вне контракта и вёрстке
> разрешены). Расхождение: контракт требует `--input` (граница полей ввода, §1),
> а `board.css` его не объявляет и не использует — дашборд не рендерит полей
> ввода. Код требует только 7 токенов (`REQUIRED_TOKENS`, `board_render.py:44`:
> `--background --foreground --card --muted --border --destructive --primary`),
> тест — ноль жёстких цветов плюс объявленность каждого `var(--x)`
> (`test_board_render.py:15`). Главный
> инвариант тот же: **Zero Hardcoded Colors in Components** — ни одного
> `#hex`/`rgb()` вне блока объявления токенов.

# Контракт дизайн-токенов и визуальных тем (Theme Tokens Specification)

> **Назначение документа:** Этот файл определяет единый стандарт семантических токенов для интерфейсов.
> Главный инвариант: **Zero Hardcoded Colors in Components**.
> Все компоненты верстаются исключительно на семантических переменных. Добавление новой темы или смена визуального стиля выполняется через объявление нового набора токенов **без изменения кода компонентов**.

---

## 1. Базовый контракт семантических токенов

Все темы (светлая, тёмная, альтернативные палитры) обязаны реализовывать полный набор следующих CSS-переменных:

```css
:root {
  /* Поверхности и фон */
  --background: 0 0% 100%;             /* Основной фон страницы */
  --foreground: 240 10% 3.9%;           /* Основной цвет текста */
  --card: 0 0% 100%;                   /* Фон карточек и модулей */
  --card-foreground: 240 10% 3.9%;      /* Текст на карточках */
  --popover: 0 0% 100%;                /* Фон выпадающих списков и тултипов */
  --popover-foreground: 240 10% 3.9%;   /* Текст в попапах */

  /* Первичные и акцентные действия */
  --primary: 240 5.9% 10%;             /* Primary CTA кнопка / акцент */
  --primary-foreground: 0 0% 98%;      /* Текст на Primary кнопке */
  --secondary: 240 4.8% 95.9%;         /* Второстепенная кнопка / бейдж */
  --secondary-foreground: 240 5.9% 10%;/* Текст второстепенного элемента */
  --accent: 240 4.8% 95.9%;            /* Акцентный интерактивный ховер */
  --accent-foreground: 240 5.9% 10%;   /* Текст на акценте */

  /* Приглушенные элементы и границы */
  --muted: 240 4.8% 95.9%;             /* Приглушенный фон (таблицы, инпуты) */
  --muted-foreground: 240 3.8% 46.1%;  /* Вторичный приглушенный текст / плейсхолдер */
  --border: 240 5.9% 90%;              /* Границы карточек, разделители */
  --input: 240 5.9% 90%;               /* Границы полей ввода */
  --ring: 240 5.9% 10%;                /* Фокусный контур (:focus-visible) */

  /* Деструктивные действия */
  --destructive: 0 84.2% 60.2%;        /* Ошибки, удаление, опасные действия */
  --destructive-foreground: 0 0% 98%;  /* Текст на опасной кнопке */

  /* Радиусы скругления */
  --radius: 0.5rem;                    /* Базовый радиус скругления кнопок и карточек */
}

/* Тёмная тема (Dark Minimalist по умолчанию) */
.dark, [data-theme="dark"] {
  --background: 240 10% 3.9%;
  --foreground: 0 0% 98%;
  --card: 240 10% 5.5%;
  --card-foreground: 0 0% 98%;
  --popover: 240 10% 5.5%;
  --popover-foreground: 0 0% 98%;
  --primary: 0 0% 98%;
  --primary-foreground: 240 5.9% 10%;
  --secondary: 240 3.7% 15.9%;
  --secondary-foreground: 0 0% 98%;
  --accent: 240 3.7% 15.9%;
  --accent-foreground: 0 0% 98%;
  --muted: 240 3.7% 15.9%;
  --muted-foreground: 240 5% 64.9%;
  --border: 240 3.7% 15.9%;
  --input: 240 3.7% 15.9%;
  --ring: 240 4.9% 83.9%;
  --destructive: 0 62.8% 30.6%;
  --destructive-foreground: 0 0% 98%;
}
```

---

## 2. Как добавлять новые темы (Zero Component Edits)

Чтобы подключить новую тему (например, `Nord`, `Cyberpunk`, `Corporate Blue`), достаточно объявить новый селектор в CSS-файле тем проекта:

```css
/* Пример: Скандинавская палитра (Nord Theme) */
[data-theme="nord"] {
  --background: 220 16% 22%;
  --foreground: 218 27% 94%;
  --card: 222 16% 28%;
  --card-foreground: 218 27% 94%;
  --popover: 222 16% 28%;
  --popover-foreground: 218 27% 94%;
  --primary: 193 43% 67%;
  --primary-foreground: 220 16% 22%;
  --secondary: 213 32% 52%;
  --secondary-foreground: 218 27% 94%;
  --accent: 193 43% 67%;
  --accent-foreground: 220 16% 22%;
  --muted: 220 17% 32%;
  --muted-foreground: 219 14% 71%;
  --border: 220 17% 32%;
  --input: 220 17% 32%;
  --ring: 193 43% 67%;
  --destructive: 354 42% 54%;
  --destructive-foreground: 0 0% 98%;
  --radius: 0.375rem;
}
```

**Компоненты не меняются.** Смена темы происходит простым переключением атрибута:
```html
<html data-theme="nord">
```

---

## 3. Маппинг в Tailwind CSS / фреймворки

В файле конфигурации Tailwind (`tailwind.config.ts` или CSS-переменные v4):

```typescript
// Пример интеграции токенов в Tailwind v3/v4:
colors: {
  background: "hsl(var(--background) / <alpha-value>)",
  foreground: "hsl(var(--foreground) / <alpha-value>)",
  card: {
    DEFAULT: "hsl(var(--card) / <alpha-value>)",
    foreground: "hsl(var(--card-foreground) / <alpha-value>)",
  },
  primary: {
    DEFAULT: "hsl(var(--primary) / <alpha-value>)",
    foreground: "hsl(var(--primary-foreground) / <alpha-value>)",
  },
  muted: {
    DEFAULT: "hsl(var(--muted) / <alpha-value>)",
    foreground: "hsl(var(--muted-foreground) / <alpha-value>)",
  },
  border: "hsl(var(--border) / <alpha-value>)",
}
```

---

## 4. Инварианты верстки для ИИ-агента

1. ❌ **Запрещено:** `<div className="bg-[#121212] text-gray-400">`
2. ✅ **Обязательно:** `<div className="bg-card text-muted-foreground border border-border">`
3. ❌ **Запрещено:** `<button className="bg-blue-600 hover:bg-blue-700">`
4. ✅ **Обязательно:** `<button className="bg-primary text-primary-foreground hover:bg-primary/90 focus-visible:ring-2 focus-visible:ring-ring">`
5. ❌ **Запрещено:** Кликабельный `div` со слушателем клика.
6. ✅ **Обязательно:** Семантический `<button>` с явным типом `type="button"`.
