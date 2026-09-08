/**
 * Derivations over the API's response models.
 *
 * These exist because the interesting facts about a PostgresDB are *relations
 * between* its status lists, not any single field.
 */

/**
 * One row per schema the operator knows of, tagged with how it stands.
 *
 * The lists a PostgresDB reports overlap deliberately, and telling them apart
 * is the whole point of the view:
 *
 *   managed    declared in the spec; the operator created it and grants on it
 *   orphaned   managed once, no longer declared, kept because retention is RETAIN
 *   unmanaged  present on the server but never created here - never touched,
 *              not even under DROP
 *   unowned    exists, but its owner is not the database's owner group, so
 *              the operator cannot set default privileges on it
 *   missing    declared and recorded as managed, but not observed on the
 *              server - it was dropped behind the operator's back and the
 *              next pass will recreate it
 */
export function schemaRows(db) {
  const managed = new Set(db.managed_schemas ?? [])
  const observed = new Set(db.observed_schemas ?? [])
  const orphaned = new Set(db.orphaned_schemas ?? [])
  const unmanaged = new Set(db.unmanaged_schemas ?? [])
  const unowned = new Set(db.unowned_schemas ?? [])

  const names = [...new Set([...managed, ...observed, ...orphaned, ...unmanaged])]
  names.sort()

  return names.map((name) => ({
    name,
    present: observed.has(name),
    unowned: unowned.has(name),
    grants: db.schema_grants?.[name] ?? [],
    state: schemaState({
      managed: managed.has(name),
      present: observed.has(name),
      orphaned: orphaned.has(name),
      unmanaged: unmanaged.has(name),
      unowned: unowned.has(name),
    }),
  }))
}

function schemaState({ managed, present, orphaned, unmanaged, unowned }) {
  if (orphaned) return 'orphaned'
  if (unmanaged) return 'unmanaged'
  if (managed && !present) return 'missing'
  if (unowned) return 'unowned'
  return managed ? 'managed' : 'observed'
}

/**
 * Same idea for extensions, which have no ownership or grant dimension.
 *
 * `status.observedExtensions` reports `name@version` while managedExtensions
 * and orphanedExtensions carry bare names, so the version has to be split off
 * before the three lists can be compared - and it is worth showing once it is.
 */
export function extensionRows(db) {
  const managed = new Set(db.managed_extensions ?? [])
  const orphaned = new Set(db.orphaned_extensions ?? [])

  const versions = new Map()
  for (const entry of db.observed_extensions ?? []) {
    const at = entry.lastIndexOf('@')
    if (at > 0) versions.set(entry.slice(0, at), entry.slice(at + 1))
    else versions.set(entry, null)
  }

  const names = [...new Set([...managed, ...versions.keys(), ...orphaned])]
  names.sort()

  return names.map((name) => {
    const present = versions.has(name)
    return {
      name,
      version: versions.get(name) ?? null,
      present,
      state: orphaned.has(name)
        ? 'orphaned'
        : managed.has(name)
          ? present
            ? 'managed'
            : 'missing'
          : 'observed',
    }
  })
}

export const STATE_META = {
  managed: { color: 'emerald', label: 'managed', hint: 'declared and reconciled' },
  observed: {
    color: 'zinc',
    label: 'observed',
    hint: 'seen on the server, not recorded as managed',
  },
  orphaned: {
    color: 'amber',
    label: 'orphaned',
    hint: 'no longer declared; retained because retentionPolicy is RETAIN',
  },
  unmanaged: {
    color: 'slate',
    label: 'unmanaged',
    hint: 'created outside the operator; never modified or dropped',
  },
  unowned: {
    color: 'orange',
    label: 'unowned',
    hint: 'not owned by the owner group, so default privileges cannot be set',
  },
  missing: {
    color: 'red',
    label: 'missing',
    hint: 'recorded as managed but absent; the next pass recreates it',
  },
}

/**
 * The store emits attention entries as `Kind/name is Phase: message` or
 * `Kind/ns/name retains orphaned object(s): ...`. Splitting the reference back
 * out turns each line into a link instead of a dead string.
 */
export function parseAttention(entry) {
  const space = entry.indexOf(' ')
  if (space === -1) return { text: entry }

  const ref = entry.slice(0, space)
  const text = entry.slice(space + 1)
  const parts = ref.split('/')

  if (parts[0] === 'PostgresInstance' && parts.length === 2) {
    return { kind: parts[0], ref, text, name: parts[1], to: `/instances/${parts[1]}` }
  }
  if ((parts[0] === 'PostgresDB' || parts[0] === 'PostgresUser') && parts.length === 3) {
    const [kind, namespace, name] = parts
    const section = kind === 'PostgresDB' ? 'databases' : 'users'
    return { kind, ref, text, namespace, name, to: `/${section}/${namespace}/${name}` }
  }
  return { text: entry }
}
