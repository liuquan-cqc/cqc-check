/* Strict adapter for the approving-list page; never execute page JavaScript. */
const BatchParser = (() => {
  const origin = 'https://certification.example'
  const listPath = '/inner/tests.TestReportCtl.queryListTestReportByApproving.do'
  const detailPath = '/inner/tests.TestReportCtl.testReportListUploadedAffirm.do'
  const downloadPath = '/inner/sys.SysUploadedFileCtl.downloadFile.do'
  const text = el => String(el?.textContent || '').replace(/\s+/g, '')
  const unique = (s, re) => [...new Set(s.match(re) || [])]
  function target(el, base, path) {
    for (const attr of ['href', 'data-href', 'data-url', 'onclick']) {
      const raw = el.getAttribute(attr) || ''
      const values = /^(?:javascript:|.*\()/i.test(raw) ? [...raw.matchAll(/['"]([^'"]+)['"]/g)].map(x => x[1]) : [raw]
      for (const value of values) {
        try {
          const u = new URL(value, base)
          if (u.origin === origin && u.pathname === path && !u.username && !u.password) return u.href
        } catch {}
      }
    }
    return ''
  }
  function scan(doc, base) {
    const u = new URL(base)
    if (u.origin !== origin || u.pathname !== listPath) throw Error('请在试验报告待审核列表页扫描；不自动翻页。')
    const table = [...doc.querySelectorAll('table')].find(t => [...t.rows].some(r => [...r.cells].some(c => text(c) === '操作') && text(r).includes('试验任务编号')))
    if (!table) throw Error('未找到明确的任务表格，请保留页面供核验，不猜测入口。')
    const header = [...table.rows].find(r => [...r.cells].some(c => text(c) === '操作'))
    const op = [...header.cells].findIndex(c => text(c) === '操作')
    const tasks = [], issues = [], seen = new Set()
    for (const row of [...table.rows].slice([...table.rows].indexOf(header) + 1)) {
      const s = text(row)
      if (!s) continue
      const apps = unique(s, /A\d{4}CCC\d{4}-\d+/g), ids = unique(s, /T\d{4}CCC-[A-Z]-\d+/g)
      const links = [...(row.cells[op]?.querySelectorAll('a,button,[onclick]') || [])].filter(a => text(a) === '查看')
      const url = links.length === 1 ? target(links[0], base, detailPath) : ''
      if (apps.length !== 1 || ids.length !== 1 || !url) { issues.push(`第${row.rowIndex + 1}行：编号或查看链接无法唯一解析`); continue }
      const p = new URL(url).searchParams
      if (p.get('appNumber') !== apps[0] || p.get('taskNumber') !== ids[0] || p.get('view') !== '1') { issues.push(`${ids[0]}：链接与业务行不一致或不是只读查看`); continue }
      const key = `${ids[0]}|${apps[0]}`
      if (seen.has(key)) { issues.push(`${ids[0]}：重复业务行，请核对`); continue }
      seen.add(key)
      tasks.push({ key, taskNo: ids[0], applicationNo: apps[0], url, selected: true, status: '待采集', files: [] })
    }
    if (!tasks.length) throw Error('没有可安全解析的任务。' + issues.join('；'))
    return { tasks, issues, pageUrl: base, scannedAt: Date.now() }
  }
  function detail(doc, task) {
    const s = text(doc.body)
    const apps = unique(s, /A\d{4}CCC\d{4}-\d+/g), ids = unique(s, /T\d{4}CCC-[A-Z]-\d+/g)
    if (apps.length !== 1 || apps[0] !== task.applicationNo || ids.length !== 1 || ids[0] !== task.taskNo) throw Error('详情编号不匹配或登录失效；停止该任务')
    const headings = [...doc.querySelectorAll('*')].filter(el => text(el) === '有效的文件列表' && ![...el.children].some(c => text(c) === '有效的文件列表'))
    if (headings.length !== 1) throw Error('无法唯一定位有效文件区域')
    // 标题和文件表可能是兄弟节点；不能假定它们有一个不含作废区的共同父节点。
    // 有效区必须有明确结束边界，不以“扫描全页PDF”兜底。
    const boundaryNames = ['作废的文件列表', '无效的文件列表', '查看以往审核历史', '提交整改']
    const boundaries = [...doc.querySelectorAll('*')].filter(el => boundaryNames.includes(text(el))
      && ![...el.children].some(c => text(c) === text(el)))
    const end = boundaries.find(el => headings[0].compareDocumentPosition(el) & 4)
    let group = headings[0], files = []
    if (end) {
      const range = doc.createRange()
      range.setStartAfter(headings[0])
      range.setEndBefore(end)
      group = range.cloneContents()
      files = [...group.querySelectorAll('a')].filter(a => /\.pdf$/i.test(text(a)))
    } else {
      // 无结束标题的旧页面仅接受明确的语义分区，绝不扩展到body/html。
      group = headings[0].closest('section,fieldset')
      if (group && !/作废的文件列表|无效的文件列表/.test(text(group))) {
        files = [...group.querySelectorAll('a')].filter(a => /\.pdf$/i.test(text(a))
          && (headings[0].compareDocumentPosition(a) & 4))
      }
    }
    if (!files.length) throw Error(end
      ? '已定位有效文件标题和结束边界，但返回的HTML中没有PDF链接；可能为动态加载，需核验实际页面'
      : '有效文件区域缺少明确结束边界，停止采集以避免混入作废文件')
    files = files.map(a => ({filename: text(a), url: target(a, task.url, downloadPath)}))
    const seen = new Set()
    return files.map(f => {
      if (!f.url || !/^[a-f\d]{32}$/i.test(new URL(f.url).searchParams.get('objID') || '')) throw Error('文件下载地址无法安全解析')
      if (seen.has(f.url)) return null
      seen.add(f.url)
      return { ...f, key: `${task.key}|${f.url}`, selected: true, status: '待处理', reportType: '服务器将从PDF内容自动识别类别' }
    }).filter(Boolean)
  }
  return { scan, detail, target, origin, detailPath }
})()
if (typeof module !== 'undefined') module.exports = BatchParser
