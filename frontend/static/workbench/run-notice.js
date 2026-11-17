import {node,button} from './dom.js';

export function mountRunNotice(container,{openSettings}) {
  const box=node('div','wb-run-notice'),title=node('strong'),reason=node('p');
  const settings=button('检查模型配置',()=>openSettings());
  box.setAttribute('role','status');box.hidden=true;box.append(title,reason,settings);container.append(box);
  return {update(view){
    const task=view?.task,blocked=task?.run_id&&!['exploration','delegation'].includes(task.purpose)&&(task.workflow_status||task.execution_status)!=='running'&&['已暂停','已阻塞','等你处理','已停止'].includes(task.status);
    box.hidden=!blocked;if(!blocked)return;
    title.textContent='当前流程'+task.status;
    reason.textContent=task.workflow_status?task.current_activity:task.reason||task.current_activity||'当前进度已保存。';
    settings.hidden=!!task.readonly||!/模型配置|模型不可用|配置失效|provider|model|api.?key/i.test(task.reason||'');
  }};
}
