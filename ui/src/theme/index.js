import { createTheme } from '@mantine/core'

import * as t from './tokens'

/**
 * Mantine, wearing Tailwind's tokens.
 *
 * Every scale here comes from `tokens.js`; the component defaults below exist
 * to reproduce Tailwind's flat surface treatment — a hairline border and a
 * barely-there shadow rather than Mantine's softer default elevation.
 */
export const theme = createTheme({
  colors: {
    slate: t.slate,
    zinc: t.zinc,
    blue: t.blue,
    emerald: t.emerald,
    amber: t.amber,
    red: t.red,
    orange: t.orange,
    violet: t.violet,
    cyan: t.cyan,
    dark: t.dark,
    // Mantine resolves `gray` for its own neutral surfaces; pointing it at
    // slate keeps unstyled components in the same family as everything else.
    gray: t.slate,
  },

  primaryColor: 'blue',
  // Tailwind uses -600 for interactive elements on white and -400 on a dark
  // background, which is exactly what these two indices are.
  primaryShade: { light: 6, dark: 4 },

  fontFamily: t.fontFamily,
  fontFamilyMonospace: t.fontFamilyMonospace,
  fontSizes: t.fontSizes,
  lineHeights: t.lineHeights,
  spacing: t.spacing,
  radius: t.radius,
  shadows: t.shadows,
  breakpoints: t.breakpoints,

  defaultRadius: 'md',
  cursorType: 'pointer',
  autoContrast: true,

  headings: {
    fontWeight: '600',
    sizes: {
      h1: { fontSize: t.fontSizes['3xl'], lineHeight: '1.2', fontWeight: '600' },
      h2: { fontSize: t.fontSizes['2xl'], lineHeight: '1.25', fontWeight: '600' },
      h3: { fontSize: t.fontSizes.xl, lineHeight: '1.3', fontWeight: '600' },
      h4: { fontSize: t.fontSizes.lg, lineHeight: '1.4', fontWeight: '600' },
      h5: { fontSize: t.fontSizes.md, lineHeight: '1.5', fontWeight: '600' },
      h6: { fontSize: t.fontSizes.sm, lineHeight: '1.5', fontWeight: '600' },
    },
  },

  other: {
    deepest: t.deepest,
  },

  components: {
    Card: {
      defaultProps: { withBorder: true, shadow: 'xs', radius: 'md', padding: 'md' },
    },
    Paper: {
      defaultProps: { withBorder: true, shadow: 'none', radius: 'md' },
    },
    Table: {
      defaultProps: { highlightOnHover: true, verticalSpacing: 'sm', horizontalSpacing: 'md' },
    },
    Badge: {
      defaultProps: { variant: 'light', radius: 'sm', size: 'sm' },
    },
    Code: {
      defaultProps: { color: 'var(--mantine-color-default)' },
    },
    Button: {
      defaultProps: { radius: 'md', size: 'sm', fw: 500 },
    },
    ActionIcon: {
      defaultProps: { variant: 'subtle', radius: 'md', color: 'gray' },
    },
    Tooltip: {
      defaultProps: { withArrow: true, openDelay: 250, fz: 'xs' },
    },
    Anchor: {
      defaultProps: { underline: 'never' },
    },
    Alert: {
      defaultProps: { radius: 'md', variant: 'light' },
    },
    Loader: {
      defaultProps: { type: 'oval', size: 'sm' },
    },
  },
})
