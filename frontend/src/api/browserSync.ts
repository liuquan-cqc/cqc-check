import http, { del, get, post } from './http'

export interface BrowserSyncDevice {
  id: number
  name: string
  status: 'active' | 'revoked' | string
  last_seen_at?: string | null
  created_at: string
}

export function createBrowserPairingCode() {
  return post<{ code: string; expires_at: string }>('/browser-sync/pairing-code')
}

export function getBrowserSyncDevices() {
  return get<BrowserSyncDevice[]>('/browser-sync/devices')
}

export function revokeBrowserSyncDevice(id: number) {
  return del<{ ok: boolean }>(`/browser-sync/devices/${id}`)
}

export async function downloadBrowserSyncExtension() {
  const response = await http.get('/browser-sync/extension-package', { responseType: 'blob' })
  const url = URL.createObjectURL(response.data as Blob)
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = 'edge-report-sync-0.4.4.zip'
  document.body.appendChild(anchor)
  anchor.click()
  anchor.remove()
  URL.revokeObjectURL(url)
}
