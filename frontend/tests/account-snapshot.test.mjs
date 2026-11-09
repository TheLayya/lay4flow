import { test } from 'node:test'
import assert from 'node:assert/strict'
import { accountSnapshotEpoch, readAccountSnapshot, writeAccountSnapshot, clearAccountSnapshot } from '../src/lib/account-snapshot.js'

test('account snapshot expires and logout prevents late writes from a previous session', () => {
  clearAccountSnapshot()
  const epoch = accountSnapshotEpoch()
  const data = { accounts: [{ id: 1, email: 'synthetic@example.invalid' }] }
  writeAccountSnapshot(data, epoch, 1000)
  assert.deepEqual(readAccountSnapshot(1001), data)
  assert.equal(readAccountSnapshot(61_000), null)
  clearAccountSnapshot()
  writeAccountSnapshot(data, epoch, 2000)
  assert.equal(readAccountSnapshot(2001), null)
  writeAccountSnapshot(data, accountSnapshotEpoch(), 3000)
  assert.deepEqual(readAccountSnapshot(3001), data)
  clearAccountSnapshot()
})
