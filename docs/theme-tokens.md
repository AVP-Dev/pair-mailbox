# Design token contract (theme tokens)

> Language: this is the English original. The Russian mirror is [theme-tokens.ru.md](./theme-tokens.ru.md).

> **Where this file comes from.** Copied unchanged from `agent-starter-kit/docs/theme-tokens.md` —
> the repository that `docs/project-structure.md` §2 cites directly as the
> source of the scale. A copy, not a link: the contract that the layout
> (`mcp/pair_mailbox/assets/board.css`) stands on must sit next to that layout, otherwise
> "no agreement" looks like "a different agreement".
>
> **Who follows it.** `mcp/pair_mailbox/assets/board.css` declares the §1 set
> in `:root` and in `[data-theme="dark"]` (plus auxiliary `--ok/--warn/--bad/--unknown`
> for statuses and `--gap/--pad/--row` for layout — they are outside the contract and permitted
> for the layout). Divergence: the contract requires `--input` (input field borders, §1),
> but `board.css` neither declares nor uses it — the dashboard renders no
> input fields. The code requires only 7 tokens (`REQUIRED_TOKENS`, `board_render.py:44`:
> `--background --foreground --card --muted --border --destructive --primary`),
> the test — zero hardcoded colors plus declaredness of every `var(--x)`
> (`test_board_render.py:15`). The main
> invariant is the same: **Zero Hardcoded Colors in Components** — not a single
> `#hex`/`rgb()` outside the token declaration block.

# Design token and visual theme contract (Theme Tokens Specification)

> **Purpose of this document:** this file defines a single standard of semantic tokens for interfaces.
> Main invariant: **Zero Hardcoded Colors in Components**.
> All components are laid out exclusively on semantic variables. Adding a new theme or changing the visual style is done by declaring a new token set **without changing component code**.

---

## 1. Base semantic token contract

All themes (light, dark, alternative palettes) must implement the full set of the following CSS variables:

```css
:root {
  /* Surfaces and background */
  --background: 0 0% 100%;             /* Main page background */
  --foreground: 240 10% 3.9%;           /* Main text color */
  --card: 0 0% 100%;                   /* Card and module background */
  --card-foreground: 240 10% 3.9%;      /* Text on cards */
  --popover: 0 0% 100%;                /* Dropdown and tooltip background */
  --popover-foreground: 240 10% 3.9%;   /* Text in popups */

  /* Primary and accent actions */
  --primary: 240 5.9% 10%;             /* Primary CTA button / accent */
  --primary-foreground: 0 0% 98%;      /* Text on the Primary button */
  --secondary: 240 4.8% 95.9%;         /* Secondary button / badge */
  --secondary-foreground: 240 5.9% 10%;/* Secondary element text */
  --accent: 240 4.8% 95.9%;            /* Accent interactive hover */
  --accent-foreground: 240 5.9% 10%;   /* Text on accent */

  /* Muted elements and borders */
  --muted: 240 4.8% 95.9%;             /* Muted background (tables, inputs) */
  --muted-foreground: 240 3.8% 46.1%;  /* Secondary muted text / placeholder */
  --border: 240 5.9% 90%;              /* Card borders, separators */
  --input: 240 5.9% 90%;               /* Input field borders */
  --ring: 240 5.9% 10%;                /* Focus ring (:focus-visible) */

  /* Destructive actions */
  --destructive: 0 84.2% 60.2%;        /* Errors, deletion, dangerous actions */
  --destructive-foreground: 0 0% 98%;  /* Text on the dangerous button */

  /* Border radii */
  --radius: 0.5rem;                    /* Base rounding radius of buttons and cards */
}

/* Dark theme (Dark Minimalist by default) */
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

## 2. How to add new themes (Zero Component Edits)

To add a new theme (e.g. `Nord`, `Cyberpunk`, `Corporate Blue`), it suffices to declare a new selector in the project's theme CSS file:

```css
/* Example: Scandinavian palette (Nord Theme) */
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

**Components do not change.** Theme switching happens by simply toggling the attribute:
```html
<html data-theme="nord">
```

---

## 3. Mapping to Tailwind CSS / frameworks

In the Tailwind config file (`tailwind.config.ts` or v4 CSS variables):

```typescript
// Example of integrating tokens into Tailwind v3/v4:
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

## 4. Layout invariants for the AI agent

1. ❌ **Forbidden:** `<div className="bg-[#121212] text-gray-400">`
2. ✅ **Required:** `<div className="bg-card text-muted-foreground border border-border">`
3. ❌ **Forbidden:** `<button className="bg-blue-600 hover:bg-blue-700">`
4. ✅ **Required:** `<button className="bg-primary text-primary-foreground hover:bg-primary/90 focus-visible:ring-2 focus-visible:ring-ring">`
5. ❌ **Forbidden:** clickable `div` with a click listener.
6. ✅ **Required:** semantic `<button>` with an explicit `type="button"`.
