import { createTheme } from '@mantine/core'

import * as t from './tokens'

/**
 * Mantine, wearing the console's design system.
 *
 * Every scale here comes from `tokens.js`. The component defaults below are
 * what make the system recognisable at a glance: a full pill on everything
 * that can be pressed, a 12px corner on everything that holds content, and a
 * whisper of layered shadow instead of a single heavy lift.
 */
export const theme = createTheme({
  colors: {
    brand: t.brand,
    gold: t.gold,
    cream: t.cream,
    red: t.red,
    amber: t.amber,
    dark: t.dark,
    // Mantine resolves `gray` for its own neutral surfaces; pointing it at the
    // warm ramp keeps unstyled components at the canvas temperature rather
    // than dropping a cold grey into a cream page.
    gray: t.cream,
  },

  primaryColor: 'brand',
  // Index 6 is the accent green the design system fills CTAs with; on a dark
  // ground the same role passes to the luminous mint at index 3.
  primaryShade: { light: 6, dark: 3 },

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

  // Hierarchy comes from weight and colour rather than from size alone, so the
  // heading sizes step gently and every one of them is set at 600.
  headings: {
    fontWeight: '600',
    sizes: {
      h1: { fontSize: t.fontSizes['2xl'], lineHeight: '1.2', fontWeight: '600' },
      h2: { fontSize: t.fontSizes.xl, lineHeight: '1.3', fontWeight: '600' },
      h3: { fontSize: t.fontSizes.xl, lineHeight: '1.3', fontWeight: '600' },
      h4: { fontSize: t.fontSizes.lg, lineHeight: '1.4', fontWeight: '600' },
      h5: { fontSize: t.fontSizes.lg, lineHeight: '1.4', fontWeight: '600' },
      h6: { fontSize: t.fontSizes.sm, lineHeight: '1.5', fontWeight: '600' },
    },
  },

  other: {
    green: t.green,
    fontFamilySerif: t.fontFamilySerif,
    letterSpacing: t.letterSpacing,
  },

  components: {
    Card: {
      defaultProps: { withBorder: true, shadow: 'xs', radius: 'md', padding: 'lg' },
    },
    Paper: {
      defaultProps: { withBorder: true, shadow: 'none', radius: 'md' },
    },
    Table: {
      defaultProps: { highlightOnHover: true, verticalSpacing: 'sm', horizontalSpacing: 'md' },
      styles: {
        // Column headers read as small caps labels on the quiet surface, which
        // is what keeps a nine-column table scannable without rules between
        // the columns.
        th: {
          background: 'var(--pgop-surface-alt)',
          fontSize: t.fontSizes.xs,
          fontWeight: 700,
          letterSpacing: t.letterSpacing.caps,
          textTransform: 'uppercase',
          color: 'var(--pgop-ink-soft)',
        },
      },
    },
    // The pill is universal: every badge and button in the system is one.
    // `tt: none` undoes Mantine's own uppercasing: in this system upper case
    // is reserved for the small-caps labels - eyebrows, field labels, column
    // headers - and a phase is a value, not a label.
    Badge: {
      defaultProps: { variant: 'light', radius: 'xl', size: 'sm', fw: 600, tt: 'none' },
    },
    Code: {
      defaultProps: { color: 'var(--pgop-code-bg)', radius: 'xs' },
    },
    Button: {
      defaultProps: { radius: 'xl', size: 'sm', fw: 600 },
    },
    ActionIcon: {
      defaultProps: { variant: 'subtle', radius: 'xl', color: 'gray' },
    },
    Tooltip: {
      defaultProps: { withArrow: true, openDelay: 250, fz: 'xs', radius: 'sm' },
    },
    Anchor: {
      defaultProps: { underline: 'hover' },
    },
    Alert: {
      defaultProps: { radius: 'md', variant: 'light' },
    },
    Loader: {
      defaultProps: { type: 'oval', size: 'sm' },
    },
    // Inputs keep a 4px corner rather than the pill: a pill-shaped text field
    // reads as a button, and these sit in a filter row next to real ones.
    Select: {
      defaultProps: { radius: 'sm' },
    },
    TextInput: {
      defaultProps: { radius: 'sm' },
    },
    // Every field label is the same small-caps label the table headers use, so
    // a filter row and the columns it filters read as one surface.
    InputWrapper: {
      styles: {
        label: {
          fontSize: t.fontSizes.xs,
          fontWeight: 700,
          letterSpacing: t.letterSpacing.caps,
          textTransform: 'uppercase',
          color: 'var(--pgop-ink-soft)',
          marginBottom: 6,
        },
      },
    },
    Switch: {
      defaultProps: { color: 'brand.6' },
    },
    Skeleton: {
      defaultProps: { radius: 'sm' },
    },
  },
})
