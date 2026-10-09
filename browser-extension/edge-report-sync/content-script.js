const TARGET_STATUS = [
  '型式试验报告待审核',
  '型式试验报告已审核（审核未通过）',
  '型式试验报告已审核(审核未通过)'
]

const BLOCKED_STATUS = [
  '型式试验报告已审核（审核通过）',
  '型式试验报告已审核(审核通过)',
  '型式试验报告审核通过',
  '型式试验报告已通过',
  '型式试验报告审核合格'
]

const ALL_TASK_STATUS = [...TARGET_STATUS, ...BLOCKED_STATUS]

const REPORT_ENTRY_TEXT = '电子型式试验报告查看'
const VALID_FILE_MARKERS = ['总报告及产品描述报告', '有效的文件列表', '文件列表', '附件列表']
const INVALID_FILE_MARKERS = ['作废的文件列表', '无效的文件列表', '已作废']
const FILE_ATTRIBUTE_NAMES = [
  'href', 'data-href', 'data-url', 'data-file-url', 'data-download-url',
  'data-file', 'formaction', 'onclick'
]

function compact(value) {
  return String(value || '').replace(/\s+/g, '')
}

function firstMatch(text, pattern) {
  return (String(text || '').match(pattern) || [])[0] || ''
}

function elementLabel(element) {
  return String(
    element?.innerText || element?.textContent || element?.getAttribute?.('title') ||
    element?.getAttribute?.('aria-label') || element?.getAttribute?.('download') || ''
  ).trim()
}

function nearestContext(element, markers, maxDepth = 8) {
  const normalizedMarkers = markers.map(compact)
  let current = element
  for (let depth = 0; current && depth < maxDepth; depth += 1, current = current.parentElement) {
    const text = compact(current.innerText || current.textContent)
    if (normalizedMarkers.some((marker) => text.includes(marker))) return { element: current, text }
  }
  return { element, text: compact(elementLabel(element)) }
}

function uniqueMatches(text, pattern) {
  return [...new Set((String(text || '').match(pattern) || []).map(compact))]
}

function taskContext(element, maxDepth = 10) {
  let current = element
  let pendingStatus = ''
  for (let depth = 0; current && depth < maxDepth; depth += 1, current = current.parentElement) {
    const text = compact(current.innerText || current.textContent)
    const statuses = ALL_TASK_STATUS.filter((status) => text.includes(compact(status)))
    const blockedStatuses = statuses.filter((status) => BLOCKED_STATUS.includes(status))
    const targetStatuses = statuses.filter((status) => TARGET_STATUS.includes(status))

    // “审核通过”是硬边界：当前入口一旦遇到该状态，不得再向外层列表寻找可上传状态。
    if (blockedStatuses.length) {
      if (targetStatuses.length) {
        return { element: current, text, status: '', applicationNo: '', ambiguous: true }
      }
      return {
        element: current,
        text,
        status: blockedStatuses[0],
        applicationNo: '',
        ambiguous: false
      }
    }

    const normalizedTargetStatuses = [...new Set(targetStatuses.map(compact))]
    if (normalizedTargetStatuses.length > 1) {
      return { element: current, text, status: '', applicationNo: '', ambiguous: true }
    }
    if (targetStatuses.length && !pendingStatus) pendingStatus = targetStatuses[0]
    if (!pendingStatus) continue

    // 列表页可能只显示任务编号，申请编号要进入报告详情后才能读取。
    // 一个唯一的申请编号或任务编号都可以界定当前业务卡片；多个则表示已进入外层混合列表。
    const applicationNos = uniqueMatches(text, /A\d{4}CCC\d{4}-\d+/gi)
    const taskNos = uniqueMatches(text, /T\d{4}CCC-[A-Z]-\d+/gi)
    if (applicationNos.length > 1 || taskNos.length > 1) {
      return { element: current, text, status: '', applicationNo: '', taskNo: '', ambiguous: true }
    }
    if (!applicationNos.length && !taskNos.length) continue

    return {
      element: current,
      text,
      status: pendingStatus,
      applicationNo: applicationNos[0] || '',
      taskNo: taskNos[0] || '',
      ambiguous: false
    }
  }
  return { element, text: compact(elementLabel(element)), status: '', applicationNo: '', ambiguous: true }
}

function interactiveElements() {
  return [...document.querySelectorAll(
    'a, button, [role="button"], [onclick], [data-href], [data-url], [data-file-url], [data-download-url]'
  )]
}

function taskCandidates() {
  const elements = interactiveElements()
  const seen = new Set()
  const candidates = []
  elements.forEach((element, elementIndex) => {
    const label = compact(elementLabel(element))
    if (!label.includes(compact(REPORT_ENTRY_TEXT))) return
    const context = taskContext(element, 10)
    if (context.ambiguous || !TARGET_STATUS.includes(context.status)) return
    const status = context.status
    const taskNo = context.taskNo || firstMatch(context.text, /T\d{4}CCC-[A-Z]-\d+/i)
    const applicationNo = context.applicationNo
    const key = `${taskNo}|${applicationNo}|${elementIndex}`
    if (seen.has(key)) return
    seen.add(key)
    candidates.push({
      elementIndex,
      status,
      taskNo,
      applicationNo,
      label: elementLabel(element) || REPORT_ENTRY_TEXT
    })
  })
  return candidates
}

function quotedDownloadTarget(value) {
  const source = String(value || '').trim()
  if (!source || source.startsWith('#')) return ''
  const scriptLike = /^javascript:/i.test(source)
    || /^\s*(?:return\s+)?(?:window\.)?(?:open|location)/i.test(source)
    || /^\s*[\w$.]+\s*\(/.test(source)
  if (!scriptLike) {
    return source
  }
  const quoted = [...source.matchAll(/['"]([^'"]+)['"]/g)].map((match) => match[1])
  return quoted.find((item) => /^(?:https?:\/\/|\/|\.\.?\/)/i.test(item)
    || /(?:download|file|attachment|report|\.pdf(?:$|[?#]))/i.test(item)) || ''
}

function resolveHttpUrl(values, baseUrl) {
  for (const rawValue of values) {
    const candidate = quotedDownloadTarget(rawValue)
    if (!candidate || /^javascript:|^#|^void\b/i.test(candidate)) continue
    try {
      const resolved = new URL(candidate, baseUrl)
      if (/^https?:$/i.test(resolved.protocol)) return resolved.href
    } catch { /* 继续尝试下一个属性 */ }
  }
  return ''
}

function elementDownloadUrl(element) {
  const values = []
  FILE_ATTRIBUTE_NAMES.forEach((name) => {
    const value = element?.getAttribute?.(name)
    if (value) values.push(value)
  })
  if (element?.href) values.unshift(element.href)
  return resolveHttpUrl(values, location.href)
}

function safeDecode(value) {
  try { return decodeURIComponent(value) } catch { return value }
}

function filenameFromCandidate(label, url, element, fallbackNumber) {
  const direct = String(element?.getAttribute?.('download') || '').trim()
  const labelMatch = String(label || '').match(/[^/\\?&]+\.(?:pdf|docx?|xlsx?|zip|rar)(?=$|[\s）)\]】])/i)
  if (labelMatch) return labelMatch[0].trim()
  if (direct) return direct
  if (url) {
    try {
      const parsed = new URL(url)
      for (const key of ['filename', 'fileName', 'name', 'downloadName', 'file']) {
        const value = parsed.searchParams.get(key)
        if (value && /\.[a-z0-9]{2,5}$/i.test(value)) return safeDecode(value).split(/[\\/]/).pop()
      }
      const pathName = safeDecode(parsed.pathname.split('/').pop() || '')
      if (/\.[a-z0-9]{2,5}$/i.test(pathName)) return pathName
    } catch { /* 使用回退名称 */ }
  }
  const readable = String(label || '').replace(/\s+/g, ' ').trim()
  return readable && readable.length <= 120 ? readable : `待识别文件-${fallbackNumber}`
}

function classifyFile(filename, label, contextText) {
  const ownText = compact(`${filename} ${label}`)
  const context = compact(contextText)
  if (/(?:收费|缴费|付费|发票|付款|账单|费用|收款|汇款)/.test(ownText)) {
    return { category: '疑似收费文件', tone: 'warning', recommended: false }
  }
  if (/(?:申请书|委托单|通知单|合同|回执|照片|其他附件|附件材料)/.test(ownText)) {
    return { category: '其他附件', tone: 'warning', recommended: false }
  }
  if (/(?:型式试验报告|总报告|产品描述报告|检验报告|检测报告|试验报告)/.test(ownText)
      || context.includes(compact('总报告及产品描述报告'))) {
    return { category: '疑似型式试验报告', tone: 'success', recommended: true }
  }
  if (/\.pdf(?:$|[?#\s）)\]】])/i.test(`${filename} ${label}`)) {
    return { category: '其他PDF', tone: 'info', recommended: false }
  }
  return { category: '其他文件', tone: 'info', recommended: false }
}

function fileCandidates() {
  const elements = interactiveElements()
  const seen = new Set()
  const candidates = []
  elements.forEach((element, elementIndex) => {
    const label = elementLabel(element)
    const context = nearestContext(element, [...VALID_FILE_MARKERS, ...INVALID_FILE_MARKERS], 9)
    const inValidGroup = VALID_FILE_MARKERS.some((marker) => context.text.includes(compact(marker)))
    const inInvalidGroup = INVALID_FILE_MARKERS.some((marker) => context.text.includes(compact(marker)))
    const attributeText = FILE_ATTRIBUTE_NAMES.map((name) => element?.getAttribute?.(name) || '').join(' ')
    const looksLikeFile = /\.(?:pdf|docx?|xlsx?|zip|rar)(?:$|[?#'"\s）)\]])/i.test(`${label} ${attributeText}`)
      || /(?:下载|文件名|附件|报告)/.test(label)
    if (inInvalidGroup || (!inValidGroup && !looksLikeFile)) return

    const url = elementDownloadUrl(element)
    const filename = filenameFromCandidate(label, url, element, elementIndex + 1)
    const identity = `${url || `element:${elementIndex}`}|${filename}`
    if (seen.has(identity)) return
    seen.add(identity)
    const kind = classifyFile(filename, label, context.text)
    const sourceGroup = context.text.includes(compact('总报告及产品描述报告'))
      ? '总报告及产品描述报告'
      : context.text.includes(compact('有效的文件列表')) ? '有效的文件列表' : '文件列表'
    candidates.push({
      elementIndex,
      url,
      filename,
      sourceGroup,
      category: kind.category,
      tone: kind.tone,
      recommended: kind.recommended,
      selectable: Boolean(url),
      note: url ? '由您确认后上传' : '未解析到直接下载地址，暂不能上传'
    })
  })
  return candidates.sort((a, b) => {
    if (a.selectable !== b.selectable) return a.selectable ? -1 : 1
    if (a.recommended !== b.recommended) return a.recommended ? -1 : 1
    return a.filename.localeCompare(b.filename, 'zh-CN')
  })
}

async function inspectPage() {
  const bodyText = compact(document.body?.innerText)
  const tasks = taskCandidates()
  if (tasks.length) {
    return {
      pageType: 'task', ready: false, canOpenReport: true,
      pageUrl: location.href,
      tasks
    }
  }

  const pending = (await chrome.storage.local.get('pendingTask')).pendingTask || {}
  const files = fileCandidates()
  if (files.length) {
    const pendingIsCurrent = pending.status && TARGET_STATUS.some((status) => compact(status) === compact(pending.status))
      && Date.now() - Number(pending.savedAt || 0) < 30 * 60 * 1000
    if (!pendingIsCurrent) {
      return {
        pageType: 'report-files', ready: false, canOpenReport: false,
        pageUrl: location.href,
        message: '为确保文件归属正确，请先回到目标业务页，通过扩展选择一个“电子型式试验报告查看”入口。'
      }
    }
    const selectableCount = files.filter((item) => item.selectable).length
    return {
      pageType: 'report-files',
      ready: selectableCount > 0,
      pageUrl: location.href,
      files,
      taskNo: firstMatch(bodyText, /T\d{4}CCC-[A-Z]-\d+/i) || pending.taskNo || '',
      applicationNo: firstMatch(bodyText, /A\d{4}CCC\d{4}-\d+/i) || pending.applicationNo || '',
      sourceStatus: pending.status || '',
      entryLabel: pending.entryLabel || REPORT_ENTRY_TEXT,
      message: selectableCount ? '' : '已找到文件条目，但未解析到可直接下载的地址。'
    }
  }
  return {
    pageType: 'unsupported', ready: false, canOpenReport: false,
    pageUrl: location.href,
    message: '当前页面未找到目标报告入口，也未找到可选择的文件。请确认页面已经加载完成。'
  }
}

function registerMessageListener() {
  chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
    if (message?.type === 'SCAN_APPROVING_BATCH') {
      try { sendResponse({ ok: true, data: BatchParser.scan(document, location.href) }) }
      catch (error) { sendResponse({ ok: false, message: error.message }) }
      return
    }
    if (message?.type === 'INSPECT_PAGE') {
      inspectPage().then(sendResponse)
      return true
    }
    if (message?.type === 'OPEN_REPORT_PAGE') {
      const tasks = taskCandidates()
      const task = tasks[Number(message.taskChoiceIndex)]
      const element = interactiveElements()[task?.elementIndex]
      if (!task || !element) {
        sendResponse({ ok: false, message: '未找到所选报告入口，请重新识别页面' })
        return
      }
      chrome.storage.local.set({
        pendingTask: {
          taskNo: task.taskNo,
          applicationNo: task.applicationNo,
          status: task.status,
          entryLabel: task.label,
          sourceUrl: location.href,
          savedAt: Date.now()
        }
      }).then(() => {
        element.click()
        sendResponse({ ok: true })
      })
      return true
    }
  })
}

if (typeof chrome !== 'undefined' && chrome.runtime?.onMessage) registerMessageListener()

if (typeof module !== 'undefined') {
  module.exports = {
    compact,
    quotedDownloadTarget,
    resolveHttpUrl,
    filenameFromCandidate,
    classifyFile,
    taskCandidates,
    fileCandidates
  }
}
