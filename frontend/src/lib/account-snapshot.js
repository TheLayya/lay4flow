// Short-lived in-memory data only. Never persist account credentials in storage.
let snapshot
let epoch = 0
const maxAge = 60_000

export function accountSnapshotEpoch() {
  return epoch
}
export function readAccountSnapshot(now = Date.now()) {
  return snapshot && now - snapshot.savedAt < maxAge ? snapshot.data : null
}
export function writeAccountSnapshot(data, expectedEpoch, now = Date.now()) {
  if (expectedEpoch !== epoch) return
  snapshot = { savedAt: now, data }
}
export function clearAccountSnapshot() {
  snapshot = undefined
  epoch++
}
