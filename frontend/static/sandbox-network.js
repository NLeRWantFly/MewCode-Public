(function () {
  'use strict';
  function mount(body) {
    const section = document.createElement('section');
    section.className = 'sandbox-network-section';
    const heading = document.createElement('h3'); heading.textContent = '沙箱网络';
    const label = document.createElement('label');
    const enabled = document.createElement('input'); enabled.type = 'checkbox';
    label.append(enabled, document.createTextNode(' 允许网络访问'));
    const help = document.createElement('p');
    help.textContent = '开启后可访问未列入黑名单的目标。设置作用于普通对话和编排任务；关闭任务联网不会中断模型对话。';
    const denyLabel = document.createElement('label'); denyLabel.textContent = '网络黑名单（每行一个域名或 IP）';
    const denied = document.createElement('textarea'); denied.rows = 4;
    denied.placeholder = 'example.com\n*.example.org\n192.0.2.1';
    denyLabel.append(denied);
    const status = document.createElement('p'); status.setAttribute('role', 'status');
    const actions = document.createElement('div'); actions.className = 'sandbox-network-actions';
    const save = document.createElement('button'); save.type = 'button'; save.className = 'btn'; save.textContent = '保存网络设置';
    const configure = document.createElement('button'); configure.type = 'button'; configure.className = 'btn';
    configure.textContent = '配置联网运行器';
    configure.title = '仅首次配置可能出现 Windows 系统安装提示；使用合成材料完成验收。';
    actions.append(save, configure); section.append(heading, label, help, denyLabel, status, actions);
    body.prepend(section);
    let state;
    const display = data => {
      state = data; enabled.checked = data.policy.enabled;
      denied.value = data.policy.denied_domains.join('\n');
      const host = data.effective?.execution_backend === 'host_reviewed';
      status.textContent = !data.policy.enabled ? '任务联网已关闭' : host ?
        '联网已开启，Auto 命令经风险审查运行。' : data.runtime.ready ?
        '联网已开启，命令运行器已就绪' : '联网已开启，网页工具可用；命令联网运行器尚未就绪。';
      if (!host && data.policy.enabled && !data.runtime.ready && data.runtime.reason)
        status.textContent += ' ' + data.runtime.reason.replace(/^NETWORK_RUNTIME_UNAVAILABLE:\s*/, '');
      configure.hidden = host || data.runtime.ready;
    };
    const load = async () => {
      save.disabled = configure.disabled = true;
      try {
        const response = await fetch('/api/chat/sandbox-network', { cache: 'no-store' });
        const data = await response.json();
        if (!response.ok) throw new Error(data.error || '网络设置读取失败');
        display(data);
      } catch (error) { status.textContent = error.message; }
      finally { save.disabled = configure.disabled = !state; }
    };
    const post = async (path, payload) => {
      if (!state) return;
      save.disabled = configure.disabled = true;
      try {
        const response = await fetch('/api/chat/sandbox-network' + path, { method: 'POST',
          headers: { 'Content-Type': 'application/json', 'X-Mew-Sandbox-Control': '1' },
          body: JSON.stringify({ conversation_id: state.conversation_id, ...payload }) });
        const data = await response.json();
        if (!response.ok) throw new Error(data.error || '网络设置保存失败');
        display(data);
      } catch (error) { status.textContent = error.message; }
      finally { save.disabled = configure.disabled = false; }
    };
    save.addEventListener('click', () => post('', { enabled: enabled.checked,
      denied_domains: denied.value.split(/\r?\n/).map(x => x.trim()).filter(Boolean), revision: state.policy.revision }));
    configure.addEventListener('click', () => {
      status.textContent = '正在配置并验收运行器；如出现 Windows 系统安装提示，请完成该提示。';
      void post('/configure', {});
    });
    void load();
  }
  window.MewSandboxNetwork = { mount };
}());
