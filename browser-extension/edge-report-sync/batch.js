const $ = id => document.getElementById(id)
const storeKey = 'approvingBatchV1'
let state = null, busy = false, paused = false, config = null
let source = Number(new URL(location.href).searchParams.get('source'))
chrome.runtime.onMessage.addListener(message => {
  if(message?.type === 'BATCH_SOURCE_CHANGED' && !busy && Number.isInteger(message.tabId)) {
    source = message.tabId
    history.replaceState(null,'',`?source=${source}`)
    status('扫描来源已更新；现有批次保留，点击扫描可读取新页面。')
  }
})
const escape = s => String(s || '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))
async function save() { await chrome.storage.local.set({ [storeKey]: state }); render() }
function status(s) { $('status').textContent = s }
function render() {
  $('issues').textContent = state?.issues?.join('；') || ''
  $('tasks').innerHTML = (state?.tasks || []).map((t,i) => `<article><label><input type="checkbox" data-task="${i}" ${t.selected?'checked':''} ${busy?'disabled':''}>${escape(t.taskNo)} · ${escape(t.applicationNo)}</label><small>${escape(t.status)} ${escape(t.error)}</small>${t.files.map(f => `<label>${escape(f.filename)} · ${escape(f.status)}</label><small>${escape(f.reportType)} ${escape(f.error)}</small>`).join('')}</article>`).join('')
  for (const id of ['scan','allTasks','collect','upload']) $(id).disabled = busy
  $('pause').disabled = !busy
}
async function run(fn) {
  if (busy) return
  await navigator.locks.request('approving-batch-run', {ifAvailable:true}, async lock => {
    if (!lock) return status('另一个批量页面正在操作，请回到该页面。')
    busy = true; paused = false; render()
    try { await fn() } catch(e) { status(e.message || String(e)) }
    finally { busy = false; render() }
  })
}
$('tasks').addEventListener('change', async e => {
  if (busy) return
  const d = e.target.dataset
  if (d.task !== undefined) state.tasks[Number(d.task)].selected = e.target.checked
  if (d.file !== undefined) state.tasks[Number(d.owner)].files[Number(d.file)].selected = e.target.checked
  await save()
})
$('tasks').addEventListener('click', async e => {
  if (busy || !e.target.dataset.resolve) return
  const [i,j] = e.target.dataset.resolve.split(':').map(Number)
  Object.assign(state.tasks[i].files[j], {status:'待处理', error:'', selected:true})
  await save()
})
$('scan').onclick = () => run(async () => {
  if (state && !confirm('新建批次将替换本机现有清单，继续吗？')) return
  const reply = await chrome.tabs.sendMessage(source, {type:'SCAN_APPROVING_BATCH'})
  if (!reply?.ok) throw Error(reply?.message || '请回到待审核列表，刷新后重新打开批量入口')
  state = {...reply.data, serverUrl: config.serverUrl}
  await save(); status(`识别${state.tasks.length}个任务，${state.issues.length}项异常；请与原页面数量核对。`)
})
$('allTasks').onclick = async () => { if (!state || busy) return; state.tasks.forEach(t => t.selected = true); await save() }
$('pause').onclick = () => { paused = true; status('已请求暂停，等待当前操作结束。') }
async function collectFiles() {
  if (!state) throw Error('请先扫描原列表页')
  for (const task of state.tasks.filter(t => t.selected && t.status !== '已采集')) {
    if (paused) break
    task.status = '采集中'; task.error = ''; await save()
    try {
      const response = await fetch(task.url, {credentials:'include', cache:'no-store', redirect:'error', signal:AbortSignal.timeout(60000)})
      if (!response.ok) throw Error(`读取详情失败 HTTP ${response.status}`)
      const raw = await response.arrayBuffer()
      if (raw.byteLength > 10*1024*1024) throw Error('详情页过大，停止解析')
      const charset = /charset=([\w-]+)/i.exec(response.headers.get('content-type') || '')?.[1] || 'utf-8'
      const doc = new DOMParser().parseFromString(new TextDecoder(charset).decode(raw),'text/html')
      task.files = BatchParser.detail(doc,task); task.status = '已采集'
    } catch(e) { task.status = '采集失败'; task.error = e.message }
    await save()
  }
  status('采集已结束/暂停。有效PDF已自动纳入处理；收费附件由服务器识别后跳过。')
}
$('collect').onclick = () => run(collectFiles)
$('upload').onclick = () => run(async () => {
  if (!state) throw Error('请先采集报告')
  const capability = await chrome.runtime.sendMessage({type:'GET_BATCH_CAPABILITIES'})
  if (!capability?.ok || capability.data?.typed_intake !== true) throw Error('服务器尚未启用按任务和报告类别自动归属；请先更新配套服务。')
  const latest = await chrome.runtime.sendMessage({type:'GET_CONFIG'})
  if (!latest?.ok || latest.data.serverUrl !== state.serverUrl) throw Error('审核网站地址已变化，请核对后新建批次')
  await collectFiles()
  if (paused) return
  const selected = state.tasks.filter(t=>t.selected).flatMap(t => t.files.filter(f => !['完成','重复跳过','附件已跳过'].includes(f.status)).map(f => ({t,f})))
  if (!selected.length) throw Error('没有待处理PDF；已成功接收的文件不会重复提交')
  for (const {t,f} of selected) {
    if (paused) break
    f.status = '上传中'; f.error = ''; await save()
    try {
      const reply = await chrome.runtime.sendMessage({type:'SYNC_REPORT', payload:{autoClassify:true, expectedServer:state.serverUrl, taskNo:t.taskNo, applicationNo:t.applicationNo, sourceUrl:t.url, pdf:f}})
      if (!reply?.ok) throw Error(reply?.message || '未收到成功回执')
      f.status = reply.data.action === 'duplicate' ? '重复跳过' : reply.data.action === 'skipped' ? '附件已跳过' : '完成'
      f.reportType = reply.data.category_label || reply.data.message || f.reportType
      f.error = reply.data.message || ''
      f.result = {action:reply.data.action, report_id:reply.data.report_id}; f.selected = false
    } catch(e) { f.status = '异常待重试'; f.error = e.message; f.selected = false }
    await save()
  }
  const files = state.tasks.flatMap(t=>t.files)
  status(`接收${files.filter(f=>f.status==='完成').length}份，重复跳过${files.filter(f=>f.status==='重复跳过').length}份，附件跳过${files.filter(f=>f.status==='附件已跳过').length}份，异常${files.filter(f=>f.status==='异常待重试').length}份。接收成功不等于审核通过。`)
})
async function init() {
  const reply = await chrome.runtime.sendMessage({type:'GET_CONFIG'})
  if (!reply?.ok || !reply.data.deviceToken) throw Error('请先在插件弹窗完成配对')
  config = reply.data
  $('destination').textContent = `上传目的地：${config.serverUrl}`
  state = (await chrome.storage.local.get(storeKey))[storeKey] || null
  if (state) {
    for (const t of state.tasks) {
      if (t.status === '采集中') t.status = '采集失败'
      for (const f of t.files) if (f.status === '上传中') { f.status = '异常待重试'; f.selected = true; f.error = '页面中断；再次处理会按同任务同类别文件指纹去重' }
    }
    await save(); status('已恢复本机批次；不会自动上传。')
  } else render()
}
init().catch(e => {status(e.message); for(const id of ['scan','allTasks','collect','upload','pause']) $(id).disabled=true})
