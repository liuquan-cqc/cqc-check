let currentInspection = null
let pendingSelectedFiles = []

const $ = (id) => document.getElementById(id)
const show = (id, visible = true) => $(id).classList.toggle('hidden', !visible)

function message(text, success = false) {
  $('message').textContent = text
  $('message').classList.toggle('success', success)
  show('message', Boolean(text))
}

function escapeHtml(value) {
  return String(value || '').replace(/[&<>"']/g, (char) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[char]))
}

function choiceMarkup(choice, index, type, id) {
  const disabled = choice.selectable === false
  const badge = choice.badge
    ? `<span class="file-badge ${escapeHtml(choice.tone || 'info')}">${escapeHtml(choice.badge)}</span>`
    : ''
  const recommended = choice.recommended ? '<span class="recommended">建议核对</span>' : ''
  return `
    <label class="choice-row${disabled ? ' unavailable' : ''}">
      <input type="${type}" name="${id}" value="${index}" ${disabled ? 'disabled' : ''} />
      <span>
        ${(badge || recommended) ? `<span class="choice-meta">${badge}${recommended}</span>` : ''}
        <strong>${escapeHtml(choice.title)}</strong>
        <small>${escapeHtml(choice.detail)}</small>
      </span>
    </label>`
}

function setChoiceList(id, choices, type) {
  const box = $(id)
  box.innerHTML = choices.map((choice, index) => choiceMarkup(choice, index, type, id)).join('')
  show(id, choices.length > 0)
  box.querySelectorAll('input').forEach((input) => input.addEventListener('change', () => {
    if (id === 'taskChoices') $('openButton').disabled = !box.querySelector('input:checked')
    if (id === 'fileChoices') updateSyncButton()
  }))
  if (id === 'taskChoices') {
    $('openButton').textContent = `进入所选报告入口${choices.length > 1 ? `（共${choices.length}个）` : ''}`
    $('openButton').disabled = true
  }
  if (id === 'fileChoices') updateSyncButton()
}

function selectedChoiceIndexes(id) {
  return [...$(id).querySelectorAll('input:checked')].map((input) => Number(input.value))
}

function updateSyncButton() {
  const count = selectedChoiceIndexes('fileChoices').length
  $('syncButton').disabled = count === 0
  $('syncButton').textContent = count ? `下一步：确认上传（${count}份）` : '请先勾选需要上传的文件'
}

async function worker(payload) {
  return chrome.runtime.sendMessage(payload)
}

async function activeTabMessage(payload) {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true })
  if (!tab?.id) throw new Error('无法读取当前标签页')
  return chrome.tabs.sendMessage(tab.id, payload)
}

function permissionPattern(serverUrl) {
  const parsed = new URL(String(serverUrl || '').trim())
  if (!['http:', 'https:'].includes(parsed.protocol)) throw new Error('审核网站地址必须使用HTTP或HTTPS')
  return `${parsed.origin}/*`
}

async function ensureServerPermission(serverUrl) {
  const pattern = permissionPattern(serverUrl)
  if (await chrome.permissions.contains({ origins: [pattern] })) return
  const granted = await chrome.permissions.request({ origins: [pattern] })
  if (!granted) throw new Error('未获得访问该审核网站地址的权限')
}

function resetSelectionView() {
  pendingSelectedFiles = []
  show('selectionPanel')
  show('confirmPanel', false)
}

async function inspect() {
  message('')
  resetSelectionView()
  $('pageState').textContent = '正在识别当前页面…'
  show('openButton', false); show('syncButton', false); show('reportInfo', false)
  show('taskChoices', false); show('fileChoices', false)
  try {
    currentInspection = await activeTabMessage({ type: 'INSPECT_PAGE' })
    if (currentInspection.pageType === 'task') {
      const tasks = currentInspection.tasks || []
      $('pageState').textContent = tasks.length > 1 ? `发现 ${tasks.length} 个目标报告入口，请选择一个进入` : '已识别目标报告入口，请选择后进入'
      $('taskNo').textContent = tasks.length === 1 ? (tasks[0].taskNo || '-') : '多个入口'
      $('applicationNo').textContent = tasks.length === 1 ? (tasks[0].applicationNo || '进入报告页后读取') : '请在下方选择'
      show('reportInfo'); show('openButton')
      setChoiceList('taskChoices', tasks.map((task) => ({
        title: task.applicationNo || task.taskNo || task.label,
        detail: `${task.status}${task.taskNo ? ` · ${task.taskNo}` : ''}`
      })), 'radio')
    } else if (currentInspection.ready) {
      const files = currentInspection.files || currentInspection.pdfs || []
      const selectable = files.filter((item) => item.selectable !== false)
      const blocked = files.length - selectable.length
      $('pageState').textContent = `发现 ${files.length} 个文件条目，请勾选需要上传的报告${blocked ? `；其中${blocked}个未解析到下载地址` : ''}`
      $('taskNo').textContent = currentInspection.taskNo || '-'
      $('applicationNo').textContent = currentInspection.applicationNo || '-'
      show('reportInfo'); show('syncButton')
      setChoiceList('fileChoices', files.map((file) => ({
        title: file.filename,
        detail: `${file.sourceGroup || '文件列表'} · ${file.note || '由您确认后上传'}`,
        badge: file.category || '文件',
        tone: file.tone || 'info',
        recommended: Boolean(file.recommended),
        selectable: file.selectable !== false
      })), 'checkbox')
    } else {
      $('pageState').textContent = currentInspection.message || '当前页面暂不支持同步'
    }
  } catch {
    $('pageState').textContent = '请在单位系统的任务详情或报告文件页面使用本扩展；如果刚更新扩展，请先刷新单位系统页面。'
  }
}

function renderConfirmation() {
  $('confirmApplicationNo').textContent = currentInspection?.applicationNo || '-'
  $('confirmEntry').textContent = `${currentInspection?.entryLabel || '电子型式试验报告'}${currentInspection?.sourceStatus ? ` · ${currentInspection.sourceStatus}` : ''}`
  $('confirmFiles').innerHTML = pendingSelectedFiles.map((file) => `
    <li>${escapeHtml(file.filename)}<small>${escapeHtml(file.category || file.sourceGroup || '报告文件')}</small></li>
  `).join('')
  $('confirmButton').textContent = `确认并上传（${pendingSelectedFiles.length}份）`
  $('confirmButton').disabled = pendingSelectedFiles.length === 0
}

function showConfirmation() {
  const files = currentInspection?.files || currentInspection?.pdfs || []
  pendingSelectedFiles = selectedChoiceIndexes('fileChoices').map((index) => files[index]).filter((file) => file?.selectable !== false)
  if (!pendingSelectedFiles.length) return message('请先勾选需要上传的文件')
  renderConfirmation()
  show('selectionPanel', false)
  show('confirmPanel')
  message('')
}

async function uploadConfirmedFiles() {
  if (!pendingSelectedFiles.length) return
  $('confirmButton').disabled = true
  $('backButton').disabled = true
  $('confirmButton').textContent = `正在上传 0/${pendingSelectedFiles.length}…`
  message('')
  const payload = {
    ...currentInspection,
    pdfs: pendingSelectedFiles,
    sourceUrl: currentInspection.pageUrl || ''
  }
  try {
    const result = await worker({ type: 'SYNC_REPORTS', payload })
    if (!result?.ok) throw new Error(result?.message || '上传失败')
    const data = result.data
    const successText = data.results.map((item) => `${item.filename}：${({ created: '已创建新报告', revision: `已创建V${item.revision_no}更正版本`, duplicate: '已存在，跳过', skipped:'非报告附件已跳过' }[item.action] || '上传完成')}${item.message ? `；${item.message}` : ''}`).join('；')
    const failedText = data.failures?.map((item) => `${item.filename}：${item.message}`).join('；')
    message(failedText ? `${successText || '无文件上传成功'}；失败：${failedText}` : successText, !failedText)
    if (failedText) {
      const failedNames = new Set(data.failures.map((item) => item.filename))
      pendingSelectedFiles = pendingSelectedFiles.filter((file) => failedNames.has(file.filename))
      renderConfirmation()
      $('confirmButton').textContent = `重试失败文件（${pendingSelectedFiles.length}份）`
      $('confirmButton').disabled = false
      $('backButton').disabled = false
    } else {
      $('confirmButton').textContent = '全部上传完成'
      $('confirmButton').disabled = true
      $('backButton').disabled = false
    }
  } catch (error) {
    message(error.message || String(error))
    $('confirmButton').textContent = '重新上传'
    $('confirmButton').disabled = false
    $('backButton').disabled = false
  }
}

async function loadCompanyState() {
  const response = await worker({ type: 'GET_COMPANY_VERIFICATION_STATE' })
  const state = response?.data || {}
  const task = state.task
  $('companyState').textContent = state.message || (state.autoRun ? '企业核验正在运行' : '已就绪，请先在网站中生成今日核验队列')
  $('companyName').textContent = task?.company_name || '-'
  show('companyInfo', Boolean(task))
  show('companyStartButton', !state.autoRun)
  show('companyStopButton', state.autoRun)
}

async function init() {
  const response = await worker({ type: 'GET_CONFIG' })
  const config = response?.data || {}
  if (!config.deviceToken) {
    show('pairPanel'); show('syncPanel', false)
    $('serverUrl').value = config.serverUrl || 'http://localhost:8080'
    return
  }
  show('pairPanel', false); show('syncPanel'); show('companyPanel')
  $('serverLabel').textContent = config.serverUrl
  $('serverEditUrl').value = config.serverUrl
  await Promise.all([inspect(), loadCompanyState()])
}

$('pairButton').addEventListener('click', async () => {
  const code = $('pairCode').value.trim()
  if (!/^\d{6}$/.test(code)) return message('请输入管理员生成的6位配对码')
  $('pairButton').disabled = true
  try {
    await ensureServerPermission($('serverUrl').value)
    const result = await worker({ type: 'PAIR_DEVICE', code, deviceName: $('deviceName').value, serverUrl: $('serverUrl').value })
    if (!result?.ok) throw new Error(result?.message || '配对失败')
    message('配对成功', true)
    await init()
  } catch (error) { message(error.message || String(error)) }
  finally { $('pairButton').disabled = false }
})

$('editServerButton').addEventListener('click', () => show('serverEditor', true))
$('cancelServerButton').addEventListener('click', () => show('serverEditor', false))
$('saveServerButton').addEventListener('click', async () => {
  $('saveServerButton').disabled = true
  try {
    const serverUrl = $('serverEditUrl').value
    await ensureServerPermission(serverUrl)
    const result = await worker({ type: 'UPDATE_SERVER_URL', serverUrl })
    if (!result?.ok) throw new Error(result?.message || '地址保存失败')
    $('serverLabel').textContent = result.data.serverUrl
    $('serverEditUrl').value = result.data.serverUrl
    show('serverEditor', false)
    message('审核网站地址已更新', true)
  } catch (error) { message(error.message || String(error)) }
  finally { $('saveServerButton').disabled = false }
})

$('openButton').addEventListener('click', async () => {
  const selected = selectedChoiceIndexes('taskChoices')[0]
  if (!Number.isInteger(selected)) return message('请先选择一个报告入口')
  const result = await activeTabMessage({ type: 'OPEN_REPORT_PAGE', taskChoiceIndex: selected })
  if (!result?.ok) message(result?.message || '进入报告页面失败')
  else window.close()
})

$('syncButton').addEventListener('click', showConfirmation)
$('backButton').addEventListener('click', () => {
  show('confirmPanel', false)
  show('selectionPanel')
  message('')
})
$('confirmButton').addEventListener('click', uploadConfirmedFiles)
$('refreshButton').addEventListener('click', inspect)

chrome.runtime.onMessage.addListener((event) => {
  if (event?.type !== 'SYNC_PROGRESS') return
  const progress = event.detail || {}
  if (progress.stage === 'downloading') {
    $('confirmButton').textContent = `正在下载并上传 ${progress.current}/${progress.total}…`
  } else {
    $('confirmButton').textContent = `正在上传 ${progress.current}/${progress.total}…`
  }
})

$('companyStartButton').addEventListener('click', async () => {
  $('companyStartButton').disabled = true
  try {
    const result = await worker({ type: 'START_COMPANY_VERIFICATION' })
    if (!result?.ok) throw new Error(result?.message || '无法开始企业核验')
    message(`已开始核验：${result.data.company_name}`, true)
    window.close()
  } catch (error) {
    message(error.message || String(error))
    $('companyStartButton').disabled = false
    await loadCompanyState()
  }
})

$('companyStopButton').addEventListener('click', async () => {
  $('companyStopButton').disabled = true
  try {
    await worker({ type: 'PAUSE_COMPANY_VERIFICATION', reason: '用户在 Edge 扩展中手动暂停' })
    message('企业核验已暂停', true)
    await loadCompanyState()
  } catch (error) { message(error.message || String(error)) }
  finally { $('companyStopButton').disabled = false }
})

$('batchButton').addEventListener('click', async () => {
  const [source] = await chrome.tabs.query({ active: true, currentWindow: true })
  const url = chrome.runtime.getURL('batch.html')
  const existing = (await chrome.tabs.query({})).find(tab => tab.url?.startsWith(url))
  if (existing) {
    await chrome.runtime.sendMessage({type:'BATCH_SOURCE_CHANGED',tabId:source.id}).catch(()=>{})
    await chrome.tabs.update(existing.id, { active: true })
  }
  else await chrome.tabs.create({ url: `${url}?source=${source.id}` })
  window.close()
})

init()
