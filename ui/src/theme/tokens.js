/**
 * Tailwind's design tokens, shaped for Mantine.
 *
 * Tailwind publishes eleven shades per palette (50, 100…900, 950); Mantine's
 * colour arrays hold exactly ten and index them 0–9. The mapping used here is
 * 50…900 → 0…9, which keeps Tailwind's `-600` at index 6 — the shade Tailwind
 * itself uses for interactive elements in light mode, and therefore the right
 * `primaryShade.light`. The 950 shades are not dropped: they are exported
 * separately and used to build Mantine's `dark` surface ramp.
 */

// prettier-ignore
export const slate = [
  '#f8fafc', '#f1f5f9', '#e2e8f0', '#cbd5e1', '#94a3b8',
  '#64748b', '#475569', '#334155', '#1e293b', '#0f172a',
]

// prettier-ignore
export const zinc = [
  '#fafafa', '#f4f4f5', '#e4e4e7', '#d4d4d8', '#a1a1aa',
  '#71717a', '#52525b', '#3f3f46', '#27272a', '#18181b',
]

// prettier-ignore
export const blue = [
  '#eff6ff', '#dbeafe', '#bfdbfe', '#93c5fd', '#60a5fa',
  '#3b82f6', '#2563eb', '#1d4ed8', '#1e40af', '#1e3a8a',
]

// prettier-ignore
export const emerald = [
  '#ecfdf5', '#d1fae5', '#a7f3d0', '#6ee7b7', '#34d399',
  '#10b981', '#059669', '#047857', '#065f46', '#064e3b',
]

// prettier-ignore
export const amber = [
  '#fffbeb', '#fef3c7', '#fde68a', '#fcd34d', '#fbbf24',
  '#f59e0b', '#d97706', '#b45309', '#92400e', '#78350f',
]

// prettier-ignore
export const red = [
  '#fef2f2', '#fee2e2', '#fecaca', '#fca5a5', '#f87171',
  '#ef4444', '#dc2626', '#b91c1c', '#991b1b', '#7f1d1d',
]

// prettier-ignore
export const orange = [
  '#fff7ed', '#ffedd5', '#fed7aa', '#fdba74', '#fb923c',
  '#f97316', '#ea580c', '#c2410c', '#9a3412', '#7c2d12',
]

// prettier-ignore
export const violet = [
  '#f5f3ff', '#ede9fe', '#ddd6fe', '#c4b5fd', '#a78bfa',
  '#8b5cf6', '#7c3aed', '#6d28d9', '#5b21b6', '#4c1d95',
]

// prettier-ignore
export const cyan = [
  '#ecfeff', '#cffafe', '#a5f3fc', '#67e8f9', '#22d3ee',
  '#06b6d4', '#0891b2', '#0e7490', '#155e75', '#164e63',
]

/** Tailwind's 950 shades, which the 50…900 mapping above has no slot for. */
export const deepest = {
  slate: '#020617',
  zinc: '#09090b',
  blue: '#172554',
  emerald: '#022c22',
  amber: '#451a03',
  red: '#450a0a',
}

/**
 * Mantine's `dark` array is not a palette — it is the dark scheme's surface
 * ramp, read from both ends: index 0 is body text, 2 is dimmed text, 4 is the
 * default border, 6 is a card and 7 the page background. Built here from
 * Tailwind's slate so dark mode is recognisably the same design as light.
 */
// prettier-ignore
export const dark = [
  slate[1],   // 0  body text          slate-100
  slate[2],   // 1                     slate-200
  slate[4],   // 2  dimmed text        slate-400
  slate[5],   // 3  placeholder        slate-500
  slate[7],   // 4  default border     slate-700
  '#253449',  // 5  hover              between slate-700 and -800
  slate[8],   // 6  card / input       slate-800
  slate[9],   // 7  page background    slate-900
  '#0b1220',  // 8                     between slate-900 and -950
  deepest.slate, // 9                  slate-950
]

/** Tailwind's spacing scale: `n` is `n * 0.25rem`. */
export const spacing = {
  '2xs': '0.25rem', // 1
  xs: '0.5rem', //    2
  sm: '0.75rem', //   3
  md: '1rem', //      4
  lg: '1.5rem', //    6
  xl: '2rem', //      8
  '2xl': '3rem', //  12
  '3xl': '4rem', //  16
}

/** Tailwind's `text-*` sizes. */
export const fontSizes = {
  xs: '0.75rem', //    text-xs
  sm: '0.875rem', //   text-sm
  md: '1rem', //       text-base
  lg: '1.125rem', //   text-lg
  xl: '1.25rem', //    text-xl
  '2xl': '1.5rem', //  text-2xl
  '3xl': '1.875rem', //text-3xl
}

/** The line-height Tailwind pairs with each of those sizes, as a ratio. */
export const lineHeights = {
  xs: '1.3333', // 1rem     / 0.75rem
  sm: '1.4286', // 1.25rem  / 0.875rem
  md: '1.5', //    1.5rem   / 1rem
  lg: '1.5556', // 1.75rem  / 1.125rem
  xl: '1.4', //    1.75rem  / 1.25rem
}

/** Tailwind's `rounded-*`. */
export const radius = {
  xs: '0.125rem', // rounded-sm
  sm: '0.25rem', //  rounded
  md: '0.375rem', // rounded-md
  lg: '0.5rem', //   rounded-lg
  xl: '0.75rem', //  rounded-xl
}

/** Tailwind's `shadow-*`. */
export const shadows = {
  xs: '0 1px 2px 0 rgb(0 0 0 / 0.05)',
  sm: '0 1px 3px 0 rgb(0 0 0 / 0.1), 0 1px 2px -1px rgb(0 0 0 / 0.1)',
  md: '0 4px 6px -1px rgb(0 0 0 / 0.1), 0 2px 4px -2px rgb(0 0 0 / 0.1)',
  lg: '0 10px 15px -3px rgb(0 0 0 / 0.1), 0 4px 6px -4px rgb(0 0 0 / 0.1)',
  xl: '0 20px 25px -5px rgb(0 0 0 / 0.1), 0 8px 10px -6px rgb(0 0 0 / 0.1)',
}

/** Tailwind's breakpoints, in em because Mantine's media queries use em. */
export const breakpoints = {
  xs: '40em', //  640px  sm
  sm: '48em', //  768px  md
  md: '64em', // 1024px  lg
  lg: '80em', // 1280px  xl
  xl: '96em', // 1536px  2xl
}

/** Tailwind's own default stacks. No webfont, so no external request. */
export const fontFamily = [
  'ui-sans-serif',
  'system-ui',
  '-apple-system',
  '"Segoe UI"',
  'Roboto',
  '"Helvetica Neue"',
  'Arial',
  '"Noto Sans"',
  'sans-serif',
  '"Apple Color Emoji"',
  '"Segoe UI Emoji"',
].join(', ')

export const fontFamilyMonospace = [
  'ui-monospace',
  'SFMono-Regular',
  'Menlo',
  'Monaco',
  'Consolas',
  '"Liberation Mono"',
  '"Courier New"',
  'monospace',
].join(', ')
