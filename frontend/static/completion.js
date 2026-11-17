(() => {
  'use strict'

  if (window.mewcodeDesktop) document.body.classList.add('is-desktop')

  const SAMPLE = `import java.util.Scanner;

public class Main {
    public static void main(String[] args) {
        Scanner in = new Scanner(System.in);
        int count = in.nextInt();
        String word = in.next
    }
}`

  const editor = document.getElementById('code-editor')
  const lineNumbers = document.getElementById('line-numbers')
  const cursorPosition = document.getElementById('cursor-position')
  const requestButton = document.getElementById('request-completion')
  const autoComplete = document.getElementById('auto-complete')
  const candidateList = document.getElementById('candidate-list')
  const candidateCount = document.getElementById('candidate-count')
  const completionState = document.getElementById('completion-state')
  const capabilityStatus = document.getElementById('capability-status')
  const feedbackStatus = document.getElementById('feedback-status')
  const toast = document.getElementById('toast')

  let debounceTimer = 0
  let toastTimer = 0
  let requestSequence = 0
  let documentVersion = 0
  let activeController = null
  let activeCompletion = null
  let activeCompletionVersion = -1

  function setTheme(theme) {
    const normalized = theme === 'light' ? 'light' : 'dark'
    document.documentElement.setAttribute('data-theme', normalized)
    const icon = document.getElementById('completion-theme-icon')
    if (icon) icon.setAttribute('href', normalized === 'dark' ? '/static/icons.svg#sun' : '/static/icons.svg#moon')
    try { localStorage.setItem('mewcode-theme', normalized) } catch (error) { /* ignore */ }
    if (window.mewcodeDesktop && typeof window.mewcodeDesktop.setWindowTheme === 'function') {
      window.mewcodeDesktop.setWindowTheme(normalized).catch(() => {})
    }
  }

  function showToast(message, error = false) {
    window.clearTimeout(toastTimer)
    toast.textContent = message
    toast.classList.toggle('is-error', error)
    toast.classList.add('is-visible')
    toastTimer = window.setTimeout(() => toast.classList.remove('is-visible'), 2600)
  }

  function updateLineNumbers() {
    const count = Math.max(1, editor.value.split('\n').length)
    lineNumbers.textContent = Array.from({ length: count }, (_, index) => index + 1).join('\n')
    lineNumbers.scrollTop = editor.scrollTop
  }

  function updateCursorPosition() {
    const before = editor.value.slice(0, editor.selectionStart)
    const lines = before.split('\n')
    cursorPosition.textContent = `Ln ${lines.length}, Col ${lines.at(-1).length + 1}`
  }

  function memberContextAtCursor() {
    const before = editor.value.slice(0, editor.selectionStart)
    return /[A-Za-z_$][\w$]*\.[A-Za-z_$\w]*$/.test(before)
  }

  function setCompletionState(kind, title, detail) {
    completionState.className = `completion-state${kind ? ` is-${kind}` : ''}`
    completionState.replaceChildren()
    const glyph = document.createElement('span')
    glyph.className = 'state-glyph'
    glyph.setAttribute('aria-hidden', 'true')
    glyph.textContent = kind === 'error' ? '!' : kind === 'loading' ? '···' : '⌁'
    const strong = document.createElement('strong')
    strong.textContent = title
    const paragraph = document.createElement('p')
    paragraph.textContent = detail
    completionState.append(glyph, strong, paragraph)
  }

  function clearAnalysis() {
    document.getElementById('receiver').textContent = '—'
    document.getElementById('resolved-type').textContent = '—'
    document.getElementById('type-inference').textContent = '—'
    document.getElementById('confidence').textContent = '—'
    document.getElementById('latency').textContent = '— ms'
    const path = document.getElementById('graph-path')
    path.replaceChildren()
    const item = document.createElement('li')
    item.className = 'is-muted'
    item.textContent = '尚未运行'
    path.append(item)
  }

  function renderAnalysis(payload) {
    const analysis = payload.analysis || {}
    document.getElementById('receiver').textContent = analysis.receiver || '—'
    document.getElementById('resolved-type').textContent = analysis.resolved_type || '未解析'
    document.getElementById('type-inference').textContent = analysis.type_inference || 'unresolved'
    document.getElementById('confidence').textContent = `${Math.round((analysis.confidence || 0) * 100)}%`
    document.getElementById('latency').textContent = `${Number(payload.latency_ms || 0).toFixed(2)} ms`
    const graph = document.getElementById('graph-path')
    graph.replaceChildren()
    const nodes = Array.isArray(analysis.graph_path) ? analysis.graph_path : []
    for (const node of nodes) {
      const item = document.createElement('li')
      item.className = 'is-active'
      item.textContent = node
      graph.append(item)
    }
    if (!nodes.length) {
      const item = document.createElement('li')
      item.className = 'is-muted'
      item.textContent = '无执行节点'
      graph.append(item)
    }
  }

  async function sendFeedback(candidate, accepted) {
    if (!activeCompletion) return
    try {
      const response = await fetch('/api/completion/feedback', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          completion_id: activeCompletion.completion_id,
          candidate_id: candidate.candidate_id,
          accepted
        })
      })
      if (!response.ok) throw new Error(`反馈接口返回 ${response.status}`)
      const result = await response.json()
      feedbackStatus.textContent = result.recorded
        ? (accepted ? `已学习：接受 ${candidate.label}` : `已学习：忽略 ${candidate.label}`)
        : '本候选已记录过反馈'
    } catch (error) {
      feedbackStatus.textContent = ''
      showToast(error.message || '反馈提交失败', true)
    }
  }

  function applyCandidate(candidate) {
    if (activeCompletionVersion !== documentVersion) {
      showToast('文档已变化，已阻止应用过期候选', true)
      return
    }
    const source = editor.value
    if (candidate.replacement_start > source.length || candidate.replacement_end > source.length) {
      showToast('候选替换范围无效', true)
      return
    }
    editor.value = source.slice(0, candidate.replacement_start) + candidate.insert_text + source.slice(candidate.replacement_end)
    const cursor = candidate.replacement_start + candidate.insert_text.length
    editor.setSelectionRange(cursor, cursor)
    documentVersion += 1
    updateLineNumbers()
    updateCursorPosition()
    editor.focus()
    sendFeedback(candidate, true)
    showToast(`已应用 ${candidate.label}`)
  }

  function renderCandidates(payload) {
    activeCompletion = payload
    activeCompletionVersion = documentVersion
    candidateList.replaceChildren()
    const candidates = Array.isArray(payload.candidates) ? payload.candidates : []
    candidateCount.textContent = String(candidates.length)
    renderAnalysis(payload)

    if (!candidates.length) {
      setCompletionState('', '没有匹配候选', '分析已完成；可以补充声明、调整前缀或移动光标后重试。')
      return
    }

    completionState.className = 'completion-state is-hidden'
    candidates.forEach((candidate, index) => {
      const row = document.createElement('div')
      row.className = 'candidate-row'
      const main = document.createElement('button')
      main.className = 'candidate-main'
      main.type = 'button'
      main.dataset.testid = `candidate-${index}`
      main.setAttribute('aria-label', `应用候选 ${candidate.label}`)
      main.addEventListener('click', () => applyCandidate(candidate))
      const icon = document.createElement('span')
      icon.className = 'candidate-icon'
      icon.textContent = candidate.kind === 'method' ? 'fn' : candidate.kind === 'field' ? 'fd' : 'sn'
      const copy = document.createElement('span')
      copy.className = 'candidate-copy'
      const label = document.createElement('strong')
      label.textContent = candidate.label
      const detail = document.createElement('small')
      detail.textContent = `${candidate.detail || candidate.kind} · ${candidate.source}`
      copy.append(label, detail)
      const score = document.createElement('span')
      score.className = 'candidate-score'
      score.textContent = `${Math.round(candidate.score * 100)}%`
      main.append(icon, copy, score)
      const reject = document.createElement('button')
      reject.className = 'candidate-reject'
      reject.type = 'button'
      reject.title = `忽略 ${candidate.label}`
      reject.setAttribute('aria-label', `忽略候选 ${candidate.label}`)
      reject.innerHTML = '<svg class="ui-icon" aria-hidden="true"><use href="/static/icons.svg#close"></use></svg>'
      reject.addEventListener('click', async () => {
        reject.disabled = true
        await sendFeedback(candidate, false)
      })
      row.append(main, reject)
      candidateList.append(row)
    })
  }

  async function requestCompletion() {
    window.clearTimeout(debounceTimer)
    if (activeController) activeController.abort()
    const controller = new AbortController()
    activeController = controller
    const sequence = ++requestSequence
    const requestedVersion = documentVersion
    requestButton.disabled = true
    feedbackStatus.textContent = ''
    candidateList.replaceChildren()
    candidateCount.textContent = '…'
    setCompletionState('loading', '正在分析当前光标', '轻量语法树、别名学习和成员检索正在依次执行。')

    try {
      const response = await fetch('/api/completion/complete', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        signal: controller.signal,
        body: JSON.stringify({
          language: 'java',
          source: editor.value,
          cursor_offset: editor.selectionStart,
          max_results: 12
        })
      })
      if (!response.ok) {
        const body = await response.json().catch(() => ({}))
        throw new Error(typeof body.detail === 'string' ? body.detail : `补全接口返回 ${response.status}`)
      }
      const payload = await response.json()
      if (sequence !== requestSequence || requestedVersion !== documentVersion) return
      renderCandidates(payload)
    } catch (error) {
      if (error.name === 'AbortError') return
      if (sequence !== requestSequence) return
      candidateCount.textContent = '0'
      setCompletionState('error', '补全请求失败', error.message || '请确认服务已启动后重试。')
    } finally {
      if (sequence === requestSequence) {
        requestButton.disabled = false
        activeController = null
      }
    }
  }

  function scheduleCompletion() {
    window.clearTimeout(debounceTimer)
    if (!autoComplete.checked || !memberContextAtCursor()) return
    debounceTimer = window.setTimeout(requestCompletion, 100)
  }

  async function loadCapabilities() {
    try {
      const response = await fetch('/api/completion/capabilities')
      if (!response.ok) throw new Error(`HTTP ${response.status}`)
      const payload = await response.json()
      const nodeCount = Array.isArray(payload.graph_nodes) ? payload.graph_nodes.length : 0
      capabilityStatus.className = 'status-pill'
      capabilityStatus.querySelector('span').textContent = `Java · ${nodeCount} 节点 · 在线`
    } catch (error) {
      capabilityStatus.className = 'status-pill is-error'
      capabilityStatus.querySelector('span').textContent = '补全服务不可用'
    }
  }

  function resetEditor() {
    if (activeController) activeController.abort()
    requestSequence += 1
    editor.value = SAMPLE
    const cursor = SAMPLE.lastIndexOf('in.next') + 'in.next'.length
    editor.setSelectionRange(cursor, cursor)
    documentVersion += 1
    activeCompletion = null
    candidateList.replaceChildren()
    candidateCount.textContent = '0'
    feedbackStatus.textContent = ''
    setCompletionState('', '等待成员表达式', '在编辑器中输入类似 in.next 的内容。')
    clearAnalysis()
    updateLineNumbers()
    updateCursorPosition()
    editor.focus()
  }

  editor.addEventListener('input', () => {
    documentVersion += 1
    updateLineNumbers()
    updateCursorPosition()
    scheduleCompletion()
  })
  editor.addEventListener('click', updateCursorPosition)
  editor.addEventListener('keyup', updateCursorPosition)
  editor.addEventListener('scroll', () => { lineNumbers.scrollTop = editor.scrollTop })
  editor.addEventListener('keydown', event => {
    if (event.key === 'Tab') {
      event.preventDefault()
      const start = editor.selectionStart
      const end = editor.selectionEnd
      editor.setRangeText('    ', start, end, 'end')
      editor.dispatchEvent(new Event('input'))
      return
    }
    if ((event.ctrlKey || event.metaKey) && event.code === 'Space') {
      event.preventDefault()
      requestCompletion()
    }
  })
  requestButton.addEventListener('click', requestCompletion)
  document.getElementById('reset-code').addEventListener('click', resetEditor)
  document.getElementById('clear-code').addEventListener('click', () => {
    editor.value = ''
    editor.dispatchEvent(new Event('input'))
    editor.focus()
  })
  document.getElementById('theme-toggle').addEventListener('click', () => {
    setTheme(document.documentElement.getAttribute('data-theme') === 'light' ? 'dark' : 'light')
  })

  resetEditor()
  loadCapabilities()
  window.setTimeout(requestCompletion, 0)
})()
