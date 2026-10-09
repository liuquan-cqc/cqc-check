const DEFAULT_SERVER = 'http://localhost:8080'

function normalizeServerUrl(value) {
  let parsed
  try { parsed = new URL(String(value || DEFAULT_SERVER).trim()) }
  catch { throw new Error('审核网站地址格式不正确') }
  if (!['http:', 'https:'].includes(parsed.protocol)) throw new Error('审核网站地址必须使用HTTP或HTTPS')
  return parsed.origin
}

async function settings() {
  const stored = await chrome.storage.local.get(['serverUrl', 'deviceToken'])
  return {
    serverUrl: normalizeServerUrl(stored.serverUrl || DEFAULT_SERVER),
    deviceToken: stored.deviceToken || ''
  }
}

async function pairDevice(code, deviceName, serverUrl) {
  const base = normalizeServerUrl(serverUrl || DEFAULT_SERVER)
  const form = new FormData()
  form.append('code', code)
  form.append('device_name', deviceName || 'Parallels Win11 Edge')
  const response = await fetch(`${base}/api/browser-sync/pair`, { method: 'POST', body: form })
  const result = await response.json().catch(() => ({}))
  if (!response.ok) throw new Error(result.detail || '配对失败')
  await chrome.storage.local.set({ serverUrl: base, deviceToken: result.token, deviceName })
  return result.device
}

async function updateServerUrl(serverUrl) {
  const base = normalizeServerUrl(serverUrl)
  await chrome.storage.local.set({ serverUrl: base })
  return { serverUrl: base }
}

function contentDispositionFilename(value) {
  const source = String(value || '')
  const utf8 = source.match(/filename\*=UTF-8''([^;]+)/i)
  if (utf8) {
    try { return decodeURIComponent(utf8[1].trim()) } catch { /* 使用普通文件名 */ }
  }
  return (source.match(/filename="?([^";]+)"?/i) || [])[1]?.trim() || ''
}

async function isPdfBlob(blob) {
  if (!blob?.size) return false
  const header = await blob.slice(0, 5).text()
  return header === '%PDF-'
}

function notifySyncProgress(detail) {
  chrome.runtime.sendMessage({ type: 'SYNC_PROGRESS', detail }).catch(() => {})
}

async function syncReport(payload) {
  const config = await settings()
  if (!config.deviceToken) throw new Error('请先使用管理员配对码完成配对')
  if (payload.expectedServer && config.serverUrl !== payload.expectedServer) throw new Error('审核网站地址已变化，停止上传')
  if (payload.expectedServer) {
    const target = new URL(payload.pdf.url)
    if (target.origin !== 'https://certification.example' || target.pathname !== '/inner/sys.SysUploadedFileCtl.downloadFile.do' || !/^[a-f\d]{32}$/i.test(target.searchParams.get('objID') || '')) throw new Error('批量下载地址不符合已核验范围')
  }
  const pdfResponse = await fetch(payload.pdf.url, { credentials: 'include', cache: 'no-store', ...(payload.expectedServer ? {redirect:'error', signal:AbortSignal.timeout(120000)} : {}) })
  if (!pdfResponse.ok) throw new Error(`PDF下载失败（${pdfResponse.status}）`)
  const blob = await pdfResponse.blob()
  if (!(await isPdfBlob(blob))) throw new Error('下载内容不是有效PDF，可能是登录页、收费单页面或下载链接已失效')
  const responseFilename = contentDispositionFilename(pdfResponse.headers.get('content-disposition'))
  const filename = responseFilename || payload.pdf.filename || 'report.pdf'
  const form = new FormData()
  form.append('file', blob, filename)
  form.append('source_task_no', payload.taskNo || '')
  form.append('source_application_no', payload.applicationNo || '')
  form.append('source_url', payload.sourceUrl || '')
  if (payload.expectedServer) form.append('batch_mode', 'true')
  if (payload.autoClassify) form.append('intake_mode', 'typed_v1')
  const response = await fetch(`${config.serverUrl}/api/browser-sync/upload`, {
    method: 'POST',
    ...(payload.expectedServer ? {redirect:'error', signal:AbortSignal.timeout(180000)} : {}),
    headers: { 'X-Browser-Sync-Token': config.deviceToken },
    body: form
  })
  const result = await response.json().catch(() => ({}))
  if (!response.ok) {
    const detail = result.detail
    throw new Error(typeof detail === 'string' ? detail : detail?.message || '报告同步失败')
  }
  return { ...result, uploaded_filename: filename }
}

async function syncReports(payload) {
  const pdfs = Array.isArray(payload?.pdfs) ? payload.pdfs : []
  if (!pdfs.length) throw new Error('请至少选择一份报告')
  const results = []
  const failures = []
  for (const [index, pdf] of pdfs.entries()) {
    notifySyncProgress({ current: index, total: pdfs.length, filename: pdf.filename || 'report.pdf', stage: 'downloading' })
    try {
      const capability = await backendRequest('/api/browser-sync/batch-capabilities')
      if (capability?.typed_intake !== true) throw new Error('服务器尚未启用按任务和类别归属，请更新配套服务')
      const result = await syncReport({ ...payload, autoClassify: true, pdf })
      results.push({ filename: pdf.filename || 'report.pdf', ...result })
    } catch (error) {
      failures.push({ filename: pdf.filename || 'report.pdf', message: error?.message || String(error) })
    }
    notifySyncProgress({ current: index + 1, total: pdfs.length, filename: pdf.filename || 'report.pdf', stage: 'completed' })
  }
  if (results.length && !failures.length) await chrome.storage.local.remove('pendingTask')
  return { results, failures }
}

async function backendRequest(path, options = {}) {
  const config = await settings()
  if (!config.deviceToken) throw new Error('请先使用管理员配对码完成配对')
  const response = await fetch(`${config.serverUrl}${path}`, {
    ...options,
    headers: {
      'X-Browser-Sync-Token': config.deviceToken,
      'Content-Type': 'application/json',
      ...(options.headers || {})
    }
  })
  const result = await response.json().catch(() => ({}))
  if (!response.ok) {
    const detail = result.detail
    const message = typeof detail === 'string' ? detail : detail?.message || '请求失败'
    const error = new Error(message)
    error.code = detail?.code || ''
    throw error
  }
  return result
}

async function companyVerificationState() {
  const stored = await chrome.storage.local.get([
    'companyVerificationTask', 'companyAutoRun', 'companyVerificationMessage', 'verificationTabId'
  ])
  return {
    task: stored.companyVerificationTask || null,
    autoRun: Boolean(stored.companyAutoRun),
    message: stored.companyVerificationMessage || '',
    tabId: stored.verificationTabId || null
  }
}

async function startNextCompanyVerification() {
  try {
    const task = await backendRequest('/api/browser-sync/company-verification/next')
    const state = await companyVerificationState()
    let tabId = state.tabId
    if (tabId) {
      try { await chrome.tabs.update(tabId, { url: task.search_url, active: true }) }
      catch { tabId = null }
    }
    if (!tabId) {
      const tab = await chrome.tabs.create({ url: task.search_url, active: true })
      tabId = tab.id
    }
    await chrome.storage.local.set({
      companyVerificationTask: task,
      companyAutoRun: true,
      companyVerificationMessage: `正在核验：${task.company_name}`,
      verificationTabId: tabId
    })
    return task
  } catch (error) {
    await chrome.storage.local.set({
      companyAutoRun: false,
      companyVerificationMessage: error?.message || String(error)
    })
    throw error
  }
}

async function submitCompanyVerification(payload) {
  const state = await companyVerificationState()
  const task = state.task
  if (!task || Number(task.task_id) !== Number(payload.taskId)) throw new Error('当前企业核验任务已失效')
  const result = await backendRequest(`/api/browser-sync/company-verification/${task.task_id}/result`, {
    method: 'POST',
    body: JSON.stringify(payload.result)
  })
  await chrome.storage.local.set({
    companyVerificationTask: null,
    companyVerificationMessage: `${task.company_name}核验完成：${result.raw_status}`
  })
  if (state.autoRun) {
    chrome.alarms.create('companyVerificationNext', { delayInMinutes: Math.max(0.2, Number(task.interval_seconds || 20) / 60) })
  }
  return result
}

async function pauseCompanyVerification(reason) {
  const state = await companyVerificationState()
  if (state.task) {
    await backendRequest(`/api/browser-sync/company-verification/${state.task.task_id}/pause`, {
      method: 'POST', body: JSON.stringify({ reason: reason || '网页核验已暂停' })
    }).catch(() => {})
  }
  await chrome.alarms.clear('companyVerificationNext')
  await chrome.storage.local.set({
    companyAutoRun: false,
    companyVerificationTask: null,
    companyVerificationMessage: reason || '企业核验已暂停'
  })
  return { ok: true }
}

chrome.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name !== 'companyVerificationNext') return
  companyVerificationState().then((state) => {
    if (state.autoRun) return startNextCompanyVerification()
  }).catch(() => {})
})

chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  const run = async () => {
    if (message?.type === 'GET_CONFIG') return settings()
    if (message?.type === 'GET_BATCH_CAPABILITIES') return backendRequest('/api/browser-sync/batch-capabilities')
    if (message?.type === 'PAIR_DEVICE') return pairDevice(message.code, message.deviceName, message.serverUrl)
    if (message?.type === 'UPDATE_SERVER_URL') return updateServerUrl(message.serverUrl)
    if (message?.type === 'SYNC_REPORT') return syncReport(message.payload)
    if (message?.type === 'SYNC_REPORTS') return syncReports(message.payload)
    if (message?.type === 'GET_COMPANY_VERIFICATION_STATE') return companyVerificationState()
    if (message?.type === 'START_COMPANY_VERIFICATION') return startNextCompanyVerification()
    if (message?.type === 'SUBMIT_COMPANY_VERIFICATION') return submitCompanyVerification(message)
    if (message?.type === 'PAUSE_COMPANY_VERIFICATION') return pauseCompanyVerification(message.reason)
    throw new Error('未知操作')
  }
  run().then((data) => sendResponse({ ok: true, data })).catch((error) => {
    sendResponse({ ok: false, message: error?.message || String(error) })
  })
  return true
})
