import { Badge, Tooltip } from '@mantine/core'

/**
 * A toned pill.
 *
 * Every status the console shows — a phase, a condition, an access level, a
 * schema's standing, a retention policy — is one of these, and the tone is the
 * only thing that varies. The colours come from the `--pgop-tone-*` variables
 * rather than from a Mantine palette shade, because each tone is an explicit
 * background/foreground pair per colour scheme rather than one hue at two
 * opacities: the dark scheme restates them as translucency over green.
 *
 * `outline` is for the quiet cases that should register as chrome rather than
 * as status — a retention default, a Secret key, a grant read back verbatim.
 */
export function Pill({ tone = 'Unknown', label, message, variant = 'light', style, ...props }) {
  const fg = `var(--pgop-tone-${tone}-fg)`
  const tint =
    variant === 'outline'
      ? { background: 'transparent', color: fg, border: `1px solid ${fg}` }
      : { background: `var(--pgop-tone-${tone}-bg)`, color: fg, border: 'none' }

  const pill = (
    // The caller's own style merges *over* the tone rather than replacing it:
    // spreading props last would let a single `style={{ cursor }}` silently
    // strip the colours this component exists to apply.
    <Badge variant="default" style={{ ...tint, ...style }} {...props}>
      {label}
    </Badge>
  )

  // The message is the condition that explains the status, which for anything
  // other than Ready is the only thing worth reading.
  return message ? (
    <Tooltip label={message} multiline maw={420}>
      {pill}
    </Tooltip>
  ) : (
    pill
  )
}
