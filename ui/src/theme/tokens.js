/**
 * The console's design tokens, shaped for Mantine.
 */

/** The four brand greens, each with one surface role. */
export const green = {
  /** Headings, and the whole brand signal in dark mode. */
  brand: '#006241',
  /** Filled CTAs, the floating refresh button, focus rings. */
  accent: '#00754A',
  /** The deep near-black green: hero bands, the logo mark. */
  house: '#1E3932',
  /** A mid-dark green for decorative and secondary dark surfaces. */
  uplift: '#2b5148',
  /** The pale mint wash behind active nav items and Ready pills. */
  light: '#d4e9e2',
  /** The luminous mint that carries the brand on a dark ground. */
  onDark: '#8fd6bb',
}

// prettier-ignore
export const brand = [
  '#f0f8f5', '#d4e9e2', '#b0d9cc', '#8fd6bb', '#5fb695',
  '#249a72', '#00754A', '#006241', '#2b5148', '#1E3932',
]

/** Gold. Reserved for the cluster badge and the health panel's ceremony. */
// prettier-ignore
export const gold = [
  '#faf6ee', '#f3ead6', '#e7d6b4', '#dfc49d', '#d4b37c',
  '#cba258', '#b98f45', '#8a6a2c', '#6d5423', '#4c3a18',
]

/**
 * The warm neutral ramp
 */
// prettier-ignore
export const cream = [
  '#faf9f7', '#f2f0eb', '#edebe9', '#e7e7e7', '#d6d3cd',
  '#b3afa8', '#8a8680', '#5c5a56', '#3d3b38', '#24221f',
]

/** Destructive and failure. */
// prettier-ignore
export const red = [
  '#fbeceb', '#f7d6d3', '#efaba5', '#e57e75', '#d95246',
  '#c82014', '#a81a11', '#8a150e', '#6b100b', '#4a0b07',
]

/** Warning, and the amber an unreachable server is reported in. */
// prettier-ignore
export const amber = [
  '#fdf3e0', '#fbe7bd', '#f8d68a', '#f5c65c', '#fbbc05',
  '#e0a300', '#b87f00', '#8f5a00', '#6b4300', '#472c00',
]

// prettier-ignore
export const dark = [
  '#ffffff', // 0  body text
  '#e6efeb', // 1
  '#b9cfc7', // 2  dimmed text
  '#8fada3', // 3  placeholder
  '#3a544b', // 4  default border
  '#264238', // 5  hover
  '#1E3932', // 6  card / input      house green
  '#14231d', // 7  page background
  '#0f1c17', // 8  band
  '#0a1411', // 9
]

/** Spacing. A rem scale whose most frequent step, 1rem, is the outer gutter. */
export const spacing = {
  '2xs': '0.25rem', //  4px
  xs: '0.5rem', //      8px
  sm: '0.75rem', //    12px
  md: '1rem', //       16px  the universal rhythm constant
  lg: '1.5rem', //     24px
  xl: '2rem', //       32px
  '2xl': '2.5rem', //  40px
  '3xl': '4rem', //    64px
}

export const fontSizes = {
  xs: '0.8125rem', //  13px  micro-copy
  sm: '0.875rem', //   14px  metadata, button labels, table body
  md: '0.9375rem', //  15px  body
  lg: '1.1875rem', //  19px  body large, panel titles
  xl: '1.5rem', //     24px  section headings
  '2xl': '2rem', //    32px  page headings
  '3xl': '2.8125rem', // 45px hero display
}

export const lineHeights = {
  xs: '1.5',
  sm: '1.5',
  md: '1.5', //  the body default
  lg: '1.75', // body large: generous, the way the hero copy is set
  xl: '1.3',
}

/**
 * Radius. `md` is the 12px card corner and `xl` the 50px full pill, which is
 * the radius every button and badge in this system takes without exception.
 */
export const radius = {
  xs: '0.25rem', //  4px  code, tight inline chrome
  sm: '0.25rem', //  4px  inputs
  md: '0.75rem', // 12px  cards, panels, modals
  lg: '1rem', //    16px
  xl: '50px', //          full pill
}

export const shadows = {
  xs: '0 0 0.5px 0 rgb(0 0 0 / 0.14), 0 1px 1px 0 rgb(0 0 0 / 0.24)',
  sm: '0 1px 3px rgb(0 0 0 / 0.1), 0 2px 2px rgb(0 0 0 / 0.06), 0 0 2px rgb(0 0 0 / 0.07)',
  md: '0 0 3px rgb(0 0 0 / 0.16), 0 4px 8px rgb(0 0 0 / 0.12)',
  lg: '0 0 6px rgb(0 0 0 / 0.24), 0 8px 12px rgb(0 0 0 / 0.14)',
  xl: '0 0 8px rgb(0 0 0 / 0.24), 0 16px 24px rgb(0 0 0 / 0.16)',
}

/** Breakpoints, in em because Mantine's media queries use em. */
export const breakpoints = {
  xs: '30em', //  480px
  sm: '48em', //  768px
  md: '64em', // 1024px
  lg: '90em', // 1440px
  xl: '96em', // 1536px
}

export const fontFamily = [
  'Manrope',
  '"Helvetica Neue"',
  'Helvetica',
  'Arial',
  'sans-serif',
  '"Apple Color Emoji"',
  '"Segoe UI Emoji"',
].join(', ')

export const fontFamilyMonospace = [
  '"IBM Plex Mono"',
  'ui-monospace',
  'SFMono-Regular',
  'Menlo',
  'Consolas',
  'monospace',
].join(', ')

export const fontFamilySerif = ['Lora', '"Iowan Old Style"', 'Georgia', 'serif'].join(', ')

export const letterSpacing = {
  normal: '-0.01em',
  heading: '-0.16px',
  /** Uppercase eyebrow labels and table headers. */
  caps: '0.03em',
  looser: '0.15em',
}
