import assert from 'node:assert/strict'
import { test } from 'node:test'
import {
    STUDIO_HISTORY_KEY,
    STUDIO_HISTORY_OWNER_KEY,
    readStudioHistory,
    selectStudioHistory,
    studioHistoryKey,
    writeStudioHistory,
} from '../src/lib/studio-history.js'

const pending = {
    provider: 'vids',
    status: 'unknown',
    receipt: 'vids_' + 'a'.repeat(64),
    signature: 'original',
    outputs: [],
}
const records = () =>
    Array.from({ length: 340 }, (_, index) => ({
        provider: 'flow',
        status: 'completed',
        title: `work-${index}`,
        outputs: [],
    }))

test('the ordinary 300 request window retains every unresolved receipt regardless of age', () => {
    const entries = records()
    entries.push(pending)
    const selected = selectStudioHistory(entries)
    assert.equal(selected.length, 301)
    assert.equal(selected.at(-1), pending)
    assert.equal(selected[299].title, 'work-299')
})

test('storage quota fallback drops completed requests before unresolved receipts', () => {
    let saved
    const entries = records()
    entries.push(pending)
    const storage = {
        setItem(key, json) {
            if (json.length > 600) throw new Error('quota exceeded')
            saved = JSON.parse(json)
        },
    }
    const result = writeStudioHistory(storage, entries)
    assert.equal(result.saved, true)
    assert.equal(result.reduced, true)
    assert.ok(saved.length < 300)
    assert.equal(saved.at(-1).receipt, pending.receipt)
    assert.equal(saved.at(-1).signature, 'original')
    assert.equal(result.retained, saved.length)
})

test('fully blocked storage reports failure instead of claiming records were persisted', () => {
    const result = writeStudioHistory(
        {
            setItem() {
                throw new Error('disabled')
            },
        },
        [...records(), pending],
    )
    assert.equal(result.saved, false)
    assert.equal(result.retained, 0)
})

test('durable history stays scoped to the administrator and migrates only that session owner', () => {
    const local = new Map([[studioHistoryKey('admin-a'), JSON.stringify([{ title: 'A only' }])]])
    const session = new Map([
        [STUDIO_HISTORY_KEY, JSON.stringify([{ title: 'legacy A' }])],
        [STUDIO_HISTORY_OWNER_KEY, 'admin-a'],
    ])
    const storage = (values) => ({ getItem: (key) => values.get(key) ?? null })
    assert.equal(readStudioHistory(storage(local), storage(session), 'admin-a')[0].title, 'A only')
    assert.deepEqual(readStudioHistory(storage(local), storage(session), 'admin-b'), [])
    local.clear()
    assert.equal(readStudioHistory(storage(local), storage(session), 'admin-a')[0].title, 'legacy A')
})
