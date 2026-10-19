import { ElMessage } from 'element-plus'
import { createApi } from './http'

export const api = createApi()
export function notifyError(error) {
  if (error?.isAdminSessionExpired) return
  ElMessage.error(error?.message || '连接暂时中断，请稍后重试')
}

export function formatNumber(value) {
  return value == null ? '—' : new Intl.NumberFormat('zh-CN').format(value)
}
export function formatDate(value) {
  if (!value) return '尚无记录'
  const date = parseDate(value)
  return Number.isNaN(date.getTime())
    ? String(value)
    : date.toLocaleString('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' })
}
export function parseDate(value) {
  if (typeof value !== 'string') return new Date(value)
  const iso = value.trim().replace(' ', 'T')
  return new Date(/^\d{4}-\d{2}-\d{2}T/.test(iso) && !/(?:Z|[+-]\d{2}:?\d{2})$/i.test(iso) ? `${iso}Z` : iso)
}
