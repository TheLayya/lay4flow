const DB_NAME = 'lay4flow-studio-media'
const DB_VERSION = 1
const STORE_NAME = 'media'
const MAX_NAMESPACE_BYTES = 512 * 1024 * 1024
const MAX_NAMESPACE_RECORDS = 64

// Media is keyed by the signed-in admin namespace and request identity. No
// request body, token, cookie, or reference material is written here.
export function studioMediaNamespace(storage = globalThis.localStorage) {
  try {
    const username = storage?.getItem?.('admin_username') || 'creator'
    return String(username).trim().slice(0, 160) || 'creator'
  } catch {
    return 'creator'
  }
}

export function studioMediaIdentity(entry) {
  return String(entry?.receipt || entry?.key || entry?.started || '').slice(0, 180)
}

function keyFor(namespace, identity, index) {
  return `${namespace}\u0000${identity}\u0000${Number(index) || 0}`
}

function openDatabase() {
  if (typeof indexedDB === 'undefined') return Promise.resolve(null)
  return new Promise((resolve) => {
    let request
    try {
      request = indexedDB.open(DB_NAME, DB_VERSION)
      request.onupgradeneeded = () => {
        const db = request.result
        if (!db.objectStoreNames.contains(STORE_NAME)) {
          const store = db.createObjectStore(STORE_NAME, { keyPath: 'key' })
          store.createIndex('namespace', 'namespace', { unique: false })
        }
      }
      request.onsuccess = () => resolve(request.result)
      request.onerror = () => resolve(null)
      request.onblocked = () => resolve(null)
    } catch {
      resolve(null)
    }
  })
}

function transaction(db, mode, action) {
  return new Promise((resolve, reject) => {
    let tx
    try {
      tx = db.transaction(STORE_NAME, mode)
      const request = action(tx.objectStore(STORE_NAME))
      let result
      request.onsuccess = () => {
        result = request.result
      }
      request.onerror = () => reject(request.error || new Error('media cache request failed'))
      tx.oncomplete = () => resolve(result)
      tx.onerror = () => reject(tx.error || new Error('media cache transaction failed'))
      tx.onabort = () => reject(tx.error || new Error('media cache transaction aborted'))
    } catch (error) {
      reject(error)
    }
  })
}

export async function getStudioMedia(namespace, entry, index) {
  const identity = studioMediaIdentity(entry)
  if (!identity) return null
  const db = await openDatabase()
  if (!db) return null
  try {
    const value = await transaction(db, 'readonly', (store) => store.get(keyFor(namespace, identity, index)))
    return value?.blob instanceof Blob ? value : null
  } catch {
    return null
  } finally {
    db.close()
  }
}

export async function putStudioMedia(namespace, entry, index, blob, metadata = {}) {
  const identity = studioMediaIdentity(entry)
  if (!identity || !(blob instanceof Blob)) return false
  // Keep video/audio as authenticated URLs when they are large; images and
  // small clips still get an offline preview without consuming the profile.
  if (blob.size > 64 * 1024 * 1024 && metadata.kind !== 'image') return false
  const db = await openDatabase()
  if (!db) return false
  try {
    await transaction(db, 'readwrite', (store) =>
      store.put({
        key: keyFor(namespace, identity, index),
        namespace,
        identity,
        index: Number(index) || 0,
        blob,
        mime: blob.type || metadata.mime || '',
        kind: metadata.kind || '',
        savedAt: Date.now(),
      }),
    )
    await pruneNamespace(db, namespace)
    return true
  } catch {
    return false
  } finally {
    db.close()
  }
}

async function pruneNamespace(db, namespace) {
  const records = await new Promise((resolve, reject) => {
    const found = []
    let tx
    try {
      tx = db.transaction(STORE_NAME, 'readonly')
      const request = tx.objectStore(STORE_NAME).index('namespace').openCursor(IDBKeyRange.only(namespace))
      request.onsuccess = () => {
        const cursor = request.result
        if (!cursor) return
        found.push(cursor.value)
        cursor.continue()
      }
      request.onerror = () => reject(request.error)
      tx.oncomplete = () => resolve(found)
      tx.onerror = () => reject(tx.error)
    } catch (error) {
      reject(error)
    }
  })
  records.sort((a, b) => (a.savedAt || 0) - (b.savedAt || 0))
  let bytes = records.reduce((sum, item) => sum + Number(item.blob?.size || 0), 0)
  const remove = []
  while (records.length - remove.length > MAX_NAMESPACE_RECORDS || bytes > MAX_NAMESPACE_BYTES) {
    const item = records[remove.length]
    if (!item) break
    remove.push(item)
    bytes -= Number(item.blob?.size || 0)
  }
  if (!remove.length) return
  await new Promise((resolve, reject) => {
    let tx
    try {
      tx = db.transaction(STORE_NAME, 'readwrite')
      const store = tx.objectStore(STORE_NAME)
      remove.forEach((item) => store.delete(item.key))
      tx.oncomplete = resolve
      tx.onerror = () => reject(tx.error)
    } catch (error) {
      reject(error)
    }
  })
}

export async function deleteStudioMedia(namespace, entry) {
  const identity = studioMediaIdentity(entry)
  if (!identity) return false
  const db = await openDatabase()
  if (!db) return false
  try {
    const prefix = `${namespace}\u0000${identity}\u0000`
    await new Promise((resolve, reject) => {
      let tx
      try {
        tx = db.transaction(STORE_NAME, 'readwrite')
        const request = tx.objectStore(STORE_NAME).openCursor()
        request.onsuccess = () => {
          const cursor = request.result
          if (!cursor) return
          if (String(cursor.key).startsWith(prefix)) cursor.delete()
          cursor.continue()
        }
        request.onerror = () => reject(request.error)
        tx.oncomplete = resolve
        tx.onerror = () => reject(tx.error)
      } catch (error) {
        reject(error)
      }
    })
    return true
  } catch {
    return false
  } finally {
    db.close()
  }
}

export async function clearStudioMedia(namespace) {
  const db = await openDatabase()
  if (!db) return false
  try {
    await new Promise((resolve, reject) => {
      let tx
      try {
        tx = db.transaction(STORE_NAME, 'readwrite')
        const request = tx.objectStore(STORE_NAME).index('namespace').openCursor(IDBKeyRange.only(namespace))
        request.onsuccess = () => {
          const cursor = request.result
          if (!cursor) return
          cursor.delete()
          cursor.continue()
        }
        request.onerror = () => reject(request.error)
        tx.oncomplete = resolve
        tx.onerror = () => reject(tx.error)
      } catch (error) {
        reject(error)
      }
    })
    return true
  } catch {
    return false
  } finally {
    db.close()
  }
}
