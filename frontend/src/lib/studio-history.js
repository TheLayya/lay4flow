export const STUDIO_HISTORY_KEY = 'lay4flow_studio_receipts'
export const STUDIO_HISTORY_LIMIT = 300
export const STUDIO_HISTORY_OWNER_KEY = `${STUDIO_HISTORY_KEY}:owner`

export function studioHistoryKey(namespace) {
  return `${STUDIO_HISTORY_KEY}:v2:${encodeURIComponent(namespace)}`
}

export function readStudioHistory(local, session, namespace) {
  let durable
  try {
    durable = local.getItem(studioHistoryKey(namespace))
  } catch {
    /* A restricted browser can still restore its temporary session copy. */
  }
  if (durable !== null && durable !== undefined) {
    try {
      return JSON.parse(durable)
    } catch {
      /* An older interrupted write can fall back to the session mirror. */
    }
  }
  const previousOwner = session.getItem(STUDIO_HISTORY_OWNER_KEY)
  if (previousOwner && previousOwner !== namespace) return []
  return JSON.parse(session.getItem(STUDIO_HISTORY_KEY) || '[]')
}

const unsettledStates = new Set([
  'preparing',
  'generating',
  'queued',
  'in_progress',
  'received',
  'unknown',
  'download_failed',
])

export function isUnsettledVids(entry) {
  return (
    entry?.provider === 'vids' && /^vids_[a-f0-9]{64}$/.test(entry.receipt || '') && unsettledStates.has(entry.status)
  )
}

// Keep unresolved receipts even beyond the ordinary history window: they prevent duplicate generation charges.
export function selectStudioHistory(entries, limit = STUDIO_HISTORY_LIMIT) {
  return entries.filter((entry, index) => index < limit || isUnsettledVids(entry))
}

export function writeStudioHistory(storage, entries, key = STUDIO_HISTORY_KEY) {
  let limit = STUDIO_HISTORY_LIMIT
  while (true) {
    const selected = selectStudioHistory(entries, limit)
    try {
      storage.setItem(key, JSON.stringify(selected))
      return {
        saved: true,
        retained: selected.length,
        omitted: entries.length - selected.length,
        reduced: limit < STUDIO_HISTORY_LIMIT,
      }
    } catch {
      if (limit === 0) return { saved: false, retained: 0, omitted: entries.length, reduced: true }
      limit = Math.floor(limit / 2)
    }
  }
}
