const TYC_STATUS_WORDS = [
  '存续', '在业', '开业', '正常', '营业', '迁入', '迁出', '注销', '已注销', '吊销', '吊销未注销', '清算', '清算中'
]

function tycCompact(value) {
  return String(value || '').replace(/[\s　]+/g, '').replace(/\(/g, '（').replace(/\)/g, '）')
}

function tycVisible(element) {
  if (!element) return false
  const style = getComputedStyle(element)
  const rect = element.getBoundingClientRect()
  return style.display !== 'none' && style.visibility !== 'hidden' && rect.width > 0 && rect.height > 0
}

function tycBanner(text, type = 'info') {
  let banner = document.getElementById('ccc-tyc-verification-banner')
  if (!banner) {
    banner = document.createElement('div')
    banner.id = 'ccc-tyc-verification-banner'
    Object.assign(banner.style, {
      position: 'fixed', right: '20px', bottom: '20px', zIndex: '2147483647', maxWidth: '380px',
      padding: '13px 16px', borderRadius: '10px', color: '#fff', font: '13px/1.55 Microsoft YaHei, sans-serif',
      boxShadow: '0 12px 32px rgba(0,0,0,.22)'
    })
    document.documentElement.appendChild(banner)
  }
  banner.style.background = type === 'error' ? '#b42318' : type === 'success' ? '#16803c' : '#2457a6'
  banner.textContent = text
}

async function tycWorker(payload) {
  const response = await chrome.runtime.sendMessage(payload)
  if (!response?.ok) throw new Error(response?.message || '扩展通信失败')
  return response.data
}

function tycBlocker(bodyText) {
  if (location.pathname.includes('/login')) return '需要登录天眼查'
  const compact = tycCompact(bodyText)
  const markers = [
    ['今日查询次数已用完', '今日免费次数已用完'],
    ['请完成安全验证', '访问验证', '拖动滑块完成拼图', '点击完成验证'],
    ['请先登录']
  ]
  for (const group of markers) {
    const matched = group.find((marker) => compact.includes(tycCompact(marker)))
    if (matched) return matched
  }
  return ''
}

function tycExactCompanyLink(companyName) {
  const expected = tycCompact(companyName)
  const links = [...document.querySelectorAll('a[href*="/company/"]')].filter(tycVisible)
  return links.find((link) => {
    if (tycCompact(link.innerText) === expected) return true
    const context = link.closest('li, article, [class*="search"], [class*="result"], .card')
    if (!context) return false
    const headings = [...context.querySelectorAll('h1,h2,h3,h4,a,span')].filter(tycVisible)
    return headings.some((item) => tycCompact(item.innerText) === expected)
  }) || null
}

function tycDirectText(element) {
  return [...(element?.childNodes || [])]
    .filter((node) => node.nodeType === Node.TEXT_NODE)
    .map((node) => node.textContent || '')
    .join('')
    .trim()
}

function tycExactVisibleTextElements(expectedText, maxTop = Infinity) {
  const expected = tycCompact(expectedText)
  if (!expected || !document.body) return []
  const matches = []
  const seen = new Set()
  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT)
  let node
  while ((node = walker.nextNode())) {
    if (tycCompact(node.textContent) !== expected) continue
    const element = node.parentElement
    if (!element || seen.has(element) || !tycVisible(element)) continue
    const rect = element.getBoundingClientRect()
    if (rect.bottom < -50 || rect.top > maxTop) continue
    seen.add(element)
    matches.push(element)
  }
  return matches
}

function tycPageCompanyElement(expectedName) {
  const expected = tycCompact(expectedName)
  const selectors = ['h1', '[class*="company-name"]', '[class*="companyName"]', '[class*="name"] h1']
  for (const selector of selectors) {
    for (const item of document.querySelectorAll(selector)) {
      if (tycVisible(item) && tycCompact(item.innerText) === expected) return item
    }
  }

  // 天眼查的企业标题类名会随前端版本变化。直接查找页面上方的可见文本节点，
  // 但仍要求企业全称完全一致，避免把相似企业误写入核验结果。
  const visibleTextMatches = tycExactVisibleTextElements(expectedName, 900)
  if (visibleTextMatches.length) {
    return visibleTextMatches.sort((a, b) => {
      const aRect = a.getBoundingClientRect()
      const bRect = b.getBoundingClientRect()
      return aRect.top - bRect.top || aRect.left - bRect.left
    })[0]
  }

  // 兼容标题文字节点旁边还带有图标等兄弟节点的情况。
  const candidates = [...document.querySelectorAll('h1,h2,h3,h4,span,div')]
  return candidates.find((item) => {
    if (!tycVisible(item)) return false
    const rect = item.getBoundingClientRect()
    return rect.bottom >= -50 && rect.top < 900 && tycCompact(tycDirectText(item)) === expected
  }) || null
}

function tycRegistrationStatus(companyElement) {
  const companyRect = companyElement?.getBoundingClientRect()
  const nearbyStatuses = TYC_STATUS_WORDS.flatMap((word) => tycExactVisibleTextElements(word, 1200))
  if (nearbyStatuses.length) {
    nearbyStatuses.sort((a, b) => {
      if (!companyRect) return a.getBoundingClientRect().top - b.getBoundingClientRect().top
      const aRect = a.getBoundingClientRect()
      const bRect = b.getBoundingClientRect()
      const aDistance = Math.abs(aRect.top - companyRect.top) + Math.abs(aRect.left - companyRect.right) * 0.25
      const bDistance = Math.abs(bRect.top - companyRect.top) + Math.abs(bRect.left - companyRect.right) * 0.25
      return aDistance - bDistance
    })
    return tycCompact(nearbyStatuses[0].textContent)
  }

  const elements = [...document.querySelectorAll('span,em,b,div')].filter((item) => {
    if (!tycVisible(item)) return false
    const rect = item.getBoundingClientRect()
    if (rect.top > 1100 || rect.width > 260 || rect.height > 80) return false
    return TYC_STATUS_WORDS.includes(tycCompact(item.innerText))
  })
  return elements.length ? tycCompact(elements[0].innerText) : ''
}

function tycCreditCode() {
  const text = String(document.body?.innerText || '')
  const labelled = text.match(/统一社会信用代码\s*[:：]?\s*([0-9A-Z]{18})/i)
  return (labelled?.[1] || '').toUpperCase()
}

async function tycPause(reason) {
  tycBanner(`${reason}。自动核验已暂停，请您手动处理后再点扩展继续。`, 'error')
  await tycWorker({ type: 'PAUSE_COMPANY_VERIFICATION', reason }).catch(() => {})
}

async function tycRun() {
  const state = await tycWorker({ type: 'GET_COMPANY_VERIFICATION_STATE' }).catch(() => null)
  const task = state?.task
  if (!state?.autoRun || !task) return
  const blocker = tycBlocker(document.body?.innerText)
  if (blocker) return tycPause(`天眼查页面提示：${blocker}`)

  if (location.pathname.startsWith('/search')) {
    tycBanner(`正在查找精确企业：${task.company_name}`)
    const link = tycExactCompanyLink(task.company_name)
    if (link?.href) {
      location.href = link.href
      return
    }
    tycBanner(`未找到与“${task.company_name}”完全一致的结果，未写入任何数据。`, 'error')
    return
  }

  if (location.pathname.includes('/company/')) {
    const companyElement = tycPageCompanyElement(task.company_name)
    if (!companyElement) {
      tycBanner('尚未在当前详情页精确匹配待核验企业，不会写入数据。', 'error')
      return
    }
    const companyName = String(companyElement.textContent || task.company_name).trim()
    const rawStatus = tycRegistrationStatus(companyElement)
    if (!rawStatus) {
      tycBanner('已精确匹配企业，但暂未读取到登记状态；请等待页面加载后在扩展中重试。', 'error')
      return
    }
    tycBanner(`已精确匹配，正在回传：${companyName}（${rawStatus}）`)
    try {
      await tycWorker({
        type: 'SUBMIT_COMPANY_VERIFICATION',
        taskId: task.task_id,
        result: {
          company_name: companyName,
          raw_status: rawStatus,
          unified_social_credit_code: tycCreditCode(),
          source_url: location.href
        }
      })
      tycBanner(`${companyName}核验完成：${rawStatus}。稍后自动处理下一家。`, 'success')
    } catch (error) {
      await tycPause(error?.message || String(error))
    }
  }
}

setTimeout(tycRun, 2200)
setTimeout(tycRun, 6500)
setTimeout(tycRun, 12000)
